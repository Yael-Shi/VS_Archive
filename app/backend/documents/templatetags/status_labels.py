"""Single source of truth for user-facing Hebrew status labels.

PR1 (presentation-only) consolidates the per-template ``{% if %}`` badge chains
that previously drifted across pages (e.g. ``READY`` shown as both "מוכן" and
"עיבוד הסתיים"). Enum values and semantics are unchanged; this module only maps
each stored value to a Hebrew label + a badge tone class.

``READY`` means processing/display output is available — **not** that a human
verified the text. Human verification is a separate axis
(``DocumentTextResult.verification_status``) with its own labels here.
"""

from __future__ import annotations

from typing import Tuple

from django import template

from documents.models import Document, DocumentTextResult

register = template.Library()

# Each entry maps a stored enum value -> (Hebrew label, badge tone class).
# Tone "" renders a neutral ``.badge`` (no color modifier).
_PROCESSING_STATE: dict[str, Tuple[str, str]] = {
    Document.ProcessingState.READY.value: ("עיבוד הושלם", "badge-ok"),
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
    Document.MetadataStatus.NEEDS_COMPLETION.value: ("דורש השלמת פרטים", "badge-warn"),
    Document.MetadataStatus.COMPLETED.value: ("פרטים הושלמו", "badge-ok"),
}

_VERIFICATION_STATUS: dict[str, Tuple[str, str]] = {
    DocumentTextResult.VerificationStatus.VERIFIED.value: ("אושר אנושית", "badge-ok"),
    DocumentTextResult.VerificationStatus.REJECTED.value: ("נדחה בבקרה", "badge-bad"),
    DocumentTextResult.VerificationStatus.UNVERIFIED.value: (
        "ממתין לבדיקה אנושית",
        "badge-warn",
    ),
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
