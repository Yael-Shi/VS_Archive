"""Public transcription edit suggestion form helpers."""

from __future__ import annotations

import html
from difflib import SequenceMatcher

from django.utils.safestring import SafeString, mark_safe

from public.services.registration import HONEYPOT_FIELD_NAME

NAME_REQUIRED_ERROR = "יש למלא שם."
SUGGESTED_TEXT_REQUIRED_ERROR = "יש להזין טקסט מוצע."
IDENTICAL_TEXT_ERROR = "הטקסט המוצע זהה לתעתוק הנוכחי."

SUGGESTION_STATUS_LABELS = {
    "PENDING": "ממתין לבדיקה",
    "APPROVED": "אושר",
    "REJECTED": "נדחה",
}


def normalize_transcription_text(text: str) -> str:
    return (text or "").strip()


def is_honeypot_triggered(post_data) -> bool:
    return bool((post_data.get(HONEYPOT_FIELD_NAME) or "").strip())


def texts_are_equivalent(current: str, suggested: str) -> bool:
    return normalize_transcription_text(current) == normalize_transcription_text(suggested)


def suggestion_status_label(status: str) -> str:
    return SUGGESTION_STATUS_LABELS.get(status, status)


def _escape_transcription_text(text: str) -> str:
    return html.escape(text or "", quote=True)


def render_transcription_diff_html(current_text: str, suggested_text: str) -> SafeString:
    """Inline diff HTML with escaped user text; deletions and insertions highlighted."""
    current = current_text or ""
    suggested = suggested_text or ""
    matcher = SequenceMatcher(None, current, suggested)
    parts: list[str] = []

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.append(_escape_transcription_text(current[i1:i2]))
        elif op == "delete":
            parts.append(
                f'<del class="transcription-diff-del">{_escape_transcription_text(current[i1:i2])}</del>'
            )
        elif op == "insert":
            parts.append(
                f'<ins class="transcription-diff-ins">{_escape_transcription_text(suggested[j1:j2])}</ins>'
            )
        elif op == "replace":
            parts.append(
                f'<del class="transcription-diff-del">{_escape_transcription_text(current[i1:i2])}</del>'
            )
            parts.append(
                f'<ins class="transcription-diff-ins">{_escape_transcription_text(suggested[j1:j2])}</ins>'
            )

    return mark_safe("".join(parts))
