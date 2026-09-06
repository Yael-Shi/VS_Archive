"""Ordered ArchiveItemAuthor relations and legacy author_name dual-write.

Author is bibliographic, not Person. Callers that persist ``author_name`` on
OCR / MANUAL_TEXT / VIDEO writers must use ``apply_legacy_author_name`` in the
same transaction unless they pass staff ``author_ids`` / ``new_author_name``,
which use ``apply_staff_archive_item_authors``. PHOTO writers do not dual-write.
``apply_legacy_author_name`` does not split commas. ``rename_author`` renames one
Author globally and rebuilds every affected ``author_name`` from its ordered links.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.db.models import Count, Q

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_metadata_validation import SOURCE_METADATA_MAX_LENGTH

AMBIGUOUS_AUTHOR_ERROR = (
    "multiple authors exist with this exact name; resolve duplicates before saving"
)
DUPLICATE_AUTHOR_IN_ORDER_ERROR = "duplicate author in ordered author list"
AUTHOR_NOT_FOUND_ERROR = "מחבר/ת לא נמצא."
AUTHOR_NAME_TOO_LONG_ERROR = "שם המחבר/ת חייב להיות עד 255 תווים."
AUTHOR_NAMES_COMMAS_ONLY_ERROR = (
    "יש להזין לפחות שם מחבר/ת אחד. לא ניתן לשמור קלט שמכיל רק פסיקים או רווחים."
)
AUTHOR_JOINED_TOO_LONG_ERROR = "מחבר/ת חייב להיות עד 255 תווים"
AUTHOR_NAME_REQUIRED_ERROR = "יש להזין שם מחבר/ת."
AUTHOR_NAME_COLLISION_ERROR = (
    "קיים/ת כבר מחבר/ת אחר/ת בשם זהה. שינוי השם היה מאחד רשומות ולכן נחסם."
)
AUTHOR_LINKS_CHANGED_RETRY_ERROR = (
    "רשימת הפריטים המשויכים השתנתה בזמן השמירה. יש לטעון מחדש ולנסות שוב."
)
AUTHOR_IDS_FIELD = "author_ids"
NEW_AUTHOR_NAME_FIELD = "new_author_name"
AUTHOR_NAME_MAX_LENGTH = SOURCE_METADATA_MAX_LENGTH


class ArchiveItemAuthorError(Exception):
    """Fail-closed author relation write error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class StaffAuthorChoice:
    id: int
    name: str
    selected: bool = False


def staff_author_index_queryset(*, search_query: str = ""):
    """Staff Author index: one row per Author, with ArchiveItemAuthor counts.

    Optional ``search_query`` matches ``Author.name`` case-insensitively after
    trim. It does not search aliases, Person, or ``ArchiveItem.author_name``.
    Counts use ``Count("archive_item_links")`` so the index does not N+1.
    """
    authors = Author.objects.annotate(
        archive_item_author_count=Count("archive_item_links"),
    ).order_by("name", "id")
    q = (search_query or "").strip()
    if q:
        authors = authors.filter(name__icontains=q)
    return authors


def ordered_author_links(archive_item: ArchiveItem) -> list[ArchiveItemAuthor]:
    """Return this item's author links in position order."""
    return list(
        archive_item.author_links.select_related("author").order_by("position", "id")
    )


def searchable_author_names_for_item(archive_item: ArchiveItem) -> tuple[str, ...]:
    """Author strings for public ``q`` indexing and match-source attribution.

    When any ``ArchiveItemAuthor`` row exists, return those ``Author.name``
    values in ``(position, id)`` order. ``author_name`` is ignored then,
    including stale or empty values. When there are zero links, return the
    trimmed ``author_name`` when nonempty.
    """
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and "author_links" in cache:
        links = sorted(
            cache["author_links"],
            key=lambda link: (link.position, link.id),
        )
    else:
        links = ordered_author_links(archive_item)
    if links:
        return tuple(link.author.name for link in links)
    fallback = (archive_item.author_name or "").strip()
    return (fallback,) if fallback else ()


def ordered_authors(archive_item: ArchiveItem) -> list[Author]:
    """Return this item's authors in position order."""
    return [link.author for link in ordered_author_links(archive_item)]


