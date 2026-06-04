"""User-facing Hebrew labels for ArchiveItem UI (presentation only).

Stored enum/database values are unchanged; templates and forms map values here.
"""

from __future__ import annotations

from documents.models import ArchiveItem

ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL = ""
ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR = "ocr_document"
ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL = "manual_text"

ARCHIVE_LIST_ITEM_TYPE_FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    (ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL, "הכול"),
    (ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR, "מסמכים סרוקים / PDF"),
    (ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL, "טקסטים מוקלדים"),
)

_VISIBILITY_LABELS: dict[str, str] = {
    ArchiveItem.Visibility.PUBLIC.value: "ציבורי",
    ArchiveItem.Visibility.PRIVATE.value: "פרטי",
}

_ARCHIVE_METADATA_STATUS_LABELS: dict[str, str] = {
    ArchiveItem.MetadataStatus.NEEDS_COMPLETION.value: "דורש השלמת פרטים",
    ArchiveItem.MetadataStatus.COMPLETED.value: "הושלם",
}

_ARCHIVE_ITEM_TYPE_LABELS: dict[str, str] = {
    ArchiveItem.ItemType.OCR_DOCUMENT.value: "מסמך סרוק / PDF",
    ArchiveItem.ItemType.MANUAL_TEXT.value: "טקסט מוקלד",
}

_LANGUAGE_LABELS: dict[str, str] = {
    "he": "עברית",
    "heb": "עברית",
    "hebrew": "עברית",
    "en": "אנגלית",
    "eng": "אנגלית",
    "english": "אנגלית",
    "fr": "צרפתית",
    "french": "צרפתית",
    "ar": "ערבית",
    "arabic": "ערבית",
}


def _safe_label(mapping: dict[str, str], value) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    return mapping.get(key, mapping.get(key.lower(), key))


def visibility_label(value) -> str:
    return _safe_label(_VISIBILITY_LABELS, value)


def archive_metadata_status_label(value) -> str:
    return _safe_label(_ARCHIVE_METADATA_STATUS_LABELS, value)


def archive_item_type_label(value) -> str:
    return _safe_label(_ARCHIVE_ITEM_TYPE_LABELS, value)


def language_label(value) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    return _LANGUAGE_LABELS.get(key, _LANGUAGE_LABELS.get(key.lower(), key))


def archive_visibility_ui_choices() -> list[tuple[str, str]]:
    return [
        (ArchiveItem.Visibility.PUBLIC, visibility_label(ArchiveItem.Visibility.PUBLIC)),
        (ArchiveItem.Visibility.PRIVATE, visibility_label(ArchiveItem.Visibility.PRIVATE)),
    ]


def archive_metadata_status_ui_choices() -> list[tuple[str, str]]:
    return [
        (
            ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            archive_metadata_status_label(ArchiveItem.MetadataStatus.NEEDS_COMPLETION),
        ),
        (
            ArchiveItem.MetadataStatus.COMPLETED,
            archive_metadata_status_label(ArchiveItem.MetadataStatus.COMPLETED),
        ),
    ]


def archive_manage_item_type_ui_choices() -> list[tuple[str, str]]:
    """Slugs used on /archive/manage/new/ (not stored enum values)."""
    return [
        (ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL, archive_item_type_label(ArchiveItem.ItemType.MANUAL_TEXT)),
        (ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR, archive_item_type_label(ArchiveItem.ItemType.OCR_DOCUMENT)),
    ]


def normalize_archive_list_item_type_filter(raw: str | None) -> str:
    """Return stored ``ArchiveItem.ItemType`` value, or empty string for «all»."""
    value = (raw or "").strip().lower()
    if value in ("", "all", ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL):
        return ""
    if value in (
        ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR,
        ArchiveItem.ItemType.OCR_DOCUMENT.value.lower(),
    ):
        return ArchiveItem.ItemType.OCR_DOCUMENT
    if value in (
        ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL,
        ArchiveItem.ItemType.MANUAL_TEXT.value.lower(),
    ):
        return ArchiveItem.ItemType.MANUAL_TEXT
    return ""
