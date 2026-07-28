"""Claim, lease fencing, and terminalization for durable PROCESS_DOCUMENT work."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Callable, Mapping

from django.db import transaction
from django.utils import timezone

from documents.models import ProcessDocumentRequest
from documents.services.hebrew_translation_retry import (
    PROCESS_DOCUMENT_OPERATION_KEY,
    RETRY_HEBREW_TRANSLATION_OPERATION,
)
from documents.services.ocr_reprocess import (
    OCR_RETRY_MODE_PAYLOAD_KEY,
    SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY,
)
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
    ProcessDocumentOutcome,
)

logger = logging.getLogger(__name__)

PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY = "request_id"
EXECUTION_LEASE = timedelta(minutes=45)
SQS_VISIBILITY_AFTER_CLAIM_SECONDS = 45 * 60
FRESH_IN_PROGRESS_DEFER_SECONDS = 2 * 60

_REQUEST_NOOP = "PROCESS_DOCUMENT_NOOP"
_REQUEST_FAILED = "PROCESS_DOCUMENT_FAILED"

_TERMINAL_REQUEST_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.COMPLETED,
        ProcessDocumentRequest.Status.PARTIAL,
        ProcessDocumentRequest.Status.FAILED,
    }
)


class ProcessDocumentRequestAction(StrEnum):
    ACK = "ack"
    DEFER = "defer"
    EXECUTE = "execute"


@dataclass(frozen=True)
class ProcessDocumentRequestClaim:
    action: ProcessDocumentRequestAction
    request_id: int
    lease_token: uuid.UUID | None = None
    execution_payload: dict[str, Any] | None = None


def parse_process_document_request_id(raw: Any) -> int | None:
    """Accept only a positive plain int Request id."""
    if type(raw) is not int or raw < 1:
        return None
    return raw


def _change_message_visibility(
    sqs: Any,
    *,
    queue_url: str,
    receipt_handle: str,
    visibility_timeout: int,
) -> bool:
    try:
        sqs.change_message_visibility(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=visibility_timeout,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Process document ChangeMessageVisibility failed "
            "visibility_timeout=%s exception_class=%s",
            visibility_timeout,
            type(exc).__name__,
        )
        return False


def _defer_in_progress(
    sqs: Any,
    *,
    queue_url: str,
    receipt_handle: str,
) -> bool:
    _change_message_visibility(
        sqs,
        queue_url=queue_url,
        receipt_handle=receipt_handle,
        visibility_timeout=FRESH_IN_PROGRESS_DEFER_SECONDS,
    )
    return False


def _execution_payload(sync_request: ProcessDocumentRequest) -> dict[str, Any]:
    """Build the legacy execution contract from the durable Request row."""
    payload: dict[str, Any] = {
        "type": "PROCESS_DOCUMENT",
        "document_id": sync_request.document_id,
    }

    if sync_request.operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION:
        payload[PROCESS_DOCUMENT_OPERATION_KEY] = RETRY_HEBREW_TRANSLATION_OPERATION
        return payload

    if sync_request.operation != ProcessDocumentRequest.Operation.OCR:
        raise ValueError(
            f"Unsupported ProcessDocumentRequest operation={sync_request.operation!r}"
        )

    if (
        sync_request.ocr_retry_mode
        == ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
    ):
        payload[OCR_RETRY_MODE_PAYLOAD_KEY] = sync_request.ocr_retry_mode
        payload[SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY] = (
            sync_request.source_transkribus_run_id
        )
    return payload


def _lock_request(request_id: int) -> ProcessDocumentRequest:
    return ProcessDocumentRequest.objects.select_for_update().get(pk=request_id)


def _claim_new_lease(
    sync_request: ProcessDocumentRequest,
    *,
    now,
) -> uuid.UUID:
    token = uuid.uuid4()
    sync_request.status = ProcessDocumentRequest.Status.RUNNING
    sync_request.lease_token = token
    sync_request.lease_expires_at = now + EXECUTION_LEASE
    if sync_request.started_at is None:
        sync_request.started_at = now
    sync_request.failure_code = ""
    sync_request.failure_message = ""
    sync_request.save(
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
    return token


def _mark_recovery_required(sync_request: ProcessDocumentRequest) -> None:
    """Fence stale work without authorizing another provider execution."""
    sync_request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
    sync_request.lease_expires_at = None
    sync_request.save(update_fields=["status", "lease_expires_at", "updated_at"])


def claim_process_document_request(
    *,
    request_id: int,
) -> ProcessDocumentRequestClaim:
    """Claim queued work, defer a fresh holder, or fence an expired holder."""
    now = timezone.now()
    with transaction.atomic():
        try:
            sync_request = _lock_request(request_id)
        except ProcessDocumentRequest.DoesNotExist:
            logger.info(
                "Process document request id=%s missing; ack poison message",
                request_id,
            )
            return ProcessDocumentRequestClaim(
                ProcessDocumentRequestAction.ACK,
                request_id,
            )

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            return ProcessDocumentRequestClaim(
                ProcessDocumentRequestAction.ACK,
                request_id,
            )

        if sync_request.status in (
            ProcessDocumentRequest.Status.QUEUED,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        ):
            execution_payload = _execution_payload(sync_request)
            token = _claim_new_lease(sync_request, now=now)
            return ProcessDocumentRequestClaim(
                ProcessDocumentRequestAction.EXECUTE,
                request_id,
                lease_token=token,
                execution_payload=execution_payload,
            )

        if sync_request.status == ProcessDocumentRequest.Status.RUNNING:
            lease_expires_at = sync_request.lease_expires_at
            if lease_expires_at is not None and lease_expires_at > now:
                return ProcessDocumentRequestClaim(
                    ProcessDocumentRequestAction.DEFER,
                    request_id,
                )

            # There is no generic provider Attempt to reconcile or safely replay.
            # Retain the old token so the original late holder may still finish.
            _mark_recovery_required(sync_request)
            return ProcessDocumentRequestClaim(
                ProcessDocumentRequestAction.ACK,
                request_id,
            )

        if sync_request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
            return ProcessDocumentRequestClaim(
                ProcessDocumentRequestAction.ACK,
                request_id,
            )

        logger.warning(
            "Process document request id=%s has unexpected status=%s; ack",
            request_id,
            sync_request.status,
        )
        return ProcessDocumentRequestClaim(
            ProcessDocumentRequestAction.ACK,
            request_id,
        )


def _terminal_status_and_failure(
    outcome: ProcessDocumentOutcome,
) -> tuple[str, str, str] | None:
    if outcome.disposition == ProcessDocumentDisposition.COMPLETED:
        return ProcessDocumentRequest.Status.COMPLETED, "", ""

    if outcome.disposition == ProcessDocumentDisposition.PARTIAL:
        return (
            ProcessDocumentRequest.Status.PARTIAL,
            outcome.failure_code.strip()[:64],
            outcome.failure_message.strip()[:512],
        )

    if outcome.disposition == ProcessDocumentDisposition.FAILED:
        return (
            ProcessDocumentRequest.Status.FAILED,
            outcome.failure_code.strip()[:64] or _REQUEST_FAILED,
            outcome.failure_message.strip()[:512],
        )

    if outcome.disposition == ProcessDocumentDisposition.NOOP:
        return (
            ProcessDocumentRequest.Status.FAILED,
            outcome.failure_code.strip()[:64] or _REQUEST_NOOP,
            outcome.failure_message.strip()[:512],
        )

    return None


def terminalize_process_document_request(
    *,
    request_id: int,
    lease_token: uuid.UUID,
    outcome: ProcessDocumentOutcome,
) -> bool:
    """Persist a terminal outcome only for the current or retained lease holder."""
    terminal = _terminal_status_and_failure(outcome)
    if terminal is None:
        return False

    target_status, failure_code, failure_message = terminal
    now = timezone.now()
    with transaction.atomic():
        try:
            sync_request = _lock_request(request_id)
        except ProcessDocumentRequest.DoesNotExist:
            return False

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            return True

        if sync_request.lease_token != lease_token:
            return False

        if sync_request.status not in (
            ProcessDocumentRequest.Status.RUNNING,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        ):
            return False

        sync_request.status = target_status
        sync_request.completed_at = now
        sync_request.lease_token = None
        sync_request.lease_expires_at = None
        sync_request.failure_code = failure_code
        sync_request.failure_message = failure_message
        sync_request.save(
            update_fields=[
                "status",
                "completed_at",
                "lease_token",
                "lease_expires_at",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )
        return True


def handle_process_document_request(
    payload: Mapping[str, Any],
    *,
    sqs: Any,
    queue_url: str,
    receipt_handle: str,
    execute_payload: Callable[[dict[str, Any]], ProcessDocumentOutcome],
) -> bool:
    """Handle one request-aware PROCESS_DOCUMENT message."""

    raw_request_id = payload.get(PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY)
    request_id = parse_process_document_request_id(raw_request_id)
    if request_id is None:
        logger.error(
            "Invalid PROCESS_DOCUMENT request_id_type=%s",
            type(raw_request_id).__name__,
        )
        return True

    claim = claim_process_document_request(request_id=request_id)

    if claim.action == ProcessDocumentRequestAction.ACK:
        return True

    if claim.action == ProcessDocumentRequestAction.DEFER:
        return _defer_in_progress(
            sqs,
            queue_url=queue_url,
            receipt_handle=receipt_handle,
        )

    assert claim.action == ProcessDocumentRequestAction.EXECUTE
    assert claim.lease_token is not None
    assert claim.execution_payload is not None

    _change_message_visibility(
        sqs,
        queue_url=queue_url,
        receipt_handle=receipt_handle,
        visibility_timeout=SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
    )

    try:
        outcome = execute_payload(claim.execution_payload)
    except Exception as exc:
        logger.error(
            "Process document request id=%s execution raised exception_class=%s",
            request_id,
            type(exc).__name__,
        )
        return _defer_in_progress(
            sqs,
            queue_url=queue_url,
            receipt_handle=receipt_handle,
        )

    if outcome.disposition == ProcessDocumentDisposition.DEFERRED:
        return _defer_in_progress(
            sqs,
            queue_url=queue_url,
            receipt_handle=receipt_handle,
        )

    if outcome.disposition == ProcessDocumentDisposition.RETRYABLE:
        # Keep the message and current lease. On expiry another delivery fences
        # the request to RECOVERY_REQUIRED instead of repeating provider work.
        return False

    try:
        terminal = terminalize_process_document_request(
            request_id=request_id,
            lease_token=claim.lease_token,
            outcome=outcome,
        )
    except Exception as exc:
        logger.error(
            "Process document request id=%s terminalization raised exception_class=%s",
            request_id,
            type(exc).__name__,
        )
        return _defer_in_progress(
            sqs,
            queue_url=queue_url,
            receipt_handle=receipt_handle,
        )

    if terminal:
        return True

    return _defer_in_progress(
        sqs,
        queue_url=queue_url,
        receipt_handle=receipt_handle,
    )


__all__ = [
    "EXECUTION_LEASE",
    "FRESH_IN_PROGRESS_DEFER_SECONDS",
    "PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY",
    "SQS_VISIBILITY_AFTER_CLAIM_SECONDS",
    "ProcessDocumentRequestAction",
    "ProcessDocumentRequestClaim",
    "claim_process_document_request",
    "handle_process_document_request",
    "parse_process_document_request_id",
    "terminalize_process_document_request",
]
