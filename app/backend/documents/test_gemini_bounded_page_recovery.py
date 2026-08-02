"""PR D bounded per-page Gemini OCR recovery — engine retry-policy tests.

Synthetic fixtures only. Production documents 271/277 (transient empty
response) and 289/291 (MAX_TOKENS truncation) are referenced only as modeled
failure classes. Document 293 belongs to PR C and is not reimplemented here.

Checkpoint-identity and durable-page-reuse coverage lives in
``test_gemini_page_checkpoints.py``; privacy coverage lives in
``test_gemini_response_safety.py``; env-validation coverage lives in
``WorkerEnvConfigTests`` (``tests.py``).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from django.test import SimpleTestCase

from documents.models import DocumentTextResult
from documents.services.gemini_engine import (
    GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
    GeminiError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    _next_max_output_tokens_cap,
    _transcription_empty_response_backoff_seconds,
    transcribe_pages_with_gemini,
)
from documents.services.page_extraction import PageImage

_PAGES = [PageImage(page_index=1, image_bytes=b"png-bytes", mime_type="image/png")]
_VALID_JSON = (
    '{"text": "Synthetic archival transcription long enough", '
    '"has_unclear": false, "unclear_count": 0}'
)


def _response(
    *,
    text: str = "",
    finish_reason: str = "STOP",
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


def _empty() -> SimpleNamespace:
    return _response(text="", finish_reason="STOP")


def _truncated() -> SimpleNamespace:
    return _response(text="partial", finish_reason="MAX_TOKENS")


def _success() -> SimpleNamespace:
    return _response(text=_VALID_JSON)


class GeminiRetryHelperTests(SimpleTestCase):
    def test_token_cap_ladder_is_deterministic(self):
        cases = (
            (None, 32768, 8192),
            (2048, 32768, 8192),
            (8192, 32768, 16384),
            (16384, 32768, 32768),
            (8192, 20000, 16384),
            (16384, 20000, 20000),  # clamped to hard cap
            (8192, 8192, None),  # cannot increase
            (32768, 32768, None),
        )
        for current, hard_cap, expected in cases:
            with self.subTest(current=current, hard_cap=hard_cap):
                self.assertEqual(
                    _next_max_output_tokens_cap(current, hard_cap=hard_cap),
                    expected,
                )

    def test_empty_response_backoff_is_one_then_two_seconds(self):
        self.assertEqual(_transcription_empty_response_backoff_seconds(1), 1.0)
        self.assertEqual(_transcription_empty_response_backoff_seconds(2), 2.0)
        self.assertIsNone(_transcription_empty_response_backoff_seconds(3))


class GeminiBoundedPageRecoveryTests(SimpleTestCase):
    """Engine-level bounded retry: one page against one model candidate."""

    def _run(self, responses, **overrides):
        """Run one transcription against mocked provider responses.

        ``responses`` entries may be response objects or exceptions.
        Returns calls/caps/sleeps plus either the result or the raised error.
        """
        kwargs = {
            "prompt_variant": DocumentTextResult.OcrPromptVariant.PRINTED,
            "model_name": "test-model",
        }
        language_hint = overrides.pop("language_hint", "en")
        kwargs.update(overrides)
        with (
            patch(
                "documents.services.gemini_engine._get_api_key",
                return_value="test-key",
            ),
            patch(
                "documents.services.gemini_engine._create_client"
            ) as mock_create_client,
            patch("documents.services.gemini_engine.time.sleep") as mock_sleep,
        ):
            client = Mock()
            client.models.generate_content.side_effect = list(responses)
            mock_create_client.return_value = client
            result = None
            error = None
            try:
                result = transcribe_pages_with_gemini(_PAGES, language_hint, **kwargs)
            except GeminiError as exc:
                error = exc
        return SimpleNamespace(
            calls=client.models.generate_content.call_count,
            caps=[
                c.kwargs["config"].max_output_tokens
                for c in client.models.generate_content.call_args_list
            ],
            sleeps=mock_sleep.call_args_list,
            result=result,
            error=error,
        )

    # ---------------------------------------------- EMPTY_RESPONSE backoff

    def test_empty_response_recovers_on_attempt_two(self):
        run = self._run([_empty(), _success()], max_output_tokens=2048)

        self.assertIsNone(run.error)
        self.assertEqual(run.calls, 2)
        self.assertEqual(run.sleeps, [call(1.0)])

    def test_empty_response_recovers_on_attempt_three(self):
        run = self._run([_empty(), _empty(), _success()])

        self.assertIsNone(run.error)
        self.assertEqual(run.calls, 3)
        self.assertEqual(run.sleeps, [call(1.0), call(2.0)])

    def test_empty_response_exhaustion_makes_exactly_three_calls(self):
        run = self._run([_empty(), _empty(), _empty()])

        assert isinstance(run.error, GeminiResponseError)
        self.assertEqual(
            run.error.failure_code,
            GeminiResponseFailureCode.EMPTY_RESPONSE,
        )
        self.assertEqual(run.calls, GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS)
        # Deterministic backoff before attempts 2 and 3 only — no sleep after
        # the final attempt, and never a fourth provider call.
        self.assertEqual(run.sleeps, [call(1.0), call(2.0)])

    # ---------------------------------------------- MAX_TOKENS escalation

    def test_max_tokens_escalates_caps_without_sleeping(self):
        cases = (
            (2048, 32768, [2048, 8192, 16384]),
            (8192, 32768, [8192, 16384, 32768]),
            (None, 32768, [None, 8192, 16384]),
            (8192, 20000, [8192, 16384, 20000]),  # hard-cap clamping
        )
        for initial, hard_cap, expected_caps in cases:
            with self.subTest(initial=initial, hard_cap=hard_cap):
                responses = [_truncated()] * (len(expected_caps) - 1) + [_success()]
                run = self._run(
                    responses,
                    max_output_tokens=initial,
                    max_output_tokens_hard_cap=hard_cap,
                )

                self.assertIsNone(run.error)
                self.assertEqual(run.caps, expected_caps)
                self.assertEqual(run.sleeps, [])

    def test_max_tokens_fails_immediately_when_cap_cannot_increase(self):
        run = self._run(
            [_truncated()],
            max_output_tokens=8192,
            max_output_tokens_hard_cap=8192,
        )

        assert isinstance(run.error, GeminiResponseError)
        self.assertEqual(
            run.error.failure_code,
            GeminiResponseFailureCode.MAX_TOKENS,
        )
        # No identical repeated call and no sleep.
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.sleeps, [])

    # ---------------------------------------------- permanent classifications

    def test_permanent_finish_reasons_are_not_retried(self):
        for finish_reason in ("SAFETY", "RECITATION", "LANGUAGE", "SPII"):
            with self.subTest(finish_reason=finish_reason):
                run = self._run(
                    [_response(text='{"text": "x"}', finish_reason=finish_reason)]
                )

                assert isinstance(run.error, GeminiResponseError)
                self.assertEqual(run.error.failure_code.value, finish_reason)
                self.assertEqual(run.calls, 1)
                self.assertEqual(run.sleeps, [])

    def test_classification_occurs_before_json_parsing(self):
        with patch(
            "documents.services.gemini_engine._parse_page_json_strict"
        ) as mock_parse:
            run = self._run(
                [_response(text='{"text": "looks-like-json"}', finish_reason="SAFETY")]
            )

        assert isinstance(run.error, GeminiResponseError)
        mock_parse.assert_not_called()

    # ---------------------------------------------- JSON contract failures

    def test_json_parse_is_bounded_and_never_repaired(self):
        broken = '{"text": "broken'
        run = self._run(
            [_response(text=broken)] * 3,
            language_hint="ar",
        )

        assert isinstance(run.error, GeminiResponseError)
        self.assertEqual(
            run.error.failure_code,
            GeminiResponseFailureCode.JSON_PARSE,
        )
        self.assertEqual(run.calls, 3)

    def test_json_schema_is_not_retried(self):
        invalid_schema = (
            '{"text": "valid text here", "has_unclear": "false", "unclear_count": 0}'
        )
        run = self._run(
            [_response(text=invalid_schema)],
            language_hint="ar",
        )

        assert isinstance(run.error, GeminiResponseError)
        self.assertEqual(
            run.error.failure_code,
            GeminiResponseFailureCode.JSON_SCHEMA,
        )
        self.assertEqual(run.calls, 1)

    # ---------------------------------------------- quota/rate-limit budget

    def test_transient_quota_exhaustion_consumes_the_same_three_call_budget(self):
        run = self._run(
            [
                RuntimeError("429 RESOURCE_EXHAUSTED"),
                RuntimeError("429 RESOURCE_EXHAUSTED"),
                RuntimeError("429 RESOURCE_EXHAUSTED"),
            ]
        )

        # Exactly three provider calls — never a fourth.
        self.assertEqual(run.calls, GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS)
        # Rate-limit backoff before attempts 2 and 3 only; no sleep after the
        # final failed call.
        self.assertEqual(run.sleeps, [call(5), call(5)])
        # Final error keeps the quota marker the adapter's quota-only
        # candidate fallback matches on.
        self.assertIsInstance(run.error, GeminiError)
        self.assertNotIsInstance(run.error, GeminiResponseError)
        self.assertIn("QUOTA_EXHAUSTED", str(run.error))

    def test_limit_zero_quota_does_not_consume_engine_retries(self):
        run = self._run([RuntimeError("429 RESOURCE_EXHAUSTED quota limit: 0")])

        # Immediate typed quota failure: one call, no engine retry, no sleep.
        self.assertEqual(run.calls, 1)
        self.assertEqual(run.sleeps, [])
        # Same quota marker, so the outer ordered candidate fallback in the
        # adapter still applies.
        self.assertIsInstance(run.error, GeminiError)
        self.assertNotIsInstance(run.error, GeminiResponseError)
        self.assertIn("QUOTA_EXHAUSTED", str(run.error))
