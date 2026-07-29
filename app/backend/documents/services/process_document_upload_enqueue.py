"""Upload-specific adapter for durable PROCESS_DOCUMENT enqueue."""

from __future__ import annotations

from django.contrib.auth.models import User

from documents.models import Document, ProcessDocumentRequest
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    enqueue_process_document_request,
)

_SAFE_QUEUE_FAILURE_MESSAGE = (
    "Document processing could not be queued. Please try again."
)
_SAFE_CONFLICT_MESSAGE = (
    "Another processing request is already active for this document."
)
_SAFE_RECOVERY_MESSAGE = (
    "Document processing requires recovery before it can be queued again."
)
_SAFE_REQUEST_REJECTED_MESSAGE = "Document processing could not be requested."


class UploadProcessEnqueueErrorCode:
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    ACTIVE_REQUEST_CONFLICT = "ACTIVE_REQUEST_CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REQUEST_REJECTED = "REQUEST_REJECTED"


class UploadProcessEnqueueError(RuntimeError):
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


def _mark_processing_if_request_still_queued(
    *,
    document_id: int,
    request_id: int,
) -> None:
    """
    Restore PROCESSING after a successful ENQUEUE_FAILED retry.

    Restrict the update to QUEUED. If the worker has already claimed or
    terminalized the Request, its Document state remains authoritative.
    """
    Document.objects.filter(
        pk=document_id,
        process_document_requests__pk=request_id,
        process_document_requests__status=(ProcessDocumentRequest.Status.QUEUED),
    ).update(
        processing_state_user=Document.ProcessingState.PROCESSING,
        upload_error=None,
    )


def _mark_failed_if_request_still_enqueue_failed(
    *,
    document_id: int,
    request_id: int,
) -> None:
    """
    Surface a queue failure without overwriting worker-owned state.

    An ambiguous send may already have reached the worker. If the Request is
    no longer ENQUEUE_FAILED, the worker's state wins.
    """
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


def enqueue_uploaded_document_processing(
    *,
    document_id: int,
    initiated_by: User | None,
) -> EnqueueResult:
    """Create, retry, or coalesce the one-time upload-finalize OCR Request."""
    try:
        result = enqueue_process_document_request(
            document_id=document_id,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=(ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE),
            source_transkribus_run_id=None,
            initiated_by=initiated_by,
        )
    except ProcessDocumentRequestEnqueueError as exc:
        raise UploadProcessEnqueueError(
            code=UploadProcessEnqueueErrorCode.REQUEST_REJECTED,
            public_message=_SAFE_REQUEST_REJECTED_MESSAGE,
            http_status=500,
        ) from exc

    if result.outcome in {
        "CREATED_AND_ENQUEUED",
        "REENQUEUED",
        "ALREADY_QUEUED",
    }:
        _mark_processing_if_request_still_queued(
            document_id=document_id,
            request_id=result.request.pk,
        )
        return result

    if result.outcome in {
        "ALREADY_RUNNING",
        "ALREADY_TERMINAL",
    }:
        return result

    if result.outcome in {
        "ENQUEUE_FAILED",
        "ENQUEUE_OUTCOME_UNKNOWN",
    }:
        _mark_failed_if_request_still_enqueue_failed(
            document_id=document_id,
            request_id=result.request.pk,
        )
        raise UploadProcessEnqueueError(
            code=UploadProcessEnqueueErrorCode.QUEUE_UNAVAILABLE,
            public_message=_SAFE_QUEUE_FAILURE_MESSAGE,
            http_status=500,
            outcome=result.outcome,
        )

    if result.outcome == "ACTIVE_REQUEST_CONFLICT":
        raise UploadProcessEnqueueError(
            code=UploadProcessEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
            public_message=_SAFE_CONFLICT_MESSAGE,
            http_status=409,
            outcome=result.outcome,
        )

    if result.outcome == "BLOCKED_RECOVERY_REQUIRED":
        raise UploadProcessEnqueueError(
            code=UploadProcessEnqueueErrorCode.RECOVERY_REQUIRED,
            public_message=_SAFE_RECOVERY_MESSAGE,
            http_status=409,
            outcome=result.outcome,
        )

    raise AssertionError(
        f"Unhandled upload PROCESS_DOCUMENT enqueue outcome: {result.outcome}"
    )
