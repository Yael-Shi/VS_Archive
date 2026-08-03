"""Regression: general Hebrew handwritten prompt anti-runaway hardening.

Hardens only ``_HEBREW_GENERAL_HANDWRITTEN_PROMPT`` and introduces the
route-specific contract version
``gemini-hebrew-general-handwritten-prompt-v2``. This is prompt-contract
hardening only: it does not add deterministic repetition detection, change
retry budgets, routing, model candidates, or output mode, and does not claim
the provider runaway issue is fully resolved.

Production observations motivating this change (after PRs B–E):

* Document 289, ``hebrew_general_handwritten``, page 1: three bounded attempts;
  attempt 3 still ``MAX_TOKENS``; ``raw_output_length=30163`` with
  ``max_output_tokens=16384`` for ordinary sparse handwriting.
* Document 291, same route: page 1 succeeded (961 chars) and was checkpointed;
  page 2 (handwriting plus a pasted portrait) hit ``MAX_TOKENS`` after three
  attempts with attempt-3 ``raw_output_length=30369``.

No production data is used and no live Gemini call is made.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult
from documents.services.gemini_engine import (
    GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION,
    GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
    GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
    _HEBREW_GENERAL_HANDWRITTEN_PROMPT,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL_CANDIDATES
from documents.services.gemini_page_checkpoints import build_gemini_attempt_identity
from documents.services.ocr_routing import (
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_ROUTE,
    gemini_model_candidates,
    select_ocr_route,
)
from documents.services.page_extraction import PageImage

# Pinned SHA-256 of the hardened general Hebrew handwritten prompt.
# Any wording, punctuation, ordering, marker, or whitespace change to
# _HEBREW_GENERAL_HANDWRITTEN_PROMPT is a contract change and must update
# this hash deliberately together with a prompt-contract-version review.
_APPROVED_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256 = (
    "ce9aef4f083d6493db5e344b5f8405676bd75f019741bd7aaaa52785a3126988"
)

# Exact SHA-256 of ``_HEBREW_GENERAL_HANDWRITTEN_PROMPT`` at the branch base
# commit (``86f8774600505939c03b8c5a9c6573eae1c63b4d``, current ``main`` /
# pre-hardening). Computed from that revision's source via ``git show`` +
# AST literal evaluation — not an invented approximation.
_BASE_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256 = (
    "c576010ed8127d4cd1c65c01d09d5641396dce2fed49374730de60cc342cf863"
)


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


class HebrewGeneralHandwrittenPromptHardeningTests(SimpleTestCase):
    def test_prompt_matches_pinned_sha256(self):
        self.assertEqual(
            hashlib.sha256(
                _HEBREW_GENERAL_HANDWRITTEN_PROMPT.encode("utf-8")
            ).hexdigest(),
            _APPROVED_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
        )

    def test_anti_runaway_instructions_are_present(self):
        guardrails = (
            "Transcribe all visible handwritten text in the image as "
            "faithfully and completely as possible",
            "Transcribe all visible text, including short, isolated, or faint "
            "text covered by the preceding rules",
            "Do not invent text from non-text visual regions such as "
            "photographs, portraits, or illustrations",
            "Do not describe, interpret, classify, or narrate photographs, "
            "portraits, illustrations, stains",
            "Stop immediately after the last visible text on the page",
            "Do not repeat text, continue with blank lines, or generate padding "
            "after the page content",
            "Output only the raw transcription text",
            "Do not add commentary or labels",
            # Existing accuracy / uncertainty / output requirements preserved.
            "extreme visual fidelity and verbatim accuracy",
            "Visual evidence always takes priority",
            "Use the exact marker [?]",
            "Example: \u05d1[?]\u05d9\u05ea.",
            "Example: \u05d9\u05e8\u05d5\u05e9\u05dc\u05d9\u05dd[?].",
            "If no responsible reading is possible, use [UNCLEAR]",
            "Do not output JSON",
        )
        for phrase in guardrails:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, _HEBREW_GENERAL_HANDWRITTEN_PROMPT)

        self.assertNotIn("OUTPUT FORMAT", _HEBREW_GENERAL_HANDWRITTEN_PROMPT)
        self.assertNotIn('{"text":', _HEBREW_GENERAL_HANDWRITTEN_PROMPT)
        self.assertNotIn("meaningful visible", _HEBREW_GENERAL_HANDWRITTEN_PROMPT)


class HebrewGeneralHandwrittenContractVersionTests(SimpleTestCase):
    def test_general_hebrew_handwritten_receives_v2_contract(self):
        self.assertEqual(
            GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION,
            "gemini-hebrew-general-handwritten-prompt-v2",
        )
        self.assertLessEqual(
            len(GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION),
            64,
        )
        contract = _contract(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "he",
        )
        self.assertEqual(
            contract.prompt_contract_version,
            GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(contract.output_mode, "plain_text")
        self.assertEqual(contract.api_version, "v1beta")
        self.assertEqual(contract.effective_temperature, 0.0)

    def test_other_routes_retain_their_contract_versions(self):
        cases = (
            (
                "he",
                DocumentTextResult.OcrPromptVariant.PRINTED,
                GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
            ),
            (
                "he",
                DocumentTextResult.OcrPromptVariant.MIXED,
                GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
            ),
            (
                "en",
                DocumentTextResult.OcrPromptVariant.PRINTED,
                GEMINI_OCR_PROMPT_CONTRACT_VERSION,
            ),
            (
                "fr",
                DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                GEMINI_OCR_PROMPT_CONTRACT_VERSION,
            ),
            (
                "ar",
                DocumentTextResult.OcrPromptVariant.PRINTED,
                GEMINI_OCR_PROMPT_CONTRACT_VERSION,
            ),
            (
                "he",
                DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                GEMINI_OCR_PROMPT_CONTRACT_VERSION,
            ),
        )
        for language_hint, prompt_variant, expected_version in cases:
            with self.subTest(prompt_variant=prompt_variant):
                contract = _contract(prompt_variant, language_hint)
                self.assertEqual(contract.prompt_contract_version, expected_version)


class HebrewGeneralHandwrittenRoutingAndExecutionTests(SimpleTestCase):
    def test_routing_and_model_candidates_unchanged(self):
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
        self.assertEqual(candidates, DEFAULT_GEMINI_MODEL_CANDIDATES)

    @patch("documents.services.gemini_engine._parse_page_json_strict")
    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_plain_text_output_mode_and_no_json_parsing(
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
        self.assertIn(
            "Stop immediately after the last visible text on the page",
            prompt_text,
        )
        self.assertIn("photographs, portraits", prompt_text)
        self.assertNotIn("OUTPUT FORMAT", prompt_text)
        config = mock_client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.temperature, 0.0)


class HebrewGeneralHandwrittenCheckpointIdentityTests(SimpleTestCase):
    def _identity(self, contract):
        return build_gemini_attempt_identity(
            pages=_pages(),
            language_hint="he",
            text_input_type=Document.TextInputType.HANDWRITTEN,
            handwriting_type=Document.HandwritingType.GENERAL,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
            ),
            model_candidates=("model-a",),
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

    def test_identical_inputs_produce_stable_identity(self):
        first = self._identity(
            _contract(
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
                "he",
            )
        )
        second = self._identity(
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
            GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION,
        )

    def test_current_prompt_sha_differs_from_branch_base_prompt_sha(self):
        current_sha = hashlib.sha256(
            _HEBREW_GENERAL_HANDWRITTEN_PROMPT.encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            current_sha,
            _APPROVED_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
        )
        self.assertNotEqual(
            current_sha,
            _BASE_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
        )
        self.assertEqual(
            _BASE_HEBREW_GENERAL_HANDWRITTEN_PROMPT_SHA256,
            "c576010ed8127d4cd1c65c01d09d5641396dce2fed49374730de60cc342cf863",
        )

    def test_contract_version_alone_changes_checkpoint_identity(self):
        # Keep the final hardened prompt fingerprint unchanged; change only
        # the contract-version field back to the shared pre-hardening marker.
        current_contract = _contract(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "he",
        )
        version_only_prior = replace(
            current_contract,
            prompt_contract_version=GEMINI_OCR_PROMPT_CONTRACT_VERSION,
        )

        current_identity = self._identity(current_contract)
        version_only_identity = self._identity(version_only_prior)

        self.assertEqual(
            current_identity.prompt_fingerprint,
            version_only_identity.prompt_fingerprint,
        )
        self.assertNotEqual(
            current_identity.prompt_contract_version,
            version_only_identity.prompt_contract_version,
        )
        self.assertEqual(
            current_identity.prompt_contract_version,
            GEMINI_HEBREW_GENERAL_HANDWRITTEN_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            version_only_identity.prompt_contract_version,
            GEMINI_OCR_PROMPT_CONTRACT_VERSION,
        )
        self.assertNotEqual(
            current_identity.identity_fingerprint,
            version_only_identity.identity_fingerprint,
        )
