"""Verify persists textarea text only on explicit per-form user-edit intent."""

from __future__ import annotations

import json
from typing import TypedDict

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_binding_freshness import (
    is_binding_structurally_fresh,
    is_binding_trusted_for_hover,
)
from documents.services.transkribus_corrected_current_activation import (
    activate_corrected_current_sync_attempt,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.verified_text_result_edit import (
    STALE_REVIEW_FORM_MESSAGE,
    review_form_text_post_data,
)

_ENGINE = "transkribus-pylaia:user-edit-intent"
_PARSER = "test_parser_user_edit_intent_v1"
_OLD_TEXT = "Old displayed Transkribus text before activation"
_CANONICAL = "Corrected current snapshot canonical text"
_BROWSER_NOISE = "Browser restored or autofilled textarea, not a staff edit"
_MANUAL = "Intentionally edited staff transcription"


def _sha(text: str) -> str:
    return compute_sha256_hex(text)


class _AsyncClientHeaders(TypedDict):
    HTTP_X_REQUESTED_WITH: str


def _async_headers() -> _AsyncClientHeaders:
    return {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@override_settings(UPLOADS_BUCKET_NAME="")
class ReviewVerifyUserEditIntentTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="user_edit_intent_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="User edit intent Hebrew",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/user-edit-intent/original.pdf",
            mime_type="application/pdf",
        )

    def _english_doc(self) -> Document:
        return create_ocr_document(
            title="User edit intent English",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/user-edit-intent-en/original.jpg",
            mime_type="image/jpeg",
        )

    def _pending(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-intent",
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

    def _ready_snapshot(
        self,
        doc: Document,
        run: TranskribusRun,
        *,
        text: str,
        ts_id: str,
        hover_eligible: bool,
    ) -> TranskribusTranscriptSnapshot:
        unique = f"{doc.pk}:{run.pk}:{ts_id}"
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            transkribus_run=run,
            source_kind=(
                TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC
            ),
            remote_doc_id=str(run.remote_doc_id or ""),
            collection_id=str(run.collection_id or ""),
            model_id=str(run.model_id or ""),
            recognition_job_id=str(run.recognition_job_id or ""),
            parser_version=_PARSER,
            provider_identity_fingerprint=_sha(f"prov:{unique}"),
            raw_xml_fingerprint=_sha(f"raw:{unique}"),
            canonical_text=text,
            canonical_text_sha256=_sha(text),
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.PARTIAL
            ),
            hover_eligible=hover_eligible,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id=ts_id,
            page_xml_sha256=_sha(f"xml:{snapshot.pk}:1"),
            page_xml_s3_key=f"s3://test/{snapshot.pk}/1.xml",
        )
        return snapshot

    def _hebrew_activation_fixture(self, *, hover_eligible: bool) -> dict[str, object]:
        doc = self._hebrew_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="user-edit-intent-remote",
            pages_query="1",
            recognition_job_id="job-user-edit-intent",
            page_index_to_page_nr={1: 1},
            engine_runtime=_ENGINE,
        )
        old_snapshot = self._ready_snapshot(
            doc, run, text=_OLD_TEXT, ts_id="ts-old", hover_eligible=hover_eligible
        )
        new_snapshot = self._ready_snapshot(
            doc, run, text=_CANONICAL, ts_id="ts-new", hover_eligible=hover_eligible
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=new_snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-new",
        )
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=_OLD_TEXT,
            source_revision=1,
        )
        hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=_OLD_TEXT,
            based_on_source_revision=1,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=source,
            snapshot=old_snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha(_OLD_TEXT),
            bound_source_revision=1,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=hebrew,
            snapshot=old_snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_text_sha256=_sha(_OLD_TEXT),
            bound_source_revision=1,
        )
        return {
            "doc": doc,
            "attempt": attempt,
            "new_snapshot": new_snapshot,
            "source": source,
            "hebrew": hebrew,
        }

    def _activate(self, fixture: dict[str, object]) -> None:
        source = fixture["source"]
        assert isinstance(source, DocumentTextResult)
        doc = fixture["doc"]
        attempt = fixture["attempt"]
        assert isinstance(doc, Document)
        assert isinstance(attempt, TranskribusCorrectedCurrentSyncAttempt)
        source.refresh_from_db()
        activate_corrected_current_sync_attempt(
            document_id=doc.pk,
            attempt_id=attempt.pk,
            source_text_result_id=source.pk,
            activated_by=self.staff,
            expected_source_revision=source.source_revision,
            expected_source_sha256=compute_sha256_hex(source.text or ""),
        )

    def _review_get(self, doc: Document):
        return self.client.get(reverse("review-detail-page", kwargs={"doc_id": doc.id}))

    def _card_fields(self, response, result_id: int) -> dict[str, str]:
        for card in response.context["text_result_cards"]:
            if card["row"].id == result_id:
                fields = {
                    "expected_text_sha256": card["expected_text_sha256"],
                }
                revision = card["expected_source_revision"]
                if revision is not None:
                    fields["expected_source_revision"] = str(revision)
                return fields
        self.fail(f"no review card for result_id={result_id}")

    def _verify_url(self, result_id: int) -> str:
        return reverse("review-text-result-verify", kwargs={"result_id": result_id})

    def _save_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-update-text", kwargs={"result_id": result_id}
        )

    def test_review_detail_each_editable_form_has_independent_user_edit_flag(self):
        doc = self._hebrew_doc()
        engine = "engine-two-cards"
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="source card",
            engine=engine,
        )
        hebrew = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="hebrew card",
            engine=engine,
            based_on_source_revision=1,
        )
        resp = self._review_get(doc)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertEqual(html.count('name="text_was_user_edited"'), 2)
        self.assertEqual(html.count('name="text_was_user_edited" value="0"'), 2)
        source_url = reverse(
            "review-text-result-update-text", kwargs={"result_id": source.id}
        )
        hebrew_url = reverse(
            "review-text-result-update-text", kwargs={"result_id": hebrew.id}
        )
        source_idx = html.find(source_url)
        hebrew_idx = html.find(hebrew_url)
        self.assertGreater(source_idx, 0)
        self.assertGreater(hebrew_idx, 0)
        first, second = sorted((source_idx, hebrew_idx))
        first_form = html[first:second]
        second_form = html[second : second + 1800]
        self.assertIn('name="text_was_user_edited" value="0"', first_form)
        self.assertIn('name="text_was_user_edited" value="0"', second_form)

    def test_hebrew_verify_without_user_edit_ignores_posted_textarea(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        self._activate(fixture)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        source_edit_ids_before = set(
            DocumentTextResultEdit.objects.filter(text_result=source).values_list(
                "id", flat=True
            )
        )
        hebrew_edit_ids_before = set(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).values_list(
                "id", flat=True
            )
        )
        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _BROWSER_NOISE, "text_was_user_edited": "0"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("text_saved"))
        self.assertEqual(
            payload.get("verification_status"),
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            set(
                DocumentTextResultEdit.objects.filter(text_result=source).values_list(
                    "id", flat=True
                )
            ),
            source_edit_ids_before,
        )
        self.assertEqual(
            DocumentTextResultEdit.objects.filter(text_result=source).count(),
            len(source_edit_ids_before),
        )
        self.assertEqual(
            set(
                DocumentTextResultEdit.objects.filter(text_result=hebrew).values_list(
                    "id", flat=True
                )
            ),
            hebrew_edit_ids_before,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew)
            .exclude(id__in=hebrew_edit_ids_before)
            .exists()
        )

    def test_hebrew_verify_missing_user_edit_flag_ignores_posted_textarea(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        self._activate(fixture)
        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _BROWSER_NOISE},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(json.loads(resp.content).get("text_saved"))
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).exists()
        )

    def test_hebrew_verify_with_user_edit_saves_mirrors_and_verifies(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        self._activate(fixture)
        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _MANUAL, "text_was_user_edited": "1"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("text_saved"))

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _MANUAL)
        self.assertEqual(hebrew.text, _MANUAL)
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(hebrew.based_on_source_revision, 3)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        audit = DocumentTextResultEdit.objects.get(text_result=hebrew)
        self.assertEqual(audit.old_text, _CANONICAL)
        self.assertEqual(audit.new_text, _MANUAL)

    def test_source_verify_without_user_edit_ignores_posted_textarea(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="canonical source",
            source_revision=4,
        )
        post = review_form_text_post_data(
            source,
            _BROWSER_NOISE,
            text_was_user_edited=False,
        )
        resp = self.client.post(
            self._verify_url(source.id), data=post, **_async_headers()
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("text_saved"))
        source.refresh_from_db()
        self.assertEqual(source.text, "canonical source")
        self.assertEqual(source.source_revision, 4)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=source).exists()
        )

    def test_source_verify_with_user_edit_saves_and_verifies(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="canonical source",
            source_revision=4,
        )
        post = review_form_text_post_data(
            source,
            "edited source",
            text_was_user_edited=True,
        )
        resp = self.client.post(
            self._verify_url(source.id), data=post, **_async_headers()
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("text_saved"))
        source.refresh_from_db()
        self.assertEqual(source.text, "edited source")
        self.assertEqual(source.source_revision, 5)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        audit = DocumentTextResultEdit.objects.get(text_result=source)
        self.assertEqual(audit.old_text, "canonical source")
        self.assertEqual(audit.new_text, "edited source")

    def test_stale_baseline_rejects_even_with_user_edit_flag(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        stale = self._card_fields(review, hebrew.id)
        self._activate(fixture)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**stale, "text": _MANUAL, "text_was_user_edited": "1"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            json.loads(resp.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).exists()
        )

        stale_source = self._card_fields(review, source.id)
        source_resp = self.client.post(
            self._verify_url(source.id),
            data={**stale_source, "text": _MANUAL, "text_was_user_edited": "1"},
            **_async_headers(),
        )
        self.assertEqual(source_resp.status_code, 400)
        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(source.text, _CANONICAL)

    def test_explicit_save_persists_without_user_edit_flag(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before save",
            source_revision=2,
        )
        resp = self.client.post(
            self._save_url(source.id),
            data=review_form_text_post_data(source, "explicit save"),
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("text_saved"))
        source.refresh_from_db()
        self.assertEqual(source.text, "explicit save")
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertTrue(
            DocumentTextResultEdit.objects.filter(text_result=source).exists()
        )

    def test_hebrew_verify_without_user_edit_keeps_fresh_hover_binding(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        self._activate(fixture)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertTrue(is_binding_structurally_fresh(source))
        self.assertTrue(is_binding_structurally_fresh(hebrew))
        self.assertTrue(is_binding_trusted_for_hover(source))
        self.assertTrue(is_binding_trusted_for_hover(hebrew))
        src_bind_before = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind_before = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        revision_before = source.source_revision

        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)
        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _BROWSER_NOISE, "text_was_user_edited": "0"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertFalse(payload.get("text_saved"))
        hebrew_html = None
        for card in payload.get("cards") or []:
            if card.get("result_id") == hebrew.id:
                hebrew_html = card.get("html") or ""
                break
        self.assertIsNotNone(hebrew_html)
        self.assertEqual(len(payload.get("cards") or []), 1)
        self.assertEqual(payload["cards"][0]["result_id"], hebrew.id)
        self.assertIn("עריכת תרגום מאושר", hebrew_html)
        self.assertNotIn("אשר תעתוק", hebrew_html)

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, revision_before)
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        self.assertEqual(src_bind.pk, src_bind_before.pk)
        self.assertEqual(he_bind.pk, he_bind_before.pk)
        self.assertEqual(src_bind.bound_text_sha256, _sha(_CANONICAL))
        self.assertEqual(he_bind.bound_text_sha256, _sha(_CANONICAL))
        self.assertEqual(src_bind.bound_source_revision, revision_before)
        self.assertEqual(he_bind.bound_source_revision, revision_before)
        self.assertTrue(is_binding_structurally_fresh(source, binding=src_bind))
        self.assertTrue(is_binding_structurally_fresh(hebrew, binding=he_bind))
        self.assertTrue(is_binding_trusted_for_hover(source, binding=src_bind))
        self.assertTrue(is_binding_trusted_for_hover(hebrew, binding=he_bind))

    def test_hebrew_verify_with_user_edit_may_stale_binding(self):
        fixture = self._hebrew_activation_fixture(hover_eligible=True)
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)
        self._activate(fixture)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertTrue(is_binding_trusted_for_hover(source))
        self.assertTrue(is_binding_trusted_for_hover(hebrew))

        doc = fixture["doc"]
        assert isinstance(doc, Document)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)
        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _MANUAL, "text_was_user_edited": "1"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        self.assertEqual(src_bind.bound_source_revision, 2)
        self.assertEqual(source.source_revision, 3)
        self.assertFalse(is_binding_structurally_fresh(source, binding=src_bind))
        self.assertFalse(is_binding_structurally_fresh(hebrew, binding=he_bind))
        self.assertFalse(is_binding_trusted_for_hover(source, binding=src_bind))
        self.assertFalse(is_binding_trusted_for_hover(hebrew, binding=he_bind))
