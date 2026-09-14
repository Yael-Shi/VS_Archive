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


class _FakeHiddenFlag:
    def __init__(self, *, restored_value: str | None = None) -> None:
        self.defaultValue = "0"
        self.value = "0" if restored_value is None else restored_value
        self.name = "text_was_user_edited"

    def matches(self, selector: str) -> bool:
        return selector == 'input[name="text_was_user_edited"]'


class _FakeReviewForm:
    def __init__(self, *, flag: _FakeHiddenFlag | None = None) -> None:
        self.flag = flag if flag is not None else _FakeHiddenFlag()

    def querySelector(self, selector: str):
        if selector == 'input[name="text_was_user_edited"]':
            return self.flag
        return None


class _FakeReviewTextarea:
    def __init__(
        self,
        html_text: str,
        *,
        restored_value: str | None = None,
        form: _FakeReviewForm | None = None,
    ) -> None:
        self.defaultValue = html_text
        self.value = html_text if restored_value is None else restored_value
        self.form = form if form is not None else _FakeReviewForm()

    def matches(self, selector: str) -> bool:
        return selector == "textarea.review-textarea"


class _FakeReviewRoot:
    def __init__(
        self,
        textareas: list[_FakeReviewTextarea],
        *,
        flags: list[_FakeHiddenFlag] | None = None,
    ) -> None:
        self._textareas = list(textareas)
        if flags is not None:
            self._flags = list(flags)
        else:
            self._flags = [textarea.form.flag for textarea in self._textareas]

    def querySelectorAll(self, selector: str) -> list[object]:
        if selector == "textarea.review-textarea":
            return list(self._textareas)
        if selector == 'input[name="text_was_user_edited"]':
            return list(self._flags)
        return []


_MUTATION_INPUT_TYPES = frozenset(
    {
        "insertText",
        "insertLineBreak",
        "insertParagraph",
        "insertFromPaste",
        "insertFromDrop",
        "insertCompositionText",
        "deleteContentBackward",
        "deleteContentForward",
        "deleteByCut",
        "historyUndo",
        "historyRedo",
    }
)
_IGNORED_AUTOFILL_INPUT_TYPES = frozenset(
    {
        "insertFromAutoComplete",
        "insertReplacementText",
    }
)
_HIGH_LEVEL_NON_MUTATION_EVENTS = frozenset(
    {
        "keydown",
        "paste",
        "cut",
        "drop",
        "compositionend",
    }
)


def restore_review_textareas_from_server_default(root: object) -> None:
    """DOM fixture of ``restoreReviewTextareasFromServerDefault`` in review JS."""
    query = getattr(root, "querySelectorAll", None)
    if query is None:
        return
    textareas = query("textarea.review-textarea")
    for textarea in textareas:
        form = getattr(textarea, "form", None)
        flag = None
        if form is not None:
            form_query = getattr(form, "querySelector", None)
            if form_query is not None:
                flag = form_query('input[name="text_was_user_edited"]')
        if flag is not None and flag.value == "1":
            continue
        if textarea.value != textarea.defaultValue:
            textarea.value = textarea.defaultValue


