"""Tests for the approved person-name Tag → Person + ArchiveItemPerson backfill."""

from __future__ import annotations

import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.core.management.color import no_style
from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.db.migrations.executor import MigrationExecutor

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_items import create_ocr_document
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    sync_archive_item_search_index,
)

MIGRATION_MODULE_NAME = (
    "documents.migrations.0055_backfill_person_from_person_name_tags"
)
_migration_module = importlib.import_module(MIGRATION_MODULE_NAME)
APPROVED_PERSON_NAME_TAG_IDS = _migration_module.APPROVED_PERSON_NAME_TAG_IDS
APPROVED_PERSON_NAME_TAGS = _migration_module.APPROVED_PERSON_NAME_TAGS
PersonNameTagBackfillError = _migration_module.PersonNameTagBackfillError
backfill_persons_from_approved_person_name_tags = (
    _migration_module.backfill_persons_from_approved_person_name_tags
)


def _run_backfill():
    backfill_persons_from_approved_person_name_tags(django_apps, schema_editor=None)


def _reset_pk_sequence(model):
    sql_statements = connection.ops.sequence_reset_sql(no_style(), [model])
    with connection.cursor() as cursor:
        for sql in sql_statements:
            cursor.execute(sql)


def _create_approved_tags(*, exclude_ids=(), name_overrides=None):
    name_overrides = name_overrides or {}
    exclude_ids = set(exclude_ids)
    created = {}
    for tag_id, name in APPROVED_PERSON_NAME_TAGS:
        if tag_id in exclude_ids:
            continue
        created[tag_id] = Tag.objects.create(
            pk=tag_id,
            name=name_overrides.get(tag_id, name),
        )
    _reset_pk_sequence(Tag)
    return created


def _create_archive_item(
    *,
    title: str,
    item_type: str,
    visibility: str = ArchiveItem.Visibility.PUBLIC,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        title=title,
        item_type=item_type,
        visibility=visibility,
    )


def _create_photo_content(
    archive_item: ArchiveItem,
    *,
    position: int = 1,
    people_present: str = "",
) -> PhotoContent:
    return PhotoContent.objects.create(
        archive_item=archive_item,
        position=position,
        original_file_key=f"photos/{archive_item.id}/{position}.jpg",
        original_filename=f"{position}.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        people_present=people_present,
    )


def _create_ocr_document(**kwargs) -> Document:
    kwargs.setdefault("title", "OCR source")
    kwargs.setdefault("doc_type", Document.DocType.PDF)
    kwargs.setdefault("text_input_type", Document.TextInputType.PRINTED)
    return create_ocr_document(**kwargs)


class ApprovedPersonNameTagMappingTests(TestCase):
    def test_mapping_encodes_exactly_twenty_nine_tag_id_name_pairs(self):
        self.assertEqual(len(APPROVED_PERSON_NAME_TAGS), 29)
        self.assertEqual(len(APPROVED_PERSON_NAME_TAG_IDS), 29)
        self.assertEqual(len(set(APPROVED_PERSON_NAME_TAG_IDS)), 29)
        self.assertEqual(
            APPROVED_PERSON_NAME_TAG_IDS,
            (
                2,
                4,
                5,
                7,
                8,
                10,
                11,
                14,
                15,
                16,
                19,
                20,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                33,
                34,
                35,
                36,
                37,
                38,
                39,
            ),
        )
        self.assertEqual(
            dict(APPROVED_PERSON_NAME_TAGS),
            {
                2: "רפאל רקנטי",
                4: "פליקס בן זקן",
                5: "יוסף קטאוי",
                7: "אלי פלג",
                8: "אליהו ברכה",
                10: "לאון קסטרו",
                11: "הרב נחום אפנדי",
                14: "מדרכי אביצור",
                15: "הרב דר' משה ונטורה",
                16: "אלי כהן",
                19: "המלך פארוק",
                20: "יולנדה הארמר- גבאי",
                23: "משה מרזוק",
                24: "שמואל עזר",
                25: "איסר הראל",
                26: "רוברט דסה",
                27: "ויקטור לוי",
                28: "מרסל ניניו",
                29: "שלמה הלל",
                30: "שלמה פלטנר",
                31: "מקס בינט",
                32: "אלי נעים",
                33: "יצחק לוי - גבלאוי",
                34: "שמואל שפיטלניק",
                35: "פיליפ נתנזון",
                36: "מוריס זקס",
                37: "אברי אלעד",
                38: "עובדיה דנון",
                39: "מאיר מיוחס",
            },
        )


