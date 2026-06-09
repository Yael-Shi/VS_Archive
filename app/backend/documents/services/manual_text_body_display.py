"""Safe display formatting for manual text body (plain text only)."""

from __future__ import annotations

import re

from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

_HTTP_URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}\"'"


def _trim_trailing_punctuation(url: str) -> tuple[str, str]:
    trimmed = url
    trailing = ""
    while trimmed and trimmed[-1] in _TRAILING_PUNCTUATION:
        trailing = trimmed[-1] + trailing
        trimmed = trimmed[:-1]
    return trimmed, trailing


def _linkify_http_urls_in_escaped_text(escaped_text: str) -> str:
    def replace_match(match: re.Match[str]) -> str:
        raw_url = match.group(1)
        url, trailing = _trim_trailing_punctuation(raw_url)
        if not url:
            return raw_url
        return (
            f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'
            f"{trailing}"
        )

    return _HTTP_URL_RE.sub(replace_match, escaped_text)


def format_manual_text_body_for_display(body: str) -> SafeString:
    """Escape plain text, preserve line breaks, and linkify http/https URLs."""
    escaped = escape(body or "")
    with_breaks = escaped.replace("\n", "<br />")
    return mark_safe(_linkify_http_urls_in_escaped_text(with_breaks))
