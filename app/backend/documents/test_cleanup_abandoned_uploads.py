from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from documents.models import ArchiveItem, Document, DocumentSourceFile
from documents.s3 import S3DeleteObjectResult
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class CleanupAbandonedUploadsCommandTests(TestCase):
    def _create_incremental_draft(self, *, title: str = "Abandoned draft", **kwargs):
        defaults = {
            "title": title,
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "upload_status": Document.UploadStatus.UPLOADING,
            "expected_source_file_count": None,
            "file_s3_key": "",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _add_source_file(
        self,
        document: Document,
        *,
        order_index: int = 0,
        upload_status: str = DocumentSourceFile.UploadStatus.UPLOADED,
        s3_key: str | None = None,
    ) -> DocumentSourceFile:
        key = s3_key or f"documents/{document.pk}/source/{order_index}.jpeg"
        return DocumentSourceFile.objects.create(
            document=document,
            order_index=order_index,
            file_s3_key=key,
            file_original_name=f"page-{order_index}.jpg",
            mime_type="image/jpeg",
            upload_status=upload_status,
        )

    def _backdate_document(self, document: Document, *, hours: int) -> None:
        stale_time = timezone.now() - timedelta(hours=hours)
        Document.objects.filter(pk=document.pk).update(
            created_at=stale_time,
            updated_at=stale_time,
        )

    def test_dry_run_does_not_modify_db_or_call_s3_delete(self):
        doc = self._create_incremental_draft(title="Dry run draft")
        self._add_source_file(doc)
        self._backdate_document(doc, hours=25)
        archive_item_id = doc.archive_item_id

        stdout = StringIO()
        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--stale-hours=24",
                stdout=stdout,
            )

        output = stdout.getvalue()
        self.assertIn("dry run", output.lower())
        self.assertIn(f"document_id={doc.id}", output)
        self.assertIn("Dry run draft", output)
        self.assertIn(f"documents/{doc.id}/source/0.jpeg", output)
        mock_delete.assert_not_called()

        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)
        self.assertTrue(ArchiveItem.objects.filter(pk=archive_item_id).exists())
        self.assertEqual(
            DocumentSourceFile.objects.filter(document_id=doc.id).count(),
            1,
        )

    def test_commit_cleans_abandoned_incremental_draft(self):
        doc = self._create_incremental_draft(title="Commit draft")
        source = self._add_source_file(doc)
        self._backdate_document(doc, hours=30)
        archive_item_id = doc.archive_item_id
        s3_key = source.file_s3_key

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object",
            return_value=S3DeleteObjectResult(deleted=True),
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                "--stale-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_called_once_with("test-uploads-bucket", s3_key)
        self.assertFalse(Document.objects.filter(pk=doc.id).exists())
        self.assertFalse(DocumentSourceFile.objects.filter(document_id=doc.id).exists())
        self.assertFalse(ArchiveItem.objects.filter(pk=archive_item_id).exists())

    def test_recent_unfinished_draft_is_not_touched(self):
        doc = self._create_incremental_draft(title="Recent draft")
        self._add_source_file(doc)
        self._backdate_document(doc, hours=2)

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                "--stale-hours=24",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())

    def test_finalized_document_is_not_touched(self):
        doc = self._create_incremental_draft(
            title="Finalized doc",
            upload_status=Document.UploadStatus.UPLOADED,
            expected_source_file_count=1,
            file_s3_key="documents/99/source/0.jpeg",
        )
        self._backdate_document(doc, hours=48)

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())

    def test_non_incremental_uploads_are_not_touched(self):
        batch_doc = self._create_incremental_draft(
            title="Batch multi-image",
            expected_source_file_count=2,
        )
        single_file_doc = self._create_incremental_draft(
            title="Single-file in progress",
            file_s3_key="documents/5/original.jpeg",
        )
        self._backdate_document(batch_doc, hours=30)
        self._backdate_document(single_file_doc, hours=30)

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()
        self.assertTrue(Document.objects.filter(pk=batch_doc.id).exists())
        self.assertTrue(Document.objects.filter(pk=single_file_doc.id).exists())

    def test_manual_text_and_photo_items_are_not_touched(self):
        manual = create_manual_text_archive_item(
            title="Manual text item",
            body="Typed content",
        )
        photo_item = ArchiveItem.objects.create(
            title="Photo item",
            item_type=ArchiveItem.ItemType.PHOTO,
        )
        from documents.models import PhotoContent

        PhotoContent.objects.create(
            archive_item=photo_item,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object"
        ) as mock_delete:
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
            )

        mock_delete.assert_not_called()
        self.assertTrue(ArchiveItem.objects.filter(pk=manual.pk).exists())
        self.assertTrue(ArchiveItem.objects.filter(pk=photo_item.pk).exists())

    def test_s3_delete_failures_are_reported_and_db_cleanup_continues(self):
        doc = self._create_incremental_draft(title="S3 failure draft")
        source = self._add_source_file(doc)
        self._backdate_document(doc, hours=30)
        archive_item_id = doc.archive_item_id

        def _delete_side_effect(_bucket, key):
            if key == source.file_s3_key:
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                    "DeleteObject",
                )
            return S3DeleteObjectResult(deleted=True)

        stderr = StringIO()
        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object",
            side_effect=_delete_side_effect,
        ):
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
                stderr=stderr,
            )

        stderr_text = stderr.getvalue()
        self.assertIn(source.file_s3_key, stderr_text)
        self.assertIn(str(doc.id), stderr_text)
        self.assertIn("AccessDenied", stderr_text)
        self.assertIn("still removed", stderr_text.lower())
        self.assertFalse(Document.objects.filter(pk=doc.id).exists())
        self.assertFalse(ArchiveItem.objects.filter(pk=archive_item_id).exists())

    def test_commit_is_idempotent_when_s3_objects_already_gone(self):
        doc = self._create_incremental_draft(title="Already gone")
        self._add_source_file(doc)
        self._backdate_document(doc, hours=30)

        with patch(
            "documents.services.abandoned_upload_cleanup.delete_s3_object",
            return_value=S3DeleteObjectResult(deleted=False, not_found=True),
        ):
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
            )
            call_command(
                "cleanup_abandoned_uploads",
                "--commit",
                stdout=StringIO(),
            )

        self.assertFalse(Document.objects.filter(pk=doc.id).exists())

    def test_json_output_includes_candidates(self):
        doc = self._create_incremental_draft(title="JSON draft")
        self._add_source_file(doc)
        self._backdate_document(doc, hours=40)

        stdout = StringIO()
        call_command(
            "cleanup_abandoned_uploads",
            "--json",
            "--stale-hours=24",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(payload["candidates"][0]["document_id"], doc.id)
        self.assertEqual(payload["candidates"][0]["title"], "JSON draft")
