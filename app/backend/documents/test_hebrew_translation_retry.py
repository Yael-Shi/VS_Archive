from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.management.commands.run_worker import Command
from documents.models import Document, DocumentTextResult
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import WorkerEnvConfig
from documents.services.gemini_engine import GeminiError, GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.hebrew_translation_retry import (
    RETRY_HEBREW_TRANSLATION_OPERATION,
    STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD,
    HebrewTranslationRetryError,
    enqueue_hebrew_translation_retry,
    execute_hebrew_translation_retry,
    run_hebrew_translation_retry,
)
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
)

ENGINE = "gemini-2.0-flash"
OTHER_ENGINE = "gemini-1.5-pro"


def _worker_env_config() -> WorkerEnvConfig:
    return WorkerEnvConfig(
        gemini_api_key="key",
        gemini_confidence_threshold=0.7,
        min_text_length=5,
        max_retries=3,
        retry_delay_seconds_1=30,
        retry_delay_seconds_2=300,
        report_window_start="00:00",
        report_send_time="08:00",
        free_tier_alert_pct=80,
        gemini_free_daily_request_limit=1500,
        gemini_free_daily_image_limit=1000,
        transkribus_free_monthly_credits=500,
        enable_hybrid_htr=False,
        enable_daily_report=False,
        smtp_host=None,
        smtp_port=None,
        smtp_username=None,
        smtp_password=None,
        default_from_email=None,
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=8192,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.7,
        transkribus_api_token=None,
        transkribus_username=None,
        transkribus_password=None,
        transkribus_collection_id=None,
        transkribus_model_id=None,
    )


def _non_hebrew_doc(**kwargs) -> Document:
    defaults = {
        "title": "English OCR doc",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.ENGLISH,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.PARTIAL,
        "file_s3_key": "doc.pdf",
        "mime_type": "application/pdf",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _usable_source(
    doc: Document,
    *,
    text: str = "recognized source text",
    engine: str = ENGINE,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
        source_revision=3,
    )


def _failed_hebrew(doc: Document, *, engine: str = ENGINE) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
        status=DocumentTextResult.Status.FAILED,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=None,
        error_code="HEBREW_TRANSLATION_FAILED",
        error_details="timeout",
    )


def _worker_message(payload: dict) -> dict:
    return {"Body": json.dumps(payload)}


def _set_processing_updated_at(doc: Document, *, age: timedelta) -> None:
    doc.processing_state_user = Document.ProcessingState.PROCESSING
    doc.save(update_fields=["processing_state_user", "updated_at"])
    Document.objects.filter(pk=doc.pk).update(updated_at=timezone.now() - age)


