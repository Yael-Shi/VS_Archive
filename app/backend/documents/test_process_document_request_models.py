from __future__ import annotations

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import RestrictedError
from django.test import TestCase
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest, TranskribusRun
from documents.services.archive_items import create_ocr_document


class ProcessDocumentRequestModelTests(TestCase):
    def setUp(self) -> None:
        self.document = self._document("Request document")
        self.other_document = self._document("Other request document")

    def _document(self, title: str) -> Document:
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

    def _run(self, document: Document) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=document,
            status=TranskribusRun.Status.FAILED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="model",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

    def _ocr_kwargs(self, **overrides):
        values = {
            "document": self.document,
            "status": ProcessDocumentRequest.Status.QUEUED,
            "operation": ProcessDocumentRequest.Operation.OCR,
            "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            "ocr_retry_mode": (ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        }
        values.update(overrides)
        return values

    def _assert_integrity_error(self, **kwargs) -> None:
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProcessDocumentRequest.objects.create(**kwargs)

    def test_valid_queued_upload_request(self):
        request = ProcessDocumentRequest.objects.create(**self._ocr_kwargs())

        self.assertEqual(request.status, ProcessDocumentRequest.Status.QUEUED)
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.started_at)
        self.assertIsNone(request.completed_at)

    def test_status_value_is_database_constrained(self):
        self._assert_integrity_error(**self._ocr_kwargs(status="NOT_A_REAL_STATUS"))

    def test_only_one_active_request_per_document(self):
        ProcessDocumentRequest.objects.create(**self._ocr_kwargs())

        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                failure_code="ENQUEUE_SEND_FAILED",
            )
        )

    def test_terminal_history_allows_new_active_request(self):
        ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.COMPLETED,
                completed_at=timezone.now(),
            )
        )

        active = ProcessDocumentRequest.objects.create(**self._ocr_kwargs())

        self.assertEqual(active.status, ProcessDocumentRequest.Status.QUEUED)

    def test_running_request_requires_complete_lease_shape(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.RUNNING,
                started_at=timezone.now(),
            )
        )

        now = timezone.now()
        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.RUNNING,
                lease_token=uuid.uuid4(),
                lease_expires_at=now + timedelta(minutes=45),
                started_at=now,
            )
        )

        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)

    def test_recovery_required_requires_retained_token_without_expiry(self):
        now = timezone.now()
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                lease_token=uuid.uuid4(),
                lease_expires_at=now + timedelta(minutes=45),
                started_at=now,
            )
        )

        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                lease_token=uuid.uuid4(),
                started_at=now,
            )
        )

        self.assertIsNone(request.lease_expires_at)

    def test_completed_requires_terminal_timestamp_and_no_failure(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.COMPLETED,
            )
        )
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.COMPLETED,
                completed_at=timezone.now(),
                failure_code="UNEXPECTED",
            )
        )

        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.COMPLETED,
                completed_at=timezone.now(),
            )
        )

        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)

    def test_partial_is_terminal_and_allows_optional_metadata(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.PARTIAL,
            )
        )

        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.PARTIAL,
                completed_at=timezone.now(),
            )
        )

        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.PARTIAL,
        )
        self.assertEqual(request.failure_code, "")
        self.assertEqual(request.failure_message, "")

        with_metadata = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.PARTIAL,
                completed_at=timezone.now(),
                failure_code="PROCESS_DOCUMENT_PARTIAL",
                failure_message="Expected output is incomplete.",
            )
        )
        self.assertEqual(
            with_metadata.failure_code,
            "PROCESS_DOCUMENT_PARTIAL",
        )
        self.assertEqual(
            with_metadata.failure_message,
            "Expected output is incomplete.",
        )

        active = ProcessDocumentRequest.objects.create(**self._ocr_kwargs())
        self.assertEqual(
            active.status,
            ProcessDocumentRequest.Status.QUEUED,
        )

    def test_failed_requires_terminal_timestamp_and_failure_code(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.FAILED,
                completed_at=timezone.now(),
            )
        )
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.FAILED,
                failure_code="PROCESSING_FAILED",
            )
        )

        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.FAILED,
                completed_at=timezone.now(),
                failure_code="PROCESSING_FAILED",
            )
        )

        self.assertEqual(request.failure_code, "PROCESSING_FAILED")

    def test_enqueue_failed_requires_failure_code_and_unclaimed_shape(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            )
        )
        self._assert_integrity_error(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                failure_code="ENQUEUE_SEND_FAILED",
                started_at=timezone.now(),
            )
        )

        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                failure_code="ENQUEUE_SEND_FAILED",
            )
        )

        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.started_at)

    def test_valid_hebrew_translation_payload(self):
        request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )

        self.assertEqual(
            request.operation,
            ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
        )

    def test_hebrew_translation_rejects_ocr_retry_payload(self):
        self._assert_integrity_error(
            document=self.document,
            status=ProcessDocumentRequest.Status.QUEUED,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
        )

    def test_recognition_only_requires_source_transkribus_run(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=(
                    ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                ),
            )
        )

        run = self._run(self.document)
        request = ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=(
                    ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                ),
                source_transkribus_run=run,
            )
        )

        self.assertEqual(request.source_transkribus_run_id, run.id)

    def test_normal_reenqueue_rejects_source_transkribus_run(self):
        run = self._run(self.document)

        self._assert_integrity_error(**self._ocr_kwargs(source_transkribus_run=run))

    def test_origin_must_match_operation(self):
        self._assert_integrity_error(
            **self._ocr_kwargs(
                origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            )
        )

    def test_source_transkribus_run_must_belong_to_document(self):
        other_run = self._run(self.other_document)

        with self.assertRaises(ValidationError):
            ProcessDocumentRequest.objects.create(
                **self._ocr_kwargs(
                    origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                    ocr_retry_mode=(
                        ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                    ),
                    source_transkribus_run=other_run,
                )
            )

    def test_referenced_source_transkribus_run_is_restricted(self):
        run = self._run(self.document)
        ProcessDocumentRequest.objects.create(
            **self._ocr_kwargs(
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=(
                    ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
                ),
                source_transkribus_run=run,
            )
        )

        with self.assertRaises(RestrictedError):
            run.delete()
