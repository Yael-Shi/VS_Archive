"""Shared server-side validation for archive metadata fields (no body)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from documents.models import ArchiveItem
from documents.services.archive_date_input import (
    DATE_COMPONENT_FIELD_NAMES,
    archive_date_entry_flags,
    archive_date_form_data,
    empty_date_component_form_data,
    parse_archive_date_bounds,
    scalar_post_field,
)
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


SOURCE_METADATA_MAX_LENGTH = 255

_EMPTY_METADATA_MARKERS = frozenset(
    {
        "אין",
        "—",
        "-",
        "–",
        "none",
        "n/a",
        "na",
        "null",
        "ללא",
    }
)


def meaningful_metadata_value(value: Any) -> str:
    """Return displayable metadata text, or empty when missing/placeholder."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    normalized = text.casefold()
    if normalized in _EMPTY_METADATA_MARKERS:
        return ""
    if normalized.replace(" ", "") in _EMPTY_METADATA_MARKERS:
        return ""
    return text


def parse_public_note(raw_value: Any) -> str:
    if raw_value is None:
        return ""
    return str(raw_value)


def validate_source_metadata_fields(
    *,
    author_name: str,
    source_title: str,
) -> list[str]:
    """Return user-facing validation errors for ArchiveItem source metadata fields."""
    errors: list[str] = []
    if len(author_name) > SOURCE_METADATA_MAX_LENGTH:
        errors.append("מחבר/ת חייב להיות עד 255 תווים")
    if len(source_title) > SOURCE_METADATA_MAX_LENGTH:
        errors.append("מקור חייב להיות עד 255 תווים")
    return errors


def validate_archive_metadata_fields(
    *,
    title: str,
    visibility: str,
    metadata_status: str,
    date_precision: str,
    date_start: date | None,
    date_end: date | None,
    author_name: str = "",
    source_title: str = "",
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

    errors.extend(
        validate_source_metadata_fields(
            author_name=author_name,
            source_title=source_title,
        )
    )

    return errors


def _base_form_data_from_post(post_data: dict[str, Any]) -> dict[str, Any]:
    component_values = {
        name: scalar_post_field(post_data, name) for name in DATE_COMPONENT_FIELD_NAMES
    }
    return {
        "title": (post_data.get("title") or "").strip(),
        "visibility": (post_data.get("visibility") or "").strip(),
        "date_precision": (post_data.get("date_precision") or "").strip(),
        "metadata_status": (post_data.get("metadata_status") or "").strip(),
        "author_name": (post_data.get("author_name") or "").strip(),
        "source_title": (post_data.get("source_title") or "").strip(),
        "public_note": parse_public_note(post_data.get("public_note")),
        "date_start": scalar_post_field(post_data, "date_start"),
        "date_end": scalar_post_field(post_data, "date_end"),
        **component_values,
    }


def parse_archive_metadata_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST fields for shared archive metadata and return normalized data plus errors."""
    errors: list[str] = []
    date_components = empty_date_component_form_data()
    date_start: date | None = None
    date_end: date | None = None
    visibility: str = ArchiveItem.Visibility.PRIVATE
    metadata_status: str = ArchiveItem.MetadataStatus.NEEDS_COMPLETION
    date_precision: str = ArchiveItem.DatePrecision.UNKNOWN
    try:
        form_data = _base_form_data_from_post(post_data)
    except ValueError as exc:
        errors.append(str(exc))
        form_data = {
            "title": (post_data.get("title") or "").strip(),
            "visibility": (post_data.get("visibility") or "").strip(),
            "date_precision": (post_data.get("date_precision") or "").strip(),
            "metadata_status": (post_data.get("metadata_status") or "").strip(),
            "author_name": (post_data.get("author_name") or "").strip(),
            "source_title": (post_data.get("source_title") or "").strip(),
            "public_note": parse_public_note(post_data.get("public_note")),
            "date_start": "",
            "date_end": "",
            **date_components,
        }
        date_precision = (
            form_data["date_precision"] or ArchiveItem.DatePrecision.UNKNOWN
        )
        parsed = {
            **form_data,
            **date_components,
            **archive_date_entry_flags(date_precision),
            "visibility": form_data["visibility"] or ArchiveItem.Visibility.PRIVATE,
            "metadata_status": form_data["metadata_status"]
            or ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": date_precision,
            "date_start": "",
            "date_end": "",
            "date_start_value": None,
            "date_end_value": None,
        }
        return parsed, errors

    try:
        visibility = parse_visibility(form_data["visibility"])
        metadata_status = parse_metadata_status(form_data["metadata_status"])
        date_precision = parse_date_precision(form_data["date_precision"])
    except ValueError as exc:
        errors.append(str(exc))

    if not errors:
        date_start, date_end, date_components, date_errors = parse_archive_date_bounds(
            date_precision=date_precision,
            post_data=post_data,
        )
        errors.extend(date_errors)

    if not errors:
        errors.extend(
            validate_archive_metadata_fields(
                title=form_data["title"],
                visibility=visibility,
                metadata_status=metadata_status,
                date_precision=date_precision,
                date_start=date_start,
                date_end=date_end,
                author_name=form_data["author_name"],
                source_title=form_data["source_title"],
            )
        )

    parsed = {
        **form_data,
        **date_components,
        **archive_date_entry_flags(date_precision),
        "visibility": visibility,
        "metadata_status": metadata_status,
        "date_precision": date_precision,
        "date_start": date_start.isoformat() if date_start else "",
        "date_end": date_end.isoformat() if date_end else "",
        "date_start_value": date_start,
        "date_end_value": date_end,
    }
    return parsed, errors


def archive_metadata_form_data_for_template(
    *,
    title: str,
    visibility: str,
    date_start: date | None,
    date_end: date | None,
    date_precision: str,
    metadata_status: str,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
) -> dict[str, Any]:
    return {
        "title": title,
        "visibility": visibility,
        "metadata_status": metadata_status,
        "author_name": author_name,
        "source_title": source_title,
        "public_note": public_note,
        **archive_date_form_data(
            date_start=date_start,
            date_end=date_end,
            date_precision=date_precision,
        ),
    }
