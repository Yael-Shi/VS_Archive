"""PR6: staff PHOTO upload/renderability status clarity on manage surfaces."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, Document, PhotoContent
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.photo_presentation import (
    photo_archive_renderability_label,
    photo_archive_renderability_tone,
    photo_is_archive_renderable,
    photo_upload_status_label,
    photo_upload_status_tone,
)


def _create_photo_archive_item(
    *,
    title: str,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str = "photos/99/original.jpg",
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
        upload_status=upload_status,
        upload_error="",
    )
    return item


class PhotoPresentationHelperTests(TestCase):
    def test_photo_upload_status_label_maps_known_values(self):
        uploaded = _create_photo_archive_item(
            title="Uploaded",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        ).photo_content
        pending = _create_photo_archive_item(
            title="Pending",
            upload_status=PhotoContent.UploadStatus.PENDING,
        ).photo_content
        failed = _create_photo_archive_item(
            title="Failed",
            upload_status=PhotoContent.UploadStatus.FAILED,
        ).photo_content
        self.assertEqual(photo_upload_status_label(uploaded), "הועלה")
        self.assertEqual(photo_upload_status_label(pending), "ממתין להעלאה")
        self.assertEqual(photo_upload_status_label(failed), "העלאה נכשלה")

    def test_photo_is_archive_renderable_requires_uploaded_and_key(self):
        uploaded = _create_photo_archive_item(
            title="Renderable",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="photos/1/original.jpg",
        ).photo_content
        empty_key = _create_photo_archive_item(
            title="Empty key",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="",
        ).photo_content
        pending = _create_photo_archive_item(
            title="Pending",
            upload_status=PhotoContent.UploadStatus.PENDING,
        ).photo_content
        self.assertTrue(photo_is_archive_renderable(uploaded))
        self.assertEqual(photo_archive_renderability_label(uploaded), "מוצג בארכיון")
        self.assertFalse(photo_is_archive_renderable(empty_key))
        self.assertEqual(
            photo_archive_renderability_label(empty_key), "לא מוצג בארכיון"
        )
        self.assertFalse(photo_is_archive_renderable(pending))
        self.assertEqual(photo_archive_renderability_label(pending), "לא מוצג בארכיון")

    def test_photo_presentation_helpers_handle_none_photo_content(self):
        self.assertEqual(photo_upload_status_label(None), "")
        self.assertEqual(photo_upload_status_tone(None), "")
        self.assertFalse(photo_is_archive_renderable(None))
        self.assertEqual(photo_archive_renderability_label(None), "לא מוצג בארכיון")
        self.assertEqual(photo_archive_renderability_tone(None), "badge-warn")

    def test_photo_presentation_helpers_handle_unknown_upload_status(self):
        photo_content = _create_photo_archive_item(
            title="Unknown status",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        ).photo_content
        PhotoContent.objects.filter(pk=photo_content.pk).update(upload_status="UNKNOWN")
        photo_content.refresh_from_db()

        self.assertEqual(photo_upload_status_label(photo_content), "UNKNOWN")
        self.assertEqual(photo_upload_status_tone(photo_content), "")
        self.assertFalse(photo_is_archive_renderable(photo_content))
        self.assertEqual(
            photo_archive_renderability_label(photo_content), "לא מוצג בארכיון"
        )
        self.assertEqual(photo_archive_renderability_tone(photo_content), "badge-warn")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoManageListStatusClarityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_status_staff",
            password="test-pass",
            is_staff=True,
        )
        self.uploaded = _create_photo_archive_item(
            title="Uploaded manage photo",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self.pending = _create_photo_archive_item(
            title="Pending manage photo",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.failed = _create_photo_archive_item(
            title="Failed manage photo",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )
        self.empty_key = _create_photo_archive_item(
            title="Empty key manage photo",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="",
        )
        self.manual_item = create_manual_text_archive_item(
            title="Manual manage row",
            body="Body",
        )
        self.ocr_doc = create_ocr_document(
            title="OCR manage row",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )

    def _manage_list_response(self):
        self.client.force_login(self.staff)
        return self.client.get(reverse("archive-manage-list"))

    @staticmethod
    def _row_html_for_title(html: str, title: str) -> str:
        title_pos = html.find(title)
        if title_pos == -1:
            raise AssertionError(f"missing manage-list row title={title!r}")
        return html[title_pos:].split("</tr>", 1)[0]

    def test_manage_list_shows_photo_upload_status_labels_in_correct_rows(self):
        resp = self._manage_list_response()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "סטטוס העלאת תמונה")
        self.assertContains(resp, "תצוגת תמונה בארכיון")
        html = resp.content.decode()

        uploaded_row = self._row_html_for_title(html, self.uploaded.title)
        pending_row = self._row_html_for_title(html, self.pending.title)
        failed_row = self._row_html_for_title(html, self.failed.title)
        empty_key_row = self._row_html_for_title(html, self.empty_key.title)

        self.assertIn("הועלה", uploaded_row)
        self.assertIn("ממתין להעלאה", pending_row)
        self.assertIn("העלאה נכשלה", failed_row)
        self.assertIn("הועלה", empty_key_row)

        self.assertNotIn("ממתין להעלאה", uploaded_row)
        self.assertNotIn("העלאה נכשלה", uploaded_row)
        self.assertNotIn("הועלה", pending_row)
        self.assertNotIn("העלאה נכשלה", pending_row)
        self.assertNotIn("ממתין להעלאה", failed_row)
        self.assertNotIn("הועלה", failed_row)

    def test_manage_list_shows_renderability_labels_in_correct_rows(self):
        resp = self._manage_list_response()
        html = resp.content.decode()

        uploaded_row = self._row_html_for_title(html, self.uploaded.title)
        pending_row = self._row_html_for_title(html, self.pending.title)
        failed_row = self._row_html_for_title(html, self.failed.title)
        empty_key_row = self._row_html_for_title(html, self.empty_key.title)

        renderable_badge = ">מוצג בארכיון</span>"
        non_renderable_badge = ">לא מוצג בארכיון</span>"

        self.assertIn(renderable_badge, uploaded_row)
        self.assertNotIn(non_renderable_badge, uploaded_row)

        for row in (pending_row, failed_row, empty_key_row):
            self.assertIn(non_renderable_badge, row)
            self.assertNotIn(renderable_badge, row)

    def test_manual_text_and_ocr_rows_unaffected_by_photo_status_columns(self):
        resp = self._manage_list_response()
        html = resp.content.decode()
        manual_row = self._row_html_for_title(html, self.manual_item.title)
        ocr_row = self._row_html_for_title(html, self.ocr_doc.archive_item.title)
        self.assertNotIn("ממתין להעלאה", manual_row)
        self.assertNotIn("מוצג בארכיון", manual_row)
        self.assertNotIn("לא מוצג בארכיון", manual_row)
        self.assertNotIn("ממתין להעלאה", ocr_row)
        self.assertNotIn("מוצג בארכיון", ocr_row)
        self.assertNotIn("לא מוצג בארכיון", ocr_row)
        self.assertIn("—", manual_row)
        self.assertIn("—", ocr_row)

    @patch("documents.views.create_presigned_get")
    def test_manage_list_does_not_generate_presigned_get(self, mock_presigned_get):
        resp = self._manage_list_response()
        self.assertEqual(resp.status_code, 200)
        mock_presigned_get.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoManageCopyClarityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_copy_staff",
            password="test-pass",
            is_staff=True,
        )
        self.photo_item = _create_photo_archive_item(title="Copy clarity photo")

    def test_photo_edit_page_contains_metadata_only_guidance(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            f"/archive/manage/{self.photo_item.id}/edit/",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת מטא־דאטה בלבד")
        self.assertContains(resp, "לא ניתן להחליף או להעלות מחדש את קובץ התמונה")
        self.assertContains(resp, "תופיע בארכיון הציבורי רק לאחר שההעלאה הושלמה")
        self.assertNotContains(resp, 'type="file"')

    def test_photo_delete_confirmation_contains_db_delete_and_s3_deferred_guidance(
        self,
    ):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-manage-delete", kwargs={"item_id": self.photo_item.id}),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמחק את פריט הארכיון (תמונה) ממסד הנתונים")
        self.assertContains(resp, "ניקוי הקובץ השמור ב-S3 נדחה")
        self.assertContains(resp, "אינה מוחקת את אובייקט התמונה מהאחסון")

    @patch("boto3.client")
    def test_photo_delete_still_does_not_attempt_s3_cleanup(self, mock_boto_client):
        item_id = self.photo_item.id
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("archive-manage-delete", kwargs={"item_id": item_id}),
        )
        self.assertEqual(resp.status_code, 302)
        mock_boto_client.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoPublicArchiveUnchangedTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_public_staff",
            password="test-pass",
            is_staff=True,
        )
        self.pending = _create_photo_archive_item(
            title="Public hidden pending",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        self.uploaded = _create_photo_archive_item(
            title="Public visible uploaded",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )

    def test_public_archive_list_hides_non_renderable_photo(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.uploaded.title)
        self.assertNotContains(resp, self.pending.title)

    def test_public_archive_detail_404_for_non_renderable_photo(self):
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{self.pending.id}/")
        self.assertEqual(resp.status_code, 404)
