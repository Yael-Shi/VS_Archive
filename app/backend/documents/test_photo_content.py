from datetime import date
from importlib import import_module

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from documents.admin import PhotoContentAdmin
from documents.models import ArchiveItem, Document, Person, PhotoContent, PhotoPerson
from documents.services.archive_item_access import (
    filter_browse_renderable_archive_items,
)
from documents.services.archive_item_validation import (
    validate_stored_archive_date_fields,
)


def _create_photo_archive_item(*, title: str = "Family photo") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _photo_content_defaults(**overrides) -> dict:
    values = {
        "original_file_key": "photos/1/original.jpg",
        "original_filename": "scan.jpg",
        "original_mime_type": "image/jpeg",
        "original_size_bytes": 2048,
        "upload_status": PhotoContent.UploadStatus.UPLOADED,
        "upload_error": "",
    }
    values.update(overrides)
    return values


class PhotoContentModelTests(TestCase):
    def test_photo_content_can_be_created_for_photo_archive_item(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.archive_item_id, archive_item.id)
        self.assertEqual(content.original_filename, "scan.jpg")
        self.assertEqual(content.position, 1)
        self.assertEqual(archive_item.primary_photo_content.id, content.id)
        self.assertEqual(list(archive_item.photo_contents.all()), [content])
        with self.assertRaises(AttributeError):
            _ = archive_item.photo_content

    def test_photo_content_cannot_validate_for_non_photo_archive_item(self):
        manual_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Manual note",
        )
        content = PhotoContent(
            archive_item=manual_item,
            **_photo_content_defaults(),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("archive_item", ctx.exception.message_dict)

    def test_one_archive_item_can_own_multiple_photo_content_rows(self):
        archive_item = _create_photo_archive_item()
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        second = PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(
                original_file_key="photos/2/original.jpg",
                original_filename="scan-2.jpg",
            ),
        )
        self.assertCountEqual(
            archive_item.photo_contents.values_list("pk", flat=True),
            [first.pk, second.pk],
        )
        self.assertEqual(archive_item.primary_photo_content.pk, first.pk)

    def test_duplicate_position_within_same_archive_item_is_rejected(self):
        archive_item = _create_photo_archive_item()
        PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PhotoContent.objects.create(
                    archive_item=archive_item,
                    original_file_key="photos/1/original-duplicate.jpg",
                    original_filename="duplicate.jpg",
                    original_mime_type="image/jpeg",
                    original_size_bytes=1024,
                    position=1,
                )
        self.assertEqual(archive_item.photo_contents.count(), 1)

    def test_same_position_is_allowed_on_different_archive_items(self):
        first_item = _create_photo_archive_item(title="Album A")
        second_item = _create_photo_archive_item(title="Album B")
        PhotoContent.objects.create(
            archive_item=first_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/a/original.jpg"),
        )
        PhotoContent.objects.create(
            archive_item=second_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/b/original.jpg"),
        )
        self.assertEqual(first_item.photo_contents.count(), 1)
        self.assertEqual(second_item.photo_contents.count(), 1)

    def test_existing_style_one_photo_archive_item_defaults_position_one(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.position, 1)
        self.assertEqual(archive_item.photo_contents.get().pk, content.pk)
        self.assertEqual(archive_item.primary_photo_content.pk, content.pk)

    def test_photo_rows_order_deterministically_by_position_then_id(self):
        archive_item = _create_photo_archive_item()
        third = PhotoContent.objects.create(
            archive_item=archive_item,
            position=3,
            **_photo_content_defaults(original_file_key="photos/3/original.jpg"),
        )
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        second = PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(original_file_key="photos/2/original.jpg"),
        )
        self.assertEqual(
            list(archive_item.photo_contents.values_list("pk", flat=True)),
            [first.pk, second.pk, third.pk],
        )
        self.assertEqual(archive_item.primary_photo_content.pk, first.pk)

    def test_prefetched_primary_photo_content_does_not_query(self):
        archive_item = _create_photo_archive_item()
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(original_file_key="photos/2/original.jpg"),
        )
        prefetched = ArchiveItem.objects.prefetch_related("photo_contents").get(
            pk=archive_item.pk
        )

        with CaptureQueriesContext(connection) as ctx:
            primary = prefetched.primary_photo_content

        self.assertEqual(primary.pk, first.pk)
        self.assertEqual(len(ctx), 0)

    def test_non_prefetched_primary_photo_content_reflects_database_changes(self):
        archive_item = _create_photo_archive_item()
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        second = PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(original_file_key="photos/2/original.jpg"),
        )
        item = ArchiveItem.objects.get(pk=archive_item.pk)
        self.assertEqual(item.primary_photo_content.pk, first.pk)

        PhotoContent.objects.filter(pk=first.pk).update(position=3)
        PhotoContent.objects.filter(pk=second.pk).update(position=1)
        PhotoContent.objects.filter(pk=first.pk).update(position=2)

        self.assertEqual(item.primary_photo_content.pk, second.pk)

    def test_deleting_archive_item_cascades_to_all_photo_content_rows(self):
        archive_item = _create_photo_archive_item()
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        second = PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(original_file_key="photos/2/original.jpg"),
        )
        first_id = first.id
        second_id = second.id
        archive_item.delete()
        self.assertFalse(
            PhotoContent.objects.filter(pk__in=[first_id, second_id]).exists()
        )

    def test_thumbnail_fields_are_optional_in_v1(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.thumbnail_file_key, "")
        self.assertEqual(content.thumbnail_mime_type, "")
        self.assertIsNone(content.thumbnail_size_bytes)
        self.assertIsNone(content.width)
        self.assertIsNone(content.height)

    def test_photo_metadata_fields_exist_and_default_blank(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.description, "")
        self.assertEqual(content.location, "")
        self.assertEqual(content.context, "")
        self.assertEqual(content.people_present, "")
        self.assertEqual(content.notes, "")

    def test_no_document_is_required_for_photo_content(self):
        archive_item = _create_photo_archive_item()
        PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertFalse(Document.objects.filter(archive_item=archive_item).exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_photo_content_admin_is_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            username="photo_content_admin",
            password="test-pass",
            email="photo-admin@example.com",
        )
        site = AdminSite()
        admin = PhotoContentAdmin(PhotoContent, site)
        self.assertTrue(admin.has_view_permission(request))
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_delete_permission(request))


