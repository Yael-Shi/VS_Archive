"""PR4: public /archive/ list and detail display for uploaded PHOTO items."""

from unittest.mock import patch

from botocore.exceptions import ClientError
from django.contrib.auth.models import Group, User
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    Document,
    PhotoContent,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_manual_text_archive_item
from documents.test_historical_person_tag_reuse import _create_tag


def _create_photo_archive_item(
    *,
    title: str,
    visibility=ArchiveItem.Visibility.PUBLIC,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str = "photos/42/original.jpg",
    thumbnail_file_key: str = "",
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=original_file_key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=upload_status,
        upload_error="",
        thumbnail_file_key=thumbnail_file_key,
        thumbnail_mime_type="image/jpeg" if thumbnail_file_key else "",
        thumbnail_size_bytes=512 if thumbnail_file_key else None,
    )
    return item


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveDisplayListTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="photo_display_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)
        self.staff = User.objects.create_user(
            username="photo_display_staff",
            password="test-pass",
            is_staff=True,
        )

        self.manual_item = create_manual_text_archive_item(
            title="Manual text still visible",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.uploaded_public = _create_photo_archive_item(
            title="Uploaded public photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self.pending_public = _create_photo_archive_item(
            title="Pending public photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.failed_public = _create_photo_archive_item(
            title="Failed public photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.FAILED,
        )

    def test_public_archive_list_includes_uploaded_photo(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.manual_item.title)
        self.assertContains(resp, self.uploaded_public.title)
        self.assertContains(resp, "archive-browse-card")
        self.assertContains(resp, "archive-browse-card__marker--photo")

    def test_public_archive_list_excludes_pending_and_failed_photo(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.pending_public.title)
        self.assertNotContains(resp, self.failed_public.title)

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value="https://s3.example/presigned",
    )
    def test_archive_list_does_not_generate_presigned_get_without_thumbnails(
        self, mock_presigned_get
    ):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()

    def test_manual_text_archive_list_behavior_unchanged(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.manual_item.title)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoEmptyFileKeyBrowseTests(TestCase):
    def setUp(self):
        self.empty_key_photo = _create_photo_archive_item(
            title="Uploaded photo with empty key",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="",
        )

    def test_empty_original_file_key_excluded_from_archive_list(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, self.empty_key_photo.title)

    def test_empty_original_file_key_detail_returns_404(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.empty_key_photo.id})
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveDisplayDetailTests(TestCase):
    PRESIGNED_URL = "https://s3.example/presigned-photo"

    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="photo_detail_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)
        self.staff = User.objects.create_user(
            username="photo_detail_staff",
            password="test-pass",
            is_staff=True,
        )

        self.public_uploaded = _create_photo_archive_item(
            title="Public uploaded photo detail",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_uploaded = _create_photo_archive_item(
            title="Private uploaded photo detail",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.public_pending = _create_photo_archive_item(
            title="Public pending photo detail",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.public_failed = _create_photo_archive_item(
            title="Public failed photo detail",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_anonymous_can_view_public_uploaded_photo_with_presigned_url(
        self, mock_presigned_get
    ):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.PRESIGNED_URL)
        self.assertContains(resp, 'class="photo-detail__image"')
        mock_presigned_get.assert_called_once_with(
            bucket="test-uploads-bucket",
            key="photos/42/original.jpg",
            expires_in=3600,
        )

    def test_anonymous_gets_404_for_private_uploaded_photo(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.private_uploaded.id})
        )
        self.assertEqual(resp.status_code, 404)

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_family_user_can_view_private_uploaded_photo(self, mock_presigned_get):
        self.client.force_login(self.family_user)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.private_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.PRESIGNED_URL)
        mock_presigned_get.assert_called_once()

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_staff_can_view_uploaded_photo(self, mock_presigned_get):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.PRESIGNED_URL)
        mock_presigned_get.assert_called_once()

    def test_pending_photo_detail_returns_404(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_pending.id})
        )
        self.assertEqual(resp.status_code, 404)

    def test_failed_photo_detail_returns_404(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_failed.id})
        )
        self.assertEqual(resp.status_code, 404)

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_presigned_get_not_called_for_unauthorized_private_photo(
        self, mock_presigned_get
    ):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.private_uploaded.id})
        )
        self.assertEqual(resp.status_code, 404)
        mock_presigned_get.assert_not_called()

    @override_settings(UPLOADS_BUCKET_NAME="")
    def test_detail_without_bucket_config_fails_safely(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "התמונה אינה זמינה כרגע")
        self.assertNotContains(resp, "photo-detail__image")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_no_document_is_created_for_photo_display(self, _mock_presigned_get):
        before = Document.objects.count()
        self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(Document.objects.count(), before)

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_renders_context_metadata_row(self, _mock_presigned_get):
        photo = self.public_uploaded.primary_photo_content
        photo.context = "חתונה בירושלים"
        photo.save(update_fields=["context", "updated_at"])

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הקשר / נסיבות:")
        self.assertContains(resp, "חתונה בירושלים")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_displays_non_empty_metadata(self, _mock_presigned_get):
        photo = self.public_uploaded.primary_photo_content
        photo.description = "Family picnic"
        photo.location = "Jerusalem"
        photo.context = "Summer outing"
        photo.people_present = "Grandpa, Grandma"
        photo.notes = "From album page 2"
        photo.save()

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Family picnic")
        self.assertNotContains(resp, "תיאור / כיתוב:")
        self.assertContains(resp, "מיקום:")
        self.assertContains(resp, "Jerusalem")
        self.assertContains(resp, "הקשר / נסיבות:")
        self.assertContains(resp, "Summer outing")
        self.assertContains(resp, "נוכחים:")
        self.assertNotContains(resp, "נוכחים בתמונה:")
        self.assertContains(resp, "Grandpa, Grandma")
        self.assertContains(resp, "הערות נוספות:")
        self.assertContains(resp, "From album page 2")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_does_not_render_empty_metadata_labels(
        self, _mock_presigned_get
    ):
        photo = self.public_uploaded.primary_photo_content
        photo.description = "Only caption filled"
        photo.save()

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-detail-photo-description")
        self.assertContains(resp, "Only caption filled")
        self.assertNotContains(resp, "תיאור / כיתוב:")
        self.assertNotContains(resp, "מיקום:")
        self.assertNotContains(resp, "הקשר / נסיבות:")
        self.assertNotContains(resp, "נוכחים בתמונה:")
        self.assertNotContains(resp, "אנשים מזוהים:")
        self.assertNotContains(resp, "תאריך התמונה:")
        self.assertNotContains(resp, "הערות נוספות:")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_does_not_show_author_or_source_labels(
        self, _mock_presigned_get
    ):
        self.public_uploaded.author_name = "Hidden author"
        self.public_uploaded.source_title = "Hidden source"
        self.public_uploaded.save()

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "מחבר/ת:")
        self.assertNotContains(resp, "Hidden author")
        self.assertNotContains(resp, "מקור:")
        self.assertNotContains(resp, "Hidden source")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_shows_public_note_when_present(self, _mock_presigned_get):
        self.public_uploaded.public_note = "Photo archive note"
        self.public_uploaded.save(update_fields=["public_note", "updated_at"])

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-detail-meta-block--public-note")
        self.assertContains(resp, "Photo archive note")
        self.assertNotContains(resp, "הערת הארכיון:")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_hides_empty_public_note_label(self, _mock_presigned_get):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "archive-detail-meta-block--public-note")
        self.assertNotContains(resp, "הערת הארכיון:")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_renders_discovery_before_public_note_without_label(
        self, _mock_presigned_get
    ):
        self.public_uploaded.public_note = "Photo note after discovery"
        self.public_uploaded.save(update_fields=["public_note", "updated_at"])
        category, _ = ArchiveCategory.objects.get_or_create(
            slug="photo-public-note-order",
            defaults={"name": "קטגוריית הערת תמונה"},
        )
        self.public_uploaded.categories.set([category])

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        discovery_idx = html.find("archive-detail-meta-block--discovery")
        note_idx = html.find("archive-detail-meta-block--public-note")
        self.assertNotEqual(discovery_idx, -1)
        self.assertNotEqual(note_idx, -1)
        self.assertLess(discovery_idx, note_idx)
        self.assertContains(resp, "Photo note after discovery")
        self.assertNotContains(resp, "הערת הארכיון:")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_photo_detail_public_view_omits_details_wrapper(self, _mock_presigned_get):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "<summary>פרטים</summary>")
        self.assertContains(resp, "btn-secondary archive-detail-suggest-btn")
        self.assertContains(resp, "הוספת מידע על הפריט")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_public_photo_detail_keeps_public_actions_without_staff_controls(
        self, _mock_presigned_get
    ):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertContains(resp, "archive-detail-photo-header")
        self.assertContains(resp, "archive-detail-photo-header-main")
        self.assertContains(resp, "חזרה לארכיון")
        self.assertContains(resp, reverse("archive-list"))
        self.assertContains(resp, "הוספת מידע על הפריט")
        self.assertContains(resp, "archive-detail-navigation-actions")
        self.assertNotContains(resp, "archive-detail-staff-management-actions")
        self.assertNotContains(resp, "עריכת מטא־דאטה")
        self.assertNotContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.public_uploaded.id}),
        )
        self.assertNotContains(
            resp,
            reverse(
                "archive-manage-delete",
                kwargs={"item_id": self.public_uploaded.id},
            ),
        )
        self.assertNotIn("<summary>פרטים</summary>", html)
        self.assertContains(resp, 'class="photo-detail__image"')
        page_start = html.index("archive-detail-page--photo")
        self.assertNotIn('href="/admin/', html[page_start:])
        self.assertNotIn(f"פריט ארכיון #{self.public_uploaded.id}", html)
        self.assertNotContains(resp, "archive-detail-badges")

        public_start = html.index("archive-detail-navigation-actions")
        public_end = html.index("</div>", public_start)
        public_column = html[public_start:public_end]
        back_pos = public_column.index("חזרה לארכיון")
        suggest_pos = public_column.index("הוספת מידע על הפריט")
        self.assertLess(back_pos, suggest_pos)
        archive_list_href = reverse("archive-list")
        back_href_pos = public_column.index(f'href="{archive_list_href}"')
        back_tag_start = public_column.rfind("<a", 0, back_href_pos)
        back_tag_end = public_column.find(">", back_href_pos) + 1
        self.assertIn("btn-primary", public_column[back_tag_start:back_tag_end])
        self.assertNotIn("btn-link", public_column[back_tag_start:back_tag_end])

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_staff_photo_detail_keeps_discovery_metadata_and_moves_admin_badges_to_technical_details(
        self, _mock_presigned_get
    ):
        category = ArchiveCategory.objects.create(
            name="Photo detail category",
            slug="photo-detail-category",
        )
        event = ArchiveEvent.objects.create(
            name="Photo detail event",
            slug="photo-detail-event",
        )
        tag = _create_tag(name="photo-detail-tag")
        self.public_uploaded.categories.add(category)
        self.public_uploaded.events.add(event)
        self.public_uploaded.tags.add(tag)

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )

        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        header_start = html.index("archive-detail-photo-header")
        header_end = html.index("</header>", header_start)
        header = html[header_start:header_end]

        main_start = header.index("archive-detail-photo-header-main")
        metadata_start = header.index("archive-detail-photo-meta")
        toolbar_start = header.index("archive-detail-photo-top")

        self.assertLess(main_start, metadata_start)
        self.assertLess(metadata_start, toolbar_start)

        # Public/archive metadata stays together in header-main,
        # exactly like document detail.
        self.assertIn("Photo detail category", header)
        self.assertIn("Photo detail event", header)
        self.assertIn("photo-detail-tag", header)

        discovery_start = header.index("archive-detail-meta-block--discovery")

        # Admin-only badges move to technical details instead.
        technical_start = header.index("archive-detail-photo-technical")

        self.assertLess(discovery_start, technical_start)
        self.assertIn("Photo detail category", header)
        self.assertIn("Photo detail event", header)
        self.assertIn("photo-detail-tag", header)
        technical_end = header.index("</details>", technical_start)
        technical_section = header[technical_start:technical_end]

        self.assertIn("<summary>פרטים טכניים</summary>", technical_section)
        self.assertIn('class="badge-row"', technical_section)
        self.assertNotIn("Photo detail category", technical_section)
        self.assertNotIn("Photo detail event", technical_section)
        self.assertNotIn("photo-detail-tag", technical_section)

        # The old standalone PHOTO badge area is gone.
        self.assertNotContains(resp, "archive-detail-badges")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_public_photo_detail_still_renders_discovery_metadata(
        self, _mock_presigned_get
    ):
        category = ArchiveCategory.objects.create(
            name="Public photo category",
            slug="public-photo-category",
        )
        event = ArchiveEvent.objects.create(
            name="Public photo event",
            slug="public-photo-event",
        )
        tag = _create_tag(name="public-photo-tag")
        self.public_uploaded.categories.add(category)
        self.public_uploaded.events.add(event)
        self.public_uploaded.tags.add(tag)

        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-detail-meta-block--discovery")
        self.assertContains(resp, "Public photo category")
        self.assertContains(resp, "Public photo event")
        self.assertContains(resp, "public-photo-tag")
        self.assertNotContains(resp, "archive-detail-photo-technical")

    @patch(
        "documents.views.create_presigned_get",
        return_value=PRESIGNED_URL,
    )
    def test_staff_photo_detail_separates_public_nav_from_staff_management(
        self, _mock_presigned_get
    ):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_uploaded.id})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        edit_href = reverse(
            "archive-manage-edit", kwargs={"item_id": self.public_uploaded.id}
        )
        delete_href = reverse(
            "archive-manage-delete", kwargs={"item_id": self.public_uploaded.id}
        )

        self.assertContains(resp, "archive-detail-photo-header")
        self.assertContains(resp, "archive-detail-photo-header-main")
        self.assertContains(resp, "archive-detail-photo-top")
        self.assertContains(resp, "archive-detail-navigation-actions")
        self.assertContains(resp, "archive-detail-staff-management-actions")
        self.assertContains(resp, "חזרה לארכיון")
        self.assertContains(resp, "הוספת מידע על הפריט")
        self.assertContains(resp, "עריכת מטא־דאטה")
        self.assertContains(resp, "מחיקה")
        self.assertContains(resp, edit_href)
        self.assertContains(resp, delete_href)
        self.assertNotContains(resp, ">עריכה</a>")
        self.assertNotContains(resp, "<summary>פרטים</summary>")
        self.assertContains(resp, 'class="photo-detail__image"')
        self.assertNotContains(resp, "staff-document-nav")
        self.assertNotContains(resp, "staff-document-nav__button")

        page_start = html.index("archive-detail-page--photo")
        page_end = html.find("</div>", html.rfind("archive-detail-photo"))
        if page_end == -1:
            page_end = len(html)
        page_html = html[page_start:page_end]
        self.assertNotIn('href="/admin/', page_html)
        self.assertNotIn("עריכה טכנית", page_html)

        header_start = html.index("archive-detail-photo-header")
        header_end = html.index("</header>", header_start)
        header = html[header_start:header_end]
        self.assertIn("archive-detail-photo-header-main", header)
        self.assertIn("archive-detail-photo-top", header)
        self.assertIn(self.public_uploaded.title, header)
        self.assertIn("חזרה לארכיון", header)
        self.assertIn("עריכת מטא־דאטה", header)
        self.assertIn(f"פריט ארכיון #{self.public_uploaded.id}", header)

        public_start = html.index("archive-detail-navigation-actions")
        public_end = html.index("</div>", public_start)
        public_column = html[public_start:public_end]
        self.assertIn("חזרה לארכיון", public_column)
        self.assertIn("הוספת מידע על הפריט", public_column)
        self.assertLess(
            public_column.index("חזרה לארכיון"),
            public_column.index("הוספת מידע על הפריט"),
        )
        self.assertNotIn(edit_href, public_column)
        self.assertNotIn(delete_href, public_column)
        self.assertNotIn("עריכת מטא־דאטה", public_column)
        self.assertNotIn("מחיקה", public_column)

        archive_list_href = reverse("archive-list")
        back_href_pos = public_column.index(f'href="{archive_list_href}"')
        back_tag_start = public_column.rfind("<a", 0, back_href_pos)
        back_tag_end = public_column.find(">", back_href_pos) + 1
        self.assertIn("btn-primary", public_column[back_tag_start:back_tag_end])
        self.assertNotIn("btn-link", public_column[back_tag_start:back_tag_end])

        staff_start = html.index("archive-detail-staff-management-actions")
        staff_end = html.index("</div>", staff_start)
        staff_section = html[staff_start:staff_end]
        self.assertIn("עריכת מטא־דאטה", staff_section)
        self.assertIn("מחיקה", staff_section)
        self.assertIn("btn-primary", staff_section)
        self.assertNotIn("staff-document-nav__button", staff_section)
        self.assertNotIn("חזרה לארכיון", staff_section)
        self.assertNotIn("הוספת מידע על הפריט", staff_section)
        self.assertNotIn('href="/admin/', staff_section)

        # Admin-only item/status badges live inside the compact
        # technical-details control in the PHOTO toolbar.
        technical_start = header.index("archive-detail-photo-technical")
        technical_end = header.index("</details>", technical_start)
        technical_section = header[technical_start:technical_end]

        self.assertIn("<summary>פרטים טכניים</summary>", technical_section)
        self.assertIn("תמונה", technical_section)
        self.assertNotIn("חזרה לארכיון", technical_section)
        self.assertNotIn("הוספת מידע על הפריט", technical_section)

        edit_tag_start = html.rfind("<a", 0, html.index(f'href="{edit_href}"'))
        edit_tag_end = html.find(">", html.index(f'href="{edit_href}"')) + 1
        self.assertIn("btn-primary", html[edit_tag_start:edit_tag_end])
        self.assertNotIn(
            "staff-document-nav__button", html[edit_tag_start:edit_tag_end]
        )
        delete_tag_start = html.rfind("<a", 0, html.index(f'href="{delete_href}"'))
        delete_tag_end = html.find(">", html.index(f'href="{delete_href}"')) + 1
        self.assertIn("btn-primary", html[delete_tag_start:delete_tag_end])
        self.assertNotIn(
            "staff-document-nav__button", html[delete_tag_start:delete_tag_end]
        )


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveDiscoveryBrowseTests(TestCase):
    def setUp(self):
        self.category = ArchiveCategory.objects.create(
            name="Photo display category",
            slug="photo-display-category",
        )
        self.event = ArchiveEvent.objects.create(
            name="Photo display event",
            slug="photo-display-event",
        )
        self.tag = _create_tag(name="photo-display-tag")

        self.uploaded = _create_photo_archive_item(title="Uploaded discovery photo")
        self.pending = _create_photo_archive_item(
            title="Pending discovery photo",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.uploaded.categories.add(self.category)
        self.uploaded.events.add(self.event)
        self.uploaded.tags.add(self.tag)
        self.pending.categories.add(self.category)
        self.pending.events.add(self.event)
        self.pending.tags.add(self.tag)

    def test_category_browse_includes_uploaded_photo(self):
        resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": self.category.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded.title)
        self.assertNotContains(resp, self.pending.title)

    def test_event_browse_includes_uploaded_photo(self):
        resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": self.event.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded.title)
        self.assertNotContains(resp, self.pending.title)

    def test_tag_browse_includes_uploaded_photo(self):
        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": self.tag.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded.title)
        self.assertNotContains(resp, self.pending.title)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveManageListTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_manage_staff",
            password="test-pass",
            is_staff=True,
        )
        self.uploaded = _create_photo_archive_item(
            title="Manage uploaded photo",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self.pending = _create_photo_archive_item(
            title="Manage pending photo",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.failed = _create_photo_archive_item(
            title="Manage failed photo",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )

    def test_staff_manage_list_shows_photo_regardless_of_upload_status(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded.title)
        self.assertContains(resp, self.pending.title)
        self.assertContains(resp, self.failed.title)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveBrowseThumbnailTests(TestCase):
    THUMBNAIL_PRESIGNED_URL = "https://s3.example/presigned-thumb"
    THUMBNAIL_KEY = "photos/42/thumb_400.jpg"

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_photo_with_thumbnail_presigns_and_renders_thumbnail(
        self, mock_presigned_get
    ):
        photo = _create_photo_archive_item(
            title="Photo with browse thumbnail",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once_with(
            bucket="test-uploads-bucket",
            key=self.THUMBNAIL_KEY,
            expires_in=3600,
        )
        self.assertContains(resp, photo.title)
        self.assertContains(resp, self.THUMBNAIL_PRESIGNED_URL)
        self.assertContains(resp, 'class="archive-browse-card__photo-preview"')
        self.assertContains(resp, 'class="archive-browse-card__photo-preview-image"')
        self.assertContains(resp, 'alt="Photo with browse thumbnail"')
        self.assertNotContains(resp, "archive-browse-card__marker-thumbnail")

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_archive_list_never_presigns_original_file_key(self, mock_presigned_get):
        _create_photo_archive_item(
            title="Photo original key guard",
            original_file_key="photos/77/original.jpg",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_called_once()
        for call in mock_presigned_get.call_args_list:
            self.assertNotEqual(call.kwargs.get("key"), "photos/77/original.jpg")
            self.assertEqual(call.kwargs.get("key"), self.THUMBNAIL_KEY)

    def test_photo_without_thumbnail_keeps_css_marker_fallback(self):
        photo = _create_photo_archive_item(
            title="Photo without browse thumbnail",
            original_file_key="photos/99/original.jpg",
        )
        with patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            return_value="https://s3.example/should-not-be-used",
        ) as mock_presigned_get:
            resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()
        self.assertContains(resp, "archive-browse-card__marker--photo")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")
        self.assertNotContains(resp, "archive-browse-card__marker-thumbnail")
        self.assertContains(resp, photo.title)

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        side_effect=ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetObject",
        ),
    )
    def test_presign_failure_renders_css_marker_fallback(self, _mock_presigned_get):
        photo = _create_photo_archive_item(
            title="Photo presign failure fallback",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, photo.title)
        self.assertContains(resp, "archive-browse-card__marker--photo")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")
        self.assertNotContains(resp, "archive-browse-card__marker-thumbnail")

    @override_settings(UPLOADS_BUCKET_NAME="")
    def test_missing_bucket_config_keeps_css_marker_fallback(self):
        photo = _create_photo_archive_item(
            title="Photo missing bucket fallback",
            thumbnail_file_key=self.THUMBNAIL_KEY,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, photo.title)
        self.assertContains(resp, "archive-browse-card__marker--photo")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")
        self.assertNotContains(resp, "archive-browse-card__marker-thumbnail")

    @patch(
        "documents.services.photo_archive_urls.create_presigned_get",
        return_value=THUMBNAIL_PRESIGNED_URL,
    )
    def test_non_photo_cards_remain_unchanged(self, mock_presigned_get):
        manual_item = create_manual_text_archive_item(
            title="Manual browse unchanged",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, manual_item.title)
        self.assertContains(resp, "archive-browse-card__fallback-preview--manual")
        self.assertNotContains(resp, "archive-browse-card__marker--manual")
        self.assertNotContains(resp, "archive-browse-card__photo-preview")
        self.assertNotContains(resp, 'alt="Manual browse unchanged"')
        mock_presigned_get.assert_not_called()


class PhotoArchiveBrowsePreviewStyleTests(SimpleTestCase):
    def test_photo_preview_css_fills_preview_area_from_top(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        image_block_start = css.index(".archive-browse-card__photo-preview-image")
        image_block = css[image_block_start : css.index("}", image_block_start) + 1]

        self.assertIn("object-fit: cover", image_block)
        self.assertIn("object-position: top center", image_block)
        self.assertNotIn("object-fit: contain", image_block)
        self.assertNotIn("border:", image_block)
        self.assertNotIn("box-shadow:", image_block)


class PhotoArchiveDetailLayoutStyleTests(SimpleTestCase):
    def test_photo_top_action_columns_match_document_detail_pattern(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")

        header_start = css.index(".archive-detail-photo-header {")
        header_rule = css[header_start : css.index("}", header_start)]
        self.assertIn("display: flex;", header_rule)
        self.assertIn("flex-wrap: wrap;", header_rule)
        self.assertIn("align-items: flex-start;", header_rule)

        main_start = css.index(".archive-detail-photo-header-main {")
        main_rule = css[main_start : css.index("}", main_start)]
        self.assertIn("flex: 1 1 16rem;", main_rule)

        top_start = css.index(".archive-detail-photo-top {")
        top_rule = css[top_start : css.index("}", top_start)]
        self.assertIn("justify-content: flex-end;", top_rule)
        self.assertIn("align-items: flex-start;", top_rule)

        public_start = css.index(".archive-detail-navigation-actions {")
        public_rule = css[public_start : css.index("}", public_start)]
        self.assertIn("flex-direction: column;", public_rule)
        self.assertIn("inline-size: max-content;", public_rule)

        staff_start = css.index(".archive-detail-staff-management-actions {")
        staff_rule = css[staff_start : css.index("}", staff_start)]
        self.assertIn("flex-direction: column;", staff_rule)
        self.assertIn("inline-size: max-content;", staff_rule)
        self.assertNotIn("display: grid", staff_rule)

    def test_photo_detail_figure_is_centered_and_width_capped(self):
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        figure_start = css.index(".archive-detail-photo {")
        figure_block = css[figure_start : css.index("}", figure_start) + 1]
        self.assertIn("max-width: 960px", figure_block)
        self.assertIn("margin: var(--space-5) auto 0", figure_block)
        self.assertIn("width: 100%", figure_block)

        image_start = css.index(".archive-detail-photo .photo-detail__image {")
        image_block = css[image_start : css.index("}", image_start) + 1]
        self.assertIn("height: auto", image_block)
        self.assertIn("width: 100%", image_block)
        self.assertIn("border-radius: var(--radius)", image_block)
        self.assertIn("box-shadow: var(--shadow-soft)", image_block)
