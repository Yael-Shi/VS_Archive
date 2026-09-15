"""Bounded Hebrew printed RECITATION crop recovery."""

from __future__ import annotations

import io
import uuid
from unittest.mock import patch

from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase
from PIL import Image

from documents.models import Document, DocumentTextResult, GeminiOcrPageCheckpoint
from documents.services.gemini_engine import (
    GeminiQuotaError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    GeminiResponseMetadata,
    GeminiResult,
    GeminiTranscriptionContract,
    gemini_transcription_contract,
)
from documents.services.gemini_hebrew_printed_crop_recovery import (
    HEBREW_PRINTED_RECITATION_CROP_COUNT,
    HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY,
    REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY,
    HebrewPrintedCropPlan,
    crop_assembly_engine_name,
    hebrew_printed_recitation_crop_recovery_policy,
    horizontal_crop_boxes,
    merge_overlapping_crop_texts,
    overlap_px_for_height,
    plan_hebrew_printed_recitation_crops,
)
from documents.services.gemini_models import GEMINI_36_FLASH_MODEL
from documents.services.gemini_page_checkpoints import (
    GeminiAttemptIdentity,
    build_gemini_attempt_identity,
)
from documents.services.htr_adapters.base import (
    EnginePageCheckpointPersistenceRetryableError,
    EnginePageIncompleteError,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.page_extraction import PageImage
from documents.test_gemini_page_checkpoints import _document
from documents.test_gemini_recitation_model_fallback import (
    _ExpectedIncomplete,
    _response_error,
)


def _png_bytes(*, width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), (240, 240, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png_page(*, width: int = 80, height: int = 240, page_index: int = 1) -> PageImage:
    return PageImage(
        page_index=page_index,
        image_bytes=_png_bytes(width=width, height=height),
        mime_type="image/png",
        source_identity="page.png",
        source_content_fingerprint="b" * 64,
    )


def _identity(
    page: PageImage,
    *,
    language_hint: str | None = Document.Language.HEBREW,
    text_input_type: str | None = Document.TextInputType.PRINTED,
    prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
    contract: GeminiTranscriptionContract | None = None,
) -> GeminiAttemptIdentity:
    resolved_contract = contract or gemini_transcription_contract(
        prompt_variant=prompt_variant,
        language_hint=language_hint,
        temperature=0.2,
    )
    return build_gemini_attempt_identity(
        pages=[page],
        language_hint=language_hint,
        text_input_type=text_input_type,
        handwriting_type=None,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=prompt_variant,
        model_candidates=("gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL),
        contract=resolved_contract,
        min_text_length=20,
        double_pass=False,
        consistency_min_ratio=0.85,
        temperature=0.2,
        top_k=40,
        top_p=0.95,
        max_output_tokens=4096,
        max_output_tokens_hard_cap=32768,
    )


def _require_crop_plan(page: PageImage) -> HebrewPrintedCropPlan:
    plan = plan_hebrew_printed_recitation_crops(page)
    if plan is None:
        raise AssertionError("expected a Hebrew printed crop plan")
    return plan


class HebrewPrintedCropGeometryTests(SimpleTestCase):
    def test_two_horizontal_crops_are_deterministic_and_overlapping(self):
        height = 240
        overlap = overlap_px_for_height(height)
        boxes = horizontal_crop_boxes(width=80, height=height)

        self.assertEqual(len(boxes), HEBREW_PRINTED_RECITATION_CROP_COUNT)
        self.assertEqual(overlap, 64)
        self.assertEqual(
            [(box.left, box.top, box.right, box.bottom) for box in boxes],
            [(0, 0, 80, 184), (0, 56, 80, 240)],
        )
        self.assertLess(boxes[1].top, boxes[0].bottom)
        self.assertEqual(boxes[0].top, 0)
        self.assertEqual(boxes[1].bottom, height)

    def test_short_pages_do_not_plan_crops(self):
        self.assertEqual(horizontal_crop_boxes(width=80, height=191), ())
        page = _png_page(height=100)
        self.assertIsNone(plan_hebrew_printed_recitation_crops(page))

    def test_invalid_image_bytes_do_not_plan_crops(self):
        page = PageImage(
            page_index=1,
            image_bytes=b"not-an-image",
            mime_type="image/png",
        )
        self.assertIsNone(plan_hebrew_printed_recitation_crops(page))

    def test_overlap_lines_are_not_concatenated_twice(self):
        merged = merge_overlapping_crop_texts(
            "line one\noverlap line\n",
            "overlap line\nline two\n",
        )
        self.assertEqual(merged, "line one\noverlap line\nline two")

    def test_crop_engine_name_is_crop_marker_when_models_match(self):
        first = crop_assembly_engine_name(["gemini-3.6-flash", "gemini-3.6-flash"])
        second = crop_assembly_engine_name(["gemini-3.6-flash", "gemini-3.6-flash"])
        self.assertEqual(first, second)
        self.assertRegex(first, r"^gemini-crop:[0-9a-f]{48}$")
        self.assertNotEqual(first, "gemini-3.6-flash")
        self.assertLessEqual(len(first), 64)

    def test_crop_engine_name_is_stable_for_mixed_models(self):
        first = crop_assembly_engine_name(["gemini-3.1-flash-lite", "gemini-3.6-flash"])
        second = crop_assembly_engine_name(
            ["gemini-3.1-flash-lite", "gemini-3.6-flash"]
        )
        self.assertEqual(first, second)
        self.assertRegex(first, r"^gemini-crop:[0-9a-f]{48}$")
        self.assertLessEqual(len(first), 64)


class HebrewPrintedCropIdentityTests(SimpleTestCase):
    def test_policy_is_hebrew_printed_only(self):
        self.assertEqual(
            hebrew_printed_recitation_crop_recovery_policy(
                language_hint=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.PRINTED,
            ),
            HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY,
        )
        self.assertEqual(
            hebrew_printed_recitation_crop_recovery_policy(
                language_hint=Document.Language.ENGLISH,
                text_input_type=Document.TextInputType.HANDWRITTEN,
            ),
            "",
        )

    def test_hebrew_printed_identity_changes_when_crop_policy_is_present(self):
        page = _png_page()
        with_policy = _identity(page)
        with patch(
            "documents.services.gemini_page_checkpoints."
            "hebrew_printed_recitation_crop_recovery_policy",
            return_value="",
        ):
            without_policy = _identity(page)
        self.assertNotEqual(
            with_policy.config_fingerprint,
            without_policy.config_fingerprint,
        )
        self.assertNotEqual(
            with_policy.identity_fingerprint,
            without_policy.identity_fingerprint,
        )

    def test_english_handwritten_identity_omits_crop_policy_key(self):
        page = _png_page()
        contract = gemini_transcription_contract(
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            language_hint=Document.Language.ENGLISH,
            temperature=0.2,
        )
        english = _identity(
            page,
            language_hint=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            contract=contract,
        )
        hebrew = _identity(page)
        self.assertNotEqual(english.config_fingerprint, hebrew.config_fingerprint)


class HebrewPrintedCropRecoveryAdapterTests(SimpleTestCase):
    def _execute(
        self,
        adapter: GeminiAdapter,
        page: PageImage,
        *,
        language_hint: str | None = Document.Language.HEBREW,
        prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
        model_candidates: list[str] | None = None,
        hebrew_printed_crop_recovery_enabled: bool = True,
    ) -> None:
        candidates = (
            model_candidates
            if model_candidates is not None
            else ["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL]
        )
        adapter._execute_claimed_page(
            page=page,
            language_hint=language_hint,
            prompt_variant=prompt_variant,
            model_candidates=candidates,
            recitation_model_fallback_enabled=True,
            hebrew_general_model_fallback_enabled=False,
            kwargs={"max_output_tokens": 4096},
            checkpoint_id=1,
            lease_token=uuid.uuid4(),
            attempt_id=2,
            hebrew_printed_crop_recovery_enabled=(hebrew_printed_crop_recovery_enabled),
            hebrew_printed_mixed_script_enabled=True,
        )

    def test_crop_recovery_runs_only_after_full_page_recitation_chain(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        recitation_b = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model=GEMINI_36_FLASH_MODEL,
            attempt=2,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                if model_name == "gemini-3.1-flash-lite":
                    raise recitation_a
                raise recitation_b
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(
                    text="line one\noverlap line",
                    engine_name=model_name,
                )
            return GeminiResult(
                text="overlap line\nline two",
                engine_name=model_name,
            )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 4)
        self.assertEqual(
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
            [
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
                "gemini-3.1-flash-lite",
                "gemini-3.1-flash-lite",
            ],
        )
        self.assertEqual(
            mock_transcribe.call_args_list[2].kwargs["max_provider_calls"],
            1,
        )
        self.assertEqual(
            mock_transcribe.call_args_list[3].kwargs["max_provider_calls"],
            1,
        )
        mock_persist_failure.assert_not_called()
        persist_kwargs = mock_persist_success.call_args.kwargs
        self.assertEqual(persist_kwargs["text"], "line one\noverlap line\nline two")
        self.assertRegex(
            persist_kwargs["actual_model"],
            r"^gemini-crop:[0-9a-f]{48}$",
        )
        self.assertTrue(persist_kwargs["needs_review"])
        self.assertEqual(
            persist_kwargs["review_reasons"],
            [REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY],
        )

    def test_successful_full_page_fallback_does_not_crop(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    recitation,
                    GeminiResult(text="full page", engine_name=GEMINI_36_FLASH_MODEL),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 2)
        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            GEMINI_36_FLASH_MODEL,
        )
        self.assertEqual(mock_persist_success.call_args.kwargs["review_reasons"], [])

    def test_safety_does_not_start_crop_recovery(self):
        adapter = GeminiAdapter()
        page = _png_page()
        safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=safety,
            ) as mock_transcribe,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], safety)

    def test_one_crop_failure_fails_the_page_without_persisting_partial_text(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        recitation_b = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model=GEMINI_36_FLASH_MODEL,
            attempt=2,
            max_output_tokens=4096,
        )
        crop_safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                if model_name == "gemini-3.1-flash-lite":
                    raise recitation_a
                raise recitation_b
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text="upper crop", engine_name=model_name)
            raise crop_safety

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 4)
        mock_persist_success.assert_not_called()
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], crop_safety)

    def test_crop_recitation_uses_existing_model_chain_then_fails_closed(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        # Full page: 2 models. First crop: 2 models. Page fails before crop 2.
        self.assertEqual(mock_transcribe.call_count, 4)
        mock_persist_success.assert_not_called()
        self.assertEqual(
            mock_persist_failure.call_args.kwargs["exc"].failure_code,
            GeminiResponseFailureCode.RECITATION,
        )

    def test_max_tokens_on_a_crop_fails_the_page(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        recitation_b = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model=GEMINI_36_FLASH_MODEL,
            attempt=2,
            max_output_tokens=4096,
        )
        max_tokens = _response_error(
            GeminiResponseFailureCode.MAX_TOKENS,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                if model_name == "gemini-3.1-flash-lite":
                    raise recitation_a
                raise recitation_b
            raise max_tokens

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 3)
        mock_persist_success.assert_not_called()
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], max_tokens)

    def test_english_handwritten_recitation_does_not_crop(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )
        recitation_b = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-b",
            attempt=2,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[recitation_a, recitation_b],
            ) as mock_transcribe,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(
                adapter,
                page,
                language_hint=Document.Language.ENGLISH,
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                model_candidates=["model-a", "model-b"],
                hebrew_printed_crop_recovery_enabled=False,
            )

        self.assertEqual(mock_transcribe.call_count, 2)
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], recitation_b)

    def test_worst_case_crop_chain_is_bounded_to_six_provider_invocations(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if (
                pages[0].image_bytes == plan.crops[0].image_bytes
                and model_name == GEMINI_36_FLASH_MODEL
            ):
                return GeminiResult(text="upper", engine_name=model_name)
            if (
                pages[0].image_bytes == plan.crops[1].image_bytes
                and model_name == GEMINI_36_FLASH_MODEL
            ):
                return GeminiResult(text="lower", engine_name=model_name)
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 6)
        mock_persist_failure.assert_not_called()
        self.assertEqual(
            mock_persist_success.call_args.kwargs["text"],
            "upper\nlower",
        )
        self.assertRegex(
            mock_persist_success.call_args.kwargs["actual_model"],
            r"^gemini-crop:[0-9a-f]{48}$",
        )

    def test_crop_quota_on_primary_advances_to_next_model(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        quota = GeminiQuotaError(
            model_name="gemini-3.1-flash-lite",
            provider_calls_used=1,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if model_name == "gemini-3.1-flash-lite":
                raise quota
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text="upper", engine_name=model_name)
            return GeminiResult(text="lower", engine_name=model_name)

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 6)
        self.assertEqual(
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
            [
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
            ],
        )
        mock_persist_failure.assert_not_called()
        self.assertEqual(
            mock_persist_success.call_args.kwargs["text"],
            "upper\nlower",
        )
        self.assertRegex(
            mock_persist_success.call_args.kwargs["actual_model"],
            r"^gemini-crop:[0-9a-f]{48}$",
        )

    def test_crop_quota_on_last_candidate_fails_closed(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        quota = GeminiQuotaError(
            model_name=GEMINI_36_FLASH_MODEL,
            provider_calls_used=1,
        )
        _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            raise quota

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 4)
        self.assertEqual(
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
            [
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
                "gemini-3.1-flash-lite",
                GEMINI_36_FLASH_MODEL,
            ],
        )
        mock_persist_success.assert_not_called()
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], quota)

    def test_crop_safety_does_not_switch_model(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        crop_safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            raise crop_safety

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 3)
        self.assertEqual(
            mock_transcribe.call_args_list[-1].kwargs["model_name"],
            "gemini-3.1-flash-lite",
        )
        mock_persist_success.assert_not_called()
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], crop_safety)

    def test_crop_failure_persist_database_error_is_retryable(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        crop_safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        _require_crop_plan(page)
        marker = "PRIVATE_DB_ERROR_CONTENT_CROP_QUOTA"

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            raise crop_safety

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ),
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_failure",
                side_effect=DatabaseError(marker),
            ) as mock_persist_failure,
            self.assertRaises(EnginePageCheckpointPersistenceRetryableError) as raised,
        ):
            self._execute(adapter, page)

        self.assertEqual(raised.exception.stage, "failure")
        self.assertEqual(raised.exception.page_index, 1)
        self.assertNotIn(marker, raised.exception.safe_message)
        self.assertEqual(mock_persist_failure.call_count, 1)
        mock_persist_success.assert_not_called()


