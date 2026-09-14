"""Distinct ArchiveItem holdings counts by item type for public directory rows."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from documents.models import ArchiveItem

DIRECTORY_HOLDINGS_TYPE_ORDER: tuple[str, ...] = (
    ArchiveItem.ItemType.MANUAL_TEXT,
    ArchiveItem.ItemType.OCR_DOCUMENT,
    ArchiveItem.ItemType.PHOTO,
    ArchiveItem.ItemType.VIDEO,
)

DIRECTORY_HOLDINGS_LABELS: dict[str, tuple[str, str]] = {
    ArchiveItem.ItemType.MANUAL_TEXT: ("טקסט", "טקסטים"),
    ArchiveItem.ItemType.OCR_DOCUMENT: ("מסמך", "מסמכים"),
    ArchiveItem.ItemType.PHOTO: ("תמונה", "תמונות"),
    ArchiveItem.ItemType.VIDEO: ("קטע וידאו", "קטעי וידאו"),
}

HOLDINGS_SUMMARY_SEPARATOR = " · "


def format_holdings_type_label(*, item_type: str, count: int) -> str:
    """Hebrew singular/plural label for one ArchiveItem type count."""
    singular, plural = DIRECTORY_HOLDINGS_LABELS[item_type]
    if count == 1:
        return f"1 {singular}"
    return f"{count} {plural}"


def ordered_nonzero_type_counts(
    by_type: Mapping[str, int],
) -> tuple[tuple[str, int], ...]:
    """Return nonzero ``(item_type, count)`` pairs in directory type order."""
    ordered: list[tuple[str, int]] = []
    for item_type in DIRECTORY_HOLDINGS_TYPE_ORDER:
        count = int(by_type.get(item_type, 0) or 0)
        if count > 0:
            ordered.append((item_type, count))
    return tuple(ordered)


def format_holdings_summary(by_type: Mapping[str, int]) -> str:
    """Join nonzero type labels with `` · `` in stable type order."""
    segments = [
        format_holdings_type_label(item_type=item_type, count=count)
        for item_type, count in ordered_nonzero_type_counts(by_type)
    ]
    return HOLDINGS_SUMMARY_SEPARATOR.join(segments)


@dataclass(frozen=True, slots=True)
class PublicHoldingsCounts:
    """Distinct ArchiveItem totals and per-type counts for one directory identity."""

    total: int
    by_type: dict[str, int]

    @property
    def type_counts(self) -> tuple[tuple[str, int], ...]:
        return ordered_nonzero_type_counts(self.by_type)

    @property
    def summary(self) -> str:
        return format_holdings_summary(self.by_type)


EMPTY_PUBLIC_HOLDINGS = PublicHoldingsCounts(total=0, by_type={})


def holdings_from_item_type_pairs(
    pairs: Iterable[tuple[int, int, str]],
) -> dict[int, PublicHoldingsCounts]:
    """Deduplicate ``(identity_id, archive_item_id)`` then count by item type.

    First occurrence of an ArchiveItem id for an identity keeps that row's
    ``item_type``. Duplicate pairs (AIP+PhotoPerson, multiple photos, overlapping
    AIA) do not increment the count.
    """
    seen: dict[tuple[int, int], str] = {}
    for identity_id, item_id, raw_item_type in pairs:
        key = (identity_id, item_id)
        if key in seen:
            continue
        seen[key] = str(raw_item_type or "")

    by_identity: dict[int, dict[str, int]] = {}
    for (identity_id, _item_id), item_type in seen.items():
        type_counts = by_identity.setdefault(identity_id, {})
        type_counts[item_type] = type_counts.get(item_type, 0) + 1

    return {
        identity_id: PublicHoldingsCounts(
            total=sum(type_counts.values()),
            by_type=type_counts,
        )
        for identity_id, type_counts in by_identity.items()
    }