@override_settings(UPLOADS_BUCKET_NAME="")
class HebrewTranslationRetryWorkerTests(TestCase):
    def setUp(self):
        self.worker_env = _worker_env_config()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_successful_worker_retry_preserves_source_and_writes_hebrew(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        source = _usable_source(doc, text="keep this source exactly")
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        outcome = execute_hebrew_translation_retry(
            doc.id,
            worker_env=self.worker_env,
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.COMPLETED,
        )
        self.assertTrue(outcome.should_ack)
        source.refresh_from_db()
        self.assertEqual(source.text, "keep this source exactly")
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(hebrew.text, "translated hebrew text long enough")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_successful_worker_retry_returns_true(self, mock_translate):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_failed_worker_retry_preserves_source_writes_failed_hebrew_and_partial(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        source = _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.side_effect = GeminiError("Gemini API Error: timeout")

        outcome = execute_hebrew_translation_retry(
            doc.id,
            worker_env=self.worker_env,
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.FAILED,
        )
        self.assertEqual(outcome.failure_code, "HEBREW_TRANSLATION_FAILED")
        self.assertTrue(outcome.should_ack)
        source.refresh_from_db()
        self.assertEqual(source.text, "recognized source text")
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.status, DocumentTextResult.Status.FAILED)
        self.assertIn("timeout", hebrew.error_details or "")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_gemini_failure_persisted_as_failed_returns_true(self, mock_translate):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.side_effect = GeminiError("Gemini API Error: timeout")

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)

    def test_hebrew_documents_are_rejected_safely(self):
        doc = create_ocr_document(
            title="Hebrew doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="doc.pdf",
            mime_type="application/pdf",
        )
        _usable_source(doc)
        _failed_hebrew(doc)

        run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    def test_missing_source_is_rejected_safely(self):
        doc = _non_hebrew_doc()

        outcome = execute_hebrew_translation_retry(
            doc.id,
            worker_env=self.worker_env,
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.NOOP,
        )
        self.assertTrue(outcome.should_ack)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    def test_successful_hebrew_text_is_not_overwritten(self):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        existing = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="existing hebrew translation",
        )

        run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        existing.refresh_from_db()
        self.assertEqual(existing.text, "existing hebrew translation")

    def test_verified_hebrew_text_is_not_overwritten(self):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        existing = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text=None,
        )

        run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        existing.refresh_from_db()
        self.assertIsNone(existing.text)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_fresh_processing_defers_without_gemini_or_state_change(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        _set_processing_updated_at(doc, age=timedelta(minutes=1))

        outcome = execute_hebrew_translation_retry(
            doc.id,
            worker_env=self.worker_env,
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.DEFERRED,
        )
        self.assertFalse(outcome.should_ack)
        mock_translate.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_stale_processing_is_reclaimed_successfully(self, mock_translate):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        _set_processing_updated_at(
            doc,
            age=STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD + timedelta(minutes=1),
        )
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        mock_translate.assert_called_once()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    @patch(
        "documents.services.hebrew_translation_retry._recompute_processing_state_from_source_engine"
    )
    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_stale_processing_recompute_failure_leaves_partial(
        self, mock_translate, mock_recompute
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        _set_processing_updated_at(
            doc,
            age=STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD + timedelta(minutes=1),
        )
        mock_recompute.side_effect = HebrewTranslationRetryError(
            "no usable SOURCE_TEXT during stale reclaim"
        )

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        mock_translate.assert_not_called()
        mock_recompute.assert_called_once()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_stale_processing_no_longer_eligible_is_safe_acknowledged_noop(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="existing hebrew translation",
        )
        _set_processing_updated_at(
            doc,
            age=STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD + timedelta(minutes=1),
        )

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        mock_translate.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    def test_cross_engine_hebrew_blocks_enqueue(self):
        doc = _non_hebrew_doc()
        _usable_source(doc, engine=ENGINE)
        _failed_hebrew(doc, engine=ENGINE)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=OTHER_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="hebrew from other engine",
        )

        with self.assertRaises(HebrewTranslationRetryError):
            enqueue_hebrew_translation_retry(doc.id)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_cross_engine_hebrew_blocks_worker_claim(self, mock_translate):
        doc = _non_hebrew_doc()
        _usable_source(doc, engine=ENGINE)
        _failed_hebrew(doc, engine=ENGINE)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=OTHER_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="hebrew from other engine",
        )

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_cross_engine_hebrew_appearing_during_gemini_is_not_overwritten(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc, engine=ENGINE)
        _failed_hebrew(doc, engine=ENGINE)

        def _add_other_engine_hebrew_during_translate(*args, **kwargs):
            DocumentTextResult.objects.create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=OTHER_ENGINE,
                engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
                status=DocumentTextResult.Status.NEEDS_REVIEW,
                verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
                text="hebrew from other engine during gemini",
            )
            return GeminiResult(
                text="translated hebrew text long enough",
                engine_name=ENGINE,
            )

        mock_translate.side_effect = _add_other_engine_hebrew_during_translate

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        other = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=OTHER_ENGINE,
        )
        self.assertEqual(other.text, "hebrew from other engine during gemini")
        failed = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(failed.status, DocumentTextResult.Status.FAILED)
        self.assertIsNone(failed.text)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_duplicate_messages_do_not_overwrite_completed_translation(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)
        second_ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertEqual(mock_translate.call_count, 1)
        self.assertTrue(second_ack)
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.text, "translated hebrew text long enough")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_source_change_before_persistence_aborts_without_leaving_processing(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        source = _usable_source(doc, text="original source text")
        failed_hebrew = _failed_hebrew(doc)

        def _change_source_during_translate(*args, **kwargs):
            source.text = "changed during gemini call"
            source.save(update_fields=["text", "updated_at"])
            return GeminiResult(
                text="translated hebrew text long enough",
                engine_name=ENGINE,
            )

        mock_translate.side_effect = _change_source_during_translate

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        failed_hebrew.refresh_from_db()
        self.assertEqual(failed_hebrew.status, DocumentTextResult.Status.FAILED)
        self.assertIsNone(failed_hebrew.text)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_hebrew_appearing_during_gemini_returns_true_without_overwrite(
        self, mock_translate
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)

        def _add_successful_hebrew_during_translate(*args, **kwargs):
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=ENGINE,
                defaults={
                    "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
                    "prompt_variant": DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
                    "status": DocumentTextResult.Status.NEEDS_REVIEW,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "text": "hebrew added during gemini call",
                },
            )
            return GeminiResult(
                text="translated hebrew text long enough",
                engine_name=ENGINE,
            )

        mock_translate.side_effect = _add_successful_hebrew_during_translate

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertTrue(ack)
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.text, "hebrew added during gemini call")
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    @patch(
        "documents.services.hebrew_translation_retry.persist_hebrew_translation_result"
    )
    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_unexpected_persist_failure_returns_false_and_restores_state(
        self, mock_translate, mock_persist
    ):
        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )
        mock_persist.side_effect = RuntimeError("db write failed for test")

        outcome = execute_hebrew_translation_retry(
            doc.id,
            worker_env=self.worker_env,
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.RETRYABLE,
        )
        self.assertEqual(
            outcome.failure_code,
            "HEBREW_TRANSLATION_PERSISTENCE_RETRYABLE",
        )
        self.assertFalse(outcome.should_ack)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.update_document_processing_state_for_engine"
    )
    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_unexpected_processing_state_update_failure_returns_false(
        self, mock_translate, mock_update_state
    ):
        from documents.services.processing_state import (
            update_document_processing_state_for_engine as real_update_state,
        )

        doc = _non_hebrew_doc()
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )
        call_attempts = {"count": 0}

        def _update_state_side_effect(doc, engine):
            call_attempts["count"] += 1
            if call_attempts["count"] == 1:
                raise RuntimeError("state update failed for test")
            real_update_state(doc, engine)

        mock_update_state.side_effect = _update_state_side_effect

        ack = run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)

        self.assertFalse(ack)
        self.assertEqual(mock_update_state.call_count, 2)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)


