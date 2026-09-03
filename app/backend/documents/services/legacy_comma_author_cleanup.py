"""One-time reviewed cleanup of four legacy comma-containing Author rows.

This is not a general comma-splitting policy. Ids and names are constants
from the 2026-09-03 live audit. The command infers no additional cases.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Count

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
    ordered_author_links,
)

CLEANUP_MISMATCH_ERROR = (
    "legacy comma-author cleanup snapshot mismatch; aborting with no writes"
)
CLEANUP_PARTIAL_STATE_ERROR = (
    "legacy comma-author cleanup is in a partial or unexpected state; "
    "aborting with no writes"
)
CLEANUP_ORPHAN_STILL_LINKED_ERROR = (
    "aggregate Author still has ArchiveItemAuthor links; aborting cleanup"
)
CLEANUP_CONCURRENCY_ERROR = "הניקוי נכשל בגלל שינוי מקביל. נסו שוב."

STATUS_DRY_RUN = "dry_run"
STATUS_APPLIED = "applied"
STATUS_ALREADY_COMPLETE = "already_complete"

ITEM_311_ID = 311
ITEM_311_AUTHOR_NAME = "חגי אשד, אביעזר גולן"
AUTHOR_69_ID = 69
AUTHOR_69_NAME = ITEM_311_AUTHOR_NAME
AUTHOR_29_ID = 29
AUTHOR_29_NAME = "חגי אשד"
AUTHOR_68_ID = 68
AUTHOR_68_NAME = "אביעזר גולן"
ITEM_311_CURRENT_AUTHOR_IDS = (AUTHOR_69_ID,)
ITEM_311_DESIRED_AUTHOR_IDS = (AUTHOR_29_ID, AUTHOR_68_ID)

AUTHOR_4_ID = 4
AUTHOR_4_NAME = "פרופסור חיים דורון, פרופסור שפרה שורץ, פרופסור שלמה וינקר"
AUTHOR_6_ID = 6
AUTHOR_6_NAME = "פרופסור ששון נקר, פרופסור שלמה מוניקנדם"
AUTHOR_61_ID = 61
AUTHOR_61_NAME = "רחל שיפמן (לבית סעדיה), שרה מלמד (לבית סעדיה)"

ITEM_13_ID = 13
ITEM_13_AUTHOR_NAME = AUTHOR_4_NAME
ITEM_13_AUTHOR_IDS = (76, 77, 78)
ITEM_13_AUTHOR_NAMES = (
    "פרופסור חיים דורון",
    "פרופסור שפרה שורץ",
    "פרופסור שלמה וינקר",
)

ITEM_29_ID = 29
ITEM_29_AUTHOR_NAME = AUTHOR_6_NAME
ITEM_29_AUTHOR_IDS = (79, 80)
ITEM_29_AUTHOR_NAMES = (
    "פרופסור ששון נקר",
    "פרופסור שלמה מוניקנדם",
)

ITEM_289_ID = 289
ITEM_289_AUTHOR_NAME = AUTHOR_61_NAME
ITEM_289_AUTHOR_IDS = (81, 82)
ITEM_289_AUTHOR_NAMES = (
    "רחל שיפמן (לבית סעדיה)",
    "שרה מלמד (לבית סעדיה)",
)

REVIEWED_ITEM_IDS = (ITEM_13_ID, ITEM_29_ID, ITEM_289_ID, ITEM_311_ID)
ORPHAN_AGGREGATE_AUTHOR_IDS = (AUTHOR_4_ID, AUTHOR_6_ID, AUTHOR_61_ID)
DELETE_AUTHOR_IDS = (AUTHOR_4_ID, AUTHOR_6_ID, AUTHOR_61_ID, AUTHOR_69_ID)
LOCK_AUTHOR_IDS = (
    AUTHOR_4_ID,
    AUTHOR_6_ID,
    AUTHOR_29_ID,
    AUTHOR_61_ID,
    AUTHOR_68_ID,
    AUTHOR_69_ID,
    *ITEM_13_AUTHOR_IDS,
    *ITEM_29_AUTHOR_IDS,
    *ITEM_289_AUTHOR_IDS,
)


class LegacyCommaAuthorCleanupError(ArchiveItemAuthorError):
    """Fail-closed reviewed comma-author cleanup error. No partial writes."""


@dataclass(frozen=True)
class LegacyCommaAuthorCleanupResult:
    status: str
    verifications: tuple[str, ...]
    planned_item_311_author_ids: tuple[int, ...]
    planned_author_name: str
    authors_planned_unlinked_and_deleted: tuple[int, ...]
    deleted_author_ids: tuple[int, ...]
    search_indexes_refreshed: int


def _fail(message: str) -> None:
    raise LegacyCommaAuthorCleanupError(message)


def _ordered_ids(item: ArchiveItem) -> tuple[int, ...]:
    return tuple(link.author_id for link in ordered_author_links(item))


def _ordered_names(item: ArchiveItem) -> tuple[str, ...]:
    return tuple(link.author.name for link in ordered_author_links(item))


def _require_item(item_id: int) -> ArchiveItem:
    item = ArchiveItem.objects.filter(pk=item_id).first()
    if item is None:
        _fail(f"{CLEANUP_MISMATCH_ERROR}: ArchiveItem.id={item_id} is missing")
    return item


def _require_author(author_id: int, expected_name: str) -> Author:
    author = Author.objects.filter(pk=author_id).first()
    if author is None:
        _fail(f"{CLEANUP_MISMATCH_ERROR}: Author.id={author_id} is missing")
    if author.name != expected_name:
        _fail(
            f"{CLEANUP_MISMATCH_ERROR}: Author.id={author_id} name mismatch "
            f"(expected {expected_name!r})"
        )
    return author


def _require_item_authors(
    item: ArchiveItem,
    *,
    author_name: str,
    author_ids: tuple[int, ...],
    author_names: tuple[str, ...],
) -> list[str]:
    notes: list[str] = []
    if item.author_name != author_name:
        _fail(
            f"{CLEANUP_MISMATCH_ERROR}: ArchiveItem.id={item.pk} author_name mismatch"
        )
    notes.append(
        f"ArchiveItem.id={item.pk} author_name matches {author_name!r}"
    )
    ids = _ordered_ids(item)
    names = _ordered_names(item)
    if ids != author_ids or names != author_names:
        _fail(
            f"{CLEANUP_MISMATCH_ERROR}: ArchiveItem.id={item.pk} ordered Authors "
            f"are {list(ids)}, expected {list(author_ids)}"
        )
    notes.append(
        f"ArchiveItem.id={item.pk} ordered Author ids {list(author_ids)} "
        f"and names match"
    )
    return notes


def _author_link_item_ids(author_id: int) -> list[int]:
    return sorted(
        ArchiveItemAuthor.objects.filter(author_id=author_id)
        .values_list("archive_item_id", flat=True)
        .distinct()
    )


def _verify_already_complete() -> list[str] | None:
    """Return verification notes if the reviewed end state is already present."""
    if Author.objects.filter(pk__in=DELETE_AUTHOR_IDS).exists():
        return None
    item_311 = ArchiveItem.objects.filter(pk=ITEM_311_ID).first()
    if item_311 is None:
        return None
    if _ordered_ids(item_311) != ITEM_311_DESIRED_AUTHOR_IDS:
        return None
    notes: list[str] = []
    try:
        notes.extend(_verify_split_items())
        notes.extend(
            _require_item_authors(
                item_311,
                author_name=ITEM_311_AUTHOR_NAME,
                author_ids=ITEM_311_DESIRED_AUTHOR_IDS,
                author_names=(AUTHOR_29_NAME, AUTHOR_68_NAME),
            )
        )
        _require_author(AUTHOR_29_ID, AUTHOR_29_NAME)
        _require_author(AUTHOR_68_ID, AUTHOR_68_NAME)
    except LegacyCommaAuthorCleanupError:
        _fail(CLEANUP_PARTIAL_STATE_ERROR)
    for author_id in DELETE_AUTHOR_IDS:
        notes.append(f"Author.id={author_id} is already absent")
    notes.append("reviewed cleanup already complete; no writes")
    return notes


def _verify_split_items() -> list[str]:
    notes: list[str] = []
    item_13 = _require_item(ITEM_13_ID)
    notes.append(f"ArchiveItem.id={ITEM_13_ID} exists")
    notes.extend(
        _require_item_authors(
            item_13,
            author_name=ITEM_13_AUTHOR_NAME,
            author_ids=ITEM_13_AUTHOR_IDS,
            author_names=ITEM_13_AUTHOR_NAMES,
        )
    )
    for author_id, name in zip(ITEM_13_AUTHOR_IDS, ITEM_13_AUTHOR_NAMES, strict=True):
        _require_author(author_id, name)
        notes.append(f"Author.id={author_id} name matches {name!r}")

    item_29 = _require_item(ITEM_29_ID)
    notes.append(f"ArchiveItem.id={ITEM_29_ID} exists")
    notes.extend(
        _require_item_authors(
            item_29,
            author_name=ITEM_29_AUTHOR_NAME,
            author_ids=ITEM_29_AUTHOR_IDS,
            author_names=ITEM_29_AUTHOR_NAMES,
        )
    )
    for author_id, name in zip(ITEM_29_AUTHOR_IDS, ITEM_29_AUTHOR_NAMES, strict=True):
        _require_author(author_id, name)
        notes.append(f"Author.id={author_id} name matches {name!r}")

    item_289 = _require_item(ITEM_289_ID)
    notes.append(f"ArchiveItem.id={ITEM_289_ID} exists")
    notes.extend(
        _require_item_authors(
            item_289,
            author_name=ITEM_289_AUTHOR_NAME,
            author_ids=ITEM_289_AUTHOR_IDS,
            author_names=ITEM_289_AUTHOR_NAMES,
        )
    )
    for author_id, name in zip(ITEM_289_AUTHOR_IDS, ITEM_289_AUTHOR_NAMES, strict=True):
        _require_author(author_id, name)
        notes.append(f"Author.id={author_id} name matches {name!r}")
    return notes


def _verify_pre_cleanup_snapshot() -> tuple[list[str], str]:
    notes = _verify_split_items()

    item_311 = _require_item(ITEM_311_ID)
    notes.append(f"ArchiveItem.id={ITEM_311_ID} exists")
    notes.extend(
        _require_item_authors(
            item_311,
            author_name=ITEM_311_AUTHOR_NAME,
            author_ids=ITEM_311_CURRENT_AUTHOR_IDS,
            author_names=(AUTHOR_69_NAME,),
        )
    )
    _require_author(AUTHOR_69_ID, AUTHOR_69_NAME)
    notes.append(f"Author.id={AUTHOR_69_ID} name matches {AUTHOR_69_NAME!r}")
    if _author_link_item_ids(AUTHOR_69_ID) != [ITEM_311_ID]:
        _fail(
            f"{CLEANUP_MISMATCH_ERROR}: Author.id={AUTHOR_69_ID} links are not "
            f"exactly ArchiveItem.id={ITEM_311_ID}"
        )
    notes.append(f"Author.id={AUTHOR_69_ID} is linked only to ArchiveItem.id={ITEM_311_ID}")

    _require_author(AUTHOR_29_ID, AUTHOR_29_NAME)
    notes.append(f"Author.id={AUTHOR_29_ID} name matches {AUTHOR_29_NAME!r}")
    _require_author(AUTHOR_68_ID, AUTHOR_68_NAME)
    notes.append(f"Author.id={AUTHOR_68_ID} name matches {AUTHOR_68_NAME!r}")

    for author_id, expected_name in (
        (AUTHOR_4_ID, AUTHOR_4_NAME),
        (AUTHOR_6_ID, AUTHOR_6_NAME),
        (AUTHOR_61_ID, AUTHOR_61_NAME),
    ):
        _require_author(author_id, expected_name)
        notes.append(f"Author.id={author_id} name matches {expected_name!r}")
        linked = _author_link_item_ids(author_id)
        if linked:
            _fail(
                f"{CLEANUP_ORPHAN_STILL_LINKED_ERROR}: Author.id={author_id} "
                f"linked to {linked}"
            )
        notes.append(f"Author.id={author_id} has zero ArchiveItemAuthor links")

    planned_authors = [
        _require_author(AUTHOR_29_ID, AUTHOR_29_NAME),
        _require_author(AUTHOR_68_ID, AUTHOR_68_NAME),
    ]
    planned_name = _joined_author_name([author.name for author in planned_authors])
    if len(planned_name) > AUTHOR_NAME_MAX_LENGTH:
        _fail(AUTHOR_JOINED_TOO_LONG_ERROR)
    notes.append(
        f"planned ArchiveItem.id={ITEM_311_ID} Author ids "
        f"{list(ITEM_311_DESIRED_AUTHOR_IDS)}"
    )
    notes.append(f"planned rebuilt author_name {planned_name!r}")
    notes.append(
        "Authors that would become/remain unlinked and be deleted: "
        f"{list(DELETE_AUTHOR_IDS)}"
    )
    return notes, planned_name


def _already_complete_result(notes: list[str]) -> LegacyCommaAuthorCleanupResult:
    planned_name = _joined_author_name([AUTHOR_29_NAME, AUTHOR_68_NAME])
    return LegacyCommaAuthorCleanupResult(
        status=STATUS_ALREADY_COMPLETE,
        verifications=tuple(notes),
        planned_item_311_author_ids=ITEM_311_DESIRED_AUTHOR_IDS,
        planned_author_name=planned_name,
        authors_planned_unlinked_and_deleted=(),
        deleted_author_ids=(),
        search_indexes_refreshed=0,
    )


def cleanup_legacy_comma_authors(*, apply: bool = False) -> LegacyCommaAuthorCleanupResult:
    """Dry-run by default. ``apply=True`` mutates inside one transaction."""
    complete_notes = _verify_already_complete()
    if complete_notes is not None:
        return _already_complete_result(complete_notes)

    item_311 = ArchiveItem.objects.filter(pk=ITEM_311_ID).first()
    if item_311 is not None and _ordered_ids(item_311) == ITEM_311_DESIRED_AUTHOR_IDS:
        _fail(CLEANUP_PARTIAL_STATE_ERROR)

    notes, planned_name = _verify_pre_cleanup_snapshot()
    if not apply:
        return LegacyCommaAuthorCleanupResult(
            status=STATUS_DRY_RUN,
            verifications=tuple(notes),
            planned_item_311_author_ids=ITEM_311_DESIRED_AUTHOR_IDS,
            planned_author_name=planned_name,
            authors_planned_unlinked_and_deleted=DELETE_AUTHOR_IDS,
            deleted_author_ids=(),
            search_indexes_refreshed=0,
        )
    return _apply_cleanup(notes=notes, planned_name=planned_name)


def _apply_cleanup(
    *,
    notes: list[str],
    planned_name: str,
) -> LegacyCommaAuthorCleanupResult:
    try:
        with transaction.atomic():
            locked_items = _lock_archive_items_for_update(REVIEWED_ITEM_IDS)
            if set(locked_items) != set(REVIEWED_ITEM_IDS):
                _fail(f"{CLEANUP_MISMATCH_ERROR}: reviewed ArchiveItem rows changed")

            locked_links = _lock_archive_item_author_links_for_update(REVIEWED_ITEM_IDS)
            locked_authors = _lock_authors_for_update(author_ids=LOCK_AUTHOR_IDS)
            if set(locked_authors) != set(LOCK_AUTHOR_IDS):
                _fail(AUTHOR_NOT_FOUND_ERROR)

            if any(
                item_id not in locked_items
                for item_id in _author_link_item_ids(AUTHOR_69_ID)
            ):
                raise LegacyCommaAuthorCleanupError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)

            _verify_locked_snapshot(locked_items, locked_links, locked_authors)

            author_29 = locked_authors[AUTHOR_29_ID]
            author_68 = locked_authors[AUTHOR_68_ID]
            locked_311 = locked_items[ITEM_311_ID]
            _replace_author_links(locked_311, [author_29, author_68])
            rebuilt_links = ordered_author_links(locked_311)
            joined = _joined_author_name([link.author.name for link in rebuilt_links])
            if len(joined) > AUTHOR_NAME_MAX_LENGTH:
                _fail(AUTHOR_JOINED_TOO_LONG_ERROR)
            if (
                tuple(link.author_id for link in rebuilt_links) != ITEM_311_DESIRED_AUTHOR_IDS
                or [link.position for link in rebuilt_links] != [0, 1]
            ):
                _fail(f"{CLEANUP_MISMATCH_ERROR}: ArchiveItem.id={ITEM_311_ID} order after replace")
            if joined != planned_name:
                _fail(
                    f"{CLEANUP_MISMATCH_ERROR}: rebuilt author_name changed under lock"
                )
            locked_311.author_name = joined
            locked_311.save(update_fields=["author_name", "updated_at"])

            remaining = {
                row["author_id"]: row["n"]
                for row in (
                    ArchiveItemAuthor.objects.filter(author_id__in=DELETE_AUTHOR_IDS)
                    .values("author_id")
                    .annotate(n=Count("id"))
                )
            }
            still_linked = [
                author_id
                for author_id in DELETE_AUTHOR_IDS
                if remaining.get(author_id, 0) != 0
            ]
            if still_linked:
                _fail(
                    f"{CLEANUP_ORPHAN_STILL_LINKED_ERROR}: {still_linked}"
                )

            for author_id in DELETE_AUTHOR_IDS:
                locked_authors[author_id].delete()

            from documents.services.archive_search_index import (
                sync_archive_item_search_indexes,
            )

            synced = sync_archive_item_search_indexes([ITEM_311_ID])
    except IntegrityError as exc:
        raise LegacyCommaAuthorCleanupError(CLEANUP_CONCURRENCY_ERROR) from exc

    return LegacyCommaAuthorCleanupResult(
        status=STATUS_APPLIED,
        verifications=tuple(notes),
        planned_item_311_author_ids=ITEM_311_DESIRED_AUTHOR_IDS,
        planned_author_name=planned_name,
        authors_planned_unlinked_and_deleted=DELETE_AUTHOR_IDS,
        deleted_author_ids=DELETE_AUTHOR_IDS,
        search_indexes_refreshed=len(synced),
    )


def _verify_locked_snapshot(
    locked_items: dict[int, ArchiveItem],
    locked_links: list[ArchiveItemAuthor],
    locked_authors: dict[int, Author],
) -> None:
    links_by_item: dict[int, list[ArchiveItemAuthor]] = {
        item_id: [] for item_id in REVIEWED_ITEM_IDS
    }
    for link in locked_links:
        links_by_item.setdefault(link.archive_item_id, []).append(link)

    expected_items = {
        ITEM_13_ID: (ITEM_13_AUTHOR_NAME, ITEM_13_AUTHOR_IDS, ITEM_13_AUTHOR_NAMES),
        ITEM_29_ID: (ITEM_29_AUTHOR_NAME, ITEM_29_AUTHOR_IDS, ITEM_29_AUTHOR_NAMES),
        ITEM_289_ID: (ITEM_289_AUTHOR_NAME, ITEM_289_AUTHOR_IDS, ITEM_289_AUTHOR_NAMES),
        ITEM_311_ID: (
            ITEM_311_AUTHOR_NAME,
            ITEM_311_CURRENT_AUTHOR_IDS,
            (AUTHOR_69_NAME,),
        ),
    }
    for item_id, (author_name, author_ids, author_names) in expected_items.items():
        item = locked_items[item_id]
        if item.author_name != author_name:
            _fail(
                f"{CLEANUP_MISMATCH_ERROR}: ArchiveItem.id={item_id} author_name "
                "changed under lock"
            )
        item_links = links_by_item.get(item_id, [])
        ids = tuple(link.author_id for link in item_links)
        names = tuple(locked_authors[link.author_id].name for link in item_links)
        if ids != author_ids or names != author_names:
            raise LegacyCommaAuthorCleanupError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)

    expected_author_names = {
        AUTHOR_4_ID: AUTHOR_4_NAME,
        AUTHOR_6_ID: AUTHOR_6_NAME,
        AUTHOR_29_ID: AUTHOR_29_NAME,
        AUTHOR_61_ID: AUTHOR_61_NAME,
        AUTHOR_68_ID: AUTHOR_68_NAME,
        AUTHOR_69_ID: AUTHOR_69_NAME,
        **dict(zip(ITEM_13_AUTHOR_IDS, ITEM_13_AUTHOR_NAMES, strict=True)),
        **dict(zip(ITEM_29_AUTHOR_IDS, ITEM_29_AUTHOR_NAMES, strict=True)),
        **dict(zip(ITEM_289_AUTHOR_IDS, ITEM_289_AUTHOR_NAMES, strict=True)),
    }
    for author_id, expected_name in expected_author_names.items():
        if locked_authors[author_id].name != expected_name:
            _fail(
                f"{CLEANUP_MISMATCH_ERROR}: Author.id={author_id} name changed "
                "under lock"
            )

    for author_id in ORPHAN_AGGREGATE_AUTHOR_IDS:
        if _author_link_item_ids(author_id):
            _fail(
                f"{CLEANUP_ORPHAN_STILL_LINKED_ERROR}: Author.id={author_id}"
            )
    if _author_link_item_ids(AUTHOR_69_ID) != [ITEM_311_ID]:
        raise LegacyCommaAuthorCleanupError(AUTHOR_LINKS_CHANGED_RETRY_ERROR)
