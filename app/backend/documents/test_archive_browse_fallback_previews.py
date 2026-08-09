"""Regression tests for local 160px archive browse-card fallback previews."""

from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, Document, PhotoContent
from documents.services.archive_item_presentation import (
    ARCHIVE_BROWSE_FALLBACK_PREVIEW_MANUAL,
    ARCHIVE_BROWSE_FALLBACK_PREVIEW_PDF,
    ARCHIVE_BROWSE_FALLBACK_PREVIEW_VIDEO,
    build_archive_browse_card,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
)


YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
THUMBNAIL_PRESIGNED_URL = "https://s3.example/presigned-thumb"


def _ocr_image_item(
    *,
    title: str,
    thumbnail_file_key: str = "",
    file_s3_key: str = "documents/1/source.jpg",
) -> ArchiveItem:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.PRINTED,
        visibility=Document.Visibility.PUBLIC,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key=file_s3_key,
        thumbnail_file_key=thumbnail_file_key,
    )
    return doc.archive_item


def _ocr_pdf_item(*, title: str) -> ArchiveItem:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        visibility=Document.Visibility.PUBLIC,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key="documents/2/source.pdf",
    )
    return doc.archive_item


def _create_photo_archive_item(
    *,
    title: str,
    thumbnail_file_key: str = "",
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key="photos/1/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        thumbnail_file_key=thumbnail_file_key,
        thumbnail_mime_type="image/jpeg" if thumbnail_file_key else "",
        thumbnail_size_bytes=512 if thumbnail_file_key else None,
    )
    return item


