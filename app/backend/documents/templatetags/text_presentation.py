"""Template filters for safe plain-text display."""

from django import template

from documents.models import Document
from documents.services.manual_text_body_display import (
    format_manual_text_body_for_display,
)
from documents.services.text_presentation import source_text_is_rtl

register = template.Library()


@register.filter
def manual_text_body_display(value) -> str:
    return format_manual_text_body_for_display("" if value is None else str(value))


@register.filter
def source_text_is_rtl_filter(doc: Document) -> bool:
    return source_text_is_rtl(doc)
