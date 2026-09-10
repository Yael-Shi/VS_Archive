"""Browser form restoration must not replace server-rendered review textarea text."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, Document, DocumentTextResult
from documents.services.archive_items import create_ocr_document

_REVIEW_ACTIONS_JS = (
    Path(__file__).resolve().parents[1]
    / "public"
    / "static"
    / "public"
    / "review_detail_actions.js"
)


class _FakeReviewTextarea:
    def __init__(self, html_text: str, *, restored_value: str | None = None) -> None:
        self.defaultValue = html_text
        self.value = html_text if restored_value is None else restored_value


class _FakeReviewRoot:
    def __init__(self, textareas: list[_FakeReviewTextarea]) -> None:
        self._textareas = list(textareas)

    def querySelectorAll(self, selector: str) -> list[_FakeReviewTextarea]:
        if selector != "textarea.review-textarea":
            return []
        return self._textareas


def restore_review_textareas_from_server_default(root: object) -> None:
    """DOM fixture of ``restoreReviewTextareasFromServerDefault`` in review JS."""
    query = getattr(root, "querySelectorAll", None)
    if query is None:
        return
    textareas = query("textarea.review-textarea")
    for textarea in textareas:
        if textarea.value != textarea.defaultValue:
            textarea.value = textarea.defaultValue


class RestoreReviewTextareasFromServerDefaultTests(SimpleTestCase):
    def test_browser_restored_value_is_replaced_with_html_default(self) -> None:
        textarea = _FakeReviewTextarea(
            "new canonical",
            restored_value="old stale",
        )
        restore_review_textareas_from_server_default(_FakeReviewRoot([textarea]))
        self.assertEqual(textarea.value, "new canonical")
        self.assertEqual(textarea.defaultValue, "new canonical")

    def test_matching_value_and_default_are_left_unchanged(self) -> None:
        textarea = _FakeReviewTextarea("new canonical")
        restore_review_textareas_from_server_default(_FakeReviewRoot([textarea]))
        self.assertEqual(textarea.value, "new canonical")
        self.assertEqual(textarea.defaultValue, "new canonical")

    def test_multiple_review_cards_are_reset_independently(self) -> None:
        source = _FakeReviewTextarea("source canonical", restored_value="source stale")
        hebrew = _FakeReviewTextarea("hebrew canonical", restored_value="hebrew stale")
        matching = _FakeReviewTextarea("already canonical")
        restore_review_textareas_from_server_default(
            _FakeReviewRoot([source, hebrew, matching])
        )
        self.assertEqual(source.value, "source canonical")
        self.assertEqual(hebrew.value, "hebrew canonical")
        self.assertEqual(matching.value, "already canonical")
        self.assertNotEqual(source.value, hebrew.value)


class ReviewDetailActionsJsRestorationContractTests(SimpleTestCase):
    def test_js_resets_review_textarea_value_from_default_on_pageshow(self) -> None:
        js = _REVIEW_ACTIONS_JS.read_text(encoding="utf-8")
        self.assertIn(
            'scope.querySelectorAll("textarea.review-textarea")',
            js,
        )
        self.assertIn("textarea.value !== textarea.defaultValue", js)
        self.assertIn("textarea.value = textarea.defaultValue", js)
        self.assertIn(
            "restoreReviewTextareasFromServerDefault(document);",
            js,
        )
        self.assertIn(
            'window.addEventListener("pageshow", onPageShow, false);',
            js,
        )
        self.assertNotIn("localStorage", js)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("get_displayed_transcription_text", js)


@override_settings(UPLOADS_BUCKET_NAME="")
class ReviewDetailTextareaMarkupTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="review_restore_staff",
            password="test-pass",
            is_staff=True,
        )

    def test_review_detail_textareas_and_forms_disable_autocomplete(self) -> None:
        doc = create_ocr_document(
            title="EN review restore",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/review-restore/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-restore",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="new canonical",
            source_revision=1,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("review-detail-page", kwargs={"doc_id": doc.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("review_detail_actions.js", html)
        self.assertIn('class="review-textarea"', html)
        self.assertIn('autocomplete="off"', html)
        self.assertGreaterEqual(html.count('class="review-textarea"'), 1)
        self.assertIn(
            '<textarea class="review-textarea" name="text" rows="12" autocomplete="off">',
            html,
        )
        self.assertIn("data-review-text-form", html)
        self.assertRegex(
            html,
            r'<form\b[^>]*data-review-text-form\b[^>]*autocomplete="off"',
        )
