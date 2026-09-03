from __future__ import annotations

import hashlib
import uuid
from io import BytesIO
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from PIL import Image

from documents.models import (
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
    Document,
)
from documents.services.antigravity_engine import (
    AntigravityBandCancelResult,
    AntigravityBandCheckpointError,
    AntigravityBandOcrResult,
)
from documents.services.arabic_printed_banded_ocr import (
    BAND_FAILURE_CROP_MISMATCH,
    BAND_FAILURE_FALLBACK_AMBIGUOUS,
    BAND_FAILURE_PRIMARY_AMBIGUOUS,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    PAGE_FAILURE_BANDS_UNRESOLVED,
    PAGE_FAILURE_DEADLINE,
    PAGE_FAILURE_VISION_AMBIGUOUS,
    ArabicPrintedBandedPageResult,
    process_claimed_arabic_printed_page,
)
from documents.services.arabic_printed_banding import (
    ArabicPrintedWordBox as PlannerWordBox,
)
from documents.services.arabic_printed_banding import plan_arabic_printed_bands
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedBandPlan,
    ArabicPrintedCheckpointPersistenceRetryableError,
    ArabicPrintedIdentityMismatchError,
    ArabicPrintedPageClaim,
    ArabicPrintedPageClaimAction,
    ArabicPrintedPageSource,
    StaleArabicPrintedPageClaimError,
    build_arabic_printed_attempt_identity,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
    persist_arabic_printed_vision_plan,
)
from documents.services.archive_items import create_ocr_document
from documents.services.cloud_vision_document_text import (
    CloudVisionDocumentTextResult,
    CloudVisionSymbol,
    CloudVisionWord,
    encode_arabic_printed_band_crop,
    prepare_arabic_printed_working_image,
    reconstruct_draft_from_word_indexes,
)

MODULE = "documents.services.arabic_printed_banded_ocr"
PAGE_WIDTH = 40
PAGE_HEIGHT = 60
GEMINI_KEY = "gemini-band-key-DO-NOT-LEAK"
VISION_KEY = "vision-band-key-DO-NOT-LEAK"
RESPONSE_SHA = "b" * 64
BAND_ONE_TEXT = "النص الأول للنطاق"
BAND_TWO_TEXT = "النص الثاني للنطاق"
SECRET_REJECTED = "REJECTED_OUTPUT_MUST_NOT_APPEAR"


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _symbols(text: str) -> tuple[CloudVisionSymbol, ...]:
    chars = list(text)
    return tuple(
        CloudVisionSymbol(
            char,
            "SPACE" if index < len(chars) - 1 else "LINE_BREAK",
            False,
        )
        for index, char in enumerate(chars)
    )


def _vision_words() -> tuple[CloudVisionWord, ...]:
    return (
        CloudVisionWord(0, 1, 2, 10, 6, _symbols("ألف")),
        CloudVisionWord(1, 12, 2, 20, 6, _symbols("باء")),
        CloudVisionWord(2, 1, 30, 10, 34, _symbols("جيم")),
    )


def _detection() -> CloudVisionDocumentTextResult:
    words = _vision_words()
    return CloudVisionDocumentTextResult(
        words=words,
        draft_text="draft",
        response_sha256=RESPONSE_SHA,
    )


def _source_jpeg(period: int = 7) -> bytes:
    """Same-dimension page bytes; ``period`` changes the pixels and the digest."""
    image = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), (240, 240, 240))
    for y in range(PAGE_HEIGHT):
        for x in range(PAGE_WIDTH):
            if (x + y) % period == 0:
                image.putpixel((x, y), (10, 20, 30))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _ok_result(
    text: str,
    *,
    interaction_id: str = "ix-primary",
) -> AntigravityBandOcrResult:
    return AntigravityBandOcrResult(
        interaction_id=interaction_id,
        last_status="completed",
        polling_outcome="completed",
        step_types=("model_output",),
        total_input_tokens=10,
        total_output_tokens=5,
        total_thought_tokens=1,
        total_tokens=16,
        latency_seconds=1.25,
        marker_seen=True,
        coverage_ratio=1.0,
        output_non_whitespace=len(text),
        draft_non_whitespace=len(text),
        accepted=True,
        transcription=text,
        failure_kind=None,
        create_returned_interaction=True,
    )


def _rejected_result(
    *,
    interaction_id: str = "ix-primary",
    failure_kind: str = "coverage_ratio",
) -> AntigravityBandOcrResult:
    return AntigravityBandOcrResult(
        interaction_id=interaction_id,
        last_status="completed",
        polling_outcome="completed",
        step_types=("model_output",),
        total_input_tokens=10,
        total_output_tokens=5,
        total_thought_tokens=1,
        total_tokens=16,
        latency_seconds=2.0,
        marker_seen=True,
        coverage_ratio=0.1,
        output_non_whitespace=1,
        draft_non_whitespace=40,
        accepted=False,
        transcription="",
        failure_kind=failure_kind,
        create_returned_interaction=True,
    )


