"""Controlled staff Author merge: duplicate INTO keeper.

Keeper and duplicate are explicit Author ids. This is not name matching.
Author remains bibliographic and separate from Person. No aliases.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_item_authors import (
    AUTHOR_JOINED_TOO_LONG_ERROR,
    AUTHOR_LINKS_CHANGED_RETRY_ERROR,
    AUTHOR_NAME_MAX_LENGTH,
    AUTHOR_NOT_FOUND_ERROR,
    ArchiveItemAuthorError,
    _joined_author_name,
    _lock_archive_item_author_links_for_update,
    _lock_archive_items_for_update,
    _lock_authors_for_update,
    _replace_author_links,
    affected_archive_item_ids_for_author,
)

AUTHOR_MERGE_ID_REQUIRED_ERROR = "יש להזין מזהה של רשומת המחבר/ת הכפולה."
AUTHOR_MERGE_ID_INVALID_ERROR = "מזהה המחבר/ת חייב להיות מספר שלם חיובי."
AUTHOR_MERGE_SAME_ID_ERROR = "לא ניתן למזג רשומת מחבר/ת עם עצמה."
AUTHOR_MERGE_CONCURRENCY_ERROR = "המיזוג נכשל בגלל שינוי מקביל. נסו שוב."


class AuthorMergeError(ArchiveItemAuthorError):
    """Staff-facing Author merge error. No partial merge is applied."""


@dataclass(frozen=True)
class AuthorMergeItemPreview:
    archive_item_id: int
    title: str
    current_author_name: str
    current_order: tuple[str, ...]
    planned_order: tuple[str, ...]
    keeper_already_linked: bool


@dataclass(frozen=True)
class AuthorMergePreview:
    keeper_id: int
    keeper_name: str
    duplicate_id: int
    duplicate_name: str
    names_differ: bool
    affected_items: tuple[AuthorMergeItemPreview, ...]
    links_moved: int
    links_deduped: int
    blockers: tuple[str, ...]

    @property
    def can_execute(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class AuthorMergeResult:
    keeper_id: int
    deleted_duplicate_id: int
    affected_archive_item_ids: tuple[int, ...]
    links_moved: int
    links_deduped: int
    search_indexes_refreshed: int


def parse_author_merge_id(raw: object) -> int:
    """Parse an explicit Author primary key. Names are not accepted."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise AuthorMergeError(AUTHOR_MERGE_ID_REQUIRED_ERROR)
    try:
        author_id = int(text)
    except (TypeError, ValueError) as exc:
        raise AuthorMergeError(AUTHOR_MERGE_ID_INVALID_ERROR) from exc
    if author_id < 1:
        raise AuthorMergeError(AUTHOR_MERGE_ID_INVALID_ERROR)
    return author_id


def _raise_if_same_author(keeper: Author, duplicate: Author) -> None:
    if keeper.pk == duplicate.pk:
        raise AuthorMergeError(AUTHOR_MERGE_SAME_ID_ERROR)


def _author_label(author: Author) -> str:
    return f"{author.name} (מזהה {author.pk})"


def _planned_author_ids(
    links: list[ArchiveItemAuthor],
    *,
    keeper_id: int,
    duplicate_id: int,
) -> tuple[list[int], bool]:
    """Return final author ids and whether the duplicate link was dropped.

    If keeper is already linked, the duplicate slot is removed and keeper
    stays where it is. Otherwise the duplicate slot becomes keeper.
    """
    keeper_present = any(link.author_id == keeper_id for link in links)
    planned: list[int] = []
    for link in links:
        if link.author_id == duplicate_id:
            if keeper_present:
                continue
            planned.append(keeper_id)
            continue
        planned.append(link.author_id)
    return planned, keeper_present


def _item_preview(
    item: ArchiveItem,
    links: list[ArchiveItemAuthor],
    *,
    keeper: Author,
    duplicate: Author,
) -> tuple[AuthorMergeItemPreview, str, bool]:
    authors_by_id = {link.author_id: link.author for link in links}
    authors_by_id[keeper.pk] = keeper
    authors_by_id[duplicate.pk] = duplicate
    planned_ids, keeper_present = _planned_author_ids(
        links, keeper_id=keeper.pk, duplicate_id=duplicate.pk
    )
    planned_authors = [authors_by_id[author_id] for author_id in planned_ids]
    joined = _joined_author_name([author.name for author in planned_authors])
    preview = AuthorMergeItemPreview(
        archive_item_id=item.pk,
        title=item.title,
        current_author_name=item.author_name,
        current_order=tuple(_author_label(link.author) for link in links),
        planned_order=tuple(_author_label(author) for author in planned_authors),
        keeper_already_linked=keeper_present,
    )
    return preview, joined, keeper_present


