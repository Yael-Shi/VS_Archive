"""Model tests for corrected/current Transkribus sync queue requests (schema PR1)."""

from __future__ import annotations

import importlib
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from documents.models import (
    Document,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncRequest,
    TranskribusRun,
)
from documents.services.sqs import SYNC_TRANSKRIBUS_CORRECTED_CURRENT
from documents.test_transkribus_corrected_current_sync_models import (
    _create_he_doc,
    _ready_snapshot,
    _staff_user,
    _upload_run,
)

User = get_user_model()


class TranskribusCorrectedCurrentSyncRequestModelTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()

    def test_status_choices_include_recovery_required(self):
        values = set(TranskribusCorrectedCurrentSyncRequest.Status.values)
        self.assertEqual(
            values,
            {
                "QUEUED",
                "RUNNING",
                "RECOVERY_REQUIRED",
                "COMPLETED",
                "REFUSED",
                "FAILED",
                "ENQUEUE_FAILED",
            },
        )

    def test_queued_defaults_shape(self):
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.failure_message, "")
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNone(request.started_at)
        self.assertIsNone(request.completed_at)
        self.assertIsNone(request.attempt_id)

    def test_enqueue_failed_shape(self):
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
        )
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.started_at)

    def test_running_shape(self):
        now = timezone.now()
        token = uuid.uuid4()
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            lease_token=token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )
        self.assertEqual(request.lease_token, token)
        self.assertIsNone(request.completed_at)

    def test_recovery_required_shape(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        now = timezone.now()
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=now,
        )
        self.assertEqual(request.attempt_id, attempt.pk)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNone(request.completed_at)

    def test_completed_shape(self):
        attempt = self._completed_attempt()
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
            attempt=attempt,
            completed_at=timezone.now(),
        )
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.lease_expires_at)

    def test_refused_shape(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
            completed_at=timezone.now(),
        )
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.REFUSED,
            attempt=attempt,
            completed_at=timezone.now(),
        )
        self.assertIsNone(request.lease_token)

    def test_failed_shape_without_attempt(self):
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
            completed_at=timezone.now(),
            failure_code="ENQUEUE_FAILED",
            failure_message="Could not enqueue sync request.",
        )
        self.assertIsNone(request.attempt_id)

    def test_queued_with_lease_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
                    lease_token=uuid.uuid4(),
                )

    def test_running_without_lease_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
                    started_at=timezone.now(),
                )

    def test_recovery_required_without_attempt_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=(
                        TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
                    ),
                    lease_token=uuid.uuid4(),
                    started_at=timezone.now(),
                )

    def test_recovery_required_with_lease_expires_rejected_by_db(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        now = timezone.now()
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=(
                        TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
                    ),
                    attempt=attempt,
                    lease_token=uuid.uuid4(),
                    lease_expires_at=now + timedelta(minutes=45),
                    started_at=now,
                )

    def test_completed_without_attempt_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
                    completed_at=timezone.now(),
                )

    def test_failed_without_failure_code_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
                    completed_at=timezone.now(),
                    failure_code="",
                )

    def test_terminal_with_active_lease_rejected_by_db(self):
        now = timezone.now()
        attempt = self._completed_attempt()
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
                    attempt=attempt,
                    completed_at=now,
                    lease_token=uuid.uuid4(),
                    lease_expires_at=now + timedelta(minutes=45),
                )

    def test_only_one_active_request_per_document(self):
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
                    lease_token=uuid.uuid4(),
                    lease_expires_at=timezone.now() + timedelta(minutes=45),
                    started_at=timezone.now(),
                )

    def test_active_uniqueness_includes_recovery_required_and_enqueue_failed(self):
        now = timezone.now()
        token = uuid.uuid4()
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=TranskribusCorrectedCurrentSyncAttempt.objects.create(
                document=self.doc,
                transkribus_run=self.transkribus_run,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
            ),
            lease_token=token,
            started_at=now,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
                )

    def test_new_request_allowed_after_terminal_request(self):
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
            completed_at=timezone.now(),
            failure_code="EXECUTION_LOST",
        )
        follow_up = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        self.assertEqual(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                document=self.doc
            ).count(),
            2,
        )
        self.assertEqual(follow_up.status, "QUEUED")

    def test_attempt_correlation_uniqueness(self):
        attempt = self._completed_attempt()
        now = timezone.now()
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
            attempt=attempt,
            completed_at=now,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=self.doc,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
                    attempt=attempt,
                    completed_at=now,
                    failure_code="DUPLICATE_CORRELATION",
                )

    def test_document_delete_cascades_request(self):
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        request_id = request.pk
        doc_id = self.doc.pk
        self.doc.delete()
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
        self.assertFalse(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                pk=request_id
            ).exists()
        )

    def test_document_delete_cascades_linked_request_and_attempt(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=timezone.now(),
        )
        doc_id = self.doc.pk
        request_id = request.pk
        attempt_id = attempt.pk
        run_id = self.transkribus_run.pk
        self.doc.delete()
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
        self.assertFalse(
            TranskribusCorrectedCurrentSyncRequest.objects.filter(
                pk=request_id
            ).exists()
        )
        self.assertFalse(
            TranskribusCorrectedCurrentSyncAttempt.objects.filter(
                pk=attempt_id
            ).exists()
        )
        self.assertFalse(TranskribusRun.objects.filter(pk=run_id).exists())

    def test_initiated_by_set_null_on_user_delete(self):
        request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        )
        user_id = self.user.pk
        self.user.delete()
        request.refresh_from_db()
        self.assertIsNone(request.initiated_by_id)
        self.assertFalse(User.objects.filter(pk=user_id).exists())

    def test_attempt_must_match_document(self):
        other_doc = _create_he_doc(title="Attempt mismatch")
        other_run = _upload_run(other_doc)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=other_doc,
            transkribus_run=other_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncRequest(
                document=self.doc,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
                attempt=attempt,
                lease_token=uuid.uuid4(),
                started_at=timezone.now(),
            ).save()

    def test_referenced_attempt_delete_restricted(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
            attempt=attempt,
            lease_token=uuid.uuid4(),
            started_at=timezone.now(),
        )
        with transaction.atomic():
            with self.assertRaises(RestrictedError):
                attempt.delete()

    def _completed_attempt(self) -> TranskribusCorrectedCurrentSyncAttempt:
        snap = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        return TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            completed_at=timezone.now(),
            resolved_snapshot=snap,
            storage_outcome=TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED,
        )


