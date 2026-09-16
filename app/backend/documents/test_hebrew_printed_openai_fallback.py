"""Phase 1 Hebrew printed OpenAI fallback helper and env validation."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.openai_hebrew_printed_fallback import (
    DEFAULT_OPENAI_HEBREW_PRINTED_MODEL,
    OPENAI_IMAGE_DETAIL,
    HebrewPrintedOpenAIFallbackError,
    HebrewPrintedOpenAIFallbackFailureCode,
    openai_runtime_engine_name,
    transcribe_hebrew_printed_page_with_openai,
)
from documents.services.review_reasons import HEBREW_PRINTED_OPENAI_FALLBACK
from documents.templatetags.status_labels import review_reason_label


class _FakeResponses:
    def __init__(self, *, response=None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _FakeOpenAIClient:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses


class _ProviderApiError(Exception):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"not-a-full-png"


def _transcribe(*, client, prompt: str = "Transcribe the page.", **kwargs):
    defaults = {
        "image_bytes": _png_bytes(),
        "mime_type": "image/png",
        "prompt": prompt,
        "model": DEFAULT_OPENAI_HEBREW_PRINTED_MODEL,
        "api_key": "test-openai-key-DO-NOT-LEAK",
        "client": client,
    }
    defaults.update(kwargs)
    return transcribe_hebrew_printed_page_with_openai(**defaults)


class HebrewPrintedOpenAIFallbackEnvTests(SimpleTestCase):
    def test_flag_off_does_not_require_openai_api_key(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "test-gemini-key"},
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertFalse(cfg.enable_hebrew_printed_openai_fallback)
        self.assertIsNone(cfg.openai_api_key)
        self.assertEqual(
            cfg.openai_hebrew_printed_model,
            DEFAULT_OPENAI_HEBREW_PRINTED_MODEL,
        )

    def test_flag_on_missing_key_fails_validation(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-gemini-key",
                "ENABLE_HEBREW_PRINTED_OPENAI_FALLBACK": "true",
            },
            clear=True,
        ):
            with self.assertRaises(EnvConfigError) as raised:
                validate_required_env()

        self.assertIn("OPENAI_API_KEY", str(raised.exception))

    def test_flag_on_with_key_accepts_optional_model_override(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-gemini-key",
                "ENABLE_HEBREW_PRINTED_OPENAI_FALLBACK": "true",
                "OPENAI_API_KEY": "test-openai-key-DO-NOT-LEAK",
                "OPENAI_HEBREW_PRINTED_MODEL": "gpt-5.6-sol",
            },
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertTrue(cfg.enable_hebrew_printed_openai_fallback)
        self.assertEqual(cfg.openai_api_key, "test-openai-key-DO-NOT-LEAK")
        self.assertEqual(cfg.openai_hebrew_printed_model, "gpt-5.6-sol")

    def test_flag_off_does_not_validate_overlong_openai_model(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-gemini-key",
                "ENABLE_HEBREW_PRINTED_OPENAI_FALLBACK": "false",
                "OPENAI_HEBREW_PRINTED_MODEL": "x" * 80,
            },
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertFalse(cfg.enable_hebrew_printed_openai_fallback)
        self.assertIsNone(cfg.openai_api_key)
        self.assertEqual(
            cfg.openai_hebrew_printed_model,
            DEFAULT_OPENAI_HEBREW_PRINTED_MODEL,
        )


class HebrewPrintedOpenAIFallbackHelperTests(SimpleTestCase):
    def test_completed_non_empty_returns_stripped_text_and_openai_provenance(self):
        responses = _FakeResponses(
            response=SimpleNamespace(
                status="completed",
                output_text="  שורה  \n",
                output=[],
            )
        )
        result = _transcribe(client=_FakeOpenAIClient(responses))

        self.assertEqual(result.text, "שורה")
        self.assertEqual(result.engine_name, "openai:gpt-5.6-sol")
        self.assertLessEqual(len(result.engine_name), 64)
        self.assertEqual(result.review_reasons, (HEBREW_PRINTED_OPENAI_FALLBACK,))
        self.assertTrue(result.needs_review)
        self.assertEqual(
            openai_runtime_engine_name(DEFAULT_OPENAI_HEBREW_PRINTED_MODEL),
            "openai:gpt-5.6-sol",
        )

    def test_completed_empty_is_failure(self):
        responses = _FakeResponses(
            response=SimpleNamespace(status="completed", output_text="  \n", output=[])
        )
        with self.assertRaises(HebrewPrintedOpenAIFallbackError) as raised:
            _transcribe(client=_FakeOpenAIClient(responses))

        self.assertEqual(
            raised.exception.failure_code,
            HebrewPrintedOpenAIFallbackFailureCode.EMPTY_OUTPUT,
        )
        self.assertEqual(str(raised.exception), "EMPTY_OUTPUT")

    def test_incomplete_is_failure(self):
        responses = _FakeResponses(
            response=SimpleNamespace(
                status="incomplete",
                output_text="partial",
                output=[],
            )
        )
        with self.assertRaises(HebrewPrintedOpenAIFallbackError) as raised:
            _transcribe(client=_FakeOpenAIClient(responses))

        self.assertEqual(
            raised.exception.failure_code,
            HebrewPrintedOpenAIFallbackFailureCode.INCOMPLETE,
        )

    def test_refusal_is_failure(self):
        responses = _FakeResponses(
            response=SimpleNamespace(
                status="completed",
                output_text="",
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[
                            SimpleNamespace(
                                type="refusal",
                                refusal="secret-refusal-body-DO-NOT-LEAK",
                            )
                        ],
                    )
                ],
            )
        )
        with self.assertRaises(HebrewPrintedOpenAIFallbackError) as raised:
            _transcribe(client=_FakeOpenAIClient(responses))

        self.assertEqual(
            raised.exception.failure_code,
            HebrewPrintedOpenAIFallbackFailureCode.REFUSAL,
        )
        self.assertNotIn("secret-refusal-body", str(raised.exception))

    def test_provider_exception_is_sanitized_typed_failure(self):
        responses = _FakeResponses(
            error=_ProviderApiError(
                "sk-secret-api-message-DO-NOT-LEAK",
                status_code=429,
            )
        )
        with self.assertRaises(HebrewPrintedOpenAIFallbackError) as raised:
            _transcribe(client=_FakeOpenAIClient(responses))

        self.assertEqual(
            raised.exception.failure_code,
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
        )
        self.assertEqual(raised.exception.http_status, 429)
        self.assertEqual(str(raised.exception), "PROVIDER_ERROR")
        self.assertNotIn("sk-secret", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_client_construction_failure_is_sanitized_provider_error(self):
        class _ClientInitError(Exception):
            def __init__(self) -> None:
                super().__init__("sk-client-init-DO-NOT-LEAK")
                self.status_code = 401

        with patch(
            "documents.services.openai_hebrew_printed_fallback._build_openai_client",
            side_effect=_ClientInitError(),
        ):
            with self.assertRaises(HebrewPrintedOpenAIFallbackError) as raised:
                transcribe_hebrew_printed_page_with_openai(
                    image_bytes=_png_bytes(),
                    mime_type="image/png",
                    prompt="Transcribe the page.",
                    model=DEFAULT_OPENAI_HEBREW_PRINTED_MODEL,
                    api_key="test-openai-key-DO-NOT-LEAK",
                )

        self.assertEqual(
            raised.exception.failure_code,
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
        )
        self.assertEqual(raised.exception.http_status, 401)
        self.assertEqual(str(raised.exception), "PROVIDER_ERROR")
        self.assertNotIn("sk-client-init", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_request_shape_is_responses_store_false_stream_false_original_no_tools(
        self,
    ):
        responses = _FakeResponses(
            response=SimpleNamespace(
                status="completed",
                output_text="ok",
                output=[],
            )
        )
        prompt = "Caller-supplied archival prompt."
        _transcribe(client=_FakeOpenAIClient(responses), prompt=prompt)

        self.assertEqual(len(responses.calls), 1)
        kwargs = responses.calls[0]
        self.assertEqual(kwargs["model"], DEFAULT_OPENAI_HEBREW_PRINTED_MODEL)
        self.assertIs(kwargs["store"], False)
        self.assertIs(kwargs["stream"], False)
        self.assertNotIn("tools", kwargs)
        self.assertEqual(len(kwargs["input"]), 1)
        content = kwargs["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertEqual(content[0]["text"], prompt)
        self.assertEqual(content[1]["type"], "input_image")
        self.assertEqual(content[1]["detail"], OPENAI_IMAGE_DETAIL)
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("sk-", content[1]["image_url"][:20])

    def test_openai_fallback_review_reason_has_staff_label(self):
        self.assertEqual(
            review_reason_label(HEBREW_PRINTED_OPENAI_FALLBACK),
            "תעתוק OpenAI (גיבוי למודפס עברי)",
        )
        self.assertNotEqual(
            review_reason_label(HEBREW_PRINTED_OPENAI_FALLBACK),
            HEBREW_PRINTED_OPENAI_FALLBACK,
        )
