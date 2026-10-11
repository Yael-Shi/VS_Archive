"""Contract tests for Pilot 01 transcription-review agent read APIs."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings

from documents.models import (
    ArchiveItem,
    Document,
    DocumentSourceFile,
    DocumentTextResult,
    TranscriptionEditSuggestion,
)
from documents.services.archive_item_access import VIEW_RESTRICTED_ARCHIVEITEM_CODENAME
from documents.services.archive_items import create_ocr_document
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.transcription_review_agent_read import (
    CODE_INCOMPLETE_MULTI_IMAGE_SOURCES,
    CODE_MISSING_DISPLAYED_TRANSCRIPTION,
    CODE_MISSING_PRIMARY_SOURCE,
    CODE_MISSING_SOURCE_FILE,
    CODE_NOT_OCR_DOCUMENT,
    CODE_NOT_READY_PROCESSING,
    CODE_SOURCE_URL_UNAVAILABLE,
    CODE_UNSUPPORTED_SOURCE_MIME,
    CODE_UPLOADED_DISPLAY_ONLY_SOURCE_MISSING_KEY,
    CODE_VERIFIED_TRANSCRIPTION_OUT_OF_SCOPE,
    OUTCOME_BLOCKING,
    OUTCOME_NON_ELIGIBLE,
    OUTCOME_REVIEWABLE,
    build_review_bundle_for_document,
)

SCAN_URL = "/api/agent/v1/transcription-review/documents/scan"
BUNDLE_URL = "/api/agent/v1/transcription-review/documents/{document_id}/review-bundle"

PRESIGN_URL = "https://example.test/presigned"


def _bundle_url(document_id: int) -> str:
    return BUNDLE_URL.format(document_id=document_id)


class TranscriptionReviewAgentApiTests(TestCase):
    def _create_doc(self, **kwargs) -> Document:
        defaults = {
            "title": "Pilot EN doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.PRINTED,
            "language": Document.Language.ENGLISH,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": Document.MetadataStatus.COMPLETED,
            "file_s3_key": "documents/pilot/original.jpg",
            "mime_type": "image/jpeg",
            "file_original_name": "original.jpg",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _add_text_result(
        self,
        doc: Document,
        *,
        text: str = "hello transcription",
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        status=DocumentTextResult.Status.SUCCEEDED,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        engine: str = "engine_v1",
        source_revision: int = 1,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=engine_key,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=status,
            verification_status=verification_status,
            text=text,
            source_revision=source_revision,
        )

    def _add_source(
        self,
        doc: Document,
        order_index: int,
        *,
        upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        mime_type: str = "image/png",
        include_in_ocr: bool = True,
        file_s3_key: str | None = None,
    ) -> DocumentSourceFile:
        key = (
            f"documents/{doc.id}/source/{order_index}.png"
            if file_s3_key is None
            else file_s3_key
        )
        return DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=key,
            file_original_name=f"page-{order_index}.png",
            mime_type=mime_type,
            upload_status=upload_status,
            include_in_ocr=include_in_ocr,
            size_bytes=100 + order_index,
        )

    def _write_counts(self) -> dict[str, int]:
        return {
            "documents": Document.objects.count(),
            "text_results": DocumentTextResult.objects.count(),
            "source_files": DocumentSourceFile.objects.count(),
            "suggestions": TranscriptionEditSuggestion.objects.count(),
        }

    def test_scan_ordering_cursor_limit_languages_visibility_exhaustion(self):
        he = self._create_doc(
            title="Hebrew",
            language=Document.Language.HEBREW,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private_en = self._create_doc(
            title="Private EN",
            language=Document.Language.ENGLISH,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        en1 = self._create_doc(title="EN 1", language=Document.Language.ENGLISH)
        fr1 = self._create_doc(title="FR 1", language=Document.Language.FRENCH)
        en2 = self._create_doc(title="EN 2", language=Document.Language.ENGLISH)
        # Broken sources / no transcription still appear as discovery candidates.
        broken = self._create_doc(
            title="Broken EN",
            language=Document.Language.ENGLISH,
            file_s3_key="",
            expected_source_file_count=3,
        )

        resp = self.client.get(SCAN_URL, {"limit": 2})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["schema_version"], "transcription-review-scan-v1")
        self.assertEqual(body["after_id"], 0)
        self.assertEqual(body["limit"], 2)
        self.assertFalse(body["exhausted"])
        ids = [item["document_id"] for item in body["items"]]
        self.assertEqual(ids, sorted(ids))
        self.assertNotIn(he.id, ids)
        self.assertNotIn(private_en.id, ids)
        self.assertEqual(len(ids), 2)
        self.assertEqual(body["next_after_id"], ids[-1])

        resp2 = self.client.get(
            SCAN_URL, {"after_id": body["next_after_id"], "limit": 50}
        )
        body2 = resp2.json()
        ids2 = [item["document_id"] for item in body2["items"]]
        self.assertTrue(set(ids).isdisjoint(ids2))
        self.assertIn(broken.id, ids + ids2)
        self.assertIn(en1.id, ids + ids2)
        self.assertIn(fr1.id, ids + ids2)
        self.assertIn(en2.id, ids + ids2)

        last = max(ids + ids2)
        resp3 = self.client.get(SCAN_URL, {"after_id": last, "limit": 50})
        body3 = resp3.json()
        self.assertTrue(body3["exhausted"])
        self.assertEqual(body3["items"], [])
        self.assertIsNone(body3["next_after_id"])

    def test_scan_invalid_query_400(self):
        resp = self.client.get(SCAN_URL, {"limit": "0"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_limit")

        resp = self.client.get(SCAN_URL, {"limit": "201"})
        self.assertEqual(resp.status_code, 400)

        resp = self.client.get(SCAN_URL, {"after_id": "-1"})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "invalid_after_id")

        resp = self.client.get(SCAN_URL, {"after_id": "x"})
        self.assertEqual(resp.status_code, 400)

    def test_scan_does_not_require_transcription_or_intact_sources(self):
        doc = self._create_doc(
            language=Document.Language.ENGLISH,
            file_s3_key="",
            expected_source_file_count=2,
        )
        self._add_source(doc, 0, upload_status=DocumentSourceFile.UploadStatus.PENDING)
        resp = self.client.get(SCAN_URL)
        self.assertEqual(resp.status_code, 200)
        ids = [item["document_id"] for item in resp.json()["items"]]
        self.assertIn(doc.id, ids)

    def test_scan_excludes_non_ocr_document_bundle_guard_remains(self):
        ocr = self._create_doc(title="OCR EN")
        drifted = self._create_doc(
            title="Drifted EN non-OCR item",
            language=Document.Language.ENGLISH,
        )
        ArchiveItem.objects.filter(pk=drifted.archive_item_id).update(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT
        )

        scan_ids = {
            item["document_id"] for item in self.client.get(SCAN_URL).json()["items"]
        }
        self.assertIn(ocr.id, scan_ids)
        self.assertNotIn(drifted.id, scan_ids)

        # Access helpers fail closed for non-OCR-linked Documents over HTTP.
        self.assertEqual(self.client.get(_bundle_url(drifted.id)).status_code, 404)

        # Bundle classifier still guards NOT_OCR_DOCUMENT if a Document is loaded.
        drifted = Document.objects.select_related("archive_item").get(pk=drifted.pk)
        body = build_review_bundle_for_document(drifted)
        self.assertEqual(body["outcome"], OUTCOME_NON_ELIGIBLE)
        self.assertEqual(body["error"]["code"], CODE_NOT_OCR_DOCUMENT)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_reviewable_single_image_uses_displayed_transcription(self, mock_presign):
        doc = self._create_doc(language=Document.Language.ENGLISH)
        older = self._add_text_result(
            doc,
            text="older",
            engine="older-engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
        )
        newer = self._add_text_result(
            doc,
            text="newer displayed",
            engine="newer-engine",
            status=DocumentTextResult.Status.SUCCEEDED,
        )
        # Force created_at ordering: bump older to be older than newer by re-saving
        # via update is enough when newer was created second.
        DocumentTextResult.objects.filter(pk=older.pk).update(text="older")

        before = self._write_counts()
        resp = self.client.get(_bundle_url(doc.id))
        after = self._write_counts()
        self.assertEqual(before, after)

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_REVIEWABLE)
        self.assertEqual(body["transcription"]["text_result_id"], newer.id)
        self.assertEqual(body["transcription"]["text"], "newer displayed")
        self.assertEqual(
            body["transcription"]["text_sha256"],
            source_text_sha256("newer displayed"),
        )
        self.assertEqual(len(body["source_pages"]), 1)
        page = body["source_pages"][0]
        self.assertEqual(page["source_kind"], "PRIMARY_FILE")
        self.assertEqual(page["file_s3_key"], doc.file_s3_key)
        self.assertEqual(page["download_url"], PRESIGN_URL)
        self.assertTrue(mock_presign.called)
        self.assertEqual(
            body["provenance"]["displayed_transcription_resolver"],
            "resolve_displayed_transcription_result",
        )

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_reviewable_multi_image_includes_display_only_extras(self, mock_presign):
        doc = self._create_doc(
            language=Document.Language.FRENCH,
            expected_source_file_count=2,
            file_s3_key="documents/x/source/0.png",
            mime_type="image/png",
        )
        self._add_text_result(doc, text="page text fr")
        s0 = self._add_source(doc, 0)
        s1 = self._add_source(doc, 1)
        s2 = self._add_source(doc, 2, include_in_ocr=False)

        resp = self.client.get(_bundle_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_REVIEWABLE)
        pages = body["source_pages"]
        self.assertEqual([p["order_index"] for p in pages], [0, 1, 2])
        self.assertEqual(pages[0]["source_file_id"], s0.id)
        self.assertEqual(pages[1]["source_file_id"], s1.id)
        self.assertEqual(pages[2]["source_file_id"], s2.id)
        self.assertFalse(pages[2]["include_in_ocr"])
        self.assertEqual(body["provenance"]["source_set"], "display_physical")
        self.assertEqual(mock_presign.call_count, 3)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_reviewable_pdf_without_rasterization(self, mock_presign):
        doc = self._create_doc(
            title="PDF FR",
            language=Document.Language.FRENCH,
            doc_type=Document.DocType.PDF,
            file_s3_key="documents/pdf/original.pdf",
            mime_type="application/pdf",
            expected_source_file_count=None,
        )
        self._add_text_result(doc, text="pdf text")

        resp = self.client.get(_bundle_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_REVIEWABLE)
        self.assertEqual(len(body["source_pages"]), 1)
        page = body["source_pages"][0]
        self.assertEqual(page["source_kind"], "PRIMARY_FILE")
        self.assertEqual(page["mime_type"], "application/pdf")
        self.assertEqual(page["file_s3_key"], "documents/pdf/original.pdf")
        self.assertEqual(body["provenance"]["source_set"], "primary_file")
        self.assertEqual(mock_presign.call_count, 1)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_verified_transcription_non_eligible_no_presign(self, mock_presign):
        doc = self._create_doc()
        row = self._add_text_result(
            doc,
            text="verified text",
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )

        resp = self.client.get(_bundle_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_NON_ELIGIBLE)
        self.assertEqual(
            body["error"]["code"], CODE_VERIFIED_TRANSCRIPTION_OUT_OF_SCOPE
        )
        self.assertEqual(body["transcription"]["text_result_id"], row.id)
        self.assertEqual(body["source_pages"], [])
        mock_presign.assert_not_called()

    def test_processing_and_recovery_without_text_non_eligible(self):
        processing = self._create_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        recovery = self._create_doc(
            title="Recovery",
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
        )

        for doc, _label in ((processing, "PROCESSING"), (recovery, "RECOVERY")):
            resp = self.client.get(_bundle_url(doc.id))
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["outcome"], OUTCOME_NON_ELIGIBLE)
            self.assertEqual(body["error"]["code"], CODE_NOT_READY_PROCESSING)

    def test_failed_and_ready_without_text_blocking(self):
        failed = self._create_doc(
            processing_state_user=Document.ProcessingState.FAILED,
        )
        ready = self._create_doc(
            title="Ready empty",
            processing_state_user=Document.ProcessingState.READY,
        )

        for doc in (failed, ready):
            resp = self.client.get(_bundle_url(doc.id))
            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
            self.assertEqual(
                body["error"]["code"], CODE_MISSING_DISPLAYED_TRANSCRIPTION
            )

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_uploaded_display_only_extra_empty_key_blocking(self, mock_presign):
        doc = self._create_doc(
            title="Display-only empty key",
            language=Document.Language.ENGLISH,
            expected_source_file_count=2,
            file_s3_key="documents/x/source/0.png",
            mime_type="image/png",
        )
        self._add_text_result(doc, text="ok")
        self._add_source(doc, 0)
        self._add_source(doc, 1)
        self._add_source(
            doc,
            2,
            include_in_ocr=False,
            file_s3_key="",
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )

        resp = self.client.get(_bundle_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(
            body["error"]["code"], CODE_UPLOADED_DISPLAY_ONLY_SOURCE_MISSING_KEY
        )
        self.assertEqual(body["error"]["details"]["order_index"], 2)
        self.assertEqual(body["source_pages"], [])
        mock_presign.assert_not_called()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_incomplete_multi_image_sources_blocking(self, mock_presign):
        missing_row = self._create_doc(
            title="Missing row",
            expected_source_file_count=2,
            file_s3_key="documents/m/0.png",
            mime_type="image/png",
        )
        self._add_text_result(missing_row, text="t")
        self._add_source(missing_row, 0)

        pending = self._create_doc(
            title="Pending part",
            expected_source_file_count=2,
            file_s3_key="documents/p/0.png",
            mime_type="image/png",
        )
        self._add_text_result(pending, text="t")
        self._add_source(pending, 0)
        self._add_source(
            pending, 1, upload_status=DocumentSourceFile.UploadStatus.PENDING
        )

        failed_part = self._create_doc(
            title="Failed part",
            expected_source_file_count=2,
            file_s3_key="documents/f/0.png",
            mime_type="image/png",
        )
        self._add_text_result(failed_part, text="t")
        self._add_source(failed_part, 0)
        self._add_source(
            failed_part, 1, upload_status=DocumentSourceFile.UploadStatus.FAILED
        )

        resp = self.client.get(_bundle_url(missing_row.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_MISSING_SOURCE_FILE)
        mock_presign.assert_not_called()

        resp = self.client.get(_bundle_url(pending.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_INCOMPLETE_MULTI_IMAGE_SOURCES)

        resp = self.client.get(_bundle_url(failed_part.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_INCOMPLETE_MULTI_IMAGE_SOURCES)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_missing_primary_source_blocking(self, mock_presign):
        doc = self._create_doc(file_s3_key="")
        self._add_text_result(doc, text="t")
        resp = self.client.get(_bundle_url(doc.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_MISSING_PRIMARY_SOURCE)
        mock_presign.assert_not_called()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_unsupported_mime_blocking(self, mock_presign):
        single = self._create_doc(
            mime_type="application/zip",
            file_s3_key="documents/z/file.zip",
        )
        self._add_text_result(single, text="t")
        resp = self.client.get(_bundle_url(single.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_UNSUPPORTED_SOURCE_MIME)
        mock_presign.assert_not_called()

        multi = self._create_doc(
            title="Multi bad mime",
            expected_source_file_count=1,
        )
        # Force multi-image path: expected_source_file_count >= 2
        multi.expected_source_file_count = 2
        multi.save(update_fields=["expected_source_file_count"])
        self._add_text_result(multi, text="t")
        self._add_source(multi, 0)
        self._add_source(multi, 1, mime_type="application/zip")

        resp = self.client.get(_bundle_url(multi.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_UNSUPPORTED_SOURCE_MIME)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        side_effect=RuntimeError("presign boom"),
    )
    def test_presign_failure_blocking(self, _mock_presign):
        doc = self._create_doc()
        self._add_text_result(doc, text="t")
        resp = self.client.get(_bundle_url(doc.id))
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_BLOCKING)
        self.assertEqual(body["error"]["code"], CODE_SOURCE_URL_UNAVAILABLE)

    def test_private_and_restricted_not_viewable_404(self):
        private = self._create_doc(visibility=ArchiveItem.Visibility.PRIVATE)
        restricted = self._create_doc(
            title="Restricted",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self._add_text_result(private, text="t")
        self._add_text_result(restricted, text="t")

        self.assertEqual(self.client.get(_bundle_url(private.id)).status_code, 404)
        self.assertEqual(self.client.get(_bundle_url(restricted.id)).status_code, 404)
        self.assertEqual(self.client.get(_bundle_url(999999)).status_code, 404)

        scan = self.client.get(SCAN_URL).json()
        scan_ids = {item["document_id"] for item in scan["items"]}
        self.assertNotIn(private.id, scan_ids)
        self.assertNotIn(restricted.id, scan_ids)

    def test_language_change_race_becomes_non_eligible(self):
        doc = self._create_doc(language=Document.Language.ENGLISH)
        self._add_text_result(doc, text="t")
        scan_ids = {
            item["document_id"] for item in self.client.get(SCAN_URL).json()["items"]
        }
        self.assertIn(doc.id, scan_ids)

        doc.language = Document.Language.HEBREW
        doc.save(update_fields=["language"])

        resp = self.client.get(_bundle_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["outcome"], OUTCOME_NON_ELIGIBLE)
        self.assertEqual(body["error"]["code"], "LANGUAGE_NOT_IN_SCOPE")

    def test_delete_race_returns_404(self):
        doc = self._create_doc()
        doc_id = doc.id
        self.assertIn(
            doc_id,
            {item["document_id"] for item in self.client.get(SCAN_URL).json()["items"]},
        )
        doc.delete()
        self.assertEqual(self.client.get(_bundle_url(doc_id)).status_code, 404)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.services.transcription_review_agent_read.create_presigned_get",
        return_value=PRESIGN_URL,
    )
    def test_gets_perform_no_writes(self, _mock_presign):
        doc = self._create_doc()
        self._add_text_result(doc, text="no write")
        before = self._write_counts()
        self.client.get(SCAN_URL)
        self.client.get(_bundle_url(doc.id))
        after = self._write_counts()
        self.assertEqual(before, after)

    def test_staff_can_see_private_in_scan_but_pilot_anonymous_path_is_public(self):
        """Staff visibility still uses helpers; Pilot 01 clients are anonymous."""
        private = self._create_doc(visibility=ArchiveItem.Visibility.PRIVATE)
        public = self._create_doc(title="Public EN")
        staff = User.objects.create_user(
            username="tr_agent_staff", password="x", is_staff=True
        )

        anon = self.client.get(SCAN_URL).json()
        anon_ids = {item["document_id"] for item in anon["items"]}
        self.assertIn(public.id, anon_ids)
        self.assertNotIn(private.id, anon_ids)

        self.client.force_login(staff)
        staff_scan = self.client.get(SCAN_URL).json()
        staff_ids = {item["document_id"] for item in staff_scan["items"]}
        self.assertIn(private.id, staff_ids)

    def test_restricted_with_permission_is_viewable(self):
        restricted = self._create_doc(
            title="Restricted EN",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self._add_text_result(restricted, text="secret")
        user = User.objects.create_user(username="restricted_viewer", password="x")
        perm = Permission.objects.get(
            codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
            content_type=ContentType.objects.get_for_model(ArchiveItem),
        )
        user.user_permissions.add(perm)
        self.client.force_login(user)

        scan_ids = {
            item["document_id"] for item in self.client.get(SCAN_URL).json()["items"]
        }
        self.assertIn(restricted.id, scan_ids)

        with override_settings(UPLOADS_BUCKET_NAME="test-bucket"):
            with patch(
                "documents.services.transcription_review_agent_read.create_presigned_get",
                return_value=PRESIGN_URL,
            ):
                resp = self.client.get(_bundle_url(restricted.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["outcome"], OUTCOME_REVIEWABLE)