def _cancelled_result(
    *,
    interaction_id: str = "ix-primary",
) -> AntigravityBandOcrResult:
    return AntigravityBandOcrResult(
        interaction_id=interaction_id,
        last_status="cancelled",
        polling_outcome="completed",
        step_types=(),
        total_input_tokens=None,
        total_output_tokens=None,
        total_thought_tokens=None,
        total_tokens=None,
        latency_seconds=1.0,
        marker_seen=False,
        coverage_ratio=None,
        output_non_whitespace=None,
        draft_non_whitespace=None,
        accepted=False,
        transcription="",
        failure_kind="cancelled",
        create_returned_interaction=True,
    )


def _timeout_result(
    *,
    interaction_id: str = "ix-primary",
) -> AntigravityBandOcrResult:
    return AntigravityBandOcrResult(
        interaction_id=interaction_id,
        last_status="in_progress",
        polling_outcome="timeout",
        step_types=(),
        total_input_tokens=None,
        total_output_tokens=None,
        total_thought_tokens=None,
        total_tokens=None,
        latency_seconds=90.0,
        marker_seen=False,
        coverage_ratio=None,
        output_non_whitespace=None,
        draft_non_whitespace=None,
        accepted=False,
        transcription="",
        failure_kind="poll_timeout",
        create_returned_interaction=True,
    )


def _cancel(
    outcome: str,
    *,
    last_status: str | None = None,
    accepted: bool | None = None,
    transcription: str = "",
    http_status: int | None = 200,
) -> AntigravityBandCancelResult:
    return AntigravityBandCancelResult(
        cancel_outcome=outcome,
        last_status=last_status if last_status is not None else outcome,
        http_status=http_status,
        provider_error_code=None,
        evaluation_accepted=accepted,
        transcription=transcription,
        marker_seen=bool(accepted),
        coverage_ratio=1.0 if accepted else None,
        failure_kind=None if accepted else "poll_timeout",
    )


class ArabicPrintedBandedOcrTestBase(TestCase):
    def setUp(self) -> None:
        self.source_jpeg = _source_jpeg()
        self.working_image = prepare_arabic_printed_working_image(self.source_jpeg)
        self.document = create_ocr_document(
            title="Arabic printed banded orchestrator",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="arabic-printed-banded.pdf",
            mime_type="application/pdf",
        )
        page = ArabicPrintedPageSource(
            page_index=0,
            mime_type="image/jpeg",
            source_identity="arabic-printed-banded.pdf",
            source_content_fingerprint=_sha_bytes(self.source_jpeg),
            oriented_image_sha256=self.working_image.sha256,
            oriented_image_width=self.working_image.width,
            oriented_image_height=self.working_image.height,
        )
        self.identity = build_arabic_printed_attempt_identity(
            pages=[page],
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            engine_key="ANTIGRAVITY",
            prompt_variant="printed",
        )
        self.attempt = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=self.identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=self.attempt.id,
            identity=self.identity,
        )
        self.clock = Clock(0.0)

    def _claim(self) -> ArabicPrintedPageClaim:
        return claim_arabic_printed_page(
            attempt_id=self.attempt.id,
            page_index=0,
            page_fingerprint=self.identity.page_fingerprints[0],
            source_content_fingerprint=self.identity.source_content_fingerprints[0],
            oriented_image_sha256=self.identity.oriented_image_sha256s[0],
        )

    def _other_working_image(self):
        """A different page image with identical dimensions and a different SHA-256."""
        other = prepare_arabic_printed_working_image(_source_jpeg(period=3))
        self.assertEqual(other.width, self.working_image.width)
        self.assertEqual(other.height, self.working_image.height)
        self.assertNotEqual(other.sha256, self.working_image.sha256)
        return other

    def _run(
        self,
        claim: ArabicPrintedPageClaim,
        *,
        deadline: float = 1_000.0,
        working_image=None,
    ) -> ArabicPrintedBandedPageResult:
        return process_claimed_arabic_printed_page(
            claim=claim,
            working_image=self.working_image if working_image is None else working_image,
            gemini_api_key=GEMINI_KEY,
            cloud_vision_api_key=VISION_KEY,
            absolute_deadline_monotonic=deadline,
            poll_seconds=0.0,
            sleep_fn=lambda _seconds: None,
            monotonic_fn=self.clock,
        )

    def _persist_plan(self, claim: ArabicPrintedPageClaim) -> None:
        """Persist the same durable plan the orchestrator would create."""
        words = _vision_words()
        rects = plan_arabic_printed_bands(
            tuple(
                PlannerWordBox(w.index, w.xmin, w.ymin, w.xmax, w.ymax) for w in words
            ),
            image_width=self.working_image.width,
            image_height=self.working_image.height,
        )
        plans = []
        for rect in rects:
            crop = encode_arabic_printed_band_crop(
                self.working_image,
                left=rect.left,
                top=rect.top,
                right=rect.right,
                bottom=rect.bottom,
            )
            draft = reconstruct_draft_from_word_indexes(words, rect.word_indexes)
            plans.append(
                ArabicPrintedBandPlan(
                    band_index=rect.band_index - 1,
                    rect_x=rect.left,
                    rect_y=rect.top,
                    rect_width=rect.right - rect.left,
                    rect_height=rect.bottom - rect.top,
                    crop_mime=crop.mime_type,
                    crop_byte_length=crop.byte_length,
                    crop_sha256=crop.sha256,
                    vision_draft_text=draft,
                    vision_draft_byte_length=len(draft.encode("utf-8")),
                    vision_draft_sha256=_sha_text(draft),
                )
            )
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        checkpoint.cloud_vision_call_count = 1
        checkpoint.save(update_fields=["cloud_vision_call_count", "updated_at"])
        persist_arabic_printed_vision_plan(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            cloud_vision_response_sha256=RESPONSE_SHA,
            bands=plans,
        )

    def _bands(self, claim: ArabicPrintedPageClaim):
        return list(
            ArabicPrintedOcrBandCheckpoint.objects.filter(
                page_checkpoint_id=claim.checkpoint_id
            ).order_by("band_index")
        )

    def _assert_privacy(self, result: object, *forbidden: str) -> None:
        text = repr(result) + str(result)
        for secret in (
            GEMINI_KEY,
            VISION_KEY,
            SECRET_REJECTED,
            BAND_ONE_TEXT,
            BAND_TWO_TEXT,
            *forbidden,
        ):
            self.assertNotIn(secret, text)

    def _assert_band_diagnostics_safe(self, claim: ArabicPrintedPageClaim) -> None:
        for band in self._bands(claim):
            blob = " ".join(
                str(value)
                for value in (
                    band.primary_interaction_id,
                    band.primary_provider_status,
                    band.primary_failure_type,
                    band.primary_safe_diagnostics,
                    band.fallback_interaction_id,
                    band.fallback_provider_status,
                    band.fallback_failure_type,
                    band.fallback_safe_diagnostics,
                    band.cancel_confirmed_status,
                    band.cancel_safe_diagnostics,
                    band.failure_message,
                    band.prior_attempts,
                )
            )
            self.assertNotIn(GEMINI_KEY, blob)
            self.assertNotIn(VISION_KEY, blob)
            self.assertNotIn(SECRET_REJECTED, blob)


