"""Optimistic-concurrency guard so a stale review textarea cannot overwrite DTR text."""

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
from documents.services.transkribus_corrected_current_activation import (
    activate_corrected_current_sync_attempt,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.verified_text_result_edit import (
    STALE_REVIEW_FORM_MESSAGE,
    edit_verified_text_result,
    review_form_baseline_for_result_id,
)

_ENGINE = "transkribus-pylaia:stale-form"
_PARSER = "test_parser_stale_review_form_v1"
_OLD_TEXT = "Old displayed Transkribus text before activation"
_CANONICAL = "Corrected current snapshot canonical text"
_MANUAL = "Manually corrected staff transcription"


def _sha(text: str) -> str:
    return compute_sha256_hex(text)


class _AsyncClientHeaders(TypedDict):
    HTTP_X_REQUESTED_WITH: str


def _async_headers() -> _AsyncClientHeaders:
    return {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


@override_settings(UPLOADS_BUCKET_NAME="")
class StaleReviewFormGuardTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="stale_review_form_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Stale review form Hebrew",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/stale-review-form/original.pdf",
            mime_type="application/pdf",
        )

    def _english_doc(self) -> Document:
        return create_ocr_document(
            title="Stale review form English",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/stale-review-form-en/original.jpg",
            mime_type="image/jpeg",
        )

    def _pending(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-stale",
        source_revision: int = 1,
        based_on_source_revision: int | None = None,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=verification_status,
            text=text,
            source_revision=source_revision,
            based_on_source_revision=based_on_source_revision,
        )

    def _ready_snapshot(
        self, doc: Document, run: TranskribusRun, *, text: str, ts_id: str
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
            hover_eligible=False,
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

    def _hebrew_activation_fixture(self) -> dict[str, object]:
        doc = self._hebrew_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="stale-form-remote",
            pages_query="1",
            recognition_job_id="job-stale-form",
            page_index_to_page_nr={1: 1},
            engine_runtime=_ENGINE,
        )
        old_snapshot = self._ready_snapshot(doc, run, text=_OLD_TEXT, ts_id="ts-old")
        new_snapshot = self._ready_snapshot(doc, run, text=_CANONICAL, ts_id="ts-new")
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
        src_bind = TranskribusTextResultBinding.objects.create(
            text_result=source,
            snapshot=old_snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha(_OLD_TEXT),
            bound_source_revision=1,
        )
        he_bind = TranskribusTextResultBinding.objects.create(
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
            "src_bind": src_bind,
            "he_bind": he_bind,
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

    def _verified_edit_url(self, result_id: int) -> str:
        return reverse(
            "review-text-result-verified-edit", kwargs={"result_id": result_id}
        )

    def test_review_detail_renders_hidden_baseline_for_editable_cards(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Rendered source",
            source_revision=3,
        )
        resp = self._review_get(doc)
        self.assertEqual(resp.status_code, 200)
        fields = self._card_fields(resp, source.id)
        self.assertEqual(fields["expected_text_sha256"], _sha("Rendered source"))
        self.assertEqual(fields["expected_source_revision"], "3")
        self.assertContains(resp, 'name="expected_text_sha256"')
        self.assertContains(resp, 'name="expected_source_revision"')
        self.assertContains(resp, fields["expected_text_sha256"])

    def test_stale_hebrew_verify_after_activation_is_rejected(self):
        fixture = self._hebrew_activation_fixture()
        doc = fixture["doc"]
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(doc, Document)
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)

        review = self._review_get(doc)
        stale = self._card_fields(review, hebrew.id)
        self.assertEqual(stale["expected_text_sha256"], _sha(_OLD_TEXT))
        self.assertEqual(stale["expected_source_revision"], "1")

        self._activate(fixture)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**stale, "text": _OLD_TEXT},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        payload = json.loads(resp.content)
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error"), STALE_REVIEW_FORM_MESSAGE)

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
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).exists()
        )
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        new_snapshot = fixture["new_snapshot"]
        assert isinstance(new_snapshot, TranskribusTranscriptSnapshot)
        self.assertEqual(src_bind.snapshot_id, new_snapshot.pk)
        self.assertEqual(he_bind.snapshot_id, new_snapshot.pk)
        self.assertEqual(src_bind.bound_source_revision, 2)
        self.assertEqual(he_bind.bound_source_revision, 2)
        expected_src_bind = fixture["src_bind"]
        expected_he_bind = fixture["he_bind"]
        assert isinstance(expected_src_bind, TranskribusTextResultBinding)
        assert isinstance(expected_he_bind, TranskribusTextResultBinding)
        self.assertEqual(src_bind.pk, expected_src_bind.pk)
        self.assertEqual(he_bind.pk, expected_he_bind.pk)

    def test_fresh_hebrew_verify_after_activation_still_saves_and_verifies(self):
        fixture = self._hebrew_activation_fixture()
        doc = fixture["doc"]
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(doc, Document)
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)

        self._activate(fixture)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)
        self.assertEqual(fresh["expected_text_sha256"], _sha(_CANONICAL))
        self.assertEqual(fresh["expected_source_revision"], "2")

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _MANUAL},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("text_saved"))
        self.assertEqual(
            payload.get("verification_status"),
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _MANUAL)
        self.assertEqual(hebrew.text, _MANUAL)
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(hebrew.based_on_source_revision, 3)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        audit = DocumentTextResultEdit.objects.get(text_result=hebrew)
        self.assertEqual(audit.old_text, _CANONICAL)
        self.assertEqual(audit.new_text, _MANUAL)
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        self.assertEqual(src_bind.bound_source_revision, 2)

    def test_fresh_noop_hebrew_verify_does_not_bump_revision(self):
        fixture = self._hebrew_activation_fixture()
        doc = fixture["doc"]
        hebrew = fixture["hebrew"]
        source = fixture["source"]
        assert isinstance(doc, Document)
        assert isinstance(hebrew, DocumentTextResult)
        assert isinstance(source, DocumentTextResult)

        self._activate(fixture)
        review = self._review_get(doc)
        fresh = self._card_fields(review, hebrew.id)

        resp = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": _CANONICAL},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        payload = json.loads(resp.content)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("text_saved"))

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).exists()
        )

    def test_stale_source_pending_save_and_verify_are_rejected(self):
        fixture = self._hebrew_activation_fixture()
        doc = fixture["doc"]
        source = fixture["source"]
        hebrew = fixture["hebrew"]
        assert isinstance(doc, Document)
        assert isinstance(source, DocumentTextResult)
        assert isinstance(hebrew, DocumentTextResult)

        review = self._review_get(doc)
        stale = self._card_fields(review, source.id)
        self._activate(fixture)

        save = self.client.post(
            self._save_url(source.id),
            data={**stale, "text": _OLD_TEXT},
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 400)
        self.assertEqual(
            json.loads(save.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )

        verify = self.client.post(
            self._verify_url(source.id),
            data={**stale, "text": _OLD_TEXT},
            **_async_headers(),
        )
        self.assertEqual(verify.status_code, 400)
        self.assertEqual(
            json.loads(verify.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_stale_verified_edit_is_rejected(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Verified original",
            source_revision=2,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        review = self._review_get(doc)
        stale = self._card_fields(review, source.id)

        edit_verified_text_result(
            result_id=source.id,
            new_text="Server-side newer text",
            editor=self.staff,
            baseline=review_form_baseline_for_result_id(source.id),
        )

        resp = self.client.post(
            self._verified_edit_url(source.id),
            data={**stale, "text": "Stale client overwrite"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.content.decode(), STALE_REVIEW_FORM_MESSAGE)

        source.refresh_from_db()
        self.assertEqual(source.text, "Server-side newer text")
        self.assertEqual(source.source_revision, 3)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            DocumentTextResultEdit.objects.filter(text_result=source).count(), 1
        )

    def test_missing_baseline_is_fail_closed_stale(self):
        doc = self._english_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Needs baseline",
        )
        resp = self.client.post(
            self._verify_url(source.id),
            data={"text": "Needs baseline"},
            **_async_headers(),
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            json.loads(resp.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )
        source.refresh_from_db()
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(source.text, "Needs baseline")

    def test_hebrew_without_paired_source_uses_sha_only_baseline(self):
        original = "Hebrew card with no SOURCE pair"
        updated = "Server changed this HEBREW row"
        doc = self._hebrew_doc()
        hebrew = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text=original,
        )
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=doc,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).exists()
        )

        review = self._review_get(doc)
        self.assertEqual(review.status_code, 200)
        fields = self._card_fields(review, hebrew.id)
        self.assertEqual(fields["expected_text_sha256"], _sha(original))
        self.assertNotIn("expected_source_revision", fields)
        self.assertContains(review, 'name="expected_text_sha256"')
        self.assertNotContains(review, 'name="expected_source_revision"')

        save = self.client.post(
            self._save_url(hebrew.id),
            data={**fields, "text": original},
            **_async_headers(),
        )
        self.assertEqual(save.status_code, 200)
        self.assertTrue(json.loads(save.content).get("ok"))
        self.assertFalse(json.loads(save.content).get("text_saved"))

        hebrew.text = updated
        hebrew.save(update_fields=["text", "updated_at"])

        stale_save = self.client.post(
            self._save_url(hebrew.id),
            data={**fields, "text": original},
            **_async_headers(),
        )
        stale_verify = self.client.post(
            self._verify_url(hebrew.id),
            data={**fields, "text": original},
            **_async_headers(),
        )
        self.assertEqual(stale_save.status_code, 400)
        self.assertEqual(stale_verify.status_code, 400)
        self.assertEqual(
            json.loads(stale_save.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )
        self.assertEqual(
            json.loads(stale_verify.content).get("error"), STALE_REVIEW_FORM_MESSAGE
        )
        hebrew.refresh_from_db()
        self.assertEqual(hebrew.text, updated)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=hebrew).exists()
        )

        fresh_review = self._review_get(doc)
        fresh = self._card_fields(fresh_review, hebrew.id)
        self.assertEqual(fresh["expected_text_sha256"], _sha(updated))
        self.assertNotIn("expected_source_revision", fresh)
        verify = self.client.post(
            self._verify_url(hebrew.id),
            data={**fresh, "text": updated},
            **_async_headers(),
        )
        self.assertEqual(verify.status_code, 200)
        payload = json.loads(verify.content)
        self.assertTrue(payload.get("ok"))
        self.assertFalse(payload.get("text_saved"))
        self.assertEqual(
            payload.get("verification_status"),
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        hebrew.refresh_from_db()
        self.assertEqual(hebrew.text, updated)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    def test_hebrew_card_baseline_uses_paired_source_revision_not_display_helper(self):
        doc = self._hebrew_doc()
        source = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Card source",
            engine="engine-card",
            source_revision=4,
        )
        hebrew = self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Card hebrew",
            engine="engine-card",
            based_on_source_revision=4,
        )
        other_engine = "engine-other"
        self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Other engine displayed candidate",
            engine=other_engine,
            source_revision=9,
        )
        self._pending(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Other engine displayed candidate",
            engine=other_engine,
            based_on_source_revision=9,
        )
        review = self._review_get(doc)
        hebrew_fields = self._card_fields(review, hebrew.id)
        source_fields = self._card_fields(review, source.id)
        self.assertEqual(hebrew_fields["expected_source_revision"], "4")
        self.assertEqual(source_fields["expected_source_revision"], "4")
        self.assertEqual(hebrew_fields["expected_text_sha256"], _sha("Card hebrew"))
        self.assertNotEqual(
            hebrew_fields["expected_text_sha256"],
            _sha("Other engine displayed candidate"),
        )
