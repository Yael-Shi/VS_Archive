"""Deterministic Transkribus cross-snapshot paragraph-boundary correspondence.

Proof only: does not copy, adopt, or write mappings. No fuzzy, AI, geometry,
canonical-text, char-offset, or provider-page-identity matching.

Identity key is ``(page_index, provider_line_id)`` for contributing lines.
PAGE XML TextLine ids are page-scoped; ``TranskribusSnapshotLine.provider_line_id``
has no snapshot-level unique constraint, so page index is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from documents.models import (
    TranskribusParagraphMapping,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.services.transkribus_paragraph_mapping import (
    contributing_lines_for_snapshot,
)


class CorrespondenceRefusal:
    DIFFERENT_DOCUMENT = "DIFFERENT_DOCUMENT"
    SOURCE_NOT_READY = "SOURCE_NOT_READY"
    TARGET_NOT_READY = "TARGET_NOT_READY"
    PARSER_VERSION_MISMATCH = "PARSER_VERSION_MISMATCH"
    PAGE_STRUCTURE_MISMATCH = "PAGE_STRUCTURE_MISMATCH"
    CONTRIBUTING_LINE_COUNT_MISMATCH = "CONTRIBUTING_LINE_COUNT_MISMATCH"
    MISSING_PROVIDER_LINE_ID = "MISSING_PROVIDER_LINE_ID"
    DUPLICATE_PROVIDER_LINE_ID = "DUPLICATE_PROVIDER_LINE_ID"
    IDENTITY_SEQUENCE_MISMATCH = "IDENTITY_SEQUENCE_MISMATCH"


@dataclass(frozen=True)
class CorrespondingContributingLine:
    """Maps one historical contributing line PK to the target snapshot line PK."""

    source_line_id: int
    target_line_id: int
    page_index: int
    provider_line_id: str
    source_order_index: int
    target_order_index: int


@dataclass(frozen=True)
class ContributingLineCorrespondenceProof:
    """Strict fail-closed result of comparing two snapshots' contributing lines."""

    compatible: bool
    refusal_reason: str | None
    source_snapshot_id: int
    target_snapshot_id: int
    line_correspondence: tuple[CorrespondingContributingLine, ...]


@dataclass(frozen=True)
class HistoricalParagraphMappingCandidate:
    """A historical mapping that may be offered later for explicit manager adoption."""

    mapping_id: int
    source_snapshot_id: int
    source_snapshot_created_at: datetime
    break_after_source_line_ids: tuple[int, ...]
    correspondence: ContributingLineCorrespondenceProof


def _ordered_page_indexes(snapshot: TranskribusTranscriptSnapshot) -> tuple[int, ...]:
    return tuple(
        TranskribusSnapshotPage.objects.filter(snapshot_id=snapshot.pk)
        .order_by("page_index")
        .values_list("page_index", flat=True)
    )


def _contributing_identity_rows(
    snapshot: TranskribusTranscriptSnapshot,
) -> tuple[tuple[int, int, str, int], str | None]:
    """Return ``((line_id, page_index, provider_line_id, order_index), ...), refusal``.

    Refusal is ``MISSING_PROVIDER_LINE_ID`` or ``DUPLICATE_PROVIDER_LINE_ID``.
    Uniqueness scope is ``(page_index, provider_line_id)``.
    """
    rows: list[tuple[int, int, str, int]] = []
    seen: set[tuple[int, str]] = set()
    for line in contributing_lines_for_snapshot(snapshot):
        provider_line_id = (line.provider_line_id or "").strip()
        if not provider_line_id:
            return (), CorrespondenceRefusal.MISSING_PROVIDER_LINE_ID
        identity = (line.page.page_index, provider_line_id)
        if identity in seen:
            return (), CorrespondenceRefusal.DUPLICATE_PROVIDER_LINE_ID
        seen.add(identity)
        rows.append(
            (
                int(line.pk),
                line.page.page_index,
                provider_line_id,
                int(line.order_index),
            )
        )
    return tuple(rows), None


def _per_page_contributing_counts(
    identities: tuple[tuple[int, int, str, int], ...],
    page_indexes: tuple[int, ...],
) -> tuple[int, ...]:
    counts = {page_index: 0 for page_index in page_indexes}
    for _line_id, page_index, _provider_line_id, _order_index in identities:
        counts[page_index] = counts.get(page_index, 0) + 1
    return tuple(counts[page_index] for page_index in page_indexes)


