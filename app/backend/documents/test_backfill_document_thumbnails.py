"""Management command tests for backfill_document_thumbnails."""

from __future__ import annotations

import json
from io import BytesIO, StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image

from documents.models import Document, DocumentSourceFile
from documents.s3 import (
    build_document_source_file_s3_key,
    build_document_thumbnail_s3_key,
)
from documents.services.archive_items import create_ocr_document
from documents.services.document_thumbnail_backfill import (
    build_document_thumbnail_backfill_report,
)
from documents.services.image_thumbnail import THUMBNAIL_JPEG_MIME


def _solid_jpeg_bytes(
    width: int,
    height: int,
    *,
    color: tuple[int, int, int] = (10, 20, 30),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _create_uploaded_image_document(
    *,
    title: str = "Backfill document",
    thumbnail_file_key: str = "",
    upload_status: str = Document.UploadStatus.UPLOADED,
    doc_type: str = Document.DocType.IMAGE,
    create_source: bool = True,
    source_upload_status: str = DocumentSourceFile.UploadStatus.UPLOADED,
    source_s3_key: str | None = None,
    order_index: int = 0,
) -> Document:
    doc = create_ocr_document(
        title=title,
        doc_type=doc_type,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        file_original_name="page-1.jpg",
        mime_type="image/jpeg",
        size_bytes=4096,
    )
    doc.upload_status = upload_status
    doc.thumbnail_file_key = thumbnail_file_key
    doc.save(update_fields=["upload_status", "thumbnail_file_key", "updated_at"])

    if create_source:
        key = source_s3_key or build_document_source_file_s3_key(
            doc.id,
            order_index,
            "image/jpeg",
        )
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=key,
            file_original_name="page-1.jpg",
            mime_type="image/jpeg",
            size_bytes=4096,
            upload_status=source_upload_status,
        )
    return doc


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class BackfillDocumentThumbnailsCommandTests(TestCase):
    def test_dry_run_finds_candidates_without_s3_calls_or_db_changes(self):
        candidate = _create_uploaded_image_document(title="Needs thumbnail")
        completed = _create_uploaded_image_document(title="Already done")
        completed.thumbnail_file_key = build_document_thumbnail_s3_key(completed.id)
        completed.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        with (
            patch(
                "documents.services.document_thumbnail_backfill.generate_and_persist_document_thumbnail"
            ) as mock_generate,
            patch("documents.services.document_thumbnail.get_object_bytes") as mock_get,
            patch("documents.services.document_thumbnail.put_object_bytes") as mock_put,
        ):
            call_command("backfill_document_thumbnails", stdout=stdout)

        output = stdout.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertIn(f"document_id={candidate.id}", output)
        self.assertNotIn(f"document_id={completed.id}", output)
        mock_generate.assert_not_called()
        mock_get.assert_not_called()
        mock_put.assert_not_called()

        candidate.refresh_from_db()
        self.assertEqual(candidate.thumbnail_file_key, "")
        completed.refresh_from_db()
        self.assertEqual(
            completed.thumbnail_file_key,
            build_document_thumbnail_s3_key(completed.id),
        )

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=12345)
    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(1200, 800), "image/jpeg"),
    )
    def test_commit_successfully_generates_thumbnail(self, _mock_get, _mock_put):
        doc = _create_uploaded_image_document(title="Commit candidate")

        call_command("backfill_document_thumbnails", "--commit", stdout=StringIO())

        doc.refresh_from_db()
        self.assertEqual(doc.first_page_width, 1200)
        self.assertEqual(doc.first_page_height, 800)
        self.assertEqual(
            doc.thumbnail_file_key,
            build_document_thumbnail_s3_key(doc.id),
        )
        self.assertEqual(doc.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(doc.thumbnail_size_bytes, 12345)

    def test_pdf_documents_are_skipped(self):
        pdf_doc = _create_uploaded_image_document(
            title="PDF document",
            doc_type=Document.DocType.PDF,
        )

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={pdf_doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("doc_type=PDF", output)
        self.assertIn("candidate_count: 0", output)

    def test_already_completed_documents_are_skipped(self):
        doc = _create_uploaded_image_document(title="Completed")
        thumb_key = build_document_thumbnail_s3_key(doc.id)
        doc.thumbnail_file_key = thumb_key
        doc.thumbnail_mime_type = THUMBNAIL_JPEG_MIME
        doc.thumbnail_size_bytes = 999
        doc.first_page_width = 100
        doc.first_page_height = 200
        doc.save()

        stdout = StringIO()
        with patch(
            "documents.services.document_thumbnail_backfill.generate_and_persist_document_thumbnail"
        ) as mock_generate:
            call_command(
                "backfill_document_thumbnails",
                f"--document-id={doc.id}",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("already has thumbnail_file_key", output)
        mock_generate.assert_not_called()

    def test_missing_order_index_zero_source_is_skipped(self):
        doc = _create_uploaded_image_document(
            title="Missing primary source",
            create_source=False,
        )

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("missing primary source file at order_index=0", output)
        self.assertIn("candidate_count: 0", output)

    def test_pending_and_failed_documents_and_sources_are_excluded(self):
        pending_doc = _create_uploaded_image_document(
            title="Pending document",
            upload_status=Document.UploadStatus.UPLOADING,
        )
        failed_doc = _create_uploaded_image_document(
            title="Failed document",
            upload_status=Document.UploadStatus.FAILED,
        )
        pending_source = _create_uploaded_image_document(
            title="Pending source",
            source_upload_status=DocumentSourceFile.UploadStatus.PENDING,
        )
        failed_source = _create_uploaded_image_document(
            title="Failed source",
            source_upload_status=DocumentSourceFile.UploadStatus.FAILED,
        )

        stdout = StringIO()
        call_command("backfill_document_thumbnails", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("No document thumbnail backfill candidates found.", output)
        for doc in (pending_doc, failed_doc, pending_source, failed_source):
            self.assertNotIn(f"document_id={doc.id}", output)

    def test_missing_source_s3_key_is_excluded(self):
        doc = _create_uploaded_image_document(title="Missing source key")
        primary = DocumentSourceFile.objects.get(document=doc, order_index=0)
        primary.file_s3_key = ""
        primary.save(update_fields=["file_s3_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("missing primary source file_s3_key", output)

    def test_document_id_targeting_candidate(self):
        doc = _create_uploaded_image_document(title="Targeted")

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"document_id={doc.id}", output)
        self.assertIn("candidate_count: 1", output)

    def test_document_id_not_found(self):
        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--document-id=999999",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("Document id=999999 does not exist.", output)
        self.assertIn("candidate_count: 0", output)

    def test_limit_caps_candidates(self):
        first = _create_uploaded_image_document(title="First")
        second = _create_uploaded_image_document(title="Second")

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--limit=1",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"document_id={first.id}", output)
        self.assertNotIn(f"document_id={second.id}", output)
        self.assertIn("candidate_count: 1", output)

    def test_limit_selects_first_n_candidates_by_pk(self):
        first = _create_uploaded_image_document(title="Limit first")
        second = _create_uploaded_image_document(title="Limit second")
        third = _create_uploaded_image_document(title="Limit third")

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--limit=2",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["candidate_count"], 2)
        selected_ids = [row["document_id"] for row in payload["candidates"]]
        self.assertEqual(selected_ids, [first.id, second.id])
        self.assertNotIn(third.id, selected_ids)

    def test_deterministic_ordering_by_document_id(self):
        second = _create_uploaded_image_document(title="Second created")
        first = _create_uploaded_image_document(title="First created")

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        selected_ids = [row["document_id"] for row in payload["candidates"]]
        self.assertEqual(selected_ids, sorted([first.id, second.id]))

    def test_whitespace_only_thumbnail_key_is_treated_as_missing(self):
        doc = _create_uploaded_image_document(title="Whitespace thumbnail")
        doc.thumbnail_file_key = "   "
        doc.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn(f"document_id={doc.id}", output)
        self.assertIn("candidate_count: 1", output)
        self.assertNotIn("already has thumbnail_file_key", output)

    def test_whitespace_only_thumbnail_key_included_in_bulk_scan(self):
        doc = _create_uploaded_image_document(title="Bulk whitespace thumbnail")
        doc.thumbnail_file_key = " \t "
        doc.save(update_fields=["thumbnail_file_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["document_id"], doc.id)

    def test_whitespace_only_source_key_is_excluded(self):
        doc = _create_uploaded_image_document(title="Whitespace source key")
        primary = DocumentSourceFile.objects.get(document=doc, order_index=0)
        primary.file_s3_key = "   "
        primary.save(update_fields=["file_s3_key", "updated_at"])

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()

        self.assertIn("is not eligible", output)
        self.assertIn("missing primary source file_s3_key", output)
        self.assertIn("candidate_count: 0", output)

    def test_invalid_limit_raises_command_error(self):
        with self.assertRaises(CommandError) as ctx:
            call_command("backfill_document_thumbnails", "--limit=0", stdout=StringIO())
        self.assertIn("positive integer", str(ctx.exception).lower())

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=1111)
    @patch("documents.services.document_thumbnail.get_object_bytes")
    def test_one_failure_does_not_stop_later_candidates(self, mock_get, _mock_put):
        failing = _create_uploaded_image_document(title="Failing")
        succeeding = _create_uploaded_image_document(title="Succeeding")
        succeeding_key = build_document_source_file_s3_key(
            succeeding.id, 0, "image/jpeg"
        )
        primary = DocumentSourceFile.objects.get(document=failing, order_index=0)
        primary.file_s3_key = f"documents/{failing.id}/missing/source/0.jpg"
        primary.save(update_fields=["file_s3_key", "updated_at"])

        def get_side_effect(_bucket, key):
            if key == succeeding_key:
                return _solid_jpeg_bytes(640, 480), "image/jpeg"
            raise ClientError(
                {"Error": {"Code": "500", "Message": "fail"}},
                "GetObject",
            )

        mock_get.side_effect = get_side_effect

        stdout = StringIO()
        call_command("backfill_document_thumbnails", "--commit", stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("failed_count: 1", output)
        self.assertIn("generated_count: 1", output)
        self.assertIn(f"document_id={failing.id}", output)
        self.assertIn(f"document_id={succeeding.id}", output)

        failing.refresh_from_db()
        succeeding.refresh_from_db()
        self.assertEqual(failing.thumbnail_file_key, "")
        self.assertEqual(
            succeeding.thumbnail_file_key,
            build_document_thumbnail_s3_key(succeeding.id),
        )

    @override_settings(UPLOADS_BUCKET_NAME="")
    def test_missing_bucket_on_commit_raises_command_error(self):
        _create_uploaded_image_document(title="Needs bucket")

        with self.assertRaises(CommandError) as ctx:
            call_command(
                "backfill_document_thumbnails",
                "--commit",
                stdout=StringIO(),
            )
        self.assertIn("UPLOADS_BUCKET_NAME is not configured", str(ctx.exception))

    def test_json_output_includes_counts_and_per_document_results(self):
        doc = _create_uploaded_image_document(title="JSON document")

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            "--json",
            stdout=stdout,
        )
        dry_payload = json.loads(stdout.getvalue())
        self.assertEqual(dry_payload["mode"], "dry-run")
        self.assertEqual(dry_payload["filters"]["document_id"], None)
        self.assertEqual(dry_payload["candidate_count"], 1)
        self.assertEqual(dry_payload["generated_count"], 0)
        self.assertEqual(dry_payload["failed_count"], 0)
        self.assertEqual(len(dry_payload["results"]), 1)
        self.assertEqual(dry_payload["results"][0]["document_id"], doc.id)
        self.assertEqual(dry_payload["results"][0]["status"], "candidate")

        with (
            patch(
                "documents.services.document_thumbnail.get_object_bytes",
                return_value=(_solid_jpeg_bytes(800, 600), "image/jpeg"),
            ),
            patch(
                "documents.services.document_thumbnail.put_object_bytes",
                return_value=2222,
            ),
        ):
            stdout = StringIO()
            call_command(
                "backfill_document_thumbnails",
                "--commit",
                "--json",
                stdout=stdout,
            )

        commit_payload = json.loads(stdout.getvalue())
        self.assertEqual(commit_payload["mode"], "commit")
        self.assertEqual(commit_payload["generated_count"], 1)
        self.assertEqual(commit_payload["failed_count"], 0)
        self.assertEqual(len(commit_payload["results"]), 1)
        self.assertEqual(commit_payload["results"][0]["status"], "generated")

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=3333)
    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(500, 500), "image/jpeg"),
    )
    def test_repeated_execution_is_idempotent(self, _mock_get, _mock_put):
        doc = _create_uploaded_image_document(title="Idempotent")

        call_command("backfill_document_thumbnails", "--commit", stdout=StringIO())
        doc.refresh_from_db()
        first_key = doc.thumbnail_file_key
        first_size = doc.thumbnail_size_bytes

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("already has thumbnail_file_key", output)

        with patch(
            "documents.services.document_thumbnail_backfill.generate_and_persist_document_thumbnail"
        ) as mock_generate:
            call_command("backfill_document_thumbnails", "--commit", stdout=StringIO())
            mock_generate.assert_not_called()

        doc.refresh_from_db()
        self.assertEqual(doc.thumbnail_file_key, first_key)
        self.assertEqual(doc.thumbnail_size_bytes, first_size)

    def test_json_target_disposition_for_ineligible_document_id(self):
        doc = _create_uploaded_image_document(
            title="Pending target",
            upload_status=Document.UploadStatus.UPLOADING,
        )

        stdout = StringIO()
        call_command(
            "backfill_document_thumbnails",
            f"--document-id={doc.id}",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["target"]["disposition"], "ineligible")
        self.assertEqual(payload["candidate_count"], 0)
        self.assertEqual(payload["results"][0]["status"], "skipped")
        self.assertIn("upload_status", payload["results"][0]["reason"])


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class BackfillDocumentThumbnailsQueryCountTests(TestCase):
    def _bulk_report_query_count(self, *, candidate_count: int) -> int:
        Document.objects.all().delete()
        for index in range(candidate_count):
            _create_uploaded_image_document(title=f"Bulk query count {index}")

        with CaptureQueriesContext(connection) as context:
            report = build_document_thumbnail_backfill_report()
            self.assertEqual(report.candidate_count, candidate_count)
            self.assertEqual(
                len({row.source_file_key for row in report.candidates}),
                candidate_count,
            )

        return len(context)

    def test_bulk_report_query_count_stable_with_more_candidates(self):
        count_for_three = self._bulk_report_query_count(candidate_count=3)
        count_for_six = self._bulk_report_query_count(candidate_count=6)
        self.assertEqual(count_for_three, count_for_six)
