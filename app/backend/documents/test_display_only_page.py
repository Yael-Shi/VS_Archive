"""Staff display-only (no OCR) page append for existing IMAGE OCR documents."""

from __future__ import annotations

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from documents.management.commands.run_worker import Command
from documents.models import (
    ArchiveItem,
    Document,
    DocumentSourceFile,
    DocumentTextResult,
    ProcessDocumentRequest,
)
from documents.services.archive_items import create_ocr_document
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedPageSource,
    build_arabic_printed_attempt_identity,
)
from documents.services.display_only_page_upload import (
    DisplayOnlyPageUploadError,
    is_display_only_page_add_eligible,
    prepare_display_only_page_upload,
)
from documents.services.gemini_engine import GeminiResult, gemini_transcription_contract
from documents.services.gemini_page_checkpoints import build_gemini_attempt_identity
from documents.services.htr_adapters.base import HtrResult
from documents.services.source_files import (
    MULTI_IMAGE_MAX_FILES,
    build_source_preview,
    get_ordered_source_files_for_processing,
    page_images_from_ocr_source_bytes,
    sync_primary_document_source_file,
)
from documents.s3 import S3HeadObjectResult


def _png_bytes(color=(255, 0, 0)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


def _gemini_identity(pages):
    contract = gemini_transcription_contract(
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        language_hint=Document.Language.ENGLISH,
        temperature=0.2,
    )
    return build_gemini_attempt_identity(
        pages=pages,
        language_hint=Document.Language.ENGLISH,
        text_input_type=Document.TextInputType.PRINTED,
        handwriting_type=Document.HandwritingType.VS,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        model_candidates=("model-a",),
        contract=contract,
        min_text_length=20,
        double_pass=False,
        consistency_min_ratio=0.85,
        temperature=0.2,
        top_k=40,
        top_p=0.95,
        max_output_tokens=8192,
        max_output_tokens_hard_cap=32768,
    )


def _arabic_identity(pages):
    sources = [
        ArabicPrintedPageSource(
            page_index=page.page_index - 1,
            mime_type=page.mime_type,
            source_identity=page.source_identity,
            source_content_fingerprint=page.source_content_fingerprint,
            oriented_image_sha256=page.source_content_fingerprint,
            oriented_image_width=4,
            oriented_image_height=4,
        )
        for page in pages
    ]
    return build_arabic_printed_attempt_identity(
        pages=sources,
        language_hint=Document.Language.ARABIC,
        text_input_type=Document.TextInputType.PRINTED,
        engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    )


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class DisplayOnlyPageUploadTests(TestCase):
    def setUp(self):
        from documents.test_exif_orientation import minimal_upright_jpeg_bytes

        self.jpeg_bytes = minimal_upright_jpeg_bytes()
        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)
        self.s3_get_patcher = patch(
            "documents.services.exif_orientation.get_object_bytes",
            return_value=(self.jpeg_bytes, "image/jpeg"),
        )
        self.s3_get_patcher.start()
        self.addCleanup(self.s3_get_patcher.stop)
        self.presign_patcher = patch(
            "documents.views.create_presigned_put",
            return_value="https://example/upload",
        )
        self.presign_patcher.start()
        self.addCleanup(self.presign_patcher.stop)

        self.staff = User.objects.create_user(
            username="display_only_staff",
            password="test-pass",
            is_staff=True,
        )
        self.plain = User.objects.create_user(
            username="display_only_user",
            password="test-pass",
            is_staff=False,
        )

    def _ready_multi_image_doc(self, *, count: int = 2) -> Document:
        doc = create_ocr_document(
            title="Ready multi-image",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            expected_source_file_count=count,
            file_s3_key=f"documents/pending/source/0.jpg",
            mime_type="image/jpeg",
            thumbnail_file_key="documents/pending/thumb_400.jpg",
        )
        doc.file_s3_key = f"documents/{doc.id}/source/0.jpg"
        doc.thumbnail_file_key = f"documents/{doc.id}/thumb_400.jpg"
        doc.save(update_fields=["file_s3_key", "thumbnail_file_key"])
        for order_index in range(count):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=order_index,
                file_s3_key=f"documents/{doc.id}/source/{order_index}.jpg",
                file_original_name=f"page-{order_index}.jpg",
                mime_type="image/jpeg",
                size_bytes=1000 + order_index,
                upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="existing source text",
        )
        return doc

    def _ready_legacy_image_doc(self) -> Document:
        doc = create_ocr_document(
            title="Ready legacy image",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/pending/original.jpg",
            mime_type="image/jpeg",
            file_original_name="original.jpg",
            size_bytes=2000,
            thumbnail_file_key="documents/pending/thumb_400.jpg",
        )
        doc.file_s3_key = f"documents/{doc.id}/original.jpg"
        doc.thumbnail_file_key = f"documents/{doc.id}/thumb_400.jpg"
        doc.save(update_fields=["file_s3_key", "thumbnail_file_key"])
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="legacy source text",
        )
        return doc

    def _post_add(self, doc_id: int, user=None, **overrides):
        payload = {
            "original_name": "extra.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1500,
        }
        payload.update(overrides)
        self.client.force_login(user or self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/display-only-pages/add/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_complete(self, doc_id: int, order_index: int, payload: dict, user=None):
        self.client.force_login(user or self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/display-only-pages/{order_index}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _add_and_complete(self, doc: Document) -> DocumentSourceFile:
        add = self._post_add(doc.id)
        self.assertEqual(add.status_code, 201, add.content)
        order_index = add.json()["order_index"]
        complete = self._post_complete(
            doc.id,
            order_index,
            {"success": True, "file_mime": "image/jpeg", "file_size": 1500},
        )
        self.assertEqual(complete.status_code, 200, complete.content)
        return DocumentSourceFile.objects.get(document=doc, order_index=order_index)

    def test_include_in_ocr_defaults_true_for_normal_source_files(self):
        doc = self._ready_multi_image_doc()
        source = doc.source_files.get(order_index=0)
        self.assertTrue(source.include_in_ocr)
        synced = sync_primary_document_source_file(doc)
        self.assertTrue(synced.include_in_ocr)

    def test_add_display_only_page_to_existing_multi_image_document(self):
        doc = self._ready_multi_image_doc(count=2)
        source = self._add_and_complete(doc)
        doc.refresh_from_db()
        self.assertEqual(source.order_index, 2)
        self.assertFalse(source.include_in_ocr)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.UPLOADED)
        self.assertEqual(doc.expected_source_file_count, 3)
        self.assertEqual(
            doc.processing_state_user, Document.ProcessingState.READY
        )
        self.assertEqual(
            DocumentTextResult.objects.filter(document=doc).get().text,
            "existing source text",
        )
        self.assertFalse(
            ProcessDocumentRequest.objects.filter(document=doc).exists()
        )
        self.assertEqual(doc.thumbnail_file_key, f"documents/{doc.id}/thumb_400.jpg")

    def test_add_display_only_page_to_legacy_single_image_document(self):
        doc = self._ready_legacy_image_doc()
        self.assertIsNone(doc.expected_source_file_count)
        source = self._add_and_complete(doc)
        doc.refresh_from_db()
        primary = doc.source_files.get(order_index=0)
        self.assertTrue(primary.include_in_ocr)
        self.assertEqual(primary.file_s3_key, doc.file_s3_key)
        self.assertEqual(source.order_index, 1)
        self.assertFalse(source.include_in_ocr)
        self.assertEqual(doc.expected_source_file_count, 2)
        self.assertEqual(
            doc.processing_state_user, Document.ProcessingState.READY
        )

    def test_display_only_page_visible_in_preview_and_public_display(self):
        doc = self._ready_multi_image_doc(count=2)
        self._add_and_complete(doc)
        with patch(
            "documents.services.source_files.create_presigned_get",
            side_effect=lambda **kw: f"https://example/{kw['key']}",
        ):
            preview = build_source_preview(doc, "test-bucket")
        self.assertEqual([item["order_index"] for item in preview.items], [0, 1, 2])
        self.assertFalse(preview.items[2]["include_in_ocr"])

        self.client.force_login(self.staff)
        staff_resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(staff_resp.status_code, 200)
        self.assertContains(staff_resp, "ללא תעתוק")
        self.assertContains(staff_resp, "הוספת עמוד ללא תעתוק")

        self.client.logout()
        self.client.force_login(self.plain)
        public_resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(public_resp.status_code, 200)
        self.assertContains(public_resp, "עמוד 3")
        self.assertNotContains(public_resp, "ללא תעתוק")
        self.assertNotContains(public_resp, "include_in_ocr")

    def test_failed_upload_does_not_fail_existing_document(self):
        doc = self._ready_multi_image_doc()
        add = self._post_add(doc.id)
        order_index = add.json()["order_index"]
        complete = self._post_complete(
            doc.id,
            order_index,
            {"success": False, "error": "client aborted"},
        )
        self.assertEqual(complete.status_code, 200)
        doc.refresh_from_db()
        source = DocumentSourceFile.objects.get(document=doc, order_index=order_index)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.FAILED)
        self.assertFalse(source.include_in_ocr)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)
        self.assertEqual(doc.expected_source_file_count, 2)

    def test_pdf_rejected(self):
        doc = create_ocr_document(
            title="PDF doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/x/original.pdf",
            mime_type="application/pdf",
        )
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "NOT_OCR_IMAGE")

    def test_non_ocr_item_rejected(self):
        doc = self._ready_legacy_image_doc()
        item = doc.archive_item
        item.item_type = ArchiveItem.ItemType.PHOTO
        item.save(update_fields=["item_type"])
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "NOT_OCR_IMAGE")

    def test_processing_and_recovery_required_rejected(self):
        doc = self._ready_multi_image_doc()
        doc.processing_state_user = Document.ProcessingState.PROCESSING
        doc.save(update_fields=["processing_state_user"])
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DOCUMENT_BUSY")

        doc.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
        doc.save(update_fields=["processing_state_user"])
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "DOCUMENT_BUSY")

    def test_active_process_document_request_rejected(self):
        doc = self._ready_multi_image_doc()
        ProcessDocumentRequest.objects.create(
            document=doc,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()["code"], "ACTIVE_REQUEST")

    def test_source_file_cap_rejected(self):
        doc = self._ready_multi_image_doc(count=2)
        doc.expected_source_file_count = MULTI_IMAGE_MAX_FILES
        doc.save(update_fields=["expected_source_file_count"])
        resp = self._post_add(doc.id)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["code"], "SOURCE_FILE_LIMIT")

    def test_unauthorized_request_rejected(self):
        doc = self._ready_multi_image_doc()
        resp = self._post_add(doc.id, user=self.plain)
        self.assertEqual(resp.status_code, 403)

    def test_existing_incremental_parts_add_remains_draft_only(self):
        doc = self._ready_multi_image_doc()
        self.client.force_login(self.staff)
        resp = self.client.post(
            f"/api/uploads/{doc.id}/parts/add/",
            data=json.dumps(
                {
                    "original_name": "nope.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 100,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("incremental", resp.json()["error"])

    def test_staff_get_page_renders_copy(self):
        doc = self._ready_multi_image_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/display-only-page/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הוספת עמוד ללא תעתוק")
        self.assertContains(resp, "לא")
        self.assertContains(resp, "לתעתוק")

    def test_existing_finalize_and_ocr_reprocess_routes_still_resolve(self):
        self.assertEqual(
            reverse("uploads-finalize", kwargs={"doc_id": 7}),
            "/api/uploads/7/finalize/",
        )
        self.assertEqual(
            reverse("documents-ocr-reprocess", kwargs={"doc_id": 7}),
            "/api/ui/documents/7/ocr-reprocess/",
        )
        self.assertEqual(
            reverse("uploads-display-only-page-add", kwargs={"doc_id": 7}),
            "/api/uploads/7/display-only-pages/add/",
        )
        self.assertEqual(
            reverse(
                "uploads-display-only-page-complete",
                kwargs={"doc_id": 7, "order_index": 3},
            ),
            "/api/uploads/7/display-only-pages/3/complete/",
        )
        self.assertEqual(
            reverse("documents-display-only-page-add", kwargs={"doc_id": 7}),
            "/api/ui/documents/7/display-only-page/",
        )


class DisplayOnlyPageOcrPathTests(TestCase):
    def _png_doc_with_display_only(self) -> Document:
        doc = create_ocr_document(
            title="OCR filter doc",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            expected_source_file_count=3,
            file_s3_key="documents/x/source/0.png",
            mime_type="image/png",
        )
        doc.file_s3_key = f"documents/{doc.id}/source/0.png"
        doc.save(update_fields=["file_s3_key"])
        for order_index in range(3):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=order_index,
                file_s3_key=f"documents/{doc.id}/source/{order_index}.png",
                file_original_name=f"page-{order_index}.png",
                mime_type="image/png",
                size_bytes=100,
                upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
                include_in_ocr=order_index < 2,
            )
        return doc

    def test_ocr_page_images_are_contiguous_and_omit_display_only(self):
        doc = self._png_doc_with_display_only()
        ocr_sources = get_ordered_source_files_for_processing(doc)
        self.assertEqual([s.order_index for s in ocr_sources], [0, 1])
        loaded = [(source, _png_bytes()) for source in ocr_sources]
        pages = page_images_from_ocr_source_bytes(doc, loaded)
        self.assertEqual([page.page_index for page in pages], [1, 2])
        self.assertEqual(len(pages), 2)

    def test_gemini_and_arabic_identity_ignore_display_only_pages(self):
        doc = self._png_doc_with_display_only()
        ocr_sources = get_ordered_source_files_for_processing(doc)
        ocr_pages = page_images_from_ocr_source_bytes(
            doc, [(source, _png_bytes((1, 2, 3))) for source in ocr_sources]
        )
        all_sources = list(doc.source_files.order_by("order_index"))
        unfiltered_pages = page_images_from_ocr_source_bytes(
            doc, [(source, _png_bytes((1, 2, 3))) for source in all_sources]
        )
        gemini_ocr = _gemini_identity(ocr_pages)
        gemini_all = _gemini_identity(unfiltered_pages)
        self.assertEqual(gemini_ocr.expected_page_count, 2)
        self.assertNotEqual(
            gemini_ocr.identity_fingerprint, gemini_all.identity_fingerprint
        )
        self.assertEqual(_arabic_identity(ocr_pages).expected_page_count, 2)
        self.assertNotEqual(
            _arabic_identity(ocr_pages).identity_fingerprint,
            _arabic_identity(unfiltered_pages).identity_fingerprint,
        )

        two_page = create_ocr_document(
            title="Identity before append",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            expected_source_file_count=2,
            file_s3_key="documents/id/source/0.png",
            mime_type="image/png",
        )
        two_page.file_s3_key = f"documents/{two_page.id}/source/0.png"
        two_page.save(update_fields=["file_s3_key"])
        page_bytes = [_png_bytes((1, 2, 3)), _png_bytes((4, 5, 6))]
        for order_index, color_bytes in enumerate(page_bytes):
            DocumentSourceFile.objects.create(
                document=two_page,
                order_index=order_index,
                file_s3_key=f"documents/{two_page.id}/source/{order_index}.png",
                file_original_name=f"page-{order_index}.png",
                mime_type="image/png",
                size_bytes=len(color_bytes),
                upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            )
        before_sources = get_ordered_source_files_for_processing(two_page)
        before_pages = page_images_from_ocr_source_bytes(
            two_page, list(zip(before_sources, page_bytes))
        )
        before_gemini = _gemini_identity(before_pages)
        before_arabic = _arabic_identity(before_pages)
        DocumentSourceFile.objects.create(
            document=two_page,
            order_index=2,
            file_s3_key=f"documents/{two_page.id}/source/2.png",
            file_original_name="display-only.png",
            mime_type="image/png",
            size_bytes=100,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            include_in_ocr=False,
        )
        two_page.expected_source_file_count = 3
        two_page.save(update_fields=["expected_source_file_count"])
        after_sources = get_ordered_source_files_for_processing(two_page)
        after_pages = page_images_from_ocr_source_bytes(
            two_page, list(zip(after_sources, page_bytes))
        )
        self.assertEqual(
            _gemini_identity(after_pages).identity_fingerprint,
            before_gemini.identity_fingerprint,
        )
        self.assertEqual(
            _arabic_identity(after_pages).identity_fingerprint,
            before_arabic.identity_fingerprint,
        )

    def test_legacy_identity_preserved_when_only_display_only_pages_are_appended(self):
        doc = create_ocr_document(
            title="Legacy identity",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/legacy/original.jpg",
            mime_type="image/jpeg",
        )
        doc.file_s3_key = f"documents/{doc.id}/original.jpg"
        doc.save(update_fields=["file_s3_key"])
        primary = sync_primary_document_source_file(doc)
        png = _png_bytes()
        from documents.services.page_extraction import source_file_bytes_to_page
        from dataclasses import replace as dc_replace

        legacy_page = dc_replace(
            source_file_bytes_to_page(0, png, "image/png"),
            source_identity=doc.file_s3_key,
            source_content_fingerprint="a" * 64,
        )
        # After append, expected_count=2 with one display-only file.
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=1,
            file_s3_key=f"documents/{doc.id}/source/1.png",
            file_original_name="extra.png",
            mime_type="image/png",
            size_bytes=100,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
            include_in_ocr=False,
        )
        doc.expected_source_file_count = 2
        doc.save(update_fields=["expected_source_file_count"])
        ocr_sources = get_ordered_source_files_for_processing(doc)
        self.assertEqual(list(ocr_sources), [primary])
        pages = page_images_from_ocr_source_bytes(doc, [(primary, png)])
        self.assertEqual(pages[0].source_identity, doc.file_s3_key)
        self.assertEqual(pages[0].page_index, 1)
        self.assertEqual(_gemini_identity(pages).expected_page_count, 1)
        self.assertEqual(legacy_page.page_index, 1)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch(
        "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini"
    )
    def test_worker_and_transkribus_path_omit_display_only_pages(
        self, mock_translate, mock_transcribe, mock_get_object_bytes
    ):
        doc = self._png_doc_with_display_only()
        mock_get_object_bytes.side_effect = lambda bucket, key: (_png_bytes(), "image/png")
        mock_transcribe.return_value = HtrResult(
            text="combined text",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name="gemini-2.0-flash",
        )
        command = Command()
        command._cfg = SimpleNamespace(
            min_text_length=5,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.85,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=8192,
        )
        msg = {"Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": doc.id})}
        self.assertTrue(command._process_message(msg))
        mock_transcribe.assert_called_once()
        read_keys = [call.kwargs["key"] for call in mock_get_object_bytes.call_args_list]
        self.assertEqual(
            read_keys,
            [
                f"documents/{doc.id}/source/0.png",
                f"documents/{doc.id}/source/1.png",
            ],
        )
        pages = mock_transcribe.call_args.kwargs["pages"]
        self.assertEqual([page.page_index for page in pages], [1, 2])
        self.assertEqual(len(pages), 2)

    def test_prepare_is_not_eligible_during_initial_upload(self):
        doc = create_ocr_document(
            title="Draft",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADING,
        )
        self.assertFalse(is_display_only_page_add_eligible(doc))
        with self.assertRaises(DisplayOnlyPageUploadError):
            prepare_display_only_page_upload(
                document=doc,
                original_name="x.jpg",
                mime_type="image/jpeg",
                size_bytes=1,
            )
