"""Provider-independent printed-Arabic band geometry (Arm E structural-gap-v3-hybrid).

This module is pure: typed boxes in, typed rectangles out. It does not load
images, interpret EXIF, encode JPEG, call providers, or parse Vision JSON.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


BANDING_STRATEGY = "structural-gap-v3-hybrid"
MAX_BANDS = 6
MAX_BAND_HEIGHT_RATIO = 0.35

REASON_INVALID_IMAGE_DIMENSIONS = "invalid_image_dimensions"
REASON_INVALID_BANDING_CONFIG = "invalid_banding_config"
REASON_EMPTY_GEOMETRY = "empty_geometry"
REASON_INVALID_BOX = "invalid_box"
REASON_UNORDERED_LINES = "unordered_lines"
REASON_LINE_EXCEEDS_MAX_HEIGHT = "line_exceeds_max_height"
REASON_CANNOT_COVER = "cannot_cover"
REASON_EXCEEDS_MAX_BANDS = "exceeds_max_bands"
REASON_UNSAFE_CUT = "unsafe_cut"
REASON_EMPTY_BAND = "empty_band"
REASON_OVERLAPPING_BANDS = "overlapping_bands"
REASON_UNORDERED_BANDS = "unordered_bands"
REASON_WORD_CROSSES_CUT = "word_crosses_cut"
REASON_WORD_ASSIGNMENT = "word_assignment"


class ArabicPrintedBandingError(ValueError):
    """Fail-closed geometry error. Message must not include transcription text."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ArabicPrintedWordBox:
    index: int
    xmin: int
    ymin: int
    xmax: int
    ymax: int


@dataclass(frozen=True)
class ArabicPrintedLineBox:
    ymin: int
    ymax: int
    word_indexes: tuple[int, ...]


@dataclass(frozen=True)
class ArabicPrintedBandRect:
    """Inclusive-top, exclusive-bottom, full-width band in source pixels."""

    band_index: int
    left: int
    top: int
    right: int
    bottom: int
    word_indexes: tuple[int, ...]
    preceding_gap_pixels: int | None
    line_start: int
    line_end: int


def _is_int(value: object) -> bool:
    return type(value) is int


def _require_positive_dimension(value: object, *, name: str) -> int:
    if not _is_int(value) or value <= 0:
        raise ArabicPrintedBandingError(
            f"{name} must be a positive integer",
            reason=REASON_INVALID_IMAGE_DIMENSIONS,
        )
    return value


def _require_banding_config(
    *,
    max_bands: object,
    max_height_ratio: object,
) -> tuple[int, float]:
    if not _is_int(max_bands) or max_bands < 1 or max_bands > MAX_BANDS:
        raise ArabicPrintedBandingError(
            "max_bands must be an integer in 1..MAX_BANDS",
            reason=REASON_INVALID_BANDING_CONFIG,
        )
    if type(max_height_ratio) is bool or type(max_height_ratio) not in {int, float}:
        raise ArabicPrintedBandingError(
            "max_height_ratio must be a finite real in (0, 1]",
            reason=REASON_INVALID_BANDING_CONFIG,
        )
    ratio = float(max_height_ratio)
    if not math.isfinite(ratio) or ratio <= 0 or ratio > 1:
        raise ArabicPrintedBandingError(
            "max_height_ratio must be a finite real in (0, 1]",
            reason=REASON_INVALID_BANDING_CONFIG,
        )
    return max_bands, ratio


def line_content_height(
    lines: Sequence[ArabicPrintedLineBox], start: int, end: int
) -> int:
    return lines[end].ymax - lines[start].ymin + 1


def gap_pixels(lines: Sequence[ArabicPrintedLineBox], after_line: int) -> int:
    return lines[after_line + 1].ymin - lines[after_line].ymax - 1