def _joined_author_name(names: Sequence[str]) -> str:
    """Build the compatibility ``ArchiveItem.author_name`` from ordered names."""
    return ", ".join(names)


def empty_archive_item_authors_form_fields() -> dict[str, Any]:
    """Empty staff form values for item-level authors on create."""
    return {
        AUTHOR_IDS_FIELD: [],
        NEW_AUTHOR_NAME_FIELD: "",
    }


def archive_item_authors_form_data_from_item(
    archive_item: ArchiveItem,
) -> dict[str, Any]:
    """Seed staff form values from current ArchiveItemAuthor links in position order."""
    return {
        AUTHOR_IDS_FIELD: [author.pk for author in ordered_authors(archive_item)],
        NEW_AUTHOR_NAME_FIELD: "",
    }


def split_comma_separated_author_names(raw: str | None) -> list[str]:
    """Split, trim, drop empty tokens, and order-preserving dedupe within input.

    Splits on ASCII commas only. Does not look up or merge existing Authors.
    """
    names: list[str] = []
    seen: set[str] = set()
    for token in (raw or "").split(","):
        name = token.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def parse_new_author_names_input(raw: str | None) -> tuple[str, list[str], list[str]]:
    """Validate comma-separated Author name tokens.

    Returns ``(display, names, errors)``. Empty/whitespace-only input is a
    no-op. Nonempty input that yields no tokens after split/trim is rejected.
    Each remaining token is checked independently against the name length limit.
    Does not create Author rows. Exact-name reuse is resolved by callers.
    """
    display = "" if raw is None else str(raw).strip()
    if not display:
        return "", [], []
    names = split_comma_separated_author_names(display)
    if not names:
        return display, [], [AUTHOR_NAMES_COMMAS_ONLY_ERROR]
    errors: list[str] = []
    for name in names:
        if len(name) > AUTHOR_NAME_MAX_LENGTH:
            errors.append(AUTHOR_NAME_TOO_LONG_ERROR)
            break
    return display, names, errors


def parse_author_ids(post_data) -> tuple[list[int], list[str]]:
    """Parse Author primary keys from keep checkboxes, add-picker, or JSON.

    Values must be positive integers. Names are not accepted.
    """
    if hasattr(post_data, "getlist"):
        raw_values = post_data.getlist(AUTHOR_IDS_FIELD)
    else:
        raw = post_data.get(AUTHOR_IDS_FIELD) if post_data is not None else None
        if raw is None:
            raw_values = []
        elif isinstance(raw, (list, tuple)):
            raw_values = list(raw)
        else:
            raw_values = [raw]

    author_ids: list[int] = []
    seen: set[int] = set()
    errors: list[str] = []
    for raw in raw_values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            author_id = int(text)
        except (TypeError, ValueError):
            errors.append(AUTHOR_NOT_FOUND_ERROR)
            return [], errors
        if author_id < 1:
            errors.append(AUTHOR_NOT_FOUND_ERROR)
            return [], errors
        if author_id not in seen:
            seen.add(author_id)
            author_ids.append(author_id)
    return author_ids, errors


def parse_new_author_name(post_data) -> tuple[str, list[str], list[str]]:
    raw = post_data.get(NEW_AUTHOR_NAME_FIELD) if post_data is not None else None
    return parse_new_author_names_input(raw)


def _authors_in_submitted_id_order(author_ids: list[int]) -> tuple[list[Author], bool]:
    authors = list(Author.objects.filter(pk__in=author_ids))
    by_id = {author.pk: author for author in authors}
    if set(by_id) != set(author_ids):
        return [], False
    return [by_id[author_id] for author_id in author_ids], True


def _authors_grouped_by_exact_name(
    names: Sequence[str],
    *,
    for_update: bool = False,
) -> dict[str, list[Author]]:
    unique_names = list(dict.fromkeys(names))
    grouped: dict[str, list[Author]] = {name: [] for name in unique_names}
    if not unique_names:
        return grouped
    queryset = Author.objects.filter(name__in=unique_names).order_by("id")
    if for_update:
        queryset = queryset.select_for_update()
    for author in queryset:
        grouped.setdefault(author.name, []).append(author)
    return grouped


