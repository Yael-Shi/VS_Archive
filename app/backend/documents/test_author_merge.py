"""Controlled staff Author merge (explicit keeper/duplicate ids)."""

from __future__ import annotations

from unittest.mock import patch

from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Author,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_authors import (
    AUTHOR_JOINED_TOO_LONG_ERROR,
    AUTHOR_LINKS_CHANGED_RETRY_ERROR,
    AUTHOR_NOT_FOUND_ERROR,
    ordered_author_links,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.author_merge import (
    AUTHOR_MERGE_CONCURRENCY_ERROR,
    AUTHOR_MERGE_ID_INVALID_ERROR,
    AUTHOR_MERGE_ID_REQUIRED_ERROR,
    AUTHOR_MERGE_SAME_ID_ERROR,
    AuthorMergeError,
    merge_author,
    parse_author_merge_id,
    preview_author_merge,
)


def _item(*, title: str, author_name: str = "") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
    )


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


def _order(item: ArchiveItem) -> list[int]:
    return [link.author_id for link in ordered_author_links(item)]


def _positions(item: ArchiveItem) -> list[int]:
    return [link.position for link in ordered_author_links(item)]


def _author_name(item: ArchiveItem) -> str:
    item.refresh_from_db()
    return item.author_name


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


class AuthorMergeParseTests(TestCase):
    def test_parse_rejects_empty_invalid_and_non_positive_ids(self):
        with self.assertRaises(AuthorMergeError) as ctx:
            parse_author_merge_id("")
        self.assertEqual(ctx.exception.message, AUTHOR_MERGE_ID_REQUIRED_ERROR)
        with self.assertRaises(AuthorMergeError) as ctx:
            parse_author_merge_id("abc")
        self.assertEqual(ctx.exception.message, AUTHOR_MERGE_ID_INVALID_ERROR)
        with self.assertRaises(AuthorMergeError) as ctx:
            parse_author_merge_id("0")
        self.assertEqual(ctx.exception.message, AUTHOR_MERGE_ID_INVALID_ERROR)

    def test_parse_accepts_positive_integer_and_rejects_names(self):
        self.assertEqual(parse_author_merge_id("12"), 12)
        with self.assertRaises(AuthorMergeError):
            parse_author_merge_id("Ada Lovelace")


