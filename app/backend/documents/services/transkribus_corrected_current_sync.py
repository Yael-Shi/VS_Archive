"""Staff corrected/current Transkribus sync orchestration (transport-agnostic).

Fetches provider metadata once, runs the pure v1 selector, optionally stores a
CORRECTED_CURRENT_SYNC snapshot, and records provenance on
``TranskribusCorrectedCurrentSyncAttempt`` / ``Page`` rows.

Does not activate geometry, mutate automatic HTR associations, or update
``DocumentTextResult`` / bindings / processing status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, NoReturn, Sequence

import requests
from django.db import transaction
from django.utils import timezone

from documents.models import (
    Document,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.services import transkribus_engine as tr
from documents.services.transkribus_corrected_current_selection import (
    CorrectedCurrentPageInput,
    CorrectedCurrentPageSelectionError,
    CorrectedCurrentTranscriptSelection,
    select_corrected_current_transcripts_for_document,
)
from documents.services.transkribus_page_xml_geometry import (
    TranskribusPageXmlGeometryError,
    resolve_audit_transkribus_run,
    resolve_page_indices_to_audit,
)
from documents.services.transkribus_page_xml_types import SelectedTranscriptPage
from documents.services.transkribus_snapshot_pages import (
    TranskribusPageMappingError,
    normalize_page_index_to_page_nr,
    snapshot_pages_from_upload_mapping,
)
from documents.services.transkribus_snapshot_parser import TranskribusSnapshotParseError
from documents.services.transkribus_snapshot_storage import (
    SnapshotStorageOutcome,
    SnapshotStorageResult,
    TranskribusSnapshotStorageError,
    store_transkribus_transcript_snapshot,
)

logger = logging.getLogger(__name__)

_MAX_SAFE_MESSAGE_LEN = 512


class CorrectedCurrentSyncFailureCode:
    RUN_RESOLUTION = "RUN_RESOLUTION_FAILED"
    HTTP_METADATA = "HTTP_METADATA_FAILED"
    HTTP_TRANSCRIPT_XML = "HTTP_TRANSCRIPT_XML_FAILED"
    PAGE_MAPPING = "PAGE_MAPPING_FAILED"
    SNAPSHOT_STORAGE = "SNAPSHOT_STORAGE_FAILED"
    SNAPSHOT_PARSE = "SNAPSHOT_PARSE_FAILED"
    SNAPSHOT_PAGE_MISMATCH = "SNAPSHOT_PAGE_MISMATCH"
    UNEXPECTED = "UNEXPECTED_FAILURE"


_PUBLIC_FAILURE_MESSAGES: dict[str, str] = {
    CorrectedCurrentSyncFailureCode.RUN_RESOLUTION: (
        "Corrected/current sync could not resolve a trusted Transkribus run."
    ),
    CorrectedCurrentSyncFailureCode.HTTP_METADATA: (
        "Transkribus login or pages metadata request failed."
    ),
    CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML: (
        "Transkribus transcript PAGE XML request failed."
    ),
    CorrectedCurrentSyncFailureCode.PAGE_MAPPING: (
        "Trusted page mapping or snapshot page inputs were invalid."
    ),
    CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE: (
        "Transkribus transcript snapshot storage failed."
    ),
    CorrectedCurrentSyncFailureCode.SNAPSHOT_PARSE: (
        "Transkribus PAGE XML parsing for snapshot storage failed."
    ),
    CorrectedCurrentSyncFailureCode.SNAPSHOT_PAGE_MISMATCH: (
        "Stored snapshot pages did not match sync attempt selections."
    ),
    CorrectedCurrentSyncFailureCode.UNEXPECTED: (
        "Corrected/current sync failed unexpectedly."
    ),
}


class _SyncPhase(str, Enum):
    RESOLVE = "resolve"
    HTTP_LOGIN_METADATA = "http_login_metadata"
    BUILD_SELECTOR = "build_selector"
    SELECT = "select"
    PERSIST_REFUSED = "persist_refused"
    PERSIST_SELECTED = "persist_selected"
    HTTP_TRANSCRIPT_XML = "http_transcript_xml"
    MAP_SNAPSHOT_INPUTS = "map_snapshot_inputs"
    SNAPSHOT_STORAGE = "snapshot_storage"
    VERIFY_COMPLETE = "verify_complete"


class CorrectedCurrentSyncError(Exception):
    """Corrected/current sync failed (safe public message only)."""

    def __init__(
        self,
        message: str,
        *,
        attempt_id: int | None = None,
        failure_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.failure_code = failure_code


class CorrectedCurrentSyncTerminalConflictError(CorrectedCurrentSyncError):
    """Terminal attempt transition rejected (wrong state or conflicting payload)."""


class CorrectedCurrentSyncPageMetadataError(Exception):
    """Pages metadata lookup failed for a mapped pageNr."""


@dataclass(frozen=True)
class CorrectedCurrentSyncResult:
    attempt: TranskribusCorrectedCurrentSyncAttempt
    refused: bool
    snapshot: TranskribusTranscriptSnapshot | None
    storage_outcome: SnapshotStorageOutcome | None


def _public_message(failure_code: str) -> str:
    return _PUBLIC_FAILURE_MESSAGES.get(
        failure_code,
        _PUBLIC_FAILURE_MESSAGES[CorrectedCurrentSyncFailureCode.UNEXPECTED],
    )


def _bound_safe_message(message: str, *, max_len: int = _MAX_SAFE_MESSAGE_LEN) -> str:
    text = (message or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _page_metadata_by_page_nr(
    pages_meta: Sequence[tr.TrpPageMetadata],
    page_nr: int,
) -> tr.TrpPageMetadata:
    matches = [pm for pm in pages_meta if pm.page_nr == page_nr]
    if not matches:
        raise CorrectedCurrentSyncPageMetadataError(
            f"Transkribus pages metadata missing pageNr={page_nr}."
        )
    if len(matches) > 1:
        raise CorrectedCurrentSyncPageMetadataError(
            f"Transkribus pages metadata returned duplicate pageNr={page_nr}."
        )
    return matches[0]


def _resolve_document_run_and_mapping(
    document_id: int,
) -> tuple[Document, TranskribusRun, dict[int, int]]:
    try:
        document = Document.objects.get(pk=document_id)
        run = resolve_audit_transkribus_run(document_id)
        mapping = normalize_page_index_to_page_nr(run.page_index_to_page_nr)
    except Document.DoesNotExist:
        raise CorrectedCurrentSyncError(
            _public_message(CorrectedCurrentSyncFailureCode.RUN_RESOLUTION),
            failure_code=CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
            attempt_id=None,
        ) from None
    except (TranskribusPageXmlGeometryError, TranskribusPageMappingError) as exc:
        logger.info(
            "Corrected/current run resolution failed doc_id=%s exception_class=%s",
            document_id,
            type(exc).__name__,
        )
        raise CorrectedCurrentSyncError(
            _public_message(CorrectedCurrentSyncFailureCode.RUN_RESOLUTION),
            failure_code=CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
            attempt_id=None,
        ) from None
    if run.document_id != document.pk:
        raise CorrectedCurrentSyncError(
            _public_message(CorrectedCurrentSyncFailureCode.RUN_RESOLUTION),
            failure_code=CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
            attempt_id=None,
        )
    return document, run, mapping


def _create_started_attempt(
    *,
    document: Document,
    run: TranskribusRun,
    initiated_by: Any,
) -> TranskribusCorrectedCurrentSyncAttempt:
    if initiated_by is None:
        raise CorrectedCurrentSyncError(
            _public_message(CorrectedCurrentSyncFailureCode.UNEXPECTED),
            failure_code=CorrectedCurrentSyncFailureCode.UNEXPECTED,
            attempt_id=None,
        )
    attempt = TranskribusCorrectedCurrentSyncAttempt(
        document=document,
        transkribus_run=run,
        initiated_by=initiated_by,
        status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
    )
    attempt.save()
    return attempt


def _build_selector_inputs(
    *,
    page_index_map: Mapping[int, int],
    pages_meta: Sequence[tr.TrpPageMetadata],
) -> list[CorrectedCurrentPageInput]:
    page_indices = resolve_page_indices_to_audit(page_index_map, page_index=None)
    inputs: list[CorrectedCurrentPageInput] = []
    for page_index in page_indices:
        page_nr = page_index_map[page_index]
        pm = _page_metadata_by_page_nr(pages_meta, page_nr)
        inputs.append(
            CorrectedCurrentPageInput(
                page_index=page_index,
                page_nr=page_nr,
                raw_transcripts=tuple(pm.transcripts),
            )
        )
    return inputs


def _find_raw_transcript_by_ts_id(
    pages_meta: Sequence[tr.TrpPageMetadata],
    *,
    page_nr: int,
    transcript_ts_id: str,
) -> Mapping[str, Any]:
    pm = _page_metadata_by_page_nr(pages_meta, page_nr)
    target = str(transcript_ts_id).strip()
    for raw in pm.transcripts:
        ts_raw = raw.get("tsId")
        ts_id = str(ts_raw).strip() if ts_raw is not None else ""
        if ts_id == target:
            return raw
    raise CorrectedCurrentSyncPageMetadataError(
        f"Selected transcript tsId not found in metadata for pageNr={page_nr}."
    )


def _fetch_selected_transcript_pages(
    *,
    selections: Sequence[CorrectedCurrentTranscriptSelection],
    pages_meta: Sequence[tr.TrpPageMetadata],
    fetch_transcript_xml: Callable[..., bytes],
    bearer_token: str,
) -> list[SelectedTranscriptPage]:
    selected: list[SelectedTranscriptPage] = []
    for sel in sorted(selections, key=lambda s: s.page_index):
        raw = _find_raw_transcript_by_ts_id(
            pages_meta,
            page_nr=sel.page_nr,
            transcript_ts_id=sel.transcript_ts_id,
        )
        url = raw.get("url")
        if not url or not isinstance(url, str):
            raise CorrectedCurrentSyncPageMetadataError(
                f"Transcript URL missing for pageNr={sel.page_nr}."
            )
        xml_bytes = fetch_transcript_xml(url, bearer_token=bearer_token)
        pm = _page_metadata_by_page_nr(pages_meta, sel.page_nr)
        selected.append(
            SelectedTranscriptPage(
                page_nr=sel.page_nr,
                transcript_ts_id=sel.transcript_ts_id,
                page_xml=xml_bytes,
                url=url,
                provider_page_id=pm.page_id,
                remote_transcript_status=sel.remote_transcript_status,
            )
        )
    return selected


def _terminal_payload_matches(
    attempt: TranskribusCorrectedCurrentSyncAttempt,
    *,
    target_status: str,
    resolved_snapshot_id: int | None,
    storage_outcome: str | None,
    failure_code: str | None,
    failure_message: str | None,
) -> bool:
    if attempt.status != target_status:
        return False
    if target_status == TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED:
        return (
            attempt.resolved_snapshot_id == resolved_snapshot_id
            and attempt.storage_outcome == storage_outcome
            and attempt.failure_code is None
            and attempt.failure_message is None
        )
    if target_status == TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED:
        return (
            attempt.resolved_snapshot_id is None
            and attempt.storage_outcome is None
            and attempt.failure_code is None
            and attempt.failure_message is None
        )
    if target_status == TranskribusCorrectedCurrentSyncAttempt.Status.FAILED:
        return (
            attempt.resolved_snapshot_id is None
            and attempt.storage_outcome is None
            and attempt.failure_code == failure_code
            and attempt.failure_message == failure_message
        )
    return False


def _transition_attempt_terminal(
    attempt_id: int,
    *,
    target_status: str,
    resolved_snapshot: TranskribusTranscriptSnapshot | None = None,
    storage_outcome: str | None = None,
    failure_code: str | None = None,
    failure_message: str | None = None,
) -> TranskribusCorrectedCurrentSyncAttempt:
    completed_at = timezone.now()
    resolved_snapshot_id = resolved_snapshot.pk if resolved_snapshot else None
    safe_failure_message = (
        _bound_safe_message(failure_message) if failure_message else None
    )

    with transaction.atomic():
        attempt = (
            TranskribusCorrectedCurrentSyncAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
        )
        if attempt.status == target_status:
            if _terminal_payload_matches(
                attempt,
                target_status=target_status,
                resolved_snapshot_id=resolved_snapshot_id,
                storage_outcome=storage_outcome,
                failure_code=failure_code,
                failure_message=safe_failure_message,
            ):
                return attempt
            raise CorrectedCurrentSyncTerminalConflictError(
                _public_message(CorrectedCurrentSyncFailureCode.UNEXPECTED),
                attempt_id=attempt_id,
                failure_code=CorrectedCurrentSyncFailureCode.UNEXPECTED,
            )
        if attempt.status != TranskribusCorrectedCurrentSyncAttempt.Status.STARTED:
            raise CorrectedCurrentSyncTerminalConflictError(
                _public_message(CorrectedCurrentSyncFailureCode.UNEXPECTED),
                attempt_id=attempt_id,
                failure_code=CorrectedCurrentSyncFailureCode.UNEXPECTED,
            )

        attempt.status = target_status
        attempt.completed_at = completed_at
        attempt.resolved_snapshot = resolved_snapshot
        attempt.storage_outcome = storage_outcome
        attempt.failure_code = failure_code
        attempt.failure_message = safe_failure_message
        attempt.save(
            update_fields=[
                "status",
                "completed_at",
                "resolved_snapshot",
                "storage_outcome",
                "failure_code",
                "failure_message",
                "updated_at",
            ]
        )
        return attempt


def _persist_refused_pages(
    attempt_id: int,
    page_errors: Sequence[CorrectedCurrentPageSelectionError],
) -> None:
    with transaction.atomic():
        attempt = (
            TranskribusCorrectedCurrentSyncAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
        )
        if attempt.status != TranskribusCorrectedCurrentSyncAttempt.Status.STARTED:
            raise CorrectedCurrentSyncTerminalConflictError(
                _public_message(CorrectedCurrentSyncFailureCode.UNEXPECTED),
                attempt_id=attempt_id,
            )
        for err in page_errors:
            TranskribusCorrectedCurrentSyncPage.objects.create(
                attempt=attempt,
                page_index=err.page_index,
                page_nr=err.page_nr,
                outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED,
                selection_error_code=err.code,
                selection_error_message=_bound_safe_message(err.message),
            )
    _transition_attempt_terminal(
        attempt_id,
        target_status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
    )


def _persist_selected_pages(
    attempt_id: int,
    selections: Sequence[CorrectedCurrentTranscriptSelection],
) -> None:
    with transaction.atomic():
        attempt = (
            TranskribusCorrectedCurrentSyncAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
        )
        if attempt.status != TranskribusCorrectedCurrentSyncAttempt.Status.STARTED:
            raise CorrectedCurrentSyncTerminalConflictError(
                _public_message(CorrectedCurrentSyncFailureCode.UNEXPECTED),
                attempt_id=attempt_id,
            )
        for sel in selections:
            TranskribusCorrectedCurrentSyncPage.objects.create(
                attempt=attempt,
                page_index=sel.page_index,
                page_nr=sel.page_nr,
                outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
                transcript_ts_id=sel.transcript_ts_id,
                remote_transcript_status=sel.remote_transcript_status or "",
                in_progress_warning=sel.in_progress_warning is not None,
            )


class _SnapshotPageMismatchError(Exception):
    """Attempt page rows do not match READY snapshot pages."""


def _verify_snapshot_matches_attempt_pages(
    attempt: TranskribusCorrectedCurrentSyncAttempt,
    snapshot: TranskribusTranscriptSnapshot,
) -> None:
    if snapshot.document_id != attempt.document_id:
        raise _SnapshotPageMismatchError("document mismatch")
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise _SnapshotPageMismatchError("snapshot not READY")

    selected_pages = list(
        attempt.pages.filter(
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED
        ).order_by("page_index")
    )
    snapshot_pages = list(
        TranskribusSnapshotPage.objects.filter(snapshot=snapshot).order_by("page_index")
    )
    if len(selected_pages) != len(snapshot_pages):
        raise _SnapshotPageMismatchError("page count mismatch")
    for attempt_page, snap_page in zip(selected_pages, snapshot_pages, strict=True):
        if (
            attempt_page.page_index != snap_page.page_index
            or attempt_page.page_nr != snap_page.page_nr
            or attempt_page.transcript_ts_id != snap_page.transcript_ts_id
        ):
            raise _SnapshotPageMismatchError("page field mismatch")


def _complete_attempt_with_snapshot(
    attempt_id: int,
    *,
    snapshot: TranskribusTranscriptSnapshot,
    storage_outcome: SnapshotStorageOutcome,
) -> TranskribusCorrectedCurrentSyncAttempt:
    attempt = TranskribusCorrectedCurrentSyncAttempt.objects.get(pk=attempt_id)
    _verify_snapshot_matches_attempt_pages(attempt, snapshot)
    return _transition_attempt_terminal(
        attempt_id,
        target_status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
        resolved_snapshot=snapshot,
        storage_outcome=storage_outcome.value,
    )


def _fail_attempt_best_effort(
    attempt_id: int,
    *,
    failure_code: str,
) -> None:
    try:
        _transition_attempt_terminal(
            attempt_id,
            target_status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code=failure_code,
            failure_message=_public_message(failure_code),
        )
    except CorrectedCurrentSyncTerminalConflictError:
        logger.warning(
            "Corrected/current sync attempt id=%s could not transition to FAILED "
            "(terminal state already set) failure_code=%s",
            attempt_id,
            failure_code,
        )
    except Exception as update_exc:
        logger.warning(
            "Corrected/current sync attempt id=%s FAILED transition update failed "
            "failure_code=%s exception_class=%s",
            attempt_id,
            failure_code,
            type(update_exc).__name__,
        )


def _classify_exception(
    exc: BaseException,
    *,
    phase: _SyncPhase,
) -> str:
    if isinstance(exc, TranskribusSnapshotParseError):
        return CorrectedCurrentSyncFailureCode.SNAPSHOT_PARSE
    if isinstance(exc, TranskribusSnapshotStorageError):
        return CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE
    if isinstance(exc, (TranskribusPageMappingError, TranskribusPageXmlGeometryError)):
        return CorrectedCurrentSyncFailureCode.PAGE_MAPPING
    if isinstance(exc, ValueError) and phase in (
        _SyncPhase.BUILD_SELECTOR,
        _SyncPhase.MAP_SNAPSHOT_INPUTS,
    ):
        return CorrectedCurrentSyncFailureCode.PAGE_MAPPING
    if isinstance(exc, CorrectedCurrentSyncPageMetadataError):
        return (
            CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML
            if phase == _SyncPhase.HTTP_TRANSCRIPT_XML
            else CorrectedCurrentSyncFailureCode.HTTP_METADATA
        )
    if isinstance(
        exc,
        (
            tr.TranskribusPermanentError,
            tr.TranskribusRetryableError,
            requests.RequestException,
        ),
    ):
        return (
            CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML
            if phase == _SyncPhase.HTTP_TRANSCRIPT_XML
            else CorrectedCurrentSyncFailureCode.HTTP_METADATA
        )
    return CorrectedCurrentSyncFailureCode.UNEXPECTED


def _handle_failure(
    *,
    attempt_id: int | None,
    failure_code: str,
    cause: BaseException | None = None,
) -> None:
    if cause is None:
        logger.error(
            "Corrected/current sync failure code=%s attempt_id=%s",
            failure_code,
            attempt_id,
        )
    else:
        logger.error(
            "Corrected/current sync failure code=%s attempt_id=%s exception_class=%s",
            failure_code,
            attempt_id,
            type(cause).__name__,
        )
    if attempt_id is None:
        return
    _fail_attempt_best_effort(attempt_id, failure_code=failure_code)


def _reraise_sync_error(
    *,
    attempt_id: int | None,
    failure_code: str,
) -> NoReturn:
    raise CorrectedCurrentSyncError(
        _public_message(failure_code),
        attempt_id=attempt_id,
        failure_code=failure_code,
    ) from None


def run_corrected_current_transkribus_sync(
    *,
    document_id: int,
    initiated_by: Any,
    username: str,
    password: str,
    bearer_token: str,
    session_factory: Callable[[], requests.Session] | None = None,
    login: Callable[..., None] | None = None,
    fetch_pages_metadata: Callable[..., list[tr.TrpPageMetadata]] | None = None,
    fetch_transcript_xml: Callable[..., bytes] | None = None,
    store_snapshot: Callable[
        ..., SnapshotStorageResult
    ] = store_transkribus_transcript_snapshot,
) -> CorrectedCurrentSyncResult:
    """Run one corrected/current sync for ``document_id`` (new attempt each call)."""
    document, run, page_index_map = _resolve_document_run_and_mapping(document_id)
    attempt = _create_started_attempt(
        document=document,
        run=run,
        initiated_by=initiated_by,
    )
    attempt_id = attempt.pk
    phase = _SyncPhase.HTTP_LOGIN_METADATA

    session_factory = session_factory or requests.Session
    login_fn = login or tr.login_trp_server
    fetch_pages_fn = fetch_pages_metadata or tr.fetch_pages_metadata
    fetch_xml_fn = fetch_transcript_xml or tr.fetch_transcript_xml

    try:
        with session_factory() as session:
            login_fn(session, username=username, password=password)
            pages_meta = fetch_pages_fn(
                session,
                collection_id=run.collection_id,
                document_id=str(run.remote_doc_id).strip(),
                pages_query=str(run.pages_query).strip(),
            )
        if not pages_meta:
            raise CorrectedCurrentSyncPageMetadataError(
                "Transkribus pages metadata returned empty list."
            )

        phase = _SyncPhase.BUILD_SELECTOR
        selector_inputs = _build_selector_inputs(
            page_index_map=page_index_map,
            pages_meta=pages_meta,
        )
        phase = _SyncPhase.SELECT
        selection = select_corrected_current_transcripts_for_document(selector_inputs)

        if selection.is_refused:
            assert selection.page_errors is not None
            phase = _SyncPhase.PERSIST_REFUSED
            _persist_refused_pages(attempt_id, selection.page_errors)
            attempt = TranskribusCorrectedCurrentSyncAttempt.objects.get(pk=attempt_id)
            return CorrectedCurrentSyncResult(
                attempt=attempt,
                refused=True,
                snapshot=None,
                storage_outcome=None,
            )

        assert selection.selections is not None
        phase = _SyncPhase.PERSIST_SELECTED
        _persist_selected_pages(attempt_id, selection.selections)

        phase = _SyncPhase.HTTP_TRANSCRIPT_XML
        selected_pages = _fetch_selected_transcript_pages(
            selections=selection.selections,
            pages_meta=pages_meta,
            fetch_transcript_xml=fetch_xml_fn,
            bearer_token=bearer_token,
        )
        phase = _SyncPhase.MAP_SNAPSHOT_INPUTS
        snapshot_inputs = snapshot_pages_from_upload_mapping(
            selected_pages,
            run.page_index_to_page_nr,
        )

        phase = _SyncPhase.SNAPSHOT_STORAGE
        # hover_eligible=None: use parser-derived eligibility from PAGE XML geometry.
        storage_result = store_snapshot(
            document=document,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
            pages=snapshot_inputs,
            transkribus_run=run,
            remote_doc_id=str(run.remote_doc_id or ""),
            collection_id=str(run.collection_id or ""),
            model_id=str(run.model_id or ""),
            recognition_job_id=str(run.recognition_job_id or ""),
            created_by=initiated_by,
            hover_eligible=None,
        )

        phase = _SyncPhase.VERIFY_COMPLETE
        attempt = _complete_attempt_with_snapshot(
            attempt_id,
            snapshot=storage_result.snapshot,
            storage_outcome=storage_result.outcome,
        )
        return CorrectedCurrentSyncResult(
            attempt=attempt,
            refused=False,
            snapshot=storage_result.snapshot,
            storage_outcome=storage_result.outcome,
        )

    except CorrectedCurrentSyncTerminalConflictError:
        raise
    except _SnapshotPageMismatchError as exc:
        code = CorrectedCurrentSyncFailureCode.SNAPSHOT_PAGE_MISMATCH
        _handle_failure(attempt_id=attempt_id, failure_code=code, cause=exc)
        _reraise_sync_error(attempt_id=attempt_id, failure_code=code)
    except Exception as exc:
        code = _classify_exception(exc, phase=phase)
        _handle_failure(attempt_id=attempt_id, failure_code=code, cause=exc)
        _reraise_sync_error(attempt_id=attempt_id, failure_code=code)


__all__ = [
    "CorrectedCurrentSyncError",
    "CorrectedCurrentSyncFailureCode",
    "CorrectedCurrentSyncResult",
    "CorrectedCurrentSyncTerminalConflictError",
    "run_corrected_current_transkribus_sync",
    "_transition_attempt_terminal",
    "_verify_snapshot_matches_attempt_pages",
]
