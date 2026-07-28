"""Create, coalesce, and enqueue durable PROCESS_DOCUMENT Requests.

Only the caller that creates a QUEUED Request, or safely retries a matching
ENQUEUE_FAILED Request, receives send right. SQS I/O always occurs after the
short Document-lock transaction commits.

Known limitation: a process crash after commit but before SendMessage can leave
a stranded QUEUED Request. Recovery tooling remains a later task.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest, TranskribusRun
from documents.services.sqs import (
    SqsConfigurationError,
    send_process_document_request_message,
)

EnqueueOutcome = Literal[
    "CREATED_AND_ENQUEUED",
    "REENQUEUED",
    "ALREADY_QUEUED",
    "ALREADY_RUNNING",
    "BLOCKED_RECOVERY_REQUIRED",
    "ACTIVE_REQUEST_CONFLICT",
    "ALREADY_TERMINAL",
    "ENQUEUE_FAILED",
    "ENQUEUE_OUTCOME_UNKNOWN",
]

_ACTIVE_STATUSES = (
    ProcessDocumentRequest.Status.QUEUED,
    ProcessDocumentRequest.Status.RUNNING,
    ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
)

_TERMINAL_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.COMPLETED,
        ProcessDocumentRequest.Status.PARTIAL,
        ProcessDocumentRequest.Status.FAILED,
    }
)

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
_SAFE_SEND_FAILED_MESSAGE = (
    "Could not send the document request to the processing queue."
)
_SAFE_OUTCOME_UNKNOWN_MESSAGE = (
    "Queue send outcome is unknown; the request may or may not have been accepted."
)

_EXPECTED_SEND_EXCEPTIONS = (ClientError, BotoCoreError, SqsConfigurationError)


class ProcessDocumentRequestEnqueueErrorCode:
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    INVALID_INITIATOR = "INVALID_INITIATOR"
    INVALID_REQUEST_PAYLOAD = "INVALID_REQUEST_PAYLOAD"
    SOURCE_RUN_NOT_FOUND = "SOURCE_RUN_NOT_FOUND"


class ProcessDocumentRequestEnqueueError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    outcome: EnqueueOutcome
    request: ProcessDocumentRequest
    created: bool
    message_sent: bool | None
    observed_status: str
    send_attempted: bool


@dataclass(frozen=True, slots=True)
class _RequestSpec:
    operation: str
    origin: str
    ocr_retry_mode: str
    source_transkribus_run_id: int | None


@dataclass(frozen=True, slots=True)
class _ResolveResult:
    request: ProcessDocumentRequest
    created: bool
    send_right: bool
    coalesce_outcome: EnqueueOutcome | None


def _invalid_payload(message: str) -> ProcessDocumentRequestEnqueueError:
    return ProcessDocumentRequestEnqueueError(
        ProcessDocumentRequestEnqueueErrorCode.INVALID_REQUEST_PAYLOAD,
        message,
    )


def _normalize_initiator(initiated_by: object | None) -> User | None:
    if initiated_by is None:
        return None
    if not isinstance(initiated_by, User) or initiated_by.pk is None:
        raise ProcessDocumentRequestEnqueueError(
            ProcessDocumentRequestEnqueueErrorCode.INVALID_INITIATOR,
            "initiated_by must be a persisted user or None.",
        )
    return initiated_by


def _validate_request_spec(
    *,
    operation: str,
    origin: str,
    ocr_retry_mode: str,
    source_transkribus_run_id: int | None,
) -> _RequestSpec:
    if operation == ProcessDocumentRequest.Operation.OCR:
        if origin not in (
            ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        ):
            raise _invalid_payload("OCR request origin is invalid.")

        if ocr_retry_mode not in (
            ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
        ):
            raise _invalid_payload("OCR retry mode is invalid.")

        if (
            origin == ProcessDocumentRequest.Origin.UPLOAD_FINALIZE
            and ocr_retry_mode != ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE
        ):
            raise _invalid_payload(
                "Upload-finalize requests require normal re-enqueue mode."
            )

        if (
            ocr_retry_mode
            == ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        ):
            if (
                type(source_transkribus_run_id) is not int
                or source_transkribus_run_id < 1
            ):
                raise _invalid_payload(
                    "Recognition-only requests require a positive source run id."
                )
        elif source_transkribus_run_id is not None:
            raise _invalid_payload(
                "Normal OCR requests cannot reference a source Transkribus run."
            )

    elif operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION:
        if origin != ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY:
            raise _invalid_payload("Hebrew translation request origin is invalid.")
        if ocr_retry_mode != "":
            raise _invalid_payload(
                "Hebrew translation requests cannot include an OCR retry mode."
            )
        if source_transkribus_run_id is not None:
            raise _invalid_payload(
                "Hebrew translation requests cannot reference a Transkribus run."
            )
    else:
        raise _invalid_payload("Process document operation is invalid.")

    return _RequestSpec(
        operation=operation,
        origin=origin,
        ocr_retry_mode=ocr_retry_mode,
        source_transkribus_run_id=source_transkribus_run_id,
    )


def _lock_active_request(document_id: int) -> ProcessDocumentRequest | None:
    return (
        ProcessDocumentRequest.objects.select_for_update()
        .filter(document_id=document_id, status__in=_ACTIVE_STATUSES)
        .order_by("pk")
        .first()
    )


def _payload_matches(
    request: ProcessDocumentRequest,
    spec: _RequestSpec,
) -> bool:
    return (
        request.operation == spec.operation
        and request.origin == spec.origin
        and request.ocr_retry_mode == spec.ocr_retry_mode
        and request.source_transkribus_run_id == spec.source_transkribus_run_id
    )


def _coalesce_outcome_for_status(status: str) -> EnqueueOutcome:
    if status == ProcessDocumentRequest.Status.QUEUED:
        return "ALREADY_QUEUED"
    if status == ProcessDocumentRequest.Status.RUNNING:
        return "ALREADY_RUNNING"
    if status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        return "BLOCKED_RECOVERY_REQUIRED"
    return "ALREADY_QUEUED"


def _validate_source_run(
    *,
    document: Document,
    spec: _RequestSpec,
) -> None:
    source_run_id = spec.source_transkribus_run_id
    if source_run_id is None:
        return
    if not TranskribusRun.objects.filter(
        pk=source_run_id,
        document_id=document.pk,
    ).exists():
        raise ProcessDocumentRequestEnqueueError(
            ProcessDocumentRequestEnqueueErrorCode.SOURCE_RUN_NOT_FOUND,
            "Source Transkribus run was not found for this document.",
        )


def _resolve_under_document_lock(
    *,
    document: Document,
    initiated_by: User | None,
    spec: _RequestSpec,
) -> _ResolveResult:
    active = _lock_active_request(document.pk)
    if active is None:
        try:
            with transaction.atomic():
                created_request = ProcessDocumentRequest.objects.create(
                    document=document,
                    initiated_by=initiated_by,
                    status=ProcessDocumentRequest.Status.QUEUED,
                    operation=spec.operation,
                    origin=spec.origin,
                    ocr_retry_mode=spec.ocr_retry_mode,
                    source_transkribus_run_id=(spec.source_transkribus_run_id),
                )
            return _ResolveResult(
                request=created_request,
                created=True,
                send_right=True,
                coalesce_outcome=None,
            )
        except IntegrityError:
            active = _lock_active_request(document.pk)
            if active is None:
                raise

    assert active is not None

    if not _payload_matches(active, spec):
        return _ResolveResult(
            request=active,
            created=False,
            send_right=False,
            coalesce_outcome="ACTIVE_REQUEST_CONFLICT",
        )

    if active.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
        active.status = ProcessDocumentRequest.Status.QUEUED
        active.failure_code = ""
        active.failure_message = ""
        update_fields = [
            "status",
            "failure_code",
            "failure_message",
            "updated_at",
        ]
        if initiated_by is not None:
            active.initiated_by = initiated_by
            update_fields.append("initiated_by")
        active.save(update_fields=update_fields)
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
    return ProcessDocumentRequest.objects.filter(
        pk=request_id,
        status=ProcessDocumentRequest.Status.QUEUED,
        lease_token__isnull=True,
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
        status=ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        failure_code=failure_code,
        failure_message=failure_message,
        updated_at=now,
    )


def _client_error_code(exc: ClientError) -> str:
    response = getattr(exc, "response", None) or {}
    error = response.get("Error") or {}
    return str(error.get("Code") or "")


def classify_process_document_sqs_send_failure(
    exc: BaseException,
) -> Literal["definite", "ambiguous"]:
    if isinstance(exc, ClientError):
        if _client_error_code(exc) in _DEFINITE_SQS_REJECT_CODES:
            return "definite"
        return "ambiguous"
    if isinstance(exc, SqsConfigurationError):
        return "definite"
    if isinstance(exc, BotoCoreError):
        return "ambiguous"
    raise TypeError(
        f"Expected a botocore or SQS configuration error, got {type(exc).__name__}."
    )


def _reload_request(request_id: int) -> ProcessDocumentRequest:
    return ProcessDocumentRequest.objects.select_related(
        "document",
        "initiated_by",
        "source_transkribus_run",
    ).get(pk=request_id)


def _outcome_from_observed(
    *,
    created: bool,
    send_accepted: bool,
    observed_status: str,
) -> EnqueueOutcome:
    if observed_status == ProcessDocumentRequest.Status.QUEUED:
        if send_accepted:
            return "CREATED_AND_ENQUEUED" if created else "REENQUEUED"
        return "ALREADY_QUEUED"
    if observed_status == ProcessDocumentRequest.Status.RUNNING:
        return "ALREADY_RUNNING"
    if observed_status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        return "BLOCKED_RECOVERY_REQUIRED"
    if observed_status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
        return "ENQUEUE_OUTCOME_UNKNOWN"
    if observed_status in _TERMINAL_STATUSES:
        return "ALREADY_TERMINAL"
    if send_accepted:
        return "CREATED_AND_ENQUEUED" if created else "REENQUEUED"
    return "ALREADY_QUEUED"


def enqueue_process_document_request(
    *,
    document_id: int,
    operation: str,
    origin: str,
    ocr_retry_mode: str = "",
    source_transkribus_run_id: int | None = None,
    initiated_by: object | None = None,
) -> EnqueueResult:
    """Create/coalesce a durable Request and send its id to SQS when authorized."""
    if connection.in_atomic_block:
        raise RuntimeError(
            "PROCESS_DOCUMENT Request enqueue must be called outside "
            "database transactions."
        )

    if type(document_id) is not int or document_id < 1:
        raise _invalid_payload("document_id must be a positive int.")

    initiator = _normalize_initiator(initiated_by)
    spec = _validate_request_spec(
        operation=operation,
        origin=origin,
        ocr_retry_mode=ocr_retry_mode,
        source_transkribus_run_id=source_transkribus_run_id,
    )

    with transaction.atomic():
        try:
            document = Document.objects.select_for_update().get(pk=document_id)
        except Document.DoesNotExist as exc:
            raise ProcessDocumentRequestEnqueueError(
                ProcessDocumentRequestEnqueueErrorCode.DOCUMENT_NOT_FOUND,
                "Document was not found.",
            ) from exc

        _validate_source_run(document=document, spec=spec)
        resolved = _resolve_under_document_lock(
            document=document,
            initiated_by=initiator,
            spec=spec,
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

    if connection.in_atomic_block:
        raise RuntimeError(
            "BUG: PROCESS_DOCUMENT Request enqueue attempted SendMessage "
            "inside a database atomic block."
        )

    created = resolved.created
    try:
        send_process_document_request_message(request.pk)
    except _EXPECTED_SEND_EXCEPTIONS as exc:
        kind = classify_process_document_sqs_send_failure(exc)
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
        if (
            updated == 1
            and request.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED
        ):
            outcome = cas_outcome
        else:
            outcome = _outcome_from_observed(
                created=created,
                send_accepted=False,
                observed_status=request.status,
            )
            if request.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
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
    return EnqueueResult(
        outcome=_outcome_from_observed(
            created=created,
            send_accepted=True,
            observed_status=request.status,
        ),
        request=request,
        created=created,
        message_sent=True,
        observed_status=request.status,
        send_attempted=True,
    )
