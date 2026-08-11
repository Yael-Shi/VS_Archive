"""Tests for staff edits to pending/unverified DocumentTextResult rows."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
)
from documents.services.archive_items import create_ocr_document
from documents.services.verified_text_result_edit import (
    PendingTextResultEditError,
    edit_pending_text_result,
    is_hebrew_translation_stale,
)


@override_settings(UPLOADS_BUCKET_NAME="")
class PendingTextResultEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="pending_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="pending_edit_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_english_doc(self) -> Document:
        return create_ocr_document(
            title="English pending edit doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/50/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Hebrew pending edit doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/51/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_pending_text_result(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-a",
        prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
        source_revision: int = 1,
        based_on_source_revision: int | None = None,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=prompt_variant,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=verification_status,
            text=text,
            source_revision=source_revision,
            based_on_source_revision=based_on_source_revision,
        )

    def _pending_edit_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-update-text",
            kwargs={"result_id": result_id},
        )

    def test_pending_source_edit_increments_source_revision(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Version one",
            source_revision=1,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Version two",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(source.source_revision, 2)

    def test_pending_edit_creates_audit_row_with_editor(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Before audit",
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="After audit",
            editor=self.staff,
        )

        audit = DocumentTextResultEdit.objects.get(text_result=source)
        self.assertEqual(audit.old_text, "Before audit")
        self.assertEqual(audit.new_text, "After audit")
        self.assertEqual(audit.editor, self.staff)
        self.assertEqual(
            audit.edit_type,
            DocumentTextResultEdit.EditType.SOURCE_TEXT,
        )

    def test_no_op_does_not_increment_revision_or_audit(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Same text",
            source_revision=3,
        )
        updated_at_before = source.updated_at

        outcome = edit_pending_text_result(
            result_id=source.id,
            new_text="Same text",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(outcome.row.id, source.id)
        self.assertFalse(outcome.text_saved)
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(source.text, "Same text")
        self.assertEqual(source.updated_at, updated_at_before)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_repeated_edits_increment_revision_deterministically(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Rev 0",
            source_revision=1,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Rev 1",
            editor=self.staff,
        )
        edit_pending_text_result(
            result_id=source.id,
            new_text="Rev 2",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(source.text, "Rev 2")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 2)

    def test_pending_edit_preserves_unverified_and_needs_review(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Unverified source",
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Edited unverified source",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(source.status, DocumentTextResult.Status.NEEDS_REVIEW)

    def test_pending_edit_preserves_rejected_verification(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Rejected source",
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Fixed rejected source",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )

    def test_source_edit_makes_hebrew_translation_stale_without_overwrite(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            source_revision=1,
        )
        hebrew = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Hebrew translation",
            based_on_source_revision=1,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Updated English source",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.text, "Hebrew translation")
        self.assertTrue(is_hebrew_translation_stale(hebrew, source))

    def test_manual_hebrew_edit_links_to_current_source_revision(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            source_revision=2,
        )
        hebrew = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Old Hebrew",
            based_on_source_revision=1,
        )

        edit_pending_text_result(
            result_id=hebrew.id,
            new_text="Updated Hebrew",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.text, "Updated Hebrew")
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertFalse(is_hebrew_translation_stale(hebrew, source))

    def test_hebrew_mirror_edit_updates_both_rows_and_revisions(self):
        doc = self._create_hebrew_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            source_revision=1,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        hebrew = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            based_on_source_revision=1,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="טקסט מעודכן",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "טקסט מעודכן")
        self.assertEqual(hebrew.text, "טקסט מעודכן")
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)

    def test_hebrew_mirror_edit_creates_single_audit_row(self):
        doc = self._create_hebrew_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="טקסט מבוקר",
            editor=self.staff,
        )

        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)
        audit = DocumentTextResultEdit.objects.get()
        self.assertEqual(audit.text_result, source)

    def test_hebrew_mirror_edit_fails_when_paired_row_missing(self):
        doc = self._create_hebrew_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט ללא זוג",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        with self.assertRaises(PendingTextResultEditError):
            edit_pending_text_result(
                result_id=source.id,
                new_text="טקסט חדש",
                editor=self.staff,
            )

        source.refresh_from_db()
        self.assertEqual(source.text, "טקסט ללא זוג")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_unauthorized_non_staff_edit_is_blocked(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Protected source",
        )

        self.client.force_login(self.viewer)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": "Blocked edit"},
        )
        self.assertEqual(resp.status_code, 403)

        source.refresh_from_db()
        self.assertEqual(source.text, "Protected source")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_verified_row_not_editable_via_pending_path(self):
        doc = self._create_english_doc()
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="Already verified",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": "Should not apply"},
        )
        self.assertEqual(resp.status_code, 400)

        source.refresh_from_db()
        self.assertEqual(source.text, "Already verified")

    def test_pending_edit_view_redirects_on_success(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Redirect test",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": "Redirected edit"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"],
            reverse("review-detail-page", kwargs={"doc_id": doc.id}),
        )

    def test_pending_edit_view_no_op_still_redirects(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Unchanged",
            source_revision=5,
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": "Unchanged"},
        )
        self.assertEqual(resp.status_code, 302)

        source.refresh_from_db()
        self.assertEqual(source.source_revision, 5)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_pending_edit_persists_raw_submitted_whitespace_in_row_and_audit(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before",
        )
        submitted = "  after edit  \n"

        edit_pending_text_result(
            result_id=source.id,
            new_text=submitted,
            editor=self.staff,
        )

        source.refresh_from_db()
        audit = DocumentTextResultEdit.objects.get(text_result=source)
        self.assertEqual(source.text, submitted)
        self.assertEqual(audit.new_text, submitted)

    def test_equivalent_whitespace_only_submission_is_no_op(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="stable text",
            source_revision=4,
        )
        updated_at_before = source.updated_at

        edit_pending_text_result(
            result_id=source.id,
            new_text="  stable text  \n",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(source.text, "stable text")
        self.assertEqual(source.source_revision, 4)
        self.assertEqual(source.updated_at, updated_at_before)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_view_missing_result_blank_submission_returns_404(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(999999),
            {"text": ""},
        )
        self.assertEqual(resp.status_code, 404)

    def test_view_ineligible_result_before_blank_text_validation(self):
        doc = self._create_english_doc()
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="Already verified",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": ""},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(
            resp,
            "transcription result is not eligible for review action",
            status_code=400,
        )

    def test_view_eligible_blank_text_returns_400_after_eligibility(self):
        doc = self._create_english_doc()
        source = self._create_pending_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Eligible row",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._pending_edit_url(source.id),
            {"text": "  \n\t  "},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertContains(
            resp,
            "text is required and must be non-empty",
            status_code=400,
        )
        source.refresh_from_db()
        self.assertEqual(source.text, "Eligible row")