class ArabicPrintedBandedHappyPathTests(ArabicPrintedBandedOcrTestBase):
    def test_fresh_page_one_vision_and_accepted_primary(self):
        claim = self._claim()
        texts = [BAND_ONE_TEXT, BAND_TWO_TEXT]
        calls: dict[str, int] = {"vision": 0, "create": 0}

        def fake_vision(**kwargs):
            calls["vision"] += 1
            self.assertEqual(kwargs["api_key"], VISION_KEY)
            self.assertGreater(kwargs["remaining_timeout_seconds"], 0)
            return _detection()

        def fake_transcribe(**kwargs):
            index = calls["create"]
            calls["create"] += 1
            self.assertEqual(kwargs["attempt_kind"], "unassisted")
            self.assertEqual(kwargs["mime_type"], "image/jpeg")
            kwargs["on_interaction_created"](f"ix-primary-{index}")
            return _ok_result(texts[index], interaction_id=f"ix-primary-{index}")

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text", fake_vision),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(result.assembled_text, f"{BAND_ONE_TEXT}\n{BAND_TWO_TEXT}")
        self.assertEqual(
            result.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED,
        )
        self.assertEqual(result.runtime_engine_marker, "antigravity-banded:unassisted")
        self.assertIsNone(result.failure_code)
        self.assertEqual(calls, {"vision": 1, "create": 2})
        poll.assert_not_called()
        cancel.assert_not_called()
        self._assert_privacy(result)
        self._assert_band_diagnostics_safe(claim)

    def test_planner_one_based_indexes_persist_as_zero_based(self):
        claim = self._claim()

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix-1")
            return _ok_result(BAND_ONE_TEXT)

        with (
            patch(
                f"{MODULE}.detect_arabic_printed_document_text",
                lambda **kwargs: _detection(),
            ),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
        ):
            self._run(claim)

        bands = self._bands(claim)
        self.assertEqual([band.band_index for band in bands], [0, 1])
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(checkpoint.band_count, 2)
        self.assertEqual(checkpoint.cloud_vision_call_count, 1)
        for band in bands:
            self.assertEqual(band.rect_x, 0)
            self.assertEqual(band.rect_width, self.working_image.width)
            self.assertEqual(band.crop_mime, "image/jpeg")
            self.assertTrue(band.vision_draft_text.strip())
            self.assertEqual(
                band.vision_draft_sha256, _sha_text(band.vision_draft_text)
            )
        self.assertLess(bands[0].rect_y, bands[1].rect_y)

    def test_terminal_primary_rejection_runs_one_assisted_fallback(self):
        claim = self._claim()
        kinds: list[str] = []

        def fake_transcribe(**kwargs):
            kinds.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix-a")
            if kwargs["attempt_kind"] == "unassisted":
                return _rejected_result()
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-b")

        with (
            patch(
                f"{MODULE}.detect_arabic_printed_document_text",
                lambda **kwargs: _detection(),
            ),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(
            kinds,
            ["unassisted", "assisted_fallback", "unassisted", "assisted_fallback"],
        )
        cancel.assert_not_called()
        for band in self._bands(claim):
            self.assertEqual(band.create_call_count, 2)
            self.assertEqual(
                band.selected_result,
                ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK,
            )
        self.assertEqual(
            result.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.ASSISTED,
        )


class ArabicPrintedBandedCancelTests(ArabicPrintedBandedOcrTestBase):
    def _single_band_setup(self, claim: ArabicPrintedPageClaim) -> None:
        """Persist the plan and pre-succeed band 1 so tests focus on band 0."""
        self._persist_plan(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=1
        )
        band.status = ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
        band.selected_result = ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED
        band.create_call_count = 1
        band.transcription_text = BAND_TWO_TEXT
        band.transcription_sha256 = _sha_text(BAND_TWO_TEXT)
        band.transcription_byte_length = len(BAND_TWO_TEXT.encode("utf-8"))
        band.completed_at = timezone.now()
        band.save()

    def _seed_stale_cancel_pending(
        self,
        claim: ArabicPrintedPageClaim,
        *,
        band_index: int = 0,
        interaction_id: str = "ix-stale",
        cancel_confirmed_status: str = "",
    ) -> ArabicPrintedOcrBandCheckpoint:
        """Document-322 shape: CANCEL_PENDING with id and blank confirmation."""
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=band_index
        )
        band.status = ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
        band.create_call_count = 1
        band.primary_interaction_id = interaction_id
        band.primary_provider_status = "in_progress"
        band.cancel_attempted = True
        band.cancel_attempted_at = timezone.now()
        band.cancel_http_status = None
        band.cancel_confirmed_status = cancel_confirmed_status
        band.save()
        return band

    def test_primary_timeout_persists_id_then_cancel_pending_then_fallback(self):
        claim = self._claim()
        self._single_band_setup(claim)
        order: list[str] = []

        def fake_transcribe(**kwargs):
            order.append(f"create:{kwargs['attempt_kind']}")
            kwargs["on_interaction_created"]("ix-timeout")
            order.append("id_persisted")
            band = ArabicPrintedOcrBandCheckpoint.objects.get(
                page_checkpoint_id=claim.checkpoint_id, band_index=0
            )
            self.assertEqual(band.primary_interaction_id, "ix-timeout")
            if kwargs["attempt_kind"] == "unassisted":
                return _timeout_result(interaction_id="ix-timeout")
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-fb")

        def fake_cancel(**kwargs):
            order.append("cancel")
            band = ArabicPrintedOcrBandCheckpoint.objects.get(
                page_checkpoint_id=claim.checkpoint_id, band_index=0
            )
            self.assertEqual(
                band.status,
                ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
            )
            self.assertTrue(band.cancel_attempted)
            return _cancel("cancelled")

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.cancel_antigravity_interaction", fake_cancel),
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim)

        vision.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(
            order,
            [
                "create:unassisted",
                "id_persisted",
                "cancel",
                "create:assisted_fallback",
                "id_persisted",
            ],
        )
        band = self._bands(claim)[0]
        self.assertEqual(band.create_call_count, 2)
        self.assertEqual(band.cancel_confirmed_status, "cancelled")
        self._assert_band_diagnostics_safe(claim)

    def test_cancel_completed_race_with_accepted_output_skips_fallback(self):
        claim = self._claim()
        self._single_band_setup(claim)
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix-race")
            return _timeout_result(interaction_id="ix-race")

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(
                f"{MODULE}.cancel_antigravity_interaction",
                lambda **kwargs: _cancel(
                    "completed", accepted=True, transcription=BAND_ONE_TEXT
                ),
            ),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(creates, ["unassisted"])
        band = self._bands(claim)[0]
        self.assertEqual(band.create_call_count, 1)
        self.assertEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
        )
        self.assertEqual(band.transcription_text, BAND_ONE_TEXT)

    def test_inconclusive_primary_cancel_blocks_fallback_and_low_quality(self):
        claim = self._claim()
        self._single_band_setup(claim)
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix-unknown")
            return _timeout_result(interaction_id="ix-unknown")

        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _timeout_result(interaction_id="ix-unknown")

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(
                f"{MODULE}.cancel_antigravity_interaction",
                lambda **kwargs: _cancel(
                    "http_error", last_status=None, http_status=500
                ),
            ),
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_BANDS_UNRESOLVED)
        self.assertEqual(creates, ["unassisted"])
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]["interaction_id"], "ix-unknown")
        self.assertEqual(polls[0]["last_status"], "in_progress")
        band = self._bands(claim)[0]
        self.assertEqual(band.create_call_count, 1)
        self.assertEqual(
            band.status, ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
        )
        self.assertNotEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self._assert_privacy(result)

    def test_fallback_timeout_with_confirmed_cancel_selects_low_quality(self):
        claim = self._claim()
        self._single_band_setup(claim)
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix-fb")
            if kwargs["attempt_kind"] == "unassisted":
                return _rejected_result()
            return _timeout_result(interaction_id="ix-fb")

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(
                f"{MODULE}.cancel_antigravity_interaction",
                lambda **kwargs: _cancel("cancelled"),
            ),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(creates, ["unassisted", "assisted_fallback"])
        band = self._bands(claim)[0]
        self.assertEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertEqual(
            result.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertTrue(
            result.runtime_engine_marker.startswith(
                "antigravity-banded:cloud-vision-lq:"
            )
        )

    def test_persisted_completed_cancel_recovers_with_one_in_progress_poll(self):
        claim = self._claim()
        self._single_band_setup(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.status = ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
        band.create_call_count = 1
        band.primary_interaction_id = "ix-stored"
        band.cancel_attempted = True
        band.cancel_attempted_at = timezone.now()
        band.cancel_confirmed_status = "completed"
        band.save()

        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-stored")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim)

        transcribe.assert_not_called()
        cancel.assert_not_called()
        vision.assert_not_called()
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]["interaction_id"], "ix-stored")
        self.assertEqual(polls[0]["last_status"], "in_progress")
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        recovered = self._bands(claim)[0]
        self.assertEqual(recovered.create_call_count, 1)
        self.assertEqual(
            recovered.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
        )
        self.assertEqual(recovered.transcription_text, BAND_ONE_TEXT)

    def test_stale_cancel_pending_blank_confirmation_recovers_completed_output(self):
        claim = self._claim()
        self._single_band_setup(claim)
        self._seed_stale_cancel_pending(claim, interaction_id="ix-stale")
        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-stale")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim)

        transcribe.assert_not_called()
        cancel.assert_not_called()
        vision.assert_not_called()
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]["interaction_id"], "ix-stale")
        self.assertEqual(polls[0]["last_status"], "in_progress")
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        bands = self._bands(claim)
        self.assertEqual(
            bands[0].status, ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
        )
        self.assertEqual(bands[0].create_call_count, 1)
        self.assertEqual(bands[0].transcription_text, BAND_ONE_TEXT)
        self.assertEqual(
            bands[1].status, ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
        )
        self.assertEqual(bands[1].transcription_text, BAND_TWO_TEXT)

    def test_stale_cancel_pending_blank_confirmation_cancelled_uses_fallback(self):
        claim = self._claim()
        self._single_band_setup(claim)
        self._seed_stale_cancel_pending(claim, interaction_id="ix-stale")
        creates: list[str] = []
        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _cancelled_result(interaction_id="ix-stale")

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix-fb")
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-fb")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim)

        cancel.assert_not_called()
        vision.assert_not_called()
        self.assertEqual(creates, ["assisted_fallback"])
        self.assertEqual(len(polls), 1)
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        bands = self._bands(claim)
        self.assertEqual(bands[0].create_call_count, 2)
        self.assertEqual(bands[0].cancel_confirmed_status, "cancelled")
        self.assertEqual(
            bands[0].selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK,
        )
        self.assertEqual(
            bands[1].status, ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
        )
        self.assertEqual(bands[1].transcription_text, BAND_TWO_TEXT)

    def test_stale_cancel_pending_blank_confirmation_unresolved_still_fail_closed(self):
        claim = self._claim()
        self._single_band_setup(claim)
        self._seed_stale_cancel_pending(claim, interaction_id="ix-stale")
        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _timeout_result(interaction_id="ix-stale")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim)

        transcribe.assert_not_called()
        cancel.assert_not_called()
        vision.assert_not_called()
        self.assertEqual(len(polls), 1)
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_BANDS_UNRESOLVED)
        bands = self._bands(claim)
        self.assertEqual(
            bands[0].status, ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
        )
        self.assertEqual(bands[0].create_call_count, 1)
        self.assertEqual(
            bands[1].status, ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
        )

    def test_stale_cancel_pending_blank_confirmation_without_id_does_not_poll(self):
        claim = self._claim()
        self._single_band_setup(claim)
        self._seed_stale_cancel_pending(claim, interaction_id="")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        poll.assert_not_called()
        transcribe.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_BANDS_UNRESOLVED)
        self.assertEqual(
            self._bands(claim)[0].status,
            ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
        )

    def test_stale_cancel_pending_cancelled_without_budget_uses_low_quality(self):
        claim = self._claim()
        self._single_band_setup(claim)
        self._seed_stale_cancel_pending(claim, interaction_id="ix-stale")

        def fake_poll(**kwargs):
            self.clock.now = 1_000.0
            return _cancelled_result(interaction_id="ix-stale")

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
        ):
            result = self._run(claim, deadline=1_000.0)

        transcribe.assert_not_called()
        cancel.assert_not_called()
        vision.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        band = self._bands(claim)[0]
        self.assertEqual(band.create_call_count, 1)
        self.assertEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertEqual(
            self._bands(claim)[1].status,
            ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED,
        )

    def test_fallback_timeout_with_inconclusive_cancel_has_no_low_quality(self):
        claim = self._claim()
        self._single_band_setup(claim)

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix-fb")
            if kwargs["attempt_kind"] == "unassisted":
                return _rejected_result()
            return _timeout_result(interaction_id="ix-fb")

        def fake_poll(**kwargs):
            return _timeout_result(interaction_id="ix-fb")

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(
                f"{MODULE}.cancel_antigravity_interaction",
                lambda **kwargs: _cancel("other", last_status="weird"),
            ),
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_FAILED)
        band = self._bands(claim)[0]
        self.assertNotEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertEqual(
            band.status, ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
        )


