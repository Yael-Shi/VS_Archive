"""Single source of truth for user-facing Hebrew status labels.

Presentation-only: maps stored enum values to Hebrew labels + badge tone classes.
Enum values and semantics are unchanged.

``READY`` means processing/display output is available — **not** human-verified text.
Human verification uses ``DocumentTextResult.verification_status`` labels here.
"""

from __future__ import annotations

from typing import Tuple

from django import template

from documents.models import Document, DocumentTextResult
from documents.services.archive_item_presentation import language_label as _language_label
from documents.services.archive_item_validation import TEXT_INPUT_TYPE_UI_CHOICES
from documents.services.review_reasons import (
    AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW,
    NEEDS_REVIEW_FLAG,
)

register = template.Library()

# Each entry maps a stored enum value -> (Hebrew label, badge tone class).
# Tone "" renders a neutral ``.badge`` (no color modifier).
_PROCESSING_STATE: dict[str, Tuple[str, str]] = {
    Document.ProcessingState.READY.value: ("מוכן לצפייה", "badge-ok"),
    Document.ProcessingState.PROCESSING.value: ("בעיבוד", "badge-warn"),
    Document.ProcessingState.PARTIAL.value: ("חלקי", ""),
    Document.ProcessingState.FAILED.value: ("עיבוד נכשל", "badge-bad"),
}

_UPLOAD_STATUS: dict[str, Tuple[str, str]] = {
    Document.UploadStatus.UPLOADED.value: ("הועלה", "badge-ok"),
    Document.UploadStatus.UPLOADING.value: ("בהעלאה", "badge-warn"),
    Document.UploadStatus.FAILED.value: ("העלאה נכשלה", "badge-bad"),
}

_METADATA_STATUS: dict[str, Tuple[str, str]] = {
    Document.MetadataStatus.NEEDS_COMPLETION.value: ("דרושה השלמת פרטים", "badge-warn"),
    Document.MetadataStatus.COMPLETED.value: ("פרטים הושלמו", "badge-ok"),
}

_TEXT_INPUT_TYPE: dict[str, str] = dict(TEXT_INPUT_TYPE_UI_CHOICES)

_TEXT_RESULT_STATUS: dict[str, Tuple[str, str]] = {
    DocumentTextResult.Status.NEEDS_REVIEW.value: ("ממתין לבקרת תמלול", "badge-warn"),
    DocumentTextResult.Status.SUCCEEDED.value: ("הושלם", "badge-ok"),
    DocumentTextResult.Status.FAILED.value: ("עיבוד נכשל", "badge-bad"),
}

_RESULT_TYPE: dict[str, str] = {
    DocumentTextResult.ResultType.SOURCE_TEXT.value: "תמלול מקור",
    DocumentTextResult.ResultType.HEBREW_TEXT.value: "טקסט עברי",
}

_DOC_TYPE: dict[str, str] = {
    Document.DocType.IMAGE.value: "תמונה",
    Document.DocType.PDF.value: "PDF",
}

_ENGINE_KEY: dict[str, str] = {
    DocumentTextResult.OcrEngineKey.GEMINI.value: "Gemini",
    DocumentTextResult.OcrEngineKey.TRANSKRIBUS.value: "Transkribus",
}

_REVIEW_REASON: dict[str, str] = {
    AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW: "נדרשת בקרת תמלול אנושית",
    NEEDS_REVIEW_FLAG: "סימון חוסר ודאות מהמנוע",
    "MIN_TEXT_LENGTH": "טקסט קצר מדי",
    "HAS_UNCLEAR": "יש קטעים לא ברורים",
}

_VERIFICATION_STATUS: dict[str, Tuple[str, str]] = {
    DocumentTextResult.VerificationStatus.VERIFIED.value: ("אושר", "badge-ok"),
    DocumentTextResult.VerificationStatus.REJECTED.value: ("נדחה בבקרה", "badge-bad"),
    DocumentTextResult.VerificationStatus.UNVERIFIED.value: ("טרם אושר", "badge-warn"),
}


def _resolve(mapping: dict[str, Tuple[str, str]], value) -> Tuple[str, str]:
    """Return (label, tone) for a stored value.

    Unknown/empty values fall back to the raw value in a neutral badge, matching
    the previous templates' ``{% else %}`` branches so nothing is hidden.
    """
    key = str(value or "")
    return mapping.get(key, (key, ""))


# --- label-only filters (for inline copy / non-badge contexts) ---------------


@register.filter
def processing_state_label(value) -> str:
    return _resolve(_PROCESSING_STATE, value)[0]


@register.filter
def upload_status_label(value) -> str:
    return _resolve(_UPLOAD_STATUS, value)[0]


@register.filter
def metadata_status_label(value) -> str:
    return _resolve(_METADATA_STATUS, value)[0]


@register.filter
def verification_status_label(value) -> str:
    return _resolve(_VERIFICATION_STATUS, value)[0]


@register.filter
def text_input_type_label(value) -> str:
    key = str(value or "")
    return _TEXT_INPUT_TYPE.get(key, key)


@register.filter
def text_result_status_label(value) -> str:
    return _resolve(_TEXT_RESULT_STATUS, value)[0]


@register.filter
def result_type_label(value) -> str:
    key = str(value or "")
    return _RESULT_TYPE.get(key, key)


@register.filter
def doc_type_label(value) -> str:
    key = str(value or "")
    return _DOC_TYPE.get(key, key)


@register.filter
def engine_key_label(value) -> str:
    key = str(value or "").strip()
    return _ENGINE_KEY.get(key, key)


@register.filter
def review_reason_label(value) -> str:
    key = str(value or "").strip()
    return _REVIEW_REASON.get(key, key)


@register.filter
def language_label(value) -> str:
    return _language_label(value)


# --- badge inclusion tags (label + tone, shared markup) ----------------------


@register.inclusion_tag("documents/partials/_status_badge.html")
def processing_state_badge(value):
    label, tone = _resolve(_PROCESSING_STATE, value)
    return {"label": label, "tone": tone}


@register.inclusion_tag("documents/partials/_status_badge.html")
def upload_status_badge(value):
    label, tone = _resolve(_UPLOAD_STATUS, value)
    return {"label": label, "tone": tone}


@register.inclusion_tag("documents/partials/_status_badge.html")
def metadata_status_badge(value):
    label, tone = _resolve(_METADATA_STATUS, value)
    return {"label": label, "tone": tone}


@register.inclusion_tag("documents/partials/_verification_badge.html")
def verification_status_badge(value):
    label, tone = _resolve(_VERIFICATION_STATUS, value)
    return {"label": label, "tone": tone}
