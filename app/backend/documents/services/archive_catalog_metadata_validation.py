"""Server-side validation for OCR catalog scalar metadata fields."""

from __future__ import annotations

from typing import Any

_MAX_SCALAR_LENGTH = 255

_FIELD_LABELS_HE: dict[str, str] = {
    "donor": "תורם/ת",
    "collection": "אוסף",
    "original_location": "מיקום מקורי",
}


def _validate_max_length(field_name: str, value: str, errors: list[str]) -> None:
    if len(value) > _MAX_SCALAR_LENGTH:
        label = _FIELD_LABELS_HE[field_name]
        errors.append(f"{label} חייב להיות עד {_MAX_SCALAR_LENGTH} תווים")


def parse_ocr_catalog_metadata_form(
    post_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Parse POST fields for OCR catalog metadata and return normalized data plus errors."""
    donor = (post_data.get("donor") or "").strip()
    collection = (post_data.get("collection") or "").strip()
    original_location = (post_data.get("original_location") or "").strip()

    notes_raw = post_data.get("notes")
    notes = "" if notes_raw is None else str(notes_raw)

    errors: list[str] = []
    for field_name, value in (
        ("donor", donor),
        ("collection", collection),
        ("original_location", original_location),
    ):
        _validate_max_length(field_name, value, errors)

    parsed = {
        "donor": donor,
        "collection": collection,
        "original_location": original_location,
        "notes": notes,
    }
    return parsed, errors
