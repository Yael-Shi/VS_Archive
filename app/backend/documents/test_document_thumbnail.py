"""Document thumbnail generation on OCR image upload finalize/complete."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from PIL import Image

from documents.models import Document, DocumentSourceFile
from documents.s3 import (
    S3HeadObjectResult,
    build_document_source_file_s3_key,
    build_document_thumbnail_s3_key,
)
from documents.services.archive_items import create_ocr_document
from documents.services.document_thumbnail import (
    generate_and_persist_document_thumbnail,
    schedule_document_thumbnail_after_upload,
    should_generate_document_thumbnail,
)
from documents.services.image_thumbnail import (
    THUMBNAIL_JPEG_MIME,
    THUMBNAIL_MAX_EDGE,
    generate_image_thumbnail_bytes,
)
from documents.test_exif_orientation import (
    make_oriented_jpeg,
    minimal_upright_jpeg_bytes,
)


def _solid_jpeg_bytes(
    width: int,
    height: int,
    *,
    color: tuple[int, int, int] = (10, 20, 30),
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color=color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _solid_rgba_png_bytes(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (width, height), color=(255, 0, 0, 128)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


class BuildDocumentThumbnailS3KeyTests(SimpleTestCase):
    def test_deterministic_thumbnail_key(self):
        self.assertEqual(
            build_document_thumbnail_s3_key(42), "documents/42/thumb_400.jpg"
        )
        self.assertEqual(
            build_document_thumbnail_s3_key(7), "documents/7/thumb_400.jpg"
        )


class GenerateDocumentThumbnailBytesTests(SimpleTestCase):
    def test_exif_transpose_records_transposed_dimensions_and_jpeg_size(self):
        image_bytes = make_oriented_jpeg(800, 600, 6)
        jpeg_bytes, width, height = generate_image_thumbnail_bytes(image_bytes)
        self.assertEqual((width, height), (600, 800))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.size, (300, 400))

    def test_thumbnail_bytes_are_jpeg_within_max_edge(self):
        image_bytes = _solid_jpeg_bytes(900, 600)
        jpeg_bytes, width, height = generate_image_thumbnail_bytes(image_bytes)
        self.assertEqual((width, height), (900, 600))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertLessEqual(max(thumb.size), THUMBNAIL_MAX_EDGE)

    def test_rgba_png_is_encoded_as_rgb_jpeg(self):
        png_bytes = _solid_rgba_png_bytes(64, 48)
        jpeg_bytes, width, height = generate_image_thumbnail_bytes(png_bytes)
        self.assertEqual((width, height), (64, 48))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.mode, "RGB")
            self.assertEqual(thumb.format, "JPEG")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class GenerateAndPersistDocumentThumbnailTests(TestCase):
    def _create_image_document_with_sources(
        self,
        *,
        source_specs: list[tuple[int, int, tuple[int, int, int]]],
    ) -> Document:
        doc = create_ocr_document(
            title="Thumbnail document",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            file_original_name="page-1.jpg",
            mime_type="image/jpeg",
            size_bytes=1000,
        )
        doc.upload_status = Document.UploadStatus.UPLOADED
        doc.save(update_fields=["upload_status", "updated_at"])

        for order_index, (width, height, color) in enumerate(source_specs):
            key = build_document_source_file_s3_key(
                doc.id,
                order_index,
                "image/jpeg",
            )
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=order_index,
                file_s3_key=key,
                file_original_name=f"page-{order_index + 1}.jpg",
                mime_type="image/jpeg",
                size_bytes=1000 + order_index,
                upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            )
        return doc

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=12345)
    @patch("documents.services.document_thumbnail.get_object_bytes")
    def test_multi_image_uses_only_first_source_file(self, mock_get, mock_put):
        first_bytes = _solid_jpeg_bytes(1200, 800, color=(255, 0, 0))
        second_bytes = _solid_jpeg_bytes(640, 480, color=(0, 255, 0))

        doc = self._create_image_document_with_sources(
            source_specs=[
                (1200, 800, (255, 0, 0)),
                (640, 480, (0, 255, 0)),
            ]
        )
        primary_key = build_document_source_file_s3_key(doc.id, 0, "image/jpeg")
        secondary_key = build_document_source_file_s3_key(doc.id, 1, "image/jpeg")

        def get_side_effect(bucket, key):
            if key == primary_key:
                return first_bytes, "image/jpeg"
            if key == secondary_key:
                return second_bytes, "image/jpeg"
            raise AssertionError(f"unexpected S3 key: {key}")

        mock_get.side_effect = get_side_effect

        result = generate_and_persist_document_thumbnail(
            doc,
            bucket="test-uploads-bucket",
        )
        self.assertIsNotNone(result)

        mock_get.assert_called_once_with("test-uploads-bucket", primary_key)
        doc.refresh_from_db()
        self.assertEqual(doc.first_page_width, 1200)
        self.assertEqual(doc.first_page_height, 800)
        self.assertEqual(
            doc.thumbnail_file_key,
            build_document_thumbnail_s3_key(doc.id),
        )
        self.assertEqual(doc.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(doc.thumbnail_size_bytes, 12345)
        mock_put.assert_called_once()

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=9999)
    @patch("documents.services.document_thumbnail.get_object_bytes")
    def test_uses_source_file_at_order_index_zero(self, mock_get, mock_put):
        doc = self._create_image_document_with_sources(
            source_specs=[(800, 600, (1, 2, 3)), (400, 300, (4, 5, 6))]
        )
        primary_key = build_document_source_file_s3_key(doc.id, 0, "image/jpeg")
        mock_get.return_value = (_solid_jpeg_bytes(800, 600), "image/jpeg")

        generate_and_persist_document_thumbnail(doc, bucket="test-uploads-bucket")

        mock_get.assert_called_once_with("test-uploads-bucket", primary_key)
        doc.refresh_from_db()
        self.assertEqual(doc.first_page_width, 800)
        self.assertEqual(doc.first_page_height, 600)
        mock_put.assert_called_once()

    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        side_effect=ClientError(
            {"Error": {"Code": "500", "Message": "fail"}},
            "GetObject",
        ),
    )
    def test_generation_failure_leaves_document_without_thumbnail_metadata(
        self, _mock_get
    ):
        doc = self._create_image_document_with_sources(
            source_specs=[(800, 600, (1, 2, 3))]
        )
        result = generate_and_persist_document_thumbnail(
            doc,
            bucket="test-uploads-bucket",
        )
        self.assertIsNone(result)

        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.thumbnail_file_key, "")
        self.assertIsNone(doc.first_page_width)

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=7777)
    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(800, 600), "image/jpeg"),
    )
    def test_metadata_save_failure_restores_in_memory_fields(self, _mock_get, mock_put):
        doc = self._create_image_document_with_sources(
            source_specs=[(800, 600, (1, 2, 3))]
        )
        original_save = Document.save

        def save_raises_for_thumbnail_metadata(self, *args, **kwargs):
            update_fields = kwargs.get("update_fields") or ()
            if "thumbnail_file_key" in update_fields:
                raise RuntimeError("db save failed")
            return original_save(self, *args, **kwargs)

        with patch.object(Document, "save", save_raises_for_thumbnail_metadata):
            result = generate_and_persist_document_thumbnail(
                doc,
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(result)
        mock_put.assert_called_once()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.thumbnail_file_key, "")
        self.assertIsNone(doc.first_page_width)
        doc.refresh_from_db()
        self.assertEqual(doc.thumbnail_file_key, "")

    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=7777)
    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(800, 600), "image/jpeg"),
    )
    def test_preserved_field_capture_runs_before_s3_upload(self, _mock_get, mock_put):
        doc = self._create_image_document_with_sources(
            source_specs=[(800, 600, (1, 2, 3))]
        )
        with patch(
            "documents.services.document_thumbnail._PreservedDocumentThumbnailFields",
            side_effect=RuntimeError("capture failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "capture failed"):
                generate_and_persist_document_thumbnail(
                    doc,
                    bucket="test-uploads-bucket",
                )

        mock_put.assert_not_called()
        self.assertEqual(doc.thumbnail_file_key, "")
        self.assertIsNone(doc.first_page_width)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class DocumentUploadThumbnailIntegrationTests(TestCase):
    def setUp(self):
        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.s3_get_patcher = patch(
            "documents.services.exif_orientation.get_object_bytes",
            return_value=(minimal_upright_jpeg_bytes(), "image/jpeg"),
        )
        self.s3_get_patcher.start()
        self.addCleanup(self.s3_get_patcher.stop)

        self.staff = User.objects.create_user(
            username="doc_thumb_staff",
            password="test-pass",
            is_staff=True,
        )

    def _post_create(self, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            "/api/uploads/create/",
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

    def _post_complete(self, doc_id: int, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _multi_files_payload(self, count: int = 2):
        files = [
            {
                "original_name": f"page-{i + 1}.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 1000 + i,
            }
            for i in range(count)
        ]
        return {
            "title": "Multi-image thumbnail test",
            "text_input_type": "HANDWRITTEN",
            "files": files,
        }

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.enqueue_uploaded_document_processing")
    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=5555)
    @patch("documents.services.document_thumbnail.get_object_bytes")
    def test_finalize_persists_thumbnail_metadata(
        self, mock_get, mock_put, _mock_enqueue, _mock_presign
    ):
        mock_get.return_value = (_solid_jpeg_bytes(800, 1200), "image/jpeg")

        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        self._post_part_complete(doc_id, 0, {"success": True, "file_size": 100})
        self._post_part_complete(doc_id, 1, {"success": True, "file_size": 200})

        with self.captureOnCommitCallbacks(execute=True):
            resp = self._post_finalize(doc_id)

        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.first_page_width, 800)
        self.assertEqual(doc.first_page_height, 1200)
        self.assertEqual(
            doc.thumbnail_file_key,
            build_document_thumbnail_s3_key(doc_id),
        )
        self.assertEqual(doc.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(doc.thumbnail_size_bytes, 5555)

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.enqueue_uploaded_document_processing")
    @patch(
        "documents.services.document_thumbnail.generate_and_persist_document_thumbnail"
    )
    def test_repeated_finalize_does_not_regenerate_thumbnail(
        self, mock_thumbnail, _mock_enqueue, _mock_presign
    ):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": True})

        with self.captureOnCommitCallbacks(execute=True):
            first = self._post_finalize(doc_id)
            second = self._post_finalize(doc_id)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mock_thumbnail.assert_called_once()

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.enqueue_uploaded_document_processing")
    @patch(
        "documents.services.document_thumbnail.generate_and_persist_document_thumbnail"
    )
    def test_pdf_complete_does_not_generate_thumbnail(
        self, mock_thumbnail, _mock_enqueue, _mock_presign
    ):
        create_resp = self._post_create(
            {
                "title": "PDF upload",
                "doc_type": "PDF",
                "text_input_type": "PRINTED",
                "original_name": "document.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 1000,
            }
        )
        doc_id = create_resp.json()["document_id"]
        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True,
            content_type="application/pdf",
        )

        with self.captureOnCommitCallbacks(execute=True):
            resp = self._post_complete(
                doc_id,
                {"success": True, "file_mime": "application/pdf"},
            )

        self.assertEqual(resp.status_code, 200)
        mock_thumbnail.assert_not_called()
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.thumbnail_file_key, "")

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.enqueue_uploaded_document_processing")
    @patch(
        "documents.services.document_thumbnail.get_object_bytes",
        side_effect=ClientError(
            {"Error": {"Code": "500", "Message": "fail"}},
            "GetObject",
        ),
    )
    def test_thumbnail_failure_leaves_finalize_successful(
        self, _mock_get, _mock_enqueue, _mock_presign
    ):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": True})

        with self.captureOnCommitCallbacks(execute=True):
            resp = self._post_finalize(doc_id)

        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.thumbnail_file_key, "")

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.enqueue_uploaded_document_processing")
    @patch("documents.services.document_thumbnail.put_object_bytes", return_value=4444)
    @patch("documents.services.document_thumbnail.get_object_bytes")
    def test_metadata_save_failure_leaves_finalize_successful(
        self, mock_get, mock_put, _mock_enqueue, _mock_presign
    ):
        mock_get.return_value = (_solid_jpeg_bytes(640, 480), "image/jpeg")
        original_save = Document.save

        def save_raises_for_thumbnail_metadata(self, *args, **kwargs):
            update_fields = kwargs.get("update_fields") or ()
            if "thumbnail_file_key" in update_fields:
                raise RuntimeError("db save failed")
            return original_save(self, *args, **kwargs)

        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": True})

        with patch.object(Document, "save", save_raises_for_thumbnail_metadata):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self._post_finalize(doc_id)

        self.assertEqual(resp.status_code, 200)
        mock_put.assert_called_once()
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.thumbnail_file_key, "")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class DocumentUploadThumbnailTransactionBoundaryTests(TransactionTestCase):
    def setUp(self):
        self.doc = create_ocr_document(
            title="Transaction boundary document",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            file_original_name="page.jpg",
            mime_type="image/jpeg",
            size_bytes=1000,
        )
        self.doc.upload_status = Document.UploadStatus.UPLOADING
        self.doc.save(update_fields=["upload_status", "updated_at"])
        DocumentSourceFile.objects.create(
            document=self.doc,
            order_index=0,
            file_s3_key=build_document_source_file_s3_key(
                self.doc.id,
                0,
                "image/jpeg",
            ),
            file_original_name="page.jpg",
            mime_type="image/jpeg",
            size_bytes=1000,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )

    @patch(
        "documents.services.document_thumbnail.generate_and_persist_document_thumbnail"
    )
    def test_thumbnail_runs_outside_finalize_atomic_block(self, mock_thumbnail):
        call_order: list[str] = []

        def thumbnail_side_effect(document, *, bucket):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            call_order.append("thumbnail")
            return None

        mock_thumbnail.side_effect = thumbnail_side_effect

        with transaction.atomic():
            self.doc.upload_status = Document.UploadStatus.UPLOADED
            self.doc.save(update_fields=["upload_status", "updated_at"])
            schedule_document_thumbnail_after_upload(
                self.doc,
                bucket="test-uploads-bucket",
                already_uploaded=False,
            )
            call_order.append("finalize_tx")

        self.assertEqual(call_order, ["finalize_tx", "thumbnail"])
        mock_thumbnail.assert_called_once()


class ShouldGenerateDocumentThumbnailTests(SimpleTestCase):
    def test_pdf_document_is_skipped(self):
        doc = Document(doc_type=Document.DocType.PDF)
        self.assertFalse(
            should_generate_document_thumbnail(doc, already_uploaded=False)
        )

    def test_already_uploaded_is_skipped(self):
        doc = Document(doc_type=Document.DocType.IMAGE)
        self.assertFalse(should_generate_document_thumbnail(doc, already_uploaded=True))

    def test_existing_thumbnail_key_is_skipped(self):
        doc = Document(
            doc_type=Document.DocType.IMAGE,
            thumbnail_file_key="documents/1/thumb_400.jpg",
        )
        self.assertFalse(
            should_generate_document_thumbnail(doc, already_uploaded=False)
        )
