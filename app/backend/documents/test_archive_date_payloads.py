"""Shared date POST payload helpers for archive metadata form tests."""

from __future__ import annotations

from typing import Any

from documents.models import ArchiveItem
from documents.services.archive_date_input import DATE_COMPONENT_FIELD_NAMES


def _strip(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def post_data_has_date_input(post_data: dict[str, Any]) -> bool:
    """True when a test/API payload includes legacy ISO and/or segmented date values."""
    if _strip(post_data.get("date_start")):
        return True
    if _strip(post_data.get("date_end")):
        return True
    return any(_strip(post_data.get(name)) for name in DATE_COMPONENT_FIELD_NAMES)


def default_date_post_fields_for_precision(date_precision: str) -> dict[str, str]:
    """Fixture segmented POST fields for complete archive metadata test submissions."""
    if date_precision == ArchiveItem.DatePrecision.UNKNOWN:
        return {}
    if date_precision == ArchiveItem.DatePrecision.YEAR:
        return {"date_start_year": "1948"}
    if date_precision == ArchiveItem.DatePrecision.MONTH:
        return {"date_start_year": "1948", "date_start_month": "5"}
    if date_precision == ArchiveItem.DatePrecision.EXACT_DAY:
        return {
            "date_start_year": "1948",
            "date_start_month": "5",
            "date_start_day": "12",
        }
    if date_precision == ArchiveItem.DatePrecision.RANGE:
        return {
            "date_start_year": "1947",
            "date_start_month": "1",
            "date_start_day": "1",
            "date_end_year": "1949",
            "date_end_month": "12",
            "date_end_day": "31",
        }
    if date_precision == ArchiveItem.DatePrecision.RANGE_MONTH:
        return {
            "date_start_year": "2021",
            "date_start_month": "12",
            "date_end_year": "2022",
            "date_end_month": "2",
        }
    if date_precision == ArchiveItem.DatePrecision.RANGE_YEAR:
        return {"date_start_year": "1953", "date_end_year": "1954"}
    return {}


def merge_default_date_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Return ``payload`` with fixture date fields when precision requires dates."""
    merged = dict(payload)
    if not post_data_has_date_input(merged):
        merged.update(default_date_post_fields_for_precision(merged["date_precision"]))
    return merged
