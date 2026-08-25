"""Server-side validation for ArchiveItem discovery metadata (categories, events, tags)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from documents.historical_person_tag_map import (
    historical_person_name_tag_ids,
    is_historical_person_name_tag,
    is_retired_historical_person_tag_name,
)
from documents.services.archive_tags_validation import (
    normalize_tag_names_from_list,
    parse_comma_separated_tag_names,
)

_MAX_DISCOVERY_NAME_LENGTH = 255

_CATEGORY_MAX_LENGTH_ERROR = "קטגוריה חייבת להיות עד 255 תווים"
_EVENT_MAX_LENGTH_ERROR = "אירוע חייב להיות עד 255 תווים"
HISTORICAL_PERSON_TAG_REUSE_ERROR = "לא ניתן להשתמש בתגיות היסטוריות של שמות אנשים."
HISTORICAL_PERSON_TAG_DELETE_ERROR = "לא ניתן למחוק תגיות היסטוריות של שמות אנשים."


def empty_discovery_metadata_form_fields() -> dict[str, Any]:
    """Default discovery metadata form field values (no selections, no new text)."""
    return {
        "categories": "",
        "events": "",
        "discovery_tags": "",
        "selected_category_ids": [],
        "selected_event_ids": [],
        "selected_tag_ids": [],
    }


def discovery_metadata_option_querysets() -> dict[str, Any]:
    """Querysets of all existing discovery metadata values for form multi-selects."""
    from documents.models import ArchiveCategory, ArchiveEvent, Tag

    return {
        "discovery_all_categories": ArchiveCategory.objects.order_by("name"),
        "discovery_all_events": ArchiveEvent.objects.order_by("name"),
        "discovery_all_tags": Tag.objects.exclude(
            pk__in=historical_person_name_tag_ids()
        ).order_by("name"),
    }


def selected_tag_ids_from_post(post_data: dict[str, Any]) -> list[int]:
    """Parse posted ``selected_tags`` values as integer Tag ids."""
    return _parse_selected_ids(post_data, "selected_tags")


def historical_person_tag_reuse_errors(tag_ids: Iterable[int]) -> list[str]:
    """Reject any frozen historical person Tag.id. ID membership only."""
    if any(is_historical_person_name_tag(int(tag_id)) for tag_id in tag_ids):
        return [HISTORICAL_PERSON_TAG_REUSE_ERROR]
    return []


def existing_tag_name_reuse_errors(tag_names: list[str]) -> list[str]:
    """Reject names that resolve to an existing frozen historical person Tag.id."""
    if not tag_names:
        return []
    from documents.models import Tag

    existing_ids = Tag.objects.filter(name__in=tag_names).values_list("pk", flat=True)
    return historical_person_tag_reuse_errors(existing_ids)


def retired_historical_person_tag_name_errors(tag_names: Iterable[str]) -> list[str]:
    """Reject exact frozen historical Tag names. Input must already be normalized.

    Remains in force even when the original mapped Tag row no longer exists.
    """
    if any(is_retired_historical_person_tag_name(name) for name in tag_names):
        return [HISTORICAL_PERSON_TAG_REUSE_ERROR]
    return []


def historical_person_tag_name_write_errors(tag_names: list[str]) -> list[str]:
    """Reject mapped-ID name reuse while Tag rows exist, and retired names always."""
    return existing_tag_name_reuse_errors(
        tag_names
    ) or retired_historical_person_tag_name_errors(tag_names)


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


def _raw_values_from_post_data(post_data: dict[str, Any], field_name: str) -> list[Any]:
    if hasattr(post_data, "getlist"):
        return post_data.getlist(field_name)
    raw = post_data.get(field_name)
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return [raw]


def _parse_selected_ids(post_data: dict[str, Any], field_name: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in _raw_values_from_post_data(post_data, field_name):
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            pk = int(text)
        except (ValueError, TypeError):
            continue
        if pk in seen:
            continue
        seen.add(pk)
        ids.append(pk)
    return ids


def _resolve_names_by_ids(model, ids: list[int]) -> list[str]:
    if not ids:
        return []
    pk_to_name = dict(model.objects.filter(pk__in=ids).values_list("pk", "name"))
    names: list[str] = []
    seen: set[str] = set()
    for pk in ids:
        name = pk_to_name.get(pk)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _merge_discovery_names(
    selected_names: list[str], new_names: list[str]
) -> list[str]:
    return normalize_tag_names_from_list(selected_names + new_names)


def parse_archive_item_discovery_metadata_form(
    post_data: dict[str, Any],
    *,
    tags_field: str = "tags",
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST/JSON fields for ArchiveItem discovery metadata."""
    from documents.models import ArchiveCategory, ArchiveEvent, Tag

    categories_raw = post_data.get("categories")
    events_raw = post_data.get("events")
    tags_raw = post_data.get(tags_field)

    categories_display = "" if categories_raw is None else str(categories_raw)
    events_display = "" if events_raw is None else str(events_raw)
    tags_display = "" if tags_raw is None else str(tags_raw)

    selected_category_ids = _parse_selected_ids(post_data, "selected_categories")
    selected_event_ids = _parse_selected_ids(post_data, "selected_events")
    selected_tag_ids = selected_tag_ids_from_post(post_data)

    selected_category_names = _resolve_names_by_ids(
        ArchiveCategory, selected_category_ids
    )
    selected_event_names = _resolve_names_by_ids(ArchiveEvent, selected_event_ids)
    selected_tag_names = _resolve_names_by_ids(Tag, selected_tag_ids)

    new_category_names, category_errors = parse_comma_separated_discovery_names(
        categories_display,
        max_length_error=_CATEGORY_MAX_LENGTH_ERROR,
    )
    new_event_names, event_errors = parse_comma_separated_discovery_names(
        events_display,
        max_length_error=_EVENT_MAX_LENGTH_ERROR,
    )
    new_tag_names = parse_comma_separated_tag_names(tags_display)

    category_names = _merge_discovery_names(selected_category_names, new_category_names)
    event_names = _merge_discovery_names(selected_event_names, new_event_names)
    tag_names = _merge_discovery_names(selected_tag_names, new_tag_names)

    errors = category_errors + event_errors
    for name in tag_names:
        if len(name) > 64:
            errors.append("תגית חייבת להיות עד 64 תווים")
            break
    reuse_errors = historical_person_tag_reuse_errors(
        selected_tag_ids
    ) or historical_person_tag_name_write_errors(tag_names)
    for error in reuse_errors:
        if error not in errors:
            errors.append(error)

    parsed = {
        "categories": categories_display,
        "events": events_display,
        "discovery_tags": tags_display,
        "selected_category_ids": selected_category_ids,
        "selected_event_ids": selected_event_ids,
        "selected_tag_ids": selected_tag_ids,
        "category_names": category_names,
        "event_names": event_names,
        "tag_names": tag_names,
    }
    return parsed, errors
