"""OCR-reprocess adapter for durable PROCESS_DOCUMENT enqueue."""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import User

from documents.models import Document, ProcessDocumentRequest
from documents.services.ocr_reprocess import (
    OcrReprocessAssessment,
    OcrReprocessError,
    OcrRetryMode,
    assess_ocr_reprocess,
)
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    enqueue_process_document_request,
)

_SAFE_QUEUE_FAILURE_MESSAGE = "לא ניתן היה לתזמן את העיבוד מחדש. אפשר לנסות שוב."
_SAFE_CONFLICT_MESSAGE = "בקשת עיבוד אחרת כבר פעילה עבור המסמך."
_SAFE_RECOVERY_MESSAGE = "העיבוד הקודם דורש שחזור לפני שאפשר לבקש עיבוד מחדש."
_SAFE_REQUEST_REJECTED_MESSAGE = "לא ניתן היה לבקש עיבוד מחדש למסמך."


class OcrReprocessEnqueueErrorCode:
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    ACTIVE_REQUEST_CONFLICT = "ACTIVE_REQUEST_CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REQUEST_REJECTED = "REQUEST_REJECTED"


class OcrReprocessEnqueueError(OcrReprocessError):
    def __init__(
        self,
        *,
        code: str,
        public_message: str,
        http_status: int,
        outcome: EnqueueOutcome | None = None,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.http_status = http_status
        self.outcome = outcome
        super().__init__(public_message)


@dataclass(frozen=True, slots=True)
class OcrReprocessApplyResult:
    assessment: OcrReprocessAssessment
    enqueue_result: EnqueueResult


def _assessment_from_matching_active_request(
    document_id: int,
) -> tuple[OcrReprocessAssessment, ProcessDocumentRequest] | None:
    """
    Reuse the canonical payload of an already-approved OCR reprocess.

    The first successful caller changes the Document to PROCESSING, so a
    sequential double-click can no longer pass the failed-state eligibility
    check. Reconstructing only a valid matching active request lets this
    adapter coalesce it, or lets the generic service retry ENQUEUE_FAILED,
    without weakening first-attempt eligibility.
    """
    request = (
        ProcessDocumentRequest.objects.filter(
            document_id=document_id,
            status__in=(
                ProcessDocumentRequest.Status.QUEUED,
                ProcessDocumentRequest.Status.RUNNING,
                ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            ),
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        .order_by("pk")
        .first()
    )
    if request is None:
        return None

    if (
        request.ocr_retry_mode == ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE
        and request.source_transkribus_run_id is None
    ):
        retry_mode = OcrRetryMode.NORMAL_REENQUEUE
    elif (
        request.ocr_retry_mode
        == ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        and request.source_transkribus_run_id is not None
    ):
        retry_mode = OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
    else:
        return None

    return (
        OcrReprocessAssessment(
            document_id=document_id,
            retry_mode=retry_mode,
            source_transkribus_run_id=request.source_transkribus_run_id,
        ),
        request,
    )


def _coalesced_active_result(request: ProcessDocumentRequest) -> EnqueueResult:
    if request.status == ProcessDocumentRequest.Status.QUEUED:
        outcome: EnqueueOutcome = "ALREADY_QUEUED"
    elif request.status == ProcessDocumentRequest.Status.RUNNING:
        outcome = "ALREADY_RUNNING"
    elif request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        outcome = "BLOCKED_RECOVERY_REQUIRED"
    else:
        raise AssertionError(
            "Expected a non-retryable active OCR-reprocess Request, got "
            f"status={request.status!r}."
        )
    return EnqueueResult(
        outcome=outcome,
        request=request,
        created=False,
        message_sent=False,
        observed_status=request.status,
        send_attempted=False,
    )


def _mark_processing_if_request_still_queued(
    *,
    document_id: int,
    request_id: int,
) -> None:
    """Reflect queued work without overwriting worker-owned state."""
    Document.objects.filter(
        pk=document_id,
        process_document_requests__pk=request_id,
        process_document_requests__status=ProcessDocumentRequest.Status.QUEUED,
    ).update(
        processing_state_user=Document.ProcessingState.PROCESSING,
        upload_error=None,
    )


def _mark_failed_if_request_still_enqueue_failed(
    *,
    document_id: int,
    request_id: int,
) -> None:
    """Surface a safe queue failure only while enqueue still owns the state."""
    Document.objects.filter(
        pk=document_id,
        process_document_requests__pk=request_id,
        process_document_requests__status=(
            ProcessDocumentRequest.Status.ENQUEUE_FAILED
        ),
    ).update(
        processing_state_user=Document.ProcessingState.FAILED,
        upload_error=_SAFE_QUEUE_FAILURE_MESSAGE,
    )


def apply_ocr_reprocess(
    document_id: int,
    *,
    collection_id: str,
    model_id: str,
    initiated_by: User | None = None,
) -> OcrReprocessApplyResult:
    """Assess and enqueue an intentional OCR reprocess through a durable Request."""
    enqueue_result: EnqueueResult | None = None
    try:
        assessment = assess_ocr_reprocess(
            document_id,
            collection_id=collection_id,
            model_id=model_id,
        )
    except OcrReprocessError:
        active = _assessment_from_matching_active_request(document_id)
        if active is None:
            raise
        assessment, active_request = active
        if active_request.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
            raise
        enqueue_result = _coalesced_active_result(active_request)

    if enqueue_result is None:
        try:
            enqueue_result = enqueue_process_document_request(
                document_id=document_id,
                operation=ProcessDocumentRequest.Operation.OCR,
                origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
                ocr_retry_mode=assessment.retry_mode.value,
                source_transkribus_run_id=assessment.source_transkribus_run_id,
                initiated_by=initiated_by,
            )
        except ProcessDocumentRequestEnqueueError as exc:
            raise OcrReprocessEnqueueError(
                code=OcrReprocessEnqueueErrorCode.REQUEST_REJECTED,
                public_message=_SAFE_REQUEST_REJECTED_MESSAGE,
                http_status=500,
            ) from exc

    if enqueue_result.outcome in {
        "CREATED_AND_ENQUEUED",
        "REENQUEUED",
        "ALREADY_QUEUED",
    }:
        _mark_processing_if_request_still_queued(
            document_id=document_id,
            request_id=enqueue_result.request.pk,
        )
        return OcrReprocessApplyResult(assessment, enqueue_result)

    if enqueue_result.outcome in {
        "ALREADY_RUNNING",
        "ALREADY_TERMINAL",
    }:
        return OcrReprocessApplyResult(assessment, enqueue_result)

    if enqueue_result.outcome in {
        "ENQUEUE_FAILED",
        "ENQUEUE_OUTCOME_UNKNOWN",
    }:
        _mark_failed_if_request_still_enqueue_failed(
            document_id=document_id,
            request_id=enqueue_result.request.pk,
        )
        raise OcrReprocessEnqueueError(
            code=OcrReprocessEnqueueErrorCode.QUEUE_UNAVAILABLE,
            public_message=_SAFE_QUEUE_FAILURE_MESSAGE,
            http_status=500,
            outcome=enqueue_result.outcome,
        )

    if enqueue_result.outcome == "ACTIVE_REQUEST_CONFLICT":
        raise OcrReprocessEnqueueError(
            code=OcrReprocessEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
            public_message=_SAFE_CONFLICT_MESSAGE,
            http_status=409,
            outcome=enqueue_result.outcome,
        )

    if enqueue_result.outcome == "BLOCKED_RECOVERY_REQUIRED":
        raise OcrReprocessEnqueueError(
            code=OcrReprocessEnqueueErrorCode.RECOVERY_REQUIRED,
            public_message=_SAFE_RECOVERY_MESSAGE,
            http_status=409,
            outcome=enqueue_result.outcome,
        )

    raise AssertionError(
        "Unhandled OCR-reprocess PROCESS_DOCUMENT enqueue outcome: "
        f"{enqueue_result.outcome}"
    )
