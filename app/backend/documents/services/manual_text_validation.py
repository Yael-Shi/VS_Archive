"""Server-side validation for manual text archive item create/edit forms."""

from __future__ import annotations

from typing import Any

from documents.services.archive_metadata_validation import parse_archive_metadata_form


def parse_manual_text_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST fields and return normalized form data plus validation errors."""
    parsed, errors = parse_archive_metadata_form(post_data)
    parsed["body"] = post_data.get("body") or ""

    if not errors and (not parsed["body"] or not parsed["body"].strip()):
        errors.append("body is required")

    return parsed, errors
