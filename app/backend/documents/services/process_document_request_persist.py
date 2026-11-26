"""Request-token fence for automated PROCESS_DOCUMENT result persistence."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from documents.models import Document, ProcessDocumentRequest, TranskribusRun

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


def staff_resume_request_run_provenance_error(
    *,
    request: ProcessDocumentRequest,
    run: TranskribusRun,
) -> str | None:
    """Return a provenance error message, or None when the run may bind to request.

    There is no durable Request→attempt-Run FK. Operator-selected run_id +
    recognition_job_id remains the attempt selector. This enforces:

    - recognition_job_id uniqueness on the document
    - for ``transkribus_recognition_only``: source_transkribus_run present,
      attempt ≠ source, and matching upload lineage fields copied onto the
      attempt row (remote_doc_id / collection_id / model_id / pages_query)

    Call under Document→Request→Run locks at write time; lineage fields can be
    rewritten by run persistence helpers after create.
    """
    job_id = str(run.recognition_job_id or "").strip()
    if job_id and (
        TranskribusRun.objects.filter(
            document_id=run.document_id,
            recognition_job_id=job_id,
        )
        .exclude(pk=run.pk)
        .exists()
    ):
        return (
            f"recognition_job_id={job_id!r} is present on more than one "
            f"TranskribusRun for document id={run.document_id}."
        )

    if (
        request.ocr_retry_mode
        != ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
    ):
        return None

    source = request.source_transkribus_run
    if source is None:
        return (
            f"ProcessDocumentRequest id={request.pk} is "
            "transkribus_recognition_only but source_transkribus_run is missing."
        )
    if source.document_id != run.document_id:
        return (
            f"source_transkribus_run id={source.pk} belongs to a different "
            f"document than run id={run.pk}."
        )
    if run.pk == source.pk:
        return (
            f"Run id={run.pk} is the request source_transkribus_run; resume-only "
            "targets the recognition-only attempt row, not the source upload row."
        )
    for label, left, right in (
        ("remote_doc_id", run.remote_doc_id, source.remote_doc_id),
        ("collection_id", run.collection_id, source.collection_id),
        ("model_id", run.model_id, source.model_id),
        ("pages_query", run.pages_query, source.pages_query),
    ):
        if str(left or "").strip() != str(right or "").strip():
            return (
                f"Run id={run.pk} {label} does not match "
                f"source_transkribus_run id={source.pk} (recognition-only lineage)."
            )
    return None


def staff_resume_only_persist_is_allowed(
    *,
    document: Document,
    request_id: int,
    run_id: int,
    recognition_job_id: str,
) -> bool:
    """True when staff resume-only may persist for this exact request/run/job.

    Caller must already hold the Document row lock. Locks Request then Run
    (Document → Request → Run). Never authorizes by lease_token and is not
    reachable via payload identity parsing. Re-checks recognition-only lineage
    under those locks.
    """
    if type(request_id) is not int or request_id < 1:
        return False
    if type(run_id) is not int or run_id < 1:
        return False
    job_id = (recognition_job_id or "").strip()
    if not job_id:
        return False

    try:
        # Do not select_related nullable source_transkribus_run under
        # select_for_update: Postgres rejects FOR UPDATE on that outer join.
        sync_request = ProcessDocumentRequest.objects.select_for_update().get(
            pk=request_id
        )
    except ProcessDocumentRequest.DoesNotExist:
        logger.info(
            "Skipping staff resume-only persist; request missing document_id=%s "
            "request_id=%s",
            document.pk,
            request_id,
        )
        return False
    if sync_request.document_id != document.pk:
        logger.info(
            "Skipping staff resume-only persist; request/document mismatch "
            "document_id=%s request_id=%s request_document_id=%s",
            document.pk,
            request_id,
            sync_request.document_id,
        )
        return False
    if sync_request.operation != ProcessDocumentRequest.Operation.OCR:
        logger.info(
            "Skipping staff resume-only persist; request operation not OCR "
            "document_id=%s request_id=%s operation=%s",
            document.pk,
            request_id,
            sync_request.operation,
        )
        return False
    if sync_request.status != ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        logger.info(
            "Skipping staff resume-only persist; request not parked "
            "document_id=%s request_id=%s status=%s",
            document.pk,
            request_id,
            sync_request.status,
        )
        return False
    if not (
        sync_request.lease_token is not None
        and sync_request.lease_expires_at is None
        and sync_request.started_at is not None
        and sync_request.completed_at is None
    ):
        logger.info(
            "Skipping staff resume-only persist; invalid recovery shape "
            "document_id=%s request_id=%s",
            document.pk,
            request_id,
        )
        return False

    try:
        run = TranskribusRun.objects.select_for_update().get(pk=run_id)
    except TranskribusRun.DoesNotExist:
        logger.info(
            "Skipping staff resume-only persist; run missing document_id=%s run_id=%s",
            document.pk,
            run_id,
        )
        return False
    if run.document_id != document.pk:
        logger.info(
            "Skipping staff resume-only persist; run/document mismatch "
            "document_id=%s run_id=%s run_document_id=%s",
            document.pk,
            run_id,
            run.document_id,
        )
        return False
    if str(run.recognition_job_id or "").strip() != job_id:
        logger.info(
            "Skipping staff resume-only persist; recognition_job_id mismatch "
            "document_id=%s run_id=%s",
            document.pk,
            run_id,
        )
        return False
    if run.status not in {
        TranskribusRun.Status.RECOGNITION_STARTED,
        TranskribusRun.Status.SUCCEEDED,
    }:
        logger.info(
            "Skipping staff resume-only persist; run status not resumeable "
            "document_id=%s run_id=%s status=%s",
            document.pk,
            run_id,
            run.status,
        )
        return False
    provenance_error = staff_resume_request_run_provenance_error(
        request=sync_request, run=run
    )
    if provenance_error is not None:
        logger.info(
            "Skipping staff resume-only persist; provenance refused "
            "document_id=%s request_id=%s run_id=%s detail=%s",
            document.pk,
            request_id,
            run_id,
            provenance_error,
        )
        return False
    return True


class StaleProcessDocumentPageClaimError(RuntimeError):
    """Raised when a Request execution may not acquire a new page lease."""


def require_process_document_page_claim_allowed(
    *,
    document_id: int,
    identity: ProcessDocumentExecutionIdentity | None,
) -> None:
    """Fail closed before issuing a new Gemini/Arabic page lease.

    Lock order is Document then Request, matching persist/abandon/claim.
    ``None`` identity is treated as mixed-version legacy (both identity keys
    absent). Present-but-malformed identity remains fail-closed.
    Does not clear or steal existing page leases.
    """
    resolved = (
        identity if identity is not None else ProcessDocumentExecutionIdentity.legacy()
    )
    try:
        document = Document.objects.select_for_update().get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise StaleProcessDocumentPageClaimError(
            "ProcessDocumentRequest execution cannot acquire a page lease; "
            "document was not found."
        ) from exc
    if automated_process_document_persist_is_allowed(
        document=document,
        identity=resolved,
    ):
        return
    raise StaleProcessDocumentPageClaimError(
        "ProcessDocumentRequest execution identity cannot acquire a page lease."
    )


__all__ = [
    "LEASE_TOKEN_PAYLOAD_KEY",
    "PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY",
    "ProcessDocumentExecutionIdentity",
    "ProcessDocumentExecutionIdentityKind",
    "StaleProcessDocumentPageClaimError",
    "automated_process_document_persist_is_allowed",
    "parse_process_document_lease_token",
    "parse_process_document_request_id",
    "require_process_document_page_claim_allowed",
    "resolve_process_document_execution_identity",
    "staff_resume_only_persist_is_allowed",
    "staff_resume_request_run_provenance_error",
]