def prove_contributing_line_correspondence(
    source: TranskribusTranscriptSnapshot,
    target: TranskribusTranscriptSnapshot,
) -> ContributingLineCorrespondenceProof:
    """Prove that paragraph breaks from ``source`` can be remapped onto ``target``.

    Allows differing line text. Refuses unless both snapshots are READY, same
    document, same parser version, same ordered ``page_index`` structure, same
    per-page contributing-line counts, complete unique
    ``(page_index, provider_line_id)`` identities, and identical ordered
    identity sequences. Fail closed.
    """
    source_id = int(source.pk)
    target_id = int(target.pk)

    def refused(reason: str) -> ContributingLineCorrespondenceProof:
        return ContributingLineCorrespondenceProof(
            compatible=False,
            refusal_reason=reason,
            source_snapshot_id=source_id,
            target_snapshot_id=target_id,
            line_correspondence=(),
        )

    if source.document_id != target.document_id:
        return refused(CorrespondenceRefusal.DIFFERENT_DOCUMENT)
    if source.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        return refused(CorrespondenceRefusal.SOURCE_NOT_READY)
    if target.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        return refused(CorrespondenceRefusal.TARGET_NOT_READY)
    if source.parser_version != target.parser_version:
        return refused(CorrespondenceRefusal.PARSER_VERSION_MISMATCH)

    source_pages = _ordered_page_indexes(source)
    target_pages = _ordered_page_indexes(target)
    if source_pages != target_pages:
        return refused(CorrespondenceRefusal.PAGE_STRUCTURE_MISMATCH)

    source_rows, source_id_reason = _contributing_identity_rows(source)
    if source_id_reason is not None:
        return refused(source_id_reason)
    target_rows, target_id_reason = _contributing_identity_rows(target)
    if target_id_reason is not None:
        return refused(target_id_reason)

    source_counts = _per_page_contributing_counts(source_rows, source_pages)
    target_counts = _per_page_contributing_counts(target_rows, target_pages)
    if source_counts != target_counts:
        return refused(CorrespondenceRefusal.CONTRIBUTING_LINE_COUNT_MISMATCH)

    source_identities = tuple(
        (page_index, provider_line_id)
        for _line_id, page_index, provider_line_id, _order in source_rows
    )
    target_identities = tuple(
        (page_index, provider_line_id)
        for _line_id, page_index, provider_line_id, _order in target_rows
    )
    if source_identities != target_identities:
        return refused(CorrespondenceRefusal.IDENTITY_SEQUENCE_MISMATCH)

    correspondence = tuple(
        CorrespondingContributingLine(
            source_line_id=source_line_id,
            target_line_id=target_line_id,
            page_index=page_index,
            provider_line_id=provider_line_id,
            source_order_index=source_order,
            target_order_index=target_order,
        )
        for (
            source_line_id,
            page_index,
            provider_line_id,
            source_order,
        ), (
            target_line_id,
            _target_page,
            _target_provider_id,
            target_order,
        ) in zip(source_rows, target_rows, strict=True)
    )
    return ContributingLineCorrespondenceProof(
        compatible=True,
        refusal_reason=None,
        source_snapshot_id=source_id,
        target_snapshot_id=target_id,
        line_correspondence=correspondence,
    )


def discover_transferable_historical_mappings(
    target: TranskribusTranscriptSnapshot,
) -> tuple[HistoricalParagraphMappingCandidate, ...]:
    """Read-only suggestions from older snapshots with saved mappings.

    Never writes, adopts, or overwrites a mapping. Includes candidates even when
    ``target`` already has its own mapping. Ordered newest historical snapshot
    to oldest. Structurally incompatible mappings are omitted. Snapshots with
    no mapping are ignored.
    """
    if target.pk is None or target.created_at is None:
        return ()

    historical_mappings = (
        TranskribusParagraphMapping.objects.filter(document_id=target.document_id)
        .exclude(snapshot_id=target.pk)
        .filter(snapshot__created_at__lt=target.created_at)
        .select_related("snapshot")
        .prefetch_related("breaks")
        .order_by("-snapshot__created_at", "-snapshot_id")
    )

    candidates: list[HistoricalParagraphMappingCandidate] = []
    for mapping in historical_mappings:
        proof = prove_contributing_line_correspondence(mapping.snapshot, target)
        if not proof.compatible:
            continue
        break_after_ids = tuple(
            break_row.after_line_id
            for break_row in sorted(mapping.breaks.all(), key=lambda row: int(row.pk))
        )
        candidates.append(
            HistoricalParagraphMappingCandidate(
                mapping_id=int(mapping.pk),
                source_snapshot_id=int(mapping.snapshot_id),
                source_snapshot_created_at=mapping.snapshot.created_at,
                break_after_source_line_ids=break_after_ids,
                correspondence=proof,
            )
        )
    return tuple(candidates)


__all__ = [
    "ContributingLineCorrespondenceProof",
    "CorrespondingContributingLine",
    "CorrespondenceRefusal",
    "HistoricalParagraphMappingCandidate",
    "discover_transferable_historical_mappings",
    "prove_contributing_line_correspondence",
]
