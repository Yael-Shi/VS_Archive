"""Tests for staff edits to already-verified DocumentTextResult rows."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranscriptionEditSuggestion,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transcription_edit_suggestions import texts_are_equivalent
from documents.services.verified_text_result_edit import (
    VerifiedTextResultEditError,
    edit_verified_text_result,
    is_hebrew_translation_stale,
)


@override_settings(UPLOADS_BUCKET_NAME="")
class VerifiedTextResultEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="verified_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="verified_edit_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_english_doc(self) -> Document:
        return create_ocr_document(
            title="English verified edit doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/40/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Hebrew verified edit doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/41/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_text_result(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-a",
        prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
        verification_status: str = DocumentTextResult.VerificationStatus.VERIFIED,
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

    def _verified_edit_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-verified-edit",
            kwargs={"result_id": result_id},
        )

    def test_editing_verified_source_keeps_it_verified(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Original source",
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="Edited source",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(source.text, "Edited source")

    def test_source_revision_increments_on_source_edit(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Version one",
            source_revision=1,
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="Version two",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertEqual(source.source_revision, 2)

    def test_audit_row_contains_old_new_text_and_editor(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Before audit",
        )

        edit_verified_text_result(
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

    def test_no_op_does_not_create_audit(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Same text",
        )

        with self.assertRaises(VerifiedTextResultEditError):
            edit_verified_text_result(
                result_id=source.id,
                new_text="Same text",
                editor=self.staff,
            )

        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_source_edit_makes_hebrew_translation_stale(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            source_revision=1,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Hebrew translation",
            based_on_source_revision=1,
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="Updated English source",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.text, "Hebrew translation")
        self.assertTrue(is_hebrew_translation_stale(hebrew, source))

    def test_manual_hebrew_edit_marks_current_for_latest_source_revision(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            source_revision=2,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Old Hebrew",
            based_on_source_revision=1,
        )

        edit_verified_text_result(
            result_id=hebrew.id,
            new_text="Updated Hebrew",
            editor=self.staff,
        )

        hebrew.refresh_from_db()
        self.assertEqual(hebrew.text, "Updated Hebrew")
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertFalse(is_hebrew_translation_stale(hebrew, source))

    def test_unauthorized_non_staff_edit_is_blocked(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Protected source",
        )

        self.client.force_login(self.viewer)
        resp = self.client.post(
            self._verified_edit_url(source.id),
            {"text": "Blocked edit"},
        )
        self.assertEqual(resp.status_code, 403)

        source.refresh_from_db()
        self.assertEqual(source.text, "Protected source")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_suggestion_reports_drift_after_staff_edit(self):
        doc = self._create_english_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Displayed source",
        )
        suggestion = TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot="Displayed source",
            suggested_text="Suggested fix",
            submitter_name="Contributor",
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="Staff edited source",
            editor=self.staff,
        )

        source.refresh_from_db()
        self.assertFalse(
            texts_are_equivalent(source.text, suggestion.current_text_snapshot)
        )

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse(
                "transcription-suggestion-detail",
                kwargs={"suggestion_id": suggestion.id},
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "התעתוק באתר השתנה מאז שליחת ההצעה.")

    def test_review_detail_shows_stale_hebrew_message(self):
        doc = self._create_english_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Source v2",
            source_revision=2,
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Stale Hebrew",
            based_on_source_revision=1,
        )

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טעון תרגום מחדש")
        self.assertContains(resp, "עריכת תרגום מאושר")

    def test_review_detail_shows_verified_source_edit_action(self):
        doc = self._create_english_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Approved source",
        )

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת תעתוק מאושר")
        self.assertNotContains(resp, "source_revision")

    def test_hebrew_source_edit_updates_hebrew_text(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="טקסט מעודכן",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "טקסט מעודכן")
        self.assertEqual(hebrew.text, "טקסט מעודכן")

    def test_hebrew_hebrew_edit_updates_source_text(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_verified_text_result(
            result_id=hebrew.id,
            new_text="טקסט מעודכן",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "טקסט מעודכן")
        self.assertEqual(hebrew.text, "טקסט מעודכן")

    def test_hebrew_mirror_edit_keeps_both_verified(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="טקסט מאומת",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_hebrew_mirror_edit_creates_single_audit_row(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        edit_verified_text_result(
            result_id=source.id,
            new_text="טקסט מבוקר",
            editor=self.staff,
        )

        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)
        audit = DocumentTextResultEdit.objects.get()
        self.assertEqual(audit.text_result, source)

    def test_hebrew_mirror_edit_fails_when_paired_row_missing(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט ללא זוג",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        with self.assertRaises(VerifiedTextResultEditError):
            edit_verified_text_result(
                result_id=source.id,
                new_text="טקסט חדש",
                editor=self.staff,
            )

        source.refresh_from_db()
        self.assertEqual(source.text, "טקסט ללא זוג")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_hebrew_document_is_never_marked_stale(self):
        doc = self._create_hebrew_doc()
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט משותף",
            source_revision=3,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט משותף",
            based_on_source_revision=1,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        self.assertFalse(is_hebrew_translation_stale(hebrew, source))

        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "טעון תרגום מחדש")