class PersonNameTagBackfillFailClosedTests(TestCase):
    def test_empty_environment_is_noop(self):
        Tag.objects.create(name="unrelated-topic")
        Person.objects.create(name="pre-existing")

        _run_backfill()

        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 1)

    def test_missing_required_tag_writes_nothing(self):
        _create_approved_tags(exclude_ids={39})
        leftover_tag = Tag.objects.create(pk=1000, name="not-a-person-tag")
        _reset_pk_sequence(Tag)
        item = _create_archive_item(
            title="Should stay unlinked",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        )
        item.tags.add(leftover_tag)
        Person.objects.create(name="pre-existing")

        with self.assertRaises(PersonNameTagBackfillError) as caught:
            _run_backfill()

        self.assertIn("missing", str(caught.exception))
        self.assertIn("39", str(caught.exception))
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)
        self.assertEqual(PhotoPerson.objects.count(), 0)
        self.assertEqual(PersonAlias.objects.count(), 0)

    def test_wrong_tag_name_writes_nothing(self):
        _create_approved_tags(name_overrides={2: "רפאל רקנטי "})
        Person.objects.create(name="pre-existing")

        with self.assertRaises(PersonNameTagBackfillError) as caught:
            _run_backfill()

        self.assertIn("do not match exactly", str(caught.exception))
        self.assertIn("id=2", str(caught.exception))
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_ambiguous_existing_archive_item_person_writes_nothing(self):
        tags = _create_approved_tags()
        item = _create_archive_item(
            title="Ambiguous letter",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        )
        item.tags.add(tags[2])
        first = Person.objects.create(name="רפאל רקנטי")
        second = Person.objects.create(name="רפאל רקנטי")
        ArchiveItemPerson.objects.create(archive_item=item, person=first)
        ArchiveItemPerson.objects.create(archive_item=item, person=second)

        with self.assertRaises(PersonNameTagBackfillError) as caught:
            _run_backfill()

        self.assertIn("Ambiguous Person identity", str(caught.exception))
        self.assertIn("id=2", str(caught.exception))
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(ArchiveItemPerson.objects.count(), 2)


