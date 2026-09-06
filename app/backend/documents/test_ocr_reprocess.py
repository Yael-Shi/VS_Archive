from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from typing import TypedDict
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from documents.management.commands.run_worker import Command as RunWorkerCommand
from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    DocumentTextResult,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
    ProcessDocumentRequest,
    TranskribusRun,
)
from documents.services.archive_items import create_ocr_document
from documents.services.arabic_printed_banded_document_ocr import (
    ArabicPrintedDocumentPageInput,
    process_arabic_printed_banded_document,
)
from documents.services.arabic_printed_banded_ocr import (
    AMBIGUOUS_BAND_FAILURE_CODES,
    BAND_FAILURE_PRIMARY_AMBIGUOUS,
    OUTCOME_FAILED,
    PAGE_FAILURE_VISION_AMBIGUOUS,
    ArabicPrintedBandedPageResult,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedBandPlan,
    ArabicPrintedPageSource,
    build_arabic_printed_attempt_identity,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
    persist_arabic_printed_band_failure,
    persist_arabic_printed_page_failure,
    persist_arabic_printed_vision_plan,
    reserve_arabic_printed_primary_create,
    reserve_arabic_printed_vision_call,
)
from documents.services.cloud_vision_document_text import ArabicPrintedWorkingImage
from documents.services.env_validation import WorkerEnvConfig
from documents.services import ocr_reprocess as ocr_reprocess_service
from documents.services.gemini_engine import GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    OcrRetryMode,
    assess_ocr_reprocess,
    is_ocr_reprocess_ui_eligible,
)
from documents.services.page_extraction import PageImage
from documents.services.process_document_ocr_reprocess_enqueue import (
    apply_ocr_reprocess,
)
from documents.services.process_document_outcome import ProcessDocumentDisposition

COLLECTION_ID = "col"
MODEL_ID = "42"
PROD_COLLECTION_ID = "2339723"
PROD_MODEL_ID = "564149"


class _TranskribusWorkerEnvFields(TypedDict):
    transkribus_api_token: str
    transkribus_username: str
    transkribus_password: str
    transkribus_collection_id: str
    transkribus_model_id: str


_TRANSKRIBUS_WORKER_ENV_FIELDS: _TranskribusWorkerEnvFields = {
    "transkribus_api_token": "tok",
    "transkribus_username": "u",
    "transkribus_password": "p",
    "transkribus_collection_id": COLLECTION_ID,
    "transkribus_model_id": MODEL_ID,
}


