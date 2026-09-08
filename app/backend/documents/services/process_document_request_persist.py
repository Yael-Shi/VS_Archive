"""Request-token fence for automated PROCESS_DOCUMENT result persistence."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from documents.models import Document, ProcessDocumentRequest

logger = logging.getLogger(__name__)

PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY = "request_id"
LEASE_TOKEN_PAYLOAD_KEY = "lease_token"

_PERSIST_ALLOWED_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.RUNNING,
        ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
    }
)


class ProcessDocumentExecutionIdentityKind(StrEnum):
    LEGACY = "legacy"
    REQUEST_AWARE = "request_aware"
    INVALID = "invalid"


@dataclass(frozen=True)
class ProcessDocumentExecutionIdentity:
    """Resolved PROCESS_DOCUMENT persist identity from a raw payload."""

    kind: ProcessDocumentExecutionIdentityKind
    request_id: int | None = None
    lease_token: uuid.UUID | None = None

    @classmethod
    def legacy(cls) -> ProcessDocumentExecutionIdentity:
        return cls(kind=ProcessDocumentExecutionIdentityKind.LEGACY)

    @classmethod
    def invalid(cls) -> ProcessDocumentExecutionIdentity:
        return cls(kind=ProcessDocumentExecutionIdentityKind.INVALID)

    @classmethod
    def request_aware(
        cls,
        request_id: int,
        lease_token: uuid.UUID,
    ) -> ProcessDocumentExecutionIdentity:
        return cls(
            kind=ProcessDocumentExecutionIdentityKind.REQUEST_AWARE,
            request_id=request_id,
            lease_token=lease_token,
        )


def parse_process_document_request_id(raw: Any) -> int | None:
    """Accept only a positive plain int Request id."""
    if type(raw) is not int or raw < 1:
        return None
    return raw


def parse_process_document_lease_token(raw: Any) -> uuid.UUID | None:
    """Accept a UUID object or a canonical UUID string."""
    if isinstance(raw, uuid.UUID):
        return raw
    if isinstance(raw, str):
        try:
            return uuid.UUID(raw)
        except ValueError:
            return None
    return None


def resolve_process_document_execution_identity(
    payload: Mapping[str, Any],
) -> ProcessDocumentExecutionIdentity:
    """Classify persist identity from payload key presence, not parsed Nones.

    Legacy mixed-version `{type, document_id}` is allowed only when both
    identity keys are absent. If either key is present, both must parse.
    """
    request_present = PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY in payload
    token_present = LEASE_TOKEN_PAYLOAD_KEY in payload
    if not request_present and not token_present:
        return ProcessDocumentExecutionIdentity.legacy()
    if not request_present or not token_present:
        return ProcessDocumentExecutionIdentity.invalid()

    request_id = parse_process_document_request_id(
        payload[PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY]
    )
    lease_token = parse_process_document_lease_token(payload[LEASE_TOKEN_PAYLOAD_KEY])
    if request_id is None or lease_token is None:
        return ProcessDocumentExecutionIdentity.invalid()
    return ProcessDocumentExecutionIdentity.request_aware(request_id, lease_token)


def automated_process_document_persist_is_allowed(
    *,
    document: Document,
    identity: ProcessDocumentExecutionIdentity,
) -> bool:
    """True when this execution may persist automated PROCESS_DOCUMENT results.

    Caller must already hold the Document row lock. This then locks the Request
    (Document → Request). Legacy identity is allowed only when the payload
    boundary classified both identity keys as absent.
    """
    if identity.kind == ProcessDocumentExecutionIdentityKind.LEGACY:
        return True
    if identity.kind != ProcessDocumentExecutionIdentityKind.REQUEST_AWARE:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; invalid request "
            "identity document_id=%s",
            document.pk,
        )
        return False

    request_id = identity.request_id
    lease_token = identity.lease_token
    if request_id is None or lease_token is None:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; incomplete request "
            "identity document_id=%s request_id=%s lease_token_present=%s",
            document.pk,
            request_id,
            lease_token is not None,
        )
        return False

    try:
        sync_request = ProcessDocumentRequest.objects.select_for_update().get(
            pk=request_id
        )
    except ProcessDocumentRequest.DoesNotExist:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; request missing "
            "document_id=%s request_id=%s",
            document.pk,
            request_id,
        )
        return False

    if sync_request.document_id != document.pk:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; request/document "
            "mismatch document_id=%s request_id=%s request_document_id=%s",
            document.pk,
            request_id,
            sync_request.document_id,
        )
        return False

    if sync_request.status not in _PERSIST_ALLOWED_STATUSES:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; request not runnable "
            "document_id=%s request_id=%s status=%s",
            document.pk,
            request_id,
            sync_request.status,
        )
        return False

    if sync_request.lease_token != lease_token:
        logger.info(
            "Skipping automated PROCESS_DOCUMENT persist; lease token mismatch "
            "document_id=%s request_id=%s status=%s",
            document.pk,
            request_id,
            sync_request.status,
        )
        return False

    return True


__all__ = [
    "LEASE_TOKEN_PAYLOAD_KEY",
    "PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY",
    "ProcessDocumentExecutionIdentity",
    "ProcessDocumentExecutionIdentityKind",
    "automated_process_document_persist_is_allowed",
    "parse_process_document_lease_token",
    "parse_process_document_request_id",
    "resolve_process_document_execution_identity",
]
