from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    ProcessDocumentRequestEnqueueErrorCode,
)
from documents.services.process_document_upload_enqueue import (
    UploadProcessEnqueueError,
    UploadProcessEnqueueErrorCode,
    enqueue_uploaded_document_processing,
)


def _result(
    request: ProcessDocumentRequest,
    outcome: EnqueueOutcome,
    *,
    created: bool = False,
    message_sent: bool | None = False,
    send_attempted: bool = False,
) -> EnqueueResult:
    return EnqueueResult(
        outcome=outcome,
        request=request,
        created=created,
        message_sent=message_sent,
        observed_status=request.status,
        send_attempted=send_attempted,
    )


class UploadProcessDocumentEnqueueTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.user = User.objects.create_user(
            username="upload-process-enqueue-staff",
            password="test-pass",
            is_staff=True,
        )
        self.document = create_ocr_document(
            title="Upload enqueue document",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.FAILED,
            upload_error="prior queue failure",
            file_s3_key="documents/1/original.pdf",
            mime_type="application/pdf",
        )

    def _queued_request(self) -> ProcessDocumentRequest:
        return ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        )

    def _enqueue_failed_request(
        self,
        *,
        failure_code: str = "ENQUEUE_SEND_FAILED",
    ) -> ProcessDocumentRequest:
        return ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            failure_code=failure_code,
            failure_message="safe stored failure",
        )

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_queued_result_uses_exact_upload_contract_and_sets_processing(
        self,
        mock_enqueue,
    ):
        request = self._queued_request()
        mock_enqueue.return_value = _result(
            request,
            "CREATED_AND_ENQUEUED",
            created=True,
            message_sent=True,
            send_attempted=True,
        )

        result = enqueue_uploaded_document_processing(
            document_id=self.document.pk,
            initiated_by=self.user,
        )

        self.assertEqual(result.request.pk, request.pk)
        mock_enqueue.assert_called_once_with(
            document_id=self.document.pk,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            source_transkribus_run_id=None,
            initiated_by=self.user,
        )
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_terminal_result_preserves_worker_document_state(
        self,
        mock_enqueue,
    ):
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            completed_at=timezone.now(),
        )
        mock_enqueue.return_value = _result(
            request,
            "ALREADY_TERMINAL",
        )

        enqueue_uploaded_document_processing(
            document_id=self.document.pk,
            initiated_by=self.user,
        )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_stale_queued_result_does_not_overwrite_terminal_document_state(
        self,
        mock_enqueue,
    ):
        request = self._queued_request()
        stale_result = _result(
            request,
            "ALREADY_QUEUED",
        )

        ProcessDocumentRequest.objects.filter(pk=request.pk).update(
            status=ProcessDocumentRequest.Status.COMPLETED,
            completed_at=timezone.now(),
        )
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_enqueue.return_value = stale_result

        enqueue_uploaded_document_processing(
            document_id=self.document.pk,
            initiated_by=self.user,
        )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_definite_enqueue_failure_sets_safe_failed_state(
        self,
        mock_enqueue,
    ):
        request = self._enqueue_failed_request()
        mock_enqueue.return_value = _result(
            request,
            "ENQUEUE_FAILED",
            send_attempted=True,
        )

        with self.assertRaises(UploadProcessEnqueueError) as ctx:
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            UploadProcessEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertEqual(ctx.exception.outcome, "ENQUEUE_FAILED")
        self.assertNotIn("ENQUEUE_SEND_FAILED", str(ctx.exception))

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(
            self.document.upload_error,
            "Document processing could not be queued. Please try again.",
        )

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_ambiguous_enqueue_failure_uses_same_safe_boundary(
        self,
        mock_enqueue,
    ):
        request = self._enqueue_failed_request(
            failure_code="ENQUEUE_OUTCOME_UNKNOWN",
        )
        mock_enqueue.return_value = _result(
            request,
            "ENQUEUE_OUTCOME_UNKNOWN",
            message_sent=None,
            send_attempted=True,
        )

        with self.assertRaises(UploadProcessEnqueueError) as ctx:
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            UploadProcessEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(
            ctx.exception.outcome,
            "ENQUEUE_OUTCOME_UNKNOWN",
        )
        self.assertNotIn("ENQUEUE_OUTCOME_UNKNOWN", str(ctx.exception))

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_stale_failure_result_does_not_overwrite_worker_owned_state(
        self,
        mock_enqueue,
    ):
        request = self._enqueue_failed_request()
        stale_result = _result(
            request,
            "ENQUEUE_FAILED",
            send_attempted=True,
        )

        now = timezone.now()
        ProcessDocumentRequest.objects.filter(pk=request.pk).update(
            status=ProcessDocumentRequest.Status.RUNNING,
            lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
            failure_code="",
            failure_message="",
        )
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_enqueue.return_value = stale_result

        with self.assertRaises(UploadProcessEnqueueError):
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_active_conflict_is_explicit_and_does_not_mutate_document(
        self,
        mock_enqueue,
    ):
        request = self._queued_request()
        mock_enqueue.return_value = _result(
            request,
            "ACTIVE_REQUEST_CONFLICT",
        )

        with self.assertRaises(UploadProcessEnqueueError) as ctx:
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            UploadProcessEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
        )
        self.assertEqual(ctx.exception.http_status, 409)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(self.document.upload_error, "prior queue failure")

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_recovery_required_is_explicit_and_does_not_mutate_document(
        self,
        mock_enqueue,
    ):
        now = timezone.now()
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            lease_token=uuid.uuid4(),
            started_at=now - timedelta(hours=1),
        )
        mock_enqueue.return_value = _result(
            request,
            "BLOCKED_RECOVERY_REQUIRED",
        )

        with self.assertRaises(UploadProcessEnqueueError) as ctx:
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            UploadProcessEnqueueErrorCode.RECOVERY_REQUIRED,
        )
        self.assertEqual(ctx.exception.http_status, 409)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(self.document.upload_error, "prior queue failure")

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request"
    )
    def test_request_validation_error_is_wrapped_with_safe_message(
        self,
        mock_enqueue,
    ):
        mock_enqueue.side_effect = ProcessDocumentRequestEnqueueError(
            ProcessDocumentRequestEnqueueErrorCode.INVALID_REQUEST_PAYLOAD,
            "internal validation detail",
        )

        with self.assertRaises(UploadProcessEnqueueError) as ctx:
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            UploadProcessEnqueueErrorCode.REQUEST_REJECTED,
        )
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertNotIn("internal validation detail", str(ctx.exception))

    @patch(
        "documents.services.process_document_upload_enqueue."
        "enqueue_process_document_request",
        side_effect=RuntimeError("programming failure"),
    )
    def test_programming_error_propagates(self, mock_enqueue):
        with self.assertRaisesMessage(RuntimeError, "programming failure"):
            enqueue_uploaded_document_processing(
                document_id=self.document.pk,
                initiated_by=self.user,
            )

        mock_enqueue.assert_called_once()
