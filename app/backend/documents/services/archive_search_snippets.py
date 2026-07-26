"""Safe contextual snippets and match-source labels for public archive search (PR4).

Snippets are built only for an already-authorized page slice. Matching/ranking
remain in ``filter_archive_items_by_search_query``; this module never changes
whether a row matches.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from documents.models import ArchiveItem, ArchiveItemSearchIndex
from documents.services.archive_item_presentation import (
    ARCHIVE_LIST_SEARCH_SEARCH,
    ArchiveBrowseCard,
    ArchiveSearchSnippetSegment,
    resolve_archive_list_search_terms,
)

# Target ~160–220 characters; prefer the middle of that band.
ARCHIVE_SEARCH_SNIPPET_TARGET_LEN = 190
ARCHIVE_SEARCH_SNIPPET_MAX_LEN = 220

MATCH_SOURCE_OCR_BODY = "נמצא בתעתוק"
MATCH_SOURCE_MANUAL_BODY = "נמצא בטקסט"
MATCH_SOURCE_ITEM_DETAILS = "נמצא בפרטי הפריט"
MATCH_SOURCE_AUTHOR = "נמצא במחבר/ת"
MATCH_SOURCE_SOURCE_TITLE = "נמצא בכותרת המקור"
MATCH_SOURCE_PUBLIC_NOTE = "נמצא בהערה"
MATCH_SOURCE_CATEGORIES = "נמצא בקטגוריות"
MATCH_SOURCE_EVENTS = "נמצא באירועים"
MATCH_SOURCE_TAGS = "נמצא בתגיות"


@dataclass(frozen=True)
class ArchiveSearchMatchPresentation:
    """Presentation extras for one search result card."""

    match_source_label: str
    snippet_segments: tuple[ArchiveSearchSnippetSegment, ...]
    replaces_preview: bool


def _normalize_snippet_source(text: str | None) -> str:
    return " ".join((text or "").split())


def _is_token_char(ch: str) -> bool:
    # Align with PR3 term separators: letters/digits are tokens; underscore is not.
    return ch.isalnum()


def iter_token_spans(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, token)`` spans for alphanumeric tokens."""
    spans: list[tuple[int, int, str]] = []
    index = 0
    length = len(text)
    while index < length:
        if _is_token_char(text[index]):
            end = index + 1
            while end < length and _is_token_char(text[end]):
                end += 1
            spans.append((index, end, text[index:end]))
            index = end
        else:
            index += 1
    return spans


def short_field_contains_any_term(field: str | None, terms: Sequence[str]) -> bool:
    """Substring match aligned with PR3 ``icontains`` on short discovery fields."""
    haystack = (field or "").casefold()
    if not haystack:
        return False
    return any(term.casefold() in haystack for term in terms if term)


def body_contains_any_term(body: str | None, terms: Sequence[str]) -> bool:
    """Whole-token match for body text (no substring / morphology)."""
    normalized = _normalize_snippet_source(body)
    if not normalized:
        return False
    term_set = {term.casefold() for term in terms if term}
    if not term_set:
        return False
    return any(
        token.casefold() in term_set for _, _, token in iter_token_spans(normalized)
    )


def _match_spans_for_terms(
    text: str, terms: Sequence[str]
) -> list[tuple[int, int, str]]:
    term_set = {term.casefold() for term in terms if term}
    return [
        (start, end, token.casefold())
        for start, end, token in iter_token_spans(text)
        if token.casefold() in term_set
    ]


