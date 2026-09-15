"""Unified public People directory presentation (Person-backed and Author-only).

Person and Author remain distinct models. Rows are merged in Python after
SQL authorization/membership/search. Linked Authors (``Author.person``) are
absorbed into the Person row and are never a second directory identity.
Name equality is not identity. Directory order uses raw identity names
(``Person.name`` / ``Author.name``), not honorifics or holdings summaries.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    build_archive_public_list_query,
    normalize_archive_public_list_page,
    person_public_page_url,
)
from documents.services.author_public import (
    author_public_page_url,
    public_authors_holdings_counts_for_author_ids,
    public_unlinked_authors_queryset,
)
from documents.services.person_display import format_person_display_name
from documents.services.person_public import (
    public_people_holdings_counts_for_person_ids,
    public_people_queryset,
)
from documents.services.public_holdings import EMPTY_PUBLIC_HOLDINGS


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
    """One authorized public identity before item-count attachment.

    ``name`` is the raw sort/identity string (``Person.name`` or ``Author.name``).
    """

    identity_kind: str
    source_id: int
    name: str
    honorific: str = ""


@dataclass(frozen=True, slots=True)
class PublicDirectoryRow:
    """One public People-directory row.

    ``identity_kind`` and ``source_id`` are for pagination/tests, not display.
    ``name`` is the public display label. ``sort_name`` is the raw identity
    name used for ordering. ``item_count`` remains the DISTINCT ArchiveItem
    total; ``type_counts`` / ``holdings_summary`` are the public breakdown.
    """

    identity_kind: str
    source_id: int
    name: str
    href: str
    item_count: int
    sort_name: str
    type_counts: tuple[tuple[str, int], ...]
    holdings_summary: str


HEBREW_INDEX_LETTERS: tuple[str, ...] = tuple("אבגדהוזחטיכלמנסעפצקרשת")
_HEBREW_INDEX_LETTER_SET = frozenset(HEBREW_INDEX_LETTERS)
_HEBREW_FINAL_TO_REGULAR = str.maketrans("ךםןףץ", "כמנפצ")
OTHER_INDEX_LETTER = "#"
OTHER_INDEX_HEADING = "אחר"


@dataclass(frozen=True, slots=True)
class PublicDirectoryLetterGroup:
    """One A–Z section of the current directory page (display only)."""

    letter: str
    heading: str
    heading_id: str
    rows: tuple[PublicDirectoryRow, ...]


def directory_index_letter(sort_name: str) -> str:
    """First meaningful letter of a raw identity name for A–Z grouping.

    Uses ``Person.name`` / ``Author.name`` only. Skips leading whitespace,
    punctuation, marks, and symbols. Hebrew final letters map to their regular
    forms. Honorific is never consulted.
    """
    for char in sort_name or "":
        if char.isspace():
            continue
        mapped = char.translate(_HEBREW_FINAL_TO_REGULAR)
        if mapped in _HEBREW_INDEX_LETTER_SET:
            return mapped
        if mapped.isalpha():
            return mapped.upper()
        category = unicodedata.category(mapped)
        if category.startswith(("M", "P", "S", "Z", "C")):
            continue
        return OTHER_INDEX_LETTER
    return OTHER_INDEX_LETTER


def people_letter_anchor_id(letter: str) -> str:
    if letter == OTHER_INDEX_LETTER:
        return "people-letter-other"
    return f"people-letter-{letter}"


def group_directory_rows_by_index_letter(
    rows: list[PublicDirectoryRow],
) -> list[PublicDirectoryLetterGroup]:
    """Group already-ordered page rows by ``directory_index_letter(sort_name)``.

    Does not re-sort. Consecutive identical letters form one section in the
    order rows already have (raw identity name).
    """
    groups: list[PublicDirectoryLetterGroup] = []
    current_letter = ""
    current_rows: list[PublicDirectoryRow] = []
    for row in rows:
        letter = directory_index_letter(row.sort_name)
        if letter != current_letter and current_rows:
            groups.append(
                _letter_group(letter=current_letter, rows=tuple(current_rows))
            )
            current_rows = []
        current_letter = letter
        current_rows.append(row)
    if current_rows:
        groups.append(_letter_group(letter=current_letter, rows=tuple(current_rows)))
    return groups


def first_directory_letter_pages(
    sort_names: Iterable[str],
    *,
    per_page: int,
) -> dict[str, int]:
    """First 1-based page of each index letter in an already-ordered name list."""
    first_pages: dict[str, int] = {}
    for index, name in enumerate(sort_names):
        letter = directory_index_letter(name)
        if letter not in first_pages:
            first_pages[letter] = index // per_page + 1
    return first_pages


def hebrew_alphabet_nav_items(
    letter_first_pages: dict[str, int],
    *,
    page: int,
    per_page: int,
    search_query: str = "",
    index_path: str,
) -> list[tuple[str, str]]:
    """``(letter, href_or_empty)`` for the Hebrew jump row.

    ``letter_first_pages`` comes from the full filtered ordered result set
    before pagination. Present letters link to that first page plus the
    existing section anchor. Same-page letters keep the in-page hash.
    Empty letters stay inactive.
    """
    items: list[tuple[str, str]] = []
    for letter in HEBREW_INDEX_LETTERS:
        target_page = letter_first_pages.get(letter)
        if target_page is None:
            items.append((letter, ""))
            continue
        anchor = f"#{people_letter_anchor_id(letter)}"
        if target_page == page:
            items.append((letter, anchor))
            continue
        query = build_archive_public_list_query(
            q=search_query,
            page=target_page,
            per_page=per_page,
        )
        suffix = f"?{query}" if query else ""
        items.append((letter, f"{index_path}{suffix}{anchor}"))
    return items


def _letter_group(
    *, letter: str, rows: tuple[PublicDirectoryRow, ...]
) -> PublicDirectoryLetterGroup:
    heading = OTHER_INDEX_HEADING if letter == OTHER_INDEX_LETTER else letter
    return PublicDirectoryLetterGroup(
        letter=letter,
        heading=heading,
        heading_id=people_letter_anchor_id(letter),
        rows=rows,
    )


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


def _display_name_for_identity(identity: PublicDirectoryIdentity) -> str:
    if identity.identity_kind == PublicDirectoryIdentityKind.PERSON:
        return format_person_display_name(
            name=identity.name, honorific=identity.honorific
        )
    return identity.name


def list_public_directory_identities(
    user,
    *,
    search_query: str = "",
) -> list[PublicDirectoryIdentity]:
    """Authorized Person-backed and Author-only identities, globally ordered.

    Membership and ``q`` are applied in SQL per identity type. Results are
    merged here so pagination is global, not concatenated page slices.
    Person order uses ``Person.name``, not honorific or formatted display name.
    """
    people = public_people_queryset(user, search_query=search_query).values_list(
        "id", "name", "honorific"
    )
    authors = public_unlinked_authors_queryset(
        user, search_query=search_query
    ).values_list("id", "name")
    identities = [
        PublicDirectoryIdentity(
            identity_kind=PublicDirectoryIdentityKind.PERSON,
            source_id=int(person_id),
            name=name,
            honorific=honorific or "",
        )
        for person_id, name, honorific in people
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
) -> tuple[list[PublicDirectoryRow], int, int, dict[str, int]]:
    """Return the current page of rows, total count, page, and first letter pages."""
    identities = list_public_directory_identities(user, search_query=search_query)
    letter_first_pages = first_directory_letter_pages(
        (identity.name for identity in identities),
        per_page=per_page,
    )
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
    person_holdings = public_people_holdings_counts_for_person_ids(user, person_ids)
    author_holdings = public_authors_holdings_counts_for_author_ids(user, author_ids)
    rows: list[PublicDirectoryRow] = []
    for identity in page_identities:
        if identity.identity_kind == PublicDirectoryIdentityKind.PERSON:
            holdings = person_holdings.get(identity.source_id, EMPTY_PUBLIC_HOLDINGS)
        else:
            holdings = author_holdings.get(identity.source_id, EMPTY_PUBLIC_HOLDINGS)
        rows.append(
            PublicDirectoryRow(
                identity_kind=identity.identity_kind,
                source_id=identity.source_id,
                name=_display_name_for_identity(identity),
                href=_href_for_identity(identity),
                item_count=holdings.total,
                sort_name=identity.name,
                type_counts=holdings.type_counts,
                holdings_summary=holdings.summary,
            )
        )
    return rows, total_count, page, letter_first_pages
