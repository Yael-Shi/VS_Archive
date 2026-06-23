from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import Document, DocumentTextResult, TranscriptionEditSuggestion
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_ocr_document
from documents.services.text_presentation import (
    get_displayed_transcription_text,
    resolve_displayed_transcription_result,
)
from documents.services.transcription_edit_suggestions import render_transcription_diff_html
from documents.services.transcription_suggestion_review import (
    TranscriptionSuggestionReviewError,
    approve_suggestion,
    reject_suggestion,
)
from documents.services.verified_text_result_edit import is_hebrew_translation_stale
from public.services.registration import HONEYPOT_FIELD_NAME


@override_settings(UPLOADS_BUCKET_NAME="")
class DisplayedTranscriptionTextTests(TestCase):
    def _create_hebrew_doc(self, title: str) -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/1/original.jpg",
            mime_type="image/jpeg",
        )

    def test_hebrew_prefers_hebrew_text(self):
        doc = self._create_hebrew_doc("Hebrew prefer")
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="מקור",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="עברית",
        )

        self.assertEqual(get_displayed_transcription_text(doc), "עברית")

    def test_hebrew_falls_back_to_source_text(self):
        doc = self._create_hebrew_doc("Hebrew fallback")
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="מקור בלבד",
        )

        self.assertEqual(get_displayed_transcription_text(doc), "מקור בלבד")

    def test_non_hebrew_prefers_source_text(self):
        doc = create_ocr_document(
            title="English prefer source",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/2/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Hello world",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="שלום",
        )

        self.assertEqual(get_displayed_transcription_text(doc), "Hello world")

    def test_non_hebrew_falls_back_to_hebrew_text(self):
        doc = create_ocr_document(
            title="English fallback hebrew",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/3/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="שלום",
        )

        self.assertEqual(get_displayed_transcription_text(doc), "שלום")


