"""Plain-text completion-marker evaluation for printed-Arabic banded OCR.

Port of Arm E evaluation: accept only the completed terminal state, strip the
exact completion marker and outer whitespace, and classify coverage without
repairing Arabic or calling providers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence


COMPLETION_MARKER = "[VS_ARCHIVE_TRANSCRIPTION_COMPLETE]"
COVERAGE_RATIO_MIN = 0.65
COVERAGE_RATIO_MAX = 1.60
ALLOWED_COMPLETED_STATUS = "completed"
JOIN_STYLE = "single-newline"

TOOL_STEP_TYPES = frozenset(
    {
        "function_call",
        "function_result",
        "code_execution_call",
        "code_execution_result",
        "tool_call",
        "tool_result",
    }
)
UNRELATED_OUTPUT_MARKERS = (
    "hello! how can i help you today?",
    "how can i help you today",
    "no image files were found to transcribe",
)


class ArabicPrintedOcrFailureKind(StrEnum):
    EMPTY_OUTPUT = "empty_output"
    GREETING_OR_UNRELATED = "greeting_or_unrelated"
    INCOMPLETE_OUTPUT = "incomplete_output"
    UNEXPECTED_TOOL_USE = "unexpected_tool_use"
    TERMINAL_ELLIPSIS = "terminal_ellipsis"
    COVERAGE_RATIO = "coverage_ratio"
    TRUNCATED_JSON = "truncated_json"
    INVALID_JSON = "invalid_json"
    INCOMPLETE_STATUS = "incomplete"
    OTHER_STATUS = "other"


@dataclass(frozen=True)
class ArabicPrintedOcrEvaluation:
    """Privacy-safe evaluation. Failed transcription text is never returned."""

    accepted: bool
    failure_kind: str | None
    transcription: str
    marker_seen: bool
    coverage_ratio: float | None
    output_non_whitespace: int | None
    draft_non_whitespace: int | None


def non_whitespace_count(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def split_trailing_completion_marker(text: str) -> tuple[str, bool]:
    lines = text.splitlines()
    last_idx: int | None = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            last_idx = index
            break
    if last_idx is None or lines[last_idx] != COMPLETION_MARKER:
        return text, False
    return "\n".join(lines[:last_idx]), True


def is_greeting_or_unrelated(text: str) -> bool:
    lowered = text.strip().lower()
    return any(marker in lowered for marker in UNRELATED_OUTPUT_MARKERS)


def classify_plain_text_output(text: str | None) -> str:
    if text is None or not text.strip():
        return ArabicPrintedOcrFailureKind.EMPTY_OUTPUT
    stripped = text.strip()
    if is_greeting_or_unrelated(stripped):
        return ArabicPrintedOcrFailureKind.GREETING_OR_UNRELATED
    if stripped.startswith("{") and not stripped.endswith("}"):
        return ArabicPrintedOcrFailureKind.TRUNCATED_JSON
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return ArabicPrintedOcrFailureKind.INVALID_JSON
        return ArabicPrintedOcrFailureKind.INVALID_JSON
    return "ok"


def join_arabic_printed_band_texts(texts: Sequence[str]) -> str:
    return "\n".join(text.strip() for text in texts)


def _rejected(
    kind: ArabicPrintedOcrFailureKind,
    *,
    marker_seen: bool = False,
    coverage_ratio: float | None = None,
    output_non_whitespace: int | None = None,
    draft_non_whitespace: int | None = None,
) -> ArabicPrintedOcrEvaluation:
    return ArabicPrintedOcrEvaluation(
        accepted=False,
        failure_kind=kind.value,
        transcription="",
        marker_seen=marker_seen,
        coverage_ratio=coverage_ratio,
        output_non_whitespace=output_non_whitespace,
        draft_non_whitespace=draft_non_whitespace,
    )


def unexpected_tool_step_types(step_types: Sequence[str] | None) -> tuple[str, ...]:
    found: list[str] = []
    for step_type in step_types or ():
        if step_type in TOOL_STEP_TYPES:
            found.append(step_type)
    return tuple(found)


def evaluate_arabic_printed_band_output(
    raw_text: object,
    draft_text: object,
    *,
    status: str | None,
    step_types: Sequence[str] = (),
) -> ArabicPrintedOcrEvaluation:
    """Evaluate already-extracted provider fields. Does not parse HTTP bodies."""
    if unexpected_tool_step_types(step_types):
        return _rejected(ArabicPrintedOcrFailureKind.UNEXPECTED_TOOL_USE)
    if status == "incomplete":
        return _rejected(ArabicPrintedOcrFailureKind.INCOMPLETE_STATUS)
    if status != ALLOWED_COMPLETED_STATUS:
        return _rejected(ArabicPrintedOcrFailureKind.OTHER_STATUS)
    if type(raw_text) is not str:
        return _rejected(ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT)

    transcription, marker_seen = split_trailing_completion_marker(raw_text)
    if not marker_seen:
        return _rejected(
            ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT,
            marker_seen=False,
        )

    classified = classify_plain_text_output(transcription)
    if classified != "ok":
        return _rejected(
            ArabicPrintedOcrFailureKind(classified),
            marker_seen=True,
        )

    stripped = transcription.strip()
    if stripped.endswith("...") or stripped.endswith("…"):
        return _rejected(
            ArabicPrintedOcrFailureKind.TERMINAL_ELLIPSIS,
            marker_seen=True,
        )

    if type(draft_text) is not str:
        return _rejected(
            ArabicPrintedOcrFailureKind.COVERAGE_RATIO,
            marker_seen=True,
            coverage_ratio=None,
            output_non_whitespace=non_whitespace_count(transcription),
            draft_non_whitespace=None,
        )

    draft_nw = non_whitespace_count(draft_text)
    output_nw = non_whitespace_count(transcription)
    if draft_nw <= 0:
        return _rejected(
            ArabicPrintedOcrFailureKind.COVERAGE_RATIO,
            marker_seen=True,
            coverage_ratio=None,
            output_non_whitespace=output_nw,
            draft_non_whitespace=draft_nw,
        )
    ratio = output_nw / draft_nw
    if ratio < COVERAGE_RATIO_MIN or ratio > COVERAGE_RATIO_MAX:
        return _rejected(
            ArabicPrintedOcrFailureKind.COVERAGE_RATIO,
            marker_seen=True,
            coverage_ratio=ratio,
            output_non_whitespace=output_nw,
            draft_non_whitespace=draft_nw,
        )
    return ArabicPrintedOcrEvaluation(
        accepted=True,
        failure_kind=None,
        transcription=stripped,
        marker_seen=True,
        coverage_ratio=ratio,
        output_non_whitespace=output_nw,
        draft_non_whitespace=draft_nw,
    )
