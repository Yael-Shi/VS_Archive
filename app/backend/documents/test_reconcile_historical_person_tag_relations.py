"""Management command: reconcile missing ArchiveItemPerson links from the frozen map."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.management.color import no_style
from django.db import connection
from django.test import TestCase

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
    person_id_for_historical_person_name_tag,
)
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Person,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_people import create_archive_item_person
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.historical_person_tag_reconciliation import (
    build_historical_person_tag_reconciliation_plan,
)

BLOCKED_TAG_ID = 29
BLOCKED_PERSON_ID = person_id_for_historical_person_name_tag(BLOCKED_TAG_ID)
COMMAND_NAME = "reconcile_historical_person_tag_relations"


def _reset_pk_sequence(model):
    sql_statements = connection.ops.sequence_reset_sql(no_style(), [model])
    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _seed_frozen_map_rows() -> None:
    for tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID:
        Tag.objects.create(pk=tag_id, name=f"tag-{tag_id}-not-a-lookup-key")
        Person.objects.create(pk=person_id, name=f"person-{person_id}-not-a-lookup-key")
    _reset_pk_sequence(Tag)
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


class ReconcileHistoricalPersonTagRelationsCommandTests(TestCase):
    def test_missing_required_ids_fail_closed_without_writes(self):
        item = create_manual_text_archive_item(title="No map rows", body="Body")
        stdout = StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command(COMMAND_NAME, stdout=stdout)
        self.assertIn("missing Tag ids", str(caught.exception))
        self.assertIn("missing Person ids", str(caught.exception))
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)
        self.assertEqual(item.tags.count(), 0)

    def test_dry_run_identifies_missing_link_and_writes_nothing(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        linked_item = create_manual_text_archive_item(title="Already linked", body="A")
        missing_item = create_manual_text_archive_item(title="Missing person", body="B")
        linked_item.tags.add(blocked)
        missing_item.tags.add(blocked)
        create_archive_item_person(archive_item=linked_item, person=person)

        person_links_before = list(
            ArchiveItemPerson.objects.order_by("id").values_list(
                "id", "archive_item_id", "person_id"
            )
        )
        stdout = StringIO()
        call_command(COMMAND_NAME, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode: dry-run", output)
        self.assertIn("planned: 1", output)
        self.assertIn("created: 0", output)
        missing_row = (BLOCKED_TAG_ID, BLOCKED_PERSON_ID, missing_item.id)
        linked_row = (BLOCKED_TAG_ID, BLOCKED_PERSON_ID, linked_item.id)
        self.assertIn(str(missing_row), output)
        self.assertIn(str(linked_row), output)
        self.assertEqual(
            list(
                ArchiveItemPerson.objects.order_by("id").values_list(
                    "id", "archive_item_id", "person_id"
                )
            ),
            person_links_before,
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=missing_item, person=person
            ).exists()
        )

    def test_apply_creates_only_missing_archive_item_person_links(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        extra_person = Person.objects.create(name="unrelated-already-linked")
        linked_item = create_manual_text_archive_item(title="Has person", body="A")
        missing_item = create_manual_text_archive_item(title="Needs person", body="B")
        linked_item.tags.add(blocked)
        missing_item.tags.add(blocked)
        create_archive_item_person(archive_item=linked_item, person=person)
        create_archive_item_person(archive_item=missing_item, person=extra_person)
        existing_ids = set(ArchiveItemPerson.objects.values_list("id", flat=True))

        stdout = StringIO()
        call_command(COMMAND_NAME, "--apply", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode: apply", output)
        self.assertIn("planned: 1", output)
        self.assertIn("created: 1", output)
        created_row = (BLOCKED_TAG_ID, BLOCKED_PERSON_ID, missing_item.id)
        self.assertIn(str(created_row), output)
        created = ArchiveItemPerson.objects.exclude(id__in=existing_ids)
        self.assertEqual(created.count(), 1)
        link = created.get()
        self.assertEqual(link.archive_item_id, missing_item.id)
        self.assertEqual(link.person_id, BLOCKED_PERSON_ID)
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=missing_item, person=extra_person
            ).exists()
        )
        index = ArchiveItemSearchIndex.objects.get(archive_item_id=missing_item.id)
        self.assertIn(person.name, index.metadata_text)

    def test_repeat_apply_is_idempotent(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        missing_item = create_manual_text_archive_item(title="Idempotent", body="Body")
        missing_item.tags.add(blocked)

        call_command(COMMAND_NAME, "--apply", stdout=StringIO())
        ids_after_first = list(
            ArchiveItemPerson.objects.order_by("id").values_list("id", flat=True)
        )
        stdout = StringIO()
        call_command(COMMAND_NAME, "--apply", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("planned: 0", output)
        self.assertIn("created: 0", output)
        self.assertEqual(
            list(ArchiveItemPerson.objects.order_by("id").values_list("id", flat=True)),
            ids_after_first,
        )

    def test_apply_does_not_mutate_photo_person(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        mapped_person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        item, photo = _create_photo_item(title="Photo drift")
        appearance = Person.objects.create(name="appearance-only")
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        item.tags.add(blocked)
        photo_person_before = list(
            PhotoPerson.objects.order_by("id").values_list(
                "id", "photo_content_id", "person_id"
            )
        )

        call_command(COMMAND_NAME, "--apply", stdout=StringIO())

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
        self.assertFalse(
            PhotoPerson.objects.filter(
                photo_content=photo, person=mapped_person
            ).exists()
        )

    def test_plan_is_id_only_and_ignores_names(self):
        _seed_frozen_map_rows()
        blocked = Tag.objects.get(pk=BLOCKED_TAG_ID)
        mapped_person = Person.objects.get(pk=BLOCKED_PERSON_ID)
        self.assertNotEqual(blocked.name, mapped_person.name)
        decoy = Person.objects.create(name=blocked.name)
        missing_item = create_manual_text_archive_item(title="Name decoy", body="Body")
        missing_item.tags.add(blocked)

        plan = build_historical_person_tag_reconciliation_plan()
        planned_tuples = [row.as_tuple() for row in plan.planned]
        expected = (BLOCKED_TAG_ID, BLOCKED_PERSON_ID, missing_item.id)
        self.assertIn(expected, planned_tuples)
        self.assertFalse(any(row.person_id == decoy.pk for row in plan.planned))

        call_command(COMMAND_NAME, "--apply", stdout=StringIO())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=missing_item, person_id=BLOCKED_PERSON_ID
            ).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=missing_item, person=decoy
            ).exists()
        )
