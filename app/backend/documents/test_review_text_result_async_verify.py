"""Combined pending verify (save-if-changed) and async review mutation responses."""

from __future__ import annotations

import json
import re
from html import unescape
from typing import TypedDict
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
    review_form_baseline_for_result_id,
    is_hebrew_translation_stale,
    review_form_text_post_data,
    verify_pending_text_result,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex


def _select_for_update_model_order(captured_queries):
    order = []
    for query in captured_queries:
        if "FOR UPDATE" not in query["sql"].upper():
            continue
        sql = query["sql"].replace("`", '"').lower()
        if "documents_documenttextresult" in sql:
            order.append("documenttextresult")
        elif "documents_document" in sql:
            order.append("document")
    return order


class _AsyncClientHeaders(TypedDict):
    HTTP_X_REQUESTED_WITH: str


def _async_headers() -> _AsyncClientHeaders:
    return {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _card_ids(payload: dict) -> list[int]:
    return [int(card["result_id"]) for card in payload.get("cards") or []]


def _card_html(payload: dict, result_id: int) -> str:
    for card in payload.get("cards") or []:
        if card.get("result_id") == result_id:
            return str(card.get("html") or "")
    raise AssertionError(f"no card html for result_id={result_id}")


def _hidden_value(html: str, name: str) -> str | None:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', html)
    if match is None:
        return None
    return match.group(1)


def _textarea_default(html: str) -> str:
    match = re.search(
        r'<textarea class="review-textarea"[^>]*>(.*?)</textarea>',
        html,
        flags=re.DOTALL,
    )
    if match is None:
        raise AssertionError("no review textarea in card html")
    return unescape(match.group(1))


def _post_from_card_html(
    html: str, *, text_was_user_edited: bool = False
) -> dict[str, str]:
    sha = _hidden_value(html, "expected_text_sha256")
    if sha is None:
        raise AssertionError("missing expected_text_sha256")
    data = {
        "expected_text_sha256": sha,
        "text": _textarea_default(html),
        "text_was_user_edited": "1" if text_was_user_edited else "0",
    }
    revision = _hidden_value(html, "expected_source_revision")
    if revision is not None:
        data["expected_source_revision"] = revision
    return data


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
            baseline=review_form_baseline_for_result_id(source.id),
            text_was_user_edited=True,
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

    def test_verify_pending_locks_document_before_text_result(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="stable lock-order text",
            source_revision=1,
        )

        with CaptureQueriesContext(connection) as ctx:
            verify_pending_text_result(
                result_id=source.id,
                new_text="stable lock-order text",
                editor=self.staff,
                baseline=review_form_baseline_for_result_id(source.id),
            )

        order = _select_for_update_model_order(ctx.captured_queries)
        self.assertIn("document", order)
        self.assertIn("documenttextresult", order)
        self.assertLess(order.index("document"), order.index("documenttextresult"))
        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

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
            baseline=review_form_baseline_for_result_id(source.id),
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
                    baseline=review_form_baseline_for_result_id(source.id),
                    text_was_user_edited=True,
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
            baseline=review_form_baseline_for_result_id(source.id),
            text_was_user_edited=True,
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
            baseline=review_form_baseline_for_result_id(source.id),
            text_was_user_edited=True,
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
            baseline=review_form_baseline_for_result_id(hebrew.id),
            text_was_user_edited=True,
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
        engine = "engine-async"
        self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="restricted secret",
            engine=engine,
        )
        row = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="restricted secret",
            engine=engine,
            based_on_source_revision=1,
        )
        self.client.force_login(self.staff_restricted)
        resp = self.client.post(
            self._verify_url(row.id),
            review_form_text_post_data(row, "restricted secret"),
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

        save = self.client.post(
            self._save_url(source.id),
            review_form_text_post_data(source, "redirect me"),
        )
        self.assertEqual(save.status_code, 302)
        self.assertEqual(save["Location"], detail)

        verify = self.client.post(
            self._verify_url(source.id),
            review_form_text_post_data(source, "redirect me"),
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
            review_form_text_post_data(source, "async saved"),
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        save_body = json.loads(save.content)
        self.assertEqual(save_body["ok"], True)
        self.assertEqual(save_body["action"], "save")
        self.assertEqual(save_body["result_id"], source.id)
        self.assertEqual(save_body["document_id"], doc.id)
        self.assertEqual(
            save_body["verification_status"],
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertTrue(save_body["text_saved"])
        self.assertEqual(len(save_body["cards"]), 1)
        save_html = _card_html(save_body, source.id)
        self.assertEqual(_textarea_default(save_html), "async saved")
        self.assertEqual(_hidden_value(save_html, "text_was_user_edited"), "0")
        self.assertEqual(
            _hidden_value(save_html, "expected_text_sha256"),
            compute_sha256_hex("async saved"),
        )
        self.assertEqual(_hidden_value(save_html, "expected_source_revision"), "2")

        source.refresh_from_db()
        verify = self.client.post(
            self._verify_url(source.id),
            review_form_text_post_data(source, "async saved"),
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
        verify_html = _card_html(verify_body, source.id)
        self.assertIn("עריכת תעתוק מאושר", verify_html)
        self.assertNotIn("אשר תעתוק", verify_html)
        self.assertEqual(_textarea_default(verify_html), "async saved")

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
        self.assertEqual(reject_body["ok"], True)
        self.assertEqual(reject_body["action"], "reject")
        self.assertEqual(reject_body["result_id"], pending.id)
        self.assertEqual(reject_body["document_id"], doc.id)
        self.assertEqual(
            reject_body["verification_status"],
            DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertFalse(reject_body["text_saved"])
        reject_html = _card_html(reject_body, pending.id)
        self.assertEqual(_textarea_default(reject_html), "async reject")
        self.assertNotIn("דחה תעתוק", reject_html)
        self.assertIn("אשר תעתוק", reject_html)

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
        self.assertIn("applyAuthoritativeReviewCards", js)
        self.assertIn("existing.replaceWith(next)", js)
        self.assertNotIn("function applyVerifiedUi", js)
        self.assertNotIn("function applyRejectedUi", js)

    def test_async_save_tokens_allow_immediate_approve_without_refresh(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before save",
            source_revision=1,
        )
        stale = review_form_text_post_data(source, "after save")
        self.client.force_login(self.staff)

        save = self.client.post(
            self._save_url(source.id),
            stale,
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        save_body = json.loads(save.content)
        save_html = _card_html(save_body, source.id)
        self.assertEqual(_hidden_value(save_html, "text_was_user_edited"), "0")
        self.assertEqual(_textarea_default(save_html), "after save")

        reuse_stale = self.client.post(
            self._verify_url(source.id),
            {**stale, "text_was_user_edited": "0"},
            **_async_headers(),
        )
        self.assertEqual(reuse_stale.status_code, 400)

        verify = self.client.post(
            self._verify_url(source.id),
            _post_from_card_html(save_html),
            **_async_headers(),
        )
        self.assertEqual(verify.status_code, 200)
        verify_body = json.loads(verify.content)
        self.assertFalse(verify_body["text_saved"])
        source.refresh_from_db()
        self.assertEqual(source.text, "after save")
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_hebrew_mirror_sibling_card_is_returned_after_source_save(self):
        doc = self._create_hebrew_doc()
        engine = "engine-he-ajax-mirror"
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
        unrelated = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מנוע אחר",
            engine="engine-unrelated-he",
            source_revision=1,
        )
        self.client.force_login(self.staff)
        save = self.client.post(
            self._save_url(source.id),
            review_form_text_post_data(source, "מקור מתוקן"),
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        body = json.loads(save.content)
        self.assertCountEqual(_card_ids(body), [source.id, hebrew.id])
        self.assertNotIn(unrelated.id, _card_ids(body))
        self.assertEqual(len(body["cards"]), 2)
        source_html = _card_html(body, source.id)
        hebrew_html = _card_html(body, hebrew.id)
        self.assertEqual(_textarea_default(source_html), "מקור מתוקן")
        self.assertEqual(_textarea_default(hebrew_html), "מקור מתוקן")
        self.assertEqual(_hidden_value(source_html, "expected_source_revision"), "2")
        self.assertEqual(_hidden_value(hebrew_html, "expected_source_revision"), "2")
        self.assertEqual(_hidden_value(source_html, "text_was_user_edited"), "0")
        self.assertEqual(_hidden_value(hebrew_html, "text_was_user_edited"), "0")
        hebrew.refresh_from_db()
        self.assertEqual(hebrew.text, "מקור מתוקן")
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_hebrew_hebrew_save_returns_mirrored_source_card(self):
        doc = self._create_hebrew_doc()
        engine = "engine-he-ajax-he-edit"
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
        self.client.force_login(self.staff)
        save = self.client.post(
            self._save_url(hebrew.id),
            review_form_text_post_data(hebrew, "עברית מתוקנת"),
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        body = json.loads(save.content)
        self.assertCountEqual(_card_ids(body), [source.id, hebrew.id])
        self.assertEqual(_textarea_default(_card_html(body, source.id)), "עברית מתוקנת")
        source.refresh_from_db()
        self.assertEqual(source.text, "עברית מתוקנת")
        self.assertEqual(source.source_revision, 2)

    def test_async_mutation_does_not_return_unrelated_engine_card(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="engine a source",
            engine="engine-a",
            source_revision=1,
        )
        unrelated = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="engine b unsaved local edit",
            engine="engine-b",
            source_revision=1,
        )
        self.client.force_login(self.staff)

        save = self.client.post(
            self._save_url(source.id),
            review_form_text_post_data(source, "engine a saved"),
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        save_ids = _card_ids(json.loads(save.content))
        self.assertEqual(save_ids, [source.id])
        self.assertNotIn(unrelated.id, save_ids)

        source.refresh_from_db()
        verify = self.client.post(
            self._verify_url(source.id),
            review_form_text_post_data(source, "engine a saved"),
            **_async_headers(),
        )
        self.assertEqual(verify.status_code, 200)
        verify_ids = _card_ids(json.loads(verify.content))
        self.assertEqual(verify_ids, [source.id])
        self.assertNotIn(unrelated.id, verify_ids)

        reject = self.client.post(
            self._reject_url(unrelated.id),
            **_async_headers(),
        )
        self.assertEqual(reject.status_code, 200)
        reject_ids = _card_ids(json.loads(reject.content))
        self.assertEqual(reject_ids, [unrelated.id])
        self.assertNotIn(source.id, reject_ids)

    def test_verify_only_returns_target_card_not_hebrew_mirror(self):
        doc = self._create_hebrew_doc()
        engine = "engine-he-verify-only"
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מקור יציב",
            engine=engine,
            source_revision=2,
        )
        hebrew = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="מקור יציב",
            engine=engine,
            based_on_source_revision=2,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._verify_url(hebrew.id),
            {
                **review_form_text_post_data(hebrew, "מקור יציב"),
                "text_was_user_edited": "0",
            },
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertFalse(body["text_saved"])
        self.assertEqual(_card_ids(body), [hebrew.id])
        self.assertNotIn(source.id, _card_ids(body))
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_non_hebrew_source_save_returns_dependent_translation_card(self):
        doc = self._create_english_doc()
        engine = "engine-en-stale"
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
        unrelated = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="other engine",
            engine="engine-other",
            source_revision=1,
        )
        self.client.force_login(self.staff)
        save = self.client.post(
            self._save_url(source.id),
            review_form_text_post_data(source, "English source revised"),
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        body = json.loads(save.content)
        ids = _card_ids(body)
        self.assertCountEqual(ids, [source.id, hebrew.id])
        self.assertNotIn(unrelated.id, ids)
        hebrew.refresh_from_db()
        source.refresh_from_db()
        self.assertEqual(hebrew.text, "תרגום ישן")
        self.assertTrue(is_hebrew_translation_stale(hebrew, source))

    def test_async_reject_card_keeps_original_textarea_text(self):
        doc = self._create_english_doc()
        source = self._create_pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="keep original",
            source_revision=4,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._reject_url(source.id),
            {"text": "should not save"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content)
        self.assertFalse(body["text_saved"])
        html = _card_html(body, source.id)
        self.assertEqual(_textarea_default(html), "keep original")
        source.refresh_from_db()
        self.assertEqual(source.text, "keep original")
        self.assertEqual(source.source_revision, 4)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )
