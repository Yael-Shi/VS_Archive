from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    Document,
    DocumentMetadata,
    Tag,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    update_archive_item_discovery_metadata,
)


class BackfillArchiveDiscoveryMetadataCommandTests(TestCase):
    def _create_ocr_doc(self, **kwargs):
        defaults = {
            "title": "Test doc",
            "doc_type": Document.DocType.PDF,
            "text_input_type": Document.TextInputType.PRINTED,
            "visibility": Document.Visibility.PRIVATE,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def test_dry_run_copies_nothing(self):
        doc = self._create_ocr_doc(title="Dry run doc", category_event="יהדות מצרים")
        tag = Tag.objects.create(name="family")
        doc.tags_m2m.add(tag)

        stdout = StringIO()
        call_command("backfill_archive_discovery_metadata", stdout=stdout)
        output = stdout.getvalue()

        doc.refresh_from_db()
        item = doc.archive_item
        self.assertEqual(list(item.tags.all()), [])
        self.assertEqual(list(item.categories.all()), [])
        self.assertIn("dry run", output.lower())
        self.assertIn("tag_links_to_add: 1", output)
        self.assertEqual(doc.category_event, "יהדות מצרים")
        self.assertEqual(list(doc.tags_m2m.values_list("name", flat=True)), ["family"])

    def test_apply_copies_legacy_tags_to_archive_item(self):
        doc = self._create_ocr_doc(title="Tags doc")
        tag_one = Tag.objects.create(name="cairo")
        tag_two = Tag.objects.create(name="family")
        doc.tags_m2m.add(tag_one, tag_two)

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        item = doc.archive_item
        self.assertCountEqual(
            list(item.tags.values_list("name", flat=True)),
            ["cairo", "family"],
        )
        self.assertCountEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["cairo", "family"],
        )

    def test_apply_maps_category_event_to_archive_item_categories(self):
        doc = self._create_ocr_doc(
            title="Category doc",
            category_event="יהדות מצרים",
        )

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        item = doc.archive_item
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["יהדות מצרים"],
        )
        category = ArchiveCategory.objects.get(name="יהדות מצרים")
        self.assertTrue(category.slug)

    def test_apply_twice_is_idempotent(self):
        doc = self._create_ocr_doc(
            title="Idempotent",
            category_event="הפרשה",
        )
        tag = Tag.objects.create(name="heritage")
        doc.tags_m2m.add(tag)

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )
        item = doc.archive_item
        tag_ids_after_first = list(item.tags.values_list("pk", flat=True))
        category_ids_after_first = list(item.categories.values_list("pk", flat=True))

        stdout = StringIO()
        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=stdout,
        )
        output = stdout.getvalue()

        item.refresh_from_db()
        self.assertEqual(
            list(item.tags.values_list("pk", flat=True)), tag_ids_after_first
        )
        self.assertEqual(
            list(item.categories.values_list("pk", flat=True)),
            category_ids_after_first,
        )
        self.assertIn("tag_links_to_add: 0", output)
        self.assertIn("category_links_to_add: 0", output)

    def test_existing_archive_item_tags_preserved_and_legacy_tags_added(self):
        doc = self._create_ocr_doc(title="Merge tags")
        existing_tag = Tag.objects.create(name="existing-tag")
        legacy_tag = Tag.objects.create(name="legacy-tag")
        doc.archive_item.tags.add(existing_tag)
        doc.tags_m2m.add(legacy_tag)

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        self.assertCountEqual(
            list(doc.archive_item.tags.values_list("name", flat=True)),
            ["existing-tag", "legacy-tag"],
        )

    def test_existing_archive_item_categories_preserved_and_category_event_added(self):
        doc = self._create_ocr_doc(
            title="Merge categories",
            category_event="legacy-category",
        )
        existing = ArchiveCategory.objects.create(
            name="existing-category",
            slug="existing-category",
        )
        doc.archive_item.categories.add(existing)

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        self.assertCountEqual(
            list(doc.archive_item.categories.values_list("name", flat=True)),
            ["existing-category", "legacy-category"],
        )

    def test_document_tags_m2m_not_cleared_or_changed(self):
        doc = self._create_ocr_doc(title="Keep legacy tags")
        tag = Tag.objects.create(name="keep-me")
        doc.tags_m2m.add(tag)
        tag_ids_before = list(doc.tags_m2m.values_list("pk", flat=True))

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        doc.refresh_from_db()
        self.assertEqual(
            list(doc.tags_m2m.values_list("pk", flat=True)), tag_ids_before
        )

    def test_document_category_event_not_cleared_or_changed(self):
        doc = self._create_ocr_doc(
            title="Keep category_event",
            category_event="unchanged-event",
        )

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        doc.refresh_from_db()
        self.assertEqual(doc.category_event, "unchanged-event")

    def test_apply_strips_category_event_whitespace(self):
        doc = self._create_ocr_doc(
            title="Whitespace category",
            category_event="  יהדות מצרים  ",
        )

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        doc.refresh_from_db()
        self.assertEqual(doc.category_event, "  יהדות מצרים  ")
        self.assertEqual(
            list(doc.archive_item.categories.values_list("name", flat=True)),
            ["יהדות מצרים"],
        )
        self.assertTrue(ArchiveCategory.objects.filter(name="יהדות מצרים").exists())
        self.assertFalse(
            ArchiveCategory.objects.filter(name="  יהדות מצרים  ").exists()
        )

    def test_dry_run_counts_unique_categories_to_create(self):
        self._create_ocr_doc(title="Doc one", category_event="shared-category")
        self._create_ocr_doc(title="Doc two", category_event="shared-category")

        stdout = StringIO()
        call_command("backfill_archive_discovery_metadata", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("categories_to_create: 1", output)
        self.assertIn("category_links_to_add: 2", output)
        self.assertEqual(output.count("(new ArchiveCategory)"), 1)

        json_stdout = StringIO()
        call_command(
            "backfill_archive_discovery_metadata",
            "--json",
            stdout=json_stdout,
        )
        payload = json.loads(json_stdout.getvalue())
        category_rows = [row for row in payload["rows"] if row["category_link_to_add"]]
        self.assertEqual(len(category_rows), 2)
        self.assertEqual(
            sum(1 for row in category_rows if row["category_would_be_created"]),
            1,
        )
        self.assertTrue(all(row["category_link_to_add"] for row in category_rows))

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        category = ArchiveCategory.objects.get(name="shared-category")
        linked_item_ids = list(
            category.archive_items.order_by("id").values_list("id", flat=True)
        )
        self.assertEqual(len(linked_item_ids), 2)
        self.assertEqual(
            ArchiveCategory.objects.filter(name="shared-category").count(), 1
        )

    def test_archive_item_events_unchanged(self):
        doc = self._create_ocr_doc(
            title="Events unchanged",
            category_event="looks-like-event",
        )
        event = ArchiveEvent.objects.create(
            name="existing-event", slug="existing-event"
        )
        doc.archive_item.events.add(event)
        event_ids_before = list(doc.archive_item.events.values_list("pk", flat=True))

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        doc.archive_item.refresh_from_db()
        self.assertEqual(
            list(doc.archive_item.events.values_list("pk", flat=True)),
            event_ids_before,
        )
        self.assertEqual(
            list(doc.archive_item.events.values_list("name", flat=True)),
            ["existing-event"],
        )

    def test_manual_text_items_ignored(self):
        manual_item = create_manual_text_archive_item(title="Manual only", body="Hello")
        manual_item.tags.add(Tag.objects.create(name="manual-tag"))
        doc = self._create_ocr_doc(title="OCR only")
        tag = Tag.objects.create(name="ocr-tag")
        doc.tags_m2m.add(tag)

        stdout = StringIO()
        call_command("backfill_archive_discovery_metadata", stdout=stdout)
        output = stdout.getvalue()

        manual_item.refresh_from_db()
        self.assertEqual(
            list(manual_item.tags.values_list("name", flat=True)), ["manual-tag"]
        )
        self.assertIn("scanned_ocr_documents: 1", output)

    def test_documents_without_backfill_needs_report_zero_adds(self):
        doc = self._create_ocr_doc(title="Empty legacy")
        stdout = StringIO()
        call_command("backfill_archive_discovery_metadata", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("scanned_ocr_documents: 1", output)
        self.assertIn("documents_missing_archive_item: 0", output)
        self.assertIn("tag_links_to_add: 0", output)
        self.assertIn("category_links_to_add: 0", output)
        self.assertEqual(list(doc.archive_item.tags.all()), [])

    def test_reuses_existing_archive_category_by_exact_name(self):
        doc = self._create_ocr_doc(
            title="Reuse category",
            category_event="shared-name",
        )
        ArchiveCategory.objects.create(name="shared-name", slug="shared-name")

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        self.assertEqual(ArchiveCategory.objects.filter(name="shared-name").count(), 1)
        self.assertEqual(
            list(doc.archive_item.categories.values_list("name", flat=True)),
            ["shared-name"],
        )

    def test_apply_does_not_touch_document_metadata_or_discovery_edit_fields(self):
        doc = self._create_ocr_doc(title="Side effects")
        DocumentMetadata.objects.create(
            document=doc,
            donor="Donor",
            collection="Collection",
            original_location="Loc",
            notes="Notes",
        )
        update_archive_item_discovery_metadata(
            doc.archive_item,
            category_names=["edited-category"],
            event_names=["edited-event"],
            tag_names=["edited-tag"],
        )
        doc.category_event = "legacy-category"
        doc.save(update_fields=["category_event", "updated_at"])
        legacy_tag = Tag.objects.create(name="legacy-only")
        doc.tags_m2m.add(legacy_tag)
        meta_before = DocumentMetadata.objects.get(document=doc)
        events_before = list(doc.archive_item.events.values_list("name", flat=True))
        categories_before = list(
            doc.archive_item.categories.values_list("name", flat=True)
        )
        discovery_tags_before = list(
            doc.archive_item.tags.values_list("name", flat=True)
        )

        call_command(
            "backfill_archive_discovery_metadata",
            "--apply",
            stdout=StringIO(),
        )

        meta_after = DocumentMetadata.objects.get(document=doc)
        doc.archive_item.refresh_from_db()
        self.assertEqual(meta_after.donor, meta_before.donor)
        self.assertEqual(meta_after.collection, meta_before.collection)
        self.assertEqual(
            list(doc.archive_item.events.values_list("name", flat=True)),
            events_before,
        )
        self.assertCountEqual(
            list(doc.archive_item.categories.values_list("name", flat=True)),
            [*categories_before, "legacy-category"],
        )
        self.assertCountEqual(
            list(doc.archive_item.tags.values_list("name", flat=True)),
            [*discovery_tags_before, "legacy-only"],
        )

    def test_json_output_is_parseable(self):
        doc = self._create_ocr_doc(
            title="JSON doc",
            category_event="json-category",
        )
        doc.tags_m2m.add(Tag.objects.create(name="json-tag"))

        stdout = StringIO()
        call_command(
            "backfill_archive_discovery_metadata",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["mode"], "dry-run")
        self.assertIn("summary", payload)
        self.assertEqual(payload["summary"]["scanned_ocr_documents"], 1)
        self.assertEqual(payload["summary"]["documents_missing_archive_item"], 0)
        self.assertEqual(payload["summary"]["tag_links_to_add"], 1)
        self.assertEqual(payload["summary"]["category_links_to_add"], 1)
        self.assertIn("rows", payload)
        self.assertEqual(payload["rows"][0]["document_id"], doc.pk)
        self.assertEqual(payload["rows"][0]["archive_item_id"], doc.archive_item_id)

    def test_document_id_filter(self):
        doc_one = self._create_ocr_doc(title="Filter one", category_event="cat-one")
        doc_two = self._create_ocr_doc(title="Filter two", category_event="cat-two")

        stdout = StringIO()
        call_command(
            "backfill_archive_discovery_metadata",
            "--document-id",
            str(doc_one.pk),
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("scanned_ocr_documents: 1", output)
        self.assertIn("category to add: 'cat-one'", output)
        self.assertNotIn("cat-two", output)
        self.assertEqual(list(doc_two.archive_item.categories.all()), [])
