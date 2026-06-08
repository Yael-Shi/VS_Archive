"""Server-side validation for ArchiveItem discovery metadata (categories, events, tags)."""

from __future__ import annotations

from typing import Any

from documents.services.archive_tags_validation import (
    normalize_tag_names_from_list,
    parse_comma_separated_tag_names,
)

_MAX_DISCOVERY_NAME_LENGTH = 255

_CATEGORY_MAX_LENGTH_ERROR = "קטגוריה חייבת להיות עד 255 תווים"
_EVENT_MAX_LENGTH_ERROR = "אירוע חייב להיות עד 255 תווים"


def parse_comma_separated_discovery_names(
    raw_value: str | None,
    *,
    max_length_error: str,
) -> tuple[list[str], list[str]]:
    """Parse comma-separated discovery names (trim, drop empty, dedupe; order preserved)."""
    names = normalize_tag_names_from_list((raw_value or "").split(","))
    errors: list[str] = []
    for name in names:
        if len(name) > _MAX_DISCOVERY_NAME_LENGTH:
            errors.append(max_length_error)
            break
    return names, errors


def parse_archive_item_discovery_metadata_form(
    post_data: dict[str, Any],
    *,
    tags_field: str = "tags",
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST fields for ArchiveItem discovery metadata."""
    categories_raw = post_data.get("categories")
    events_raw = post_data.get("events")
    tags_raw = post_data.get(tags_field)

    categories_display = "" if categories_raw is None else str(categories_raw)
    events_display = "" if events_raw is None else str(events_raw)
    tags_display = "" if tags_raw is None else str(tags_raw)

    category_names, category_errors = parse_comma_separated_discovery_names(
        categories_display,
        max_length_error=_CATEGORY_MAX_LENGTH_ERROR,
    )
    event_names, event_errors = parse_comma_separated_discovery_names(
        events_display,
        max_length_error=_EVENT_MAX_LENGTH_ERROR,
    )
    tag_names = parse_comma_separated_tag_names(tags_display)

    errors = category_errors + event_errors
    for name in tag_names:
        if len(name) > 64:
            errors.append("תגית חייבת להיות עד 64 תווים")
            break

    parsed = {
        "categories": categories_display,
        "events": events_display,
        "discovery_tags": tags_display,
        "category_names": category_names,
        "event_names": event_names,
        "tag_names": tag_names,
    }
    return parsed, errors
