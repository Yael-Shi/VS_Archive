"""Hebrew printed OpenAI fallback wiring in checkpointed GeminiAdapter."""

from __future__ import annotations

import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    GeminiOcrPageCheckpoint,
)
from documents.services.env_validation import WorkerEnvConfig
from documents.services.gemini_engine import (
    GeminiApiError,
    GeminiQuotaError,
    GeminiResponseFailureCode,
    GeminiResult,
    _effective_transcription_prompt,
)
from documents.services.gemini_models import GEMINI_36_FLASH_MODEL
from documents.services.htr_adapters.base import EnginePageIncompleteError
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.openai_hebrew_printed_fallback import (
    HebrewPrintedOpenAIFallbackError,
    HebrewPrintedOpenAIFallbackFailureCode,
    HebrewPrintedOpenAIFallbackResult,
)
from documents.services.review_reasons import HEBREW_PRINTED_OPENAI_FALLBACK
from documents.test_gemini_hebrew_printed_recitation_crop_recovery import (
    _png_page,
    _require_crop_plan,
)
from documents.test_gemini_page_checkpoints import _document
from documents.test_gemini_recitation_model_fallback import (
    _ExpectedIncomplete,
    _response_error,
)

_OPENAI_TRANSCRIBE = (
    "documents.services.openai_hebrew_printed_fallback."
    "transcribe_hebrew_printed_page_with_openai"
)


def _openai_worker_env(*, enabled: bool = True) -> WorkerEnvConfig:
    return WorkerEnvConfig(
        gemini_api_key="key",
        gemini_confidence_threshold=0.7,
        min_text_length=20,
        max_retries=3,
        retry_delay_seconds_1=30,
        retry_delay_seconds_2=300,
        report_window_start="00:00",
        report_send_time="08:00",
        free_tier_alert_pct=80,
        gemini_free_daily_request_limit=1500,
        gemini_free_daily_image_limit=1000,
        transkribus_free_monthly_credits=500,
        enable_hybrid_htr=False,
        enable_daily_report=False,
        smtp_host=None,
        smtp_port=None,
        smtp_username=None,
        smtp_password=None,
        default_from_email=None,
        transkribus_api_token=None,
        transkribus_username=None,
        transkribus_password=None,
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=8192,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.85,
        enable_hebrew_printed_openai_fallback=enabled,
        openai_api_key="test-openai-key-DO-NOT-LEAK" if enabled else None,
        openai_hebrew_printed_model="gpt-5.6-sol",
    )


def _openai_success_result() -> HebrewPrintedOpenAIFallbackResult:
    return HebrewPrintedOpenAIFallbackResult(
        text="openai page text",
        engine_name="openai:gpt-5.6-sol",
        review_reasons=(HEBREW_PRINTED_OPENAI_FALLBACK,),
        needs_review=True,
    )


def _full_page_recitation_errors():
    return (
        _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        ),
        _response_error(
            GeminiResponseFailureCode.RECITATION,
            model=GEMINI_36_FLASH_MODEL,
            attempt=2,
            max_output_tokens=4096,
        ),
    )


