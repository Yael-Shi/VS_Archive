"""Template helpers for the shared archive date-entry widget."""

from __future__ import annotations

import re

from django import template
from django.template import TemplateSyntaxError

register = template.Library()

_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@register.filter
def archive_date_id_prefix(prefix) -> str:
    """Return ``prefix_`` for DOM ids, or ``""`` when the widget is unprefixed."""
    value = str(prefix or "").strip()
    if not value:
        return ""
    if not _PREFIX_RE.fullmatch(value):
        raise TemplateSyntaxError(
            "date_widget_prefix must be an HTML id token "
            "(letter, then letters, digits, _ or -)"
        )
    return f"{value}_"