def _partition_better(
    candidate: tuple[tuple[int, ...], tuple[int, ...]],
    current: tuple[tuple[int, ...], tuple[int, ...]],
) -> bool:
    cand_gaps, cand_cuts = candidate
    cur_gaps, cur_cuts = current
    if cand_gaps != cur_gaps:
        return cand_gaps > cur_gaps
    return cand_cuts < cur_cuts


def _validate_word_box(
    word: ArabicPrintedWordBox,
    *,
    image_width: int,
    image_height: int,
) -> None:
    if not all(
        _is_int(value)
        for value in (word.index, word.xmin, word.ymin, word.xmax, word.ymax)
    ):
        raise ArabicPrintedBandingError(
            "Word box coordinates and index must be integers",
            reason=REASON_INVALID_BOX,
        )
    if word.xmin > word.xmax or word.ymin > word.ymax:
        raise ArabicPrintedBandingError(
            "Word box min/max are inverted",
            reason=REASON_INVALID_BOX,
        )
    if (
        word.xmin < 0
        or word.ymin < 0
        or word.xmax >= image_width
        or word.ymax >= image_height
    ):
        raise ArabicPrintedBandingError(
            "Word box is outside the source image",
            reason=REASON_INVALID_BOX,
        )


def _validate_line_box(line: ArabicPrintedLineBox, *, image_height: int) -> None:
    if not all(
        _is_int(value)
        for value in (line.ymin, line.ymax, *line.word_indexes)
    ):
        raise ArabicPrintedBandingError(
            "Line box coordinates and word indexes must be integers",
            reason=REASON_INVALID_BOX,
        )
    if not (0 <= line.ymin <= line.ymax < image_height):
        raise ArabicPrintedBandingError(
            "Line box is outside the source image",
            reason=REASON_INVALID_BOX,
        )
    if not line.word_indexes:
        raise ArabicPrintedBandingError(
            "Line box has no word indexes",
            reason=REASON_EMPTY_GEOMETRY,
        )


def cluster_arabic_printed_lines(
    words: Sequence[ArabicPrintedWordBox],
) -> list[ArabicPrintedLineBox]:
    if not words:
        raise ArabicPrintedBandingError(
            "Cannot plan bands without recognized text lines",
            reason=REASON_EMPTY_GEOMETRY,
        )
    ordered = sorted(words, key=lambda word: (word.ymin, word.ymax, word.index))
    groups: list[list[ArabicPrintedWordBox]] = []
    for word in ordered:
        if groups:
            current = groups[-1]
            line_ymax = max(item.ymax for item in current)
            if word.ymin <= line_ymax:
                current.append(word)
                continue
        groups.append([word])
    clustered: list[ArabicPrintedLineBox] = []
    for group in groups:
        clustered.append(
            ArabicPrintedLineBox(
                ymin=min(item.ymin for item in group),
                ymax=max(item.ymax for item in group),
                word_indexes=tuple(item.index for item in group),
            )
        )
    return clustered


def _validate_line_sequence(
    lines: Sequence[ArabicPrintedLineBox],
    *,
    image_height: int,
) -> None:
    if not lines:
        raise ArabicPrintedBandingError(
            "Cannot plan bands without recognized text lines",
            reason=REASON_EMPTY_GEOMETRY,
        )
    seen_indexes: set[int] = set()
    previous: ArabicPrintedLineBox | None = None
    for line in lines:
        _validate_line_box(line, image_height=image_height)
        for word_index in line.word_indexes:
            if word_index in seen_indexes:
                raise ArabicPrintedBandingError(
                    "Word indexes must be unique across line groups",
                    reason=REASON_INVALID_BOX,
                )
            seen_indexes.add(word_index)
        if previous is not None:
            if line.ymin < previous.ymin:
                raise ArabicPrintedBandingError(
                    "Line boxes are not vertically ordered",
                    reason=REASON_UNORDERED_LINES,
                )
            if line.ymin <= previous.ymax:
                raise ArabicPrintedBandingError(
                    "Cuts cannot split a detected line",
                    reason=REASON_UNSAFE_CUT,
                )
        previous = line


