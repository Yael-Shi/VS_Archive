from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from documents.management.commands.run_worker import Command
from documents.models import Document, ProcessDocumentRequest, TranskribusRun
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
    ProcessDocumentOutcome,
)
from documents.services.process_document_request_worker import (
    EXECUTION_LEASE,
    FRESH_IN_PROGRESS_DEFER_SECONDS,
    LEASE_EXPIRES_AT_PAYLOAD_KEY,
    SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
    ProcessDocumentRequestAction,
    ProcessDocumentRequestClaim,
    claim_process_document_request,
    handle_process_document_request,
    parse_process_document_request_id,
    terminalize_process_document_request,
)


class ProcessDocumentRequestWorkerTests(TestCase):
    def setUp(self) -> None:
        self.document = create_ocr_document(
            title="Worker fencing document",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="worker-fencing.pdf",
            mime_type="application/pdf",
        )

    def _request(self, **overrides) -> ProcessDocumentRequest:
        values = {
            "document": self.document,
            "status": ProcessDocumentRequest.Status.QUEUED,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        }
        values.update(overrides)
        return ProcessDocumentRequest.objects.create(**values)

    def _run(self) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=self.document,
            status=TranskribusRun.Status.FAILED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="model",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

    def test_request_id_accepts_only_positive_plain_int(self):
        self.assertEqual(parse_process_document_request_id(1), 1)
        for value in (None, True, False, 0, -1, 1.0, "1"):
            with self.subTest(value=value):
                self.assertIsNone(parse_process_document_request_id(value))

    def test_missing_request_acks_without_execution(self):
        claim = claim_process_document_request(request_id=999_999)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.ACK)
        self.assertIsNone(claim.lease_token)
        self.assertIsNone(claim.execution_payload)

    def test_queued_normal_ocr_claims_and_builds_legacy_payload(self):
        request = self._request()
        before = timezone.now()

        claim = claim_process_document_request(request_id=request.id)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.EXECUTE)
        self.assertIsNotNone(claim.lease_token)
        request.refresh_from_db()
        self.assertEqual(
            claim.execution_payload,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
                LEASE_EXPIRES_AT_PAYLOAD_KEY: request.lease_expires_at,
            },
        )
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.lease_token, claim.lease_token)
        self.assertIsNotNone(request.started_at)
        self.assertGreaterEqual(request.lease_expires_at, before + EXECUTION_LEASE)

    def test_recognition_only_payload_is_derived_from_request(self):
        run = self._run()
        request = self._request(
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=(
                ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
            ),
            source_transkribus_run=run,
        )

        claim = claim_process_document_request(request_id=request.id)

        request.refresh_from_db()
        self.assertEqual(
            claim.execution_payload,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
                "ocr_retry_mode": "transkribus_recognition_only",
                "source_transkribus_run_id": run.id,
                LEASE_EXPIRES_AT_PAYLOAD_KEY: request.lease_expires_at,
            },
        )

    def test_translation_payload_is_derived_from_request(self):
        request = self._request(
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )

        claim = claim_process_document_request(request_id=request.id)

        request.refresh_from_db()
        self.assertEqual(
            claim.execution_payload,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
                "operation": "retry_hebrew_translation",
                LEASE_EXPIRES_AT_PAYLOAD_KEY: request.lease_expires_at,
            },
        )

    def test_enqueue_failed_message_may_claim_and_clears_failure(self):
        request = self._request(
            status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            failure_code="SQS_SEND_FAILED",
            failure_message="safe",
        )

        claim = claim_process_document_request(request_id=request.id)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.EXECUTE)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.failure_message, "")

    def test_fresh_running_request_defers_without_rotating_token(self):
        token = uuid.uuid4()
        request = self._request(
            status=ProcessDocumentRequest.Status.RUNNING,
            lease_token=token,
            lease_expires_at=timezone.now() + timedelta(minutes=1),
            started_at=timezone.now(),
        )

        claim = claim_process_document_request(request_id=request.id)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.DEFER)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.lease_token, token)

    def test_expired_running_request_is_fenced_for_recovery_without_reclaim(self):
        token = uuid.uuid4()
        request = self._request(
            status=ProcessDocumentRequest.Status.RUNNING,
            lease_token=token,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
            started_at=timezone.now() - timedelta(hours=1),
        )

        claim = claim_process_document_request(request_id=request.id)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.ACK)
        self.assertIsNone(claim.lease_token)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(request.lease_token, token)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNone(request.completed_at)

    def test_recovery_required_and_terminal_requests_ack(self):
        token = uuid.uuid4()
        recovery = self._request(
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
            lease_token=token,
            started_at=timezone.now(),
        )
        self.assertEqual(
            claim_process_document_request(request_id=recovery.id).action,
            ProcessDocumentRequestAction.ACK,
        )

        recovery.status = ProcessDocumentRequest.Status.COMPLETED
        recovery.lease_token = None
        recovery.completed_at = timezone.now()
        recovery.save(
            update_fields=[
                "status",
                "lease_token",
                "completed_at",
                "updated_at",
            ]
        )
        self.assertEqual(
            claim_process_document_request(request_id=recovery.id).action,
            ProcessDocumentRequestAction.ACK,
        )

    def test_completed_outcome_terminalizes_and_clears_lease(self):
        request = self._request()
        claim = claim_process_document_request(request_id=request.id)
        assert claim.lease_token is not None

        terminal = terminalize_process_document_request(
            request_id=request.id,
            lease_token=claim.lease_token,
            outcome=ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED),
        )

        self.assertTrue(terminal)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNotNone(request.completed_at)
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.failure_message, "")

    def test_partial_outcome_preserves_optional_metadata(self):
        request = self._request()
        claim = claim_process_document_request(request_id=request.id)
        assert claim.lease_token is not None

        terminal = terminalize_process_document_request(
            request_id=request.id,
            lease_token=claim.lease_token,
            outcome=ProcessDocumentOutcome(
                ProcessDocumentDisposition.PARTIAL,
                failure_code="PROCESS_DOCUMENT_PARTIAL",
                failure_message="translation incomplete",
            ),
        )

        self.assertTrue(terminal)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.PARTIAL)
        self.assertEqual(request.failure_code, "PROCESS_DOCUMENT_PARTIAL")
        self.assertEqual(request.failure_message, "translation incomplete")

    def test_failed_and_noop_outcomes_always_have_failure_code(self):
        for disposition, expected_code in (
            (ProcessDocumentDisposition.FAILED, "PROCESS_DOCUMENT_FAILED"),
            (ProcessDocumentDisposition.NOOP, "PROCESS_DOCUMENT_NOOP"),
        ):
            with self.subTest(disposition=disposition):
                request = self._request()
                claim = claim_process_document_request(request_id=request.id)
                assert claim.lease_token is not None

                terminal = terminalize_process_document_request(
                    request_id=request.id,
                    lease_token=claim.lease_token,
                    outcome=ProcessDocumentOutcome(disposition),
                )

                self.assertTrue(terminal)
                request.refresh_from_db()
                self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
                self.assertEqual(request.failure_code, expected_code)

    def test_wrong_token_cannot_terminalize(self):
        request = self._request()
        claim = claim_process_document_request(request_id=request.id)
        assert claim.lease_token is not None

        terminal = terminalize_process_document_request(
            request_id=request.id,
            lease_token=uuid.uuid4(),
            outcome=ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED),
        )

        self.assertFalse(terminal)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.lease_token, claim.lease_token)

    def test_late_original_holder_can_terminalize_recovery_required(self):
        token = uuid.uuid4()
        request = self._request(
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
            lease_token=token,
            started_at=timezone.now() - timedelta(hours=1),
        )

        terminal = terminalize_process_document_request(
            request_id=request.id,
            lease_token=token,
            outcome=ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED),
        )

        self.assertTrue(terminal)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)

    def _assert_nonterminal_outcome_keeps_running(
        self,
        disposition: ProcessDocumentDisposition,
    ) -> None:
        request = self._request()
        claim = claim_process_document_request(request_id=request.id)
        assert claim.lease_token is not None

        terminal = terminalize_process_document_request(
            request_id=request.id,
            lease_token=claim.lease_token,
            outcome=ProcessDocumentOutcome(disposition),
        )

        self.assertFalse(terminal)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.lease_token, claim.lease_token)

    def test_deferred_outcome_keeps_running_request(self):
        self._assert_nonterminal_outcome_keeps_running(
            ProcessDocumentDisposition.DEFERRED
        )

    def test_retryable_outcome_keeps_running_request(self):
        self._assert_nonterminal_outcome_keeps_running(
            ProcessDocumentDisposition.RETRYABLE
        )


