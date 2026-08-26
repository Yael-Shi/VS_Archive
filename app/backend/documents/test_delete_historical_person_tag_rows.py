"""D2b: delete the 29 frozen historical person-name Tag rows only."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_RECORDS,
    historical_person_name_tag_ids,
    person_id_for_historical_person_name_tag,
)
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveMetadataSuggestion,
    Document,
    Person,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_discovery_metadata_validation import (
    HISTORICAL_PERSON_TAG_REUSE_ERROR,
)
from documents.services.archive_item_people import create_archive_item_person
from documents.services.archive_item_presentation import person_public_page_url
from documents.services.archive_items import (
    _get_or_create_tag_by_name,
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.test_historical_person_tag_reuse import (
    _create_tag,
    _reset_pk_sequence,
)

COMMAND_NAME = "delete_historical_person_tag_rows"
CLEANUP_COMMAND_NAME = "cleanup_historical_person_tags"
RECONCILE_COMMAND_NAME = "reconcile_historical_person_tag_relations"
RETIRED_NAME = "שלמה הלל"
ORDINARY_NAME = "ordinary-keep-d2b"
FIRST_MAPPED_TAG_ID = HISTORICAL_PERSON_NAME_TAG_RECORDS[0][0]
FIRST_MAPPED_PERSON_ID = HISTORICAL_PERSON_NAME_TAG_RECORDS[0][1]
DOCUMENT_MAPPED_TAG_ID = 8


def _seed_frozen_identity_rows() -> None:
    for tag_id, person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
        Tag.objects.create(pk=tag_id, name=name)
        Person.objects.create(pk=person_id, name=f"person-{person_id}-not-a-lookup-key")
    _reset_pk_sequence(Tag)
    _reset_pk_sequence(Person)


def _seed_frozen_person_rows() -> None:
    for _tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
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


class DeleteHistoricalPersonTagRowsCommandTests(TestCase):
    def test_dry_run_default_writes_nothing(self):
        _seed_frozen_identity_rows()
        ordinary = _create_tag(name=ORDINARY_NAME)
        mapped_ids = historical_person_name_tag_ids()
        stdout = StringIO()
        call_command(COMMAND_NAME, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode: dry-run", output)
        self.assertIn("state: all_present", output)
        self.assertIn("planned: 29", output)
        self.assertIn("deleted: 0", output)
        self.assertIn("django_delete_total: 0", output)
        self.assertIn("remaining_mapped_tag_rows: 29", output)
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 29)
        self.assertTrue(Tag.objects.filter(pk=ordinary.pk, name=ORDINARY_NAME).exists())

    def test_apply_deletes_exactly_twenty_nine_tag_rows(self):
        _seed_frozen_identity_rows()
        ordinary = _create_tag(name=ORDINARY_NAME)
        mapped_ids = historical_person_name_tag_ids()
        stdout = StringIO()
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as sync_indexes:
            call_command(COMMAND_NAME, "--apply-rows", stdout=stdout)
            sync_indexes.assert_not_called()
        output = stdout.getvalue()

        self.assertIn("mode: apply-rows", output)
        self.assertIn("state: all_present", output)
        self.assertIn("planned: 29", output)
        self.assertIn("deleted: 29", output)
        self.assertIn("django_delete_total: 29", output)
        self.assertIn(f"django_delete_per_model: {{'{Tag._meta.label}': 29}}", output)
        self.assertIn("remaining_mapped_tag_rows: 0", output)
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)
        self.assertTrue(Tag.objects.filter(pk=ordinary.pk, name=ORDINARY_NAME).exists())
        for tag_id, _person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
            self.assertIn(str((tag_id, name)), output)

    def test_apply_rolls_back_on_delete_count_mismatch(self):
        from django.db.models.query import QuerySet

        _seed_frozen_identity_rows()
        mapped_ids = historical_person_name_tag_ids()
        real_delete = QuerySet.delete

        def lying_delete(self):
            if self.model is Tag:
                real_delete(self)
                return (29, {Tag._meta.label: 29, "documents.ArchiveItem_tags": 1})
            return real_delete(self)

        with patch.object(QuerySet, "delete", lying_delete):
            with self.assertRaises(CommandError) as caught:
                call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertIn("delete count mismatch", str(caught.exception))
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 29)

    def test_apply_rolls_back_when_mapped_rows_remain_after_reported_delete(self):
        from django.db.models.query import QuerySet

        _seed_frozen_identity_rows()
        mapped_ids = historical_person_name_tag_ids()
        real_delete = QuerySet.delete

        def skip_tag_delete(self):
            if self.model is Tag:
                return (29, {Tag._meta.label: 29})
            return real_delete(self)

        with patch.object(QuerySet, "delete", skip_tag_delete):
            with self.assertRaises(CommandError) as caught:
                call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertIn("mapped Tag rows remained after delete", str(caught.exception))
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 29)

    def test_all_absent_is_idempotent_success(self):
        _seed_frozen_person_rows()
        mapped_ids = historical_person_name_tag_ids()
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)

        dry_stdout = StringIO()
        call_command(COMMAND_NAME, stdout=dry_stdout)
        dry_output = dry_stdout.getvalue()
        self.assertIn("mode: dry-run", dry_output)
        self.assertIn("state: all_absent", dry_output)
        self.assertIn("planned: 0", dry_output)
        self.assertIn("deleted: 0", dry_output)

        apply_stdout = StringIO()
        call_command(COMMAND_NAME, "--apply-rows", stdout=apply_stdout)
        apply_output = apply_stdout.getvalue()
        self.assertIn("mode: apply-rows", apply_output)
        self.assertIn("state: all_absent", apply_output)
        self.assertIn("planned: 0", apply_output)
        self.assertIn("deleted: 0", apply_output)
        self.assertIn("django_delete_total: 0", apply_output)
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)

        repeat_stdout = StringIO()
        call_command(COMMAND_NAME, "--apply-rows", stdout=repeat_stdout)
        self.assertIn("planned: 0", repeat_stdout.getvalue())
        self.assertEqual(Tag.objects.filter(pk__in=mapped_ids).count(), 0)

    def test_partial_mapped_tag_ids_fail_closed(self):
        _seed_frozen_person_rows()
        for tag_id, _person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
            if tag_id == FIRST_MAPPED_TAG_ID:
                continue
            Tag.objects.create(pk=tag_id, name=name)
        _reset_pk_sequence(Tag)

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=StringIO())
        self.assertIn("missing Tag ids", str(caught.exception))
        self.assertIn(str(FIRST_MAPPED_TAG_ID), str(caught.exception))
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(),
            28,
        )

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(),
            28,
        )

    def test_wrong_name_on_frozen_id_fail_closed(self):
        _seed_frozen_identity_rows()
        mapped = Tag.objects.get(pk=FIRST_MAPPED_TAG_ID)
        mapped.name = "wrong-name-for-mapped-id"
        mapped.save(update_fields=["name"])

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=StringIO())
        self.assertIn("mapped Tag name mismatch", str(caught.exception))
        self.assertIn(str(FIRST_MAPPED_TAG_ID), str(caught.exception))
        self.assertTrue(Tag.objects.filter(pk=FIRST_MAPPED_TAG_ID).exists())

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 29
        )

    def test_retired_name_on_non_mapped_pk_fail_closed(self):
        _seed_frozen_person_rows()
        rogue = _create_tag(name=RETIRED_NAME)
        self.assertNotIn(rogue.pk, historical_person_name_tag_ids())

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=StringIO())
        self.assertIn(
            "retired historical Tag names on non-mapped ids",
            str(caught.exception),
        )
        self.assertIn(str(rogue.pk), str(caught.exception))
        self.assertTrue(Tag.objects.filter(pk=rogue.pk, name=RETIRED_NAME).exists())

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertTrue(Tag.objects.filter(pk=rogue.pk, name=RETIRED_NAME).exists())

    def test_mapped_archiveitem_through_rows_fail_closed(self):
        _seed_frozen_identity_rows()
        blocked = Tag.objects.get(pk=FIRST_MAPPED_TAG_ID)
        item = create_manual_text_archive_item(title="Mapped leftover", body="Body")
        item.tags.add(blocked)

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertIn(
            "mapped ArchiveItem.tags through rows exist", str(caught.exception)
        )
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(),
            29,
        )
        self.assertTrue(
            ArchiveItem.tags.through.objects.filter(
                archiveitem_id=item.id, tag_id=FIRST_MAPPED_TAG_ID
            ).exists()
        )

    def test_mapped_document_through_rows_fail_closed(self):
        _seed_frozen_identity_rows()
        mapped_doc_tag = Tag.objects.get(pk=DOCUMENT_MAPPED_TAG_ID)
        doc = create_ocr_document(
            title="Doc mapped leftover",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(mapped_doc_tag)

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertIn(
            "mapped Document.tags_m2m through rows exist", str(caught.exception)
        )
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 29
        )
        self.assertTrue(
            Document.tags_m2m.through.objects.filter(
                document_id=doc.id, tag_id=DOCUMENT_MAPPED_TAG_ID
            ).exists()
        )

    def test_pending_retired_name_inventory_fail_closed(self):
        _seed_frozen_identity_rows()
        item = create_manual_text_archive_item(title="Pending retired", body="Body")
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_tags=RETIRED_NAME,
        )

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertIn(
            "pending retired-name ArchiveMetadataSuggestion ids",
            str(caught.exception),
        )
        self.assertIn(str(suggestion.id), str(caught.exception))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.PENDING)
        self.assertEqual(suggestion.suggested_tags, RETIRED_NAME)
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 29
        )

    def test_missing_person_ids_fail_closed(self):
        for tag_id, person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
            Tag.objects.create(pk=tag_id, name=name)
            if person_id == FIRST_MAPPED_PERSON_ID:
                continue
            Person.objects.create(
                pk=person_id, name=f"person-{person_id}-not-a-lookup-key"
            )
        _reset_pk_sequence(Tag)
        _reset_pk_sequence(Person)

        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=StringIO())
        self.assertIn("missing Person ids", str(caught.exception))
        self.assertIn(str(FIRST_MAPPED_PERSON_ID), str(caught.exception))
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 29
        )

        with self.assertRaises(CommandError):
            call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 29
        )

    def test_apply_preserves_ordinary_tag_person_aip_and_photoperson(self):
        _seed_frozen_identity_rows()
        ordinary = _create_tag(name=ORDINARY_NAME)
        mapped_person = Person.objects.get(pk=FIRST_MAPPED_PERSON_ID)
        item = create_manual_text_archive_item(title="Keep AIP", body="Body")
        create_archive_item_person(archive_item=item, person=mapped_person)
        photo_item, photo = _create_photo_item(title="Keep appearance")
        appearance = Person.objects.create(name="appearance-only")
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        person_ids_before = list(
            Person.objects.order_by("id").values_list("id", "name")
        )
        aip_before = list(
            ArchiveItemPerson.objects.order_by("id").values_list(
                "id", "archive_item_id", "person_id"
            )
        )
        photo_person_before = list(
            PhotoPerson.objects.order_by("id").values_list(
                "id", "photo_content_id", "person_id"
            )
        )

        call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())

        self.assertTrue(Tag.objects.filter(pk=ordinary.pk, name=ORDINARY_NAME).exists())
        self.assertEqual(
            list(Person.objects.order_by("id").values_list("id", "name")),
            person_ids_before,
        )
        self.assertEqual(
            list(
                ArchiveItemPerson.objects.order_by("id").values_list(
                    "id", "archive_item_id", "person_id"
                )
            ),
            aip_before,
        )
        self.assertEqual(
            list(
                PhotoPerson.objects.order_by("id").values_list(
                    "id", "photo_content_id", "person_id"
                )
            ),
            photo_person_before,
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=mapped_person
            ).exists()
        )
        self.assertTrue(photo_item.pk)

    def test_d1_and_reconcile_succeed_planned_zero_after_apply(self):
        _seed_frozen_identity_rows()
        call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())

        cleanup_stdout = StringIO()
        call_command(CLEANUP_COMMAND_NAME, stdout=cleanup_stdout)
        cleanup_output = cleanup_stdout.getvalue()
        self.assertIn("planned: 0", cleanup_output)
        apply_cleanup = StringIO()
        call_command(CLEANUP_COMMAND_NAME, "--apply-relations", stdout=apply_cleanup)
        self.assertIn("planned: 0", apply_cleanup.getvalue())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 0
        )

        reconcile_stdout = StringIO()
        call_command(RECONCILE_COMMAND_NAME, stdout=reconcile_stdout)
        self.assertIn("planned: 0", reconcile_stdout.getvalue())
        apply_reconcile = StringIO()
        call_command(RECONCILE_COMMAND_NAME, "--apply", stdout=apply_reconcile)
        self.assertIn("planned: 0", apply_reconcile.getvalue())
        self.assertIn("created: 0", apply_reconcile.getvalue())
        self.assertEqual(
            Tag.objects.filter(pk__in=historical_person_name_tag_ids()).count(), 0
        )
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_mapped_browse_redirect_survives_row_deletion(self):
        _seed_frozen_identity_rows()
        call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertFalse(Tag.objects.filter(pk=FIRST_MAPPED_TAG_ID).exists())
        mapped_person_id = person_id_for_historical_person_name_tag(FIRST_MAPPED_TAG_ID)
        self.assertEqual(mapped_person_id, FIRST_MAPPED_PERSON_ID)

        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": FIRST_MAPPED_TAG_ID})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"], person_public_page_url(FIRST_MAPPED_PERSON_ID)
        )

    def test_retired_name_cannot_be_recreated_after_apply(self):
        _seed_frozen_identity_rows()
        call_command(COMMAND_NAME, "--apply-rows", stdout=StringIO())
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        with self.assertRaises(ValueError) as caught:
            _get_or_create_tag_by_name(RETIRED_NAME)
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
