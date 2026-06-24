"""PR5c/PR5f — OCR_DOCUMENT ArchiveItem shared-field read and filter cutover."""

from __future__ import annotations

from datetime import date

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from unittest.mock import patch

from documents.models import ArchiveItem, Document, DocumentTextResult, Tag
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.review_backlog import documents_in_review_backlog


class OcrReadPathCutoverTests(TestCase):
    """Display and filters use ArchiveItem for shared archival fields."""

    EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="pr5c_read_cutover_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_ocr_doc(self, **kwargs):
        defaults = {
            "title": "Document-side title",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "visibility": Document.Visibility.PRIVATE,
            "metadata_status": Document.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": Document.DatePrecision.UNKNOWN,
            "file_s3_key": "documents/99/original.jpg",
            "mime_type": "image/jpeg",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _apply_archive_item_drift(self, doc: Document) -> None:
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem-side title",
            visibility=ArchiveItem.Visibility.PUBLIC,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            date_start=date(1950, 6, 15),
            date_end=date(1950, 6, 15),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )

    def _create_drifted_doc(self, **kwargs) -> Document:
        doc = self._create_ocr_doc(**kwargs)
        self._apply_archive_item_drift(doc)
        doc.refresh_from_db()
        return doc

    def _create_review_pending_doc(self, **kwargs) -> Document:
        doc = self._create_drifted_doc(**kwargs)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="שורת בדיקה",
            review_reasons='["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
        )
        return doc

    def test_list_page_displays_archive_item_shared_fields_when_drifted(self):
        doc = self._create_drifted_doc()
        self._create_drifted_doc(title="Other document title")
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ArchiveItem-side title", html)
        self.assertNotIn("Document-side title", html)
        self.assertIn("15/06/1950", html)
        self.assertIn("פרטים הושלמו", html)
        self.assertIn("ציבורי", html)
        self.assertNotIn(doc.title, html)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.com/presigned",
    )
    def test_detail_page_displays_archive_item_shared_fields_when_drifted(
        self, _mock_presign
    ):
        doc = self._create_drifted_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ArchiveItem-side title", html)
        self.assertNotIn("Document-side title", html)
        self.assertIn("15/06/1950", html)
        self.assertIn("ציבורי", html)

    def test_json_list_api_serializes_archive_item_shared_fields(self):
        doc = self._create_drifted_doc()
        self.client.force_login(self.staff)
        resp = self.client.get("/api/documents/")
        self.assertEqual(resp.status_code, 200)
        item = next(i for i in resp.json()["items"] if i["id"] == doc.id)
        self.assertEqual(item["title"], "ArchiveItem-side title")
        self.assertEqual(item["metadata_status"], ArchiveItem.MetadataStatus.COMPLETED)
        self.assertEqual(item["date_start"], "1950-06-15")
        self.assertEqual(item["date_end"], "1950-06-15")
        self.assertEqual(item["visibility"], ArchiveItem.Visibility.PUBLIC)

    def test_review_detail_displays_archive_item_title_when_drifted(self):
        doc = self._create_review_pending_doc()
        self.client.force_login(self.staff)
        with (
            override_settings(UPLOADS_BUCKET_NAME="test-bucket"),
            patch(
                "documents.views.create_presigned_get",
                return_value="https://example.com/presigned",
            ),
        ):
            resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ArchiveItem-side title", html)
        self.assertNotIn("Document-side title", html)

    def test_review_backlog_displays_archive_item_title_when_drifted(self):
        self._create_review_pending_doc()
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ArchiveItem-side title", html)
        self.assertNotIn("Document-side title", html)

    def test_metadata_backlog_inclusion_uses_archive_item_metadata_status(self):
        excluded_drift = self._create_drifted_doc(
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        included_drift = self._create_ocr_doc(
            title="Excluded document title",
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        ArchiveItem.objects.filter(pk=included_drift.archive_item_id).update(
            title="Included archive title",
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Included archive title", html)
        self.assertNotIn("ArchiveItem-side title", html)
        self.assertNotIn("Excluded document title", html)
        self.assertIn(f"/api/ui/documents/{included_drift.id}/", html)
        self.assertNotIn(f"/api/ui/documents/{excluded_drift.id}/", html)

    def test_list_metadata_status_filter_uses_archive_item_field(self):
        excluded = self._create_ocr_doc(
            title="Needs filter doc",
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        self._apply_archive_item_drift(excluded)
        included = self._create_ocr_doc(
            title="Completed filter doc",
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        ArchiveItem.objects.filter(pk=included.archive_item_id).update(
            title="Completed archive title",
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?metadata_status=NEEDS_COMPLETION")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Completed archive title", html)
        self.assertNotIn("ArchiveItem-side title", html)

    def test_json_api_metadata_status_filter_uses_archive_item_field(self):
        excluded = self._create_ocr_doc(
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        self._apply_archive_item_drift(excluded)
        included = self._create_ocr_doc(
            title="API filter included",
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        ArchiveItem.objects.filter(pk=included.archive_item_id).update(
            title="API filter archive title",
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/documents/?metadata_status=NEEDS_COMPLETION")
        self.assertEqual(resp.status_code, 200)
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertIn(included.id, ids)
        self.assertNotIn(excluded.id, ids)

    def test_visibility_filter_uses_archive_item_field(self):
        excluded = self._create_ocr_doc(
            title="Private mirror doc",
            visibility=Document.Visibility.PRIVATE,
        )
        ArchiveItem.objects.filter(pk=excluded.archive_item_id).update(
            title="Public archive title",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        included = self._create_ocr_doc(
            title="Public mirror doc",
            visibility=Document.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=included.archive_item_id).update(
            title="Private archive title",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?visibility=private")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Private archive title", html)
        self.assertNotIn("Public archive title", html)

    def test_q_search_finds_archive_item_title_when_document_title_differs(self):
        doc = self._create_ocr_doc(title="Document-only title phrase")
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="ArchiveItem-only title phrase",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?q=ArchiveItem-only+title+phrase")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ArchiveItem-only title phrase")
        self.assertNotContains(resp, "Document-only title phrase")

    def test_q_search_still_finds_category_event_on_document(self):
        doc = self._create_ocr_doc(
            title="Category search doc",
            category_event="Unique category event marker",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?q=Unique+category+event+marker")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, str(doc.id))

    def test_q_search_still_finds_tags_on_document(self):
        doc = self._create_ocr_doc(title="Tag search doc")
        tag = Tag.objects.create(name="pr5f-filter-tag-unique")
        doc.tags_m2m.add(tag)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?q=pr5f-filter-tag-unique")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, str(doc.id))

    def test_doc_type_filter_still_uses_document_field(self):
        self._create_ocr_doc(
            title="Image type doc",
            doc_type=Document.DocType.IMAGE,
        )
        self._create_ocr_doc(
            title="PDF type doc",
            doc_type=Document.DocType.PDF,
            file_s3_key="documents/99/original.pdf",
            mime_type="application/pdf",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?doc_type=PDF")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("PDF type doc", html)
        self.assertNotIn("Image type doc", html)

    def test_upload_status_filter_still_uses_document_field(self):
        self._create_ocr_doc(
            title="Uploaded status doc",
            upload_status=Document.UploadStatus.UPLOADED,
        )
        self._create_ocr_doc(
            title="Failed status doc",
            upload_status=Document.UploadStatus.FAILED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/?upload_status=FAILED")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Failed status doc", html)
        self.assertNotIn("Uploaded status doc", html)

    def test_review_backlog_q_title_search_uses_archive_item_title(self):
        doc = self._create_review_pending_doc()
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            title="Review backlog archive title unique",
        )
        ids = set(
            documents_in_review_backlog(
                q="Review backlog archive title unique"
            ).values_list("id", flat=True)
        )
        self.assertIn(doc.id, ids)

    def test_review_backlog_membership_unchanged_when_metadata_status_drifts(self):
        doc = self._create_review_pending_doc()
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        Document.objects.filter(pk=doc.pk).update(
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        ids = set(documents_in_review_backlog().values_list("id", flat=True))
        self.assertIn(doc.id, ids)

    def test_access_still_follows_archive_item_visibility(self):
        doc = create_ocr_document(
            title="Access drift doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            file_s3_key="documents/99/original.jpg",
            mime_type="image/jpeg",
        )
        ArchiveItem.objects.filter(pk=doc.archive_item_id).update(
            visibility=ArchiveItem.Visibility.PRIVATE
        )
        self.assertEqual(
            self.client.get(f"/api/ui/documents/{doc.id}/").status_code, 404
        )

    def test_ocr_edit_form_seed_reads_archive_item_shared_fields(self):
        doc = self._create_drifted_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(
            self.EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id)
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn('value="ArchiveItem-side title"', html)
        self.assertNotIn('value="Document-side title"', html)

    def test_manual_text_manage_list_unaffected(self):
        create_manual_text_archive_item(
            title="Manual text unchanged",
            body="Body text",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/archive/manage/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Manual text unchanged")