class AuthorMergeServiceTests(TestCase):
    def setUp(self):
        self.keeper = Author.objects.create(name="KeeperAuthor")
        self.duplicate = Author.objects.create(name="DuplicateAuthor")
        self.bob = Author.objects.create(name="Bob")

    def test_same_author_is_rejected(self):
        with self.assertRaises(AuthorMergeError) as ctx:
            merge_author(keeper=self.keeper, duplicate=self.keeper)
        self.assertEqual(ctx.exception.message, AUTHOR_MERGE_SAME_ID_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.keeper.pk).exists())

    def test_missing_duplicate_is_rejected_safely(self):
        missing = Author(pk=999999, name="Missing")
        with self.assertRaises(AuthorMergeError) as ctx:
            merge_author(keeper=self.keeper, duplicate=missing)
        self.assertEqual(ctx.exception.message, AUTHOR_NOT_FOUND_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.keeper.pk).exists())

    def test_duplicate_only_link_repoints_to_keeper(self):
        item = _item(title="Dup only", author_name="DuplicateAuthor")
        _link(item, self.duplicate, position=0)

        result = merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.keeper.pk])
        self.assertEqual(_positions(item), [0])
        self.assertEqual(_author_name(item), "KeeperAuthor")
        self.assertFalse(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(result.affected_archive_item_ids, (item.pk,))
        self.assertEqual(result.links_moved, 1)
        self.assertEqual(result.links_deduped, 0)

    def test_keeper_only_item_is_untouched_and_not_in_fanout(self):
        keeper_item = _item(title="Keeper only", author_name="KeeperAuthor")
        _link(keeper_item, self.keeper, position=0)
        dup_item = _item(title="Dup only", author_name="DuplicateAuthor")
        _link(dup_item, self.duplicate, position=0)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            mocked_sync.return_value = []
            result = merge_author(keeper=self.keeper, duplicate=self.duplicate)

        mocked_sync.assert_called_once_with([dup_item.pk])
        self.assertEqual(result.affected_archive_item_ids, (dup_item.pk,))
        self.assertEqual(_order(keeper_item), [self.keeper.pk])
        self.assertEqual(_author_name(keeper_item), "KeeperAuthor")
        self.assertEqual(_order(dup_item), [self.keeper.pk])

    def test_both_linked_drops_duplicate_and_keeps_keeper_slot_after_bob(self):
        item = _item(
            title="Both after bob",
            author_name="DuplicateAuthor, Bob, KeeperAuthor",
        )
        _link(item, self.duplicate, position=0)
        _link(item, self.bob, position=1)
        _link(item, self.keeper, position=2)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.bob.pk, self.keeper.pk])
        self.assertEqual(_positions(item), [0, 1])
        self.assertEqual(_author_name(item), "Bob, KeeperAuthor")

    def test_both_linked_drops_duplicate_after_keeper_and_bob(self):
        item = _item(
            title="Both before bob",
            author_name="KeeperAuthor, Bob, DuplicateAuthor",
        )
        _link(item, self.keeper, position=0)
        _link(item, self.bob, position=1)
        _link(item, self.duplicate, position=2)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.keeper.pk, self.bob.pk])
        self.assertEqual(_positions(item), [0, 1])
        self.assertEqual(_author_name(item), "KeeperAuthor, Bob")

    def test_duplicate_in_middle_of_multi_author_order(self):
        item = _item(title="Middle", author_name="Bob, DuplicateAuthor, KeeperAuthor")
        _link(item, self.bob, position=0)
        _link(item, self.duplicate, position=1)
        _link(item, self.keeper, position=2)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.bob.pk, self.keeper.pk])
        self.assertEqual(_positions(item), [0, 1])

    def test_keeper_and_duplicate_adjacent(self):
        item = _item(title="Adjacent", author_name="KeeperAuthor, DuplicateAuthor")
        _link(item, self.keeper, position=0)
        _link(item, self.duplicate, position=1)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.keeper.pk])
        self.assertEqual(_positions(item), [0])
        self.assertEqual(_author_name(item), "KeeperAuthor")

    def test_duplicate_only_among_several_preserves_relative_order(self):
        item = _item(title="Among several", author_name="Bob, DuplicateAuthor")
        _link(item, self.bob, position=0)
        _link(item, self.duplicate, position=1)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(_order(item), [self.bob.pk, self.keeper.pk])
        self.assertEqual(_positions(item), [0, 1])
        self.assertEqual(_author_name(item), "Bob, KeeperAuthor")

    def test_multiple_affected_items_and_unaffected_item(self):
        first = _item(title="First", author_name="DuplicateAuthor")
        _link(first, self.duplicate, position=0)
        second = _item(title="Second", author_name="Bob, DuplicateAuthor")
        _link(second, self.bob, position=0)
        _link(second, self.duplicate, position=1)
        unrelated = Author.objects.create(name="UnrelatedAuthor")
        untouched = _item(title="Untouched", author_name="UnrelatedAuthor")
        _link(untouched, unrelated, position=0)

        result = merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(
            result.affected_archive_item_ids, tuple(sorted([first.pk, second.pk]))
        )
        self.assertEqual(_order(first), [self.keeper.pk])
        self.assertEqual(_order(second), [self.bob.pk, self.keeper.pk])
        self.assertEqual(_order(untouched), [unrelated.pk])
        self.assertEqual(_author_name(untouched), "UnrelatedAuthor")
        self.assertTrue(Author.objects.filter(pk=unrelated.pk).exists())

    def test_over_255_joined_name_fails_before_writes(self):
        long_coauthor = Author.objects.create(name="y" * 200)
        long_keeper = Author.objects.create(name="K" * 60)
        item = _item(title="Too long", author_name="DuplicateAuthor, " + ("y" * 200))
        _link(item, self.duplicate, position=0)
        _link(item, long_coauthor, position=1)

        with self.assertRaises(AuthorMergeError) as ctx:
            merge_author(keeper=long_keeper, duplicate=self.duplicate)
        self.assertEqual(ctx.exception.message, AUTHOR_JOINED_TOO_LONG_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(_order(item), [self.duplicate.pk, long_coauthor.pk])
        self.assertEqual(_author_name(item), "DuplicateAuthor, " + ("y" * 200))

    def test_search_refresh_gets_exactly_duplicate_linked_ids(self):
        keeper_item = _item(title="Keeper indexed", author_name="KeeperAuthor")
        _link(keeper_item, self.keeper, position=0)
        first = _item(title="Dup A", author_name="DuplicateAuthor")
        _link(first, self.duplicate, position=0)
        second = _item(title="Dup B", author_name="DuplicateAuthor")
        _link(second, self.duplicate, position=0)
        _rebuild(keeper_item.pk)
        _rebuild(first.pk)
        _rebuild(second.pk)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked:
            merge_author(keeper=self.keeper, duplicate=self.duplicate)

        called_ids = [call.args[0] for call in mocked.call_args_list]
        self.assertEqual(called_ids, sorted([first.pk, second.pk]))
        self.assertNotIn(keeper_item.pk, called_ids)

    def test_search_refresh_failure_rolls_back_everything(self):
        item = _item(title="Rollback indexed", author_name="DuplicateAuthor")
        _link(item, self.duplicate, position=0)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes",
            side_effect=RuntimeError("index failed"),
        ):
            with self.assertRaises(RuntimeError):
                merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(_order(item), [self.duplicate.pk])
        self.assertEqual(_author_name(item), "DuplicateAuthor")

    def test_integrity_error_is_mapped_and_rolls_back(self):
        item = _item(title="Integrity", author_name="DuplicateAuthor")
        _link(item, self.duplicate, position=0)

        with patch(
            "documents.services.author_merge._replace_author_links",
            side_effect=IntegrityError("forced uniqueness collision"),
        ):
            with self.assertRaises(AuthorMergeError) as ctx:
                merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(ctx.exception.message, AUTHOR_MERGE_CONCURRENCY_ERROR)
        self.assertNotIsInstance(ctx.exception, IntegrityError)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(_order(item), [self.duplicate.pk])

    def test_same_named_person_and_person_relations_are_untouched(self):
        person = Person.objects.create(name="KeeperAuthor")
        PersonAlias.objects.create(person=person, name="KeeperAlias")
        item = _item(title="Isolation", author_name="DuplicateAuthor")
        _link(item, self.duplicate, position=0)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo isolation",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = PhotoContent.objects.create(
            archive_item=photo_item,
            position=1,
            original_file_key="photos/iso/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        PhotoPerson.objects.create(photo_content=photo, person=person)

        merge_author(keeper=self.keeper, duplicate=self.duplicate)

        person.refresh_from_db()
        self.assertEqual(person.name, "KeeperAuthor")
        self.assertTrue(
            PersonAlias.objects.filter(person=person, name="KeeperAlias").exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.keeper.refresh_from_db()
        self.assertEqual(self.keeper.name, "KeeperAuthor")

    def test_stale_link_after_author_locks_fails_closed(self):
        item = _item(title="Locked", author_name="DuplicateAuthor")
        _link(item, self.duplicate, position=0)
        late_item = _item(title="Late", author_name="LateUnrelatedAuthor")
        original_ids = [item.pk]
        stale_ids = sorted([item.pk, late_item.pk])

        def affected_ids_for_lock_then_stale_fanout(_author):
            call_index = affected_ids_for_lock_then_stale_fanout.calls
            affected_ids_for_lock_then_stale_fanout.calls += 1
            # merge_author: expand, stabilize, then re-read after Author locks.
            if call_index < 2:
                return list(original_ids)
            return list(stale_ids)

        affected_ids_for_lock_then_stale_fanout.calls = 0

        with patch(
            "documents.services.author_merge.affected_archive_item_ids_for_author",
            side_effect=affected_ids_for_lock_then_stale_fanout,
        ):
            with self.assertRaises(AuthorMergeError) as ctx:
                merge_author(keeper=self.keeper, duplicate=self.duplicate)

        self.assertEqual(ctx.exception.message, AUTHOR_LINKS_CHANGED_RETRY_ERROR)
        self.assertEqual(affected_ids_for_lock_then_stale_fanout.calls, 3)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.keeper.refresh_from_db()
        self.assertEqual(self.keeper.name, "KeeperAuthor")
        self.assertEqual(_order(item), [self.duplicate.pk])
        self.assertEqual(_positions(item), [0])
        self.assertEqual(_author_name(item), "DuplicateAuthor")
        self.assertEqual(_order(late_item), [])
        self.assertEqual(_author_name(late_item), "LateUnrelatedAuthor")
        self.assertFalse(
            ArchiveItemAuthor.objects.filter(author=self.keeper).exists()
        )

    def test_locks_items_before_authors_and_locks_coauthors(self):
        item = _item(title="Lock order", author_name="Bob, DuplicateAuthor")
        _link(item, self.bob, position=0)
        _link(item, self.duplicate, position=1)
        item_table = ArchiveItem._meta.db_table
        author_table = Author._meta.db_table
        with CaptureQueriesContext(connection) as ctx:
            merge_author(keeper=self.keeper, duplicate=self.duplicate)
        for_update = [
            query["sql"]
            for query in ctx.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
        ]
        item_lock_indexes = [
            index for index, sql in enumerate(for_update) if item_table in sql
        ]
        author_lock_indexes = [
            index for index, sql in enumerate(for_update) if author_table in sql
        ]
        self.assertTrue(item_lock_indexes)
        self.assertTrue(author_lock_indexes)
        self.assertLess(min(item_lock_indexes), min(author_lock_indexes))
        author_lock_sql = " ".join(
            sql for index, sql in enumerate(for_update) if index in author_lock_indexes
        )
        self.assertIn(str(self.keeper.pk), author_lock_sql)
        self.assertIn(str(self.duplicate.pk), author_lock_sql)
        self.assertIn(str(self.bob.pk), author_lock_sql)

    def test_preview_is_read_only_and_shows_order_plan(self):
        item = _item(
            title="Preview both",
            author_name="DuplicateAuthor, Bob, KeeperAuthor",
        )
        _link(item, self.duplicate, position=0)
        _link(item, self.bob, position=1)
        _link(item, self.keeper, position=2)
        preview = preview_author_merge(keeper=self.keeper, duplicate=self.duplicate)
        self.assertTrue(preview.can_execute)
        self.assertEqual(len(preview.affected_items), 1)
        self.assertEqual(preview.affected_items[0].archive_item_id, item.pk)
        self.assertTrue(preview.affected_items[0].keeper_already_linked)
        self.assertIn("Bob", preview.affected_items[0].planned_order[0])
        self.assertEqual(_order(item), [self.duplicate.pk, self.bob.pk, self.keeper.pk])
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())

    def test_zero_link_duplicate_is_deleted(self):
        result = merge_author(keeper=self.keeper, duplicate=self.duplicate)
        self.assertEqual(result.affected_archive_item_ids, ())
        self.assertFalse(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertTrue(Author.objects.filter(pk=self.keeper.pk).exists())
