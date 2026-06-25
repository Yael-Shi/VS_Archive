import datetime
import json
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import Group, User
from django.db import IntegrityError
from django.test import (
    Client,
    RequestFactory,
    SimpleTestCase,
    TestCase,
    override_settings,
)
from django.urls import reverse

from documents.admin import ArchiveItemAdmin, DocumentAdmin, ManualTextContentAdmin
from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    Document,
    DocumentMetadata,
    DocumentSourceFile,
    DocumentTextResult,
    ManualTextContent,
    Tag,
)
from documents.services.archive_items import (
    ARCHIVE_ITEM_SHARED_FIELD_NAMES,
    archive_item_field_values_from_document,
    create_manual_text_archive_item,
    create_ocr_document,
    sync_archive_item_shared_fields_from_document,
    sync_document_shared_fields_from_archive_item,
    update_archive_item_discovery_metadata,
    update_manual_text_archive_item,
    update_ocr_document_catalog_metadata,
    update_ocr_document_metadata,
    update_ocr_document_tags,
)
from documents.services.archive_tags_validation import (
    normalize_tag_names_from_list,
    parse_comma_separated_tag_names,
    parse_ocr_tags_form,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_presentation import (
    archive_item_has_discovery_metadata,
    archive_item_type_label,
    archive_metadata_status_label,
    language_label,
    visibility_label,
)
from documents.services.manual_text_body_display import (
    format_manual_text_body_for_display,
)


def assert_ocr_shared_fields_match(test_case, doc: Document) -> None:
    """Assert all six shared archival fields match between Document and ArchiveItem."""
    item = doc.archive_item
    for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES:
        test_case.assertEqual(
            getattr(doc, name),
            getattr(item, name),
            msg=f"shared field mismatch: {name}",
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
        assert_ocr_shared_fields_match(self, doc)

    def test_create_ocr_document_all_six_shared_fields_match(self):
        date_start = datetime.date(1920, 3, 15)
        date_end = datetime.date(1925, 6, 1)
        doc = create_ocr_document(
            title="All six shared fields",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
            metadata_status=Document.MetadataStatus.COMPLETED,
            date_start=date_start,
            date_end=date_end,
            date_precision=Document.DatePrecision.RANGE,
        )
        item = doc.archive_item
        self.assertEqual(item.title, "All six shared fields")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(item.metadata_status, ArchiveItem.MetadataStatus.COMPLETED)
        self.assertEqual(item.date_start, date_start)
        self.assertEqual(item.date_end, date_end)
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.RANGE)
        assert_ocr_shared_fields_match(self, doc)

    def test_create_ocr_document_omitted_shared_fields_use_archive_item_defaults(self):
        doc = create_ocr_document(
            title="Defaults test",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        item = doc.archive_item
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(
            item.metadata_status, ArchiveItem.MetadataStatus.NEEDS_COMPLETION
        )
        self.assertIsNone(item.date_start)
        self.assertIsNone(item.date_end)
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.UNKNOWN)
        assert_ocr_shared_fields_match(self, doc)

    def test_create_ocr_document_runtime_fields_remain_document_side(self):
        doc = create_ocr_document(
            title="Runtime fields test",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            category_event="family-event",
        )
        item = doc.archive_item
        self.assertEqual(doc.doc_type, Document.DocType.PDF)
        self.assertEqual(doc.language, Document.Language.HEBREW)
        self.assertEqual(doc.text_input_type, Document.TextInputType.HANDWRITTEN)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)
        self.assertEqual(doc.category_event, "family-event")
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        self.assertEqual(item.item_type, ArchiveItem.ItemType.OCR_DOCUMENT)
        self.assertFalse(hasattr(item, "doc_type"))
        self.assertFalse(hasattr(item, "upload_status"))

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

    def test_sync_archive_item_shared_fields_from_document(self):
        doc = create_ocr_document(
            title="Before sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        doc.title = "After sync"
        doc.visibility = Document.Visibility.PUBLIC
        doc.metadata_status = Document.MetadataStatus.COMPLETED
        doc.date_precision = Document.DatePrecision.YEAR
        sync_archive_item_shared_fields_from_document(doc)
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(item.title, "After sync")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(item.metadata_status, ArchiveItem.MetadataStatus.COMPLETED)
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.YEAR)

    def test_sync_document_shared_fields_from_archive_item(self):
        doc = create_ocr_document(
            title="Document before mirror sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        item = doc.archive_item
        item.title = "ArchiveItem after mirror sync"
        item.visibility = ArchiveItem.Visibility.PUBLIC
        item.metadata_status = ArchiveItem.MetadataStatus.COMPLETED
        item.date_precision = ArchiveItem.DatePrecision.YEAR
        item.save(
            update_fields=[
                "title",
                "visibility",
                "metadata_status",
                "date_precision",
                "updated_at",
            ]
        )
        sync_document_shared_fields_from_archive_item(doc)
        doc.refresh_from_db()
        self.assertEqual(doc.title, "ArchiveItem after mirror sync")
        self.assertEqual(doc.visibility, Document.Visibility.PUBLIC)
        self.assertEqual(doc.metadata_status, Document.MetadataStatus.COMPLETED)
        self.assertEqual(doc.date_precision, Document.DatePrecision.YEAR)

    def test_update_ocr_document_metadata_keeps_document_and_archive_item_in_sync(
        self,
    ):
        doc = create_ocr_document(
            title="Service before",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PRIVATE,
        )
        update_ocr_document_metadata(
            doc,
            title="Service after",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.MONTH,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(doc.title, "Service after")
        self.assertEqual(item.title, "Service after")
        self.assertEqual(doc.visibility, Document.Visibility.PUBLIC)
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(doc.date_precision, Document.DatePrecision.MONTH)
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.MONTH)

    def test_update_ocr_document_metadata_writes_archive_item_canonical_when_drifted(
        self,
    ):
        doc = create_ocr_document(
            title="Document drift title",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PRIVATE,
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem drift title",
            visibility=ArchiveItem.Visibility.PUBLIC,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        doc.refresh_from_db()

        update_ocr_document_metadata(
            doc,
            title="Submitted canonical title",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.MONTH,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        for model in (doc, item):
            self.assertEqual(model.title, "Submitted canonical title")
            self.assertEqual(model.visibility, ArchiveItem.Visibility.PRIVATE)
            self.assertEqual(
                model.metadata_status, ArchiveItem.MetadataStatus.NEEDS_COMPLETION
            )
            self.assertEqual(model.date_precision, ArchiveItem.DatePrecision.MONTH)

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

    def test_document_instance_delete_returns_django_delete_tuple(self):
        doc = create_ocr_document(
            title="Document delete return value",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc_id = doc.id
        archive_item_id = doc.archive_item_id

        deleted_count, deleted_by_model = doc.delete()

        self.assertIsInstance(deleted_count, int)
        self.assertIsInstance(deleted_by_model, dict)
        self.assertGreaterEqual(deleted_count, 1)
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
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

    def test_document_admin_shared_mirror_fields_are_readonly(self):
        admin = DocumentAdmin(Document, self.site)
        for field in (
            "title",
            "visibility",
            "metadata_status",
            "date_start",
            "date_end",
            "date_precision",
        ):
            self.assertIn(field, admin.readonly_fields)

    def test_document_admin_post_cannot_change_shared_mirror_fields(self):
        doc = create_ocr_document(
            title="Admin mirror before",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
            file_s3_key="documents/99/original.jpg",
            mime_type="image/jpeg",
        )
        item = doc.archive_item
        before_title = item.title
        before_visibility = item.visibility
        before_metadata_status = item.metadata_status

        client = Client()
        user = self.request.user
        assert isinstance(user, User)
        client.force_login(user)
        change_url = f"/admin/documents/document/{doc.id}/change/"
        get_resp = client.get(change_url)
        self.assertEqual(get_resp.status_code, 200)

        form = get_resp.context["adminform"].form
        post_data = {}
        for name in form.fields:
            value = form[name].value()
            if value is None:
                value = ""
            post_data[name] = value
        post_data["_save"] = "Save"
        post_data["title"] = "Admin POST drift title"
        post_data["visibility"] = Document.Visibility.PUBLIC
        post_data["metadata_status"] = Document.MetadataStatus.COMPLETED

        post_resp = client.post(change_url, post_data, follow=True)
        self.assertEqual(post_resp.status_code, 200)

        item.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(item.title, before_title)
        self.assertEqual(item.visibility, before_visibility)
        self.assertEqual(item.metadata_status, before_metadata_status)
        self.assertEqual(doc.title, before_title)
        self.assertEqual(doc.visibility, before_visibility)
        self.assertEqual(doc.metadata_status, before_metadata_status)


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
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
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
        assert_ocr_shared_fields_match(self, doc)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
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
        assert_ocr_shared_fields_match(self, doc)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_still_enqueues_processing(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(self._base_create_payload()),
            content_type="application/json",
        )
        doc_id = create_resp.json()["document_id"]
        complete_resp = self.client.post(
            f"/api/uploads/{doc_id}/complete/",
            data=json.dumps(
                {"success": True, "file_size": 2048, "file_mime": "image/jpeg"}
            ),
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
        self.assertEqual(
            item.metadata_status, ArchiveItem.MetadataStatus.NEEDS_COMPLETION
        )
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
        self.assertContains(resp, "line one<br />line two", html=True)

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

    def test_create_manual_text_post_saves_author_name_and_source_title(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(
                title="Manual with source metadata",
                body="Body text.",
                author_name="  Ada Lovelace  ",
                source_title=" The Times ",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Manual with source metadata")
        self.assertEqual(item.author_name, "Ada Lovelace")
        self.assertEqual(item.source_title, "The Times")

    def test_edit_manual_text_post_updates_author_name_and_source_title(self):
        item = create_manual_text_archive_item(
            title="Manual source edit",
            body="Body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "Before author"
        item.source_title = "Before source"
        item.save(update_fields=["author_name", "source_title", "updated_at"])
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(
                title="Manual source edit",
                body="Body.",
                author_name="After author",
                source_title="After source",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.author_name, "After author")
        self.assertEqual(item.source_title, "After source")

    def test_edit_manual_text_post_can_clear_author_name_and_source_title(self):
        item = create_manual_text_archive_item(
            title="Manual source clear",
            body="Body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "To clear author"
        item.source_title = "To clear source"
        item.save(update_fields=["author_name", "source_title", "updated_at"])
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(
                title="Manual source clear",
                body="Body.",
                author_name="   ",
                source_title="",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.source_title, "")

    def test_edit_manual_text_get_seeds_author_name_and_source_title(self):
        item = create_manual_text_archive_item(
            title="Manual source seed",
            body="Body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "Seeded author"
        item.source_title = "Seeded source"
        item.save(update_fields=["author_name", "source_title", "updated_at"])
        self.client.force_login(self.staff)
        resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="Seeded author"')
        self.assertContains(resp, 'value="Seeded source"')

    def test_over_255_author_name_rejected_on_manual_text_create(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._valid_create_payload(
                title="Too long author",
                author_name="a" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחבר/ת חייב להיות עד 255 תווים")
        self.assertFalse(ArchiveItem.objects.filter(title="Too long author").exists())

    def test_over_255_source_title_rejected_on_manual_text_edit(self):
        item = create_manual_text_archive_item(title="Too long source", body="Body.")
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._valid_create_payload(
                title="Too long source",
                body="Body.",
                source_title="s" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מקור חייב להיות עד 255 תווים")
        item.refresh_from_db()
        self.assertEqual(item.source_title, "")

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


class OcrDocumentMetadataEditTests(TestCase):
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="ocr_edit_family_user"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _valid_metadata_payload(self, **overrides):
        payload = {
            "title": "Updated OCR title",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
            "date_precision": ArchiveItem.DatePrecision.YEAR,
        }
        payload.update(overrides)
        return payload

    def test_staff_can_get_ocr_metadata_edit_form(self):
        doc = create_ocr_document(
            title="OCR edit form",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "OCR edit form")
        self.assertContains(resp, "עריכת מטא־דאטה")
        self.assertNotContains(resp, 'name="body"')

    def test_staff_can_post_valid_ocr_metadata_edits(self):
        doc = create_ocr_document(
            title="Before OCR edit",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="After OCR edit",
                date_start="1940-01-01",
                date_end="1945-12-31",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"],
            reverse("documents-detail-page", kwargs={"doc_id": doc.id}),
        )

    def test_shared_fields_updated_on_document_and_archive_item(self):
        doc = create_ocr_document(
            title="Shared fields before",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PRIVATE,
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Shared fields after",
                visibility=ArchiveItem.Visibility.PUBLIC,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                date_precision=ArchiveItem.DatePrecision.RANGE,
                date_start="1920-03-01",
                date_end="1921-06-30",
            ),
        )
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        for model in (doc, item):
            self.assertEqual(model.title, "Shared fields after")
            self.assertEqual(model.visibility, ArchiveItem.Visibility.PUBLIC)
            self.assertEqual(
                model.metadata_status, ArchiveItem.MetadataStatus.COMPLETED
            )
            self.assertEqual(model.date_precision, ArchiveItem.DatePrecision.RANGE)
            self.assertEqual(str(model.date_start), "1920-03-01")
            self.assertEqual(str(model.date_end), "1921-06-30")

    def test_post_writes_archive_item_canonical_and_mirrors_document_when_drifted(
        self,
    ):
        doc = create_ocr_document(
            title="Document drift POST",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
            category_event="Before event",
        )
        doc.tags_m2m.add(Tag.objects.create(name="before-tag"))
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem drift POST",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data={
                **self._valid_metadata_payload(
                    title="POST canonical title",
                    visibility=ArchiveItem.Visibility.PRIVATE,
                    metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                    date_precision=ArchiveItem.DatePrecision.YEAR,
                    date_start="1930-01-01",
                    date_end="1935-12-31",
                ),
                "donor": "POST donor",
                "collection": "POST collection",
                "original_location": "POST location",
                "notes": "POST notes",
                "category_event": "After event",
                "tags": "after-tag",
            },
        )
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        for model in (doc, item):
            self.assertEqual(model.title, "POST canonical title")
            self.assertEqual(model.visibility, ArchiveItem.Visibility.PRIVATE)
            self.assertEqual(
                model.metadata_status, ArchiveItem.MetadataStatus.COMPLETED
            )
            self.assertEqual(model.date_precision, ArchiveItem.DatePrecision.YEAR)
            self.assertEqual(str(model.date_start), "1930-01-01")
            self.assertEqual(str(model.date_end), "1935-12-31")
        admin_meta = doc.admin_meta
        self.assertEqual(admin_meta.donor, "POST donor")
        self.assertEqual(admin_meta.collection, "POST collection")
        self.assertEqual(admin_meta.original_location, "POST location")
        self.assertEqual(admin_meta.notes, "POST notes")
        self.assertEqual(doc.category_event, "Before event")
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["before-tag"],
        )

    def test_visibility_change_affects_archive_access(self):
        doc = create_ocr_document(
            title="Visibility toggle OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        detail_url = f"/api/ui/documents/{doc.id}/"
        self.assertEqual(self.client.get(detail_url).status_code, 404)

        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Visibility toggle OCR",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
        )
        self.client.logout()
        self.assertEqual(self.client.get(detail_url).status_code, 200)

        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Visibility toggle OCR",
                visibility=ArchiveItem.Visibility.PRIVATE,
            ),
        )
        self.client.logout()
        self.assertEqual(self.client.get(detail_url).status_code, 404)

        self.client.force_login(self._create_family_user())
        self.assertEqual(self.client.get(detail_url).status_code, 200)

    def test_anonymous_cannot_edit_ocr_metadata(self):
        doc = create_ocr_document(
            title="Anonymous OCR guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="Hacked"),
        )
        self.assertIn(resp.status_code, (302, 403))
        doc.refresh_from_db()
        self.assertEqual(doc.title, "Anonymous OCR guard")

    def test_family_user_cannot_edit_ocr_metadata(self):
        doc = create_ocr_document(
            title="Family OCR guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="Hacked family"),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertEqual(doc.title, "Family OCR guard")

    def test_family_user_cannot_get_ocr_metadata_edit_form(self):
        doc = create_ocr_document(
            title="Family GET guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 403)

    def test_shared_validation_error_blocks_catalog(self):
        doc = create_ocr_document(
            title="Before shared error",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Before event",
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem before shared error",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        doc.tags_m2m.add(Tag.objects.create(name="keep-tag"))
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data={
                **self._valid_metadata_payload(
                    title="After shared error",
                    date_start="not-a-date",
                ),
                "donor": "New donor",
                "collection": "",
                "original_location": "",
                "notes": "",
                "category_event": "After event",
                "tags": "new-tag",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "invalid date_start format")
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(doc.title, "Before shared error")
        self.assertEqual(item.title, "ArchiveItem before shared error")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PUBLIC)
        self.assertEqual(doc.category_event, "Before event")
        self.assertFalse(
            DocumentMetadata.objects.filter(document=doc, donor="New donor").exists()
        )
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["keep-tag"],
        )

    def test_non_staff_cannot_edit_ocr_metadata(self):
        doc = create_ocr_document(
            title="User OCR guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        user = User.objects.create_user(
            username="ocr_edit_non_staff",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="Hacked user"),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertEqual(doc.title, "User OCR guard")

    def test_invalid_date_rejected_on_ocr_edit(self):
        doc = create_ocr_document(
            title="Invalid date OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(date_start="not-a-date"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "invalid date_start format")

    def test_invalid_date_precision_rejected_on_ocr_edit(self):
        doc = create_ocr_document(
            title="Invalid precision OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(date_precision="GUESS"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "date_precision is invalid")

    def test_edit_does_not_change_document_text_results(self):
        doc = create_ocr_document(
            title="OCR result guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        result = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="test-engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="unchanged transcript",
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="OCR result guard edited"),
        )
        result.refresh_from_db()
        self.assertEqual(result.text, "unchanged transcript")
        self.assertEqual(result.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(result.engine, "test-engine")

    def test_edit_does_not_change_ocr_processing_fields(self):
        doc = create_ocr_document(
            title="Processing guard",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="uploads/guard.pdf",
            file_original_name="guard.pdf",
            mime_type="application/pdf",
        )
        before = {
            "doc_type": doc.doc_type,
            "text_input_type": doc.text_input_type,
            "language": doc.language,
            "upload_status": doc.upload_status,
            "file_s3_key": doc.file_s3_key,
            "file_original_name": doc.file_original_name,
            "mime_type": doc.mime_type,
        }
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="Processing guard edited"),
        )
        doc.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(doc, field), value)

    def test_missing_linked_document_returns_404(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
            title="Orphan OCR item",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 404)

    def test_edit_link_appears_on_document_detail_for_staff(self):
        doc = create_ocr_document(
            title="Detail edit link OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": doc.archive_item_id}),
        )
        self.assertContains(resp, "עריכת מטא־דאטה")

    def test_edit_action_appears_for_ocr_document_on_manage_list(self):
        doc = create_ocr_document(
            title="OCR manage edit link",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": doc.archive_item_id}),
        )
        self.assertContains(resp, "עריכת מטא־דאטה")

    def test_manual_text_manage_list_row_labels_unchanged(self):
        item = create_manual_text_archive_item(title="Manage list manual", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(
            resp, reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )
        html = resp.content.decode()
        edit_href = reverse("archive-manage-edit", kwargs={"item_id": item.id})
        self.assertEqual(self._link_label_for_href(html, edit_href), "עריכה")

    def test_edit_ocr_post_updates_author_name_and_source_title_on_archive_item(self):
        doc = create_ocr_document(
            title="OCR source edit",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="OCR source edit",
                author_name="  יוסף לוי  ",
                source_title=" דבר ",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(item.author_name, "יוסף לוי")
        self.assertEqual(item.source_title, "דבר")

    def test_edit_ocr_post_can_clear_author_name_and_source_title(self):
        doc = create_ocr_document(
            title="OCR source clear",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            author_name="Clear me author",
            source_title="Clear me source",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="OCR source clear",
                author_name="   ",
                source_title="",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.source_title, "")

    def test_edit_ocr_get_seeds_author_name_and_source_title(self):
        doc = create_ocr_document(
            title="OCR source seed",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            author_name="Seeded OCR author",
            source_title="Seeded OCR source",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="Seeded OCR author"')
        self.assertContains(resp, 'value="Seeded OCR source"')

    def test_over_255_author_name_rejected_on_ocr_edit(self):
        doc = create_ocr_document(
            title="OCR long author",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="OCR long author",
                author_name="a" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחבר/ת חייב להיות עד 255 תווים")
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.author_name, "")

    def test_over_255_source_title_rejected_on_ocr_edit(self):
        doc = create_ocr_document(
            title="OCR long source",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            source_title="Keep this source",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="OCR long source",
                source_title="s" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מקור חייב להיות עד 255 תווים")
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.source_title, "Keep this source")

    def _link_label_for_href(self, html: str, href: str) -> str:
        marker = f'href="{href}"'
        href_pos = html.find(marker)
        self.assertNotEqual(href_pos, -1, f"missing link href={href!r}")
        tag_end = html.find(">", href_pos)
        close_start = html.find("</a>", tag_end)
        self.assertNotEqual(close_start, -1)
        return html[tag_end + 1 : close_start].strip()


class OcrDocumentCatalogMetadataEditTests(TestCase):
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_catalog_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="ocr_catalog_edit_family_user"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _valid_metadata_payload(self, **overrides):
        payload = {
            "title": "Catalog OCR title",
            "visibility": ArchiveItem.Visibility.PRIVATE,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "donor": "",
            "collection": "",
            "original_location": "",
            "notes": "",
            "categories": "",
            "events": "",
            "discovery_tags": "",
        }
        payload.update(overrides)
        return payload

    def _valid_catalog_payload(self, **overrides):
        payload = {
            "donor": "יעל שיפמן",
            "collection": "ארכיון משפחתי",
            "original_location": "ירושלים",
            "notes": "הערה פנימית",
        }
        payload.update(overrides)
        return payload

    def test_staff_get_ocr_edit_form_shows_catalog_fields(self):
        doc = create_ocr_document(
            title="Catalog form fields",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="בר מצווה",
        )
        DocumentMetadata.objects.create(
            document=doc,
            donor="Donor A",
            collection="Collection B",
            original_location="Tel Aviv",
            notes="Some notes",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="donor"')
        self.assertContains(resp, 'name="collection"')
        self.assertContains(resp, 'name="original_location"')
        self.assertContains(resp, 'name="notes"')
        self.assertNotContains(resp, 'name="category_event"')
        self.assertContains(resp, 'name="categories"')
        self.assertContains(resp, 'name="events"')
        self.assertContains(resp, 'name="discovery_tags"')
        self.assertContains(resp, "Donor A")
        self.assertContains(resp, "Collection B")
        self.assertContains(resp, "Tel Aviv")
        self.assertContains(resp, "Some notes")

    def test_staff_post_saves_catalog_metadata(self):
        doc = create_ocr_document(
            title="Catalog save",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Catalog save",
                **self._valid_catalog_payload(),
            ),
        )
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        admin_meta = doc.admin_meta
        self.assertEqual(admin_meta.donor, "יעל שיפמן")
        self.assertEqual(admin_meta.collection, "ארכיון משפחתי")
        self.assertEqual(admin_meta.original_location, "ירושלים")
        self.assertEqual(admin_meta.notes, "הערה פנימית")
        self.assertIsNone(doc.category_event)

    def test_post_creates_document_metadata_when_missing(self):
        doc = create_ocr_document(
            title="Missing admin meta",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self.assertFalse(DocumentMetadata.objects.filter(document=doc).exists())
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Missing admin meta",
                donor="New donor",
            ),
        )
        doc.refresh_from_db()
        self.assertTrue(DocumentMetadata.objects.filter(document=doc).exists())
        self.assertEqual(doc.admin_meta.donor, "New donor")

    def test_post_updates_existing_document_metadata(self):
        doc = create_ocr_document(
            title="Existing admin meta",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Before event",
        )
        DocumentMetadata.objects.create(
            document=doc,
            donor="Old donor",
            collection="Old collection",
            original_location="Old location",
            notes="Old notes",
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Existing admin meta",
                **self._valid_catalog_payload(),
            ),
        )
        doc.refresh_from_db()
        self.assertEqual(DocumentMetadata.objects.filter(document=doc).count(), 1)
        self.assertEqual(doc.admin_meta.donor, "יעל שיפמן")
        self.assertEqual(doc.admin_meta.collection, "ארכיון משפחתי")
        self.assertEqual(doc.admin_meta.original_location, "ירושלים")
        self.assertEqual(doc.admin_meta.notes, "הערה פנימית")
        self.assertEqual(doc.category_event, "Before event")

    def test_clearing_catalog_fields_persists_empty_values(self):
        doc = create_ocr_document(
            title="Clear catalog",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Event to clear",
        )
        DocumentMetadata.objects.create(
            document=doc,
            donor="Donor",
            collection="Collection",
            original_location="Location",
            notes="Notes",
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(title="Clear catalog"),
        )
        doc.refresh_from_db()
        self.assertEqual(doc.admin_meta.donor, "")
        self.assertEqual(doc.admin_meta.collection, "")
        self.assertEqual(doc.admin_meta.original_location, "")
        self.assertEqual(doc.admin_meta.notes, "")
        self.assertEqual(doc.category_event, "Event to clear")

    def test_donor_max_length_rejected(self):
        doc = create_ocr_document(
            title="Donor length",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Donor length",
                donor="x" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תורם/ת חייב להיות עד 255 תווים")

    def test_collection_max_length_rejected(self):
        doc = create_ocr_document(
            title="Collection length",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Collection length",
                collection="x" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אוסף חייב להיות עד 255 תווים")

    def test_original_location_max_length_rejected(self):
        doc = create_ocr_document(
            title="Location length",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Location length",
                original_location="x" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מיקום מקורי חייב להיות עד 255 תווים")

    def test_ocr_edit_get_hides_legacy_category_event_field(self):
        doc = create_ocr_document(
            title="Hide category event",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Legacy event value",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="category_event"')
        self.assertNotContains(resp, "אירוע / קטגוריה")

    def test_catalog_validation_error_blocks_shared_and_discovery(self):
        doc = create_ocr_document(
            title="Before catalog error",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem before catalog error",
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        doc.tags_m2m.add(Tag.objects.create(name="keep-tag"))
        item = doc.archive_item
        cat = ArchiveCategory.objects.create(name="Keep cat", slug="keep-cat")
        item.categories.add(cat)
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="After catalog error",
                donor="x" * 256,
                categories="New cat",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תורם/ת חייב להיות עד 255 תווים")
        doc.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(doc.title, "Before catalog error")
        self.assertEqual(item.title, "ArchiveItem before catalog error")
        self.assertEqual(item.metadata_status, ArchiveItem.MetadataStatus.COMPLETED)
        self.assertFalse(DocumentMetadata.objects.filter(document=doc).exists())
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["keep-tag"],
        )
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["Keep cat"],
        )

    def test_anonymous_cannot_edit_catalog_metadata(self):
        doc = create_ocr_document(
            title="Anonymous catalog guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked",
                **self._valid_catalog_payload(),
            ),
        )
        self.assertIn(resp.status_code, (302, 403))
        doc.refresh_from_db()
        self.assertFalse(DocumentMetadata.objects.filter(document=doc).exists())
        self.assertIsNone(doc.category_event)

    def test_family_user_cannot_edit_catalog_metadata(self):
        doc = create_ocr_document(
            title="Family catalog guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked family",
                **self._valid_catalog_payload(),
            ),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(DocumentMetadata.objects.filter(document=doc).exists())

    def test_non_staff_cannot_edit_catalog_metadata(self):
        doc = create_ocr_document(
            title="User catalog guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        user = User.objects.create_user(
            username="ocr_catalog_edit_non_staff",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked user",
                **self._valid_catalog_payload(),
            ),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(DocumentMetadata.objects.filter(document=doc).exists())

    def test_catalog_edit_does_not_change_archive_item_shared_fields_when_unchanged(
        self,
    ):
        doc = create_ocr_document(
            title="Shared unchanged",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        item = doc.archive_item
        before = archive_item_field_values_from_document(doc)
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Shared unchanged",
                **self._valid_catalog_payload(),
            ),
        )
        item.refresh_from_db()
        after = archive_item_field_values_from_document(doc)
        self.assertEqual(before, after)

    def test_catalog_edit_does_not_change_document_text_results(self):
        doc = create_ocr_document(
            title="Catalog result guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        result = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="test-engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="unchanged transcript",
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Catalog result guard",
                **self._valid_catalog_payload(),
            ),
        )
        result.refresh_from_db()
        self.assertEqual(result.text, "unchanged transcript")
        self.assertEqual(result.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(result.engine, "test-engine")

    def test_catalog_edit_does_not_change_ocr_processing_fields(self):
        doc = create_ocr_document(
            title="Catalog processing guard",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="uploads/catalog-guard.pdf",
            file_original_name="catalog-guard.pdf",
            mime_type="application/pdf",
        )
        before = {
            "doc_type": doc.doc_type,
            "text_input_type": doc.text_input_type,
            "language": doc.language,
            "upload_status": doc.upload_status,
            "file_s3_key": doc.file_s3_key,
            "file_original_name": doc.file_original_name,
            "mime_type": doc.mime_type,
        }
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Catalog processing guard",
                **self._valid_catalog_payload(),
            ),
        )
        doc.refresh_from_db()
        for field, value in before.items():
            self.assertEqual(getattr(doc, field), value)

    def test_update_ocr_document_catalog_metadata_service_upserts(self):
        doc = create_ocr_document(
            title="Service upsert",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        update_ocr_document_catalog_metadata(
            doc,
            donor="Service donor",
            collection="Service collection",
            original_location="Service location",
            notes="Service notes",
            category_event="Service event",
        )
        doc.refresh_from_db()
        self.assertEqual(doc.category_event, "Service event")
        self.assertEqual(doc.admin_meta.donor, "Service donor")


class ArchiveTagsValidationTests(SimpleTestCase):
    def test_parse_comma_separated_basic(self):
        self.assertEqual(parse_comma_separated_tag_names("a, b"), ["a", "b"])

    def test_parse_comma_separated_trims_and_drops_empty(self):
        self.assertEqual(
            parse_comma_separated_tag_names("  a  , , b ,  "),
            ["a", "b"],
        )

    def test_parse_comma_separated_dedupes_preserving_order(self):
        self.assertEqual(
            parse_comma_separated_tag_names("b, a, b, c, a"),
            ["b", "a", "c"],
        )

    def test_parse_comma_separated_preserves_casing(self):
        self.assertEqual(
            parse_comma_separated_tag_names("Family, family"),
            ["Family", "family"],
        )

    def test_parse_ocr_tags_form_rejects_max_length(self):
        long_tag = "x" * 65
        _, errors = parse_ocr_tags_form({"tags": f"ok, {long_tag}"})
        self.assertEqual(errors, ["תגית חייבת להיות עד 64 תווים"])

    def test_normalize_tag_names_from_list_skips_none_and_empty(self):
        self.assertEqual(
            normalize_tag_names_from_list([None, "", "  ", "a"]),
            ["a"],
        )

    def test_normalize_tag_names_from_list_strips_whitespace(self):
        self.assertEqual(
            normalize_tag_names_from_list(["  a  ", " b "]),
            ["a", "b"],
        )

    def test_normalize_tag_names_from_list_dedupes_preserving_order(self):
        self.assertEqual(
            normalize_tag_names_from_list(["b", "a", "b", "c", "a"]),
            ["b", "a", "c"],
        )

    def test_normalize_tag_names_from_list_preserves_casing(self):
        self.assertEqual(
            normalize_tag_names_from_list(["Family", "family"]),
            ["Family", "family"],
        )


class OcrDocumentTagsEditTests(TestCase):
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_tags_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="ocr_tags_edit_family_user"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _valid_metadata_payload(self, **overrides):
        payload = {
            "title": "Tags OCR title",
            "visibility": ArchiveItem.Visibility.PRIVATE,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "donor": "",
            "collection": "",
            "original_location": "",
            "notes": "",
            "categories": "",
            "events": "",
            "discovery_tags": "",
        }
        payload.update(overrides)
        return payload

    def test_staff_get_ocr_edit_form_hides_legacy_tags_field(self):
        doc = create_ocr_document(
            title="Tags prefill",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        family_tag = Tag.objects.create(name="משפחה")
        jerusalem_tag = Tag.objects.create(name="ירושלים")
        doc.tags_m2m.add(family_tag, jerusalem_tag)
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="tags"')
        self.assertNotContains(resp, 'name="tags"')
        self.assertContains(resp, 'name="discovery_tags"')

    def test_staff_post_ignores_legacy_tags_field(self):
        doc = create_ocr_document(
            title="Tags save",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(Tag.objects.create(name="keep-me"))
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Tags save",
                tags="משפחה, 1948",
                discovery_tags="item-tag",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["keep-me"],
        )
        self.assertEqual(
            list(item.tags.values_list("name", flat=True)),
            ["item-tag"],
        )

    def test_anonymous_cannot_edit_tags(self):
        doc = create_ocr_document(
            title="Anonymous tags guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked",
                tags="hacked",
            ),
        )
        self.assertIn(resp.status_code, (302, 403))
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_family_user_cannot_edit_tags(self):
        doc = create_ocr_document(
            title="Family tags guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked family",
                tags="hacked",
            ),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_non_staff_cannot_edit_tags(self):
        doc = create_ocr_document(
            title="User tags guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        user = User.objects.create_user(
            username="ocr_tags_edit_non_staff",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._valid_metadata_payload(
                title="Hacked user",
                tags="hacked",
            ),
        )
        self.assertEqual(resp.status_code, 403)
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_update_ocr_document_tags_service_sets_and_reuses_tags(self):
        doc = create_ocr_document(
            title="Service tags",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        existing = Tag.objects.create(name="existing")
        update_ocr_document_tags(doc, tag_names=["existing", "new-tag"])
        doc.refresh_from_db()
        self.assertEqual(
            set(doc.tags_m2m.values_list("name", flat=True)),
            {"existing", "new-tag"},
        )
        self.assertEqual(Tag.objects.filter(name="existing").count(), 1)
        self.assertEqual(existing.pk, Tag.objects.get(name="existing").pk)

    def test_update_ocr_document_tags_service_replace_all(self):
        doc = create_ocr_document(
            title="Service replace",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        old_tag = Tag.objects.create(name="old")
        doc.tags_m2m.add(old_tag)
        update_ocr_document_tags(doc, tag_names=["fresh"])
        doc.refresh_from_db()
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["fresh"],
        )
        self.assertTrue(Tag.objects.filter(name="old").exists())

    def test_update_ocr_document_tags_service_clears_tags(self):
        doc = create_ocr_document(
            title="Service clear",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(Tag.objects.create(name="gone"))
        update_ocr_document_tags(doc, tag_names=[])
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_update_ocr_document_tags_service_rejects_non_ocr_item(self):
        doc = create_ocr_document(
            title="Service guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        item = doc.archive_item
        item.item_type = ArchiveItem.ItemType.MANUAL_TEXT
        item.save(update_fields=["item_type"])
        with self.assertRaises(ValueError):
            update_ocr_document_tags(doc, tag_names=["a"])


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
    NEW_ITEM_URL = "/archive/manage/new/"

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

    def test_global_nav_does_not_show_documents_link(self):
        resp = self.client.get(reverse("public-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'href="/api/ui/documents/">מסמכים')

    def test_global_nav_shows_create_archive_item_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, reverse("archive-manage-new"))
        self.assertContains(resp, "יצירת פריט חדש")

    def test_global_nav_hides_create_archive_item_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-new"))
        self.assertNotContains(resp, "יצירת פריט חדש")

    def test_global_nav_hides_create_archive_item_for_family_user(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-new"))
        self.assertNotContains(resp, "יצירת פריט חדש")

    def test_global_nav_hides_create_archive_item_for_non_staff_authenticated_user(
        self,
    ):
        user = User.objects.create_user(
            username="archive_nav_non_staff",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-new"))
        self.assertNotContains(resp, "יצירת פריט חדש")

    def test_global_nav_shows_archive_link_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("archive-list"))
        self.assertContains(resp, "ארכיון")

    def test_global_nav_hides_manage_link_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")
        self.assertNotContains(resp, "nav-staff-panel")

    def test_global_nav_shows_manage_link_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, reverse("archive-manage-list"))
        self.assertContains(resp, "ניהול ארכיון")
        self.assertContains(resp, 'class="card nav-shell nav-staff-panel"')
        self.assertContains(resp, 'class="nav-staff-header section-title">ניהול</div>')

    def test_global_nav_hides_manage_link_for_family_user(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, reverse("archive-list"))
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")
        self.assertNotContains(resp, "nav-staff-panel")

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
        self.assertNotContains(resp, "nav-staff-panel")

    def test_global_nav_shows_django_admin_link_for_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("public-home"))
        self.assertContains(resp, 'href="/admin/"')
        self.assertContains(resp, "ניהול מערכת")

    def test_global_nav_hides_django_admin_link_for_anonymous(self):
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, 'href="/admin/"')
        self.assertNotContains(resp, "ניהול מערכת")

    def test_global_nav_hides_django_admin_link_for_family_user(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, 'href="/admin/"')
        self.assertNotContains(resp, "ניהול מערכת")

    def test_global_nav_hides_django_admin_link_for_non_staff_authenticated_user(self):
        user = User.objects.create_user(
            username="archive_nav_django_admin_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(reverse("public-home"))
        self.assertNotContains(resp, 'href="/admin/"')
        self.assertNotContains(resp, "ניהול מערכת")

    def test_archive_list_page_shows_manage_link_in_staff_nav_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertContains(resp, "nav-staff-panel")
        self.assertContains(resp, reverse("archive-manage-list"))
        self.assertContains(resp, "ניהול ארכיון")
        filters = html[
            html.index("page-toolbar--archive-filters") : html.index(
                '<div class="spacer"></div>'
            )
        ]
        self.assertNotIn(reverse("archive-manage-list"), filters)

    def test_archive_list_page_hides_manage_toolbar_for_anonymous(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("archive-manage-list"))
        self.assertNotContains(resp, "ניהול ארכיון")

    def test_archive_manage_list_shows_create_entrypoint_in_staff_nav_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertContains(resp, "nav-staff-panel")
        self.assertContains(resp, reverse("archive-manage-new"))
        self.assertContains(resp, "יצירת פריט חדש")
        self.assertEqual(html.count(reverse("archive-manage-new")), 1)
        self.assertNotContains(resp, reverse("archive-manage-manual-text-create"))
        self.assertNotContains(resp, "יצירת טקסט מוקלד")


class UnifiedArchiveItemCreatePageTests(TestCase):
    NEW_URL = "/archive/manage/new/"
    MANUAL_TEXT_CREATE_URL = "/archive/manage/new/manual-text/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="unified_create_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="unified_create_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _valid_create_payload(self, **overrides):
        payload = {
            "item_type": "manual_text",
            "title": "Unified manual text",
            "body": "Typed through unified page.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return payload

    def test_staff_can_access_unified_create_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יצירת פריט חדש")

    def test_anonymous_cannot_access_unified_create_page(self):
        resp = self.client.get(self.NEW_URL)
        self.assertIn(resp.status_code, (302, 403))

    def test_non_staff_cannot_access_unified_create_page(self):
        user = User.objects.create_user(
            username="unified_create_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(self.NEW_URL)
        self.assertEqual(resp.status_code, 403)

    def test_family_user_cannot_access_unified_create_page(self):
        self.client.force_login(self._create_family_user())
        resp = self.client.get(self.NEW_URL)
        self.assertEqual(resp.status_code, 403)

    def test_unified_page_contains_item_type_select(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL)
        self.assertContains(resp, 'name="item_type"')
        self.assertContains(resp, 'value="manual_text"')
        self.assertContains(resp, "טקסט")
        self.assertContains(resp, 'value="ocr_document"')
        self.assertContains(resp, "מסמך")
        self.assertNotContains(resp, "OCR document")

    def test_unified_page_shows_manual_text_form_when_type_selected(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "manual_text"})
        self.assertContains(resp, 'name="title"')
        self.assertContains(resp, 'name="body"')
        self.assertContains(resp, 'name="metadata_status"')
        self.assertContains(resp, 'name="date_precision"')

    def test_staff_can_create_manual_text_through_unified_page(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.NEW_URL, data=self._valid_create_payload())
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Unified manual text")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)
        self.assertEqual(item.manual_text_content.body, "Typed through unified page.")

    def test_unified_page_shows_validation_errors_for_manual_text(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.NEW_URL,
            data=self._valid_create_payload(title="   ", body="Body"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "title is required")
        self.assertContains(resp, 'name="body"')

    def test_ocr_document_branch_renders_upload_form(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="uploadForm"')
        self.assertContains(resp, "העלאת מסמך סרוק או PDF לעיבוד טקסט")
        self.assertContains(resp, 'value="IMAGE"')
        self.assertContains(resp, "תמונה")
        self.assertNotContains(resp, ">Image<")
        self.assertNotContains(resp, "OCR/HTR")

    def test_ocr_document_branch_includes_key_upload_fields(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        for needle in (
            'name="file" type="file" multiple',
            'id="title"',
            'id="doc_type"',
            'id="text_input_type"',
        ):
            self.assertContains(resp, needle)

    def test_ocr_document_branch_hides_legacy_discovery_fields(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="category_event"')
        self.assertNotContains(resp, 'id="category_event"')
        self.assertNotContains(resp, 'name="tags"')

    def test_ocr_document_branch_renders_archive_item_discovery_metadata_fields(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריות, אירועים ותגיות")
        for needle in (
            'id="categories"',
            'id="events"',
            'id="discovery_tags"',
            'name="categories"',
            'name="events"',
            'name="discovery_tags"',
        ):
            self.assertContains(resp, needle)

    def test_ocr_document_branch_renders_csrf_token_in_upload_form(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="csrfmiddlewaretoken"')
        form_start = resp.content.index(b'id="uploadForm"')
        csrf_pos = resp.content.index(b'name="csrfmiddlewaretoken"', form_start)
        nav_pos = resp.content.index(b"nav-shell")
        self.assertGreater(csrf_pos, form_start)
        self.assertGreater(csrf_pos, nav_pos)

    def test_ocr_document_branch_js_references_upload_endpoints(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/api/uploads/create/")
        self.assertContains(resp, "/complete/")
        self.assertContains(resp, "/parts/")
        self.assertContains(resp, "/finalize/")
        self.assertContains(resp, "X-CSRFToken")
        self.assertContains(resp, "getCsrfToken")

    def test_ocr_document_branch_keeps_secondary_upload_page_link(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.NEW_URL, {"item_type": "ocr_document"})
        self.assertContains(resp, reverse("upload-page"))
        self.assertContains(resp, "פתיחה בדף העלאה נפרד")

    def test_existing_manual_text_route_still_works(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.MANUAL_TEXT_CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יצירת טקסט מוקלד")
        self.assertContains(resp, 'name="body"')


class ArchiveItemSourceMetadataTests(TestCase):
    def test_archive_item_stores_author_name_and_source_title(self):
        item = create_manual_text_archive_item(
            title="Source metadata storage",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "Ada Lovelace"
        item.source_title = "The Times"
        item.save(update_fields=["author_name", "source_title", "updated_at"])
        item.refresh_from_db()
        self.assertEqual(item.author_name, "Ada Lovelace")
        self.assertEqual(item.source_title, "The Times")

    def test_manual_text_detail_shows_source_metadata_when_present(self):
        item = create_manual_text_archive_item(
            title="Manual with source metadata",
            body="Typed body text.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "רחל כהן"
        item.source_title = "הארץ"
        item.save(update_fields=["author_name", "source_title", "updated_at"])

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחבר/ת")
        self.assertContains(resp, "רחל כהן")
        self.assertContains(resp, "מקור:")
        self.assertContains(resp, "הארץ")
        self.assertContains(resp, "Typed body text.")

    def test_manual_text_detail_hides_empty_source_metadata_labels(self):
        item = create_manual_text_archive_item(
            title="Manual without source metadata",
            body="Only body text.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Only body text.")
        self.assertNotContains(resp, "מחבר/ת")
        self.assertNotContains(resp, "מקור:")

    def test_ocr_document_detail_shows_source_metadata_when_present(self):
        doc = create_ocr_document(
            title="OCR with source metadata",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            author_name="יוסף לוי",
            source_title="דבר",
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחבר/ת")
        self.assertContains(resp, "יוסף לוי")
        self.assertContains(resp, "מקור:")
        self.assertContains(resp, "דבר")
        self.assertContains(resp, "OCR with source metadata")

    def test_ocr_document_detail_hides_empty_source_metadata_labels(self):
        doc = create_ocr_document(
            title="OCR without source metadata",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "OCR without source metadata")
        self.assertNotContains(resp, "מחבר/ת")
        self.assertNotContains(resp, "מקור:")

    def test_ocr_document_detail_hides_legacy_category_event_for_public_viewer(self):
        doc = create_ocr_document(
            title="Legacy category event hidden",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            category_event="חתונה",
        )

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "document-catalog-meta")
        self.assertNotContains(resp, "אירוע / קטגוריה")
        self.assertNotContains(resp, "חתונה")
        self.assertNotContains(resp, "מטא־דאטה למנהלים")

    def test_ocr_document_detail_hides_legacy_tags_for_staff_without_public_display(
        self,
    ):
        staff = User.objects.create_user(
            username="catalog_detail_staff",
            password="test-pass",
            is_staff=True,
        )
        doc = create_ocr_document(
            title="Staff legacy tags hidden",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            category_event="בר מצווה",
        )
        doc.tags_m2m.add(Tag.objects.create(name="ירושלים"))

        self.client.force_login(staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "document-catalog-meta")
        self.assertNotContains(resp, "אירוע / קטגוריה")
        self.assertNotContains(resp, "בר מצווה")
        self.assertNotContains(resp, "ירושלים")
        self.assertContains(resp, "מטא־דאטה למנהלים")

    def test_existing_manual_text_detail_behavior_unchanged_without_source_metadata(
        self,
    ):
        item = create_manual_text_archive_item(
            title="Regression manual detail",
            body="Regression body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Regression manual detail")
        self.assertContains(resp, "Regression body.")
        self.assertContains(resp, "טקסט")

    def test_existing_ocr_document_detail_behavior_unchanged_without_source_metadata(
        self,
    ):
        doc = create_ocr_document(
            title="Regression OCR detail",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{doc.archive_item_id}/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/api/ui/documents/{doc.id}/")

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Regression OCR detail")
        self.assertContains(resp, "טקסט שחולץ")


class ArchiveItemDiscoveryMetadataDisplayTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="discovery_display_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="discovery_display_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _attach_discovery_metadata(self, item: ArchiveItem) -> None:
        category, _ = ArchiveCategory.objects.get_or_create(
            slug="yehudit-mitzraim",
            defaults={"name": "יהדות מצרים"},
        )
        event, _ = ArchiveEvent.objects.get_or_create(
            slug="khatunat-mishpacha",
            defaults={"name": "חתונת משפחה"},
        )
        tag, _ = Tag.objects.get_or_create(name="משפחה")
        item.categories.add(category)
        item.events.add(event)
        item.tags.add(tag)

    def test_manual_text_detail_shows_discovery_metadata_for_public_item(self):
        item = create_manual_text_archive_item(
            title="Manual discovery display",
            body="Typed discovery body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self._attach_discovery_metadata(item)

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "קטגוריות")
        self.assertContains(resp, "יהדות מצרים")
        self.assertContains(resp, "אירועים")
        self.assertContains(resp, "חתונת משפחה")
        self.assertContains(resp, "תגיות")
        self.assertContains(resp, "משפחה")

    def test_manual_text_detail_hides_empty_discovery_metadata_labels(self):
        item = create_manual_text_archive_item(
            title="Manual without discovery display",
            body="Only typed body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Only typed body.")
        self.assertNotContains(resp, "archive-discovery-meta")
        self.assertNotContains(resp, "קטגוריות")
        self.assertNotContains(resp, "אירועים")
        self.assertNotContains(resp, "תגיות")

    def test_ocr_document_detail_shows_archive_item_discovery_metadata(self):
        doc = create_ocr_document(
            title="OCR discovery display",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self._attach_discovery_metadata(doc.archive_item)

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "קטגוריות")
        self.assertContains(resp, "יהדות מצרים")
        self.assertContains(resp, "אירועים")
        self.assertContains(resp, "חתונת משפחה")
        self.assertContains(resp, "תגיות")
        self.assertContains(resp, "משפחה")

    def test_ocr_document_detail_hides_duplicate_legacy_catalog_when_archive_item_has_discovery(
        self,
    ):
        doc = create_ocr_document(
            title="OCR no duplicate discovery",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            category_event="Legacy event",
        )
        doc.tags_m2m.add(Tag.objects.create(name="legacy-doc-tag"))
        self._attach_discovery_metadata(doc.archive_item)

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "יהדות מצרים")
        self.assertContains(resp, "חתונת משפחה")
        self.assertContains(resp, "משפחה")
        self.assertNotContains(resp, "document-catalog-meta")
        self.assertNotContains(resp, "Legacy event")
        self.assertNotContains(resp, "legacy-doc-tag")
        self.assertNotIn("אירוע / קטגוריה", html)

    def test_ocr_document_detail_hides_legacy_category_event_when_archive_item_categories_empty(
        self,
    ):
        doc = create_ocr_document(
            title="OCR legacy category hidden",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            category_event="Legacy wedding",
        )

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "archive-discovery-meta")
        self.assertNotContains(resp, "document-catalog-meta")
        self.assertNotContains(resp, "אירוע / קטגוריה")
        self.assertNotContains(resp, "Legacy wedding")

    def test_ocr_document_detail_hides_legacy_tags_when_archive_item_tags_empty(
        self,
    ):
        doc = create_ocr_document(
            title="OCR legacy tags hidden",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        doc.tags_m2m.add(Tag.objects.create(name="legacy-family"))

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "archive-discovery-meta")
        self.assertNotContains(resp, "document-catalog-meta")
        self.assertNotContains(resp, "legacy-family")

    def test_anonymous_cannot_see_discovery_metadata_for_private_items(self):
        item = create_manual_text_archive_item(
            title="Private discovery manual",
            body="Private body.",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self._attach_discovery_metadata(item)

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 404)

        doc = create_ocr_document(
            title="Private discovery OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        self._attach_discovery_metadata(doc.archive_item)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 404)

    def test_staff_sees_public_discovery_metadata_and_existing_admin_controls(self):
        doc = create_ocr_document(
            title="Staff discovery display",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self._attach_discovery_metadata(doc.archive_item)
        DocumentMetadata.objects.create(
            document=doc,
            donor="Private donor",
            collection="Private collection",
            original_location="Private shelf",
            notes="Private notes",
        )

        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "יהדות מצרים")
        self.assertContains(resp, "עריכת מטא־דאטה")
        self.assertContains(resp, "מטא־דאטה למנהלים")
        self.assertContains(resp, "Private donor")
        self.assertContains(resp, "Private collection")
        self.assertContains(resp, "Private shelf")
        self.assertContains(resp, "Private notes")

    def test_document_metadata_remains_staff_admin_only_for_anonymous(self):
        doc = create_ocr_document(
            title="Anonymous admin metadata guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self._attach_discovery_metadata(doc.archive_item)
        DocumentMetadata.objects.create(
            document=doc,
            donor="Hidden donor",
            collection="Hidden collection",
            original_location="Hidden location",
            notes="Hidden notes",
        )

        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "יהדות מצרים")
        self.assertNotContains(resp, "מטא־דאטה למנהלים")
        self.assertNotContains(resp, "Hidden donor")
        self.assertNotContains(resp, "Hidden collection")
        self.assertNotContains(resp, "Hidden location")
        self.assertNotContains(resp, "Hidden notes")


class ArchiveItemPresentationUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="presentation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="presentation_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def test_presentation_helpers_map_values_to_hebrew(self):
        self.assertEqual(visibility_label("public"), "ציבורי")
        self.assertEqual(visibility_label("private"), "פרטי")
        self.assertEqual(
            archive_metadata_status_label("NEEDS_COMPLETION"),
            "דרושה השלמת פרטים",
        )
        self.assertEqual(archive_metadata_status_label("COMPLETED"), "פרטים הושלמו")
        self.assertEqual(
            archive_item_type_label(ArchiveItem.ItemType.OCR_DOCUMENT),
            "מסמך",
        )
        self.assertEqual(
            archive_item_type_label(ArchiveItem.ItemType.MANUAL_TEXT),
            "טקסט",
        )
        self.assertEqual(language_label("heb"), "עברית")
        self.assertEqual(language_label("he"), "עברית")
        self.assertEqual(language_label("en"), "אנגלית")

    def test_archive_item_has_discovery_metadata_returns_false_for_none(self):
        self.assertFalse(archive_item_has_discovery_metadata(None))

    def test_archive_item_has_discovery_metadata_uses_full_prefetch_cache(self):
        item = create_manual_text_archive_item(
            title="Full prefetch discovery helper",
            body="Body",
        )
        category = ArchiveCategory.objects.create(
            name="Helper category",
            slug="helper-category",
        )
        item.categories.add(category)

        prefetched = ArchiveItem.objects.prefetch_related(
            "categories", "events", "tags"
        ).get(pk=item.pk)
        self.assertTrue(archive_item_has_discovery_metadata(prefetched))

        empty_item = create_manual_text_archive_item(
            title="Empty prefetch discovery helper",
            body="Body",
        )
        empty_prefetched = ArchiveItem.objects.prefetch_related(
            "categories", "events", "tags"
        ).get(pk=empty_item.pk)
        self.assertFalse(archive_item_has_discovery_metadata(empty_prefetched))

    def test_archive_item_has_discovery_metadata_ignores_partial_prefetch_cache(self):
        item = create_manual_text_archive_item(
            title="Partial prefetch discovery helper",
            body="Body",
        )
        event = ArchiveEvent.objects.create(
            name="Helper event",
            slug="helper-event",
        )
        item.events.add(event)

        partial_prefetch = ArchiveItem.objects.prefetch_related("categories").get(
            pk=item.pk
        )
        self.assertTrue(archive_item_has_discovery_metadata(partial_prefetch))

    def test_archive_list_does_not_expose_raw_item_type_labels(self):
        create_manual_text_archive_item(
            title="Label manual",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_ocr_document(
            title="Label OCR",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מסמך")
        self.assertContains(resp, "טקסט")
        self.assertNotContains(resp, "OCR document")
        self.assertNotContains(resp, "Manual text")

    def test_archive_list_shows_type_filter_buttons(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, "הכול")
        self.assertContains(resp, "מסמכים")
        self.assertContains(resp, "טקסטים")
        self.assertNotContains(resp, "מסמכים (OCR)")

    def test_archive_list_item_type_filter_limits_results(self):
        create_manual_text_archive_item(
            title="Filter manual only",
            body="x",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_ocr_document(
            title="Filter OCR only",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-list"), {"item_type": "manual_text"})
        self.assertContains(resp, "Filter manual only")
        self.assertNotContains(resp, "Filter OCR only")

        resp = self.client.get(reverse("archive-list"), {"item_type": "ocr_document"})
        self.assertContains(resp, "Filter OCR only")
        self.assertNotContains(resp, "Filter manual only")

    def test_archive_list_filter_respects_anonymous_visibility(self):
        create_manual_text_archive_item(
            title="Public manual filter",
            body="x",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_manual_text_archive_item(
            title="Private manual filter",
            body="x",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        resp = self.client.get(reverse("archive-list"), {"item_type": "manual_text"})
        self.assertContains(resp, "Public manual filter")
        self.assertNotContains(resp, "Private manual filter")

    def test_archive_list_search_input_preserves_q_after_submit(self):
        create_manual_text_archive_item(
            title="Search input preserve item",
            body="x",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-list"), {"q": "preserve-query"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="q"')
        self.assertContains(resp, 'value="preserve-query"')
        self.assertContains(resp, "חיפוש בארכיון")

    def test_archive_manage_list_shows_hebrew_visibility_and_metadata(self):
        create_manual_text_archive_item(
            title="Manage labels item",
            body="x",
            visibility=ArchiveItem.Visibility.PRIVATE,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(resp, "פרטי")
        self.assertContains(resp, "דרושה השלמת פרטים")
        self.assertNotContains(resp, "Private")
        self.assertNotContains(resp, "Needs completion")

    def test_manual_text_form_renders_hebrew_visibility_and_metadata_choices(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/archive/manage/new/manual-text/")
        self.assertContains(resp, "ציבורי")
        self.assertContains(resp, "פרטי")
        self.assertContains(resp, "דרושה השלמת פרטים")
        self.assertContains(resp, "פרטים הושלמו")
        self.assertNotContains(resp, ">Public<")
        self.assertNotContains(resp, "Completed")

    def test_archive_detail_admin_shows_hebrew_badges(self):
        item = create_manual_text_archive_item(
            title="Detail labels",
            body="Body text",
            visibility=ArchiveItem.Visibility.PUBLIC,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(resp, "טקסט")
        self.assertContains(resp, "ציבורי")
        self.assertContains(resp, "פרטים הושלמו")
        self.assertNotContains(resp, "MANUAL_TEXT")
        self.assertNotContains(resp, "COMPLETED")


class ArchiveItemListSearchTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="archive_search_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_public_item(self, **kwargs) -> ArchiveItem:
        defaults = {
            "title": "Public search item",
            "body": "Search test body.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
        }
        defaults.update(kwargs)
        body = defaults.pop("body")
        return create_manual_text_archive_item(body=body, **defaults)

    def _create_private_item(self, **kwargs) -> ArchiveItem:
        defaults = {
            "title": "Private search item",
            "body": "Private search body.",
            "visibility": ArchiveItem.Visibility.PRIVATE,
        }
        defaults.update(kwargs)
        body = defaults.pop("body")
        return create_manual_text_archive_item(body=body, **defaults)

    def test_anonymous_search_finds_public_item_by_title(self):
        item = self._create_public_item(title="Unique public title search")
        resp = self.client.get(reverse("archive-list"), {"q": "public title search"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)

    def test_anonymous_search_finds_public_item_by_author_name(self):
        item = self._create_public_item(
            title="Author search item",
            author_name="Unique author search name",
        )
        resp = self.client.get(reverse("archive-list"), {"q": "author search name"})
        self.assertContains(resp, item.title)

    def test_anonymous_search_finds_public_item_by_source_title(self):
        item = self._create_public_item(
            title="Source search item",
            source_title="Unique source search title",
        )
        resp = self.client.get(reverse("archive-list"), {"q": "source search title"})
        self.assertContains(resp, item.title)

    def test_anonymous_search_finds_public_item_by_category_name(self):
        item = self._create_public_item(title="Category search item")
        category = ArchiveCategory.objects.create(
            name="Unique category search term",
            slug="unique-category-search-term",
        )
        item.categories.add(category)
        resp = self.client.get(reverse("archive-list"), {"q": "category search term"})
        self.assertContains(resp, item.title)

    def test_anonymous_search_finds_public_item_by_event_name(self):
        item = self._create_public_item(title="Event search item")
        event = ArchiveEvent.objects.create(
            name="Unique event search term",
            slug="unique-event-search-term",
        )
        item.events.add(event)
        resp = self.client.get(reverse("archive-list"), {"q": "event search term"})
        self.assertContains(resp, item.title)

    def test_anonymous_search_finds_public_item_by_tag_name(self):
        item = self._create_public_item(title="Tag search item")
        tag = Tag.objects.create(name="unique-tag-search-term")
        item.tags.add(tag)
        resp = self.client.get(reverse("archive-list"), {"q": "tag-search-term"})
        self.assertContains(resp, item.title)

    def test_anonymous_search_does_not_reveal_private_items(self):
        self._create_private_item(title="Hidden private title search")
        resp = self.client.get(reverse("archive-list"), {"q": "private title search"})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Hidden private title search")

    def test_staff_search_can_find_private_items(self):
        item = self._create_private_item(title="Staff private title search")
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"), {"q": "private title search"})
        self.assertContains(resp, item.title)

    def test_search_preserves_item_type_filter(self):
        manual_item = self._create_public_item(title="Search filter manual item")
        ocr_doc = create_ocr_document(
            title="Search filter OCR item",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "search filter", "item_type": "manual_text"},
        )
        self.assertContains(resp, manual_item.title)
        self.assertNotContains(resp, ocr_doc.title)

    def test_whitespace_search_query_behaves_like_no_search(self):
        public_item = self._create_public_item(title="Whitespace search public")
        self._create_private_item(title="Whitespace search private hidden")
        resp = self.client.get(reverse("archive-list"), {"q": "   "})
        self.assertContains(resp, public_item.title)
        self.assertNotContains(resp, "Whitespace search private hidden")

    def test_search_duplicate_m2m_matches_do_not_duplicate_rows(self):
        item = self._create_public_item(title="Duplicate m2m search item")
        category_one = ArchiveCategory.objects.create(
            name="Shared duplicate search alpha",
            slug="shared-duplicate-search-alpha",
        )
        category_two = ArchiveCategory.objects.create(
            name="Shared duplicate search beta",
            slug="shared-duplicate-search-beta",
        )
        item.categories.add(category_one, category_two)
        resp = self.client.get(reverse("archive-list"), {"q": "duplicate search"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.content.decode("utf-8").count("Duplicate m2m search item"),
            1,
        )

    def test_search_does_not_match_document_metadata_fields(self):
        doc = create_ocr_document(
            title="Metadata isolation search item",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        DocumentMetadata.objects.update_or_create(
            document=doc,
            defaults={
                "donor": "unique-donor-metadata-search-term",
                "collection": "unique-collection-metadata-search-term",
                "original_location": "unique-location-metadata-search-term",
                "notes": "unique-notes-metadata-search-term",
            },
        )
        for term in (
            "unique-donor-metadata-search-term",
            "unique-collection-metadata-search-term",
            "unique-location-metadata-search-term",
            "unique-notes-metadata-search-term",
        ):
            resp = self.client.get(reverse("archive-list"), {"q": term})
            self.assertNotContains(resp, "Metadata isolation search item")

    def test_search_does_not_match_legacy_document_category_event_or_tags(self):
        doc = create_ocr_document(
            title="Legacy discovery search isolation item",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
            category_event="unique-legacy-category-event-search-term",
        )
        doc.tags_m2m.add(Tag.objects.create(name="unique-legacy-tag-search-term"))

        for term in (
            "unique-legacy-category-event-search-term",
            "unique-legacy-tag-search-term",
        ):
            resp = self.client.get(reverse("archive-list"), {"q": term})
            self.assertNotContains(resp, "Legacy discovery search isolation item")


class ArchiveItemDiscoveryBrowseTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="discovery_browse_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_public_item(self, **kwargs) -> ArchiveItem:
        defaults = {
            "title": "Public browse item",
            "body": "Browse test body.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
        }
        defaults.update(kwargs)
        body = defaults.pop("body")
        return create_manual_text_archive_item(body=body, **defaults)

    def _create_private_item(self, **kwargs) -> ArchiveItem:
        defaults = {
            "title": "Private browse item",
            "body": "Private browse body.",
            "visibility": ArchiveItem.Visibility.PRIVATE,
        }
        defaults.update(kwargs)
        body = defaults.pop("body")
        return create_manual_text_archive_item(body=body, **defaults)

    def _attach_discovery_metadata(self, item: ArchiveItem):
        category = ArchiveCategory.objects.create(
            name="Browse category term",
            slug="browse-category-term",
        )
        event = ArchiveEvent.objects.create(
            name="Browse event term",
            slug="browse-event-term",
        )
        tag = Tag.objects.create(name="browse-tag-term")
        item.categories.add(category)
        item.events.add(event)
        item.tags.add(tag)
        return category, event, tag

    def test_public_item_category_link_on_detail_page(self):
        item = self._create_public_item(title="Category link detail")
        category, _, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            reverse("archive-category-browse", kwargs={"category_id": category.id}),
        )

    def test_public_item_event_link_on_detail_page(self):
        item = self._create_public_item(title="Event link detail")
        _, event, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            reverse("archive-event-browse", kwargs={"event_id": event.id}),
        )

    def test_public_item_tag_link_on_detail_page(self):
        item = self._create_public_item(title="Tag link detail")
        _, _, tag = self._attach_discovery_metadata(item)

        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            reverse("archive-tag-browse", kwargs={"tag_id": tag.id}),
        )

    def test_category_browse_shows_public_linked_item_to_anonymous(self):
        item = self._create_public_item(title="Public category browse visible")
        category, _, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריה: Browse category term")
        self.assertContains(resp, item.title)

    def test_event_browse_shows_public_linked_item_to_anonymous(self):
        item = self._create_public_item(title="Public event browse visible")
        _, event, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": event.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אירוע: Browse event term")
        self.assertContains(resp, item.title)

    def test_tag_browse_shows_public_linked_item_to_anonymous(self):
        item = self._create_public_item(title="Public tag browse visible")
        _, _, tag = self._attach_discovery_metadata(item)

        resp = self.client.get(reverse("archive-tag-browse", kwargs={"tag_id": tag.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תגית: browse-tag-term")
        self.assertContains(resp, item.title)

    def test_category_browse_hides_private_linked_item_from_anonymous(self):
        item = self._create_private_item(title="Hidden private category browse")
        category, _, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריה: Browse category term")
        self.assertNotContains(resp, item.title)

    def test_event_browse_hides_private_linked_item_from_anonymous(self):
        item = self._create_private_item(title="Hidden private event browse")
        _, event, _ = self._attach_discovery_metadata(item)

        resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": event.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אירוע: Browse event term")
        self.assertNotContains(resp, item.title)

    def test_tag_browse_hides_private_linked_item_from_anonymous(self):
        item = self._create_private_item(title="Hidden private tag browse")
        _, _, tag = self._attach_discovery_metadata(item)

        resp = self.client.get(reverse("archive-tag-browse", kwargs={"tag_id": tag.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תגית: browse-tag-term")
        self.assertNotContains(resp, item.title)

    def test_staff_can_see_private_linked_items_on_browse_pages(self):
        item = self._create_private_item(title="Staff private browse visible")
        category, event, tag = self._attach_discovery_metadata(item)
        self.client.force_login(self.staff)

        category_resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        event_resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": event.id})
        )
        tag_resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": tag.id})
        )

        self.assertContains(category_resp, item.title)
        self.assertContains(event_resp, item.title)
        self.assertContains(tag_resp, item.title)

    def test_browse_page_returns_404_for_missing_category_event_tag(self):
        missing_category_id = 9_999_001
        missing_event_id = 9_999_002
        missing_tag_id = 9_999_003

        self.assertEqual(
            self.client.get(
                reverse(
                    "archive-category-browse",
                    kwargs={"category_id": missing_category_id},
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("archive-event-browse", kwargs={"event_id": missing_event_id})
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("archive-tag-browse", kwargs={"tag_id": missing_tag_id})
            ).status_code,
            404,
        )

    def test_browse_page_shows_empty_state_when_no_visible_items(self):
        category = ArchiveCategory.objects.create(
            name="Empty browse category",
            slug="empty-browse-category",
        )
        event = ArchiveEvent.objects.create(
            name="Empty browse event",
            slug="empty-browse-event",
        )
        tag = Tag.objects.create(name="empty-browse-tag")

        private_item = self._create_private_item(title="Only private empty browse")
        private_item.categories.add(category)
        private_item.events.add(event)
        private_item.tags.add(tag)

        category_resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        event_resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": event.id})
        )
        tag_resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": tag.id})
        )

        for resp in (category_resp, event_resp, tag_resp):
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "אין פריטי ארכיון להצגה.")
            self.assertNotContains(resp, "Only private empty browse")

    def test_tag_browse_uses_archive_item_tags_not_legacy_document_tags_m2m(self):
        doc = create_ocr_document(
            title="Legacy tag browse isolation",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        legacy_tag = Tag.objects.create(name="legacy-only-browse-tag")
        doc.tags_m2m.add(legacy_tag)

        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": legacy_tag.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תגית: legacy-only-browse-tag")
        self.assertNotContains(resp, doc.title)

    def test_existing_archive_search_still_works_after_browse_pages(self):
        item = self._create_public_item(title="Search still works browse regression")
        category = ArchiveCategory.objects.create(
            name="Search regression category term",
            slug="search-regression-category-term",
        )
        item.categories.add(category)

        resp = self.client.get(
            reverse("archive-list"), {"q": "search regression category"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, item.title)

    def test_browse_urls_are_id_based_not_slug_based(self):
        category = ArchiveCategory.objects.create(
            name="Slug unused category",
            slug="slug-unused-category",
        )
        event = ArchiveEvent.objects.create(
            name="Slug unused event",
            slug="slug-unused-event",
        )

        category_resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        event_resp = self.client.get(
            reverse("archive-event-browse", kwargs={"event_id": event.id})
        )

        self.assertEqual(category_resp.status_code, 200)
        self.assertEqual(event_resp.status_code, 200)
        self.assertNotContains(
            category_resp, "/archive/categories/slug-unused-category/"
        )
        self.assertNotContains(event_resp, "/archive/events/slug-unused-event/")


class ManualTextArchiveItemDeleteTests(TestCase):
    DELETE_URL_TEMPLATE = "/archive/manage/{item_id}/delete/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_text_delete_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _create_family_user(self, username="manual_text_delete_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _delete_url(self, item_id: int) -> str:
        return self.DELETE_URL_TEMPLATE.format(item_id=item_id)

    def test_staff_can_access_delete_confirmation_for_manual_text(self):
        item = create_manual_text_archive_item(title="Delete me", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.get(self._delete_url(item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחיקת טקסט מוקלד")
        self.assertContains(resp, item.title)
        self.assertContains(resp, "לא ניתן לשחזר מתוך האתר כרגע")

    def test_get_confirmation_does_not_delete_archive_item(self):
        item = create_manual_text_archive_item(title="Still here", body="Body")
        self.client.force_login(self.staff)
        self.client.get(self._delete_url(item.id))
        self.assertTrue(ArchiveItem.objects.filter(pk=item.id).exists())

    def test_get_confirmation_does_not_delete_manual_text_content(self):
        item = create_manual_text_archive_item(title="Content stays", body="Body")
        content_id = item.manual_text_content.id
        self.client.force_login(self.staff)
        self.client.get(self._delete_url(item.id))
        self.assertTrue(ManualTextContent.objects.filter(pk=content_id).exists())

    def test_staff_post_deletes_archive_item(self):
        item = create_manual_text_archive_item(title="Gone item", body="Body")
        item_id = item.id
        self.client.force_login(self.staff)
        resp = self.client.post(self._delete_url(item_id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("archive-manage-list"))
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())

    def test_staff_post_deletes_manual_text_content_via_cascade(self):
        item = create_manual_text_archive_item(title="Gone content", body="Body")
        content_id = item.manual_text_content.id
        self.client.force_login(self.staff)
        self.client.post(self._delete_url(item.id))
        self.assertFalse(ManualTextContent.objects.filter(pk=content_id).exists())

    def test_after_post_delete_archive_detail_returns_404(self):
        item = create_manual_text_archive_item(title="Detail gone", body="Body")
        item_id = item.id
        self.client.force_login(self.staff)
        self.client.post(self._delete_url(item_id))
        resp = self.client.get(f"/archive/{item_id}/")
        self.assertEqual(resp.status_code, 404)

    def test_after_post_delete_item_not_in_manage_list(self):
        item = create_manual_text_archive_item(title="Manage list gone", body="Body")
        self.client.force_login(self.staff)
        self.client.post(self._delete_url(item.id))
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertNotContains(resp, "Manage list gone")

    def test_anonymous_cannot_access_delete_page(self):
        item = create_manual_text_archive_item(title="Protected", body="Body")
        resp = self.client.get(self._delete_url(item.id))
        self.assertIn(resp.status_code, (302, 403))

    def test_family_user_cannot_access_delete_page(self):
        item = create_manual_text_archive_item(title="Family blocked", body="Body")
        self.client.force_login(self._create_family_user())
        resp = self.client.get(self._delete_url(item.id))
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_authenticated_user_cannot_access_delete_page(self):
        item = create_manual_text_archive_item(title="User blocked", body="Body")
        user = User.objects.create_user(
            username="manual_text_delete_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(self._delete_url(item.id))
        self.assertEqual(resp.status_code, 403)

    def test_delete_route_returns_404_for_ocr_document(self):
        doc = create_ocr_document(
            title="OCR delete guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._delete_url(doc.archive_item_id))
        self.assertEqual(resp.status_code, 404)

    def test_ocr_document_is_not_deleted_through_delete_route(self):
        doc = create_ocr_document(
            title="OCR survives",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        archive_item_id = doc.archive_item_id
        self.client.force_login(self.staff)
        resp = self.client.post(self._delete_url(archive_item_id))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(ArchiveItem.objects.filter(pk=archive_item_id).exists())
        self.assertTrue(Document.objects.filter(pk=doc.id).exists())

    def test_delete_action_appears_for_staff_on_manual_text_detail(self):
        item = create_manual_text_archive_item(title="Detail delete link", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )
        self.assertContains(
            resp, reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertContains(resp, "מחיקה")

    def test_delete_action_still_hidden_for_staff_on_ocr_document_detail(self):
        doc = create_ocr_document(
            title="OCR detail no delete action",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{doc.archive_item_id}/", follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(
            resp,
            reverse("archive-manage-delete", kwargs={"item_id": doc.archive_item_id}),
        )
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": doc.archive_item_id}),
        )

    def test_delete_action_appears_for_staff_on_manage_list_manual_text_row(self):
        item = create_manual_text_archive_item(title="Manage delete link", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )
        self.assertContains(resp, "מחיקה")

    def test_delete_action_does_not_appear_for_anonymous_on_detail(self):
        item = create_manual_text_archive_item(
            title="Anonymous detail",
            body="Body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertNotContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )

    def test_delete_action_does_not_appear_for_family_user_on_detail(self):
        item = create_manual_text_archive_item(
            title="Family detail",
            body="Body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self._create_family_user())
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertNotContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )

    def test_delete_action_does_not_appear_for_non_staff_on_detail(self):
        item = create_manual_text_archive_item(
            title="User detail",
            body="Body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        user = User.objects.create_user(
            username="manual_text_delete_viewer",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertNotContains(
            resp, reverse("archive-manage-delete", kwargs={"item_id": item.id})
        )

    def test_family_user_cannot_access_manage_list_with_delete_action(self):
        create_manual_text_archive_item(title="Family manage", body="Body")
        self.client.force_login(self._create_family_user())
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_cannot_access_manage_list_with_delete_action(self):
        create_manual_text_archive_item(title="User manage", body="Body")
        user = User.objects.create_user(
            username="manual_text_delete_manage_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(user)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 403)

    def test_delete_action_does_not_appear_for_ocr_document_on_manage_list(self):
        doc = create_ocr_document(
            title="OCR no delete button",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertNotContains(
            resp,
            reverse("archive-manage-delete", kwargs={"item_id": doc.archive_item_id}),
        )

    def test_post_delete_requires_csrf(self):
        item = create_manual_text_archive_item(title="CSRF guard", body="Body")
        self.csrf_client.force_login(self.staff)
        resp = self.csrf_client.post(self._delete_url(item.id))
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(ArchiveItem.objects.filter(pk=item.id).exists())


class ArchiveItemDiscoveryMetadataFoundationTests(TestCase):
    def test_archive_category_stores_fields_and_str_returns_name(self):
        category = ArchiveCategory.objects.create(
            name="יהדות מצרים",
            slug="yahadut-mitzrayim",
            description="Broad topic",
        )
        self.assertEqual(category.name, "יהדות מצרים")
        self.assertEqual(category.slug, "yahadut-mitzrayim")
        self.assertEqual(category.description, "Broad topic")
        self.assertEqual(str(category), "יהדות מצרים")

    def test_archive_category_name_is_unique(self):
        ArchiveCategory.objects.create(name="Unique topic", slug="unique-topic")
        with self.assertRaises(IntegrityError):
            ArchiveCategory.objects.create(name="Unique topic", slug="unique-topic-2")

    def test_archive_event_stores_fields_and_str_returns_name(self):
        date_start = datetime.date(1950, 6, 1)
        date_end = datetime.date(1950, 6, 2)
        event = ArchiveEvent.objects.create(
            name="חתונה של דוד",
            slug="david-wedding",
            description="Family wedding",
            date_start=date_start,
            date_end=date_end,
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        self.assertEqual(event.name, "חתונה של דוד")
        self.assertEqual(event.slug, "david-wedding")
        self.assertEqual(event.description, "Family wedding")
        self.assertEqual(event.date_start, date_start)
        self.assertEqual(event.date_end, date_end)
        self.assertEqual(event.date_precision, ArchiveItem.DatePrecision.RANGE)
        self.assertEqual(str(event), "חתונה של דוד")

    def test_archive_event_name_is_unique(self):
        ArchiveEvent.objects.create(name="Unique event", slug="unique-event")
        with self.assertRaises(IntegrityError):
            ArchiveEvent.objects.create(name="Unique event", slug="unique-event-2")

    def test_archive_item_can_have_multiple_categories(self):
        item = create_manual_text_archive_item(title="Multi-category item", body="Body")
        cat_a = ArchiveCategory.objects.create(name="Topic A", slug="topic-a")
        cat_b = ArchiveCategory.objects.create(name="Topic B", slug="topic-b")
        item.categories.add(cat_a, cat_b)
        self.assertEqual(
            set(item.categories.values_list("slug", flat=True)),
            {"topic-a", "topic-b"},
        )

    def test_one_archive_category_links_to_multiple_archive_items(self):
        category = ArchiveCategory.objects.create(
            name="Shared topic", slug="shared-topic"
        )
        item_one = create_manual_text_archive_item(title="Item one", body="One")
        item_two = create_ocr_document(
            title="Item two",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        ).archive_item
        item_one.categories.add(category)
        item_two.categories.add(category)
        self.assertEqual(category.archive_items.count(), 2)
        self.assertEqual(
            set(category.archive_items.values_list("title", flat=True)),
            {"Item one", "Item two"},
        )

    def test_archive_item_can_have_zero_one_or_multiple_events(self):
        item = create_manual_text_archive_item(title="Events item", body="Body")
        self.assertEqual(item.events.count(), 0)

        event_one = ArchiveEvent.objects.create(name="Event one", slug="event-one")
        item.events.add(event_one)
        self.assertEqual(
            list(item.events.values_list("slug", flat=True)), ["event-one"]
        )

        event_two = ArchiveEvent.objects.create(name="Event two", slug="event-two")
        item.events.add(event_two)
        self.assertEqual(
            set(item.events.values_list("slug", flat=True)),
            {"event-one", "event-two"},
        )

    def test_one_archive_event_links_to_multiple_archive_items(self):
        event = ArchiveEvent.objects.create(name="Shared event", slug="shared-event")
        manual_item = create_manual_text_archive_item(
            title="Manual event link",
            body="Body",
        )
        ocr_item = create_ocr_document(
            title="OCR event link",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        ).archive_item
        manual_item.events.add(event)
        ocr_item.events.add(event)
        self.assertEqual(event.archive_items.count(), 2)

    def test_archive_item_tags_use_archive_item_level_relation(self):
        item = create_manual_text_archive_item(title="Tagged item", body="Body")
        tag_a = Tag.objects.create(name="family")
        tag_b = Tag.objects.create(name="cairo")
        item.tags.add(tag_a, tag_b)
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"family", "cairo"},
        )
        self.assertEqual(tag_a.archive_items.get(), item)

    def test_document_tags_m2m_is_separate_from_archive_item_tags(self):
        doc = create_ocr_document(
            title="Separate tag relations",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        item = doc.archive_item
        shared_tag = Tag.objects.create(name="shared-label")
        doc_only_tag = Tag.objects.create(name="doc-only")
        item_only_tag = Tag.objects.create(name="item-only")

        doc.tags_m2m.add(shared_tag, doc_only_tag)
        item.tags.add(shared_tag, item_only_tag)

        self.assertEqual(
            set(doc.tags_m2m.values_list("name", flat=True)),
            {"shared-label", "doc-only"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"shared-label", "item-only"},
        )
        self.assertEqual(shared_tag.documents.get(), doc)
        self.assertEqual(shared_tag.archive_items.get(), item)

    def test_create_ocr_document_does_not_require_discovery_metadata(self):
        doc = create_ocr_document(
            title="OCR without discovery metadata",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        )
        item = doc.archive_item
        self.assertEqual(item.categories.count(), 0)
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(item.tags.count(), 0)

    def test_create_manual_text_does_not_require_discovery_metadata(self):
        item = create_manual_text_archive_item(
            title="Manual without discovery metadata",
            body="Typed body",
        )
        self.assertEqual(item.categories.count(), 0)
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)


class ArchiveItemDiscoveryMetadataEditTests(TestCase):
    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="discovery_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def _create_family_user(self, username="discovery_edit_family"):
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(self.family_group)
        return user

    def _manual_text_payload(self, **overrides):
        payload = {
            "title": "Manual discovery",
            "body": "Body text.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return payload

    def _ocr_metadata_payload(self, **overrides):
        payload = {
            "title": "OCR discovery",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "donor": "",
            "collection": "",
            "original_location": "",
            "notes": "",
            "category_event": "",
            "tags": "",
            "categories": "",
            "events": "",
            "discovery_tags": "",
        }
        payload.update(overrides)
        return payload

    def test_manual_text_get_seeds_discovery_metadata(self):
        item = create_manual_text_archive_item(title="Seed manual", body="Body")
        cat = ArchiveCategory.objects.create(
            name="יהדות מצרים", slug="yahadut-mitzrayim"
        )
        event = ArchiveEvent.objects.create(name="חתונה", slug="hatuna")
        tag = Tag.objects.create(name="family")
        item.categories.add(cat)
        item.events.add(event)
        item.tags.add(tag)

        self.client.force_login(self.staff)
        resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            f'<option value="{cat.id}" selected>{cat.name}</option>',
            html=True,
        )
        self.assertContains(
            resp,
            f'<option value="{event.id}" selected>{event.name}</option>',
            html=True,
        )
        self.assertContains(
            resp,
            f'<option value="{tag.id}" selected>{tag.name}</option>',
            html=True,
        )
        self.assertNotContains(resp, 'value="יהדות מצרים"')

    def test_manual_text_post_creates_and_reuses_discovery_metadata(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Existing topic",
            slug="existing-topic",
        )
        ArchiveEvent.objects.create(
            name="Existing event",
            slug="existing-event",
        )
        Tag.objects.create(name="existing-tag")

        item = create_manual_text_archive_item(
            title="Manual POST discovery", body="Body"
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_text_payload(
                categories="Existing topic, New topic",
                events="Existing event, New event",
                tags="existing-tag, new-tag",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Existing topic", "New topic"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"Existing event", "New event"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"existing-tag", "new-tag"},
        )
        self.assertTrue(
            ArchiveCategory.objects.filter(name="New topic", slug="new-topic").exists()
        )
        self.assertTrue(
            ArchiveEvent.objects.filter(name="New event", slug="new-event").exists()
        )
        self.assertEqual(existing_cat.archive_items.get(), item)

    def test_manual_text_post_clears_discovery_metadata(self):
        item = create_manual_text_archive_item(title="Clear manual", body="Body")
        item.categories.add(
            ArchiveCategory.objects.create(name="To clear cat", slug="to-clear-cat")
        )
        item.events.add(
            ArchiveEvent.objects.create(name="To clear event", slug="to-clear-event")
        )
        item.tags.add(Tag.objects.create(name="to-clear-tag"))

        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_text_payload(),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.categories.count(), 0)
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(item.tags.count(), 0)

    def test_ocr_get_seeds_discovery_metadata(self):
        doc = create_ocr_document(
            title="Seed OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        item = doc.archive_item
        cat = ArchiveCategory.objects.create(name="OCR category", slug="ocr-category")
        event = ArchiveEvent.objects.create(name="OCR event", slug="ocr-event")
        tag = Tag.objects.create(name="ocr-discovery-tag")
        item.categories.add(cat)
        item.events.add(event)
        item.tags.add(tag)

        self.client.force_login(self.staff)
        resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            f'<option value="{cat.id}" selected>{cat.name}</option>',
            html=True,
        )
        self.assertContains(
            resp,
            f'<option value="{event.id}" selected>{event.name}</option>',
            html=True,
        )
        self.assertContains(
            resp,
            f'<option value="{tag.id}" selected>{tag.name}</option>',
            html=True,
        )
        self.assertNotContains(resp, 'value="OCR category"')

    def test_manual_text_post_selected_existing_and_new_typed_values_together(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Selected topic",
            slug="selected-topic",
        )
        existing_event = ArchiveEvent.objects.create(
            name="Selected event",
            slug="selected-event",
        )
        existing_tag = Tag.objects.create(name="selected-tag")

        item = create_manual_text_archive_item(
            title="Combined manual discovery", body="Body"
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data={
                **self._manual_text_payload(),
                "selected_categories": [str(existing_cat.id)],
                "selected_events": [str(existing_event.id)],
                "selected_tags": [str(existing_tag.id)],
                "categories": "New typed topic",
                "events": "New typed event",
                "tags": "new-typed-tag",
            },
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Selected topic", "New typed topic"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"Selected event", "New typed event"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"selected-tag", "new-typed-tag"},
        )

    def test_ocr_post_saves_discovery_metadata_on_archive_item(self):
        item = create_ocr_document(
            title="OCR POST discovery",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        ).archive_item
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._ocr_metadata_payload(
                categories="OCR cat one, OCR cat two",
                events="OCR event one",
                discovery_tags="ocr-tag-one, ocr-tag-two",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"OCR cat one", "OCR cat two"},
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["OCR event one"],
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"ocr-tag-one", "ocr-tag-two"},
        )

    def test_ocr_post_does_not_change_document_category_event(self):
        doc = create_ocr_document(
            title="OCR category_event guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Legacy category event",
        )
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_metadata_payload(
                categories="Archive category",
                category_event="Legacy category event",
            ),
        )
        doc.refresh_from_db()
        self.assertEqual(doc.category_event, "Legacy category event")

    def test_ocr_post_does_not_change_document_tags_m2m(self):
        doc = create_ocr_document(
            title="OCR doc tags guard",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(Tag.objects.create(name="doc-only-tag"))
        self.client.force_login(self.staff)
        self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_metadata_payload(
                tags="doc-only-tag",
                discovery_tags="item-only-tag",
            ),
        )
        doc.refresh_from_db()
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["doc-only-tag"],
        )
        self.assertEqual(
            list(doc.archive_item.tags.values_list("name", flat=True)),
            ["item-only-tag"],
        )

    def test_discovery_validation_error_blocks_ocr_partial_save(self):
        doc = create_ocr_document(
            title="Before discovery error",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Before event",
        )
        doc.tags_m2m.add(Tag.objects.create(name="before-doc-tag"))
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem before discovery error",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_metadata_payload(
                title="After discovery error",
                donor="New donor",
                category_event="After event",
                tags="after-doc-tag",
                categories="c" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריה חייבת להיות עד 255 תווים")
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(doc.title, "Before discovery error")
        self.assertEqual(item.title, "ArchiveItem before discovery error")
        self.assertEqual(doc.category_event, "Before event")
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["before-doc-tag"],
        )
        self.assertEqual(item.categories.count(), 0)

    def test_discovery_validation_error_blocks_manual_text_partial_save(self):
        item = create_manual_text_archive_item(
            title="Before manual discovery error",
            body="Original body",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_text_payload(
                title="After manual discovery error",
                body="New body",
                events="e" * 256,
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אירוע חייב להיות עד 255 תווים")
        item.refresh_from_db()
        item.manual_text_content.refresh_from_db()
        self.assertEqual(item.title, "Before manual discovery error")
        self.assertEqual(item.manual_text_content.body, "Original body")

    def test_over_255_discovery_tag_rejected_with_hebrew_error(self):
        item = create_manual_text_archive_item(title="Tag length guard", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_text_payload(tags="t" * 65),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תגית חייבת להיות עד 64 תווים")
        self.assertEqual(item.tags.count(), 0)

    def test_non_staff_cannot_access_discovery_edit_page(self):
        manual_item = create_manual_text_archive_item(title="Guard manual", body="Body")
        ocr_item = create_ocr_document(
            title="Guard OCR",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
        ).archive_item

        for item_id in (manual_item.id, ocr_item.id):
            resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item_id))
            self.assertIn(resp.status_code, (302, 403))

            self.client.force_login(self._create_family_user(f"family-{item_id}"))
            resp = self.client.get(self.EDIT_URL_TEMPLATE.format(item_id=item_id))
            self.assertEqual(resp.status_code, 403)
            self.client.logout()

    def test_ocr_shared_catalog_and_discovery_metadata_edit_still_work(self):
        doc = create_ocr_document(
            title="Combined OCR edit",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            category_event="Legacy event",
        )
        doc.tags_m2m.add(Tag.objects.create(name="doc-only-tag"))
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_metadata_payload(
                title="Combined after",
                donor="Donor value",
                category_event="Tampered event",
                tags="tampered-doc-tag",
                categories="Archive cat",
                discovery_tags="item-tag",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        doc.refresh_from_db()
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(doc.title, "Combined after")
        self.assertEqual(item.title, "Combined after")
        self.assertEqual(doc.admin_meta.donor, "Donor value")
        self.assertEqual(doc.category_event, "Legacy event")
        self.assertEqual(
            list(doc.tags_m2m.values_list("name", flat=True)),
            ["doc-only-tag"],
        )
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["Archive cat"],
        )
        self.assertEqual(list(item.tags.values_list("name", flat=True)), ["item-tag"])

    def test_generated_slugs_are_unique_for_same_slug_base(self):
        item = create_manual_text_archive_item(title="Slug uniqueness", body="Body")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Topic A"],
            event_names=["Event A"],
            tag_names=[],
        )
        category_a = ArchiveCategory.objects.get(name="Topic A")
        event_a = ArchiveEvent.objects.get(name="Event A")

        update_archive_item_discovery_metadata(
            item,
            category_names=["Topic A", "topic a"],
            event_names=["Event A", "event a"],
            tag_names=[],
        )
        category_b = ArchiveCategory.objects.get(name="topic a")
        event_b = ArchiveEvent.objects.get(name="event a")

        self.assertEqual(category_a.slug, "topic-a")
        self.assertEqual(category_b.slug, "topic-a-2")
        self.assertEqual(event_a.slug, "event-a")
        self.assertEqual(event_b.slug, "event-a-2")


class ManualTextCreateDiscoveryMetadataTests(TestCase):
    CREATE_URL = "/archive/manage/new/manual-text/"
    UNIFIED_CREATE_URL = "/archive/manage/new/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_create_discovery_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_payload(self, **overrides):
        payload = {
            "title": "Create discovery item",
            "body": "Created body text.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return payload

    def test_manual_text_create_form_renders_discovery_metadata_fields(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.CREATE_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="categories"')
        self.assertContains(resp, 'name="events"')
        self.assertContains(resp, 'name="tags"')
        self.assertContains(resp, "קטגוריות, אירועים ותגיות")

    def test_unified_manage_new_get_renders_discovery_metadata_fields(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.UNIFIED_CREATE_URL, {"item_type": "manual_text"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="title"')
        self.assertContains(resp, 'name="body"')
        self.assertContains(resp, 'name="categories"')
        self.assertContains(resp, 'name="events"')
        self.assertContains(resp, 'name="tags"')
        self.assertContains(resp, "קטגוריות, אירועים ותגיות")

    def test_unified_manage_new_post_saves_discovery_metadata(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.UNIFIED_CREATE_URL,
            data=self._create_payload(
                item_type="manual_text",
                title="Unified create with discovery",
                categories="Unified category",
                events="Unified event",
                tags="unified-tag",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Unified create with discovery")
        self.assertEqual(item.item_type, ArchiveItem.ItemType.MANUAL_TEXT)
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["Unified category"],
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["Unified event"],
        )
        self.assertEqual(
            list(item.tags.values_list("name", flat=True)),
            ["unified-tag"],
        )

    def test_create_manual_text_with_categories_saves_archive_item_categories(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Create with categories",
                categories="Topic one, Topic two",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Create with categories")
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Topic one", "Topic two"},
        )

    def test_create_manual_text_with_events_saves_archive_item_events(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Create with events",
                events="Wedding, Bar mitzvah",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Create with events")
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"Wedding", "Bar mitzvah"},
        )

    def test_create_manual_text_with_tags_saves_archive_item_tags(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Create with tags",
                tags="family, jerusalem",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Create with tags")
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"family", "jerusalem"},
        )

    def test_created_discovery_metadata_appears_on_detail_page(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Create detail metadata",
                categories="Detail category",
                events="Detail event",
                tags="detail-tag",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Create detail metadata")
        detail_resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(detail_resp, "Detail category")
        self.assertContains(detail_resp, "Detail event")
        self.assertContains(detail_resp, "detail-tag")

    def test_created_discovery_metadata_is_searchable(self):
        self.client.force_login(self.staff)
        self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Searchable create metadata",
                categories="Unique create category search",
            ),
        )
        resp = self.client.get(reverse("archive-list"), {"q": "create category search"})
        self.assertContains(resp, "Searchable create metadata")

    def test_created_discovery_metadata_browse_page_works(self):
        self.client.force_login(self.staff)
        self.client.post(
            self.CREATE_URL,
            data=self._create_payload(
                title="Browse create metadata",
                categories="Browse create category term",
            ),
        )
        item = ArchiveItem.objects.get(title="Browse create metadata")
        category = item.categories.get()
        resp = self.client.get(
            reverse("archive-category-browse", kwargs={"category_id": category.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריה: Browse create category term")
        self.assertContains(resp, item.title)

    def test_create_validation_failure_preserves_discovery_metadata_fields(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Preserved selected category",
            slug="preserved-selected-category",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data={
                **self._create_payload(title=""),
                "selected_categories": [str(existing_cat.id)],
                "categories": "Preserved new category",
                "events": "Preserved event",
                "tags": "preserved-tag",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            f'<option value="{existing_cat.id}" selected>{existing_cat.name}</option>',
            html=True,
        )
        self.assertContains(resp, 'value="Preserved new category"')
        self.assertContains(resp, 'value="Preserved event"')
        self.assertContains(resp, 'value="preserved-tag"')
        self.assertFalse(
            ArchiveItem.objects.filter(events__name="Preserved event").exists()
        )

    def test_create_selected_existing_and_new_typed_values_together(self):
        existing_cat = ArchiveCategory.objects.create(
            name="Create selected category",
            slug="create-selected-category",
        )
        existing_tag = Tag.objects.create(name="create-selected-tag")

        self.client.force_login(self.staff)
        resp = self.client.post(
            self.CREATE_URL,
            data={
                **self._create_payload(title="Create selected and new"),
                "selected_categories": [str(existing_cat.id)],
                "selected_tags": [str(existing_tag.id)],
                "categories": "Create new category",
                "tags": "create-new-tag",
            },
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Create selected and new")
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Create selected category", "Create new category"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"create-selected-tag", "create-new-tag"},
        )

    def test_existing_manual_text_edit_discovery_metadata_still_works(self):
        item = create_manual_text_archive_item(title="Edit still works", body="Body")
        self.client.force_login(self.staff)
        resp = self.client.post(
            f"/archive/manage/{item.id}/edit/",
            data={
                "title": "Edit still works",
                "body": "Body",
                "visibility": ArchiveItem.Visibility.PUBLIC,
                "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "categories": "Edited category",
                "events": "",
                "tags": "",
            },
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["Edited category"],
        )


class ManualTextBodyLinkifyTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_text_linkify_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_public_item(self, body: str) -> ArchiveItem:
        return create_manual_text_archive_item(
            title="Linkify test item",
            body=body,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def test_https_url_renders_clickable_link_on_detail_page(self):
        item = self._create_public_item("See https://example.com for details.")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            '<a href="https://example.com" target="_blank" rel="noopener noreferrer">https://example.com</a>',
            html=True,
        )

    def test_http_url_renders_clickable_link_on_detail_page(self):
        item = self._create_public_item("Visit http://example.com today.")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(
            resp,
            '<a href="http://example.com" target="_blank" rel="noopener noreferrer">http://example.com</a>',
            html=True,
        )

    def test_plain_text_and_line_breaks_remain_readable(self):
        item = self._create_public_item("Line one\nLine two\nNo URL here.")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(resp, "Line one<br />Line two<br />No URL here.", html=True)

    def test_html_script_input_is_escaped_not_rendered(self):
        item = self._create_public_item('<script>alert("xss")</script>')
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(
            resp,
            "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;",
            html=True,
        )
        self.assertNotContains(resp, "<script>", html=True)

    def test_javascript_scheme_is_not_linkified(self):
        item = self._create_public_item("javascript:alert(1)")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(
            resp,
            '<div class="text-block text-block--pre">javascript:alert(1)</div>',
            html=True,
        )

    def test_body_without_urls_renders_normally(self):
        item = self._create_public_item("Plain memoir text only.")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertContains(
            resp,
            '<div class="text-block text-block--pre">Plain memoir text only.</div>',
            html=True,
        )


class ManualTextBodyDisplayServiceTests(SimpleTestCase):
    def test_format_manual_text_body_linkifies_https_url(self):
        rendered = format_manual_text_body_for_display("See https://example.com.")
        self.assertIn(
            '<a href="https://example.com" target="_blank" rel="noopener noreferrer">'
            "https://example.com</a>",
            rendered,
        )

    def test_format_manual_text_body_escapes_html_before_linkify(self):
        rendered = format_manual_text_body_for_display("<b>not bold</b>")
        self.assertEqual(rendered, "&lt;b&gt;not bold&lt;/b&gt;")
