"""Pure corrected/current Transkribus transcript selection (v1).

Independent of automatic ``pick_transcript`` / job+model matching. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class CorrectedCurrentSelectionErrorCode:
    """Stable refusal codes for page-level selection failures."""

    ZERO_TRANSCRIPTS = "ZERO_TRANSCRIPTS"
    MULTIPLE_TRANSCRIPTS = "MULTIPLE_TRANSCRIPTS"
    MISSING_TS_ID = "MISSING_TS_ID"


_IN_PROGRESS_WARNING = (
    "Remote transcript status is IN_PROGRESS; sync may proceed but content "
    "may still be changing on the provider."
)


@dataclass(frozen=True)
class CorrectedCurrentPageInput:
    """One mapped page and the provider ``tsList`` transcript dicts for that page."""

    page_index: int
    page_nr: int
    raw_transcripts: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class CorrectedCurrentTranscriptSelection:
    page_index: int
    page_nr: int
    transcript_ts_id: str
    remote_transcript_status: str | None
    in_progress_warning: str | None


@dataclass(frozen=True)
class CorrectedCurrentPageSelectionError:
    page_index: int
    page_nr: int
    code: str
    message: str


@dataclass(frozen=True)
class CorrectedCurrentSelectionResult:
    """Document-level outcome: all pages selected, or refused with page errors."""

    selections: tuple[CorrectedCurrentTranscriptSelection, ...] | None
    page_errors: tuple[CorrectedCurrentPageSelectionError, ...] | None

    def __post_init__(self) -> None:
        has_selections = self.selections is not None
        has_errors = self.page_errors is not None
        if has_selections == has_errors:
            raise ValueError(
                "CorrectedCurrentSelectionResult requires exactly one of "
                "selections or page_errors to be set (the other must be None)."
            )

    @property
    def is_ok(self) -> bool:
        return self.selections is not None

    @property
    def is_refused(self) -> bool:
        return self.page_errors is not None


def _normalize_status_for_compare(status: str | None) -> str:
    if not status:
        return ""
    return status.strip().upper().replace(" ", "_")


def _is_in_progress_status(status: str | None) -> bool:
    return _normalize_status_for_compare(status) == "IN_PROGRESS"


def _transcript_ts_id(raw: Mapping[str, Any]) -> str | None:
    ts_raw = raw.get("tsId")
    if ts_raw is None:
        return None
    ts_id = str(ts_raw).strip()
    return ts_id or None


def _remote_transcript_status(raw: Mapping[str, Any]) -> str | None:
    status_raw = raw.get("status")
    if status_raw is None:
        return None
    status = str(status_raw).strip()
    return status or None


def select_corrected_current_transcript_for_page(
    raw_transcripts: Sequence[Mapping[str, Any]],
    *,
    page_index: int,
    page_nr: int,
) -> CorrectedCurrentTranscriptSelection | CorrectedCurrentPageSelectionError:
    """Select the corrected/current transcript when exactly one exists on the page."""
    count = len(raw_transcripts)
    if count == 0:
        return CorrectedCurrentPageSelectionError(
            page_index=page_index,
            page_nr=page_nr,
            code=CorrectedCurrentSelectionErrorCode.ZERO_TRANSCRIPTS,
            message=(
                f"page_index={page_index} (pageNr={page_nr}): expected exactly one "
                "transcript, found 0."
            ),
        )
    if count != 1:
        return CorrectedCurrentPageSelectionError(
            page_index=page_index,
            page_nr=page_nr,
            code=CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS,
            message=(
                f"page_index={page_index} (pageNr={page_nr}): expected exactly one "
                f"transcript, found {count}."
            ),
        )

    raw = raw_transcripts[0]
    ts_id = _transcript_ts_id(raw)
    if ts_id is None:
        return CorrectedCurrentPageSelectionError(
            page_index=page_index,
            page_nr=page_nr,
            code=CorrectedCurrentSelectionErrorCode.MISSING_TS_ID,
            message=(
                f"page_index={page_index} (pageNr={page_nr}): the sole transcript "
                "is missing a non-empty tsId."
            ),
        )

    remote_status = _remote_transcript_status(raw)
    warning = _IN_PROGRESS_WARNING if _is_in_progress_status(remote_status) else None
    return CorrectedCurrentTranscriptSelection(
        page_index=page_index,
        page_nr=page_nr,
        transcript_ts_id=ts_id,
        remote_transcript_status=remote_status,
        in_progress_warning=warning,
    )


def select_corrected_current_transcripts_for_document(
    pages: Sequence[CorrectedCurrentPageInput],
) -> CorrectedCurrentSelectionResult:
    """Select one transcript per page; refuse the whole document if any page fails."""
    if not pages:
        raise ValueError(
            "select_corrected_current_transcripts_for_document requires at least "
            "one CorrectedCurrentPageInput; got an empty pages sequence."
        )

    ordered = sorted(pages, key=lambda p: p.page_index)
    selections: list[CorrectedCurrentTranscriptSelection] = []
    errors: list[CorrectedCurrentPageSelectionError] = []

    for page in ordered:
        outcome = select_corrected_current_transcript_for_page(
            page.raw_transcripts,
            page_index=page.page_index,
            page_nr=page.page_nr,
        )
        if isinstance(outcome, CorrectedCurrentPageSelectionError):
            errors.append(outcome)
        else:
            selections.append(outcome)

    if errors:
        return CorrectedCurrentSelectionResult(
            selections=None,
            page_errors=tuple(errors),
        )
    return CorrectedCurrentSelectionResult(
        selections=tuple(selections),
        page_errors=None,
    )


__all__ = [
    "CorrectedCurrentPageInput",
    "CorrectedCurrentPageSelectionError",
    "CorrectedCurrentSelectionErrorCode",
    "CorrectedCurrentSelectionResult",
    "CorrectedCurrentTranscriptSelection",
    "select_corrected_current_transcript_for_page",
    "select_corrected_current_transcripts_for_document",
]