class HebrewPrintedOpenAIFallbackClaimedPageTests(SimpleTestCase):
    def _execute(
        self,
        adapter: GeminiAdapter,
        page,
        *,
        language_hint: str | None = Document.Language.HEBREW,
        prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
        openai_enabled: bool = True,
        crop_enabled: bool = False,
        mixed_enabled: bool = False,
    ) -> None:
        adapter._execute_claimed_page(
            page=page,
            language_hint=language_hint,
            prompt_variant=prompt_variant,
            model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            recitation_model_fallback_enabled=True,
            hebrew_general_model_fallback_enabled=False,
            kwargs={"max_output_tokens": 4096},
            checkpoint_id=1,
            lease_token=uuid.uuid4(),
            attempt_id=2,
            hebrew_printed_crop_recovery_enabled=crop_enabled,
            hebrew_printed_mixed_script_enabled=mixed_enabled,
            hebrew_printed_openai_fallback_enabled=openai_enabled,
            openai_api_key="test-openai-key-DO-NOT-LEAK",
            openai_hebrew_printed_model="gpt-5.6-sol",
        )

    def test_gemini_full_page_success_does_not_call_openai(self):
        adapter = GeminiAdapter()
        page = _png_page()
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                return_value=GeminiResult(
                    text="full page",
                    engine_name="gemini-3.1-flash-lite",
                ),
            ) as mock_transcribe,
            patch(
                _OPENAI_TRANSCRIBE,
            ) as mock_openai,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 1)
        mock_openai.assert_not_called()
        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            "gemini-3.1-flash-lite",
        )

    def test_recitation_chain_calls_openai_once_and_skips_crop(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[recitation_a, recitation_b],
            ) as mock_transcribe,
            patch(
                _OPENAI_TRANSCRIBE,
                return_value=_openai_success_result(),
            ) as mock_openai,
            patch.object(
                adapter, "_recover_hebrew_printed_recitation_crops"
            ) as mock_crop,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page, crop_enabled=True, mixed_enabled=True)

        self.assertEqual(mock_transcribe.call_count, 2)
        self.assertEqual(mock_openai.call_count, 1)
        mock_crop.assert_not_called()
        openai_kwargs = mock_openai.call_args.kwargs
        prompt, _uses_plain = _effective_transcription_prompt(
            DocumentTextResult.OcrPromptVariant.PRINTED,
            Document.Language.HEBREW,
        )
        self.assertEqual(openai_kwargs["image_bytes"], page.image_bytes)
        self.assertEqual(openai_kwargs["mime_type"], "image/png")
        self.assertEqual(openai_kwargs["prompt"], prompt)
        self.assertEqual(openai_kwargs["model"], "gpt-5.6-sol")
        persist_kwargs = mock_persist_success.call_args.kwargs
        self.assertEqual(persist_kwargs["text"], "openai page text")
        self.assertEqual(persist_kwargs["actual_model"], "openai:gpt-5.6-sol")
        self.assertTrue(persist_kwargs["needs_review"])
        self.assertEqual(
            persist_kwargs["review_reasons"],
            [HEBREW_PRINTED_OPENAI_FALLBACK],
        )

    def test_final_safety_response_error_calls_openai_once_without_crop(self):
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
            patch(
                _OPENAI_TRANSCRIBE,
                return_value=_openai_success_result(),
            ) as mock_openai,
            patch.object(
                adapter, "_recover_hebrew_printed_recitation_crops"
            ) as mock_crop,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page, crop_enabled=True, mixed_enabled=True)

        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertEqual(
            mock_transcribe.call_args.kwargs["model_name"],
            "gemini-3.1-flash-lite",
        )
        self.assertEqual(mock_openai.call_count, 1)
        mock_crop.assert_not_called()
        persist_kwargs = mock_persist_success.call_args.kwargs
        self.assertEqual(persist_kwargs["text"], "openai page text")
        self.assertEqual(persist_kwargs["actual_model"], "openai:gpt-5.6-sol")
        self.assertEqual(
            persist_kwargs["review_reasons"],
            [HEBREW_PRINTED_OPENAI_FALLBACK],
        )

    def test_openai_helper_failures_keep_gemini_page_failure(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        failures = (
            HebrewPrintedOpenAIFallbackFailureCode.INCOMPLETE,
            HebrewPrintedOpenAIFallbackFailureCode.REFUSAL,
            HebrewPrintedOpenAIFallbackFailureCode.EMPTY_OUTPUT,
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
        for failure_code in failures:
            with self.subTest(failure_code=failure_code.value):
                with (
                    patch(
                        "documents.services.htr_adapters.gemini_adapter."
                        "transcribe_pages_with_gemini",
                        side_effect=[recitation_a, recitation_b],
                    ),
                    patch(
                        _OPENAI_TRANSCRIBE,
                        side_effect=HebrewPrintedOpenAIFallbackError(failure_code),
                    ) as mock_openai,
                    patch.object(
                        adapter, "_persist_page_failure"
                    ) as mock_persist_failure,
                    patch.object(
                        adapter,
                        "_raise_incomplete",
                        side_effect=_ExpectedIncomplete,
                    ),
                    self.assertRaises(_ExpectedIncomplete),
                ):
                    self._execute(adapter, page)

                self.assertEqual(mock_openai.call_count, 1)
                self.assertIs(
                    mock_persist_failure.call_args.kwargs["exc"],
                    recitation_b,
                )

    def test_quota_does_not_call_openai(self):
        adapter = GeminiAdapter()
        page = _png_page()
        quota = GeminiQuotaError(
            model_name="gemini-3.1-flash-lite",
            provider_calls_used=1,
        )
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=quota,
            ),
            patch(
                _OPENAI_TRANSCRIBE,
            ) as mock_openai,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_failure"
            ) as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        mock_openai.assert_not_called()
        self.assertEqual(
            mock_persist_failure.call_args.kwargs["failure_code"],
            "GEMINI_MODELS_EXHAUSTED",
        )

    def test_gemini_api_error_does_not_call_openai(self):
        adapter = GeminiAdapter()
        page = _png_page()
        api_error = GeminiApiError("TimeoutError")
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=api_error,
            ),
            patch(
                _OPENAI_TRANSCRIBE,
            ) as mock_openai,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        mock_openai.assert_not_called()
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], api_error)

    def test_flag_off_keeps_crop_recovery(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                if model_name == "gemini-3.1-flash-lite":
                    raise recitation_a
                raise recitation_b
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(
                    text="line one\noverlap line", engine_name=model_name
                )
            return GeminiResult(text="overlap line\nline two", engine_name=model_name)

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                _OPENAI_TRANSCRIBE,
            ) as mock_openai,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(
                adapter,
                page,
                openai_enabled=False,
                crop_enabled=True,
                mixed_enabled=True,
            )

        self.assertEqual(mock_transcribe.call_count, 4)
        mock_openai.assert_not_called()
        self.assertRegex(
            mock_persist_success.call_args.kwargs["actual_model"],
            r"^gemini-crop:[0-9a-f]{48}$",
        )


