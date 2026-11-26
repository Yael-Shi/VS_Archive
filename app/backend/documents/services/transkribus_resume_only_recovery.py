"""Staff-authorized Transkribus resume-only local completion.

Completes a finished remote recognition job for a parked RECOVERY_REQUIRED
ProcessDocumentRequest without starting recognition or adopting the worker
lease_token. PAGE XML / S3 stay outside the local-success transaction; DTR
persistence reuses complete_transkribus_local_success.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Literal, Mapping

from django.db import transaction

from documents.models import (
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
    TranskribusRun,
    TranskribusTranscriptSnapshot,
)
from documents.services.htr_adapters.base import (
    TranskribusLocalPersistenceRetryableError,
)
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.process_document_request_worker import (
    terminalize_parked_process_document_after_staff_resume,
)
from documents.services.processing_state import ORDINARY_RESULT_PROCESSING_STATES
from documents.services.transkribus_engine import (
    PylaiaTranscriptionOutcome,
    _job_terminal_success,
)
from documents.services.process_document_request_persist import (
    staff_resume_only_persist_is_allowed,
    staff_resume_request_run_provenance_error,
)
from documents.services.transkribus_local_completion import (
    TranskribusLocalCompletionError,
    associate_run_with_automatic_snapshot,
    complete_transkribus_local_success,
    get_run_automatic_snapshot_association,
    inspect_local_completion_bindings,
    local_completion_is_structurally_done,
    reconstruct_engine_review_reasons_from_snapshot,
    store_automatic_snapshot_from_selected_pages,
)
from documents.services.transkribus_snapshot_pages import (
    TranskribusPageMappingError,
    normalize_page_index_to_page_nr,
    snapshot_pages_from_upload_mapping,
)
from documents.services.transkribus_snapshot_storage import (
    TranskribusSnapshotStorageValidationError,
)

logger = logging.getLogger(__name__)

RecoveryPhase = Literal[
    "FETCH_AND_COMPLETE",
    "COMPLETE_FROM_SNAPSHOT",
    "TERMINALIZE_ONLY",
    "ALREADY_DONE",
]

JobFetcher = Callable[[str], Mapping]
PageCompleter = Callable[[TranskribusRun], PylaiaTranscriptionOutcome]


class TranskribusResumeOnlyRecoveryError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TranskribusResumeOnlyRecoveryAssessment:
    document_id: int
    request_id: int
    run_id: int
    recognition_job_id: str
    phase: RecoveryPhase | None
    apply_permitted: bool
    reason: str
    message: str
    document_processing_state: str | None = None
    request_status: str | None = None
    run_status: str | None = None
    association_present: bool = False
    snapshot_id: int | None = None
    remote_job_state: str | None = None
    remote_job_success: bool | None = None
    review_preservation_note: str = (
        "Apply persists NEEDS_REVIEW + UNVERIFIED with "
        "AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW; does not auto-approve."
    )


def _positive_id(value: object, *, code: str, label: str) -> int:
    if type(value) is not int or value < 1:
        raise TranskribusResumeOnlyRecoveryError(
            code, f"{label} must be a positive int."
        )
    return value


def _normalize_job_id(value: object) -> str:
    if isinstance(value, bool):
        raise TranskribusResumeOnlyRecoveryError(
            "INVALID_RECOGNITION_JOB_ID",
            "recognition_job_id must be a non-empty string.",
        )
    if isinstance(value, int):
        if value < 1:
            raise TranskribusResumeOnlyRecoveryError(
                "INVALID_RECOGNITION_JOB_ID",
                "recognition_job_id must be a positive int or non-empty string.",
            )
        return str(value)
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    raise TranskribusResumeOnlyRecoveryError(
        "INVALID_RECOGNITION_JOB_ID",
        "recognition_job_id must be a non-empty string.",
    )


def _recovery_shape_ok(request: ProcessDocumentRequest) -> bool:
    return (
        request.lease_token is not None
        and request.lease_expires_at is None
        and request.started_at is not None
        and request.completed_at is None
    )


def _is_hebrew(language: str | None) -> bool:
    return (language or "").strip().lower() in ("he", "heb", "hebrew")


def _engine_runtime(run: TranskribusRun) -> str:
    return f"transkribus-pylaia:{str(run.model_id).strip()}"


def _route_for_document(document: Document) -> OcrRouteConfig:
    text_input = (document.text_input_type or "").strip().upper()
    if text_input == Document.TextInputType.PRINTED:
        variant = DocumentTextResult.OcrPromptVariant.PRINTED
    elif text_input == Document.TextInputType.MIXED:
        variant = DocumentTextResult.OcrPromptVariant.MIXED
    else:
        variant = DocumentTextResult.OcrPromptVariant.HANDWRITTEN
    return OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=variant,
    )


def _min_text_length() -> int:
    raw = (os.getenv("MIN_TEXT_LENGTH") or "").strip()
    try:
        return max(0, int(raw)) if raw else 20
    except ValueError:
        return 20


def _has_usable_source_for_engine(*, document_id: int, engine: str) -> bool:
    texts = DocumentTextResult.objects.filter(
        document_id=document_id,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=engine,
        status__in=(
            DocumentTextResult.Status.SUCCEEDED,
            DocumentTextResult.Status.NEEDS_REVIEW,
        ),
    ).values_list("text", flat=True)
    return any((text or "").strip() for text in texts)


def _has_verified(document_id: int) -> bool:
    return DocumentTextResult.objects.filter(
        document_id=document_id,
        verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
    ).exists()


def _load_chain(
    *,
    document_id: int,
    request_id: int,
    run_id: int,
    recognition_job_id: str | int,
) -> tuple[Document, ProcessDocumentRequest, TranskribusRun, str]:
    doc_id = _positive_id(document_id, code="INVALID_DOCUMENT_ID", label="document_id")
    req_id = _positive_id(request_id, code="INVALID_REQUEST_ID", label="request_id")
    r_id = _positive_id(run_id, code="INVALID_RUN_ID", label="run_id")
    job_id = _normalize_job_id(recognition_job_id)

    try:
        document = Document.objects.get(pk=doc_id)
        request = ProcessDocumentRequest.objects.get(pk=req_id)
        run = TranskribusRun.objects.get(pk=r_id)
    except Document.DoesNotExist as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "DOCUMENT_NOT_FOUND", f"Document id={doc_id} was not found."
        ) from exc
    except ProcessDocumentRequest.DoesNotExist as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "REQUEST_NOT_FOUND",
            f"ProcessDocumentRequest id={req_id} was not found.",
        ) from exc
    except TranskribusRun.DoesNotExist as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "RUN_NOT_FOUND", f"TranskribusRun id={r_id} was not found."
        ) from exc

    if request.document_id != document.pk or run.document_id != document.pk:
        raise TranskribusResumeOnlyRecoveryError(
            "IDENTIFIER_MISMATCH",
            "Request/run/document identifiers do not form one processing chain.",
        )
    if str(run.recognition_job_id or "").strip() != job_id:
        raise TranskribusResumeOnlyRecoveryError(
            "IDENTIFIER_MISMATCH",
            f"TranskribusRun id={run.pk} recognition_job_id does not match "
            f"provided recognition_job_id={job_id!r}.",
        )
    provenance_error = staff_resume_request_run_provenance_error(
        request=request, run=run
    )
    if provenance_error is not None:
        raise TranskribusResumeOnlyRecoveryError(
            "AMBIGUOUS_RUN_PROVENANCE", provenance_error
        )
    return document, request, run, job_id


def _ready_association(
    run: TranskribusRun,
) -> tuple[object | None, TranskribusTranscriptSnapshot | None]:
    assoc = get_run_automatic_snapshot_association(run)
    if assoc is None:
        return None, None
    snapshot = assoc.snapshot
    if (
        snapshot.document_id != run.document_id
        or snapshot.source_kind
        != TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR
    ):
        raise TranskribusResumeOnlyRecoveryError(
            "AMBIGUOUS_SNAPSHOT_STATE",
            f"Associated snapshot id={snapshot.pk} is invalid for run id={run.pk}.",
        )
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise TranskribusResumeOnlyRecoveryError(
            "SNAPSHOT_NOT_READY",
            f"Associated snapshot id={snapshot.pk} is not READY.",
        )
    return assoc, snapshot


def _validate_upload_run(run: TranskribusRun) -> None:
    if run.mode != TranskribusRun.Mode.UPLOAD_CREATED:
        raise TranskribusResumeOnlyRecoveryError(
            "RUN_MODE_NOT_SUPPORTED",
            f"TranskribusRun id={run.pk} mode={run.mode} is not UPLOAD_CREATED.",
        )
    if not (
        (run.recognition_job_id or "").strip()
        and (run.remote_doc_id or "").strip()
        and (run.pages_query or "").strip()
        and (run.collection_id or "").strip()
        and (run.model_id or "").strip()
    ):
        raise TranskribusResumeOnlyRecoveryError(
            "DURABLE_IDS_INCOMPLETE",
            f"TranskribusRun id={run.pk} is missing durable recognition ids.",
        )
    try:
        normalize_page_index_to_page_nr(run.page_index_to_page_nr)
    except TranskribusPageMappingError as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "PAGE_MAPPING_INVALID", str(exc)
        ) from exc


def _require_remote_job_success(
    *,
    job_id: str,
    job_fetcher: JobFetcher | None,
) -> tuple[str | None, bool | None]:
    if job_fetcher is None:
        raise TranskribusResumeOnlyRecoveryError(
            "REMOTE_JOB_EVIDENCE_REQUIRED",
            "Remote job evidence is required (job_fetcher or --remote-job-json / "
            "--fetch-remote-job) before FETCH_AND_COMPLETE.",
        )
    try:
        job = job_fetcher(job_id)
    except TranskribusResumeOnlyRecoveryError:
        raise
    except Exception as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "REMOTE_JOB_FETCH_FAILED",
            f"Failed to fetch Transkribus job id={job_id}: {exc}",
        ) from exc
    if not isinstance(job, Mapping):
        raise TranskribusResumeOnlyRecoveryError(
            "REMOTE_JOB_FETCH_FAILED",
            f"Transkribus job id={job_id} payload is not a mapping.",
        )

    # When the payload includes an id, require it to match the requested job.
    raw_job_id = job.get("jobId", job.get("job_id"))
    if raw_job_id is not None and str(raw_job_id).strip() != job_id:
        raise TranskribusResumeOnlyRecoveryError(
            "REMOTE_JOB_NOT_SUCCESS",
            f"Remote job payload id={raw_job_id!r} does not match "
            f"requested recognition_job_id={job_id!r}.",
        )

    raw_state = job.get("state")
    state = str(raw_state).strip() if raw_state is not None else None
    success = job.get("success") if isinstance(job.get("success"), bool) else None
    if not _job_terminal_success(dict(job)):
        raise TranskribusResumeOnlyRecoveryError(
            "REMOTE_JOB_NOT_SUCCESS",
            f"Remote recognition job id={job_id} is not terminal success "
            f"(state={state!r}, success={success!r}).",
        )
    return state, success


def _refuse(
    *,
    document: Document,
    request: ProcessDocumentRequest,
    run: TranskribusRun,
    job_id: str,
    reason: str,
    message: str,
    association_present: bool = False,
    snapshot: TranskribusTranscriptSnapshot | None = None,
    remote_job_state: str | None = None,
    remote_job_success: bool | None = None,
) -> TranskribusResumeOnlyRecoveryAssessment:
    return TranskribusResumeOnlyRecoveryAssessment(
        document_id=document.pk,
        request_id=request.pk,
        run_id=run.pk,
        recognition_job_id=job_id,
        phase=None,
        apply_permitted=False,
        reason=reason,
        message=message,
        document_processing_state=document.processing_state_user,
        request_status=request.status,
        run_status=run.status,
        association_present=association_present,
        snapshot_id=snapshot.pk if snapshot is not None else None,
        remote_job_state=remote_job_state,
        remote_job_success=remote_job_success,
    )


def _permit(
    *,
    document: Document,
    request: ProcessDocumentRequest,
    run: TranskribusRun,
    job_id: str,
    phase: RecoveryPhase,
    message: str,
    association_present: bool = False,
    snapshot: TranskribusTranscriptSnapshot | None = None,
    remote_job_state: str | None = None,
    remote_job_success: bool | None = None,
) -> TranskribusResumeOnlyRecoveryAssessment:
    return TranskribusResumeOnlyRecoveryAssessment(
        document_id=document.pk,
        request_id=request.pk,
        run_id=run.pk,
        recognition_job_id=job_id,
        phase=phase,
        apply_permitted=True,
        reason=phase,
        message=message,
        document_processing_state=document.processing_state_user,
        request_status=request.status,
        run_status=run.status,
        association_present=association_present,
        snapshot_id=snapshot.pk if snapshot is not None else None,
        remote_job_state=remote_job_state,
        remote_job_success=remote_job_success,
    )


def assess_transkribus_resume_only_recovery(
    *,
    document_id: int,
    request_id: int,
    run_id: int,
    recognition_job_id: str | int,
    job_fetcher: JobFetcher | None = None,
) -> TranskribusResumeOnlyRecoveryAssessment:
    """Classify the recovery phase. Read-only aside from the optional job GET."""
    document, request, run, job_id = _load_chain(
        document_id=document_id,
        request_id=request_id,
        run_id=run_id,
        recognition_job_id=recognition_job_id,
    )
    _validate_upload_run(run)
    engine = _engine_runtime(run)
    is_hebrew = _is_hebrew(document.language)
    assoc, snapshot = _ready_association(run)
    association_present = assoc is not None
    usable_same_engine = _has_usable_source_for_engine(
        document_id=document.pk, engine=engine
    )
    verified = _has_verified(document.pk)

    if request.operation != ProcessDocumentRequest.Operation.OCR:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="REQUEST_OPERATION_NOT_OCR",
            message=(
                f"ProcessDocumentRequest id={request.pk} operation="
                f"{request.operation} is not OCR."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    local_done = False
    if association_present and snapshot is not None:
        try:
            local_done = local_completion_is_structurally_done(
                run=run, engine=engine, is_hebrew=is_hebrew
            )
        except TranskribusLocalCompletionError as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "AMBIGUOUS_SNAPSHOT_STATE", str(exc)
            ) from exc

    if request.status in {
        ProcessDocumentRequest.Status.COMPLETED,
        ProcessDocumentRequest.Status.PARTIAL,
    }:
        if (
            local_done
            and document.processing_state_user in ORDINARY_RESULT_PROCESSING_STATES
        ):
            return _permit(
                document=document,
                request=request,
                run=run,
                job_id=job_id,
                phase="ALREADY_DONE",
                message=(
                    f"Request id={request.pk} is already terminal and local "
                    "completion is structurally complete."
                ),
                association_present=True,
                snapshot=snapshot,
            )
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="REQUEST_ALREADY_TERMINAL",
            message=(
                f"ProcessDocumentRequest id={request.pk} status={request.status} "
                "is terminal without proven local completion for this run."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    if request.status == ProcessDocumentRequest.Status.FAILED:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="REQUEST_ALREADY_TERMINAL",
            message=(
                f"ProcessDocumentRequest id={request.pk} is FAILED and is not "
                "an idempotent successful resume."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    if request.status != ProcessDocumentRequest.Status.RECOVERY_REQUIRED:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="REQUEST_STATUS_NOT_PARKED_RECOVERY",
            message=(
                f"ProcessDocumentRequest id={request.pk} status={request.status} "
                "is not parked RECOVERY_REQUIRED."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    if not _recovery_shape_ok(request):
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="INVALID_RECOVERY_SHAPE",
            message=(
                f"ProcessDocumentRequest id={request.pk} does not satisfy the "
                "RECOVERY_REQUIRED shape."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    # Failed historical SOURCE_TEXT for this engine is ignored. Usable same-engine
    # text without an association for this run is ambiguous.
    if not association_present and usable_same_engine:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="AMBIGUOUS_SNAPSHOT_STATE",
            message=(
                f"Document id={document.pk} already has usable SOURCE_TEXT for "
                f"engine={engine!r} without a TranskribusRunAutomaticSnapshot "
                f"for run id={run.pk}."
            ),
        )

    if local_done:
        if document.processing_state_user in ORDINARY_RESULT_PROCESSING_STATES:
            return _permit(
                document=document,
                request=request,
                run=run,
                job_id=job_id,
                phase="TERMINALIZE_ONLY",
                message=(
                    "Local completion is done; staff terminalize can close the "
                    "parked Request without provider I/O."
                ),
                association_present=True,
                snapshot=snapshot,
            )
        return _permit(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            phase="COMPLETE_FROM_SNAPSHOT",
            message=(
                "Bindings are complete but Document is not ordinary; re-run "
                "local-success rollup then terminalize."
            ),
            association_present=True,
            snapshot=snapshot,
        )

    if verified:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="VERIFIED_WRITE_FENCE",
            message=(
                f"Document id={document.pk} has a VERIFIED DocumentTextResult; "
                "resume-only OCR persistence must not overwrite it."
            ),
            association_present=association_present,
            snapshot=snapshot,
        )

    if association_present and snapshot is not None:
        bind_status = inspect_local_completion_bindings(
            document_id=document.pk,
            engine=engine,
            snapshot=snapshot,
            is_hebrew=is_hebrew,
        )
        if bind_status.corrupt:
            return _refuse(
                document=document,
                request=request,
                run=run,
                job_id=job_id,
                reason="AMBIGUOUS_SNAPSHOT_STATE",
                message=f"Corrupt Transkribus bindings for run id={run.pk}.",
                association_present=True,
                snapshot=snapshot,
            )
        return _permit(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            phase="COMPLETE_FROM_SNAPSHOT",
            message=(
                f"Run id={run.pk} has a READY automatic snapshot; complete DTR "
                "bind without re-fetching PAGE XML."
            ),
            association_present=True,
            snapshot=snapshot,
        )

    if run.status != TranskribusRun.Status.RECOGNITION_STARTED:
        return _refuse(
            document=document,
            request=request,
            run=run,
            job_id=job_id,
            reason="RUN_STATUS_NOT_RESUMEABLE",
            message=(
                f"TranskribusRun id={run.pk} status={run.status} is not "
                "RECOGNITION_STARTED and has no READY association."
            ),
        )

    try:
        remote_state, remote_success = _require_remote_job_success(
            job_id=job_id, job_fetcher=job_fetcher
        )
    except TranskribusResumeOnlyRecoveryError as exc:
        if exc.code in {
            "REMOTE_JOB_NOT_SUCCESS",
            "REMOTE_JOB_EVIDENCE_REQUIRED",
            "REMOTE_JOB_FETCH_FAILED",
        }:
            return _refuse(
                document=document,
                request=request,
                run=run,
                job_id=job_id,
                reason=exc.code,
                message=exc.message,
            )
        raise

    return _permit(
        document=document,
        request=request,
        run=run,
        job_id=job_id,
        phase="FETCH_AND_COMPLETE",
        message=(
            "Remote recognition job finished; staff apply may fetch PAGE XML, "
            "store snapshot, persist DTRs, and terminalize."
        ),
        remote_job_state=remote_state,
        remote_job_success=remote_success,
    )


def _associate_snapshot_under_staff_resume_lock(
    *,
    document_id: int,
    request_id: int,
    run_id: int,
    recognition_job_id: str,
    snapshot: TranskribusTranscriptSnapshot,
    mapping_trusted: bool,
    review_reasons: list[str],
) -> TranskribusTranscriptSnapshot:
    """Associate a READY snapshot only while the parked staff target still holds.

    S3/store must already be finished. This short transaction is the protected
    association boundary; it does not claim to protect prior network I/O.
    If the Request was abandoned or the run changed, raise and leave any
    unassociated READY snapshot as a recoverable intermediate (no DTR write).
    """
    reasons = list(review_reasons or [])
    if not reasons:
        reasons = reconstruct_engine_review_reasons_from_snapshot(snapshot)

    with transaction.atomic():
        document = Document.objects.select_for_update().get(pk=document_id)
        if not staff_resume_only_persist_is_allowed(
            document=document,
            request_id=request_id,
            run_id=run_id,
            recognition_job_id=recognition_job_id,
        ):
            raise TranskribusResumeOnlyRecoveryError(
                "REQUEST_STATUS_NOT_PARKED_RECOVERY",
                "Parked request/run/job is no longer valid for snapshot association.",
            )
        if _has_verified(document.pk):
            raise TranskribusResumeOnlyRecoveryError(
                "VERIFIED_WRITE_FENCE",
                "Document gained a VERIFIED text result before association.",
            )
        run = TranskribusRun.objects.select_for_update().get(pk=run_id)
        if run.status != TranskribusRun.Status.RECOGNITION_STARTED:
            raise TranskribusResumeOnlyRecoveryError(
                "RUN_STATUS_NOT_RESUMEABLE",
                f"Run status={run.status} before association; refusing associate.",
            )
        existing = get_run_automatic_snapshot_association(run)
        if existing is not None:
            if existing.snapshot_id != snapshot.pk:
                raise TranskribusResumeOnlyRecoveryError(
                    "AMBIGUOUS_SNAPSHOT_STATE",
                    f"Run id={run.pk} already associated with snapshot "
                    f"id={existing.snapshot_id}, not id={snapshot.pk}.",
                )
            return snapshot
        associate_run_with_automatic_snapshot(
            run=run,
            snapshot=snapshot,
            mapping_trusted=mapping_trusted,
            review_reasons=reasons,
        )
        return snapshot


def _htr_inputs_from_snapshot(
    *,
    run: TranskribusRun,
    snapshot: TranskribusTranscriptSnapshot,
) -> tuple[str, list[str], bool]:
    assoc = get_run_automatic_snapshot_association(run)
    reasons = list(assoc.review_reasons or []) if assoc is not None else []
    if not reasons:
        reasons = reconstruct_engine_review_reasons_from_snapshot(snapshot)
    return snapshot.canonical_text or "", reasons, bool(reasons)


def _persist_and_terminalize(
    *,
    document: Document,
    request_id: int,
    run: TranskribusRun,
    snapshot: TranskribusTranscriptSnapshot,
    text: str,
    review_reasons: list[str],
    needs_review: bool,
    recognition_job_id: str,
) -> None:
    """Persist local success and terminalize in one DB transaction.

    Both callees use nested ``transaction.atomic()`` + Document→Request→Run
    locks. An outer atomic keeps those locks until final commit so staff
    abandon cannot observe a SUCCEEDED run / DTR with a still-parked or
    abandoned Request mid-flight. Search-index sync inside complete is
    Postgres-only and rolls back with this transaction. No Transkribus/S3 I/O.
    """
    with transaction.atomic():
        complete_transkribus_local_success(
            document_id=document.pk,
            run_id=run.pk,
            snapshot_id=snapshot.pk,
            text=text,
            engine=_engine_runtime(run),
            route=_route_for_document(document),
            needs_review=needs_review,
            review_reasons=review_reasons,
            min_text_length=_min_text_length(),
            pre_run_processing_state=Document.ProcessingState.RECOVERY_REQUIRED,
            staff_resume_request_id=request_id,
            staff_resume_recognition_job_id=recognition_job_id,
        )
        document.refresh_from_db()
        if not terminalize_parked_process_document_after_staff_resume(
            request_id=request_id,
            document_id=document.pk,
            run_id=run.pk,
            recognition_job_id=recognition_job_id,
        ):
            raise TranskribusResumeOnlyRecoveryError(
                "TERMINALIZE_REFUSED",
                "Staff resume terminalize refused after local completion; "
                "Document/Request/run state changed.",
            )


def apply_transkribus_resume_only_recovery(
    *,
    document_id: int,
    request_id: int,
    run_id: int,
    recognition_job_id: str | int,
    job_fetcher: JobFetcher | None = None,
    page_completer: PageCompleter | None = None,
) -> TranskribusResumeOnlyRecoveryAssessment:
    """Apply the classified phase. Never starts recognition or uses lease_token."""
    assessment = assess_transkribus_resume_only_recovery(
        document_id=document_id,
        request_id=request_id,
        run_id=run_id,
        recognition_job_id=recognition_job_id,
        job_fetcher=job_fetcher,
    )
    if not assessment.apply_permitted or assessment.phase is None:
        raise TranskribusResumeOnlyRecoveryError(assessment.reason, assessment.message)

    document, request, run, job_id = _load_chain(
        document_id=document_id,
        request_id=request_id,
        run_id=run_id,
        recognition_job_id=recognition_job_id,
    )

    if assessment.phase == "ALREADY_DONE":
        logger.info(
            "event=staff_resume_only_apply phase=ALREADY_DONE document_id=%s "
            "request_id=%s run_id=%s",
            document.pk,
            request.pk,
            run.pk,
        )
        return assessment

    if assessment.phase == "TERMINALIZE_ONLY":
        if not terminalize_parked_process_document_after_staff_resume(
            request_id=request.pk,
            document_id=document.pk,
            run_id=run.pk,
            recognition_job_id=job_id,
        ):
            raise TranskribusResumeOnlyRecoveryError(
                "TERMINALIZE_REFUSED",
                "Staff resume terminalize refused; state changed under lock.",
            )
        logger.info(
            "event=staff_resume_only_apply phase=TERMINALIZE_ONLY document_id=%s "
            "request_id=%s run_id=%s",
            document.pk,
            request.pk,
            run.pk,
        )
        # Terminal request → ALREADY_DONE without remote job evidence.
        return assess_transkribus_resume_only_recovery(
            document_id=document_id,
            request_id=request_id,
            run_id=run_id,
            recognition_job_id=job_id,
        )

    if assessment.phase == "FETCH_AND_COMPLETE":
        if page_completer is None:
            raise TranskribusResumeOnlyRecoveryError(
                "PAGE_COMPLETER_REQUIRED",
                "FETCH_AND_COMPLETE apply requires a page_completer that fetches "
                "PAGE XML for the existing recognition job (no start_pylaia).",
            )
        _require_remote_job_success(job_id=job_id, job_fetcher=job_fetcher)
        try:
            outcome = page_completer(run)
        except Exception as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "PAGE_FETCH_FAILED",
                f"Failed to complete PAGE XML fetch for job id={job_id}: {exc}",
            ) from exc

        # PAGE XML / S3 are intentionally outside DB locks. Association below
        # is the protected write; a READY snapshot without association is a
        # recoverable intermediate if the parked target disappeared.
        try:
            page_inputs = snapshot_pages_from_upload_mapping(
                outcome.selected_pages,
                run.page_index_to_page_nr,
            )
            snapshot = store_automatic_snapshot_from_selected_pages(
                document=document,
                run=run,
                page_inputs=page_inputs,
                mapping_trusted=True,
                review_reasons=list(outcome.review_reasons),
                create_association=False,
            )
        except TranskribusLocalPersistenceRetryableError as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "LOCAL_PERSISTENCE_RETRYABLE", str(exc)
            ) from exc
        except (
            TranskribusPageMappingError,
            TranskribusSnapshotStorageValidationError,
            TranskribusLocalCompletionError,
        ) as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "SNAPSHOT_STORE_FAILED", str(exc)
            ) from exc

        reasons = list(outcome.review_reasons or [])
        try:
            snapshot = _associate_snapshot_under_staff_resume_lock(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=job_id,
                snapshot=snapshot,
                mapping_trusted=True,
                review_reasons=reasons,
            )
        except TranskribusLocalCompletionError as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "SNAPSHOT_ASSOCIATION_FAILED", str(exc)
            ) from exc

        text = outcome.text or ""
        if not reasons:
            reasons = reconstruct_engine_review_reasons_from_snapshot(snapshot)
        try:
            _persist_and_terminalize(
                document=document,
                request_id=request.pk,
                run=run,
                snapshot=snapshot,
                text=text,
                review_reasons=reasons,
                needs_review=bool(reasons),
                recognition_job_id=job_id,
            )
        except TranskribusLocalCompletionError as exc:
            raise TranskribusResumeOnlyRecoveryError(
                "LOCAL_COMPLETION_FAILED", str(exc)
            ) from exc
        logger.info(
            "event=staff_resume_only_apply phase=FETCH_AND_COMPLETE "
            "document_id=%s request_id=%s run_id=%s snapshot_id=%s",
            document.pk,
            request.pk,
            run.pk,
            snapshot.pk,
        )
        return assess_transkribus_resume_only_recovery(
            document_id=document_id,
            request_id=request_id,
            run_id=run_id,
            recognition_job_id=job_id,
        )

    # COMPLETE_FROM_SNAPSHOT
    _assoc, snapshot = _ready_association(run)
    if snapshot is None:
        raise TranskribusResumeOnlyRecoveryError(
            "SNAPSHOT_NOT_READY",
            f"Run id={run.pk} lost its READY association before complete.",
        )
    text, reasons, needs_review = _htr_inputs_from_snapshot(run=run, snapshot=snapshot)
    try:
        _persist_and_terminalize(
            document=document,
            request_id=request.pk,
            run=run,
            snapshot=snapshot,
            text=text,
            review_reasons=reasons,
            needs_review=needs_review,
            recognition_job_id=job_id,
        )
    except TranskribusLocalCompletionError as exc:
        raise TranskribusResumeOnlyRecoveryError(
            "LOCAL_COMPLETION_FAILED", str(exc)
        ) from exc
    logger.info(
        "event=staff_resume_only_apply phase=COMPLETE_FROM_SNAPSHOT "
        "document_id=%s request_id=%s run_id=%s snapshot_id=%s",
        document.pk,
        request.pk,
        run.pk,
        snapshot.pk,
    )
    return assess_transkribus_resume_only_recovery(
        document_id=document_id,
        request_id=request_id,
        run_id=run_id,
        recognition_job_id=job_id,
    )


__all__ = [
    "JobFetcher",
    "PageCompleter",
    "RecoveryPhase",
    "TranskribusResumeOnlyRecoveryAssessment",
    "TranskribusResumeOnlyRecoveryError",
    "apply_transkribus_resume_only_recovery",
    "assess_transkribus_resume_only_recovery",
]
