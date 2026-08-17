"""Service, search, visibility, and browse-renderability tests for VIDEO items."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemSearchIndex,
    Document,
    ManualTextContent,
    PhotoContent,
    VideoContent,
)
from documents.services.archive_item_access import (
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
    archive_browse_queryset_for_user,
    filter_browse_renderable_archive_items,
)
from documents.services.archive_item_presentation import archive_item_type_label
from documents.services.archive_items import (
    VIDEO_DISCOVERY_PARTIAL_ERROR,
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
    update_video_archive_item,
)
from documents.services.archive_metadata_validation import VISIBILITY_INVALID_ERROR
from documents.services.archive_search_index import (
    build_archive_item_search_content,
    sync_archive_item_search_index,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)

User = get_user_model()

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_URL_ALT = "https://youtu.be/xxxxxxxxxxx?t=12"
KAN_URL = "https://www.kan.org.il/content/kan/item/999/"
OTHER_URL = "https://example.com/videos/clip-1"


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    return User.objects.get(pk=user.pk)


def _load_item(pk: int) -> ArchiveItem:
    return (
        ArchiveItem.objects.select_related(
            "manual_text_content",
            "ocr_document",
            "video_content",
            "search_index",
        )
        .prefetch_related("photo_contents", "categories", "events", "tags")
        .get(pk=pk)
    )


class VideoArchiveItemServiceTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_staff",
            password="x",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="video_authorized",
                password="x",
                is_staff=True,
            )
        )

    def test_hebrew_type_label(self):
        self.assertEqual(
            archive_item_type_label(ArchiveItem.ItemType.VIDEO),
            "סרטון",
        )

    def test_atomic_create_success(self):
        item = create_video_archive_item(
            title="Family film",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            author_name="Archive Author",
            source_title="Source Title",
            public_note="Public note text",
            category_names=["Video Category"],
            event_names=[],
            tag_names=[],
            user=self.staff,
        )
        item = _load_item(item.pk)
        self.assertEqual(item.item_type, ArchiveItem.ItemType.VIDEO)
        self.assertEqual(item.video_content.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(item.video_content.provider_video_id, "dQw4w9WgXcQ")
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item=item).exists()
        )
        self.assertEqual(item.categories.count(), 1)

    def test_atomic_create_rollback_on_invalid_url(self):
        before_items = ArchiveItem.objects.count()
        before_content = VideoContent.objects.count()
        with self.assertRaises(ValueError):
            create_video_archive_item(
                title="Broken",
                source_url="not-a-url",
                user=self.staff,
            )
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(VideoContent.objects.count(), before_content)

    def test_atomic_create_rollback_on_search_sync_failure(self):
        before_items = ArchiveItem.objects.count()
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                create_video_archive_item(
                    title="Sync fail create",
                    source_url=YOUTUBE_URL,
                    user=self.staff,
                )
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertFalse(
            VideoContent.objects.filter(archive_item__title="Sync fail create").exists()
        )

    def test_atomic_update_success_and_recompute(self):
        item = create_video_archive_item(
            title="Before",
            source_url=YOUTUBE_URL,
            user=self.staff,
        )
        update_video_archive_item(
            item,
            title="After",
            source_url="https://www.youtube.com/watch?v=abcdefghijk&t=25&end=80",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            user=self.staff,
        )
        item = _load_item(item.pk)
        self.assertEqual(item.title, "After")
        self.assertEqual(item.video_content.provider_video_id, "abcdefghijk")
        self.assertEqual(item.video_content.start_seconds, 25)
        self.assertEqual(item.video_content.end_seconds, 80)
        self.assertEqual(item.search_index.title_text, "After")

    def test_atomic_update_rollback_on_invalid_url(self):
        item = create_video_archive_item(
            title="Keep title",
            source_url=YOUTUBE_URL,
            user=self.staff,
        )
        with self.assertRaises(ValueError):
            update_video_archive_item(
                item,
                title="Should not stick",
                source_url="https://www.youtube.com/playlist?list=PLxxxx",
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                user=self.staff,
            )
        item.refresh_from_db()
        content = item.video_content
        content.refresh_from_db()
        self.assertEqual(item.title, "Keep title")
        self.assertEqual(content.provider_video_id, "dQw4w9WgXcQ")

    def test_youtube_to_kan_clears_stale_fields(self):
        item = create_video_archive_item(
            title="YT",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10&end=50",
            user=self.staff,
        )
        self.assertEqual(item.video_content.start_seconds, 10)
        update_video_archive_item(
            item,
            title="YT to Kan",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            user=self.staff,
        )
        content = _load_item(item.pk).video_content
        self.assertEqual(content.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )
        self.assertEqual(content.provider_video_id, "")
        self.assertIsNone(content.start_seconds)
        self.assertIsNone(content.end_seconds)

    def test_youtube_to_other_clears_stale_fields(self):
        item = create_video_archive_item(
            title="YT",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=8",
            user=self.staff,
        )
        update_video_archive_item(
            item,
            title="YT to Other",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            user=self.staff,
        )
        content = _load_item(item.pk).video_content
        self.assertEqual(content.provider, VideoContent.Provider.OTHER)
        self.assertEqual(content.provider_video_id, "")
        self.assertIsNone(content.start_seconds)
        self.assertIsNone(content.end_seconds)

    def test_changing_between_youtube_urls_recomputes_id_and_times(self):
        item = create_video_archive_item(
            title="YT A",
            source_url=YOUTUBE_URL,
            user=self.staff,
        )
        update_video_archive_item(
            item,
            title="YT B",
            source_url=YOUTUBE_URL_ALT,
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            user=self.staff,
        )
        content = _load_item(item.pk).video_content
        self.assertEqual(content.provider_video_id, "xxxxxxxxxxx")
        self.assertEqual(content.start_seconds, 12)
        self.assertIsNone(content.end_seconds)

    def test_restricted_visibility_rejected_without_permission(self):
        before = ArchiveItem.objects.count()
        with self.assertRaises(ValueError) as ctx:
            create_video_archive_item(
                title="Restricted denied",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.RESTRICTED,
                user=self.staff,
            )
        self.assertEqual(str(ctx.exception), VISIBILITY_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)

    def test_restricted_visibility_accepted_with_permission(self):
        item = create_video_archive_item(
            title="Restricted ok",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.authorized,
        )
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_search_index_sync_occurs_exactly_once_per_write(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked_sync:
            item = create_video_archive_item(
                title="Sync create",
                source_url=YOUTUBE_URL,
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 1)
            update_video_archive_item(
                item,
                title="Sync update",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 2)
            item = _load_item(item.pk)
            self.assertEqual(item.search_index.title_text, "Sync update")

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked_sync:
            create_video_archive_item(
                title="Sync create discovery",
                source_url=YOUTUBE_URL,
                category_names=["VideoCatSync"],
                event_names=[],
                tag_names=[],
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 1)

        item = create_video_archive_item(
            title="Sync update discovery",
            source_url=YOUTUBE_URL,
            user=self.staff,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked_sync:
            update_video_archive_item(
                item,
                title="Sync update discovery done",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                category_names=["UpdatedCat"],
                event_names=[],
                tag_names=[],
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 1)

    def test_discovery_all_three_required_partial_rejected_before_write(self):
        item = create_video_archive_item(
            title="Discovery preserve",
            source_url=YOUTUBE_URL,
            category_names=["KeepCat"],
            event_names=["KeepEvent"],
            tag_names=["keep-tag"],
            user=self.staff,
        )
        item = _load_item(item.pk)
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"KeepCat"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"KeepEvent"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"keep-tag"},
        )

        with self.assertRaises(ValueError) as ctx:
            update_video_archive_item(
                item,
                title="Should not stick",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                category_names=["OnlyCategories"],
                user=self.staff,
            )
        self.assertEqual(str(ctx.exception), VIDEO_DISCOVERY_PARTIAL_ERROR)

        item.refresh_from_db()
        self.assertEqual(item.title, "Discovery preserve")
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"KeepCat"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"KeepEvent"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"keep-tag"},
        )

        with self.assertRaises(ValueError) as ctx:
            create_video_archive_item(
                title="Partial create",
                source_url=YOUTUBE_URL,
                category_names=["CatOnly"],
                user=self.staff,
            )
        self.assertEqual(str(ctx.exception), VIDEO_DISCOVERY_PARTIAL_ERROR)
        self.assertFalse(ArchiveItem.objects.filter(title="Partial create").exists())

    def test_discovery_omitted_preserves_and_full_replace_syncs_once(self):
        item = create_video_archive_item(
            title="Discovery omit",
            source_url=YOUTUBE_URL,
            category_names=["OrigCat"],
            event_names=["OrigEvent"],
            tag_names=["orig-tag"],
            user=self.staff,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked_sync:
            update_video_archive_item(
                item,
                title="Discovery omit updated",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 1)

        item = _load_item(item.pk)
        self.assertEqual(item.title, "Discovery omit updated")
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"OrigCat"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"OrigEvent"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"orig-tag"},
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked_sync:
            update_video_archive_item(
                item,
                title="Discovery replaced",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PRIVATE,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                category_names=["NewCat"],
                event_names=[],
                tag_names=["new-tag"],
                user=self.staff,
            )
            self.assertEqual(mocked_sync.call_count, 1)

        item = _load_item(item.pk)
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"NewCat"},
        )
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"new-tag"},
        )


class VideoSearchIndexTests(TestCase):
    def test_metadata_indexed_body_and_hebrew_empty_urls_not_searchable(self):
        item = create_video_archive_item(
            title="Searchable Video Title",
            source_url=YOUTUBE_URL,
            author_name="Video Author UniqueToken",
            source_title="Video Source UniqueToken",
            public_note="Video note UniqueToken",
            category_names=["VideoCatUnique"],
            event_names=[],
            tag_names=[],
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        loaded = _load_item(item.pk)
        content = build_archive_item_search_content(loaded)
        self.assertEqual(content.title_text, "Searchable Video Title")
        self.assertIn("Video Author UniqueToken", content.metadata_text)
        self.assertIn("Video Source UniqueToken", content.metadata_text)
        self.assertIn("Video note UniqueToken", content.metadata_text)
        self.assertIn("VideoCatUnique", content.metadata_text)
        self.assertEqual(content.body_text, "")
        self.assertEqual(content.hebrew_translation_text, "")

        blob = (
            f"{content.title_text}\n"
            f"{content.metadata_text}\n"
            f"{content.body_text}\n"
            f"{content.hebrew_translation_text}"
        )
        self.assertNotIn(YOUTUBE_URL, blob)
        self.assertNotIn("dQw4w9WgXcQ", blob)
        self.assertNotIn(loaded.video_content.source_url, blob)
        self.assertNotIn(loaded.video_content.provider_video_id, blob)

        index = loaded.search_index
        self.assertEqual(index.body_text, "")
        self.assertEqual(index.hebrew_translation_text, "")

        public_qs = archive_browse_queryset_for_user(None)
        matched = filter_archive_items_by_search_query(
            public_qs,
            "Searchable Video Title",
        )
        self.assertTrue(matched.filter(pk=item.pk).exists())
        url_matched = filter_archive_items_by_search_query(public_qs, "dQw4w9WgXcQ")
        self.assertFalse(url_matched.filter(pk=item.pk).exists())

    def test_visibility_filtering_remains_query_time(self):
        private_item = create_video_archive_item(
            title="PrivateVideoUnique",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        anon_qs = archive_browse_queryset_for_user(None)
        matched = filter_archive_items_by_search_query(
            anon_qs,
            "PrivateVideoUnique",
        )
        self.assertFalse(matched.filter(pk=private_item.pk).exists())


class VideoBrowseRenderabilityTests(TestCase):
    def test_valid_video_is_renderable(self):
        item = create_video_archive_item(
            title="Renderable",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        qs = filter_browse_renderable_archive_items(ArchiveItem.objects.all())
        self.assertTrue(qs.filter(pk=item.pk).exists())
        self.assertTrue(
            archive_browse_queryset_for_user(None).filter(pk=item.pk).exists()
        )

    def test_video_without_videocontent_excluded(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Missing content",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        qs = filter_browse_renderable_archive_items(ArchiveItem.objects.all())
        self.assertFalse(qs.filter(pk=item.pk).exists())

    def test_browse_guards_missing_content_and_db_invalid_shape(self):
        """Browse fails closed for missing rows / DB-invalid shape.

        Semantic source_url↔provider mismatches that still satisfy DB shape are
        rejected by ``VideoContent.full_clean()`` at write time (tested in
        ``test_video_content``). Browse does not re-parse URLs in SQL; it guards
        missing VideoContent plus the DB-enforceable provider/mode/id/source_url
        shape, including the approved YouTube ID regex.
        """
        missing = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Incomplete video",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.assertFalse(
            filter_browse_renderable_archive_items(ArchiveItem.objects.all())
            .filter(pk=missing.pk)
            .exists()
        )

        deleted = create_video_archive_item(
            title="Valid then delete content",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        deleted.video_content.delete()
        self.assertFalse(
            filter_browse_renderable_archive_items(ArchiveItem.objects.all())
            .filter(pk=deleted.pk)
            .exists()
        )

        # Strongest DB-constructable invalid shape without disabling constraints:
        # empty source_url is blocked by CheckConstraint.
        shaped = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Empty source url shape",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        with self.assertRaises(Exception):
            from django.db import transaction

            with transaction.atomic():
                VideoContent.objects.create(
                    archive_item=shaped,
                    source_url="",
                    provider=VideoContent.Provider.OTHER,
                    presentation_mode=VideoContent.PresentationMode.EXTERNAL_LINK,
                    provider_video_id="",
                )
        self.assertFalse(
            filter_browse_renderable_archive_items(ArchiveItem.objects.all())
            .filter(pk=shaped.pk)
            .exists()
        )

    def test_existing_ocr_manual_photo_behavior_unchanged(self):
        manual = create_manual_text_archive_item(
            title="Manual still ok",
            body="Body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ocr = create_ocr_document(
            title="OCR still ok",
            visibility=ArchiveItem.Visibility.PUBLIC,
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
        )
        pending_ocr = create_ocr_document(
            title="OCR pending",
            visibility=ArchiveItem.Visibility.PUBLIC,
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADING,
        )
        photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo uploaded",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=photo,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=100,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        pending_photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo pending",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=pending_photo,
            original_file_key="",
            original_filename="pending.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )

        qs = filter_browse_renderable_archive_items(ArchiveItem.objects.all())
        self.assertTrue(qs.filter(pk=manual.pk).exists())
        self.assertTrue(qs.filter(pk=ocr.archive_item_id).exists())
        self.assertFalse(qs.filter(pk=pending_ocr.archive_item_id).exists())
        self.assertTrue(qs.filter(pk=photo.pk).exists())
        self.assertFalse(qs.filter(pk=pending_photo.pk).exists())
        self.assertTrue(ManualTextContent.objects.filter(archive_item=manual).exists())
