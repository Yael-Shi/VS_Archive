from __future__ import annotations

import json
from datetime import date
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    Document,
    DocumentMetadata,
    DocumentTextResult,
    Tag,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)


class ReconcileOcrSharedFieldsCommandTests(TestCase):
    def _create_ocr_doc(self, **kwargs):
        defaults = {
            "title": "Test doc",
            "doc_type": Document.DocType.PDF,
            "text_input_type": Document.TextInputType.PRINTED,
            "visibility": Document.Visibility.PRIVATE,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _drift_archive_item(self, doc, **fields):
        item = doc.archive_item
        for name, value in fields.items():
            setattr(item, name, value)
        item.save(update_fields=[*fields.keys(), "updated_at"])

    def test_dry_run_all_in_sync_writes_nothing(self):
        doc = self._create_ocr_doc(title="In sync")
        item = doc.archive_item
        item_updated_at = item.updated_at

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", stdout=stdout)
        output = stdout.getvalue()

        item.refresh_from_db()
        self.assertEqual(item.updated_at, item_updated_at)
        self.assertIn("in_sync: 1", output)
        self.assertIn("with_mismatches: 0", output)

    def test_dry_run_reports_field_drift(self):
        doc = self._create_ocr_doc(title="Document title")
        self._drift_archive_item(doc, title="ArchiveItem title")

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("with_mismatches: 1", output)
        self.assertIn("Document title", output)
        self.assertIn("ArchiveItem title", output)
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.title, "ArchiveItem title")

    def test_dry_run_reports_per_field_counts(self):
        doc_one = self._create_ocr_doc(title="One")
        doc_two = self._create_ocr_doc(title="Two")
        self._drift_archive_item(doc_one, title="Drift one")
        self._drift_archive_item(
            doc_two,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("with_mismatches: 2", output)
        self.assertIn("title: 1", output)
        self.assertIn("metadata_status: 1", output)

    def test_dry_run_reports_visibility_mismatch_separately(self):
        doc = self._create_ocr_doc(
            title="Vis doc",
            visibility=Document.Visibility.PRIVATE,
        )
        self._drift_archive_item(
            doc,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("visibility_mismatches: 1", output)
        self.assertIn("VISIBILITY MISMATCHES", output)
        self.assertIn("visibility:", output)

    def test_apply_reconciles_non_visibility_fields(self):
        doc = self._create_ocr_doc(title="Doc title")
        self._drift_archive_item(doc, title="Stale title")

        call_command("reconcile_ocr_shared_fields", "--apply", stdout=StringIO())

        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.title, "Doc title")

    def test_apply_leaves_visibility_unchanged(self):
        doc = self._create_ocr_doc(
            title="Vis apply",
            visibility=Document.Visibility.PRIVATE,
        )
        self._drift_archive_item(
            doc,
            visibility=ArchiveItem.Visibility.PUBLIC,
            title="Stale title",
        )

        call_command("reconcile_ocr_shared_fields", "--apply", stdout=StringIO())

        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.title, "Vis apply")
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.PUBLIC)

    def test_apply_include_visibility_reconciles_visibility(self):
        doc = self._create_ocr_doc(
            title="Vis full",
            visibility=Document.Visibility.PRIVATE,
        )
        self._drift_archive_item(
            doc,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        call_command(
            "reconcile_ocr_shared_fields",
            "--apply",
            "--include-visibility",
            stdout=StringIO(),
        )

        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.PRIVATE)

    def test_apply_is_idempotent(self):
        doc = self._create_ocr_doc(title="Idempotent")
        self._drift_archive_item(doc, title="Drift")

        call_command("reconcile_ocr_shared_fields", "--apply", stdout=StringIO())
        doc.archive_item.refresh_from_db()
        item_updated_at = doc.archive_item.updated_at

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", "--apply", stdout=stdout)
        output = stdout.getvalue()

        doc.archive_item.refresh_from_db()
        self.assertIn("with_mismatches: 0", output)
        self.assertEqual(doc.archive_item.updated_at, item_updated_at)

    def test_manual_text_ignored(self):
        create_manual_text_archive_item(title="Manual only", body="Hello")
        doc = self._create_ocr_doc(title="OCR only")

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("documents_checked: 1", output)
        self.assertIn("with_mismatches: 0", output)

    def test_document_id_filter(self):
        doc_one = self._create_ocr_doc(title="Filter one")
        doc_two = self._create_ocr_doc(title="Filter two")
        self._drift_archive_item(doc_one, title="Drift one")
        self._drift_archive_item(doc_two, title="Drift two")

        stdout = StringIO()
        call_command(
            "reconcile_ocr_shared_fields",
            "--document-id",
            str(doc_one.pk),
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("documents_checked: 1", output)
        self.assertIn("Drift one", output)
        self.assertNotIn("Drift two", output)
        doc_two.archive_item.refresh_from_db()
        self.assertEqual(doc_two.archive_item.title, "Drift two")

    def test_apply_does_not_mutate_document_fields(self):
        doc = self._create_ocr_doc(
            title="Doc unchanged",
            visibility=Document.Visibility.PUBLIC,
            metadata_status=Document.MetadataStatus.COMPLETED,
            date_precision=Document.DatePrecision.YEAR,
        )
        self._drift_archive_item(
            doc,
            title="Drift",
            visibility=ArchiveItem.Visibility.PRIVATE,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        )
        before = {
            "title": doc.title,
            "visibility": doc.visibility,
            "metadata_status": doc.metadata_status,
            "date_precision": doc.date_precision,
            "date_start": doc.date_start,
            "date_end": doc.date_end,
            "updated_at": doc.updated_at,
        }

        call_command(
            "reconcile_ocr_shared_fields",
            "--apply",
            "--include-visibility",
            stdout=StringIO(),
        )

        doc.refresh_from_db()
        self.assertEqual(doc.title, before["title"])
        self.assertEqual(doc.visibility, before["visibility"])
        self.assertEqual(doc.metadata_status, before["metadata_status"])
        self.assertEqual(doc.date_precision, before["date_precision"])
        self.assertEqual(doc.date_start, before["date_start"])
        self.assertEqual(doc.date_end, before["date_end"])
        self.assertEqual(doc.updated_at, before["updated_at"])

    def test_apply_does_not_touch_catalog_tags_or_text_results(self):
        doc = self._create_ocr_doc(title="Side effects")
        self._drift_archive_item(doc, title="Drift")
        DocumentMetadata.objects.create(
            document=doc,
            donor="Donor",
            collection="Collection",
            original_location="Loc",
            notes="Notes",
        )
        doc.category_event = "Event"
        doc.save(update_fields=["category_event", "updated_at"])
        tag = Tag.objects.create(name="heritage")
        doc.tags_m2m.add(tag)
        text_result = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="test-engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="sample text",
        )
        meta_before = DocumentMetadata.objects.get(document=doc)
        tag_ids_before = list(doc.tags_m2m.values_list("pk", flat=True))

        call_command("reconcile_ocr_shared_fields", "--apply", stdout=StringIO())

        meta_after = DocumentMetadata.objects.get(document=doc)
        doc.refresh_from_db()
        text_result.refresh_from_db()
        self.assertEqual(meta_after.donor, meta_before.donor)
        self.assertEqual(meta_after.collection, meta_before.collection)
        self.assertEqual(doc.category_event, "Event")
        self.assertEqual(list(doc.tags_m2m.values_list("pk", flat=True)), tag_ids_before)
        self.assertEqual(text_result.text, "sample text")

    def test_json_output_is_parseable(self):
        doc = self._create_ocr_doc(title="JSON doc")
        self._drift_archive_item(
            doc,
            title="Drift",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        stdout = StringIO()
        call_command("reconcile_ocr_shared_fields", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["mode"], "dry-run")
        self.assertIn("summary", payload)
        self.assertEqual(payload["summary"]["documents_checked"], 1)
        self.assertEqual(payload["summary"]["with_mismatches"], 1)
        self.assertIn("mismatched_rows", payload)
        self.assertEqual(len(payload["mismatched_rows"]), 1)
        row = payload["mismatched_rows"][0]
        self.assertEqual(row["document_id"], doc.pk)
        self.assertEqual(row["archive_item_id"], doc.archive_item_id)
        self.assertIn("title", row["fields"])
        self.assertIn("visibility", row["fields"])

    def test_include_visibility_without_apply_raises(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "reconcile_ocr_shared_fields",
                "--include-visibility",
                stdout=StringIO(),
            )
        self.assertIn("--include-visibility requires --apply", str(ctx.exception))

    def test_apply_with_dates(self):
        doc = self._create_ocr_doc(
            title="Dates",
            date_start=date(1920, 1, 1),
            date_end=date(1925, 12, 31),
            date_precision=Document.DatePrecision.RANGE,
        )
        self._drift_archive_item(
            doc,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        )

        call_command("reconcile_ocr_shared_fields", "--apply", stdout=StringIO())

        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.date_start, date(1920, 1, 1))
        self.assertEqual(doc.archive_item.date_end, date(1925, 12, 31))
        self.assertEqual(
            doc.archive_item.date_precision,
            ArchiveItem.DatePrecision.RANGE,
        )
