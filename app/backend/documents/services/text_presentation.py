from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

from documents.models import Document, DocumentTextResult


ResultTypeStr = Literal["SOURCE_TEXT", "HEBREW_TEXT"]


@dataclass(frozen=True)
class DisplayTextBlock:
    result_type: ResultTypeStr
    text: str
    status: str  # SUCCEEDED / NEEDS_REVIEW / FAILED
    verification_status: str  # UNVERIFIED / VERIFIED / REJECTED
    created_at: str  # isoformat for display/debug
    engine: str
    error_code: Optional[str] = None
    error_details: Optional[str] = None


@dataclass(frozen=True)
class TextPresentation:
    source: Optional[DisplayTextBlock]
    hebrew: Optional[DisplayTextBlock]
    missing: list[ResultTypeStr]
    expected: list[ResultTypeStr]


def _latest_displayable(
    doc: Document, result_type: ResultTypeStr
) -> Optional[DocumentTextResult]:
    """
    Choose the newest *displayable* candidate:
    - Prefer SUCCEEDED
    - Fallback to NEEDS_REVIEW
    - Ignore FAILED (usually no text to show)
    """
    qs = (
        doc.text_results.filter(result_type=result_type)
        .exclude(text__isnull=True)
        .exclude(text__exact="")
        .order_by("-created_at")
    )

    succeeded = qs.filter(status=DocumentTextResult.Status.SUCCEEDED).first()
    if succeeded:
        return succeeded

    needs_review = qs.filter(status=DocumentTextResult.Status.NEEDS_REVIEW).first()
    if needs_review:
        return needs_review

    return None


def _latest_failed(
    doc: Document, result_type: ResultTypeStr
) -> Optional[DocumentTextResult]:
    """
    Latest FAILED result (even if text is empty), for debug display.
    """
    return (
        doc.text_results.filter(
            result_type=result_type, status=DocumentTextResult.Status.FAILED
        )
        .order_by("-created_at")
        .first()
    )


def get_text_presentation_for_document(doc: Document) -> TextPresentation:
    # Expected outputs based on language rule:
    # - Hebrew doc: only HEBREW_TEXT
    # - Non-Hebrew doc: SOURCE_TEXT + HEBREW_TEXT
    if (doc.language or "").lower() in ("he", "heb", "hebrew"):
        expected: list[ResultTypeStr] = ["HEBREW_TEXT"]
    else:
        expected = ["SOURCE_TEXT", "HEBREW_TEXT"]

    source_obj = _latest_displayable(doc, "SOURCE_TEXT")
    hebrew_obj = _latest_displayable(doc, "HEBREW_TEXT")

    # If not displayable, we may still want to show FAILED reason.
    source_failed = None if source_obj else _latest_failed(doc, "SOURCE_TEXT")
    hebrew_failed = None if hebrew_obj else _latest_failed(doc, "HEBREW_TEXT")

    source = None
    if source_obj:
        source = DisplayTextBlock(
            result_type="SOURCE_TEXT",
            text=source_obj.text or "",
            status=source_obj.status,
            verification_status=source_obj.verification_status,
            created_at=source_obj.created_at.isoformat(timespec="seconds"),
            engine=source_obj.engine,
            error_code=source_obj.error_code,
            error_details=source_obj.error_details,
        )
    elif source_failed:
        source = DisplayTextBlock(
            result_type="SOURCE_TEXT",
            text="",
            status=source_failed.status,
            verification_status=source_failed.verification_status,
            created_at=source_failed.created_at.isoformat(timespec="seconds"),
            engine=source_failed.engine,
            error_code=source_failed.error_code,
            error_details=source_failed.error_details,
        )

    hebrew = None
    if hebrew_obj:
        hebrew = DisplayTextBlock(
            result_type="HEBREW_TEXT",
            text=hebrew_obj.text or "",
            status=hebrew_obj.status,
            verification_status=hebrew_obj.verification_status,
            created_at=hebrew_obj.created_at.isoformat(timespec="seconds"),
            engine=hebrew_obj.engine,
            error_code=hebrew_obj.error_code,
            error_details=hebrew_obj.error_details,
        )
    elif hebrew_failed:
        hebrew = DisplayTextBlock(
            result_type="HEBREW_TEXT",
            text="",
            status=hebrew_failed.status,
            verification_status=hebrew_failed.verification_status,
            created_at=hebrew_failed.created_at.isoformat(timespec="seconds"),
            engine=hebrew_failed.engine,
            error_code=hebrew_failed.error_code,
            error_details=hebrew_failed.error_details,
        )

    missing: list[ResultTypeStr] = []
    for rt in expected:
        if rt == "SOURCE_TEXT" and (source_obj is None):
            missing.append("SOURCE_TEXT")
        if rt == "HEBREW_TEXT" and (hebrew_obj is None):
            missing.append("HEBREW_TEXT")

    return TextPresentation(
        source=source, hebrew=hebrew, missing=missing, expected=expected
    )
