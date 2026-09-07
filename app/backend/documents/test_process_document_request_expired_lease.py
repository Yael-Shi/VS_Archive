from __future__ import annotations

import io
import inspect
import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from documents.models import Document, DocumentTextResult, ProcessDocumentRequest
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
    ProcessDocumentOutcome,
)
from documents.services.process_document_request_enqueue import (
    enqueue_process_document_request,
)
from documents.services.process_document_request_expired_lease import (
    ExpiredLeaseFenceOutcome,
    expired_running_process_document_requests,
    fence_locked_expired_running_process_document_request,
    fence_process_document_request_expired_lease,
    inspect_process_document_request_expired_lease,
    lock_document_then_request,
    process_document_request_lease_is_live,
)
from documents.services.process_document_request_recovery import (
    assess_process_document_request_recovery,
)
from documents.services.process_document_request_worker import (
    ProcessDocumentRequestAction,
    claim_process_document_request,
    terminalize_process_document_request,
)
from documents.services.processing_state import (
    update_document_processing_state_for_engine,
)


def _document(
    title: str,
    *,
    processing_state: str = Document.ProcessingState.PROCESSING,
) -> Document:
    return create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=processing_state,
        file_s3_key=f"expired-lease-{title}.pdf",
        mime_type="application/pdf",
    )