class PhotoContentDateTests(TestCase):
    def test_photo_dates_default_unknown_and_do_not_copy_archive_item_dates(self):
        archive_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Dated umbrella",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=date(1948, 5, 14),
            date_end=date(1948, 5, 14),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertIsNone(content.date_start)
        self.assertIsNone(content.date_end)
        self.assertEqual(content.date_precision, ArchiveItem.DatePrecision.UNKNOWN)
        archive_item.refresh_from_db()
        self.assertEqual(archive_item.date_start, date(1948, 5, 14))
        self.assertEqual(
            archive_item.date_precision, ArchiveItem.DatePrecision.EXACT_DAY
        )

    def test_photo_content_can_store_independent_archival_dates(self):
        archive_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Umbrella 1950s",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=date(1950, 1, 1),
            date_end=date(1959, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            date_start=date(1952, 6, 1),
            date_end=date(1952, 6, 30),
            date_precision=ArchiveItem.DatePrecision.MONTH,
            **_photo_content_defaults(),
        )
        content.refresh_from_db()
        self.assertEqual(content.date_start, date(1952, 6, 1))
        self.assertEqual(content.date_end, date(1952, 6, 30))
        self.assertEqual(content.date_precision, ArchiveItem.DatePrecision.MONTH)
        archive_item.refresh_from_db()
        self.assertEqual(archive_item.date_start, date(1950, 1, 1))
        self.assertEqual(
            archive_item.date_precision, ArchiveItem.DatePrecision.RANGE_YEAR
        )

    def test_photo_date_precision_choices_match_archive_item(self):
        photo_field = PhotoContent._meta.get_field("date_precision")
        archive_field = ArchiveItem._meta.get_field("date_precision")
        self.assertEqual(list(photo_field.choices), list(archive_field.choices))
        self.assertEqual(photo_field.default, ArchiveItem.DatePrecision.UNKNOWN)

    def test_photo_date_end_before_start_is_rejected_like_archive_item(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent(
            archive_item=archive_item,
            date_start=date(1960, 1, 1),
            date_end=date(1959, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE,
            **_photo_content_defaults(),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("date_end", ctx.exception.message_dict)
        self.assertIn(
            "date_end must not be before date_start",
            ctx.exception.message_dict["date_end"],
        )

        with self.assertRaises(ValidationError) as helper_ctx:
            validate_stored_archive_date_fields(
                date_start=date(1960, 1, 1),
                date_end=date(1959, 12, 31),
                date_precision=ArchiveItem.DatePrecision.RANGE,
            )
        self.assertIn("date_end", helper_ctx.exception.message_dict)

    def test_photo_invalid_date_precision_is_rejected(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent(
            archive_item=archive_item,
            date_precision="GUESS",
            **_photo_content_defaults(),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("date_precision", ctx.exception.message_dict)


class PhotoContentPersonRelationTests(TestCase):
    def test_photo_person_still_targets_a_specific_photo_content(self):
        archive_item = _create_photo_archive_item()
        first = PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(original_file_key="photos/1/original.jpg"),
        )
        second = PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(original_file_key="photos/2/original.jpg"),
        )
        person = Person.objects.create(name="Grandpa")
        PhotoPerson.objects.create(photo_content=first, person=person)

        self.assertEqual(list(first.people.all()), [person])
        self.assertEqual(list(second.people.all()), [])
        self.assertEqual(list(person.photo_contents.all()), [first])
        self.assertEqual(archive_item.people.count(), 0)

    def test_deleting_archive_item_cascades_photo_person_rows(self):
        archive_item = _create_photo_archive_item()
        photo = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        person = Person.objects.create(name="Grandma")
        link = PhotoPerson.objects.create(photo_content=photo, person=person)
        link_id = link.pk
        archive_item.delete()
        self.assertFalse(PhotoPerson.objects.filter(pk=link_id).exists())
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())


class PhotoContentBrowseCompatibilityTests(TestCase):
    def test_browse_eligibility_uses_the_first_photo_only(self):
        archive_item = _create_photo_archive_item(title="Mixed upload statuses")
        PhotoContent.objects.create(
            archive_item=archive_item,
            position=1,
            **_photo_content_defaults(
                original_file_key="",
                upload_status=PhotoContent.UploadStatus.PENDING,
            ),
        )
        PhotoContent.objects.create(
            archive_item=archive_item,
            position=2,
            **_photo_content_defaults(
                original_file_key="photos/2/original.jpg",
                upload_status=PhotoContent.UploadStatus.UPLOADED,
            ),
        )
        qs = filter_browse_renderable_archive_items(ArchiveItem.objects.all())
        self.assertFalse(qs.filter(pk=archive_item.pk).exists())

        archive_item.photo_contents.filter(position=1).update(
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="photos/1/original.jpg",
        )
        qs = filter_browse_renderable_archive_items(ArchiveItem.objects.all())
        self.assertTrue(qs.filter(pk=archive_item.pk).exists())


class PhotoContentMigrationContractTests(TestCase):
    def test_migration_adds_position_default_one_and_fk_related_name(self):
        migration_module = import_module(
            "documents.migrations.0053_photocontent_multi_photo_foundation"
        )
        operations = migration_module.Migration.operations
        add_position = next(
            op
            for op in operations
            if op.__class__.__name__ == "AddField" and op.name == "position"
        )
        self.assertEqual(add_position.field.default, 1)

        alter_archive_item = next(
            op
            for op in operations
            if op.__class__.__name__ == "AlterField" and op.name == "archive_item"
        )
        self.assertEqual(
            alter_archive_item.field.remote_field.related_name, "photo_contents"
        )

        constraint_names = {
            op.constraint.name
            for op in operations
            if op.__class__.__name__ == "AddConstraint"
        }
        self.assertIn("uniq_photocontent_archive_item_position", constraint_names)
        self.assertIn("photocontent_position_gte_1", constraint_names)


class PhotoContentMultiPhotoMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_existing_rows_receive_position_one_and_unknown_dates(self):
        migrate_from = [("documents", "0052_person_identity_foundation")]
        migrate_to = [("documents", "0053_photocontent_multi_photo_foundation")]
        try:
            old_apps = self._migrate(migrate_from)
            ArchiveItemModel = old_apps.get_model("documents", "ArchiveItem")
            PhotoContentModel = old_apps.get_model("documents", "PhotoContent")
            item = ArchiveItemModel.objects.create(
                title="Legacy photo",
                item_type="PHOTO",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
            )
            photo = PhotoContentModel.objects.create(
                archive_item=item,
                original_file_key="photos/legacy/original.jpg",
                original_filename="legacy.jpg",
                original_mime_type="image/jpeg",
                original_size_bytes=1024,
                upload_status="UPLOADED",
            )
            photo_id = photo.pk
            item_id = item.pk
            original_key = photo.original_file_key

            new_apps = self._migrate(migrate_to)
            MigratedPhotoContent = new_apps.get_model("documents", "PhotoContent")
            migrated = MigratedPhotoContent.objects.get(pk=photo_id)
            self.assertEqual(migrated.position, 1)
            self.assertEqual(migrated.date_precision, "UNKNOWN")
            self.assertIsNone(migrated.date_start)
            self.assertIsNone(migrated.date_end)
            self.assertEqual(migrated.archive_item_id, item_id)
            self.assertEqual(migrated.original_file_key, original_key)
        finally:
            self._migrate([("documents", "0053_photocontent_multi_photo_foundation")])
