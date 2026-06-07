"""Shared server-side validation for archive metadata fields (no body)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from documents.models import ArchiveItem
from documents.services.archive_item_validation import parse_date_precision


def parse_optional_date(value: str | None, field_name: str) -> date | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid {field_name} format, expected YYYY-MM-DD") from None


def parse_visibility(raw_value: str | None, *, default: str | None = None) -> str:
    value = (raw_value or default or ArchiveItem.Visibility.PRIVATE).strip().lower()
    valid = {choice.value for choice in ArchiveItem.Visibility}
    if value not in valid:
        raise ValueError("visibility is invalid")
    return value


def parse_metadata_status(raw_value: str | None) -> str:
    value = (raw_value or ArchiveItem.MetadataStatus.NEEDS_COMPLETION).strip().upper()
    valid = {choice.value for choice in ArchiveItem.MetadataStatus}
    if value not in valid:
        raise ValueError("metadata_status is invalid")
    return value


def validate_archive_metadata_fields(
    *,
    title: str,
    visibility: str,
    metadata_status: str,
    date_precision: str,
    date_start: date | None,
    date_end: date | None,
) -> list[str]:
    """Return a list of user-facing validation error messages."""
    errors: list[str] = []

    if not title or not title.strip():
        errors.append("title is required")

    valid_visibility = {choice.value for choice in ArchiveItem.Visibility}
    if visibility not in valid_visibility:
        errors.append("visibility is invalid")

    valid_metadata_status = {choice.value for choice in ArchiveItem.MetadataStatus}
    if metadata_status not in valid_metadata_status:
        errors.append("metadata_status is invalid")

    valid_date_precision = {choice.value for choice in ArchiveItem.DatePrecision}
    if date_precision not in valid_date_precision:
        errors.append("date_precision is invalid")

    if date_start and date_end and date_end < date_start:
        errors.append("date_end must not be before date_start")

    return errors


def parse_archive_metadata_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST fields for shared archive metadata and return normalized data plus errors."""
    form_data = {
        "title": (post_data.get("title") or "").strip(),
        "visibility": (post_data.get("visibility") or "").strip(),
        "date_start": (post_data.get("date_start") or "").strip(),
        "date_end": (post_data.get("date_end") or "").strip(),
        "date_precision": (post_data.get("date_precision") or "").strip(),
        "metadata_status": (post_data.get("metadata_status") or "").strip(),
    }

    errors: list[str] = []
    date_start = None
    date_end = None
    visibility = ArchiveItem.Visibility.PRIVATE
    metadata_status = ArchiveItem.MetadataStatus.NEEDS_COMPLETION
    date_precision = ArchiveItem.DatePrecision.UNKNOWN

    try:
        visibility = parse_visibility(form_data["visibility"])
        metadata_status = parse_metadata_status(form_data["metadata_status"])
        date_precision = parse_date_precision(form_data["date_precision"])
        date_start = parse_optional_date(form_data["date_start"], "date_start")
        date_end = parse_optional_date(form_data["date_end"], "date_end")
    except ValueError as exc:
        errors.append(str(exc))

    if not errors:
        errors.extend(
            validate_archive_metadata_fields(
                title=form_data["title"],
                visibility=visibility,
                metadata_status=metadata_status,
                date_precision=date_precision,
                date_start=date_start,
                date_end=date_end,
            )
        )

    parsed = {
        **form_data,
        "visibility": visibility,
        "metadata_status": metadata_status,
        "date_precision": date_precision,
        "date_start_value": date_start,
        "date_end_value": date_end,
    }
    return parsed, errors
