"""Staff-authored Transkribus paragraph presentation metadata.

Presentation only: mappings never copy or mutate canonical transcription,
snapshot line text, geometry, char offsets, or hover IDs.

A mapping row with zero break rows is an explicit one-paragraph save.
No mapping row means paragraph grouping has never been saved for that snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from django.contrib.auth.models import User
from django.db import transaction

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusParagraphBreak,
    TranskribusParagraphMapping,
    TranskribusSnapshotLine,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.text_presentation import resolve_displayed_transcription_result
from documents.services.transkribus_binding_freshness import (
    is_binding_structurally_fresh,
)


class TranskribusParagraphMappingError(ValueError):
    """Invalid paragraph mapping or break-after-line set."""


@dataclass(frozen=True)
class ParagraphMappingCurrentness:
    """Whether a saved mapping currently applies to the displayed transcription."""

    has_mapping: bool
    is_current: bool
    mapping: TranskribusParagraphMapping | None
    displayed_text_result_id: int | None
    bound_snapshot_id: int | None
    mapping_snapshot_id: int | None
    is_structurally_fresh: bool


def contributing_lines_for_snapshot(
    snapshot: TranskribusTranscriptSnapshot,
) -> list[TranskribusSnapshotLine]:
    """Ordered contributing source lines for ``snapshot`` (page, then order)."""
    return list(
        TranskribusSnapshotLine.objects.filter(
            page__snapshot_id=snapshot.pk,
            contributes_to_canonical=True,
        )
        .select_related("page")
        .order_by("page__page_index", "order_index")
    )


def get_paragraph_mapping_for_snapshot(
    snapshot: TranskribusTranscriptSnapshot,
) -> TranskribusParagraphMapping | None:
    """Return the mapping saved for ``snapshot``, or None if never saved."""
    return (
        TranskribusParagraphMapping.objects.filter(snapshot_id=snapshot.pk)
        .select_related("snapshot", "document", "copied_from")
        .first()
    )


def validate_break_after_lines(
    snapshot: TranskribusTranscriptSnapshot,
    after_line_ids: Sequence[int],
) -> list[TranskribusSnapshotLine]:
    """Resolve and validate break-after contributing lines for ``snapshot``.

    Rejects unknown lines, other-snapshot lines, non-contributing lines,
    the final contributing line, and duplicate identities.
    """
    contributing = contributing_lines_for_snapshot(snapshot)
    contributing_ids = {line.pk for line in contributing}
    final_line_id = contributing[-1].pk if contributing else None
    resolved: list[TranskribusSnapshotLine] = []
    seen: set[int] = set()

    for raw_id in after_line_ids:
        line_id = int(raw_id)
        if line_id in seen:
            raise TranskribusParagraphMappingError(
                f"Duplicate paragraph break after_line_id={line_id}."
            )
        seen.add(line_id)

        if line_id in contributing_ids:
            if final_line_id is not None and line_id == final_line_id:
                raise TranskribusParagraphMappingError(
                    "A paragraph break after the final contributing source line "
                    "is not meaningful."
                )
            line = next(item for item in contributing if item.pk == line_id)
            resolved.append(line)
            continue

        line = (
            TranskribusSnapshotLine.objects.filter(pk=line_id)
            .select_related("page")
            .first()
        )
        if line is None:
            raise TranskribusParagraphMappingError(
                f"Unknown snapshot line id={line_id} for paragraph break."
            )
        if line.page.snapshot_id != snapshot.pk:
            raise TranskribusParagraphMappingError(
                "Paragraph break after_line must belong to the mapping's snapshot."
            )
        if not line.contributes_to_canonical:
            raise TranskribusParagraphMappingError(
                "Paragraph break after_line must be a contributing source line."
            )
        raise TranskribusParagraphMappingError(
            f"Paragraph break after_line_id={line_id} is not a contributing "
            "source line of this snapshot."
        )

    return resolved


def save_paragraph_mapping(
    snapshot: TranskribusTranscriptSnapshot,
    break_after_line_ids: Sequence[int],
    *,
    actor: User | None = None,
    copied_from: TranskribusParagraphMapping | None = None,
) -> TranskribusParagraphMapping:
    """Create or replace the mapping for ``snapshot`` transactionally.

    Re-saving replaces break rows for this snapshot only. Other snapshots'
    mappings are left intact. Does not mutate canonical text or snapshot lines.

    ``actor`` may be omitted on create. On resave, a provided ``actor`` updates
    ``updated_by``; ``actor=None`` leaves the existing editor unchanged.

    ``copied_from`` is the source of the *current* saved paragraph division,
    not permanent ancestry. Pass a historical mapping for an explicit future
    adoption write. Omit it on an ordinary/manual save: create then stores
    null, and resave clears any previous ``copied_from``. This service does
    not perform adoption; there is no adoption UI in PR1.
    """
    if snapshot.pk is None:
        raise TranskribusParagraphMappingError(
            "Cannot save a paragraph mapping for an unsaved snapshot."
        )

    with transaction.atomic():
        locked_snapshot = TranskribusTranscriptSnapshot.objects.select_for_update().get(
            pk=snapshot.pk
        )
        validated_lines = validate_break_after_lines(
            locked_snapshot,
            break_after_line_ids,
        )
        mapping = (
            TranskribusParagraphMapping.objects.select_for_update()
            .filter(snapshot_id=locked_snapshot.pk)
            .first()
        )
        if mapping is None:
            mapping = TranskribusParagraphMapping(
                snapshot=locked_snapshot,
                document_id=locked_snapshot.document_id,
                copied_from=copied_from,
                created_by=actor,
                updated_by=actor,
            )
            mapping.save()
        else:
            mapping.copied_from = copied_from
            if actor is not None:
                mapping.updated_by = actor
            mapping.document_id = locked_snapshot.document_id
            mapping.save()

        mapping.breaks.all().delete()
        for line in validated_lines:
            break_row = TranskribusParagraphBreak(
                mapping=mapping,
                after_line=line,
            )
            break_row.save()

        return (
            TranskribusParagraphMapping.objects.select_related(
                "snapshot",
                "document",
                "copied_from",
            )
            .prefetch_related("breaks")
            .get(pk=mapping.pk)
        )


def _binding_for_text_result(
    text_result: DocumentTextResult,
) -> TranskribusTextResultBinding | None:
    return (
        TranskribusTextResultBinding.objects.filter(text_result_id=text_result.pk)
        .select_related("snapshot")
        .first()
    )


def assess_paragraph_mapping_currentness(
    document: Document,
    *,
    mapping: TranskribusParagraphMapping | None = None,
) -> ParagraphMappingCurrentness:
    """Whether a saved mapping currently applies to the displayed transcription.

    Currentness follows the existing Transkribus baseline: displayed
    ``DocumentTextResult``, a ``TranskribusTextResultBinding``, structural
    freshness, and ``binding.snapshot_id == mapping.snapshot_id``.

    Does **not** require ``hover_eligible``. A local text/revision edit makes
    the mapping non-current (freshness fails) but does not delete it. A new
    snapshot/rebinding makes an old mapping non-current because it belongs to
    another snapshot.

    When ``mapping`` is omitted, the mapping for the currently bound snapshot
    is used. No mapping row is distinct from a valid zero-break mapping.
    """
    displayed = resolve_displayed_transcription_result(document)
    displayed_id = int(displayed.pk) if displayed is not None else None
    binding = _binding_for_text_result(displayed) if displayed is not None else None
    bound_snapshot_id = int(binding.snapshot_id) if binding is not None else None
    structurally_fresh = bool(
        displayed is not None
        and binding is not None
        and is_binding_structurally_fresh(displayed, binding=binding)
    )

    resolved_mapping = mapping
    if resolved_mapping is None and bound_snapshot_id is not None:
        resolved_mapping = get_paragraph_mapping_for_snapshot(binding.snapshot)

    mapping_snapshot_id = (
        int(resolved_mapping.snapshot_id) if resolved_mapping is not None else None
    )
    is_current = bool(
        resolved_mapping is not None
        and binding is not None
        and structurally_fresh
        and bound_snapshot_id == mapping_snapshot_id
    )
    return ParagraphMappingCurrentness(
        has_mapping=resolved_mapping is not None,
        is_current=is_current,
        mapping=resolved_mapping,
        displayed_text_result_id=displayed_id,
        bound_snapshot_id=bound_snapshot_id,
        mapping_snapshot_id=mapping_snapshot_id,
        is_structurally_fresh=structurally_fresh,
    )


__all__ = [
    "ParagraphMappingCurrentness",
    "TranskribusParagraphMappingError",
    "assess_paragraph_mapping_currentness",
    "contributing_lines_for_snapshot",
    "get_paragraph_mapping_for_snapshot",
    "save_paragraph_mapping",
    "validate_break_after_lines",
]
