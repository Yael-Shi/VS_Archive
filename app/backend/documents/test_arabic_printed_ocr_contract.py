from __future__ import annotations

from django.test import SimpleTestCase

from documents.services.arabic_printed_ocr_contract import (
    ALLOWED_COMPLETED_STATUS,
    COMPLETION_MARKER,
    COVERAGE_RATIO_MAX,
    COVERAGE_RATIO_MIN,
    ArabicPrintedOcrFailureKind,
    classify_plain_text_output,
    evaluate_arabic_printed_band_output,
    join_arabic_printed_band_texts,
    non_whitespace_count,
    split_trailing_completion_marker,
)


def _marked(text: str) -> str:
    return f"{text}\n{COMPLETION_MARKER}"


class ArabicPrintedOcrContractTests(SimpleTestCase):
    def test_constants(self):
        self.assertEqual(COMPLETION_MARKER, "[VS_ARCHIVE_TRANSCRIPTION_COMPLETE]")
        self.assertEqual(COVERAGE_RATIO_MIN, 0.65)
        self.assertEqual(COVERAGE_RATIO_MAX, 1.60)
        self.assertEqual(ALLOWED_COMPLETED_STATUS, "completed")

    def test_accepts_completed_plain_text_and_strips_only_marker_and_outer_ws(self):
        raw = f"  الفقرة الأولى.\n\nما زال النص  \n{COMPLETION_MARKER}\n"
        result = evaluate_arabic_printed_band_output(
            raw, "الفقرة الأولى.\n\nما زال النص", status="completed"
        )
        self.assertTrue(result.accepted)
        self.assertIsNone(result.failure_kind)
        self.assertTrue(result.marker_seen)
        self.assertEqual(result.transcription, "الفقرة الأولى.\n\nما زال النص")
        self.assertIn("\n\n", result.transcription)
        self.assertNotIn(COMPLETION_MARKER, result.transcription)

    def test_preserves_internal_newlines_and_arabic_tatweel(self):
        messy = "كه\u0640ف  مزدوج"
        result = evaluate_arabic_printed_band_output(
            _marked(f"  {messy}\nسطر"),
            messy,
            status="completed",
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.transcription, f"{messy}\nسطر")

    def test_join_single_newline_preserves_internal_blanks(self):
        left = "الفقرة الأولى.\n\nما زال النص"
        right = "الفقرة التالية"
        self.assertEqual(
            join_arabic_printed_band_texts([f"  {left}  ", f"\n{right}\n"]),
            f"{left}\n{right}",
        )
        self.assertEqual(join_arabic_printed_band_texts(["AAA", "BBB"]), "AAA\nBBB")
        self.assertNotIn("\n\n", join_arabic_printed_band_texts(["AAA", "BBB"]))
        joined = join_arabic_printed_band_texts([left, right])
        self.assertEqual(joined.count("\n\n"), 1)
        self.assertEqual(joined, f"{left}\n{right}")
        messy = "كه\u0640ف  مزدوج"
        self.assertEqual(
            join_arabic_printed_band_texts([messy, "تالي"]),
            f"{messy}\nتالي",
        )

    def test_missing_marker_is_incomplete_output(self):
        result = evaluate_arabic_printed_band_output(
            "مرحبا", "مرحبا", status="completed"
        )
        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_kind, ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT
        )
        self.assertFalse(result.marker_seen)
        self.assertEqual(result.transcription, "")

    def test_marker_must_be_exact_final_non_empty_line(self):
        padded = f"مرحبا\n  {COMPLETION_MARKER}  "
        text, seen = split_trailing_completion_marker(padded)
        self.assertFalse(seen)
        self.assertEqual(text, padded)
        result = evaluate_arabic_printed_band_output(
            padded, "مرحبا", status="completed"
        )
        self.assertEqual(
            result.failure_kind, ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT
        )

    def test_empty_output_after_marker(self):
        result = evaluate_arabic_printed_band_output(
            f"\n{COMPLETION_MARKER}", "مرحبا", status="completed"
        )
        self.assertEqual(
            result.failure_kind, ArabicPrintedOcrFailureKind.EMPTY_OUTPUT
        )
        self.assertTrue(result.marker_seen)
        self.assertEqual(result.transcription, "")
        self.assertEqual(classify_plain_text_output("   "), "empty_output")

    def test_greeting_or_unrelated(self):
        greeting = "Hello! How can I help you today?"
        result = evaluate_arabic_printed_band_output(
            _marked(greeting), "مرحبا", status="completed"
        )
        self.assertEqual(
            result.failure_kind,
            ArabicPrintedOcrFailureKind.GREETING_OR_UNRELATED,
        )
        self.assertEqual(result.transcription, "")

    def test_tool_and_code_execution_steps_rejected(self):
        for step_type in (
            "function_call",
            "function_result",
            "code_execution_call",
            "code_execution_result",
            "tool_call",
            "tool_result",
        ):
            result = evaluate_arabic_printed_band_output(
                _marked("مرحبا"),
                "مرحبا",
                status="completed",
                step_types=(step_type,),
            )
            self.assertEqual(
                result.failure_kind,
                ArabicPrintedOcrFailureKind.UNEXPECTED_TOOL_USE,
            )
            self.assertEqual(result.transcription, "")

    def test_status_must_be_completed(self):
        raw = _marked("مرحبا")
        for status in (None, "in_progress", "failed", "cancelled", ""):
            result = evaluate_arabic_printed_band_output(
                raw, "مرحبا", status=status
            )
            self.assertEqual(
                result.failure_kind, ArabicPrintedOcrFailureKind.OTHER_STATUS
            )
        incomplete = evaluate_arabic_printed_band_output(
            raw, "مرحبا", status="incomplete"
        )
        self.assertEqual(
            incomplete.failure_kind,
            ArabicPrintedOcrFailureKind.INCOMPLETE_STATUS,
        )

    def test_terminal_ellipsis_ascii_and_unicode(self):
        for suffix in ("...", "…"):
            result = evaluate_arabic_printed_band_output(
                _marked(f"BANDONE{suffix}"),
                "BANDONE",
                status="completed",
            )
            self.assertEqual(
                result.failure_kind,
                ArabicPrintedOcrFailureKind.TERMINAL_ELLIPSIS,
            )
            self.assertEqual(result.transcription, "")

    def test_internal_ellipsis_is_not_terminal(self):
        text = "BAND...ONE"
        result = evaluate_arabic_printed_band_output(
            _marked(text), text, status="completed"
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.transcription, text)

    def test_coverage_ratio_rejection_and_boundaries(self):
        draft = "a" * 20
        too_short = evaluate_arabic_printed_band_output(
            _marked("a" * 12), draft, status="completed"
        )
        self.assertEqual(
            too_short.failure_kind, ArabicPrintedOcrFailureKind.COVERAGE_RATIO
        )
        self.assertEqual(too_short.coverage_ratio, 12 / 20)
        self.assertEqual(too_short.transcription, "")

        too_long = evaluate_arabic_printed_band_output(
            _marked("a" * 33), draft, status="completed"
        )
        self.assertEqual(
            too_long.failure_kind, ArabicPrintedOcrFailureKind.COVERAGE_RATIO
        )
        self.assertEqual(too_long.coverage_ratio, 33 / 20)

        at_min = evaluate_arabic_printed_band_output(
            _marked("a" * 13), draft, status="completed"
        )
        self.assertTrue(at_min.accepted)
        self.assertEqual(at_min.coverage_ratio, 0.65)
        self.assertEqual(at_min.coverage_ratio, COVERAGE_RATIO_MIN)

        at_max = evaluate_arabic_printed_band_output(
            _marked("a" * 32), draft, status="completed"
        )
        self.assertTrue(at_max.accepted)
        self.assertEqual(at_max.coverage_ratio, 1.60)
        self.assertEqual(at_max.coverage_ratio, COVERAGE_RATIO_MAX)

        empty_draft = evaluate_arabic_printed_band_output(
            _marked("مرحبا"), "   ", status="completed"
        )
        self.assertEqual(
            empty_draft.failure_kind, ArabicPrintedOcrFailureKind.COVERAGE_RATIO
        )
        self.assertIsNone(empty_draft.coverage_ratio)

    def test_unclear_token_is_valid_within_coverage(self):
        draft = "BANDONE"
        output = "[UNCLEAR]"
        self.assertEqual(non_whitespace_count(draft), 7)
        self.assertEqual(non_whitespace_count(output), 9)
        result = evaluate_arabic_printed_band_output(
            _marked(output), draft, status="completed"
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.transcription, output)

    def test_non_string_raw_and_draft_fail_closed(self):
        for raw in (None, b"hello", 123, ["مرحبا"]):
            result = evaluate_arabic_printed_band_output(
                raw, "مرحبا", status="completed"
            )
            self.assertFalse(result.accepted)
            self.assertEqual(
                result.failure_kind, ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT
            )
            self.assertEqual(result.transcription, "")
            self.assertFalse(result.marker_seen)

        marked = _marked("مرحبا")
        for draft in (None, b"hello", 123, ["مرحبا"]):
            result = evaluate_arabic_printed_band_output(
                marked, draft, status="completed"
            )
            self.assertFalse(result.accepted)
            self.assertEqual(
                result.failure_kind, ArabicPrintedOcrFailureKind.COVERAGE_RATIO
            )
            self.assertEqual(result.transcription, "")
            self.assertTrue(result.marker_seen)
            self.assertIsNone(result.coverage_ratio)

    def test_json_shaped_plain_text_is_not_repaired(self):
        valid_json = '{"text": "مرحبا"}'
        self.assertEqual(
            classify_plain_text_output(valid_json),
            ArabicPrintedOcrFailureKind.INVALID_JSON,
        )
        truncated = evaluate_arabic_printed_band_output(
            _marked('{"text": "مرحبا"'),
            "مرحبا",
            status="completed",
        )
        self.assertEqual(
            truncated.failure_kind, ArabicPrintedOcrFailureKind.TRUNCATED_JSON
        )
        invalid = evaluate_arabic_printed_band_output(
            _marked(valid_json), "مرحبا", status="completed"
        )
        self.assertEqual(
            invalid.failure_kind, ArabicPrintedOcrFailureKind.INVALID_JSON
        )
        self.assertEqual(invalid.transcription, "")
