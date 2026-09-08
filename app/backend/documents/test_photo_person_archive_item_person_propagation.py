"""PhotoPerson implies ArchiveItemPerson: add-only production writers and backfill."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase
from django.test.utils import override_settings

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_people import (
    ensure_archive_item_person,
    set_archive_item_people,
)
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.services.person_merge import merge_persons
from documents.services.person_public import public_people_queryset
from documents.services.photo_content_management import set_photo_people
from documents.services.photo_person_archive_item_person_backfill import (
    PhotoPersonArchiveItemPersonBackfillError,
    apply_photo_person_archive_item_person_backfill,
    build_photo_person_archive_item_person_backfill_plan,
)
from documents.test_person_merge import _create_ordinary_person


def _photo_item(title: str = "Album") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _photo(item: ArchiveItem, *, position: int = 1) -> PhotoContent:
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=f"photos/{item.pk}-{position}/original.jpg",
        original_filename="scan.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )


class PhotoPersonImpliesArchiveItemPersonTests(TestCase):
    def test_set_photo_people_creates_matching_aip(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada")
        set_photo_people(photo, [person.pk])
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_existing_aip_is_not_duplicated(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        set_photo_people(photo, [person.pk])
        set_photo_people(photo, [person.pk])
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )
        self.assertEqual(
            PhotoPerson.objects.filter(photo_content=photo, person=person).count(),
            1,
        )

    def test_removing_photo_person_does_not_delete_aip(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada")
        set_photo_people(photo, [person.pk])
        set_photo_people(photo, [])
        self.assertFalse(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_aip_does_not_create_photo_person(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Item only")
        set_archive_item_people(archive_item=item, person_ids=[person.pk])
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertFalse(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )

    def test_ensure_is_add_only_and_preserves_unique_constraint(self):
        item = _photo_item()
        person = Person.objects.create(name="Ada")
        first, created = ensure_archive_item_person(
            archive_item=item, person=person, refresh_search_index=False
        )
        second, created_again = ensure_archive_item_person(
            archive_item=item, person=person, refresh_search_index=False
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )


class EnsureArchiveItemPersonRaceTests(TestCase):
    def test_existing_relation_is_noop_and_skips_index_refresh(self):
        item = _photo_item()
        person = Person.objects.create(name="Ada")
        existing = ArchiveItemPerson.objects.create(archive_item=item, person=person)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            link, created = ensure_archive_item_person(
                archive_item=item, person=person, refresh_search_index=True
            )
        self.assertFalse(created)
        self.assertEqual(link.pk, existing.pk)
        mocked.assert_not_called()
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_successful_create_refreshes_index_when_requested(self):
        item = _photo_item()
        person = Person.objects.create(name="Ada")
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            link, created = ensure_archive_item_person(
                archive_item=item, person=person, refresh_search_index=True
            )
        self.assertTrue(created)
        self.assertEqual(link.person_id, person.pk)
        mocked.assert_called_once_with(item.pk)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_unique_collision_inside_outer_transaction_recovers_without_breaking_it(
        self,
    ):
        item = _photo_item()
        person = Person.objects.create(name="Ada")
        existing = ArchiveItemPerson.objects.create(archive_item=item, person=person)
        missed = MagicMock()
        missed.first.return_value = None

        with transaction.atomic():
            with patch.object(ArchiveItemPerson.objects, "filter", return_value=missed):
                with patch(
                    "documents.services.archive_search_index.sync_archive_item_search_index"
                ) as mocked:
                    link, created = ensure_archive_item_person(
                        archive_item=item,
                        person=person,
                        refresh_search_index=True,
                    )
                mocked.assert_not_called()
            self.assertFalse(created)
            self.assertEqual(link.pk, existing.pk)
            later_person = Person.objects.create(name="After recovery")
            later = ArchiveItemPerson.objects.create(
                archive_item=item, person=later_person
            )
            self.assertEqual(
                ArchiveItemPerson.objects.filter(archive_item=item).count(),
                2,
            )
            self.assertEqual(later.person_id, later_person.pk)

    def test_search_index_refresh_includes_aip_once_after_photo_person_write(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="SearchableAdaToken")
        set_photo_people(photo, [person.pk])
        sync_archive_item_search_index(item.pk)
        index = ArchiveItemSearchIndex.objects.get(archive_item=item)
        self.assertIn("SearchableAdaToken", index.metadata_text)
        self.assertEqual(index.metadata_text.count("SearchableAdaToken"), 1)

    def test_public_person_membership_is_one_identity(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Public Ada")
        set_photo_people(photo, [person.pk])
        ids = list(public_people_queryset(None).values_list("pk", flat=True))
        self.assertEqual(ids.count(person.pk), 1)

    def test_merge_ensures_aip_for_moved_photo_person_without_creating_photo_from_aip(
        self,
    ):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        item_only = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Item only",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo_item = _photo_item(title="Photo only")
        photo = _photo(photo_item)
        ArchiveItemPerson.objects.create(archive_item=item_only, person=duplicate)
        PhotoPerson.objects.create(photo_content=photo, person=duplicate)

        merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item_only, person=keeper
            ).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=keeper).exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=photo_item, person=keeper
            ).exists()
        )
        self.assertFalse(
            PhotoPerson.objects.filter(
                photo_content__archive_item=item_only, person=keeper
            ).exists()
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(
                archive_item=photo_item, person=keeper
            ).count(),
            1,
        )


class ArchiveItemPersonReplaceKeepsPhotoPersonRequiredLinksTests(TestCase):
    def test_replace_omitting_photo_person_keeps_one_aip(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Appears")
        set_photo_people(photo, [person.pk])
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )
        set_archive_item_people(archive_item=item, person_ids=[])
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)

    def test_manual_aip_without_photo_person_can_be_removed(self):
        item = _photo_item()
        photo = _photo(item)
        keep = Person.objects.create(name="In photo")
        drop = Person.objects.create(name="Item only")
        set_photo_people(photo, [keep.pk])
        set_archive_item_people(archive_item=item, person_ids=[keep.pk, drop.pk])
        set_archive_item_people(archive_item=item, person_ids=[])
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=keep).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=drop).exists()
        )
        self.assertFalse(
            PhotoPerson.objects.filter(photo_content=photo, person=drop).exists()
        )

    def test_multiple_photo_person_rows_require_one_aip(self):
        item = _photo_item()
        first = _photo(item, position=1)
        second = _photo(item, position=2)
        person = Person.objects.create(name="Twice")
        set_photo_people(first, [person.pk])
        set_photo_people(second, [person.pk])
        set_archive_item_people(archive_item=item, person_ids=[])
        self.assertEqual(
            PhotoPerson.objects.filter(
                photo_content__archive_item=item, person=person
            ).count(),
            2,
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_empty_explicit_selection_preserves_all_photo_person_implied_aips(self):
        item = _photo_item()
        first = _photo(item, position=1)
        second = _photo(item, position=2)
        ada = Person.objects.create(name="Ada")
        rivka = Person.objects.create(name="Rivka")
        extra = Person.objects.create(name="Manual only")
        set_photo_people(first, [ada.pk])
        set_photo_people(second, [rivka.pk])
        set_archive_item_people(
            archive_item=item, person_ids=[ada.pk, rivka.pk, extra.pk]
        )
        set_archive_item_people(archive_item=item, person_ids=[])
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {ada.pk, rivka.pk},
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=extra).exists()
        )

    def test_manual_aip_add_still_does_not_create_photo_person(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Item only")
        set_archive_item_people(archive_item=item, person_ids=[person.pk])
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertFalse(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )

    def test_delete_archive_item_person_is_noop_while_photo_person_exists(self):
        from documents.services.archive_item_people import delete_archive_item_person

        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Appears")
        set_photo_people(photo, [person.pk])
        link = ArchiveItemPerson.objects.get(archive_item=item, person=person)
        deleted = delete_archive_item_person(link)
        self.assertFalse(deleted)
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )

    def test_replace_omit_does_not_refresh_index_when_aip_already_required(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Appears")
        set_photo_people(photo, [person.pk])
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            set_archive_item_people(archive_item=item, person_ids=[])
        mocked.assert_not_called()
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoPersonArchiveItemPersonBackfillTests(TestCase):
    def test_dry_run_apply_and_idempotency(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Legacy")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        people_present = photo.people_present

        dry = build_photo_person_archive_item_person_backfill_plan()
        self.assertEqual(dry.create_count, 1)
        self.assertEqual(dry.noop_count, 0)
        self.assertFalse(dry.applied)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

        applied = apply_photo_person_archive_item_person_backfill()
        self.assertTrue(applied.applied)
        self.assertEqual(applied.create_count, 1)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )
        self.assertEqual(PhotoPerson.objects.count(), 1)
        photo.refresh_from_db()
        self.assertEqual(photo.people_present, people_present)
        self.assertEqual(Person.objects.get(pk=person.pk).name, "Legacy")

        again = apply_photo_person_archive_item_person_backfill()
        self.assertEqual(again.create_count, 0)
        self.assertEqual(again.noop_count, 1)
        self.assertEqual(ArchiveItemPerson.objects.count(), 1)

    def test_apply_reports_actual_create_and_noop_for_same_person_on_two_photos(self):
        item = _photo_item()
        first = _photo(item, position=1)
        second = _photo(item, position=2)
        person = Person.objects.create(name="Shared")
        PhotoPerson.objects.create(photo_content=first, person=person)
        PhotoPerson.objects.create(photo_content=second, person=person)

        dry = build_photo_person_archive_item_person_backfill_plan()
        self.assertEqual(dry.create_count, 2)
        self.assertEqual(dry.noop_count, 0)
        self.assertFalse(dry.applied)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

        with patch(
            "documents.services.photo_person_archive_item_person_backfill.sync_archive_item_search_indexes"
        ) as mocked:
            applied = apply_photo_person_archive_item_person_backfill()
        mocked.assert_called_once_with([item.pk])
        self.assertTrue(applied.applied)
        self.assertEqual(applied.create_count, 1)
        self.assertEqual(applied.noop_count, 1)
        self.assertEqual(
            [row.status for row in applied.rows],
            ["CREATE", "NOOP"],
        )
        self.assertEqual(applied.created_archive_item_ids, (item.pk,))
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )
        self.assertEqual(PhotoPerson.objects.count(), 2)

        with patch(
            "documents.services.photo_person_archive_item_person_backfill.sync_archive_item_search_indexes"
        ) as mocked_again:
            again = apply_photo_person_archive_item_person_backfill()
        mocked_again.assert_not_called()
        self.assertEqual(again.create_count, 0)
        self.assertEqual(again.noop_count, 2)
        self.assertEqual(again.created_archive_item_ids, ())
        self.assertEqual(ArchiveItemPerson.objects.count(), 1)

    def test_command_dry_run_does_not_write(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Command Legacy")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        call_command("backfill_archive_item_person_from_photo_person")
        self.assertFalse(ArchiveItemPerson.objects.exists())
        call_command("backfill_archive_item_person_from_photo_person", apply=True)
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_fail_closed_on_non_photo_parent(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Not a photo item",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = PhotoContent(
            archive_item=item,
            position=1,
            original_file_key="photos/wrong/original.jpg",
            original_filename="scan.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        photo.save()
        person = Person.objects.create(name="Stray")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        with self.assertRaises(PhotoPersonArchiveItemPersonBackfillError):
            apply_photo_person_archive_item_person_backfill()
        plan = build_photo_person_archive_item_person_backfill_plan()
        self.assertEqual(plan.error_count, 1)
        with self.assertRaises(CommandError):
            call_command("backfill_archive_item_person_from_photo_person")
        self.assertFalse(ArchiveItemPerson.objects.exists())