class PersonNameTagBackfillSuccessTests(TestCase):
    def test_creates_person_and_archive_item_person_without_touching_other_graphs(self):
        tags = _create_approved_tags()
        non_person_tag = Tag.objects.create(name="משפחה")

        public_manual = _create_archive_item(
            title="Public manual",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private_video = _create_archive_item(
            title="Private video",
            item_type=ArchiveItem.ItemType.VIDEO,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted_photo = _create_archive_item(
            title="שלמה פלטנר portrait",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        first_photo = _create_photo_content(
            restricted_photo,
            position=1,
            people_present="שלמה פלטנר",
        )
        second_photo = _create_photo_content(restricted_photo, position=2)
        ocr_doc = _create_ocr_document(
            title="OCR with person tag",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ocr_item = ocr_doc.archive_item
        tags_m2m_only_doc = _create_ocr_document(
            title="Legacy document tags only",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        unrelated_item = _create_archive_item(
            title="Non-person tag only",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        )

        public_manual.tags.add(tags[2], tags[16], non_person_tag)
        private_video.tags.add(tags[2])
        restricted_photo.tags.add(tags[30])
        ocr_item.tags.add(tags[7])
        ocr_doc.tags_m2m.add(tags[7], tags[2])
        tags_m2m_only_doc.tags_m2m.add(tags[5])
        unrelated_item.tags.add(non_person_tag)

        same_name_unrelated = Person.objects.create(name="רפאל רקנטי")
        photo_only_person = Person.objects.create(name="שלמה פלטנר")
        PhotoPerson.objects.create(photo_content=first_photo, person=photo_only_person)
        PersonAlias.objects.create(person=same_name_unrelated, name="Raphael")

        tag_ids_before = set(Tag.objects.values_list("pk", flat=True))
        archive_item_tag_pairs_before = set(
            ArchiveItem.tags.through.objects.values_list("archiveitem_id", "tag_id")
        )
        document_tag_pairs_before = set(
            Document.tags_m2m.through.objects.values_list("document_id", "tag_id")
        )
        photo_person_pairs_before = set(
            PhotoPerson.objects.values_list("photo_content_id", "person_id")
        )
        people_present_before = first_photo.people_present
        alias_count_before = PersonAlias.objects.count()
        visibility_before = {
            public_manual.pk: public_manual.visibility,
            private_video.pk: private_video.visibility,
            restricted_photo.pk: restricted_photo.visibility,
        }

        _run_backfill()

        people_by_name = {}
        for person in Person.objects.exclude(
            pk__in=[same_name_unrelated.pk, photo_only_person.pk]
        ):
            people_by_name.setdefault(person.name, []).append(person)

        self.assertEqual(len(people_by_name), 29)
        for _tag_id, expected_name in APPROVED_PERSON_NAME_TAGS:
            created = people_by_name[expected_name]
            self.assertEqual(len(created), 1, expected_name)
            self.assertEqual(created[0].name, expected_name)

        raphael = people_by_name["רפאל רקנטי"][0]
        self.assertNotEqual(raphael.pk, same_name_unrelated.pk)
        platner = people_by_name["שלמה פלטנר"][0]
        self.assertNotEqual(platner.pk, photo_only_person.pk)

        expected_links = {
            (public_manual.pk, raphael.pk),
            (public_manual.pk, people_by_name["אלי כהן"][0].pk),
            (private_video.pk, raphael.pk),
            (restricted_photo.pk, platner.pk),
            (ocr_item.pk, people_by_name["אלי פלג"][0].pk),
        }
        actual_links = set(
            ArchiveItemPerson.objects.values_list("archive_item_id", "person_id")
        )
        self.assertEqual(actual_links, expected_links)

        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=tags_m2m_only_doc.archive_item
            ).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=unrelated_item).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(person=same_name_unrelated).exists()
        )

        self.assertEqual(
            set(PhotoPerson.objects.values_list("photo_content_id", "person_id")),
            photo_person_pairs_before,
        )
        self.assertEqual(PhotoPerson.objects.count(), 1)
        first_photo.refresh_from_db()
        second_photo.refresh_from_db()
        self.assertEqual(first_photo.people_present, people_present_before)
        self.assertEqual(first_photo.people_present, "שלמה פלטנר")
        self.assertEqual(second_photo.people_present, "")
        self.assertEqual(PersonAlias.objects.count(), alias_count_before)
        self.assertEqual(
            set(Tag.objects.values_list("pk", flat=True)),
            tag_ids_before,
        )
        self.assertEqual(
            set(
                ArchiveItem.tags.through.objects.values_list("archiveitem_id", "tag_id")
            ),
            archive_item_tag_pairs_before,
        )
        self.assertEqual(
            set(Document.tags_m2m.through.objects.values_list("document_id", "tag_id")),
            document_tag_pairs_before,
        )
        public_manual.refresh_from_db()
        private_video.refresh_from_db()
        restricted_photo.refresh_from_db()
        self.assertEqual(public_manual.visibility, visibility_before[public_manual.pk])
        self.assertEqual(private_video.visibility, visibility_before[private_video.pk])
        self.assertEqual(
            restricted_photo.visibility,
            visibility_before[restricted_photo.pk],
        )
        self.assertEqual(restricted_photo.photo_contents.count(), 2)

    def test_existing_intended_archive_item_person_is_not_duplicated(self):
        tags = _create_approved_tags()
        item = _create_archive_item(
            title="Already linked",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        )
        extra = _create_archive_item(
            title="Needs link",
            item_type=ArchiveItem.ItemType.VIDEO,
        )
        item.tags.add(tags[2])
        extra.tags.add(tags[2])
        person = Person.objects.create(name="רפאל רקנטי")
        existing = ArchiveItemPerson.objects.create(archive_item=item, person=person)

        _run_backfill()

        self.assertEqual(Person.objects.filter(name="רפאל רקנטי").count(), 1)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(person=person).count(),
            2,
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                pk=existing.pk, archive_item=item, person=person
            ).exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=extra, person=person).exists()
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_reexecution_reuses_intended_person_and_does_not_duplicate_rows(self):
        tags = _create_approved_tags()
        item = _create_archive_item(
            title="Retry source",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        )
        item.tags.add(*tags.values())

        _run_backfill()
        person_ids_after_first = set(Person.objects.values_list("pk", flat=True))
        link_ids_after_first = set(
            ArchiveItemPerson.objects.values_list("pk", flat=True)
        )

        _run_backfill()

        self.assertEqual(
            set(Person.objects.values_list("pk", flat=True)),
            person_ids_after_first,
        )
        self.assertEqual(
            set(ArchiveItemPerson.objects.values_list("pk", flat=True)),
            link_ids_after_first,
        )
        self.assertEqual(Person.objects.count(), 29)
        self.assertEqual(ArchiveItemPerson.objects.count(), 29)
        self.assertEqual(PhotoPerson.objects.count(), 0)
        self.assertEqual(PersonAlias.objects.count(), 0)

    def test_backfill_does_not_rebuild_search_index(self):
        tags = _create_approved_tags()
        item = _create_archive_item(
            title="Search stays on tags",
            item_type=ArchiveItem.ItemType.PHOTO,
        )
        photo = _create_photo_content(item, people_present="שלמה פלטנר")
        item.tags.add(tags[30])
        sync_archive_item_search_index(item.pk)
        index_before = ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk)
        metadata_before = index_before.metadata_text
        updated_before = index_before.updated_at
        self.assertIn("שלמה פלטנר", metadata_before)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked_sync:
            _run_backfill()
            mocked_sync.assert_not_called()

        index_after = ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk)
        self.assertEqual(index_after.metadata_text, metadata_before)
        self.assertEqual(index_after.updated_at, updated_before)
        self.assertEqual(PhotoPerson.objects.count(), 0)
        photo.refresh_from_db()
        self.assertEqual(photo.people_present, "שלמה פלטנר")

        extra_person = Person.objects.create(name="UniqueArchiveItemPersonToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=extra_person)
        built = archive_items_for_search_index_build().get(pk=item.pk)
        content = build_archive_item_search_content(built)
        self.assertIn("שלמה פלטנר", content.metadata_text)
        # Builder now indexes ArchiveItemPerson; migration 0055 still does not
        # rebuild stored rows, so deploy of that search change needs
        # backfill_archive_search_index.
        self.assertIn("UniqueArchiveItemPersonToken", content.metadata_text)


class PersonNameTagBackfillMigrationTests(TestCase):
    def test_migration_depends_on_personalias_head_and_is_irreversible_runpython(self):
        migration_module = importlib.import_module(MIGRATION_MODULE_NAME)
        Migration = migration_module.Migration
        self.assertEqual(Migration.dependencies, [("documents", "0054_personalias")])
        self.assertEqual(len(Migration.operations), 1)
        operation = Migration.operations[0]
        self.assertEqual(
            operation.code,
            migration_module.backfill_persons_from_approved_person_name_tags,
        )
        self.assertIs(
            operation.reverse_code, migration_module.migrations.RunPython.noop
        )


class PersonNameTagBackfillApplyMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_forward_migration_creates_person_from_approved_tag(self):
        migrate_from = [("documents", "0054_personalias")]
        migrate_to = [("documents", "0055_backfill_person_from_person_name_tags")]
        try:
            old_apps = self._migrate(migrate_from)
            TagModel = old_apps.get_model("documents", "Tag")
            ArchiveItemModel = old_apps.get_model("documents", "ArchiveItem")
            PersonModel = old_apps.get_model("documents", "Person")

            tags_by_id = {}
            for tag_id, name in APPROVED_PERSON_NAME_TAGS:
                tags_by_id[tag_id] = TagModel.objects.create(pk=tag_id, name=name)
            _reset_pk_sequence(TagModel)

            item = ArchiveItemModel.objects.create(
                title="Migrated letter",
                item_type="MANUAL_TEXT",
                visibility="public",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
            )
            item.tags.add(tags_by_id[2])
            item_id = item.pk
            self.assertEqual(PersonModel.objects.count(), 0)

            new_apps = self._migrate(migrate_to)
            MigratedPerson = new_apps.get_model("documents", "Person")
            MigratedArchiveItemPerson = new_apps.get_model(
                "documents", "ArchiveItemPerson"
            )
            MigratedPhotoPerson = new_apps.get_model("documents", "PhotoPerson")
            MigratedPersonAlias = new_apps.get_model("documents", "PersonAlias")
            MigratedTag = new_apps.get_model("documents", "Tag")
            MigratedArchiveItem = new_apps.get_model("documents", "ArchiveItem")

            self.assertEqual(MigratedPerson.objects.count(), 29)
            self.assertEqual(
                MigratedPerson.objects.get(name="רפאל רקנטי").name,
                "רפאל רקנטי",
            )
            self.assertEqual(MigratedArchiveItemPerson.objects.count(), 1)
            self.assertEqual(MigratedPhotoPerson.objects.count(), 0)
            self.assertEqual(MigratedPersonAlias.objects.count(), 0)
            self.assertEqual(
                MigratedTag.objects.filter(pk__in=APPROVED_PERSON_NAME_TAG_IDS).count(),
                29,
            )
            migrated_item = MigratedArchiveItem.objects.get(pk=item_id)
            self.assertTrue(migrated_item.tags.filter(pk=2).exists())
        finally:
            self._migrate([("documents", "0055_backfill_person_from_person_name_tags")])
