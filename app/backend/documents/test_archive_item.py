import json
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from documents.admin import ArchiveItemAdmin, DocumentAdmin, ManualTextContentAdmin
from documents.models import ArchiveItem, Document, DocumentSourceFile, DocumentTextResult, ManualTextContent
from documents.services.archive_items import (
    archive_item_field_values_from_document,
    create_manual_text_archive_item,
    create_ocr_document,
    update_manual_text_archive_item,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME


class ArchiveItemFoundationTests(TestCase):
    def test_create_ocr_document_links_archive_item_with_shared_fields(self):
        doc = create_ocr_document(
            title="Shared fields test",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
            date_precision=Document.DatePrecision.YEAR,
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        self.assertIsNotNone(doc.archive_item_id)
        item = doc.archive_item
        self.assertEqual(item.item_type, ArchiveItem.ItemType.OCR_DOCUMENT)
        self.assertEqual(item.title, doc.title)
        self.assertEqual(item.visibility, doc.visibility)
        self.assertEqual(item.date_precision, doc.date_precision)
        self.assertEqual(item.metadata_status, doc.metadata_status)

    def test_document_objects_create_requires_explicit_archive_item(self):
        with self.assertRaises(IntegrityError):
            Document.objects.create(
                title="Missing archive item",
                doc_type=Document.DocType.IMAGE,
                text_input_type=Document.TextInputType.HANDWRITTEN,
            )

    def test_archive_item_field_values_from_document_copies_without_inference(self):
        doc = create_ocr_document(
            title="Copy test",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            metadata_status=Document.MetadataStatus.COMPLETED,
            date_precision=Document.DatePrecision.RANGE,
        )
        values = archive_item_field_values_from_document(doc)
        self.assertEqual(values["title"], "Copy test")
        self.assertEqual(values["visibility"], Document.Visibility.PUBLIC)
        self.assertEqual(values["metadata_status"], Document.MetadataStatus.COMPLETED)
        self.assertEqual(values["date_precision"], Document.DatePrecision.RANGE)

    def test_archive_item_delete_cascades_document_and_text_results(self):
        doc = create_ocr_document(
            title="Archive item parent delete",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc_id = doc.id
        archive_item_id = doc.archive_item_id
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="test-engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="sample",
        )
        ArchiveItem.objects.filter(pk=archive_item_id).delete()
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
        self.assertFalse(DocumentTextResult.objects.filter(document_id=doc_id).exists())
        self.assertFalse(ArchiveItem.objects.filter(pk=archive_item_id).exists())

    def test_document_instance_delete_removes_linked_archive_item(self):
        doc = create_ocr_document(
            title="Document instance delete",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        archive_item_id = doc.archive_item_id
        doc.delete()
        self.assertFalse(ArchiveItem.objects.filter(pk=archive_item_id).exists())

    def test_document_queryset_delete_removes_linked_archive_items(self):
        doc_one = create_ocr_document(
            title="Bulk delete one",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc_two = create_ocr_document(
            title="Bulk delete two",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        archive_item_ids = {doc_one.archive_item_id, doc_two.archive_item_id}
        Document.objects.filter(
            pk__in=[doc_one.pk, doc_two.pk],
        ).delete()
        self.assertFalse(ArchiveItem.objects.filter(pk__in=archive_item_ids).exists())


class ArchiveItemAdminPolicyTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = User.objects.create_superuser(
            username="archive_item_admin",
            password="test-pass",
            email="admin@example.com",
        )
        self.site = AdminSite()

    def test_document_admin_has_add_permission_false(self):
        self.assertFalse(
            DocumentAdmin(Document, self.site).has_add_permission(self.request)
        )

    def test_archive_item_admin_has_add_permission_false(self):
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, self.site).has_add_permission(self.request)
        )

    def test_archive_item_admin_has_change_permission_false(self):
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, self.site).has_change_permission(self.request)
        )

    def test_archive_item_admin_has_view_permission_true_for_superuser(self):
        self.assertTrue(
            ArchiveItemAdmin(ArchiveItem, self.site).has_view_permission(self.request)
        )

    def test_archive_item_admin_has_delete_permission_false(self):
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, self.site).has_delete_permission(self.request)
        )

    def test_document_admin_archive_item_is_readonly(self):
        admin = DocumentAdmin(Document, self.site)
        self.assertIn("archive_item", admin.readonly_fields)