@override_settings(UPLOADS_BUCKET_NAME="")
class TranscriptionEditSuggestionPublicFlowTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)

    def _create_family_user(self, username: str = "family_suggestion_user") -> User:
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(Group.objects.get(name=ARCHIVE_FAMILY_GROUP_NAME))
        return user

    def _create_public_doc_with_text(self, *, text: str = "טקסט מקורי") -> Document:
        doc = create_ocr_document(
            title="Public suggestion doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/10/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
        )
        return doc

    def _form_url(self, doc_id: int) -> str:
        return reverse("transcription-suggestion-new", kwargs={"doc_id": doc_id})

    def _thanks_url(self, doc_id: int) -> str:
        return reverse("transcription-suggestion-thanks", kwargs={"doc_id": doc_id})

    def _detail_url(self, doc_id: int) -> str:
        return reverse("documents-detail-page", kwargs={"doc_id": doc_id})

    def _valid_post_data(self, **overrides) -> dict[str, str]:
        data = {
            "submitter_name": "מציע/ה",
            "submitter_email": "suggester@example.com",
            "submitter_note": "הערה",
            "suggested_text": "טקסט מתוקן",
            HONEYPOT_FIELD_NAME: "",
        }
        data.update(overrides)
        return data

    def test_anonymous_can_get_form_for_public_document(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.get(self._form_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טקסט מקורי")
        self.assertContains(resp, "ההצעה תיבדק לפני שתופיע באתר")

    def test_anonymous_cannot_access_private_document(self):
        doc = self._create_public_doc_with_text()
        doc.archive_item.visibility = Document.Visibility.PRIVATE
        doc.archive_item.save(update_fields=["visibility"])
        doc.visibility = Document.Visibility.PRIVATE
        doc.save(update_fields=["visibility"])

        self.assertEqual(self.client.get(self._form_url(doc.id)).status_code, 404)
        self.assertEqual(
            self.client.post(self._form_url(doc.id), self._valid_post_data()).status_code,
            404,
        )

    def test_family_viewer_can_access_private_document(self):
        doc = self._create_public_doc_with_text()
        doc.archive_item.visibility = Document.Visibility.PRIVATE
        doc.archive_item.save(update_fields=["visibility"])
        doc.visibility = Document.Visibility.PRIVATE
        doc.save(update_fields=["visibility"])

        self.client.force_login(self._create_family_user())
        resp = self.client.get(self._form_url(doc.id))
        self.assertEqual(resp.status_code, 200)

    def test_post_creates_suggestion_without_mutating_text_result(self):
        doc = self._create_public_doc_with_text()
        before_text = doc.text_results.get().text

        resp = self.client.post(self._form_url(doc.id), self._valid_post_data())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(doc.id))

        suggestion = TranscriptionEditSuggestion.objects.get()
        self.assertEqual(suggestion.document_id, doc.id)
        self.assertEqual(suggestion.current_text_snapshot, "טקסט מקורי")
        self.assertEqual(suggestion.suggested_text, "טקסט מתוקן")
        self.assertEqual(suggestion.submitter_name, "מציע/ה")
        self.assertEqual(
            suggestion.status,
            TranscriptionEditSuggestion.Status.PENDING,
        )
        self.assertIsNone(suggestion.submitter_user)

        doc.text_results.get().refresh_from_db()
        self.assertEqual(doc.text_results.get().text, before_text)

    def test_name_required(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.post(
            self._form_url(doc.id),
            self._valid_post_data(submitter_name=""),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יש למלא שם.")
        self.assertEqual(TranscriptionEditSuggestion.objects.count(), 0)

    def test_suggested_text_required(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.post(
            self._form_url(doc.id),
            self._valid_post_data(suggested_text="   "),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יש להזין טקסט מוצע.")
        self.assertEqual(TranscriptionEditSuggestion.objects.count(), 0)

    def test_identical_suggested_text_rejected(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.post(
            self._form_url(doc.id),
            self._valid_post_data(suggested_text="טקסט מקורי"),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הטקסט המוצע זהה לתעתוק הנוכחי.")
        self.assertEqual(TranscriptionEditSuggestion.objects.count(), 0)

    def test_honeypot_does_not_create_suggestion(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.post(
            self._form_url(doc.id),
            self._valid_post_data(**{HONEYPOT_FIELD_NAME: "bot corp"}),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(doc.id))
        self.assertEqual(TranscriptionEditSuggestion.objects.count(), 0)

    def test_detail_button_shown_when_displayed_text_exists(self):
        doc = self._create_public_doc_with_text()
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצע/י תיקון לתעתוק")
        self.assertContains(resp, self._form_url(doc.id))

    def test_detail_button_hidden_when_no_displayable_text(self):
        doc = create_ocr_document(
            title="No text doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="documents/11/original.jpg",
            mime_type="image/jpeg",
        )
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "הצע/י תיקון לתעתוק")


class TranscriptionDiffHelperTests(TestCase):
    def test_diff_escapes_html_and_marks_changes(self):
        current = "שלום <b>עולם</b>"
        suggested = "שלום עולם יפה"
        html = str(render_transcription_diff_html(current, suggested))

        self.assertIn("&lt;b&gt;", html)
        self.assertNotIn("<b>", html)
        self.assertIn("transcription-diff-del", html)
        self.assertIn("transcription-diff-ins", html)
        self.assertIn("יפה", html)


@override_settings(UPLOADS_BUCKET_NAME="")
class TranscriptionEditSuggestionStaffUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="suggestion_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="suggestion_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_doc(self, *, title: str) -> Document:
        doc = create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/20/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="טקסט בסיס",
        )
        return doc

    def _create_suggestion(
        self,
        doc: Document,
        *,
        submitter_name: str = "מציע/ה",
        submitter_email: str = "",
        submitter_note: str = "",
        suggested_text: str = "טקסט מתוקן",
        current_text_snapshot: str = "טקסט בסיס",
    ) -> TranscriptionEditSuggestion:
        return TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot=current_text_snapshot,
            suggested_text=suggested_text,
            submitter_name=submitter_name,
            submitter_email=submitter_email,
            submitter_note=submitter_note,
        )

    def _backlog_url(self) -> str:
        return reverse("transcription-suggestion-backlog")

    def _staff_detail_url(self, suggestion_id: int) -> str:
        return reverse(
            "transcription-suggestion-detail",
            kwargs={"suggestion_id": suggestion_id},
        )

    def test_non_staff_cannot_access_staff_backlog(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 403)

    def test_non_staff_cannot_access_staff_detail(self):
        doc = self._create_doc(title="Private staff detail")
        suggestion = self._create_suggestion(doc)
        self.client.force_login(self.viewer)
        resp = self.client.get(self._staff_detail_url(suggestion.id))
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_backlog(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצעות תיקון לתעתוקים")

    def test_backlog_lists_one_row_per_suggestion(self):
        doc_a = self._create_doc(title="Doc A")
        doc_b = self._create_doc(title="Doc B")
        suggestion_a = self._create_suggestion(doc_a, submitter_name="ראשון")
        suggestion_b = self._create_suggestion(doc_b, submitter_name="שני")

        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Doc A")
        self.assertContains(resp, "Doc B")
        self.assertContains(resp, "ראשון")
        self.assertContains(resp, "שני")
        self.assertContains(resp, self._staff_detail_url(suggestion_a.id))
        self.assertContains(resp, self._staff_detail_url(suggestion_b.id))

    def test_staff_can_access_suggestion_detail(self):
        doc = self._create_doc(title="Detail doc")
        suggestion = self._create_suggestion(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(self._staff_detail_url(suggestion.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצעת תיקון לתעתוק")

    def test_detail_shows_submitter_metadata_when_present(self):
        doc = self._create_doc(title="Metadata doc")
        suggestion = self._create_suggestion(
            doc,
            submitter_name="ישראל ישראלי",
            submitter_email="israel@example.com",
            submitter_note="הערת מציע",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._staff_detail_url(suggestion.id))
        self.assertContains(resp, "ישראל ישראלי")
        self.assertContains(resp, "israel@example.com")
        self.assertContains(resp, "הערת מציע")

    def test_detail_shows_snapshot_suggested_text_and_diff_markup(self):
        doc = self._create_doc(title="Diff doc")
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="אבג",
            suggested_text="אדג",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._staff_detail_url(suggestion.id))
        self.assertContains(resp, "אבג")
        self.assertContains(resp, "אדג")
        self.assertContains(resp, 'class="transcription-diff-del"')
        self.assertContains(resp, 'class="transcription-diff-ins"')
        self.assertContains(resp, "הבדלים מודגשים")
        self.assertNotContains(resp, "<script>")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/preview.jpg")
    def test_detail_uses_source_preview_path(self, _mock_presign):
        doc = self._create_doc(title="Preview doc")
        suggestion = self._create_suggestion(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(self._staff_detail_url(suggestion.id))
        self.assertContains(resp, "קובץ מקור")
        self.assertContains(resp, "https://example.com/preview.jpg")

    def test_staff_nav_contains_backlog_link(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("documents-list-page"))
        self.assertContains(resp, "הצעות תיקון לתעתוקים")
        self.assertContains(resp, self._backlog_url())


@override_settings(UPLOADS_BUCKET_NAME="")
class TranscriptionEditSuggestionReviewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="suggestion_review_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="suggestion_review_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_hebrew_doc(self, *, title: str = "Hebrew review doc") -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/30/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_english_doc(self, *, title: str = "English review doc") -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/31/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_text_result(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-a",
        source_revision: int = 1,
        based_on_source_revision: int | None = None,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            source_revision=source_revision,
            based_on_source_revision=based_on_source_revision,
        )

    def _create_hebrew_paired_results(
        self,
        doc: Document,
        *,
        text: str,
        engine: str = "engine-a",
    ) -> tuple[DocumentTextResult, DocumentTextResult]:
        source = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text=text,
            engine=engine,
        )
        hebrew = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text=text,
            engine=engine,
        )
        return source, hebrew

    def _create_suggestion(
        self,
        doc: Document,
        *,
        current_text_snapshot: str = "טקסט בסיס",
        suggested_text: str = "טקסט מתוקן",
        status: str = TranscriptionEditSuggestion.Status.PENDING,
    ) -> TranscriptionEditSuggestion:
        return TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot=current_text_snapshot,
            suggested_text=suggested_text,
            submitter_name="מציע/ה",
            status=status,
        )

    def _detail_url(self, suggestion_id: int) -> str:
        return reverse(
            "transcription-suggestion-detail",
            kwargs={"suggestion_id": suggestion_id},
        )

    def _approve_url(self, suggestion_id: int) -> str:
        return reverse(
            "transcription-suggestion-approve",
            kwargs={"suggestion_id": suggestion_id},
        )

    def _reject_url(self, suggestion_id: int) -> str:
        return reverse(
            "transcription-suggestion-reject",
            kwargs={"suggestion_id": suggestion_id},
        )

    def test_staff_approve_updates_displayed_text(self):
        doc = self._create_hebrew_doc()
        _, row = self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc)

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._approve_url(suggestion.id),
            {"approved_text": "טקסט מאושר"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._detail_url(suggestion.id))

        row.refresh_from_db()
        self.assertEqual(row.text, "טקסט מאושר")

    def test_staff_approve_sets_verification_status_verified(self):
        doc = self._create_hebrew_doc()
        _, row = self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc)

        self.client.force_login(self.staff)
        self.client.post(
            self._approve_url(suggestion.id),
            {"approved_text": "טקסט מאושר"},
        )

        row.refresh_from_db()
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(row.status, DocumentTextResult.Status.NEEDS_REVIEW)

    def test_partial_approve_stores_approved_text(self):
        doc = self._create_hebrew_doc()
        self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc, suggested_text="טקסט מתוקן")

        approve_suggestion(
            suggestion.id,
            approved_text="גרסה סופית שונה",
            reviewer=self.staff,
        )

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.approved_text, "גרסה סופית שונה")
        self.assertEqual(suggestion.status, TranscriptionEditSuggestion.Status.APPROVED)
        self.assertEqual(suggestion.reviewed_by, self.staff)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertIsNotNone(suggestion.applied_text_result_id)

    def test_reject_marks_suggestion_rejected_without_mutating_text_result(self):
        doc = self._create_hebrew_doc()
        row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
        )
        suggestion = self._create_suggestion(doc)

        self.client.force_login(self.staff)
        resp = self.client.post(self._reject_url(suggestion.id))
        self.assertEqual(resp.status_code, 302)

        suggestion.refresh_from_db()
        row.refresh_from_db()
        self.assertEqual(suggestion.status, TranscriptionEditSuggestion.Status.REJECTED)
        self.assertEqual(suggestion.reviewed_by, self.staff)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertEqual(row.text, "טקסט בסיס")
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_non_staff_cannot_approve_or_reject(self):
        doc = self._create_hebrew_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
        )
        suggestion = self._create_suggestion(doc)
        self.client.force_login(self.viewer)

        self.assertEqual(
            self.client.post(
                self._approve_url(suggestion.id),
                {"approved_text": "טקסט"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(self._reject_url(suggestion.id)).status_code,
            403,
        )

    def test_already_reviewed_suggestion_cannot_be_approved_or_rejected_again(self):
        doc = self._create_hebrew_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
        )
        suggestion = self._create_suggestion(
            doc,
            status=TranscriptionEditSuggestion.Status.APPROVED,
        )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._approve_url(suggestion.id),
            {"approved_text": "טקסט"},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertContains(
            self.client.get(self._detail_url(suggestion.id)),
            "ההצעה כבר נבדקה.",
        )

        suggestion.status = TranscriptionEditSuggestion.Status.REJECTED
        suggestion.save(update_fields=["status"])
        resp = self.client.post(self._reject_url(suggestion.id))
        self.assertEqual(resp.status_code, 302)

    def test_empty_approved_text_rejected(self):
        doc = self._create_hebrew_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
        )
        suggestion = self._create_suggestion(doc)

        with self.assertRaises(TranscriptionSuggestionReviewError):
            approve_suggestion(
                suggestion.id,
                approved_text="   ",
                reviewer=self.staff,
            )

        self.client.force_login(self.staff)
        resp = self.client.post(
            self._approve_url(suggestion.id),
            {"approved_text": "   "},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertContains(
            self.client.get(self._detail_url(suggestion.id)),
            "יש להזין טקסט מאושר.",
        )

    def test_hebrew_approval_targets_hebrew_text_when_displayed(self):
        doc = self._create_hebrew_doc()
        source_row, hebrew_row = self._create_hebrew_paired_results(
            doc,
            text="עברית",
        )
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="עברית",
            suggested_text="עברית מתוקנת",
        )

        approve_suggestion(
            suggestion.id,
            approved_text="עברית מאושרת",
            reviewer=self.staff,
        )

        hebrew_row.refresh_from_db()
        source_row.refresh_from_db()
        self.assertEqual(hebrew_row.text, "עברית מאושרת")
        self.assertEqual(source_row.text, "עברית מאושרת")
        self.assertEqual(
            resolve_displayed_transcription_result(doc),
            hebrew_row,
        )

    def test_non_hebrew_approval_targets_source_text_when_displayed(self):
        doc = self._create_english_doc()
        source_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Hello",
            engine="engine-a",
        )
        hebrew_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="שלום",
            engine="engine-a",
        )
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="Hello",
            suggested_text="Hello fixed",
        )

        approve_suggestion(
            suggestion.id,
            approved_text="Hello approved",
            reviewer=self.staff,
        )

        source_row.refresh_from_db()
        hebrew_row.refresh_from_db()
        self.assertEqual(source_row.text, "Hello approved")
        self.assertEqual(hebrew_row.text, "שלום")
        self.assertEqual(
            resolve_displayed_transcription_result(doc),
            source_row,
        )

    def test_hebrew_dual_row_sync_updates_both_rows_for_same_engine(self):
        doc = self._create_hebrew_doc()
        hebrew_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="עברית",
            engine="engine-a",
        )
        source_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="עברית",
            engine="engine-a",
        )
        suggestion = self._create_suggestion(doc)

        approve_suggestion(
            suggestion.id,
            approved_text="עברית מאושרת",
            reviewer=self.staff,
        )

        hebrew_row.refresh_from_db()
        source_row.refresh_from_db()
        self.assertEqual(hebrew_row.text, "עברית מאושרת")
        self.assertEqual(source_row.text, "עברית מאושרת")
        self.assertEqual(
            hebrew_row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            source_row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_non_hebrew_source_suggestion_increments_revision_and_makes_hebrew_stale(
        self,
    ):
        doc = self._create_english_doc()
        source_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Hello",
            engine="engine-a",
            source_revision=1,
        )
        hebrew_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="שלום",
            engine="engine-a",
            based_on_source_revision=1,
        )
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="Hello",
            suggested_text="Hello fixed",
        )

        approve_suggestion(
            suggestion.id,
            approved_text="Hello approved",
            reviewer=self.staff,
        )

        source_row.refresh_from_db()
        hebrew_row.refresh_from_db()
        self.assertEqual(source_row.text, "Hello approved")
        self.assertEqual(source_row.source_revision, 2)
        self.assertEqual(hebrew_row.text, "שלום")
        self.assertEqual(hebrew_row.based_on_source_revision, 1)
        self.assertTrue(is_hebrew_translation_stale(hebrew_row, source_row))

    def test_hebrew_suggestion_updates_both_rows_and_synchronizes_revisions(self):
        doc = self._create_hebrew_doc()
        source_row, hebrew_row = self._create_hebrew_paired_results(
            doc,
            text="עברית",
            engine="engine-a",
        )
        suggestion = self._create_suggestion(doc)

        approve_suggestion(
            suggestion.id,
            approved_text="עברית מאושרת",
            reviewer=self.staff,
        )

        source_row.refresh_from_db()
        hebrew_row.refresh_from_db()
        self.assertEqual(source_row.text, "עברית מאושרת")
        self.assertEqual(hebrew_row.text, "עברית מאושרת")
        self.assertEqual(source_row.source_revision, 2)
        self.assertEqual(hebrew_row.based_on_source_revision, 2)

    def test_hebrew_suggestion_fails_when_paired_row_missing(self):
        doc = self._create_hebrew_doc()
        hebrew_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="עברית",
        )
        suggestion = self._create_suggestion(doc)

        with self.assertRaises(TranscriptionSuggestionReviewError):
            approve_suggestion(
                suggestion.id,
                approved_text="עברית מאושרת",
                reviewer=self.staff,
            )

        hebrew_row.refresh_from_db()
        self.assertEqual(hebrew_row.text, "עברית")
        self.assertEqual(
            hebrew_row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, TranscriptionEditSuggestion.Status.PENDING)

    def test_non_hebrew_hebrew_displayed_approval_links_to_current_source_revision(
        self,
    ):
        doc = self._create_english_doc()
        source_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="",
            engine="engine-a",
            source_revision=3,
        )
        hebrew_row = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום מוצג",
            engine="engine-a",
        )
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="תרגום מוצג",
            suggested_text="תרגום מתוקן",
        )

        approve_suggestion(
            suggestion.id,
            approved_text="תרגום מאושר",
            reviewer=self.staff,
        )

        hebrew_row.refresh_from_db()
        source_row.refresh_from_db()
        self.assertEqual(
            resolve_displayed_transcription_result(doc),
            hebrew_row,
        )
        self.assertEqual(hebrew_row.text, "תרגום מאושר")
        self.assertEqual(hebrew_row.based_on_source_revision, 3)
        self.assertEqual(source_row.source_revision, 3)

    def test_detail_shows_live_text_drift_warning(self):
        doc = self._create_hebrew_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט מעודכן באתר",
        )
        suggestion = self._create_suggestion(
            doc,
            current_text_snapshot="טקסט ישן",
            suggested_text="טקסט מתוקן",
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(suggestion.id))
        self.assertContains(resp, "תעתוק נוכחי באתר")
        self.assertContains(resp, "התעתוק באתר השתנה מאז שליחת ההצעה.")
        self.assertContains(resp, "טקסט מעודכן באתר")

    def test_document_detail_shows_approved_text_after_approval(self):
        doc = self._create_hebrew_doc()
        self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc)

        approve_suggestion(
            suggestion.id,
            approved_text="טקסט שמוצג באתר",
            reviewer=self.staff,
        )

        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טקסט שמוצג באתר")

    def test_get_displayed_transcription_text_reflects_approved_text(self):
        doc = self._create_hebrew_doc()
        self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc)

        approve_suggestion(
            suggestion.id,
            approved_text="טקסט מאושר",
            reviewer=self.staff,
        )

        self.assertEqual(get_displayed_transcription_text(doc), "טקסט מאושר")

    def test_reviewed_suggestion_detail_is_read_only(self):
        doc = self._create_hebrew_doc()
        self._create_hebrew_paired_results(doc, text="טקסט בסיס")
        suggestion = self._create_suggestion(doc)
        approve_suggestion(
            suggestion.id,
            approved_text="טקסט מאושר",
            reviewer=self.staff,
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(suggestion.id))
        self.assertContains(resp, "טקסט מאושר")
        self.assertNotContains(resp, "אישור גרסה זו")
        self.assertNotContains(resp, "דחיית ההצעה")

    def test_reject_via_service(self):
        doc = self._create_hebrew_doc()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
        )
        suggestion = self._create_suggestion(doc)

        reject_suggestion(suggestion.id, reviewer=self.staff)

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, TranscriptionEditSuggestion.Status.REJECTED)
        self.assertEqual(suggestion.reviewed_by, self.staff)
