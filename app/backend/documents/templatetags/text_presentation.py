"""Template filters for safe plain-text display."""

from django import template

from documents.services.manual_text_body_display import format_manual_text_body_for_display

register = template.Library()


@register.filter
def manual_text_body_display(value) -> str:
    return format_manual_text_body_for_display("" if value is None else str(value))
