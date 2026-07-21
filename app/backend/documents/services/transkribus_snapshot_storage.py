"""Persist already-fetched Transkribus PAGE XML as an immutable transcript snapshot.

Callers supply PAGE XML bytes and provider metadata. This service parses via the
pure snapshot parser, writes normalized DB rows, and uploads raw PAGE XML to S3.
It does not fetch Transkribus metadata, select transcripts, create bindings, or
update DocumentTextResult rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn, Sequence

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Prefetch

from documents.models import (
    Document,
    TranskribusRun,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.s3 import (
    TRANSKRIBUS_SNAPSHOT_PAGE_XML_CONTENT_TYPE,
    build_transkribus_snapshot_page_xml_s3_key,
    delete_s3_object,
    put_object_bytes,
)
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION,
    ParsedSnapshotDocument,
    ParsedSnapshotLine,
    ParsedSnapshotPage,
    SnapshotPageInput,
    TranskribusSnapshotParseError,
    parse_document_pages_for_snapshot,
)

logger = logging.getLogger(__name__)


class SnapshotStorageOutcome(str, Enum):
    CREATED = "CREATED"
    REUSED_EXISTING = "REUSED_EXISTING"
    REUSED_CONCURRENT_WINNER = "REUSED_CONCURRENT_WINNER"


@dataclass(frozen=True)
class SnapshotStorageResult:
    outcome: SnapshotStorageOutcome
    snapshot: TranskribusTranscriptSnapshot

    @property
    def reused(self) -> bool:
        return self.outcome != SnapshotStorageOutcome.CREATED


class TranskribusSnapshotStorageError(Exception):
    """Base error for snapshot storage failures (safe message; no secrets/XML)."""


class TranskribusSnapshotStorageValidationError(TranskribusSnapshotStorageError):
    """Invalid caller input rejected before S3 uploads."""


class TranskribusSnapshotStorageUploadError(TranskribusSnapshotStorageError):
    """PAGE XML upload/finalization failed; snapshot is not READY.

    ``cleanup_errors`` and ``state_update_errors`` are secondary best-effort
    failure notes and must never replace the primary upload/finalization message.
    """

    def __init__(
        self,
        message: str,
        *,
        snapshot_id: int | None = None,
        cleanup_errors: tuple[str, ...] = (),
        state_update_errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.snapshot_id = snapshot_id
        self.cleanup_errors = cleanup_errors
        self.state_update_errors = state_update_errors


def _uploads_bucket() -> str:
    bucket = (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()
    if not bucket:
        raise TranskribusSnapshotStorageValidationError(
            "UPLOADS_BUCKET_NAME is not configured."
        )
    return bucket


def _validate_source_kind(source_kind: str) -> str:
    normalized = (source_kind or "").strip()
    valid = {choice.value for choice in TranskribusTranscriptSnapshot.SourceKind}
    if normalized not in valid:
        raise TranskribusSnapshotStorageValidationError(
            f"Invalid source_kind={source_kind!r}."
        )
    return normalized


def _require_positive_int(value: Any, *, field_name: str) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or type(value) is not int:
        raise TranskribusSnapshotStorageValidationError(
            f"{field_name} must be an int >= 1, got {value!r}."
        )
    if value < 1:
        raise TranskribusSnapshotStorageValidationError(
            f"{field_name} must be an int >= 1, got {value!r}."
        )
    return value


def _validate_page_inputs(
    pages: Sequence[SnapshotPageInput],
) -> list[SnapshotPageInput]:
    if not pages:
        raise TranskribusSnapshotStorageValidationError(
            "At least one PAGE XML page is required."
        )

    seen_indexes: set[int] = set()
    seen_page_nrs: set[int] = set()
    ordered: list[SnapshotPageInput] = []

    for page in pages:
        page_index = _require_positive_int(page.page_index, field_name="page_index")
        page_nr = _require_positive_int(page.page_nr, field_name="page_nr")
        if page_index in seen_indexes:
            raise TranskribusSnapshotStorageValidationError(
                f"Duplicate page_index={page_index}."
            )
        if page_nr in seen_page_nrs:
            raise TranskribusSnapshotStorageValidationError(
                f"Duplicate page_nr={page_nr}."
            )
        ts_id = str(page.transcript_ts_id).strip()
        if not ts_id:
            raise TranskribusSnapshotStorageValidationError(
                f"transcript_ts_id must be non-empty for page_index={page_index}."
            )
        if not isinstance(page.page_xml, (bytes, bytearray)) or not page.page_xml:
            raise TranskribusSnapshotStorageValidationError(
                f"page_xml must be non-empty bytes for page_index={page_index}."
            )
        seen_indexes.add(page_index)
        seen_page_nrs.add(page_nr)
        ordered.append(
            SnapshotPageInput(
                page_index=page_index,
                page_nr=page_nr,
                transcript_ts_id=ts_id,
                page_xml=bytes(page.page_xml),
                provider_page_id=page.provider_page_id,
                remote_transcript_status=page.remote_transcript_status,
            )
        )

    return ordered


def _validate_transkribus_run(
    *,
    document: Document,
    transkribus_run: TranskribusRun | None,
) -> TranskribusRun | None:
    if transkribus_run is None:
        return None
    if transkribus_run.document_id != document.pk:
        raise TranskribusSnapshotStorageValidationError(
            "TranskribusRun belongs to a different document."
        )
    return transkribus_run


def _points_to_json(
    points: tuple[tuple[float, float], ...],
) -> list[list[float]] | None:
    if not points:
        return None
    return [[float(x), float(y)] for x, y in points]


def _build_remote_status_summary(
    pages: Sequence[ParsedSnapshotPage],
) -> dict[str, Any] | None:
    entries: list[dict[str, Any]] = []
    for page in pages:
        status = (page.remote_transcript_status or "").strip()
        if not status:
            continue
        entries.append(
            {
                "page_index": page.page_index,
                "page_nr": page.page_nr,
                "transcript_ts_id": page.transcript_ts_id,
                "remote_transcript_status": status,
            }
        )
    if not entries:
        return None
    return {"pages": entries}


def _find_ready_snapshot(
    *,
    document_id: int,
    parser_version: str,
    raw_xml_fingerprint: str,
) -> TranskribusTranscriptSnapshot | None:
    return (
        TranskribusTranscriptSnapshot.objects.filter(
            document_id=document_id,
            parser_version=parser_version,
            raw_xml_fingerprint=raw_xml_fingerprint,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        .order_by("id")
        .first()
    )


def _line_kwargs(line: ParsedSnapshotLine) -> dict[str, Any]:
    bbox = line.bbox
    return {
        "order_index": line.order_index,
        "provider_region_id": line.provider_region_id or "",
        "provider_line_id": line.provider_line_id or "",
        "text": line.text,
        "contributes_to_canonical": line.contributes_to_canonical,
        "char_start": line.char_start,
        "char_end": line.char_end,
        "polygon_points": _points_to_json(line.polygon_points),
        "baseline_points": _points_to_json(line.baseline_points),
        "bbox_min_x": None if bbox is None else float(bbox.min_x),
        "bbox_min_y": None if bbox is None else float(bbox.min_y),
        "bbox_max_x": None if bbox is None else float(bbox.max_x),
        "bbox_max_y": None if bbox is None else float(bbox.max_y),
        "coords_valid": line.coords_valid,
        "baseline_valid": line.baseline_valid,
        "has_meaningful_geometry": line.has_meaningful_geometry,
    }


class _ConcurrentReadyWinner(Exception):
    """Internal control flow when another READY snapshot won the race."""

    def __init__(self, winner: TranskribusTranscriptSnapshot) -> None:
        self.winner = winner
        super().__init__("concurrent READY snapshot exists")


def _create_pending_snapshot_rows(
    *,
    document: Document,
    source_kind: str,
    parsed: ParsedSnapshotDocument,
    transkribus_run: TranskribusRun | None,
    remote_doc_id: str,
    collection_id: str,
    model_id: str,
    recognition_job_id: str,
    created_by: Any,
    remote_status_summary: dict[str, Any] | None,
    hover_eligible: bool | None = None,
) -> TranskribusTranscriptSnapshot:
    """Create PENDING_UPLOAD snapshot + pages/lines with deterministic S3 keys.

    Short transaction only — no network I/O.
    """
    effective_hover = (
        parsed.hover_eligible if hover_eligible is None else bool(hover_eligible)
    )
    with transaction.atomic():
        snapshot = TranskribusTranscriptSnapshot(
            document=document,
            transkribus_run=transkribus_run,
            source_kind=source_kind,
            remote_doc_id=(remote_doc_id or "").strip(),
            collection_id=(collection_id or "").strip(),
            model_id=(model_id or "").strip(),
            recognition_job_id=(recognition_job_id or "").strip(),
            parser_version=parsed.parser_version,
            provider_identity_fingerprint=parsed.provider_identity_fingerprint,
            raw_xml_fingerprint=parsed.raw_xml_fingerprint,
            canonical_text=parsed.canonical_text,
            canonical_text_sha256=parsed.canonical_text_sha256,
            geometry_capability=parsed.geometry_capability,
            hover_eligible=effective_hover,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
            remote_status_summary=remote_status_summary,
            created_by=created_by,
        )
        snapshot.save()

        for page in parsed.pages:
            s3_key = build_transkribus_snapshot_page_xml_s3_key(
                document_id=document.pk,
                snapshot_id=snapshot.pk,
                page_index=page.page_index,
            )
            page_row = TranskribusSnapshotPage.objects.create(
                snapshot=snapshot,
                page_index=page.page_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                provider_page_id=page.provider_page_id,
                image_width=page.image_width,
                image_height=page.image_height,
                image_filename=page.image_filename or "",
                page_namespace=page.page_namespace or "",
                remote_transcript_status=page.remote_transcript_status or "",
                page_xml_s3_key=s3_key,
                page_xml_sha256=page.page_xml_sha256,
                text_region_count=page.text_region_count,
                text_line_count=page.text_line_count,
                lines_with_non_empty_text=page.lines_with_non_empty_text,
                duplicate_line_ids=page.duplicate_line_ids,
                reading_order_present=page.reading_order_present,
                reading_order_resolved=page.reading_order_resolved,
                lines_xml_order_differs_from_reading_order=(
                    page.lines_xml_order_differs_from_reading_order
                ),
                page_geometry_capability=page.page_geometry_capability,
            )
            if page.lines:
                TranskribusSnapshotLine.objects.bulk_create(
                    [
                        TranskribusSnapshotLine(page=page_row, **_line_kwargs(line))
                        for line in page.lines
                    ]
                )

    return snapshot


def _upload_page_xml_objects(
    *,
    bucket: str,
    snapshot: TranskribusTranscriptSnapshot,
    page_inputs_by_index: dict[int, SnapshotPageInput],
    uploaded_keys: list[str],
) -> None:
    """Upload every page XML, appending each successful key to ``uploaded_keys``.

    ``uploaded_keys`` is a caller-owned accumulator so partial failures still leave
    earlier successful keys available for cleanup. The failing key is never
    appended.
    """
    pages = list(snapshot.pages.order_by("page_index"))
    for page_row in pages:
        key = (page_row.page_xml_s3_key or "").strip()
        checksum = (page_row.page_xml_sha256 or "").strip()
        if not key or not checksum:
            raise TranskribusSnapshotStorageUploadError(
                "Snapshot page is missing S3 key or PAGE XML checksum before upload.",
                snapshot_id=snapshot.pk,
            )
        page_input = page_inputs_by_index[page_row.page_index]
        try:
            put_object_bytes(
                bucket=bucket,
                key=key,
                body=bytes(page_input.page_xml),
                content_type=TRANSKRIBUS_SNAPSHOT_PAGE_XML_CONTENT_TYPE,
            )
        except (BotoCoreError, ClientError, OSError, TypeError, ValueError) as exc:
            raise TranskribusSnapshotStorageUploadError(
                f"Failed to upload PAGE XML for page_index={page_row.page_index}.",
                snapshot_id=snapshot.pk,
            ) from exc
        # Record only after a successful put; never append the failing key.
        uploaded_keys.append(key)


def _best_effort_delete_keys(
    *,
    bucket: str,
    keys: Sequence[str],
) -> tuple[str, ...]:
    cleanup_errors: list[str] = []
    seen: set[str] = set()
    for key in keys:
        normalized = (key or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            delete_s3_object(bucket, normalized)
        except (BotoCoreError, ClientError, OSError) as exc:
            cleanup_errors.append(
                f"cleanup_failed key={normalized} category={type(exc).__name__}"
            )
            logger.warning(
                "transkribus snapshot S3 cleanup failed",
                extra={
                    "s3_key": normalized,
                    "failure_category": type(exc).__name__,
                },
            )
    return tuple(cleanup_errors)


def _best_effort_mark_snapshot_failed(snapshot_id: int) -> str | None:
    """Best-effort PENDING_UPLOAD → FAILED update.

    Returns a safe state-update error string when the DB update fails, or ``None``
    on success / no-op. Never raises to replace a primary storage error. Never
    overwrites READY. Does not catch ``BaseException``.
    """
    try:
        with transaction.atomic():
            updated = TranskribusTranscriptSnapshot.objects.filter(
                pk=snapshot_id,
                storage_status=(
                    TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD
                ),
            ).update(
                storage_status=TranskribusTranscriptSnapshot.StorageStatus.FAILED,
            )
            if updated:
                return None
            current = (
                TranskribusTranscriptSnapshot.objects.filter(pk=snapshot_id)
                .values_list("storage_status", flat=True)
                .first()
            )
            if current == TranskribusTranscriptSnapshot.StorageStatus.READY:
                logger.warning(
                    "refusing to mark READY snapshot FAILED",
                    extra={"snapshot_id": snapshot_id},
                )
            return None
    except DatabaseError as exc:
        logger.warning(
            "transkribus snapshot FAILED state update failed",
            extra={
                "snapshot_id": snapshot_id,
                "failure_category": type(exc).__name__,
            },
        )
        return f"failed_state_update category={type(exc).__name__}"


def _assert_ready_invariants(snapshot: TranskribusTranscriptSnapshot) -> None:
    if not (snapshot.provider_identity_fingerprint or "").strip():
        raise TranskribusSnapshotStorageUploadError(
            "Cannot mark READY: missing provider_identity_fingerprint.",
            snapshot_id=snapshot.pk,
        )
    if not (snapshot.raw_xml_fingerprint or "").strip():
        raise TranskribusSnapshotStorageUploadError(
            "Cannot mark READY: missing raw_xml_fingerprint.",
            snapshot_id=snapshot.pk,
        )
    if not (snapshot.canonical_text_sha256 or "").strip():
        raise TranskribusSnapshotStorageUploadError(
            "Cannot mark READY: missing canonical_text_sha256.",
            snapshot_id=snapshot.pk,
        )
    pages = list(snapshot.pages.all())
    if not pages:
        raise TranskribusSnapshotStorageUploadError(
            "Cannot mark READY: snapshot has no pages.",
            snapshot_id=snapshot.pk,
        )
    for page in pages:
        if not (page.page_xml_s3_key or "").strip():
            raise TranskribusSnapshotStorageUploadError(
                f"Cannot mark READY: page_index={page.page_index} missing S3 key.",
                snapshot_id=snapshot.pk,
            )
        if not (page.page_xml_sha256 or "").strip():
            raise TranskribusSnapshotStorageUploadError(
                f"Cannot mark READY: page_index={page.page_index} missing checksum.",
                snapshot_id=snapshot.pk,
            )


def _reuse_concurrent_winner(
    *,
    winner: TranskribusTranscriptSnapshot,
    losing_snapshot_id: int,
    uploaded_keys: Sequence[str],
    bucket: str,
) -> SnapshotStorageResult:
    """Clean up a losing attempt and return the READY winner.

    Expected fate of the losing PENDING_UPLOAD row: best-effort mark FAILED after
    best-effort deletion of this attempt's uploaded S3 objects. Immutable content
    on either snapshot is never overwritten. Any residual keys left by failed
    cleanup remain orphan-eligible once status is FAILED (or after the PENDING
    orphan-protection window expires).
    """
    cleanup_errors = _best_effort_delete_keys(bucket=bucket, keys=uploaded_keys)
    state_update_error = _best_effort_mark_snapshot_failed(losing_snapshot_id)
    if cleanup_errors or state_update_error:
        logger.warning(
            "transkribus snapshot race-loser cleanup had failures",
            extra={
                "document_id": winner.document_id,
                "snapshot_id": losing_snapshot_id,
                "winner_snapshot_id": winner.pk,
                "cleanup_error_count": len(cleanup_errors),
                "state_update_failed": bool(state_update_error),
            },
        )
    logger.info(
        "transkribus snapshot reused concurrent winner",
        extra={
            "document_id": winner.document_id,
            "snapshot_id": winner.pk,
            "losing_snapshot_id": losing_snapshot_id,
            "page_count": winner.pages.count(),
            "outcome": SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER.value,
        },
    )
    return SnapshotStorageResult(
        outcome=SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER,
        snapshot=winner,
    )


def _finalize_ready_or_reuse_winner(
    *,
    snapshot: TranskribusTranscriptSnapshot,
    uploaded_keys: Sequence[str],
    bucket: str,
) -> SnapshotStorageResult:
    """Transition PENDING → READY, or reuse a concurrent READY winner."""
    document_id = snapshot.document_id
    parser_version = snapshot.parser_version
    raw_xml_fingerprint = snapshot.raw_xml_fingerprint or ""

    winner = _find_ready_snapshot(
        document_id=document_id,
        parser_version=parser_version,
        raw_xml_fingerprint=raw_xml_fingerprint,
    )
    if winner is not None and winner.pk != snapshot.pk:
        return _reuse_concurrent_winner(
            winner=winner,
            losing_snapshot_id=snapshot.pk,
            uploaded_keys=uploaded_keys,
            bucket=bucket,
        )

    try:
        with transaction.atomic():
            locked = (
                TranskribusTranscriptSnapshot.objects.select_for_update()
                .prefetch_related(
                    Prefetch(
                        "pages",
                        queryset=TranskribusSnapshotPage.objects.order_by("page_index"),
                    )
                )
                .get(pk=snapshot.pk)
            )
            if (
                locked.storage_status
                == TranskribusTranscriptSnapshot.StorageStatus.READY
            ):
                return SnapshotStorageResult(
                    outcome=SnapshotStorageOutcome.CREATED,
                    snapshot=locked,
                )
            if locked.storage_status != (
                TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD
            ):
                raise TranskribusSnapshotStorageUploadError(
                    f"Cannot finalize snapshot in status={locked.storage_status}.",
                    snapshot_id=locked.pk,
                )

            winner = _find_ready_snapshot(
                document_id=document_id,
                parser_version=parser_version,
                raw_xml_fingerprint=raw_xml_fingerprint,
            )
            if winner is not None and winner.pk != locked.pk:
                # Defer S3 cleanup / FAILED mark outside this short transaction.
                raise _ConcurrentReadyWinner(winner)

            _assert_ready_invariants(locked)
            locked.storage_status = TranskribusTranscriptSnapshot.StorageStatus.READY
            locked.save(update_fields=["storage_status"])
            locked.refresh_from_db()
            logger.info(
                "transkribus snapshot stored",
                extra={
                    "document_id": document_id,
                    "snapshot_id": locked.pk,
                    "page_count": locked.pages.count(),
                    "outcome": SnapshotStorageOutcome.CREATED.value,
                },
            )
            return SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.CREATED,
                snapshot=locked,
            )
    except _ConcurrentReadyWinner as race:
        return _reuse_concurrent_winner(
            winner=race.winner,
            losing_snapshot_id=snapshot.pk,
            uploaded_keys=uploaded_keys,
            bucket=bucket,
        )
    except IntegrityError:
        winner = _find_ready_snapshot(
            document_id=document_id,
            parser_version=parser_version,
            raw_xml_fingerprint=raw_xml_fingerprint,
        )
        if winner is None or winner.pk == snapshot.pk:
            raise TranskribusSnapshotStorageUploadError(
                "Failed to finalize READY snapshot due to uniqueness conflict.",
                snapshot_id=snapshot.pk,
            )
        return _reuse_concurrent_winner(
            winner=winner,
            losing_snapshot_id=snapshot.pk,
            uploaded_keys=uploaded_keys,
            bucket=bucket,
        )


def _raise_upload_failure(
    primary: Exception,
    *,
    snapshot_id: int,
    bucket: str,
    uploaded_keys: Sequence[str],
    document_id: int,
    page_count: int,
) -> NoReturn:
    """Cleanup + best-effort FAILED mark, then re-raise primary as UploadError."""
    cleanup_errors = _best_effort_delete_keys(bucket=bucket, keys=uploaded_keys)
    state_update_error = _best_effort_mark_snapshot_failed(snapshot_id)
    state_update_errors = (state_update_error,) if state_update_error else ()

    if isinstance(primary, TranskribusSnapshotStorageUploadError):
        message = str(primary)
        logger.error(
            "transkribus snapshot storage failed",
            extra={
                "document_id": document_id,
                "snapshot_id": snapshot_id,
                "page_count": page_count,
                "outcome": "FAILED",
                "failure_category": type(primary).__name__,
                "cleanup_error_count": len(cleanup_errors),
                "state_update_failed": bool(state_update_error),
            },
        )
        raise TranskribusSnapshotStorageUploadError(
            message,
            snapshot_id=snapshot_id,
            cleanup_errors=cleanup_errors,
            state_update_errors=state_update_errors,
        ) from primary

    logger.exception(
        "transkribus snapshot storage failed unexpectedly after PENDING create",
        extra={
            "document_id": document_id,
            "snapshot_id": snapshot_id,
            "page_count": page_count,
            "outcome": "FAILED",
            "failure_category": type(primary).__name__,
            "cleanup_error_count": len(cleanup_errors),
            "state_update_failed": bool(state_update_error),
        },
    )
    raise TranskribusSnapshotStorageUploadError(
        f"Snapshot storage failed ({type(primary).__name__}).",
        snapshot_id=snapshot_id,
        cleanup_errors=cleanup_errors,
        state_update_errors=state_update_errors,
    ) from primary


def store_transkribus_transcript_snapshot(
    *,
    document: Document,
    source_kind: str,
    pages: Sequence[SnapshotPageInput],
    transkribus_run: TranskribusRun | None = None,
    remote_doc_id: str = "",
    collection_id: str = "",
    model_id: str = "",
    recognition_job_id: str = "",
    created_by: Any = None,
    hover_eligible: bool | None = None,
) -> SnapshotStorageResult:
    """Parse and persist PAGE XML pages as an immutable Transkribus snapshot.

    Lifecycle (no cross-system atomicity between PostgreSQL and S3):

    1. Validate inputs and parse all pages / fingerprints before persistent writes.
    2. Reuse an existing READY snapshot for the same document + parser_version +
       raw_xml_fingerprint (no upload / no duplicate rows).
    3. Otherwise create PENDING_UPLOAD rows with deterministic final S3 keys.
    4. Upload every PAGE XML object (outside any DB transaction), recording each
       successful key in a caller-owned accumulator before the next upload.
    5. Only after all uploads succeed, transition to READY.
    6. On upload failure: never READY; best-effort mark FAILED; best-effort delete
       objects uploaded in this attempt; preserve the primary upload error.
    7. On concurrent identical finalization: reuse the READY winner, clean up this
       attempt's S3 objects, and best-effort mark the losing PENDING row FAILED.

    Activation / ``TranskribusTextResultBinding`` / ``DocumentTextResult`` updates
    are intentionally out of scope.
    """
    if document.pk is None:
        raise TranskribusSnapshotStorageValidationError(
            "Document must be saved before snapshot storage."
        )

    # Validation and parse happen before any snapshot row exists so failures here
    # never leave a PENDING_UPLOAD row behind.
    validated_source_kind = _validate_source_kind(source_kind)
    validated_pages = _validate_page_inputs(pages)
    validated_run = _validate_transkribus_run(
        document=document,
        transkribus_run=transkribus_run,
    )
    bucket = _uploads_bucket()

    try:
        parsed = parse_document_pages_for_snapshot(validated_pages)
    except TranskribusSnapshotParseError as exc:
        raise TranskribusSnapshotStorageValidationError(str(exc)) from exc

    existing = _find_ready_snapshot(
        document_id=document.pk,
        parser_version=parsed.parser_version,
        raw_xml_fingerprint=parsed.raw_xml_fingerprint,
    )
    if existing is not None:
        logger.info(
            "transkribus snapshot reused existing READY",
            extra={
                "document_id": document.pk,
                "snapshot_id": existing.pk,
                "page_count": existing.pages.count(),
                "outcome": SnapshotStorageOutcome.REUSED_EXISTING.value,
            },
        )
        return SnapshotStorageResult(
            outcome=SnapshotStorageOutcome.REUSED_EXISTING,
            snapshot=existing,
        )

    page_inputs_by_index = {page.page_index: page for page in validated_pages}
    remote_status_summary = _build_remote_status_summary(parsed.pages)

    snapshot = _create_pending_snapshot_rows(
        document=document,
        source_kind=validated_source_kind,
        parsed=parsed,
        transkribus_run=validated_run,
        remote_doc_id=remote_doc_id,
        collection_id=collection_id,
        model_id=model_id,
        recognition_job_id=recognition_job_id,
        created_by=created_by,
        remote_status_summary=remote_status_summary,
        hover_eligible=hover_eligible,
    )

    # Post-create guard: once a PENDING row exists, unexpected failures must still
    # best-effort mark FAILED and clean uploaded keys so the snapshot is not left
    # abandoned indefinitely. Validation/parser failures above never reach here.
    uploaded_keys: list[str] = []
    try:
        # Recheck before network I/O in case another attempt finished first.
        existing_before_upload = _find_ready_snapshot(
            document_id=document.pk,
            parser_version=parsed.parser_version,
            raw_xml_fingerprint=parsed.raw_xml_fingerprint,
        )
        if existing_before_upload is not None:
            _best_effort_mark_snapshot_failed(snapshot.pk)
            logger.info(
                "transkribus snapshot reused concurrent winner before upload",
                extra={
                    "document_id": document.pk,
                    "snapshot_id": existing_before_upload.pk,
                    "losing_snapshot_id": snapshot.pk,
                    "page_count": existing_before_upload.pages.count(),
                    "outcome": SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER.value,
                },
            )
            return SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER,
                snapshot=existing_before_upload,
            )

        _upload_page_xml_objects(
            bucket=bucket,
            snapshot=snapshot,
            page_inputs_by_index=page_inputs_by_index,
            uploaded_keys=uploaded_keys,
        )
        return _finalize_ready_or_reuse_winner(
            snapshot=snapshot,
            uploaded_keys=uploaded_keys,
            bucket=bucket,
        )
    except TranskribusSnapshotStorageUploadError as exc:
        _raise_upload_failure(
            exc,
            snapshot_id=snapshot.pk,
            bucket=bucket,
            uploaded_keys=uploaded_keys,
            document_id=document.pk,
            page_count=len(validated_pages),
        )
    except Exception as exc:
        # Necessary only after PENDING create: prevent abandoning the row / S3
        # objects on unexpected post-creation failures. Does not catch validation
        # or parser errors (those happen before create). Does not catch BaseException.
        _raise_upload_failure(
            exc,
            snapshot_id=snapshot.pk,
            bucket=bucket,
            uploaded_keys=uploaded_keys,
            document_id=document.pk,
            page_count=len(validated_pages),
        )


__all__ = [
    "SnapshotStorageOutcome",
    "SnapshotStorageResult",
    "TranskribusSnapshotStorageError",
    "TranskribusSnapshotStorageValidationError",
    "TranskribusSnapshotStorageUploadError",
    "store_transkribus_transcript_snapshot",
    "PARSER_VERSION",
]