def _failed_ocr_document(**kwargs) -> Document:
    defaults = {
        "title": "Failed OCR doc",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.HEBREW,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.FAILED,
        "file_s3_key": "doc.pdf",
        "mime_type": "application/pdf",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _seed_transkribus_run(
    doc: Document,
    *,
    status: str,
    remote_doc_id: str | None = None,
    pages_query: str | None = None,
    error_code: str | None = None,
) -> TranskribusRun:
    return TranskribusRun.objects.create(
        document=doc,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id=COLLECTION_ID,
        model_id=MODEL_ID,
        status=status,
        remote_doc_id=remote_doc_id,
        pages_query=pages_query,
        page_index_to_page_nr={0: 1} if remote_doc_id else None,
        upload_id=10 if remote_doc_id else None,
        ingest_job_id="ingest-1" if remote_doc_id else None,
        error_code=error_code,
    )


class OcrReprocessServiceTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        flag_patcher = patch.dict(
            "os.environ",
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
            clear=False,
        )
        flag_patcher.start()
        self.addCleanup(flag_patcher.stop)

    def test_dry_run_upload_failure_classifies_normal_reenqueue(self):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_programming_error_preserves_document_state(self, mock_enqueue):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )
        mock_enqueue.side_effect = RuntimeError("sqs down for test")

        with self.assertRaisesMessage(RuntimeError, "sqs down for test"):
            apply_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)
        request = ProcessDocumentRequest.objects.get(document=doc)
        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_normal_sets_processing_and_enqueues_default_payload(
        self, mock_enqueue
    ):
        doc = _failed_ocr_document(upload_error="prior enqueue failed")
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )

        apply_result = apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            apply_result.assessment.retry_mode,
            OcrRetryMode.NORMAL_REENQUEUE,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        self.assertIsNone(doc.upload_error)
        request = ProcessDocumentRequest.objects.get(document=doc)
        self.assertEqual(
            request.origin,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        mock_enqueue.assert_called_once_with(request.pk)

    def test_missing_transkribus_config_blocks_misclassification_as_normal_reenqueue(
        self,
    ):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="16842456",
            pages_query="1-2",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )
        TranskribusRun.objects.filter(document=doc).update(
            collection_id=PROD_COLLECTION_ID,
            model_id=PROD_MODEL_ID,
        )

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id="",
                model_id="",
            )

        self.assertIn("TRANSKRIBUS_COLLECTION_ID", str(ctx.exception))
        self.assertIn("TRANSKRIBUS_MODEL_ID", str(ctx.exception))
        self.assertNotIn("normal_reenqueue", str(ctx.exception))

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_missing_transkribus_config_does_not_enqueue(self, mock_enqueue):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="16842456",
            pages_query="1-2",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )
        TranskribusRun.objects.filter(document=doc).update(
            collection_id=PROD_COLLECTION_ID,
            model_id=PROD_MODEL_ID,
        )

        with self.assertRaises(OcrReprocessError):
            apply_ocr_reprocess(
                doc.id,
                collection_id="",
                model_id="",
            )

        mock_enqueue.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    def test_prod_collection_model_classifies_recognition_only(self):
        doc = _failed_ocr_document()
        source = _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="16842456",
            pages_query="1-2",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )
        TranskribusRun.objects.filter(document=doc).update(
            collection_id=PROD_COLLECTION_ID,
            model_id=PROD_MODEL_ID,
        )
        source.refresh_from_db()

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=PROD_COLLECTION_ID,
            model_id=PROD_MODEL_ID,
        )

        self.assertEqual(
            assessment.retry_mode, OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        )
        self.assertEqual(assessment.source_transkribus_run_id, source.id)

    def test_dry_run_recognition_failure_classifies_recognition_only(self):
        doc = _failed_ocr_document()
        source = _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            assessment.retry_mode, OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        )
        self.assertEqual(assessment.source_transkribus_run_id, source.id)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_recognition_only_enqueues_explicit_retry_mode(self, mock_enqueue):
        doc = _failed_ocr_document()
        source = _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        apply_result = apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        request = apply_result.enqueue_result.request
        self.assertEqual(
            request.ocr_retry_mode,
            ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
        )
        self.assertEqual(request.source_transkribus_run_id, source.id)
        mock_enqueue.assert_called_once_with(request.pk)

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_general_hebrew_handwriting_does_not_require_transkribus_config(self):
        doc = _failed_ocr_document(
            handwriting_type=Document.HandwritingType.GENERAL,
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id="",
            model_id="",
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_general_hebrew_handwriting_ignores_reusable_transkribus_run(self):
        doc = _failed_ocr_document(
            handwriting_type=Document.HandwritingType.GENERAL,
        )
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_vs_handwriting_with_disabled_route_does_not_enqueue(self, mock_enqueue):
        doc = _failed_ocr_document()

        with self.assertRaises(OcrReprocessError) as ctx:
            apply_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

        self.assertIn("valid OCR route", str(ctx.exception))
        mock_enqueue.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    def test_verified_text_blocks_reprocess(self):
        doc = _failed_ocr_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified ground truth",
        )

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("VERIFIED", str(ctx.exception))

    def test_non_uploaded_document_blocks(self):
        doc = _failed_ocr_document(upload_status=Document.UploadStatus.UPLOADING)

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("UPLOADED", str(ctx.exception))

    def test_generic_partial_without_failure_evidence_blocks(self):
        doc = _failed_ocr_document(
            processing_state_user=Document.ProcessingState.PARTIAL
        )

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("not eligible for OCR reprocess", str(ctx.exception))


def _gemini_partial_failed_source_document(**kwargs) -> Document:
    defaults = {
        "title": "Gemini partial OCR failure",
        "doc_type": Document.DocType.IMAGE,
        "language": Document.Language.ENGLISH,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.PARTIAL,
        "file_s3_key": "documents/232/source/0.jpeg",
        "mime_type": "image/jpeg",
    }
    defaults.update(kwargs)
    doc = create_ocr_document(**defaults)
    DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine="ocr-dispatch",
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.FAILED,
        error_code="OCR_FAILED",
        error_details="Gemini OCR failed for test",
        text=None,
    )
    return doc


@patch.dict(
    "os.environ",
    {"ENABLE_ANTIGRAVITY_ARABIC_PRINTED": "true"},
    clear=False,
)
class ReadyUnverifiedOcrReprocessTests(TransactionTestCase):
    def _ready_unverified_antigravity_document(self) -> Document:
        doc = create_ocr_document(
            title="Arabic printed READY unverified OCR",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ready-unverified/source/0.jpeg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="usable unverified source text",
        )
        return doc

    def test_ready_unverified_is_eligible_for_normal_reenqueue(self):
        doc = self._ready_unverified_antigravity_document()

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    def test_ready_verified_text_blocks_reprocess(self):
        doc = self._ready_unverified_antigravity_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="antigravity",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified ground truth",
        )

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("VERIFIED", str(ctx.exception))


