"""Staff intentional retry for parked PROCESS_DOCUMENT Requests.

Closes a RECOVERY_REQUIRED Request via the existing abandon service, then
enqueues a new Request through the existing OCR-reprocess or Hebrew-translation
retry adapters. Does not replay the parked Request, token, or payload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from django.contrib.auth.models import User
from django.db import connection
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrPageCheckpoint,
    GeminiOcrPageCheckpoint,
    ProcessDocumentRequest,
)
from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.hebrew_translation_retry import HebrewTranslationRetryError
from documents.services.ocr_reprocess import OcrReprocessError
from documents.services.process_document_hebrew_translation_retry_enqueue import (
    HebrewTranslationRetryEnqueueError,
    enqueue_hebrew_translation_retry,
)
from documents.services.process_document_ocr_reprocess_enqueue import (
    OcrReprocessEnqueueError,
    apply_ocr_reprocess,
)
from documents.services.process_document_request_enqueue import EnqueueResult
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
    ProcessDocumentRequestStaffAbandonError,
    StaffAbandonOutcome,
    abandon_process_document_request,
    get_recovery_required_process_document_request,
)

logger = logging.getLogger(__name__)

_TERMINAL_REQUEST_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.COMPLETED,
        ProcessDocumentRequest.Status.PARTIAL,
        ProcessDocumentRequest.Status.FAILED,
    }
)

LIVE_PAGE_LEASE_PUBLIC_MESSAGE = (
    "לא ניתן להתחיל עיבוד מחדש כעת. פעילות עיבוד עדיין מוגנת על ידי "
    "חכירת עמוד פעילה. נסו שוב מאוחר יותר."
)
REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE = (
    "בקשת העיבוד כבר הסתיימה ואינה מאשרת התחלה מחדש."
)


class StaffRetryOutcome(StrEnum):
    ENQUEUED = "ENQUEUED"


class ProcessDocumentRequestStaffRetryErrorCode:
    INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
    INVALID_DOCUMENT_ID = "INVALID_DOCUMENT_ID"
    INVALID_INITIATOR = "INVALID_INITIATOR"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    DOCUMENT_MISMATCH = "DOCUMENT_MISMATCH"
    STATUS_NOT_RETRYABLE = "STATUS_NOT_RETRYABLE"
    INVALID_RECOVERY_SHAPE = "INVALID_RECOVERY_SHAPE"
    LIVE_PAGE_LEASE = "LIVE_PAGE_LEASE"
    REQUEST_ALREADY_FINISHED = "REQUEST_ALREADY_FINISHED"
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    CONFIG_ERROR = "CONFIG_ERROR"


class ProcessDocumentRequestStaffRetryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StaffRetryResult:
    outcome: StaffRetryOutcome
    source_request: ProcessDocumentRequest
    enqueue_result: EnqueueResult
    abandoned_now: bool
    operation: str


def _validate_ids(*, request_id: int, document_id: int) -> None:
    if type(request_id) is not int or request_id < 1:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.INVALID_REQUEST_ID,
            "request_id must be a positive int.",
        )
    if type(document_id) is not int or document_id < 1:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.INVALID_DOCUMENT_ID,
            "document_id must be a positive int.",
        )


def _validate_initiator(initiated_by: object) -> User:
    if not isinstance(initiated_by, User) or initiated_by.pk is None:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.INVALID_INITIATOR,
            "initiated_by must be a persisted user.",
        )
    return initiated_by


def _is_staff_abandoned(sync_request: ProcessDocumentRequest) -> bool:
    return (
        sync_request.status == ProcessDocumentRequest.Status.FAILED
        and sync_request.failure_code == STAFF_ABANDONED_FAILURE_CODE
    )


def document_has_live_ocr_page_checkpoint_lease(
    document_id: int,
    *,
    now=None,
) -> bool:
    """True when a Gemini or Arabic printed page lease is still live.

    Read-only. Matches claim busy semantics: status RUNNING and
    lease_expires_at in the future. Does not inspect or mutate lease tokens.
    """
    if type(document_id) is not int or document_id < 1:
        return False
    if now is None:
        now = timezone.now()
    gemini_live = GeminiOcrPageCheckpoint.objects.filter(
        attempt__document_id=document_id,
        status=GeminiOcrPageCheckpoint.Status.RUNNING,
        lease_expires_at__isnull=False,
        lease_expires_at__gt=now,
    ).exists()
    if gemini_live:
        return True
    return ArabicPrintedOcrPageCheckpoint.objects.filter(
        attempt__document_id=document_id,
        status=ArabicPrintedOcrPageCheckpoint.Status.RUNNING,
        lease_expires_at__isnull=False,
        lease_expires_at__gt=now,
    ).exists()


def get_staff_retry_source_process_document_request(
    *,
    document_id: int,
) -> ProcessDocumentRequest | None:
    """Return the Request the staff retry control should bind to, or None.

    Prefer the unique parked RECOVERY_REQUIRED Request. Otherwise the latest
    STAFF_ABANDONED Request is eligible only when later history is a single
    matching ENQUEUE_FAILED row (abandon succeeded, enqueue failed) or there
    is no later Request. Zero matches, more than one parked RR, and any other
    later history fail closed.
    """
    parked = get_recovery_required_process_document_request(document_id=document_id)
    if parked is not None:
        return parked
    if type(document_id) is not int or document_id < 1:
        return None

    abandoned = list(
        ProcessDocumentRequest.objects.filter(
            document_id=document_id,
            status=ProcessDocumentRequest.Status.FAILED,
            failure_code=STAFF_ABANDONED_FAILURE_CODE,
        ).order_by("-pk")[:1]
    )
    if len(abandoned) != 1:
        return None
    source = abandoned[0]
    later = list(
        ProcessDocumentRequest.objects.filter(
            document_id=document_id,
            pk__gt=source.pk,
        ).order_by("pk")
    )
    if not later:
        return source
    if len(later) != 1:
        return None
    stranded = later[0]
    if stranded.status != ProcessDocumentRequest.Status.ENQUEUE_FAILED:
        return None
    if stranded.operation != source.operation:
        return None
    return source


def _load_source_request(
    *,
    request_id: int,
    document_id: int,
) -> ProcessDocumentRequest:
    try:
        sync_request = ProcessDocumentRequest.objects.get(pk=request_id)
    except ProcessDocumentRequest.DoesNotExist as exc:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.REQUEST_NOT_FOUND,
            "ProcessDocumentRequest was not found.",
        ) from exc
    if sync_request.document_id != document_id:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.DOCUMENT_MISMATCH,
            "ProcessDocumentRequest does not belong to this document.",
        )
    return sync_request


def _map_abandon_error(
    exc: ProcessDocumentRequestStaffAbandonError,
) -> ProcessDocumentRequestStaffRetryError:
    if exc.code == "INVALID_REQUEST_ID":
        code = ProcessDocumentRequestStaffRetryErrorCode.INVALID_REQUEST_ID
    elif exc.code == "INVALID_DOCUMENT_ID":
        code = ProcessDocumentRequestStaffRetryErrorCode.INVALID_DOCUMENT_ID
    elif exc.code in {"REQUEST_NOT_FOUND", "DOCUMENT_MISMATCH"}:
        code = (
            ProcessDocumentRequestStaffRetryErrorCode.REQUEST_NOT_FOUND
            if exc.code == "REQUEST_NOT_FOUND"
            else ProcessDocumentRequestStaffRetryErrorCode.DOCUMENT_MISMATCH
        )
    elif exc.code == "STATUS_NOT_ABANDONABLE":
        code = ProcessDocumentRequestStaffRetryErrorCode.STATUS_NOT_RETRYABLE
    elif exc.code == "INVALID_RECOVERY_SHAPE":
        code = ProcessDocumentRequestStaffRetryErrorCode.INVALID_RECOVERY_SHAPE
    else:
        code = ProcessDocumentRequestStaffRetryErrorCode.STATUS_NOT_RETRYABLE
    return ProcessDocumentRequestStaffRetryError(code, exc.message)


def _refuse_live_page_lease(*, document_id: int, request_id: int) -> None:
    if not document_has_live_ocr_page_checkpoint_lease(document_id):
        return
    logger.info(
        "event=staff_retry_process_document_request outcome=live_page_lease "
        "document_id=%s request_id=%s",
        document_id,
        request_id,
    )
    raise ProcessDocumentRequestStaffRetryError(
        ProcessDocumentRequestStaffRetryErrorCode.LIVE_PAGE_LEASE,
        LIVE_PAGE_LEASE_PUBLIC_MESSAGE,
    )


def _enqueue_for_operation(
    *,
    operation: str,
    document_id: int,
    initiated_by: User,
    collection_id: str | None,
    model_id: str | None,
) -> EnqueueResult:
    if operation == ProcessDocumentRequest.Operation.OCR:
        ocr_collection_id = collection_id
        ocr_model_id = model_id
        if ocr_collection_id is None or ocr_model_id is None:
            try:
                worker_env = validate_required_env()
            except EnvConfigError as exc:
                raise ProcessDocumentRequestStaffRetryError(
                    ProcessDocumentRequestStaffRetryErrorCode.CONFIG_ERROR,
                    f"שגיאת תצורה: {exc}",
                ) from exc
            ocr_collection_id = worker_env.transkribus_collection_id or ""
            ocr_model_id = worker_env.transkribus_model_id or ""
        try:
            return apply_ocr_reprocess(
                document_id,
                collection_id=ocr_collection_id,
                model_id=ocr_model_id,
                initiated_by=initiated_by,
            ).enqueue_result
        except OcrReprocessEnqueueError as exc:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.QUEUE_UNAVAILABLE
                if exc.code == "QUEUE_UNAVAILABLE"
                else ProcessDocumentRequestStaffRetryErrorCode.REQUEST_REJECTED,
                exc.public_message,
            ) from exc
        except OcrReprocessError as exc:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.REQUEST_REJECTED,
                str(exc),
            ) from exc

    if operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION:
        try:
            return enqueue_hebrew_translation_retry(
                document_id,
                initiated_by=initiated_by,
            )
        except HebrewTranslationRetryEnqueueError as exc:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.QUEUE_UNAVAILABLE
                if exc.code == "QUEUE_UNAVAILABLE"
                else ProcessDocumentRequestStaffRetryErrorCode.REQUEST_REJECTED,
                exc.public_message,
            ) from exc
        except HebrewTranslationRetryError as exc:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.REQUEST_REJECTED,
                "לא ניתן לשלוח תרגום לעברית לעיבוד כעת.",
            ) from exc

    raise ProcessDocumentRequestStaffRetryError(
        ProcessDocumentRequestStaffRetryErrorCode.STATUS_NOT_RETRYABLE,
        "ProcessDocumentRequest operation is not retryable.",
    )


def retry_process_document_request(
    *,
    request_id: int,
    document_id: int,
    initiated_by: User,
    collection_id: str | None = None,
    model_id: str | None = None,
) -> StaffRetryResult:
    """Abandon a parked Request if needed, then enqueue a new OCR or translation job.

    Must not run inside a database transaction: enqueue SendMessage is
    post-commit. Abandon commits before enqueue starts.
    """
    if connection.in_atomic_block:
        raise RuntimeError(
            "PROCESS_DOCUMENT staff retry must run outside database transactions."
        )

    _validate_ids(request_id=request_id, document_id=document_id)
    initiator = _validate_initiator(initiated_by)
    source_request = _load_source_request(
        request_id=request_id,
        document_id=document_id,
    )
    operation = source_request.operation
    abandoned_now = False

    needs_ocr_lease_guard = operation == ProcessDocumentRequest.Operation.OCR and (
        source_request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        or _is_staff_abandoned(source_request)
    )
    if needs_ocr_lease_guard:
        _refuse_live_page_lease(document_id=document_id, request_id=request_id)

    if source_request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        try:
            abandon_result = abandon_process_document_request(
                request_id=request_id,
                document_id=document_id,
            )
        except ProcessDocumentRequestStaffAbandonError as exc:
            raise _map_abandon_error(exc) from exc

        source_request = abandon_result.request
        if abandon_result.outcome == StaffAbandonOutcome.ABANDONED:
            abandoned_now = True
        elif abandon_result.outcome == StaffAbandonOutcome.ALREADY_TERMINAL:
            if not _is_staff_abandoned(source_request):
                raise ProcessDocumentRequestStaffRetryError(
                    ProcessDocumentRequestStaffRetryErrorCode.REQUEST_ALREADY_FINISHED,
                    REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE,
                )
        else:
            raise AssertionError(
                f"Unhandled staff abandon outcome: {abandon_result.outcome}"
            )
        if source_request.status != ProcessDocumentRequest.Status.FAILED:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.REQUEST_ALREADY_FINISHED,
                REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE,
            )
        if source_request.failure_code != STAFF_ABANDONED_FAILURE_CODE:
            raise ProcessDocumentRequestStaffRetryError(
                ProcessDocumentRequestStaffRetryErrorCode.REQUEST_ALREADY_FINISHED,
                REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE,
            )
    elif _is_staff_abandoned(source_request):
        pass
    elif source_request.status in _TERMINAL_REQUEST_STATUSES:
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.REQUEST_ALREADY_FINISHED,
            REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE,
        )
    else:
        try:
            abandon_process_document_request(
                request_id=request_id,
                document_id=document_id,
            )
        except ProcessDocumentRequestStaffAbandonError as exc:
            raise _map_abandon_error(exc) from exc
        raise ProcessDocumentRequestStaffRetryError(
            ProcessDocumentRequestStaffRetryErrorCode.STATUS_NOT_RETRYABLE,
            "ProcessDocumentRequest is not retryable.",
        )

    enqueue_result = _enqueue_for_operation(
        operation=operation,
        document_id=document_id,
        initiated_by=initiator,
        collection_id=collection_id,
        model_id=model_id,
    )

    logger.info(
        "event=staff_retry_process_document_request outcome=%s "
        "document_id=%s source_request_id=%s new_request_id=%s "
        "operation=%s abandoned_now=%s enqueue_outcome=%s",
        StaffRetryOutcome.ENQUEUED,
        document_id,
        source_request.pk,
        enqueue_result.request.pk,
        operation,
        abandoned_now,
        enqueue_result.outcome,
    )
    return StaffRetryResult(
        outcome=StaffRetryOutcome.ENQUEUED,
        source_request=source_request,
        enqueue_result=enqueue_result,
        abandoned_now=abandoned_now,
        operation=operation,
    )


__all__ = [
    "LIVE_PAGE_LEASE_PUBLIC_MESSAGE",
    "REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE",
    "ProcessDocumentRequestStaffRetryError",
    "ProcessDocumentRequestStaffRetryErrorCode",
    "StaffRetryOutcome",
    "StaffRetryResult",
    "document_has_live_ocr_page_checkpoint_lease",
    "get_staff_retry_source_process_document_request",
    "retry_process_document_request",
]
