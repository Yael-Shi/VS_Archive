"""Hebrew general handwritten prompt rollback and Gemini 3.6 coverage.

PR #371 changed only the prompt and failed live, so the exact restored v1
prompt remains locked here. Later non-persistent full-page probes on documents
289, 291, and 306 selected Gemini 3.6 Flash without changing that prompt.

The model candidate change creates a new checkpoint configuration identity.
No production data is used and no live Gemini call is made here.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult
from documents.services.gemini_engine import (
    GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
    GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
    _HEBREW_GENERAL_HANDWRITTEN_PROMPT,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_models import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_GEMINI_MODEL_CANDIDATES,
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL,
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
)
from documents.services.gemini_page_checkpoints import build_gemini_attempt_identity
from documents.services.ocr_routing import (
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_ROUTE,
    gemini_model_candidates,
    select_ocr_route,
)
from documents.services.page_extraction import PageImage

# Exact SHA-256 of the prompt active in image 202608031223, before PR #371.
_RESTORED_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256 = (
    "c576010ed8127d4cd1c65c01d09d5641396dce2fed49374730de60cc342cf863"
)

# Exact SHA-256 of the rejected PR #371 prompt. It is retained as incident
# evidence only and must not become the active prompt again accidentally.
_REJECTED_V2_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256 = (
    "ce9aef4f083d6493db5e344b5f8405676bd75f019741bd7aaaa52785a3126988"
)
_REJECTED_V2_PROMPT_CONTRACT_VERSION = "gemini-hebrew-general-handwritten-prompt-v2"


def _response(*, text: str, finish_reason: str = "STOP") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def _pages() -> list[PageImage]:
    return [
        PageImage(
            page_index=1,
            image_bytes=b"png",
            mime_type="image/png",
            source_identity="document.pdf",
            source_content_fingerprint="a" * 64,
        )
    ]


def _contract(prompt_variant: str, language_hint: str | None):
    return gemini_transcription_contract(
        prompt_variant=prompt_variant,
        language_hint=language_hint,
        temperature=0.2,
    )


class HebrewGeneralHandwrittenPromptRollbackTests(SimpleTestCase):
    def test_prompt_matches_restored_pre_experiment_sha256(self):
        current_sha = hashlib.sha256(
            _HEBREW_GENERAL_HANDWRITTEN_PROMPT.encode("utf-8")
        ).hexdigest()

        self.assertEqual(
            current_sha,
            _RESTORED_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
        )
        self.assertNotEqual(
            current_sha,
            _REJECTED_V2_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
        )

    def test_rejected_v2_instructions_are_not_active(self):
        self.assertIn(
            "Transcribe all visible handwritten text in the image as "
            "faithfully and completely as possible",
            _HEBREW_GENERAL_HANDWRITTEN_PROMPT,
        )
        self.assertIn(
            "Output only the transcription text",
            _HEBREW_GENERAL_HANDWRITTEN_PROMPT,
        )
        for rejected_phrase in (
            "Do not invent text from non-text visual regions",
            "Stop immediately after the last visible text on the page",
            "Do not repeat text, continue with blank lines, or generate padding",
            "Output only the raw transcription text",
        ):
            with self.subTest(rejected_phrase=rejected_phrase):
                self.assertNotIn(
                    rejected_phrase,
                    _HEBREW_GENERAL_HANDWRITTEN_PROMPT,
                )


class HebrewGeneralHandwrittenContractRollbackTests(SimpleTestCase):
    def test_general_hebrew_handwritten_uses_shared_v1_contract(self):
        contract = _contract(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "he",
        )

        self.assertEqual(
            contract.prompt_contract_version,
            GEMINI_OCR_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(contract.output_mode, "plain_text")
        self.assertEqual(contract.api_version, "v1beta")
        self.assertEqual(contract.effective_temperature, 0.0)

    def test_printed_and_mixed_contract_versions_are_unchanged(self):
        printed = _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he")
        mixed = _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he")

        self.assertEqual(
            printed.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            mixed.prompt_contract_version,
            GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
        )


class HebrewGeneralHandwrittenRoutingAndExecutionTests(SimpleTestCase):
    def test_routing_uses_cost_aware_25_then_36_candidates(self):
        with patch.dict(
            __import__("os").environ,
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
            clear=False,
        ):
            route = select_ocr_route(
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                handwriting_type=Document.HandwritingType.GENERAL,
            )

        self.assertEqual(route, HEBREW_GENERAL_HANDWRITTEN_GEMINI_ROUTE)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            route.prompt_variant,
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
        )
        candidates = gemini_model_candidates(
            route,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            gemini_hebrew_printed_model="hebrew-printed-model",
        )
        self.assertEqual(
            candidates,
            HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
        )
        self.assertEqual(
            candidates,
            (
                DEFAULT_GEMINI_MODEL,
                HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL,
            ),
        )

    @patch("documents.services.gemini_engine._create_client")
    @patch(
        "documents.services.gemini_engine._get_api_key",
        return_value="test-key",
    )
    def test_selected_model_uses_minimal_thinking_and_default_decoding(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        transcription = "זהו תעתוק עברי כללי ארוך מספיק לבדיקת פרופיל Gemini 3.6 Flash."
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text=transcription)
        mock_create_client.return_value = mock_client

        result = transcribe_pages_with_gemini(
            _pages(),
            Document.Language.HEBREW,
            prompt_variant=(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
            ),
            model_name=HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL,
            max_output_tokens=4096,
            max_provider_calls=1,
        )

        self.assertEqual(result.text, transcription)
        self.assertEqual(
            result.engine_name,
            HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL,
        )

        config = mock_client.models.generate_content.call_args.kwargs["config"]
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

    @patch("documents.services.gemini_engine._parse_page_json_strict")
    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_plain_text_execution_keeps_the_restored_prompt(
        self,
        _mock_get_key,
        mock_create_client,
        mock_parse_json,
    ):
        transcription = (
            "זהו תעתוק עברי כללי ארוך מספיק לצורך בדיקת מסלול כתב היד העברי הכללי."
        )
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text=transcription)
        mock_create_client.return_value = mock_client

        result = transcribe_pages_with_gemini(
            _pages(),
            Document.Language.HEBREW,
            prompt_variant=(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
            ),
            temperature=0.11,
        )

        self.assertEqual(result.text, transcription)
        mock_parse_json.assert_not_called()
        mock_create_client.assert_called_once_with("test-key", api_version="v1beta")
        prompt_text = mock_client.models.generate_content.call_args.kwargs["contents"][
            0
        ].text
        self.assertIn("Output only the transcription text", prompt_text)
        self.assertNotIn(
            "Stop immediately after the last visible text on the page",
            prompt_text,
        )
        self.assertNotIn("OUTPUT FORMAT", prompt_text)
        config = mock_client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.temperature, 0.0)


class HebrewGeneralHandwrittenCheckpointIdentityRollbackTests(SimpleTestCase):
    def _identity(
        self,
        contract,
        *,
        prompt_variant: str,
        text_input_type: str,
        handwriting_type: str | None,
        model_candidates: tuple[str, ...] = ("model-a",),
    ):
        return build_gemini_attempt_identity(
            pages=_pages(),
            language_hint="he",
            text_input_type=text_input_type,
            handwriting_type=handwriting_type,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=prompt_variant,
            model_candidates=model_candidates,
            contract=contract,
            min_text_length=20,
            double_pass=False,
            consistency_min_ratio=0.85,
            temperature=0.2,
            top_k=40,
            top_p=0.95,
            max_output_tokens=8192,
            max_output_tokens_hard_cap=32768,
        )

    def _general_identity(
        self,
        contract,
        *,
        model_candidates: tuple[str, ...] = ("model-a",),
    ):
        return self._identity(
            contract,
            prompt_variant=(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
            ),
            text_input_type=Document.TextInputType.HANDWRITTEN,
            handwriting_type=Document.HandwritingType.GENERAL,
            model_candidates=model_candidates,
        )

    def test_selected_model_creates_new_configuration_identity(self):
        contract = _contract(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "he",
        )
        selected = self._general_identity(
            contract,
            model_candidates=(HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES),
        )
        legacy = self._general_identity(
            contract,
            model_candidates=DEFAULT_GEMINI_MODEL_CANDIDATES,
        )

        self.assertEqual(
            selected.prompt_fingerprint,
            legacy.prompt_fingerprint,
        )
        self.assertEqual(
            selected.prompt_contract_version,
            legacy.prompt_contract_version,
        )
        self.assertNotEqual(
            selected.config_fingerprint,
            legacy.config_fingerprint,
        )
        self.assertNotEqual(
            selected.identity_fingerprint,
            legacy.identity_fingerprint,
        )

    def test_identical_restored_inputs_produce_stable_identity(self):
        first = self._general_identity(
            _contract(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
                "he",
            )
        )
        second = self._general_identity(
            _contract(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
                "he",
            )
        )

        self.assertEqual(first.identity_fingerprint, second.identity_fingerprint)
        self.assertEqual(first.prompt_fingerprint, second.prompt_fingerprint)
        self.assertEqual(first.config_fingerprint, second.config_fingerprint)
        self.assertEqual(
            first.prompt_contract_version,
            GEMINI_OCR_PROMPT_CONTRACT_VERSION,
        )

    def test_rejected_v2_contract_identity_is_not_reused_after_rollback(self):
        restored_contract = _contract(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "he",
        )
        rejected_version_contract = replace(
            restored_contract,
            prompt_contract_version=_REJECTED_V2_PROMPT_CONTRACT_VERSION,
        )

        restored_identity = self._general_identity(restored_contract)
        rejected_identity = self._general_identity(rejected_version_contract)

        self.assertEqual(
            restored_identity.prompt_fingerprint,
            rejected_identity.prompt_fingerprint,
        )
        self.assertEqual(
            restored_identity.config_fingerprint,
            rejected_identity.config_fingerprint,
        )
        self.assertNotEqual(
            restored_identity.prompt_contract_version,
            rejected_identity.prompt_contract_version,
        )
        self.assertNotEqual(
            restored_identity.identity_fingerprint,
            rejected_identity.identity_fingerprint,
        )

    def test_pr_d_config_fingerprint_is_unchanged_across_current_contracts(self):
        general = self._general_identity(
            _contract(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
                "he",
            )
        )
        printed = self._identity(
            _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he"),
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            text_input_type=Document.TextInputType.PRINTED,
            handwriting_type=None,
        )
        mixed = self._identity(
            _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he"),
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
            handwriting_type=None,
        )

        config_fingerprints = {
            general.config_fingerprint,
            printed.config_fingerprint,
            mixed.config_fingerprint,
        }
        self.assertEqual(config_fingerprints, {general.config_fingerprint})
        self.assertEqual(
            printed.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            mixed.prompt_contract_version,
            GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
        )
