"""Bounded Hebrew printed mixed-script region fallback."""

from __future__ import annotations

import io
import logging
import uuid
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from PIL import Image

from documents.models import Document, DocumentTextResult, GeminiOcrPageCheckpoint
from documents.services.gemini_engine import GeminiResponseFailureCode, GeminiResult
from documents.services.gemini_hebrew_printed_crop_recovery import (
    HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY,
    REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY,
    hebrew_printed_recitation_crop_recovery_policy,
)
from documents.services.gemini_hebrew_printed_mixed_script import (
    HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK_POLICY,
    MixedScriptPlanDecision,
    MixedScriptRegionBox,
    MixedScriptRegionProvenance,
    REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
    ScriptDominance,
    count_script_letters,
    evaluate_script_dominance,
    hebrew_printed_mixed_script_region_fallback_policy,
    hebrew_text_is_reusable_for_mixed_script,
    mixed_script_assembly_engine_name,
    plan_structural_candidate_region,
    script_dominance,
)
from documents.services.gemini_models import (
    GEMINI_36_FLASH_MODEL,
    LATIN_PRINTED_GEMINI_MODEL,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.page_extraction import PageImage
from documents.test_gemini_hebrew_printed_recitation_crop_recovery import (
    _identity,
    _png_page,
    _require_crop_plan,
)
from documents.test_gemini_page_checkpoints import _document
from documents.test_gemini_recitation_model_fallback import (
    _ExpectedIncomplete,
    _response_error,
)


HEBREW_SUBSTANTIAL = ("אבגדהוזחטיכלמנסעפצקרשת" * 2) + "סוף"
LATIN_SUBSTANTIAL = (
    "Collected bibliographic references including authors titles and dates "
    "from printed catalogues"
)
LATIN_INCIDENTAL = "https://example.com/item?id=12"


def _png_bands(
    *,
    width: int = 80,
    height: int = 400,
    bands: tuple[tuple[int, int], ...],
) -> bytes:
    image = Image.new("RGB", (width, height), (240, 240, 240))
    for top, bottom in bands:
        image.paste((20, 20, 20), (0, top, width, bottom))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _band_page(
    *,
    bands: tuple[tuple[int, int], ...],
    width: int = 80,
    height: int = 400,
    page_index: int = 1,
) -> PageImage:
    return PageImage(
        page_index=page_index,
        image_bytes=_png_bands(width=width, height=height, bands=bands),
        mime_type="image/png",
        source_identity="page.png",
        source_content_fingerprint="c" * 64,
    )


def _two_region_page() -> PageImage:
    return _band_page(bands=((0, 180), (250, 400)))


def _lower_one_span_page() -> PageImage:
    return _band_page(bands=((250, 400),))


def _three_region_crop_page() -> PageImage:
    return _band_page(bands=((0, 80), (140, 200), (230, 290), (330, 400)))


def _alpha_letter_count(text: str) -> int:
    return sum(1 for char in text if char.isalpha())


def _hebrew_letter_count(text: str) -> int:
    return sum(1 for char in text if char.isalpha() and 0x0590 <= ord(char) <= 0x05FF)


def _latin_letter_count(text: str) -> int:
    return sum(
        1
        for char in text
        if char.isalpha()
        and not (0x0590 <= ord(char) <= 0x05FF)
        and (
            0x0041 <= ord(char) <= 0x005A
            or 0x0061 <= ord(char) <= 0x007A
            or 0x00C0 <= ord(char) <= 0x024F
            or 0x1E00 <= ord(char) <= 0x1EFF
        )
    )


class MixedScriptDetectionTests(SimpleTestCase):
    def test_hebrew_only_text_is_hebrew_dominant(self):
        self.assertEqual(script_dominance(HEBREW_SUBSTANTIAL), ScriptDominance.HEBREW)
        self.assertTrue(hebrew_text_is_reusable_for_mixed_script(HEBREW_SUBSTANTIAL))

    def test_incidental_latin_url_does_not_split_hebrew_text(self):
        text = f"{HEBREW_SUBSTANTIAL} {LATIN_INCIDENTAL}"
        self.assertEqual(script_dominance(text), ScriptDominance.HEBREW)
        self.assertTrue(hebrew_text_is_reusable_for_mixed_script(text))

    def test_substantial_hebrew_plus_long_url_is_hebrew_dominant(self):
        text = (
            f"{HEBREW_SUBSTANTIAL} "
            "https://archive.example.org/records/catalog/item/"
            "abcdefghijklmnopqrstuvwxyz-0123456789"
        )
        self.assertEqual(script_dominance(text), ScriptDominance.HEBREW)
        self.assertEqual(count_script_letters(text).latin, 0)

    def test_substantial_hebrew_plus_email_or_domain_is_hebrew_dominant(self):
        email_text = f"{HEBREW_SUBSTANTIAL} name.surname@archive.example.org"
        domain_text = f"{HEBREW_SUBSTANTIAL} archive.example.org"
        self.assertEqual(script_dominance(email_text), ScriptDominance.HEBREW)
        self.assertEqual(script_dominance(domain_text), ScriptDominance.HEBREW)
        self.assertEqual(count_script_letters(email_text).latin, 0)
        self.assertEqual(count_script_letters(domain_text).latin, 0)

    def test_ordinary_latin_names_are_counted_not_stripped(self):
        short_names = f"{HEBREW_SUBSTANTIAL} John Smith"
        counted = count_script_letters(short_names)
        self.assertEqual(counted.latin, len("JohnSmith"))
        self.assertEqual(script_dominance(short_names), ScriptDominance.HEBREW)

        many_names = f"{HEBREW_SUBSTANTIAL} Alexander Benjamin Christopher"
        name_letters = len("AlexanderBenjaminChristopher")
        counted_many = count_script_letters(many_names)
        self.assertEqual(counted_many.latin, name_letters)
        self.assertLess(
            counted_many.hebrew / counted_many.identified,
            0.80,
        )
        self.assertEqual(script_dominance(many_names), ScriptDominance.AMBIGUOUS)

    def test_substantial_latin_text_is_latin_dominant(self):
        self.assertEqual(script_dominance(LATIN_SUBSTANTIAL), ScriptDominance.LATIN)
        self.assertGreaterEqual(
            count_script_letters(LATIN_SUBSTANTIAL).latin,
            40,
        )

    def test_latin_dominant_output_with_some_hebrew_is_accepted(self):
        text = f"{LATIN_SUBSTANTIAL} שלום"
        counted = count_script_letters(text)
        self.assertGreaterEqual(counted.latin, 40)
        self.assertGreaterEqual(counted.latin / counted.identified, 0.80)
        self.assertGreater(counted.hebrew, 0)
        self.assertEqual(script_dominance(text), ScriptDominance.LATIN)

    def test_ambiguous_bilingual_block_is_fail_closed(self):
        text = f"{HEBREW_SUBSTANTIAL}\n{LATIN_SUBSTANTIAL}"
        self.assertEqual(script_dominance(text), ScriptDominance.AMBIGUOUS)

    def test_dominance_diagnostics_match_counted_hebrew_and_latin_letters(self):
        text = f"{LATIN_SUBSTANTIAL} שלום"
        counted = count_script_letters(text)
        evaluation = evaluate_script_dominance(text)
        expected_latin = _latin_letter_count(text)
        expected_hebrew = _hebrew_letter_count(text)
        self.assertEqual(evaluation.dominance, ScriptDominance.LATIN)
        self.assertEqual(script_dominance(text), evaluation.dominance)
        self.assertEqual(evaluation.latin_letters, expected_latin)
        self.assertEqual(evaluation.hebrew_letters, expected_hebrew)
        self.assertEqual(evaluation.latin_letters, counted.latin)
        self.assertEqual(evaluation.hebrew_letters, counted.hebrew)
        self.assertEqual(
            evaluation.identified_letters,
            expected_latin + expected_hebrew,
        )
        self.assertEqual(
            evaluation.latin_ratio,
            expected_latin / (expected_latin + expected_hebrew),
        )
        self.assertEqual(
            evaluation.hebrew_ratio,
            expected_hebrew / (expected_latin + expected_hebrew),
        )
        self.assertEqual(evaluation.technical_token_letters_excluded, 0)

    def test_technical_token_letters_are_reported_as_excluded(self):
        url_text = f"{HEBREW_SUBSTANTIAL} {LATIN_INCIDENTAL}"
        email_text = f"{HEBREW_SUBSTANTIAL} name.surname@archive.example.org"
        domain_text = f"{HEBREW_SUBSTANTIAL} archive.example.org"
        names_text = f"{HEBREW_SUBSTANTIAL} John Smith"

        url_eval = evaluate_script_dominance(url_text)
        email_eval = evaluate_script_dominance(email_text)
        domain_eval = evaluate_script_dominance(domain_text)
        names_eval = evaluate_script_dominance(names_text)

        self.assertEqual(url_eval.dominance, ScriptDominance.HEBREW)
        self.assertEqual(email_eval.dominance, ScriptDominance.HEBREW)
        self.assertEqual(domain_eval.dominance, ScriptDominance.HEBREW)
        self.assertEqual(names_eval.dominance, ScriptDominance.HEBREW)
        self.assertEqual(script_dominance(url_text), ScriptDominance.HEBREW)
        self.assertEqual(url_eval.latin_letters, 0)
        self.assertEqual(email_eval.latin_letters, 0)
        self.assertEqual(domain_eval.latin_letters, 0)
        self.assertEqual(names_eval.latin_letters, len("JohnSmith"))
        self.assertEqual(
            url_eval.technical_token_letters_excluded,
            _alpha_letter_count(LATIN_INCIDENTAL),
        )
        self.assertEqual(
            email_eval.technical_token_letters_excluded,
            _alpha_letter_count("name.surname@archive.example.org"),
        )
        self.assertEqual(
            domain_eval.technical_token_letters_excluded,
            _alpha_letter_count("archive.example.org"),
        )
        self.assertEqual(names_eval.technical_token_letters_excluded, 0)
        self.assertEqual(
            url_eval.hebrew_letters,
            _hebrew_letter_count(HEBREW_SUBSTANTIAL),
        )

    def test_ambiguous_diagnostics_keep_ambiguous_classification(self):
        text = f"{HEBREW_SUBSTANTIAL}\n{LATIN_SUBSTANTIAL}"
        evaluation = evaluate_script_dominance(text)
        identified = evaluation.latin_letters + evaluation.hebrew_letters
        self.assertEqual(evaluation.dominance, ScriptDominance.AMBIGUOUS)
        self.assertEqual(script_dominance(text), ScriptDominance.AMBIGUOUS)
        self.assertEqual(
            evaluation.latin_letters,
            _latin_letter_count(LATIN_SUBSTANTIAL),
        )
        self.assertEqual(
            evaluation.hebrew_letters,
            _hebrew_letter_count(HEBREW_SUBSTANTIAL),
        )
        self.assertEqual(evaluation.identified_letters, identified)
        self.assertGreaterEqual(evaluation.latin_letters, 40)
        self.assertGreaterEqual(evaluation.hebrew_letters, 40)
        self.assertLess(evaluation.latin_ratio, 0.80)
        self.assertLess(evaluation.hebrew_ratio, 0.80)
        self.assertEqual(evaluation.technical_token_letters_excluded, 0)

    def test_zero_ink_spans_are_not_a_structural_candidate(self):
        page = _png_page()
        crop_plan = _require_crop_plan(page)
        decision = plan_structural_candidate_region(crop_plan.crops[1])
        self.assertEqual(decision.decision, MixedScriptPlanDecision.NO_REGION)

    def test_one_ink_band_is_the_structural_candidate(self):
        page = _lower_one_span_page()
        crop_plan = _require_crop_plan(page)
        decision = plan_structural_candidate_region(crop_plan.crops[1])
        self.assertEqual(decision.decision, MixedScriptPlanDecision.CANDIDATE)
        assert decision.box is not None
        self.assertGreater(decision.box.bottom - decision.box.top, 32)

    def test_two_ink_bands_are_structurally_ambiguous(self):
        page = _two_region_page()
        crop_plan = _require_crop_plan(page)
        decision = plan_structural_candidate_region(crop_plan.crops[1])
        self.assertEqual(
            decision.decision,
            MixedScriptPlanDecision.STRUCTURAL_AMBIGUOUS,
        )

    def test_three_ink_bands_are_structurally_ambiguous(self):
        page = _three_region_crop_page()
        crop_plan = _require_crop_plan(page)
        decision = plan_structural_candidate_region(crop_plan.crops[1])
        self.assertEqual(
            decision.decision,
            MixedScriptPlanDecision.STRUCTURAL_AMBIGUOUS,
        )


class MixedScriptIdentityTests(SimpleTestCase):
    def test_policy_is_hebrew_printed_only(self):
        self.assertEqual(
            hebrew_printed_mixed_script_region_fallback_policy(
                language_hint=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.PRINTED,
            ),
            HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK_POLICY,
        )
        self.assertEqual(
            hebrew_printed_mixed_script_region_fallback_policy(
                language_hint=Document.Language.ENGLISH,
                text_input_type=Document.TextInputType.PRINTED,
            ),
            "",
        )
        self.assertEqual(
            hebrew_printed_recitation_crop_recovery_policy(
                language_hint=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.PRINTED,
            ),
            HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY,
        )

    def test_mixed_script_policy_changes_only_hebrew_printed_identity(self):
        page = _png_page()
        with_policy = _identity(page)
        with patch(
            "documents.services.gemini_page_checkpoints."
            "hebrew_printed_mixed_script_region_fallback_policy",
            return_value="",
        ):
            without_policy = _identity(page)
        self.assertNotEqual(
            with_policy.config_fingerprint,
            without_policy.config_fingerprint,
        )
        self.assertNotEqual(
            with_policy.identity_fingerprint,
            without_policy.identity_fingerprint,
        )

    def test_engine_name_is_region_marker(self):
        name = mixed_script_assembly_engine_name(
            [
                MixedScriptRegionProvenance(
                    order=1,
                    script="he",
                    model="gemini-3.1-flash-lite",
                    source="crop",
                ),
                MixedScriptRegionProvenance(
                    order=2,
                    script="latn",
                    model=LATIN_PRINTED_GEMINI_MODEL,
                    source="region",
                    region_box=MixedScriptRegionBox(
                        left=0, top=10, right=80, bottom=120
                    ),
                ),
            ]
        )
        self.assertRegex(name, r"^gemini-regions:[0-9a-f]{48}$")
        self.assertLessEqual(len(name), 64)

    def test_engine_name_is_stable_for_identical_provenance(self):
        regions = (
            MixedScriptRegionProvenance(
                order=1,
                script="he",
                model="gemini-3.1-flash-lite",
                source="crop",
            ),
            MixedScriptRegionProvenance(
                order=2,
                script="latn",
                model=LATIN_PRINTED_GEMINI_MODEL,
                source="region",
                region_box=MixedScriptRegionBox(left=0, top=10, right=80, bottom=120),
            ),
        )
        first = mixed_script_assembly_engine_name(regions)
        second = mixed_script_assembly_engine_name(regions)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^gemini-regions:[0-9a-f]{48}$")

    def test_engine_name_changes_when_latin_region_box_changes(self):
        hebrew = MixedScriptRegionProvenance(
            order=1,
            script="he",
            model="gemini-3.1-flash-lite",
            source="crop",
        )
        latin_a = MixedScriptRegionProvenance(
            order=2,
            script="latn",
            model=LATIN_PRINTED_GEMINI_MODEL,
            source="region",
            region_box=MixedScriptRegionBox(left=0, top=10, right=80, bottom=120),
        )
        latin_b = MixedScriptRegionProvenance(
            order=2,
            script="latn",
            model=LATIN_PRINTED_GEMINI_MODEL,
            source="region",
            region_box=MixedScriptRegionBox(left=0, top=20, right=80, bottom=140),
        )
        first = mixed_script_assembly_engine_name((hebrew, latin_a))
        second = mixed_script_assembly_engine_name((hebrew, latin_b))
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^gemini-regions:[0-9a-f]{48}$")
        self.assertRegex(second, r"^gemini-regions:[0-9a-f]{48}$")


class MixedScriptAdapterTests(SimpleTestCase):
    def _execute(self, adapter: GeminiAdapter, page: PageImage) -> None:
        adapter._execute_claimed_page(
            page=page,
            language_hint=Document.Language.HEBREW,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            recitation_model_fallback_enabled=True,
            hebrew_general_model_fallback_enabled=False,
            kwargs={"max_output_tokens": 4096},
            checkpoint_id=1,
            lease_token=uuid.uuid4(),
            attempt_id=2,
            hebrew_printed_crop_recovery_enabled=True,
            hebrew_printed_mixed_script_enabled=True,
        )

    def test_hebrew_only_full_page_success_skips_mixed_script(self):
        adapter = GeminiAdapter()
        page = _png_page()
        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                return_value=GeminiResult(
                    text=HEBREW_SUBSTANTIAL,
                    engine_name="gemini-3.1-flash-lite",
                ),
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page)

        self.assertEqual(mock_transcribe.call_count, 1)
        persist_kwargs = mock_persist_success.call_args.kwargs
        self.assertEqual(persist_kwargs["actual_model"], "gemini-3.1-flash-lite")
        self.assertNotIn(
            REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
            persist_kwargs["review_reasons"],
        )

    def test_incidental_latin_does_not_call_latin_printed_path(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, language_hint, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(
                    text=f"{HEBREW_SUBSTANTIAL} {LATIN_INCIDENTAL}",
                    engine_name=model_name,
                )
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertNotIn(
            LATIN_PRINTED_GEMINI_MODEL,
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
        )
        mock_persist_success.assert_not_called()
        mock_persist_failure.assert_called()

    def test_zero_span_failed_crop_does_not_probe_latin(self):
        adapter = GeminiAdapter()
        page = _png_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, language_hint, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure"),
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertNotIn(
            LATIN_PRINTED_GEMINI_MODEL,
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
        )
        mock_persist_success.assert_not_called()

    def test_substantial_regions_route_latin_in_reading_order(self):
        adapter = GeminiAdapter()
        page = _lower_one_span_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)
        candidate_plan = plan_structural_candidate_region(plan.crops[1])
        candidate_region = candidate_plan.region
        self.assertIsNotNone(candidate_region)
        assert candidate_region is not None

        def execute(*, pages, model_name, language_hint, **_kwargs):
            image_bytes = pages[0].image_bytes
            if image_bytes == page.image_bytes:
                raise recitation
            if image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            if image_bytes == plan.crops[1].image_bytes:
                raise recitation
            if image_bytes == candidate_region.image_bytes:
                self.assertEqual(language_hint, Document.Language.ENGLISH)
                self.assertEqual(model_name, LATIN_PRINTED_GEMINI_MODEL)
                return GeminiResult(
                    text=f"{LATIN_SUBSTANTIAL} שלום",
                    engine_name=LATIN_PRINTED_GEMINI_MODEL,
                )
            raise AssertionError("unexpected provider image")

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
        ):
            self._execute(adapter, page)

        latin_calls = [
            call
            for call in mock_transcribe.call_args_list
            if call.kwargs["model_name"] == LATIN_PRINTED_GEMINI_MODEL
        ]
        self.assertEqual(len(latin_calls), 1)
        persist_kwargs = mock_persist_success.call_args.kwargs
        self.assertIn(HEBREW_SUBSTANTIAL, persist_kwargs["text"])
        self.assertIn(LATIN_SUBSTANTIAL, persist_kwargs["text"])
        self.assertIn("שלום", persist_kwargs["text"])
        self.assertLess(
            persist_kwargs["text"].index(HEBREW_SUBSTANTIAL),
            persist_kwargs["text"].index(LATIN_SUBSTANTIAL),
        )
        self.assertTrue(persist_kwargs["needs_review"])
        self.assertEqual(
            persist_kwargs["review_reasons"],
            [
                REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY,
                REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
            ],
        )
        self.assertRegex(
            persist_kwargs["actual_model"],
            r"^gemini-regions:[0-9a-f]{48}$",
        )
        self.assertLessEqual(mock_transcribe.call_count, 8)

    def test_ambiguous_latin_ocr_does_not_persist_partial_text(self):
        adapter = GeminiAdapter()
        page = _lower_one_span_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)
        candidate_plan = plan_structural_candidate_region(plan.crops[1])
        candidate_region = candidate_plan.region
        self.assertIsNotNone(candidate_region)
        assert candidate_region is not None

        def execute(*, pages, model_name, language_hint, **_kwargs):
            image_bytes = pages[0].image_bytes
            if image_bytes == page.image_bytes:
                raise recitation
            if image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            if image_bytes == plan.crops[1].image_bytes:
                raise recitation
            if image_bytes == candidate_region.image_bytes:
                return GeminiResult(
                    text=f"{HEBREW_SUBSTANTIAL}\n{LATIN_SUBSTANTIAL}",
                    engine_name=LATIN_PRINTED_GEMINI_MODEL,
                )
            raise AssertionError("unexpected provider image")

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ),
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        mock_persist_success.assert_not_called()
        self.assertEqual(
            str(mock_persist_failure.call_args.kwargs["exc"]),
            "MIXED_SCRIPT_REGION_AMBIGUOUS",
        )

    def test_latin_ocr_dominance_logs_structured_diagnostics_when_ambiguous(self):
        adapter = GeminiAdapter()
        page = _lower_one_span_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)
        candidate_plan = plan_structural_candidate_region(plan.crops[1])
        candidate_region = candidate_plan.region
        self.assertIsNotNone(candidate_region)
        assert candidate_region is not None
        latin_text = f"{HEBREW_SUBSTANTIAL}\n{LATIN_SUBSTANTIAL}"
        expected = evaluate_script_dominance(latin_text)
        self.assertEqual(expected.dominance, ScriptDominance.AMBIGUOUS)

        def execute(*, pages, model_name, language_hint, **_kwargs):
            image_bytes = pages[0].image_bytes
            if image_bytes == page.image_bytes:
                raise recitation
            if image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            if image_bytes == plan.crops[1].image_bytes:
                raise recitation
            if image_bytes == candidate_region.image_bytes:
                return GeminiResult(
                    text=latin_text,
                    engine_name=LATIN_PRINTED_GEMINI_MODEL,
                )
            raise AssertionError("unexpected provider image")

        records: list[logging.LogRecord] = []

        class ExtraHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        logger = logging.getLogger("documents.services.htr_adapters.gemini_adapter")
        handler = ExtraHandler()
        logger.addHandler(handler)
        try:
            with (
                patch(
                    "documents.services.htr_adapters.gemini_adapter."
                    "transcribe_pages_with_gemini",
                    side_effect=execute,
                ),
                patch(
                    "documents.services.htr_adapters.gemini_adapter."
                    "persist_gemini_page_success"
                ) as mock_persist_success,
                patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
                patch.object(
                    adapter,
                    "_raise_incomplete",
                    side_effect=_ExpectedIncomplete,
                ),
                self.assertRaises(_ExpectedIncomplete),
            ):
                self._execute(adapter, page)
        finally:
            logger.removeHandler(handler)

        evaluated = [
            record
            for record in records
            if isinstance(record.msg, str)
            and record.msg.startswith(
                "Hebrew printed mixed-script Latin OCR dominance evaluated:"
            )
        ]
        self.assertEqual(len(evaluated), 1)
        record = evaluated[0]
        self.assertEqual(record.dominance, "ambiguous")
        self.assertEqual(record.latin_letters, expected.latin_letters)
        self.assertEqual(record.hebrew_letters, expected.hebrew_letters)
        self.assertEqual(record.identified_letters, expected.identified_letters)
        self.assertEqual(record.latin_ratio, expected.latin_ratio)
        self.assertEqual(record.hebrew_ratio, expected.hebrew_ratio)
        self.assertEqual(
            record.technical_token_letters_excluded,
            expected.technical_token_letters_excluded,
        )
        mock_persist_success.assert_not_called()
        self.assertEqual(
            str(mock_persist_failure.call_args.kwargs["exc"]),
            "MIXED_SCRIPT_REGION_AMBIGUOUS",
        )

    def test_latin_region_failure_does_not_persist_hebrew_crop(self):
        adapter = GeminiAdapter()
        page = _lower_one_span_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)
        candidate_plan = plan_structural_candidate_region(plan.crops[1])
        candidate_region = candidate_plan.region
        self.assertIsNotNone(candidate_region)
        assert candidate_region is not None

        def execute(*, pages, model_name, language_hint, **_kwargs):
            image_bytes = pages[0].image_bytes
            if image_bytes == page.image_bytes:
                raise recitation
            if image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            if image_bytes == plan.crops[1].image_bytes:
                raise recitation
            if image_bytes == candidate_region.image_bytes:
                raise recitation
            raise AssertionError("unexpected provider image")

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertEqual(
            [
                call.kwargs["model_name"]
                for call in mock_transcribe.call_args_list
                if call.kwargs["model_name"] == LATIN_PRINTED_GEMINI_MODEL
            ],
            [LATIN_PRINTED_GEMINI_MODEL],
        )
        mock_persist_success.assert_not_called()
        self.assertEqual(
            mock_persist_failure.call_args.kwargs["exc"].failure_code,
            GeminiResponseFailureCode.RECITATION,
        )

    def test_two_span_failed_crop_is_ambiguous_without_latin_probe(self):
        adapter = GeminiAdapter()
        page = _two_region_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, language_hint, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertNotIn(
            LATIN_PRINTED_GEMINI_MODEL,
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
        )
        mock_persist_success.assert_not_called()
        self.assertEqual(
            mock_persist_failure.call_args.kwargs["exc"].failure_code,
            GeminiResponseFailureCode.RECITATION,
        )
        self.assertNotEqual(
            str(mock_persist_failure.call_args.kwargs["exc"]),
            "MIXED_SCRIPT_REGION_AMBIGUOUS",
        )

    def test_ambiguous_segmentation_does_not_call_latin_path(self):
        adapter = GeminiAdapter()
        page = _three_region_crop_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)

        def execute(*, pages, model_name, language_hint, **_kwargs):
            if pages[0].image_bytes == page.image_bytes:
                raise recitation
            if pages[0].image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            raise recitation

        with (
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "transcribe_pages_with_gemini",
                side_effect=execute,
            ) as mock_transcribe,
            patch(
                "documents.services.htr_adapters.gemini_adapter."
                "persist_gemini_page_success"
            ) as mock_persist_success,
            patch.object(adapter, "_persist_page_failure") as mock_persist_failure,
            patch.object(
                adapter,
                "_raise_incomplete",
                side_effect=_ExpectedIncomplete,
            ),
            self.assertRaises(_ExpectedIncomplete),
        ):
            self._execute(adapter, page)

        self.assertNotIn(
            LATIN_PRINTED_GEMINI_MODEL,
            [call.kwargs["model_name"] for call in mock_transcribe.call_args_list],
        )
        mock_persist_success.assert_not_called()
        self.assertEqual(
            mock_persist_failure.call_args.kwargs["exc"].failure_code,
            GeminiResponseFailureCode.RECITATION,
        )
        self.assertNotEqual(
            str(mock_persist_failure.call_args.kwargs["exc"]),
            "MIXED_SCRIPT_REGION_AMBIGUOUS",
        )