def plan_arabic_printed_line_groups(
    lines: Sequence[ArabicPrintedLineBox],
    *,
    image_height: int,
    max_bands: int = MAX_BANDS,
    max_height_ratio: float = MAX_BAND_HEIGHT_RATIO,
) -> list[tuple[int, int]]:
    image_height = _require_positive_dimension(image_height, name="image_height")
    max_bands, max_height_ratio = _require_banding_config(
        max_bands=max_bands,
        max_height_ratio=max_height_ratio,
    )
    _validate_line_sequence(lines, image_height=image_height)
    max_height = max_height_ratio * image_height
    pct = f"{max_height_ratio:.0%}"
    for line in lines:
        if (line.ymax - line.ymin) + 1 > max_height:
            raise ArabicPrintedBandingError(
                f"A recognized text line exceeds the {pct} source-image height bound "
                "and cannot be split",
                reason=REASON_LINE_EXCEEDS_MAX_HEIGHT,
            )
    line_count = len(lines)
    uncoverable = line_count + 1
    min_bands = [uncoverable] * line_count
    for end in range(line_count):
        for start in range(end + 1):
            if line_content_height(lines, start, end) > max_height:
                continue
            previous = 0 if start == 0 else min_bands[start - 1]
            needed = previous + 1
            if needed < min_bands[end]:
                min_bands[end] = needed
    band_count = min_bands[line_count - 1]
    if band_count >= uncoverable:
        raise ArabicPrintedBandingError(
            f"Safe banding cannot cover the page within the {pct} height bound",
            reason=REASON_CANNOT_COVER,
        )
    if band_count > max_bands:
        raise ArabicPrintedBandingError(
            f"Safe banding requires {band_count} bands, which exceeds the maximum of "
            f"{max_bands}",
            reason=REASON_EXCEEDS_MAX_BANDS,
        )
    best: list[list[tuple[tuple[int, ...], tuple[int, ...], int] | None]] = [
        [None] * (band_count + 1) for _ in range(line_count)
    ]
    for end in range(line_count):
        if line_content_height(lines, 0, end) <= max_height:
            best[end][1] = ((), (), 0)
    for end in range(line_count):
        for start in range(1, end + 1):
            if line_content_height(lines, start, end) > max_height:
                continue
            for prev_bands in range(1, band_count):
                previous_state = best[start - 1][prev_bands]
                if previous_state is None:
                    continue
                prev_gaps, prev_cuts, _prev_start = previous_state
                gap = gap_pixels(lines, start - 1)
                candidate_gaps = tuple(sorted(prev_gaps + (gap,), reverse=True))
                candidate_cuts = prev_cuts + (start - 1,)
                candidate_key = (candidate_gaps, candidate_cuts)
                current_state = best[end][prev_bands + 1]
                current_key = (
                    None
                    if current_state is None
                    else (current_state[0], current_state[1])
                )
                if current_key is None or _partition_better(candidate_key, current_key):
                    best[end][prev_bands + 1] = (
                        candidate_gaps,
                        candidate_cuts,
                        start,
                    )
    chosen = best[line_count - 1][band_count]
    if chosen is None:
        raise ArabicPrintedBandingError(
            f"Safe banding cannot cover the page within the {pct} height bound",
            reason=REASON_CANNOT_COVER,
        )
    _gaps, cuts, _start = chosen
    groups: list[tuple[int, int]] = []
    start = 0
    for cut_end in cuts:
        groups.append((start, cut_end))
        start = cut_end + 1
    groups.append((start, line_count - 1))
    return groups


def _cut_in_gap(prev_ymax: int, next_ymin: int) -> int:
    gap_lo = prev_ymax + 1
    gap_hi = next_ymin
    if gap_hi < gap_lo:
        raise ArabicPrintedBandingError(
            "Cannot place a band cut that would split a recognized line",
            reason=REASON_UNSAFE_CUT,
        )
    return (gap_lo + gap_hi) // 2