@override_settings(UPLOADS_BUCKET_NAME="")
class HebrewTranslationRetryWorkerMessageTests(TestCase):
    def setUp(self):
        self.command = Command()
        self.command._cfg = _worker_env_config()
        self.doc = _non_hebrew_doc()
        _usable_source(self.doc)
        _failed_hebrew(self.doc)

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_ordinary_sqs_message_still_runs_normal_ocr_path(
        self, mock_get_object_bytes, mock_extract_pages, mock_transcribe
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [object()]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name=ENGINE,
            review_reasons=[],
        )

        ok = self.command._process_message(
            _worker_message({"type": "PROCESS_DOCUMENT", "document_id": self.doc.id})
        )

        self.assertTrue(ok)
        mock_get_object_bytes.assert_called_once()
        mock_extract_pages.assert_called_once()
        mock_transcribe.assert_called_once()

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_ocr_phase_one_claim_refreshes_updated_at(
        self, mock_get_object_bytes, mock_extract_pages, mock_transcribe
    ):
        doc = _non_hebrew_doc()
        old_updated_at = timezone.now() - timedelta(days=7)
        Document.objects.filter(pk=doc.pk).update(updated_at=old_updated_at)
        phase_one_updated_at: dict[str, object] = {}

        def _assert_phase_one_claim(*args, **kwargs):
            doc.refresh_from_db()
            self.assertEqual(
                doc.processing_state_user, Document.ProcessingState.PROCESSING
            )
            self.assertGreater(doc.updated_at, old_updated_at)
            phase_one_updated_at["value"] = doc.updated_at
            return (b"%PDF-1.4", "application/pdf")

        mock_get_object_bytes.side_effect = _assert_phase_one_claim
        mock_extract_pages.return_value = [object()]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name=ENGINE,
            review_reasons=[],
        )

        ok = self.command._process_message(
            _worker_message({"type": "PROCESS_DOCUMENT", "document_id": doc.id})
        )

        self.assertTrue(ok)
        mock_get_object_bytes.assert_called_once()
        mock_extract_pages.assert_called_once()
        mock_transcribe.assert_called_once()
        doc.refresh_from_db()
        self.assertGreaterEqual(doc.updated_at, phase_one_updated_at["value"])

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_translation_only_message_does_not_download_or_transcribe(
        self, mock_get_object_bytes, mock_transcribe, mock_translate
    ):
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        ok = self.command._process_message(
            _worker_message(
                {
                    "type": "PROCESS_DOCUMENT",
                    "document_id": self.doc.id,
                    "operation": RETRY_HEBREW_TRANSLATION_OPERATION,
                }
            )
        )

        self.assertTrue(ok)
        mock_get_object_bytes.assert_not_called()
        mock_transcribe.assert_not_called()
        mock_translate.assert_called_once()