class ProcessDocumentRequestWorkerPostgresConcurrencyTests(TransactionTestCase):
    """Exercise competing claims on independent PostgreSQL connections."""

    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Worker fencing concurrency test requires PostgreSQL")

        self.document = create_ocr_document(
            title="Concurrent worker fencing document",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="concurrent-worker-fencing.pdf",
            mime_type="application/pdf",
        )
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )

    def test_competing_claims_produce_exactly_one_execute(self):
        barrier = threading.Barrier(2, timeout=10)
        claims: list[ProcessDocumentRequestClaim] = []
        errors: list[Exception] = []
        result_lock = threading.Lock()

        def worker() -> None:
            connections.close_all()
            try:
                barrier.wait()
                claim = claim_process_document_request(
                    request_id=self.request.id,
                )
                with result_lock:
                    claims.append(claim)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        first = threading.Thread(target=worker)
        second = threading.Thread(target=worker)
        first.start()
        second.start()
        first.join(timeout=15)
        second.join(timeout=15)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(claims), 2)

        actions = [claim.action for claim in claims]
        self.assertEqual(actions.count(ProcessDocumentRequestAction.EXECUTE), 1)
        self.assertEqual(actions.count(ProcessDocumentRequestAction.DEFER), 1)

        execute_claim = next(
            claim
            for claim in claims
            if claim.action == ProcessDocumentRequestAction.EXECUTE
        )
        self.assertIsNotNone(execute_claim.lease_token)
        self.assertIsNotNone(execute_claim.execution_payload)

        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RUNNING,
        )
        self.assertEqual(
            self.request.lease_token,
            execute_claim.lease_token,
        )


