"""PR E regression tests: explicit MIXED printed/handwritten Gemini OCR.

``MIXED`` is a manual document-level ``Document.text_input_type`` choice.
Every page of a MIXED document uses the single approved mixed prompt contract
(``gemini-mixed-content-prompt-v1``) with raw plain-text output; there is no
per-page classification, no per-page routing, and no mixed-specific database
persistence. Existing printed, handwritten, Hebrew printed (PR C), and general
Hebrew handwritten contracts are unchanged. No production data is used and no
live Gemini call is made.
"""

from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult, GeminiOcrPageCheckpoint
from documents.services.archive_item_validation import TEXT_INPUT_TYPE_UI_CHOICES
from documents.services.gemini_engine import (
    GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
    GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
    _MIXED_CONTENT_PROMPT,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL_CANDIDATES
from documents.services.gemini_page_checkpoints import build_gemini_attempt_identity
from documents.services.ocr_routing import (
    OCR_ROUTES,
    gemini_model_candidates,
    select_ocr_route,
)
from documents.services.page_extraction import PageImage
from documents.views import _parse_text_input_type

# Pinned SHA-256 of the approved mixed prompt (closed product contract).
# Any wording, punctuation, ordering, marker, or whitespace change to
# _MIXED_CONTENT_PROMPT is a contract change and must update this hash
# deliberately together with a prompt-contract-version review.
_APPROVED_MIXED_PROMPT_SHA256 = (
    "f4a781de2bd04f6a971bfc2714a028adfff47734298853aa6110891376808c43"
)

_ROUTED_LANGUAGES = ("he", "en", "fr", "ar")


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


class MixedTextInputTypeChoiceTests(SimpleTestCase):
    def test_mixed_is_a_document_level_choice(self):
        self.assertEqual(Document.TextInputType.MIXED, "MIXED")
        self.assertIn(
            ("MIXED", "Mixed printed and handwritten"),
            Document.TextInputType.choices,
        )

    def test_existing_choices_are_unchanged(self):
        self.assertEqual(Document.TextInputType.HANDWRITTEN, "HANDWRITTEN")
        self.assertEqual(Document.TextInputType.PRINTED, "PRINTED")
        self.assertEqual(DocumentTextResult.OcrPromptVariant.PRINTED, "printed")
        self.assertEqual(DocumentTextResult.OcrPromptVariant.HANDWRITTEN, "handwritten")
        self.assertEqual(
            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
            "hebrew_general_handwritten",
        )

    def test_upload_api_parser_accepts_mixed(self):
        self.assertEqual(_parse_text_input_type("MIXED"), "MIXED")
        self.assertEqual(_parse_text_input_type(" mixed "), "MIXED")
        self.assertEqual(_parse_text_input_type("HANDWRITTEN"), "HANDWRITTEN")
        self.assertEqual(_parse_text_input_type("PRINTED"), "PRINTED")

    def test_upload_api_parser_still_rejects_unknown_values(self):
        for raw in ("UNKNOWN", "MIXED_CONTENT", "", None, "PRINTED_AND_HANDWRITTEN"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    _parse_text_input_type(raw)

    def test_upload_form_choices_expose_mixed(self):
        values = [value for value, _label in TEXT_INPUT_TYPE_UI_CHOICES]
        self.assertEqual(
            values,
            [
                Document.TextInputType.HANDWRITTEN,
                Document.TextInputType.PRINTED,
                Document.TextInputType.MIXED,
            ],
        )


class MixedOcrRoutingTests(SimpleTestCase):
    def test_mixed_routes_to_gemini_for_every_supported_language(self):
        for language in _ROUTED_LANGUAGES:
            with self.subTest(language=language):
                route = select_ocr_route(language, Document.TextInputType.MIXED)
                self.assertEqual(
                    route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI
                )
                self.assertEqual(
                    route.prompt_variant, DocumentTextResult.OcrPromptVariant.MIXED
                )

    def test_hebrew_mixed_does_not_require_transkribus_flag(self):
        with patch.dict(
            os.environ,
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
            clear=False,
        ):
            route = select_ocr_route("he", Document.TextInputType.MIXED)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            route.prompt_variant, DocumentTextResult.OcrPromptVariant.MIXED
        )

    def test_arabic_mixed_is_not_routed_to_antigravity(self):
        with patch.dict(
            os.environ,
            {"ENABLE_ANTIGRAVITY_ARABIC_PRINTED": "true"},
            clear=False,
        ):
            route = select_ocr_route("ar", Document.TextInputType.MIXED)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            route.prompt_variant, DocumentTextResult.OcrPromptVariant.MIXED
        )

    def test_existing_routes_are_unchanged(self):
        cases = (
            ("he", Document.TextInputType.PRINTED, "printed"),
            ("en", Document.TextInputType.HANDWRITTEN, "handwritten"),
            ("en", Document.TextInputType.PRINTED, "printed"),
            ("fr", Document.TextInputType.HANDWRITTEN, "handwritten"),
            ("fr", Document.TextInputType.PRINTED, "printed"),
            ("ar", Document.TextInputType.HANDWRITTEN, "handwritten"),
            ("ar", Document.TextInputType.PRINTED, "printed"),
        )
        for language, text_input_type, expected_variant in cases:
            with self.subTest(language=language, text_input_type=text_input_type):
                route = OCR_ROUTES[(language, text_input_type)]
                self.assertEqual(
                    route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI
                )
                self.assertEqual(route.prompt_variant, expected_variant)

    def test_unknown_text_input_type_is_rejected_not_treated_as_mixed(self):
        for raw in ("UNKNOWN", "MIXED_CONTENT", "", None):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    select_ocr_route("he", raw)
                self.assertIn("text_input_type", str(ctx.exception))

    def test_mixed_uses_default_gemini_model_candidates(self):
        for language in _ROUTED_LANGUAGES:
            with self.subTest(language=language):
                route = OCR_ROUTES[(language, Document.TextInputType.MIXED)]
                candidates = gemini_model_candidates(
                    route,
                    language=language,
                    text_input_type=Document.TextInputType.MIXED,
                    gemini_hebrew_printed_model="hebrew-printed-model",
                )
                self.assertEqual(candidates, DEFAULT_GEMINI_MODEL_CANDIDATES)