class ArabicPrintedBandedLowQualityTests(ArabicPrintedBandedOcrTestBase):
    def test_terminal_fallback_failure_selects_stored_vision_low_quality(self):
        claim = self._claim()
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix")
            return _rejected_result(failure_kind="incomplete_output")

        with (
            patch(
                f"{MODULE}.detect_arabic_printed_document_text",
                lambda **kwargs: _detection(),
            ),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(len(creates), 4)
        cancel.assert_not_called()
        bands = self._bands(claim)
        for band in bands:
            self.assertEqual(
                band.selected_result,
                ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
            )
            self.assertEqual(band.transcription_text, band.vision_draft_text.strip())
        self.assertEqual(
            result.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertNotEqual(
            result.runtime_engine_marker, "antigravity-banded:unassisted"
        )

    def test_primary_failure_without_time_for_fallback_uses_low_quality(self):
        claim = self._claim()
        self._persist_plan(claim)
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            kwargs["on_interaction_created"]("ix")
            self.clock.now = 999_999.0
            return _rejected_result()

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        self.assertEqual(creates, ["unassisted"])
        cancel.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        first = self._bands(claim)[0]
        self.assertEqual(
            first.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertEqual(first.create_call_count, 1)


class ArabicPrintedBandedResumeTests(ArabicPrintedBandedOcrTestBase):
    def test_durable_plan_resume_makes_no_vision_call_and_verifies_crops(self):
        claim = self._claim()
        self._persist_plan(claim)
        stored = self._bands(claim)

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix")
            return _ok_result(BAND_ONE_TEXT)

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
        ):
            result = self._run(claim)

        vision.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        after = self._bands(claim)
        for before, now in zip(stored, after, strict=True):
            self.assertEqual(before.crop_sha256, now.crop_sha256)
            self.assertEqual(before.crop_byte_length, now.crop_byte_length)
            self.assertEqual(before.vision_draft_text, now.vision_draft_text)

    def test_tampered_stored_crop_hash_fails_closed_without_provider_calls(self):
        claim = self._claim()
        self._persist_plan(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.crop_sha256 = "0" * 64
        band.save(update_fields=["crop_sha256"])

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
        ):
            result = self._run(claim)

        vision.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, BAND_FAILURE_CROP_MISMATCH)

    def test_succeeded_page_reuse_returns_persisted_text_with_zero_http(self):
        claim = self._claim()
        self._persist_plan(claim)

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix")
            return _ok_result(BAND_ONE_TEXT)

        with patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe):
            first = self._run(claim)
        self.assertEqual(first.outcome, OUTCOME_SUCCEEDED)

        reuse_claim = self._claim()
        self.assertEqual(reuse_claim.action, ArabicPrintedPageClaimAction.REUSE)
        self.assertIsNone(reuse_claim.lease_token)

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            reused = self._run(reuse_claim)

        vision.assert_not_called()
        transcribe.assert_not_called()
        poll.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(reused.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(reused.assembled_text, first.assembled_text)
        self.assertEqual(reused.page_quality, first.page_quality)
        self.assertEqual(reused.runtime_engine_marker, first.runtime_engine_marker)
        self._assert_privacy(reused)

    def _resume_running_band(self, status: str, id_field: str, create_count: int):
        claim = self._claim()
        self._persist_plan(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.status = status
        band.create_call_count = create_count
        setattr(band, id_field, "ix-resumed")
        band.save()

        polls: list[dict[str, Any]] = []

        def fake_poll(**kwargs):
            polls.append(kwargs)
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-resumed")

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix-new")
            return _ok_result(BAND_TWO_TEXT)

        with (
            patch(f"{MODULE}.poll_arabic_printed_band_interaction", fake_poll),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        self.assertEqual(len(polls), 1)
        self.assertEqual(polls[0]["interaction_id"], "ix-resumed")
        first = self._bands(claim)[0]
        self.assertEqual(first.create_call_count, create_count)

    def test_primary_running_with_id_resumes_polling_without_creating(self):
        self._resume_running_band(
            ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
            "primary_interaction_id",
            1,
        )

    def test_fallback_running_with_id_resumes_polling_without_creating(self):
        self._resume_running_band(
            ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
            "fallback_interaction_id",
            2,
        )

    def test_reserved_running_state_without_id_never_creates_again(self):
        claim = self._claim()
        self._persist_plan(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.status = ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
        band.create_call_count = 1
        band.save()

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim)

        transcribe.assert_not_called()
        poll.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        first = self._bands(claim)[0]
        self.assertEqual(first.failure_code, BAND_FAILURE_PRIMARY_AMBIGUOUS)
        self.assertEqual(first.create_call_count, 1)

    def _fence_ambiguous_reservation(
        self,
        status: str,
        create_count: int,
        expected_code: str,
    ) -> None:
        claim = self._claim()
        self._persist_plan(claim)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.status = status
        band.create_call_count = create_count
        band.save()

        with patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe:
            first = self._run(claim)

        transcribe.assert_not_called()
        self.assertEqual(first.outcome, OUTCOME_FAILED)
        fenced = self._bands(claim)[0]
        self.assertEqual(fenced.failure_code, expected_code)
        self.assertEqual(fenced.create_call_count, create_count)

        reclaim = self._claim()
        self.assertEqual(reclaim.action, ArabicPrintedPageClaimAction.EXECUTE)

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as recreate,
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
            patch(f"{MODULE}.reserve_arabic_printed_primary_create") as primary,
            patch(f"{MODULE}.reserve_arabic_printed_fallback_create") as fallback,
            patch(
                f"{MODULE}.select_arabic_printed_band_cloud_vision_low_quality"
            ) as low_quality,
        ):
            second = self._run(reclaim)

        vision.assert_not_called()
        recreate.assert_not_called()
        poll.assert_not_called()
        cancel.assert_not_called()
        primary.assert_not_called()
        fallback.assert_not_called()
        low_quality.assert_not_called()
        self.assertEqual(second.outcome, OUTCOME_FAILED)
        self.assertEqual(second.failure_code, PAGE_FAILURE_BANDS_UNRESOLVED)
        still_fenced = self._bands(claim)[0]
        self.assertEqual(still_fenced.failure_code, expected_code)
        self.assertEqual(still_fenced.create_call_count, create_count)
        self.assertEqual(
            still_fenced.status, ArabicPrintedOcrBandCheckpoint.Status.FAILED
        )
        self.assertEqual(still_fenced.selected_result, "")

    def test_ambiguous_primary_reservation_stays_fenced_across_reclaim(self):
        self._fence_ambiguous_reservation(
            ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
            1,
            BAND_FAILURE_PRIMARY_AMBIGUOUS,
        )

    def test_ambiguous_fallback_reservation_stays_fenced_across_reclaim(self):
        self._fence_ambiguous_reservation(
            ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
            2,
            BAND_FAILURE_FALLBACK_AMBIGUOUS,
        )

    def test_vision_reserved_without_plan_never_calls_vision_again(self):
        claim = self._claim()
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        checkpoint.cloud_vision_call_count = 1
        checkpoint.save(update_fields=["cloud_vision_call_count"])

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
        ):
            result = self._run(claim)

        vision.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_VISION_AMBIGUOUS)


