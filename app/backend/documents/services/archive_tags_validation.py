"""Server-side validation for OCR document tags."""

from __future__ import annotations

from typing import Any

_MAX_TAG_LENGTH = 64


def normalize_tag_names_from_list(raw_tags: list) -> list[str]:
    """Normalize tag names from list input (strip, skip empty, dedupe; order preserved)."""
    seen: set[str] = set()
    names: list[str] = []
    for raw in raw_tags:
        if raw is None:
            continue
        name = str(raw).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def parse_comma_separated_tag_names(raw: str) -> list[str]:
    """Split comma-separated tag input into normalized unique names (order preserved)."""
    return normalize_tag_names_from_list((raw or "").split(","))


def parse_ocr_tags_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST field ``tags`` and return display value, normalized names, and errors."""
    tags_raw = post_data.get("tags")
    tags_display = "" if tags_raw is None else str(tags_raw)
    tag_names = parse_comma_separated_tag_names(tags_display)

    errors: list[str] = []
    for name in tag_names:
        if len(name) > _MAX_TAG_LENGTH:
            errors.append("תגית חייבת להיות עד 64 תווים")
            break

    parsed = {
        "tags": tags_display,
        "tag_names": tag_names,
    }
    return parsed, errors