class ProcessDocumentRequestHandlerTests(TestCase):
    def setUp(self) -> None:
        self.document = create_ocr_document(
            title="Request-aware handler document",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="request-aware-handler.pdf",
            mime_type="application/pdf",
        )
        self.sqs = MagicMock()
        self.queue_url = "https://sqs.example/queue"
        self.receipt_handle = "receipt-handle"

    def _request(self, **overrides) -> ProcessDocumentRequest:
        values = {
            "document": self.document,
            "status": ProcessDocumentRequest.Status.QUEUED,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        }
        values.update(overrides)
        return ProcessDocumentRequest.objects.create(**values)

    def _handle(
        self,
        payload,
        *,
        execute_payload,
    ) -> bool:
        return handle_process_document_request(
            payload,
            sqs=self.sqs,
            queue_url=self.queue_url,
            receipt_handle=self.receipt_handle,
            execute_payload=execute_payload,
        )

    def _visibility_timeouts(self) -> list[int]:
        return [
            call.kwargs["VisibilityTimeout"]
            for call in self.sqs.change_message_visibility.call_args_list
        ]

    def test_invalid_request_ids_ack_without_execution(self):
        for raw_request_id in (None, True, False, 0, -1, 1.0, "1"):
            with self.subTest(raw_request_id=raw_request_id):
                self.sqs.reset_mock()
                execute_payload = MagicMock()

                ack = self._handle(
                    {
                        "type": "PROCESS_DOCUMENT",
                        "request_id": raw_request_id,
                    },
                    execute_payload=execute_payload,
                )

                self.assertTrue(ack)
                execute_payload.assert_not_called()
                self.sqs.change_message_visibility.assert_not_called()

    def test_missing_request_acks_without_execution(self):
        execute_payload = MagicMock()

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": 999_999,
            },
            execute_payload=execute_payload,
        )

        self.assertTrue(ack)
        execute_payload.assert_not_called()
        self.sqs.change_message_visibility.assert_not_called()

    def test_queued_request_executes_canonical_db_payload_and_terminalizes(self):
        request = self._request()
        persisted_lease = {}

        def execute_payload(_payload):
            request.refresh_from_db()
            persisted_lease["value"] = request.lease_expires_at
            return ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED)

        execute_payload_mock = MagicMock(side_effect=execute_payload)

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": request.id,
                "document_id": 999_999,
                "operation": "tampered",
            },
            execute_payload=execute_payload_mock,
        )

        self.assertTrue(ack)
        execute_payload_mock.assert_called_once()
        payload = execute_payload_mock.call_args.args[0]
        self.assertIn(LEASE_EXPIRES_AT_PAYLOAD_KEY, payload)
        self.assertEqual(payload[LEASE_EXPIRES_AT_PAYLOAD_KEY], persisted_lease["value"])
        self.assertEqual(
            payload,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
                LEASE_EXPIRES_AT_PAYLOAD_KEY: persisted_lease["value"],
            },
        )
        self.assertEqual(
            self._visibility_timeouts(),
            [SQS_VISIBILITY_AFTER_CLAIM_SECONDS],
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertIsNone(request.lease_token)

    def test_fresh_running_request_defers_without_execution(self):
        token = uuid.uuid4()
        request = self._request(
            status=ProcessDocumentRequest.Status.RUNNING,
            lease_token=token,
            lease_expires_at=timezone.now() + timedelta(minutes=1),
            started_at=timezone.now(),
        )
        execute_payload = MagicMock()

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": request.id,
            },
            execute_payload=execute_payload,
        )

        self.assertFalse(ack)
        execute_payload.assert_not_called()
        self.assertEqual(
            self._visibility_timeouts(),
            [FRESH_IN_PROGRESS_DEFER_SECONDS],
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(request.lease_token, token)

    def test_partial_outcome_terminalizes_partial_and_acks(self):
        request = self._request()
        execute_payload = MagicMock(
            return_value=ProcessDocumentOutcome(
                ProcessDocumentDisposition.PARTIAL,
                failure_code="PROCESS_DOCUMENT_PARTIAL",
                failure_message="translation incomplete",
            )
        )

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": request.id,
            },
            execute_payload=execute_payload,
        )

        self.assertTrue(ack)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.PARTIAL)
        self.assertEqual(request.failure_code, "PROCESS_DOCUMENT_PARTIAL")

    def test_retryable_outcome_keeps_message_and_running_lease(self):
        request = self._request()
        execute_payload = MagicMock(
            return_value=ProcessDocumentOutcome(ProcessDocumentDisposition.RETRYABLE)
        )

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": request.id,
            },
            execute_payload=execute_payload,
        )

        self.assertFalse(ack)
        self.assertEqual(
            self._visibility_timeouts(),
            [SQS_VISIBILITY_AFTER_CLAIM_SECONDS],
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertIsNotNone(request.lease_token)

    def test_unexpected_execution_exception_defers_safely(self):
        request = self._request()
        execute_payload = MagicMock(side_effect=RuntimeError("private details"))

        ack = self._handle(
            {
                "type": "PROCESS_DOCUMENT",
                "request_id": request.id,
            },
            execute_payload=execute_payload,
        )

        self.assertFalse(ack)
        self.assertEqual(
            self._visibility_timeouts(),
            [
                SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
                FRESH_IN_PROGRESS_DEFER_SECONDS,
            ],
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertIsNotNone(request.lease_token)


class RunWorkerProcessDocumentRequestDispatchTests(SimpleTestCase):
    @patch("documents.management.commands.run_worker.handle_process_document_request")
    def test_request_aware_payload_delegates_with_sqs_context(
        self,
        mock_handle,
    ):
        command = Command()
        sqs = MagicMock()
        payload = {
            "type": "PROCESS_DOCUMENT",
            "request_id": 17,
            "document_id": 999,
        }
        message = {
            "Body": json.dumps(payload),
            "ReceiptHandle": "receipt-17",
        }
        mock_handle.return_value = True

        ack = command._process_message(
            message,
            sqs=sqs,
            queue_url="https://sqs.example/queue",
        )

        self.assertTrue(ack)
        mock_handle.assert_called_once_with(
            payload,
            sqs=sqs,
            queue_url="https://sqs.example/queue",
            receipt_handle="receipt-17",
            execute_payload=command._execute_process_document_payload,
        )

    @patch("documents.management.commands.run_worker.handle_process_document_request")
    def test_legacy_payload_keeps_existing_execution_path(
        self,
        mock_handle,
    ):
        command = Command()
        execute_payload = MagicMock(
            return_value=ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED)
        )
        command._execute_process_document_payload = execute_payload
        payload = {
            "type": "PROCESS_DOCUMENT",
            "document_id": 23,
        }

        ack = command._process_message(
            {"Body": json.dumps(payload)},
        )

        self.assertTrue(ack)
        execute_payload.assert_called_once_with(payload)
        mock_handle.assert_not_called()

    @patch("documents.management.commands.run_worker.handle_process_document_request")
    def test_request_aware_payload_without_sqs_context_does_not_execute(
        self,
        mock_handle,
    ):
        command = Command()
        execute_payload = MagicMock()
        command._execute_process_document_payload = execute_payload

        ack = command._process_message(
            {
                "Body": json.dumps(
                    {
                        "type": "PROCESS_DOCUMENT",
                        "request_id": 31,
                    }
                )
            }
        )

        self.assertFalse(ack)
        execute_payload.assert_not_called()
        mock_handle.assert_not_called()

    @patch("documents.management.commands.run_worker.handle_process_document_request")
    def test_request_aware_payload_without_receipt_handle_does_not_execute(
        self,
        mock_handle,
    ):
        command = Command()
        execute_payload = MagicMock()
        command._execute_process_document_payload = execute_payload

        ack = command._process_message(
            {
                "Body": json.dumps(
                    {
                        "type": "PROCESS_DOCUMENT",
                        "request_id": 37,
                    }
                )
            },
            sqs=MagicMock(),
            queue_url="https://sqs.example/queue",
        )

        self.assertFalse(ack)
        execute_payload.assert_not_called()
        mock_handle.assert_not_called()
