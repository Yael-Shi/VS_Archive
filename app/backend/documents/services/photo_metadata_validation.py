"""Parse and normalize PHOTO-specific metadata fields on PhotoContent."""

from __future__ import annotations

from typing import Any

from documents.services.archive_metadata_validation import (
    parse_archive_metadata_form,
    validate_archive_metadata_fields,
)

PHOTO_METADATA_FIELD_NAMES: tuple[str, ...] = (
    "description",
    "location",
    "context",
    "people_present",
    "notes",
)


def normalize_photo_metadata_text(value: Any) -> str:
    return (value or "").strip()


def empty_photo_metadata_form_data() -> dict[str, str]:
    return {name: "" for name in PHOTO_METADATA_FIELD_NAMES}


def photo_metadata_from_mapping(data: dict[str, Any]) -> dict[str, str]:
    return {
        name: normalize_photo_metadata_text(data.get(name))
        for name in PHOTO_METADATA_FIELD_NAMES
    }


def photo_metadata_form_data_from_content(photo_content) -> dict[str, str]:
    if photo_content is None:
        return empty_photo_metadata_form_data()
    return {
        name: normalize_photo_metadata_text(getattr(photo_content, name, ""))
        for name in PHOTO_METADATA_FIELD_NAMES
    }


def parse_photo_metadata_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    parsed = photo_metadata_from_mapping(post_data)
    return parsed, []


def _photo_staff_form_data(
    parsed_shared: dict[str, Any], photo_metadata: dict[str, str]
) -> dict[str, Any]:
    return {
        key: value for key, value in parsed_shared.items() if not key.endswith("_value")
    } | photo_metadata


def parse_photo_staff_metadata_form(
    post_data: dict[str, Any],
    *,
    user=None,
) -> tuple[dict[str, Any], list[str]]:
    """
    Parse shared ArchiveItem fields and PhotoContent metadata for PHOTO staff forms.

    Does not read or validate author_name/source_title.
    """
    parsed_shared, errors = parse_archive_metadata_form(post_data, user=user)
    photo_metadata, photo_errors = parse_photo_metadata_form(post_data)
    errors = errors + photo_errors

    if errors:
        return _photo_staff_form_data(parsed_shared, photo_metadata), errors

    shared_errors = validate_archive_metadata_fields(
        title=parsed_shared["title"],
        visibility=parsed_shared["visibility"],
        metadata_status=parsed_shared["metadata_status"],
        date_precision=parsed_shared["date_precision"],
        date_start=parsed_shared["date_start_value"],
        date_end=parsed_shared["date_end_value"],
        user=user,
    )
    if shared_errors:
        return _photo_staff_form_data(parsed_shared, photo_metadata), shared_errors

    parsed = _photo_staff_form_data(parsed_shared, photo_metadata)
    parsed["date_start_value"] = parsed_shared["date_start_value"]
    parsed["date_end_value"] = parsed_shared["date_end_value"]
    return parsed, []
