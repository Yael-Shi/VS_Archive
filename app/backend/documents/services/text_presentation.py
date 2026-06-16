from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

from documents.models import Document, DocumentTextResult
from documents.services.expected_outputs import expected_result_types_for_document


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
class TextBlockDisplayMeta:
    label: str
    description: str
    empty_message: str


@dataclass(frozen=True)
class TextPresentation:
    source: Optional[DisplayTextBlock]
    hebrew: Optional[DisplayTextBlock]
    missing: list[ResultTypeStr]
    expected: list[ResultTypeStr]
    source_meta: TextBlockDisplayMeta
    hebrew_meta: TextBlockDisplayMeta
    show_source: bool
    show_hebrew: bool


def _is_hebrew_language(doc: Document) -> bool:
    lang = (doc.language or "").strip().lower()
    return lang in ("he", "heb", "hebrew")


def text_block_display_meta(doc: Document, result_type: ResultTypeStr) -> TextBlockDisplayMeta:
    """User-facing label and short explanation for a document detail text block."""
    is_hebrew_doc = _is_hebrew_language(doc)

    if result_type == "SOURCE_TEXT":
        if is_hebrew_doc:
            return TextBlockDisplayMeta(
                label="תעתוק מקור",
                description="הטקסט כפי שחולץ אוטומטית מן המסמך.",
                empty_message="אין תעתוק מקור להצגה.",
            )
        return TextBlockDisplayMeta(
            label="תעתוק מקור",
            description="טקסט בשפת המקור כפי שחולץ אוטומטית.",
            empty_message="אין תעתוק מקור עדיין.",
        )

    if result_type == "HEBREW_TEXT":
        if is_hebrew_doc:
            return TextBlockDisplayMeta(
                label="טקסט עברי לבדיקה",
                description="הטקסט העברי שמיועד לבדיקה, עריכה ואישור.",
                empty_message="אין תעתוק לעברית עדיין.",
            )
        return TextBlockDisplayMeta(
            label="תרגום לעברית",
            description="תרגום לעברית של טקסט המקור (אם קיים).",
            empty_message="אין תרגום לעברית עדיין.",
        )

    return TextBlockDisplayMeta(
        label=result_type,
        description="",
        empty_message="אין טקסט להצגה עדיין.",
    )


def _latest_displayable(doc: Document, result_type: ResultTypeStr) -> Optional[DocumentTextResult]:
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


def _latest_failed(doc: Document, result_type: ResultTypeStr) -> Optional[DocumentTextResult]:
    return (
        doc.text_results.filter(result_type=result_type, status=DocumentTextResult.Status.FAILED)
        .order_by("-created_at")
        .first()
    )


def get_text_presentation_for_document(doc: Document) -> TextPresentation:
    expected = expected_result_types_for_document(doc)

    source_obj = _latest_displayable(doc, "SOURCE_TEXT")
    hebrew_obj = _latest_displayable(doc, "HEBREW_TEXT")

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
        if rt == "SOURCE_TEXT" and source_obj is None:
            missing.append("SOURCE_TEXT")
        if rt == "HEBREW_TEXT" and hebrew_obj is None:
            missing.append("HEBREW_TEXT")

    source_meta = text_block_display_meta(doc, "SOURCE_TEXT")
    hebrew_meta = text_block_display_meta(doc, "HEBREW_TEXT")

    if _is_hebrew_language(doc):
        # Display-only: one panel for Hebrew docs — prefer HEBREW_TEXT, else SOURCE_TEXT.
        if hebrew is not None:
            show_source = False
            show_hebrew = True
        elif source is not None:
            show_source = True
            show_hebrew = False
        else:
            show_source = "SOURCE_TEXT" in expected
            show_hebrew = "HEBREW_TEXT" in expected and not show_source
    else:
        show_source = "SOURCE_TEXT" in expected or source is not None
        show_hebrew = "HEBREW_TEXT" in expected or hebrew is not None

    return TextPresentation(
        source=source,
        hebrew=hebrew,
        missing=missing,
        expected=expected,
        source_meta=source_meta,
        hebrew_meta=hebrew_meta,
        show_source=show_source,
        show_hebrew=show_hebrew,
    )


def resolve_displayed_transcription_result(
    doc: Document,
) -> Optional[DocumentTextResult]:
    """
    DocumentTextResult row backing get_displayed_transcription_text().

    Hebrew documents prefer displayable HEBREW_TEXT, then SOURCE_TEXT.
    Non-Hebrew documents prefer SOURCE_TEXT, then HEBREW_TEXT.
    """
    if _is_hebrew_language(doc):
        hebrew_obj = _latest_displayable(doc, "HEBREW_TEXT")
        if hebrew_obj:
            return hebrew_obj
        return _latest_displayable(doc, "SOURCE_TEXT")

    source_obj = _latest_displayable(doc, "SOURCE_TEXT")
    if source_obj:
        return source_obj
    return _latest_displayable(doc, "HEBREW_TEXT")


def get_displayed_transcription_text(doc: Document) -> str:
    """
    Plain text shown as the primary OCR transcription on the document detail page.

    Hebrew documents prefer displayable HEBREW_TEXT, then SOURCE_TEXT.
    Non-Hebrew documents prefer SOURCE_TEXT, then HEBREW_TEXT.
    """
    row = resolve_displayed_transcription_result(doc)
    if row is None:
        return ""
    return row.text or ""
