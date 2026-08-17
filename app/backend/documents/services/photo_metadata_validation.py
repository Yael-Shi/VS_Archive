"""Parse and normalize PHOTO-specific metadata fields on PhotoContent."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from documents.services.archive_date_input import (
    archive_date_form_data,
    parse_archive_date_bounds,
)
from documents.services.archive_item_validation import (
    parse_date_precision,
    validate_stored_archive_date_fields,
)
from documents.services.archive_metadata_validation import (
    parse_archive_metadata_form,
    validate_archive_metadata_fields,
)
from documents.services.photo_content_management import (
    parse_new_person_name,
    parse_photo_person_ids,
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


def parse_photo_content_date_fields(
    *,
    date_start,
    date_end,
    date_precision: str,
) -> str | None:
    try:
        validate_stored_archive_date_fields(
            date_start=date_start,
            date_end=date_end,
            date_precision=date_precision,
        )
    except ValidationError as exc:
        messages = []
        if hasattr(exc, "message_dict"):
            for values in exc.message_dict.values():
                messages.extend(values)
        else:
            messages.extend(exc.messages)
        return messages[0] if messages else "date_precision is invalid"
    return None


def photo_content_staff_form_data(photo_content) -> dict[str, Any]:
    selected_ids: list[int] = []
    if photo_content is not None:
        selected_ids = list(photo_content.people.values_list("id", flat=True))
    return {
        **photo_metadata_form_data_from_content(photo_content),
        **archive_date_form_data(
            date_start=getattr(photo_content, "date_start", None),
            date_end=getattr(photo_content, "date_end", None),
            date_precision=getattr(photo_content, "date_precision", "UNKNOWN")
            or "UNKNOWN",
        ),
        "person_ids": selected_ids,
        "new_person_name": "",
    }


def parse_photo_content_staff_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse per-photo metadata, dates, and identified people. No ArchiveItem fields."""
    photo_metadata, photo_errors = parse_photo_metadata_form(post_data)
    person_ids, person_errors = parse_photo_person_ids(post_data)
    new_person_name, name_errors = parse_new_person_name(post_data)
    errors = photo_errors + person_errors + name_errors

    date_precision = "UNKNOWN"
    date_start = None
    date_end = None
    try:
        date_precision = parse_date_precision(post_data.get("date_precision"))
    except ValueError as exc:
        errors.append(str(exc))

    if not errors:
        date_start, date_end, date_components, date_errors = parse_archive_date_bounds(
            date_precision=date_precision,
            post_data=post_data,
        )
        errors.extend(date_errors)
        date_components = {
            **date_components,
            **archive_date_form_data(
                date_start=date_start,
                date_end=date_end,
                date_precision=date_precision,
            ),
        }
    else:
        date_components = archive_date_form_data(
            date_start=None,
            date_end=None,
            date_precision=date_precision,
        )

    if not errors:
        date_error = parse_photo_content_date_fields(
            date_start=date_start,
            date_end=date_end,
            date_precision=date_precision,
        )
        if date_error:
            errors.append(date_error)

    parsed = {
        **photo_metadata,
        **date_components,
        "person_ids": person_ids,
        "new_person_name": new_person_name,
        "date_start_value": date_start,
        "date_end_value": date_end,
        "date_precision": date_precision,
    }
    return parsed, errors


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
