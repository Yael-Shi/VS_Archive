"""Staff recovery for parked PROCESS_DOCUMENT RECOVERY_REQUIRED Requests.

Abandon is service-only write: no SQS, provider I/O, or retry. Staff HTTP UI
calls this service. Intentional retry orchestration is out of scope.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from django.db import transaction
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest
from documents.services.ocr_reprocess import has_recoverable_ocr_partial_evidence
from documents.services.process_document_request_expired_lease import (
    lock_document_then_request,
)
from documents.services.processing_state import (
    ORDINARY_RESULT_PROCESSING_STATES,
    update_document_processing_state_from_displayed_source,
)
from documents.services.text_presentation import resolve_displayable_source_text_result

logger = logging.getLogger(__name__)

STAFF_ABANDONED_FAILURE_CODE = "STAFF_ABANDONED"
STAFF_ABANDONED_FAILURE_MESSAGE = (
    "Staff released a recovery-required request without retry."
)

_TERMINAL_REQUEST_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.COMPLETED,
        ProcessDocumentRequest.Status.PARTIAL,
        ProcessDocumentRequest.Status.FAILED,
    }
)


class StaffAbandonOutcome(StrEnum):
    ABANDONED = "ABANDONED"
    ALREADY_TERMINAL = "ALREADY_TERMINAL"


class ProcessDocumentRequestStaffAbandonErrorCode:
    INVALID_REQUEST_ID = "INVALID_REQUEST_ID"
    INVALID_DOCUMENT_ID = "INVALID_DOCUMENT_ID"
    REQUEST_NOT_FOUND = "REQUEST_NOT_FOUND"
    DOCUMENT_MISMATCH = "DOCUMENT_MISMATCH"
    STATUS_NOT_ABANDONABLE = "STATUS_NOT_ABANDONABLE"
    INVALID_RECOVERY_SHAPE = "INVALID_RECOVERY_SHAPE"


class ProcessDocumentRequestStaffAbandonError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StaffAbandonResult:
    outcome: StaffAbandonOutcome
    request: ProcessDocumentRequest
    document: Document
    previous_request_status: str
    previous_document_state: str


def _validate_ids(*, request_id: int, document_id: int) -> None:
    if type(request_id) is not int or request_id < 1:
        raise ProcessDocumentRequestStaffAbandonError(
            ProcessDocumentRequestStaffAbandonErrorCode.INVALID_REQUEST_ID,
            "request_id must be a positive int.",
        )
    if type(document_id) is not int or document_id < 1:
        raise ProcessDocumentRequestStaffAbandonError(
            ProcessDocumentRequestStaffAbandonErrorCode.INVALID_DOCUMENT_ID,
            "document_id must be a positive int.",
        )


def _recovery_shape_is_valid(sync_request: ProcessDocumentRequest) -> bool:
    return (
        sync_request.lease_token is not None
        and sync_request.lease_expires_at is None
        and sync_request.started_at is not None
        and sync_request.completed_at is None
    )


def _replace_recovery_overlay(
    *,
    document: Document,
    sync_request: ProcessDocumentRequest,
) -> None:
    if document.processing_state_user != Document.ProcessingState.RECOVERY_REQUIRED:
        return

    if sync_request.operation == ProcessDocumentRequest.Operation.HEBREW_TRANSLATION:
        update_document_processing_state_from_displayed_source(document)
        if document.processing_state_user not in ORDINARY_RESULT_PROCESSING_STATES:
            document.processing_state_user = Document.ProcessingState.PARTIAL
        return

    source_row = resolve_displayable_source_text_result(document)
    if source_row is not None and (source_row.text or "").strip():
        update_document_processing_state_from_displayed_source(document)
        return
    if has_recoverable_ocr_partial_evidence(document):
        document.processing_state_user = Document.ProcessingState.PARTIAL
        return
    document.processing_state_user = Document.ProcessingState.FAILED


def get_recovery_required_process_document_request(
    *,
    document_id: int,
) -> ProcessDocumentRequest | None:
    """Return the parked RECOVERY_REQUIRED Request for a document, or None.

    Fail-closed: invalid ids, zero matches, and more than one match return None.
    Does not validate recovery shape; POST abandon remains the write fence.
    """
    if type(document_id) is not int or document_id < 1:
        return None
    matches = list(
        ProcessDocumentRequest.objects.filter(
            document_id=document_id,
            status=ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        ).order_by("pk")[:2]
    )
    if len(matches) != 1:
        return None
    return matches[0]


def abandon_process_document_request(
    *,
    request_id: int,
    document_id: int,
) -> StaffAbandonResult:
    """Release a parked RECOVERY_REQUIRED Request without enqueue or provider I/O."""
    _validate_ids(request_id=request_id, document_id=document_id)
    now = timezone.now()

    with transaction.atomic():
        try:
            document, sync_request = lock_document_then_request(request_id)
        except ProcessDocumentRequest.DoesNotExist as exc:
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.REQUEST_NOT_FOUND,
                "ProcessDocumentRequest was not found.",
            ) from exc
        except Document.DoesNotExist as exc:
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.REQUEST_NOT_FOUND,
                "ProcessDocumentRequest was not found.",
            ) from exc

        if sync_request.document_id != document_id:
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.DOCUMENT_MISMATCH,
                "ProcessDocumentRequest does not belong to this document.",
            )

        previous_request_status = sync_request.status
        previous_document_state = document.processing_state_user

        if sync_request.status in _TERMINAL_REQUEST_STATUSES:
            logger.info(
                "event=staff_abandon_process_document_request outcome=%s "
                "document_id=%s request_id=%s operation=%s origin=%s "
                "previous_request_status=%s previous_document_state=%s "
                "new_request_status=%s new_document_state=%s "
                "retained_token_cleared=false",
                StaffAbandonOutcome.ALREADY_TERMINAL,
                document.pk,
                sync_request.pk,
                sync_request.operation,
                sync_request.origin,
                previous_request_status,
                previous_document_state,
                sync_request.status,
                document.processing_state_user,
            )
            return StaffAbandonResult(
                outcome=StaffAbandonOutcome.ALREADY_TERMINAL,
                request=sync_request,
                document=document,
                previous_request_status=previous_request_status,
                previous_document_state=previous_document_state,
            )

        if sync_request.status != ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.STATUS_NOT_ABANDONABLE,
                "ProcessDocumentRequest is not recovery-required.",
            )

        if not _recovery_shape_is_valid(sync_request):
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.INVALID_RECOVERY_SHAPE,
                "ProcessDocumentRequest recovery-required shape is invalid.",
            )

        _replace_recovery_overlay(document=document, sync_request=sync_request)
        if document.processing_state_user not in ORDINARY_RESULT_PROCESSING_STATES:
            raise ProcessDocumentRequestStaffAbandonError(
                ProcessDocumentRequestStaffAbandonErrorCode.INVALID_RECOVERY_SHAPE,
                "Abandon refused to leave a non-ordinary Document processing state.",
            )

        sync_request.status = ProcessDocumentRequest.Status.FAILED
        sync_request.failure_code = STAFF_ABANDONED_FAILURE_CODE
        sync_request.failure_message = STAFF_ABANDONED_FAILURE_MESSAGE
        sync_request.lease_token = None
        sync_request.lease_expires_at = None
        sync_request.completed_at = now
        sync_request.save(
            update_fields=[
                "status",
                "failure_code",
                "failure_message",
                "lease_token",
                "lease_expires_at",
                "completed_at",
                "updated_at",
            ]
        )
        if document.processing_state_user != previous_document_state:
            document.save(update_fields=["processing_state_user", "updated_at"])

        logger.info(
            "event=staff_abandon_process_document_request outcome=%s "
            "document_id=%s request_id=%s operation=%s origin=%s "
            "previous_request_status=%s previous_document_state=%s "
            "new_request_status=%s new_document_state=%s "
            "retained_token_cleared=true",
            StaffAbandonOutcome.ABANDONED,
            document.pk,
            sync_request.pk,
            sync_request.operation,
            sync_request.origin,
            previous_request_status,
            previous_document_state,
            sync_request.status,
            document.processing_state_user,
        )
        return StaffAbandonResult(
            outcome=StaffAbandonOutcome.ABANDONED,
            request=sync_request,
            document=document,
            previous_request_status=previous_request_status,
            previous_document_state=previous_document_state,
        )


__all__ = [
    "STAFF_ABANDONED_FAILURE_CODE",
    "STAFF_ABANDONED_FAILURE_MESSAGE",
    "ProcessDocumentRequestStaffAbandonError",
    "ProcessDocumentRequestStaffAbandonErrorCode",
    "StaffAbandonOutcome",
    "StaffAbandonResult",
    "abandon_process_document_request",
    "get_recovery_required_process_document_request",
]
