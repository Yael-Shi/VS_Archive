from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrPageCheckpoint,
    Document,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
    ProcessDocumentRequest,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedPageClaimAction,
    ArabicPrintedPageSource,
    StaleArabicPrintedPageClaimError,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
)
from documents.services.archive_items import create_ocr_document
from documents.services.cloud_vision_document_text import ArabicPrintedWorkingImage
from documents.services.gemini_page_checkpoints import (
    GeminiPageClaimAction,
    StaleGeminiPageClaimError,
    claim_gemini_page,
)
from documents.services.htr_adapters.antigravity_adapter import AntigravityAdapter
from documents.services.htr_adapters.base import (
    EnginePageCheckpointBusyError,
    EnginePermanentError,
)
from documents.services.process_document_request_persist import (
    ProcessDocumentExecutionIdentity,
)
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
    abandon_process_document_request,
)
from documents.services.process_document_request_staff_retry import (
    ProcessDocumentRequestStaffRetryError,
    ProcessDocumentRequestStaffRetryErrorCode,
    retry_process_document_request,
)
from documents.test_antigravity_ocr import (
    _BANDED_DEADLINE,
    _jpeg_page,
    _make_banded_worker_env,
)
from documents.test_arabic_printed_page_checkpoints import _identity, _pages
from documents.test_process_document_request_staff_retry import _ocr_doc, _rr_request


def _running_request(document: Document) -> ProcessDocumentRequest:
    now = timezone.now()
    token = uuid.uuid4()
    return ProcessDocumentRequest.objects.create(
        document=document,
        status=ProcessDocumentRequest.Status.RUNNING,
        operation=ProcessDocumentRequest.Operation.OCR,
        origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        lease_token=token,
        lease_expires_at=now + timedelta(minutes=45),
        started_at=now,
    )


def _identity_for(request: ProcessDocumentRequest) -> ProcessDocumentExecutionIdentity:
    assert request.lease_token is not None
    return ProcessDocumentExecutionIdentity.request_aware(
        request.pk,
        request.lease_token,
    )