def _staff_author_plan(
    selected_authors: Sequence[Author],
    new_names: Sequence[str],
    matches_by_name: dict[str, list[Author]],
) -> tuple[list[Author], list[str], list[str]]:
    """Plan kept/reused authors plus names that still need a new Author row.

    Selected ids keep first-occurrence order. Each new token reuses the unique
    exact-name Author, queues a create when none exists, and fails closed when
    more than one exact match exists. Duplicate Author ids are skipped.
    """
    ordered: list[Author] = []
    seen_ids: set[int] = set()
    for author in selected_authors:
        if author.pk in seen_ids:
            continue
        seen_ids.add(author.pk)
        ordered.append(author)

    pending_create: list[str] = []
    pending_seen: set[str] = set()
    for name in new_names:
        matches = matches_by_name.get(name, [])
        if len(matches) > 1:
            return [], [], [AMBIGUOUS_AUTHOR_ERROR]
        if len(matches) == 1:
            author = matches[0]
            if author.pk not in seen_ids:
                seen_ids.add(author.pk)
                ordered.append(author)
            continue
        if name in pending_seen:
            continue
        pending_seen.add(name)
        pending_create.append(name)
    return ordered, pending_create, []


def parse_archive_item_authors_form(post_data) -> tuple[dict[str, Any], list[str]]:
    """Parse staff author fields and reject invalid ids or ambiguous names.

    Validates before any caller write. Empty ids plus empty new names is a
    valid clear. Typed tokens with exactly one exact Author match are reused;
    unmatched tokens are new names. Does not create Author rows or Person rows.
    """
    author_ids, id_errors = parse_author_ids(post_data)
    display, names, name_errors = parse_new_author_name(post_data)
    errors = id_errors + name_errors
    selected_authors: list[Author] = []
    if not errors and author_ids:
        selected_authors, found = _authors_in_submitted_id_order(author_ids)
        if not found:
            errors.append(AUTHOR_NOT_FOUND_ERROR)
    if not errors:
        reused, pending_create, plan_errors = _staff_author_plan(
            selected_authors,
            names,
            _authors_grouped_by_exact_name(names),
        )
        errors.extend(plan_errors)
        if not errors:
            ordered_names = [author.name for author in reused] + pending_create
            joined = _joined_author_name(ordered_names)
            if len(joined) > AUTHOR_NAME_MAX_LENGTH:
                errors.append(AUTHOR_JOINED_TOO_LONG_ERROR)
    return {
        AUTHOR_IDS_FIELD: author_ids,
        NEW_AUTHOR_NAME_FIELD: display,
    }, errors


def build_staff_author_choices(
    *,
    selected_author_ids: list[int] | tuple[int, ...] | set[int],
) -> tuple[list[StaffAuthorChoice], list[StaffAuthorChoice]]:
    """Return picker choices with kept authors first in submitted order.

    Remaining unselected authors follow in ``(name, id)`` order for the add
    picker. Kept authors are rendered as keep/remove checkboxes, not in the
    native multi-select, so a no-op save does not require Ctrl-click.
    """
    selected_ids = list(
        dict.fromkeys(int(author_id) for author_id in selected_author_ids)
    )
    selected_set = set(selected_ids)
    all_authors = list(Author.objects.order_by("name", "id"))
    by_id = {author.pk: author for author in all_authors}

    choices: list[StaffAuthorChoice] = []
    selected: list[StaffAuthorChoice] = []
    for author_id in selected_ids:
        author = by_id.get(author_id)
        if author is None:
            continue
        choice = StaffAuthorChoice(id=author.pk, name=author.name, selected=True)
        choices.append(choice)
        selected.append(choice)
    for author in all_authors:
        if author.pk in selected_set:
            continue
        choices.append(
            StaffAuthorChoice(id=author.pk, name=author.name, selected=False)
        )
    return choices, selected


def _resolve_unique_author_by_exact_name(name: str) -> Author:
    matches = list(Author.objects.select_for_update().filter(name=name).order_by("id"))
    if len(matches) > 1:
        raise ArchiveItemAuthorError(AMBIGUOUS_AUTHOR_ERROR)
    if matches:
        return matches[0]
    return Author.objects.create(name=name)