class ArabicPrintedBandedDeadlineTests(ArabicPrintedBandedOcrTestBase):
    def test_expired_deadline_prevents_any_provider_start(self):
        claim = self._claim()

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
        ):
            result = self._run(claim, deadline=-1.0)

        vision.assert_not_called()
        transcribe.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_DEADLINE)
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(checkpoint.cloud_vision_call_count, 0)

    def test_expired_deadline_before_band_start_does_not_create(self):
        claim = self._claim()
        self._persist_plan(claim)
        self.clock.now = 10_000.0

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            result = self._run(claim, deadline=1_000.0)

        transcribe.assert_not_called()
        cancel.assert_not_called()
        self.assertEqual(result.outcome, OUTCOME_FAILED)
        self.assertEqual(result.failure_code, PAGE_FAILURE_BANDS_UNRESOLVED)


class ArabicPrintedBandedControlErrorTests(ArabicPrintedBandedOcrTestBase):
    def test_stale_lease_token_propagates(self):
        claim = self._claim()
        stale = ArabicPrintedPageClaim(
            ArabicPrintedPageClaimAction.EXECUTE,
            claim.checkpoint_id,
            claim.page_index,
            lease_token=uuid.uuid4(),
        )

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
        ):
            with self.assertRaises(StaleArabicPrintedPageClaimError):
                self._run(stale)

        vision.assert_not_called()
        transcribe.assert_not_called()

    def test_missing_lease_token_propagates(self):
        claim = self._claim()
        broken = ArabicPrintedPageClaim(
            ArabicPrintedPageClaimAction.EXECUTE,
            claim.checkpoint_id,
            claim.page_index,
            lease_token=None,
        )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            self._run(broken)

    def test_fresh_execute_with_mismatched_oriented_sha_raises_identity_error(self):
        claim = self._claim()

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            with self.assertRaises(ArabicPrintedIdentityMismatchError):
                self._run(claim, working_image=self._other_working_image())

        vision.assert_not_called()
        transcribe.assert_not_called()
        cancel.assert_not_called()
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(
            pk=claim.checkpoint_id
        )
        self.assertEqual(checkpoint.cloud_vision_call_count, 0)
        self.assertEqual(
            checkpoint.status, ArabicPrintedOcrPageCheckpoint.Status.RUNNING
        )

    def test_durable_plan_execute_with_mismatched_oriented_sha_raises_identity_error(
        self,
    ):
        claim = self._claim()
        self._persist_plan(claim)
        before = self._bands(claim)

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text") as vision,
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            with self.assertRaises(ArabicPrintedIdentityMismatchError):
                self._run(claim, working_image=self._other_working_image())

        vision.assert_not_called()
        transcribe.assert_not_called()
        poll.assert_not_called()
        cancel.assert_not_called()
        for stored, now in zip(before, self._bands(claim), strict=True):
            self.assertEqual(stored.status, now.status)
            self.assertEqual(stored.crop_sha256, now.crop_sha256)
            self.assertEqual(stored.create_call_count, now.create_call_count)

    def test_matching_oriented_sha_follows_the_happy_path(self):
        claim = self._claim()
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(
            pk=claim.checkpoint_id
        )
        self.assertEqual(checkpoint.oriented_image_sha256, self.working_image.sha256)

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix")
            return _ok_result(BAND_ONE_TEXT)

        with (
            patch(
                f"{MODULE}.detect_arabic_printed_document_text",
                lambda **kwargs: _detection(),
            ),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
        ):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)

    def test_page_index_mismatch_propagates(self):
        claim = self._claim()
        broken = ArabicPrintedPageClaim(
            ArabicPrintedPageClaimAction.EXECUTE,
            claim.checkpoint_id,
            claim.page_index + 5,
            lease_token=claim.lease_token,
        )
        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            self._run(broken)

    def test_checkpoint_persistence_error_propagates(self):
        claim = self._claim()

        def boom(**kwargs):
            raise ArabicPrintedCheckpointPersistenceRetryableError(
                stage="persist_vision_plan", page_index=0
            )

        with (
            patch(
                f"{MODULE}.detect_arabic_printed_document_text",
                lambda **kwargs: _detection(),
            ),
            patch(f"{MODULE}.persist_arabic_printed_vision_plan", boom),
            patch(f"{MODULE}.transcribe_band_with_antigravity") as transcribe,
        ):
            with self.assertRaises(ArabicPrintedCheckpointPersistenceRetryableError):
                self._run(claim)

        transcribe.assert_not_called()

    def test_callback_persistence_failure_stops_poll_and_fallback(self):
        claim = self._claim()
        self._persist_plan(claim)
        creates: list[str] = []

        def fake_transcribe(**kwargs):
            creates.append(kwargs["attempt_kind"])
            cause = StaleArabicPrintedPageClaimError("stale during checkpoint")
            raise AntigravityBandCheckpointError(
                interaction_id="ix-callback",
                exception_class="StaleArabicPrintedPageClaimError",
            ) from cause

        with (
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
            patch(f"{MODULE}.poll_arabic_printed_band_interaction") as poll,
            patch(f"{MODULE}.cancel_antigravity_interaction") as cancel,
        ):
            with self.assertRaises(StaleArabicPrintedPageClaimError):
                self._run(claim)

        self.assertEqual(creates, ["unassisted"])
        poll.assert_not_called()
        cancel.assert_not_called()