class MixedPromptContractTests(SimpleTestCase):
    def test_mixed_prompt_matches_approved_contract_exactly(self):
        self.assertEqual(
            hashlib.sha256(_MIXED_CONTENT_PROMPT.encode("utf-8")).hexdigest(),
            _APPROVED_MIXED_PROMPT_SHA256,
        )

    def test_mixed_prompt_retains_approved_guardrails(self):
        guardrails = (
            "expert transcriber of mixed printed and handwritten historical",
            "extreme visual fidelity, completeness, and verbatim accuracy",
            "including both printed and handwritten text",
            "entirely printed, entirely handwritten, or contain any mixture",
            "Never omit visible text merely because it belongs to a different",
            "Apply the same strict accuracy standards to printed text",
            "In pre-printed forms filled in by hand, transcribe the printed "
            "field label followed naturally by the handwritten entry",
            "Preserve handwritten additions, marginal notes, interlinear",
            "Follow the document\u2019s visible reading order",
            "Do not move all handwritten material to the end",
            "Do not add labels such as \u201cprinted text\u201d",
            "Use the exact marker [?]",
            "Example: \u05d1[?]\u05d9\u05ea.",
            "Example: \u05d9\u05e8\u05d5\u05e9\u05dc\u05d9\u05dd[?].",
            "If no responsible reading is possible, use the exact token [UNCLEAR]",
            "Output raw plain text only",
            "Stop immediately after the last visible meaningful text",
        )
        for phrase in guardrails:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, _MIXED_CONTENT_PROMPT)

        self.assertNotIn("OUTPUT FORMAT", _MIXED_CONTENT_PROMPT)
        self.assertNotIn('{"text":', _MIXED_CONTENT_PROMPT)

    def test_mixed_prompt_serialization_matches_approved_contract(self):
        # Instruction bullets use "- ", never "* ".
        self.assertGreater(
            sum(
                1
                for line in _MIXED_CONTENT_PROMPT.splitlines()
                if line.startswith("- ")
            ),
            0,
        )
        self.assertFalse(
            any(line.startswith("* ") for line in _MIXED_CONTENT_PROMPT.splitlines())
        )

        # The six section headings are followed directly by the first
        # instruction line — no extra blank line after the heading.
        heading_first_lines = (
            ("CRITICAL ACCURACY DIRECTIVE:", "- Faithfulness to the visible source"),
            ("TEXT COVERAGE:", "1. Transcribe all meaningful printed"),
            ("ORDER AND STRUCTURE:", "- Follow the document"),
            ("VERBATIM TRANSCRIPTION:", "- Preserve the original wording"),
            ("UNCERTAINTY AND UNREADABLE TEXT:", "- Use the exact marker [?]"),
            ("OUTPUT:", "- Output raw plain text only"),
        )
        for heading, first_line_prefix in heading_first_lines:
            with self.subTest(heading=heading):
                self.assertIn(
                    f"{heading}\n{first_line_prefix}",
                    _MIXED_CONTENT_PROMPT,
                )
                self.assertNotIn(f"{heading}\n\n", _MIXED_CONTENT_PROMPT)

        # Code-block prohibition uses two exact three-backtick sequences.
        code_block_prohibition = "(no ``` or ```text)"
        self.assertIn(code_block_prohibition, _MIXED_CONTENT_PROMPT)
        self.assertEqual(_MIXED_CONTENT_PROMPT.count("```"), 2)
        self.assertNotIn("(no `or`text)", _MIXED_CONTENT_PROMPT)

    def test_mixed_contract_version_is_new_and_distinct(self):
        self.assertEqual(
            GEMINI_MIXED_PROMPT_CONTRACT_VERSION, "gemini-mixed-content-prompt-v1"
        )
        self.assertLessEqual(len(GEMINI_MIXED_PROMPT_CONTRACT_VERSION), 64)
        self.assertNotEqual(
            GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
            GEMINI_OCR_PROMPT_CONTRACT_VERSION,
        )
        self.assertNotEqual(
            GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )

    def test_mixed_contract_is_plain_text_for_every_language_hint(self):
        for language_hint in ("he", "en", "fr", "ar", None, ""):
            with self.subTest(language_hint=language_hint):
                contract = _contract(
                    DocumentTextResult.OcrPromptVariant.MIXED,
                    language_hint,
                )
                self.assertEqual(contract.output_mode, "plain_text")
                self.assertEqual(contract.api_version, "v1beta")
                self.assertEqual(contract.effective_temperature, 0.0)
                self.assertEqual(
                    contract.prompt_contract_version,
                    GEMINI_MIXED_PROMPT_CONTRACT_VERSION,
                )

    def test_other_contracts_keep_their_versions(self):
        hebrew_printed = _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he")
        self.assertEqual(
            hebrew_printed.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        cases = (
            ("en", DocumentTextResult.OcrPromptVariant.PRINTED),
            ("fr", DocumentTextResult.OcrPromptVariant.HANDWRITTEN),
            ("ar", DocumentTextResult.OcrPromptVariant.PRINTED),
            ("he", DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN),
        )
        for language_hint, prompt_variant in cases:
            with self.subTest(prompt_variant=prompt_variant):
                contract = _contract(prompt_variant, language_hint)
                self.assertEqual(
                    contract.prompt_contract_version,
                    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
                )


class MixedPlainTextExecutionTests(SimpleTestCase):
    @patch("documents.services.gemini_engine._parse_page_json_strict")
    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_mixed_route_uses_plain_text_and_never_json_parsing(
        self,
        _mock_get_key,
        mock_create_client,
        mock_parse_json,
    ):
        transcription = (
            "טופס רישום מודפס: שם המבקש ירושלים[?]\n"
            "הערה בכתב יד בשוליים: יש לבדוק את התאריך 12.3.1947.\n"
            "המשך הטקסט המודפס של הטופס נמשך כאן ללא שינוי."
        )
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text=transcription)
        mock_create_client.return_value = mock_client

        result = transcribe_pages_with_gemini(
            _pages(),
            "he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
        )

        self.assertEqual(result.text, transcription)
        mock_parse_json.assert_not_called()
        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        mock_create_client.assert_called_once_with("test-key", api_version="v1beta")

        prompt_text = mock_client.models.generate_content.call_args.kwargs["contents"][
            0
        ].text
        self.assertIn("Output raw plain text only", prompt_text)
        self.assertIn("Language hint: he.", prompt_text)
        self.assertNotIn("OUTPUT FORMAT", prompt_text)

        config = mock_client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.temperature, 0.0)


