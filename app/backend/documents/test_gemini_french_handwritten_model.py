"""French handwritten Gemini 3.6 Flash routing and generation profile."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult
from documents.services.gemini_engine import transcribe_pages_with_gemini
from documents.services.gemini_models import (
    FRENCH_HANDWRITTEN_GEMINI_MODEL,
    FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    LATIN_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
)
from documents.services.ocr_routing import OcrRouteConfig, gemini_model_candidates
from documents.services.page_extraction import PageImage


_PAGE = PageImage(
    page_index=1,
    image_bytes=b"synthetic-french-handwriting",
    mime_type="image/png",
)


def _successful_response() -> SimpleNamespace:
    return SimpleNamespace(
        text="French handwritten archival transcription",
        candidates=[SimpleNamespace(finish_reason="STOP")],
        prompt_feedback=SimpleNamespace(block_reason=None),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=None,
            total_token_count=120,
        ),
    )


class FrenchHandwrittenGeminiRoutingTests(SimpleTestCase):
    def _route(self) -> OcrRouteConfig:
        return OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

    def test_french_handwriting_uses_single_gemini_36_flash_candidate(self):
        candidates = gemini_model_candidates(
            self._route(),
            language=Document.Language.FRENCH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            gemini_hebrew_printed_model="unused-model",
        )

        self.assertEqual(
            candidates,
            FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
        )
        self.assertEqual(candidates, (FRENCH_HANDWRITTEN_GEMINI_MODEL,))

    def test_english_handwriting_keeps_existing_candidate_chain(self):
        candidates = gemini_model_candidates(
            self._route(),
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            gemini_hebrew_printed_model="unused-model",
        )

        self.assertEqual(
            candidates,
            LATIN_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
        )


class FrenchHandwrittenGeminiGenerationProfileTests(SimpleTestCase):
    def test_gemini_36_uses_minimal_thinking_and_model_default_decoding(self):
        client = Mock()
        client.models.generate_content.return_value = _successful_response()

        with (
            patch(
                "documents.services.gemini_engine._get_api_key",
                return_value="test-key",
            ),
            patch(
                "documents.services.gemini_engine._create_client",
                return_value=client,
            ),
        ):
            result = transcribe_pages_with_gemini(
                [_PAGE],
                Document.Language.FRENCH,
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                model_name=FRENCH_HANDWRITTEN_GEMINI_MODEL,
                max_output_tokens=4096,
                max_provider_calls=1,
            )

        self.assertEqual(
            result.engine_name,
            FRENCH_HANDWRITTEN_GEMINI_MODEL,
        )
        self.assertEqual(client.models.generate_content.call_count, 1)

        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.top_k)
        self.assertIsNone(config.top_p)
        self.assertEqual(config.max_output_tokens, 4096)
        self.assertIsNotNone(config.thinking_config)
        self.assertIsNone(config.thinking_config.thinking_budget)

        thinking_level = getattr(
            config.thinking_config.thinking_level,
            "value",
            config.thinking_config.thinking_level,
        )
        self.assertEqual(
            str(thinking_level).rsplit(".", 1)[-1].lower(),
            "minimal",
        )