def _replace_author_links(
    archive_item: ArchiveItem,
    authors: Sequence[Author],
) -> list[ArchiveItemAuthor]:
    unique_ids = [author.pk for author in authors]
    if len(unique_ids) != len(set(unique_ids)):
        raise ArchiveItemAuthorError(DUPLICATE_AUTHOR_IN_ORDER_ERROR)

    ArchiveItemAuthor.objects.filter(archive_item=archive_item).delete()
    links: list[ArchiveItemAuthor] = []
    for position, author in enumerate(authors):
        links.append(
            ArchiveItemAuthor.objects.create(
                archive_item=archive_item,
                author=author,
                position=position,
            )
        )
    return links


@transaction.atomic
def replace_archive_item_authors(
    archive_item: ArchiveItem,
    authors: Sequence[Author],
) -> list[ArchiveItemAuthor]:
    """Replace author links with ``authors`` in order (position 0..n-1).

    Does not write ``author_name``, create Person rows, or split names.
    Duplicate authors in ``authors`` fail closed before any relation write.
    """
    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist
    return _replace_author_links(locked_item, authors)


@transaction.atomic
def apply_staff_archive_item_authors(
    archive_item: ArchiveItem,
    *,
    author_ids: list[int] | None = None,
    new_author_name: str = "",
) -> list[ArchiveItemAuthor]:
    """Replace ordered author links from staff ids plus comma-separated names.

    Submitted ids keep first-occurrence order. Each new token reuses the unique
    exact-name Author, creates one when none exists, and fails closed when more
    than one exact match exists — before any item, Author, or link write.
    Duplicate Author ids (already selected or repeated tokens) keep the first
    occurrence. Dual-writes ``author_name`` as ``", ".join(ordered names)``.
    Empty ids plus empty new names clears relations and the compatibility
    string. Unlinking removes ``ArchiveItemAuthor`` rows only; Author rows are
    not deleted. Does not create Person / ArchiveItemPerson / PhotoPerson rows.
    """
    parsed, errors = parse_archive_item_authors_form(
        {
            AUTHOR_IDS_FIELD: list(author_ids or []),
            NEW_AUTHOR_NAME_FIELD: new_author_name,
        }
    )
    if errors:
        raise ArchiveItemAuthorError(errors[0])

    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist

    locked_ids = parsed[AUTHOR_IDS_FIELD]
    existing_authors: list[Author] = []
    if locked_ids:
        locked_existing = list(
            Author.objects.select_for_update().filter(pk__in=locked_ids)
        )
        by_id = {author.pk: author for author in locked_existing}
        if set(by_id) != set(locked_ids):
            raise ArchiveItemAuthorError(AUTHOR_NOT_FOUND_ERROR)
        existing_authors = [by_id[author_id] for author_id in locked_ids]

    _display, names, name_errors = parse_new_author_names_input(new_author_name)
    if name_errors:
        raise ArchiveItemAuthorError(name_errors[0])

    reused, pending_create, plan_errors = _staff_author_plan(
        existing_authors,
        names,
        _authors_grouped_by_exact_name(names, for_update=True),
    )
    if plan_errors:
        raise ArchiveItemAuthorError(plan_errors[0])

    seen_ids = {author.pk for author in reused}
    ordered = list(reused)
    for name in pending_create:
        matches = list(
            Author.objects.select_for_update().filter(name=name).order_by("id")
        )
        if len(matches) > 1:
            raise ArchiveItemAuthorError(AMBIGUOUS_AUTHOR_ERROR)
        if matches:
            author = matches[0]
            if author.pk not in seen_ids:
                seen_ids.add(author.pk)
                ordered.append(author)
            continue
        created = Author.objects.create(name=name)
        seen_ids.add(created.pk)
        ordered.append(created)

    joined = _joined_author_name([author.name for author in ordered])
    if len(joined) > AUTHOR_NAME_MAX_LENGTH:
        raise ArchiveItemAuthorError(AUTHOR_JOINED_TOO_LONG_ERROR)

    locked_item.author_name = joined
    locked_item.save(update_fields=["author_name", "updated_at"])
    archive_item.author_name = joined
    return _replace_author_links(locked_item, ordered)


