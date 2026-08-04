"""Document visibility and view access for the public archive."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from documents.models import ArchiveItem, Document


def is_document_admin(user) -> bool:
    """Staff/superuser: may use admin workflows and view private (family) items.

    Restricted items still require ``documents.view_restricted_archiveitem``.
    """
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    )


def user_can_view_document(user, document: Document) -> bool:
    """Whether ``user`` may view ``document`` (metadata, text, presigned source URLs)."""
    from documents.services.archive_item_access import can_view_archive_item

    return can_view_archive_item(user, document.archive_item)


def filter_documents_for_user(user, queryset: QuerySet[Document]) -> QuerySet[Document]:
    """Restrict ``queryset`` to documents the user may list or open by id."""
    from documents.services.archive_item_access import filter_archive_items_for_user

    visible_archive_item_ids = filter_archive_items_for_user(
        user,
        ArchiveItem.objects.filter(item_type=ArchiveItem.ItemType.OCR_DOCUMENT),
    ).values_list("pk", flat=True)
    qs = queryset.filter(archive_item_id__in=visible_archive_item_ids)
    if not is_document_admin(user):
        qs = qs.filter(upload_status=Document.UploadStatus.UPLOADED)
    return qs


def document_queryset_for_user(user) -> QuerySet[Document]:
    """Base queryset of documents visible in list/detail/API for ``user``."""
    return filter_documents_for_user(user, Document.objects.all())


def get_viewable_document(
    user,
    doc_id: int,
    *,
    queryset: QuerySet[Document] | None = None,
) -> Document:
    """
    Return a document the user may view, or raise Http404.

    Uses 404 for both missing ids and unauthorized documents.
    """
    base = (
        queryset
        if queryset is not None
        else Document.objects.select_related("archive_item").all()
    )
    qs = filter_documents_for_user(user, base)
    try:
        return qs.get(id=doc_id)
    except Document.DoesNotExist:
        raise Http404() from None
