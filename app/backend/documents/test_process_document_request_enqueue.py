"""Tests for durable PROCESS_DOCUMENT Request enqueue."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from typing import cast
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError
from django.contrib.auth.models import User
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest, TranskribusRun
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_enqueue import (
    ProcessDocumentRequestEnqueueError,
    ProcessDocumentRequestEnqueueErrorCode,
    classify_process_document_sqs_send_failure,
    enqueue_process_document_request,
)
from documents.services.sqs import (
    PROCESS_DOCUMENT_MESSAGE_TYPE,
    SqsConfigurationError,
)


def _document(title: str) -> Document:
    return create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.PROCESSING,
        file_s3_key=f"{title}.pdf",
        mime_type="application/pdf",
    )


def _run(document: Document) -> TranskribusRun:
    return TranskribusRun.objects.create(
        document=document,
        status=TranskribusRun.Status.FAILED,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id="col",
        model_id="model",
        error_code="TRANSKRIBUS_RECOGNITION_FAILED",
    )


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "private"}},
        operation_name="SendMessage",
    )


def _claim_as_worker(request: ProcessDocumentRequest) -> None:
    now = timezone.now()
    request.status = ProcessDocumentRequest.Status.RUNNING
    request.lease_token = uuid.uuid4()
    request.lease_expires_at = now + timedelta(minutes=45)
    request.started_at = now
    request.failure_code = ""
    request.failure_message = ""
    request.save(
        update_fields=[
            "status",
            "lease_token",
            "lease_expires_at",
            "started_at",
            "failure_code",
            "failure_message",
            "updated_at",
        ]
    )


class SendProcessDocumentRequestMessageTests(SimpleTestCase):
    @patch.dict(
        "os.environ",
        {"SQS_QUEUE_URL": "https://sqs.example/queue"},
        clear=False,
    )
    @patch("documents.services.sqs.boto3.client")
    def test_payload_contains_type_and_request_id_only(self, mock_boto_client):
        from documents.services.sqs import send_process_document_request_message

        mock_sqs = mock_boto_client.return_value
        send_process_document_request_message(42)

        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(
            body,
            {
                "type": PROCESS_DOCUMENT_MESSAGE_TYPE,
                "request_id": 42,
            },
        )

    def test_rejects_invalid_request_ids(self):
        from documents.services.sqs import send_process_document_request_message

        for request_id in (0, -1, True, 1.0, "1", None):
            with self.subTest(request_id=request_id):
                with self.assertRaises(ValueError):
                    send_process_document_request_message(cast(int, request_id))


class ProcessDocumentSqsFailureClassificationTests(SimpleTestCase):
    def test_definite_reject(self):
        self.assertEqual(
            classify_process_document_sqs_send_failure(_client_error("AccessDenied")),
            "definite",
        )

    def test_ambiguous_transport_failure(self):
        self.assertEqual(
            classify_process_document_sqs_send_failure(
                EndpointConnectionError(endpoint_url="https://sqs")
            ),
            "ambiguous",
        )

    def test_programming_error_is_rejected(self):
        with self.assertRaises(TypeError):
            classify_process_document_sqs_send_failure(RuntimeError("programming bug"))


class ProcessDocumentEnqueueCoalesceTests(TransactionTestCase):
    def setUp(self) -> None:
        self.document = _document("enqueue-document")
        self.other_document = _document("other-enqueue-document")
        self.user = User.objects.create_user(username="enqueue-user")
        self.user2 = User.objects.create_user(username="enqueue-user-2")

    def _enqueue(self, **overrides):
        values = {
            "document_id": self.document.pk,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            "initiated_by": self.user,
        }
        values.update(overrides)
        return enqueue_process_document_request(**values)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_existing_matching_queued_request_does_not_resend(self, mock_send):
        existing = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            last_enqueued_at=timezone.now(),
        )

        result = self._enqueue()

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_QUEUED")
        self.assertEqual(result.request.pk, existing.pk)
        self.assertFalse(result.send_attempted)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_different_active_payload_is_explicit_conflict(self, mock_send):
        existing = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        )

        result = self._enqueue(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ACTIVE_REQUEST_CONFLICT")
        self.assertEqual(result.request.pk, existing.pk)
        existing.refresh_from_db()
        self.assertEqual(
            existing.origin,
            ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        )

    def test_recognition_only_requires_source_run(self):
        with self.assertRaises(ProcessDocumentRequestEnqueueError) as ctx:
            self._enqueue(
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=(
                    ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                ),
                source_transkribus_run_id=None,
            )

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestEnqueueErrorCode.INVALID_REQUEST_PAYLOAD,
        )

    def test_source_run_must_belong_to_document(self):
        other_run = _run(self.other_document)

        with self.assertRaises(ProcessDocumentRequestEnqueueError) as ctx:
            self._enqueue(
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=(
                    ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                ),
                source_transkribus_run_id=other_run.pk,
            )

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestEnqueueErrorCode.SOURCE_RUN_NOT_FOUND,
        )


class ProcessDocumentEnqueueSendPathTests(TransactionTestCase):
    def setUp(self) -> None:
        self.document = _document("enqueue-send-document")
        self.user = User.objects.create_user(username="enqueue-send-user")
        self.user2 = User.objects.create_user(username="enqueue-send-user-2")

    def _enqueue(self, **overrides):
        values = {
            "document_id": self.document.pk,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            "initiated_by": self.user,
        }
        values.update(overrides)
        return enqueue_process_document_request(**values)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_create_sends_outside_atomic_and_stamps_timestamp(self, mock_send):
        atomic_states: list[bool] = []

        def tracked_send(request_id: int) -> None:
            atomic_states.append(connection.in_atomic_block)

        mock_send.side_effect = tracked_send

        result = self._enqueue()

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertTrue(result.created)
        self.assertTrue(result.message_sent)
        self.assertEqual(atomic_states, [False])
        mock_send.assert_called_once_with(result.request.pk)
        self.assertIsNotNone(result.request.last_enqueued_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_matching_enqueue_failed_request_is_retried(self, mock_send):
        existing = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="old",
        )

        result = self._enqueue(initiated_by=self.user2)

        mock_send.assert_called_once_with(existing.pk)
        self.assertEqual(result.outcome, "REENQUEUED")
        self.assertFalse(result.created)
        existing.refresh_from_db()
        self.assertEqual(existing.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertEqual(existing.failure_code, "")
        self.assertEqual(existing.initiated_by_id, self.user2.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_definite_send_failure_marks_enqueue_failed(self, mock_send):
        mock_send.side_effect = _client_error("AccessDenied")

        result = self._enqueue()

        self.assertEqual(result.outcome, "ENQUEUE_FAILED")
        self.assertFalse(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(result.request.failure_code, "ENQUEUE_SEND_FAILED")
        self.assertNotIn("AccessDenied", result.request.failure_message)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_ambiguous_send_failure_marks_outcome_unknown(self, mock_send):
        mock_send.side_effect = EndpointConnectionError(endpoint_url="https://sqs")

        result = self._enqueue()

        self.assertEqual(result.outcome, "ENQUEUE_OUTCOME_UNKNOWN")
        self.assertIsNone(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(
            result.request.failure_code,
            "ENQUEUE_OUTCOME_UNKNOWN",
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_programming_error_propagates_without_false_failure(self, mock_send):
        mock_send.side_effect = RuntimeError("programming bug")

        with self.assertRaises(RuntimeError):
            self._enqueue()

        request = ProcessDocumentRequest.objects.get(document=self.document)
        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertEqual(request.failure_code, "")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_success_cas_does_not_overwrite_worker_claim(self, mock_send):
        def send_and_claim(request_id: int) -> None:
            request = ProcessDocumentRequest.objects.get(pk=request_id)
            _claim_as_worker(request)

        mock_send.side_effect = send_and_claim

        result = self._enqueue()

        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertTrue(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.RUNNING,
        )
        self.assertIsNotNone(result.request.lease_token)
        self.assertIsNone(result.request.last_enqueued_at)


class ProcessDocumentEnqueueAdditionalCoverageTests(TransactionTestCase):
    def setUp(self) -> None:
        self.document = _document("enqueue-additional-document")
        self.other_document = _document("enqueue-additional-other")
        self.user = User.objects.create_user(username="enqueue-additional-user")

    def _upload_enqueue(self, **overrides):
        values = {
            "document_id": self.document.pk,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            "initiated_by": self.user,
        }
        values.update(overrides)
        return enqueue_process_document_request(**values)

    def test_source_run_error_code_literal_is_stable(self):
        self.assertEqual(
            ProcessDocumentRequestEnqueueErrorCode.SOURCE_RUN_NOT_FOUND,
            "SOURCE_RUN_NOT_FOUND",
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_recognition_only_request_persists_exact_payload(self, mock_send):
        run = _run(self.document)

        result = self._upload_enqueue(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=(
                ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
            ),
            source_transkribus_run_id=run.pk,
        )

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        mock_send.assert_called_once_with(result.request.pk)
        self.assertEqual(
            result.request.origin,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        self.assertEqual(
            result.request.ocr_retry_mode,
            ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
        )
        self.assertEqual(result.request.source_transkribus_run_id, run.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_translation_request_persists_exact_payload(self, mock_send):
        result = enqueue_process_document_request(
            document_id=self.document.pk,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            initiated_by=self.user,
        )

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        mock_send.assert_called_once_with(result.request.pk)
        self.assertEqual(
            result.request.operation,
            ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
        )
        self.assertEqual(
            result.request.origin,
            ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
        )
        self.assertEqual(result.request.ocr_retry_mode, "")
        self.assertIsNone(result.request.source_transkribus_run_id)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_system_initiated_upload_request_allows_null_actor(self, mock_send):
        result = self._upload_enqueue(initiated_by=None)

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertIsNone(result.request.initiated_by_id)
        mock_send.assert_called_once_with(result.request.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_matching_running_request_does_not_resend(self, mock_send):
        now = timezone.now()
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

        result = self._upload_enqueue()

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertEqual(result.request.pk, request.pk)
        self.assertFalse(result.send_attempted)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_matching_recovery_required_request_does_not_resend(
        self,
        mock_send,
    ):
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            lease_token=uuid.uuid4(),
            started_at=timezone.now() - timedelta(hours=1),
        )

        result = self._upload_enqueue()

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "BLOCKED_RECOVERY_REQUIRED")
        self.assertEqual(result.request.pk, request.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_conflicting_enqueue_failed_request_is_not_repurposed(
        self,
        mock_send,
    ):
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="prior",
        )

        result = self._upload_enqueue(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ACTIVE_REQUEST_CONFLICT")
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(
            request.origin,
            ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        )
        self.assertEqual(request.failure_code, "ENQUEUE_SEND_FAILED")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_terminal_upload_history_is_idempotent(self, mock_send):
        terminal = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            completed_at=timezone.now(),
        )

        result = self._upload_enqueue()

        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_TERMINAL")
        self.assertEqual(result.request.pk, terminal.pk)
        self.assertFalse(result.created)
        self.assertFalse(result.send_attempted)
        self.assertFalse(result.message_sent)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            1,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_terminal_ocr_reprocess_history_allows_new_request(
        self,
        mock_send,
    ):
        terminal = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            completed_at=timezone.now(),
        )

        result = self._upload_enqueue(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )

        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertNotEqual(result.request.pk, terminal.pk)
        self.assertTrue(result.created)
        self.assertTrue(result.send_attempted)
        self.assertTrue(result.message_sent)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            2,
        )
        mock_send.assert_called_once_with(result.request.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_success_cas_observes_worker_terminalization(self, mock_send):
        def send_and_terminalize(request_id: int) -> None:
            request = ProcessDocumentRequest.objects.get(pk=request_id)
            _claim_as_worker(request)
            request.refresh_from_db()
            request.status = ProcessDocumentRequest.Status.COMPLETED
            request.lease_token = None
            request.lease_expires_at = None
            request.completed_at = timezone.now()
            request.save(
                update_fields=[
                    "status",
                    "lease_token",
                    "lease_expires_at",
                    "completed_at",
                    "updated_at",
                ]
            )

        mock_send.side_effect = send_and_terminalize

        result = self._upload_enqueue()

        self.assertEqual(result.outcome, "ALREADY_TERMINAL")
        self.assertTrue(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.COMPLETED,
        )
        self.assertIsNone(result.request.last_enqueued_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_failure_cas_does_not_overwrite_worker_claim(self, mock_send):
        def send_claim_and_fail(request_id: int) -> None:
            request = ProcessDocumentRequest.objects.get(pk=request_id)
            _claim_as_worker(request)
            raise _client_error("AccessDenied")

        mock_send.side_effect = send_claim_and_fail

        result = self._upload_enqueue()

        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertFalse(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.RUNNING,
        )
        self.assertIsNotNone(result.request.lease_token)
        self.assertEqual(result.request.failure_code, "")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_outer_atomic_guard_prevents_all_writes_and_send(self, mock_send):
        with transaction.atomic():
            with self.assertRaises(RuntimeError):
                self._upload_enqueue()

        mock_send.assert_not_called()
        self.assertFalse(
            ProcessDocumentRequest.objects.filter(document=self.document).exists()
        )

    def test_invalid_initiator_is_rejected_before_writes(self):
        unsaved_user = User(username="unsaved-enqueue-user")

        with self.assertRaises(ProcessDocumentRequestEnqueueError) as ctx:
            self._upload_enqueue(initiated_by=unsaved_user)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestEnqueueErrorCode.INVALID_INITIATOR,
        )
        self.assertFalse(
            ProcessDocumentRequest.objects.filter(document=self.document).exists()
        )

    def test_missing_document_is_rejected_before_writes(self):
        with self.assertRaises(ProcessDocumentRequestEnqueueError) as ctx:
            self._upload_enqueue(document_id=9_999_999)

        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestEnqueueErrorCode.DOCUMENT_NOT_FOUND,
        )
        self.assertFalse(ProcessDocumentRequest.objects.exists())

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_sqs_configuration_failure_is_safe_and_definite(self, mock_send):
        mock_send.side_effect = SqsConfigurationError(
            "Missing required env var: SQS_QUEUE_URL"
        )

        result = self._upload_enqueue()

        self.assertEqual(result.outcome, "ENQUEUE_FAILED")
        self.assertFalse(result.message_sent)
        self.assertEqual(
            result.request.failure_code,
            "ENQUEUE_SEND_FAILED",
        )
        self.assertNotIn(
            "SQS_QUEUE_URL",
            result.request.failure_message,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_enqueue_failed_retry_without_actor_preserves_original_actor(
        self,
        mock_send,
    ):
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            failure_code="ENQUEUE_SEND_FAILED",
        )

        result = self._upload_enqueue(initiated_by=None)

        self.assertEqual(result.outcome, "REENQUEUED")
        mock_send.assert_called_once_with(request.pk)
        request.refresh_from_db()
        self.assertEqual(request.initiated_by_id, self.user.pk)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_success_cas_observes_partial_as_terminal(self, mock_send):
        def send_and_partial(request_id: int) -> None:
            request = ProcessDocumentRequest.objects.get(pk=request_id)
            _claim_as_worker(request)
            request.refresh_from_db()
            request.status = ProcessDocumentRequest.Status.PARTIAL
            request.lease_token = None
            request.lease_expires_at = None
            request.completed_at = timezone.now()
            request.failure_code = "PROCESS_DOCUMENT_PARTIAL"
            request.failure_message = "Expected output is incomplete."
            request.save(
                update_fields=[
                    "status",
                    "lease_token",
                    "lease_expires_at",
                    "completed_at",
                    "failure_code",
                    "failure_message",
                    "updated_at",
                ]
            )

        mock_send.side_effect = send_and_partial

        result = self._upload_enqueue()

        self.assertEqual(result.outcome, "ALREADY_TERMINAL")
        self.assertTrue(result.message_sent)
        self.assertEqual(
            result.request.status,
            ProcessDocumentRequest.Status.PARTIAL,
        )
        self.assertEqual(
            result.request.failure_code,
            "PROCESS_DOCUMENT_PARTIAL",
        )
        self.assertIsNone(result.request.last_enqueued_at)


class ProcessDocumentEnqueuePostgresConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Enqueue concurrency tests require PostgreSQL")
        self.document = _document("enqueue-concurrency-document")
        self.user = User.objects.create_user(username="enqueue-concurrency-a")
        self.user2 = User.objects.create_user(username="enqueue-concurrency-b")

    def _run_concurrent(
        self,
        specs: list[dict],
    ) -> tuple[list, list[Exception], list[int]]:
        barrier = threading.Barrier(2, timeout=10)
        results: list = []
        errors: list[Exception] = []
        send_ids: list[int] = []
        result_lock = threading.Lock()

        def tracked_send(request_id: int) -> None:
            with result_lock:
                send_ids.append(request_id)

        def worker(spec: dict) -> None:
            connections.close_all()
            try:
                barrier.wait()
                user = User.objects.get(pk=spec.pop("user_id"))
                result = enqueue_process_document_request(
                    initiated_by=user,
                    **spec,
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
            first = threading.Thread(target=worker, args=(dict(specs[0]),))
            second = threading.Thread(target=worker, args=(dict(specs[1]),))
            first.start()
            second.start()
            first.join(timeout=15)
            second.join(timeout=15)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        return results, errors, send_ids

    def _upload_spec(self, user_id: int) -> dict:
        return {
            "user_id": user_id,
            "document_id": self.document.pk,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        }

    def test_concurrent_create_sends_exactly_once(self):
        results, errors, send_ids = self._run_concurrent(
            [
                self._upload_spec(self.user.pk),
                self._upload_spec(self.user2.pk),
            ]
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(send_ids), 1)
        outcomes = {result.outcome for result in results}
        self.assertIn("CREATED_AND_ENQUEUED", outcomes)
        self.assertTrue(outcomes <= {"CREATED_AND_ENQUEUED", "ALREADY_QUEUED"})
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(
                document=self.document,
                status=ProcessDocumentRequest.Status.QUEUED,
            ).count(),
            1,
        )

    def test_concurrent_enqueue_failed_retry_sends_exactly_once(self):
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            initiated_by=self.user,
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            failure_code="ENQUEUE_SEND_FAILED",
        )

        results, errors, send_ids = self._run_concurrent(
            [
                self._upload_spec(self.user.pk),
                self._upload_spec(self.user2.pk),
            ]
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(send_ids, [request.pk])
        outcomes = {result.outcome for result in results}
        self.assertIn("REENQUEUED", outcomes)
        self.assertTrue(outcomes <= {"REENQUEUED", "ALREADY_QUEUED"})

    def test_concurrent_conflicting_payloads_send_only_winner(self):
        translation_spec = {
            "user_id": self.user2.pk,
            "document_id": self.document.pk,
            "operation": (ProcessDocumentRequest.Operation.HEBREW_TRANSLATION),
            "origin": (ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY),
        }

        results, errors, send_ids = self._run_concurrent(
            [
                self._upload_spec(self.user.pk),
                translation_spec,
            ]
        )

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(send_ids), 1)
        self.assertEqual(
            {result.outcome for result in results},
            {"CREATED_AND_ENQUEUED", "ACTIVE_REQUEST_CONFLICT"},
        )
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(
                document=self.document,
                status=ProcessDocumentRequest.Status.QUEUED,
            ).count(),
            1,
        )
