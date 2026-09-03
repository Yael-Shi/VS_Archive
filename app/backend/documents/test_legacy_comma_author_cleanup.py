"""One-time reviewed legacy comma-author cleanup."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Author,
    Person,
)
from documents.services.archive_item_authors import (
    AUTHOR_LINKS_CHANGED_RETRY_ERROR,
    ordered_author_links,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.services.legacy_comma_author_cleanup import (
    AUTHOR_4_ID,
    AUTHOR_4_NAME,
    AUTHOR_6_ID,
    AUTHOR_6_NAME,
    AUTHOR_29_ID,
    AUTHOR_29_NAME,
    AUTHOR_61_ID,
    AUTHOR_61_NAME,
    AUTHOR_68_ID,
    AUTHOR_68_NAME,
    AUTHOR_69_ID,
    AUTHOR_69_NAME,
    CLEANUP_MISMATCH_ERROR,
    CLEANUP_ORPHAN_STILL_LINKED_ERROR,
    CLEANUP_PARTIAL_STATE_ERROR,
    DELETE_AUTHOR_IDS,
    ITEM_13_AUTHOR_IDS,
    ITEM_13_AUTHOR_NAME,
    ITEM_13_AUTHOR_NAMES,
    ITEM_13_ID,
    ITEM_29_AUTHOR_IDS,
    ITEM_29_AUTHOR_NAME,
    ITEM_29_AUTHOR_NAMES,
    ITEM_29_ID,
    ITEM_289_AUTHOR_IDS,
    ITEM_289_AUTHOR_NAME,
    ITEM_289_AUTHOR_NAMES,
    ITEM_289_ID,
    ITEM_311_AUTHOR_NAME,
    ITEM_311_DESIRED_AUTHOR_IDS,
    ITEM_311_ID,
    LOCK_AUTHOR_IDS,
    REVIEWED_ITEM_IDS,
    STATUS_ALREADY_COMPLETE,
    STATUS_APPLIED,
    STATUS_DRY_RUN,
    LegacyCommaAuthorCleanupError,
    cleanup_legacy_comma_authors,
)
from documents.tag_pk_sequence_support import reset_pk_sequence

COMMAND_NAME = "cleanup_legacy_comma_authors"
UNRELATED_AUTHOR_ID = 9001
UNRELATED_ITEM_ID = 9001
ITEM_121_ID = 121
ITEM_310_ID = 310


def _item(
    *,
    pk: int,
    title: str,
    item_type: str,
    author_name: str,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        pk=pk,
        item_type=item_type,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
    )


def _author(*, pk: int, name: str) -> Author:
    return Author.objects.create(pk=pk, name=name)


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


def _order(item_id: int) -> list[int]:
    item = ArchiveItem.objects.get(pk=item_id)
    return [link.author_id for link in ordered_author_links(item)]


def _positions(item_id: int) -> list[int]:
    item = ArchiveItem.objects.get(pk=item_id)
    return [link.position for link in ordered_author_links(item)]


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


def _metadata(archive_item_id: int) -> str:
    return ArchiveItemSearchIndex.objects.get(
        archive_item_id=archive_item_id
    ).metadata_text


def _seed_reviewed_snapshot() -> dict[str, object]:
    authors = {
        AUTHOR_4_ID: _author(pk=AUTHOR_4_ID, name=AUTHOR_4_NAME),
        AUTHOR_6_ID: _author(pk=AUTHOR_6_ID, name=AUTHOR_6_NAME),
        AUTHOR_29_ID: _author(pk=AUTHOR_29_ID, name=AUTHOR_29_NAME),
        AUTHOR_61_ID: _author(pk=AUTHOR_61_ID, name=AUTHOR_61_NAME),
        AUTHOR_68_ID: _author(pk=AUTHOR_68_ID, name=AUTHOR_68_NAME),
        AUTHOR_69_ID: _author(pk=AUTHOR_69_ID, name=AUTHOR_69_NAME),
        **{
            pk: _author(pk=pk, name=name)
            for pk, name in zip(ITEM_13_AUTHOR_IDS, ITEM_13_AUTHOR_NAMES, strict=True)
        },
        **{
            pk: _author(pk=pk, name=name)
            for pk, name in zip(ITEM_29_AUTHOR_IDS, ITEM_29_AUTHOR_NAMES, strict=True)
        },
        **{
            pk: _author(pk=pk, name=name)
            for pk, name in zip(ITEM_289_AUTHOR_IDS, ITEM_289_AUTHOR_NAMES, strict=True)
        },
        UNRELATED_AUTHOR_ID: _author(pk=UNRELATED_AUTHOR_ID, name="UnrelatedAuthorToken"),
    }
    item_13 = _item(
        pk=ITEM_13_ID,
        title="item-13",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        author_name=ITEM_13_AUTHOR_NAME,
    )
    for position, author_id in enumerate(ITEM_13_AUTHOR_IDS):
        _link(item_13, authors[author_id], position=position)

    item_29 = _item(
        pk=ITEM_29_ID,
        title="item-29",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        author_name=ITEM_29_AUTHOR_NAME,
    )
    for position, author_id in enumerate(ITEM_29_AUTHOR_IDS):
        _link(item_29, authors[author_id], position=position)

    item_289 = _item(
        pk=ITEM_289_ID,
        title="item-289",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        author_name=ITEM_289_AUTHOR_NAME,
    )
    for position, author_id in enumerate(ITEM_289_AUTHOR_IDS):
        _link(item_289, authors[author_id], position=position)

    item_311 = _item(
        pk=ITEM_311_ID,
        title="item-311",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        author_name=ITEM_311_AUTHOR_NAME,
    )
    _link(item_311, authors[AUTHOR_69_ID], position=0)

    item_121 = _item(
        pk=ITEM_121_ID,
        title="item-121",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        author_name=AUTHOR_29_NAME,
    )
    _link(item_121, authors[AUTHOR_29_ID], position=0)

    item_310 = _item(
        pk=ITEM_310_ID,
        title="item-310",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        author_name=AUTHOR_68_NAME,
    )
    _link(item_310, authors[AUTHOR_68_ID], position=0)

    unrelated = _item(
        pk=UNRELATED_ITEM_ID,
        title="unrelated-item",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        author_name="UnrelatedAuthorToken",
    )
    _link(unrelated, authors[UNRELATED_AUTHOR_ID], position=0)

    person = Person.objects.create(name="UnrelatedPersonToken")
    ArchiveItemPerson.objects.create(archive_item=item_311, person=person)

    reset_pk_sequence(Author)
    reset_pk_sequence(ArchiveItem)
    for item in (item_13, item_29, item_289, item_311, item_121, item_310, unrelated):
        _rebuild(item.pk)
    return {
        "authors": authors,
        "person": person,
        "item_311": item_311,
        "unrelated": unrelated,
    }


class LegacyCommaAuthorCleanupTests(TestCase):
    def setUp(self):
        self.seed = _seed_reviewed_snapshot()

    def test_dry_run_writes_nothing(self):
        before_authors = set(Author.objects.values_list("pk", "name"))
        before_links = set(
            ArchiveItemAuthor.objects.values_list(
                "archive_item_id", "author_id", "position"
            )
        )
        before_names = dict(
            ArchiveItem.objects.filter(
                pk__in=[
                    ITEM_13_ID,
                    ITEM_29_ID,
                    ITEM_289_ID,
                    ITEM_311_ID,
                    UNRELATED_ITEM_ID,
                ]
            ).values_list("pk", "author_name")
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            result = cleanup_legacy_comma_authors(apply=False)

        self.assertEqual(result.status, STATUS_DRY_RUN)
        self.assertEqual(result.planned_item_311_author_ids, ITEM_311_DESIRED_AUTHOR_IDS)
        self.assertEqual(result.planned_author_name, ITEM_311_AUTHOR_NAME)
        self.assertEqual(result.authors_planned_unlinked_and_deleted, DELETE_AUTHOR_IDS)
        self.assertEqual(result.deleted_author_ids, ())
        self.assertEqual(result.search_indexes_refreshed, 0)
        mocked_sync.assert_not_called()
        self.assertEqual(set(Author.objects.values_list("pk", "name")), before_authors)
        self.assertEqual(
            set(
                ArchiveItemAuthor.objects.values_list(
                    "archive_item_id", "author_id", "position"
                )
            ),
            before_links,
        )
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_69_ID])
        self.assertEqual(
            dict(
                ArchiveItem.objects.filter(pk__in=before_names).values_list(
                    "pk", "author_name"
                )
            ),
            before_names,
        )

    def test_apply_splits_item_311_rebuilds_author_name_and_deletes_aggregates(self):
        result = cleanup_legacy_comma_authors(apply=True)

        self.assertEqual(result.status, STATUS_APPLIED)
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_29_ID, AUTHOR_68_ID])
        self.assertEqual(_positions(ITEM_311_ID), [0, 1])
        item_311 = ArchiveItem.objects.get(pk=ITEM_311_ID)
        self.assertEqual(item_311.author_name, ITEM_311_AUTHOR_NAME)
        self.assertEqual(result.deleted_author_ids, DELETE_AUTHOR_IDS)
        self.assertEqual(result.search_indexes_refreshed, 1)
        for author_id in DELETE_AUTHOR_IDS:
            self.assertFalse(Author.objects.filter(pk=author_id).exists())
        self.assertIn(AUTHOR_29_NAME, _metadata(ITEM_311_ID))
        self.assertIn(AUTHOR_68_NAME, _metadata(ITEM_311_ID))

    def test_apply_refreshes_only_item_311_search_index(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            mocked_sync.return_value = [object()]
            result = cleanup_legacy_comma_authors(apply=True)

        mocked_sync.assert_called_once_with([ITEM_311_ID])
        self.assertEqual(result.search_indexes_refreshed, 1)

    def test_unrelated_authors_items_and_person_are_untouched(self):
        person = self.seed["person"]
        cleanup_legacy_comma_authors(apply=True)

        self.assertEqual(_order(ITEM_13_ID), list(ITEM_13_AUTHOR_IDS))
        self.assertEqual(_order(ITEM_29_ID), list(ITEM_29_AUTHOR_IDS))
        self.assertEqual(_order(ITEM_289_ID), list(ITEM_289_AUTHOR_IDS))
        self.assertEqual(_order(ITEM_121_ID), [AUTHOR_29_ID])
        self.assertEqual(_order(ITEM_310_ID), [AUTHOR_68_ID])
        self.assertEqual(_order(UNRELATED_ITEM_ID), [UNRELATED_AUTHOR_ID])
        self.assertTrue(Author.objects.filter(pk=UNRELATED_AUTHOR_ID).exists())
        self.assertTrue(Author.objects.filter(pk=AUTHOR_29_ID).exists())
        self.assertTrue(Author.objects.filter(pk=AUTHOR_68_ID).exists())
        person.refresh_from_db()
        self.assertEqual(person.name, "UnrelatedPersonToken")
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item_id=ITEM_311_ID, person=person
            ).exists()
        )
        self.assertEqual(Person.objects.count(), 1)

    def test_command_dry_run_default_and_apply_flag(self):
        dry = StringIO()
        call_command(COMMAND_NAME, stdout=dry)
        self.assertIn("mode: dry_run", dry.getvalue())
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_69_ID])
        self.assertTrue(Author.objects.filter(pk=AUTHOR_69_ID).exists())

        applied = StringIO()
        call_command(COMMAND_NAME, "--apply", stdout=applied)
        self.assertIn("mode: applied", applied.getvalue())
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_29_ID, AUTHOR_68_ID])

    def test_repeat_after_success_is_already_complete_with_no_writes(self):
        cleanup_legacy_comma_authors(apply=True)
        author_ids = set(Author.objects.values_list("pk", flat=True))
        links = set(
            ArchiveItemAuthor.objects.values_list(
                "archive_item_id", "author_id", "position"
            )
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            again = cleanup_legacy_comma_authors(apply=True)

        self.assertEqual(again.status, STATUS_ALREADY_COMPLETE)
        self.assertEqual(again.deleted_author_ids, ())
        self.assertEqual(again.search_indexes_refreshed, 0)
        mocked_sync.assert_not_called()
        self.assertEqual(set(Author.objects.values_list("pk", flat=True)), author_ids)
        self.assertEqual(
            set(
                ArchiveItemAuthor.objects.values_list(
                    "archive_item_id", "author_id", "position"
                )
            ),
            links,
        )

        dry = cleanup_legacy_comma_authors(apply=False)
        self.assertEqual(dry.status, STATUS_ALREADY_COMPLETE)

    def test_search_index_failure_rolls_back_everything(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes",
            side_effect=RuntimeError("index failed"),
        ):
            with self.assertRaises(RuntimeError):
                cleanup_legacy_comma_authors(apply=True)

        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_69_ID])
        self.assertEqual(
            ArchiveItem.objects.get(pk=ITEM_311_ID).author_name, ITEM_311_AUTHOR_NAME
        )
        for author_id in DELETE_AUTHOR_IDS:
            self.assertTrue(Author.objects.filter(pk=author_id).exists())

    def test_nonzero_orphan_link_blocks_the_whole_cleanup(self):
        extra = _item(
            pk=9100,
            title="orphan-link",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            author_name=AUTHOR_4_NAME,
        )
        _link(extra, Author.objects.get(pk=AUTHOR_4_ID), position=0)

        with self.assertRaises(LegacyCommaAuthorCleanupError) as ctx:
            cleanup_legacy_comma_authors(apply=True)

        self.assertIn(CLEANUP_ORPHAN_STILL_LINKED_ERROR, ctx.exception.message)
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_69_ID])
        self.assertTrue(Author.objects.filter(pk=AUTHOR_69_ID).exists())
        self.assertTrue(Author.objects.filter(pk=AUTHOR_4_ID).exists())


class LegacyCommaAuthorCleanupMismatchTests(TestCase):
    def setUp(self):
        _seed_reviewed_snapshot()

    def _assert_no_apply(self, mutate, *, fragment: str) -> None:
        mutate()
        reviewed_links = set(
            ArchiveItemAuthor.objects.filter(archive_item_id__in=REVIEWED_ITEM_IDS)
            .values_list("archive_item_id", "author_id", "position")
        )
        reviewed_author_names = dict(
            ArchiveItem.objects.filter(pk__in=REVIEWED_ITEM_IDS).values_list(
                "pk", "author_name"
            )
        )
        author_rows = set(
            Author.objects.filter(pk__in=LOCK_AUTHOR_IDS).values_list("pk", "name")
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            with self.assertRaises(LegacyCommaAuthorCleanupError) as ctx:
                cleanup_legacy_comma_authors(apply=True)
        self.assertIn(fragment, ctx.exception.message)
        mocked_sync.assert_not_called()
        self.assertEqual(
            set(
                ArchiveItemAuthor.objects.filter(
                    archive_item_id__in=REVIEWED_ITEM_IDS
                ).values_list("archive_item_id", "author_id", "position")
            ),
            reviewed_links,
        )
        self.assertEqual(
            dict(
                ArchiveItem.objects.filter(pk__in=REVIEWED_ITEM_IDS).values_list(
                    "pk", "author_name"
                )
            ),
            reviewed_author_names,
        )
        self.assertEqual(
            set(Author.objects.filter(pk__in=LOCK_AUTHOR_IDS).values_list("pk", "name")),
            author_rows,
        )

    def test_item_311_author_name_mismatch_fails_closed(self):
        self._assert_no_apply(
            lambda: ArchiveItem.objects.filter(pk=ITEM_311_ID).update(
                author_name="wrong"
            ),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_item_311_relation_mismatch_fails_closed(self):
        def mutate():
            link = ArchiveItemAuthor.objects.get(archive_item_id=ITEM_311_ID)
            link.author_id = AUTHOR_29_ID
            link.save(update_fields=["author_id"])

        self._assert_no_apply(mutate, fragment=CLEANUP_MISMATCH_ERROR)

    def test_author_29_name_mismatch_fails_closed(self):
        self._assert_no_apply(
            lambda: Author.objects.filter(pk=AUTHOR_29_ID).update(name="wrong-29"),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_author_68_name_mismatch_fails_closed(self):
        self._assert_no_apply(
            lambda: Author.objects.filter(pk=AUTHOR_68_ID).update(name="wrong-68"),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_author_69_name_mismatch_fails_closed(self):
        self._assert_no_apply(
            lambda: Author.objects.filter(pk=AUTHOR_69_ID).update(name="wrong-69"),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_orphan_aggregate_name_mismatch_fails_closed(self):
        self._assert_no_apply(
            lambda: Author.objects.filter(pk=AUTHOR_6_ID).update(name="wrong-6"),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_item_289_order_mismatch_fails_closed(self):
        def mutate():
            ArchiveItemAuthor.objects.filter(archive_item_id=ITEM_289_ID).delete()
            item = ArchiveItem.objects.get(pk=ITEM_289_ID)
            _link(item, Author.objects.get(pk=ITEM_289_AUTHOR_IDS[1]), position=0)
            _link(item, Author.objects.get(pk=ITEM_289_AUTHOR_IDS[0]), position=1)

        self._assert_no_apply(mutate, fragment=CLEANUP_MISMATCH_ERROR)

    def test_missing_item_311_fails_closed(self):
        self._assert_no_apply(
            lambda: ArchiveItem.objects.filter(pk=ITEM_311_ID).delete(),
            fragment=CLEANUP_MISMATCH_ERROR,
        )

    def test_author_69_extra_link_fails_closed(self):
        def mutate():
            extra = _item(
                pk=9200,
                title="extra-69",
                item_type=ArchiveItem.ItemType.MANUAL_TEXT,
                author_name=AUTHOR_69_NAME,
            )
            _link(extra, Author.objects.get(pk=AUTHOR_69_ID), position=0)

        self._assert_no_apply(mutate, fragment=CLEANUP_MISMATCH_ERROR)

    def test_partial_split_with_aggregates_remaining_fails_closed(self):
        def mutate():
            item = ArchiveItem.objects.get(pk=ITEM_311_ID)
            ArchiveItemAuthor.objects.filter(archive_item=item).delete()
            _link(item, Author.objects.get(pk=AUTHOR_29_ID), position=0)
            _link(item, Author.objects.get(pk=AUTHOR_68_ID), position=1)

        mutate()
        with self.assertRaises(LegacyCommaAuthorCleanupError) as ctx:
            cleanup_legacy_comma_authors(apply=True)
        self.assertEqual(ctx.exception.message, CLEANUP_PARTIAL_STATE_ERROR)
        self.assertTrue(Author.objects.filter(pk=AUTHOR_4_ID).exists())
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_29_ID, AUTHOR_68_ID])

    def test_command_mismatch_is_command_error(self):
        ArchiveItem.objects.filter(pk=ITEM_311_ID).update(author_name="wrong")
        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply", stdout=StringIO())


class LegacyCommaAuthorCleanupLockRetryTests(TestCase):
    def setUp(self):
        _seed_reviewed_snapshot()

    def test_new_link_on_author_69_after_item_locks_fails_closed(self):
        from documents.services.legacy_comma_author_cleanup import (
            _lock_archive_items_for_update as real_lock_items,
        )

        def lock_then_attach(item_ids):
            locked = real_lock_items(item_ids)
            if not ArchiveItem.objects.filter(pk=9300).exists():
                extra = ArchiveItem(
                    pk=9300,
                    item_type=ArchiveItem.ItemType.MANUAL_TEXT,
                    title="late-69",
                    visibility=ArchiveItem.Visibility.PUBLIC,
                    author_name=AUTHOR_69_NAME,
                )
                extra.save()
                ArchiveItemAuthor.objects.create(
                    archive_item=extra,
                    author_id=AUTHOR_69_ID,
                    position=0,
                )
            return locked

        with patch(
            "documents.services.legacy_comma_author_cleanup._lock_archive_items_for_update",
            side_effect=lock_then_attach,
        ):
            with self.assertRaises(LegacyCommaAuthorCleanupError) as ctx:
                cleanup_legacy_comma_authors(apply=True)
        self.assertEqual(ctx.exception.message, AUTHOR_LINKS_CHANGED_RETRY_ERROR)
        self.assertEqual(_order(ITEM_311_ID), [AUTHOR_69_ID])
        self.assertTrue(Author.objects.filter(pk=AUTHOR_69_ID).exists())