@patch.dict(
    "os.environ",
    {"ENABLE_ANTIGRAVITY_ARABIC_PRINTED": "true"},
    clear=False,
)
class AntigravityPartialOcrReprocessTests(TransactionTestCase):
    _OLDER_SOURCE_UPDATED_AT = datetime(
        2026, 8, 17, 20, 33, 17, tzinfo=dt_timezone.utc
    )
    _LATEST_SOURCE_UPDATED_AT = datetime(
        2026, 8, 24, 19, 40, 25, tzinfo=dt_timezone.utc
    )

    def _partial_arabic_printed_document(self) -> Document:
        return create_ocr_document(
            title="Arabic printed Antigravity partial OCR failure",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/320/source/0.jpeg",
            mime_type="image/jpeg",
        )

    def _partial_failed_source_document(self) -> Document:
        doc = self._partial_arabic_printed_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.FAILED,
            error_code="OCR_FAILED",
            error_details="temporary Antigravity read timeout",
            text=None,
        )
        return doc

    def _set_text_result_updated_at(
        self, row: DocumentTextResult, updated_at: datetime
    ) -> None:
        DocumentTextResult.objects.filter(pk=row.pk).update(updated_at=updated_at)

    def _hybrid_partial_later_failed_with_older_usable_source(self) -> Document:
        """Document 320 shape: later FAILED/OCR_FAILED SOURCE_TEXT, older usable rows."""
        doc = self._partial_arabic_printed_document()
        # Create the effective/latest failure first so it has the lower pk,
        # matching production ids (FAILED pk < older usable pk).
        latest_failed = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="OCR_FAILED",
            error_details="OCR dispatch failed",
            text=None,
        )
        older_source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity-preview-05-2026",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="older unverified source text",
        )
        older_hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="antigravity-preview-05-2026",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="older unverified hebrew text",
        )
        self._set_text_result_updated_at(
            latest_failed, self._LATEST_SOURCE_UPDATED_AT
        )
        self._set_text_result_updated_at(older_source, self._OLDER_SOURCE_UPDATED_AT)
        self._set_text_result_updated_at(older_hebrew, self._OLDER_SOURCE_UPDATED_AT)
        return doc

    def test_partial_failed_source_ocr_is_eligible_for_normal_reenqueue(self):
        doc = self._partial_failed_source_document()

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    def test_partial_later_failed_source_with_older_usable_source_is_eligible(self):
        doc = self._hybrid_partial_later_failed_with_older_usable_source()

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    def test_partial_later_usable_source_after_older_failed_source_is_ineligible(self):
        doc = self._partial_arabic_printed_document()
        older_failed = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="OCR_FAILED",
            error_details="older source OCR failure",
            text=None,
        )
        latest_usable = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity-preview-05-2026",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="recovered on a later attempt",
        )
        self._set_text_result_updated_at(older_failed, self._OLDER_SOURCE_UPDATED_AT)
        self._set_text_result_updated_at(
            latest_usable, self._LATEST_SOURCE_UPDATED_AT
        )

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("not eligible for OCR reprocess", str(ctx.exception))

    def test_partial_latest_source_neither_usable_nor_ocr_failed_is_ineligible(self):
        doc = self._partial_arabic_printed_document()
        older_failed = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="OCR_FAILED",
            error_details="older source OCR failure",
            text=None,
        )
        latest_other = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity-preview-05-2026",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="OCR_ROUTING_INVALID",
            error_details="latest source is not a qualifying OCR_FAILED row",
            text=None,
        )
        self._set_text_result_updated_at(older_failed, self._OLDER_SOURCE_UPDATED_AT)
        self._set_text_result_updated_at(
            latest_other, self._LATEST_SOURCE_UPDATED_AT
        )

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("not eligible for OCR reprocess", str(ctx.exception))

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_enqueues_normal_antigravity_flow(self, mock_enqueue):
        doc = self._partial_failed_source_document()

        apply_result = apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            apply_result.assessment.retry_mode,
            OcrRetryMode.NORMAL_REENQUEUE,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)

        request = ProcessDocumentRequest.objects.get(document=doc)
        self.assertEqual(
            request.ocr_retry_mode,
            ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        mock_enqueue.assert_called_once_with(request.pk)


_AR_PAGE_WIDTH = 1000
_AR_PAGE_HEIGHT = 2000


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _arabic_printed_page_sources(*labels: bytes) -> list[ArabicPrintedPageSource]:
    pages: list[ArabicPrintedPageSource] = []
    for index, label in enumerate(labels):
        pages.append(
            ArabicPrintedPageSource(
                page_index=index,
                mime_type="image/jpeg",
                source_identity="document.pdf",
                source_content_fingerprint=_sha256_bytes(label),
                oriented_image_sha256=_sha256_bytes(label + b"-oriented"),
                oriented_image_width=_AR_PAGE_WIDTH,
                oriented_image_height=_AR_PAGE_HEIGHT,
            )
        )
    return pages


class ArabicPrintedBandedPartialOcrReprocessTests(TransactionTestCase):
    def _partial_arabic_printed_document(self) -> Document:
        return create_ocr_document(
            title="Arabic printed banded PARTIAL without SOURCE_TEXT",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/banded-partial/source/0.jpeg",
            mime_type="image/jpeg",
        )

    def _seed_attempt(self, doc: Document, *labels: bytes):
        pages = _arabic_printed_page_sources(*labels)
        identity = build_arabic_printed_attempt_identity(
            pages=pages,
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            engine_key="ANTIGRAVITY",
            prompt_variant="printed",
        )
        attempt = get_or_create_arabic_printed_attempt(
            document_id=doc.id,
            identity=identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=attempt.id,
            identity=identity,
        )
        return identity, attempt, pages

    def _claim_page(self, attempt, identity, page_index: int):
        return claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=page_index,
            page_fingerprint=identity.page_fingerprints[page_index],
            source_content_fingerprint=identity.source_content_fingerprints[page_index],
            oriented_image_sha256=identity.oriented_image_sha256s[page_index],
        )

    def _fail_page_deadline(self, attempt, identity, page_index: int = 0):
        claim = self._claim_page(attempt, identity, page_index)
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code="ARABIC_PRINTED_DOCUMENT_DEADLINE",
            failure_message=f"insufficient page start budget page_index={page_index}",
        )
        return claim

    def _fence_vision_ambiguous(self, attempt, identity, page_index: int = 0):
        claim = self._claim_page(attempt, identity, page_index)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code=PAGE_FAILURE_VISION_AMBIGUOUS,
            failure_message="vision reserved without a durable plan",
        )
        return claim

    def test_fence_code_constants_match_banded_orchestrator(self):
        self.assertEqual(
            ocr_reprocess_service._ARABIC_PRINTED_VISION_AMBIGUOUS,
            PAGE_FAILURE_VISION_AMBIGUOUS,
        )
        self.assertEqual(
            ocr_reprocess_service._ARABIC_PRINTED_AMBIGUOUS_BAND_FAILURE_CODES,
            AMBIGUOUS_BAND_FAILURE_CODES,
        )

    def test_partial_without_source_and_resumable_checkpoints_is_eligible(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"page-a", b"page-b")
        self._fail_page_deadline(attempt, identity, 0)

        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())
        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )
        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    def test_partial_without_source_or_arabic_resume_evidence_is_ineligible(self):
        doc = self._partial_arabic_printed_document()

        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())
        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))
        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("not eligible for OCR reprocess", str(ctx.exception))

    def test_partial_vision_ambiguous_only_is_not_resumable(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"only")
        self._fence_vision_ambiguous(attempt, identity, 0)

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))
        with self.assertRaises(OcrReprocessError):
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

    def test_partial_mixed_fenced_and_planning_pages_is_eligible(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"page-a", b"page-b")
        self._fence_vision_ambiguous(attempt, identity, 0)

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

    def test_partial_ambiguous_band_only_is_not_resumable(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"only")
        claim = self._claim_page(attempt, identity, 0)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        draft = "مسودة"
        persist_arabic_printed_vision_plan(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
            bands=[
                ArabicPrintedBandPlan(
                    band_index=0,
                    rect_x=0,
                    rect_y=0,
                    rect_width=_AR_PAGE_WIDTH,
                    rect_height=40,
                    crop_mime="image/jpeg",
                    crop_byte_length=12,
                    crop_sha256=_sha256_bytes(b"crop-0"),
                    vision_draft_text=draft,
                    vision_draft_byte_length=len(draft.encode("utf-8")),
                    vision_draft_sha256=hashlib.sha256(
                        draft.encode("utf-8")
                    ).hexdigest(),
                )
            ],
        )
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
            failure_message="primary reserved without an interaction id",
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            failure_message="band_index=0 did not reach success",
        )

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

    def test_stale_prompt_contract_attempt_is_not_eligible(self):
        doc = self._partial_arabic_printed_document()
        pages = _arabic_printed_page_sources(b"stale")
        identity = build_arabic_printed_attempt_identity(
            pages=pages,
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            engine_key="ANTIGRAVITY",
            prompt_variant="printed",
            prompt_contract_version="arabic-printed-banded-prompt-stale",
        )
        attempt = get_or_create_arabic_printed_attempt(
            document_id=doc.id,
            identity=identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=attempt.id,
            identity=identity,
        )
        self._fail_page_deadline(attempt, identity, 0)

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

    def test_older_source_attempt_does_not_override_latest_fenced_attempt(self):
        doc = self._partial_arabic_printed_document()
        old_identity, old_attempt, _old_pages = self._seed_attempt(doc, b"older-source")
        self._fail_page_deadline(old_attempt, old_identity, 0)
        new_identity, new_attempt, _new_pages = self._seed_attempt(doc, b"newer-source")
        self._fence_vision_ambiguous(new_attempt, new_identity, 0)

        self.assertGreater(new_attempt.pk, old_attempt.pk)
        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

    def test_current_contract_attempt_remains_eligible_when_older_stale_exists(self):
        doc = self._partial_arabic_printed_document()
        stale_pages = _arabic_printed_page_sources(b"stale")
        stale_identity = build_arabic_printed_attempt_identity(
            pages=stale_pages,
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            engine_key="ANTIGRAVITY",
            prompt_variant="printed",
            prompt_contract_version="arabic-printed-banded-prompt-stale",
        )
        stale_attempt = get_or_create_arabic_printed_attempt(
            document_id=doc.id,
            identity=stale_identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=stale_attempt.id,
            identity=stale_identity,
        )
        self._fence_vision_ambiguous(stale_attempt, stale_identity, 0)

        current_identity, current_attempt, _pages = self._seed_attempt(
            doc, b"current-source"
        )
        self._fail_page_deadline(current_attempt, current_identity, 0)

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

    def test_actively_leased_running_page_is_not_eligible(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"only")
        claim = self._claim_page(attempt, identity, 0)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(page.status, ArabicPrintedOcrPageCheckpoint.Status.RUNNING)
        self.assertGreater(page.lease_expires_at, timezone.now())

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

    def test_expired_running_page_is_eligible(self):
        doc = self._partial_arabic_printed_document()
        identity, attempt, _pages = self._seed_attempt(doc, b"only")
        claim = self._claim_page(attempt, identity, 0)
        ArabicPrintedOcrPageCheckpoint.objects.filter(pk=claim.checkpoint_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch(
        "documents.services.arabic_printed_banded_document_ocr."
        "process_claimed_arabic_printed_page"
    )
    def test_reprocess_reuses_worker_selected_attempt(
        self, mock_claimed, mock_enqueue
    ):
        doc = self._partial_arabic_printed_document()
        identity, attempt, pages = self._seed_attempt(doc, b"page-a", b"page-b")
        self._fail_page_deadline(attempt, identity, 0)
        page_ids = list(
            ArabicPrintedOcrPageCheckpoint.objects.filter(attempt=attempt)
            .order_by("page_index")
            .values_list("pk", flat=True)
        )

        apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        def _fake_claimed(*, claim, **_kwargs):
            checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(
                pk=claim.checkpoint_id
            )
            return ArabicPrintedBandedPageResult(
                checkpoint_id=checkpoint.id,
                page_index=checkpoint.page_index,
                outcome=OUTCOME_FAILED,
                assembled_text="",
                page_quality="",
                runtime_engine_marker="",
                failure_code="ARABIC_PRINTED_DOCUMENT_DEADLINE",
            )

        mock_claimed.side_effect = _fake_claimed
        page_inputs = [
            ArabicPrintedDocumentPageInput(
                page_index=page.page_index,
                mime_type=page.mime_type,
                source_identity=page.source_identity,
                source_content_fingerprint=page.source_content_fingerprint,
                oriented_image_sha256=page.oriented_image_sha256,
                oriented_image_width=page.oriented_image_width,
                oriented_image_height=page.oriented_image_height,
            )
            for page in pages
        ]
        working = {
            page.page_index: ArabicPrintedWorkingImage(
                width=page.oriented_image_width,
                height=page.oriented_image_height,
                jpeg_bytes=b"",
                mime_type="image/jpeg",
                sha256=page.oriented_image_sha256,
                byte_length=0,
                rgb_pixels=b"",
            )
            for page in pages
        }

        result = process_arabic_printed_banded_document(
            document_id=doc.id,
            pages=page_inputs,
            load_working_image=working.__getitem__,
            gemini_api_key="gemini-test-key-DO-NOT-LEAK",
            cloud_vision_api_key="vision-test-key-DO-NOT-LEAK",
            absolute_deadline_monotonic=10_000.0,
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            engine_key="ANTIGRAVITY",
            prompt_variant="printed",
        )

        self.assertEqual(result.attempt_id, attempt.id)
        self.assertEqual(ArabicPrintedOcrAttempt.objects.filter(document=doc).count(), 1)
        self.assertEqual(
            list(
                ArabicPrintedOcrPageCheckpoint.objects.filter(attempt_id=attempt.id)
                .order_by("page_index")
                .values_list("pk", flat=True)
            ),
            page_ids,
        )
        mock_enqueue.assert_called_once()
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())


