"""Public Author catalog and Author-detail ArchiveItem relations.

Membership is ArchiveItemAuthor only. Author is not Person. ArchiveItem.author_name
is not a public membership signal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from django.db.models import Exists, OuterRef, QuerySet
from django.urls import reverse

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.public_holdings import (
    EMPTY_PUBLIC_HOLDINGS,
    PublicHoldingsCounts,
    holdings_from_item_type_pairs,
)


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


def author_public_membership_q(user) -> Exists:
    """Author rows with at least one authorized+browse-renderable ArchiveItemAuthor item."""
    return Exists(
        ArchiveItemAuthor.objects.filter(
            author_id=OuterRef("pk"),
            archive_item_id__in=authorized_browse_item_pks(user),
        )
    )


def public_authors_queryset(user, *, search_query: str = "") -> QuerySet[Author]:
    """Authors with public AIA membership (linked and unlinked).

    The public People directory uses ``public_unlinked_authors_queryset`` so
    linked Authors are absorbed into Person-backed rows.
    """
    authors = Author.objects.filter(author_public_membership_q(user)).order_by(
        "name", "id"
    )
    q = (search_query or "").strip()
    if q:
        authors = authors.filter(name__icontains=q)
    return authors


def public_unlinked_authors_queryset(
    user, *, search_query: str = ""
) -> QuerySet[Author]:
    """Public directory Author-only identities: public AIA and no ``Author.person``."""
    authors = (
        Author.objects.filter(person_id__isnull=True)
        .filter(author_public_membership_q(user))
        .order_by("name", "id")
    )
    q = (search_query or "").strip()
    if q:
        authors = authors.filter(name__icontains=q)
    return authors


def public_author_archive_items_queryset(user, author_id: int) -> QuerySet[ArchiveItem]:
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


def public_authors_holdings_counts_for_author_ids(
    user,
    author_ids: Iterable[int],
) -> dict[int, PublicHoldingsCounts]:
    """DISTINCT authorized+browse-renderable holdings for a page of Author ids.

    One page-restricted ``ArchiveItemAuthor`` query of
    ``(author_id, archive_item_id, item_type)``. Duplicate ArchiveItem ids for
    the same Author are dropped before type counting. Does not read
    ``author_name`` or Person.
    """
    page_ids = [int(author_id) for author_id in author_ids]
    if not page_ids:
        return {}

    typed_pairs = ArchiveItemAuthor.objects.filter(
        author_id__in=page_ids,
        archive_item_id__in=authorized_browse_item_pks(user),
    ).values_list("author_id", "archive_item_id", "archive_item__item_type")
    holdings = holdings_from_item_type_pairs(
        (int(author_id), int(item_id), str(item_type))
        for author_id, item_id, item_type in typed_pairs
    )
    return {
        author_id: holdings.get(author_id, EMPTY_PUBLIC_HOLDINGS)
        for author_id in page_ids
    }


def public_authors_item_counts_for_author_ids(
    user,
    author_ids: Iterable[int],
) -> dict[int, int]:
    """DISTINCT authorized+browse-renderable ArchiveItem totals for a page of Author ids."""
    return {
        author_id: counts.total
        for author_id, counts in public_authors_holdings_counts_for_author_ids(
            user, author_ids
        ).items()
    }


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
