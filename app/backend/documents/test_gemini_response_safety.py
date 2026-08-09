from __future__ import annotations

import traceback
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import DocumentTextResult
from documents.services.gemini_engine import (
    GeminiApiError,
    GeminiError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    _classify_response_failure,
    _response_metadata,
    transcribe_pages_with_gemini,
)
from documents.services.page_extraction import PageImage


def _response(
    *,
    text: str = "complete transcription",
    finish_reason: object = "STOP",
    finish_message: str | None = None,
    block_reason: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[
            SimpleNamespace(
                finish_reason=finish_reason,
                finish_message=finish_message,
            )
        ],
        prompt_feedback=SimpleNamespace(block_reason=block_reason),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=120,
        ),
    )


def _metadata(response: object):
    return _response_metadata(
        response,
        model_name="test-model",
        page_index=3,
        attempt=2,
        max_output_tokens=8192,
    )[0]


class GeminiResponseMetadataTests(SimpleTestCase):
    def test_safe_details_labels_provider_call_ordinal(self):
        metadata = _metadata(
            _response(
                text="page text",
                finish_reason="STOP",
            )
        )

        details = metadata.safe_details()

        self.assertIn("provider_call_ordinal=2", details)
        self.assertNotIn("attempt=2", details)


class GeminiResponseClassificationTests(SimpleTestCase):
    def test_known_finish_reasons_have_stable_failure_codes(self):
        cases = (
            ("MAX_TOKENS", GeminiResponseFailureCode.MAX_TOKENS),
            ("SAFETY", GeminiResponseFailureCode.SAFETY),
            ("RECITATION", GeminiResponseFailureCode.RECITATION),
            ("LANGUAGE", GeminiResponseFailureCode.LANGUAGE),
            ("SPII", GeminiResponseFailureCode.SPII),
            ("PROHIBITED_CONTENT", GeminiResponseFailureCode.BLOCKED),
            ("UNEXPECTED_REASON", GeminiResponseFailureCode.OTHER),
        )

        for finish_reason, expected_code in cases:
            with self.subTest(finish_reason=finish_reason):
                metadata = _metadata(_response(finish_reason=finish_reason))
                self.assertEqual(
                    _classify_response_failure(metadata),
                    expected_code,
                )

    def test_enum_style_finish_reason_is_normalized(self):
        metadata = _metadata(
            _response(
                finish_reason=SimpleNamespace(value="MAX_TOKENS"),
            )
        )

        self.assertEqual(metadata.finish_reason, "MAX_TOKENS")
        self.assertEqual(
            _classify_response_failure(metadata),
            GeminiResponseFailureCode.MAX_TOKENS,
        )

    def test_blocked_response_without_candidates_is_classified_before_text_access(self):
        class BlockedResponse:
            candidates = []
            prompt_feedback = SimpleNamespace(block_reason="SAFETY")
            usage_metadata = None

            @property
            def text(self):
                raise ValueError("response has no text parts")

        metadata = _metadata(BlockedResponse())

        self.assertEqual(metadata.candidate_count, 0)
        self.assertEqual(metadata.output_length, 0)
        self.assertEqual(
            _classify_response_failure(metadata),
            GeminiResponseFailureCode.SAFETY,
        )

    def test_stop_with_text_is_success_and_stop_without_text_is_empty(self):
        success = _metadata(_response(text="text", finish_reason="STOP"))
        empty = _metadata(_response(text="", finish_reason="STOP"))

        self.assertIsNone(_classify_response_failure(success))
        self.assertEqual(
            _classify_response_failure(empty),
            GeminiResponseFailureCode.EMPTY_RESPONSE,
        )


