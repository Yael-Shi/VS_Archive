"""Template filters for ArchiveItem user-facing Hebrew labels."""

from django import template

from documents.services import archive_item_presentation as labels
from documents.services import photo_presentation as photo_labels

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


@register.filter
def photo_upload_status_label(photo_content) -> str:
    return photo_labels.photo_upload_status_label(photo_content)


@register.filter
def photo_upload_status_tone(photo_content) -> str:
    return photo_labels.photo_upload_status_tone(photo_content)


@register.filter
def photo_archive_renderability_label(photo_content) -> str:
    return photo_labels.photo_archive_renderability_label(photo_content)


@register.filter
def photo_archive_renderability_tone(photo_content) -> str:
    return photo_labels.photo_archive_renderability_tone(photo_content)
