from __future__ import annotations

import inspect
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
    ProcessDocumentRequest,
)
from documents.services import process_document_request_staff_recovery as staff_recovery
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_enqueue import (
    enqueue_process_document_request,
)
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
    ProcessDocumentRequestStaffAbandonError,
    ProcessDocumentRequestStaffAbandonErrorCode,
    StaffAbandonOutcome,
    abandon_process_document_request,
)
from documents.test_hebrew_translation_retry import (
    ENGINE,
    _non_hebrew_doc,
    _usable_source,
)


def _hebrew_doc(**kwargs) -> Document:
    defaults = {
        "title": "Staff abandon OCR",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.HEBREW,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.RECOVERY_REQUIRED,
        "file_s3_key": "staff-abandon.pdf",
        "mime_type": "application/pdf",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _rr_request(document: Document, **overrides) -> ProcessDocumentRequest:
    now = timezone.now()
    values = {
        "document": document,
        "status": ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        "operation": ProcessDocumentRequest.Operation.OCR,
        "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        "ocr_retry_mode": ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        "lease_token": uuid.uuid4(),
        "started_at": now - timedelta(hours=1),
    }
    values.update(overrides)
    return ProcessDocumentRequest.objects.create(**values)


def _usable_hebrew_pair(doc: Document, *, engine: str = ENGINE) -> None:
    DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text="displayed source text",
    )
    DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text="displayed hebrew text",
    )


class ProcessDocumentRequestStaffAbandonTests(TestCase):
    def test_rr_overlay_without_results_becomes_failed(self):
        document = _hebrew_doc()
        request = _rr_request(document)
        started_at = request.started_at

        result = abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(result.outcome, StaffAbandonOutcome.ABANDONED)
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNotNone(request.completed_at)
        self.assertEqual(request.started_at, started_at)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertFalse(DocumentTextResult.objects.filter(document=document).exists())

    def test_usable_displayed_source_restores_engine_rollup(self):
        document = _hebrew_doc()
        _usable_hebrew_pair(document)
        request = _rr_request(document)

        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        document.refresh_from_db()
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.READY,
        )

    def test_recoverable_checkpoint_evidence_becomes_partial(self):
        document = _hebrew_doc(
            handwriting_type=Document.HandwritingType.GENERAL,
        )
        attempt = GeminiOcrAttempt.objects.create(
            document=document,
            identity_fingerprint="1" * 64,
            source_fingerprint="2" * 64,
            route_fingerprint="3" * 64,
            prompt_fingerprint="4" * 64,
            config_fingerprint="5" * 64,
            prompt_contract_version="gemini-ocr-prompt-v1",
            model_candidates=["gemini-2.5-flash"],
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
        request = _rr_request(document)

        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        document.refresh_from_db()
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.PARTIAL,
        )

    def test_hebrew_translation_overlay_recomputes_from_source_engine(self):
        document = _non_hebrew_doc(
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
        )
        _usable_source(document)
        request = _rr_request(
            document,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )

        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        document.refresh_from_db()
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.PARTIAL,
        )
        self.assertEqual(
            DocumentTextResult.objects.get(
                document=document,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).text,
            "recognized source text",
        )

    def test_ordinary_document_state_is_left_unchanged(self):
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.READY,
        )
        request = _rr_request(document)

        result = abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        document.refresh_from_db()
        self.assertEqual(result.outcome, StaffAbandonOutcome.ABANDONED)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.READY,
        )

    def test_running_queued_and_enqueue_failed_are_refused(self):
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        now = timezone.now()
        running = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )
        with self.assertRaises(ProcessDocumentRequestStaffAbandonError) as ctx:
            abandon_process_document_request(
                request_id=running.pk,
                document_id=document.pk,
            )
        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffAbandonErrorCode.STATUS_NOT_ABANDONABLE,
        )
        running.refresh_from_db()
        self.assertEqual(running.status, ProcessDocumentRequest.Status.RUNNING)

        running.delete()
        queued = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        with self.assertRaises(ProcessDocumentRequestStaffAbandonError):
            abandon_process_document_request(
                request_id=queued.pk,
                document_id=document.pk,
            )

        queued.delete()
        enqueue_failed = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            failure_code="ENQUEUE_SEND_FAILED",
        )
        with self.assertRaises(ProcessDocumentRequestStaffAbandonError):
            abandon_process_document_request(
                request_id=enqueue_failed.pk,
                document_id=document.pk,
            )

    def test_already_terminal_is_idempotent(self):
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.FAILED,
        )
        completed_at = timezone.now()
        request = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            completed_at=completed_at,
        )

        result = abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        request.refresh_from_db()
        self.assertEqual(result.outcome, StaffAbandonOutcome.ALREADY_TERMINAL)
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.completed_at, completed_at)

    def test_document_mismatch_is_refused(self):
        document = _hebrew_doc()
        other = _hebrew_doc(title="Other abandon doc", file_s3_key="other.pdf")
        request = _rr_request(document)
        with self.assertRaises(ProcessDocumentRequestStaffAbandonError) as ctx:
            abandon_process_document_request(
                request_id=request.pk,
                document_id=other.pk,
            )
        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffAbandonErrorCode.DOCUMENT_MISMATCH,
        )

    def test_service_does_not_call_sqs_or_providers(self):
        module_source = inspect.getsource(staff_recovery)
        self.assertNotIn("send_process_document", module_source)
        self.assertNotIn("transcribe_pages", module_source)
        self.assertNotIn("google.genai", module_source)


class ProcessDocumentRequestStaffAbandonCommitTests(TransactionTestCase):
    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_active_unique_slot_is_released_after_commit(self, mock_send):
        document = _hebrew_doc()
        request = _rr_request(document)

        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        result = enqueue_process_document_request(
            document_id=document.pk,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertNotEqual(result.request.pk, request.pk)
        mock_send.assert_called_once()
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(
                document=document,
                status__in=(
                    ProcessDocumentRequest.Status.QUEUED,
                    ProcessDocumentRequest.Status.RUNNING,
                    ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                ),
            ).count(),
            1,
        )
