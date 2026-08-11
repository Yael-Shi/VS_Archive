"""Combined pending verify (save-if-changed) and async review mutation responses."""

from __future__ import annotations

import json
from unittest.mock import patch

from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
)
from documents.services.archive_items import create_ocr_document
from documents.services.verified_text_result_edit import (
    is_hebrew_translation_stale,
    verify_pending_text_result,
)


def _async_headers() -> dict[str, str]:
    return {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@override_settings(UPLOADS_BUCKET_NAME="")
class ReviewCombinedVerifyAndAsyncTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="async_review_staff",
            password="test-pass",
            is_staff=True,
        )
        self.staff_restricted = User.objects.create_user(
            username="async_review_staff_restricted",
            password="test-pass",
            is_staff=True,
        )
        ct = ContentType.objects.get_for_model(ArchiveItem)
        perm = Permission.objects.get(
            content_type=ct,
            codename="view_restricted_archiveitem",
        )
        self.staff_restricted.user_permissions.add(perm)

    def _create_english_doc(self, **kwargs) -> Document:
        defaults = {
            "title": "EN combined verify",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.PRINTED,
            "language": Document.Language.ENGLISH,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "file_s3_key": "documents/async-en/original.jpg",
            "mime_type": "image/jpeg",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_hebrew_doc(self, **kwargs) -> Document:
        defaults = {
            "title": "HE combined verify",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "file_s3_key": "documents/async-he/original.jpg",
            "mime_type": "image/jpeg",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_pending(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-async",
        source_revision: int = 1,
        based_on_source_revision: int | None = None,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            source_revision=source_revision,
            based_on_source_revision=based_on_source_revision,
        )

    def _verify_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-verify",
            kwargs={"result_id": result_id},
        )

    def _reject_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-reject",
            kwargs={"result_id": result_id},
        )

    def _save_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-update-text",
            kwargs={"result_id": result_id},
        )

    def test_combined_verify_changed_text_persists_pending_semantics_and_verifies(
        self,
    ):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before",
            source_revision=2,
        )
        submitted = "  after edit  \n"

        outcome = verify_pending_text_result(
            result_id=source.id,
            new_text=submitted,
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertTrue(outcome.text_saved)
        self.assertEqual(source.text, submitted)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(source.source_revision, 3)
        audit = DocumentTextResultEdit.objects.get(text_result=source)
        self.assertEqual(audit.old_text, "before")
        self.assertEqual(audit.new_text, submitted)
        self.assertEqual(audit.editor_id, self.staff.id)

    def test_combined_verify_unchanged_text_verifies_without_edit_audit(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="stable text",
            source_revision=4,
        )
        updated_at_before = source.updated_at

        outcome = verify_pending_text_result(
            result_id=source.id,
            new_text="  stable text  \n",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertFalse(outcome.text_saved)
        self.assertEqual(source.text, "stable text")
        self.assertEqual(source.source_revision, 4)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)
        self.assertNotEqual(source.updated_at, updated_at_before)

    def test_index_sync_failure_rolls_back_text_and_verification(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before",
            source_revision=1,
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index boom"),
        ):
            with self.assertRaises(DatabaseError):
                verify_pending_text_result(
                    result_id=source.id,
                    new_text="changed for index fail",
                    editor=self.staff,
                )

        source.refresh_from_db()
        self.assertEqual(source.text, "before")
        self.assertEqual(source.source_revision, 1)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_hebrew_document_mirror_combined_verify(self):
        doc = self._create_hebrew_doc()
        engine = "engine-he-mirror"
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מקור",
            engine=engine,
            source_revision=1,
        )
        hebrew = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="מקור",
            engine=engine,
            based_on_source_revision=1,
        )

        outcome = verify_pending_text_result(
            result_id=source.id,
            new_text="מקור מתוקן",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertTrue(outcome.text_saved)
        self.assertEqual(source.text, "מקור מתוקן")
        self.assertEqual(hebrew.text, "מקור מתוקן")
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)

    def test_non_hebrew_source_verify_marks_translation_stale(self):
        doc = self._create_english_doc()
        engine = "engine-stale"
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            engine=engine,
            source_revision=3,
        )
        hebrew = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום ישן",
            engine=engine,
            based_on_source_revision=3,
        )

        verify_pending_text_result(
            result_id=source.id,
            new_text="English source revised",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(hebrew.text, "תרגום ישן")
        self.assertTrue(is_hebrew_translation_stale(hebrew, source))
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_hebrew_text_verify_sets_based_on_source_revision(self):
        doc = self._create_english_doc()
        engine = "engine-he-edit"
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Source stays",
            engine=engine,
            source_revision=5,
        )
        hebrew = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום ישן",
            engine=engine,
            based_on_source_revision=4,
        )

        verify_pending_text_result(
            result_id=hebrew.id,
            new_text="תרגום מעודכן",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "Source stays")
        self.assertEqual(source.source_revision, 5)
        self.assertEqual(hebrew.text, "תרגום מעודכן")
        self.assertEqual(hebrew.based_on_source_revision, 5)
        self.assertFalse(is_hebrew_translation_stale(hebrew, source))
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_restricted_verify_404_before_mutation(self):
        doc = self._create_hebrew_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        row = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="restricted secret",
        )
        self.client.force_login(self.staff)
        with patch("documents.views.verify_pending_text_result") as mock_verify:
            resp = self.client.post(
                self._verify_url(row.id),
                {"text": "restricted secret"},
            )
            self.assertEqual(resp.status_code, 404)
            mock_verify.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_restricted_verify_succeeds_with_permission(self):
        doc = self._create_hebrew_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        row = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="restricted secret",
        )
        self.client.force_login(self.staff_restricted)
        resp = self.client.post(
            self._verify_url(row.id),
            {"text": "restricted secret"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"],
            reverse("review-detail-page", kwargs={"doc_id": doc.id}),
        )
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_ordinary_post_paths_still_redirect(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="redirect me",
        )
        detail = reverse("review-detail-page", kwargs={"doc_id": doc.id})
        self.client.force_login(self.staff)

        save = self.client.post(self._save_url(source.id), {"text": "redirect me"})
        self.assertEqual(save.status_code, 302)
        self.assertEqual(save["Location"], detail)

        verify = self.client.post(
            self._verify_url(source.id),
            {"text": "redirect me"},
        )
        self.assertEqual(verify.status_code, 302)
        self.assertEqual(verify["Location"], detail)

        # Recreate pending row for reject redirect check.
        rejected = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="reject redirect",
            engine="engine-reject-redirect",
        )
        reject = self.client.post(self._reject_url(rejected.id))
        self.assertEqual(reject.status_code, 302)
        self.assertEqual(reject["Location"], detail)

    def test_async_save_verify_reject_json_contract(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="async original",
            source_revision=1,
        )
        self.client.force_login(self.staff)

        save = self.client.post(
            self._save_url(source.id),
            {"text": "async saved"},
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        save_body = json.loads(save.content)
        self.assertEqual(
            save_body,
            {
                "ok": True,
                "action": "save",
                "result_id": source.id,
                "document_id": doc.id,
                "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                "text_saved": True,
            },
        )

        verify = self.client.post(
            self._verify_url(source.id),
            {"text": "async saved"},
            **_async_headers(),
        )
        self.assertEqual(verify.status_code, 200)
        verify_body = json.loads(verify.content)
        self.assertEqual(verify_body["ok"], True)
        self.assertEqual(verify_body["action"], "verify")
        self.assertEqual(verify_body["result_id"], source.id)
        self.assertEqual(verify_body["document_id"], doc.id)
        self.assertEqual(
            verify_body["verification_status"],
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertFalse(verify_body["text_saved"])

        pending = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="async reject",
            engine="engine-async-reject",
        )
        reject = self.client.post(
            self._reject_url(pending.id),
            **_async_headers(),
        )
        self.assertEqual(reject.status_code, 200)
        reject_body = json.loads(reject.content)
        self.assertEqual(
            reject_body,
            {
                "ok": True,
                "action": "reject",
                "result_id": pending.id,
                "document_id": doc.id,
                "verification_status": DocumentTextResult.VerificationStatus.REJECTED,
                "text_saved": False,
            },
        )

    def test_reject_does_not_persist_posted_textarea_text(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="keep me",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._reject_url(source.id),
            {"text": "should not save"},
        )
        self.assertEqual(resp.status_code, 302)
        source.refresh_from_db()
        self.assertEqual(source.text, "keep me")
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_review_detail_template_verify_submits_textarea_reject_does_not(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="template text",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("review-detail-page", kwargs={"doc_id": doc.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        save_url = self._save_url(source.id)
        verify_url = self._verify_url(source.id)
        reject_url = self._reject_url(source.id)

        verified_edit_url = reverse(
            "review-text-result-verified-edit",
            kwargs={"result_id": source.id},
        )
        self.assertIn(f'action="{save_url}"', html)
        self.assertIn(f'formaction="{verify_url}"', html)
        self.assertIn(f'data-verified-edit-url="{verified_edit_url}"', html)
        self.assertIn('data-label-verified="אושר"', html)
        self.assertIn('data-label-rejected="נדחה בבקרה"', html)
        self.assertIn('name="text"', html)
        self.assertIn("שמור טקסט", html)
        self.assertIn("אשר תעתוק", html)
        self.assertIn("דחה תעתוק", html)
        self.assertIn(f'action="{reject_url}"', html)
        self.assertIn("review_detail_actions.js", html)

        # Reject form must not own the textarea: formaction verify shares the text form.
        reject_idx = html.find(f'action="{reject_url}"')
        self.assertGreater(reject_idx, 0)
        reject_slice = html[reject_idx : reject_idx + 600]
        self.assertNotIn('name="text"', reject_slice)
        self.assertIn("דחה תעתוק", reject_slice)

    def test_review_detail_actions_js_does_not_hardcode_verified_edit_path(self):
        from pathlib import Path

        js_path = (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "review_detail_actions.js"
        )
        js = js_path.read_text(encoding="utf-8")
        self.assertNotIn("/api/ui/admin/review/text-results/", js)
        self.assertNotIn("verified-edit/", js)
        self.assertIn("data-verified-edit-url", js)