class HebrewPrintedCropRecoveryCheckpointTests(TestCase):
    def test_adapter_execute_persists_assembled_crop_page(self):
        document = _document()
        document.language = Document.Language.HEBREW
        document.text_input_type = Document.TextInputType.PRINTED
        document.save(update_fields=["language", "text_input_type", "updated_at"])
        page = _png_page()
        recitation_a = GeminiResponseError(
            GeminiResponseFailureCode.RECITATION,
            GeminiResponseMetadata(
                model="gemini-3.1-flash-lite",
                page_index=1,
                attempt=1,
                max_output_tokens=4096,
                candidate_count=1,
                finish_reason="RECITATION",
                block_reason=None,
                raw_output_length=0,
                output_length=0,
                trailing_whitespace_chars=0,
                prompt_token_count=100,
                candidates_token_count=None,
                thoughts_token_count=None,
                total_token_count=100,
            ),
        )
        recitation_b = GeminiResponseError(
            GeminiResponseFailureCode.RECITATION,
            GeminiResponseMetadata(
                model=GEMINI_36_FLASH_MODEL,
                page_index=1,
                attempt=2,
                max_output_tokens=4096,
                candidate_count=1,
                finish_reason="RECITATION",
                block_reason=None,
                raw_output_length=0,
                output_length=0,
                trailing_whitespace_chars=0,
                prompt_token_count=100,
                candidates_token_count=None,
                thoughts_token_count=None,
                total_token_count=100,
            ),
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                if model_name == "gemini-3.1-flash-lite":
                    raise recitation_a
                raise recitation_b
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(
                    text="alpha\nshared",
                    engine_name=model_name,
                )
            return GeminiResult(
                text="shared\nbeta",
                engine_name=model_name,
            )

        with patch(
            "documents.services.htr_adapters.gemini_adapter."
            "transcribe_pages_with_gemini",
            side_effect=execute,
        ):
            result = GeminiAdapter().execute(
                pages=[page],
                language_hint=Document.Language.HEBREW,
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                document_id=document.id,
                text_input_type=Document.TextInputType.PRINTED,
                handwriting_type=Document.HandwritingType.VS,
                engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            )

        self.assertEqual(result.text, "alpha\nshared\nbeta")
        self.assertEqual(
            result.review_reasons,
            [REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY],
        )
        self.assertRegex(result.engine_name, r"^gemini-crop:[0-9a-f]{48}$")
        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=document,
            page_index=1,
        )
        self.assertEqual(checkpoint.status, GeminiOcrPageCheckpoint.Status.SUCCEEDED)
        self.assertEqual(checkpoint.text, "alpha\nshared\nbeta")
        self.assertEqual(
            checkpoint.review_reasons,
            [REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY],
        )

    def test_one_crop_failure_leaves_page_failed_and_attempt_partial(self):
        document = _document()
        page = _png_page()
        recitation = GeminiResponseError(
            GeminiResponseFailureCode.RECITATION,
            GeminiResponseMetadata(
                model="gemini-3.1-flash-lite",
                page_index=1,
                attempt=1,
                max_output_tokens=4096,
                candidate_count=1,
                finish_reason="RECITATION",
                block_reason=None,
                raw_output_length=0,
                output_length=0,
                trailing_whitespace_chars=0,
                prompt_token_count=100,
                candidates_token_count=None,
                thoughts_token_count=None,
                total_token_count=100,
            ),
        )
        crop_recitation = GeminiResponseError(
            GeminiResponseFailureCode.RECITATION,
            GeminiResponseMetadata(
                model=GEMINI_36_FLASH_MODEL,
                page_index=1,
                attempt=1,
                max_output_tokens=4096,
                candidate_count=1,
                finish_reason="RECITATION",
                block_reason=None,
                raw_output_length=0,
                output_length=0,
                trailing_whitespace_chars=0,
                prompt_token_count=100,
                candidates_token_count=None,
                thoughts_token_count=None,
                total_token_count=100,
            ),
        )

        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text="upper only", engine_name=model_name)
            raise crop_recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ),
            self.assertRaises(EnginePageIncompleteError) as raised,
        ):
            GeminiAdapter().execute(
                pages=[page],
                language_hint=Document.Language.HEBREW,
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                document_id=document.id,
                text_input_type=Document.TextInputType.PRINTED,
                handwriting_type=Document.HandwritingType.VS,
                engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            )

        self.assertEqual(raised.exception.missing_page_indices, (1,))
        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=document,
            page_index=1,
        )
        self.assertEqual(checkpoint.status, GeminiOcrPageCheckpoint.Status.FAILED)
        self.assertEqual(checkpoint.text, None)
        self.assertEqual(checkpoint.failure_code, "RECITATION")
