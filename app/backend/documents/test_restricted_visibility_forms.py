"""Restricted visibility — staff form/UI choices and write validation."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    Document,
    ManualTextContent,
    PhotoContent,
    VideoContent,
)
from documents.services.archive_item_access import VIEW_RESTRICTED_ARCHIVEITEM_CODENAME
from documents.services.archive_item_presentation import (
    archive_visibility_ui_choices,
    visibility_choice_label,
    visibility_label,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
)
from documents.services.archive_metadata_validation import (
    VISIBILITY_INVALID_ERROR,
    parse_visibility,
    validate_archive_metadata_fields,
)
from documents.test_archive_date_payloads import merge_default_date_fields

User = get_user_model()

RESTRICTED_HEBREW_LABEL = "רגיש — למורשים בלבד"
MANUAL_CREATE_URL = "/archive/manage/new/manual-text/"
EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"
OCR_UPLOAD_CREATE_URL = "/api/uploads/create/"
PHOTO_UPLOAD_CREATE_URL = "/api/photo-uploads/create/"
MANAGE_NEW_URL = "/archive/manage/new/"


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    return User.objects.get(pk=user.pk)


def _create_photo(*, title: str, visibility: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key="photos/forms/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item


class RestrictedVisibilityPresentationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_forms_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_forms_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_forms_super",
            email="super@example.com",
            password="test-pass",
        )

    def test_visibility_label_uses_exact_hebrew_restricted_label(self):
        self.assertEqual(
            visibility_label(ArchiveItem.Visibility.RESTRICTED),
            RESTRICTED_HEBREW_LABEL,
        )
        self.assertEqual(
            visibility_choice_label(ArchiveItem.Visibility.RESTRICTED),
            RESTRICTED_HEBREW_LABEL,
        )
        self.assertEqual(visibility_label("public"), "ציבורי")
        self.assertEqual(visibility_label("private"), "פרטי")

    def test_ui_choices_exclude_restricted_for_staff_without_permission(self):
        values = [value for value, _label in archive_visibility_ui_choices(self.staff)]
        self.assertEqual(
            values,
            [ArchiveItem.Visibility.PUBLIC, ArchiveItem.Visibility.PRIVATE],
        )
        labels = [label for _value, label in archive_visibility_ui_choices(self.staff)]
        self.assertNotIn(RESTRICTED_HEBREW_LABEL, labels)

    def test_ui_choices_include_restricted_for_authorized_staff_and_superuser(self):
        for user in (self.authorized, self.superuser):
            choices = archive_visibility_ui_choices(user)
            values = [value for value, _label in choices]
            self.assertEqual(
                values,
                [
                    ArchiveItem.Visibility.PUBLIC,
                    ArchiveItem.Visibility.PRIVATE,
                    ArchiveItem.Visibility.RESTRICTED,
                ],
            )
            self.assertIn(
                (ArchiveItem.Visibility.RESTRICTED, RESTRICTED_HEBREW_LABEL),
                choices,
            )


class RestrictedVisibilityValidatorTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_validator_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_validator_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_validator_super",
            email="validator-super@example.com",
            password="test-pass",
        )

    def test_parse_visibility_rejects_unknown_values(self):
        with self.assertRaises(ValueError) as ctx:
            parse_visibility("secret", user=self.authorized)
        self.assertEqual(str(ctx.exception), VISIBILITY_INVALID_ERROR)

    def test_parse_visibility_rejects_restricted_for_unauthorized_staff(self):
        with self.assertRaises(ValueError) as ctx:
            parse_visibility(ArchiveItem.Visibility.RESTRICTED, user=self.staff)
        self.assertEqual(str(ctx.exception), VISIBILITY_INVALID_ERROR)

    def test_parse_visibility_allows_restricted_for_authorized_and_superuser(self):
        for user in (self.authorized, self.superuser):
            self.assertEqual(
                parse_visibility(ArchiveItem.Visibility.RESTRICTED, user=user),
                ArchiveItem.Visibility.RESTRICTED,
            )

    def test_parse_visibility_defaults_remain_private(self):
        self.assertEqual(parse_visibility(None, user=self.staff), "private")
        self.assertEqual(parse_visibility("", user=self.authorized), "private")

    def test_validate_archive_metadata_fields_rejects_unauthorized_restricted(self):
        errors = validate_archive_metadata_fields(
            title="T",
            visibility=ArchiveItem.Visibility.RESTRICTED,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            date_start=None,
            date_end=None,
            user=self.staff,
        )
        self.assertEqual(errors, [VISIBILITY_INVALID_ERROR])


class RestrictedManualTextFormWriteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_manual_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_manual_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_manual_super",
            email="manual-super@example.com",
            password="test-pass",
        )

    def _payload(self, **overrides):
        payload = {
            "title": "Manual restricted write",
            "body": "Typed body",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def test_create_form_hides_restricted_option_without_permission(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANUAL_CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ציבורי")
        self.assertContains(resp, "פרטי")
        self.assertNotContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertNotContains(resp, 'value="restricted"')

    def test_create_form_shows_restricted_option_with_permission(self):
        self.client.force_login(self.authorized)
        resp = self.client.get(MANUAL_CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertContains(resp, 'value="restricted"')

    def test_superuser_create_form_shows_restricted_option(self):
        self.client.force_login(self.superuser)
        resp = self.client.get(MANUAL_CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_HEBREW_LABEL)

    def test_unauthorized_staff_cannot_create_restricted_via_crafted_post(self):
        before = ArchiveItem.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._payload(visibility=ArchiveItem.Visibility.RESTRICTED),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)
        self.assertFalse(ManualTextContent.objects.filter(body="Typed body").exists())

    def test_authorized_staff_can_create_restricted_manual_text(self):
        self.client.force_login(self.authorized)
        resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._payload(
                title="Authorized restricted manual",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Authorized restricted manual")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_superuser_can_create_restricted_manual_text(self):
        self.client.force_login(self.superuser)
        resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._payload(
                title="Superuser restricted manual",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Superuser restricted manual")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_unauthorized_staff_cannot_change_item_to_restricted_on_edit(self):
        item = create_manual_text_archive_item(
            title="Edit stay public",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Edit stay public",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(item.title, "Edit stay public")

    def test_authorized_staff_can_change_visibility_to_and_from_restricted(self):
        item = create_manual_text_archive_item(
            title="Toggle visibility",
            body="body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.authorized)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Toggle visibility",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Toggle visibility",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)

    def test_public_private_create_unchanged_for_unauthorized_staff(self):
        self.client.force_login(self.staff)
        for visibility in (
            ArchiveItem.Visibility.PUBLIC,
            ArchiveItem.Visibility.PRIVATE,
        ):
            title = f"Manual {visibility}"
            resp = self.client.post(
                MANUAL_CREATE_URL,
                data=self._payload(title=title, visibility=visibility),
            )
            self.assertEqual(resp.status_code, 302)
            item = ArchiveItem.objects.get(title=title)
            self.assertEqual(item.visibility, visibility)

    def test_invalid_visibility_still_rejected(self):
        before = ArchiveItem.objects.count()
        self.client.force_login(self.authorized)
        resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._payload(visibility="secret"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class RestrictedOcrUploadWriteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_ocr_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_ocr_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_ocr_super",
            email="ocr-super@example.com",
            password="test-pass",
        )
        self.presigned_patcher = patch(
            "documents.views.create_presigned_put",
            return_value="https://example/upload",
        )
        self.presigned_patcher.start()
        self.addCleanup(self.presigned_patcher.stop)

    def _payload(self, **overrides):
        payload = {
            "title": "OCR restricted write",
            "doc_type": "IMAGE",
            "text_input_type": "HANDWRITTEN",
            "original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
            "visibility": ArchiveItem.Visibility.PRIVATE,
        }
        payload.update(overrides)
        return payload

    def _post(self, user, payload):
        self.client.force_login(user)
        return self.client.post(
            OCR_UPLOAD_CREATE_URL,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_upload_form_hides_restricted_without_permission(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="visibility"')
        self.assertNotContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertNotContains(resp, 'value="restricted"')

    def test_upload_form_shows_restricted_with_permission(self):
        self.client.force_login(self.authorized)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertContains(resp, 'value="restricted"')

    def test_unauthorized_staff_cannot_create_restricted_ocr_via_json(self):
        before_docs = Document.objects.count()
        before_items = ArchiveItem.objects.count()
        resp = self._post(
            self.staff,
            self._payload(visibility=ArchiveItem.Visibility.RESTRICTED),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(VISIBILITY_INVALID_ERROR.encode(), resp.content)
        self.assertEqual(Document.objects.count(), before_docs)
        self.assertEqual(ArchiveItem.objects.count(), before_items)

    def test_authorized_staff_can_create_restricted_ocr(self):
        resp = self._post(
            self.authorized,
            self._payload(
                title="Authorized restricted OCR",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_superuser_can_create_restricted_ocr(self):
        resp = self._post(
            self.superuser,
            self._payload(
                title="Superuser restricted OCR",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_default_visibility_remains_private(self):
        payload = self._payload()
        del payload["visibility"]
        resp = self._post(self.staff, payload)
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.PRIVATE)

    def test_invalid_visibility_rejected(self):
        before = Document.objects.count()
        resp = self._post(self.authorized, self._payload(visibility="secret"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn(VISIBILITY_INVALID_ERROR.encode(), resp.content)
        self.assertEqual(Document.objects.count(), before)

    def test_unauthorized_staff_cannot_set_restricted_on_ocr_metadata_edit(self):
        doc = create_ocr_document(
            title="OCR edit stay private",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=merge_default_date_fields(
                {
                    "title": "OCR edit stay private",
                    "visibility": ArchiveItem.Visibility.RESTRICTED,
                    "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                    "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                }
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.PRIVATE)

    def test_authorized_staff_can_set_restricted_on_ocr_metadata_edit(self):
        doc = create_ocr_document(
            title="OCR edit to restricted",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.authorized)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=merge_default_date_fields(
                {
                    "title": "OCR edit to restricted",
                    "visibility": ArchiveItem.Visibility.RESTRICTED,
                    "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
                    "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                }
            ),
        )
        self.assertEqual(resp.status_code, 302)
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.visibility, ArchiveItem.Visibility.RESTRICTED)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class RestrictedPhotoUploadWriteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_photo_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_photo_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_photo_super",
            email="photo-super@example.com",
            password="test-pass",
        )
        self.presigned_patcher = patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://example/photo-upload",
        )
        self.presigned_patcher.start()
        self.addCleanup(self.presigned_patcher.stop)

    def _payload(self, **overrides):
        payload = {
            "title": "Photo restricted write",
            "visibility": ArchiveItem.Visibility.PRIVATE,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "original_name": "photo.jpg",
            "mime_type": "image/jpeg",
        }
        payload.update(overrides)
        return payload

    def _post(self, user, payload):
        self.client.force_login(user)
        return self.client.post(
            PHOTO_UPLOAD_CREATE_URL,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _edit_payload(self, **overrides):
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

    def test_photo_create_form_hides_restricted_without_permission(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "photo"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertNotContains(resp, 'value="restricted"')

    def test_photo_create_form_shows_restricted_with_permission(self):
        self.client.force_login(self.authorized)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "photo"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertContains(resp, 'value="restricted"')

    def test_unauthorized_staff_cannot_create_restricted_photo_via_json(self):
        before_items = ArchiveItem.objects.count()
        before_photos = PhotoContent.objects.count()
        resp = self._post(
            self.staff,
            self._payload(visibility=ArchiveItem.Visibility.RESTRICTED),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], VISIBILITY_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(PhotoContent.objects.count(), before_photos)

    def test_authorized_staff_can_create_restricted_photo(self):
        resp = self._post(
            self.authorized,
            self._payload(
                title="Authorized restricted photo",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 201)
        item = ArchiveItem.objects.get(id=resp.json()["archive_item_id"])
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_superuser_can_create_restricted_photo(self):
        resp = self._post(
            self.superuser,
            self._payload(
                title="Superuser restricted photo",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 201)
        item = ArchiveItem.objects.get(id=resp.json()["archive_item_id"])
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_unauthorized_staff_cannot_set_restricted_on_photo_edit(self):
        item = _create_photo(
            title="Photo edit stay public",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._edit_payload(
                title="Photo edit stay public",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)

    def test_authorized_staff_can_set_restricted_on_photo_edit(self):
        item = _create_photo(
            title="Photo edit to restricted",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.authorized)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._edit_payload(
                title="Photo edit to restricted",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_public_private_photo_create_unchanged(self):
        resp = self._post(
            self.staff,
            self._payload(
                title="Photo private default path",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
        )
        self.assertEqual(resp.status_code, 201)
        item = ArchiveItem.objects.get(id=resp.json()["archive_item_id"])
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)


class RestrictedVideoFormWriteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="restricted_video_staff",
            password="test-pass",
            is_staff=True,
        )
        self.authorized = _grant_restricted_permission(
            User.objects.create_user(
                username="restricted_video_authorized",
                password="test-pass",
                is_staff=True,
            )
        )
        self.superuser = User.objects.create_superuser(
            username="restricted_video_super",
            email="video-super@example.com",
            password="test-pass",
        )

    def _payload(self, **overrides):
        payload = {
            "item_type": "video",
            "title": "Video restricted write",
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
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

    def test_create_form_hides_restricted_option_without_permission(self):
        self.client.force_login(self.staff)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ציבורי")
        self.assertContains(resp, "פרטי")
        self.assertNotContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertNotContains(resp, 'value="restricted"')

    def test_create_form_shows_restricted_option_with_permission(self):
        self.client.force_login(self.authorized)
        resp = self.client.get(MANAGE_NEW_URL, {"item_type": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_HEBREW_LABEL)
        self.assertContains(resp, 'value="restricted"')

    def test_unauthorized_staff_cannot_create_restricted_via_crafted_post(self):
        before_items = ArchiveItem.objects.count()
        before_videos = VideoContent.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(visibility=ArchiveItem.Visibility.RESTRICTED),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(VideoContent.objects.count(), before_videos)

    def test_authorized_staff_can_create_restricted_video(self):
        self.client.force_login(self.authorized)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(
                title="Authorized restricted video",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Authorized restricted video")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.VIDEO)
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_superuser_can_create_restricted_video(self):
        self.client.force_login(self.superuser)
        resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._payload(
                title="Superuser restricted video",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Superuser restricted video")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

    def test_unauthorized_staff_cannot_change_item_to_restricted_on_edit(self):
        item = create_video_archive_item(
            title="Video edit stay public",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Video edit stay public",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, VISIBILITY_INVALID_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)

    def test_authorized_staff_can_change_visibility_to_and_from_restricted(self):
        item = create_video_archive_item(
            title="Video toggle visibility",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.authorized)
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Video toggle visibility",
                visibility=ArchiveItem.Visibility.RESTRICTED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.RESTRICTED)

        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._payload(
                title="Video toggle visibility",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)

    def test_unauthorized_staff_cannot_get_or_post_delete_restricted_video(self):
        item = create_video_archive_item(
            title="Restricted video delete blocked",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.authorized,
        )
        content_id = item.video_content.pk
        delete_url = reverse("archive-manage-delete", kwargs={"item_id": item.id})

        self.client.force_login(self.staff)
        get_resp = self.client.get(delete_url)
        self.assertEqual(get_resp.status_code, 404)

        before_items = ArchiveItem.objects.count()
        before_videos = VideoContent.objects.count()
        post_resp = self.client.post(delete_url)
        self.assertEqual(post_resp.status_code, 404)
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(VideoContent.objects.count(), before_videos)
        self.assertTrue(ArchiveItem.objects.filter(pk=item.pk).exists())
        self.assertTrue(VideoContent.objects.filter(pk=content_id).exists())

    def test_authorized_staff_can_delete_restricted_video(self):
        item = create_video_archive_item(
            title="Restricted video delete allowed",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.authorized,
        )
        item_id = item.pk
        content_id = item.video_content.pk
        delete_url = reverse("archive-manage-delete", kwargs={"item_id": item_id})

        self.client.force_login(self.authorized)
        get_resp = self.client.get(delete_url)
        self.assertEqual(get_resp.status_code, 200)
        self.assertContains(get_resp, "מחיקת סרטון")

        post_resp = self.client.post(delete_url)
        self.assertEqual(post_resp.status_code, 302)
        self.assertEqual(post_resp["Location"], reverse("archive-manage-list"))
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())
        self.assertFalse(VideoContent.objects.filter(pk=content_id).exists())

    def test_superuser_can_delete_restricted_video(self):
        item = create_video_archive_item(
            title="Restricted video delete superuser",
            source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.superuser,
        )
        item_id = item.pk
        delete_url = reverse("archive-manage-delete", kwargs={"item_id": item_id})

        self.client.force_login(self.superuser)
        post_resp = self.client.post(delete_url)
        self.assertEqual(post_resp.status_code, 302)
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())

    def test_public_and_private_video_delete_remain_available_to_staff(self):
        for visibility, title in (
            (ArchiveItem.Visibility.PUBLIC, "Public video delete ok"),
            (ArchiveItem.Visibility.PRIVATE, "Private video delete ok"),
        ):
            item = create_video_archive_item(
                title=title,
                source_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                visibility=visibility,
            )
            delete_url = reverse("archive-manage-delete", kwargs={"item_id": item.id})
            self.client.force_login(self.staff)
            get_resp = self.client.get(delete_url)
            self.assertEqual(get_resp.status_code, 200)
            post_resp = self.client.post(delete_url)
            self.assertEqual(post_resp.status_code, 302)
            self.assertFalse(ArchiveItem.objects.filter(pk=item.pk).exists())