@override_settings(UPLOADS_BUCKET_NAME="")
class HebrewTranslationRetryUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="hebrew_translation_retry_staff",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="hebrew_translation_retry_user",
            password="test-pass",
            is_staff=False,
        )

    def _detail_url(self, doc_id: int) -> str:
        return reverse("documents-detail-page", kwargs={"doc_id": doc_id})

    def _retry_url(self, doc_id: int) -> str:
        return reverse("documents-hebrew-translation-retry", kwargs={"doc_id": doc_id})

    def _eligible_doc(self) -> Document:
        doc = _non_hebrew_doc(visibility=Document.Visibility.PUBLIC)
        _usable_source(doc)
        _failed_hebrew(doc)
        return doc

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    @patch("documents.services.hebrew_translation_retry.send_process_document_message")
    def test_staff_post_enqueues_translation_only_operation_without_gemini(
        self, mock_send, mock_translate
    ):
        doc = self._eligible_doc()
        self.client.force_login(self.staff)
        resp = self.client.post(self._retry_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        mock_send.assert_called_once_with(
            doc.id,
            operation=RETRY_HEBREW_TRANSLATION_OPERATION,
        )
        mock_translate.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("נשלח לעיבוד", messages[0])

    @patch("documents.services.hebrew_translation_retry.send_process_document_message")
    def test_unauthorized_users_cannot_enqueue(self, mock_send):
        doc = self._eligible_doc()
        self.client.force_login(self.user)
        resp = self.client.post(self._retry_url(doc.id))
        self.assertEqual(resp.status_code, 403)
        mock_send.assert_not_called()

    @patch("documents.services.hebrew_translation_retry.send_process_document_message")
    def test_enqueue_failure_leaves_state_unchanged_and_shows_safe_feedback(
        self, mock_send
    ):
        doc = self._eligible_doc()
        original_state = doc.processing_state_user
        mock_send.side_effect = RuntimeError("sqs down for test")

        self.client.force_login(self.staff)
        resp = self.client.post(self._retry_url(doc.id), follow=True)

        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, original_state)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("שליחת התרגום לעיבוד נכשלה", messages[0])
        self.assertNotIn("sqs down for test", messages[0])

    @patch("documents.services.hebrew_translation_retry.send_process_document_message")
    def test_ineligible_post_shows_safe_feedback_without_enqueue(self, mock_send):
        doc = _non_hebrew_doc(visibility=Document.Visibility.PUBLIC)
        self.client.force_login(self.staff)
        resp = self.client.post(self._retry_url(doc.id), follow=True)

        mock_send.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("לא ניתן לשלוח תרגום לעברית לעיבוד", messages[0])
