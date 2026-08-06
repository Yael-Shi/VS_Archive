"""Centralized ArchiveItem visibility and view access."""

from __future__ import annotations

from django.db.models import Q, QuerySet
from django.http import Http404

from documents.models import ArchiveItem, Document, PhotoContent, VideoContent
from documents.services.document_access import is_document_admin
from documents.services.video_url_contract import YOUTUBE_VIDEO_ID_PATTERN

ARCHIVE_FAMILY_GROUP_NAME = "archive_family"

VIEW_RESTRICTED_ARCHIVEITEM_CODENAME = "view_restricted_archiveitem"
VIEW_RESTRICTED_ARCHIVEITEM_PERMISSION = (
    f"documents.{VIEW_RESTRICTED_ARCHIVEITEM_CODENAME}"
)


def is_archive_family_user(user) -> bool:
    """Authenticated member of the approved family archive group (not staff by default)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()


def can_view_restricted_archive_items(user) -> bool:
    """Whether ``user`` has the explicit restricted-archive view permission.

    ``is_staff`` alone is not sufficient. Active superusers follow Django's normal
    ``has_perm`` behavior (typically allowed). Non-staff users may be granted the
    permission directly.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return bool(user.has_perm(VIEW_RESTRICTED_ARCHIVEITEM_PERMISSION))


def can_view_archive_item(user, archive_item: ArchiveItem) -> bool:
    """Whether ``user`` may view ``archive_item``."""
    visibility = archive_item.visibility
    if visibility == ArchiveItem.Visibility.PUBLIC:
        return True
    if visibility == ArchiveItem.Visibility.PRIVATE:
        return is_archive_family_user(user) or is_document_admin(user)
    if visibility == ArchiveItem.Visibility.RESTRICTED:
        return can_view_restricted_archive_items(user)
    # Unknown visibility values fail closed for everyone.
    return False


def filter_archive_items_for_user(
    user,
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """Restrict ``queryset`` to archive items the user may list or open by id."""
    visibility_q = Q(visibility=ArchiveItem.Visibility.PUBLIC)
    if is_archive_family_user(user) or is_document_admin(user):
        visibility_q |= Q(visibility=ArchiveItem.Visibility.PRIVATE)
    if can_view_restricted_archive_items(user):
        visibility_q |= Q(visibility=ArchiveItem.Visibility.RESTRICTED)
    return queryset.filter(visibility_q)


def archive_item_queryset_for_user(user) -> QuerySet[ArchiveItem]:
    """
    Visibility/access-filtered queryset of all ``ArchiveItem`` rows for ``user``.

    Includes every item type the user may access by visibility rules (e.g. PHOTO).
    Does not imply the item is renderable on ``/archive/`` browse/detail surfaces.
    """
    return filter_archive_items_for_user(user, ArchiveItem.objects.all())


def filter_browse_renderable_archive_items(
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """
    Restrict ``/archive/`` browse/detail to items with completed uploads where required.

    PHOTO is renderable only when linked ``PhotoContent`` is uploaded with a key.
    OCR_DOCUMENT is renderable only when linked ``Document.upload_status`` is UPLOADED.
    VIDEO is renderable only when linked ``VideoContent`` matches the DB-enforceable
    provider/mode/id/source_url shape (missing content fails closed). Semantic
    source_url↔provider consistency is enforced at write time via ``full_clean()``.
    Other item types (e.g. MANUAL_TEXT) are unchanged.
    """
    uploaded_photo = Q(
        item_type=ArchiveItem.ItemType.PHOTO,
        photo_content__upload_status=PhotoContent.UploadStatus.UPLOADED,
        photo_content__original_file_key__gt="",
    )
    uploaded_ocr = Q(
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        ocr_document__upload_status=Document.UploadStatus.UPLOADED,
    )
    valid_video = Q(item_type=ArchiveItem.ItemType.VIDEO) & (
        Q(
            video_content__provider=VideoContent.Provider.YOUTUBE,
            video_content__presentation_mode=VideoContent.PresentationMode.EMBEDDED,
            video_content__provider_video_id__regex=YOUTUBE_VIDEO_ID_PATTERN,
            video_content__source_url__gt="",
        )
        | Q(
            video_content__provider=VideoContent.Provider.KAN,
            video_content__presentation_mode=(
                VideoContent.PresentationMode.EXTERNAL_LINK
            ),
            video_content__provider_video_id="",
            video_content__source_url__gt="",
        )
        | Q(
            video_content__provider=VideoContent.Provider.OTHER,
            video_content__presentation_mode=(
                VideoContent.PresentationMode.EXTERNAL_LINK
            ),
            video_content__provider_video_id="",
            video_content__source_url__gt="",
        )
    )
    other_types = ~Q(
        item_type__in=(
            ArchiveItem.ItemType.PHOTO,
            ArchiveItem.ItemType.OCR_DOCUMENT,
            ArchiveItem.ItemType.VIDEO,
        )
    )
    return queryset.filter(uploaded_photo | uploaded_ocr | valid_video | other_types)


def filter_browse_renderable_photo_items(
    queryset: QuerySet[ArchiveItem],
) -> QuerySet[ArchiveItem]:
    """Backward-compatible alias; prefer ``filter_browse_renderable_archive_items``."""
    return filter_browse_renderable_archive_items(queryset)


def archive_browse_queryset_for_user(user) -> QuerySet[ArchiveItem]:
    """
    Items currently renderable on ``/archive/`` list, detail, and discovery browse.

    Applies visibility/access via ``archive_item_queryset_for_user``. Applies PHOTO
    upload-completion eligibility via ``filter_browse_renderable_archive_items``.
    """
    return filter_browse_renderable_archive_items(archive_item_queryset_for_user(user))


def get_accessible_archive_item(
    user,
    item_id: int,
    *,
    queryset: QuerySet[ArchiveItem] | None = None,
) -> ArchiveItem:
    """
    Return an archive item the user may access by visibility rules, or raise Http404.

    Unlike ``get_viewable_archive_item``, does not require browse renderability
    (used by staff manage edit/delete and similar authenticated surfaces).
    """
    base = queryset if queryset is not None else ArchiveItem.objects.all()
    qs = filter_archive_items_for_user(user, base)
    try:
        return qs.select_related(
            "manual_text_content",
            "ocr_document",
            "photo_content",
            "video_content",
        ).get(id=item_id)
    except ArchiveItem.DoesNotExist:
        raise Http404() from None


def get_viewable_archive_item(
    user,
    item_id: int,
    *,
    queryset: QuerySet[ArchiveItem] | None = None,
) -> ArchiveItem:
    """
    Return an archive item currently renderable on ``/archive/<id>/``, or raise Http404.

    Applies visibility/access rules and upload-completion eligibility for PHOTO/OCR.
    Uses 404 for missing ids, unauthorized items, and non-renderable rows.
    """
    base = queryset if queryset is not None else ArchiveItem.objects.all()
    qs = filter_browse_renderable_archive_items(
        filter_archive_items_for_user(user, base)
    )
    try:
        return qs.select_related(
            "manual_text_content",
            "ocr_document",
            "photo_content",
            "video_content",
        ).get(id=item_id)
    except ArchiveItem.DoesNotExist:
        raise Http404() from None
