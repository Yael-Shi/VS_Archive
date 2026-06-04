"""Centralized ArchiveItem visibility and view access."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from documents.models import ArchiveItem
from documents.services.document_access import is_document_admin

ARCHIVE_FAMILY_GROUP_NAME = "archive_family"

_VIEWABLE_VISIBILITIES = (
    ArchiveItem.Visibility.PUBLIC,
    ArchiveItem.Visibility.PRIVATE,
)


def is_archive_family_user(user) -> bool:
    """Authenticated member of the approved family archive group (not staff by default)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()


def can_view_archive_item(user, archive_item: ArchiveItem) -> bool:
    """Whether ``user`` may view ``archive_item``."""
    if is_document_admin(user):
        return True

    visibility = archive_item.visibility
    if visibility == ArchiveItem.Visibility.PUBLIC:
        return True
    if visibility == ArchiveItem.Visibility.PRIVATE:
        return is_archive_family_user(user)
    return False


def filter_archive_items_for_user(
    user,
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """Restrict ``queryset`` to archive items the user may list or open by id."""
    if is_document_admin(user):
        return queryset
    if is_archive_family_user(user):
        return queryset.filter(visibility__in=_VIEWABLE_VISIBILITIES)
    return queryset.filter(visibility=ArchiveItem.Visibility.PUBLIC)


def archive_item_queryset_for_user(user) -> QuerySet[ArchiveItem]:
    """Base queryset of archive items visible in list/detail for ``user``."""
    return filter_archive_items_for_user(user, ArchiveItem.objects.all())


def get_viewable_archive_item(
    user,
    item_id: int,
    *,
    queryset: QuerySet[ArchiveItem] | None = None,
) -> ArchiveItem:
    """
    Return an archive item the user may view, or raise Http404.

    Uses 404 for both missing ids and unauthorized items.
    """
    base = queryset if queryset is not None else ArchiveItem.objects.all()
    qs = filter_archive_items_for_user(user, base)
    try:
        return qs.select_related("manual_text_content", "ocr_document").get(id=item_id)
    except ArchiveItem.DoesNotExist:
        raise Http404() from None