class HebrewPrintedOpenAIFallbackExecuteTests(TestCase):
    def _execute(
        self,
        *,
        page,
        document,
        worker_env: WorkerEnvConfig,
        language_hint: str = Document.Language.HEBREW,
        text_input_type: str = Document.TextInputType.PRINTED,
        prompt_variant: str = DocumentTextResult.OcrPromptVariant.PRINTED,
        handwriting_type: str = Document.HandwritingType.VS,
    ):
        return GeminiAdapter().execute(
            pages=[page],
            language_hint=language_hint,
            prompt_variant=prompt_variant,
            document_id=document.id,
            text_input_type=text_input_type,
            handwriting_type=handwriting_type,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            worker_env=worker_env,
        )

    def test_execute_flag_on_skips_crop_and_persists_openai_checkpoint(self):
        document = _document("Hebrew printed openai fallback")
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[recitation_a, recitation_b],
            ),
            patch(
                _OPENAI_TRANSCRIBE,
                return_value=_openai_success_result(),
            ) as mock_openai,
            patch.object(
                GeminiAdapter,
                "_recover_hebrew_printed_recitation_crops",
            ) as mock_crop,
        ):
            result = self._execute(
                page=page,
                document=document,
                worker_env=_openai_worker_env(enabled=True),
            )

        self.assertEqual(mock_openai.call_count, 1)
        mock_crop.assert_not_called()
        self.assertEqual(result.engine_name, "openai:gpt-5.6-sol")
        self.assertEqual(result.review_reasons, [HEBREW_PRINTED_OPENAI_FALLBACK])
        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=document,
            page_index=1,
        )
        self.assertEqual(checkpoint.status, GeminiOcrPageCheckpoint.Status.SUCCEEDED)
        self.assertEqual(checkpoint.actual_model, "openai:gpt-5.6-sol")
        self.assertEqual(checkpoint.text, "openai page text")

    def test_successful_openai_checkpoint_is_reused_on_resume(self):
        document = _document("Hebrew printed openai resume")
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        worker_env = _openai_worker_env(enabled=True)
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[recitation_a, recitation_b],
            ) as mock_transcribe,
            patch(
                _OPENAI_TRANSCRIBE,
                return_value=_openai_success_result(),
            ) as mock_openai,
        ):
            self._execute(page=page, document=document, worker_env=worker_env)
            mock_transcribe.reset_mock()
            mock_openai.reset_mock()
            result = self._execute(page=page, document=document, worker_env=worker_env)

        mock_transcribe.assert_not_called()
        mock_openai.assert_not_called()
        self.assertEqual(result.engine_name, "openai:gpt-5.6-sol")

    def test_execute_flag_off_still_uses_crop_recovery(self):
        document = _document("Hebrew printed openai flag off crop")
        page = _png_page()
        recitation_a, recitation_b = _full_page_recitation_errors()
        recovered = GeminiResult(
            text="crop assembled",
            engine_name="gemini-crop:" + ("a" * 48),
            needs_review=True,
            review_reasons=["HEBREW_PRINTED_RECITATION_CROP_RECOVERY"],
        )
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[recitation_a, recitation_b],
            ),
            patch(
                _OPENAI_TRANSCRIBE,
            ) as mock_openai,
            patch.object(
                GeminiAdapter,
                "_recover_hebrew_printed_recitation_crops",
                return_value=recovered,
            ) as mock_crop,
        ):
            result = self._execute(
                page=page,
                document=document,
                worker_env=_openai_worker_env(enabled=False),
            )

        mock_openai.assert_not_called()
        mock_crop.assert_called_once()
        self.assertEqual(result.engine_name, recovered.engine_name)

    def test_other_routes_never_call_openai_when_flag_on(self):
        document = _document("OpenAI fallback route isolation")
        page = _png_page()
        safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        cases = (
            (
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                Document.HandwritingType.GENERAL,
            ),
            (
                Document.Language.HEBREW,
                Document.TextInputType.MIXED,
                DocumentTextResult.OcrPromptVariant.MIXED,
                Document.HandwritingType.VS,
            ),
            (
                Document.Language.ENGLISH,
                Document.TextInputType.PRINTED,
                DocumentTextResult.OcrPromptVariant.PRINTED,
                Document.HandwritingType.VS,
            ),
        )
        for language, text_type, prompt_variant, handwriting in cases:
            with self.subTest(language=language, text_type=text_type):
                with (
                    patch(
                        "documents.services.htr_adapters.gemini_adapter."
                        "transcribe_pages_with_gemini",
                        side_effect=safety,
                    ),
                    patch(
                        _OPENAI_TRANSCRIBE,
                    ) as mock_openai,
                    self.assertRaises(EnginePageIncompleteError),
                ):
                    self._execute(
                        page=page,
                        document=document,
                        worker_env=_openai_worker_env(enabled=True),
                        language_hint=language,
                        text_input_type=text_type,
                        prompt_variant=prompt_variant,
                        handwriting_type=handwriting,
                    )
                mock_openai.assert_not_called()
