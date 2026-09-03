"""Document-level coordinator for printed-Arabic banded OCR.

This module sits above the Phase 5A single-page orchestrator. It creates or
reuses a durable ArabicPrinted attempt, claims pages in 0-based order, and
calls ``process_claimed_arabic_printed_page`` once per claimed page. It does
not extract PDFs, talk to S3, route documents, persist ``DocumentTextResult``,
or get imported by production adapters/workers.

Working images are loaded only for EXECUTE claims. REUSE reads the persisted
page through Phase 5A with a checkpoint-derived stub image and never calls the
loader.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrPageCheckpoint,
)
from documents.services.antigravity_defaults import (
    ARABIC_PRINTED_DOCUMENT_SAFETY_MARGIN_SECONDS,
    ARABIC_PRINTED_PAGE_BUDGET_CAP_SECONDS,
    ARABIC_PRINTED_PAGE_START_BUDGET_SECONDS,
    DEFAULT_POLL_SECONDS,
)
from documents.services.arabic_printed_banded_ocr import (
    CHECKPOINT_CONTROL_ERRORS,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    ArabicPrintedBandedPageResult,
    process_claimed_arabic_printed_page,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedPageClaimAction,
    ArabicPrintedPageSource,
    build_arabic_printed_attempt_identity,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
    missing_pages_for_arabic_printed_attempt,
    persist_arabic_printed_page_failure,
)
from documents.services.cloud_vision_document_text import ArabicPrintedWorkingImage

OUTCOME_COMPLETED = ArabicPrintedOcrAttempt.Status.COMPLETED
OUTCOME_PARTIAL = ArabicPrintedOcrAttempt.Status.PARTIAL

DOCUMENT_FAILURE_DEADLINE = "ARABIC_PRINTED_DOCUMENT_DEADLINE"
DOCUMENT_FAILURE_IMAGE_LOAD = "ARABIC_PRINTED_IMAGE_LOAD_FAILED"

WorkingImageLoader = Callable[[int], ArabicPrintedWorkingImage]


@dataclass(frozen=True)
class ArabicPrintedDocumentPageInput:
    """Caller-supplied page identity. Contains no image bytes or OCR text."""

    page_index: int
    mime_type: str
    source_identity: str
    source_content_fingerprint: str
    oriented_image_sha256: str
    oriented_image_width: int
    oriented_image_height: int


@dataclass(frozen=True)
class ArabicPrintedBandedDocumentResult:
    """Document rollup. ``repr``/``str`` never expose assembled page text."""

    attempt_id: int
    document_id: int
    outcome: str
    pages: tuple[ArabicPrintedBandedPageResult, ...]
    missing_page_indices: tuple[int, ...]
    failure_code: str | None

    def __repr__(self) -> str:
        page_summaries = tuple(
            (
                page.page_index,
                page.outcome,
                page.page_quality,
                page.runtime_engine_marker,
                page.failure_code,
            )
            for page in self.pages
        )
        return (
            "ArabicPrintedBandedDocumentResult("
            f"attempt_id={self.attempt_id}, document_id={self.document_id}, "
            f"outcome={self.outcome!r}, missing_page_indices={self.missing_page_indices!r}, "
            f"failure_code={self.failure_code!r}, page_count={len(self.pages)}, "
            f"pages={page_summaries!r})"
        )

    def __str__(self) -> str:
        return repr(self)


def arabic_printed_page_absolute_deadline(
    *,
    now: float,
    document_deadline_monotonic: float,
    unfinished_executable_pages: int,
    page_budget_cap_seconds: float = ARABIC_PRINTED_PAGE_BUDGET_CAP_SECONDS,
    page_start_budget_seconds: float = ARABIC_PRINTED_PAGE_START_BUDGET_SECONDS,
    safety_margin_seconds: float = ARABIC_PRINTED_DOCUMENT_SAFETY_MARGIN_SECONDS,
) -> float | None:
    """Absolute monotonic deadline for the next EXECUTE page, or ``None``.

    Usable document time is the caller deadline minus the terminalization
    reserve. That remainder is split equally among unfinished executable
    pages. If the equal share is below the 150-second start minimum, the page
    must not start. Otherwise the share is capped at 480 seconds.
    """
    if unfinished_executable_pages < 1:
        return None
    usable = (
        float(document_deadline_monotonic) - float(now) - float(safety_margin_seconds)
    )
    if usable <= 0:
        return None
    share = usable / unfinished_executable_pages
    if share < float(page_start_budget_seconds):
        return None
    budget = min(share, float(page_budget_cap_seconds))
    if budget <= 0:
        return None
    return float(now) + budget


def _page_sources(
    pages: Sequence[ArabicPrintedDocumentPageInput],
) -> list[ArabicPrintedPageSource]:
    return [
        ArabicPrintedPageSource(
            page_index=page.page_index,
            mime_type=page.mime_type,
            source_identity=page.source_identity,
            source_content_fingerprint=page.source_content_fingerprint,
            oriented_image_sha256=page.oriented_image_sha256,
            oriented_image_width=page.oriented_image_width,
            oriented_image_height=page.oriented_image_height,
        )
        for page in pages
    ]


def _unfinished_executable_indices(
    *,
    attempt_id: int,
    start_index: int,
    expected_page_count: int,
) -> list[int]:
    succeeded = set(
        ArabicPrintedOcrPageCheckpoint.objects.filter(
            attempt_id=attempt_id,
            status=ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED,
            page_index__gte=start_index,
        ).values_list("page_index", flat=True)
    )
    return [
        page_index
        for page_index in range(start_index, expected_page_count)
        if page_index not in succeeded
    ]


def _stub_working_image_for_reuse(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
) -> ArabicPrintedWorkingImage:
    """Identity-only stub. Never used for Vision/crops; REUSE returns first."""
    return ArabicPrintedWorkingImage(
        width=checkpoint.oriented_image_width,
        height=checkpoint.oriented_image_height,
        jpeg_bytes=b"",
        mime_type="image/jpeg",
        sha256=checkpoint.oriented_image_sha256,
        byte_length=0,
        rgb_pixels=b"",
    )


def _document_result(
    *,
    attempt_id: int,
    document_id: int,
    pages: Sequence[ArabicPrintedBandedPageResult],
    failure_code: str | None,
) -> ArabicPrintedBandedDocumentResult:
    missing = tuple(missing_pages_for_arabic_printed_attempt(attempt_id))
    outcome = OUTCOME_COMPLETED if not missing else OUTCOME_PARTIAL
    return ArabicPrintedBandedDocumentResult(
        attempt_id=attempt_id,
        document_id=document_id,
        outcome=outcome,
        pages=tuple(pages),
        missing_page_indices=missing,
        failure_code=None if not missing else failure_code,
    )


def process_arabic_printed_banded_document(
    *,
    document_id: int,
    pages: Sequence[ArabicPrintedDocumentPageInput],
    load_working_image: WorkingImageLoader,
    gemini_api_key: str,
    cloud_vision_api_key: str,
    absolute_deadline_monotonic: float,
    language_hint: str,
    text_input_type: str,
    engine_key: str = "ANTIGRAVITY",
    prompt_variant: str = "printed",
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> ArabicPrintedBandedDocumentResult:
    """Claim and OCR every 0-based page under one document deadline.

    Checkpoint control errors propagate. An ordinary Phase 5A page failure
    stops the document loop and returns PARTIAL with ordered missing indexes.
    """
    identity = build_arabic_printed_attempt_identity(
        pages=_page_sources(pages),
        language_hint=language_hint,
        text_input_type=text_input_type,
        engine_key=engine_key,
        prompt_variant=prompt_variant,
    )
    attempt = get_or_create_arabic_printed_attempt(
        document_id=document_id,
        identity=identity,
    )
    ensure_arabic_printed_page_checkpoints(
        attempt_id=attempt.id,
        identity=identity,
    )

    collected: list[ArabicPrintedBandedPageResult] = []
    stop_code: str | None = None
    expected = identity.expected_page_count

    for page_index in range(expected):
        unfinished = _unfinished_executable_indices(
            attempt_id=attempt.id,
            start_index=page_index,
            expected_page_count=expected,
        )
        page_deadline: float | None = None
        if page_index in unfinished:
            page_deadline = arabic_printed_page_absolute_deadline(
                now=monotonic_fn(),
                document_deadline_monotonic=absolute_deadline_monotonic,
                unfinished_executable_pages=len(unfinished),
            )

        claim = claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=page_index,
            page_fingerprint=identity.page_fingerprints[page_index],
            source_content_fingerprint=identity.source_content_fingerprints[page_index],
            oriented_image_sha256=identity.oriented_image_sha256s[page_index],
        )
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)

        if claim.action == ArabicPrintedPageClaimAction.REUSE:
            page_result = process_claimed_arabic_printed_page(
                claim=claim,
                working_image=_stub_working_image_for_reuse(checkpoint),
                gemini_api_key=gemini_api_key,
                cloud_vision_api_key=cloud_vision_api_key,
                absolute_deadline_monotonic=monotonic_fn() + 1.0,
                poll_seconds=poll_seconds,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
            collected.append(page_result)
            continue

        if page_deadline is None:
            persist_arabic_printed_page_failure(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                failure_code=DOCUMENT_FAILURE_DEADLINE,
                failure_message=f"insufficient page start budget page_index={page_index}",
            )
            stop_code = DOCUMENT_FAILURE_DEADLINE
            break
        try:
            working_image = load_working_image(page_index)
        except CHECKPOINT_CONTROL_ERRORS:
            raise
        except Exception:
            persist_arabic_printed_page_failure(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                failure_code=DOCUMENT_FAILURE_IMAGE_LOAD,
                failure_message=f"working image load failed page_index={page_index}",
            )
            stop_code = DOCUMENT_FAILURE_IMAGE_LOAD
            break

        try:
            page_result = process_claimed_arabic_printed_page(
                claim=claim,
                working_image=working_image,
                gemini_api_key=gemini_api_key,
                cloud_vision_api_key=cloud_vision_api_key,
                absolute_deadline_monotonic=page_deadline,
                poll_seconds=poll_seconds,
                sleep_fn=sleep_fn,
                monotonic_fn=monotonic_fn,
            )
        except CHECKPOINT_CONTROL_ERRORS:
            raise

        collected.append(page_result)
        if page_result.outcome != OUTCOME_SUCCEEDED:
            stop_code = page_result.failure_code or OUTCOME_FAILED
            break

    return _document_result(
        attempt_id=attempt.id,
        document_id=document_id,
        pages=collected,
        failure_code=stop_code,
    )