def _snap_window_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Prefer whole-token boundaries when a cut lands mid-token."""
    length = len(text)
    start = max(0, min(start, length))
    end = max(start, min(end, length))

    if start > 0 and start < length and _is_token_char(text[start]):
        if _is_token_char(text[start - 1]):
            while start > 0 and _is_token_char(text[start - 1]):
                start -= 1

    if end < length and end > 0 and _is_token_char(text[end - 1]):
        if _is_token_char(text[end]):
            while end < length and _is_token_char(text[end]):
                end += 1

    return start, end


def _expand_window(text: str, core_start: int, core_end: int) -> tuple[int, int]:
    """Expand a match core toward the target length, then snap to word edges."""
    length = len(text)
    core_start = max(0, core_start)
    core_end = min(length, core_end)
    if core_end < core_start:
        core_end = core_start

    core_len = core_end - core_start
    if core_len >= ARCHIVE_SEARCH_SNIPPET_MAX_LEN:
        # Deterministic fallback: window anchored at the earliest core start.
        end = min(length, core_start + ARCHIVE_SEARCH_SNIPPET_MAX_LEN)
        return _snap_window_to_words(text, core_start, end)

    budget = min(ARCHIVE_SEARCH_SNIPPET_TARGET_LEN, ARCHIVE_SEARCH_SNIPPET_MAX_LEN)
    need = max(0, budget - core_len)
    left = need // 2
    right = need - left
    start = max(0, core_start - left)
    end = min(length, core_end + right)
    unused_left = left - (core_start - start)
    unused_right = right - (end - core_end)
    if unused_left:
        end = min(length, end + unused_left)
    if unused_right:
        start = max(0, start - unused_right)

    if end - start > ARCHIVE_SEARCH_SNIPPET_MAX_LEN:
        # Keep the core; trim evenly from the expanded sides.
        overflow = (end - start) - ARCHIVE_SEARCH_SNIPPET_MAX_LEN
        trim_left = min(core_start - start, overflow // 2)
        start += trim_left
        overflow -= trim_left
        end -= min(end - core_end, overflow)

    return _snap_window_to_words(text, start, end)


def select_snippet_window(text: str, terms: Sequence[str]) -> tuple[int, int] | None:
    """
    Choose one deterministic snippet window.

    Prefers the window covering the greatest number of distinct query terms
    within ``ARCHIVE_SEARCH_SNIPPET_MAX_LEN``, then the earliest suitable match.
    """
    normalized = _normalize_snippet_source(text)
    if not normalized:
        return None
    matches = _match_spans_for_terms(normalized, terms)
    if not matches:
        return None

    best_key: tuple[int, int, int] | None = None
    best_window: tuple[int, int] | None = None

    for i, (start_i, _end_i, _term_i) in enumerate(matches):
        distinct: set[str] = set()
        for j in range(i, len(matches)):
            start_j, end_j, term_j = matches[j]
            if end_j - start_i > ARCHIVE_SEARCH_SNIPPET_MAX_LEN:
                break
            distinct.add(term_j)
            window = _expand_window(normalized, start_i, end_j)
            # Maximize distinct terms; tie-break by earliest match, then window start.
            key = (-len(distinct), start_i, window[0])
            if best_key is None or key < best_key:
                best_key = key
                best_window = window

    return best_window


def build_highlighted_snippet_segments(
    text: str,
    terms: Sequence[str],
    *,
    window: tuple[int, int] | None = None,
) -> tuple[ArchiveSearchSnippetSegment, ...]:
    """Escape-ready plain segments with match flags; caller/template escapes text."""
    normalized = _normalize_snippet_source(text)
    if not normalized:
        return ()

    if window is None:
        window = select_snippet_window(normalized, terms)
    if window is None:
        return ()

    start, end = window
    snippet = normalized[start:end]
    lead = start > 0
    trail = end < len(normalized)

    term_set = {term.casefold() for term in terms if term}
    segments: list[ArchiveSearchSnippetSegment] = []
    if lead:
        segments.append(ArchiveSearchSnippetSegment(text="…", is_match=False))

    cursor = 0
    for token_start, token_end, token in iter_token_spans(snippet):
        if token.casefold() not in term_set:
            continue
        if token_start > cursor:
            segments.append(
                ArchiveSearchSnippetSegment(
                    text=snippet[cursor:token_start],
                    is_match=False,
                )
            )
        segments.append(
            ArchiveSearchSnippetSegment(
                text=snippet[token_start:token_end],
                is_match=True,
            )
        )
        cursor = token_end
    if cursor < len(snippet):
        segments.append(
            ArchiveSearchSnippetSegment(text=snippet[cursor:], is_match=False)
        )

    if trail:
        segments.append(ArchiveSearchSnippetSegment(text="…", is_match=False))
    return tuple(segments)


def _prefetched_relation_rows(archive_item: ArchiveItem, relation: str) -> list:
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and relation in cache:
        return list(cache[relation])
    return list(getattr(archive_item, relation).all())


def resolve_metadata_match_source_label(
    archive_item: ArchiveItem,
    terms: Sequence[str],
) -> str | None:
    """
    Accurate Hebrew label for metadata/public_note/discovery hits.

    Returns a specific label when exactly one metadata source matches; otherwise
    the generic item-details label when any metadata source matches.
    """
    hits: list[str] = []
    if short_field_contains_any_term(archive_item.author_name, terms):
        hits.append(MATCH_SOURCE_AUTHOR)
    if short_field_contains_any_term(archive_item.source_title, terms):
        hits.append(MATCH_SOURCE_SOURCE_TITLE)
    if short_field_contains_any_term(archive_item.public_note, terms):
        hits.append(MATCH_SOURCE_PUBLIC_NOTE)
    if any(
        short_field_contains_any_term(row.name, terms)
        for row in _prefetched_relation_rows(archive_item, "categories")
    ):
        hits.append(MATCH_SOURCE_CATEGORIES)
    if any(
        short_field_contains_any_term(row.name, terms)
        for row in _prefetched_relation_rows(archive_item, "events")
    ):
        hits.append(MATCH_SOURCE_EVENTS)
    if any(
        short_field_contains_any_term(row.name, terms)
        for row in _prefetched_relation_rows(archive_item, "tags")
    ):
        hits.append(MATCH_SOURCE_TAGS)

    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    return MATCH_SOURCE_ITEM_DETAILS


def _body_match_source_label(archive_item: ArchiveItem) -> str | None:
    if archive_item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        return MATCH_SOURCE_OCR_BODY
    if archive_item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        return MATCH_SOURCE_MANUAL_BODY
    return None


def build_archive_search_match_presentation(
    *,
    archive_item: ArchiveItem,
    search_index: ArchiveItemSearchIndex | None,
    terms: Sequence[str],
) -> ArchiveSearchMatchPresentation | None:
    """
    Build match-source / snippet presentation for one authorized result.

    Prefers a body contextual snippet when body tokens match. Title-only hits
    add no extras (title is already visible). Metadata-only hits add a label
    without fabricating a body excerpt.
    """
    if not terms:
        return None

    body_text = ""
    title_text = archive_item.title or ""
    if search_index is not None:
        body_text = search_index.body_text or ""
        title_text = search_index.title_text or title_text

    if body_contains_any_term(body_text, terms):
        window = select_snippet_window(body_text, terms)
        segments = build_highlighted_snippet_segments(body_text, terms, window=window)
        label = _body_match_source_label(archive_item)
        if segments and label:
            return ArchiveSearchMatchPresentation(
                match_source_label=label,
                snippet_segments=segments,
                replaces_preview=True,
            )

    metadata_label = resolve_metadata_match_source_label(archive_item, terms)
    if metadata_label:
        return ArchiveSearchMatchPresentation(
            match_source_label=metadata_label,
            snippet_segments=(),
            replaces_preview=False,
        )

    if short_field_contains_any_term(title_text, terms):
        # Title is already on the card; do not invent a body snippet.
        return None

    # Result matched (e.g. FTS) but no reliable UI source to claim.
    return None


def load_archive_search_indexes_for_item_ids(
    item_ids: Sequence[int],
) -> dict[int, ArchiveItemSearchIndex]:
    """One bounded query for page-slice search-index rows (no N+1)."""
    if not item_ids:
        return {}
    return {
        row.archive_item_id: row
        for row in ArchiveItemSearchIndex.objects.filter(
            archive_item_id__in=list(item_ids)
        ).only("archive_item_id", "title_text", "metadata_text", "body_text")
    }


def apply_archive_search_match_presentation_to_cards(
    cards: Sequence[ArchiveBrowseCard],
    *,
    search_query: str,
    search_indexes_by_item_id: Mapping[int, ArchiveItemSearchIndex] | None = None,
) -> list[ArchiveBrowseCard]:
    """
    Attach PR4 search presentation to browse cards for the current page only.

    No-op when ``q`` is not an effective search. Loads index rows in one query
    when ``search_indexes_by_item_id`` is omitted.
    """
    resolved = resolve_archive_list_search_terms(search_query)
    if resolved.outcome != ARCHIVE_LIST_SEARCH_SEARCH:
        return list(cards)
    if not cards:
        return []

    terms = resolved.terms
    indexes = (
        dict(search_indexes_by_item_id)
        if search_indexes_by_item_id is not None
        else load_archive_search_indexes_for_item_ids([card.item.pk for card in cards])
    )

    enriched: list[ArchiveBrowseCard] = []
    for card in cards:
        presentation = build_archive_search_match_presentation(
            archive_item=card.item,
            search_index=indexes.get(card.item.pk),
            terms=terms,
        )
        if presentation is None:
            enriched.append(card)
            continue
        enriched.append(
            replace(
                card,
                search_match_source_label=presentation.match_source_label,
                search_snippet_segments=presentation.snippet_segments,
                show_search_snippet=presentation.replaces_preview,
            )
        )
    return enriched
