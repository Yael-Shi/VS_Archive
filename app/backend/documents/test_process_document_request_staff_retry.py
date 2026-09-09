from __future__ import annotations

import inspect
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
    ProcessDocumentRequest,
)
from documents.services import process_document_request_staff_retry as staff_retry
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
    abandon_process_document_request,
)
from documents.services.process_document_request_staff_retry import (
    LIVE_PAGE_LEASE_PUBLIC_MESSAGE,
    ProcessDocumentRequestStaffRetryError,
    ProcessDocumentRequestStaffRetryErrorCode,
    document_has_live_ocr_page_checkpoint_lease,
    retry_process_document_request,
)
from documents.services.sqs import SqsConfigurationError
from documents.test_hebrew_translation_retry import (
    _failed_hebrew,
    _non_hebrew_doc,
    _usable_source,
)


def _ocr_doc(**kwargs) -> Document:
    defaults = {
        "title": "Staff retry OCR",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.ENGLISH,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.RECOVERY_REQUIRED,
        "file_s3_key": "staff-retry-ocr.pdf",
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


def _staff_abandoned_request(
    document: Document,
    **overrides,
) -> ProcessDocumentRequest:
    now = timezone.now()
    values = {
        "document": document,
        "status": ProcessDocumentRequest.Status.FAILED,
        "operation": ProcessDocumentRequest.Operation.OCR,
        "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        "ocr_retry_mode": ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        "failure_code": STAFF_ABANDONED_FAILURE_CODE,
        "failure_message": "Staff released a recovery-required request without retry.",
        "completed_at": now,
        "started_at": now - timedelta(hours=1),
    }
    values.update(overrides)
    return ProcessDocumentRequest.objects.create(**values)


def _live_gemini_page(document: Document, *, expires_at) -> GeminiOcrPageCheckpoint:
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
        status=GeminiOcrAttempt.Status.IN_PROGRESS,
    )
    return GeminiOcrPageCheckpoint.objects.create(
        attempt=attempt,
        page_index=1,
        page_fingerprint="6" * 64,
        source_content_fingerprint="7" * 64,
        status=GeminiOcrPageCheckpoint.Status.RUNNING,
        lease_token=uuid.uuid4(),
        lease_expires_at=expires_at,
        started_at=timezone.now(),
    )


def _live_arabic_page(
    document: Document, *, expires_at
) -> ArabicPrintedOcrPageCheckpoint:
    attempt = ArabicPrintedOcrAttempt.objects.create(
        document=document,
        identity_fingerprint="a" * 64,
        source_fingerprint="b" * 64,
        route_fingerprint="c" * 64,
        prompt_fingerprint="d" * 64,
        config_fingerprint="e" * 64,
        prompt_contract_version="arabic-printed-banded-prompt-v1",
        expected_page_count=1,
        status=ArabicPrintedOcrAttempt.Status.IN_PROGRESS,
    )
    return ArabicPrintedOcrPageCheckpoint.objects.create(
        attempt=attempt,
        page_index=0,
        page_fingerprint="f" * 64,
        source_content_fingerprint="0" * 64,
        oriented_image_sha256="9" * 64,
        oriented_image_width=1,
        oriented_image_height=1,
        banding_contract_fingerprint="8" * 64,
        banding_strategy="vision-bands",
        status=ArabicPrintedOcrPageCheckpoint.Status.RUNNING,
        lease_token=uuid.uuid4(),
        lease_expires_at=expires_at,
        started_at=timezone.now(),
    )


SEND_PATCH = (
    "documents.services.process_document_request_enqueue."
    "send_process_document_request_message"
)


class ProcessDocumentRequestStaffRetryTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff_retry",
            password="test-pass",
            is_staff=True,
        )

    def _retry(self, request: ProcessDocumentRequest, document: Document):
        return retry_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
            initiated_by=self.staff,
            collection_id="",
            model_id="",
        )

    def test_retry_refuses_to_run_inside_explicit_atomic_block(self):
        document = _ocr_doc()
        request = _rr_request(document)

        with transaction.atomic():
            with self.assertRaisesRegex(
                RuntimeError,
                "PROCESS_DOCUMENT staff retry must run outside database transactions.",
            ):
                retry_process_document_request(
                    request_id=request.pk,
                    document_id=document.pk,
                    initiated_by=self.staff,
                    collection_id="",
                    model_id="",
                )

        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    @patch(SEND_PATCH)
    def test_upload_finalize_ocr_becomes_new_ocr_reprocess(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)
        old_token = request.lease_token
        old_pk = request.pk

        result = self._retry(request, document)

        request.refresh_from_db()
        new_request = result.enqueue_result.request
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertIsNone(request.lease_token)
        self.assertNotEqual(new_request.pk, old_pk)
        self.assertEqual(
            new_request.origin,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        self.assertEqual(new_request.operation, ProcessDocumentRequest.Operation.OCR)
        self.assertEqual(
            new_request.ocr_retry_mode,
            ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        self.assertIsNone(new_request.source_transkribus_run_id)
        self.assertNotEqual(new_request.lease_token, old_token)
        self.assertIsNone(new_request.lease_token)
        self.assertEqual(result.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        mock_send.assert_called_once_with(new_request.pk)

    @patch(SEND_PATCH)
    def test_ocr_reprocess_origin_still_creates_new_reprocess_request(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(
            document,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        old_pk = request.pk

        result = self._retry(request, document)

        request.refresh_from_db()
        new_request = result.enqueue_result.request
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertNotEqual(new_request.pk, old_pk)
        self.assertEqual(
            new_request.origin,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        mock_send.assert_called_once_with(new_request.pk)

    @patch(SEND_PATCH)
    def test_hebrew_translation_uses_translation_enqueue_only(self, mock_send):
        document = _non_hebrew_doc(
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
        )
        _usable_source(document)
        _failed_hebrew(document)
        request = _rr_request(
            document,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )

        result = self._retry(request, document)

        request.refresh_from_db()
        new_request = result.enqueue_result.request
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertNotEqual(new_request.pk, request.pk)
        self.assertEqual(
            new_request.operation,
            ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
        )
        self.assertEqual(
            new_request.origin,
            ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
        )
        self.assertEqual(new_request.ocr_retry_mode, "")
        mock_send.assert_called_once_with(new_request.pk)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(
                document=document,
                operation=ProcessDocumentRequest.Operation.OCR,
                status=ProcessDocumentRequest.Status.QUEUED,
            ).count(),
            0,
        )

    @patch(SEND_PATCH)
    def test_enqueue_failure_leaves_staff_abandoned(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)
        mock_send.side_effect = SqsConfigurationError("queue missing")

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            self._retry(request, document)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.QUEUE_UNAVAILABLE,
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(
                document=document,
                status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            ).count(),
            1,
        )

        mock_send.side_effect = None
        mock_send.reset_mock()
        with patch(
            "documents.services.process_document_request_staff_retry."
            "abandon_process_document_request",
            wraps=abandon_process_document_request,
        ) as mock_abandon:
            result = self._retry(request, document)
            mock_abandon.assert_not_called()
        self.assertEqual(result.abandoned_now, False)
        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        mock_send.assert_called_once()

    @patch(SEND_PATCH)
    def test_staff_abandoned_retries_enqueue_only(self, mock_send):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.FAILED,
        )
        request = _staff_abandoned_request(document)
        with patch(
            "documents.services.process_document_request_staff_retry."
            "abandon_process_document_request",
            wraps=abandon_process_document_request,
        ) as mock_abandon:
            result = self._retry(request, document)
            mock_abandon.assert_not_called()

        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertEqual(result.abandoned_now, False)
        self.assertEqual(result.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        mock_send.assert_called_once()

    @patch(SEND_PATCH)
    def test_other_terminal_does_not_enqueue(self, mock_send):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.FAILED,
        )
        request = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            completed_at=timezone.now(),
        )

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            self._retry(request, document)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.REQUEST_ALREADY_FINISHED,
        )
        mock_send.assert_not_called()
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=document).count(),
            1,
        )

    @patch(SEND_PATCH)
    def test_double_retry_coalesces_to_one_active_request(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)

        first = self._retry(request, document)
        second = self._retry(request, document)

        self.assertEqual(first.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        self.assertEqual(second.enqueue_result.outcome, "ALREADY_QUEUED")
        self.assertEqual(
            first.enqueue_result.request.pk,
            second.enqueue_result.request.pk,
        )
        mock_send.assert_called_once_with(first.enqueue_result.request.pk)
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

    @patch(SEND_PATCH)
    def test_document_mismatch_does_not_mutate(self, mock_send):
        document = _ocr_doc()
        other = _ocr_doc(title="Other retry doc", file_s3_key="other-retry.pdf")
        request = _rr_request(document)

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            retry_process_document_request(
                request_id=request.pk,
                document_id=other.pk,
                initiated_by=self.staff,
                collection_id="",
                model_id="",
            )

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.DOCUMENT_MISMATCH,
        )
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        mock_send.assert_not_called()

    @patch(SEND_PATCH)
    def test_running_is_refused(self, mock_send):
        document = _ocr_doc(
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

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            retry_process_document_request(
                request_id=running.pk,
                document_id=document.pk,
                initiated_by=self.staff,
                collection_id="",
                model_id="",
            )

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.STATUS_NOT_RETRYABLE,
        )
        running.refresh_from_db()
        self.assertEqual(running.status, ProcessDocumentRequest.Status.RUNNING)
        mock_send.assert_not_called()

    @patch(SEND_PATCH)
    def test_processing_overlay_is_refused(self, mock_send):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        request = _rr_request(document)

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            self._retry(request, document)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.INVALID_RECOVERY_SHAPE,
        )
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        mock_send.assert_not_called()

    @patch(SEND_PATCH)
    def test_live_arabic_page_lease_blocks_retry(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)
        page = _live_arabic_page(
            document,
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        lease_token = page.lease_token
        lease_expires_at = page.lease_expires_at

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            self._retry(request, document)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.LIVE_PAGE_LEASE,
        )
        self.assertEqual(str(ctx.exception), LIVE_PAGE_LEASE_PUBLIC_MESSAGE)
        request.refresh_from_db()
        page.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(page.lease_token, lease_token)
        self.assertEqual(page.lease_expires_at, lease_expires_at)
        mock_send.assert_not_called()

    @patch(SEND_PATCH)
    def test_live_gemini_page_lease_blocks_retry(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)
        page = _live_gemini_page(
            document,
            expires_at=timezone.now() + timedelta(minutes=20),
        )
        lease_token = page.lease_token
        lease_expires_at = page.lease_expires_at

        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            self._retry(request, document)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.LIVE_PAGE_LEASE,
        )
        request.refresh_from_db()
        page.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(page.lease_token, lease_token)
        self.assertEqual(page.lease_expires_at, lease_expires_at)
        mock_send.assert_not_called()

    @patch(SEND_PATCH)
    def test_expired_page_lease_does_not_block_retry(self, mock_send):
        document = _ocr_doc()
        request = _rr_request(document)
        gemini_page = _live_gemini_page(
            document,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        arabic_page = _live_arabic_page(
            document,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        result = self._retry(request, document)

        request.refresh_from_db()
        gemini_page.refresh_from_db()
        arabic_page.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertEqual(result.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        self.assertEqual(
            gemini_page.status,
            GeminiOcrPageCheckpoint.Status.RUNNING,
        )
        self.assertEqual(
            arabic_page.status,
            ArabicPrintedOcrPageCheckpoint.Status.RUNNING,
        )
        mock_send.assert_called_once()

    def test_live_lease_helper_is_read_only(self):
        document = _ocr_doc()
        page = _live_gemini_page(
            document,
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        token = page.lease_token
        expires = page.lease_expires_at

        self.assertTrue(document_has_live_ocr_page_checkpoint_lease(document.pk))
        page.refresh_from_db()
        self.assertEqual(page.lease_token, token)
        self.assertEqual(page.lease_expires_at, expires)

    def test_module_does_not_call_providers_or_upload_enqueue(self):
        module_source = inspect.getsource(staff_retry)
        self.assertNotIn("enqueue_uploaded_document_processing", module_source)
        self.assertNotIn("transcribe_pages", module_source)
        self.assertNotIn("google.genai", module_source)
        self.assertNotIn("send_process_document_message(", module_source)

    def test_retry_source_prefers_parked_recovery_required(self):
        document = _ocr_doc()
        older = _staff_abandoned_request(document)
        parked = _rr_request(document)
        source = staff_retry.get_staff_retry_source_process_document_request(
            document_id=document.pk
        )
        self.assertEqual(source.pk, parked.pk)
        self.assertNotEqual(source.pk, older.pk)

    def test_retry_source_allows_staff_abandoned_with_stranded_enqueue(self):
        document = _ocr_doc(processing_state_user=Document.ProcessingState.FAILED)
        abandoned = _staff_abandoned_request(document)
        ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            failure_code="ENQUEUE_SEND_FAILED",
        )
        source = staff_retry.get_staff_retry_source_process_document_request(
            document_id=document.pk
        )
        self.assertIsNotNone(source)
        self.assertEqual(source.pk, abandoned.pk)

    def test_retry_source_rejects_stale_staff_abandoned_after_completed(self):
        document = _ocr_doc(processing_state_user=Document.ProcessingState.READY)
        _staff_abandoned_request(document)
        ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            completed_at=timezone.now(),
        )
        self.assertIsNone(
            staff_retry.get_staff_retry_source_process_document_request(
                document_id=document.pk
            )
        )

    def test_retry_source_rejects_ambiguous_later_history(self):
        document = _ocr_doc(processing_state_user=Document.ProcessingState.FAILED)
        _staff_abandoned_request(document)
        ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            failure_code="ENQUEUE_SEND_FAILED",
        )
        ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            completed_at=timezone.now(),
        )
        self.assertIsNone(
            staff_retry.get_staff_retry_source_process_document_request(
                document_id=document.pk
            )
        )

    def test_retry_source_rejects_mismatched_operation_enqueue_failed(self):
        document = _ocr_doc(processing_state_user=Document.ProcessingState.FAILED)
        abandoned = _staff_abandoned_request(document)
        ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
            failure_code="ENQUEUE_SEND_FAILED",
        )
        self.assertIsNone(
            staff_retry.get_staff_retry_source_process_document_request(
                document_id=document.pk
            )
        )
        self.assertEqual(abandoned.operation, ProcessDocumentRequest.Operation.OCR)