def band_rects_from_line_groups(
    lines: Sequence[ArabicPrintedLineBox],
    groups: Sequence[tuple[int, int]],
    *,
    image_width: int,
) -> tuple[list[tuple[int, int, int, int]], list[int | None]]:
    image_width = _require_positive_dimension(image_width, name="image_width")
    if not groups:
        raise ArabicPrintedBandingError(
            "Cannot plan bands without recognized text lines",
            reason=REASON_EMPTY_GEOMETRY,
        )
    rects: list[tuple[int, int, int, int]] = []
    preceding_gaps: list[int | None] = []
    for group_index, (start, end) in enumerate(groups):
        if group_index == 0:
            top = lines[start].ymin
            preceding_gaps.append(None)
        else:
            prev_end = groups[group_index - 1][1]
            top = _cut_in_gap(lines[prev_end].ymax, lines[start].ymin)
            preceding_gaps.append(gap_pixels(lines, prev_end))
        if group_index + 1 < len(groups):
            next_start = groups[group_index + 1][0]
            bottom = _cut_in_gap(lines[end].ymax, lines[next_start].ymin)
        else:
            bottom = lines[end].ymax + 1
        if bottom <= top:
            raise ArabicPrintedBandingError(
                "Planned band rectangle is empty",
                reason=REASON_EMPTY_BAND,
            )
        rects.append((0, top, image_width, bottom))
    for index in range(1, len(rects)):
        _prev_left, _prev_top, _prev_right, prev_bottom = rects[index - 1]
        _left, top, _right, _bottom = rects[index]
        if top < prev_bottom:
            raise ArabicPrintedBandingError(
                "Planned bands overlap",
                reason=REASON_OVERLAPPING_BANDS,
            )
        if top < rects[index - 1][1]:
            raise ArabicPrintedBandingError(
                "Planned bands are not vertically ordered",
                reason=REASON_UNORDERED_BANDS,
            )
    return rects, preceding_gaps


def assign_words_to_band_rects(
    words: Sequence[ArabicPrintedWordBox],
    rects: Sequence[tuple[int, int, int, int]],
) -> list[tuple[int, ...]]:
    assigned: list[list[int]] = [[] for _ in rects]
    used: set[int] = set()
    for word in words:
        matches: list[int] = []
        for band_index, (_left, top, _right, bottom) in enumerate(rects):
            crosses = (
                word.ymin < bottom
                and word.ymax >= top
                and not (word.ymin >= top and word.ymax < bottom)
            )
            if crosses:
                raise ArabicPrintedBandingError(
                    "A recognized word intersects a band cut",
                    reason=REASON_WORD_CROSSES_CUT,
                )
            if word.ymin >= top and word.ymax < bottom:
                matches.append(band_index)
        if len(matches) != 1:
            raise ArabicPrintedBandingError(
                "Every recognized word must belong to exactly one band",
                reason=REASON_WORD_ASSIGNMENT,
            )
        assigned[matches[0]].append(word.index)
        used.add(word.index)
    if used != {word.index for word in words}:
        raise ArabicPrintedBandingError(
            "Every recognized word must belong to exactly one band",
            reason=REASON_WORD_ASSIGNMENT,
        )
    return [tuple(indexes) for indexes in assigned]


