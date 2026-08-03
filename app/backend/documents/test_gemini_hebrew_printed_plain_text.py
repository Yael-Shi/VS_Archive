"""PR C regression tests: Hebrew printed OCR plain-text response contract.

Hebrew printed (canonical hint ``he`` + ``printed``) uses the plain-text
contract with its route-specific ``gemini-hebrew-printed-prompt-v2`` marker;
all other routes retain ``gemini-ocr-prompt-v1`` and their checkpoint
identities. Provider finish/block privacy behavior is covered by the PR A
tests in ``test_gemini_response_safety.py`` (JSON cases now use Arabic
printed). No production data is used and no live Gemini call is made.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult
from documents.services.gemini_engine import (
    GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
    GeminiError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    _HEBREW_PRINTED_PROMPT,
    _PRINTED_TEXT_PROMPT,
    _parse_page_json_strict,
    _plain_text_response_to_page_data,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_page_checkpoints import build_gemini_attempt_identity
from documents.services.page_extraction import PageImage


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


def _long_synthetic_hebrew_printed_text() -> str:
    """Synthetic document-293-class page: long and otherwise usable. Under
    the JSON contract the whole page depended on one complete ``"text"``
    string; truncation inside it (``Unterminated string``) lost all of it."""
    paragraph = (
        'פרוטוקול הישיבה נפתח במילים "לזכרם של בני הקהילה" ונמשך בדיווח.\n'
        "רשימת השמות כללה כתובות, תאריכים כמו 12.3.1947, ומספרי תיקים.\n"
        'המזכיר ציין: "יש לשמור את המסמך בארכיון" \\ העתק נשלח לוועד.\n'
    )
    return (paragraph * 120).strip()


def _contract(prompt_variant: str, language_hint: str | None):
    return gemini_transcription_contract(
        prompt_variant=prompt_variant,
        language_hint=language_hint,
        temperature=0.2,
    )


class HebrewPrintedContractTests(SimpleTestCase):
    def test_hebrew_printed_contract_is_plain_text_with_route_specific_v2(self):
        contract = _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he")

        self.assertEqual(contract.output_mode, "plain_text")
        self.assertEqual(contract.api_version, "v1beta")
        self.assertEqual(contract.effective_temperature, 0.0)
        self.assertEqual(
            contract.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
            "gemini-hebrew-printed-prompt-v2",
        )
        self.assertLessEqual(len(GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION), 64)

        # The effective prompt fingerprint changed from the JSON-era prompt.
        old_effective_prompt = _PRINTED_TEXT_PROMPT + "\nLanguage hint: he."
        new_effective_prompt = _HEBREW_PRINTED_PROMPT + "\nLanguage hint: he."
        self.assertEqual(
            contract.prompt_fingerprint,
            hashlib.sha256(new_effective_prompt.encode("utf-8")).hexdigest(),
        )
        self.assertNotEqual(
            contract.prompt_fingerprint,
            hashlib.sha256(old_effective_prompt.encode("utf-8")).hexdigest(),
        )

    def test_only_canonical_hebrew_hint_selects_plain_text_contract(self):
        for language_hint in ("ar", "hebrew", "iw", None, ""):
            with self.subTest(language_hint=language_hint):
                contract = _contract(
                    DocumentTextResult.OcrPromptVariant.PRINTED,
                    language_hint,
                )
                self.assertEqual(contract.output_mode, "json")
                self.assertEqual(
                    contract.prompt_contract_version,
                    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
                )

    def test_unchanged_routes_retain_v1_contract_version(self):
        self.assertEqual(GEMINI_OCR_PROMPT_CONTRACT_VERSION, "gemini-ocr-prompt-v1")
        cases = (
            ("en", DocumentTextResult.OcrPromptVariant.PRINTED),
            ("fr", DocumentTextResult.OcrPromptVariant.HANDWRITTEN),
            ("ar", DocumentTextResult.OcrPromptVariant.PRINTED),
            ("he", DocumentTextResult.OcrPromptVariant.HANDWRITTEN),
        )
        for language_hint, prompt_variant in cases:
            with self.subTest(
                language_hint=language_hint,
                prompt_variant=prompt_variant,
            ):
                contract = _contract(prompt_variant, language_hint)
                self.assertEqual(
                    contract.prompt_contract_version,
                    GEMINI_OCR_PROMPT_CONTRACT_VERSION,
                )

    def test_hebrew_printed_prompt_retains_archival_guardrails(self):
        guardrails = (
            "Preserve typos, non-standard spelling, unusual Hebrew forms",
            "Do NOT add Hebrew vowel marks",
            "Do NOT silently omit visible words",
            "Do NOT omit readable URLs",
            "Pay special attention to short Hebrew words",
            "Do not replace an unclear name or personal detail",
            "add the exact marker [?] immediately after it",
            "output the exact token [UNCLEAR]",
            "Output only the transcription text",
            "Do not output JSON",
        )
        for phrase in guardrails:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, _HEBREW_PRINTED_PROMPT)

        self.assertNotIn("OUTPUT FORMAT", _HEBREW_PRINTED_PROMPT)
        self.assertNotIn('{"text":', _HEBREW_PRINTED_PROMPT)


class HebrewPrintedPlainTextExecutionTests(SimpleTestCase):
    @patch("documents.services.gemini_engine._parse_page_json_strict")
    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_document_293_truncated_json_fails_but_plain_text_succeeds_verbatim(
        self,
        _mock_get_key,
        mock_create_client,
        mock_parse_json,
    ):
        # Document-293 failure class: the provider serialized the page as
        # valid JSON, but the response was cut off inside the long "text"
        # string, so the one JSON object carrying the whole page could not be
        # parsed and all otherwise usable OCR text was lost.
        long_text = _long_synthetic_hebrew_printed_text()
        self.assertGreater(len(long_text), 5000)
        serialized = json.dumps(
            {"text": long_text, "has_unclear": False, "unclear_count": 0},
            ensure_ascii=False,
        )
        self.assertEqual(json.loads(serialized)["text"], long_text)

        truncated = serialized[: len(serialized) // 2]
        with self.assertRaises(json.JSONDecodeError) as decode_ctx:
            json.loads(truncated, strict=False)
        self.assertIn("Unterminated string", str(decode_ctx.exception))
        with self.assertRaises(GeminiError):
            _parse_page_json_strict(truncated, page_index=1)

        # The same full transcription is accepted verbatim through the
        # plain-text execution path without any JSON parsing.
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text=long_text)
        mock_create_client.return_value = mock_client

        result = transcribe_pages_with_gemini(
            _pages(),
            "he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )

        self.assertEqual(result.text, long_text)
        mock_parse_json.assert_not_called()
        self.assertEqual(mock_client.models.generate_content.call_count, 1)
        mock_create_client.assert_called_once_with("test-key", api_version="v1beta")
        prompt_text = mock_client.models.generate_content.call_args.kwargs["contents"][
            0
        ].text
        self.assertIn("Output only the transcription text", prompt_text)
        self.assertNotIn("OUTPUT FORMAT", prompt_text)

    def test_uncertainty_markers_produce_review_metadata(self):
        # needs_review derives from has_unclear via the shared plain-text
        # page path already exercised by the existing plain-text routes.
        text = (
            "שורה ראשונה עם קריאה לא ודאית ירושלים[?] בהמשך.\n"
            "שורה שנייה עם קטע בלתי קריא [UNCLEAR] ועוד מילה[?] בסוף."
        )

        data = _plain_text_response_to_page_data(text, page_index=1)

        self.assertEqual(data["text"], text)
        self.assertTrue(data["has_unclear"])
        self.assertEqual(data["unclear_count"], 3)

    @patch("documents.services.gemini_engine._create_client")
    @patch("documents.services.gemini_engine._get_api_key", return_value="test-key")
    def test_empty_hebrew_output_fails_with_typed_empty_response(
        self,
        _mock_get_key,
        mock_create_client,
    ):
        mock_client = Mock()
        mock_client.models.generate_content.return_value = _response(text="   \n ")
        mock_create_client.return_value = mock_client

        with self.assertRaises(GeminiResponseError) as ctx:
            transcribe_pages_with_gemini(
                _pages(),
                "he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                model_name="test-model",
            )

        self.assertEqual(
            ctx.exception.failure_code,
            GeminiResponseFailureCode.EMPTY_RESPONSE,
        )
        # Empty output stays retryable twice, then fails typed — PR D policy.
        self.assertEqual(mock_client.models.generate_content.call_count, 3)


class HebrewPrintedCheckpointIdentityTests(SimpleTestCase):
    def _identity(self, contract):
        return build_gemini_attempt_identity(
            pages=_pages(),
            language_hint="he",
            text_input_type=Document.TextInputType.PRINTED,
            handwriting_type=None,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
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

    def test_old_hebrew_json_checkpoints_cannot_be_reused_under_plain_text(self):
        new_contract = _contract(DocumentTextResult.OcrPromptVariant.PRINTED, "he")
        old_effective_prompt = _PRINTED_TEXT_PROMPT + "\nLanguage hint: he."
        old_contract = replace(
            new_contract,
            prompt_fingerprint=hashlib.sha256(
                old_effective_prompt.encode("utf-8")
            ).hexdigest(),
            prompt_contract_version=GEMINI_OCR_PROMPT_CONTRACT_VERSION,
            output_mode="json",
            api_version="v1",
            effective_temperature=0.2,
        )

        new_identity = self._identity(new_contract)
        old_identity = self._identity(old_contract)

        self.assertNotEqual(
            new_identity.identity_fingerprint,
            old_identity.identity_fingerprint,
        )
        self.assertNotEqual(
            new_identity.prompt_fingerprint,
            old_identity.prompt_fingerprint,
        )
