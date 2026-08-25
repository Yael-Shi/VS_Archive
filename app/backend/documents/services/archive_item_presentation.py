"""User-facing Hebrew labels for ArchiveItem UI (presentation only).

Stored enum/database values are unchanged; templates and forms map values here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urlencode

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db.models import (
    Case,
    Count,
    F,
    FloatField,
    Prefetch,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django.urls import reverse

from documents.historical_person_tag_map import historical_person_name_tag_ids
from documents.models import ArchiveItem, Document
from documents.services.archive_advanced_search import (
    ARCHIVE_ADVANCED_PANEL_PARAM,
    ARCHIVE_ADVANCED_PANEL_VALUE,
    EMPTY_ARCHIVE_ADVANCED_FILTERS,
    ArchiveAdvancedFilters,
    archive_advanced_filters_without_author,
    archive_advanced_filters_without_year,
    build_archive_advanced_filter_summary_items,
)
from documents.services.archive_search_index import SEARCH_VECTOR_CONFIG
from documents.services.document_date import format_document_date
from documents.services.text_presentation import (
    archive_item_displayable_text_results_prefetch,
    get_displayed_transcription_text,
)
from documents.services.video_url_contract import video_provider_display_label

# Local 160px browse-card visual fallbacks (no remote media / PDF rendering).
ARCHIVE_BROWSE_FALLBACK_PREVIEW_PDF = "pdf"
ARCHIVE_BROWSE_FALLBACK_PREVIEW_MANUAL = "manual"
ARCHIVE_BROWSE_FALLBACK_PREVIEW_VIDEO = "video"

ARCHIVE_LIST_ITEM_TYPE_FILTER_ALL = ""
ARCHIVE_LIST_ITEM_TYPE_FILTER_OCR = "ocr_document"
ARCHIVE_LIST_ITEM_TYPE_FILTER_MANUAL = "manual_text"
ARCHIVE_LIST_ITEM_TYPE_FILTER_PHOTO = "photo"
ARCHIVE_LIST_ITEM_TYPE_FILTER_VIDEO = "video"

ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL = ""
ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS = "documents_and_texts"
ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO = "photo"
ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO = "video"

ARCHIVE_PUBLIC_LIST_TYPE_FILTER_CHOICES: tuple[tuple[str, str], ...] = (
    (ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL, "הכל"),
    (
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
        "מסמכים וטקסטים",
    ),
    (ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO, "תמונות"),
    (ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO, "קטעי וידאו"),
)

# Short read-only labels for manager-facing metadata (lists, detail leads, queues).
_VISIBILITY_DISPLAY_LABELS: dict[str, str] = {
    ArchiveItem.Visibility.PUBLIC.value: "ציבורי",
    ArchiveItem.Visibility.PRIVATE.value: "פרטי",
    ArchiveItem.Visibility.RESTRICTED.value: "רגיש",
}

# Full form-choice labels (selection controls / explanatory form contexts).
_VISIBILITY_CHOICE_LABELS: dict[str, str] = {
    ArchiveItem.Visibility.PUBLIC.value: "ציבורי",
    ArchiveItem.Visibility.PRIVATE.value: "פרטי",
    ArchiveItem.Visibility.RESTRICTED.value: "רגיש — למורשים בלבד",
}

_ARCHIVE_METADATA_STATUS_LABELS: dict[str, str] = {
    ArchiveItem.MetadataStatus.NEEDS_COMPLETION.value: "דרושה השלמת פרטים",
    ArchiveItem.MetadataStatus.COMPLETED.value: "פרטים הושלמו",
}

_ARCHIVE_ITEM_TYPE_LABELS: dict[str, str] = {
    ArchiveItem.ItemType.OCR_DOCUMENT.value: "מסמך",
    ArchiveItem.ItemType.MANUAL_TEXT.value: "טקסט",
    ArchiveItem.ItemType.PHOTO.value: "תמונה",
    ArchiveItem.ItemType.VIDEO.value: "סרטון",
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


def visibility_display_label(value) -> str:
    """Short Hebrew label for read-only manager-facing visibility metadata."""
    return _safe_label(_VISIBILITY_DISPLAY_LABELS, value)


def visibility_choice_label(value) -> str:
    """Full Hebrew label for visibility form/select options."""
    return _safe_label(_VISIBILITY_CHOICE_LABELS, value)


def visibility_label(value) -> str:
    """Backward-compatible alias for ``visibility_choice_label`` (form choices)."""
    return visibility_choice_label(value)


def archive_metadata_status_label(value) -> str:
    return _safe_label(_ARCHIVE_METADATA_STATUS_LABELS, value)


def archive_item_type_label(value) -> str:
    return _safe_label(_ARCHIVE_ITEM_TYPE_LABELS, value)


def language_label(value) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    return _LANGUAGE_LABELS.get(key, _LANGUAGE_LABELS.get(key.lower(), key))


def archive_visibility_ui_choices(user=None) -> list[tuple[str, str]]:
    """Return visibility options for staff forms/filters.

    ``public`` / ``private`` are always included. ``restricted`` is included only
    when ``user`` has ``documents.view_restricted_archiveitem`` (active superusers
    follow Django's normal ``has_perm`` behavior).
    """
    from documents.services.archive_item_access import can_view_restricted_archive_items

    choices: list[tuple[str, str]] = [
        (
            ArchiveItem.Visibility.PUBLIC.value,
            visibility_choice_label(ArchiveItem.Visibility.PUBLIC),
        ),
        (
            ArchiveItem.Visibility.PRIVATE.value,
            visibility_choice_label(ArchiveItem.Visibility.PRIVATE),
        ),
    ]
    if can_view_restricted_archive_items(user):
        choices.append(
            (
                ArchiveItem.Visibility.RESTRICTED.value,
                visibility_choice_label(ArchiveItem.Visibility.RESTRICTED),
            )
        )
    return choices


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
        (
            ARCHIVE_LIST_ITEM_TYPE_FILTER_VIDEO,
            archive_item_type_label(ArchiveItem.ItemType.VIDEO),
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
    if value in (
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO,
        ARCHIVE_LIST_ITEM_TYPE_FILTER_VIDEO,
        ArchiveItem.ItemType.VIDEO.value.lower(),
    ):
        return ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO
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
    if slug == ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO:
        return queryset.filter(item_type=ArchiveItem.ItemType.VIDEO)
    return queryset


def aggregate_archive_public_list_type_counts(
    queryset: QuerySet[ArchiveItem],
) -> dict[str, int]:
    """
    Per-tab counts for the public archive type filter.

    Counts the CURRENT search/filter universe (authorization + advanced filters
    + ``q``) BEFORE ``item_type`` filtering. Uses one conditional aggregate so
    switching the active type tab does not change sibling tab counts and does
    not issue one ``.count()`` per tab.

    Rows are scoped through ``pk__in`` so ranking annotations / joins on the
    input queryset cannot inflate counts.
    """
    scoped = ArchiveItem.objects.filter(pk__in=queryset.order_by().values("pk"))
    row = scoped.aggregate(
        all_count=Count("pk"),
        documents_and_texts_count=Count(
            "pk",
            filter=Q(
                item_type__in=(
                    ArchiveItem.ItemType.OCR_DOCUMENT,
                    ArchiveItem.ItemType.MANUAL_TEXT,
                )
            ),
        ),
        photo_count=Count(
            "pk",
            filter=Q(item_type=ArchiveItem.ItemType.PHOTO),
        ),
        video_count=Count(
            "pk",
            filter=Q(item_type=ArchiveItem.ItemType.VIDEO),
        ),
    )
    return {
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: int(row["all_count"] or 0),
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: int(
            row["documents_and_texts_count"] or 0
        ),
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: int(row["photo_count"] or 0),
        ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: int(row["video_count"] or 0),
    }


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


# Safety cap for public ``/archive/?q=`` (display string length after trim).
# Overlong queries return no matches; the trimmed original remains in form/URL.
ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH = 200

# Letters/digits stay in terms; ordinary punctuation and underscore are separators.
# ``\W`` is ``[^\w]``; underscore is included explicitly because ``\w`` matches it.
_ARCHIVE_LIST_SEARCH_TERM_SEPARATOR_RE = re.compile(r"[\W_]+", flags=re.UNICODE)

# Substring boosts align with PostgreSQL A/B weights (title > metadata > body).
# Body matches rely on SearchRank against weight C only (no body substring arm).
# Multi-term: each boost is applied once if any term hits that short field (not
# summed per term), so single-term title > metadata > body stays deterministic.
_ARCHIVE_SEARCH_TITLE_SUBSTRING_BOOST = 1.0
_ARCHIVE_SEARCH_METADATA_SUBSTRING_BOOST = 0.4

ARCHIVE_LIST_SEARCH_NO_SEARCH = "no_search"
ARCHIVE_LIST_SEARCH_NO_MATCHES = "no_matches"
ARCHIVE_LIST_SEARCH_SEARCH = "search"


@dataclass(frozen=True)
class ArchiveListSearchTerms:
    """
    Explicit normalization outcome for public archive ``q``.

    - ``no_search``: blank/whitespace-only → browse (no search filter).
    - ``no_matches``: overlong, or nonblank punctuation-only → empty result set.
    - ``search``: one or more terms → index search.
    """

    terms: tuple[str, ...]
    outcome: str


def normalize_archive_list_search_query(raw: str | None) -> str:
    """Trim archive list ``q`` for display/URL; empty/whitespace means no search."""
    return (raw or "").strip()


def resolve_archive_list_search_terms(search_query: str) -> ArchiveListSearchTerms:
    """Normalize ``q`` into an explicit browse / empty / search outcome."""
    display_q = normalize_archive_list_search_query(search_query)
    if not display_q:
        return ArchiveListSearchTerms(terms=(), outcome=ARCHIVE_LIST_SEARCH_NO_SEARCH)
    if len(display_q) > ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH:
        return ArchiveListSearchTerms(terms=(), outcome=ARCHIVE_LIST_SEARCH_NO_MATCHES)
    collapsed = " ".join(display_q.split())
    terms = tuple(
        term for term in _ARCHIVE_LIST_SEARCH_TERM_SEPARATOR_RE.split(collapsed) if term
    )
    if not terms:
        # Nonblank q that tokenizes to nothing (e.g. "... !!!").
        return ArchiveListSearchTerms(terms=(), outcome=ARCHIVE_LIST_SEARCH_NO_MATCHES)
    return ArchiveListSearchTerms(terms=terms, outcome=ARCHIVE_LIST_SEARCH_SEARCH)


def _archive_list_plain_search_query(term: str) -> SearchQuery:
    return SearchQuery(
        term,
        config=SEARCH_VECTOR_CONFIG,
        search_type="plain",
    )


def _archive_list_term_candidate_pks(
    authorized: QuerySet[ArchiveItem],
    term: str,
) -> QuerySet:
    """
    Authorized PK candidates for one term: FTS ∪ title substring ∪ metadata substring.

    Branches are UNION'd so the FTS ``search_vector @@`` arm remains a separate
    SELECT that PostgreSQL can satisfy via ``archive_item_search_vector_gin``.
    A single WHERE with ``@@ OR ILIKE OR ILIKE`` can force a seq scan and prevent
    meaningful GIN participation.
    """
    scope = authorized.order_by().values("pk")
    fts_branch = (
        ArchiveItem.objects.order_by()
        .filter(
            pk__in=scope,
            search_index__search_vector=_archive_list_plain_search_query(term),
        )
        .values("pk")
    )
    title_branch = (
        ArchiveItem.objects.order_by()
        .filter(pk__in=scope, search_index__title_text__icontains=term)
        .values("pk")
    )
    metadata_branch = (
        ArchiveItem.objects.order_by()
        .filter(pk__in=scope, search_index__metadata_text__icontains=term)
        .values("pk")
    )
    return fts_branch.union(title_branch, metadata_branch)


def filter_archive_items_by_search_query(
    queryset: QuerySet[ArchiveItem],
    search_query: str,
) -> QuerySet[ArchiveItem]:
    """
    Public archive search against ``ArchiveItemSearchIndex`` (PR3 cutover).

    Callers must pass an already-authorized browse queryset
    (``archive_browse_queryset_for_user`` or equivalent). Matching, ranking,
    counts, and pagination must not run on unauthorized rows.

    Multi-term queries are AND across sources: each term must match via the
    per-term UNION of weighted ``search_vector`` FTS and/or short-field
    ``title_text`` / ``metadata_text`` substring arms. ``body_text`` and
    ``hebrew_translation_text`` are FTS-only (no substring). Items without an
    index row never match and do not crash the page. Blank ``q`` leaves the
    queryset (and its ordering) unchanged; punctuation-only nonblank ``q``
    returns no rows.
    """
    resolved = resolve_archive_list_search_terms(search_query)
    if resolved.outcome == ARCHIVE_LIST_SEARCH_NO_SEARCH:
        return queryset
    if resolved.outcome == ARCHIVE_LIST_SEARCH_NO_MATCHES:
        return queryset.none()

    terms = resolved.terms
    matched = queryset
    for term in terms:
        matched = matched.filter(
            pk__in=_archive_list_term_candidate_pks(queryset, term)
        )

    rank_query = _archive_list_plain_search_query(terms[0])
    for term in terms[1:]:
        rank_query &= _archive_list_plain_search_query(term)

    title_substring_q = Q(search_index__title_text__icontains=terms[0])
    metadata_substring_q = Q(search_index__metadata_text__icontains=terms[0])
    for term in terms[1:]:
        title_substring_q |= Q(search_index__title_text__icontains=term)
        metadata_substring_q |= Q(search_index__metadata_text__icontains=term)

    return (
        matched.annotate(
            archive_search_rank=Coalesce(
                SearchRank(F("search_index__search_vector"), rank_query),
                Value(0.0),
                output_field=FloatField(),
            ),
            archive_search_title_boost=Case(
                When(
                    condition=title_substring_q,
                    then=Value(_ARCHIVE_SEARCH_TITLE_SUBSTRING_BOOST),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
            archive_search_metadata_boost=Case(
                When(
                    condition=metadata_substring_q,
                    then=Value(_ARCHIVE_SEARCH_METADATA_SUBSTRING_BOOST),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            ),
        )
        .annotate(
            archive_search_relevance=(
                F("archive_search_rank")
                + F("archive_search_title_boost")
                + F("archive_search_metadata_boost")
            ),
        )
        .order_by("-archive_search_relevance", "-created_at", "pk")
    )


ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE = 48
ARCHIVE_PUBLIC_LIST_PER_PAGE_OPTIONS: tuple[int, ...] = (24, 48, 96)


def normalize_archive_public_list_per_page(raw: str | None) -> int:
    """Return supported public archive list page size, defaulting to 48."""
    if raw is None:
        return ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE
    try:
        per_page = int(raw)
    except ValueError:
        return ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE
    if per_page in ARCHIVE_PUBLIC_LIST_PER_PAGE_OPTIONS:
        return per_page
    return ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE


def normalize_archive_public_list_page(
    raw: str | None,
    *,
    total_count: int,
    per_page: int,
) -> int:
    """Return a 1-based page number bounded to the filtered result set."""
    if raw is None:
        page = 1
    else:
        try:
            page = int(raw)
        except ValueError:
            page = 1
    if page < 1:
        page = 1
    if total_count <= 0:
        return 1
    total_pages = (total_count + per_page - 1) // per_page
    return min(page, total_pages)


def build_archive_public_list_query(
    *,
    q: str = "",
    item_type_filter: str = "",
    page: int = 1,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
    advanced_open: bool = False,
) -> str:
    """Build a query string for public ``/archive/`` list links."""
    params: list[tuple[str, str]] = []
    if q:
        params.append(("q", q))
    if item_type_filter:
        params.append(("item_type", item_type_filter))
    filters = advanced_filters or EMPTY_ARCHIVE_ADVANCED_FILTERS
    params.extend(filters.query_param_pairs())
    if advanced_open:
        params.append((ARCHIVE_ADVANCED_PANEL_PARAM, ARCHIVE_ADVANCED_PANEL_VALUE))
    if page > 1:
        params.append(("page", str(page)))
    if per_page != ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE:
        params.append(("per_page", str(per_page)))
    return urlencode(params)


def archive_public_list_clear_search_query_suffix(
    *,
    item_type_filter: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
) -> str:
    """Query suffix for clearing archive search while preserving type/filters/per-page."""
    query = build_archive_public_list_query(
        item_type_filter=item_type_filter,
        per_page=per_page,
        advanced_filters=advanced_filters,
    )
    return f"?{query}" if query else ""


def archive_public_list_clear_all_query_suffix(
    *,
    item_type_filter: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
) -> str:
    """Query suffix clearing ``q`` and all advanced filters; keeps type/per-page."""
    query = build_archive_public_list_query(
        item_type_filter=item_type_filter,
        per_page=per_page,
        advanced_filters=EMPTY_ARCHIVE_ADVANCED_FILTERS,
    )
    return f"?{query}" if query else ""


def build_archive_public_list_type_filter_links(
    *,
    q: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    active_item_type_filter: str = "",
    advanced_filters: ArchiveAdvancedFilters | None = None,
    type_counts: Mapping[str, int] | None = None,
) -> list[dict[str, object]]:
    """Link metadata for public archive list type-filter controls."""
    links: list[dict[str, object]] = []
    for slug, label in ARCHIVE_PUBLIC_LIST_TYPE_FILTER_CHOICES:
        query = build_archive_public_list_query(
            q=q,
            item_type_filter=slug,
            per_page=per_page,
            advanced_filters=advanced_filters,
        )
        link: dict[str, object] = {
            "label": label,
            "href_suffix": f"?{query}" if query else "",
            "is_active": active_item_type_filter == slug
            if slug
            else not active_item_type_filter,
            "slug": slug,
            "count": (
                int(type_counts.get(slug, 0)) if type_counts is not None else None
            ),
        }
        links.append(link)
    return links


def archive_public_list_filter_context(
    *,
    q: str = "",
    item_type_filter: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
    advanced_open: bool = False,
    type_counts: Mapping[str, int] | None = None,
) -> dict[str, object]:
    """Template context for archive list search/filter query preservation."""
    open_advanced_query = build_archive_public_list_query(
        q=q,
        item_type_filter=item_type_filter,
        per_page=per_page,
        advanced_filters=advanced_filters,
        advanced_open=True,
    )
    close_advanced_query = build_archive_public_list_query(
        q=q,
        item_type_filter=item_type_filter,
        per_page=per_page,
        advanced_filters=advanced_filters,
        advanced_open=False,
    )
    return {
        "preserve_per_page_in_query": per_page != ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
        "item_type_filter_links": build_archive_public_list_type_filter_links(
            q=q,
            per_page=per_page,
            active_item_type_filter=item_type_filter,
            advanced_filters=advanced_filters,
            type_counts=type_counts,
        ),
        "item_type_counts": type_counts or {},
        "clear_search_query_suffix": archive_public_list_clear_search_query_suffix(
            item_type_filter=item_type_filter,
            per_page=per_page,
            advanced_filters=advanced_filters,
        ),
        "clear_all_query_suffix": archive_public_list_clear_all_query_suffix(
            item_type_filter=item_type_filter,
            per_page=per_page,
        ),
        "advanced_search_open_href_suffix": (
            f"?{open_advanced_query}" if open_advanced_query else ""
        ),
        "advanced_search_close_href_suffix": (
            f"?{close_advanced_query}" if close_advanced_query else ""
        ),
        "advanced_panel_open": advanced_open,
    }


def archive_public_list_active_filter_summary_context(
    *,
    q: str = "",
    item_type_filter: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
    category_choices: Sequence[object] = (),
    event_choices: Sequence[object] = (),
    tag_choices: Sequence[object] = (),
    person_choices: Sequence[object] = (),
) -> dict[str, object]:
    """Compact active-filter chips with canonical remove/edit/clear links."""
    from dataclasses import replace

    filters = advanced_filters or EMPTY_ARCHIVE_ADVANCED_FILTERS
    summary_items = build_archive_advanced_filter_summary_items(
        q=q,
        filters=filters,
        category_choices=category_choices,
        event_choices=event_choices,
        tag_choices=tag_choices,
        person_choices=person_choices,
    )
    chips: list[dict[str, object]] = []
    for item in summary_items:
        kind = str(item["kind"])
        remove_filters = filters
        remove_q = q
        if kind == "q":
            remove_q = ""
        elif kind == "author":
            remove_filters = archive_advanced_filters_without_author(filters)
        elif kind == "category":
            remove_filters = replace(filters, category_ids=())
        elif kind == "event":
            remove_filters = replace(filters, event_ids=())
        elif kind == "tag":
            remove_filters = replace(filters, tag_ids=())
        elif kind == "person":
            remove_filters = replace(filters, person_ids=())
        elif kind == "year":
            remove_filters = archive_advanced_filters_without_year(filters)
        remove_query = build_archive_public_list_query(
            q=remove_q,
            item_type_filter=item_type_filter,
            per_page=per_page,
            advanced_filters=remove_filters,
        )
        chips.append(
            {
                "kind": kind,
                "label": item["label"],
                "value": item["value"],
                "remove_href_suffix": f"?{remove_query}" if remove_query else "",
            }
        )

    edit_query = build_archive_public_list_query(
        q=q,
        item_type_filter=item_type_filter,
        per_page=per_page,
        advanced_filters=filters,
        advanced_open=True,
    )
    has_active = bool(q or filters.is_active())
    return {
        "active_filter_summary_visible": has_active,
        "active_filter_chips": chips,
        "edit_advanced_search_href_suffix": f"?{edit_query}" if edit_query else "",
        "clear_all_query_suffix": archive_public_list_clear_all_query_suffix(
            item_type_filter=item_type_filter,
            per_page=per_page,
        ),
    }


def _archive_public_list_href_suffix(
    *,
    q: str = "",
    item_type_filter: str = "",
    page: int = 1,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
) -> str:
    query = build_archive_public_list_query(
        q=q,
        item_type_filter=item_type_filter,
        page=page,
        per_page=per_page,
        advanced_filters=advanced_filters,
    )
    return f"?{query}" if query else ""


def _archive_public_list_page_number_sequence(
    total_pages: int,
    page: int,
) -> list[int | None]:
    """Return ordered page numbers and ellipsis markers for symmetric pagination."""
    if total_pages <= 0:
        return []
    if total_pages <= 6:
        return list(range(1, total_pages + 1))

    if page <= 3:
        left_end = 3 if page >= 3 else page + 1
    else:
        left_end = 2

    if page >= total_pages - 2:
        if page >= total_pages:
            right_start = total_pages - 1
        elif page == total_pages - 1:
            right_start = page - 1
        else:
            right_start = page
    else:
        right_start = total_pages - 1

    visible: set[int] = set(range(1, left_end + 1))
    visible.update(range(right_start, total_pages + 1))

    if page > left_end and page < right_start:
        visible.add(page)
        if total_pages > 7:
            if page - 1 > left_end:
                visible.add(page - 1)
            if page + 1 < right_start:
                visible.add(page + 1)

    sequence: list[int | None] = []
    previous_page: int | None = None
    for page_number in sorted(visible):
        if previous_page is not None and page_number - previous_page > 1:
            sequence.append(None)
        sequence.append(page_number)
        previous_page = page_number
    return sequence


def build_archive_public_list_page_number_items(
    *,
    total_pages: int,
    page: int,
    q: str = "",
    item_type_filter: str = "",
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    advanced_filters: ArchiveAdvancedFilters | None = None,
) -> list[dict[str, object]]:
    """Numbered page link metadata for public archive list pagination."""
    page_numbers = _archive_public_list_page_number_sequence(total_pages, page)

    items: list[dict[str, object]] = []
    for page_number in page_numbers:
        if page_number is None:
            items.append({"kind": "ellipsis"})
            continue
        items.append(
            {
                "kind": "page",
                "page": page_number,
                "is_current": page_number == page,
                "href_suffix": _archive_public_list_href_suffix(
                    q=q,
                    item_type_filter=item_type_filter,
                    page=page_number,
                    per_page=per_page,
                    advanced_filters=advanced_filters,
                ),
            }
        )
    return items


def archive_public_list_pagination_context(
    *,
    total_count: int,
    page: int,
    per_page: int,
    q: str,
    item_type_filter: str,
    advanced_filters: ArchiveAdvancedFilters | None = None,
) -> dict[str, object]:
    """Template context for public archive list pagination controls."""
    total_pages = (total_count + per_page - 1) // per_page if total_count > 0 else 0
    show_pagination = total_count > 0
    show_page_nav = total_pages > 1
    prev_href_suffix = ""
    next_href_suffix = ""
    if page > 1:
        prev_href_suffix = _archive_public_list_href_suffix(
            q=q,
            item_type_filter=item_type_filter,
            page=page - 1,
            per_page=per_page,
            advanced_filters=advanced_filters,
        )
    if page < total_pages:
        next_href_suffix = _archive_public_list_href_suffix(
            q=q,
            item_type_filter=item_type_filter,
            page=page + 1,
            per_page=per_page,
            advanced_filters=advanced_filters,
        )
    return {
        "page": page,
        "per_page": per_page,
        "total_count": total_count,
        "total_pages": total_pages,
        "show_pagination": show_pagination,
        "show_page_nav": show_page_nav,
        "per_page_options": ARCHIVE_PUBLIC_LIST_PER_PAGE_OPTIONS,
        "page_number_items": build_archive_public_list_page_number_items(
            total_pages=total_pages,
            page=page,
            q=q,
            item_type_filter=item_type_filter,
            per_page=per_page,
            advanced_filters=advanced_filters,
        ),
        "prev_href_suffix": prev_href_suffix,
        "next_href_suffix": next_href_suffix,
    }


ARCHIVE_BROWSE_PREVIEW_MAX_LEN = 160
ARCHIVE_BROWSE_PREVIEW_EMPTY = "אין תצוגה מקדימה זמינה."
ARCHIVE_BROWSE_OCR_PREVIEW = "פתחו את המסמך לצפייה בתוכן."

_TYPE_MARKER_BY_ITEM_TYPE: dict[str, str] = {
    ArchiveItem.ItemType.OCR_DOCUMENT: "ocr",
    ArchiveItem.ItemType.MANUAL_TEXT: "manual",
    ArchiveItem.ItemType.PHOTO: "photo",
    ArchiveItem.ItemType.VIDEO: "video",
}


@dataclass(frozen=True)
class ArchiveBrowseLink:
    name: str
    href: str


@dataclass(frozen=True)
class ArchiveSearchSnippetSegment:
    """Plain-text snippet segment for autoescaped template rendering (PR4)."""

    text: str
    is_match: bool = False


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
    person_links: tuple[ArchiveBrowseLink, ...] = ()
    thumbnail_url: str | None = None
    # Local 160px visual when no image thumbnail: "pdf" | "manual" | "video" | "".
    fallback_preview: str = ""
    # PR4 search presentation (empty / false when not searching or N/A).
    search_match_source_label: str = ""
    search_snippet_segments: tuple[ArchiveSearchSnippetSegment, ...] = ()
    show_search_snippet: bool = False


def _normalize_preview_source(text: str | None) -> str:
    return " ".join((text or "").split())


def _truncate_preview(
    text: str, *, max_len: int = ARCHIVE_BROWSE_PREVIEW_MAX_LEN
) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _manual_text_preview(archive_item: ArchiveItem) -> str:
    content = getattr(archive_item, "manual_text_content", None)
    if content is None:
        return ""
    return _truncate_preview(_normalize_preview_source(content.body))


def _photo_preview(archive_item: ArchiveItem) -> str:
    photo_content = archive_item.primary_photo_content
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


def _video_preview(archive_item: ArchiveItem) -> str:
    """Local provider label (+ optional public note); never fetches remote media."""
    content = getattr(archive_item, "video_content", None)
    provider_label = video_provider_display_label(
        getattr(content, "provider", None) if content is not None else None
    )
    note = _normalize_preview_source(getattr(archive_item, "public_note", None))
    if note:
        return _truncate_preview(f"{provider_label} · {note}")
    return provider_label


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
    if item_type == ArchiveItem.ItemType.VIDEO:
        preview = _video_preview(archive_item)
        return preview or ARCHIVE_BROWSE_PREVIEW_EMPTY
    return ARCHIVE_BROWSE_PREVIEW_EMPTY


def _type_marker_for_item(archive_item: ArchiveItem) -> str:
    return _TYPE_MARKER_BY_ITEM_TYPE.get(archive_item.item_type, "generic")


def _fallback_preview_for_item(archive_item: ArchiveItem) -> str:
    """Return a local visual fallback kind for browse cards without thumbnails.

    PHOTO and OCR IMAGE cards keep the small CSS type marker when they have no
    ``thumbnail_url``; PDF OCR, manual text, and video use a 160px placeholder.
    """
    item_type = archive_item.item_type
    if item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        return ARCHIVE_BROWSE_FALLBACK_PREVIEW_MANUAL
    if item_type == ArchiveItem.ItemType.VIDEO:
        return ARCHIVE_BROWSE_FALLBACK_PREVIEW_VIDEO
    if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        document = getattr(archive_item, "ocr_document", None)
        if document is not None and document.doc_type == Document.DocType.PDF:
            return ARCHIVE_BROWSE_FALLBACK_PREVIEW_PDF
    return ""


def _author_display_for_archive_item(archive_item: ArchiveItem) -> str:
    return (archive_item.author_name or "").strip()


def _prefetched_relation(archive_item: ArchiveItem, relation: str) -> Iterable:
    """Return a prefetched M2M relation when available.

    Falls back to ``relation.all()`` (one query per item). Archive browse views
    should call ``prefetch_related("categories", "events", "tags", "people")``
    before ``build_archive_browse_cards``.
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


def _category_links_for_item(
    archive_item: ArchiveItem,
) -> tuple[ArchiveBrowseLink, ...]:
    return _browse_links_for_relation(
        archive_item,
        "categories",
        url_name="archive-category-browse",
        id_kwarg="category_id",
    )


def public_discovery_tags(archive_item: ArchiveItem):
    """Return item Tags that may appear in public discovery, sorted by name.

    Historical person-name Tags are excluded by frozen Tag.id membership only.
    """
    blocked_ids = historical_person_name_tag_ids()
    return tuple(
        tag
        for tag in sorted(
            _prefetched_relation(archive_item, "tags"),
            key=lambda item: item.name,
        )
        if tag.id not in blocked_ids
    )


def person_archive_filter_url(person_id: int) -> str:
    """Return ``/archive/?person=<id>&advanced=1`` via the canonical list query."""
    query = build_archive_public_list_query(
        advanced_filters=ArchiveAdvancedFilters(person_ids=(person_id,)),
        advanced_open=True,
    )
    path = reverse("archive-list")
    return f"{path}?{query}" if query else path


def person_public_page_url(person_id: int) -> str:
    """Return ``/archive/people/<person_id>/``."""
    return reverse("archive-person-detail", kwargs={"person_id": person_id})


def person_links_for_item(archive_item: ArchiveItem) -> tuple[ArchiveBrowseLink, ...]:
    """Item-level ArchiveItemPerson links, ordered by ``(name, id)``.

    Identity is ``Person.id``. Duplicate names stay distinct. PhotoPerson is
    not read.
    """
    people = sorted(
        _prefetched_relation(archive_item, "people"),
        key=lambda person: (person.name, person.id),
    )
    return tuple(
        ArchiveBrowseLink(
            name=person.name,
            href=person_public_page_url(person.id),
        )
        for person in people
    )


def public_discovery_context(archive_item: ArchiveItem | None) -> dict:
    """Template context for public discovery Tags and Person links."""
    if archive_item is None:
        return {"public_tags": (), "person_links": ()}
    return {
        "public_tags": public_discovery_tags(archive_item),
        "person_links": person_links_for_item(archive_item),
    }


def _related_links_for_item(archive_item: ArchiveItem) -> tuple[ArchiveBrowseLink, ...]:
    event_links = _browse_links_for_relation(
        archive_item,
        "events",
        url_name="archive-event-browse",
        id_kwarg="event_id",
    )
    tag_links = tuple(
        ArchiveBrowseLink(
            name=tag.name,
            href=reverse("archive-tag-browse", kwargs={"tag_id": tag.id}),
        )
        for tag in public_discovery_tags(archive_item)
    )
    return event_links + tag_links


def archive_browse_displayable_text_results_prefetch() -> Prefetch:
    """Prefetch displayable OCR text rows for archive browse cards (avoids N+1)."""
    return archive_item_displayable_text_results_prefetch()


def build_archive_browse_card(
    archive_item: ArchiveItem,
    *,
    search_query: str = "",
) -> ArchiveBrowseCard:
    detail_url = reverse("archive-detail", kwargs={"item_id": archive_item.id})
    if search_query and archive_item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        detail_url = f"{detail_url}?{urlencode({'q': search_query})}"

    return ArchiveBrowseCard(
        item=archive_item,
        title=archive_item.title,
        type_label=archive_item_type_label(archive_item.item_type),
        type_marker=_type_marker_for_item(archive_item),
        date_display=format_document_date(archive_item),
        author_display=_author_display_for_archive_item(archive_item),
        detail_url=detail_url,
        preview_text=_preview_text_for_archive_item(archive_item),
        category_links=_category_links_for_item(archive_item),
        related_links=_related_links_for_item(archive_item),
        person_links=person_links_for_item(archive_item),
        fallback_preview=_fallback_preview_for_item(archive_item),
    )


def build_archive_browse_cards(
    items: Sequence[ArchiveItem] | Iterable[ArchiveItem],
    *,
    search_query: str = "",
) -> list[ArchiveBrowseCard]:
    """Build public browse cards for archive list and discovery browse pages.

    Callers should pass items from ``_archive_browse_select_related`` (or
    equivalent) so ``categories``, ``events``, ``tags``, ``people``, and
    displayable OCR ``DocumentTextResult`` rows are prefetched.
    """
    return [
        build_archive_browse_card(item, search_query=search_query) for item in items
    ]
