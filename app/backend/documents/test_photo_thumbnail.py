"""Photo thumbnail generation on PHOTO upload finalize."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.db import transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from PIL import Image

from documents.models import ArchiveItem, PhotoContent
from documents.s3 import S3HeadObjectResult, build_photo_thumbnail_s3_key
from documents.services.image_thumbnail import (
    THUMBNAIL_JPEG_MIME,
    THUMBNAIL_MAX_EDGE,
    compute_thumbnail_dimensions,
)
from documents.services.photo_thumbnail import (
    generate_and_persist_photo_thumbnail,
    generate_photo_thumbnail_bytes,
)
from documents.services.photo_upload import (
    _finalize_photo_upload_in_transaction,
    finalize_photo_upload,
)
from documents.test_exif_orientation import make_oriented_jpeg


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


class BuildPhotoThumbnailS3KeyTests(SimpleTestCase):
    def test_deterministic_thumbnail_key(self):
        self.assertEqual(build_photo_thumbnail_s3_key(42), "photos/42/thumb_400.jpg")
        self.assertEqual(build_photo_thumbnail_s3_key(7), "photos/7/thumb_400.jpg")


class ComputeThumbnailDimensionsTests(SimpleTestCase):
    def test_landscape_resize_preserves_aspect_ratio(self):
        width, height = compute_thumbnail_dimensions(1200, 800, max_edge=400)
        self.assertEqual(width, 400)
        self.assertEqual(height, 267)

    def test_portrait_resize_preserves_aspect_ratio(self):
        width, height = compute_thumbnail_dimensions(800, 1200, max_edge=400)
        self.assertEqual(width, 267)
        self.assertEqual(height, 400)

    def test_smaller_image_keeps_original_dimensions(self):
        width, height = compute_thumbnail_dimensions(320, 240, max_edge=400)
        self.assertEqual((width, height), (320, 240))


class GeneratePhotoThumbnailBytesTests(SimpleTestCase):
    def test_exif_transpose_records_transposed_dimensions_and_jpeg_size(self):
        image_bytes = make_oriented_jpeg(800, 600, 6)
        jpeg_bytes, width, height = generate_photo_thumbnail_bytes(image_bytes)
        self.assertEqual((width, height), (600, 800))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.size, (300, 400))

    def test_thumbnail_bytes_are_jpeg_within_max_edge(self):
        image_bytes = _solid_jpeg_bytes(900, 600)
        jpeg_bytes, width, height = generate_photo_thumbnail_bytes(image_bytes)
        self.assertEqual((width, height), (900, 600))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertLessEqual(max(thumb.size), THUMBNAIL_MAX_EDGE)

    def test_rgba_png_is_encoded_as_rgb_jpeg(self):
        png_bytes = _solid_rgba_png_bytes(64, 48)
        jpeg_bytes, width, height = generate_photo_thumbnail_bytes(png_bytes)
        self.assertEqual((width, height), (64, 48))
        with Image.open(BytesIO(jpeg_bytes)) as thumb:
            self.assertEqual(thumb.mode, "RGB")
            self.assertEqual(thumb.format, "JPEG")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class GenerateAndPersistPhotoThumbnailTests(TestCase):
    def setUp(self):
        self.item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Thumbnail test photo",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.photo_content = PhotoContent.objects.create(
            archive_item=self.item,
            original_file_key="",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
            upload_error="",
        )
        self.photo_content.original_file_key = (
            f"photos/{self.photo_content.id}/original.jpg"
        )
        self.photo_content.save(update_fields=["original_file_key", "updated_at"])

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=12345)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(1200, 800), "image/jpeg"),
    )
    def test_persists_original_dimensions_and_thumbnail_metadata(
        self, _mock_get, mock_put
    ):
        result = generate_and_persist_photo_thumbnail(
            self.photo_content,
            bucket="test-uploads-bucket",
        )
        self.assertIsNotNone(result)

        self.photo_content.refresh_from_db()
        self.assertEqual(self.photo_content.width, 1200)
        self.assertEqual(self.photo_content.height, 800)
        self.assertEqual(
            self.photo_content.thumbnail_file_key,
            build_photo_thumbnail_s3_key(self.photo_content.id),
        )
        self.assertEqual(self.photo_content.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(self.photo_content.thumbnail_size_bytes, 12345)

        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args.kwargs
        self.assertEqual(call_kwargs["bucket"], "test-uploads-bucket")
        self.assertEqual(
            call_kwargs["key"],
            build_photo_thumbnail_s3_key(self.photo_content.id),
        )
        self.assertEqual(call_kwargs["content_type"], THUMBNAIL_JPEG_MIME)
        with Image.open(BytesIO(call_kwargs["body"])) as thumb:
            self.assertEqual(thumb.format, "JPEG")
            self.assertEqual(thumb.size, (400, 267))

    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        side_effect=ClientError(
            {"Error": {"Code": "500", "Message": "fail"}},
            "GetObject",
        ),
    )
    def test_failure_returns_none_and_leaves_thumbnail_fields_empty(self, _mock_get):
        result = generate_and_persist_photo_thumbnail(
            self.photo_content,
            bucket="test-uploads-bucket",
        )
        self.assertIsNone(result)

        self.photo_content.refresh_from_db()
        self.assertIsNone(self.photo_content.width)
        self.assertIsNone(self.photo_content.height)
        self.assertEqual(self.photo_content.thumbnail_file_key, "")
        self.assertEqual(self.photo_content.thumbnail_mime_type, "")
        self.assertIsNone(self.photo_content.thumbnail_size_bytes)

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=9999)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(800, 600), "image/jpeg"),
    )
    def test_metadata_save_failure_returns_none_after_s3_upload(
        self, _mock_get, mock_put
    ):
        original_save = PhotoContent.save

        def save_raises_for_thumbnail_metadata(self, *args, **kwargs):
            update_fields = kwargs.get("update_fields") or ()
            if "thumbnail_file_key" in update_fields:
                raise RuntimeError("db save failed")
            return original_save(self, *args, **kwargs)

        with patch.object(PhotoContent, "save", save_raises_for_thumbnail_metadata):
            result = generate_and_persist_photo_thumbnail(
                self.photo_content,
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(result)
        mock_put.assert_called_once()
        self.assertIsNone(self.photo_content.width)
        self.assertIsNone(self.photo_content.height)
        self.assertEqual(self.photo_content.thumbnail_file_key, "")
        self.assertEqual(self.photo_content.thumbnail_mime_type, "")
        self.assertIsNone(self.photo_content.thumbnail_size_bytes)
        self.photo_content.refresh_from_db()
        self.assertIsNone(self.photo_content.width)
        self.assertEqual(self.photo_content.thumbnail_file_key, "")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class FinalizePhotoUploadThumbnailIntegrationTests(TestCase):
    S3_CONTENT_LENGTH = 8192

    def setUp(self):
        self.item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Finalize thumbnail photo",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.photo_content = PhotoContent.objects.create(
            archive_item=self.item,
            original_file_key="",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
            upload_error="",
        )
        self.photo_content.original_file_key = (
            f"photos/{self.photo_content.id}/original.jpg"
        )
        self.photo_content.save(update_fields=["original_file_key", "updated_at"])

    @patch("documents.services.photo_upload.generate_and_persist_photo_thumbnail")
    @patch("documents.services.photo_upload._finalize_photo_upload_in_transaction")
    def test_thumbnail_generation_runs_after_finalize_transaction(
        self, mock_finalize_tx, mock_thumbnail
    ):
        self.photo_content.upload_status = PhotoContent.UploadStatus.UPLOADED
        self.photo_content.original_size_bytes = 4096
        self.photo_content.save()

        call_order: list[str] = []

        def finalize_side_effect(*args, **kwargs):
            call_order.append("finalize_tx")
            return self.photo_content, None, True

        mock_finalize_tx.side_effect = finalize_side_effect

        def thumbnail_side_effect(*args, **kwargs):
            call_order.append("thumbnail")
            return None

        mock_thumbnail.side_effect = thumbnail_side_effect

        finalize_photo_upload(
            self.photo_content,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )

        self.assertEqual(call_order, ["finalize_tx", "thumbnail"])
        mock_thumbnail.assert_called_once()

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=5555)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(800, 1200), "image/jpeg"),
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/jpeg",
            content_length=S3_CONTENT_LENGTH,
        ),
    )
    def test_finalize_persists_upload_and_thumbnail_metadata(
        self, _mock_head, _mock_get, _mock_put
    ):
        photo_content, verify_err = finalize_photo_upload(
            self.photo_content,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )
        self.assertIsNone(verify_err)

        photo_content.refresh_from_db()
        self.assertEqual(
            photo_content.upload_status, PhotoContent.UploadStatus.UPLOADED
        )
        self.assertEqual(photo_content.original_size_bytes, self.S3_CONTENT_LENGTH)
        self.assertEqual(photo_content.width, 800)
        self.assertEqual(photo_content.height, 1200)
        self.assertEqual(
            photo_content.thumbnail_file_key,
            build_photo_thumbnail_s3_key(photo_content.id),
        )
        self.assertEqual(photo_content.thumbnail_mime_type, THUMBNAIL_JPEG_MIME)
        self.assertEqual(photo_content.thumbnail_size_bytes, 5555)

    @patch("documents.services.photo_thumbnail.put_object_bytes", return_value=5555)
    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        return_value=(_solid_jpeg_bytes(800, 1200), "image/jpeg"),
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/jpeg",
            content_length=4096,
        ),
    )
    def test_metadata_save_failure_still_leaves_upload_successful(
        self, _mock_head, _mock_get, mock_put
    ):
        original_save = PhotoContent.save

        def save_raises_for_thumbnail_metadata(self, *args, **kwargs):
            update_fields = kwargs.get("update_fields") or ()
            if "thumbnail_file_key" in update_fields:
                raise RuntimeError("db save failed")
            return original_save(self, *args, **kwargs)

        with patch.object(PhotoContent, "save", save_raises_for_thumbnail_metadata):
            photo_content, verify_err = finalize_photo_upload(
                self.photo_content,
                bucket="test-uploads-bucket",
                success=True,
                file_mime="image/jpeg",
            )

        self.assertIsNone(verify_err)
        mock_put.assert_called_once()
        self.assertEqual(
            photo_content.upload_status, PhotoContent.UploadStatus.UPLOADED
        )
        self.assertEqual(photo_content.original_size_bytes, 4096)
        self.assertEqual(photo_content.thumbnail_file_key, "")
        self.assertEqual(photo_content.thumbnail_mime_type, "")
        self.assertIsNone(photo_content.width)
        self.assertIsNone(photo_content.height)
        self.assertIsNone(photo_content.thumbnail_size_bytes)
        photo_content.refresh_from_db()
        self.assertEqual(
            photo_content.upload_status, PhotoContent.UploadStatus.UPLOADED
        )
        self.assertEqual(photo_content.original_size_bytes, 4096)
        self.assertEqual(photo_content.thumbnail_file_key, "")
        self.assertIsNone(photo_content.width)

    @patch(
        "documents.services.photo_thumbnail.get_object_bytes",
        side_effect=ClientError(
            {"Error": {"Code": "500", "Message": "fail"}},
            "GetObject",
        ),
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/jpeg",
            content_length=4096,
        ),
    )
    def test_thumbnail_failure_still_leaves_upload_successful(
        self, _mock_head, _mock_get
    ):
        photo_content, verify_err = finalize_photo_upload(
            self.photo_content,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )
        self.assertIsNone(verify_err)

        photo_content.refresh_from_db()
        self.assertEqual(
            photo_content.upload_status, PhotoContent.UploadStatus.UPLOADED
        )
        self.assertEqual(photo_content.original_size_bytes, 4096)
        self.assertEqual(photo_content.thumbnail_file_key, "")
        self.assertIsNone(photo_content.width)

    @patch("documents.services.photo_upload.generate_and_persist_photo_thumbnail")
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/jpeg",
            content_length=4096,
        ),
    )
    def test_already_uploaded_complete_does_not_regenerate_thumbnail(
        self, mock_head, mock_thumbnail
    ):
        self.photo_content.upload_status = PhotoContent.UploadStatus.UPLOADED
        self.photo_content.original_size_bytes = 4096
        self.photo_content.thumbnail_file_key = build_photo_thumbnail_s3_key(
            self.photo_content.id
        )
        self.photo_content.save()

        photo_content, verify_err = finalize_photo_upload(
            self.photo_content,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )
        self.assertIsNone(verify_err)
        mock_thumbnail.assert_not_called()
        mock_head.assert_not_called()

    @patch("documents.services.photo_upload.generate_and_persist_photo_thumbnail")
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/png",
            content_length=2048,
        ),
    )
    def test_s3_content_type_mismatch_still_fails_without_thumbnail(
        self, _mock_head, mock_thumbnail
    ):
        photo_content, verify_err = finalize_photo_upload(
            self.photo_content,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )
        self.assertIsNotNone(verify_err)
        assert verify_err is not None
        self.assertEqual(verify_err.message, "s3 content type mismatch")

        photo_content.refresh_from_db()
        self.assertEqual(photo_content.upload_status, PhotoContent.UploadStatus.FAILED)
        mock_thumbnail.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class FinalizePhotoUploadThumbnailTransactionBoundaryTests(TransactionTestCase):
    def setUp(self):
        self.item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Transaction boundary photo",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.photo_content = PhotoContent.objects.create(
            archive_item=self.item,
            original_file_key="",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
            upload_error="",
        )
        self.photo_content.original_file_key = (
            f"photos/{self.photo_content.id}/original.jpg"
        )
        self.photo_content.save(update_fields=["original_file_key", "updated_at"])

    @patch("documents.services.photo_upload.generate_and_persist_photo_thumbnail")
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=S3HeadObjectResult(
            exists=True,
            content_type="image/jpeg",
            content_length=4096,
        ),
    )
    def test_thumbnail_runs_outside_finalize_atomic_block(self, _mock_head, mock_thumb):
        call_order: list[str] = []
        real_finalize = _finalize_photo_upload_in_transaction

        def finalize_wrapper(*args, **kwargs):
            result = real_finalize(*args, **kwargs)
            call_order.append("finalize_tx")
            return result

        def thumbnail_side_effect(photo_content, *, bucket):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            call_order.append("thumbnail")
            return None

        mock_thumb.side_effect = thumbnail_side_effect

        with patch(
            "documents.services.photo_upload._finalize_photo_upload_in_transaction",
            side_effect=finalize_wrapper,
        ):
            finalize_photo_upload(
                self.photo_content,
                bucket="test-uploads-bucket",
                success=True,
                file_mime="image/jpeg",
            )

        self.assertEqual(call_order, ["finalize_tx", "thumbnail"])
        mock_thumb.assert_called_once()