class ArchiveBrowseFallbackPreviewPresentationTests(TestCase):
    def test_pdf_ocr_card_sets_pdf_fallback_preview(self):
        item = _ocr_pdf_item(title="PDF fallback presentation")
        card = build_archive_browse_card(item)
        self.assertEqual(card.fallback_preview, ARCHIVE_BROWSE_FALLBACK_PREVIEW_PDF)
        self.assertEqual(card.type_marker, "ocr")
        self.assertIsNone(card.thumbnail_url)

    def test_image_ocr_card_has_no_fallback_preview(self):
        item = _ocr_image_item(title="Image OCR no fallback")
        card = build_archive_browse_card(item)
        self.assertEqual(card.fallback_preview, "")
        self.assertEqual(card.type_marker, "ocr")

    def test_manual_text_card_sets_manual_fallback_preview(self):
        item = create_manual_text_archive_item(
            title="Manual fallback presentation",
            body="Body for manual fallback",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        card = build_archive_browse_card(item)
        self.assertEqual(card.fallback_preview, ARCHIVE_BROWSE_FALLBACK_PREVIEW_MANUAL)

    def test_video_card_sets_video_fallback_preview(self):
        item = create_video_archive_item(
            title="Video fallback presentation",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        card = build_archive_browse_card(item)
        self.assertEqual(card.fallback_preview, ARCHIVE_BROWSE_FALLBACK_PREVIEW_VIDEO)

    def test_photo_card_has_no_fallback_preview(self):
        item = _create_photo_archive_item(title="Photo no fallback")
        card = build_archive_browse_card(item)
        self.assertEqual(card.fallback_preview, "")
        self.assertEqual(card.type_marker, "photo")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class ArchiveBrowseFallbackPreviewRenderTests(TestCase):
    def test_pdf_ocr_renders_160px_document_style_fallback(self):
        item = _ocr_pdf_item(title="PDF browse fallback card")
        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()
        self.assertContains(resp, item.title)
        self.assertContains(resp, "archive-browse-card--fallback-preview")
        self.assertContains(resp, "archive-browse-card__fallback-preview--pdf")
        self.assertContains(resp, 'aria-label="מסמך"')
        self.assertContains(resp, 'aria-hidden="true"')
        self.assertNotContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__document-preview")
        self.assertNotContains(resp, "<iframe", html=False)

    def test_manual_text_renders_160px_paper_style_fallback(self):
        item = create_manual_text_archive_item(
            title="Manual browse fallback card",
            body="Snippet for the card preview area.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)
        self.assertContains(resp, "Snippet for the card preview area.")
        self.assertContains(resp, "archive-browse-card__fallback-preview--manual")
        self.assertContains(resp, 'aria-label="טקסט"')
        self.assertNotContains(resp, "archive-browse-card__marker--manual")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")
        self.assertNotContains(resp, "archive-browse-card__document-preview")

    def test_video_renders_local_play_fallback_without_remote_media(self):
        item = create_video_archive_item(
            title="Video browse fallback card",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            public_note="Local note only",
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)
        self.assertContains(resp, "Local note only")
        self.assertContains(resp, "archive-browse-card__fallback-preview--video")
        self.assertContains(resp, "archive-browse-card__fallback-preview-play")
        self.assertContains(resp, 'aria-label="סרטון"')
        self.assertNotContains(resp, "archive-browse-card__marker--video")
        self.assertNotContains(resp, "i.ytimg.com")
        self.assertNotContains(resp, "img.youtube.com")
        self.assertNotContains(resp, "youtube.com/vi/")
        self.assertNotContains(resp, "<iframe", html=False)
        self.assertNotContains(resp, "<video", html=False)

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_photo_with_thumbnail_layout_unchanged(self, mock_presigned_get):
        photo = _create_photo_archive_item(
            title="Photo thumbnail protection",
            thumbnail_file_key="photos/42/thumb_400.jpg",
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once()
        self.assertContains(resp, photo.title)
        self.assertContains(resp, THUMBNAIL_PRESIGNED_URL)
        self.assertContains(resp, 'class="archive-browse-card__photo-preview"')
        self.assertContains(resp, 'class="archive-browse-card__photo-preview-image"')
        self.assertNotContains(resp, "archive-browse-card--fallback-preview")
        self.assertNotContains(resp, "archive-browse-card__fallback-preview")

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_image_ocr_with_thumbnail_layout_unchanged(self, mock_presigned_get):
        item = _ocr_image_item(
            title="Image OCR thumbnail protection",
            thumbnail_file_key="documents/42/thumb_400.jpg",
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once()
        self.assertContains(resp, item.title)
        self.assertContains(resp, THUMBNAIL_PRESIGNED_URL)
        self.assertContains(resp, 'class="archive-browse-card__document-preview"')
        self.assertContains(resp, 'class="archive-browse-card__document-preview-image"')
        self.assertNotContains(resp, "archive-browse-card--fallback-preview")
        self.assertNotContains(resp, "archive-browse-card__fallback-preview")

    def test_image_ocr_without_thumbnail_keeps_small_marker(self):
        item = _ocr_image_item(title="Image OCR marker still used")
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)
        self.assertContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__fallback-preview")
        self.assertNotContains(resp, "archive-browse-card__document-preview")

    def test_photo_without_thumbnail_keeps_small_marker(self):
        photo = _create_photo_archive_item(title="Photo marker still used")
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, photo.title)
        self.assertContains(resp, "archive-browse-card__marker--photo")
        self.assertNotContains(resp, "archive-browse-card__fallback-preview")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")


class ArchiveBrowseFallbackPreviewStyleTests(SimpleTestCase):
    def test_fallback_preview_css_uses_exact_160px_height(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        block_start = css.index(".archive-browse-card__fallback-preview {")
        block = css[block_start : css.index("}", block_start) + 1]
        self.assertIn("height: 160px;", block)

    def test_existing_photo_and_document_thumbnail_css_unchanged(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")

        photo_start = css.index(".archive-browse-card__photo-preview {")
        photo_block = css[photo_start : css.index("}", photo_start) + 1]
        self.assertIn("height: 160px;", photo_block)

        photo_image_start = css.index(".archive-browse-card__photo-preview-image")
        photo_image_block = css[
            photo_image_start : css.index("}", photo_image_start) + 1
        ]
        self.assertIn("object-fit: cover", photo_image_block)
        self.assertIn("object-position: top center", photo_image_block)

        document_start = css.index(".archive-browse-card__document-preview {")
        document_block = css[document_start : css.index("}", document_start) + 1]
        self.assertIn("height: 160px;", document_block)

        document_image_start = css.index(".archive-browse-card__document-preview-image")
        document_image_block = css[
            document_image_start : css.index("}", document_image_start) + 1
        ]
        self.assertIn("object-fit: cover", document_image_block)
        self.assertIn("object-position: top center", document_image_block)
