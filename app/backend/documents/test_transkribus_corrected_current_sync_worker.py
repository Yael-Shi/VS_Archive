"""Tests for corrected/current sync worker claim, fencing, and reconciliation."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from documents.management.commands.run_worker import Command as RunWorkerCommand
from documents.models import (
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncRequest,
)
from documents.services.env_validation import WorkerEnvConfig
from documents.services.sqs import SYNC_TRANSKRIBUS_CORRECTED_CURRENT
from documents.services.transkribus_corrected_current_sync import (
    CorrectedCurrentSyncError,
    CorrectedCurrentSyncFailureCode,
    CorrectedCurrentSyncFencedOutError,
    CorrectedCurrentSyncResult,
)
from documents.services.transkribus_corrected_current_sync_worker import (
    FRESH_IN_PROGRESS_DEFER_SECONDS,
    SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
    STARTED_RECOVERY_REQUIRED,
    _terminalize_request_with_lease,
    handle_sync_transkribus_corrected_current,
)
from documents.services.transkribus_snapshot_storage import SnapshotStorageOutcome
from documents.test_transkribus_corrected_current_sync_models import (
    _create_he_doc,
    _ready_snapshot,
    _staff_user,
    _upload_run,
)

User = get_user_model()


def _worker_env(**kwargs) -> WorkerEnvConfig:
    defaults = dict(
        gemini_api_key="k",
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
        transkribus_api_token="token",
        transkribus_username="u",
        transkribus_password="p",
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=2048,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.7,
    )
    defaults.update(kwargs)
    return WorkerEnvConfig(**defaults)


def _completed_attempt(
    *, document, run, user
) -> TranskribusCorrectedCurrentSyncAttempt:
    """COMPLETED Attempt satisfying tr_cc_sync_completed_shape (READY snapshot)."""
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


def _started_attempt(*, document, run, user) -> TranskribusCorrectedCurrentSyncAttempt:
    return TranskribusCorrectedCurrentSyncAttempt.objects.create(
        document=document,
        transkribus_run=run,
        initiated_by=user,
        status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
    )


class CorrectedCurrentSyncWorkerTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.run = _upload_run(self.doc)
        self.user = _staff_user()
        self.sqs = MagicMock()
        self.queue_url = "https://sqs.example/test"
        self.receipt = "receipt-1"
        self.worker_env = _worker_env()

    def _queued_request(self) -> TranskribusCorrectedCurrentSyncRequest:
        return TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
            last_enqueued_at=timezone.now(),
        )

    def _running_request(
        self,
        *,
        lease_token: uuid.UUID | None = None,
        lease_expires_at=None,
        attempt=None,
        started_at=None,
    ) -> TranskribusCorrectedCurrentSyncRequest:
        now = timezone.now()
        token = lease_token or uuid.uuid4()
        return TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            lease_token=token,
            lease_expires_at=lease_expires_at
            if lease_expires_at is not None
            else now + timedelta(minutes=45),
            started_at=started_at or now,
            attempt=attempt,
        )

    def _handle(self, request_id: int, *, run_sync=None, receipt: str | None = None):
        kwargs: dict[str, Any] = dict(
            sqs=self.sqs,
            queue_url=self.queue_url,
            receipt_handle=receipt or self.receipt,
            worker_env=self.worker_env,
        )
        if run_sync is not None:
            kwargs["run_sync"] = run_sync
        return handle_sync_transkribus_corrected_current(
            {"type": SYNC_TRANSKRIBUS_CORRECTED_CURRENT, "request_id": request_id},
            **kwargs,
        )

    def _visibility_timeouts(self) -> list[int]:
        return [
            c.kwargs["VisibilityTimeout"]
            for c in self.sqs.change_message_visibility.call_args_list
        ]

    def test_invalid_request_id_values_ack_without_db_or_sync(self):
        run_sync = MagicMock()
        cases: list[tuple[str, dict[str, Any]]] = [
            ("string", {"request_id": "x"}),
            ("numeric_string", {"request_id": "12"}),
            ("bool_true", {"request_id": True}),
            ("bool_false", {"request_id": False}),
            ("zero", {"request_id": 0}),
            ("negative", {"request_id": -1}),
            ("float", {"request_id": 1.5}),
            ("null", {"request_id": None}),
            ("missing_key", {}),
        ]
        for label, request_fields in cases:
            with self.subTest(case=label):
                run_sync.reset_mock()
                self.sqs.reset_mock()
                payload: dict[str, Any] = {
                    "type": SYNC_TRANSKRIBUS_CORRECTED_CURRENT,
                    **request_fields,
                }
                ack = handle_sync_transkribus_corrected_current(
                    payload,
                    sqs=self.sqs,
                    queue_url=self.queue_url,
                    receipt_handle=self.receipt,
                    worker_env=self.worker_env,
                    run_sync=run_sync,
                )
                self.assertTrue(ack)
                run_sync.assert_not_called()
                self.sqs.change_message_visibility.assert_not_called()

    def test_missing_request_acks_without_sync(self):
        run_sync = MagicMock()
        ack = self._handle(999999, run_sync=run_sync)
        self.assertTrue(ack)
        run_sync.assert_not_called()

    def test_terminal_request_noop_ack(self):
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
            attempt=attempt,
            completed_at=timezone.now(),
        )
        run_sync = MagicMock()
        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )

    def test_concurrent_delivery_defers_while_lease_fresh(self):
        token = uuid.uuid4()
        req = self._running_request(lease_token=token)
        run_sync = MagicMock()
        ack = self._handle(req.pk, run_sync=run_sync)
        self.assertFalse(ack)
        run_sync.assert_not_called()
        self.assertEqual(self._visibility_timeouts(), [FRESH_IN_PROGRESS_DEFER_SECONDS])
        req.refresh_from_db()
        self.assertEqual(req.lease_token, token)
        self.assertIsNone(req.attempt_id)

    def test_expired_reclaim_before_attempt_rotates_token_and_executes(self):
        old_token = uuid.uuid4()
        req = self._running_request(
            lease_token=old_token,
            lease_expires_at=timezone.now() - timedelta(minutes=1),
        )
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)

        def run_sync(**kwargs):
            self.assertEqual(kwargs["sync_request_id"], req.pk)
            self.assertNotEqual(kwargs["lease_token"], old_token)
            req.refresh_from_db()
            self.assertEqual(req.lease_token, kwargs["lease_token"])
            req.attempt = attempt
            req.save(update_fields=["attempt", "updated_at"])
            return CorrectedCurrentSyncResult(
                attempt=attempt,
                refused=False,
                snapshot=None,
                storage_outcome=SnapshotStorageOutcome.CREATED,
            )

        ack = self._handle(req.pk, run_sync=run_sync)
        self.assertTrue(ack)
        self.assertIn(SQS_VISIBILITY_AFTER_CLAIM_SECONDS, self._visibility_timeouts())
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )
        self.assertEqual(req.attempt_id, attempt.pk)
        self.assertIsNone(req.lease_token)

    def test_queued_claim_extends_visibility_and_terminalizes(self):
        req = self._queued_request()
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)

        def run_sync(**kwargs):
            req.attempt = attempt
            req.save(update_fields=["attempt", "updated_at"])
            return CorrectedCurrentSyncResult(
                attempt=attempt,
                refused=False,
                snapshot=None,
                storage_outcome=SnapshotStorageOutcome.CREATED,
            )

        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        self.assertEqual(
            self.sqs.change_message_visibility.call_args.kwargs["VisibilityTimeout"],
            SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
        )
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )

    def test_stale_token_fencing_before_provider_io_defers(self):
        req = self._queued_request()
        run_sync = MagicMock(side_effect=CorrectedCurrentSyncFencedOutError("fenced"))
        ack = self._handle(req.pk, run_sync=run_sync)
        self.assertFalse(ack)
        run_sync.assert_called_once()
        self.assertIn(FRESH_IN_PROGRESS_DEFER_SECONDS, self._visibility_timeouts())
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )
        self.assertIsNone(req.attempt_id)
        self.assertIsNotNone(req.lease_token)

    def test_linked_started_fresh_never_reruns(self):
        attempt = _started_attempt(document=self.doc, run=self.run, user=self.user)
        TranskribusCorrectedCurrentSyncAttempt.objects.filter(pk=attempt.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        req = self._running_request(attempt=attempt)
        run_sync = MagicMock()
        ack = self._handle(req.pk, run_sync=run_sync)
        self.assertFalse(ack)
        run_sync.assert_not_called()
        self.assertEqual(self._visibility_timeouts(), [FRESH_IN_PROGRESS_DEFER_SECONDS])
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )

    def test_linked_started_just_below_60_minutes_defers(self):
        fixed_now = timezone.now()
        attempt = _started_attempt(document=self.doc, run=self.run, user=self.user)
        TranskribusCorrectedCurrentSyncAttempt.objects.filter(pk=attempt.pk).update(
            created_at=fixed_now - STARTED_RECOVERY_REQUIRED + timedelta(microseconds=1)
        )
        req = self._running_request(attempt=attempt)
        run_sync = MagicMock()
        with patch(
            "documents.services.transkribus_corrected_current_sync_worker.timezone.now",
            return_value=fixed_now,
        ):
            ack = self._handle(req.pk, run_sync=run_sync)
        self.assertFalse(ack)
        run_sync.assert_not_called()
        self.assertEqual(self._visibility_timeouts(), [FRESH_IN_PROGRESS_DEFER_SECONDS])
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )

    def test_linked_started_exactly_60_minutes_moves_to_recovery_required(self):
        fixed_now = timezone.now()
        attempt = _started_attempt(document=self.doc, run=self.run, user=self.user)
        TranskribusCorrectedCurrentSyncAttempt.objects.filter(pk=attempt.pk).update(
            created_at=fixed_now - STARTED_RECOVERY_REQUIRED
        )
        token = uuid.uuid4()
        req = self._running_request(lease_token=token, attempt=attempt)
        run_sync = MagicMock()
        with patch(
            "documents.services.transkribus_corrected_current_sync_worker.timezone.now",
            return_value=fixed_now,
        ):
            ack = self._handle(req.pk, run_sync=run_sync)
        self.assertTrue(ack)
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(req.lease_token, token)
        self.assertIsNone(req.lease_expires_at)
        self.assertIsNone(req.completed_at)
        attempt.refresh_from_db()
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.STARTED
        )

    def test_enqueue_failed_without_attempt_can_be_claimed_and_executed(self):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
            failure_code="ENQUEUE_SEND_FAILED",
            failure_message="SQS send failed.",
        )
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)

        def run_sync(**kwargs):
            self.assertEqual(kwargs["sync_request_id"], req.pk)
            req.refresh_from_db()
            self.assertEqual(
                req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
            )
            self.assertIsNotNone(req.lease_token)
            req.attempt = attempt
            req.save(update_fields=["attempt", "updated_at"])
            return CorrectedCurrentSyncResult(
                attempt=attempt,
                refused=False,
                snapshot=None,
                storage_outcome=SnapshotStorageOutcome.CREATED,
            )

        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        self.assertIn(SQS_VISIBILITY_AFTER_CLAIM_SECONDS, self._visibility_timeouts())
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )
        self.assertEqual(req.attempt_id, attempt.pk)

    def test_terminal_attempt_reconciles_running_request(self):
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        req = self._running_request(attempt=attempt)
        run_sync = MagicMock()
        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )
        self.assertIsNone(req.lease_token)
        self.assertIsNotNone(req.completed_at)

    def test_crash_after_terminal_attempt_reconciles_without_rerun(self):
        """Attempt COMPLETED, Request still RUNNING (worker died before Request update)."""
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        req = self._running_request(attempt=attempt)
        run_sync = MagicMock()
        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )
        self.assertIsNone(req.lease_token)
        self.assertIsNotNone(req.completed_at)

    def test_crash_after_failed_attempt_copies_failure_code(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code="HTTP_METADATA_FAILED",
            failure_message="Transkribus login or pages metadata request failed.",
            completed_at=timezone.now(),
        )
        req = self._running_request(attempt=attempt)
        run_sync = MagicMock()
        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.FAILED
        )
        self.assertEqual(req.failure_code, "HTTP_METADATA_FAILED")

    def test_recovery_required_late_terminal_attempt_reconciles(self):
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=timezone.now() - timedelta(hours=2),
        )
        run_sync = MagicMock()
        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        run_sync.assert_not_called()
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )
        self.assertIsNone(req.lease_token)

    def test_late_worker_terminalizes_from_recovery_required_with_lease(self):
        token = uuid.uuid4()
        attempt = _started_attempt(document=self.doc, run=self.run, user=self.user)
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=token,
            started_at=timezone.now() - timedelta(hours=2),
        )
        # Simulate late legitimate worker finishing after recovery mark.
        snap = _ready_snapshot(document=self.doc, run=self.run)
        attempt.status = TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED
        attempt.completed_at = timezone.now()
        attempt.resolved_snapshot = snap
        attempt.storage_outcome = (
            TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
        )
        attempt.save(
            update_fields=[
                "status",
                "completed_at",
                "resolved_snapshot",
                "storage_outcome",
                "updated_at",
            ]
        )
        # Wrong token must not terminalize (fencing retained on RECOVERY_REQUIRED).
        self.assertFalse(
            _terminalize_request_with_lease(
                request_id=req.pk,
                lease_token=uuid.uuid4(),
                attempt=attempt,
            )
        )
        req.refresh_from_db()
        self.assertEqual(
            req.status,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        )

        self.assertTrue(
            _terminalize_request_with_lease(
                request_id=req.pk,
                lease_token=token,
                attempt=attempt,
            )
        )
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )

    def test_terminalize_rejects_unrelated_attempt_same_document(self):
        token = uuid.uuid4()
        linked = _started_attempt(document=self.doc, run=self.run, user=self.user)
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=linked,
            lease_token=token,
            started_at=timezone.now() - timedelta(hours=2),
        )
        unrelated = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        self.assertNotEqual(unrelated.pk, linked.pk)

        self.assertFalse(
            _terminalize_request_with_lease(
                request_id=req.pk,
                lease_token=token,
                attempt=unrelated,
            )
        )
        req.refresh_from_db()
        self.assertEqual(
            req.status,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(req.attempt_id, linked.pk)
        self.assertEqual(req.lease_token, token)
        self.assertIsNone(req.completed_at)

    def test_terminalize_rejects_unrelated_attempt_other_document(self):
        token = uuid.uuid4()
        linked = _started_attempt(document=self.doc, run=self.run, user=self.user)
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            attempt=linked,
            lease_token=token,
            lease_expires_at=timezone.now() + timedelta(minutes=45),
            started_at=timezone.now(),
        )
        other_doc = _create_he_doc(title="other-doc-for-unrelated-attempt")
        other_run = _upload_run(other_doc, remote_doc_id="888")
        unrelated = _completed_attempt(
            document=other_doc, run=other_run, user=self.user
        )

        self.assertFalse(
            _terminalize_request_with_lease(
                request_id=req.pk,
                lease_token=token,
                attempt=unrelated,
            )
        )
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
        )
        self.assertEqual(req.attempt_id, linked.pk)
        self.assertEqual(req.lease_token, token)
        self.assertIsNone(req.completed_at)

    def test_visibility_failure_still_executes_under_lease(self):
        req = self._queued_request()
        attempt = _completed_attempt(document=self.doc, run=self.run, user=self.user)
        self.sqs.change_message_visibility.side_effect = Exception("visibility boom")

        def run_sync(**kwargs):
            req.attempt = attempt
            req.save(update_fields=["attempt", "updated_at"])
            return CorrectedCurrentSyncResult(
                attempt=attempt,
                refused=False,
                snapshot=None,
                storage_outcome=SnapshotStorageOutcome.CREATED,
            )

        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
        )

    def test_resolve_failure_before_attempt_fails_request(self):
        req = self._queued_request()

        def run_sync(**kwargs):
            raise CorrectedCurrentSyncError(
                "no run",
                failure_code=CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
                attempt_id=None,
            )

        self.assertTrue(self._handle(req.pk, run_sync=run_sync))
        req.refresh_from_db()
        self.assertEqual(
            req.status, TranskribusCorrectedCurrentSyncRequest.Status.FAILED
        )
        self.assertEqual(
            req.failure_code, CorrectedCurrentSyncFailureCode.RUN_RESOLUTION
        )
        self.assertIsNone(req.attempt_id)
        self.assertIsNone(req.lease_token)


class CorrectedCurrentSyncWorkerDispatchTests(TestCase):
    def setUp(self) -> None:
        self.command = RunWorkerCommand()
        self.command._cfg = _worker_env()
        self.doc = _create_he_doc()
        self.user = _staff_user()
        self.sqs = MagicMock()

    def test_dispatch_handles_sync_type_before_process_document(self):
        req = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
            attempt=_completed_attempt(
                document=self.doc,
                run=_upload_run(self.doc),
                user=self.user,
            ),
            completed_at=timezone.now(),
        )
        msg = {
            "Body": json.dumps(
                {
                    "type": SYNC_TRANSKRIBUS_CORRECTED_CURRENT,
                    "request_id": req.pk,
                }
            ),
            "ReceiptHandle": "rh-dispatch",
        }
        with patch(
            "documents.management.commands.run_worker.handle_sync_transkribus_corrected_current",
            return_value=True,
        ) as handler:
            ok = self.command._process_message(
                msg, sqs=self.sqs, queue_url="https://sqs.example/q"
            )
        self.assertTrue(ok)
        handler.assert_called_once()
        self.assertEqual(handler.call_args.args[0]["request_id"], req.pk)

    def test_unknown_message_type_still_acks(self):
        msg = {"Body": json.dumps({"type": "TOTALLY_UNKNOWN"}), "ReceiptHandle": "r"}
        self.assertTrue(
            self.command._process_message(
                msg, sqs=self.sqs, queue_url="https://sqs.example/q"
            )
        )

    def test_sync_type_without_receipt_does_not_ack(self):
        msg = {
            "Body": json.dumps(
                {"type": SYNC_TRANSKRIBUS_CORRECTED_CURRENT, "request_id": 1}
            )
        }
        self.assertFalse(
            self.command._process_message(
                msg, sqs=self.sqs, queue_url="https://sqs.example/q"
            )
        )

    def test_delete_failure_does_not_raise_after_ack(self):
        self.command._delete_message(
            self.sqs,
            "https://sqs.example/q",
            {"ReceiptHandle": "gone"},
        )
        self.sqs.delete_message.side_effect = Exception("expired receipt")
        # Should not raise (existing best-effort delete).
        self.command._delete_message(
            self.sqs,
            "https://sqs.example/q",
            {"ReceiptHandle": "gone"},
        )
