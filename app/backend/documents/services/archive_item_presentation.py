"""User-facing Hebrew labels for ArchiveItem UI (presentation only).

Stored enum/database values are unchanged; templates and forms map values here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db.models import Prefetch, Q, QuerySet
from django.urls import reverse

from documents.models import ArchiveItem, DocumentTextResult
from documents.services.document_date import format_document_date
from documents.services.text_presentation import (
    PREFETCHED_DISPLAYABLE_TEXT_RESULTS_ATTR,
    get_displayed_transcription_text,
)

ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL = ""
ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR = "ocr_document"
ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL = "manual_text"
ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO = "photo"

ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL = ""
ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS = "documents_and_texts"
ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO = "photo"

ARCHIVE_PUBLIC_LIST_TYPE_FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    (ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL, "הכל"),
    (
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
        "מסמכים וטקסטים",
    ),
    (ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO, "תמונות"),
)

_VISIBILITY_LABELS: dict[str, str] = {
    ArchiveItem.Visibility.PUBLIC.value: "ציבורי",
    ArchiveItem.Visibility.PRIVATE.value: "פרטי",
}

_ARCHIVE_METADATA_STATUS_LABELS: dict[str, str] = {
    ArchiveItem.MetadataStatus.NEEDS_COMPLETION.value: "דרושה השלמת פרטים",
    ArchiveItem.MetadataStatus.COMPLETED.value: "פרטים הושלמו",
}

_ARCHIVE_ITEM_TYPE_LABELS: dict[str, str] = {
    ArchiveItem.ItemType.OCR_DOCUMENT.value: "מסמך",
    ArchiveItem.ItemType.MANUAL_TEXT.value: "טקסט",
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
        (
            ArchiveItem.Visibility.PUBLIC,
            visibility_label(ArchiveItem.Visibility.PUBLIC),
        ),
        (
            ArchiveItem.Visibility.PRIVATE,
            visibility_label(ArchiveItem.Visibility.PRIVATE),
        ),
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
        (
            ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL,
            archive_item_type_label(ArchiveItem.ItemType.MANUAL_TEXT),
        ),
        (
            ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR,
            archive_item_type_label(ArchiveItem.ItemType.OCR_DOCUMENT),
        ),
        (
            ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO,
            archive_item_type_label(ArchiveItem.ItemType.PHOTO),
        ),
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


def normalize_archive_public_list_type_filter(raw: str | None) -> str:
    """Return public ``/archive/`` list type-filter slug, or empty string for all."""
    value = (raw or "").strip().lower()
    if value in ("", "all", ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL):
        return ""
    if value in (
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
        ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR,
        ArchiveItem.ItemType.OCR_DOCUMENT.value.lower(),
        ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL,
        ArchiveItem.ItemType.MANUAL_TEXT.value.lower(),
    ):
        return ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS
    if value in (
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO,
        ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO,
        ArchiveItem.ItemType.PHOTO.value.lower(),
    ):
        return ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO
    return ""


def filter_archive_items_by_public_list_type(
    queryset: QuerySet[ArchiveItem],
    filter_slug: str,
) -> QuerySet[ArchiveItem]:
    """Apply public archive list type filter slug to ``queryset``."""
    slug = normalize_archive_public_list_type_filter(filter_slug)
    if not slug:
        return queryset
    if slug == ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS:
        return queryset.filter(
            item_type__in=(
                ArchiveItem.ItemType.OCR_DOCUMENT,
                ArchiveItem.ItemType.MANUAL_TEXT,
            )
        )
    if slug == ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO:
        return queryset.filter(item_type=ArchiveItem.ItemType.PHOTO)
    return queryset


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
    if value in (
        ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO,
        ArchiveItem.ItemType.PHOTO.value.lower(),
    ):
        return ArchiveItem.ItemType.PHOTO
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


ARCHIVE_BROWSE_PREVIEW_MAX_LEN = 160
ARCHIVE_BROWSE_PREVIEW_EMPTY = "אין תצוגה מקדימה זמינה."
ARCHIVE_BROWSE_OCR_PREVIEW = "פתחו את המסמך לצפייה בתוכן."

_TYPE_MARKER_BY_ITEM_TYPE: dict[str, str] = {
    ArchiveItem.ItemType.OCR_DOCUMENT: "ocr",
    ArchiveItem.ItemType.MANUAL_TEXT: "manual",
    ArchiveItem.ItemType.PHOTO: "photo",
}


@dataclass(frozen=True)
class ArchiveBrowseLink:
    name: str
    href: str


@dataclass(frozen=True)
class ArchiveBrowseCard:
    item: ArchiveItem
    title: str
    type_label: str
    type_marker: str
    date_display: str
    author_display: str
    detail_url: str
    preview_text: str
    category_links: tuple[ArchiveBrowseLink, ...]
    related_links: tuple[ArchiveBrowseLink, ...]


def _normalize_preview_source(text: str | None) -> str:
    return " ".join((text or "").split())


def _truncate_preview(text: str, *, max_len: int = ARCHIVE_BROWSE_PREVIEW_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _manual_text_preview(archive_item: ArchiveItem) -> str:
    content = getattr(archive_item, "manual_text_content", None)
    if content is None:
        return ""
    return _truncate_preview(_normalize_preview_source(content.body))


def _photo_preview(archive_item: ArchiveItem) -> str:
    photo_content = getattr(archive_item, "photo_content", None)
    if photo_content is None:
        return ""
    for value in (
        photo_content.description,
        photo_content.context,
        photo_content.people_present,
        photo_content.location,
        photo_content.notes,
    ):
        normalized = _normalize_preview_source(value)
        if normalized:
            return _truncate_preview(normalized)
    return ""


def _ocr_document_preview(archive_item: ArchiveItem) -> str:
    document = getattr(archive_item, "ocr_document", None)
    if document is None:
        return ""
    text = get_displayed_transcription_text(document)
    if not text.strip():
        return ""
    return _truncate_preview(_normalize_preview_source(text))


def _preview_text_for_archive_item(archive_item: ArchiveItem) -> str:
    item_type = archive_item.item_type
    if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        preview = _ocr_document_preview(archive_item)
        return preview or ARCHIVE_BROWSE_OCR_PREVIEW
    if item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        preview = _manual_text_preview(archive_item)
        return preview or ARCHIVE_BROWSE_PREVIEW_EMPTY
    if item_type == ArchiveItem.ItemType.PHOTO:
        preview = _photo_preview(archive_item)
        return preview or ARCHIVE_BROWSE_PREVIEW_EMPTY
    return ARCHIVE_BROWSE_PREVIEW_EMPTY


def _type_marker_for_item(archive_item: ArchiveItem) -> str:
    return _TYPE_MARKER_BY_ITEM_TYPE.get(archive_item.item_type, "generic")


def _author_display_for_archive_item(archive_item: ArchiveItem) -> str:
    return (archive_item.author_name or "").strip()


def _prefetched_relation(archive_item: ArchiveItem, relation: str) -> Iterable:
    """Return a prefetched M2M relation when available.

    Falls back to ``relation.all()`` (one query per item). Archive browse views
    should call ``prefetch_related("categories", "events", "tags")`` before
    ``build_archive_browse_cards``.
    """
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and relation in cache:
        return cache[relation]
    return getattr(archive_item, relation).all()


def _browse_links_for_relation(
    archive_item: ArchiveItem,
    relation: str,
    *,
    url_name: str,
    id_kwarg: str,
) -> tuple[ArchiveBrowseLink, ...]:
    links: list[ArchiveBrowseLink] = []
    for row in sorted(
        _prefetched_relation(archive_item, relation), key=lambda item: item.name
    ):
        links.append(
            ArchiveBrowseLink(
                name=row.name,
                href=reverse(url_name, kwargs={id_kwarg: row.id}),
            )
        )
    return tuple(links)


def _category_links_for_item(archive_item: ArchiveItem) -> tuple[ArchiveBrowseLink, ...]:
    return _browse_links_for_relation(
        archive_item,
        "categories",
        url_name="archive-category-browse",
        id_kwarg="category_id",
    )


def _related_links_for_item(archive_item: ArchiveItem) -> tuple[ArchiveBrowseLink, ...]:
    event_links = _browse_links_for_relation(
        archive_item,
        "events",
        url_name="archive-event-browse",
        id_kwarg="event_id",
    )
    tag_links = _browse_links_for_relation(
        archive_item,
        "tags",
        url_name="archive-tag-browse",
        id_kwarg="tag_id",
    )
    return event_links + tag_links


def archive_browse_displayable_text_results_prefetch() -> Prefetch:
    """Prefetch displayable OCR text rows for archive browse cards (avoids N+1)."""
    return Prefetch(
        "ocr_document__text_results",
        queryset=(
            DocumentTextResult.objects.filter(
                status__in=(
                    DocumentTextResult.Status.SUCCEEDED,
                    DocumentTextResult.Status.NEEDS_REVIEW,
                ),
            )
            .exclude(text__isnull=True)
            .exclude(text__exact="")
            .order_by("-created_at")
        ),
        to_attr=PREFETCHED_DISPLAYABLE_TEXT_RESULTS_ATTR,
    )


def build_archive_browse_card(archive_item: ArchiveItem) -> ArchiveBrowseCard:
    return ArchiveBrowseCard(
        item=archive_item,
        title=archive_item.title,
        type_label=archive_item_type_label(archive_item.item_type),
        type_marker=_type_marker_for_item(archive_item),
        date_display=format_document_date(archive_item),
        author_display=_author_display_for_archive_item(archive_item),
        detail_url=reverse("archive-detail", kwargs={"item_id": archive_item.id}),
        preview_text=_preview_text_for_archive_item(archive_item),
        category_links=_category_links_for_item(archive_item),
        related_links=_related_links_for_item(archive_item),
    )


def build_archive_browse_cards(
    items: Sequence[ArchiveItem] | Iterable[ArchiveItem],
) -> list[ArchiveBrowseCard]:
    """Build public browse cards for archive list and discovery browse pages.

    Callers should pass items from ``_archive_browse_select_related`` (or
    equivalent) so ``categories``, ``events``, ``tags``, and displayable OCR
    ``DocumentTextResult`` rows are prefetched.
    """
    return [build_archive_browse_card(item) for item in items]