@transaction.atomic
def apply_legacy_author_name(
    archive_item: ArchiveItem,
    author_name: str,
) -> list[ArchiveItemAuthor]:
    """Atomically persist ``author_name`` and dual-write one Author at position 0.

    Empty/whitespace-only clears ``author_name`` and all author relations.
    Exact name match reuses the single Author, creates one if none exists, and
    fails closed if multiple exact matching Authors exist — before any item,
    string, or relation write. Does not split commas, strip titles, merge
    spelling variants, or create Person / ArchiveItemPerson / PhotoPerson rows.
    """
    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist

    exact_name = (author_name or "").strip()
    resolved: Author | None = None
    if exact_name:
        resolved = _resolve_unique_author_by_exact_name(exact_name)

    locked_item.author_name = exact_name
    locked_item.save(update_fields=["author_name", "updated_at"])
    archive_item.author_name = exact_name

    links = _replace_author_links(
        locked_item,
        [resolved] if resolved is not None else [],
    )
    return links


def affected_archive_item_ids_for_author(author: Author) -> list[int]:
    """Return ascending ids of every ArchiveItem linked to this Author.

    Ascending order matches ``sync_archive_item_search_indexes`` so rename
    locking and index fan-out take rows in the same order.
    """
    return sorted(
        ArchiveItemAuthor.objects.filter(author=author)
        .values_list("archive_item_id", flat=True)
        .distinct()
    )


def affected_archive_items_for_author(author: Author) -> list[ArchiveItem]:
    """Return every ArchiveItem linked to this Author, for staff preview only."""
    return list(
        ArchiveItem.objects.filter(
            pk__in=affected_archive_item_ids_for_author(author)
        ).order_by("pk")
    )


def _lock_archive_items_for_update(item_ids: Sequence[int]) -> dict[int, ArchiveItem]:
    """Lock ArchiveItem rows in ascending pk order.

    Matches ``apply_staff_archive_item_authors`` / ``apply_legacy_author_name``,
    which lock the item before any Author row.
    """
    ordered_ids = sorted(set(item_ids))
    if not ordered_ids:
        return {}
    return {
        item.pk: item
        for item in (
            ArchiveItem.objects.select_for_update()
            .filter(pk__in=ordered_ids)
            .order_by("pk")
        )
    }


def _lock_archive_item_author_links_for_update(
    item_ids: Sequence[int],
) -> list[ArchiveItemAuthor]:
    """Lock through rows for ``item_ids`` in (item, position, id) order."""
    ordered_ids = sorted(set(item_ids))
    if not ordered_ids:
        return []
    return list(
        ArchiveItemAuthor.objects.select_for_update()
        .filter(archive_item_id__in=ordered_ids)
        .order_by("archive_item_id", "position", "id")
    )


def _lock_authors_for_update(
    *,
    author_ids: Sequence[int],
    exact_name: str | None = None,
) -> dict[int, Author]:
    """Lock Author rows in ascending pk order.

    Includes ``author_ids`` and, when ``exact_name`` is set, every Author with
    that exact name (collision candidates). One query so co-authors and
    collision rows are taken in the same deterministic order as item-level
    writers, which lock Authors only after the ArchiveItem.
    """
    ordered_ids = sorted({int(author_id) for author_id in author_ids})
    query = Q()
    if ordered_ids:
        query |= Q(pk__in=ordered_ids)
    if exact_name is not None:
        query |= Q(name=exact_name)
    if not query:
        return {}
    return {
        author.pk: author
        for author in Author.objects.select_for_update().filter(query).order_by("pk")
    }