class ArchiveItemUploadIntegrationTests(TestCase):
    def setUp(self):
        from documents.s3 import S3HeadObjectResult

        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.staff = User.objects.create_user(
            username="archive_item_upload_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _base_create_payload(self, **overrides):
        payload = {
            "title": "Upload archive item test",
            "doc_type": "IMAGE",
            "mime_type": "image/jpeg",
            "original_name": "scan.jpg",
            "text_input_type": "HANDWRITTEN",
            "visibility": "public",
            "date_precision": "YEAR",
            "metadata_status": "COMPLETED",
        }
        payload.update(overrides)
        return payload

    def _multi_files_payload(self, count: int = 2):
        return {
            "title": "Multi archive item test",
            "text_input_type": "HANDWRITTEN",
            "visibility": "private",
            "files": [
                {
                    "original_name": f"page-{i + 1}.jpg",
                    "mime_type": "image/jpeg",
                }
                for i in range(count)
            ],
        }

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_single_file_create_links_ocr_document_archive_item(self, _mock_put):
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(self._base_create_payload()),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.item_type, ArchiveItem.ItemType.OCR_DOCUMENT)
        self.assertEqual(doc.archive_item.visibility, Document.Visibility.PUBLIC)
        self.assertEqual(doc.archive_item.title, doc.title)
        self.assertEqual(doc.visibility, Document.Visibility.PUBLIC)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_multi_image_create_links_ocr_document_archive_item(self, _mock_put):
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(self._multi_files_payload(count=2)),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.expected_source_file_count, 2)
        self.assertEqual(doc.archive_item.item_type, ArchiveItem.ItemType.OCR_DOCUMENT)
        self.assertEqual(doc.archive_item.visibility, Document.Visibility.PRIVATE)
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 2)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_still_enqueues_processing(self, mock_enqueue, _mock_put):
        create_resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(self._base_create_payload()),
            content_type="application/json",
        )
        doc_id = create_resp.json()["document_id"]
        complete_resp = self.client.post(
            f"/api/uploads/{doc_id}/complete/",
            data=json.dumps({"success": True, "file_size": 2048, "file_mime": "image/jpeg"}),
            content_type="application/json",
        )
        self.assertEqual(complete_resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)