def preview_author_merge(*, keeper: Author, duplicate: Author) -> AuthorMergePreview:
    """Read-only merge preview. Does not lock or mutate rows."""
    _raise_if_same_author(keeper, duplicate)
    if not Author.objects.filter(pk=keeper.pk).exists():
        raise AuthorMergeError(AUTHOR_NOT_FOUND_ERROR)
    if not Author.objects.filter(pk=duplicate.pk).exists():
        raise AuthorMergeError(AUTHOR_NOT_FOUND_ERROR)

    item_ids = affected_archive_item_ids_for_author(duplicate)
    items = {
        item.pk: item
        for item in ArchiveItem.objects.filter(pk__in=item_ids).order_by("pk")
    }
    links_by_item: dict[int, list[ArchiveItemAuthor]] = {item_id: [] for item_id in item_ids}
    for link in (
        ArchiveItemAuthor.objects.filter(archive_item_id__in=item_ids)
        .select_related("author")
        .order_by("archive_item_id", "position", "id")
    ):
        links_by_item.setdefault(link.archive_item_id, []).append(link)

    affected: list[AuthorMergeItemPreview] = []
    blockers: list[str] = []
    moved = 0
    deduped = 0
    for item_id in item_ids:
        item = items.get(item_id)
        if item is None:
            continue
        preview, joined, keeper_present = _item_preview(
            item,
            links_by_item.get(item_id, []),
            keeper=keeper,
            duplicate=duplicate,
        )
        affected.append(preview)
        if keeper_present:
            deduped += 1
        else:
            moved += 1
        if len(joined) > AUTHOR_NAME_MAX_LENGTH and AUTHOR_JOINED_TOO_LONG_ERROR not in blockers:
            blockers.append(AUTHOR_JOINED_TOO_LONG_ERROR)

    return AuthorMergePreview(
        keeper_id=keeper.pk,
        keeper_name=keeper.name,
        duplicate_id=duplicate.pk,
        duplicate_name=duplicate.name,
        names_differ=(keeper.name != duplicate.name),
        affected_items=tuple(affected),
        links_moved=moved,
        links_deduped=deduped,
        blockers=tuple(blockers),
    )


def _plan_locked_merge(
    *,
    locked_items: dict[int, ArchiveItem],
    locked_links: list[ArchiveItemAuthor],
    locked_authors: dict[int, Author],
    keeper_id: int,
    duplicate_id: int,
    mutated_ids: list[int],
) -> tuple[dict[int, list[Author]], dict[int, str], int, int]:
    links_by_item: dict[int, list[ArchiveItemAuthor]] = {}
    for link in locked_links:
        links_by_item.setdefault(link.archive_item_id, []).append(link)

    planned_authors_by_item: dict[int, list[Author]] = {}
    rebuilt_author_names: dict[int, str] = {}
    moved = 0
    deduped = 0
    for item_id in mutated_ids:
        item_links = links_by_item.get(item_id, [])
        planned_ids, keeper_present = _planned_author_ids(
            item_links, keeper_id=keeper_id, duplicate_id=duplicate_id
        )
        if keeper_present:
            deduped += 1
        else:
            moved += 1
        try:
            planned_authors = [locked_authors[author_id] for author_id in planned_ids]
        except KeyError as exc:
            raise AuthorMergeError(AUTHOR_NOT_FOUND_ERROR) from exc
        joined = _joined_author_name([author.name for author in planned_authors])
        if len(joined) > AUTHOR_NAME_MAX_LENGTH:
            raise AuthorMergeError(AUTHOR_JOINED_TOO_LONG_ERROR)
        planned_authors_by_item[item_id] = planned_authors
        rebuilt_author_names[item_id] = joined
        if item_id not in locked_items:
            raise AuthorMergeError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)
    return planned_authors_by_item, rebuilt_author_names, moved, deduped


@transaction.atomic
def merge_author(*, keeper: Author, duplicate: Author) -> AuthorMergeResult:
    """Merge duplicate INTO keeper. Fail closed. Duplicate delete is last."""
    _raise_if_same_author(keeper, duplicate)

    locked_items: dict[int, ArchiveItem] = {}
    while True:
        current_ids = affected_archive_item_ids_for_author(duplicate)
        missing_ids = [item_id for item_id in current_ids if item_id not in locked_items]
        if not missing_ids:
            break
        locked_items.update(_lock_archive_items_for_update(missing_ids))

    locked_links = _lock_archive_item_author_links_for_update(locked_items.keys())
    author_ids_for_rebuild = {link.author_id for link in locked_links}
    author_ids_for_rebuild.add(keeper.pk)
    author_ids_for_rebuild.add(duplicate.pk)

    locked_authors = _lock_authors_for_update(author_ids=author_ids_for_rebuild)
    locked_keeper = locked_authors.get(keeper.pk)
    locked_duplicate = locked_authors.get(duplicate.pk)
    if locked_keeper is None or locked_duplicate is None:
        raise AuthorMergeError(AUTHOR_NOT_FOUND_ERROR)

    current_duplicate_ids = affected_archive_item_ids_for_author(locked_duplicate)
    if any(item_id not in locked_items for item_id in current_duplicate_ids):
        raise AuthorMergeError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)

    mutated_ids = list(current_duplicate_ids)
    planned_authors_by_item, rebuilt_author_names, moved, deduped = _plan_locked_merge(
        locked_items=locked_items,
        locked_links=locked_links,
        locked_authors=locked_authors,
        keeper_id=locked_keeper.pk,
        duplicate_id=locked_duplicate.pk,
        mutated_ids=mutated_ids,
    )

    try:
        for item_id in mutated_ids:
            locked_item = locked_items[item_id]
            _replace_author_links(locked_item, planned_authors_by_item[item_id])
            locked_item.author_name = rebuilt_author_names[item_id]
            locked_item.save(update_fields=["author_name", "updated_at"])
        locked_duplicate.delete()
    except IntegrityError as exc:
        raise AuthorMergeError(AUTHOR_MERGE_CONCURRENCY_ERROR) from exc

    synced_count = 0
    if mutated_ids:
        from documents.services.archive_search_index import (
            sync_archive_item_search_indexes,
        )

        synced = sync_archive_item_search_indexes(mutated_ids)
        synced_count = len(synced)

    return AuthorMergeResult(
        keeper_id=locked_keeper.pk,
        deleted_duplicate_id=duplicate.pk,
        affected_archive_item_ids=tuple(mutated_ids),
        links_moved=moved,
        links_deduped=deduped,
        search_indexes_refreshed=synced_count,
    )
