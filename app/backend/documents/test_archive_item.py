import json
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.test import RequestFactory, TestCase, override_settings

from documents.admin import ArchiveItemAdmin, DocumentAdmin
from documents.models import ArchiveItem, Document, DocumentSourceFile, DocumentTextResult
from documents.services.archive_items import (
    archive_item_field_values_from_document,
    create_ocr_document,
)


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
