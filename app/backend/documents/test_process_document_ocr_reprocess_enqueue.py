from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
    TranskribusRun,
)
from documents.services.archive_items import create_ocr_document
from documents.services.ocr_reprocess import (
    OcrReprocessAssessment,
    OcrReprocessError,
    OcrRetryMode,
)
from documents.services.process_document_ocr_reprocess_enqueue import (
    OcrReprocessEnqueueError,
    OcrReprocessEnqueueErrorCode,
    apply_ocr_reprocess,
)
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    ProcessDocumentRequestEnqueueErrorCode,
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


class OcrReprocessDurableEnqueueTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="ocr-reprocess-enqueue-staff",
            is_staff=True,
        )
        self.document = create_ocr_document(
            title="OCR reprocess enqueue document",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.FAILED,
            upload_error="prior failure",
            file_s3_key="documents/1/source.jpg",
            mime_type="image/jpeg",
        )

    def _request(
        self,
        *,
        status: str = ProcessDocumentRequest.Status.QUEUED,
        retry_mode: str = ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        source_run: TranskribusRun | None = None,
    ) -> ProcessDocumentRequest:
        values = {
            "document": self.document,
            "initiated_by": self.user,
            "status": status,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.OCR_REPROCESS,
            "ocr_retry_mode": retry_mode,
            "source_transkribus_run": source_run,
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

    def _normal_assessment(self) -> OcrReprocessAssessment:
        return OcrReprocessAssessment(
            document_id=self.document.pk,
            retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
        )

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_normal_request_uses_exact_contract_and_actor(self, mock_enqueue):
        request = self._request()
        mock_enqueue.return_value = _result(
            request,
            "CREATED_AND_ENQUEUED",
            created=True,
            message_sent=True,
            send_attempted=True,
        )

        result = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        mock_enqueue.assert_called_once_with(
            document_id=self.document.pk,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            source_transkribus_run_id=None,
            initiated_by=self.user,
        )
        self.assertEqual(
            result.assessment.retry_mode,
            OcrRetryMode.NORMAL_REENQUEUE,
        )
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
    def test_sequential_double_click_coalesces_without_second_send(self, mock_send):
        first = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )
        second = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.assertEqual(first.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        self.assertEqual(second.enqueue_result.outcome, "ALREADY_QUEUED")
        self.assertEqual(
            first.enqueue_result.request.pk,
            second.enqueue_result.request.pk,
        )
        mock_send.assert_called_once_with(first.enqueue_result.request.pk)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            1,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_enqueue_failed_request_is_retried_after_reassessment(self, mock_send):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)

        result = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.assertEqual(result.enqueue_result.outcome, "REENQUEUED")
        self.assertEqual(result.enqueue_result.request.pk, request.pk)
        mock_send.assert_called_once_with(request.pk)
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
    def test_enqueue_failed_retry_does_not_bypass_new_verified_guard(self, mock_send):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified ground truth",
        )

        with self.assertRaises(OcrReprocessError):
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        mock_send.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_terminal_history_allows_new_intentional_reprocess(self, mock_send):
        completed = self._request(status=ProcessDocumentRequest.Status.COMPLETED)

        result = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.assertEqual(result.enqueue_result.outcome, "CREATED_AND_ENQUEUED")
        self.assertNotEqual(result.enqueue_result.request.pk, completed.pk)
        mock_send.assert_called_once_with(result.enqueue_result.request.pk)
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            2,
        )

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_already_terminal_preserves_worker_document_state(
        self,
        mock_enqueue,
        mock_assess,
    ):
        request = self._request(status=ProcessDocumentRequest.Status.COMPLETED)
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_assess.return_value = self._normal_assessment()
        mock_enqueue.return_value = _result(request, "ALREADY_TERMINAL")

        result = apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.assertEqual(result.enqueue_result.outcome, "ALREADY_TERMINAL")
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_recognition_only_uses_exact_source_run(
        self,
        mock_enqueue,
        mock_assess,
    ):
        source_run = TranskribusRun.objects.create(
            document=self.document,
            status=TranskribusRun.Status.FAILED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="model",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )
        request = self._request(
            retry_mode=(
                ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
            ),
            source_run=source_run,
        )
        assessment = OcrReprocessAssessment(
            document_id=self.document.pk,
            retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
            source_transkribus_run_id=source_run.pk,
        )
        mock_assess.return_value = assessment
        mock_enqueue.return_value = _result(
            request,
            "CREATED_AND_ENQUEUED",
            created=True,
            message_sent=True,
            send_attempted=True,
        )

        result = apply_ocr_reprocess(
            self.document.pk,
            collection_id="col",
            model_id="model",
            initiated_by=self.user,
        )

        self.assertEqual(result.assessment, assessment)
        mock_enqueue.assert_called_once_with(
            document_id=self.document.pk,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=(
                ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
            ),
            source_transkribus_run_id=source_run.pk,
            initiated_by=self.user,
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    def test_missing_and_foreign_source_runs_are_rejected_before_send(
        self,
        mock_assess,
        mock_send,
    ):
        other_document = create_ocr_document(
            title="Other OCR document",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.FAILED,
            file_s3_key="documents/3/source.jpg",
            mime_type="image/jpeg",
        )
        foreign_run = TranskribusRun.objects.create(
            document=other_document,
            status=TranskribusRun.Status.FAILED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="model",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        for source_run_id in (foreign_run.pk, 2_147_483_647):
            with self.subTest(source_run_id=source_run_id):
                mock_assess.return_value = OcrReprocessAssessment(
                    document_id=self.document.pk,
                    retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
                    source_transkribus_run_id=source_run_id,
                )

                with self.assertRaises(OcrReprocessEnqueueError) as ctx:
                    apply_ocr_reprocess(
                        self.document.pk,
                        collection_id="col",
                        model_id="model",
                        initiated_by=self.user,
                    )

                self.assertEqual(
                    ctx.exception.code,
                    OcrReprocessEnqueueErrorCode.REQUEST_REJECTED,
                )

        mock_send.assert_not_called()
        self.assertFalse(
            ProcessDocumentRequest.objects.filter(document=self.document).exists()
        )

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_already_running_preserves_worker_document_state(
        self,
        mock_enqueue,
        mock_assess,
    ):
        request = self._request(status=ProcessDocumentRequest.Status.RUNNING)
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_assess.return_value = self._normal_assessment()
        mock_enqueue.return_value = _result(request, "ALREADY_RUNNING")

        apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_stale_queued_result_does_not_overwrite_terminal_state(
        self,
        mock_enqueue,
        mock_assess,
    ):
        request = self._request()
        stale_result = _result(request, "ALREADY_QUEUED")
        ProcessDocumentRequest.objects.filter(pk=request.pk).update(
            status=ProcessDocumentRequest.Status.COMPLETED,
            completed_at=timezone.now(),
        )
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_assess.return_value = self._normal_assessment()
        mock_enqueue.return_value = stale_result

        apply_ocr_reprocess(
            self.document.pk,
            collection_id="",
            model_id="",
            initiated_by=self.user,
        )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_definite_failure_sets_safe_failed_state(self, mock_enqueue):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        mock_enqueue.return_value = _result(
            request,
            "ENQUEUE_FAILED",
            send_attempted=True,
        )

        with self.assertRaises(OcrReprocessEnqueueError) as ctx:
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            OcrReprocessEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertNotIn("ENQUEUE_SEND_FAILED", str(ctx.exception))
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(self.document.upload_error, str(ctx.exception))

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_ambiguous_failure_sets_same_safe_failed_state(self, mock_enqueue):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        mock_enqueue.return_value = _result(
            request,
            "ENQUEUE_OUTCOME_UNKNOWN",
            message_sent=None,
            send_attempted=True,
        )

        with self.assertRaises(OcrReprocessEnqueueError) as ctx:
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            OcrReprocessEnqueueErrorCode.QUEUE_UNAVAILABLE,
        )
        self.assertEqual(ctx.exception.outcome, "ENQUEUE_OUTCOME_UNKNOWN")
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(self.document.upload_error, str(ctx.exception))

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue.assess_ocr_reprocess"
    )
    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_stale_failure_does_not_overwrite_worker_state(
        self,
        mock_enqueue,
        mock_assess,
    ):
        request = self._request(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        stale_result = _result(
            request,
            "ENQUEUE_OUTCOME_UNKNOWN",
            message_sent=None,
            send_attempted=True,
        )
        ProcessDocumentRequest.objects.filter(pk=request.pk).update(
            status=ProcessDocumentRequest.Status.RUNNING,
            lease_token=uuid.uuid4(),
            lease_expires_at=timezone.now() + timedelta(minutes=45),
            started_at=timezone.now(),
            failure_code="",
            failure_message="",
        )
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.upload_error = None
        self.document.save(update_fields=["processing_state_user", "upload_error"])
        mock_assess.return_value = self._normal_assessment()
        mock_enqueue.return_value = stale_result

        with self.assertRaises(OcrReprocessEnqueueError):
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.READY,
        )
        self.assertIsNone(self.document.upload_error)

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_active_conflict_is_safe_and_does_not_mutate(self, mock_enqueue):
        request = self._request()
        mock_enqueue.return_value = _result(request, "ACTIVE_REQUEST_CONFLICT")

        with self.assertRaises(OcrReprocessEnqueueError) as ctx:
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            OcrReprocessEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
        )
        self.assertEqual(ctx.exception.http_status, 409)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(self.document.upload_error, "prior failure")

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_recovery_required_is_safe_and_does_not_mutate(self, mock_enqueue):
        request = self._request(status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED)
        mock_enqueue.return_value = _result(
            request,
            "BLOCKED_RECOVERY_REQUIRED",
        )

        with self.assertRaises(OcrReprocessEnqueueError) as ctx:
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            OcrReprocessEnqueueErrorCode.RECOVERY_REQUIRED,
        )
        self.assertEqual(ctx.exception.http_status, 409)
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request"
    )
    def test_request_validation_error_is_wrapped_safely(self, mock_enqueue):
        mock_enqueue.side_effect = ProcessDocumentRequestEnqueueError(
            ProcessDocumentRequestEnqueueErrorCode.SOURCE_RUN_NOT_FOUND,
            "private source-run detail",
        )

        with self.assertRaises(OcrReprocessEnqueueError) as ctx:
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        self.assertEqual(
            ctx.exception.code,
            OcrReprocessEnqueueErrorCode.REQUEST_REJECTED,
        )
        self.assertNotIn("private source-run detail", str(ctx.exception))

    @patch(
        "documents.services.process_document_ocr_reprocess_enqueue."
        "enqueue_process_document_request",
        side_effect=RuntimeError("programming failure"),
    )
    def test_programming_error_propagates(self, mock_enqueue):
        with self.assertRaisesMessage(RuntimeError, "programming failure"):
            apply_ocr_reprocess(
                self.document.pk,
                collection_id="",
                model_id="",
                initiated_by=self.user,
            )

        mock_enqueue.assert_called_once()


class OcrReprocessDurableEnqueuePostgresConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("OCR reprocess concurrency tests require PostgreSQL")
        self.document = create_ocr_document(
            title="Concurrent OCR reprocess enqueue document",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.FAILED,
            upload_error="prior failure",
            file_s3_key="documents/2/source.jpg",
            mime_type="image/jpeg",
        )
        self.user = User.objects.create_user(username="ocr-reprocess-concurrent-a")
        self.user2 = User.objects.create_user(username="ocr-reprocess-concurrent-b")

    def test_concurrent_double_click_sends_exactly_once(self):
        assessment_barrier = threading.Barrier(2, timeout=10)
        results: list[EnqueueResult] = []
        errors: list[Exception] = []
        send_ids: list[int] = []
        result_lock = threading.Lock()

        def assess_once(*args, **kwargs) -> OcrReprocessAssessment:
            assessment_barrier.wait()
            return OcrReprocessAssessment(
                document_id=self.document.pk,
                retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
            )

        def tracked_send(request_id: int) -> None:
            with result_lock:
                send_ids.append(request_id)

        def worker(user_id: int) -> None:
            connections.close_all()
            try:
                user = User.objects.get(pk=user_id)
                result = apply_ocr_reprocess(
                    self.document.pk,
                    collection_id="",
                    model_id="",
                    initiated_by=user,
                )
                with result_lock:
                    results.append(result.enqueue_result)
            except Exception as exc:
                with result_lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        with (
            patch(
                "documents.services.process_document_ocr_reprocess_enqueue."
                "assess_ocr_reprocess",
                side_effect=assess_once,
            ),
            patch(
                "documents.services.process_document_request_enqueue."
                "send_process_document_request_message",
                side_effect=tracked_send,
            ),
        ):
            first = threading.Thread(target=worker, args=(self.user.pk,))
            second = threading.Thread(target=worker, args=(self.user2.pk,))
            first.start()
            second.start()
            first.join(timeout=15)
            second.join(timeout=15)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len(send_ids), 1)
        outcomes = {result.outcome for result in results}
        self.assertIn("CREATED_AND_ENQUEUED", outcomes)
        self.assertTrue(outcomes <= {"CREATED_AND_ENQUEUED", "ALREADY_QUEUED"})
        self.assertEqual(
            ProcessDocumentRequest.objects.filter(document=self.document).count(),
            1,
        )