def mark_review_textarea_user_edited(
    textarea: _FakeReviewTextarea,
    *,
    input_type: str | None = None,
    event_name: str = "input",
) -> None:
    """DOM fixture of review JS content-mutation dirty marking.

    High-level ``keydown`` / ``paste`` / ``cut`` / ``drop`` / ``compositionend``
    do not mark dirty. Only ``beforeinput`` / ``input`` with an explicit
    mutation ``inputType`` do.
    """
    if event_name in _HIGH_LEVEL_NON_MUTATION_EVENTS:
        return
    if event_name not in ("beforeinput", "input"):
        return
    if input_type in _IGNORED_AUTOFILL_INPUT_TYPES:
        return
    if input_type not in _MUTATION_INPUT_TYPES:
        return
    if not textarea.matches("textarea.review-textarea"):
        return
    form = textarea.form
    query = getattr(form, "querySelector", None)
    if query is None:
        return
    flag = query('input[name="text_was_user_edited"]')
    if flag is not None:
        flag.value = "1"


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

    def test_non_dirty_restored_textarea_is_reset_flag_stays_zero(self) -> None:
        form = _FakeReviewForm(flag=_FakeHiddenFlag())
        textarea = _FakeReviewTextarea(
            "server default",
            restored_value="browser stale",
            form=form,
        )
        restore_review_textareas_from_server_default(_FakeReviewRoot([textarea]))
        self.assertEqual(textarea.value, "server default")
        self.assertEqual(form.flag.value, "0")

    def test_dirty_form_pageshow_preserves_value_and_flag(self) -> None:
        form = _FakeReviewForm(flag=_FakeHiddenFlag(restored_value="1"))
        textarea = _FakeReviewTextarea(
            "server default",
            restored_value="genuine unsaved edit",
            form=form,
        )
        restore_review_textareas_from_server_default(_FakeReviewRoot([textarea]))
        self.assertEqual(textarea.value, "genuine unsaved edit")
        self.assertEqual(form.flag.value, "1")

    def test_dirty_source_clean_hebrew_are_restored_independently(self) -> None:
        source_form = _FakeReviewForm(flag=_FakeHiddenFlag(restored_value="1"))
        hebrew_form = _FakeReviewForm(flag=_FakeHiddenFlag())
        source = _FakeReviewTextarea(
            "source canonical",
            restored_value="source staff edit",
            form=source_form,
        )
        hebrew = _FakeReviewTextarea(
            "hebrew canonical",
            restored_value="hebrew browser stale",
            form=hebrew_form,
        )
        restore_review_textareas_from_server_default(_FakeReviewRoot([source, hebrew]))
        self.assertEqual(source.value, "source staff edit")
        self.assertEqual(source_form.flag.value, "1")
        self.assertEqual(hebrew.value, "hebrew canonical")
        self.assertEqual(hebrew_form.flag.value, "0")

    def test_restoration_does_not_mark_either_card_dirty(self) -> None:
        source_form = _FakeReviewForm()
        hebrew_form = _FakeReviewForm()
        source = _FakeReviewTextarea(
            "source canonical",
            restored_value="source stale",
            form=source_form,
        )
        hebrew = _FakeReviewTextarea(
            "hebrew canonical",
            restored_value="hebrew stale",
            form=hebrew_form,
        )
        restore_review_textareas_from_server_default(_FakeReviewRoot([source, hebrew]))
        self.assertEqual(source_form.flag.value, "0")
        self.assertEqual(hebrew_form.flag.value, "0")
        self.assertEqual(source.value, "source canonical")
        self.assertEqual(hebrew.value, "hebrew canonical")


