"""Tests for corrected/current sync enqueue service (PR3)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from botocore.exceptions import ClientError, EndpointConnectionError
from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from documents.models import (
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncRequest,
)
from documents.services.sqs import (
    SYNC_TRANSKRIBUS_CORRECTED_CURRENT,
    SqsConfigurationError,
)
from documents.services.transkribus_corrected_current_sync_enqueue import (
    CorrectedCurrentSyncEnqueueError,
    CorrectedCurrentSyncEnqueueErrorCode,
    classify_sqs_send_failure,
    enqueue_transkribus_corrected_current_sync,
)
from documents.test_transkribus_corrected_current_sync_models import (
    _create_he_doc,
    _ready_snapshot,
    _upload_run,
)


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "x"}},
        operation_name="SendMessage",
    )


def _claim_as_worker(req: TranskribusCorrectedCurrentSyncRequest) -> None:
    now = timezone.now()
    req.status = TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
    req.lease_token = uuid.uuid4()
    req.lease_expires_at = now + timedelta(minutes=45)
    req.started_at = now
    req.failure_code = ""
    req.failure_message = ""
    req.save(
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


def _completed_attempt(
    *, document, run, user
) -> TranskribusCorrectedCurrentSyncAttempt:
    snap = _ready_snapshot(document=document, run=run)
    return TranskribusCorrectedCurrentSyncAttempt.objects.create(
        document=document,
        transkribus_run=run,
        initiated_by=user,
        status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
        completed_at=timezone.now(),
        resolved_snapshot=snap,
        storage_outcome=TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED,
    )


class ClassifySqsSendFailureTests(SimpleTestCase):
    def test_definite_access_denied(self):
        self.assertEqual(
            classify_sqs_send_failure(_client_error("AccessDenied")),
            "definite",
        )

    def test_definite_queue_missing(self):
        self.assertEqual(
            classify_sqs_send_failure(
                _client_error("AWS.SimpleQueueService.NonExistentQueue")
            ),
            "definite",
        )

    def test_definite_sqs_configuration_error(self):
        self.assertEqual(
            classify_sqs_send_failure(
                SqsConfigurationError("Missing required env var: SQS_QUEUE_URL")
            ),
            "definite",
        )

    def test_sqs_configuration_error_is_runtime_error_subclass(self):
        exc = SqsConfigurationError("Missing required env var: SQS_QUEUE_URL")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIsInstance(exc, SqsConfigurationError)

    def test_rejects_ordinary_runtime_error(self):
        with self.assertRaises(TypeError):
            classify_sqs_send_failure(RuntimeError("programming bug"))

    def test_ambiguous_throttling_client_error(self):
        self.assertEqual(
            classify_sqs_send_failure(_client_error("ServiceUnavailable")),
            "ambiguous",
        )

    def test_ambiguous_endpoint_connection(self):
        self.assertEqual(
            classify_sqs_send_failure(
                EndpointConnectionError(endpoint_url="https://x")
            ),
            "ambiguous",
        )

    def test_rejects_arbitrary_programming_exceptions(self):
        with self.assertRaises(TypeError):
            classify_sqs_send_failure(ValueError("bug"))


class SendSyncTranskribusCorrectedCurrentMessageTests(SimpleTestCase):
    @patch.dict(
        "os.environ", {"SQS_QUEUE_URL": "https://sqs.example/queue"}, clear=False
    )
    @patch("documents.services.sqs.boto3.client")
    def test_payload_is_type_and_request_id_only(self, mock_boto_client):
        from documents.services.sqs import (
            send_sync_transkribus_corrected_current_message,
        )

        mock_sqs = mock_boto_client.return_value
        send_sync_transkribus_corrected_current_message(42)
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(
            body,
            {"type": SYNC_TRANSKRIBUS_CORRECTED_CURRENT, "request_id": 42},
        )

    def test_rejects_non_positive_request_id(self):
        from documents.services.sqs import (
            send_sync_transkribus_corrected_current_message,
        )

        with self.assertRaises(ValueError):
            send_sync_transkribus_corrected_current_message(0)


class EnqueueCoalesceAndGuardTests(TestCase):
    """Non-send coalesce / validation paths (TestCase is fine)."""

    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.run = _upload_run(self.doc)
        self.user = User.objects.create_user(
            username="enqueue_staff", password="test-pass"
        )
        self.user2 = User.objects.create_user(
            username="enqueue_staff_2", password="test-pass"
        )

    def _enqueue(self, **kwargs):
        return enqueue_transkribus_corrected_current_sync(
            document_id=kwargs.get("document_id", self.doc.pk),
            initiated_by=kwargs.get("initiated_by", self.user),
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_existing_queued_does_not_resend(self, mock_send):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
            last_enqueued_at=timezone.now(),
        )
        result = self._enqueue()
        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_QUEUED")
        self.assertEqual(result.request.pk, req.pk)
        self.assertFalse(result.message_sent)
        self.assertFalse(result.send_attempted)
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_running_does_not_resend(self, mock_send):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            lease_token=uuid.uuid4(),
            lease_expires_at=timezone.now() + timedelta(minutes=45),
            started_at=timezone.now(),
        )
        token = req.lease_token
        result = self._enqueue()
        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertEqual(result.observed_status, req.status)
        req.refresh_from_db()
        self.assertEqual(req.lease_token, token)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_recovery_required_blocks_without_send(self, mock_send):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=timezone.now(),
        )
        result = self._enqueue()
        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "BLOCKED_RECOVERY_REQUIRED")
        self.assertEqual(result.request.pk, req.pk)
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        )

    def test_document_not_found(self):
        with self.assertRaises(CorrectedCurrentSyncEnqueueError) as ctx:
            enqueue_transkribus_corrected_current_sync(
                document_id=9_999_999,
                initiated_by=self.user,
            )
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentSyncEnqueueErrorCode.DOCUMENT_NOT_FOUND,
        )

    def test_initiator_required(self):
        with self.assertRaises(CorrectedCurrentSyncEnqueueError) as ctx:
            enqueue_transkribus_corrected_current_sync(
                document_id=self.doc.pk,
                initiated_by=None,
            )
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentSyncEnqueueErrorCode.INITIATOR_REQUIRED,
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_integrityerror_on_create_coalesces_existing_active(self, mock_send):
        from django.db import IntegrityError

        existing = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
            last_enqueued_at=timezone.now(),
        )

        with (
            patch(
                "documents.services.transkribus_corrected_current_sync_enqueue."
                "_lock_active_request",
                side_effect=[None, existing],
            ),
            patch.object(
                TranskribusCorrectedCurrentSyncRequest.objects,
                "create",
                side_effect=IntegrityError("uniq_tr_cc_sync_req_active_doc"),
            ),
        ):
            result = enqueue_transkribus_corrected_current_sync(
                document_id=self.doc.pk,
                initiated_by=self.user,
            )
        mock_send.assert_not_called()
        self.assertEqual(result.outcome, "ALREADY_QUEUED")
        self.assertEqual(result.request.pk, existing.pk)

    def test_failure_finalize_does_not_touch_recovery_required(self):
        from documents.services.transkribus_corrected_current_sync_enqueue import (
            _finalize_failure,
        )

        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=timezone.now(),
        )
        token = req.lease_token
        updated = _finalize_failure(
            request_id=req.pk,
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="x",
        )
        self.assertEqual(updated, 0)
        req.refresh_from_db()
        self.assertEqual(
            req.status,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(req.lease_token, token)
        self.assertEqual(req.attempt_id, attempt.pk)


class EnqueueSendPathTests(TransactionTestCase):
    """Send-path tests need TransactionTestCase so outer atomic truly ends before SQS."""

    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.run = _upload_run(self.doc)
        self.user = User.objects.create_user(
            username="enqueue_send_staff", password="test-pass"
        )
        self.user2 = User.objects.create_user(
            username="enqueue_send_staff_2", password="test-pass"
        )

    def _enqueue(self, **kwargs):
        return enqueue_transkribus_corrected_current_sync(
            document_id=kwargs.get("document_id", self.doc.pk),
            initiated_by=kwargs.get("initiated_by", self.user),
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_create_path_sends_outside_atomic_and_stamps_last_enqueued(self, mock_send):
        seen_atomic: list[bool] = []

        def _send(request_id: int) -> None:
            seen_atomic.append(connection.in_atomic_block)

        mock_send.side_effect = _send

        result = self._enqueue()
        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertTrue(result.created)
        self.assertTrue(result.message_sent)
        self.assertTrue(result.send_attempted)
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        self.assertEqual(seen_atomic, [False])
        mock_send.assert_called_once_with(result.request.pk)
        result.request.refresh_from_db()
        self.assertIsNotNone(result.request.last_enqueued_at)
        self.assertEqual(result.request.initiated_by_id, self.user.pk)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_enqueue_failed_retry_transitions_and_sends(self, mock_send):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="old",
        )
        result = self._enqueue(initiated_by=self.user2)
        mock_send.assert_called_once_with(req.pk)
        self.assertEqual(result.outcome, "REENQUEUED")
        self.assertFalse(result.created)
        self.assertTrue(result.message_sent)
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.QUEUED
        )
        self.assertEqual(req.failure_code, "")
        self.assertEqual(req.initiated_by_id, self.user2.pk)
        self.assertIsNotNone(req.last_enqueued_at)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_second_caller_after_enqueue_failed_flip_does_not_send(self, mock_send):
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            failure_code="ENQUEUE_SEND_FAILED",
        )
        first = self._enqueue()
        self.assertEqual(first.outcome, "REENQUEUED")
        mock_send.reset_mock()
        second = self._enqueue(initiated_by=self.user2)
        mock_send.assert_not_called()
        self.assertEqual(second.outcome, "ALREADY_QUEUED")
        self.assertEqual(second.request.pk, first.request.pk)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_success_cas_noop_after_worker_claim_returns_observed_running(
        self, mock_send
    ):
        def _send_and_claim(request_id: int) -> None:
            req = TranskribusCorrectedCurrentSyncRequest.objects.get(pk=request_id)
            _claim_as_worker(req)

        mock_send.side_effect = _send_and_claim
        result = self._enqueue()
        self.assertTrue(result.message_sent)
        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
        )
        req = result.request
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )
        self.assertIsNotNone(req.lease_token)
        self.assertIsNone(req.last_enqueued_at)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_success_cas_noop_after_terminal_returns_already_terminal(self, mock_send):
        def _send_and_terminalize(request_id: int) -> None:
            req = TranskribusCorrectedCurrentSyncRequest.objects.get(pk=request_id)
            attempt = _completed_attempt(
                document=self.doc, run=self.run, user=self.user
            )
            req.status = TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
            req.attempt = attempt
            req.completed_at = timezone.now()
            req.lease_token = None
            req.lease_expires_at = None
            req.started_at = timezone.now()
            req.save(
                update_fields=[
                    "status",
                    "attempt",
                    "completed_at",
                    "lease_token",
                    "lease_expires_at",
                    "started_at",
                    "updated_at",
                ]
            )

        mock_send.side_effect = _send_and_terminalize
        result = self._enqueue()
        self.assertTrue(result.message_sent)
        self.assertEqual(result.outcome, "ALREADY_TERMINAL")
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
        )
        self.assertNotEqual(result.outcome, "ALREADY_QUEUED")

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_failure_cas_noop_after_worker_claim_does_not_mark_enqueue_failed(
        self, mock_send
    ):
        def _send_fail_and_claim(request_id: int) -> None:
            req = TranskribusCorrectedCurrentSyncRequest.objects.get(pk=request_id)
            _claim_as_worker(req)
            raise _client_error("AccessDenied")

        mock_send.side_effect = _send_fail_and_claim
        result = self._enqueue()
        self.assertEqual(result.outcome, "ALREADY_RUNNING")
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
        )
        self.assertFalse(result.message_sent)
        req = TranskribusCorrectedCurrentSyncRequest.objects.get(pk=result.request.pk)
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )
        self.assertNotEqual(req.status, "ENQUEUE_FAILED")
        self.assertIsNotNone(req.lease_token)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_definite_send_failure_marks_enqueue_failed(self, mock_send):
        mock_send.side_effect = _client_error("AccessDenied")
        result = self._enqueue()
        self.assertEqual(result.outcome, "ENQUEUE_FAILED")
        self.assertFalse(result.message_sent)
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
        )
        req = result.request
        self.assertEqual(req.failure_code, "ENQUEUE_SEND_FAILED")
        self.assertNotIn("AccessDenied", req.failure_message)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_ambiguous_send_failure_marks_outcome_unknown(self, mock_send):
        mock_send.side_effect = EndpointConnectionError(endpoint_url="https://sqs")
        result = self._enqueue()
        self.assertEqual(result.outcome, "ENQUEUE_OUTCOME_UNKNOWN")
        self.assertIsNone(result.message_sent)
        self.assertEqual(
            result.observed_status,
            TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
        )
        self.assertEqual(result.request.failure_code, "ENQUEUE_OUTCOME_UNKNOWN")
        self.assertIsNone(result.request.lease_token)
        self.assertIsNone(result.request.attempt_id)

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_terminal_history_allows_new_queued_create(self, mock_send):
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
            attempt=attempt,
            completed_at=timezone.now(),
        )
        result = self._enqueue()
        self.assertEqual(result.outcome, "CREATED_AND_ENQUEUED")
        self.assertTrue(result.created)
        self.assertEqual(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc
            ).count(),
            2,
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_programming_value_error_on_send_is_not_converted_to_enqueue_failed(
        self, mock_send
    ):
        mock_send.side_effect = ValueError("programming bug")
        with self.assertRaises(ValueError):
            self._enqueue()
        self.assertFalse(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc,
                status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            ).exists()
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_programming_runtime_error_on_send_is_not_converted_to_enqueue_failed(
        self, mock_send
    ):
        mock_send.side_effect = RuntimeError("programming bug")
        with self.assertRaises(RuntimeError):
            self._enqueue()
        self.assertFalse(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc,
                status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            ).exists()
        )

    @patch(
        "documents.services.transkribus_corrected_current_sync_enqueue."
        "send_sync_transkribus_corrected_current_message"
    )
    def test_sqs_configuration_error_marks_enqueue_failed(self, mock_send):
        mock_send.side_effect = SqsConfigurationError(
            "Missing required env var: SQS_QUEUE_URL"
        )
        result = self._enqueue()
        self.assertEqual(result.outcome, "ENQUEUE_FAILED")
        self.assertFalse(result.message_sent)
        self.assertEqual(result.request.failure_code, "ENQUEUE_SEND_FAILED")
        self.assertNotIn("SQS_QUEUE_URL", result.request.failure_message)


class EnqueuePostgresConcurrencyTests(TransactionTestCase):
    """Real concurrent callers on independent DB connections (PostgreSQL only)."""

    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Enqueue concurrency tests require PostgreSQL")
        self.doc = _create_he_doc()
        self.user = User.objects.create_user(
            username="enqueue_pg_a", password="test-pass"
        )
        self.user2 = User.objects.create_user(
            username="enqueue_pg_b", password="test-pass"
        )

    def _run_concurrent_enqueue(
        self,
        *,
        document_id: int,
        user_a_id: int,
        user_b_id: int,
    ) -> tuple[list, list, list[int]]:
        barrier = threading.Barrier(2, timeout=10)
        results: list = []
        errors: list[Exception] = []
        send_ids: list[int] = []
        lock = threading.Lock()

        def tracked_send(request_id: int) -> None:
            with lock:
                send_ids.append(request_id)

        def worker(user_id: int) -> None:
            connections.close_all()
            try:
                barrier.wait()
                with patch(
                    "documents.services.transkribus_corrected_current_sync_enqueue."
                    "send_sync_transkribus_corrected_current_message",
                    side_effect=tracked_send,
                ):
                    user = User.objects.get(pk=user_id)
                    result = enqueue_transkribus_corrected_current_sync(
                        document_id=document_id,
                        initiated_by=user,
                    )
                with lock:
                    results.append(result)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        t1 = threading.Thread(target=worker, args=(user_a_id,))
        t2 = threading.Thread(target=worker, args=(user_b_id,))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        return results, errors, send_ids

    def test_concurrent_create_exactly_one_send(self):
        results, errors, send_ids = self._run_concurrent_enqueue(
            document_id=self.doc.pk,
            user_a_id=self.user.pk,
            user_b_id=self.user2.pk,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(send_ids), 1)
        outcomes = {r.outcome for r in results}
        self.assertIn("CREATED_AND_ENQUEUED", outcomes)
        self.assertTrue(
            outcomes <= {"CREATED_AND_ENQUEUED", "ALREADY_QUEUED", "ALREADY_RUNNING"}
        )
        self.assertEqual(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc,
                status__in=[
                    TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
                    TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
                    TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
                ],
            ).count(),
            1,
        )

    def test_concurrent_enqueue_failed_retry_exactly_one_send(self):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="prior",
        )
        results, errors, send_ids = self._run_concurrent_enqueue(
            document_id=self.doc.pk,
            user_a_id=self.user.pk,
            user_b_id=self.user2.pk,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(send_ids), 1)
        self.assertEqual(send_ids[0], req.pk)
        outcomes = {r.outcome for r in results}
        self.assertIn("REENQUEUED", outcomes)
        self.assertTrue(outcomes <= {"REENQUEUED", "ALREADY_QUEUED", "ALREADY_RUNNING"})
        self.assertEqual(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc, pk=req.pk
            ).count(),
            1,
        )
