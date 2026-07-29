"""Hebrew-translation retry adapter for durable PROCESS_DOCUMENT enqueue."""

from __future__ import annotations

from django.contrib.auth.models import User

from documents.models import Document, ProcessDocumentRequest
from documents.services.hebrew_translation_retry import (
    HebrewTranslationRetryError,
    validate_document_for_hebrew_translation_retry,
)
from documents.services.process_document_request_enqueue import (
    EnqueueOutcome,
    EnqueueResult,
    ProcessDocumentRequestEnqueueError,
    enqueue_process_document_request,
)

_SAFE_QUEUE_FAILURE_MESSAGE = "לא ניתן היה לתזמן את התרגום לעברית. אפשר לנסות שוב."
_SAFE_CONFLICT_MESSAGE = "בקשת עיבוד אחרת כבר פעילה עבור המסמך."
_SAFE_RECOVERY_MESSAGE = "עיבוד קודם דורש שחזור לפני שאפשר לבקש שוב תרגום לעברית."
_SAFE_REQUEST_REJECTED_MESSAGE = "לא ניתן היה לבקש תרגום לעברית למסמך."


class HebrewTranslationRetryEnqueueErrorCode:
    QUEUE_UNAVAILABLE = "QUEUE_UNAVAILABLE"
    ACTIVE_REQUEST_CONFLICT = "ACTIVE_REQUEST_CONFLICT"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
    REQUEST_REJECTED = "REQUEST_REJECTED"


class HebrewTranslationRetryEnqueueError(HebrewTranslationRetryError):
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


def _active_request(document_id: int) -> ProcessDocumentRequest | None:
    """
    Return the one active PROCESS_DOCUMENT Request for this Document.

    Once the worker owns the Document it may already be PROCESSING, so the
    initial eligibility check can no longer pass. The active Request remains
    authoritative for coalescing or conflict reporting. A matching
    ENQUEUE_FAILED Request is intentionally not allowed to bypass a fresh
    eligibility check before retrying SendMessage.
    """
    return (
        ProcessDocumentRequest.objects.filter(
            document_id=document_id,
            status__in=(
                ProcessDocumentRequest.Status.QUEUED,
                ProcessDocumentRequest.Status.RUNNING,
                ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                ProcessDocumentRequest.Status.ENQUEUE_FAILED,
            ),
        )
        .order_by("pk")
        .first()
    )


def _payload_matches_translation_retry(request: ProcessDocumentRequest) -> bool:
    return (
        request.operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION
        and request.origin == ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY
        and request.ocr_retry_mode == ""
        and request.source_transkribus_run_id is None
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
            "Expected a non-retryable active Hebrew-translation Request, got "
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


def enqueue_hebrew_translation_retry(
    document_id: int,
    *,
    initiated_by: User,
) -> EnqueueResult:
    """
    Validate and enqueue an intentional translation-only retry.

    Document processing state remains worker-owned. In particular, enqueue
    success does not mark the Document PROCESSING, and queue failure does not
    overwrite the pre-existing OCR/translation state.
    """
    enqueue_result: EnqueueResult | None = None
    try:
        doc = Document.objects.select_related("archive_item").get(pk=document_id)
        validate_document_for_hebrew_translation_retry(doc)
    except HebrewTranslationRetryError:
        active = _active_request(document_id)
        if active is None:
            raise
        if _payload_matches_translation_retry(active):
            if active.status == ProcessDocumentRequest.Status.ENQUEUE_FAILED:
                raise
            enqueue_result = _coalesced_active_result(active)
        else:
            enqueue_result = EnqueueResult(
                outcome="ACTIVE_REQUEST_CONFLICT",
                request=active,
                created=False,
                message_sent=False,
                observed_status=active.status,
                send_attempted=False,
            )

    if enqueue_result is None:
        try:
            enqueue_result = enqueue_process_document_request(
                document_id=document_id,
                operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
                origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
                ocr_retry_mode="",
                source_transkribus_run_id=None,
                initiated_by=initiated_by,
            )
        except ProcessDocumentRequestEnqueueError as exc:
            raise HebrewTranslationRetryEnqueueError(
                code=HebrewTranslationRetryEnqueueErrorCode.REQUEST_REJECTED,
                public_message=_SAFE_REQUEST_REJECTED_MESSAGE,
                http_status=500,
            ) from exc

    if enqueue_result.outcome in {
        "CREATED_AND_ENQUEUED",
        "REENQUEUED",
        "ALREADY_QUEUED",
        "ALREADY_RUNNING",
        "ALREADY_TERMINAL",
    }:
        return enqueue_result

    if enqueue_result.outcome in {
        "ENQUEUE_FAILED",
        "ENQUEUE_OUTCOME_UNKNOWN",
    }:
        raise HebrewTranslationRetryEnqueueError(
            code=HebrewTranslationRetryEnqueueErrorCode.QUEUE_UNAVAILABLE,
            public_message=_SAFE_QUEUE_FAILURE_MESSAGE,
            http_status=500,
            outcome=enqueue_result.outcome,
        )

    if enqueue_result.outcome == "ACTIVE_REQUEST_CONFLICT":
        raise HebrewTranslationRetryEnqueueError(
            code=HebrewTranslationRetryEnqueueErrorCode.ACTIVE_REQUEST_CONFLICT,
            public_message=_SAFE_CONFLICT_MESSAGE,
            http_status=409,
            outcome=enqueue_result.outcome,
        )

    if enqueue_result.outcome == "BLOCKED_RECOVERY_REQUIRED":
        raise HebrewTranslationRetryEnqueueError(
            code=HebrewTranslationRetryEnqueueErrorCode.RECOVERY_REQUIRED,
            public_message=_SAFE_RECOVERY_MESSAGE,
            http_status=409,
            outcome=enqueue_result.outcome,
        )

    raise AssertionError(
        "Unhandled Hebrew-translation PROCESS_DOCUMENT enqueue outcome: "
        f"{enqueue_result.outcome}"
    )
