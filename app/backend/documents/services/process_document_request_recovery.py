"""Recovery and requeue fencing for durable PROCESS_DOCUMENT Requests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.db import connection, transaction
from django.db.models import Q, QuerySet
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
)
from documents.services.hebrew_translation_retry import (
    HebrewTranslationRetryError,
    validate_document_for_hebrew_translation_retry,
)
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    assess_ocr_reprocess,
)
from documents.services.process_document_request_enqueue import (
    EnqueueResult,
    send_reserved_process_document_request,
)

DEFAULT_RECOVERY_MINIMUM_AGE = timedelta(minutes=15)

RecoveryReason = Literal[
    "STRANDED_QUEUED",
    "ENQUEUE_FAILED",
    "TOO_RECENT",
    "QUEUED_ALREADY_ENQUEUED",
    "STATUS_NOT_RECOVERABLE",
    "INTENT_NO_LONGER_VALID",
    "REQUEST_PAYLOAD_NO_LONGER_MATCHES",
]

_OCR_REPROCESS_QUEUE_FAILURE_MESSAGE = (
    "לא ניתן היה לתזמן את העיבוד מחדש. אפשר לנסות שוב."
)
_UPLOAD_QUEUE_FAILURE_MESSAGE = (
    "Document processing could not be queued. Please try again."
)


class ProcessDocumentRequestRecoveryErrorCode:
    INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
    INVALID_MINIMUM_AGE = "INVALID_MINIMUM_AGE"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"


class ProcessDocumentRequestRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProcessDocumentRecoveryAssessment:
    request: ProcessDocumentRequest
    eligible: bool
    reason: RecoveryReason
    age: timedelta


@dataclass(frozen=True, slots=True)
class ProcessDocumentRecoveryResult:
    assessment: ProcessDocumentRecoveryAssessment
    enqueue_result: EnqueueResult | None


def _validate_request_id(request_id: int) -> None:
    if type(request_id) is not int or request_id < 1:
        raise ProcessDocumentRequestRecoveryError(
            ProcessDocumentRequestRecoveryErrorCode.INVALID_REQUEST_ID,
            "request_id must be a positive int.",
        )


def _validate_minimum_age(minimum_age: timedelta) -> None:
    if not isinstance(minimum_age, timedelta) or minimum_age <= timedelta(0):
        raise ProcessDocumentRequestRecoveryError(
            ProcessDocumentRequestRecoveryErrorCode.INVALID_MINIMUM_AGE,
            "minimum_age must be a positive timedelta.",
        )


def _assessment_for_request(
    request: ProcessDocumentRequest,
    *,
    document: Document,
    now: datetime,
    minimum_age: timedelta,
    collection_id: str,
    model_id: str,
) -> ProcessDocumentRecoveryAssessment:
    age = now - request.updated_at

    if request.status == ProcessDocumentRequest.Status.QUEUED:
        if request.last_enqueued_at is not None:
            assessment = ProcessDocumentRecoveryAssessment(
                request=request,
                eligible=False,
                reason="QUEUED_ALREADY_ENQUEUED",
                age=age,
            )
            return assessment
        eligible_reason: RecoveryReason = "STRANDED_QUEUED"
    elif request.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
        eligible_reason = "ENQUEUE_FAILED"
    else:
        assessment = ProcessDocumentRecoveryAssessment(
            request=request,
            eligible=False,
            reason="STATUS_NOT_RECOVERABLE",
            age=age,
        )
        return assessment

    if age < minimum_age:
        assessment = ProcessDocumentRecoveryAssessment(
            request=request,
            eligible=False,
            reason="TOO_RECENT",
            age=age,
        )
        return assessment

    intent_reason = _invalid_intent_reason(
        request,
        document=document,
        collection_id=collection_id,
        model_id=model_id,
    )
    if intent_reason is not None:
        return ProcessDocumentRecoveryAssessment(
            request=request,
            eligible=False,
            reason=intent_reason,
            age=age,
        )

    return ProcessDocumentRecoveryAssessment(
        request=request,
        eligible=True,
        reason=eligible_reason,
        age=age,
    )


def _has_usable_source_text(document_id: int) -> bool:
    texts = DocumentTextResult.objects.filter(
        document_id=document_id,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        status__in=(
            DocumentTextResult.Status.SUCCEEDED,
            DocumentTextResult.Status.NEEDS_REVIEW,
        ),
    ).values_list("text", flat=True)
    return any((text or "").strip() for text in texts)


def _upload_finalize_intent_is_valid(document: Document) -> bool:
    if document.upload_status != Document.UploadStatus.UPLOADED:
        return False
    if not document.archive_item_id:
        return False
    try:
        item_type = document.archive_item.item_type
    except ArchiveItem.DoesNotExist:
        return False
    if item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        return False
    if DocumentTextResult.objects.filter(
        document_id=document.pk,
        verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
    ).exists():
        return False
    return not _has_usable_source_text(document.pk)


def _invalid_intent_reason(
    request: ProcessDocumentRequest,
    *,
    document: Document,
    collection_id: str,
    model_id: str,
) -> RecoveryReason | None:
    if request.origin == ProcessDocumentRequest.Origin.UPLOAD_FINALIZE:
        if _upload_finalize_intent_is_valid(document):
            return None
        return "INTENT_NO_LONGER_VALID"

    if request.origin == ProcessDocumentRequest.Origin.OCR_REPROCESS:
        try:
            current = assess_ocr_reprocess(
                document.pk,
                collection_id=collection_id,
                model_id=model_id,
            )
        except OcrReprocessError:
            return "INTENT_NO_LONGER_VALID"
        if (
            current.retry_mode.value != request.ocr_retry_mode
            or current.source_transkribus_run_id != request.source_transkribus_run_id
        ):
            return "REQUEST_PAYLOAD_NO_LONGER_MATCHES"
        return None

    if request.origin == ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY:
        try:
            validate_document_for_hebrew_translation_retry(document)
        except HebrewTranslationRetryError:
            return "INTENT_NO_LONGER_VALID"
        return None

    return "INTENT_NO_LONGER_VALID"


def process_document_recovery_candidates(
    *,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_RECOVERY_MINIMUM_AGE,
) -> QuerySet[ProcessDocumentRequest]:
    """Return old Requests that may be recoverable after lock-time reassessment."""
    _validate_minimum_age(minimum_age)
    observed_now = now or timezone.now()
    cutoff = observed_now - minimum_age
    return (
        ProcessDocumentRequest.objects.filter(updated_at__lte=cutoff)
        .filter(
            Q(
                status=ProcessDocumentRequest.Status.QUEUED,
                last_enqueued_at__isnull=True,
            )
            | Q(status=ProcessDocumentRequest.Status.ENQUEUE_FAILED)
        )
        .order_by("pk")
    )


def assess_process_document_request_recovery(
    request_id: int,
    *,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_RECOVERY_MINIMUM_AGE,
    collection_id: str = "",
    model_id: str = "",
) -> ProcessDocumentRecoveryAssessment:
    """Read-only recovery assessment for one Request."""
    _validate_request_id(request_id)
    _validate_minimum_age(minimum_age)
    observed_now = now or timezone.now()
    try:
        request = ProcessDocumentRequest.objects.select_related(
            "document",
            "document__archive_item",
        ).get(pk=request_id)
    except ProcessDocumentRequest.DoesNotExist as exc:
        raise ProcessDocumentRequestRecoveryError(
            ProcessDocumentRequestRecoveryErrorCode.REQUEST_NOT_FOUND,
            "PROCESS_DOCUMENT request was not found.",
        ) from exc
    return _assessment_for_request(
        request,
        document=request.document,
        now=observed_now,
        minimum_age=minimum_age,
        collection_id=collection_id,
        model_id=model_id,
    )


def _reserve_for_recovery(
    request: ProcessDocumentRequest,
    *,
    now: datetime,
) -> None:
    """
    Reserve one send attempt without holding a transaction over SQS I/O.

    Touching updated_at starts a new cooldown. If this process exits after the
    commit but before SendMessage, the same Request becomes recoverable again
    after minimum_age. ENQUEUE_FAILED is returned to canonical QUEUED shape.
    """
    updates: dict[str, object] = {"updated_at": now}
    if request.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
        updates.update(
            {
                "status": ProcessDocumentRequest.Status.QUEUED,
                "failure_code": "",
                "failure_message": "",
                "last_enqueued_at": None,
            }
        )
    ProcessDocumentRequest.objects.filter(pk=request.pk).update(**updates)


def _queue_failure_message(request: ProcessDocumentRequest) -> str:
    if request.origin == ProcessDocumentRequest.Origin.OCR_REPROCESS:
        return _OCR_REPROCESS_QUEUE_FAILURE_MESSAGE
    return _UPLOAD_QUEUE_FAILURE_MESSAGE


def _sync_ocr_document_state(result: EnqueueResult) -> None:
    request = result.request
    if request.operation != ProcessDocumentRequest.Operation.OCR:
        return

    if result.outcome in {"REENQUEUED", "ALREADY_QUEUED"}:
        Document.objects.filter(
            pk=request.document_id,
            process_document_requests__pk=request.pk,
            process_document_requests__status=ProcessDocumentRequest.Status.QUEUED,
        ).update(
            processing_state_user=Document.ProcessingState.PROCESSING,
            upload_error=None,
        )
        return

    if result.outcome in {"ENQUEUE_FAILED", "ENQUEUE_OUTCOME_UNKNOWN"}:
        Document.objects.filter(
            pk=request.document_id,
            process_document_requests__pk=request.pk,
            process_document_requests__status=(
                ProcessDocumentRequest.Status.ENQUEUE_FAILED
            ),
        ).update(
            processing_state_user=Document.ProcessingState.FAILED,
            upload_error=_queue_failure_message(request),
        )


def recover_process_document_request(
    request_id: int,
    *,
    now: datetime | None = None,
    minimum_age: timedelta = DEFAULT_RECOVERY_MINIMUM_AGE,
    collection_id: str = "",
    model_id: str = "",
) -> ProcessDocumentRecoveryResult:
    """Reserve and resend one eligible Request through the canonical sender."""
    if connection.in_atomic_block:
        raise RuntimeError(
            "PROCESS_DOCUMENT recovery must be called outside database transactions."
        )
    _validate_request_id(request_id)
    _validate_minimum_age(minimum_age)
    observed_now = now or timezone.now()

    try:
        document_id = ProcessDocumentRequest.objects.values_list(
            "document_id",
            flat=True,
        ).get(pk=request_id)
    except ProcessDocumentRequest.DoesNotExist as exc:
        raise ProcessDocumentRequestRecoveryError(
            ProcessDocumentRequestRecoveryErrorCode.REQUEST_NOT_FOUND,
            "PROCESS_DOCUMENT request was not found.",
        ) from exc

    with transaction.atomic():
        try:
            document = (
                Document.objects.select_for_update()
                .select_related("archive_item")
                .get(pk=document_id)
            )
            request = ProcessDocumentRequest.objects.select_for_update().get(
                pk=request_id
            )
        except (Document.DoesNotExist, ProcessDocumentRequest.DoesNotExist) as exc:
            raise ProcessDocumentRequestRecoveryError(
                ProcessDocumentRequestRecoveryErrorCode.REQUEST_NOT_FOUND,
                "PROCESS_DOCUMENT request was not found.",
            ) from exc
        if request.document_id != document.pk:
            raise ProcessDocumentRequestRecoveryError(
                ProcessDocumentRequestRecoveryErrorCode.REQUEST_NOT_FOUND,
                "PROCESS_DOCUMENT request changed during recovery.",
            )

        assessment = _assessment_for_request(
            request,
            document=document,
            now=observed_now,
            minimum_age=minimum_age,
            collection_id=collection_id,
            model_id=model_id,
        )
        if not assessment.eligible:
            return ProcessDocumentRecoveryResult(
                assessment=assessment,
                enqueue_result=None,
            )
        _reserve_for_recovery(request, now=timezone.now())

    enqueue_result = send_reserved_process_document_request(
        request_id=request.pk,
        created=False,
    )
    _sync_ocr_document_state(enqueue_result)
    return ProcessDocumentRecoveryResult(
        assessment=assessment,
        enqueue_result=enqueue_result,
    )