class TranskribusCorrectedCurrentSyncRequestMigrationTests(TestCase):
    def test_migration_declares_model_indexes_and_constraints(self):
        migration_module = importlib.import_module(
            "documents.migrations.0044_transkribus_corrected_current_sync_request"
        )
        Migration = migration_module.Migration

        create_op = Migration.operations[0]
        self.assertEqual(create_op.name, "TranskribusCorrectedCurrentSyncRequest")
        field_names = {name for name, _field in create_op.fields}
        self.assertIn("lease_token", field_names)
        self.assertIn("lease_expires_at", field_names)
        self.assertIn("attempt", field_names)
        self.assertIn("last_enqueued_at", field_names)

        constraint_names = {
            op.constraint.name
            for op in Migration.operations
            if op.__class__.__name__ == "AddConstraint"
        }
        self.assertEqual(
            constraint_names,
            {
                "tr_cc_sync_req_status_valid",
                "uniq_tr_cc_sync_req_active_doc",
                "tr_cc_sync_req_queued_shape",
                "tr_cc_sync_req_running_shape",
                "tr_cc_sync_req_recovery_shape",
                "tr_cc_sync_req_success_shape",
                "tr_cc_sync_req_failed_shape",
                "tr_cc_sync_req_terminal_no_lease",
            },
        )

        index_names = {
            op.index.name
            for op in Migration.operations
            if op.__class__.__name__ == "AddIndex"
        }
        self.assertEqual(
            index_names,
            {
                "tr_cc_sync_req_doc_created_idx",
                "tr_cc_sync_req_doc_status_idx",
            },
        )


class TranskribusCorrectedCurrentSyncRequestMessageTypeTests(SimpleTestCase):
    def test_exact_message_type_constant(self):
        self.assertEqual(
            SYNC_TRANSKRIBUS_CORRECTED_CURRENT,
            "SYNC_TRANSKRIBUS_CORRECTED_CURRENT",
        )
