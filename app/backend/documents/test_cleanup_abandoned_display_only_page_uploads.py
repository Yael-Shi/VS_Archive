from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from documents.models import Document, DocumentSourceFile, ProcessDocumentRequest
from documents.s3 import S3DeleteObjectResult
from documents.services.archive_items import create_ocr_document


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class CleanupAbandonedDisplayOnlyPageUploadsTests(TestCase):
    def _ready_doc(self, *, count: int = 2) -> Document:
        doc = create_ocr_document(
            title="Ready multi-image",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            expected_source_file_count=count,
            file_s3_key="documents/pending/source/0.jpg",
            mime_type="image/jpeg",
        )
        doc.file_s3_key = f"documents/{doc.id}/source/0.jpg"
        doc.save(update_fields=["file_s3_key"])
        for order_index in range(count):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=order_index,
                file_s3_key=f"documents/{doc.id}/source/{order_index}.jpg",
                file_original_name=f"page-{order_index}.jpg",
                mime_type="image/jpeg",
                size_bytes=1000,
                upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            )
        return doc

    def _add_uncommitted_extra(
        self,
        doc: Document,
        *,
        upload_status: str = DocumentSourceFile.UploadStatus.PENDING,
        include_in_ocr: bool = False,
        hours: int = 30,
    ) -> DocumentSourceFile:
        order_index = doc.expected_source_file_count
        self.assertIsNotNone(order_index)
        assert order_index is not None
        extra = DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=f"documents/{doc.id}/source/{order_index}.jpg",
            file_original_name="extra.jpg",
            mime_type="image/jpeg",
            size_bytes=100,
            upload_status=upload_status,
            include_in_ocr=include_in_ocr,
        )
        stale_time = timezone.now() - timedelta(hours=hours)
        DocumentSourceFile.objects.filter(pk=extra.pk).update(
            created_at=stale_time,
            updated_at=stale_time,
        )
        extra.refresh_from_db()
        return extra

    def test_dry_run_does_not_delete(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        stdout = StringIO()
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                stdout=stdout,
            )
        self.assertIn("dry run", stdout.getvalue().lower())
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())
        doc.refresh_from_db()
        self.assertEqual(doc.expected_source_file_count, 2)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    def test_commit_deletes_stale_pending_extra_only(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        extra_key = extra.file_s3_key
        extra_id = extra.pk
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object",
            return_value=S3DeleteObjectResult(deleted=True),
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_called_once_with("test-uploads-bucket", extra_key)
        self.assertFalse(DocumentSourceFile.objects.filter(pk=extra_id).exists())
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 2)
        doc.refresh_from_db()
        self.assertEqual(doc.expected_source_file_count, 2)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    def test_commit_deletes_stale_failed_extra(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(
            doc, upload_status=DocumentSourceFile.UploadStatus.FAILED
        )
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object",
            return_value=S3DeleteObjectResult(deleted=True),
        ):
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        self.assertFalse(DocumentSourceFile.objects.filter(pk=extra.pk).exists())

    def test_recent_extra_is_not_touched(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc, hours=2)
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())

    def test_committed_display_only_page_is_not_touched(self):
        doc = self._ready_doc(count=2)
        committed = DocumentSourceFile.objects.get(document=doc, order_index=1)
        committed.include_in_ocr = False
        committed.save(update_fields=["include_in_ocr"])
        stale_time = timezone.now() - timedelta(hours=48)
        DocumentSourceFile.objects.filter(pk=committed.pk).update(updated_at=stale_time)
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=committed.pk).exists())

    def test_ocr_included_extra_is_not_touched(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc, include_in_ocr=True)
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())

    def test_busy_document_extra_is_not_touched(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        doc.processing_state_user = Document.ProcessingState.PROCESSING
        doc.save(update_fields=["processing_state_user"])
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())

        doc.processing_state_user = Document.ProcessingState.READY
        doc.save(update_fields=["processing_state_user"])
        ProcessDocumentRequest.objects.create(
            document=doc,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        mock_delete.assert_not_called()
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())

    def test_s3_failure_keeps_source_row(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        stderr = StringIO()
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object",
            side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "DeleteObject",
            ),
        ):
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
                stderr=stderr,
            )
        self.assertIn("AccessDenied", stderr.getvalue())
        self.assertTrue(DocumentSourceFile.objects.filter(pk=extra.pk).exists())
        doc.refresh_from_db()
        self.assertEqual(doc.expected_source_file_count, 2)

    def test_commit_is_idempotent_when_s3_object_already_gone(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        extra_id = extra.pk
        with patch(
            "documents.services.display_only_page_abandoned_cleanup.delete_s3_object",
            return_value=S3DeleteObjectResult(deleted=False, not_found=True),
        ):
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
            call_command(
                "cleanup_abandoned_display_only_page_uploads",
                "--commit",
                stdout=StringIO(),
            )
        self.assertFalse(DocumentSourceFile.objects.filter(pk=extra_id).exists())
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())

    def test_json_output_includes_candidates(self):
        doc = self._ready_doc()
        extra = self._add_uncommitted_extra(doc)
        stdout = StringIO()
        call_command(
            "cleanup_abandoned_display_only_page_uploads",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["source_file_id"], extra.id)
        self.assertEqual(payload["candidates"][0]["document_id"], doc.id)