class ArabicPrintedBandedPrivacyAndBoundsTests(ArabicPrintedBandedOcrTestBase):
    def test_result_repr_omits_assembled_text(self):
        result = ArabicPrintedBandedPageResult(
            checkpoint_id=7,
            page_index=0,
            outcome=OUTCOME_SUCCEEDED,
            assembled_text=BAND_ONE_TEXT,
            page_quality="UNASSISTED",
            runtime_engine_marker="antigravity-banded:unassisted",
            failure_code=None,
        )
        self.assertNotIn(BAND_ONE_TEXT, repr(result))
        self.assertNotIn(BAND_ONE_TEXT, str(result))
        self.assertIn("assembled_text_byte_length", repr(result))
        self.assertEqual(result.assembled_text, BAND_ONE_TEXT)

    def test_call_accounting_one_vision_and_at_most_two_creates_per_band(self):
        claim = self._claim()
        vision_calls = {"n": 0}
        per_band: dict[str, int] = {}

        def fake_vision(**kwargs):
            vision_calls["n"] += 1
            return _detection()

        def fake_transcribe(**kwargs):
            draft = kwargs["vision_draft_text"]
            per_band[draft] = per_band.get(draft, 0) + 1
            kwargs["on_interaction_created"]("ix")
            return _rejected_result()

        with (
            patch(f"{MODULE}.detect_arabic_printed_document_text", fake_vision),
            patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe),
        ):
            result = self._run(claim)

        self.assertEqual(vision_calls["n"], 1)
        self.assertEqual(len(per_band), 2)
        for count in per_band.values():
            self.assertLessEqual(count, 2)
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(checkpoint.cloud_vision_call_count, 1)
        self.assertLessEqual(checkpoint.antigravity_create_count, 4)
        for band in self._bands(claim):
            self.assertLessEqual(band.create_call_count, 2)
        self._assert_privacy(result)

    def test_persisted_diagnostics_are_bounded_and_privacy_safe(self):
        claim = self._claim()
        self._persist_plan(claim)

        def fake_transcribe(**kwargs):
            kwargs["on_interaction_created"]("ix-diag")
            return _ok_result(BAND_ONE_TEXT, interaction_id="ix-diag")

        with patch(f"{MODULE}.transcribe_band_with_antigravity", fake_transcribe):
            result = self._run(claim)

        self.assertEqual(result.outcome, OUTCOME_SUCCEEDED)
        for band in self._bands(claim):
            self.assertEqual(band.primary_interaction_id, "ix-diag")
            self.assertEqual(band.primary_provider_status, "completed")
            self.assertEqual(band.primary_latency_ms, 1250)
            self.assertLessEqual(len(band.primary_safe_diagnostics), 512)
            self.assertNotIn(BAND_ONE_TEXT, band.primary_safe_diagnostics)
            self.assertNotIn(GEMINI_KEY, band.primary_safe_diagnostics)
        self._assert_band_diagnostics_safe(claim)
