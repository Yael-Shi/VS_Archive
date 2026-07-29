"""EXIF orientation normalization for document image uploads."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from documents.models import Document, DocumentSourceFile
from documents.s3 import S3HeadObjectResult, put_object_bytes
from documents.services.archive_items import create_ocr_document
from documents.services.exif_orientation import (
    ExifNormalizationError,
    normalize_image_bytes_exif_orientation,
    normalize_uploaded_image_exif_in_s3,
)
from documents.services.upload_validation import normalize_upload_mime_type


def minimal_upright_jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color="white").save(buffer, format="JPEG")
    return buffer.getvalue()


def make_oriented_image(
    width: int,
    height: int,
    orientation: int | None,
    *,
    image_format: str = "JPEG",
    left_color: tuple[int, int, int] = (255, 0, 0),
    right_color: tuple[int, int, int] = (0, 0, 255),
) -> bytes:
    image = Image.new("RGB", (width, height))
    midpoint = width // 2
    for x in range(width):
        color = left_color if x < midpoint else right_color
        for y in range(height):
            image.putpixel((x, y), color)

    buffer = BytesIO()
    if orientation is None:
        image.save(buffer, format=image_format)
    else:
        exif = image.getexif()
        exif[274] = orientation
        image.save(buffer, format=image_format, exif=exif)
    return buffer.getvalue()


def make_oriented_jpeg(
    width: int,
    height: int,
    orientation: int | None,
    *,
    left_color: tuple[int, int, int] = (255, 0, 0),
    right_color: tuple[int, int, int] = (0, 0, 255),
) -> bytes:
    return make_oriented_image(
        width,
        height,
        orientation,
        image_format="JPEG",
        left_color=left_color,
        right_color=right_color,
    )


def _read_orientation_from_jpeg(image_bytes: bytes) -> int | None:
    with Image.open(BytesIO(image_bytes)) as image:
        exif = image.getexif()
        if not exif:
            return None
        value = exif.get(274)
        return int(value) if value is not None else None


class InMemoryS3Store:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}

    def seed(self, bucket: str, key: str, body: bytes, content_type: str) -> None:
        self.objects[(bucket, key)] = (body, content_type)

    def get_object_bytes(self, bucket: str, key: str) -> tuple[bytes, str | None]:
        try:
            body, content_type = self.objects[(bucket, key)]
        except KeyError as exc:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "not found"}},
                "GetObject",
            ) from exc
        return body, content_type

    def put_object_bytes(
        self,
        bucket: str,
        key: str,
        body: bytes,
        content_type: str,
    ) -> int:
        self.objects[(bucket, key)] = (body, content_type)
        return len(body)

    def head_s3_object(self, bucket: str, key: str) -> S3HeadObjectResult:
        try:
            body, content_type = self.objects[(bucket, key)]
        except KeyError:
            return S3HeadObjectResult(exists=False)
        return S3HeadObjectResult(
            exists=True,
            content_type=content_type,
            content_length=len(body),
        )


class ExifOrientationServiceTests(SimpleTestCase):
    def test_orientation_6_becomes_physically_portrait_and_clears_exif(self):
        raw = make_oriented_jpeg(200, 100, 6)

        result = normalize_image_bytes_exif_orientation(raw, "image/jpeg")

        self.assertTrue(result.rewritten)
        assert result.normalized_bytes is not None
        with Image.open(BytesIO(result.normalized_bytes)) as image:
            self.assertEqual(image.size, (100, 200))
        orientation = _read_orientation_from_jpeg(result.normalized_bytes)
        self.assertIn(orientation, (None, 1))

    def test_orientation_3_rotates_180(self):
        raw = make_oriented_image(120, 80, 3, image_format="PNG")

        result = normalize_image_bytes_exif_orientation(raw, "image/png")

        self.assertTrue(result.rewritten)
        assert result.normalized_bytes is not None
        with Image.open(BytesIO(result.normalized_bytes)) as image:
            self.assertEqual(image.size, (120, 80))
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))

    def test_orientation_8_rotates_to_upright(self):
        raw = make_oriented_jpeg(160, 90, 8)

        result = normalize_image_bytes_exif_orientation(raw, "image/jpeg")

        self.assertTrue(result.rewritten)
        assert result.normalized_bytes is not None
        with Image.open(BytesIO(result.normalized_bytes)) as image:
            self.assertEqual(image.size, (90, 160))

    def test_mirrored_orientation_2_flips_pixels(self):
        raw = make_oriented_image(100, 40, 2, image_format="PNG")

        result = normalize_image_bytes_exif_orientation(raw, "image/png")

        self.assertTrue(result.rewritten)
        assert result.normalized_bytes is not None
        with Image.open(BytesIO(result.normalized_bytes)) as image:
            self.assertEqual(image.getpixel((0, 0)), (0, 0, 255))
            self.assertEqual(image.getpixel((99, 0)), (255, 0, 0))

    def test_missing_orientation_does_not_rewrite(self):
        raw = make_oriented_jpeg(80, 60, None)

        result = normalize_image_bytes_exif_orientation(raw, "image/jpeg")

        self.assertFalse(result.rewritten)
        self.assertIsNone(result.normalized_bytes)

    def test_orientation_1_does_not_rewrite(self):
        raw = make_oriented_jpeg(80, 60, 1)

        result = normalize_image_bytes_exif_orientation(raw, "image/jpeg")

        self.assertFalse(result.rewritten)
        self.assertIsNone(result.normalized_bytes)

    def test_rerunning_normalization_is_idempotent(self):
        raw = make_oriented_jpeg(120, 70, 6)
        first = normalize_image_bytes_exif_orientation(raw, "image/jpeg")
        assert first.normalized_bytes is not None

        second = normalize_image_bytes_exif_orientation(
            first.normalized_bytes,
            "image/jpeg",
        )

        self.assertFalse(second.rewritten)

    @patch("documents.services.exif_orientation.put_object_bytes")
    @patch("documents.services.exif_orientation.get_object_bytes")
    def test_s3_normalization_preserves_content_type(self, mock_get, mock_put):
        bucket = "test-bucket"
        key = "documents/1/original.jpeg"
        raw = make_oriented_jpeg(100, 60, 6)
        mock_get.return_value = (raw, "image/jpeg")

        result = normalize_uploaded_image_exif_in_s3(
            bucket=bucket,
            key=key,
            mime_type="image/jpeg",
        )

        self.assertTrue(result.rewritten)
        mock_put.assert_called_once()
        _, kwargs = mock_put.call_args
        self.assertEqual(kwargs["content_type"], "image/jpeg")

    @patch("documents.services.exif_orientation.put_object_bytes")
    @patch("documents.services.exif_orientation.get_object_bytes")
    def test_s3_normalization_skips_noop_images(self, mock_get, mock_put):
        raw = make_oriented_jpeg(50, 40, None)
        mock_get.return_value = (raw, "image/jpeg")

        result = normalize_uploaded_image_exif_in_s3(
            bucket="test-bucket",
            key="documents/1/original.jpeg",
            mime_type="image/jpeg",
        )

        self.assertFalse(result.rewritten)
        mock_put.assert_not_called()

    @patch("documents.services.exif_orientation.put_object_bytes")
    @patch("documents.services.exif_orientation.get_object_bytes")
    def test_s3_normalization_overwrites_same_key(self, mock_get, mock_put):
        bucket = "test-bucket"
        key = "documents/5/source/0.jpeg"
        raw = make_oriented_jpeg(90, 50, 6)
        mock_get.return_value = (raw, "image/jpeg")

        normalize_uploaded_image_exif_in_s3(
            bucket=bucket,
            key=key,
            mime_type="image/jpeg",
        )

        mock_put.assert_called_once_with(
            bucket=bucket,
            key=key,
            body=mock_put.call_args.kwargs["body"],
            content_type="image/jpeg",
        )


class PutObjectBytesTests(SimpleTestCase):
    @patch("documents.s3.get_s3_client")
    def test_put_object_bytes_sets_content_type(self, mock_get_client):
        put_object_bytes(
            "bucket",
            "documents/1/original.jpeg",
            b"abc",
            "image/jpeg",
        )

        mock_get_client.return_value.put_object.assert_called_once_with(
            Bucket="bucket",
            Key="documents/1/original.jpeg",
            Body=b"abc",
            ContentType="image/jpeg",
        )


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class ExifOrientationUploadFlowTests(TestCase):
    bucket = "test-bucket"

    def setUp(self):
        self.s3_store = InMemoryS3Store()
        self.get_patcher = patch(
            "documents.services.exif_orientation.get_object_bytes",
            side_effect=self.s3_store.get_object_bytes,
        )
        self.put_patcher = patch(
            "documents.services.exif_orientation.put_object_bytes",
            side_effect=self.s3_store.put_object_bytes,
        )
        self.head_patcher = patch(
            "documents.views.head_s3_object",
            side_effect=self.s3_store.head_s3_object,
        )
        self.get_patcher.start()
        self.put_patcher.start()
        self.head_patcher.start()
        self.addCleanup(self.get_patcher.stop)
        self.addCleanup(self.put_patcher.stop)
        self.addCleanup(self.head_patcher.stop)

        self.staff = User.objects.create_user(
            username="exif_upload_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _seed_document_image(
        self,
        *,
        key: str,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> Document:
        self.s3_store.seed(self.bucket, key, image_bytes, content_type)
        return create_ocr_document(
            title="EXIF upload test",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key=key,
            file_original_name="scan.jpg",
            mime_type=content_type,
            size_bytes=len(image_bytes),
        )

    def test_single_image_completion_normalizes_before_uploaded(self):
        key = "documents/10/original.jpeg"
        raw = make_oriented_jpeg(180, 100, 6)
        doc = self._seed_document_image(key=key, image_bytes=raw)

        with patch(
            "documents.views.enqueue_uploaded_document_processing"
        ) as mock_enqueue:
            resp = self.client.post(
                f"/api/uploads/{doc.id}/complete/",
                data=json.dumps(
                    {
                        "success": True,
                        "file_size": len(raw),
                        "file_mime": "image/jpeg",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["document_id"], doc.id)
        self.assertEqual(
            mock_enqueue.call_args.kwargs["initiated_by"].pk,
            self.staff.pk,
        )

        stored_bytes, stored_type = self.s3_store.objects[(self.bucket, key)]
        self.assertEqual(stored_type, "image/jpeg")
        with Image.open(BytesIO(stored_bytes)) as image:
            self.assertEqual(image.size, (100, 180))

        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.size_bytes, len(stored_bytes))

    def test_part_completion_normalizes_before_source_file_uploaded(self):
        doc = create_ocr_document(
            title="Incremental EXIF",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key="",
            mime_type="image/jpeg",
            expected_source_file_count=None,
        )
        key = f"documents/{doc.id}/source/0.jpeg"
        raw = make_oriented_jpeg(140, 90, 6)
        self.s3_store.seed(self.bucket, key, raw, "image/jpeg")
        source_file = DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key=key,
            file_original_name="page.jpg",
            mime_type="image/jpeg",
            size_bytes=len(raw),
            upload_status=DocumentSourceFile.UploadStatus.PENDING,
        )

        resp = self.client.post(
            f"/api/uploads/{doc.id}/parts/0/complete/",
            data=json.dumps(
                {
                    "success": True,
                    "file_size": len(raw),
                    "file_mime": "image/jpeg",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 200)
        source_file.refresh_from_db()
        self.assertEqual(
            source_file.upload_status,
            DocumentSourceFile.UploadStatus.UPLOADED,
        )
        stored_bytes, _ = self.s3_store.objects[(self.bucket, key)]
        with Image.open(BytesIO(stored_bytes)) as image:
            self.assertEqual(image.size, (90, 140))
        self.assertEqual(source_file.size_bytes, len(stored_bytes))

    def test_pdf_upload_is_untouched(self):
        key = "documents/11/original.pdf"
        pdf_bytes = b"%PDF-1.4 minimal"
        self.s3_store.seed(self.bucket, key, pdf_bytes, "application/pdf")
        doc = create_ocr_document(
            title="PDF EXIF test",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key=key,
            file_original_name="scan.pdf",
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
        )

        with patch(
            "documents.views.enqueue_uploaded_document_processing"
        ) as mock_enqueue:
            resp = self.client.post(
                f"/api/uploads/{doc.id}/complete/",
                data=json.dumps(
                    {
                        "success": True,
                        "file_size": len(pdf_bytes),
                        "file_mime": "application/pdf",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["document_id"], doc.id)
        self.assertEqual(
            mock_enqueue.call_args.kwargs["initiated_by"].pk,
            self.staff.pk,
        )
        stored_bytes, stored_type = self.s3_store.objects[(self.bucket, key)]
        self.assertEqual(stored_bytes, pdf_bytes)
        self.assertEqual(stored_type, "application/pdf")

    def test_normalization_failure_does_not_mark_uploaded_or_enqueue(self):
        key = "documents/12/original.jpeg"
        doc = self._seed_document_image(
            key=key,
            image_bytes=make_oriented_jpeg(100, 60, 6),
        )

        with (
            patch(
                "documents.views.normalize_uploaded_image_exif_in_s3",
                side_effect=ExifNormalizationError("transform failed"),
            ),
            patch(
                "documents.views.enqueue_uploaded_document_processing"
            ) as mock_enqueue,
        ):
            resp = self.client.post(
                f"/api/uploads/{doc.id}/complete/",
                data=json.dumps(
                    {
                        "success": True,
                        "file_size": 1000,
                        "file_mime": "image/jpeg",
                    }
                ),
                content_type="application/json",
            )

        self.assertEqual(resp.status_code, 500)
        body = resp.json()
        self.assertEqual(body["error"], "image exif normalization failed")
        self.assertEqual(body["document_id"], doc.id)
        self.assertNotIn("details", body)
        mock_enqueue.assert_not_called()

        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    def test_existing_s3_validation_still_runs_before_normalization(self):
        key = "documents/13/original.jpeg"
        doc = create_ocr_document(
            title="Missing S3 object",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key=key,
            file_original_name="scan.jpg",
            mime_type="image/jpeg",
            size_bytes=100,
        )

        resp = self.client.post(
            f"/api/uploads/{doc.id}/complete/",
            data=json.dumps(
                {
                    "success": True,
                    "file_size": 100,
                    "file_mime": "image/jpeg",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 object not found")

    def test_finalize_behavior_unchanged_after_normalized_parts(self):
        with patch(
            "documents.views.create_presigned_put",
            return_value="https://example/upload",
        ):
            create_resp = self.client.post(
                "/api/uploads/create/",
                data=json.dumps(
                    {
                        "title": "Incremental finalize",
                        "text_input_type": "HANDWRITTEN",
                        "incremental": True,
                    }
                ),
                content_type="application/json",
            )
        doc_id = create_resp.json()["document_id"]

        with patch(
            "documents.views.create_presigned_put",
            return_value="https://example/upload",
        ):
            add_resp = self.client.post(
                f"/api/uploads/{doc_id}/parts/add/",
                data=json.dumps(
                    {
                        "original_name": "page.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 1000,
                    }
                ),
                content_type="application/json",
            )
        key = add_resp.json()["s3_key"]
        self.s3_store.seed(
            self.bucket,
            key,
            make_oriented_jpeg(120, 70, 6),
            "image/jpeg",
        )

        self.client.post(
            f"/api/uploads/{doc_id}/parts/0/complete/",
            data=json.dumps(
                {
                    "success": True,
                    "file_size": 1000,
                    "file_mime": "image/jpeg",
                }
            ),
            content_type="application/json",
        )

        with patch(
            "documents.views.enqueue_uploaded_document_processing"
        ) as mock_enqueue:
            finalize_resp = self.client.post(
                f"/api/uploads/{doc_id}/finalize/",
                data=json.dumps({"success": True}),
                content_type="application/json",
            )

        self.assertEqual(finalize_resp.status_code, 200)
        mock_enqueue.assert_called_once()
        self.assertEqual(mock_enqueue.call_args.kwargs["document_id"], doc_id)
        self.assertEqual(
            mock_enqueue.call_args.kwargs["initiated_by"].pk,
            self.staff.pk,
        )
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)

    @patch("documents.views.enqueue_uploaded_document_processing")
    def test_content_type_alias_is_preserved_on_rewrite(self, _mock_enqueue):
        key = "documents/14/original.jpeg"
        raw = make_oriented_jpeg(110, 70, 6)
        self.s3_store.seed(self.bucket, key, raw, "image/jpg")
        doc = self._seed_document_image(
            key=key,
            image_bytes=raw,
            content_type="image/jpeg",
        )

        self.client.post(
            f"/api/uploads/{doc.id}/complete/",
            data=json.dumps(
                {
                    "success": True,
                    "file_size": len(raw),
                    "file_mime": "image/jpeg",
                }
            ),
            content_type="application/json",
        )

        _, stored_type = self.s3_store.objects[(self.bucket, key)]
        self.assertEqual(
            normalize_upload_mime_type(stored_type),
            "image/jpeg",
        )