class GeminiResponsePrivacyTests(SimpleTestCase):
    def setUp(self):
        self.pages = [
            PageImage(
                page_index=1,
                image_bytes=b"png",
                mime_type="image/png",
            )
        ]

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_json_parse_failure_never_logs_or_raises_raw_response(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        # Arabic printed is the remaining Gemini JSON-contract route now that
        # Hebrew printed uses the plain-text contract (PR C).
        sensitive_marker = "SENSITIVE_ARCHIVE_TEXT"
        invalid_response = _response(
            text=f'{{"text": "{sensitive_marker}',
            finish_reason="STOP",
        )
        mock_client = Mock()
        mock_client.models.generate_content.side_effect = [
            invalid_response,
            invalid_response,
            invalid_response,
        ]
        mock_create_client.return_value = mock_client

        with self.assertLogs(
            "documents.services.gemini_engine",
            level="ERROR",
        ) as captured:
            with self.assertRaises(GeminiResponseError) as ctx:
                transcribe_pages_with_gemini(
                    self.pages,
                    "ar",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                    model_name="test-model",
                )

        self.assertEqual(
            ctx.exception.failure_code,
            GeminiResponseFailureCode.JSON_PARSE,
        )
        self.assertEqual(mock_client.models.generate_content.call_count, 3)
        self.assertNotIn(sensitive_marker, str(ctx.exception))
        self.assertNotIn(sensitive_marker, "\n".join(captured.output))

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_permanent_finish_reasons_fail_without_retry(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        cases = (
            ("SAFETY", GeminiResponseFailureCode.SAFETY),
            ("RECITATION", GeminiResponseFailureCode.RECITATION),
            ("LANGUAGE", GeminiResponseFailureCode.LANGUAGE),
            ("SPII", GeminiResponseFailureCode.SPII),
        )

        for finish_reason, expected_code in cases:
            with self.subTest(finish_reason=finish_reason):
                mock_client = Mock()
                mock_client.models.generate_content.return_value = _response(
                    text="provider output that must not be accepted",
                    finish_reason=finish_reason,
                )
                mock_create_client.return_value = mock_client

                with self.assertRaises(GeminiResponseError) as ctx:
                    transcribe_pages_with_gemini(
                        self.pages,
                        "en",
                        prompt_variant=(DocumentTextResult.OcrPromptVariant.PRINTED),
                    )

                self.assertEqual(ctx.exception.failure_code, expected_code)
                self.assertEqual(
                    mock_client.models.generate_content.call_count,
                    1,
                )

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_invalid_json_schema_is_rejected_without_raw_content(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        sensitive_marker = "SENSITIVE_ARCHIVE_TEXT"
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(
            text=(
                '{"text": "'
                f'{sensitive_marker}", '
                '"has_unclear": "false", "unclear_count": true}'
            ),
        )
        mock_create_client.return_value = mock_client

        with self.assertLogs(
            "documents.services.gemini_engine",
            level="ERROR",
        ) as captured:
            with self.assertRaises(GeminiResponseError) as ctx:
                transcribe_pages_with_gemini(
                    self.pages,
                    "ar",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                    model_name="test-model",
                )

        self.assertEqual(
            ctx.exception.failure_code,
            GeminiResponseFailureCode.JSON_SCHEMA,
        )
        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        self.assertNotIn(sensitive_marker, str(ctx.exception))
        self.assertNotIn(sensitive_marker, "\n".join(captured.output))

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_empty_json_object_is_rejected(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text="{}")
        mock_create_client.return_value = mock_client

        with self.assertLogs(
            "documents.services.gemini_engine",
            level="ERROR",
        ):
            with self.assertRaises(GeminiResponseError) as ctx:
                transcribe_pages_with_gemini(
                    self.pages,
                    "ar",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                )

        self.assertEqual(
            ctx.exception.failure_code,
            GeminiResponseFailureCode.JSON_SCHEMA,
        )
        self.assertEqual(mock_client.models.generate_content.call_count, 1)

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_finish_message_content_is_not_logged(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        sensitive_marker = "SENSITIVE_FINISH_MESSAGE"
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(
            text="complete printed transcription",
            finish_message=sensitive_marker,
        )
        mock_create_client.return_value = mock_client

        with self.assertLogs(
            "documents.services.gemini_engine",
            level="INFO",
        ) as captured:
            result = transcribe_pages_with_gemini(
                self.pages,
                "en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            )

        self.assertEqual(result.text, "complete printed transcription")
        self.assertNotIn(sensitive_marker, "\n".join(captured.output))

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_provider_exception_content_is_not_exposed(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        sensitive_marker = "SENSITIVE_PROVIDER_DETAIL"
        mock_client = Mock()
        mock_client.models.generate_content.side_effect = RuntimeError(sensitive_marker)
        mock_create_client.return_value = mock_client

        with self.assertRaises(GeminiApiError) as ctx:
            transcribe_pages_with_gemini(
                self.pages,
                "en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            )

        self.assertEqual(
            ctx.exception.failure_code,
            GeminiResponseFailureCode.API_ERROR,
        )
        self.assertIn("exception_class=RuntimeError", str(ctx.exception))
        self.assertNotIn(sensitive_marker, str(ctx.exception))
        formatted_traceback = "".join(traceback.format_exception(ctx.exception))
        self.assertNotIn(sensitive_marker, formatted_traceback)

    @patch("documents.services.gemini_engine.time.sleep")
    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_quota_errors_do_not_chain_provider_exception_text(
        self,
        _mock_get_key,
        mock_create_client,
        _mock_sleep,
    ):
        # PR A traceback privacy: safe QUOTA_EXHAUSTED raises must use
        # ``from None`` so formatted tracebacks never retain provider text.
        sensitive_marker = "SENSITIVE_QUOTA_PROVIDER_DETAIL_8841"
        cases = (
            (
                f"429 RESOURCE_EXHAUSTED {sensitive_marker} limit: 0",
                1,
                "QUOTA_EXHAUSTED: test-model",
            ),
            (
                f"429 RESOURCE_EXHAUSTED {sensitive_marker}",
                3,
                "QUOTA_EXHAUSTED: test-model after retries",
            ),
        )

        for provider_message, expected_calls, expected_message in cases:
            with self.subTest(provider_message=provider_message):
                mock_client = Mock()
                mock_client.models.generate_content.side_effect = RuntimeError(
                    provider_message
                )
                mock_create_client.return_value = mock_client

                with self.assertRaises(GeminiError) as ctx:
                    transcribe_pages_with_gemini(
                        self.pages,
                        "en",
                        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                        model_name="test-model",
                    )

                self.assertEqual(str(ctx.exception), expected_message)
                self.assertIn("QUOTA_EXHAUSTED", str(ctx.exception))
                self.assertNotIn(sensitive_marker, str(ctx.exception))
                formatted_traceback = "".join(traceback.format_exception(ctx.exception))
                self.assertNotIn(sensitive_marker, formatted_traceback)
                self.assertEqual(
                    mock_client.models.generate_content.call_count,
                    expected_calls,
                )
