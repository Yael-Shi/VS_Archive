from __future__ import annotations

import io
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from botocore.exceptions import EndpointConnectionError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from documents.models import Document, DocumentTextResult, ProcessDocumentRequest
from documents.services.archive_items import create_ocr_document
from documents.services.ocr_reprocess import (
    OcrReprocessAssessment,
    OcrRetryMode,
)
from documents.services.process_document_request_recovery import (
    DEFAULT_RECOVERY_MINIMUM_AGE,
    ProcessDocumentRequestRecoveryError,
    ProcessDocumentRequestRecoveryErrorCode,
    assess_process_document_request_recovery,
    process_document_recovery_candidates,
    recover_process_document_request,
)
from documents.services.sqs import SqsConfigurationError


def _document(
    title: str,
    *,
    processing_state: str = Document.ProcessingState.FAILED,
) -> Document:
    return create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        language=Document.Language.ENGLISH,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=processing_state,
        upload_error="prior safe error",
        file_s3_key=f"documents/{title}/source.pdf",
        mime_type="application/pdf",
    )


def _set_updated_at(request: ProcessDocumentRequest, value) -> None:
    ProcessDocumentRequest.objects.filter(pk=request.pk).update(updated_at=value)
    request.refresh_from_db()


class ProcessDocumentRequestRecoveryTests(TransactionTestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.document = _document("request-recovery")

    def _request(
        self,
        *,
        document: Document | None = None,
        status: str = ProcessDocumentRequest.Status.QUEUED,
        operation: str = ProcessDocumentRequest.Operation.OCR,
        origin: str = ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        age: timedelta = timedelta(hours=1),
        last_enqueued_at=None,
    ) -> ProcessDocumentRequest:
        values = {
            "document": document or self.document,
            "status": status,
            "operation": operation,
            "origin": origin,
            "ocr_retry_mode": (
                ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE
                if operation == ProcessDocumentRequest.Operation.OCR
                else ""
            ),
            "last_enqueued_at": last_enqueued_at,
        }
        if status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
            values["failure_code"] = "ENQUEUE_SEND_FAILED"
            values["failure_message"] = "safe stored failure"
        elif status == ProcessDocumentRequest.Status.RUNNING:
            values["lease_token"] = uuid.uuid4()
            values["lease_expires_at"] = self.now + timedelta(minutes=45)
            values["started_at"] = self.now
        elif status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
            values["lease_token"] = uuid.uuid4()
            values["started_at"] = self.now - timedelta(hours=1)
        elif status in {
            ProcessDocumentRequest.Status.COMPLETED,
            ProcessDocumentRequest.Status.PARTIAL,
            ProcessDocumentRequest.Status.FAILED,
        }:
            values["completed_at"] = self.now
            if status == ProcessDocumentRequest.Status.PARTIAL:
                values["failure_code"] = "PROCESS_DOCUMENT_PARTIAL"
            elif status == ProcessDocumentRequest.Status.FAILED:
                values["failure_code"] = "PROCESS_DOCUMENT_FAILED"

        request = ProcessDocumentRequest.objects.create(**values)
        _set_updated_at(request, self.now - age)
        return request

    def test_candidate_queryset_includes_only_old_unsent_or_failed_requests(self):
        stranded = self._request()
        failed_document = _document("request-recovery-failed")
        failed = self._request(
            document=failed_document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        recent_document = _document("request-recovery-recent")
        self._request(
            document=recent_document,
            age=timedelta(minutes=1),
        )
        sent_document = _document("request-recovery-sent")
        self._request(
            document=sent_document,
            last_enqueued_at=self.now - timedelta(hours=1),
        )
        running_document = _document("request-recovery-running")
        self._request(
            document=running_document,
            status=ProcessDocumentRequest.Status.RUNNING,
        )

        candidate_ids = list(
            process_document_recovery_candidates(
                now=self.now,
                minimum_age=DEFAULT_RECOVERY_MINIMUM_AGE,
            ).values_list("pk", flat=True)
        )

        self.assertEqual(candidate_ids, [stranded.pk, failed.pk])

    def test_assessment_reports_each_ineligible_reason(self):
        sent = self._request(last_enqueued_at=self.now - timedelta(hours=1))
        assessment = assess_process_document_request_recovery(
            sent.pk,
            now=self.now,
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "QUEUED_ALREADY_ENQUEUED")

        recent_document = _document("assessment-recent")
        recent = self._request(
            document=recent_document,
            age=timedelta(minutes=1),
        )
        assessment = assess_process_document_request_recovery(
            recent.pk,
            now=self.now,
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "TOO_RECENT")

        terminal_document = _document("assessment-terminal")
        terminal = self._request(
            document=terminal_document,
            status=ProcessDocumentRequest.Status.COMPLETED,
        )
        assessment = assess_process_document_request_recovery(
            terminal.pk,
            now=self.now,
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "STATUS_NOT_RECOVERABLE")

        recovery_document = _document("assessment-recovery-required")
        recovery = self._request(
            document=recovery_document,
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        assessment = assess_process_document_request_recovery(
            recovery.pk,
            now=self.now,
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "STATUS_NOT_RECOVERABLE")

    def test_ambiguous_enqueue_failure_is_recoverable(self):
        request = self._request(
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        ProcessDocumentRequest.objects.filter(pk=request.pk).update(
            failure_code="ENQUEUE_OUTCOME_UNKNOWN",
            failure_message="safe ambiguous outcome",
        )

        assessment = assess_process_document_request_recovery(
            request.pk,
            now=self.now,
        )

        self.assertTrue(assessment.eligible)
        self.assertEqual(assessment.reason, "ENQUEUE_FAILED")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_stranded_queued_is_reserved_and_sent_after_commit(self, mock_send):
        request = self._request()
        original_updated_at = request.updated_at

        def assert_send_boundary(request_id: int) -> None:
            self.assertFalse(connection.in_atomic_block)
            reserved = ProcessDocumentRequest.objects.get(pk=request_id)
            self.assertGreater(reserved.updated_at, original_updated_at)
            self.assertIsNone(reserved.last_enqueued_at)

        mock_send.side_effect = assert_send_boundary

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertTrue(result.assessment.eligible)
        self.assertEqual(result.assessment.reason, "STRANDED_QUEUED")
        self.assertIsNotNone(result.enqueue_result)
        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertIsNotNone(request.last_enqueued_at)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_enqueue_failed_is_reset_and_requeued(self, mock_send):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertEqual(result.assessment.reason, "ENQUEUE_FAILED")
        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        mock_send.assert_called_once_with(request.pk)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.failure_message, "")
        self.assertIsNotNone(request.last_enqueued_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_current_ocr_reprocess_intent_is_reassessed_and_requeued(
        self,
        mock_send,
    ):
        request = self._request(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertTrue(result.assessment.eligible)
        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        mock_send.assert_called_once_with(request.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_hebrew_translation_recovery_does_not_mutate_document_state(
        self,
        mock_send,
    ):
        document = _document(
            "translation-request-recovery",
            processing_state=Document.ProcessingState.PARTIAL,
        )
        request = self._request(
            document=document,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
        )
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.5-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="recognized source text",
        )
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-2.5-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            error_code="HEBREW_TRANSLATION_FAILED",
            error_details="prior translation failure",
        )
        original_state = document.processing_state_user
        original_error = document.upload_error

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        mock_send.assert_called_once_with(request.pk)
        document.refresh_from_db()
        self.assertEqual(document.processing_state_user, original_state)
        self.assertEqual(document.upload_error, original_error)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_changed_ocr_reprocess_intent_is_not_replayed(self, mock_send):
        request = self._request(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="human",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified human text",
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertFalse(result.assessment.eligible)
        self.assertEqual(
            result.assessment.reason,
            "INTENT_NO_LONGER_VALID",
        )
        self.assertIsNone(result.enqueue_result)
        mock_send.assert_not_called()
        request.refresh_from_db()
        self.assertIsNone(request.last_enqueued_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch("documents.services.process_document_request_recovery.assess_ocr_reprocess")
    def test_changed_ocr_reprocess_payload_is_not_replayed(
        self,
        mock_assess,
        mock_send,
    ):
        request = self._request(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        mock_assess.return_value = OcrReprocessAssessment(
            document_id=self.document.pk,
            retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
            source_transkribus_run_id=123,
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertFalse(result.assessment.eligible)
        self.assertEqual(
            result.assessment.reason,
            "REQUEST_PAYLOAD_NO_LONGER_MATCHES",
        )
        self.assertIsNone(result.enqueue_result)
        mock_send.assert_not_called()
        request.refresh_from_db()
        self.assertIsNone(request.last_enqueued_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_upload_finalize_with_existing_source_text_is_not_replayed(
        self,
        mock_send,
    ):
        request = self._request()
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="existing",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="already processed source text",
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertFalse(result.assessment.eligible)
        self.assertEqual(
            result.assessment.reason,
            "INTENT_NO_LONGER_VALID",
        )
        self.assertIsNone(result.enqueue_result)
        mock_send.assert_not_called()

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_definite_send_failure_is_fenced_and_safe(self, mock_send):
        request = self._request()
        mock_send.side_effect = SqsConfigurationError(
            "secret missing queue configuration"
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "ENQUEUE_FAILED")
        self.assertFalse(result.enqueue_result.message_sent)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(request.failure_code, "ENQUEUE_SEND_FAILED")
        self.assertNotIn("secret", request.failure_message)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertNotIn("secret", self.document.upload_error or "")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_ambiguous_send_failure_is_fenced_and_safe(self, mock_send):
        request = self._request()
        mock_send.side_effect = EndpointConnectionError(
            endpoint_url="https://secret.invalid/queue",
        )

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        assert result.enqueue_result is not None
        self.assertEqual(
            result.enqueue_result.outcome,
            "ENQUEUE_OUTCOME_UNKNOWN",
        )
        self.assertIsNone(result.enqueue_result.message_sent)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(request.failure_code, "ENQUEUE_OUTCOME_UNKNOWN")
        self.assertNotIn("secret.invalid", request.failure_message)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_programming_exception_propagates_after_cooldown_reservation(
        self,
        mock_send,
    ):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        mock_send.side_effect = RuntimeError("programming bug")

        with self.assertRaisesRegex(RuntimeError, "programming bug"):
            recover_process_document_request(
                request.pk,
                now=self.now,
            )

        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertEqual(request.failure_code, "")
        self.assertGreaterEqual(request.updated_at, self.now)
        self.assertIsNone(request.last_enqueued_at)
        immediate = assess_process_document_request_recovery(
            request.pk,
            now=self.now,
        )
        self.assertFalse(immediate.eligible)
        self.assertEqual(immediate.reason, "TOO_RECENT")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_worker_claim_during_send_wins_document_state(self, mock_send):
        request = self._request()

        def claim_as_worker(request_id: int) -> None:
            claimed_at = timezone.now()
            ProcessDocumentRequest.objects.filter(pk=request_id).update(
                status=ProcessDocumentRequest.Status.RUNNING,
                lease_token=uuid.uuid4(),
                lease_expires_at=claimed_at + timedelta(minutes=45),
                started_at=claimed_at,
                updated_at=claimed_at,
            )
            Document.objects.filter(pk=self.document.pk).update(
                processing_state_user=Document.ProcessingState.READY,
                upload_error="worker-owned state",
            )

        mock_send.side_effect = claim_as_worker

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        assert result.enqueue_result is not None
        self.assertEqual(result.enqueue_result.outcome, "ALREADY_RUNNING")
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertEqual(self.document.upload_error, "worker-owned state")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_ineligible_request_is_not_reserved_or_sent(self, mock_send):
        request = self._request(age=timedelta(minutes=1))
        original_updated_at = request.updated_at

        result = recover_process_document_request(
            request.pk,
            now=self.now,
        )

        self.assertFalse(result.assessment.eligible)
        self.assertEqual(result.assessment.reason, "TOO_RECENT")
        self.assertIsNone(result.enqueue_result)
        mock_send.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.updated_at, original_updated_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_outer_transaction_rejected_before_write_or_send(self, mock_send):
        request = self._request()
        original_updated_at = request.updated_at

        with transaction.atomic(), self.assertRaises(RuntimeError):
            recover_process_document_request(
                request.pk,
                now=self.now,
            )

        mock_send.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.updated_at, original_updated_at)

    def test_invalid_and_missing_request_ids_are_typed_errors(self):
        with self.assertRaises(ProcessDocumentRequestRecoveryError) as invalid:
            assess_process_document_request_recovery(0)
        self.assertEqual(
            invalid.exception.code,
            ProcessDocumentRequestRecoveryErrorCode.INVALID_REQUEST_ID,
        )

        with self.assertRaises(ProcessDocumentRequestRecoveryError) as invalid_age:
            assess_process_document_request_recovery(
                1,
                minimum_age=timedelta(0),
            )
        self.assertEqual(
            invalid_age.exception.code,
            ProcessDocumentRequestRecoveryErrorCode.INVALID_MINIMUM_AGE,
        )

        with self.assertRaises(ProcessDocumentRequestRecoveryError) as missing:
            assess_process_document_request_recovery(9_999_999)
        self.assertEqual(
            missing.exception.code,
            ProcessDocumentRequestRecoveryErrorCode.REQUEST_NOT_FOUND,
        )


class ProcessDocumentRequestRecoveryCommandTests(TransactionTestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.document = _document("recovery-command")
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        _set_updated_at(self.request, self.now - timedelta(hours=1))

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_default_is_dry_run_without_writes_or_send(self, mock_send):
        output = io.StringIO()
        original_updated_at = self.request.updated_at

        call_command("recover_process_document_requests", stdout=output)

        text = output.getvalue()
        self.assertIn("mode=dry-run", text)
        self.assertIn(f"request_id={self.request.pk}", text)
        self.assertIn("action=would_requeue", text)
        self.assertIn("no changes made (dry-run)", text)
        mock_send.assert_not_called()
        self.request.refresh_from_db()
        self.assertEqual(self.request.updated_at, original_updated_at)
        self.assertIsNone(self.request.last_enqueued_at)

    def test_apply_requires_explicit_scope(self):
        with self.assertRaisesMessage(
            CommandError,
            "--apply requires --request-id, --document-id, or --all-eligible",
        ):
            call_command("recover_process_document_requests", "--apply")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_apply_by_request_id_requeues(self, mock_send):
        output = io.StringIO()

        call_command(
            "recover_process_document_requests",
            "--apply",
            "--request-id",
            str(self.request.pk),
            stdout=output,
        )

        text = output.getvalue()
        self.assertIn("mode=apply", text)
        self.assertIn("enqueue_outcome=REENQUEUED", text)
        self.assertIn("PROCESS_DOCUMENT recovery complete", text)
        mock_send.assert_called_once_with(self.request.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_document_scope_does_not_touch_other_document(self, mock_send):
        other_document = _document("recovery-command-other")
        other_request = ProcessDocumentRequest.objects.create(
            document=other_document,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            failure_code="ENQUEUE_SEND_FAILED",
        )
        _set_updated_at(other_request, self.now - timedelta(hours=1))

        call_command(
            "recover_process_document_requests",
            "--apply",
            "--document-id",
            str(self.document.pk),
            stdout=io.StringIO(),
        )

        mock_send.assert_called_once_with(self.request.pk)
        other_request.refresh_from_db()
        self.assertEqual(
            other_request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_all_eligible_honors_limit(self, mock_send):
        second_document = _document("recovery-command-second")
        second = ProcessDocumentRequest.objects.create(
            document=second_document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        _set_updated_at(second, self.now - timedelta(hours=1))

        call_command(
            "recover_process_document_requests",
            "--apply",
            "--all-eligible",
            "--limit",
            "1",
            stdout=io.StringIO(),
        )

        mock_send.assert_called_once_with(self.request.pk)
        second.refresh_from_db()
        self.assertIsNone(second.last_enqueued_at)

    def test_missing_explicit_request_fails_before_mutation(self):
        with self.assertRaisesMessage(CommandError, "Request ids not found"):
            call_command(
                "recover_process_document_requests",
                "--apply",
                "--request-id",
                "999999",
            )
        self.request.refresh_from_db()
        self.assertIsNone(self.request.last_enqueued_at)

    def test_invalid_limit_and_age_are_rejected(self):
        with self.assertRaisesMessage(CommandError, "--limit must be between"):
            call_command(
                "recover_process_document_requests",
                "--limit",
                "0",
            )
        with self.assertRaisesMessage(
            CommandError,
            "--older-than-minutes must be at least 1",
        ):
            call_command(
                "recover_process_document_requests",
                "--older-than-minutes",
                "0",
            )

    def test_explicit_request_count_cannot_be_silently_truncated(self):
        with self.assertRaisesMessage(
            CommandError,
            "number of --request-id values cannot exceed --limit",
        ):
            call_command(
                "recover_process_document_requests",
                "--request-id",
                str(self.request.pk),
                "--request-id",
                "999999",
                "--limit",
                "1",
            )

    def test_explicit_document_count_cannot_be_silently_truncated(self):
        with self.assertRaisesMessage(
            CommandError,
            "number of --document-id values cannot exceed --limit",
        ):
            call_command(
                "recover_process_document_requests",
                "--document-id",
                str(self.document.pk),
                "--document-id",
                "999999",
                "--limit",
                "1",
            )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_send_failure_returns_nonzero_command_error_after_summary(
        self,
        mock_send,
    ):
        mock_send.side_effect = SqsConfigurationError("secret queue config")
        output = io.StringIO()

        with self.assertRaisesMessage(CommandError, "send attempt(s) failed"):
            call_command(
                "recover_process_document_requests",
                "--apply",
                "--request-id",
                str(self.request.pk),
                stdout=output,
            )

        self.assertIn("send_failures=1", output.getvalue())
        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )


class ProcessDocumentRequestRecoveryPostgresConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Recovery concurrency tests require PostgreSQL")
        self.now = timezone.now()
        self.document = _document("recovery-concurrency")
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )
        _set_updated_at(self.request, self.now - timedelta(hours=1))

    def test_concurrent_recovery_has_exactly_one_sender(self):
        barrier = threading.Barrier(2, timeout=10)
        result_lock = threading.Lock()
        results = []
        errors: list[Exception] = []
        sent_ids: list[int] = []

        def tracked_send(request_id: int) -> None:
            with result_lock:
                sent_ids.append(request_id)

        def worker() -> None:
            connections.close_all()
            try:
                barrier.wait()
                result = recover_process_document_request(
                    self.request.pk,
                    minimum_age=DEFAULT_RECOVERY_MINIMUM_AGE,
                )
                with result_lock:
                    results.append(result)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        with patch(
            "documents.services.process_document_request_enqueue."
            "send_process_document_request_message",
            side_effect=tracked_send,
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sent_ids, [self.request.pk])
        self.assertEqual(len(results), 2)
        self.assertEqual(
            sum(result.enqueue_result is not None for result in results),
            1,
        )
