"""DB-side fencing for expired PROCESS_DOCUMENT RUNNING leases.

SQS is not required. This module never sends messages, calls providers, or
recomputes DocumentTextResult rollup. The write path only fences expired
``RUNNING`` Requests to ``RECOVERY_REQUIRED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import transaction
from django.db.models import F, Q, QuerySet
from django.utils import timezone

from documents.models import Document, ProcessDocumentRequest

DEFAULT_EXPIRED_LEASE_FENCE_LIMIT = 100
MAX_EXPIRED_LEASE_FENCE_LIMIT = 1000

_ACTIVE_INSPECTION_STATUSES = (
    ProcessDocumentRequest.Status.QUEUED,
    ProcessDocumentRequest.Status.RUNNING,
    ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
)


class ExpiredLeaseFenceOutcome(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    FENCED = "FENCED"
    SKIP_LIVE = "SKIP_LIVE"
    SKIP_STATUS = "SKIP_STATUS"
    SKIP_RECOVERY_REQUIRED = "SKIP_RECOVERY_REQUIRED"
    SKIP_SCOPE = "SKIP_SCOPE"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class ExpiredLeaseFenceResult:
    outcome: ExpiredLeaseFenceOutcome
    request_id: int | None
    document_id: int | None = None
    status: str | None = None
    lease_expires_at: datetime | None = None
    document_processing_state: str | None = None
    expired_by_seconds: int | None = None
    applied: bool = False


def lock_document_then_request(
    request_id: int,
) -> tuple[Document, ProcessDocumentRequest]:
    """Lock Document then Request so fence/claim/enqueue cannot deadlock."""
    document_id = ProcessDocumentRequest.objects.values_list(
        "document_id",
        flat=True,
    ).get(pk=request_id)
    document = Document.objects.select_for_update().get(pk=document_id)
    sync_request = ProcessDocumentRequest.objects.select_for_update().get(pk=request_id)
    if sync_request.document_id != document.pk:
        raise ProcessDocumentRequest.DoesNotExist
    return document, sync_request


def process_document_request_lease_is_live(
    sync_request: ProcessDocumentRequest,
    *,
    now: datetime,
) -> bool:
    """True only for a present lease timestamp strictly after ``now``."""
    lease_expires_at = sync_request.lease_expires_at
    return lease_expires_at is not None and lease_expires_at > now


def fence_locked_expired_running_process_document_request(
    *,
    document: Document,
    sync_request: ProcessDocumentRequest,
    now: datetime,
) -> bool:
    """Fence one expired RUNNING Request. Caller must hold Document then Request.

    Returns True only when this call committed the RUNNING -> RECOVERY_REQUIRED
    transition (and the PROCESSING overlay when applicable) on the locked rows.
    """
    if sync_request.status != ProcessDocumentRequest.Status.RUNNING:
        return False
    if process_document_request_lease_is_live(sync_request, now=now):
        return False

    sync_request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
    sync_request.lease_expires_at = None
    sync_request.save(update_fields=["status", "lease_expires_at", "updated_at"])
    if document.processing_state_user == Document.ProcessingState.PROCESSING:
        document.processing_state_user = Document.ProcessingState.RECOVERY_REQUIRED
        document.save(update_fields=["processing_state_user", "updated_at"])
    return True


def expired_running_process_document_requests(
    *,
    now: datetime | None = None,
) -> QuerySet[ProcessDocumentRequest]:
    """Unlocked prefilter of RUNNING rows whose lease is expired or missing."""
    observed_now = now or timezone.now()
    return (
        ProcessDocumentRequest.objects.filter(
            status=ProcessDocumentRequest.Status.RUNNING,
        )
        .filter(
            Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=observed_now)
        )
        .order_by(F("lease_expires_at").asc(nulls_first=True), "pk")
    )


def _expired_by_seconds(
    lease_expires_at: datetime | None,
    *,
    now: datetime,
) -> int | None:
    if lease_expires_at is None:
        return None
    return max(0, int((now - lease_expires_at).total_seconds()))


def _result_from_request(
    *,
    request: ProcessDocumentRequest,
    document: Document | None,
    outcome: ExpiredLeaseFenceOutcome,
    now: datetime,
    applied: bool = False,
) -> ExpiredLeaseFenceResult:
    return ExpiredLeaseFenceResult(
        outcome=outcome,
        request_id=request.pk,
        document_id=request.document_id,
        status=request.status,
        lease_expires_at=request.lease_expires_at,
        document_processing_state=(
            document.processing_state_user if document is not None else None
        ),
        expired_by_seconds=_expired_by_seconds(
            request.lease_expires_at,
            now=now,
        ),
        applied=applied,
    )


def _normalize_allowed_document_ids(
    allowed_document_ids: set[int] | frozenset[int] | None,
) -> frozenset[int] | None:
    if allowed_document_ids is None:
        return None
    return frozenset(allowed_document_ids)


def _skip_scope_if_unauthorized(
    *,
    document: Document,
    sync_request: ProcessDocumentRequest,
    now: datetime,
    allowed_document_ids: frozenset[int] | None,
) -> ExpiredLeaseFenceResult | None:
    if allowed_document_ids is None:
        return None
    if sync_request.document_id in allowed_document_ids:
        return None
    return _result_from_request(
        request=sync_request,
        document=document,
        outcome=ExpiredLeaseFenceOutcome.SKIP_SCOPE,
        now=now,
    )


def _classify_locked_request(
    *,
    document: Document,
    sync_request: ProcessDocumentRequest,
    now: datetime,
    apply: bool,
    allowed_document_ids: frozenset[int] | None = None,
) -> ExpiredLeaseFenceResult:
    scoped_out = _skip_scope_if_unauthorized(
        document=document,
        sync_request=sync_request,
        now=now,
        allowed_document_ids=allowed_document_ids,
    )
    if scoped_out is not None:
        return scoped_out
    if sync_request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.SKIP_RECOVERY_REQUIRED,
            now=now,
        )
    if sync_request.status != ProcessDocumentRequest.Status.RUNNING:
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.SKIP_STATUS,
            now=now,
        )
    if process_document_request_lease_is_live(sync_request, now=now):
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.SKIP_LIVE,
            now=now,
        )
    if not apply:
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.ELIGIBLE,
            now=now,
        )

    prior_lease_expires_at = sync_request.lease_expires_at
    fenced = fence_locked_expired_running_process_document_request(
        document=document,
        sync_request=sync_request,
        now=now,
    )
    if fenced:
        result = _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.FENCED,
            now=now,
            applied=True,
        )
        return ExpiredLeaseFenceResult(
            outcome=result.outcome,
            request_id=result.request_id,
            document_id=result.document_id,
            status=result.status,
            lease_expires_at=prior_lease_expires_at,
            document_processing_state=result.document_processing_state,
            expired_by_seconds=_expired_by_seconds(
                prior_lease_expires_at,
                now=now,
            ),
            applied=True,
        )
    if sync_request.status == ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.SKIP_RECOVERY_REQUIRED,
            now=now,
        )
    if process_document_request_lease_is_live(sync_request, now=now):
        return _result_from_request(
            request=sync_request,
            document=document,
            outcome=ExpiredLeaseFenceOutcome.SKIP_LIVE,
            now=now,
        )
    return _result_from_request(
        request=sync_request,
        document=document,
        outcome=ExpiredLeaseFenceOutcome.SKIP_STATUS,
        now=now,
    )


def inspect_process_document_request_expired_lease(
    request_id: int,
    *,
    now: datetime | None = None,
    allowed_document_ids: set[int] | frozenset[int] | None = None,
) -> ExpiredLeaseFenceResult:
    """Unlocked classification for dry-run. Never writes."""
    observed_now = now or timezone.now()
    allowed = _normalize_allowed_document_ids(allowed_document_ids)
    try:
        request = ProcessDocumentRequest.objects.select_related("document").get(
            pk=request_id
        )
    except ProcessDocumentRequest.DoesNotExist:
        return ExpiredLeaseFenceResult(
            outcome=ExpiredLeaseFenceOutcome.NOT_FOUND,
            request_id=request_id,
        )
    return _classify_locked_request(
        document=request.document,
        sync_request=request,
        now=observed_now,
        apply=False,
        allowed_document_ids=allowed,
    )


def fence_process_document_request_expired_lease(
    request_id: int,
    *,
    now: datetime | None = None,
    allowed_document_ids: set[int] | frozenset[int] | None = None,
) -> ExpiredLeaseFenceResult:
    """Lock Document then Request, re-check, and fence if still expired RUNNING."""
    observed_now = now or timezone.now()
    allowed = _normalize_allowed_document_ids(allowed_document_ids)
    with transaction.atomic():
        try:
            document, sync_request = lock_document_then_request(request_id)
        except (ProcessDocumentRequest.DoesNotExist, Document.DoesNotExist):
            return ExpiredLeaseFenceResult(
                outcome=ExpiredLeaseFenceOutcome.NOT_FOUND,
                request_id=request_id,
            )
        return _classify_locked_request(
            document=document,
            sync_request=sync_request,
            now=observed_now,
            apply=True,
            allowed_document_ids=allowed,
        )


def select_expired_lease_fence_request_ids(
    *,
    request_ids: list[int],
    document_ids: list[int],
    all_eligible: bool,
    limit: int,
    now: datetime | None = None,
) -> list[int]:
    """Resolve operator scope to Request ids. Missing explicit ids stay listed."""
    observed_now = now or timezone.now()
    if request_ids:
        # Keep explicit ids in operator order, including missing and
        # out-of-document-scope rows. The unlocked selector is not write
        # authorization; inspect/apply revalidate allowed_document_ids.
        ordered = list(dict.fromkeys(request_ids))
        return ordered[:limit]
    if document_ids:
        return list(
            ProcessDocumentRequest.objects.filter(
                document_id__in=document_ids,
                status__in=_ACTIVE_INSPECTION_STATUSES,
            )
            .order_by(F("lease_expires_at").asc(nulls_first=True), "pk")
            .values_list("pk", flat=True)[:limit]
        )
    if all_eligible:
        return list(
            expired_running_process_document_requests(now=observed_now).values_list(
                "pk",
                flat=True,
            )[:limit]
        )
    return list(
        expired_running_process_document_requests(now=observed_now).values_list(
            "pk",
            flat=True,
        )[:limit]
    )


__all__ = [
    "DEFAULT_EXPIRED_LEASE_FENCE_LIMIT",
    "MAX_EXPIRED_LEASE_FENCE_LIMIT",
    "ExpiredLeaseFenceOutcome",
    "ExpiredLeaseFenceResult",
    "expired_running_process_document_requests",
    "fence_locked_expired_running_process_document_request",
    "fence_process_document_request_expired_lease",
    "inspect_process_document_request_expired_lease",
    "lock_document_then_request",
    "process_document_request_lease_is_live",
    "select_expired_lease_fence_request_ids",
]