class ProcessDocumentPageClaimExecutionFenceTests(TransactionTestCase):
    def _gemini_attempt(self, document: Document) -> GeminiOcrAttempt:
        return GeminiOcrAttempt.objects.create(
            document=document,
            identity_fingerprint="1" * 64,
            source_fingerprint="2" * 64,
            route_fingerprint="3" * 64,
            prompt_fingerprint="4" * 64,
            config_fingerprint="5" * 64,
            prompt_contract_version="gemini-ocr-prompt-v1",
            model_candidates=["gemini-2.5-flash"],
            expected_page_count=2,
            status=GeminiOcrAttempt.Status.IN_PROGRESS,
        )

    def _claim_gemini(
        self,
        attempt: GeminiOcrAttempt,
        page_index: int,
        identity: ProcessDocumentExecutionIdentity | None,
    ):
        digest = str(page_index) * 64
        return claim_gemini_page(
            attempt_id=attempt.id,
            page_index=page_index,
            page_fingerprint=digest,
            source_content_fingerprint=digest,
            execution_identity=identity,
        )

    def test_old_identity_can_claim_while_request_is_running(self):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        request = _running_request(document)
        attempt = self._gemini_attempt(document)
        claim = self._claim_gemini(attempt, 1, _identity_for(request))
        self.assertEqual(claim.action, GeminiPageClaimAction.EXECUTE)
        self.assertIsNotNone(claim.lease_token)

    def test_abandoned_identity_cannot_acquire_new_gemini_lease(self):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        request = _running_request(document)
        attempt = self._gemini_attempt(document)
        first = self._claim_gemini(attempt, 1, _identity_for(request))
        page = GeminiOcrPageCheckpoint.objects.get(pk=first.checkpoint_id)
        lease_token = page.lease_token
        lease_expires_at = page.lease_expires_at
        old_identity = _identity_for(request)

        request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        request.lease_expires_at = None
        request.save(update_fields=["status", "lease_expires_at", "updated_at"])
        document.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
        document.save(update_fields=["processing_state_user", "updated_at"])
        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        busy = self._claim_gemini(attempt, 1, old_identity)
        self.assertEqual(busy.action, GeminiPageClaimAction.BUSY)

        with self.assertRaises(StaleGeminiPageClaimError):
            self._claim_gemini(attempt, 2, old_identity)

        page.refresh_from_db()
        self.assertEqual(page.lease_token, lease_token)
        self.assertEqual(page.lease_expires_at, lease_expires_at)
        self.assertEqual(page.status, GeminiOcrPageCheckpoint.Status.RUNNING)

    def test_new_identity_can_acquire_gemini_lease_after_abandon(self):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        old_request = _running_request(document)
        attempt = self._gemini_attempt(document)
        self._claim_gemini(attempt, 1, _identity_for(old_request))
        old_request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        old_request.lease_expires_at = None
        old_request.save(update_fields=["status", "lease_expires_at", "updated_at"])
        document.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
        document.save(update_fields=["processing_state_user", "updated_at"])
        abandon_process_document_request(
            request_id=old_request.pk,
            document_id=document.pk,
        )
        new_request = _running_request(document)
        claim = self._claim_gemini(attempt, 2, _identity_for(new_request))
        self.assertEqual(claim.action, GeminiPageClaimAction.EXECUTE)

    def test_malformed_identity_cannot_acquire_gemini_lease(self):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        _running_request(document)
        attempt = self._gemini_attempt(document)
        with self.assertRaises(StaleGeminiPageClaimError):
            self._claim_gemini(
                attempt,
                1,
                ProcessDocumentExecutionIdentity.invalid(),
            )

    def test_abandoned_identity_cannot_acquire_new_arabic_lease(self):
        document = create_ocr_document(
            title="Arabic claim fence",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="arabic-claim-fence.pdf",
            mime_type="application/pdf",
        )
        request = _running_request(document)
        pages = _pages(b"one", b"two")
        identity = _identity(pages)
        attempt = get_or_create_arabic_printed_attempt(
            document_id=document.id,
            identity=identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=attempt.id,
            identity=identity,
        )
        first = claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=0,
            page_fingerprint=identity.page_fingerprints[0],
            source_content_fingerprint=identity.source_content_fingerprints[0],
            oriented_image_sha256=identity.oriented_image_sha256s[0],
            execution_identity=_identity_for(request),
        )
        self.assertEqual(first.action, ArabicPrintedPageClaimAction.EXECUTE)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=first.checkpoint_id)
        lease_token = page.lease_token
        lease_expires_at = page.lease_expires_at
        old_identity = _identity_for(request)

        request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        request.lease_expires_at = None
        request.save(update_fields=["status", "lease_expires_at", "updated_at"])
        document.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
        document.save(update_fields=["processing_state_user", "updated_at"])
        abandon_process_document_request(
            request_id=request.pk,
            document_id=document.pk,
        )

        with self.assertRaises(StaleArabicPrintedPageClaimError):
            claim_arabic_printed_page(
                attempt_id=attempt.id,
                page_index=1,
                page_fingerprint=identity.page_fingerprints[1],
                source_content_fingerprint=identity.source_content_fingerprints[1],
                oriented_image_sha256=identity.oriented_image_sha256s[1],
                execution_identity=old_identity,
            )
        page.refresh_from_db()
        self.assertEqual(page.lease_token, lease_token)
        self.assertEqual(page.lease_expires_at, lease_expires_at)

        new_request = _running_request(document)
        second = claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=1,
            page_fingerprint=identity.page_fingerprints[1],
            source_content_fingerprint=identity.source_content_fingerprints[1],
            oriented_image_sha256=identity.oriented_image_sha256s[1],
            execution_identity=_identity_for(new_request),
        )
        self.assertEqual(second.action, ArabicPrintedPageClaimAction.EXECUTE)

    def test_pr3_live_lease_guard_still_blocks_retry(self):
        document = _ocr_doc()
        request = _rr_request(document)
        attempt = self._gemini_attempt(document)
        self._claim_gemini(attempt, 1, _identity_for(request))
        staff = User.objects.create_user(
            username="claim_fence_staff",
            password="test-pass",
            is_staff=True,
        )
        with self.assertRaises(ProcessDocumentRequestStaffRetryError) as ctx:
            retry_process_document_request(
                request_id=request.pk,
                document_id=document.pk,
                initiated_by=staff,
                collection_id="",
                model_id="",
            )
        self.assertEqual(
            ctx.exception.code,
            ProcessDocumentRequestStaffRetryErrorCode.LIVE_PAGE_LEASE,
        )
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )


def _adapter_working_image(label: bytes) -> ArabicPrintedWorkingImage:
    digest = hashlib.sha256(label + b"-oriented").hexdigest()
    return ArabicPrintedWorkingImage(
        width=40,
        height=60,
        jpeg_bytes=label,
        mime_type="image/jpeg",
        sha256=digest,
        byte_length=len(label),
        rgb_pixels=label,
    )


def _arabic_adapter_document(*, suffix: str) -> Document:
    return create_ocr_document(
        title=f"Arabic adapter claim fence {suffix}",
        doc_type=Document.DocType.PDF,
        language=Document.Language.ARABIC,
        text_input_type=Document.TextInputType.PRINTED,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.PROCESSING,
        file_s3_key=f"arabic-adapter-claim-fence-{suffix}.pdf",
        mime_type="application/pdf",
    )


def _park_and_abandon(document: Document, request: ProcessDocumentRequest) -> None:
    request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
    request.lease_expires_at = None
    request.save(update_fields=["status", "lease_expires_at", "updated_at"])
    document.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
    document.save(update_fields=["processing_state_user", "updated_at"])
    abandon_process_document_request(
        request_id=request.pk,
        document_id=document.pk,
    )


