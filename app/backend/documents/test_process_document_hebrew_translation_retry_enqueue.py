from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from botocore.exceptions import EndpointConnectionError
from django.contrib.auth.models import User
from django.test import TransactionTestCase
from django.utils import timezone

from documents.models import Document, DocumentTextResult, ProcessDocumentRequest
from documents.services.archive_items import create_ocr_document
from documents.services.hebrew_translation_retry import (
    HebrewTranslationRetryError,
)
from documents.services.process_document_hebrew_translation_retry_enqueue import (
    HebrewTranslationRetryEnqueueError,
    HebrewTranslationRetryEnqueueErrorCode,
    enqueue_hebrew_translation_retry,
)
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    ProcessDocumentRequestEnqueueErrorCode,
)
from documents.services.sqs import SqsConfigurationError

ENGINE = "gemini-2.0-flash"


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


class HebrewTranslationRetryDurableEnqueueTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="translation-retry-enqueue-staff",
            is_staff=True,
        )
        self.document = create_ocr_document(
            title="Translation retry enqueue document",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/1/source.pdf",
            mime_type="application/pdf",
        )
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="recognized source text",
        )
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="HEBREW_TRANSLATION_FAILED",
            error_details="prior translation failure",
        )

    def _request(
        self,
        *,
        status: str = ProcessDocumentRequest.Status.QUEUED,
        operation: str = ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
        origin: str = ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
        initiated_by: User | None = None,
    ) -> ProcessDocumentRequest:
        values = {
            "document": self.document,
            "initiated_by": initiated_by or self.user,
            "status": status,
            "operation": operation,
            "origin": origin,
            "ocr_retry_mode": (
                ""
                if operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION
                else ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE
            ),
        }
        if status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
            values["failure_code"] = "ENQUEUE_SEND_FAILED"
            values["failure_message"] = "safe stored failure"
        elif status == ProcessDocumentRequest.Status.RUNNING:
            values["lease_token"] = uuid.uuid4()
            values["lease_expires_at"] = timezone.now() + timedelta(minutes=45)
            values["started_at"] = timezone.now()
        elif status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
            values["lease_token"] = uuid.uuid4()
            values["started_at"] = timezone.now() - timedelta(hours=1)
        elif status in {
            ProcessDocumentRequest.Status.COMPLETED,
            ProcessDocumentRequest.Status.PARTIAL,
            ProcessDocumentRequest.Status.FAILED,
        }:
            values["completed_at"] = timezone.now()
            if status == ProcessDocumentRequest.Status.PARTIAL:
                values["failure_code"] = "PROCESS_DOCUMENT_PARTIAL"
            elif status == ProcessDocumentRequest.Status.FAILED:
                values["failure_code"] = "PROCESS_DOCUMENT_FAILED"
        return ProcessDocumentRequest.objects.create(**values)

    @patch(
        "documents.services.process_document_hebrew_translation_retry_enqueue."
        "enqueue_process_document_request"
    )
    def test_request_uses_exact_contract_and_actor_without_mutating_document(
        self,
        mock_enqueue,
    ):
        request = self._request()
        mock_enqueue.return_value = _result(
            request,
            "CREATED_AND_ENQUEUED",
            created=True,
            message_sent=True,
            send_attempted=True,
        )
        original_state = self.document.processing_state_user
        original_error = self.document.upload_error

        result = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )

        mock_enqueue.assert_called_once_with(
            document_id=self.document.pk,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
            source_transkribus_run_id=None,
            initiated_by=self.user,
        )
        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.document.refresh_from_db()
        self.assertEqual(self.document.processing_state_user, original_state)
        self.assertEqual(self.document.upload_error, original_error)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_sequential_double_click_coalesces_without_second_send(self, mock_send):
        first = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )
        second = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )

        self.assertEqual(first.outcome, "CREATED_AND_ENQUEUED")
        self.assertEqual(second.outcome, "ALREADY_QUEUED")
        self.assertEqual(first.request.pk, second.request.pk)
        mock_send.assert_called_once_with(first.request.pk)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            1,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_terminal_history_allows_new_intentional_retry(self, mock_send):
        terminal = self._request(status=ProcessDocumentRequest.Status.COMPLETED)

        result = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertNotEqual(result.request.pk, terminal.pk)
        mock_send.assert_called_once_with(result.request.pk)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            2,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_matching_enqueue_failed_request_is_safely_retried(self, mock_send):
        original_actor = User.objects.create_user(username="original-actor")
        failed = self._request(
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            initiated_by=original_actor,
        )

        result = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )

        self.assertEqual(result.outcome, "REENQUEUED")
        self.assertEqual(result.request.pk, failed.pk)
        mock_send.assert_called_once_with(failed.pk)
        failed.refresh_from_db()
        self.assertEqual(failed.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertEqual(failed.initiated_by, self.user)
        self.assertEqual(failed.failure_code, "")
        self.assertEqual(failed.failure_message, "")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_enqueue_failed_retry_does_not_bypass_new_overwrite_guard(
        self,
        mock_send,
    ):
        failed = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-new",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="existing Hebrew translation",
        )

        with self.assertRaises(HebrewTranslationRetryError):
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        mock_send.assert_not_called()
        failed.refresh_from_db()
        self.assertEqual(
            failed.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_running_request_coalesces_after_worker_owns_document(self, mock_send):
        running = self._request(status=ProcessDocumentRequest.Status.RUNNING)
        self.document.processing_state_user = Document.ProcessingState.PROCESSING
        self.document.save(
            update_fields=["processing_state_user", "updated_at"],
        )

        result = enqueue_hebrew_translation_retry(
            self.document.pk,
            initiated_by=self.user,
        )

        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertEqual(result.request.pk, running.pk)
        self.assertFalse(result.send_attempted)
        mock_send.assert_not_called()

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_recovery_required_is_safe_typed_conflict(self, mock_send):
        recovery = self._request(
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.document.processing_state_user = Document.ProcessingState.PROCESSING
        self.document.save(
            update_fields=["processing_state_user", "updated_at"],
        )

        with self.assertRaises(HebrewTranslationRetryEnqueueError) as raised:
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            raised.exception.code,
            HebrewTranslationRetryEnqueueErrorCode.RECOVERY_REQUIRED,
        )
        self.assertEqual(raised.exception.http_status, 409)
        self.assertEqual(
            raised.exception.outcome,
            "BLOCKED_RECOVERY_REQUIRED",
        )
        self.assertNotIn(str(recovery.pk), raised.exception.public_message)
        mock_send.assert_not_called()

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_different_active_request_is_safe_typed_conflict(self, mock_send):
        conflict = self._request(
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        self.document.processing_state_user = Document.ProcessingState.PROCESSING
        self.document.save(
            update_fields=["processing_state_user", "updated_at"],
        )

        with self.assertRaises(HebrewTranslationRetryEnqueueError) as raised:
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            raised.exception.code,
            HebrewTranslationRetryEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
        )
        self.assertEqual(raised.exception.http_status, 409)
        self.assertEqual(
            raised.exception.outcome,
            "ACTIVE_REQUEST_CONFLICT",
        )
        conflict.refresh_from_db()
        self.assertEqual(conflict.operation, ProcessDocumentRequest.Operation.OCR)
        mock_send.assert_not_called()

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_definite_queue_failure_is_safe_and_preserves_document_state(
        self,
        mock_send,
    ):
        original_state = self.document.processing_state_user
        original_error = self.document.upload_error
        mock_send.side_effect = SqsConfigurationError("secret queue detail")

        with self.assertRaises(HebrewTranslationRetryEnqueueError) as raised:
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            raised.exception.code,
            HebrewTranslationRetryEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(raised.exception.outcome, "ENQUEUE_FAILED")
        self.assertNotIn("secret queue detail", raised.exception.public_message)
        request = ProcessDocumentRequest.objects.get(document=self.document)
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.document.refresh_from_db()
        self.assertEqual(self.document.processing_state_user, original_state)
        self.assertEqual(self.document.upload_error, original_error)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_ambiguous_queue_failure_is_reported_without_raw_detail(
        self,
        mock_send,
    ):
        mock_send.side_effect = EndpointConnectionError(
            endpoint_url="https://secret.invalid/queue",
        )

        with self.assertRaises(HebrewTranslationRetryEnqueueError) as raised:
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            raised.exception.code,
            HebrewTranslationRetryEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(raised.exception.outcome, "ENQUEUE_OUTCOME_UNKNOWN")
        self.assertNotIn("secret.invalid", raised.exception.public_message)

    @patch(
        "documents.services.process_document_hebrew_translation_retry_enqueue."
        "enqueue_process_document_request"
    )
    def test_expected_request_validation_error_is_wrapped_safely(self, mock_enqueue):
        mock_enqueue.side_effect = ProcessDocumentRequestEnqueueError(
            ProcessDocumentRequestEnqueueErrorCode.INVALID_INITIATOR,
            "raw actor detail",
        )

        with self.assertRaises(HebrewTranslationRetryEnqueueError) as raised:
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )

        self.assertEqual(
            raised.exception.code,
            HebrewTranslationRetryEnqueueErrorCode.REQUEST_REJECTED,
        )
        self.assertNotIn("raw actor detail", raised.exception.public_message)

    @patch(
        "documents.services.process_document_hebrew_translation_retry_enqueue."
        "enqueue_process_document_request"
    )
    def test_programming_exception_propagates(self, mock_enqueue):
        mock_enqueue.side_effect = RuntimeError("programming bug")

        with self.assertRaisesRegex(RuntimeError, "programming bug"):
            enqueue_hebrew_translation_retry(
                self.document.pk,
                initiated_by=self.user,
            )
