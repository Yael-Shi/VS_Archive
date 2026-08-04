"""Bounded RECITATION model fallback for checkpoint-backed Gemini OCR."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import DocumentTextResult
from documents.services.gemini_engine import (
    GeminiQuotaError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    GeminiResponseMetadata,
    GeminiResult,
    transcribe_pages_with_gemini,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.page_extraction import PageImage

_PAGE = PageImage(
    page_index=1,
    image_bytes=b"synthetic-image",
    mime_type="image/png",
)
_VALID_JSON = (
    '{"text": "Synthetic archival transcription long enough", '
    '"has_unclear": false, "unclear_count": 0}'
)


class _ExpectedIncomplete(Exception):
    pass


def _metadata(
    *,
    model: str,
    attempt: int,
    max_output_tokens: int,
    finish_reason: str,
) -> GeminiResponseMetadata:
    return GeminiResponseMetadata(
        model=model,
        page_index=1,
        attempt=attempt,
        max_output_tokens=max_output_tokens,
        candidate_count=1,
        finish_reason=finish_reason,
        block_reason=None,
        raw_output_length=0,
        output_length=0,
        trailing_whitespace_chars=0,
        prompt_token_count=100,
        candidates_token_count=None,
        thoughts_token_count=None,
        total_token_count=100,
    )


def _response_error(
    failure_code: GeminiResponseFailureCode,
    *,
    model: str,
    attempt: int,
    max_output_tokens: int,
) -> GeminiResponseError:
    return GeminiResponseError(
        failure_code,
        _metadata(
            model=model,
            attempt=attempt,
            max_output_tokens=max_output_tokens,
            finish_reason=failure_code.value,
        ),
    )


def _provider_response(
    *,
    text: str,
    finish_reason: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        prompt_feedback=SimpleNamespace(block_reason=None),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=120,
        ),
    )


def _execute_claimed_page(
    adapter: GeminiAdapter,
    *,
    recitation_model_fallback_enabled: bool = True,
    hebrew_general_model_fallback_enabled: bool = False,
) -> None:
    adapter._execute_claimed_page(
        page=_PAGE,
        language_hint="fr",
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        model_candidates=["model-a", "model-b"],
        recitation_model_fallback_enabled=(recitation_model_fallback_enabled),
        hebrew_general_model_fallback_enabled=(hebrew_general_model_fallback_enabled),
        kwargs={"max_output_tokens": 4096},
        checkpoint_id=1,
        lease_token=uuid.uuid4(),
        attempt_id=2,
    )


class GeminiRecitationFallbackAdapterTests(SimpleTestCase):
    def test_immediate_recitation_uses_fallback_with_remaining_budget(self):
        adapter = GeminiAdapter()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    recitation,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            _execute_claimed_page(adapter)

        self.assertEqual(mock_transcribe.call_count, 2)
        first = mock_transcribe.call_args_list[0].kwargs
        second = mock_transcribe.call_args_list[1].kwargs

        self.assertEqual(first["model_name"], "model-a")
        self.assertEqual(first["max_provider_calls"], 3)
        self.assertEqual(first["provider_call_offset"], 0)
        self.assertEqual(first["max_output_tokens"], 4096)

        self.assertEqual(second["model_name"], "model-b")
        self.assertEqual(second["max_provider_calls"], 2)
        self.assertEqual(second["provider_call_offset"], 1)
        self.assertEqual(second["max_output_tokens"], 4096)

        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            "model-b",
        )
        mock_persist_failure.assert_not_called()

    def test_hebrew_general_primary_success_avoids_36_fallback(self):
        adapter = GeminiAdapter()

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                return_value=GeminiResult(text="page text", engine_name="model-a"),
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=True,
            )

        self.assertEqual(mock_transcribe.call_count, 1)
        primary = mock_transcribe.call_args.kwargs
        self.assertEqual(primary["model_name"], "model-a")
        self.assertEqual(primary["max_provider_calls"], 1)
        self.assertEqual(primary["provider_call_offset"], 0)
        self.assertEqual(primary["max_output_tokens"], 4096)
        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            "model-a",
        )

    def test_hebrew_general_max_tokens_uses_36_with_remaining_budget(self):
        adapter = GeminiAdapter()
        max_tokens = _response_error(
            GeminiResponseFailureCode.MAX_TOKENS,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    max_tokens,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=True,
            )

        self.assertEqual(mock_transcribe.call_count, 2)
        primary = mock_transcribe.call_args_list[0].kwargs
        fallback = mock_transcribe.call_args_list[1].kwargs

        self.assertEqual(primary["model_name"], "model-a")
        self.assertEqual(primary["max_provider_calls"], 1)
        self.assertEqual(primary["provider_call_offset"], 0)

        self.assertEqual(fallback["model_name"], "model-b")
        self.assertEqual(fallback["max_provider_calls"], 2)
        self.assertEqual(fallback["provider_call_offset"], 1)
        self.assertEqual(fallback["max_output_tokens"], 4096)
        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            "model-b",
        )
        mock_persist_failure.assert_not_called()

    def test_hebrew_general_recitation_uses_36_after_one_primary_call(self):
        adapter = GeminiAdapter()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    recitation,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ),
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=True,
            )

        primary = mock_transcribe.call_args_list[0].kwargs
        fallback = mock_transcribe.call_args_list[1].kwargs
        self.assertEqual(primary["max_provider_calls"], 1)
        self.assertEqual(fallback["max_provider_calls"], 2)
        self.assertEqual(fallback["provider_call_offset"], 1)

    def test_hebrew_general_quota_uses_36_with_remaining_budget(self):
        adapter = GeminiAdapter()
        quota = _response_error(
            GeminiResponseFailureCode.MAX_TOKENS,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    quota,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter._is_quota_error",
                side_effect=lambda exc: exc is quota,
            ),
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=True,
            )

        self.assertEqual(mock_transcribe.call_count, 2)
        primary = mock_transcribe.call_args_list[0].kwargs
        fallback = mock_transcribe.call_args_list[1].kwargs

        self.assertEqual(primary["model_name"], "model-a")
        self.assertEqual(primary["max_provider_calls"], 1)
        self.assertEqual(primary["provider_call_offset"], 0)

        self.assertEqual(fallback["model_name"], "model-b")
        self.assertEqual(fallback["max_provider_calls"], 2)
        self.assertEqual(fallback["provider_call_offset"], 1)
        self.assertEqual(fallback["max_output_tokens"], 4096)
        self.assertEqual(
            mock_persist_success.call_args.kwargs["actual_model"],
            "model-b",
        )
        mock_persist_failure.assert_not_called()

    def test_hebrew_general_safety_does_not_use_36(self):
        adapter = GeminiAdapter()
        safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=safety,
            ) as mock_transcribe,
            patch.object(adapter, "_persist_page_failure"),
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=True,
            )

        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertEqual(
            mock_transcribe.call_args.kwargs["max_provider_calls"],
            1,
        )

    def test_recitation_after_two_primary_calls_leaves_one_fallback_call(self):
        adapter = GeminiAdapter()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=2,
            max_output_tokens=8192,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    recitation,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ),
        ):
            _execute_claimed_page(adapter)

        fallback = mock_transcribe.call_args_list[1].kwargs
        self.assertEqual(fallback["model_name"], "model-b")
        self.assertEqual(fallback["max_provider_calls"], 1)
        self.assertEqual(fallback["provider_call_offset"], 2)
        self.assertEqual(fallback["max_output_tokens"], 8192)

    def test_second_recitation_is_persisted_without_third_call(self):
        adapter = GeminiAdapter()
        first_recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )
        second_recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-b",
            attempt=2,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[first_recitation, second_recitation],
            ) as mock_transcribe,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            _execute_claimed_page(adapter)

        self.assertEqual(mock_transcribe.call_count, 2)
        self.assertIs(
            mock_persist_failure.call_args.kwargs["exc"],
            second_recitation,
        )

    def test_recitation_fallback_is_disabled_outside_scope(self):
        adapter = GeminiAdapter()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="model-a",
            attempt=1,
            max_output_tokens=4096,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=recitation,
            ) as mock_transcribe,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
            )

        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertIs(
            mock_persist_failure.call_args.kwargs["exc"],
            recitation,
        )

    def test_safety_does_not_use_model_fallback(self):
        adapter = GeminiAdapter()
        safety = _response_error(
            GeminiResponseFailureCode.SAFETY,
            model="model-a",
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
            _execute_claimed_page(adapter)

        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertIs(mock_persist_failure.call_args.kwargs["exc"], safety)

    def test_quota_fallback_uses_only_the_remaining_global_budget(self):
        adapter = GeminiAdapter()
        quota = GeminiQuotaError(
            model_name="model-a",
            provider_calls_used=2,
            after_retries=True,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    quota,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ),
        ):
            _execute_claimed_page(adapter)

        fallback = mock_transcribe.call_args_list[1].kwargs
        self.assertEqual(fallback["max_provider_calls"], 1)
        self.assertEqual(fallback["provider_call_offset"], 2)

    def test_quota_fallback_outside_scope_gets_fresh_candidate_budget(self):
        adapter = GeminiAdapter()
        quota = GeminiQuotaError(
            model_name="model-a",
            provider_calls_used=3,
            after_retries=True,
        )

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=[
                    quota,
                    GeminiResult(text="page text", engine_name="model-b"),
                ],
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ),
        ):
            _execute_claimed_page(
                adapter,
                recitation_model_fallback_enabled=False,
            )

        fallback = mock_transcribe.call_args_list[1].kwargs
        self.assertEqual(fallback["model_name"], "model-b")
        self.assertEqual(fallback["max_provider_calls"], 3)
        self.assertEqual(fallback["provider_call_offset"], 0)
        self.assertEqual(fallback["max_output_tokens"], 4096)


class GeminiProviderCallWindowTests(SimpleTestCase):
    def test_provider_call_window_cannot_exceed_global_budget(self):
        with self.assertRaisesRegex(
            ValueError,
            "provider call window exceeds",
        ):
            transcribe_pages_with_gemini(
                [_PAGE],
                "fr",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                max_provider_calls=2,
                provider_call_offset=2,
            )

    def test_provider_offset_is_reported_as_global_attempt_ordinal(self):
        client = Mock()
        client.models.generate_content.return_value = _provider_response(
            text="",
            finish_reason="RECITATION",
        )

        with (
            patch(
                "documents.services.gemini_engine._get_api_key",
                return_value="test-key",
            ),
            patch(
                "documents.services.gemini_engine._create_client",
                return_value=client,
            ),
            self.assertRaises(GeminiResponseError) as raised,
        ):
            transcribe_pages_with_gemini(
                [_PAGE],
                "fr",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                model_name="model-b",
                max_output_tokens=4096,
                max_provider_calls=1,
                provider_call_offset=2,
            )

        self.assertEqual(raised.exception.metadata.attempt, 3)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_limited_window_does_not_make_an_extra_empty_response_call(self):
        client = Mock()
        client.models.generate_content.side_effect = [
            _provider_response(text="", finish_reason="STOP"),
            _provider_response(text=_VALID_JSON, finish_reason="STOP"),
        ]

        with (
            patch(
                "documents.services.gemini_engine._get_api_key",
                return_value="test-key",
            ),
            patch(
                "documents.services.gemini_engine._create_client",
                return_value=client,
            ),
            self.assertRaises(GeminiResponseError) as raised,
        ):
            transcribe_pages_with_gemini(
                [_PAGE],
                "fr",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                model_name="model-b",
                max_output_tokens=4096,
                max_provider_calls=1,
                provider_call_offset=1,
            )

        self.assertEqual(
            raised.exception.failure_code,
            GeminiResponseFailureCode.EMPTY_RESPONSE,
        )
        self.assertEqual(client.models.generate_content.call_count, 1)
