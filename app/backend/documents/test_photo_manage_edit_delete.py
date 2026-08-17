"""PR5: staff PHOTO metadata edit and delete V1."""

from unittest.mock import call, patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    Document,
    ManualTextContent,
    PhotoContent,
    Tag,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)


def _create_photo_archive_item(
    *,
    title: str = "Photo manage item",
    visibility=ArchiveItem.Visibility.PUBLIC,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str = "photos/55/original.jpg",
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
        original_size_bytes=1024,
        upload_status=upload_status,
        upload_error="",
        thumbnail_file_key=thumbnail_file_key,
    )
    return item


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoManageEditTests(TestCase):
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.photo_item = _create_photo_archive_item(title="Editable photo")

    def _photo_edit_payload(self, **overrides):
        payload = {
            "title": "Editable photo",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "description": "",
            "location": "",
            "context": "",
            "people_present": "",
            "notes": "",
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return payload

    def test_staff_can_open_photo_edit_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת תמונה")
        self.assertContains(resp, "ללא החלפת קובץ התמונה")
        self.assertContains(resp, "תיאור קצר")
        self.assertContains(resp, "מטא־דאטה משותף לפריט")
        self.assertContains(resp, "תמונות בפריט זה")
        self.assertContains(resp, 'name="title"')
        self.assertNotContains(resp, 'name="description"')
        self.assertNotContains(resp, 'name="author_name"')
        self.assertNotContains(resp, 'name="source_title"')
        self.assertNotContains(resp, 'name="body"')
        photo = self.photo_item.primary_photo_content
        self.assertContains(
            resp,
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.photo_item.id, "photo_id": photo.id},
            ),
        )
        self.assertContains(
            resp,
            reverse("archive-manage-photo-add", kwargs={"item_id": self.photo_item.id}),
        )

    def test_anonymous_cannot_open_photo_edit_page(self):
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id)
        )
        self.assertIn(resp.status_code, (302, 403))

    def test_non_staff_cannot_open_photo_edit_page(self):
        user = User.objects.create_user(
            username="photo_edit_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id)
        )
        self.assertEqual(resp.status_code, 403)

    def test_family_user_cannot_open_photo_edit_page(self):
        family_user = User.objects.create_user(
            username="photo_edit_family",
            password="test-pass",
        )
        family_user.groups.add(self.family_group)
        self.client.force_login(family_user)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id)
        )
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_update_photo_title_visibility_metadata_status_and_dates(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(
                title="Updated photo title",
                visibility=ArchiveItem.Visibility.PRIVATE,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                date_start="1940-05-01",
                date_end="1940-05-31",
                date_precision=ArchiveItem.DatePrecision.RANGE,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.photo_item.refresh_from_db()
        self.assertEqual(self.photo_item.title, "Updated photo title")
        self.assertEqual(self.photo_item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(
            self.photo_item.metadata_status, ArchiveItem.MetadataStatus.COMPLETED
        )
        self.assertEqual(self.photo_item.date_start.isoformat(), "1940-05-01")
        self.assertEqual(self.photo_item.date_end.isoformat(), "1940-05-31")
        self.assertEqual(
            self.photo_item.date_precision, ArchiveItem.DatePrecision.RANGE
        )

    def test_staff_can_update_photo_metadata_on_photo_component_page(self):
        photo = self.photo_item.primary_photo_content
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.photo_item.id, "photo_id": photo.id},
            ),
            data={
                "description": "  Wedding day caption  ",
                "location": " Cairo ",
                "context": "Family gathering\nafter ceremony",
                "people_present": " Uncle Moshe, Aunt Rivka ",
                "notes": "Scanned from album page 3",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            },
        )
        self.assertEqual(resp.status_code, 302)
        photo.refresh_from_db()
        self.assertEqual(photo.description, "Wedding day caption")
        self.assertEqual(photo.location, "Cairo")
        self.assertEqual(photo.context, "Family gathering\nafter ceremony")
        self.assertEqual(photo.people_present, "Uncle Moshe, Aunt Rivka")
        self.assertEqual(photo.notes, "Scanned from album page 3")

    def test_item_edit_does_not_overwrite_photo_component_metadata(self):
        photo = self.photo_item.primary_photo_content
        photo.description = "Keep me"
        photo.location = "Keep location"
        photo.save(update_fields=["description", "location", "updated_at"])
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(
                title="Shared title only",
                description="Should be ignored",
                location="Should also be ignored",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.photo_item.refresh_from_db()
        photo.refresh_from_db()
        self.assertEqual(self.photo_item.title, "Shared title only")
        self.assertEqual(photo.description, "Keep me")
        self.assertEqual(photo.location, "Keep location")

    def test_staff_can_update_photo_public_note(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(
                public_note="Curator photo note\nline two",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.photo_item.refresh_from_db()
        self.assertEqual(self.photo_item.public_note, "Curator photo note\nline two")

    def test_staff_can_update_photo_discovery_metadata(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Photo topic",
            slug="photo-topic",
        )
        existing_event = ArchiveEvent.objects.create(
            name="Photo event",
            slug="photo-event",
        )
        existing_tag = Tag.objects.create(name="photo-tag")

        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data={
                **self._photo_edit_payload(),
                "selected_categories": [str(existing_cat.id)],
                "selected_events": [str(existing_event.id)],
                "selected_tags": [str(existing_tag.id)],
                "categories": "New photo topic",
                "events": "New photo event",
                "tags": "new-photo-tag",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.photo_item.refresh_from_db()
        self.assertEqual(
            set(self.photo_item.categories.values_list("name", flat=True)),
            {"Photo topic", "New photo topic"},
        )
        self.assertEqual(
            set(self.photo_item.events.values_list("name", flat=True)),
            {"Photo event", "New photo event"},
        )
        self.assertEqual(
            set(self.photo_item.tags.values_list("name", flat=True)),
            {"photo-tag", "new-photo-tag"},
        )

    def test_photo_edit_get_preselects_existing_discovery_metadata(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Photo preselect category",
            slug="photo-preselect-category",
        )
        existing_tag = Tag.objects.create(name="photo-preselect-tag")
        self.photo_item.categories.add(existing_cat)
        self.photo_item.tags.add(existing_tag)

        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            f'<option value="{existing_cat.id}" selected>{existing_cat.name}</option>',
            html=True,
        )
        self.assertContains(
            resp,
            f'<option value="{existing_tag.id}" selected>{existing_tag.name}</option>',
            html=True,
        )

    def test_uploaded_photo_edit_redirects_to_archive_manage_list(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(title="Uploaded redirect photo"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))

    def test_pending_photo_edit_redirects_to_archive_manage_list_and_saves_metadata(
        self,
    ):
        pending_item = _create_photo_archive_item(
            title="Pending before edit",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=pending_item.id),
            data=self._photo_edit_payload(
                title="Pending after edit",
                visibility=ArchiveItem.Visibility.PRIVATE,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        pending_item.refresh_from_db()
        self.assertEqual(pending_item.title, "Pending after edit")
        self.assertEqual(pending_item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(
            pending_item.metadata_status,
            ArchiveItem.MetadataStatus.COMPLETED,
        )
        self.assertEqual(
            pending_item.primary_photo_content.upload_status,
            PhotoContent.UploadStatus.PENDING,
        )

    def test_pending_photo_edit_does_not_require_public_renderable_detail(self):
        pending_item = _create_photo_archive_item(
            title="Non-renderable pending",
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=pending_item.id),
            data=self._photo_edit_payload(title="Still non-renderable"),
        )
        detail_resp = self.client.get(f"/archive/{pending_item.id}/")
        self.assertEqual(detail_resp.status_code, 404)

    def test_photo_edit_does_not_change_original_file_key(self):
        original_key = self.photo_item.primary_photo_content.original_file_key
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(title="Key unchanged photo"),
        )
        self.photo_item.primary_photo_content.refresh_from_db()
        self.assertEqual(
            self.photo_item.primary_photo_content.original_file_key, original_key
        )

    @patch("documents.services.sqs.send_process_document_message")
    def test_photo_edit_does_not_create_document_or_enqueue_sqs(self, mock_enqueue):
        before_docs = Document.objects.count()
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=self.photo_item.id),
            data=self._photo_edit_payload(title="No document photo edit"),
        )
        self.assertEqual(Document.objects.count(), before_docs)
        mock_enqueue.assert_not_called()

    def test_manual_text_edit_still_works(self):
        manual_item = create_manual_text_archive_item(
            title="Manual still editable",
            body="Original body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=manual_item.id),
            data={
                "title": "Manual edited",
                "body": "Edited body.",
                "visibility": ArchiveItem.Visibility.PRIVATE,
                "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "categories": "",
                "events": "",
                "tags": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        manual_item.refresh_from_db()
        manual_item.manual_text_content.refresh_from_db()
        self.assertEqual(manual_item.title, "Manual edited")
        self.assertEqual(manual_item.manual_text_content.body, "Edited body.")

    def test_ocr_document_edit_route_still_works(self):
        doc = create_ocr_document(
            title="OCR edit unchanged",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת מטא־דאטה")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoManageDeleteTests(TestCase):
    DELETE_URL_TEMPLATE = "/archive/manage/{item_id}/delete/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_delete_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.photo_item = _create_photo_archive_item(title="Deletable photo")
        self.photo_content_id = self.photo_item.primary_photo_content.id

    def _delete_url(self, item_id: int) -> str:
        return self.DELETE_URL_TEMPLATE.format(item_id=item_id)

    def test_staff_can_open_photo_delete_confirmation(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._delete_url(self.photo_item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחיקת תמונה")
        self.assertContains(resp, self.photo_item.title)

    def test_get_confirmation_does_not_delete_photo_rows(self):
        self.client.force_login(self.staff)
        self.client.get(self._delete_url(self.photo_item.id))
        self.assertTrue(ArchiveItem.objects.filter(pk=self.photo_item.id).exists())
        self.assertTrue(PhotoContent.objects.filter(pk=self.photo_content_id).exists())

    def test_anonymous_cannot_delete_photo(self):
        resp = self.client.get(self._delete_url(self.photo_item.id))
        self.assertIn(resp.status_code, (302, 403))
        self.assertTrue(ArchiveItem.objects.filter(pk=self.photo_item.id).exists())

    def test_family_user_cannot_delete_photo(self):
        family_user = User.objects.create_user(
            username="photo_delete_family",
            password="test-pass",
        )
        family_user.groups.add(self.family_group)
        self.client.force_login(family_user)
        resp = self.client.get(self._delete_url(self.photo_item.id))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(ArchiveItem.objects.filter(pk=self.photo_item.id).exists())

    def test_photo_delete_requires_post(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._delete_url(self.photo_item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(ArchiveItem.objects.filter(pk=self.photo_item.id).exists())

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_photo_delete_removes_db_rows_and_s3_objects_after_commit(
        self, mock_delete_s3_object
    ):
        item_id = self.photo_item.id
        photo = self.photo_item.primary_photo_content
        photo.thumbnail_file_key = "photos/55/thumbnail_400.jpg"
        photo.save(update_fields=["thumbnail_file_key", "updated_at"])

        self.client.force_login(self.staff)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            resp = self.client.post(self._delete_url(item_id))

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())
        self.assertFalse(PhotoContent.objects.filter(pk=self.photo_content_id).exists())
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            mock_delete_s3_object.call_args_list,
            [
                call("test-uploads-bucket", "photos/55/original.jpg"),
                call("test-uploads-bucket", "photos/55/thumbnail_400.jpg"),
            ],
        )

    def test_manual_text_delete_still_works(self):
        manual_item = create_manual_text_archive_item(
            title="Manual delete", body="Body"
        )
        content_id = manual_item.manual_text_content.id
        self.client.force_login(self.staff)
        resp = self.client.post(self._delete_url(manual_item.id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        self.assertFalse(ArchiveItem.objects.filter(pk=manual_item.id).exists())
        self.assertFalse(ManualTextContent.objects.filter(pk=content_id).exists())

    def test_ocr_document_delete_route_still_returns_404(self):
        doc = create_ocr_document(
            title="OCR delete guard",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        archive_item_id = doc.archive_item_id
        self.client.force_login(self.staff)
        resp = self.client.post(self._delete_url(archive_item_id))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(ArchiveItem.objects.filter(pk=archive_item_id).exists())
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())

    def test_delete_action_appears_for_staff_on_manage_list_photo_row(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(
            resp,
            reverse("archive-manage-delete", kwargs={"item_id": self.photo_item.id}),
        )
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.photo_item.id}),
        )
