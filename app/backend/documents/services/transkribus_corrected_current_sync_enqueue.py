"""Enqueue staff corrected/current Transkribus sync Requests onto the worker queue.

Document-lock send-right (PR3):
- Only the caller that creates a new QUEUED Request, or atomically transitions
  ENQUEUE_FAILED → QUEUED, may SendMessage.
- Existing QUEUED / RUNNING / RECOVERY_REQUIRED never resend.
- SQS I/O is always outside DB transactions (no on_commit outbox).
- Post-send finalization is CAS-only and must not regress worker-owned or
  terminal Request state.

Known limitation: crash after commit but before SendMessage can leave a stranded
QUEUED Request; repair is deferred to a later recovery/requeue command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from documents.models import Document, TranskribusCorrectedCurrentSyncRequest
from documents.services.sqs import (
    SqsConfigurationError,
    send_sync_transkribus_corrected_current_message,
)

EnqueueOutcome = Literal[
    "CREATED_AND_ENQUEUED",
    "REENQUEUED",
    "ALREADY_QUEUED",
    "ALREADY_RUNNING",
    "BLOCKED_RECOVERY_REQUIRED",
    "ALREADY_TERMINAL",
    "ENQUEUE_FAILED",
    "ENQUEUE_OUTCOME_UNKNOWN",
]

_ACTIVE_STATUSES = (
    TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
    TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
    TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED,
    TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
)

_TERMINAL_STATUSES = frozenset(
    {
        TranskribusCorrectedCurrentSyncRequest.Status.COMPLETED,
        TranskribusCorrectedCurrentSyncRequest.Status.REFUSED,
        TranskribusCorrectedCurrentSyncRequest.Status.FAILED,
    }
)

# ClientError codes where SQS did not accept the message (definite).
_DEFINITE_SQS_REJECT_CODES = frozenset(
    {
        "AccessDenied",
        "AccessDeniedException",
        "InvalidAddress",
        "InvalidParameterValue",
        "InvalidSecurity",
        "QueueDoesNotExist",
        "AWS.SimpleQueueService.NonExistentQueue",
        "AWS.SimpleQueueService.QueueDeletedRecently",
    }
)

_FAILURE_CODE_SEND_FAILED = "ENQUEUE_SEND_FAILED"
_FAILURE_CODE_OUTCOME_UNKNOWN = "ENQUEUE_OUTCOME_UNKNOWN"
_SAFE_SEND_FAILED_MESSAGE = "Could not send the sync request to the processing queue."
_SAFE_OUTCOME_UNKNOWN_MESSAGE = (
    "Sync queue send outcome is unknown; the request may or may not have been accepted."
)

# Expected send-path failures only (botocore + SQS configuration).
_EXPECTED_SEND_EXCEPTIONS = (ClientError, BotoCoreError, SqsConfigurationError)


class CorrectedCurrentSyncEnqueueErrorCode:
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    INITIATOR_REQUIRED = "INITIATOR_REQUIRED"


class CorrectedCurrentSyncEnqueueError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    outcome: EnqueueOutcome
    request: TranskribusCorrectedCurrentSyncRequest
    created: bool
    message_sent: bool | None
    observed_status: str
    send_attempted: bool


@dataclass(frozen=True, slots=True)
class _ResolveResult:
    request: TranskribusCorrectedCurrentSyncRequest
    created: bool
    send_right: bool
    coalesce_outcome: EnqueueOutcome | None


def _require_initiator(initiated_by: object | None) -> User:
    if initiated_by is None:
        raise CorrectedCurrentSyncEnqueueError(
            CorrectedCurrentSyncEnqueueErrorCode.INITIATOR_REQUIRED,
            "Enqueue requires a persisted initiating user.",
        )
    if not isinstance(initiated_by, User):
        raise CorrectedCurrentSyncEnqueueError(
            CorrectedCurrentSyncEnqueueErrorCode.INITIATOR_REQUIRED,
            "Enqueue requires a persisted initiating user.",
        )
    if getattr(initiated_by, "pk", None) is None:
        raise CorrectedCurrentSyncEnqueueError(
            CorrectedCurrentSyncEnqueueErrorCode.INITIATOR_REQUIRED,
            "Enqueue requires a persisted initiating user.",
        )
    return initiated_by


def _lock_active_request(
    document_id: int,
) -> TranskribusCorrectedCurrentSyncRequest | None:
    return (
        TranskribusCorrectedCurrentSyncRequest.objects.select_for_update()
        .filter(document_id=document_id, status__in=_ACTIVE_STATUSES)
        .order_by("pk")
        .first()
    )


def _coalesce_outcome_for_status(status: str) -> EnqueueOutcome:
    if status == TranskribusCorrectedCurrentSyncRequest.Status.QUEUED:
        return "ALREADY_QUEUED"
    if status == TranskribusCorrectedCurrentSyncRequest.Status.RUNNING:
        return "ALREADY_RUNNING"
    if status == TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED:
        return "BLOCKED_RECOVERY_REQUIRED"
    if status in _TERMINAL_STATUSES:
        return "ALREADY_TERMINAL"
    # ENQUEUE_FAILED should have taken the send-right path under the Document lock.
    return "ALREADY_QUEUED"


def _outcome_from_observed(
    *,
    created: bool,
    send_accepted: bool,
    observed_status: str,
) -> EnqueueOutcome:
    """Map post-call observed Request status to the caller-facing outcome."""
    if observed_status == TranskribusCorrectedCurrentSyncRequest.Status.QUEUED:
        if send_accepted:
            return "CREATED_AND_ENQUEUED" if created else "REENQUEUED"
        return "ALREADY_QUEUED"
    if observed_status == TranskribusCorrectedCurrentSyncRequest.Status.RUNNING:
        return "ALREADY_RUNNING"
    if (
        observed_status
        == TranskribusCorrectedCurrentSyncRequest.Status.RECOVERY_REQUIRED
    ):
        return "BLOCKED_RECOVERY_REQUIRED"
    if observed_status == TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED:
        return "ENQUEUE_OUTCOME_UNKNOWN"
    if observed_status in _TERMINAL_STATUSES:
        return "ALREADY_TERMINAL"
    if send_accepted:
        return "CREATED_AND_ENQUEUED" if created else "REENQUEUED"
    return "ALREADY_QUEUED"


def _resolve_under_document_lock(
    *,
    document: Document,
    initiated_by: User,
) -> _ResolveResult:
    active = _lock_active_request(document.pk)
    if active is None:
        try:
            with transaction.atomic():
                created_req = TranskribusCorrectedCurrentSyncRequest.objects.create(
                    document=document,
                    initiated_by=initiated_by,
                    status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
                )
            return _ResolveResult(
                request=created_req,
                created=True,
                send_right=True,
                coalesce_outcome=None,
            )
        except IntegrityError:
            active = _lock_active_request(document.pk)
            if active is None:
                raise

    assert active is not None
    if active.status == TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED:
        active.status = TranskribusCorrectedCurrentSyncRequest.Status.QUEUED
        active.failure_code = ""
        active.failure_message = ""
        active.initiated_by = initiated_by
        active.save(
            update_fields=[
                "status",
                "failure_code",
                "failure_message",
                "initiated_by",
                "updated_at",
            ]
        )
        return _ResolveResult(
            request=active,
            created=False,
            send_right=True,
            coalesce_outcome=None,
        )

    return _ResolveResult(
        request=active,
        created=False,
        send_right=False,
        coalesce_outcome=_coalesce_outcome_for_status(active.status),
    )


def _unclaimed_queued_cas_filter(*, request_id: int):
    return TranskribusCorrectedCurrentSyncRequest.objects.filter(
        pk=request_id,
        status=TranskribusCorrectedCurrentSyncRequest.Status.QUEUED,
        lease_token__isnull=True,
        attempt__isnull=True,
    )


def _finalize_success(*, request_id: int) -> int:
    now = timezone.now()
    return _unclaimed_queued_cas_filter(request_id=request_id).update(
        last_enqueued_at=now,
        updated_at=now,
    )


def _finalize_failure(
    *,
    request_id: int,
    failure_code: str,
    failure_message: str,
) -> int:
    now = timezone.now()
    return _unclaimed_queued_cas_filter(request_id=request_id).update(
        status=TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED,
        failure_code=failure_code,
        failure_message=failure_message,
        updated_at=now,
    )


def _client_error_code(exc: ClientError) -> str:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    code = error.get("Code") or ""
    return str(code)


def classify_sqs_send_failure(exc: BaseException) -> Literal["definite", "ambiguous"]:
    """Return definite only when evidence shows the message was not accepted.

    ``SqsConfigurationError`` (e.g. missing ``SQS_QUEUE_URL``) is definite.
    Other ``ClientError`` codes and ``BotoCoreError`` are ambiguous unless the
    code is in the known reject set. Ordinary ``RuntimeError`` is not handled.
    """
    if isinstance(exc, ClientError):
        if _client_error_code(exc) in _DEFINITE_SQS_REJECT_CODES:
            return "definite"
        return "ambiguous"
    if isinstance(exc, SqsConfigurationError):
        return "definite"
    if isinstance(exc, BotoCoreError):
        return "ambiguous"
    raise TypeError(
        f"classify_sqs_send_failure expected botocore/configuration error, "
        f"got {type(exc).__name__}"
    )


def _reload_request(request_id: int) -> TranskribusCorrectedCurrentSyncRequest:
    return TranskribusCorrectedCurrentSyncRequest.objects.select_related(
        "document", "initiated_by"
    ).get(pk=request_id)


def enqueue_transkribus_corrected_current_sync(
    *,
    document_id: int,
    initiated_by: object | None,
) -> EnqueueResult:
    """Create/coalesce the active Request and optionally enqueue it to SQS.

    Authz/CSRF/feature-gate belong at the future staff POST boundary. This service
    only requires a persisted Document and initiating user.
    """
    initiator = _require_initiator(initiated_by)

    with transaction.atomic():
        try:
            document = Document.objects.select_for_update().get(pk=document_id)
        except Document.DoesNotExist as exc:
            raise CorrectedCurrentSyncEnqueueError(
                CorrectedCurrentSyncEnqueueErrorCode.DOCUMENT_NOT_FOUND,
                "Document was not found.",
            ) from exc

        resolved = _resolve_under_document_lock(
            document=document,
            initiated_by=initiator,
        )

    request = resolved.request
    if not resolved.send_right:
        assert resolved.coalesce_outcome is not None
        request = _reload_request(request.pk)
        return EnqueueResult(
            outcome=resolved.coalesce_outcome,
            request=request,
            created=False,
            message_sent=False,
            observed_status=request.status,
            send_attempted=False,
        )

    # SendMessage must not run inside a DB transaction / atomic block.
    if connection.in_atomic_block:
        raise RuntimeError(
            "BUG: corrected/current sync enqueue attempted SendMessage inside a "
            "database atomic block."
        )

    created = resolved.created
    try:
        send_sync_transkribus_corrected_current_message(request.pk)
    except _EXPECTED_SEND_EXCEPTIONS as exc:
        kind = classify_sqs_send_failure(exc)
        if kind == "definite":
            failure_code = _FAILURE_CODE_SEND_FAILED
            failure_message = _SAFE_SEND_FAILED_MESSAGE
            message_sent: bool | None = False
            cas_outcome: EnqueueOutcome = "ENQUEUE_FAILED"
        else:
            failure_code = _FAILURE_CODE_OUTCOME_UNKNOWN
            failure_message = _SAFE_OUTCOME_UNKNOWN_MESSAGE
            message_sent = None
            cas_outcome = "ENQUEUE_OUTCOME_UNKNOWN"

        updated = _finalize_failure(
            request_id=request.pk,
            failure_code=failure_code,
            failure_message=failure_message,
        )
        request = _reload_request(request.pk)
        if updated == 1 and request.status == (
            TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED
        ):
            outcome = cas_outcome
        else:
            # Worker claimed (or other lifecycle move): never invent a failure write.
            outcome = _outcome_from_observed(
                created=created,
                send_accepted=False,
                observed_status=request.status,
            )
            if (
                request.status
                == TranskribusCorrectedCurrentSyncRequest.Status.ENQUEUE_FAILED
            ):
                outcome = (
                    "ENQUEUE_FAILED"
                    if request.failure_code == _FAILURE_CODE_SEND_FAILED
                    else "ENQUEUE_OUTCOME_UNKNOWN"
                )
        return EnqueueResult(
            outcome=outcome,
            request=request,
            created=created,
            message_sent=message_sent,
            observed_status=request.status,
            send_attempted=True,
        )

    _finalize_success(request_id=request.pk)
    request = _reload_request(request.pk)
    outcome = _outcome_from_observed(
        created=created,
        send_accepted=True,
        observed_status=request.status,
    )
    return EnqueueResult(
        outcome=outcome,
        request=request,
        created=created,
        message_sent=True,
        observed_status=request.status,
        send_attempted=True,
    )