@transaction.atomic
def rename_author(author: Author, *, name: str) -> Author:
    """Rename one Author globally and rebuild every linked ``author_name``.

    The rename applies to every linked ArchiveItem; there is no per-item
    override. Blank names, names over 255 characters, and an exact name
    collision with another Author are rejected before any write. A collision
    is never merged: duplicate ``Author.name`` rows make item-level saves fail
    closed with ``AMBIGUOUS_AUTHOR_ERROR``.

    Lock order matches item-level author writers and is deterministic:
    affected ``ArchiveItem`` rows (ascending pk), then their
    ``ArchiveItemAuthor`` rows, then every ``Author`` whose name is used to
    rebuild ``author_name`` plus any exact-name collision candidate
    (ascending pk). After those Author locks, the target Author's linked
    item ids are re-read. A newly linked item that is not already locked
    fails closed with ``AUTHOR_LINKS_CHANGED_RETRY_ERROR`` (staff must retry).
    Additional ArchiveItem rows are never locked while Author locks are held.
    A concurrently removed link is simply omitted from the locked through
    rows. Co-author names are read only from the locked Author rows.

    Prevalidates every rebuilt joined ``author_name`` against the 255-character
    limit before renaming. Each affected ``author_name`` is then rebuilt from
    that item's ordered author links and the affected search indexes are
    refreshed in the same transaction. Renaming to the current name is a no-op
    with no writes and no index refresh. Does not create, delete, or merge
    Author rows, change author ``position`` values, or touch Person /
    ArchiveItemPerson / PhotoPerson rows.
    """
    normalized = (name or "").strip()
    if not normalized:
        raise ArchiveItemAuthorError(AUTHOR_NAME_REQUIRED_ERROR)
    if len(normalized) > AUTHOR_NAME_MAX_LENGTH:
        raise ArchiveItemAuthorError(AUTHOR_NAME_TOO_LONG_ERROR)

    locked_items: dict[int, ArchiveItem] = {}
    while True:
        current_ids = affected_archive_item_ids_for_author(author)
        missing_ids = [
            item_id for item_id in current_ids if item_id not in locked_items
        ]
        if not missing_ids:
            break
        locked_items.update(_lock_archive_items_for_update(missing_ids))

    locked_links = _lock_archive_item_author_links_for_update(locked_items.keys())
    author_ids_for_rebuild = {link.author_id for link in locked_links}
    author_ids_for_rebuild.add(author.pk)

    locked_authors = _lock_authors_for_update(
        author_ids=author_ids_for_rebuild,
        exact_name=normalized,
    )
    locked_author = locked_authors.get(author.pk)
    if locked_author is None:
        raise Author.DoesNotExist
    if locked_author.name == normalized:
        return locked_author
    if any(
        other.pk != locked_author.pk and other.name == normalized
        for other in locked_authors.values()
    ):
        raise ArchiveItemAuthorError(AUTHOR_NAME_COLLISION_ERROR)

    current_linked_ids = affected_archive_item_ids_for_author(locked_author)
    if any(item_id not in locked_items for item_id in current_linked_ids):
        raise ArchiveItemAuthorError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)

    names_by_author_id = {pk: row.name for pk, row in locked_authors.items()}
    names_by_author_id[locked_author.pk] = normalized

    links_by_item_id: dict[int, list[ArchiveItemAuthor]] = {}
    for link in locked_links:
        links_by_item_id.setdefault(link.archive_item_id, []).append(link)

    affected_ids = sorted(
        {
            link.archive_item_id
            for link in locked_links
            if link.author_id == locked_author.pk
        }
    )
    rebuilt_author_names: dict[int, str] = {}
    for item_id in affected_ids:
        item_links = links_by_item_id.get(item_id, [])
        try:
            joined = _joined_author_name(
                [names_by_author_id[link.author_id] for link in item_links]
            )
        except KeyError as exc:
            raise ArchiveItemAuthorError(AUTHOR_NOT_FOUND_ERROR) from exc
        if len(joined) > AUTHOR_NAME_MAX_LENGTH:
            raise ArchiveItemAuthorError(AUTHOR_JOINED_TOO_LONG_ERROR)
        rebuilt_author_names[item_id] = joined

    locked_author.name = normalized
    locked_author.save(update_fields=["name", "updated_at"])
    author.name = normalized

    for item_id in affected_ids:
        locked_item = locked_items[item_id]
        locked_item.author_name = rebuilt_author_names[item_id]
        locked_item.save(update_fields=["author_name", "updated_at"])

    if affected_ids:
        from documents.services.archive_search_index import (
            sync_archive_item_search_indexes,
        )

        sync_archive_item_search_indexes(affected_ids)

    return locked_author