class MixedCheckpointIdentityTests(SimpleTestCase):
    def _identity(self, contract, *, prompt_variant, text_input_type):
        return build_gemini_attempt_identity(
            pages=_pages(),
            language_hint="he",
            text_input_type=text_input_type,
            handwriting_type=None,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=prompt_variant,
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

    def test_mixed_contract_identity_differs_from_other_prompt_contracts(self):
        mixed_identity = self._identity(
            _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he"),
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
        )
        for other_variant, other_text_input_type in (
            (
                DocumentTextResult.OcrPromptVariant.PRINTED,
                Document.TextInputType.PRINTED,
            ),
            (
                DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN,
                Document.TextInputType.HANDWRITTEN,
            ),
        ):
            with self.subTest(other_variant=other_variant):
                other_identity = self._identity(
                    _contract(other_variant, "he"),
                    prompt_variant=other_variant,
                    text_input_type=other_text_input_type,
                )
                self.assertNotEqual(
                    mixed_identity.identity_fingerprint,
                    other_identity.identity_fingerprint,
                )
                self.assertNotEqual(
                    mixed_identity.prompt_fingerprint,
                    other_identity.prompt_fingerprint,
                )
                self.assertNotEqual(
                    mixed_identity.prompt_contract_version,
                    other_identity.prompt_contract_version,
                )

    def test_mixed_contract_change_alone_changes_identity(self):
        # Same route/text_input_type inputs; only the prompt contract differs.
        # Checkpoints created under another prompt contract must not be
        # reusable for MIXED.
        mixed_contract = _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he")
        other_contract = _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he")

        mixed_identity = self._identity(
            mixed_contract,
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
        )
        crossed_identity = self._identity(
            other_contract,
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
        )

        self.assertNotEqual(
            mixed_identity.identity_fingerprint,
            crossed_identity.identity_fingerprint,
        )

    def test_identical_mixed_inputs_produce_stable_identity(self):
        first = self._identity(
            _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he"),
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
        )
        second = self._identity(
            _contract(DocumentTextResult.OcrPromptVariant.MIXED, "he"),
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            text_input_type=Document.TextInputType.MIXED,
        )
        self.assertEqual(first.identity_fingerprint, second.identity_fingerprint)
        self.assertEqual(first.prompt_fingerprint, second.prompt_fingerprint)
        self.assertEqual(first.config_fingerprint, second.config_fingerprint)


class MixedNoPerPageClassificationTests(SimpleTestCase):
    def test_page_image_has_no_content_classification_field(self):
        self.assertEqual(
            set(PageImage.__dataclass_fields__),
            {
                "page_index",
                "image_bytes",
                "mime_type",
                "source_identity",
                "source_content_fingerprint",
                "original_image_bytes",
                "original_mime_type",
            },
        )
        self.assertNotIn("content_classification", PageImage.__dataclass_fields__)
        self.assertNotIn("content_type", PageImage.__dataclass_fields__)
        self.assertNotIn("is_mixed", PageImage.__dataclass_fields__)

    def test_page_checkpoint_has_no_mixed_specific_persistence(self):
        field_names = {
            field.name for field in GeminiOcrPageCheckpoint._meta.get_fields()
        }
        for forbidden in (
            "text_input_type",
            "prompt_variant",
            "content_type",
            "content_classification",
            "is_mixed",
        ):
            self.assertNotIn(forbidden, field_names)