class ProcessDocumentRequestExpiredLeaseHelperTests(TestCase):
    def setUp(self) -> None:
        self.document = _document("helper")
        self.now = timezone.now()

    def _running(self, **overrides) -> ProcessDocumentRequest:
        values = {
            "document": self.document,
            "status": ProcessDocumentRequest.Status.RUNNING,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            "lease_token": uuid.uuid4(),
            "lease_expires_at": self.now - timedelta(seconds=1),
            "started_at": self.now - timedelta(hours=1),
        }
        values.update(overrides)
        return ProcessDocumentRequest.objects.create(**values)

    def test_lease_is_live_requires_timestamp_strictly_after_now(self):
        request = self._running(lease_expires_at=self.now + timedelta(seconds=1))
        self.assertTrue(process_document_request_lease_is_live(request, now=self.now))
        request.lease_expires_at = self.now
        self.assertFalse(process_document_request_lease_is_live(request, now=self.now))
        request.lease_expires_at = self.now - timedelta(seconds=1)
        self.assertFalse(process_document_request_lease_is_live(request, now=self.now))
        request.lease_expires_at = None
        self.assertFalse(process_document_request_lease_is_live(request, now=self.now))

    def test_locked_helper_fences_expired_running_and_processing_overlay(self):
        token = uuid.uuid4()
        request = self._running(lease_token=token)
        started_at = request.started_at

        with transaction.atomic():
            document, locked = lock_document_then_request(request.pk)
            fenced = fence_locked_expired_running_process_document_request(
                document=document,
                sync_request=locked,
                now=self.now,
            )

        self.assertTrue(fenced)
        request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(request.lease_token, token)
        self.assertIsNone(request.lease_expires_at)
        self.assertEqual(request.started_at, started_at)
        self.assertIsNone(request.completed_at)
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )

    def test_locked_helper_does_not_overwrite_ordinary_document_state(self):
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.save(update_fields=["processing_state_user", "updated_at"])
        request = self._running()

        with transaction.atomic():
            document, locked = lock_document_then_request(request.pk)
            fenced = fence_locked_expired_running_process_document_request(
                document=document,
                sync_request=locked,
                now=self.now,
            )

        self.assertTrue(fenced)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )

    def test_locked_helper_skips_live_and_non_running(self):
        live = self._running(lease_expires_at=self.now + timedelta(minutes=1))
        queued_document = _document("helper-queued")
        queued = ProcessDocumentRequest.objects.create(
            document=queued_document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        )

        with transaction.atomic():
            document, locked_live = lock_document_then_request(live.pk)
            self.assertFalse(
                fence_locked_expired_running_process_document_request(
                    document=document,
                    sync_request=locked_live,
                    now=self.now,
                )
            )
            queued_doc, locked_queued = lock_document_then_request(queued.pk)
            self.assertFalse(
                fence_locked_expired_running_process_document_request(
                    document=queued_doc,
                    sync_request=locked_queued,
                    now=self.now,
                )
            )

        live.refresh_from_db()
        queued.refresh_from_db()
        self.assertEqual(live.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(queued.status, ProcessDocumentRequest.Status.QUEUED)

    def test_lock_helper_locks_document_before_request(self):
        source = inspect.getsource(lock_document_then_request)
        document_pos = source.find("Document.objects.select_for_update()")
        request_pos = source.find("ProcessDocumentRequest.objects.select_for_update()")
        self.assertGreater(document_pos, -1)
        self.assertGreater(request_pos, document_pos)

    def test_claim_reuses_shared_expired_running_helper(self):
        request = self._running()
        with patch(
            "documents.services.process_document_request_worker."
            "fence_locked_expired_running_process_document_request",
            return_value=True,
        ) as mock_fence:
            claim = claim_process_document_request(request_id=request.pk)

        self.assertEqual(claim.action, ProcessDocumentRequestAction.ACK)
        mock_fence.assert_called_once()
        kwargs = mock_fence.call_args.kwargs
        self.assertEqual(kwargs["sync_request"].pk, request.pk)
        self.assertEqual(kwargs["document"].pk, self.document.pk)


class ProcessDocumentRequestExpiredLeaseCommandTests(TransactionTestCase):
    def setUp(self) -> None:
        self.now = timezone.now()
        self.document = _document("command")
        self.token = uuid.uuid4()
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=self.token,
            lease_expires_at=self.now - timedelta(minutes=5),
            started_at=self.now - timedelta(hours=1),
        )

    def _call(self, *args: str) -> str:
        output = io.StringIO()
        call_command(
            "fence_expired_process_document_requests",
            *args,
            stdout=output,
        )
        return output.getvalue()

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch("documents.services.htr_engine.transcribe_pages")
    def test_dry_run_expired_running_is_eligible_without_writes(
        self,
        mock_transcribe,
        mock_send,
    ):
        text = self._call("--request-id", str(self.request.pk))

        self.assertIn("mode=dry-run", text)
        self.assertIn("outcome=ELIGIBLE", text)
        self.assertIn("no changes made (dry-run)", text)
        mock_send.assert_not_called()
        mock_transcribe.assert_not_called()
        self.request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(self.request.lease_token, self.token)
        self.assertIsNotNone(self.request.lease_expires_at)
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch("documents.services.htr_engine.transcribe_pages")
    def test_apply_expired_running_fences(
        self,
        mock_transcribe,
        mock_send,
    ):
        started_at = self.request.started_at
        text = self._call("--apply", "--request-id", str(self.request.pk))

        self.assertIn("mode=apply", text)
        self.assertIn("outcome=FENCED", text)
        mock_send.assert_not_called()
        mock_transcribe.assert_not_called()
        self.request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(self.request.lease_token, self.token)
        self.assertIsNone(self.request.lease_expires_at)
        self.assertEqual(self.request.started_at, started_at)
        self.assertIsNone(self.request.completed_at)
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )

    def test_live_running_is_skipped(self):
        self.request.lease_expires_at = timezone.now() + timedelta(minutes=10)
        self.request.save(update_fields=["lease_expires_at", "updated_at"])
        text = self._call("--request-id", str(self.request.pk))
        self.assertIn("outcome=SKIP_LIVE", text)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)

    def test_recovery_required_is_skipped(self):
        self.request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        self.request.lease_expires_at = None
        self.request.save(update_fields=["status", "lease_expires_at", "updated_at"])
        text = self._call("--request-id", str(self.request.pk))
        self.assertIn("outcome=SKIP_RECOVERY_REQUIRED", text)

    def test_terminal_queued_and_enqueue_failed_are_skipped(self):
        cases = (
            ProcessDocumentRequest.Status.COMPLETED,
            ProcessDocumentRequest.Status.PARTIAL,
            ProcessDocumentRequest.Status.FAILED,
            ProcessDocumentRequest.Status.QUEUED,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        for status in cases:
            with self.subTest(status=status):
                self.request.status = status
                self.request.lease_token = None
                self.request.lease_expires_at = None
                if status in {
                    ProcessDocumentRequest.Status.QUEUED,
                    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                }:
                    self.request.started_at = None
                    self.request.completed_at = None
                else:
                    self.request.started_at = self.now - timedelta(hours=1)
                    self.request.completed_at = self.now
                if status in {
                    ProcessDocumentRequest.Status.PARTIAL,
                    ProcessDocumentRequest.Status.FAILED,
                    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                }:
                    self.request.failure_code = "PROCESS_DOCUMENT_FAILED"
                    self.request.failure_message = "test"
                else:
                    self.request.failure_code = ""
                    self.request.failure_message = ""
                self.request.save()
                text = self._call("--request-id", str(self.request.pk))
                self.assertIn("outcome=SKIP_STATUS", text)

    def test_document_scope_does_not_touch_other_document(self):
        other_document = _document("command-other")
        other = ProcessDocumentRequest.objects.create(
            document=other_document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=self.now - timedelta(minutes=5),
            started_at=self.now - timedelta(hours=1),
        )
        text = self._call(
            "--apply",
            "--document-id",
            str(self.document.pk),
        )
        self.assertIn("outcome=FENCED", text)
        other.refresh_from_db()
        self.assertEqual(other.status, ProcessDocumentRequest.Status.RUNNING)

    def test_combined_scope_dry_run_skips_request_on_other_document(self):
        other_document = _document("scope-dry-run-other")
        text = self._call(
            "--request-id",
            str(self.request.pk),
            "--document-id",
            str(other_document.pk),
        )
        self.assertIn("outcome=SKIP_SCOPE", text)
        self.assertNotIn("outcome=ELIGIBLE", text)
        self.assertNotIn("outcome=NOT_FOUND", text)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(self.request.lease_token, self.token)
        self.assertIsNotNone(self.request.lease_expires_at)

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch("documents.services.htr_engine.transcribe_pages")
    def test_combined_scope_apply_does_not_fence_request_on_other_document(
        self,
        mock_transcribe,
        mock_send,
    ):
        other_document = _document("scope-apply-other")
        text = self._call(
            "--apply",
            "--request-id",
            str(self.request.pk),
            "--document-id",
            str(other_document.pk),
        )
        self.assertIn("outcome=SKIP_SCOPE", text)
        self.assertNotIn("outcome=FENCED", text)
        mock_send.assert_not_called()
        mock_transcribe.assert_not_called()
        self.request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(self.request.lease_token, self.token)
        self.assertIsNotNone(self.request.lease_expires_at)
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )

    def test_combined_scope_apply_fences_when_request_matches_document(self):
        text = self._call(
            "--apply",
            "--request-id",
            str(self.request.pk),
            "--document-id",
            str(self.document.pk),
        )
        self.assertIn("outcome=FENCED", text)
        self.request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )

    def test_combined_scope_missing_request_is_not_found(self):
        text = self._call(
            "--request-id",
            "999999",
            "--document-id",
            str(self.document.pk),
        )
        self.assertIn("outcome=NOT_FOUND", text)
        self.assertNotIn("outcome=SKIP_SCOPE", text)
        self.assertIn("request_id=999999", text)

    def test_write_time_document_scope_blocks_even_if_selector_is_bypassed(self):
        other_document = _document("scope-write-time-other")
        result = fence_process_document_request_expired_lease(
            self.request.pk,
            now=self.now,
            allowed_document_ids=frozenset({other_document.pk}),
        )
        self.assertEqual(result.outcome, ExpiredLeaseFenceOutcome.SKIP_SCOPE)
        self.assertFalse(result.applied)
        self.assertEqual(result.document_id, self.document.pk)
        self.request.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertEqual(self.request.lease_token, self.token)
        self.assertIsNotNone(self.request.lease_expires_at)
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )

    def test_request_id_only_apply_still_fences_without_document_flag(self):
        text = self._call("--apply", "--request-id", str(self.request.pk))
        self.assertIn("outcome=FENCED", text)
        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_all_eligible_honors_limit(self):
        second_document = _document("command-second")
        second = ProcessDocumentRequest.objects.create(
            document=second_document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=self.now - timedelta(minutes=1),
            started_at=self.now - timedelta(hours=1),
        )
        text = self._call("--all-eligible", "--limit", "1")
        self.assertIn("selected=1", text)
        self.assertIn(f"request_id={self.request.pk}", text)
        self.assertNotIn(f"request_id={second.pk}", text)

    def test_apply_requires_explicit_scope(self):
        with self.assertRaisesMessage(
            CommandError,
            "--apply requires --request-id, --document-id, or --all-eligible",
        ):
            call_command("fence_expired_process_document_requests", "--apply")

    def test_all_eligible_cannot_combine_with_ids(self):
        with self.assertRaisesMessage(
            CommandError, "--all-eligible cannot be combined"
        ):
            call_command(
                "fence_expired_process_document_requests",
                "--all-eligible",
                "--request-id",
                str(self.request.pk),
            )

    def test_invalid_ids_and_limit_are_rejected(self):
        with self.assertRaisesMessage(CommandError, "--limit must be between"):
            call_command("fence_expired_process_document_requests", "--limit", "0")
        with self.assertRaisesMessage(CommandError, "positive integers"):
            call_command(
                "fence_expired_process_document_requests",
                "--request-id",
                "0",
            )

    def test_missing_request_is_not_found(self):
        text = self._call("--request-id", "999999")
        self.assertIn("outcome=NOT_FOUND", text)
        self.assertIn("request_id=999999", text)

    def test_repeated_apply_is_idempotent(self):
        self._call("--apply", "--request-id", str(self.request.pk))
        text = self._call("--apply", "--request-id", str(self.request.pk))
        self.assertIn("outcome=SKIP_RECOVERY_REQUIRED", text)
        self.request.refresh_from_db()
        self.assertEqual(self.request.lease_token, self.token)
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_state_changed_after_candidate_is_skipped_on_apply(self):
        candidate_ids = list(
            expired_running_process_document_requests(now=self.now).values_list(
                "pk",
                flat=True,
            )
        )
        self.assertIn(self.request.pk, candidate_ids)
        self.request.status = ProcessDocumentRequest.Status.COMPLETED
        self.request.lease_token = None
        self.request.lease_expires_at = None
        self.request.completed_at = self.now
        self.request.save()

        result = fence_process_document_request_expired_lease(
            self.request.pk,
            now=self.now,
        )
        self.assertEqual(result.outcome, ExpiredLeaseFenceOutcome.SKIP_STATUS)
        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.COMPLETED,
        )

    def test_apply_does_not_overwrite_ready_document(self):
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.save(update_fields=["processing_state_user", "updated_at"])
        self._call("--apply", "--request-id", str(self.request.pk))
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )

    def test_late_holder_can_terminalize_after_command_fence_and_rollup(self):
        self._call("--apply", "--request-id", str(self.request.pk))
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="usable hebrew transcription",
        )
        update_document_processing_state_for_engine(
            self.document,
            "gemini-2.0-flash",
        )
        self.document.save(update_fields=["processing_state_user"])
        terminal = terminalize_process_document_request(
            request_id=self.request.pk,
            lease_token=self.token,
            outcome=ProcessDocumentOutcome(ProcessDocumentDisposition.COMPLETED),
        )
        self.assertTrue(terminal)
        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.COMPLETED,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_recover_command_still_does_not_replay_expired_running(
        self,
        mock_send,
    ):
        assessment = assess_process_document_request_recovery(
            self.request.pk,
            now=self.now,
        )
        self.assertFalse(assessment.eligible)
        self.assertEqual(assessment.reason, "STATUS_NOT_RECOVERABLE")
        output = io.StringIO()
        call_command(
            "recover_process_document_requests",
            "--apply",
            "--request-id",
            str(self.request.pk),
            stdout=output,
        )
        mock_send.assert_not_called()
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)

    def test_inspect_unlocked_does_not_fence(self):
        result = inspect_process_document_request_expired_lease(
            self.request.pk,
            now=self.now,
        )
        self.assertEqual(result.outcome, ExpiredLeaseFenceOutcome.ELIGIBLE)
        self.assertFalse(result.applied)
        self.request.refresh_from_db()
        self.assertEqual(self.request.status, ProcessDocumentRequest.Status.RUNNING)


