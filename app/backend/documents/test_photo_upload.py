import json
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveCategory, ArchiveItem, Document, PhotoContent, Tag
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
    archive_item_queryset_for_user,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.s3 import S3HeadObjectResult, build_photo_original_s3_key, photo_mime_to_s3_extension


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoUploadAccessTests(TestCase):
    NEW_URL = "/archive/manage/new/"
    CREATE_URL = "/api/photo-uploads/create/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_upload_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="photo_upload_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def test_staff_can_access_photo_create_branch(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "photo"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="photoUploadForm"')
        self.assertContains(resp, "/api/photo-uploads/create/")
        self.assertContains(resp, "תיאור קצר")
        self.assertContains(resp, 'name="description"')
        self.assertNotContains(resp, 'name="author_name"')
        self.assertNotContains(resp, 'name="source_title"')

    def test_non_staff_cannot_access_photo_create_branch(self):
        user = User.objects.create_user(
            username="photo_upload_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(self.NEW_URL, {"item_type": "photo"})
        self.assertEqual(resp.status_code, 403)

    def test_family_user_cannot_access_photo_create_branch(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(self.NEW_URL, {"item_type": "photo"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_cannot_access_photo_create_branch(self):
        resp = self.client.get(self.NEW_URL, {"item_type": "photo"})
        self.assertIn(resp.status_code, (302, 403))

    def test_non_staff_cannot_call_photo_create_api(self):
        user = User.objects.create_user(
            username="photo_api_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps({"title": "Blocked"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoUploadValidationTests(TestCase):
    CREATE_URL = "/api/photo-uploads/create/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_validation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.presigned_patcher = patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://s3.example/presigned-put",
        )
        self.mock_presigned = self.presigned_patcher.start()
        self.addCleanup(self.presigned_patcher.stop)

    def _valid_create_payload(self, **overrides):
        payload = {
            "title": "Family portrait",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "original_name": "portrait.jpg",
            "mime_type": "image/jpeg",
        }
        payload.update(overrides)
        return payload

    def test_rejects_unsupported_mime(self):
        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps(
                self._valid_create_payload(
                    mime_type="image/gif",
                    original_name="anim.gif",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("mime_type must be one of", resp.json()["error"])
        self.assertEqual(ArchiveItem.objects.count(), 0)

    def test_rejects_mime_extension_mismatch(self):
        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps(
                self._valid_create_payload(
                    mime_type="image/png",
                    original_name="portrait.jpg",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("does not match", resp.json()["error"])
        self.assertEqual(ArchiveItem.objects.count(), 0)

    def test_photo_mime_to_s3_extension_rejects_unknown_mime(self):
        with self.assertRaises(ValueError):
            photo_mime_to_s3_extension("image/gif")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoUploadFlowTests(TestCase):
    CREATE_URL = "/api/photo-uploads/create/"
    S3_CONTENT_LENGTH = 8192

    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_flow_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.presigned_patcher = patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://s3.example/presigned-put",
        )
        self.mock_presigned = self.presigned_patcher.start()
        self.addCleanup(self.presigned_patcher.stop)
        self.s3_head_patcher = patch(
            "documents.services.photo_upload.head_s3_object",
            return_value=S3HeadObjectResult(
                exists=True,
                content_type="image/jpeg",
                content_length=self.S3_CONTENT_LENGTH,
            ),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

    def _valid_create_payload(self, **overrides):
        payload = {
            "title": "Wedding photo",
            "visibility": ArchiveItem.Visibility.PRIVATE,
            "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
            "date_precision": ArchiveItem.DatePrecision.YEAR,
            "date_start": "1950-01-01",
            "categories": "Weddings, Cairo",
            "events": "Uncle's wedding",
            "discovery_tags": "family, 1950",
            "original_name": "wedding.jpg",
            "mime_type": "image/jpeg",
        }
        payload.update(overrides)
        return payload

    def _create_pending_upload(self):
        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps(self._valid_create_payload()),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.PENDING)
        return body

    def _post_complete(self, photo_content_id: int, payload: dict):
        return self.client.post(
            f"/api/photo-uploads/{photo_content_id}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    @patch("documents.views.send_process_document_message")
    def test_successful_finalize_creates_photo_archive_item(self, mock_enqueue):
        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]
        archive_item_id = created["archive_item_id"]

        resp = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["upload_complete"])
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.UPLOADED)
        self.assertEqual(body["upload_error"], "")
        self.assertEqual(body["original_size_bytes"], self.S3_CONTENT_LENGTH)

        item = ArchiveItem.objects.get(id=archive_item_id)
        self.assertEqual(item.item_type, ArchiveItem.ItemType.PHOTO)
        self.assertEqual(item.title, "Wedding photo")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)

        photo = PhotoContent.objects.get(id=photo_content_id)
        self.assertEqual(photo.original_filename, "wedding.jpg")
        self.assertEqual(photo.original_mime_type, "image/jpeg")
        self.assertEqual(photo.original_size_bytes, self.S3_CONTENT_LENGTH)
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.UPLOADED)
        self.assertEqual(photo.upload_error, "")
        self.assertEqual(
            photo.original_file_key,
            build_photo_original_s3_key(photo.id, "image/jpeg"),
        )
        self.assertIsNone(photo.width)
        self.assertIsNone(photo.height)
        self.assertEqual(photo.thumbnail_file_key, "")

        self.assertFalse(Document.objects.exists())
        mock_enqueue.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_persisted_size_comes_from_s3_head_object(self, mock_enqueue):
        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]

        resp = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["original_size_bytes"], self.S3_CONTENT_LENGTH)
        photo = PhotoContent.objects.get(id=photo_content_id)
        self.assertEqual(photo.original_size_bytes, self.S3_CONTENT_LENGTH)

    @patch("documents.views.send_process_document_message")
    def test_repeated_complete_is_idempotent_for_uploaded_photo(self, mock_enqueue):
        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]

        first = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["upload_complete"])
        self.mock_s3_head.reset_mock()

        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True,
            content_type="image/png",
            content_length=1,
        )
        second = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertTrue(body["upload_complete"])
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.UPLOADED)
        self.assertEqual(body["original_size_bytes"], self.S3_CONTENT_LENGTH)
        self.mock_s3_head.assert_not_called()

        photo = PhotoContent.objects.get(id=photo_content_id)
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.UPLOADED)
        self.assertEqual(photo.original_size_bytes, self.S3_CONTENT_LENGTH)
        self.assertEqual(photo.original_mime_type, "image/jpeg")

    @patch("documents.views.send_process_document_message")
    def test_discovery_metadata_saved_on_create(self, mock_enqueue):
        created = self._create_pending_upload()
        item = ArchiveItem.objects.get(id=created["archive_item_id"])
        self.assertEqual(
            list(item.categories.order_by("name").values_list("name", flat=True)),
            ["Cairo", "Weddings"],
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["Uncle's wedding"],
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"1950", "family"},
        )

    @patch("documents.views.send_process_document_message")
    def test_discovery_metadata_saved_from_selected_ids_and_new_names(self, mock_enqueue):
        existing_cat = ArchiveCategory.objects.create(
            name="Existing upload category",
            slug="existing-upload-category",
        )
        existing_tag = Tag.objects.create(name="existing-upload-tag")

        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps(
                self._valid_create_payload(
                    selected_categories=[existing_cat.id],
                    selected_tags=[existing_tag.id],
                    categories="Brand new upload category",
                    events="Brand new upload event",
                    discovery_tags="brand-new-upload-tag",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        item = ArchiveItem.objects.get(id=resp.json()["archive_item_id"])
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Existing upload category", "Brand new upload category"},
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["Brand new upload event"],
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"existing-upload-tag", "brand-new-upload-tag"},
        )
        mock_enqueue.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_photo_metadata_saved_on_create(self, mock_enqueue):
        resp = self.client.post(
            self.CREATE_URL,
            data=json.dumps(
                self._valid_create_payload(
                    description="  Bride and groom  ",
                    location=" Tel Aviv ",
                    context="Outdoor reception",
                    people_present=" Cousin Yael ",
                    notes="Color scan",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(id=resp.json()["photo_content_id"])
        self.assertEqual(photo.description, "Bride and groom")
        self.assertEqual(photo.location, "Tel Aviv")
        self.assertEqual(photo.context, "Outdoor reception")
        self.assertEqual(photo.people_present, "Cousin Yael")
        self.assertEqual(photo.notes, "Color scan")
        mock_enqueue.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_s3_content_type_mismatch_marks_failed(self, mock_enqueue):
        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]
        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True,
            content_type="image/png",
            content_length=2048,
        )

        resp = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.json()
        self.assertEqual(body["error"], "s3 content type mismatch")
        self.assertFalse(body["upload_complete"])
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.FAILED)
        self.assertEqual(body["upload_error"], "s3 content type mismatch")

        photo = PhotoContent.objects.get(id=photo_content_id)
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.FAILED)
        self.assertEqual(photo.original_size_bytes, 0)
        mock_enqueue.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_client_upload_failure_marks_failed(self, mock_enqueue):
        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]

        resp = self._post_complete(
            photo_content_id,
            {"success": False, "error": "S3 PUT failed"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["upload_complete"])
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.FAILED)
        self.assertEqual(body["upload_error"], "S3 PUT failed")
        self.mock_s3_head.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_s3_verification_failure_leaves_pending_retryable(self, mock_enqueue):
        from botocore.exceptions import BotoCoreError

        created = self._create_pending_upload()
        photo_content_id = created["photo_content_id"]
        self.mock_s3_head.side_effect = BotoCoreError()

        resp = self._post_complete(
            photo_content_id,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 502)
        body = resp.json()
        self.assertEqual(body["error"], "s3 verification failed")
        self.assertFalse(body["upload_complete"])
        self.assertEqual(body["upload_status"], PhotoContent.UploadStatus.PENDING)
        self.assertEqual(body["upload_error"], "")

        photo = PhotoContent.objects.get(id=photo_content_id)
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.PENDING)

    @patch("documents.views.send_process_document_message")
    def test_create_sets_upload_status_pending(self, mock_enqueue):
        created = self._create_pending_upload()
        photo = PhotoContent.objects.get(id=created["photo_content_id"])
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.PENDING)
        self.assertEqual(photo.upload_error, "")
        self.assertEqual(photo.original_size_bytes, 0)

    def test_unified_create_page_lists_photo_item_type(self):
        resp = self.client.get("/archive/manage/new/")
        self.assertContains(resp, 'value="photo"')
        self.assertContains(resp, "תמונה")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoArchiveBrowseEligibilityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_guard_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="photo_guard_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)

        self.manual_item = create_manual_text_archive_item(
            title="Visible manual text",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.uploaded_photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Uploaded photo in browse",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=self.uploaded_photo_item,
            original_file_key="photos/99/original.jpg",
            original_filename="uploaded.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            upload_error="",
        )
        self.pending_photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Pending photo hidden from browse",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=self.pending_photo_item,
            original_file_key="photos/100/original.jpg",
            original_filename="pending.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
            upload_error="",
        )

    def test_public_archive_list_includes_uploaded_photo(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.manual_item.title)
        self.assertContains(resp, self.uploaded_photo_item.title)
        self.assertNotContains(resp, self.pending_photo_item.title)

    def test_family_user_archive_list_includes_uploaded_photo(self):
        self.client.force_login(self.family_user)
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.manual_item.title)
        self.assertContains(resp, self.uploaded_photo_item.title)
        self.assertNotContains(resp, self.pending_photo_item.title)

    def test_public_archive_detail_returns_404_for_pending_photo(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.pending_photo_item.id})
        )
        self.assertEqual(resp.status_code, 404)

    def test_staff_manage_list_still_includes_photo_items(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded_photo_item.title)
        self.assertContains(resp, self.pending_photo_item.title)


class ArchiveAccessVsBrowseQuerysetTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="access_vs_browse_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)

        self.photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo access vs browse",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.uploaded_photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Uploaded photo browse eligible",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=self.uploaded_photo_item,
            original_file_key="photos/200/original.jpg",
            original_filename="browse.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=512,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            upload_error="",
        )

    def test_access_queryset_includes_photo_by_visibility_rules(self):
        qs = archive_item_queryset_for_user(self.family_user)
        self.assertTrue(qs.filter(pk=self.photo_item.pk).exists())
        self.assertTrue(qs.filter(pk=self.uploaded_photo_item.pk).exists())

    def test_browse_queryset_excludes_incomplete_photo(self):
        access_qs = archive_item_queryset_for_user(self.family_user)
        browse_qs = archive_browse_queryset_for_user(self.family_user)
        self.assertTrue(access_qs.filter(pk=self.photo_item.pk).exists())
        self.assertFalse(browse_qs.filter(pk=self.photo_item.pk).exists())

    def test_browse_queryset_includes_uploaded_photo(self):
        browse_qs = archive_browse_queryset_for_user(self.family_user)
        self.assertTrue(browse_qs.filter(pk=self.uploaded_photo_item.pk).exists())
