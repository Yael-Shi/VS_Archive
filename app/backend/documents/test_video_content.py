"""Model/constraint tests for VideoContent."""

from __future__ import annotations

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from documents.admin import VideoContentAdmin
from documents.models import ArchiveItem, Document, VideoContent


def _create_video_archive_item(*, title: str = "Archive video") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.VIDEO,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _youtube_defaults(**overrides) -> dict:
    values = {
        "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "provider": VideoContent.Provider.YOUTUBE,
        "presentation_mode": VideoContent.PresentationMode.EMBEDDED,
        "provider_video_id": "dQw4w9WgXcQ",
        "start_seconds": None,
        "end_seconds": None,
    }
    values.update(overrides)
    return values


class VideoContentModelTests(TestCase):
    def test_valid_youtube_embedded_state(self):
        item = _create_video_archive_item()
        content = VideoContent.objects.create(
            archive_item=item,
            **_youtube_defaults(start_seconds=10, end_seconds=40),
        )
        content.full_clean()
        self.assertEqual(content.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EMBEDDED,
        )
        self.assertEqual(content.provider_video_id, "dQw4w9WgXcQ")

    def test_valid_kan_external_link_state(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            source_url="https://www.kan.org.il/item/1",
            provider=VideoContent.Provider.KAN,
            presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
            provider_video_id="",
        )
        content.full_clean()
        content.save()
        self.assertEqual(content.provider, VideoContent.Provider.KAN)

    def test_valid_other_external_link_state(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            source_url="https://example.com/watch/1",
            provider=VideoContent.Provider.OTHER,
            presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
            provider_video_id="",
        )
        content.full_clean()
        content.save()
        self.assertEqual(content.provider, VideoContent.Provider.OTHER)

    def test_missing_provider_video_id_for_youtube_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(provider_video_id=""),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("provider_video_id", ctx.exception.message_dict)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    **_youtube_defaults(provider_video_id=""),
                )

    def test_invalid_youtube_provider_video_id_rejected(self):
        item = _create_video_archive_item(title="Bad YT id")
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(provider_video_id="abc"),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("provider_video_id", ctx.exception.message_dict)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    **_youtube_defaults(provider_video_id="abc"),
                )

    def test_source_url_must_match_provider_fields(self):
        item = _create_video_archive_item(title="Mismatched URL")
        content = VideoContent(
            archive_item=item,
            source_url="https://example.com/not-youtube",
            provider=VideoContent.Provider.YOUTUBE,
            presentation_mode=VideoContent.PresentationMode.EMBEDDED,
            provider_video_id="dQw4w9WgXcQ",
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertTrue(
            {"source_url", "provider", "provider_video_id"}
            & set(ctx.exception.message_dict)
        )

    def test_youtube_time_override_allowed_when_provider_fields_match_url(self):
        item = _create_video_archive_item(title="Override times")
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(start_seconds=12, end_seconds=99),
        )
        content.full_clean()
        content.save()
        self.assertEqual(content.start_seconds, 12)
        self.assertEqual(content.end_seconds, 99)

    def test_provider_video_id_present_for_kan_or_other_rejected(self):
        item = _create_video_archive_item(title="Kan bad id")
        content = VideoContent(
            archive_item=item,
            source_url="https://www.kan.org.il/item/1",
            provider=VideoContent.Provider.KAN,
            presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
            provider_video_id="abc",
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("provider_video_id", ctx.exception.message_dict)

        other_item = _create_video_archive_item(title="Other bad id")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=other_item,
                    source_url="https://example.com/v",
                    provider=VideoContent.Provider.OTHER,
                    presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
                    provider_video_id="abc",
                )

    def test_kan_or_other_with_embedded_mode_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            source_url="https://www.kan.org.il/item/1",
            provider=VideoContent.Provider.KAN,
            presentation_mode=VideoContent.PresentationMode.EMBEDDED,
            provider_video_id="",
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("presentation_mode", ctx.exception.message_dict)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    source_url="https://example.com/v",
                    provider=VideoContent.Provider.OTHER,
                    presentation_mode=VideoContent.PresentationMode.EMBEDDED,
                    provider_video_id="",
                )

    def test_start_end_on_non_youtube_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            source_url="https://www.kan.org.il/item/1",
            provider=VideoContent.Provider.KAN,
            presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
            provider_video_id="",
            start_seconds=5,
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("start_seconds", ctx.exception.message_dict)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    source_url="https://example.com/v",
                    provider=VideoContent.Provider.OTHER,
                    presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
                    provider_video_id="",
                    end_seconds=20,
                )

    def test_negative_start_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(start_seconds=-1),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("start_seconds", ctx.exception.message_dict)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    **_youtube_defaults(start_seconds=-1),
                )

    def test_invalid_end_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(end_seconds=0),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("end_seconds", ctx.exception.message_dict)

    def test_end_less_than_or_equal_start_rejected(self):
        item = _create_video_archive_item()
        content = VideoContent(
            archive_item=item,
            **_youtube_defaults(start_seconds=30, end_seconds=30),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("end_seconds", ctx.exception.message_dict)

        content_end_only = VideoContent(
            archive_item=item,
            **_youtube_defaults(start_seconds=None, end_seconds=0),
        )
        with self.assertRaises(ValidationError):
            content_end_only.full_clean()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    **_youtube_defaults(start_seconds=40, end_seconds=10),
                )

    def test_video_content_linked_to_non_video_archive_item_rejected(self):
        manual_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Manual note",
        )
        content = VideoContent(
            archive_item=manual_item,
            **_youtube_defaults(),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("archive_item", ctx.exception.message_dict)

    def test_one_to_one_and_cascade_and_no_document(self):
        item = _create_video_archive_item()
        content = VideoContent.objects.create(
            archive_item=item,
            **_youtube_defaults(),
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=item,
                    **_youtube_defaults(provider_video_id="abcdefghijk"),
                )
        content_id = content.id
        item.delete()
        self.assertFalse(VideoContent.objects.filter(pk=content_id).exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_video_content_admin_is_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            username="video_content_admin",
            password="test-pass",
            email="video-admin@example.com",
        )
        admin = VideoContentAdmin(VideoContent, AdminSite())
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_delete_permission(request))
