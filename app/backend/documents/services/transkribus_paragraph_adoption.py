"""Explicit manager adoption of a historical Transkribus paragraph mapping.

Create-only for the current target snapshot. Re-proves PR1 correspondence,
remaps break-after line PKs, and writes ``copied_from``. Never mutates the
source mapping. Does not replace an existing target mapping.
"""

from __future__ import annotations

from typing import Sequence

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from documents.models import (
    TranskribusParagraphMapping,
    TranskribusTranscriptSnapshot,
)
from documents.services.transkribus_paragraph_correspondence import (
    ContributingLineCorrespondenceProof,
    prove_contributing_line_correspondence,
)
from documents.services.transkribus_paragraph_mapping import (
    TranskribusParagraphMappingError,
    save_paragraph_mapping,
)


class ParagraphMappingAdoptionRefusal:
    DIFFERENT_DOCUMENT = "DIFFERENT_DOCUMENT"
    SOURCE_NOT_HISTORICAL = "SOURCE_NOT_HISTORICAL"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    TARGET_MAPPING_EXISTS = "TARGET_MAPPING_EXISTS"
    CORRESPONDENCE_UNPROVEN = "CORRESPONDENCE_UNPROVEN"
    BREAK_REMAP_FAILED = "BREAK_REMAP_FAILED"


class ParagraphMappingAdoptionError(ValueError):
    """Fail-closed historical paragraph adoption refusal."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def remap_break_after_line_ids(
    proof: ContributingLineCorrespondenceProof,
    source_break_after_line_ids: Sequence[int],
) -> list[int]:
    """Map historical break-after line PKs onto the proven target snapshot."""
    if not proof.compatible:
        raise ParagraphMappingAdoptionError(
            ParagraphMappingAdoptionRefusal.CORRESPONDENCE_UNPROVEN
        )
    by_source = {
        row.source_line_id: row.target_line_id for row in proof.line_correspondence
    }
    remapped: list[int] = []
    seen: set[int] = set()
    for raw_id in source_break_after_line_ids:
        source_line_id = int(raw_id)
        target_line_id = by_source.get(source_line_id)
        if target_line_id is None:
            raise ParagraphMappingAdoptionError(
                ParagraphMappingAdoptionRefusal.BREAK_REMAP_FAILED
            )
        if target_line_id in seen:
            raise ParagraphMappingAdoptionError(
                ParagraphMappingAdoptionRefusal.BREAK_REMAP_FAILED
            )
        seen.add(target_line_id)
        remapped.append(int(target_line_id))
    return remapped


def _source_break_after_line_ids(
    mapping: TranskribusParagraphMapping,
) -> list[int]:
    return [
        int(break_row.after_line_id)
        for break_row in sorted(mapping.breaks.all(), key=lambda row: int(row.pk))
    ]


def _require_historical_source(
    source_mapping: TranskribusParagraphMapping,
    target: TranskribusTranscriptSnapshot,
) -> None:
    if source_mapping.document_id != target.document_id:
        raise ParagraphMappingAdoptionError(
            ParagraphMappingAdoptionRefusal.DIFFERENT_DOCUMENT
        )
    source_snapshot = source_mapping.snapshot
    if (
        source_snapshot.pk == target.pk
        or source_snapshot.created_at is None
        or target.created_at is None
        or source_snapshot.created_at >= target.created_at
    ):
        raise ParagraphMappingAdoptionError(
            ParagraphMappingAdoptionRefusal.SOURCE_NOT_HISTORICAL
        )


def adopt_historical_paragraph_mapping(
    source_mapping: TranskribusParagraphMapping,
    target: TranskribusTranscriptSnapshot,
    *,
    actor: User | None = None,
) -> TranskribusParagraphMapping:
    """Create a new mapping on ``target`` from a proven historical source.

    Refuses if ``target`` already has a mapping. Source rows are not updated.
    """
    if source_mapping.pk is None or target.pk is None:
        raise ParagraphMappingAdoptionError(
            ParagraphMappingAdoptionRefusal.SOURCE_UNAVAILABLE
        )

    try:
        with transaction.atomic():
            locked_target = (
                TranskribusTranscriptSnapshot.objects.select_for_update().get(
                    pk=target.pk
                )
            )
            existing = (
                TranskribusParagraphMapping.objects.select_for_update()
                .filter(snapshot_id=locked_target.pk)
                .first()
            )
            if existing is not None:
                raise ParagraphMappingAdoptionError(
                    ParagraphMappingAdoptionRefusal.TARGET_MAPPING_EXISTS
                )

            try:
                locked_source = (
                    TranskribusParagraphMapping.objects.select_for_update()
                    .select_related("snapshot")
                    .prefetch_related("breaks")
                    .get(pk=source_mapping.pk)
                )
            except TranskribusParagraphMapping.DoesNotExist as exc:
                raise ParagraphMappingAdoptionError(
                    ParagraphMappingAdoptionRefusal.SOURCE_UNAVAILABLE
                ) from exc

            _require_historical_source(locked_source, locked_target)
            proof = prove_contributing_line_correspondence(
                locked_source.snapshot,
                locked_target,
            )
            if not proof.compatible:
                raise ParagraphMappingAdoptionError(
                    ParagraphMappingAdoptionRefusal.CORRESPONDENCE_UNPROVEN
                )
            source_break_ids = _source_break_after_line_ids(locked_source)
            target_break_ids = remap_break_after_line_ids(proof, source_break_ids)

            try:
                return save_paragraph_mapping(
                    locked_target,
                    target_break_ids,
                    actor=actor,
                    copied_from=locked_source,
                    create_only=True,
                )
            except IntegrityError as exc:
                raise ParagraphMappingAdoptionError(
                    ParagraphMappingAdoptionRefusal.TARGET_MAPPING_EXISTS
                ) from exc
            except TranskribusParagraphMappingError as exc:
                if "already has a paragraph mapping" in str(exc):
                    raise ParagraphMappingAdoptionError(
                        ParagraphMappingAdoptionRefusal.TARGET_MAPPING_EXISTS
                    ) from exc
                raise ParagraphMappingAdoptionError(
                    ParagraphMappingAdoptionRefusal.BREAK_REMAP_FAILED
                ) from exc
    except TranskribusTranscriptSnapshot.DoesNotExist as exc:
        raise ParagraphMappingAdoptionError(
            ParagraphMappingAdoptionRefusal.SOURCE_UNAVAILABLE
        ) from exc


__all__ = [
    "ParagraphMappingAdoptionError",
    "ParagraphMappingAdoptionRefusal",
    "adopt_historical_paragraph_mapping",
    "remap_break_after_line_ids",
]