class MixedScriptCheckpointTests(TestCase):
    def test_adapter_execute_persists_mixed_script_provenance(self):
        document = _document()
        document.language = Document.Language.HEBREW
        document.text_input_type = Document.TextInputType.PRINTED
        document.save(update_fields=["language", "text_input_type", "updated_at"])
        page = _lower_one_span_page()
        recitation = _response_error(
            GeminiResponseFailureCode.RECITATION,
            model="gemini-3.1-flash-lite",
            attempt=1,
            max_output_tokens=4096,
        )
        plan = _require_crop_plan(page)
        candidate_plan = plan_structural_candidate_region(plan.crops[1])
        candidate_region = candidate_plan.region
        self.assertIsNotNone(candidate_region)
        assert candidate_region is not None

        def execute(*, pages, model_name, language_hint, **_kwargs):
            image_bytes = pages[0].image_bytes
            if image_bytes == page.image_bytes:
                raise recitation
            if image_bytes == plan.crops[0].image_bytes:
                return GeminiResult(text=HEBREW_SUBSTANTIAL, engine_name=model_name)
            if image_bytes == plan.crops[1].image_bytes:
                raise recitation
            if image_bytes == candidate_region.image_bytes:
                return GeminiResult(
                    text=LATIN_SUBSTANTIAL,
                    engine_name=LATIN_PRINTED_GEMINI_MODEL,
                )
            raise AssertionError("unexpected provider image")

        with patch(
            "documents.services.htr_adapters.gemini_adapter."
            "transcribe_pages_with_gemini",
            side_effect=execute,
        ):
            result = GeminiAdapter().execute(
                pages=[page],
                language_hint=Document.Language.HEBREW,
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                document_id=document.id,
                text_input_type=Document.TextInputType.PRINTED,
                handwriting_type=Document.HandwritingType.VS,
                engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                model_candidates=["gemini-3.1-flash-lite", GEMINI_36_FLASH_MODEL],
            )

        self.assertIn(HEBREW_SUBSTANTIAL, result.text)
        self.assertIn(LATIN_SUBSTANTIAL, result.text)
        self.assertTrue(result.needs_review)
        self.assertEqual(
            result.review_reasons,
            [
                REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY,
                REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
            ],
        )
        self.assertRegex(result.engine_name, r"^gemini-regions:[0-9a-f]{48}$")
        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=document,
            page_index=1,
        )
        self.assertEqual(checkpoint.status, GeminiOcrPageCheckpoint.Status.SUCCEEDED)
        self.assertEqual(checkpoint.review_reasons, result.review_reasons)
        self.assertRegex(checkpoint.actual_model, r"^gemini-regions:[0-9a-f]{48}$")
