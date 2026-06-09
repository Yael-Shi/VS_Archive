"""Centralized ArchiveItem visibility and view access."""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.http import Http404

from documents.models import ArchiveItem, PhotoContent
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


def filter_browse_renderable_photo_items(
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """
    Keep non-PHOTO rows; include PHOTO only when linked ``PhotoContent`` is complete.

    PHOTO is renderable on ``/archive/`` surfaces only when:
    - linked ``PhotoContent`` exists
    - ``upload_status == UPLOADED``
    - ``original_file_key`` is non-empty
    """
    uploaded_photo = Q(
        item_type=ArchiveItem.ItemType.PHOTO,
        photo_content__upload_status=PhotoContent.UploadStatus.UPLOADED,
        photo_content__original_file_key__gt="",
    )
    return queryset.filter(~Q(item_type=ArchiveItem.ItemType.PHOTO) | uploaded_photo)


def archive_browse_queryset_for_user(user) -> QuerySet[ArchiveItem]:
    """
    Items currently renderable on ``/archive/`` list, detail, and discovery browse.

    Applies visibility/access via ``archive_item_queryset_for_user``. Applies PHOTO
    upload-completion eligibility via ``filter_browse_renderable_photo_items``.
    """
    return filter_browse_renderable_photo_items(
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

    Applies visibility/access rules and PHOTO upload-completion eligibility.
    Uses 404 for missing ids, unauthorized items, and non-renderable PHOTO rows.
    """
    base = queryset if queryset is not None else ArchiveItem.objects.all()
    qs = filter_browse_renderable_photo_items(
        filter_archive_items_for_user(user, base)
    )
    try:
        return qs.select_related(
            "manual_text_content",
            "ocr_document",
            "photo_content",
        ).get(id=item_id)
    except ArchiveItem.DoesNotExist:
        raise Http404() from None