class GeminiPartialOcrReprocessTests(TransactionTestCase):
    def _checkpoint_failed_document(self) -> Document:
        doc = create_ocr_document(
            title="Gemini checkpoint-only failed OCR",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            handwriting_type=Document.HandwritingType.GENERAL,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/checkpoint-failed/source/0.jpeg",
            mime_type="image/jpeg",
        )
        attempt = GeminiOcrAttempt.objects.create(
            document=doc,
            identity_fingerprint="1" * 64,
            source_fingerprint="2" * 64,
            route_fingerprint="3" * 64,
            prompt_fingerprint="4" * 64,
            config_fingerprint="5" * 64,
            prompt_contract_version="gemini-ocr-prompt-v1",
            model_candidates=[
                "gemini-2.5-flash",
                "gemini-3.1-flash-lite",
            ],
            expected_page_count=1,
            status=GeminiOcrAttempt.Status.PARTIAL,
            missing_page_indices=[1],
        )
        GeminiOcrPageCheckpoint.objects.create(
            attempt=attempt,
            page_index=1,
            page_fingerprint="6" * 64,
            source_content_fingerprint="7" * 64,
            status=GeminiOcrPageCheckpoint.Status.FAILED,
            failure_code="MAX_TOKENS",
            failure_message="bounded provider failure",
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        return doc

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_partial_failed_checkpoint_without_text_result_is_eligible(self):
        doc = self._checkpoint_failed_document()

        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())
        self.assertTrue(is_ocr_reprocess_ui_eligible(doc))

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            assessment.retry_mode,
            OcrRetryMode.NORMAL_REENQUEUE,
        )
        self.assertIsNone(assessment.source_transkribus_run_id)

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_partial_without_failure_evidence_remains_ineligible(self):
        doc = create_ocr_document(
            title="Partial without OCR failure evidence",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            handwriting_type=Document.HandwritingType.GENERAL,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/partial-no-failure/source/0.jpeg",
            mime_type="image/jpeg",
        )

        self.assertFalse(is_ocr_reprocess_ui_eligible(doc))

        with self.assertRaises(OcrReprocessError):
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

    def test_partial_failed_source_ocr_is_eligible(self):
        doc = _gemini_partial_failed_source_document()

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_partial_failed_source_ocr_apply_enqueues_normal_gemini_flow(
        self, mock_enqueue
    ):
        doc = _gemini_partial_failed_source_document()

        apply_result = apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            apply_result.assessment.retry_mode,
            OcrRetryMode.NORMAL_REENQUEUE,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        request = ProcessDocumentRequest.objects.get(document=doc)
        mock_enqueue.assert_called_once_with(request.pk)

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_general_hebrew_partial_failed_source_ocr_is_eligible(self):
        doc = _gemini_partial_failed_source_document(
            language=Document.Language.HEBREW,
            handwriting_type=Document.HandwritingType.GENERAL,
        )
        DocumentTextResult.objects.filter(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        ).update(
            prompt_variant=(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
            )
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id="",
            model_id="",
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)

    def test_translation_only_partial_is_ineligible(self):
        doc = _gemini_partial_failed_source_document()
        DocumentTextResult.objects.filter(document=doc).delete()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="usable source text",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.FAILED,
            error_code="HEBREW_TRANSLATION_FAILED",
            text=None,
        )

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("not eligible for OCR reprocess", str(ctx.exception))

    def test_usable_source_text_blocks_partial_retry(self):
        doc = _gemini_partial_failed_source_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="recovered on a later attempt",
        )

        with self.assertRaises(OcrReprocessError):
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

    def test_verified_source_blocks_partial_retry(self):
        doc = _gemini_partial_failed_source_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified ground truth",
        )

        with self.assertRaises(OcrReprocessError):
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

    def test_generic_partial_without_failed_source_ocr_is_ineligible(self):
        doc = _gemini_partial_failed_source_document()
        DocumentTextResult.objects.filter(document=doc).delete()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="source ok",
        )

        with self.assertRaises(OcrReprocessError):
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
        clear=False,
    )
    def test_failed_ocr_retry_behavior_unchanged(self):
        doc = _failed_ocr_document()

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)

    @patch(
        "documents.management.commands.reprocess_failed_ocr_document.validate_required_env"
    )
    def test_command_dry_run_and_apply_support_gemini_partial_case(
        self, mock_validate_env
    ):
        doc = _gemini_partial_failed_source_document()
        mock_validate_env.return_value = WorkerEnvConfig(
            gemini_api_key="key",
            gemini_confidence_threshold=0.7,
            min_text_length=20,
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
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            **_TRANSKRIBUS_WORKER_ENV_FIELDS,
        )

        stdout = StringIO()
        call_command("reprocess_failed_ocr_document", doc.id, stdout=stdout)
        output = stdout.getvalue()
        self.assertIn("mode=dry-run", output)
        self.assertIn("retry_mode=normal_reenqueue", output)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)
        self.assertFalse(ProcessDocumentRequest.objects.filter(document=doc).exists())

        with patch(
            "documents.services.process_document_request_enqueue."
            "send_process_document_request_message"
        ) as mock_enqueue:
            stdout = StringIO()
            call_command(
                "reprocess_failed_ocr_document",
                doc.id,
                "--apply",
                stdout=stdout,
            )
            request = ProcessDocumentRequest.objects.get(document=doc)
            mock_enqueue.assert_called_once_with(request.pk)
            self.assertIsNone(request.initiated_by_id)
            self.assertEqual(
                request.origin,
                ProcessDocumentRequest.Origin.OCR_REPROCESS,
            )

        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        self.assertIn("PROCESS_DOCUMENT request handled", stdout.getvalue())
        self.assertIn("enqueue_outcome=CREATED_AND_ENQUEUED", stdout.getvalue())


