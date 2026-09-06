"""Unified public People directory presentation (Person-backed and Author-only).

Person and Author remain distinct models. Rows are merged in Python after
SQL authorization/membership/search. Linked Authors (``Author.person``) are
absorbed into the Person row and are never a second directory identity.
Name equality is not identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    normalize_archive_public_list_page,
    person_public_page_url,
)
from documents.services.author_public import (
    author_public_page_url,
    public_authors_item_counts_for_author_ids,
    public_unlinked_authors_queryset,
)
from documents.services.person_public import (
    public_people_item_counts_for_person_ids,
    public_people_queryset,
)


class PublicDirectoryIdentityKind:
    """Internal row kind. Not shown in public UI."""

    PERSON = "person"
    AUTHOR = "author"


_KIND_SORT_RANK = {
    PublicDirectoryIdentityKind.PERSON: 0,
    PublicDirectoryIdentityKind.AUTHOR: 1,
}


@dataclass(frozen=True, slots=True)
class PublicDirectoryIdentity:
    """One authorized public identity before item-count attachment."""

    identity_kind: str
    source_id: int
    name: str


@dataclass(frozen=True, slots=True)
class PublicDirectoryRow:
    """One public People-directory row.

    ``identity_kind`` and ``source_id`` are for pagination/tests, not display.
    """

    identity_kind: str
    source_id: int
    name: str
    href: str
    item_count: int


def _directory_sort_key(identity: PublicDirectoryIdentity) -> tuple[str, int, int]:
    return (
        identity.name,
        _KIND_SORT_RANK[identity.identity_kind],
        identity.source_id,
    )


def _href_for_identity(identity: PublicDirectoryIdentity) -> str:
    if identity.identity_kind == PublicDirectoryIdentityKind.PERSON:
        return person_public_page_url(identity.source_id)
    return author_public_page_url(identity.source_id)


def list_public_directory_identities(
    user,
    *,
    search_query: str = "",
) -> list[PublicDirectoryIdentity]:
    """Authorized Person-backed and Author-only identities, globally ordered.

    Membership and ``q`` are applied in SQL per identity type. Results are
    merged here so pagination is global, not concatenated page slices.
    """
    people = public_people_queryset(user, search_query=search_query).values_list(
        "id", "name"
    )
    authors = public_unlinked_authors_queryset(
        user, search_query=search_query
    ).values_list("id", "name")
    identities = [
        PublicDirectoryIdentity(
            identity_kind=PublicDirectoryIdentityKind.PERSON,
            source_id=int(person_id),
            name=name,
        )
        for person_id, name in people
    ]
    identities.extend(
        PublicDirectoryIdentity(
            identity_kind=PublicDirectoryIdentityKind.AUTHOR,
            source_id=int(author_id),
            name=name,
        )
        for author_id, name in authors
    )
    identities.sort(key=_directory_sort_key)
    return identities


def build_paginated_public_directory_rows(
    user,
    *,
    search_query: str = "",
    page_raw=None,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
) -> tuple[list[PublicDirectoryRow], int, int]:
    """Return the current page of directory rows, total count, and normalized page."""
    identities = list_public_directory_identities(user, search_query=search_query)
    total_count = len(identities)
    page = normalize_archive_public_list_page(
        page_raw,
        total_count=total_count,
        per_page=per_page,
    )
    offset = (page - 1) * per_page
    page_identities = identities[offset : offset + per_page]
    person_ids = [
        identity.source_id
        for identity in page_identities
        if identity.identity_kind == PublicDirectoryIdentityKind.PERSON
    ]
    author_ids = [
        identity.source_id
        for identity in page_identities
        if identity.identity_kind == PublicDirectoryIdentityKind.AUTHOR
    ]
    person_counts = public_people_item_counts_for_person_ids(user, person_ids)
    author_counts = public_authors_item_counts_for_author_ids(user, author_ids)
    rows: list[PublicDirectoryRow] = []
    for identity in page_identities:
        if identity.identity_kind == PublicDirectoryIdentityKind.PERSON:
            item_count = person_counts.get(identity.source_id, 0)
        else:
            item_count = author_counts.get(identity.source_id, 0)
        rows.append(
            PublicDirectoryRow(
                identity_kind=identity.identity_kind,
                source_id=identity.source_id,
                name=identity.name,
                href=_href_for_identity(identity),
                item_count=item_count,
            )
        )
    return rows, total_count, page
