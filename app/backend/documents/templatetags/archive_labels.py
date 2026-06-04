"""Template filters for ArchiveItem user-facing Hebrew labels."""

from django import template

from documents.services import archive_item_presentation as labels

register = template.Library()


@register.filter
def archive_visibility_label(value) -> str:
    return labels.visibility_label(value)


@register.filter
def archive_metadata_status_label(value) -> str:
    return labels.archive_metadata_status_label(value)


@register.filter
def archive_item_type_label(value) -> str:
    return labels.archive_item_type_label(value)


@register.filter
def archive_language_label(value) -> str:
    return labels.language_label(value)