class SendProcessDocumentMessageTests(SimpleTestCase):
    @patch.dict(
        "os.environ", {"SQS_QUEUE_URL": "https://sqs.example/queue"}, clear=False
    )
    @patch("documents.services.sqs.boto3.client")
    def test_normal_payload_omits_retry_fields(self, mock_boto_client):
        from documents.services.sqs import send_process_document_message

        mock_sqs = mock_boto_client.return_value
        send_process_document_message(123)
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(body, {"type": "PROCESS_DOCUMENT", "document_id": 123})

    @patch.dict(
        "os.environ", {"SQS_QUEUE_URL": "https://sqs.example/queue"}, clear=False
    )
    @patch("documents.services.sqs.boto3.client")
    def test_recognition_only_payload_includes_source_run_id(self, mock_boto_client):
        from documents.services.sqs import send_process_document_message

        mock_sqs = mock_boto_client.return_value
        send_process_document_message(
            123,
            ocr_retry_mode="transkribus_recognition_only",
            source_transkribus_run_id=456,
        )
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(
            body,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": 123,
                "ocr_retry_mode": "transkribus_recognition_only",
                "source_transkribus_run_id": 456,
            },
        )


class OcrReprocessCommandTests(TestCase):
    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
        clear=False,
    )
    @patch(
        "documents.management.commands.reprocess_failed_ocr_document.validate_required_env"
    )
    def test_command_dry_run_reports_retry_mode(self, mock_validate_env):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )
        mock_validate_env.return_value = WorkerEnvConfig(
            gemini_api_key="key",
            gemini_confidence_threshold=0.7,
            min_text_length=20,
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
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            **_TRANSKRIBUS_WORKER_ENV_FIELDS,
        )

        stdout = StringIO()
        call_command("reprocess_failed_ocr_document", doc.id, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode=dry-run", output)
        self.assertIn("retry_mode=normal_reenqueue", output)
        self.assertIn(f"collection_id={COLLECTION_ID!r}", output)
        self.assertIn(f"model_id={MODEL_ID!r}", output)
        self.assertIn("no changes made", output)


class RunWorkerOcrRetryModeTests(TestCase):
    def setUp(self):
        self.command = RunWorkerCommand()
        self.base_cfg = WorkerEnvConfig(
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
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            transkribus_recognition_only_retry=False,
            **_TRANSKRIBUS_WORKER_ENV_FIELDS,
        )
        self.command._cfg = self.base_cfg
        self.doc = create_ocr_document(
            title="Worker retry doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="doc.pdf",
            mime_type="application/pdf",
        )

    def _message(
        self,
        *,
        ocr_retry_mode: str | None = None,
        source_transkribus_run_id: int | None = None,
    ) -> dict:
        payload = {"type": "PROCESS_DOCUMENT", "document_id": self.doc.id}
        if ocr_retry_mode is not None:
            payload["ocr_retry_mode"] = ocr_retry_mode
        if source_transkribus_run_id is not None:
            payload["source_transkribus_run_id"] = source_transkribus_run_id
        return {"Body": json.dumps(payload)}

    @patch(
        "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
        return_value=GeminiResult(
            text="תרגום",
            engine_name="gemini-2.0-flash",
        ),
    )
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_normal_payload_unchanged_worker_env(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_translate,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        self.assertTrue(self.command._process_message(self._message()))

        mock_translate.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        self.assertIs(call_kw["worker_env"], self.base_cfg)
        self.assertFalse(call_kw["worker_env"].transkribus_recognition_only_retry)
        self.assertFalse(call_kw["worker_env"].transkribus_force_reprocess)
        self.assertIsNone(call_kw.get("source_transkribus_run_id"))

    @patch(
        "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
        side_effect=RuntimeError("translation failed for test"),
    )
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_successful_source_with_translation_failure_returns_partial_outcome(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_translate,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.return_value = HtrResult(
            text="recognized source text",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        payload = json.loads(self._message()["Body"])
        outcome = self.command._execute_process_document_payload(payload)

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.PARTIAL,
        )
        self.assertEqual(
            outcome.failure_code,
            "PROCESS_DOCUMENT_PARTIAL",
        )
        self.assertTrue(outcome.should_ack)
        mock_translate.assert_called_once()

        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.processing_state_user,
            Document.ProcessingState.PARTIAL,
        )

        source = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        self.assertEqual(
            source.status,
            DocumentTextResult.Status.NEEDS_REVIEW,
        )
        self.assertEqual(source.text, "recognized source text")

        hebrew = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        self.assertEqual(
            hebrew.status,
            DocumentTextResult.Status.FAILED,
        )
        self.assertEqual(
            hebrew.error_code,
            "HEBREW_TRANSLATION_FAILED",
        )

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
        clear=False,
    )
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_payload_sets_per_job_retry_flag(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.language = Document.Language.HEBREW
        self.doc.text_input_type = Document.TextInputType.HANDWRITTEN
        self.doc.save(update_fields=["language", "text_input_type"])
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name="transkribus-pylaia:42",
            review_reasons=[],
        )

        self.assertTrue(
            self.command._process_message(
                self._message(
                    ocr_retry_mode="transkribus_recognition_only",
                    source_transkribus_run_id=456,
                )
            )
        )

        mock_transcribe.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        worker_env = call_kw["worker_env"]
        expected = replace(self.base_cfg, transkribus_recognition_only_retry=True)
        self.assertEqual(worker_env, expected)
        self.assertIsNot(worker_env, self.base_cfg)
        self.assertFalse(self.base_cfg.transkribus_recognition_only_retry)
        self.assertEqual(call_kw["source_transkribus_run_id"], 456)

    @patch("documents.management.commands.run_worker.select_ocr_route")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_non_transkribus_route_fails_before_transcribe(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_select_route,
    ):
        from documents.services.ocr_routing import OcrRouteConfig

        self.doc.language = Document.Language.HEBREW
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["language", "processing_state_user"])
        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]

        self.assertTrue(
            self.command._process_message(
                self._message(
                    ocr_retry_mode="transkribus_recognition_only",
                    source_transkribus_run_id=456,
                )
            )
        )

        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.processing_state_user, Document.ProcessingState.FAILED
        )
        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertIn("transkribus_recognition_only", failure.error_details or "")
        self.assertIn(
            DocumentTextResult.OcrEngineKey.GEMINI, failure.error_details or ""
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_unknown_ocr_retry_mode_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])

        self.assertTrue(
            self.command._process_message(
                self._message(ocr_retry_mode="not_a_real_mode")
            )
        )

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.processing_state_user, Document.ProcessingState.FAILED
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_missing_source_run_id_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])

        self.assertTrue(
            self.command._process_message(
                self._message(ocr_retry_mode="transkribus_recognition_only")
            )
        )

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.processing_state_user, Document.ProcessingState.FAILED
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_non_int_source_run_id_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])
        payload = {
            "type": "PROCESS_DOCUMENT",
            "document_id": self.doc.id,
            "ocr_retry_mode": "transkribus_recognition_only",
            "source_transkribus_run_id": "456",
        }

        self.assertTrue(self.command._process_message({"Body": json.dumps(payload)}))

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(
            self.doc.processing_state_user, Document.ProcessingState.FAILED
        )