class AntigravityAdapterStalePageClaimMappingTests(TestCase):
    def _execute_adapter(
        self,
        *,
        document: Document,
        pages,
        identity: ProcessDocumentExecutionIdentity,
    ):
        return AntigravityAdapter().execute(
            pages=pages,
            language_hint=Document.Language.ARABIC,
            prompt_variant="printed",
            worker_env=_make_banded_worker_env(),
            document_id=document.id,
            absolute_deadline_monotonic=_BANDED_DEADLINE,
            execution_identity=identity,
            text_input_type=Document.TextInputType.PRINTED,
        )

    @patch(
        "documents.services.arabic_printed_banded_document_ocr."
        "process_claimed_arabic_printed_page",
        side_effect=AssertionError("provider page processing must not run"),
    )
    @patch(
        "documents.services.htr_adapters.antigravity_adapter."
        "prepare_arabic_printed_working_image",
    )
    def test_stale_execution_identity_is_permanent_before_new_lease(
        self,
        mock_prepare,
        mock_process_claimed,
    ):
        document = _arabic_adapter_document(suffix="stale")
        request = _running_request(document)
        old_identity = _identity_for(request)
        _park_and_abandon(document, request)
        page = _jpeg_page(1, label=b"adapter-stale")
        mock_prepare.return_value = _adapter_working_image(b"adapter-stale")

        with self.assertRaises(EnginePermanentError) as raised:
            self._execute_adapter(
                document=document,
                pages=[page],
                identity=old_identity,
            )

        self.assertIn(
            "stale process-document execution cannot claim a page",
            str(raised.exception),
        )
        self.assertIsInstance(
            raised.exception.__cause__,
            StaleArabicPrintedPageClaimError,
        )
        self.assertNotIsInstance(raised.exception, EnginePageCheckpointBusyError)
        mock_process_claimed.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertIsNone(request.lease_token)
        pages = list(
            ArabicPrintedOcrPageCheckpoint.objects.filter(attempt__document=document)
        )
        self.assertEqual(len(pages), 1)
        checkpoint = pages[0]
        self.assertNotEqual(
            checkpoint.status,
            ArabicPrintedOcrPageCheckpoint.Status.RUNNING,
        )
        self.assertIsNone(checkpoint.lease_token)
        self.assertIsNone(checkpoint.lease_expires_at)

    @patch(
        "documents.services.arabic_printed_banded_document_ocr."
        "process_claimed_arabic_printed_page",
        side_effect=AssertionError("provider page processing must not run"),
    )
    @patch(
        "documents.services.htr_adapters.antigravity_adapter."
        "prepare_arabic_printed_working_image",
    )
    def test_busy_live_page_stays_retryable_not_stale_identity(
        self,
        mock_prepare,
        mock_process_claimed,
    ):
        document = _arabic_adapter_document(suffix="busy")
        request = _running_request(document)
        page = _jpeg_page(1, label=b"adapter-busy")
        working = _adapter_working_image(b"adapter-busy")
        mock_prepare.return_value = working
        page_source = ArabicPrintedPageSource(
            page_index=0,
            mime_type=working.mime_type,
            source_identity=page.source_identity,
            source_content_fingerprint=page.source_content_fingerprint,
            oriented_image_sha256=working.sha256,
            oriented_image_width=working.width,
            oriented_image_height=working.height,
        )
        attempt_identity = _identity([page_source])
        attempt = get_or_create_arabic_printed_attempt(
            document_id=document.id,
            identity=attempt_identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=attempt.id,
            identity=attempt_identity,
        )
        first = claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=0,
            page_fingerprint=attempt_identity.page_fingerprints[0],
            source_content_fingerprint=attempt_identity.source_content_fingerprints[0],
            oriented_image_sha256=attempt_identity.oriented_image_sha256s[0],
            execution_identity=_identity_for(request),
        )
        self.assertEqual(first.action, ArabicPrintedPageClaimAction.EXECUTE)
        live = ArabicPrintedOcrPageCheckpoint.objects.get(pk=first.checkpoint_id)
        lease_token = live.lease_token
        lease_expires_at = live.lease_expires_at

        with self.assertRaises(EnginePageCheckpointBusyError) as raised:
            self._execute_adapter(
                document=document,
                pages=[page],
                identity=_identity_for(request),
            )

        self.assertEqual(raised.exception.page_index, 0)
        self.assertNotIsInstance(raised.exception, EnginePermanentError)
        mock_process_claimed.assert_not_called()
        live.refresh_from_db()
        self.assertEqual(live.status, ArabicPrintedOcrPageCheckpoint.Status.RUNNING)
        self.assertEqual(live.lease_token, lease_token)
        self.assertEqual(live.lease_expires_at, lease_expires_at)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertIsNotNone(request.lease_token)
