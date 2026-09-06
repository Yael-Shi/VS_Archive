"""Public Author catalog and Author-detail ArchiveItem relations.

Membership is ArchiveItemAuthor only. Author is not Person. ArchiveItem.author_name
is not a public membership signal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db.models import Count, Exists, OuterRef, Q, QuerySet
from django.urls import reverse

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_item_access import archive_browse_queryset_for_user


@dataclass(frozen=True, slots=True)
class PublicAuthorIndexRow:
    """One public Authors-index row (Author.id is not displayed)."""

    name: str
    href: str
    item_count: int


def author_public_page_url(author_id: int) -> str:
    """Return ``/archive/authors/<author_id>/``."""
    return reverse("archive-author-detail", kwargs={"author_id": author_id})


def authorized_browse_item_pks(user) -> QuerySet:
    """Authorized + browse-renderable ArchiveItem primary keys for ``user``."""
    return archive_browse_queryset_for_user(user).order_by().values("pk")


def author_public_membership_q(user) -> Q:
    """Author rows with at least one authorized+browse-renderable ArchiveItemAuthor item."""
    return Exists(
        ArchiveItemAuthor.objects.filter(
            author_id=OuterRef("pk"),
            archive_item_id__in=authorized_browse_item_pks(user),
        )
    )


def public_authors_queryset(user, *, search_query: str = "") -> QuerySet[Author]:
    """Public Authors index queryset: membership, optional name q, name then id."""
    authors = Author.objects.filter(author_public_membership_q(user)).order_by(
        "name", "id"
    )
    q = (search_query or "").strip()
    if q:
        authors = authors.filter(name__icontains=q)
    return authors


def public_author_archive_items_queryset(
    user, author_id: int
) -> QuerySet[ArchiveItem]:
    """Distinct authorized+renderable ArchiveItems linked via ArchiveItemAuthor.

    Outer queryset is ``ArchiveItem``, so duplicate link rows cannot duplicate
    results. Order matches public browse: ``-created_at``, then ``pk``.
    """
    aia_exists = Exists(
        ArchiveItemAuthor.objects.filter(
            author_id=author_id,
            archive_item_id=OuterRef("pk"),
        )
    )
    return (
        archive_browse_queryset_for_user(user)
        .filter(aia_exists)
        .order_by("-created_at", "pk")
    )


def public_authors_item_counts_for_author_ids(
    user,
    author_ids: Iterable[int],
) -> dict[int, int]:
    """DISTINCT authorized+browse-renderable ArchiveItem counts for a page of Author ids.

    One page-restricted ``ArchiveItemAuthor`` aggregate. Unique
    ``(archive_item, author)`` already prevents double-counting; ``distinct``
    keeps the catalog contract explicit. Does not read ``author_name`` or
    Person.
    """
    page_ids = [int(author_id) for author_id in author_ids]
    if not page_ids:
        return {}

    rows = (
        ArchiveItemAuthor.objects.filter(
            author_id__in=page_ids,
            archive_item_id__in=authorized_browse_item_pks(user),
        )
        .values("author_id")
        .annotate(item_count=Count("archive_item_id", distinct=True))
    )
    return {int(row["author_id"]): int(row["item_count"]) for row in rows}


def build_public_authors_index_rows(
    user,
    authors: Sequence[Author],
) -> list[PublicAuthorIndexRow]:
    """Attach DISTINCT public item counts to a page of Author rows."""
    counts = public_authors_item_counts_for_author_ids(
        user, [author.pk for author in authors]
    )
    return [
        PublicAuthorIndexRow(
            name=author.name,
            href=author_public_page_url(author.pk),
            item_count=counts.get(author.pk, 0),
        )
        for author in authors
    ]
