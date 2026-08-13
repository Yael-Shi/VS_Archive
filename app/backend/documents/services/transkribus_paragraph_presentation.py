"""Public Transkribus paragraph presentation overlay.

Groups existing hover/search/plain transcription fragments into human
paragraphs when a current paragraph mapping applies. Presentation only:
does not mutate canonical text, search offsets, hover IDs, or snapshot
lines.

No current mapping (including stale / other-snapshot / drifted text) leaves
``enabled=False`` so the public template keeps the legacy renderer.
A current mapping with zero break rows is one flowing paragraph, not fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from documents.models import (
    Document,
    TranskribusParagraphBreak,
    TranskribusSnapshotLine,
)
from documents.services.archive_search_transcription_presentation import (
    ArchiveSearchTranscriptionPresentation,
)
from documents.services.text_line_hover_presentation import (
    TextLineHoverPresentation,
)
from documents.services.text_presentation import (
    ResultTypeStr,
    resolve_displayed_transcription_result,
)
from documents.services.transkribus_paragraph_mapping import (
    assess_paragraph_mapping_currentness,
    contributing_lines_for_snapshot,
)


@dataclass(frozen=True)
class ParagraphPresentationFragment:
    """One canonical slice inside a paragraph, with existing identities."""

    text: str
    hover_line_id: str | None = None
    archive_search_match_index: int | None = None
    is_source_line: bool = False


@dataclass(frozen=True)
class TranscriptionParagraph:
    """One human paragraph wrapping existing presentation fragments."""

    fragments: tuple[ParagraphPresentationFragment, ...]


@dataclass(frozen=True)
class TranskribusParagraphPresentation:
    """Fail-closed public overlay; ``enabled=False`` means use legacy markup."""

    enabled: bool
    result_type: ResultTypeStr | None
    text_result_id: int | None
    paragraphs: tuple[TranscriptionParagraph, ...]


_DISABLED = TranskribusParagraphPresentation(
    enabled=False,
    result_type=None,
    text_result_id=None,
    paragraphs=(),
)


@dataclass(frozen=True)
class _InputFragment:
    text: str
    hover_line_id: str | None = None
    archive_search_match_index: int | None = None


def _input_fragments_from_hover(
    hover: TextLineHoverPresentation,
) -> tuple[_InputFragment, ...]:
    return tuple(
        _InputFragment(text=segment.text, hover_line_id=segment.hover_line_id)
        for segment in hover.segments
    )


def _input_fragments_from_search(
    search: ArchiveSearchTranscriptionPresentation,
) -> tuple[_InputFragment, ...]:
    return tuple(
        _InputFragment(
            text=segment.text,
            hover_line_id=segment.hover_line_id,
            archive_search_match_index=segment.archive_search_match_index,
        )
        for segment in search.segments
    )


def _paragraph_start_offsets(
    lines: Sequence[TranskribusSnapshotLine],
    *,
    break_after_ids: set[int],
    text_length: int,
) -> tuple[int, ...]:
    """Canonical offsets where a new paragraph begins.

    A break after line N starts the next paragraph at the next contributing
    line's ``char_start``. Page gaps stay inside whichever paragraph owns
    that offset range; they never become automatic paragraph starts.
    """
    starts: list[int] = [0]
    for index, line in enumerate(lines):
        if line.pk not in break_after_ids:
            continue
        if index + 1 >= len(lines):
            continue
        next_start = lines[index + 1].char_start
        if 0 < next_start < text_length and next_start not in starts:
            starts.append(next_start)
    return tuple(starts)


def _line_ranges(
    lines: Sequence[TranskribusSnapshotLine],
    *,
    text_length: int,
) -> tuple[tuple[int, int], ...]:
    ranges: list[tuple[int, int]] = []
    for line in lines:
        if line.char_end < line.char_start:
            return ()
        if line.char_start < 0 or line.char_end > text_length:
            return ()
        if line.char_end == line.char_start:
            continue
        ranges.append((line.char_start, line.char_end))
    return tuple(ranges)


def _is_source_line_range(
    start: int,
    end: int,
    line_ranges: tuple[tuple[int, int], ...],
) -> bool:
    if start >= end:
        return False
    for line_start, line_end in line_ranges:
        if start < line_end and end > line_start:
            return True
    return False


def _split_fragments_at_cuts(
    fragments: Sequence[_InputFragment],
    *,
    extra_cuts: set[int],
) -> tuple[tuple[int, int, _InputFragment], ...]:
    pieces: list[tuple[int, int, _InputFragment]] = []
    cursor = 0
    for fragment in fragments:
        start = cursor
        end = cursor + len(fragment.text)
        cuts = sorted({start, end, *(cut for cut in extra_cuts if start < cut < end)})
        for left, right in zip(cuts, cuts[1:]):
            if left == right:
                continue
            pieces.append(
                (
                    left,
                    right,
                    _InputFragment(
                        text=fragment.text[left - start : right - start],
                        hover_line_id=fragment.hover_line_id,
                        archive_search_match_index=fragment.archive_search_match_index,
                    ),
                )
            )
        cursor = end
    return tuple(pieces)


def _group_pieces(
    pieces: Sequence[tuple[int, int, _InputFragment]],
    *,
    paragraph_starts: Sequence[int],
    line_ranges: tuple[tuple[int, int], ...],
    text: str,
) -> tuple[TranscriptionParagraph, ...] | None:
    buckets: list[list[ParagraphPresentationFragment]] = [[] for _ in paragraph_starts]
    for start, end, fragment in pieces:
        paragraph_index = 0
        for index, paragraph_start in enumerate(paragraph_starts):
            if start >= paragraph_start:
                paragraph_index = index
            else:
                break
        buckets[paragraph_index].append(
            ParagraphPresentationFragment(
                text=fragment.text,
                hover_line_id=fragment.hover_line_id,
                archive_search_match_index=fragment.archive_search_match_index,
                is_source_line=_is_source_line_range(start, end, line_ranges),
            )
        )

    paragraphs = tuple(
        TranscriptionParagraph(fragments=tuple(fragments))
        for fragments in buckets
        if fragments
    )
    joined = "".join(
        fragment.text for paragraph in paragraphs for fragment in paragraph.fragments
    )
    if joined != text:
        return None
    return paragraphs


def _resolve_input_fragments(
    text: str,
    *,
    text_result_id: int,
    text_line_hover: TextLineHoverPresentation | None,
    archive_search_transcription: ArchiveSearchTranscriptionPresentation | None,
) -> tuple[_InputFragment, ...] | None:
    if (
        archive_search_transcription is not None
        and archive_search_transcription.enabled
        and archive_search_transcription.text_result_id == text_result_id
        and archive_search_transcription.segments
    ):
        fragments = _input_fragments_from_search(archive_search_transcription)
    elif (
        text_line_hover is not None
        and text_line_hover.enabled
        and text_line_hover.text_result_id == text_result_id
        and text_line_hover.segments
    ):
        fragments = _input_fragments_from_hover(text_line_hover)
    else:
        fragments = (_InputFragment(text=text),)

    joined = "".join(fragment.text for fragment in fragments)
    if joined != text:
        return None
    return fragments


def build_transkribus_paragraph_presentation(
    document: Document,
    *,
    text_line_hover: TextLineHoverPresentation | None = None,
    archive_search_transcription: ArchiveSearchTranscriptionPresentation | None = None,
) -> TranskribusParagraphPresentation:
    """Build paragraph wrappers for the currently displayed transcription.

    Reuses ``assess_paragraph_mapping_currentness`` for applicability.
    When the mapping is not current, returns ``enabled=False`` so callers
    keep the existing public renderer unchanged.
    """
    currentness = assess_paragraph_mapping_currentness(document)
    if not currentness.is_current or currentness.mapping is None:
        return _DISABLED

    text_result = resolve_displayed_transcription_result(document)
    if text_result is None or text_result.pk != currentness.displayed_text_result_id:
        return _DISABLED

    text = text_result.text or ""
    if not text:
        return _DISABLED

    mapping = currentness.mapping
    lines = contributing_lines_for_snapshot(mapping.snapshot)
    if not lines:
        return _DISABLED

    line_ranges = _line_ranges(lines, text_length=len(text))
    if not line_ranges:
        return _DISABLED

    break_after_ids = set(
        TranskribusParagraphBreak.objects.filter(mapping_id=mapping.pk).values_list(
            "after_line_id",
            flat=True,
        )
    )
    contributing_ids = {line.pk for line in lines}
    if not break_after_ids.issubset(contributing_ids):
        return _DISABLED

    input_fragments = _resolve_input_fragments(
        text,
        text_result_id=text_result.pk,
        text_line_hover=text_line_hover,
        archive_search_transcription=archive_search_transcription,
    )
    if input_fragments is None:
        return _DISABLED

    paragraph_starts = _paragraph_start_offsets(
        lines,
        break_after_ids=break_after_ids,
        text_length=len(text),
    )
    extra_cuts = set(paragraph_starts)
    extra_cuts.add(len(text))
    for line_start, line_end in line_ranges:
        extra_cuts.add(line_start)
        extra_cuts.add(line_end)

    pieces = _split_fragments_at_cuts(input_fragments, extra_cuts=extra_cuts)

    paragraphs = _group_pieces(
        pieces,
        paragraph_starts=paragraph_starts,
        line_ranges=line_ranges,
        text=text,
    )
    if not paragraphs:
        return _DISABLED

    return TranskribusParagraphPresentation(
        enabled=True,
        result_type=text_result.result_type,  # type: ignore[arg-type]
        text_result_id=text_result.pk,
        paragraphs=paragraphs,
    )


__all__ = [
    "ParagraphPresentationFragment",
    "TranscriptionParagraph",
    "TranskribusParagraphPresentation",
    "build_transkribus_paragraph_presentation",
]
