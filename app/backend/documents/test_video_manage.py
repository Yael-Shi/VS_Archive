"""Management UI tests for VIDEO ArchiveItem create/edit/delete."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from documents.models import ArchiveItem, ManualTextContent, VideoContent
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.services.video_validation import (
    VIDEO_PRESENTATION_EMBEDDED_HINT,
    VIDEO_PRESENTATION_EXTERNAL_HINT,
    VIDEO_SOURCE_URL_INVALID_ERROR,
    VIDEO_SOURCE_URL_REQUIRED_ERROR,
    VIDEO_SOURCE_URL_UNSUPPORTED_ERROR,
    VIDEO_TIME_YOUTUBE_ONLY_ERROR,
)
from documents.test_archive_date_payloads import merge_default_date_fields

User = get_user_model()

MANAGE_NEW_URL = "/archive/manage/new/"
EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"
DELETE_URL_TEMPLATE = "/archive/manage/{item_id}/delete/"
MANAGE_LIST_URL = "/archive/manage/"

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
KAN_URL = "https://www.kan.org.il/content/kan/item/12345/"
OTHER_URL = "https://vimeo.com/123456789"


class VideoManageCreateTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_manage_create_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _payload(self, **overrides):
        payload = {
            "item_type": "video",
            "title": "Video item",
            "source_url": YOUTUBE_URL,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "start_seconds": "",
            "end_seconds": "",
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def test_type_selector_includes_video(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="video"')
        self.assertContains(resp, "סרטון")

    def test_video_create_form_renders_url_and_hints(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="source_url"')
        self.assertContains(resp, 'name="start_seconds"')
        self.assertContains(resp, 'name="end_seconds"')
        self.assertContains(resp, "קישור לסרטון")
        self.assertContains(resp, 'name="title"')
        self.assertContains(resp, 'name="categories"')

    def test_staff_can_create_valid_youtube_video(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(
                title="YouTube manage create",
                source_url=YOUTUBE_URL,
                start_seconds="1m30s",
                end_seconds="120",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        item = ArchiveItem.objects.get(title="YouTube manage create")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.VIDEO)
        content = item.video_content
        self.assertEqual(content.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EMBEDDED,
        )
        self.assertEqual(content.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(content.start_seconds, 90)
        self.assertEqual(content.end_seconds, 120)

    def test_staff_can_create_valid_kan_video(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(title="KAN manage create", source_url=KAN_URL),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="KAN manage create")
        content = item.video_content
        self.assertEqual(content.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )
        self.assertEqual(content.provider_video_id, "")
        self.assertIsNone(content.start_seconds)
        self.assertIsNone(content.end_seconds)

    def test_staff_can_create_valid_other_video(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(title="OTHER manage create", source_url=OTHER_URL),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="OTHER manage create")
        content = item.video_content
        self.assertEqual(content.provider, VideoContent.Provider.OTHER)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )

    def test_create_requires_source_url(self):
        before = ArchiveItem.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(source_url=""),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_SOURCE_URL_REQUIRED_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)

    def test_create_rejects_invalid_url(self):
        before = ArchiveItem.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(source_url="https://"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_SOURCE_URL_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)

    def test_create_rejects_unsupported_scheme(self):
        before = ArchiveItem.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(source_url="not-a-url"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_SOURCE_URL_UNSUPPORTED_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)

    def test_create_rejects_times_for_kan(self):
        before = VideoContent.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(
                title="KAN with times",
                source_url=KAN_URL,
                start_seconds="10",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_TIME_YOUTUBE_ONLY_ERROR)
        self.assertEqual(VideoContent.objects.count(), before)

    def test_create_shows_presentation_mode_explanation_after_valid_parse_round_trip(
        self,
    ):
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(title="   ", source_url=YOUTUBE_URL),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_PRESENTATION_EMBEDDED_HINT)
        self.assertContains(resp, "YouTube")

    def test_anonymous_cannot_create_video(self):
        resp = self.client.post(MANAGE_NEW_URL, data=self._payload())
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(ArchiveItem.objects.filter(title="Video item").exists())

    def test_non_staff_cannot_create_video(self):
        user = User.objects.create_user(
            username="video_manage_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(MANAGE_NEW_URL, data=self._payload())
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ArchiveItem.objects.filter(title="Video item").exists())


class VideoManageEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_manage_edit_staff",
            password="test-pass",
            is_staff=True,
        )

    def _payload(self, **overrides):
        payload = {
            "title": "Edited video",
            "source_url": YOUTUBE_URL,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "start_seconds": "",
            "end_seconds": "",
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def test_edit_form_loads_existing_video_fields(self):
        item = create_video_archive_item(
            title="Existing YouTube",
            source_url=f"{YOUTUBE_URL}&t=45",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Existing YouTube")
        self.assertContains(resp, item.video_content.source_url)
        self.assertContains(resp, VIDEO_PRESENTATION_EMBEDDED_HINT)
        self.assertContains(resp, 'value="45"')

    def test_edit_url_change_youtube_to_kan_clears_youtube_fields(self):
        item = create_video_archive_item(
            title="Transition video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            start_seconds=12,
            end_seconds=40,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Transition video",
                source_url=KAN_URL,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.select_related("video_content").get(pk=item.pk)
        content = item.video_content
        self.assertEqual(content.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            content.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )
        self.assertEqual(content.provider_video_id, "")
        self.assertIsNone(content.start_seconds)
        self.assertIsNone(content.end_seconds)

    def test_edit_provider_transition_other_to_youtube(self):
        item = create_video_archive_item(
            title="Other to YouTube",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Other to YouTube",
                source_url=YOUTUBE_URL,
                start_seconds="15",
                end_seconds="60",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        content = ArchiveItem.objects.get(pk=item.pk).video_content
        self.assertEqual(content.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(content.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(content.start_seconds, 15)
        self.assertEqual(content.end_seconds, 60)

    def test_edit_validation_failure_does_not_partially_save(self):
        item = create_video_archive_item(
            title="Keep original",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        original_url = item.video_content.source_url
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Should not save",
                source_url="javascript:alert(1)",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_SOURCE_URL_UNSUPPORTED_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.title, "Keep original")
        self.assertEqual(item.video_content.source_url, original_url)

    def test_edit_rejects_times_when_provider_becomes_other(self):
        item = create_video_archive_item(
            title="Reject times",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Reject times",
                source_url=OTHER_URL,
                start_seconds="5",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_TIME_YOUTUBE_ONLY_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.video_content.provider, VideoContent.Provider.YOUTUBE)


class VideoManageDeleteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_manage_delete_staff",
            password="test-pass",
            is_staff=True,
        )

    def test_delete_removes_archive_item_and_video_content(self):
        item = create_video_archive_item(
            title="Delete me video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        item_id = item.pk
        content_id = item.video_content.pk
        self.client.force_login(self.staff)
        resp = self.client.post(DELETE_URL_TEMPLATE.format(item_id=item_id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())
        self.assertFalse(VideoContent.objects.filter(pk=content_id).exists())

    def test_delete_confirm_page_renders(self):
        item = create_video_archive_item(
            title="Confirm delete video",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(DELETE_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחיקת סרטון")
        self.assertContains(resp, "Confirm delete video")


class VideoManageListAndPermissionsTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_manage_list_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def test_manage_list_shows_video_edit_and_delete_links(self):
        item = create_video_archive_item(
            title="Listed video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_LIST_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Listed video")
        self.assertContains(resp, "סרטון")
        edit_href = reverse("archive-manage-edit", kwargs={"item_id": item.id})
        delete_href = reverse("archive-manage-delete", kwargs={"item_id": item.id})
        detail_href = reverse("archive-detail", kwargs={"item_id": item.id})
        self.assertContains(resp, f'href="{edit_href}"')
        self.assertContains(resp, f'href="{delete_href}"')
        self.assertContains(
            resp,
            f'<a href="{edit_href}">Listed video</a>',
            html=True,
        )
        self.assertNotContains(resp, f'href="{detail_href}"')

    def test_manage_list_manual_title_still_links_to_detail(self):
        item = create_manual_text_archive_item(
            title="Listed manual detail link",
            body="Body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_LIST_URL)
        self.assertEqual(resp.status_code, 200)
        detail_href = reverse("archive-detail", kwargs={"item_id": item.id})
        self.assertContains(
            resp,
            f'<a href="{detail_href}">Listed manual detail link</a>',
            html=True,
        )

    def test_video_forms_use_link_add_copy_not_external_only(self):
        item = create_video_archive_item(
            title="Copy check video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        expected = "פריט ארכיון מסוג סרטון — הוספה באמצעות קישור, ללא העלאת קובץ."
        obsolete = "קישור חיצוני בלבד"
        self.client.force_login(self.staff)
        create_resp = self.client.get(MANAGE_NEW_URL, {"item_type": "video"})
        self.assertEqual(create_resp.status_code, 200)
        self.assertContains(create_resp, expected)
        self.assertNotContains(create_resp, obsolete)
        edit_resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(edit_resp.status_code, 200)
        self.assertContains(edit_resp, expected)
        self.assertNotContains(edit_resp, obsolete)

    def test_video_form_script_avoids_false_other_hints(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "video"})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        # Progressive-enhancement contract markers only (no JS test runner).
        self.assertIn("function isImpersonationHost", body)
        self.assertIn("PROTECTED_APEXES", body)
        self.assertIn('if (protocol === "https:")', body)
        self.assertIn("isImpersonationHost(host)", body)
        self.assertIn("youtube.com", body)
        self.assertIn("kan.org.il", body)

    def test_family_user_cannot_access_video_edit(self):
        item = create_video_archive_item(
            title="Family blocked video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        family = User.objects.create_user(
            username="video_family_user",
            password="test-pass",
        )
        family.groups.add(self.family_group)
        self.client.force_login(family)
        resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 403)

    def test_existing_manual_text_create_still_works(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=merge_default_date_fields(
                {
                    "item_type": "manual_text",
                    "title": "Regression manual",
                    "body": "Still works",
                    "visibility": ArchiveItem.Visibility.PUBLIC,
                    "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                    "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                }
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Regression manual")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)
        self.assertEqual(item.manual_text_content.body, "Still works")
        self.assertFalse(VideoContent.objects.filter(archive_item=item).exists())

    def test_existing_manual_text_edit_and_delete_still_work(self):
        item = create_manual_text_archive_item(
            title="Manual still editable",
            body="old",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        edit_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=merge_default_date_fields(
                {
                    "title": "Manual still editable",
                    "body": "updated",
                    "visibility": ArchiveItem.Visibility.PUBLIC,
                    "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                    "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                }
            ),
        )
        self.assertEqual(edit_resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.manual_text_content.body, "updated")

        delete_resp = self.client.post(DELETE_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(delete_resp.status_code, 302)
        self.assertFalse(ArchiveItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(ManualTextContent.objects.filter(body="updated").exists())

    def test_kan_edit_shows_external_presentation_hint(self):
        item = create_video_archive_item(
            title="KAN hint",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VIDEO_PRESENTATION_EXTERNAL_HINT)
        self.assertContains(resp, "כאן")
