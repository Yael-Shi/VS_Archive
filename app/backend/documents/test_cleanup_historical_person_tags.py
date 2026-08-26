"""D1 cleanup: delete mapped historical person-Tag relations only."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.forms import ValidationError
from django.test import RequestFactory, TestCase

from documents.admin import DocumentAdmin, DocumentAdminForm, TagAdmin
from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
    historical_person_name_tag_ids,
    person_id_for_historical_person_name_tag,
)
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    Person,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_discovery_metadata_validation import (
    HISTORICAL_PERSON_TAG_DELETE_ERROR,
    HISTORICAL_PERSON_TAG_REUSE_ERROR,
)
from documents.services.archive_item_people import create_archive_item_person
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.test_historical_person_tag_reuse import (
    _create_tag,
    _reset_pk_sequence,
)

BLOCKED_TAG_ID = 29
BLOCKED_PERSON_ID = person_id_for_historical_person_name_tag(BLOCKED_TAG_ID)
DOCUMENT_MAPPED_TAG_ID = 8
DOCUMENT_MAPPED_PERSON_ID = person_id_for_historical_person_name_tag(
    DOCUMENT_MAPPED_TAG_ID
)
COMMAND_NAME = "cleanup_historical_person_tags"


def _seed_frozen_map_rows() -> None:
    for tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID:
        Tag.objects.create(pk=tag_id, name=f"tag-{tag_id}-not-a-lookup-key")
        Person.objects.create(pk=person_id, name=f"person-{person_id}-not-a-lookup-key")
    _reset_pk_sequence(Tag)
    _reset_pk_sequence(Person)


def _seed_frozen_person_rows() -> None:
    for _tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID:
        Person.objects.create(pk=person_id, name=f"person-{person_id}-not-a-lookup-key")
    _reset_pk_sequence(Person)


def _create_photo_item(*, title: str) -> tuple[ArchiveItem, PhotoContent]:
    item = ArchiveItem.objects.create(
        title=title,
        item_type=ArchiveItem.ItemType.PHOTO,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=1,
        original_file_key=f"photos/{item.id}/1.jpg",
        original_filename="1.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item, photo


class CleanupHistoricalPersonTagsCommandTests(TestCase):
    def test_missing_required_ids_fail_closed_without_writes(self):
        item = create_manual_text_archive_item(title="No map rows", body="Body")
        stdout = StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=stdout)
        self.assertIn("missing Person ids", str(caught.exception))
        self.assertNotIn("missing Tag ids", str(caught.exception))
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(ArchiveItem.tags.through.objects.count(), 0)
        self.assertEqual(Document.tags_m2m.through.objects.count(), 0)

    def test_all_mapped_tag_rows_absent_is_zero_plan_success(self):
        _seed_frozen_person_rows()
        mapped_ids = historical_person_name_tag_ids()
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)

        dry_stdout = StringIO()
        call_command(COMMAND_NAME, stdout=dry_stdout)
        dry_output = dry_stdout.getvalue()
        self.assertIn("mode: dry-run", dry_output)
        self.assertIn("planned: 0", dry_output)
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)

        apply_stdout = StringIO()
        call_command(COMMAND_NAME, "--apply-relations", stdout=apply_stdout)
        apply_output = apply_stdout.getvalue()
        self.assertIn("mode: apply-relations", apply_output)
        self.assertIn("planned: 0", apply_output)
        self.assertIn("deleted: 0", apply_output)
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)
        self.assertEqual(ArchiveItem.tags.through.objects.count(), 0)
        self.assertEqual(Document.tags_m2m.through.objects.count(), 0)

    def test_partial_mapped_tag_ids_fail_closed(self):
        _seed_frozen_person_rows()
        for tag_id, _person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID:
            if tag_id == BLOCKED_TAG_ID:
                continue
            Tag.objects.create(pk=tag_id, name=f"tag-{tag_id}-not-a-lookup-key")
        _reset_pk_sequence(Tag)

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=StringIO())
        self.assertIn("missing Tag ids", str(caught.exception))
        self.assertIn(str(BLOCKED_TAG_ID), str(caught.exception))
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 28
        )

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-relations", stdout=StringIO())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 28
        )
        self.assertEqual(ArchiveItem.tags.through.objects.count(), 0)

    def test_missing_archive_item_person_fails_before_writes(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        linked_item = create_manual_text_archive_item(title="Has AIP", body="A")
        missing_item = create_manual_text_archive_item(title="Missing AIP", body="B")
        ordinary = _create_tag(name="ordinary-keep")
        linked_item.tags.add(blocked, ordinary)
        missing_item.tags.add(blocked)
        create_archive_item_person(archive_item=linked_item, person=person)

        item_through_before = list(
            ArchiveItem.tags.through.objects.order_by("id").values_list(
                "id", "archiveitem_id", "tag_id"
            )
        )
        stdout = StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=stdout)
        self.assertIn("lack ArchiveItemPerson", str(caught.exception))
        self.assertIn(str(missing_item.id), str(caught.exception))
        self.assertEqual(
            list(
                ArchiveItem.tags.through.objects.order_by("id").values_list(
                    "id", "archiveitem_id", "tag_id"
                )
            ),
            item_through_before,
        )

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-relations", stdout=StringIO())
        self.assertEqual(
            list(
                ArchiveItem.tags.through.objects.order_by("id").values_list(
                    "id", "archiveitem_id", "tag_id"
                )
            ),
            item_through_before,
        )
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=linked_item, person=person
            ).exists()
        )

    def test_dry_run_makes_no_writes(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        ordinary = _create_tag(name="ordinary-dry-run")
        item = create_manual_text_archive_item(title="Tagged item", body="Body")
        item.tags.add(blocked, ordinary)
        create_archive_item_person(archive_item=item, person=person)
        through = ArchiveItem.tags.through.objects.get(
            archiveitem_id=item.id, tag_id=blocked.pk
        )

        stdout = StringIO()
        call_command(COMMAND_NAME, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode: dry-run", output)
        self.assertIn("planned: 1", output)
        self.assertIn("planned_archiveitem_tag_relations: 1", output)
        self.assertIn("deleted: 0", output)
        planned_row = (
            through.id,
            item.id,
            BLOCKED_TAG_ID,
            BLOCKED_PERSON_ID,
        )
        self.assertIn(str(planned_row), output)
        self.assertTrue(
            ArchiveItem.tags.through.objects.filter(
                archiveitem_id=item.id, tag_id=BLOCKED_TAG_ID
            ).exists()
        )
        self.assertTrue(item.tags.filter(pk=ordinary.pk).exists())
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())

    def test_apply_deletes_mapped_through_rows_only(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        mapped_doc_tag = Tag.objects.get(pk=DOCUMENT_MAPPED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        doc_person = Person.objects.get(pk=DOCUMENT_MAPPED_PERSON_ID)
        ordinary = _create_tag(name="ordinary-keep-apply")
        item = create_manual_text_archive_item(title="Item mapped tag", body="Body")
        item.tags.add(blocked, ordinary)
        create_archive_item_person(archive_item=item, person=person)
        photo_item, photo = _create_photo_item(title="Photo appearance")
        appearance = Person.objects.create(name="appearance-only")
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        doc = create_ocr_document(
            title="Doc mapped tags_m2m",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(mapped_doc_tag, ordinary)
        doc.archive_item.tags.add(ordinary)
        item_through = ArchiveItem.tags.through.objects.get(
            archiveitem_id=item.id, tag_id=BLOCKED_TAG_ID
        )
        doc_through = Document.tags_m2m.through.objects.get(
            document_id=doc.id, tag_id=DOCUMENT_MAPPED_TAG_ID
        )
        person_links_before = list(
            ArchiveItemPerson.objects.order_by("id").values_list(
                "id", "archive_item_id", "person_id"
            )
        )
        photo_person_before = list(
            PhotoPerson.objects.order_by("id").values_list(
                "id", "photo_content_id", "person_id"
            )
        )
        mapped_tag_ids_before = set(
            Tag.objects.filter(
                pk__in=[
                    tag_id for tag_id, _pid in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
                ]
            ).values_list("pk", flat=True)
        )
        sync_archive_item_search_index(item.pk)
        index_before = ArchiveItemSearchIndex.objects.get(archive_item_id=item.id)
        self.assertIn(blocked.name, index_before.metadata_text)
        self.assertIn(person.name, index_before.metadata_text)

        stdout = StringIO()
        call_command(COMMAND_NAME, "--apply-relations", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode: apply-relations", output)
        self.assertIn("planned: 2", output)
        self.assertIn("planned_archiveitem_tag_relations: 1", output)
        self.assertIn("planned_document_tag_relations: 1", output)
        self.assertIn("deleted: 2", output)
        item_row = (
            item_through.id,
            item.id,
            BLOCKED_TAG_ID,
            BLOCKED_PERSON_ID,
        )
        doc_row = (doc_through.id, doc.id, DOCUMENT_MAPPED_TAG_ID)
        self.assertIn(str(item_row), output)
        self.assertIn(str(doc_row), output)
        self.assertFalse(
            ArchiveItem.tags.through.objects.filter(
                archiveitem_id=item.id, tag_id=BLOCKED_TAG_ID
            ).exists()
        )
        self.assertFalse(
            Document.tags_m2m.through.objects.filter(
                document_id=doc.id, tag_id=DOCUMENT_MAPPED_TAG_ID
            ).exists()
        )
        item.refresh_from_db()
        self.assertEqual(list(item.tags.values_list("pk", flat=True)), [ordinary.pk])
        self.assertEqual(
            set(doc.tags_m2m.values_list("pk", flat=True)),
            {ordinary.pk},
        )
        self.assertEqual(
            set(doc.archive_item.tags.values_list("pk", flat=True)),
            {ordinary.pk},
        )
        self.assertEqual(
            set(
                Tag.objects.filter(
                    pk__in=[
                        tag_id
                        for tag_id, _pid in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
                    ]
                ).values_list("pk", flat=True)
            ),
            mapped_tag_ids_before,
        )
        self.assertEqual(
            list(
                ArchiveItemPerson.objects.order_by("id").values_list(
                    "id", "archive_item_id", "person_id"
                )
            ),
            person_links_before,
        )
        self.assertEqual(
            list(
                PhotoPerson.objects.order_by("id").values_list(
                    "id", "photo_content_id", "person_id"
                )
            ),
            photo_person_before,
        )
        self.assertTrue(Person.objects.filter(pk=BLOCKED_PERSON_ID).exists())
        self.assertTrue(Person.objects.filter(pk=DOCUMENT_MAPPED_PERSON_ID).exists())
        index_after = ArchiveItemSearchIndex.objects.get(archive_item_id=item.id)
        self.assertNotIn(blocked.name, index_after.metadata_text)
        self.assertIn(person.name, index_after.metadata_text)
        self.assertIn(ordinary.name, index_after.metadata_text)
        self.assertFalse(ArchiveItemPerson.objects.filter(person=doc_person).exists())

    def test_search_sync_failure_rolls_back_deletions(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        item = create_manual_text_archive_item(title="Rollback item", body="Body")
        item.tags.add(blocked)
        create_archive_item_person(archive_item=item, person=person)
        through_before = list(
            ArchiveItem.tags.through.objects.order_by("id").values_list(
                "id", "archiveitem_id", "tag_id"
            )
        )

        with patch(
            "documents.services.historical_person_tag_cleanup."
            "sync_archive_item_search_indexes",
            side_effect=RuntimeError("sync failed"),
        ):
            with self.assertRaises(RuntimeError):
                call_command(COMMAND_NAME, "--apply-relations", stdout=StringIO())

        self.assertEqual(
            list(
                ArchiveItem.tags.through.objects.order_by("id").values_list(
                    "id", "archiveitem_id", "tag_id"
                )
            ),
            through_before,
        )
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_second_apply_is_zero_plan_success(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        item = create_manual_text_archive_item(title="Idempotent", body="Body")
        item.tags.add(blocked)
        create_archive_item_person(archive_item=item, person=person)

        call_command(COMMAND_NAME, "--apply-relations", stdout=StringIO())
        tag_ids_after_first = list(
            Tag.objects.order_by("id").values_list("id", flat=True)
        )
        aip_after_first = list(
            ArchiveItemPerson.objects.order_by("id").values_list("id", flat=True)
        )

        stdout = StringIO()
        call_command(COMMAND_NAME, "--apply-relations", stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("mode: apply-relations", output)
        self.assertIn("planned: 0", output)
        self.assertIn("deleted: 0", output)
        self.assertIn("planned ArchiveItem.tags through rows:\n  (none)", output)
        self.assertEqual(
            list(Tag.objects.order_by("id").values_list("id", flat=True)),
            tag_ids_after_first,
        )
        self.assertEqual(
            list(ArchiveItemPerson.objects.order_by("id").values_list("id", flat=True)),
            aip_after_first,
        )

        dry_stdout = StringIO()
        call_command(COMMAND_NAME, stdout=dry_stdout)
        dry_output = dry_stdout.getvalue()
        self.assertIn("mode: dry-run", dry_output)
        self.assertIn("planned: 0", dry_output)

    def test_delete_count_mismatch_rolls_back_without_reporting_success(self):
        from django.db.models.query import QuerySet

        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        item = create_manual_text_archive_item(title="Count mismatch", body="Body")
        item.tags.add(blocked)
        create_archive_item_person(archive_item=item, person=person)
        through_before = list(
            ArchiveItem.tags.through.objects.order_by("id").values_list(
                "id", "archiveitem_id", "tag_id"
            )
        )
        real_delete = QuerySet.delete

        def lying_delete(self):
            if self.model is ArchiveItem.tags.through:
                real_delete(self)
                return (0, {})
            return real_delete(self)

        with patch.object(QuerySet, "delete", lying_delete):
            with self.assertRaises(CommandError) as caught:
                call_command(COMMAND_NAME, "--apply-relations", stdout=StringIO())
        self.assertIn("delete count mismatch", str(caught.exception))
        self.assertEqual(
            list(
                ArchiveItem.tags.through.objects.order_by("id").values_list(
                    "id", "archiveitem_id", "tag_id"
                )
            ),
            through_before,
        )
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_unexpected_mapped_through_row_after_delete_rolls_back(self):
        from django.db.models.query import QuerySet

        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        item = create_manual_text_archive_item(title="Planned mapped tag", body="Body")
        extra_item = create_manual_text_archive_item(
            title="Unplanned mapped tag", body="Body"
        )
        item.tags.add(blocked)
        create_archive_item_person(archive_item=item, person=person)
        through_before = list(
            ArchiveItem.tags.through.objects.order_by("id").values_list(
                "id", "archiveitem_id", "tag_id"
            )
        )
        self.assertEqual(len(through_before), 1)
        real_delete = QuerySet.delete
        test_case = self

        def delete_then_insert_unplanned(queryset):
            result = real_delete(queryset)
            if queryset.model is ArchiveItem.tags.through:
                test_case.assertEqual(result[0], 1)
                ArchiveItem.tags.through.objects.create(
                    archiveitem_id=extra_item.id,
                    tag_id=blocked.pk,
                )
            return result

        stdout = StringIO()
        with patch.object(QuerySet, "delete", delete_then_insert_unplanned):
            with self.assertRaises(CommandError) as caught:
                call_command(COMMAND_NAME, "--apply-relations", stdout=stdout)
        self.assertIn(
            "mapped through rows remained after delete", str(caught.exception)
        )
        self.assertNotIn("delete count mismatch", str(caught.exception))
        self.assertEqual(
            list(
                ArchiveItem.tags.through.objects.order_by("id").values_list(
                    "id", "archiveitem_id", "tag_id"
                )
            ),
            through_before,
        )
        self.assertFalse(
            ArchiveItem.tags.through.objects.filter(
                archiveitem_id=extra_item.id
            ).exists()
        )
        output = stdout.getvalue()
        self.assertNotIn("mode: apply-relations", output)
        self.assertNotIn("deleted ArchiveItem.tags through rows", output)
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )


class HistoricalPersonTagLegacyWriterTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = User.objects.create_superuser(
            username="historical_tag_admin",
            password="test-pass",
            email="admin@example.com",
        )
        self.site = AdminSite()

    def test_update_ocr_document_tags_rejects_mapped_tag_name(self):
        from documents.services.archive_items import update_ocr_document_tags

        blocked = Tag.objects.create(pk=BLOCKED_TAG_ID, name="blocked-ocr-tag-name")
        _reset_pk_sequence(Tag)
        ordinary = _create_tag(name="ocr-ordinary")
        doc = create_ocr_document(
            title="OCR mapped reuse",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(ordinary)

        with self.assertRaises(ValueError) as caught:
            update_ocr_document_tags(doc, tag_names=[blocked.name, "also-new"])
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        doc.refresh_from_db()
        self.assertEqual(list(doc.tags_m2m.values_list("pk", flat=True)), [ordinary.pk])
        self.assertFalse(Tag.objects.filter(name="also-new").exists())

    def test_update_ocr_document_tags_still_sets_ordinary_tags(self):
        from documents.services.archive_items import update_ocr_document_tags

        Tag.objects.create(pk=BLOCKED_TAG_ID, name="blocked-ocr-unused")
        _reset_pk_sequence(Tag)
        doc = create_ocr_document(
            title="OCR ordinary tags",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        update_ocr_document_tags(doc, tag_names=["fresh-ocr-tag"])
        doc.refresh_from_db()
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["fresh-ocr-tag"],
        )

    def test_document_admin_tags_m2m_choices_omit_mapped_ids(self):
        blocked = Tag.objects.create(pk=BLOCKED_TAG_ID, name="blocked-admin-choice")
        _reset_pk_sequence(Tag)
        ordinary = _create_tag(name="admin-ordinary")
        field = DocumentAdmin(Document, self.site).formfield_for_manytomany(
            Document._meta.get_field("tags_m2m"),
            self.request,
        )
        choice_ids = set(field.queryset.values_list("pk", flat=True))
        self.assertNotIn(blocked.pk, choice_ids)
        self.assertIn(ordinary.pk, choice_ids)

    def test_document_admin_form_rejects_mapped_tag_ids(self):
        blocked = Tag.objects.create(pk=BLOCKED_TAG_ID, name="blocked-admin-form")
        _reset_pk_sequence(Tag)
        ordinary = _create_tag(name="form-ordinary")
        doc = create_ocr_document(
            title="Admin form reject",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        form = DocumentAdminForm(instance=doc)
        form.cleaned_data = {"tags_m2m": [blocked, ordinary]}
        with self.assertRaises(ValidationError) as caught:
            form.clean_tags_m2m()
        self.assertEqual(
            caught.exception.messages[0], HISTORICAL_PERSON_TAG_REUSE_ERROR
        )
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_mapped_tag_admin_deletion_is_blocked(self):
        blocked = Tag.objects.create(pk=BLOCKED_TAG_ID, name="blocked-admin-delete")
        ordinary = _create_tag(name="deletable-ordinary")
        _reset_pk_sequence(Tag)
        admin = TagAdmin(Tag, self.site)

        self.assertFalse(admin.has_delete_permission(self.request, obj=blocked))
        self.assertTrue(admin.has_delete_permission(self.request, obj=ordinary))
        self.assertTrue(admin.has_delete_permission(self.request, obj=None))

        with self.assertRaises(PermissionDenied) as caught:
            admin.delete_model(self.request, blocked)
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_DELETE_ERROR)
        self.assertTrue(Tag.objects.filter(pk=blocked.pk).exists())

        with self.assertRaises(PermissionDenied):
            admin.delete_queryset(
                self.request,
                Tag.objects.filter(pk__in=[blocked.pk, ordinary.pk]),
            )
        self.assertTrue(Tag.objects.filter(pk=blocked.pk).exists())
        self.assertTrue(Tag.objects.filter(pk=ordinary.pk).exists())

        admin.delete_queryset(self.request, Tag.objects.filter(pk=ordinary.pk))
        self.assertFalse(Tag.objects.filter(pk=ordinary.pk).exists())
        self.assertTrue(Tag.objects.filter(pk=blocked.pk).exists())
