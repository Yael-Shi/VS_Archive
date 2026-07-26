"""Worker claim, lease fencing, and reconciliation for corrected/current sync.

Handles top-level SQS ``SYNC_TRANSKRIBUS_CORRECTED_CURRENT`` messages.

Delivery model: at-most-once provider orchestration per Request, with
idempotent terminal Request reconciliation (not exactly-once SQS delivery).
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any, Mapping

from django.db import transaction
from django.utils import timezone

from documents.models import (
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncRequest,
)
from documents.services.env_validation import WorkerEnvConfig
from documents.services.transkribus_corrected_current_sync import (
    CorrectedCurrentSyncError,
    CorrectedCurrentSyncFailureCode,
    CorrectedCurrentSyncFencedOutError,
    CorrectedCurrentSyncResult,
    run_corrected_current_transkribus_sync,
)

logger = logging.getLogger(__name__)

# Conservative v1 defaults for up to ~30-page documents; tune from production timings.
EXECUTION_LEASE = timedelta(minutes=45)
SQS_VISIBILITY_AFTER_CLAIM_SECONDS = 45 * 60
FRESH_IN_PROGRESS_DEFER_SECONDS = 2 * 60
STARTED_RECOVERY_REQUIRED = timedelta(minutes=60)

_TERMINAL_REQUEST_STATUSES = frozenset(
    {
        TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
        TranskribusCorrectedCurrentSyncRequest.Status.REFUSED,
        TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
    }
)
_TERMINAL_ATTEMPT_STATUSES = frozenset(
    {
        TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
        TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
        TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
    }
)

_REQUEST_CREDENTIALS_MISSING = "WORKER_CREDENTIALS_MISSING"
_REQUEST_UNEXPECTED = "WORKER_UNEXPECTED_FAILURE"

_PUBLIC_REQUEST_MESSAGES: dict[str, str] = {
    _REQUEST_CREDENTIALS_MISSING: (
        "Corrected/current sync worker is missing Transkribus credentials."
    ),
    _REQUEST_UNEXPECTED: ("Corrected/current sync worker failed unexpectedly."),
    CorrectedCurrentSyncFailureCode.RUN_RESOLUTION: (
        "Corrected/current sync could not resolve a trusted Transkribus run."
    ),
    CorrectedCurrentSyncFailureCode.UNEXPECTED: (
        "Corrected/current sync failed unexpectedly."
    ),
}


def _public_request_message(failure_code: str) -> str:
    return _PUBLIC_REQUEST_MESSAGES.get(
        failure_code,
        _PUBLIC_REQUEST_MESSAGES[_REQUEST_UNEXPECTED],
    )


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
        # Best-effort only: lease fencing remains the at-most-once control.
        logger.warning(
            "Corrected/current sync ChangeMessageVisibility failed "
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
    """Extend visibility briefly and do not ack (return False)."""
    _change_message_visibility(
        sqs,
        queue_url=queue_url,
        receipt_handle=receipt_handle,
        visibility_timeout=FRESH_IN_PROGRESS_DEFER_SECONDS,
    )
    return False


def _attempt_status_to_request_status(attempt_status: str) -> str:
    if attempt_status == TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED:
        return TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED
    if attempt_status == TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED:
        return TranskribusCorrectedCurrentSyncRequest.Status.REFUSED
    if attempt_status == TranskribusCorrectedCurrentSyncAttempt.Status.FAILED:
        return TranskribusCorrectedCurrentSyncRequest.Status.FAILED
    raise ValueError(f"Attempt status {attempt_status!r} is not terminal")


def _apply_terminal_from_attempt(
    sync_request: TranskribusCorrectedCurrentSyncRequest,
    attempt: TranskribusCorrectedCurrentSyncAttempt,
    *,
    now,
) -> None:
    """Mutate locked Request to match a terminal Attempt (idempotent)."""
    target = _attempt_status_to_request_status(attempt.status)
    if sync_request.status == target and sync_request.completed_at is not None:
        if (
            sync_request.lease_token is None
            and sync_request.lease_expires_at is None
            and (
                target != TranskribusCorrectedCurrentSyncRequest.Status.FAILED
                or bool(sync_request.failure_code)
            )
        ):
            return

    sync_request.status = target
    sync_request.completed_at = attempt.completed_at or now
    sync_request.lease_token = None
    sync_request.lease_expires_at = None
    if target == TranskribusCorrectedCurrentSyncRequest.Status.FAILED:
        sync_request.failure_code = (
            attempt.failure_code or ""
        ).strip() or CorrectedCurrentSyncFailureCode.UNEXPECTED
        sync_request.failure_message = (
            (attempt.failure_message or "").strip()
            or _public_request_message(sync_request.failure_code)
        )[:512]
    else:
        sync_request.failure_code = ""
        sync_request.failure_message = ""
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


def _mark_request_failed(
    sync_request: TranskribusCorrectedCurrentSyncRequest,
    *,
    failure_code: str,
    now,
) -> None:
    sync_request.status = TranskribusCorrectedCurrentSyncRequest.Status.FAILED
    sync_request.completed_at = now
    sync_request.lease_token = None
    sync_request.lease_expires_at = None
    sync_request.failure_code = failure_code
    sync_request.failure_message = _public_request_message(failure_code)[:512]
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


def _mark_recovery_required(
    sync_request: TranskribusCorrectedCurrentSyncRequest,
) -> None:
    """Linked STARTED past recovery threshold: no provider rerun; keep lease fence."""
    sync_request.status = (
        TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
    )
    sync_request.lease_expires_at = None
    sync_request.save(update_fields=["status", "lease_expires_at", "updated_at"])


def _claim_new_lease(
    sync_request: TranskribusCorrectedCurrentSyncRequest,
    *,
    now,
) -> uuid.UUID:
    token = uuid.uuid4()
    sync_request.status = TranskribusCorrectedCurrentSyncRequest.Status.RUNNING
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


def _lock_sync_request(request_id: int) -> TranskribusCorrectedCurrentSyncRequest:
    """Lock only the Request row (never join nullable Attempt under FOR UPDATE)."""
    return TranskribusCorrectedCurrentSyncRequest.objects.select_for_update().get(
        pk=request_id
    )


def _load_linked_attempt(
    sync_request: TranskribusCorrectedCurrentSyncRequest,
) -> TranskribusCorrectedCurrentSyncAttempt | None:
    """Load Attempt after the Request row is locked (separate query, no FOR UPDATE join)."""
    attempt_id = sync_request.attempt_id
    if attempt_id is None:
        return None
    try:
        return TranskribusCorrectedCurrentSyncAttempt.objects.get(pk=attempt_id)
    except TranskribusCorrectedCurrentSyncAttempt.DoesNotExist:
        return None


def _parse_sync_request_id(raw: Any) -> int | None:
    """Accept only a positive plain int Request id (reject bool/zero/negative/other)."""
    if type(raw) is not int or raw < 1:
        return None
    return raw


def _terminalize_request_with_lease(
    *,
    request_id: int,
    lease_token: uuid.UUID,
    attempt: TranskribusCorrectedCurrentSyncAttempt,
) -> bool:
    """Terminalize Request from its linked Attempt when the worker holds the lease.

    Allows ``RUNNING`` or ``RECOVERY_REQUIRED`` so a late legitimate worker can
    reconcile safely after recovery fencing. Reloads the linked Attempt under the
    Request lock and requires exact id/document correlation with the supplied
    Attempt. Returns True when Request is terminal afterward.
    """
    supplied_attempt_id = attempt.pk
    now = timezone.now()
    with transaction.atomic():
        try:
            sync_request = _lock_sync_request(request_id)
        except TranskribusCorrectedCurrentSyncRequest.DoesNotExist:
            return False

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            return True

        if sync_request.lease_token != lease_token:
            return False

        if sync_request.status not in (
            TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
        ):
            return False

        if sync_request.attempt_id is None:
            return False
        if sync_request.attempt_id != supplied_attempt_id:
            return False

        linked_attempt = _load_linked_attempt(sync_request)
        if linked_attempt is None:
            return False
        if linked_attempt.pk != supplied_attempt_id:
            return False
        if linked_attempt.document_id != sync_request.document_id:
            return False
        if linked_attempt.status not in _TERMINAL_ATTEMPT_STATUSES:
            return False

        _apply_terminal_from_attempt(sync_request, linked_attempt, now=now)
        return True


def _fail_request_with_lease(
    *,
    request_id: int,
    lease_token: uuid.UUID,
    failure_code: str,
) -> bool:
    """Fail Request under matching lease when no Attempt was linked."""
    now = timezone.now()
    with transaction.atomic():
        try:
            sync_request = _lock_sync_request(request_id)
        except TranskribusCorrectedCurrentSyncRequest.DoesNotExist:
            return False

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            return True

        if sync_request.lease_token != lease_token:
            return False

        if sync_request.attempt_id is not None:
            attempt = _load_linked_attempt(sync_request)
            if attempt is not None and attempt.status in _TERMINAL_ATTEMPT_STATUSES:
                _apply_terminal_from_attempt(sync_request, attempt, now=now)
                return True
            return False

        if sync_request.status != TranskribusCorrectedCurrentSyncRequest.Status.RUNNING:
            return False

        _mark_request_failed(sync_request, failure_code=failure_code, now=now)
        return True


def _claim_or_reconcile(
    *,
    request_id: int,
) -> tuple[str, uuid.UUID | None]:
    """Lock Request and decide claim / reconcile / defer / recovery.

    Returns ``(action, lease_token)`` where action is one of:
    ``ack``, ``defer``, ``execute``.
    """
    now = timezone.now()
    with transaction.atomic():
        try:
            # Lock Request only — never select_related(attempt) under FOR UPDATE
            # (PostgreSQL rejects FOR UPDATE on the nullable side of an outer join).
            sync_request = _lock_sync_request(request_id)
        except TranskribusCorrectedCurrentSyncRequest.DoesNotExist:
            logger.info(
                "Corrected/current sync request id=%s missing; ack poison message",
                request_id,
            )
            return ("ack", None)

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            return ("ack", None)

        if sync_request.attempt_id is not None:
            attempt = _load_linked_attempt(sync_request)
            if attempt is None:
                return ("ack", None)

            if attempt.status in _TERMINAL_ATTEMPT_STATUSES:
                # Attempt is source of truth, including over RECOVERY_REQUIRED.
                _apply_terminal_from_attempt(sync_request, attempt, now=now)
                return ("ack", None)

            if attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.STARTED:
                age = now - attempt.created_at
                if age < STARTED_RECOVERY_REQUIRED:
                    return ("defer", None)
                if (
                    sync_request.status
                    != TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
                ):
                    _mark_recovery_required(sync_request)
                return ("ack", None)

            logger.warning(
                "Corrected/current sync request id=%s linked attempt id=%s "
                "has unexpected status=%s; ack",
                request_id,
                attempt.pk,
                attempt.status,
            )
            return ("ack", None)

        # attempt_id is null — claim / reclaim / defer
        if sync_request.status in (
            TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
            TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
        ):
            token = _claim_new_lease(sync_request, now=now)
            return ("execute", token)

        if sync_request.status == TranskribusCorrectedCurrentSyncRequest.Status.RUNNING:
            lease_expires_at = sync_request.lease_expires_at
            if lease_expires_at is not None and lease_expires_at > now:
                return ("defer", None)
            token = _claim_new_lease(sync_request, now=now)
            return ("execute", token)

        if (
            sync_request.status
            == TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
        ):
            # Linked Attempt required by constraint; handled above when present.
            return ("ack", None)

        logger.warning(
            "Corrected/current sync request id=%s unexpected status=%s; ack",
            request_id,
            sync_request.status,
        )
        return ("ack", None)


def _worker_credentials(
    worker_env: WorkerEnvConfig,
) -> tuple[str, str, str] | None:
    username = (worker_env.transkribus_username or "").strip()
    password = (worker_env.transkribus_password or "").strip()
    bearer = (worker_env.transkribus_api_token or "").strip()
    if not username or not password or not bearer:
        return None
    return username, password, bearer


def handle_sync_transkribus_corrected_current(
    payload: Mapping[str, Any],
    *,
    sqs: Any,
    queue_url: str,
    receipt_handle: str,
    worker_env: WorkerEnvConfig,
    run_sync=run_corrected_current_transkribus_sync,
) -> bool:
    """Process one ``SYNC_TRANSKRIBUS_CORRECTED_CURRENT`` message.

    Returns True when the message should be deleted (ack).
    """
    raw_request_id = payload.get("request_id")
    request_id = _parse_sync_request_id(raw_request_id)
    if request_id is None:
        logger.error(
            "Invalid SYNC_TRANSKRIBUS_CORRECTED_CURRENT payload request_id_type=%s",
            type(raw_request_id).__name__,
        )
        return True

    action, lease_token = _claim_or_reconcile(request_id=request_id)

    if action == "ack":
        return True
    if action == "defer":
        return _defer_in_progress(
            sqs, queue_url=queue_url, receipt_handle=receipt_handle
        )

    assert action == "execute"
    assert lease_token is not None

    _change_message_visibility(
        sqs,
        queue_url=queue_url,
        receipt_handle=receipt_handle,
        visibility_timeout=SQS_VISIBILITY_AFTER_CLAIM_SECONDS,
    )

    creds = _worker_credentials(worker_env)
    if creds is None:
        logger.error(
            "Corrected/current sync request id=%s missing Transkribus credentials",
            request_id,
        )
        terminal = _fail_request_with_lease(
            request_id=request_id,
            lease_token=lease_token,
            failure_code=_REQUEST_CREDENTIALS_MISSING,
        )
        return terminal

    username, password, bearer_token = creds

    try:
        sync_request = TranskribusCorrectedCurrentSyncRequest.objects.select_related(
            "initiated_by", "document"
        ).get(pk=request_id)
    except TranskribusCorrectedCurrentSyncRequest.DoesNotExist:
        return True

    if sync_request.initiated_by_id is None:
        terminal = _fail_request_with_lease(
            request_id=request_id,
            lease_token=lease_token,
            failure_code=_REQUEST_UNEXPECTED,
        )
        return terminal

    try:
        result: CorrectedCurrentSyncResult = run_sync(
            document_id=sync_request.document_id,
            initiated_by=sync_request.initiated_by,
            username=username,
            password=password,
            bearer_token=bearer_token,
            sync_request_id=request_id,
            lease_token=lease_token,
        )
    except CorrectedCurrentSyncFencedOutError:
        logger.info(
            "Corrected/current sync request id=%s fenced out before provider I/O",
            request_id,
        )
        return _defer_in_progress(
            sqs, queue_url=queue_url, receipt_handle=receipt_handle
        )
    except CorrectedCurrentSyncError as exc:
        if exc.attempt_id is not None:
            try:
                attempt = TranskribusCorrectedCurrentSyncAttempt.objects.get(
                    pk=exc.attempt_id
                )
            except TranskribusCorrectedCurrentSyncAttempt.DoesNotExist:
                attempt = None
            if attempt is not None and attempt.status in _TERMINAL_ATTEMPT_STATUSES:
                return _terminalize_request_with_lease(
                    request_id=request_id,
                    lease_token=lease_token,
                    attempt=attempt,
                )
            # Linked STARTED left in place; do not ack away the only message.
            return _defer_in_progress(
                sqs, queue_url=queue_url, receipt_handle=receipt_handle
            )

        failure_code = exc.failure_code or CorrectedCurrentSyncFailureCode.UNEXPECTED
        return _fail_request_with_lease(
            request_id=request_id,
            lease_token=lease_token,
            failure_code=failure_code,
        )
    except Exception as exc:
        logger.error(
            "Corrected/current sync request id=%s unexpected worker exception "
            "exception_class=%s",
            request_id,
            type(exc).__name__,
        )
        # Best-effort: if an Attempt was linked and is terminal, reconcile; else defer.
        with transaction.atomic():
            try:
                sync_request = _lock_sync_request(request_id)
            except TranskribusCorrectedCurrentSyncRequest.DoesNotExist:
                return True
            attempt = _load_linked_attempt(sync_request)
            if (
                sync_request.lease_token == lease_token
                and attempt is not None
                and attempt.status in _TERMINAL_ATTEMPT_STATUSES
            ):
                _apply_terminal_from_attempt(sync_request, attempt, now=timezone.now())
                return True
        return _defer_in_progress(
            sqs, queue_url=queue_url, receipt_handle=receipt_handle
        )

    return _terminalize_request_with_lease(
        request_id=request_id,
        lease_token=lease_token,
        attempt=result.attempt,
    )


__all__ = [
    "EXECUTION_LEASE",
    "FRESH_IN_PROGRESS_DEFER_SECONDS",
    "SQS_VISIBILITY_AFTER_CLAIM_SECONDS",
    "STARTED_RECOVERY_REQUIRED",
    "handle_sync_transkribus_corrected_current",
]
