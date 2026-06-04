"""Template filter for precision-aware document date labels."""

from django import template

from documents.services.document_date import format_document_date

register = template.Library()


@register.filter
def document_date_display(document) -> str:
    return format_document_date(document)
