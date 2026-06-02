"""Document visibility and view access for the public archive."""

from __future__ import annotations

from django.db.models import QuerySet
from django.http import Http404

from documents.models import Document


def is_document_admin(user) -> bool:
    """Staff/superuser: may view all documents and use admin workflows."""
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        )
    )


def user_can_view_document(user, document: Document) -> bool:
    """Whether ``user`` may view ``document`` (metadata, text, presigned source URLs)."""
    if is_document_admin(user):
        return True
    return document.visibility == Document.Visibility.PUBLIC


def filter_documents_for_user(user, queryset: QuerySet[Document]) -> QuerySet[Document]:
    """Restrict ``queryset`` to documents the user may list or open by id."""
    if is_document_admin(user):
        return queryset
    return queryset.filter(visibility=Document.Visibility.PUBLIC)


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

    Uses 404 for both missing ids and unauthorized private documents.
    """
    base = queryset if queryset is not None else Document.objects.all()
    qs = filter_documents_for_user(user, base)
    try:
        return qs.get(id=doc_id)
    except Document.DoesNotExist:
        raise Http404() from None
