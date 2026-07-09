"""Incremental multi-image OCR upload API tests."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from documents.models import Document, DocumentSourceFile
from documents.services.source_files import MULTI_IMAGE_MAX_FILES


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class IncrementalUploadApiTests(TestCase):
    def setUp(self):
        from documents.s3 import S3HeadObjectResult

        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.staff = User.objects.create_user(
            username="incremental_upload_staff",
            password="test-pass",
            is_staff=True,
        )

    def _post_create_incremental(self, **overrides):
        payload = {
            "title": "Incremental draft",
            "text_input_type": "HANDWRITTEN",
            "incremental": True,
        }
        payload.update(overrides)
        self.client.force_login(self.staff)
        return self.client.post(
            "/api/uploads/create/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_part_add(self, doc_id: int, **file_overrides):
        payload = {
            "original_name": file_overrides.pop("original_name", "page.jpg"),
            "mime_type": file_overrides.pop("mime_type", "image/jpeg"),
            "size_bytes": file_overrides.pop("size_bytes", 1000),
            **file_overrides,
        }
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/parts/add/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_part_complete(self, doc_id: int, order_index: int, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/parts/{order_index}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_finalize(self, doc_id: int):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/finalize/",
            data=json.dumps({"success": True}),
            content_type="application/json",
        )

    def _post_single_create(self):
        self.client.force_login(self.staff)
        return self.client.post(
            "/api/uploads/create/",
            data=json.dumps(
                {
                    "title": "Single image",
                    "doc_type": "IMAGE",
                    "text_input_type": "HANDWRITTEN",
                    "original_name": "one.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 500,
                }
            ),
            content_type="application/json",
        )

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_incremental_create_returns_draft_without_parts(self, _mock_put):
        resp = self._post_create_incremental()
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertTrue(body["incremental"])
        self.assertIsNone(body["expected_source_file_count"])

        doc = Document.objects.get(id=body["document_id"])
        self.assertEqual(doc.doc_type, Document.DocType.IMAGE)
        self.assertIsNone(doc.expected_source_file_count)
        self.assertEqual(doc.file_s3_key, "")
        self.assertEqual(doc.source_files.count(), 0)

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_incremental_add_part_and_complete_flow(self, _mock_put):
        create_resp = self._post_create_incremental()
        doc_id = create_resp.json()["document_id"]

        add0 = self._post_part_add(doc_id, original_name="page-1.jpg")
        self.assertEqual(add0.status_code, 201)
        self.assertEqual(add0.json()["order_index"], 0)

        add1 = self._post_part_add(doc_id, original_name="page-2.jpg")
        self.assertEqual(add1.status_code, 201)
        self.assertEqual(add1.json()["order_index"], 1)

        self.assertEqual(
            DocumentSourceFile.objects.filter(document_id=doc_id).count(),
            2,
        )

        self._post_part_complete(
            doc_id, 0, {"success": True, "file_size": 100, "file_mime": "image/jpeg"}
        )
        self._post_part_complete(
            doc_id, 1, {"success": True, "file_size": 200, "file_mime": "image/jpeg"}
        )

        with patch("documents.views.send_process_document_message") as mock_enqueue:
            finalize_resp = self._post_finalize(doc_id)

        self.assertEqual(finalize_resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.expected_source_file_count, 2)
        self.assertTrue(doc.file_s3_key.endswith("/source/0.jpeg"))

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_incremental_finalize_succeeds_with_one_uploaded_part(self, _mock_put):
        create_resp = self._post_create_incremental()
        doc_id = create_resp.json()["document_id"]

        self._post_part_add(doc_id)
        self._post_part_complete(
            doc_id, 0, {"success": True, "file_size": 100, "file_mime": "image/jpeg"}
        )

        with patch("documents.views.send_process_document_message") as mock_enqueue:
            finalize_resp = self._post_finalize(doc_id)

        self.assertEqual(finalize_resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.expected_source_file_count, 1)
        self.assertTrue(doc.file_s3_key.endswith("/source/0.jpeg"))

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_incremental_finalize_rejects_zero_uploaded_parts(self, _mock_put):
        create_resp = self._post_create_incremental()
        doc_id = create_resp.json()["document_id"]

        with patch("documents.views.send_process_document_message") as mock_enqueue:
            resp = self._post_finalize(doc_id)

        self.assertEqual(resp.status_code, 400)
        self.assertIn("at least 1", resp.json()["error"])
        mock_enqueue.assert_not_called()

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)
        self.assertIsNone(doc.expected_source_file_count)

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_incremental_rejects_more_than_max_parts(self, _mock_put):
        create_resp = self._post_create_incremental()
        doc_id = create_resp.json()["document_id"]

        for i in range(MULTI_IMAGE_MAX_FILES):
            add_resp = self._post_part_add(doc_id, original_name=f"page-{i}.jpg")
            self.assertEqual(add_resp.status_code, 201, msg=f"part {i}")

        overflow = self._post_part_add(doc_id, original_name="overflow.jpg")
        self.assertEqual(overflow.status_code, 400)
        self.assertIn(str(MULTI_IMAGE_MAX_FILES), overflow.content.decode())

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_single_file_create_path_unchanged(self, mock_put):
        resp = self._post_single_create()
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(
            set(body.keys()),
            {"document_id", "upload_status", "s3_key", "upload_url"},
        )
        self.assertNotIn("incremental", body)
        mock_put.assert_called_once()
