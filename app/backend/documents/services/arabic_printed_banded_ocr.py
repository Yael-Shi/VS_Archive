"""Single-page orchestration for printed-Arabic banded OCR.

This module sequences the already-implemented phases for exactly one claimed
page: Cloud Vision geometry (Phase 3), band planning (Phase 2), Antigravity
band transport (Phase 4), and durable checkpoints (Phase 1). It does not create
attempts, claim pages, route documents, persist ``DocumentTextResult``, or
translate.

Fencing, busy-lease, identity, and checkpoint-persistence errors propagate to
the caller; they are never converted into an ordinary OCR failure.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from documents.models import (
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
)
from documents.services.antigravity_defaults import (
    ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS,
    DEFAULT_POLL_SECONDS,
)
from documents.services.antigravity_engine import (
    BAND_ATTEMPT_ASSISTED_FALLBACK,
    BAND_ATTEMPT_UNASSISTED,
    AntigravityBandCheckpointError,
    AntigravityBandOcrResult,
    cancel_antigravity_interaction,
    poll_arabic_printed_band_interaction,
    transcribe_band_with_antigravity,
)
from documents.services.arabic_printed_banding import (
    ArabicPrintedBandingError,
    ArabicPrintedWordBox,
    plan_arabic_printed_bands,
)
from documents.services.arabic_printed_page_checkpoints import (
    ARABIC_PRINTED_CANCEL_CONFIRMED_CANCELLED,
    ArabicPrintedBandPlan,
    ArabicPrintedBandSafeDiagnostics,
    ArabicPrintedCheckpointBusyError,
    ArabicPrintedCheckpointPersistenceRetryableError,
    ArabicPrintedIdentityMismatchError,
    ArabicPrintedPageClaim,
    ArabicPrintedPageClaimAction,
    StaleArabicPrintedPageClaimError,
    apply_arabic_printed_band_diagnostics,
    assemble_arabic_printed_page,
    mark_arabic_printed_band_cancel_pending,
    persist_arabic_printed_band_failure,
    persist_arabic_printed_band_success,
    persist_arabic_printed_page_failure,
    persist_arabic_printed_vision_plan,
    reserve_arabic_printed_fallback_create,
    reserve_arabic_printed_primary_create,
    reserve_arabic_printed_vision_call,
    select_arabic_printed_band_cloud_vision_low_quality,
)
from documents.services.cloud_vision_document_text import (
    ArabicPrintedWorkingImage,
    CloudVisionDocumentTextError,
    detect_arabic_printed_document_text,
    encode_arabic_printed_band_crop,
    reconstruct_draft_from_word_indexes,
)

OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_FAILED = "FAILED"

PAGE_FAILURE_VISION_AMBIGUOUS = "ARABIC_PRINTED_VISION_AMBIGUOUS"
PAGE_FAILURE_VISION_CALL = "ARABIC_PRINTED_VISION_FAILED"
PAGE_FAILURE_BANDING = "ARABIC_PRINTED_BANDING_FAILED"
PAGE_FAILURE_PLAN_MISMATCH = "ARABIC_PRINTED_PLAN_MISMATCH"
PAGE_FAILURE_DEADLINE = "ARABIC_PRINTED_PAGE_DEADLINE"
PAGE_FAILURE_BANDS_UNRESOLVED = "ARABIC_PRINTED_BANDS_UNRESOLVED"

BAND_FAILURE_PRIMARY = "ARABIC_PRINTED_PRIMARY_FAILED"
BAND_FAILURE_FALLBACK = "ARABIC_PRINTED_FALLBACK_FAILED"
BAND_FAILURE_PRIMARY_AMBIGUOUS = "ARABIC_PRINTED_PRIMARY_AMBIGUOUS"
BAND_FAILURE_FALLBACK_AMBIGUOUS = "ARABIC_PRINTED_FALLBACK_AMBIGUOUS"
BAND_FAILURE_CROP_MISMATCH = "ARABIC_PRINTED_CROP_MISMATCH"

# A reserved create whose interaction id was never persisted may still be live at
# the provider. Such a band stays permanently fenced: no create, poll, cancel, or
# low-quality selection, across this run and every later reclaim.
AMBIGUOUS_BAND_FAILURE_CODES = frozenset(
    {
        BAND_FAILURE_PRIMARY_AMBIGUOUS,
        BAND_FAILURE_FALLBACK_AMBIGUOUS,
    }
)

_ATTEMPT_PRIMARY = "primary"
_ATTEMPT_FALLBACK = "fallback"
_IN_PROGRESS_STATUS = "in_progress"
_CANCEL_STATUS_COMPLETED = "completed"
_POLL_OUTCOMES_NEEDING_CANCEL = frozenset({"timeout", "poll_error"})
_MAX_LATENCY_MS = 2_147_483_647
_MAX_BAND_STEPS = 8
_MAX_DIAGNOSTIC_CHARS = 512

CHECKPOINT_CONTROL_ERRORS = (
    ArabicPrintedCheckpointBusyError,
    StaleArabicPrintedPageClaimError,
    ArabicPrintedIdentityMismatchError,
    ArabicPrintedCheckpointPersistenceRetryableError,
)


@dataclass(frozen=True)
class ArabicPrintedBandedPageResult:
    """Privacy-safe page outcome. ``repr``/``str`` never expose OCR text."""

    checkpoint_id: int
    page_index: int
    outcome: str
    assembled_text: str
    page_quality: str
    runtime_engine_marker: str
    failure_code: str | None

    def __repr__(self) -> str:
        return (
            "ArabicPrintedBandedPageResult("
            f"checkpoint_id={self.checkpoint_id}, page_index={self.page_index}, "
            f"outcome={self.outcome!r}, page_quality={self.page_quality!r}, "
            f"runtime_engine_marker={self.runtime_engine_marker!r}, "
            f"failure_code={self.failure_code!r}, "
            f"assembled_text_byte_length={len(self.assembled_text.encode('utf-8'))})"
        )

    def __str__(self) -> str:
        return repr(self)


class _PageAborted(Exception):
    """Internal signal: persist a privacy-safe page failure and stop."""

    def __init__(self, *, failure_code: str, failure_message: str) -> None:
        self.failure_code = failure_code
        self.failure_message = failure_message
        super().__init__(failure_code)


@dataclass
class _PageContext:
    checkpoint_id: int
    page_index: int
    lease_token: object
    working_image: ArabicPrintedWorkingImage
    gemini_api_key: str
    cloud_vision_api_key: str
    absolute_deadline_monotonic: float
    poll_seconds: float
    sleep_fn: object
    monotonic_fn: object

    def remaining(self) -> float:
        return self.absolute_deadline_monotonic - self.monotonic_fn()

    def attempt_deadline(self) -> float:
        window = self.monotonic_fn() + ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS
        return min(self.absolute_deadline_monotonic, window)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _latency_ms(latency_seconds: float | None) -> int | None:
    if latency_seconds is None:
        return None
    try:
        milliseconds = int(round(float(latency_seconds) * 1000.0))
    except (TypeError, ValueError, OverflowError):
        return None
    if milliseconds < 0:
        return 0
    return min(milliseconds, _MAX_LATENCY_MS)


def _safe_token(value: object, *, limit: int = 64) -> str | None:
    if type(value) is not str:
        return None
    cleaned = value.strip()[:limit]
    return cleaned or None


def _attempt_diagnostics(
    result: AntigravityBandOcrResult,
    *,
    attempt_kind: str,
) -> ArabicPrintedBandSafeDiagnostics:
    summary = (
        f"outcome={result.polling_outcome} "
        f"failure={result.failure_kind or 'none'} "
        f"marker_seen={result.marker_seen} "
        f"accepted={result.accepted}"
    )[:_MAX_DIAGNOSTIC_CHARS]
    if attempt_kind == _ATTEMPT_PRIMARY:
        return ArabicPrintedBandSafeDiagnostics(
            primary_interaction_id=_safe_token(result.interaction_id, limit=128),
            primary_provider_status=_safe_token(result.last_status),
            primary_latency_ms=_latency_ms(result.latency_seconds),
            primary_failure_type=_safe_token(result.failure_kind),
            primary_safe_diagnostics=summary,
        )
    return ArabicPrintedBandSafeDiagnostics(
        fallback_interaction_id=_safe_token(result.interaction_id, limit=128),
        fallback_provider_status=_safe_token(result.last_status),
        fallback_latency_ms=_latency_ms(result.latency_seconds),
        fallback_failure_type=_safe_token(result.failure_kind),
        fallback_safe_diagnostics=summary,
    )


def _page_checkpoint(checkpoint_id: int) -> ArabicPrintedOcrPageCheckpoint:
    return ArabicPrintedOcrPageCheckpoint.objects.get(pk=checkpoint_id)


def _band_row(checkpoint_id: int, band_index: int) -> ArabicPrintedOcrBandCheckpoint:
    return ArabicPrintedOcrBandCheckpoint.objects.get(
        page_checkpoint_id=checkpoint_id,
        band_index=band_index,
    )


def _durable_plan_exists(checkpoint: ArabicPrintedOcrPageCheckpoint) -> bool:
    return (
        checkpoint.cloud_vision_call_count == 1
        and checkpoint.band_count >= 1
        and bool(checkpoint.cloud_vision_response_sha256)
        and checkpoint.band_checkpoints.exists()
    )


def _verified_crop(
    ctx: _PageContext,
    band: ArabicPrintedOcrBandCheckpoint,
):
    """Recreate a crop from the stored rectangle and verify stored identity."""
    if (
        not band.vision_draft_text.strip()
        or band.vision_draft_sha256 != _sha256_text(band.vision_draft_text)
        or band.vision_draft_byte_length != _byte_length(band.vision_draft_text)
    ):
        raise _PageAborted(
            failure_code=PAGE_FAILURE_PLAN_MISMATCH,
            failure_message=f"stored draft mismatch band_index={band.band_index}",
        )
    try:
        crop = encode_arabic_printed_band_crop(
            ctx.working_image,
            left=band.rect_x,
            top=band.rect_y,
            right=band.rect_x + band.rect_width,
            bottom=band.rect_y + band.rect_height,
        )
    except CloudVisionDocumentTextError as exc:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_PLAN_MISMATCH,
            failure_message=(
                f"crop encode failed band_index={band.band_index} "
                f"kind={exc.failure_kind}"
            ),
        ) from None
    if (
        crop.mime_type != band.crop_mime
        or crop.sha256 != band.crop_sha256
        or crop.byte_length != band.crop_byte_length
        or crop.width != band.rect_width
        or crop.height != band.rect_height
    ):
        raise _PageAborted(
            failure_code=BAND_FAILURE_CROP_MISMATCH,
            failure_message=f"crop identity mismatch band_index={band.band_index}",
        )
    return crop


def _verify_stored_plan(ctx: _PageContext) -> dict[int, object]:
    """Verify contiguity and crop/draft identity before any Antigravity call."""
    bands = list(
        ArabicPrintedOcrBandCheckpoint.objects.filter(
            page_checkpoint_id=ctx.checkpoint_id
        ).order_by("band_index")
    )
    checkpoint = _page_checkpoint(ctx.checkpoint_id)
    if not bands or len(bands) != checkpoint.band_count:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_PLAN_MISMATCH,
            failure_message="stored band count mismatch",
        )
    if [band.band_index for band in bands] != list(range(len(bands))):
        raise _PageAborted(
            failure_code=PAGE_FAILURE_PLAN_MISMATCH,
            failure_message="stored band indexes are not contiguous 0-based",
        )
    return {band.band_index: _verified_crop(ctx, band) for band in bands}


def _require_working_image_identity(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    working_image: ArabicPrintedWorkingImage,
) -> None:
    """Fail closed before any provider call if the page bytes are not the claimed ones."""
    if (
        checkpoint.oriented_image_width != working_image.width
        or checkpoint.oriented_image_height != working_image.height
    ):
        raise ArabicPrintedIdentityMismatchError(
            "Working image dimensions do not match the persisted page checkpoint"
        )
    if checkpoint.oriented_image_sha256 != working_image.sha256:
        raise ArabicPrintedIdentityMismatchError(
            "Working image digest does not match the persisted page checkpoint"
        )


def _run_cloud_vision_plan(ctx: _PageContext) -> None:
    checkpoint = _page_checkpoint(ctx.checkpoint_id)
    if ctx.remaining() <= 0:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_DEADLINE,
            failure_message="page deadline reached before Cloud Vision",
        )

    reserve_arabic_printed_vision_call(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
    )
    try:
        detection = detect_arabic_printed_document_text(
            api_key=ctx.cloud_vision_api_key,
            working_image=ctx.working_image,
            remaining_timeout_seconds=ctx.remaining(),
        )
    except CloudVisionDocumentTextError as exc:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_VISION_CALL,
            failure_message=f"cloud vision kind={exc.failure_kind}",
        ) from None

    words = tuple(
        ArabicPrintedWordBox(
            index=word.index,
            xmin=word.xmin,
            ymin=word.ymin,
            xmax=word.xmax,
            ymax=word.ymax,
        )
        for word in detection.words
    )
    try:
        rects = plan_arabic_printed_bands(
            words,
            image_width=checkpoint.oriented_image_width,
            image_height=checkpoint.oriented_image_height,
        )
    except ArabicPrintedBandingError as exc:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_BANDING,
            failure_message=f"banding reason={exc.reason}",
        ) from None

    plans: list[ArabicPrintedBandPlan] = []
    for rect in rects:
        try:
            crop = encode_arabic_printed_band_crop(
                ctx.working_image,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
            )
            draft = reconstruct_draft_from_word_indexes(
                detection.words, rect.word_indexes
            )
        except CloudVisionDocumentTextError as exc:
            raise _PageAborted(
                failure_code=PAGE_FAILURE_BANDING,
                failure_message=f"band crop/draft kind={exc.failure_kind}",
            ) from None
        # Planner rectangles are 1-based; checkpoint band plans are 0-based.
        stored_index = rect.band_index - 1
        plans.append(
            ArabicPrintedBandPlan(
                band_index=stored_index,
                rect_x=rect.left,
                rect_y=rect.top,
                rect_width=rect.right - rect.left,
                rect_height=rect.bottom - rect.top,
                crop_mime=crop.mime_type,
                crop_byte_length=crop.byte_length,
                crop_sha256=crop.sha256,
                vision_draft_text=draft,
                vision_draft_byte_length=_byte_length(draft),
                vision_draft_sha256=_sha256_text(draft),
            )
        )

    persist_arabic_printed_vision_plan(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        cloud_vision_response_sha256=detection.response_sha256,
        bands=plans,
    )


def _ensure_plan(ctx: _PageContext) -> dict[int, object]:
    checkpoint = _page_checkpoint(ctx.checkpoint_id)
    if _durable_plan_exists(checkpoint):
        return _verify_stored_plan(ctx)
    if checkpoint.cloud_vision_call_count != 0:
        raise _PageAborted(
            failure_code=PAGE_FAILURE_VISION_AMBIGUOUS,
            failure_message="vision reserved without a durable plan",
        )
    _run_cloud_vision_plan(ctx)
    return _verify_stored_plan(ctx)


def _persist_band_failure(
    ctx: _PageContext,
    band_index: int,
    *,
    failure_code: str,
    failure_message: str,
    diagnostics: ArabicPrintedBandSafeDiagnostics | None = None,
) -> None:
    persist_arabic_printed_band_failure(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        band_index=band_index,
        failure_code=failure_code,
        failure_message=failure_message,
        diagnostics=diagnostics,
    )


def _persist_band_success(
    ctx: _PageContext,
    band_index: int,
    *,
    attempt_kind: str,
    transcription: str,
    diagnostics: ArabicPrintedBandSafeDiagnostics | None,
) -> None:
    normalized = transcription.strip()
    selected = (
        ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED
        if attempt_kind == _ATTEMPT_PRIMARY
        else ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK
    )
    persist_arabic_printed_band_success(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        band_index=band_index,
        selected_result=selected,
        transcription_text=normalized,
        transcription_sha256=_sha256_text(normalized),
        transcription_byte_length=_byte_length(normalized),
        diagnostics=diagnostics,
    )


def _needs_cancellation(result: AntigravityBandOcrResult) -> bool:
    return (
        result.polling_outcome in _POLL_OUTCOMES_NEEDING_CANCEL
        and result.last_status == _IN_PROGRESS_STATUS
        and bool(result.interaction_id)
    )


def _cancel_diagnostics(
    outcome: str,
    *,
    http_status: int | None,
    last_status: object,
) -> ArabicPrintedBandSafeDiagnostics:
    confirmed = _safe_token(last_status) or ""
    return ArabicPrintedBandSafeDiagnostics(
        cancel_http_status=http_status,
        cancel_confirmed_status=confirmed,
        cancel_safe_diagnostics=f"cancel_outcome={outcome}"[:_MAX_DIAGNOSTIC_CHARS],
    )


def _cancel_in_flight_band(
    ctx: _PageContext,
    band_index: int,
    result: AntigravityBandOcrResult,
    *,
    attempt_kind: str,
) -> None:
    """Persist CANCEL_PENDING before cancel HTTP, then cancel at most once."""
    mark_arabic_printed_band_cancel_pending(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        band_index=band_index,
        diagnostics=_attempt_diagnostics(result, attempt_kind=attempt_kind),
    )
    band = _band_row(ctx.checkpoint_id, band_index)
    if ctx.remaining() <= 0:
        return
    cancel = cancel_antigravity_interaction(
        api_key=ctx.gemini_api_key,
        interaction_id=str(result.interaction_id),
        vision_draft_text=band.vision_draft_text,
    )
    apply_arabic_printed_band_diagnostics(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        band_index=band_index,
        diagnostics=_cancel_diagnostics(
            cancel.cancel_outcome,
            http_status=cancel.http_status,
            last_status=cancel.last_status,
        ),
    )
    if (
        cancel.cancel_outcome == _CANCEL_STATUS_COMPLETED
        and cancel.evaluation_accepted
        and cancel.transcription.strip()
    ):
        _persist_band_success(
            ctx,
            band_index,
            attempt_kind=attempt_kind,
            transcription=cancel.transcription,
            diagnostics=None,
        )


def _handle_attempt_result(
    ctx: _PageContext,
    band_index: int,
    result: AntigravityBandOcrResult,
    *,
    attempt_kind: str,
) -> None:
    diagnostics = _attempt_diagnostics(result, attempt_kind=attempt_kind)
    if result.accepted and result.transcription.strip():
        _persist_band_success(
            ctx,
            band_index,
            attempt_kind=attempt_kind,
            transcription=result.transcription,
            diagnostics=diagnostics,
        )
        return
    if _needs_cancellation(result):
        _cancel_in_flight_band(ctx, band_index, result, attempt_kind=attempt_kind)
        return
    failure_code = (
        BAND_FAILURE_PRIMARY
        if attempt_kind == _ATTEMPT_PRIMARY
        else BAND_FAILURE_FALLBACK
    )
    _persist_band_failure(
        ctx,
        band_index,
        failure_code=failure_code,
        failure_message=f"attempt failure kind={result.failure_kind or 'rejected'}",
        diagnostics=diagnostics,
    )


def _interaction_created_hook(ctx: _PageContext, band_index: int, attempt_kind: str):
    def _persist(interaction_id: str) -> None:
        if attempt_kind == _ATTEMPT_PRIMARY:
            diagnostics = ArabicPrintedBandSafeDiagnostics(
                primary_interaction_id=_safe_token(interaction_id, limit=128),
            )
        else:
            diagnostics = ArabicPrintedBandSafeDiagnostics(
                fallback_interaction_id=_safe_token(interaction_id, limit=128),
            )
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=ctx.checkpoint_id,
            lease_token=ctx.lease_token,
            band_index=band_index,
            diagnostics=diagnostics,
        )

    return _persist


def _run_attempt(
    ctx: _PageContext,
    band_index: int,
    crop,
    *,
    attempt_kind: str,
) -> None:
    band = _band_row(ctx.checkpoint_id, band_index)
    provider_kind = (
        BAND_ATTEMPT_UNASSISTED
        if attempt_kind == _ATTEMPT_PRIMARY
        else BAND_ATTEMPT_ASSISTED_FALLBACK
    )
    try:
        result = transcribe_band_with_antigravity(
            api_key=ctx.gemini_api_key,
            jpeg_bytes=crop.jpeg_bytes,
            mime_type=crop.mime_type,
            vision_draft_text=band.vision_draft_text,
            attempt_kind=provider_kind,
            absolute_deadline_monotonic=ctx.absolute_deadline_monotonic,
            on_interaction_created=_interaction_created_hook(
                ctx, band_index, attempt_kind
            ),
            poll_seconds=ctx.poll_seconds,
            sleep_fn=ctx.sleep_fn,
            monotonic_fn=ctx.monotonic_fn,
        )
    except AntigravityBandCheckpointError as exc:
        cause = exc.__cause__
        if isinstance(cause, CHECKPOINT_CONTROL_ERRORS):
            raise cause from exc
        raise
    _handle_attempt_result(ctx, band_index, result, attempt_kind=attempt_kind)


def _resume_attempt(
    ctx: _PageContext,
    band_index: int,
    interaction_id: str,
    *,
    attempt_kind: str,
) -> None:
    band = _band_row(ctx.checkpoint_id, band_index)
    result = poll_arabic_printed_band_interaction(
        api_key=ctx.gemini_api_key,
        interaction_id=interaction_id,
        vision_draft_text=band.vision_draft_text,
        last_status=_IN_PROGRESS_STATUS,
        absolute_deadline_monotonic=ctx.attempt_deadline(),
        poll_seconds=ctx.poll_seconds,
        sleep_fn=ctx.sleep_fn,
        monotonic_fn=ctx.monotonic_fn,
    )
    _handle_attempt_result(ctx, band_index, result, attempt_kind=attempt_kind)


def _try_low_quality(ctx: _PageContext, band_index: int) -> bool:
    try:
        select_arabic_printed_band_cloud_vision_low_quality(
            checkpoint_id=ctx.checkpoint_id,
            lease_token=ctx.lease_token,
            band_index=band_index,
        )
    except ValueError:
        return False
    return True


def _recover_completed_cancel(ctx: _PageContext, band_index: int) -> None:
    band = _band_row(ctx.checkpoint_id, band_index)
    attempt_kind = (
        _ATTEMPT_PRIMARY if band.create_call_count == 1 else _ATTEMPT_FALLBACK
    )
    interaction_id = (
        band.primary_interaction_id.strip()
        if attempt_kind == _ATTEMPT_PRIMARY
        else band.fallback_interaction_id.strip()
    )
    if not interaction_id or ctx.remaining() <= 0:
        _persist_band_failure(
            ctx,
            band_index,
            failure_code=(
                BAND_FAILURE_PRIMARY
                if attempt_kind == _ATTEMPT_PRIMARY
                else BAND_FAILURE_FALLBACK
            ),
            failure_message="cancel completed without a recoverable interaction",
        )
        return
    # A terminal last_status short-circuits polling, so the recovery GET is issued
    # with in_progress to actually read the stored interaction once.
    result = poll_arabic_printed_band_interaction(
        api_key=ctx.gemini_api_key,
        interaction_id=interaction_id,
        vision_draft_text=band.vision_draft_text,
        last_status=_IN_PROGRESS_STATUS,
        absolute_deadline_monotonic=ctx.attempt_deadline(),
        poll_seconds=ctx.poll_seconds,
        sleep_fn=ctx.sleep_fn,
        monotonic_fn=ctx.monotonic_fn,
    )
    diagnostics = _attempt_diagnostics(result, attempt_kind=attempt_kind)
    if result.accepted and result.transcription.strip():
        _persist_band_success(
            ctx,
            band_index,
            attempt_kind=attempt_kind,
            transcription=result.transcription,
            diagnostics=diagnostics,
        )
        return
    _persist_band_failure(
        ctx,
        band_index,
        failure_code=(
            BAND_FAILURE_PRIMARY
            if attempt_kind == _ATTEMPT_PRIMARY
            else BAND_FAILURE_FALLBACK
        ),
        failure_message=(
            f"cancel completed rejected kind={result.failure_kind or 'rejected'}"
        ),
        diagnostics=diagnostics,
    )


def _resolve_cancel_pending(
    ctx: _PageContext,
    band: ArabicPrintedOcrBandCheckpoint,
    crop,
) -> bool | None:
    """Return True/False for a decided band, or None to re-evaluate the state."""
    confirmed = band.cancel_confirmed_status.strip().lower()
    if confirmed == _CANCEL_STATUS_COMPLETED:
        _recover_completed_cancel(ctx, band.band_index)
        return None
    if confirmed != ARABIC_PRINTED_CANCEL_CONFIRMED_CANCELLED:
        # Unknown, empty, or other cancel status stays fail-closed.
        return False
    if band.create_call_count >= 2:
        return _try_low_quality(ctx, band.band_index)
    if ctx.remaining() <= 0:
        return _try_low_quality(ctx, band.band_index)
    reserve_arabic_printed_fallback_create(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        band_index=band.band_index,
    )
    _run_attempt(ctx, band.band_index, crop, attempt_kind=_ATTEMPT_FALLBACK)
    return None


def _process_band(ctx: _PageContext, band_index: int, crop) -> bool:
    statuses = ArabicPrintedOcrBandCheckpoint.Status
    for _step in range(_MAX_BAND_STEPS):
        band = _band_row(ctx.checkpoint_id, band_index)
        status = band.status

        if status == statuses.SUCCEEDED:
            return True

        if (
            status == statuses.FAILED
            and band.failure_code.strip() in AMBIGUOUS_BAND_FAILURE_CODES
        ):
            return False

        if status == statuses.PENDING or (
            status == statuses.FAILED and band.create_call_count == 0
        ):
            if ctx.remaining() <= 0:
                return False
            reserve_arabic_printed_primary_create(
                checkpoint_id=ctx.checkpoint_id,
                lease_token=ctx.lease_token,
                band_index=band_index,
            )
            _run_attempt(ctx, band_index, crop, attempt_kind=_ATTEMPT_PRIMARY)
            continue

        if status == statuses.PRIMARY_RUNNING:
            interaction_id = band.primary_interaction_id.strip()
            if not interaction_id:
                _persist_band_failure(
                    ctx,
                    band_index,
                    failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                    failure_message="primary reserved without an interaction id",
                )
                return False
            _resume_attempt(
                ctx, band_index, interaction_id, attempt_kind=_ATTEMPT_PRIMARY
            )
            continue

        if status == statuses.FALLBACK_RUNNING:
            interaction_id = band.fallback_interaction_id.strip()
            if not interaction_id:
                _persist_band_failure(
                    ctx,
                    band_index,
                    failure_code=BAND_FAILURE_FALLBACK_AMBIGUOUS,
                    failure_message="fallback reserved without an interaction id",
                )
                return False
            _resume_attempt(
                ctx, band_index, interaction_id, attempt_kind=_ATTEMPT_FALLBACK
            )
            continue

        if status == statuses.CANCEL_PENDING:
            decided = _resolve_cancel_pending(ctx, band, crop)
            if decided is None:
                continue
            return decided

        if status == statuses.FAILED:
            if band.create_call_count == 1:
                if ctx.remaining() > 0:
                    reserve_arabic_printed_fallback_create(
                        checkpoint_id=ctx.checkpoint_id,
                        lease_token=ctx.lease_token,
                        band_index=band_index,
                    )
                    _run_attempt(ctx, band_index, crop, attempt_kind=_ATTEMPT_FALLBACK)
                    continue
                return _try_low_quality(ctx, band_index)
            return _try_low_quality(ctx, band_index)

        return False
    return False


def _reuse_result(checkpoint_id: int) -> ArabicPrintedBandedPageResult:
    checkpoint = _page_checkpoint(checkpoint_id)
    if checkpoint.status != ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED:
        raise ArabicPrintedIdentityMismatchError(
            "REUSE claim requires a succeeded Arabic printed page checkpoint"
        )
    return ArabicPrintedBandedPageResult(
        checkpoint_id=checkpoint.id,
        page_index=checkpoint.page_index,
        outcome=OUTCOME_SUCCEEDED,
        assembled_text=checkpoint.assembled_text or "",
        page_quality=checkpoint.page_quality,
        runtime_engine_marker=checkpoint.runtime_engine_marker,
        failure_code=None,
    )


def _fail_page(
    ctx: _PageContext,
    *,
    failure_code: str,
    failure_message: str,
) -> ArabicPrintedBandedPageResult:
    persist_arabic_printed_page_failure(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
        failure_code=failure_code,
        failure_message=failure_message,
    )
    return ArabicPrintedBandedPageResult(
        checkpoint_id=ctx.checkpoint_id,
        page_index=ctx.page_index,
        outcome=OUTCOME_FAILED,
        assembled_text="",
        page_quality="",
        runtime_engine_marker="",
        failure_code=failure_code,
    )


def process_claimed_arabic_printed_page(
    *,
    claim: ArabicPrintedPageClaim,
    working_image: ArabicPrintedWorkingImage,
    gemini_api_key: str,
    cloud_vision_api_key: str,
    absolute_deadline_monotonic: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> ArabicPrintedBandedPageResult:
    """Run Vision, banded Antigravity OCR, and assembly for one claimed page."""
    if claim.action == ArabicPrintedPageClaimAction.REUSE:
        return _reuse_result(claim.checkpoint_id)

    if claim.lease_token is None:
        raise StaleArabicPrintedPageClaimError("EXECUTE claim requires a lease token")

    checkpoint = _page_checkpoint(claim.checkpoint_id)
    if checkpoint.page_index != claim.page_index:
        raise ArabicPrintedIdentityMismatchError(
            "Claim page index does not match the persisted page checkpoint"
        )
    _require_working_image_identity(checkpoint, working_image)

    ctx = _PageContext(
        checkpoint_id=claim.checkpoint_id,
        page_index=claim.page_index,
        lease_token=claim.lease_token,
        working_image=working_image,
        gemini_api_key=gemini_api_key,
        cloud_vision_api_key=cloud_vision_api_key,
        absolute_deadline_monotonic=float(absolute_deadline_monotonic),
        poll_seconds=poll_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )

    try:
        crops = _ensure_plan(ctx)
        for band_index in sorted(crops):
            if not _process_band(ctx, band_index, crops[band_index]):
                raise _PageAborted(
                    failure_code=PAGE_FAILURE_BANDS_UNRESOLVED,
                    failure_message=f"band_index={band_index} did not reach success",
                )
    except _PageAborted as aborted:
        return _fail_page(
            ctx,
            failure_code=aborted.failure_code,
            failure_message=aborted.failure_message,
        )

    assembled = assemble_arabic_printed_page(
        checkpoint_id=ctx.checkpoint_id,
        lease_token=ctx.lease_token,
    )
    return ArabicPrintedBandedPageResult(
        checkpoint_id=assembled.id,
        page_index=assembled.page_index,
        outcome=OUTCOME_SUCCEEDED,
        assembled_text=assembled.assembled_text or "",
        page_quality=assembled.page_quality,
        runtime_engine_marker=assembled.runtime_engine_marker,
        failure_code=None,
    )
