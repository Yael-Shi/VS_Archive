"""User-facing Hebrew labels for ArchiveItem UI (presentation only).

Stored enum/database values are unchanged; templates and forms map values here.
"""

from __future__ import annotations

from django.db.models import Q, QuerySet

from documents.models import ArchiveItem

ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL = ""
ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR = "ocr_document"
ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL = "manual_text"
ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO = "photo"

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
    ArchiveItem.ItemType.PHOTO.value: "תמונה",
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
        (ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO, archive_item_type_label(ArchiveItem.ItemType.PHOTO)),
    ]


def archive_item_has_discovery_metadata(archive_item) -> bool:
    """True when the ArchiveItem has any discovery categories, events, or tags."""
    if archive_item is None:
        return False
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and {"categories", "events", "tags"}.issubset(cache):
        return bool(cache["categories"]) or bool(cache["events"]) or bool(cache["tags"])
    return (
        archive_item.categories.exists()
        or archive_item.events.exists()
        or archive_item.tags.exists()
    )


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


def normalize_archive_list_search_query(raw: str | None) -> str:
    """Trim archive list ``q``; empty/whitespace means no search filter."""
    return (raw or "").strip()


def filter_archive_items_by_search_query(
    queryset: QuerySet[ArchiveItem],
    search_query: str,
) -> QuerySet[ArchiveItem]:
    """Case-insensitive search over ArchiveItem public discovery metadata fields."""
    q = normalize_archive_list_search_query(search_query)
    if not q:
        return queryset
    return queryset.filter(
        Q(title__icontains=q)
        | Q(author_name__icontains=q)
        | Q(source_title__icontains=q)
        | Q(categories__name__icontains=q)
        | Q(events__name__icontains=q)
        | Q(tags__name__icontains=q)
    ).distinct()
