"""Unit and integration tests for OCR document browse-card thumbnail enrichment."""

from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib.auth.models import User
from django.conf import settings
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import ArchiveCategory, ArchiveItem, Document
from documents.services.archive_item_presentation import build_archive_browse_card
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.document_archive_urls import (
    apply_document_thumbnail_urls_to_browse_cards,
)
from documents.views import (
    _archive_browse_cards_for_items,
    _archive_browse_select_related,
)
from documents.services.archive_item_access import archive_browse_queryset_for_user


def _ocr_image_item(
    *,
    title: str,
    thumbnail_file_key: str = "",
    file_s3_key: str = "documents/1/source.jpg",
    upload_status=Document.UploadStatus.UPLOADED,
) -> ArchiveItem:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.PRINTED,
        visibility=Document.Visibility.PUBLIC,
        upload_status=upload_status,
        file_s3_key=file_s3_key,
        thumbnail_file_key=thumbnail_file_key,
    )
    return doc.archive_item


def _ocr_pdf_item(
    *,
    title: str,
    thumbnail_file_key: str = "",
    file_s3_key: str = "documents/2/source.pdf",
) -> ArchiveItem:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        visibility=Document.Visibility.PUBLIC,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key=file_s3_key,
        thumbnail_file_key=thumbnail_file_key,
    )
    return doc.archive_item


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class DocumentArchiveBrowseThumbnailServiceTests(TestCase):
    def test_enriches_image_ocr_card_with_presigned_thumbnail_url(self):
        item = _ocr_image_item(
            title="Service document thumbnail",
            thumbnail_file_key="documents/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
            return_value="https://s3.example/doc-thumb",
        ) as mock_presigned_get:
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
                expires_in=3600,
            )

        self.assertEqual(enriched[0].thumbnail_url, "https://s3.example/doc-thumb")
        mock_presigned_get.assert_called_once_with(
            bucket="test-uploads-bucket",
            key="documents/1/thumb_400.jpg",
            expires_in=3600,
        )

    def test_skips_presign_when_bucket_missing(self):
        item = _ocr_image_item(
            title="No bucket document",
            thumbnail_file_key="documents/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="",
            )

        self.assertIsNone(enriched[0].thumbnail_url)
        mock_presigned_get.assert_not_called()

    def test_presign_failure_leaves_thumbnail_url_none(self):
        item = _ocr_image_item(
            title="Presign failure document",
            thumbnail_file_key="documents/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
            side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetObject",
            ),
        ):
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(enriched[0].thumbnail_url)

    def test_missing_thumbnail_key_skips_presign(self):
        item = _ocr_image_item(
            title="Document without thumbnail",
            file_s3_key="documents/9/source.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(enriched[0].thumbnail_url)
        mock_presigned_get.assert_not_called()

    def test_pdf_document_never_presigns(self):
        item = _ocr_pdf_item(
            title="PDF document browse",
            thumbnail_file_key="documents/2/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(enriched[0].thumbnail_url)
        mock_presigned_get.assert_not_called()

    def test_only_thumbnail_key_is_presigned_not_source(self):
        source_key = "documents/55/source.jpg"
        thumb_key = "documents/55/thumb_400.jpg"
        item = _ocr_image_item(
            title="Source key guard document",
            thumbnail_file_key=thumb_key,
            file_s3_key=source_key,
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
            return_value="https://s3.example/doc-thumb",
        ) as mock_presigned_get:
            apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        mock_presigned_get.assert_called_once()
        self.assertEqual(mock_presigned_get.call_args.kwargs["key"], thumb_key)
        self.assertNotEqual(mock_presigned_get.call_args.kwargs["key"], source_key)

    def test_manual_text_card_unchanged(self):
        manual_item = create_manual_text_archive_item(
            title="Manual service unchanged",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        card = build_archive_browse_card(manual_item)

        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            enriched = apply_document_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(enriched[0].thumbnail_url)
        mock_presigned_get.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class DocumentArchiveBrowseThumbnailIntegrationTests(TestCase):
    THUMBNAIL_PRESIGNED_URL = "https://s3.example/presigned-doc-thumb"
    THUMBNAIL_KEY = "documents/42/thumb_400.jpg"

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_image_ocr_with_thumbnail_presigns_and_renders_document_preview(
        self, mock_presigned_get
    ):
        item = _ocr_image_item(
            title="Image OCR with browse thumbnail",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once_with(
            bucket="test-uploads-bucket",
            key=self.THUMBNAIL_KEY,
            expires_in=3600,
        )
        self.assertContains(resp, item.title)
        self.assertContains(resp, self.THUMBNAIL_PRESIGNED_URL)
        self.assertContains(resp, 'class="archive-browse-card__document-preview"')
        self.assertContains(resp, 'class="archive-browse-card__document-preview-image"')
        self.assertContains(resp, 'alt="Image OCR with browse thumbnail"')
        self.assertNotContains(resp, "archive-browse-card__marker--ocr")

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_archive_list_never_presigns_original_source_key(self, mock_presigned_get):
        _ocr_image_item(
            title="Document original key guard",
            file_s3_key="documents/77/source.jpg",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once()
        for call in mock_presigned_get.call_args_list:
            self.assertNotEqual(call.kwargs.get("key"), "documents/77/source.jpg")
            self.assertEqual(call.kwargs.get("key"), self.THUMBNAIL_KEY)

    def test_image_ocr_without_thumbnail_keeps_css_marker_fallback(self):
        item = _ocr_image_item(
            title="Image OCR without browse thumbnail",
            file_s3_key="documents/99/source.jpg",
        )
        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
            return_value="https://s3.example/should-not-be-used",
        ) as mock_presigned_get:
            resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()
        self.assertContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__document-preview")
        self.assertContains(resp, item.title)

    def test_pdf_document_keeps_css_marker_fallback(self):
        item = _ocr_pdf_item(
            title="PDF OCR browse marker",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        with patch(
            "documents.services.document_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()
        self.assertContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__document-preview")
        self.assertContains(resp, item.title)

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetObject",
        ),
    )
    def test_presign_failure_renders_css_marker_fallback(self, _mock_presigned_get):
        item = _ocr_image_item(
            title="Document presign failure fallback",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)
        self.assertContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__document-preview")

    @override_settings(UPLOADS_BUCKET_NAME="")
    def test_missing_bucket_config_keeps_css_marker_fallback(self):
        item = _ocr_image_item(
            title="Document missing bucket fallback",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)
        self.assertContains(resp, "archive-browse-card__marker--ocr")
        self.assertNotContains(resp, "archive-browse-card__document-preview")

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_manual_text_remains_unchanged(self, mock_presigned_get):
        manual_item = create_manual_text_archive_item(
            title="Manual browse unchanged with docs",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, manual_item.title)
        self.assertContains(resp, "archive-browse-card__marker--manual")
        self.assertNotContains(resp, "archive-browse-card__document-preview")
        self.assertNotContains(resp, 'alt="Manual browse unchanged with docs"')
        mock_presigned_get.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class DocumentArchiveBrowseQueryCountTests(TestCase):
    def _browse_cards_query_count(self, *, item_count: int) -> int:
        ArchiveItem.objects.all().delete()
        category = ArchiveCategory.objects.create(
            name=f"Browse query category {item_count}",
            slug=f"browse-query-category-{item_count}",
        )
        for index in range(item_count):
            item = _ocr_image_item(
                title=f"Query count document {index}",
                thumbnail_file_key=f"documents/{index}/thumb_400.jpg",
                file_s3_key=f"documents/{index}/source.jpg",
            )
            item.categories.add(category)

        user = User.objects.create_user(username=f"browse_q_{item_count}", password="x")
        queryset = _archive_browse_select_related(
            archive_browse_queryset_for_user(user)
        ).order_by("-created_at")

        with CaptureQueriesContext(connection) as context:
            list(_archive_browse_cards_for_items(list(queryset)))

        return len(context)

    @patch(
        "documents.services.document_archive_urls.create_presigned_get",
        return_value="https://s3.example/doc-thumb",
    )
    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value="https://s3.example/photo-thumb",
    )
    def test_browse_card_pipeline_query_count_stable_with_more_items(
        self, _mock_photo_presign, _mock_doc_presign
    ):
        count_for_three = self._browse_cards_query_count(item_count=3)
        count_for_six = self._browse_cards_query_count(item_count=6)
        self.assertEqual(count_for_three, count_for_six)


class DocumentArchiveBrowsePreviewStyleTests(SimpleTestCase):
    def test_document_preview_css_fills_preview_area_from_top(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        image_block_start = css.index(".archive-browse-card__document-preview-image")
        image_block = css[image_block_start : css.index("}", image_block_start) + 1]

        self.assertIn("object-fit: cover", image_block)
        self.assertIn("object-position: top center", image_block)
        self.assertNotIn("object-fit: contain", image_block)
        self.assertNotIn("border:", image_block)
        self.assertNotIn("box-shadow:", image_block)
