"""Unit tests for PHOTO browse-card thumbnail URL enrichment."""

from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import TestCase, override_settings

from documents.models import ArchiveItem, PhotoContent
from documents.services.archive_item_presentation import build_archive_browse_card
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.photo_archive_urls import (
    apply_photo_thumbnail_urls_to_browse_cards,
)


def _photo_item(
    *,
    title: str,
    thumbnail_file_key: str = "",
    original_file_key: str = "photos/1/original.jpg",
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=original_file_key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        thumbnail_file_key=thumbnail_file_key,
    )
    return item


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveBrowseThumbnailServiceTests(TestCase):
    def test_enriches_photo_card_with_presigned_thumbnail_url(self):
        item = _photo_item(
            title="Service thumbnail photo",
            thumbnail_file_key="photos/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            return_value="https://s3.example/thumb",
        ) as mock_presigned_get:
            enriched = apply_photo_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
                expires_in=3600,
            )

        self.assertEqual(enriched[0].thumbnail_url, "https://s3.example/thumb")
        mock_presigned_get.assert_called_once_with(
            bucket="test-uploads-bucket",
            key="photos/1/thumb_400.jpg",
            expires_in=3600,
        )

    def test_skips_presign_when_bucket_missing(self):
        item = _photo_item(
            title="No bucket photo",
            thumbnail_file_key="photos/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.photo_archive_urls.create_presigned_get",
        ) as mock_presigned_get:
            enriched = apply_photo_thumbnail_urls_to_browse_cards(
                [card],
                bucket="",
            )

        self.assertIsNone(enriched[0].thumbnail_url)
        mock_presigned_get.assert_not_called()

    def test_presign_failure_leaves_thumbnail_url_none(self):
        item = _photo_item(
            title="Presign failure photo",
            thumbnail_file_key="photos/1/thumb_400.jpg",
        )
        card = build_archive_browse_card(item)

        with patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            side_effect=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetObject",
            ),
        ):
            enriched = apply_photo_thumbnail_urls_to_browse_cards(
                [card],
                bucket="test-uploads-bucket",
            )

        self.assertIsNone(enriched[0].thumbnail_url)

    def test_mixed_list_presigns_only_photo_thumbnails_in_order(self):
        thumb_a = "photos/10/thumb_400.jpg"
        thumb_b = "photos/40/thumb_400.jpg"
        original_a = "photos/10/original.jpg"
        original_no_thumb = "photos/30/original.jpg"
        original_b = "photos/40/original.jpg"

        photo_a = _photo_item(
            title="Photo thumbnail A",
            thumbnail_file_key=thumb_a,
            original_file_key=original_a,
        )
        manual_item = create_manual_text_archive_item(
            title="Manual mixed-list card",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo_no_thumb = _photo_item(
            title="Photo without thumbnail",
            original_file_key=original_no_thumb,
        )
        photo_b = _photo_item(
            title="Photo thumbnail B",
            thumbnail_file_key=thumb_b,
            original_file_key=original_b,
        )

        cards = [
            build_archive_browse_card(photo_a),
            build_archive_browse_card(manual_item),
            build_archive_browse_card(photo_no_thumb),
            build_archive_browse_card(photo_b),
        ]

        def presign_side_effect(*, bucket, key, expires_in):
            return {
                thumb_a: "https://s3.example/thumb-a",
                thumb_b: "https://s3.example/thumb-b",
            }[key]

        with patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            side_effect=presign_side_effect,
        ) as mock_presigned_get:
            enriched = apply_photo_thumbnail_urls_to_browse_cards(
                cards,
                bucket="test-uploads-bucket",
            )

        self.assertEqual(
            [card.item.id for card in enriched],
            [photo_a.id, manual_item.id, photo_no_thumb.id, photo_b.id],
        )
        self.assertEqual(enriched[0].thumbnail_url, "https://s3.example/thumb-a")
        self.assertIsNone(enriched[1].thumbnail_url)
        self.assertIsNone(enriched[2].thumbnail_url)
        self.assertEqual(enriched[3].thumbnail_url, "https://s3.example/thumb-b")
        self.assertEqual(mock_presigned_get.call_count, 2)
        presigned_keys = [
            call.kwargs["key"] for call in mock_presigned_get.call_args_list
        ]
        self.assertEqual(presigned_keys, [thumb_a, thumb_b])
        original_keys = {original_a, original_no_thumb, original_b}
        for call in mock_presigned_get.call_args_list:
            self.assertNotIn(call.kwargs["key"], original_keys)
