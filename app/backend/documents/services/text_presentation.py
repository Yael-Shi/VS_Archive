from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

from documents.models import Document, DocumentTextResult


ResultTypeStr = Literal["SOURCE_TEXT", "HEBREW_TEXT"]


@dataclass(frozen=True)
class DisplayTextBlock:
    result_type: ResultTypeStr
    text: str
    status: str  # SUCCEEDED / NEEDS_REVIEW
    verification_status: str  # UNVERIFIED / VERIFIED / REJECTED
    created_at: str  # isoformat for display/debug


@dataclass(frozen=True)
class TextPresentation:
    source: Optional[DisplayTextBlock]
    hebrew: Optional[DisplayTextBlock]
    missing: list[ResultTypeStr]


def _latest_candidate(
    doc: Document, result_type: ResultTypeStr
) -> Optional[DocumentTextResult]:
    """
    Choose the newest *displayable* candidate:
    - Prefer SUCCEEDED
    - Fallback to NEEDS_REVIEW (useful for early V2 / dummy / low-quality outputs)
    - Ignore FAILED (no text to show)
    """
    qs = (
        doc.text_results.filter(result_type=result_type)
        .exclude(text__isnull=True)
        .exclude(text__exact="")
        .order_by("-created_at")
    )

    # Prefer newest SUCCEEDED
    succeeded = qs.filter(status=DocumentTextResult.Status.SUCCEEDED).first()
    if succeeded:
        return succeeded

    # Fallback: newest NEEDS_REVIEW
    needs_review = qs.filter(status=DocumentTextResult.Status.NEEDS_REVIEW).first()
    if needs_review:
        return needs_review

    return None


def get_text_presentation_for_document(doc: Document) -> TextPresentation:
    source_obj = _latest_candidate(doc, "SOURCE_TEXT")
    hebrew_obj = _latest_candidate(doc, "HEBREW_TEXT")

    source = None
    if source_obj:
        source = DisplayTextBlock(
            result_type="SOURCE_TEXT",
            text=source_obj.text or "",
            status=source_obj.status,
            verification_status=source_obj.verification_status,
            created_at=source_obj.created_at.isoformat(timespec="seconds"),
        )

    hebrew = None
    if hebrew_obj:
        hebrew = DisplayTextBlock(
            result_type="HEBREW_TEXT",
            text=hebrew_obj.text or "",
            status=hebrew_obj.status,
            verification_status=hebrew_obj.verification_status,
            created_at=hebrew_obj.created_at.isoformat(timespec="seconds"),
        )

    # Expected outputs based on language rule:
    # - Hebrew doc: only HEBREW_TEXT
    # - Non-Hebrew doc: SOURCE_TEXT + HEBREW_TEXT
    expected: list[ResultTypeStr]
    if (doc.language or "").lower() in ("he", "heb", "hebrew"):
        expected = ["HEBREW_TEXT"]
    else:
        expected = ["SOURCE_TEXT", "HEBREW_TEXT"]

    missing: list[ResultTypeStr] = []
    for rt in expected:
        if rt == "SOURCE_TEXT" and source is None:
            missing.append("SOURCE_TEXT")
        if rt == "HEBREW_TEXT" and hebrew is None:
            missing.append("HEBREW_TEXT")

    return TextPresentation(source=source, hebrew=hebrew, missing=missing)
