"""Shared validation and UI choices for archive item date fields."""

from __future__ import annotations

from documents.models import ArchiveItem, Document

DATE_PRECISION_UI_CHOICES = (
    (ArchiveItem.DatePrecision.UNKNOWN, "ללא תאריך"),
    (ArchiveItem.DatePrecision.YEAR, "שנה בלבד"),
    (ArchiveItem.DatePrecision.MONTH, "חודש"),
    (ArchiveItem.DatePrecision.EXACT_DAY, "יום מדויק"),
    (ArchiveItem.DatePrecision.RANGE_YEAR, "טווח שנים"),
    (ArchiveItem.DatePrecision.RANGE_MONTH, "טווח חודשים"),
    (ArchiveItem.DatePrecision.RANGE, "טווח ימים"),
)

TEXT_INPUT_TYPE_UI_CHOICES = (
    (Document.TextInputType.HANDWRITTEN, "כתב יד"),
    (Document.TextInputType.PRINTED, "מודפס"),
    (Document.TextInputType.MIXED, "משולב (מודפס וכתב יד)"),
)

HANDWRITING_TYPE_UI_CHOICES = (
    (Document.HandwritingType.VS, "כתב היד של VS"),
    (Document.HandwritingType.GENERAL, "כתב יד כללי"),
)


def parse_date_precision(raw_value: str | None) -> str:
    value = (raw_value or ArchiveItem.DatePrecision.UNKNOWN).strip().upper()
    valid = {choice.value for choice in ArchiveItem.DatePrecision}
    if value not in valid:
        raise ValueError("date_precision is invalid")
    return value