def validate_arabic_printed_band_plan(
    bands: Sequence[ArabicPrintedBandRect],
    *,
    image_width: int,
    image_height: int,
    word_count: int,
) -> None:
    image_width = _require_positive_dimension(image_width, name="image_width")
    image_height = _require_positive_dimension(image_height, name="image_height")
    if not bands:
        raise ArabicPrintedBandingError(
            "Cannot plan bands without recognized text lines",
            reason=REASON_EMPTY_GEOMETRY,
        )
    if len(bands) > MAX_BANDS:
        raise ArabicPrintedBandingError(
            f"Safe banding requires {len(bands)} bands, which exceeds the maximum of "
            f"{MAX_BANDS}",
            reason=REASON_EXCEEDS_MAX_BANDS,
        )
    assigned: list[int] = []
    previous: ArabicPrintedBandRect | None = None
    for expected_index, band in enumerate(bands, start=1):
        if band.band_index != expected_index:
            raise ArabicPrintedBandingError(
                "Planned bands are not vertically ordered",
                reason=REASON_UNORDERED_BANDS,
            )
        if band.left != 0 or band.right != image_width:
            raise ArabicPrintedBandingError(
                "Planned bands must be full width",
                reason=REASON_INVALID_BOX,
            )
        if band.top < 0 or band.bottom > image_height or band.bottom <= band.top:
            raise ArabicPrintedBandingError(
                "Planned band rectangle is empty or out of bounds",
                reason=REASON_EMPTY_BAND,
            )
        if previous is not None:
            if band.top < previous.bottom:
                raise ArabicPrintedBandingError(
                    "Planned bands overlap",
                    reason=REASON_OVERLAPPING_BANDS,
                )
            if band.top < previous.top:
                raise ArabicPrintedBandingError(
                    "Planned bands are not vertically ordered",
                    reason=REASON_UNORDERED_BANDS,
                )
        assigned.extend(band.word_indexes)
        previous = band
    if len(assigned) != len(set(assigned)) or len(assigned) != word_count:
        raise ArabicPrintedBandingError(
            "Every recognized word must belong to exactly one band",
            reason=REASON_WORD_ASSIGNMENT,
        )


def plan_arabic_printed_bands(
    words: Sequence[ArabicPrintedWordBox],
    *,
    image_width: int,
    image_height: int,
    max_bands: int = MAX_BANDS,
    max_height_ratio: float = MAX_BAND_HEIGHT_RATIO,
) -> tuple[ArabicPrintedBandRect, ...]:
    image_width = _require_positive_dimension(image_width, name="image_width")
    image_height = _require_positive_dimension(image_height, name="image_height")
    max_bands, max_height_ratio = _require_banding_config(
        max_bands=max_bands,
        max_height_ratio=max_height_ratio,
    )
    if not words:
        raise ArabicPrintedBandingError(
            "Cannot plan bands without recognized text lines",
            reason=REASON_EMPTY_GEOMETRY,
        )
    seen_indexes: set[int] = set()
    for word in words:
        _validate_word_box(word, image_width=image_width, image_height=image_height)
        if word.index in seen_indexes:
            raise ArabicPrintedBandingError(
                "Word indexes must be unique",
                reason=REASON_INVALID_BOX,
            )
        seen_indexes.add(word.index)
    lines = cluster_arabic_printed_lines(words)
    groups = plan_arabic_printed_line_groups(
        lines,
        image_height=image_height,
        max_bands=max_bands,
        max_height_ratio=max_height_ratio,
    )
    rects, preceding_gaps = band_rects_from_line_groups(
        lines, groups, image_width=image_width
    )
    assignments = assign_words_to_band_rects(words, rects)
    bands: list[ArabicPrintedBandRect] = []
    for band_index, (
        (left, top, right, bottom),
        word_indexes,
        gap,
        (line_start, line_end),
    ) in enumerate(
        zip(rects, assignments, preceding_gaps, groups, strict=True),
        start=1,
    ):
        bands.append(
            ArabicPrintedBandRect(
                band_index=band_index,
                left=left,
                top=top,
                right=right,
                bottom=bottom,
                word_indexes=word_indexes,
                preceding_gap_pixels=gap,
                line_start=line_start,
                line_end=line_end,
            )
        )
    validate_arabic_printed_band_plan(
        bands,
        image_width=image_width,
        image_height=image_height,
        word_count=len(words),
    )
    return tuple(bands)
