"""Resolve public archive search terms to exact displayed-text ranges.

This module deliberately does not attempt to reproduce PostgreSQL full-text
search semantics. Public archive search may match a document through FTS even
when there is no exact literal substring that can safely be mapped back to
DocumentTextResult.text.

For hover / jump-to-match, only exact case-insensitive literal occurrences in
the currently displayed DocumentTextResult are returned. Anything ambiguous
fails closed by returning no range for that term.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from documents.models import Document, DocumentTextResult

if TYPE_CHECKING:
    from documents.services.transkribus_text_range_geometry import (
        TextRangeLineGeometry,
    )
from documents.services.archive_item_presentation import (
    ARCHIVE_LIST_SEARCH_SEARCH,
    resolve_archive_list_search_terms,
)
from documents.services.text_presentation import (
    resolve_displayed_hebrew_translation_result,
    resolve_displayed_transcription_result,
)


@dataclass(frozen=True)
class ArchiveSearchTextMatch:
    """One exact occurrence in a displayed DocumentTextResult."""

    term: str
    text_result: DocumentTextResult
    start: int
    end: int


def _literal_case_insensitive_ranges(
    text: str,
    term: str,
) -> tuple[tuple[int, int], ...]:
    """Return non-overlapping case-insensitive literal ranges.

    Unicode case conversion can change string length for some characters.
    Offsets are safe only when lowercasing preserves the original text and term
    lengths. Otherwise fail closed rather than return incorrect offsets.
    """
    if not text or not term:
        return ()

    folded_text = text.lower()
    folded_term = term.lower()

    if len(folded_text) != len(text) or len(folded_term) != len(term):
        return ()

    ranges: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = folded_text.find(folded_term, cursor)
        if start < 0:
            break
        end = start + len(term)
        ranges.append((start, end))
        cursor = end

    return tuple(ranges)


def _matches_for_result(
    text_result: DocumentTextResult | None,
    *,
    terms: tuple[str, ...],
) -> tuple[ArchiveSearchTextMatch, ...]:
    if text_result is None:
        return ()

    text = text_result.text or ""
    matches: list[ArchiveSearchTextMatch] = []

    for term in terms:
        for start, end in _literal_case_insensitive_ranges(text, term):
            matches.append(
                ArchiveSearchTextMatch(
                    term=term,
                    text_result=text_result,
                    start=start,
                    end=end,
                )
            )

    return tuple(matches)


def resolve_archive_search_text_matches(
    document: Document,
    *,
    search_query: str,
) -> tuple[ArchiveSearchTextMatch, ...]:
    """Resolve exact safe text occurrences for an archive search query.

    Uses the same query tokenization contract as public ``/archive/?q=``.

    Searches both displayed text surfaces that are independently searchable:
    the primary displayed transcription and, for non-Hebrew documents when
    applicable, the displayed Hebrew translation.

    This is intentionally narrower than PostgreSQL FTS. A document may be a
    valid public-search result while this function returns no match for one or
    more terms. Callers must treat that as "no hover/jump target", never as a
    reason to remove the search result itself.
    """
    resolved = resolve_archive_list_search_terms(search_query)
    if resolved.outcome != ARCHIVE_LIST_SEARCH_SEARCH:
        return ()

    transcription = resolve_displayed_transcription_result(document)
    translation = resolve_displayed_hebrew_translation_result(document)

    results: list[ArchiveSearchTextMatch] = []
    results.extend(_matches_for_result(transcription, terms=resolved.terms))

    if translation is not None and (
        transcription is None or translation.pk != transcription.pk
    ):
        results.extend(_matches_for_result(translation, terms=resolved.terms))

    return tuple(results)


@dataclass(frozen=True)
class ArchiveSearchGeometryMatch:
    """One exact displayed-text search occurrence with trusted line geometry."""

    term: str
    text_result: DocumentTextResult
    start: int
    end: int
    geometry: tuple["TextRangeLineGeometry", ...]


def resolve_archive_search_geometry_matches(
    document: Document,
    *,
    search_query: str,
) -> tuple[ArchiveSearchGeometryMatch, ...]:
    """Return exact search occurrences that also have trusted Transkribus geometry.

    This is strictly a projection of ``resolve_archive_search_text_matches``.
    It never affects whether the archive item itself matches public search.

    Literal occurrences that cannot be resolved through the current trusted
    Transkribus binding are omitted rather than assigned approximate geometry.
    """
    from documents.services.transkribus_text_range_geometry import (
        resolve_text_range_geometry,
    )

    resolved: list[ArchiveSearchGeometryMatch] = []

    for match in resolve_archive_search_text_matches(
        document,
        search_query=search_query,
    ):
        geometry = resolve_text_range_geometry(
            match.text_result,
            start=match.start,
            end=match.end,
        )
        if not geometry:
            continue

        resolved.append(
            ArchiveSearchGeometryMatch(
                term=match.term,
                text_result=match.text_result,
                start=match.start,
                end=match.end,
                geometry=geometry,
            )
        )

    return tuple(resolved)


__all__ = [
    "ArchiveSearchGeometryMatch",
    "ArchiveSearchTextMatch",
    "resolve_archive_search_geometry_matches",
    "resolve_archive_search_text_matches",
]
