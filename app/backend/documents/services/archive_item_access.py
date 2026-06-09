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
    """
    Visibility/access-filtered queryset of all ``ArchiveItem`` rows for ``user``.

    Includes every item type the user may access by visibility rules (e.g. PHOTO).
    Does not imply the item is renderable on ``/archive/`` browse/detail surfaces.
    """
    return filter_archive_items_for_user(user, ArchiveItem.objects.all())


def exclude_deferred_archive_browse_item_types(
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """Exclude item types not yet rendered on public /archive/ surfaces (PHOTO → PR4)."""
    return queryset.exclude(item_type=ArchiveItem.ItemType.PHOTO)


def archive_browse_queryset_for_user(user) -> QuerySet[ArchiveItem]:
    """
    Items currently renderable on ``/archive/`` list, detail, and discovery browse.

    Applies visibility/access via ``archive_item_queryset_for_user``, then excludes
    deferred item types (PHOTO until PR4 display).
    """
    return exclude_deferred_archive_browse_item_types(
        archive_item_queryset_for_user(user)
    )


def get_viewable_archive_item(
    user,
    item_id: int,
    *,
    queryset: QuerySet[ArchiveItem] | None = None,
) -> ArchiveItem:
    """
    Return an archive item currently renderable on ``/archive/<id>/``, or raise Http404.

    Applies visibility/access rules and excludes deferred item types such as PHOTO
    until their archive detail rendering is implemented. Uses 404 for missing ids,
    unauthorized items, and deferred types.
    """
    base = queryset if queryset is not None else ArchiveItem.objects.all()
    qs = exclude_deferred_archive_browse_item_types(
        filter_archive_items_for_user(user, base)
    )
    try:
        return qs.select_related("manual_text_content", "ocr_document").get(id=item_id)
    except ArchiveItem.DoesNotExist:
        raise Http404() from None