class ReviewUserEditIntentJsContractTests(SimpleTestCase):
    def test_ctrl_or_cmd_copy_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("source", form=form)
        mark_review_textarea_user_edited(
            textarea, event_name="keydown", input_type=None
        )
        self.assertEqual(form.flag.value, "0")

    def test_ctrl_or_cmd_select_all_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("source", form=form)
        mark_review_textarea_user_edited(
            textarea, event_name="keydown", input_type=None
        )
        self.assertEqual(form.flag.value, "0")

    def test_navigation_keydown_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("source", form=form)
        mark_review_textarea_user_edited(textarea, event_name="keydown")
        self.assertEqual(form.flag.value, "0")

    def test_insert_text_marks_only_that_form_dirty(self) -> None:
        source_form = _FakeReviewForm()
        hebrew_form = _FakeReviewForm()
        source = _FakeReviewTextarea("source", form=source_form)
        _FakeReviewTextarea("hebrew", form=hebrew_form)
        mark_review_textarea_user_edited(
            source, event_name="beforeinput", input_type="insertText"
        )
        self.assertEqual(source_form.flag.value, "1")
        self.assertEqual(hebrew_form.flag.value, "0")

    def test_backspace_and_delete_mutation_marks_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("source", form=form)
        mark_review_textarea_user_edited(
            textarea, event_name="input", input_type="deleteContentBackward"
        )
        self.assertEqual(form.flag.value, "1")
        other = _FakeReviewForm()
        other_area = _FakeReviewTextarea("hebrew", form=other)
        mark_review_textarea_user_edited(
            other_area, event_name="beforeinput", input_type="deleteContentForward"
        )
        self.assertEqual(other.flag.value, "1")

    def test_high_level_paste_cut_drop_do_not_mark_without_mutation(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("source", form=form)
        for event_name in ("paste", "cut", "drop", "compositionend"):
            mark_review_textarea_user_edited(textarea, event_name=event_name)
            self.assertEqual(form.flag.value, "0", event_name)
        mark_review_textarea_user_edited(
            textarea, event_name="beforeinput", input_type="insertFromPaste"
        )
        self.assertEqual(form.flag.value, "1")
        cut_form = _FakeReviewForm()
        cut_area = _FakeReviewTextarea("cut me", form=cut_form)
        mark_review_textarea_user_edited(cut_area, event_name="cut")
        self.assertEqual(cut_form.flag.value, "0")
        mark_review_textarea_user_edited(
            cut_area, event_name="input", input_type="deleteByCut"
        )
        self.assertEqual(cut_form.flag.value, "1")
        drop_form = _FakeReviewForm()
        drop_area = _FakeReviewTextarea("drop me", form=drop_form)
        mark_review_textarea_user_edited(drop_area, event_name="drop")
        self.assertEqual(drop_form.flag.value, "0")
        mark_review_textarea_user_edited(
            drop_area, event_name="input", input_type="insertFromDrop"
        )
        self.assertEqual(drop_form.flag.value, "1")

    def test_autofill_input_type_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("canonical", form=form)
        mark_review_textarea_user_edited(
            textarea,
            event_name="input",
            input_type="insertFromAutoComplete",
        )
        self.assertEqual(form.flag.value, "0")
        mark_review_textarea_user_edited(
            textarea,
            event_name="beforeinput",
            input_type="insertReplacementText",
        )
        self.assertEqual(form.flag.value, "0")

    def test_input_without_mutation_type_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea("canonical", form=form)
        mark_review_textarea_user_edited(textarea, event_name="input")
        self.assertEqual(form.flag.value, "0")

    def test_value_default_mismatch_without_input_does_not_mark_dirty(self) -> None:
        form = _FakeReviewForm()
        textarea = _FakeReviewTextarea(
            "canonical",
            restored_value="browser changed",
            form=form,
        )
        self.assertNotEqual(textarea.value, textarea.defaultValue)
        self.assertEqual(form.flag.value, "0")


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
        self.assertIn('input[name="text_was_user_edited"]', js)
        self.assertIn("isFormMarkedUserEdited(textarea.form)", js)
        self.assertNotIn("flag.value = flag.defaultValue", js)
        self.assertIn(
            'document.addEventListener("beforeinput", onReviewTextareaContentMutation, false);',
            js,
        )
        self.assertIn(
            'document.addEventListener("input", onReviewTextareaContentMutation, false);',
            js,
        )
        self.assertNotIn(
            'document.addEventListener("keydown"',
            js,
        )
        self.assertNotIn(
            'document.addEventListener("paste"',
            js,
        )
        self.assertNotIn(
            'document.addEventListener("cut"',
            js,
        )
        self.assertNotIn(
            'document.addEventListener("drop"',
            js,
        )
        self.assertNotIn(
            'document.addEventListener("compositionend"',
            js,
        )
        self.assertIn("insertFromAutoComplete", js)
        self.assertIn("insertFromPaste", js)
        self.assertIn("deleteContentBackward", js)
        self.assertIn('flag.value = "1";', js)
        submit_start = js.find("function onSubmit(event)")
        submit_end = js.find('document.addEventListener("submit"', submit_start)
        self.assertGreater(submit_start, 0)
        self.assertGreater(submit_end, submit_start)
        submit_fn = js[submit_start:submit_end]
        self.assertNotIn("text_was_user_edited", submit_fn)
        self.assertNotIn('flag.value = "1"', submit_fn)


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
        self.assertIn('name="text_was_user_edited"', html)
        self.assertIn('name="text_was_user_edited" value="0"', html)