class ManualTextArchiveItemTests(TestCase):
    CREATE_URL = "/archive/manage/new/manual-text/"
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_text_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="family_user"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _valid_create_payload(self, **overrides):
        payload = {
            "title": "Valid manual text",
            "body": "Typed content.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return payload

    @patch("documents.services.sqs.send_process_document_message")
    def test_create_manual_text_archive_item_sets_item_type(self, mock_enqueue):
        item = create_manual_text_archive_item(
            title="Manual note",
            body="Typed by staff.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)
        self.assertEqual(item.title, "Manual note")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(item.metadata_status, ArchiveItem.MetadataStatus.NEEDS_COMPLETION)
        mock_enqueue.assert_not_called()

    def test_create_manual_text_archive_item_creates_manual_text_content(self):
        item = create_manual_text_archive_item(
            title="Body test",
            body="First-party typed content.",
        )
        content = ManualTextContent.objects.get(archive_item=item)
        self.assertEqual(content.body, "First-party typed content.")

    def test_create_manual_text_archive_item_does_not_create_document(self):
        before = Document.objects.count()
        create_manual_text_archive_item(title="No document", body="text")
        self.assertEqual(Document.objects.count(), before)

    @patch("documents.services.sqs.send_process_document_message")
    def test_create_manual_text_archive_item_does_not_enqueue_sqs(self, mock_enqueue):
        create_manual_text_archive_item(title="No queue", body="text")
        mock_enqueue.assert_not_called()

    def test_archive_list_shows_public_manual_text_to_anonymous(self):
        public_item = create_manual_text_archive_item(
            title="Public list item",
            body="Public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_manual_text_archive_item(
            title="Private list item",
            body="Private body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        resp = self.client.get("/archive/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, public_item.title)
        self.assertNotContains(resp, "Private list item")

    def test_archive_list_shows_public_and_private_to_family_user(self):
        create_manual_text_archive_item(
            title="Public for family",
            body="x",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_manual_text_archive_item(
            title="Private family item",
            body="x",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.get("/archive/")
        self.assertContains(resp, "Public for family")
        self.assertContains(resp, "Private family item")

    def test_archive_list_shows_public_and_private_to_staff(self):
        create_manual_text_archive_item(
            title="Staff public",
            body="x",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_manual_text_archive_item(
            title="Staff private",
            body="x",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/archive/")
        self.assertContains(resp, "Staff public")
        self.assertContains(resp, "Staff private")

    def test_archive_detail_public_manual_text_visible_to_anonymous(self):
        item = create_manual_text_archive_item(
            title="Public detail",
            body="Visible manual text body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Visible manual text body.")

    def test_archive_detail_private_manual_text_404_for_anonymous(self):
        item = create_manual_text_archive_item(
            title="Private detail",
            body="Private secret.",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_family_user_can_view_private_manual_text(self):
        item = create_manual_text_archive_item(
            title="Private family view",
            body="Family readable body.",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Family readable body.")

    def test_staff_can_view_private_manual_text(self):
        item = create_manual_text_archive_item(
            title="Private staff view",
            body="Staff readable body.",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Staff readable body.")

    def test_manual_text_create_form_renders_hebrew_date_precision_labels(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "דיוק תאריך")
        for label in ("ללא תאריך", "שנה בלבד", "חודש", "יום מדויק", "טווח"):
            self.assertContains(resp, label)

    def test_staff_can_create_manual_text_through_ui(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(
                title="Staff manual item",
                body="Created through staff UI.",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Staff manual item")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)
        self.assertEqual(item.manual_text_content.body, "Created through staff UI.")

    def test_anonymous_cannot_create_manual_text(self):
        resp = self.client.post(
            self.CREATE_URL,
            data={"title": "Blocked", "body": "Should not save"},
        )
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(ArchiveItem.objects.filter(title="Blocked").exists())

    def test_non_staff_cannot_create_manual_text(self):
        user = User.objects.create_user(
            username="manual_text_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(
            self.CREATE_URL,
            data={"title": "Blocked user", "body": "Should not save"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ArchiveItem.objects.filter(title="Blocked user").exists())

    def test_family_user_cannot_create_manual_text(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.post(
            self.CREATE_URL,
            data={"title": "Blocked family", "body": "Should not save"},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ArchiveItem.objects.filter(title="Blocked family").exists())

    def test_staff_can_edit_manual_text(self):
        item = create_manual_text_archive_item(
            title="Before edit",
            body="Original body.",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(
                title="After edit",
                body="Updated body.",
                visibility=ArchiveItem.Visibility.PUBLIC,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        item.manual_text_content.refresh_from_db()
        self.assertEqual(item.title, "After edit")
        self.assertEqual(item.manual_text_content.body, "Updated body.")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(item.metadata_status, ArchiveItem.MetadataStatus.COMPLETED)

    def test_anonymous_cannot_edit_manual_text(self):
        item = create_manual_text_archive_item(title="Edit guard", body="x")
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(title="Hacked"),
        )
        self.assertIn(resp.status_code, (302, 403))
        item.refresh_from_db()
        self.assertEqual(item.title, "Edit guard")

    def test_family_user_cannot_edit_manual_text(self):
        item = create_manual_text_archive_item(title="Edit guard family", body="x")
        self.client.force_login(self._create_family_user())
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(title="Hacked family"),
        )
        self.assertEqual(resp.status_code, 403)
        item.refresh_from_db()
        self.assertEqual(item.title, "Edit guard family")

    def test_edit_ocr_document_archive_item_returns_404(self):
        doc = create_ocr_document(
            title="OCR edit guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 404)

    def test_blank_title_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(title="   ", body="Body"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "title is required")
        self.assertFalse(ManualTextContent.objects.filter(body="Body").exists())

    def test_blank_body_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(title="Title", body="   "),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "body is required")

    def test_invalid_visibility_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(visibility="secret"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "visibility is invalid")

    def test_invalid_metadata_status_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(metadata_status="MAYBE"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "metadata_status is invalid")

    def test_invalid_date_precision_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(date_precision="GUESS"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "date_precision is invalid")

    def test_date_end_before_date_start_rejected_on_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(
                date_start="2020-01-02",
                date_end="2020-01-01",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "date_end must not be before date_start")

    @patch("documents.services.sqs.send_process_document_message")
    def test_create_ui_does_not_create_document_or_enqueue(self, mock_enqueue):
        before_docs = Document.objects.count()
        before_results = DocumentTextResult.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(title="Service guard create"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Document.objects.count(), before_docs)
        self.assertEqual(DocumentTextResult.objects.count(), before_results)
        mock_enqueue.assert_not_called()

    @patch("documents.services.sqs.send_process_document_message")
    def test_edit_ui_does_not_create_document_or_enqueue(self, mock_enqueue):
        item = create_manual_text_archive_item(title="Edit service guard", body="x")
        before_docs = Document.objects.count()
        before_results = DocumentTextResult.objects.count()
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(title="Edited service guard", body="y"),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Document.objects.count(), before_docs)
        self.assertEqual(DocumentTextResult.objects.count(), before_results)
        mock_enqueue.assert_not_called()

    def test_manual_text_body_is_escaped_safely(self):
        item = create_manual_text_archive_item(
            title="XSS test",
            body="<script>alert(1)</script>",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(resp, "<script>alert(1)</script>")

    def test_manual_text_body_preserves_line_breaks_safely(self):
        item = create_manual_text_archive_item(
            title="Line breaks",
            body="line one\nline two",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "line one<br>line two")

    def test_update_manual_text_archive_item_service(self):
        item = create_manual_text_archive_item(title="Service edit", body="old")
        update_manual_text_archive_item(
            item,
            title="Service edited",
            body="new body",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        item.refresh_from_db()
        item.manual_text_content.refresh_from_db()
        self.assertEqual(item.title, "Service edited")
        self.assertEqual(item.manual_text_content.body, "new body")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)

    def test_archive_item_admin_remains_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            username="manual_text_archive_admin",
            password="test-pass",
            email="admin2@example.com",
        )
        site = AdminSite()
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, site).has_add_permission(request)
        )
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, site).has_change_permission(request)
        )
        self.assertFalse(
            ArchiveItemAdmin(ArchiveItem, site).has_delete_permission(request)
        )

    def test_manual_text_content_admin_is_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            username="manual_text_content_admin",
            password="test-pass",
            email="admin3@example.com",
        )
        site = AdminSite()
        admin = ManualTextContentAdmin(ManualTextContent, site)
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_delete_permission(request))


class OcrDocumentArchiveItemAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_access_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="ocr_family_user"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def test_public_ocr_document_visible_to_anonymous_via_document_url(self):
        doc = create_ocr_document(
            title="Public OCR doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_private_ocr_document_not_visible_to_anonymous_via_document_url(self):
        doc = create_ocr_document(
            title="Private OCR doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_private_ocr_document_visible_to_family_user(self):
        doc = create_ocr_document(
            title="Private OCR family doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_private_ocr_document_visible_to_staff(self):
        doc = create_ocr_document(
            title="Private OCR staff doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_document_detail_uses_archive_item_visibility_not_document_visibility(self):
        doc = create_ocr_document(
            title="Bridge mismatch doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            visibility=ArchiveItem.Visibility.PRIVATE
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 404)

        self.client.force_login(self._create_family_user())
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)

    def test_archive_detail_blocks_private_ocr_document_for_anonymous(self):
        doc = create_ocr_document(
            title="Private OCR archive route",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        resp = self.client.get(f"/archive/{doc.archive_item_id}/")
        self.assertEqual(resp.status_code, 404)

    def test_archive_detail_redirects_public_ocr_document_for_anonymous(self):
        doc = create_ocr_document(
            title="Public OCR archive route",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{doc.archive_item_id}/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/api/ui/documents/{doc.id}/")


class ArchiveNavigationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="archive_nav_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="archive_nav_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def test_global_nav_shows_archive_link_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("archive-list"))
        self.assertContains(resp, "ארכיון")

    def test_global_nav_hides_manage_link_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")

    def test_global_nav_shows_manage_link_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, reverse("archive-manage-list"))
        self.assertContains(resp, "ניהול ארכיון")

    def test_global_nav_hides_manage_link_for_family_user(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, reverse("archive-list"))
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")

    def test_global_nav_hides_manage_link_for_non_staff_authenticated_user(self):
        user = User.objects.create_user(
            username="archive_nav_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")

    def test_archive_list_page_shows_manage_toolbar_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("archive-manage-list"))
        self.assertContains(resp, "ניהול ארכיון")

    def test_archive_list_page_hides_manage_toolbar_for_anonymous(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")

    def test_archive_manage_list_shows_manual_text_create_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("archive-manage-manual-text-create"))
        self.assertContains(resp, "יצירת טקסט ידני")
