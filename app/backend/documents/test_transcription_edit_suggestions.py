from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import Document, DocumentTextResult, TranscriptionEditSuggestion
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_ocr_document
from documents.services.text_presentation import get_displayed_transcription_text
from documents.services.transcription_edit_suggestions import render_transcription_diff_html
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
