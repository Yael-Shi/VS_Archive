"""Map archive-search match indexes onto the displayed transcription DOM.

Uses the same ``ArchiveSearchGeometryMatch`` list and enumerate indexes that
feed ``data-archive-search-match-index`` on source-image overlays. Transcription
targets are emitted only when a match belongs to the exact
``DocumentTextResult`` currently displayed as the primary transcription.

Visible text is reconstructed only by slicing the displayed result text at
canonical offsets; character identity is verified before enabling markup.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import Document, DocumentTextResult
from documents.services.archive_search_match_ranges import (
    ArchiveSearchGeometryMatch,
)
from documents.services.text_line_hover_presentation import (
    TextLineHoverPresentation,
    TextLineHoverSegment,
)
from documents.services.text_presentation import (
    ResultTypeStr,
    resolve_displayed_transcription_result,
)


@dataclass(frozen=True)
class ArchiveSearchTranscriptionSegment:
    """One contiguous slice of the displayed transcription text."""

    text: str
    hover_line_id: str | None = None
    archive_search_match_index: int | None = None


@dataclass(frozen=True)
class ArchiveSearchTranscriptionPresentation:
    """Fail-closed transcription sync payload for archive-search matches."""

    enabled: bool
    result_type: ResultTypeStr | None
    text_result_id: int | None
    segments: tuple[ArchiveSearchTranscriptionSegment, ...]
    match_indexes: tuple[int, ...]


_DISABLED = ArchiveSearchTranscriptionPresentation(
    enabled=False,
    result_type=None,
    text_result_id=None,
    segments=(),
    match_indexes=(),
)


def _hover_ranges_from_segments(
    text: str,
    segments: tuple[TextLineHoverSegment, ...],
) -> tuple[tuple[int, int, str | None], ...] | None:
    """Return ``(start, end, hover_line_id)`` covering ``text``, or None."""
    ranges: list[tuple[int, int, str | None]] = []
    cursor = 0
    for segment in segments:
        piece = segment.text
        if text[cursor : cursor + len(piece)] != piece:
            return None
        end = cursor + len(piece)
        ranges.append((cursor, end, segment.hover_line_id))
        cursor = end
    if cursor != len(text):
        return None
    return tuple(ranges)


def _valid_match_ranges_for_displayed_result(
    matches: tuple[ArchiveSearchGeometryMatch, ...],
    *,
    text_result: DocumentTextResult,
    text: str,
) -> tuple[tuple[int, int, int], ...]:
    """Return non-overlapping ``(start, end, match_index)`` on ``text_result``.

    Invalid, out-of-range, or overlapping ranges are omitted (fail closed for
    that match index only). Indexes match ``enumerate(geometry_matches)``.
    """
    accepted: list[tuple[int, int, int]] = []

    for match_index, match in enumerate(matches):
        if match.text_result.pk != text_result.pk:
            continue
        start = match.start
        end = match.end
        if not (0 <= start < end <= len(text)):
            continue
        overlaps = any(
            not (end <= other_start or start >= other_end)
            for other_start, other_end, _ in accepted
        )
        if overlaps:
            continue
        accepted.append((start, end, match_index))

    return tuple(accepted)


def _hover_id_at(
    hover_ranges: tuple[tuple[int, int, str | None], ...],
    *,
    start: int,
    end: int,
) -> str | None:
    for range_start, range_end, hover_line_id in hover_ranges:
        if start >= range_start and end <= range_end:
            return hover_line_id
    return None


def _match_index_at(
    match_ranges: tuple[tuple[int, int, int], ...],
    *,
    start: int,
    end: int,
) -> int | None:
    for range_start, range_end, match_index in match_ranges:
        if start >= range_start and end <= range_end:
            return match_index
    return None


def _build_unified_segments(
    text: str,
    *,
    hover_ranges: tuple[tuple[int, int, str | None], ...],
    match_ranges: tuple[tuple[int, int, int], ...],
) -> tuple[ArchiveSearchTranscriptionSegment, ...] | None:
    cuts: set[int] = {0, len(text)}
    for start, end, _ in hover_ranges:
        cuts.add(start)
        cuts.add(end)
    for start, end, _ in match_ranges:
        cuts.add(start)
        cuts.add(end)

    ordered = sorted(cuts)
    segments: list[ArchiveSearchTranscriptionSegment] = []
    for left, right in zip(ordered, ordered[1:]):
        if left == right:
            continue
        piece = text[left:right]
        segments.append(
            ArchiveSearchTranscriptionSegment(
                text=piece,
                hover_line_id=_hover_id_at(hover_ranges, start=left, end=right),
                archive_search_match_index=_match_index_at(
                    match_ranges,
                    start=left,
                    end=right,
                ),
            )
        )

    joined = "".join(segment.text for segment in segments)
    if joined != text:
        return None
    return tuple(segments)


def build_archive_search_transcription_presentation(
    document: Document,
    *,
    geometry_matches: tuple[ArchiveSearchGeometryMatch, ...],
    text_line_hover: TextLineHoverPresentation | None = None,
) -> ArchiveSearchTranscriptionPresentation:
    """Build safe transcription anchors for geometry-backed search matches.

    Only matches whose ``text_result`` is the currently displayed transcription
    receive anchors. Match indexes are the same enumerate indexes used by
    source-image overlay targets. Hover line ids are preserved when the hover
    presentation is enabled for that same result.
    """
    if not geometry_matches:
        return _DISABLED

    text_result = resolve_displayed_transcription_result(document)
    if text_result is None:
        return _DISABLED

    text = text_result.text or ""
    if not text:
        return _DISABLED

    match_ranges = _valid_match_ranges_for_displayed_result(
        geometry_matches,
        text_result=text_result,
        text=text,
    )
    if not match_ranges:
        return _DISABLED

    hover_ranges: tuple[tuple[int, int, str | None], ...]
    if (
        text_line_hover is not None
        and text_line_hover.enabled
        and text_line_hover.text_result_id == text_result.pk
        and text_line_hover.segments
    ):
        resolved_hover = _hover_ranges_from_segments(text, text_line_hover.segments)
        if resolved_hover is None:
            return _DISABLED
        hover_ranges = resolved_hover
    else:
        hover_ranges = ((0, len(text), None),)

    segments = _build_unified_segments(
        text,
        hover_ranges=hover_ranges,
        match_ranges=match_ranges,
    )
    if segments is None:
        return _DISABLED

    if not any(segment.archive_search_match_index is not None for segment in segments):
        return _DISABLED

    return ArchiveSearchTranscriptionPresentation(
        enabled=True,
        result_type=text_result.result_type,  # type: ignore[arg-type]
        text_result_id=text_result.pk,
        segments=segments,
        match_indexes=tuple(match_index for _, _, match_index in match_ranges),
    )


__all__ = [
    "ArchiveSearchTranscriptionPresentation",
    "ArchiveSearchTranscriptionSegment",
    "build_archive_search_transcription_presentation",
]