class ProcessDocumentRequestExpiredLeasePostgresTests(TransactionTestCase):
    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Expired-lease concurrency test requires PostgreSQL")
        self.now = timezone.now()
        self.document = _document("concurrent-fence")
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=self.now - timedelta(minutes=5),
            started_at=self.now - timedelta(hours=1),
        )

    def test_concurrent_apply_fences_once(self):
        barrier = threading.Barrier(2, timeout=10)
        outcomes: list[str] = []
        errors: list[Exception] = []
        result_lock = threading.Lock()

        def worker() -> None:
            connections.close_all()
            try:
                barrier.wait()
                result = fence_process_document_request_expired_lease(
                    self.request.pk,
                    now=self.now,
                )
                with result_lock:
                    outcomes.append(result.outcome)
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
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes.count(ExpiredLeaseFenceOutcome.FENCED), 1)
        self.assertEqual(
            outcomes.count(ExpiredLeaseFenceOutcome.SKIP_RECOVERY_REQUIRED),
            1,
        )
        self.request.refresh_from_db()
        self.assertEqual(
            self.request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )


class ProcessDocumentRequestExpiredLeaseEnqueueBoundaryTests(TransactionTestCase):
    def setUp(self) -> None:
        self.document = _document("enqueue-boundary")
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=timezone.now() - timedelta(seconds=1),
            started_at=timezone.now() - timedelta(hours=1),
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_fenced_request_blocks_enqueue_without_send(self, mock_send):
        result = fence_process_document_request_expired_lease(self.request.pk)
        self.assertEqual(result.outcome, ExpiredLeaseFenceOutcome.FENCED)
        enqueue_result = enqueue_process_document_request(
            document_id=self.document.id,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            source_transkribus_run_id=None,
            initiated_by=None,
        )
        mock_send.assert_not_called()
        self.assertEqual(enqueue_result.outcome, "BLOCKED_RECOVERY_REQUIRED")


class ProcessDocumentRequestExpiredLeaseImportTests(SimpleTestCase):
    def test_service_module_does_not_import_sqs_or_worker(self):
        import documents.services.process_document_request_expired_lease as module

        source = inspect.getsource(module)
        self.assertNotIn("send_process_document_request_message", source)
        self.assertNotIn("transcribe_pages", source)
        self.assertNotIn("claim_process_document_request", source)
        self.assertNotIn("documents.services.sqs", source)
        self.assertNotIn("htr_engine", source)
        self.assertNotIn("gemini_engine", source)
