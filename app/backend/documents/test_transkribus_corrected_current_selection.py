"""Unit tests for pure corrected/current Transkribus transcript selection (v1)."""

from __future__ import annotations

import ast
from pathlib import Path

from django.test import SimpleTestCase

from documents.services.transkribus_corrected_current_selection import (
    CorrectedCurrentPageInput,
    CorrectedCurrentPageSelectionError,
    CorrectedCurrentSelectionErrorCode,
    CorrectedCurrentSelectionResult,
    CorrectedCurrentTranscriptSelection,
    select_corrected_current_transcript_for_page,
    select_corrected_current_transcripts_for_document,
)


class CorrectedCurrentSelectionSinglePageTests(SimpleTestCase):
    def test_exactly_one_transcript_selects_ts_id(self):
        outcome = select_corrected_current_transcript_for_page(
            [{"tsId": "42", "status": "DONE", "jobId": "j1"}],
            page_index=1,
            page_nr=1,
        )
        self.assertIsInstance(outcome, CorrectedCurrentTranscriptSelection)
        self.assertEqual(outcome.transcript_ts_id, "42")
        self.assertEqual(outcome.page_index, 1)
        self.assertEqual(outcome.page_nr, 1)
        self.assertEqual(outcome.remote_transcript_status, "DONE")
        self.assertIsNone(outcome.in_progress_warning)

    def test_selection_ignores_job_model_and_extra_metadata(self):
        outcome = select_corrected_current_transcript_for_page(
            [
                {
                    "tsId": "99",
                    "jobId": "other-job",
                    "modelId": "other-model",
                    "isLatest": True,
                    "timestamp": 9999999999,
                }
            ],
            page_index=2,
            page_nr=3,
        )
        self.assertIsInstance(outcome, CorrectedCurrentTranscriptSelection)
        self.assertEqual(outcome.transcript_ts_id, "99")

    def test_in_progress_exposes_warning_not_error(self):
        outcome = select_corrected_current_transcript_for_page(
            [{"tsId": "7", "status": "IN_PROGRESS"}],
            page_index=1,
            page_nr=1,
        )
        self.assertIsInstance(outcome, CorrectedCurrentTranscriptSelection)
        self.assertEqual(outcome.remote_transcript_status, "IN_PROGRESS")
        self.assertIsNotNone(outcome.in_progress_warning)
        self.assertIn("IN_PROGRESS", outcome.in_progress_warning or "")

    def test_in_progress_normalizes_spacing_and_case(self):
        outcome = select_corrected_current_transcript_for_page(
            [{"tsId": "7", "status": "in progress"}],
            page_index=1,
            page_nr=1,
        )
        self.assertIsInstance(outcome, CorrectedCurrentTranscriptSelection)
        self.assertIsNotNone(outcome.in_progress_warning)

    def test_zero_transcripts_refuses(self):
        outcome = select_corrected_current_transcript_for_page(
            [],
            page_index=1,
            page_nr=1,
        )
        self.assertIsInstance(outcome, CorrectedCurrentPageSelectionError)
        self.assertEqual(
            outcome.code, CorrectedCurrentSelectionErrorCode.ZERO_TRANSCRIPTS
        )
        self.assertIn("found 0", outcome.message)

    def test_multiple_transcripts_refuses_with_count(self):
        outcome = select_corrected_current_transcript_for_page(
            [{"tsId": "1"}, {"tsId": "2"}],
            page_index=3,
            page_nr=3,
        )
        self.assertIsInstance(outcome, CorrectedCurrentPageSelectionError)
        self.assertEqual(
            outcome.code, CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS
        )
        self.assertIn("found 2", outcome.message)
        self.assertIn("page_index=3", outcome.message)

    def test_single_transcript_missing_ts_id_refuses(self):
        outcome = select_corrected_current_transcript_for_page(
            [{"status": "NEW"}],
            page_index=1,
            page_nr=1,
        )
        self.assertIsInstance(outcome, CorrectedCurrentPageSelectionError)
        self.assertEqual(outcome.code, CorrectedCurrentSelectionErrorCode.MISSING_TS_ID)


class CorrectedCurrentSelectionDocumentTests(SimpleTestCase):
    def test_all_pages_ok_returns_ordered_selections(self):
        result = select_corrected_current_transcripts_for_document(
            [
                CorrectedCurrentPageInput(
                    page_index=2,
                    page_nr=2,
                    raw_transcripts=({"tsId": "b"},),
                ),
                CorrectedCurrentPageInput(
                    page_index=1,
                    page_nr=1,
                    raw_transcripts=({"tsId": "a"},),
                ),
            ]
        )
        self.assertTrue(result.is_ok)
        self.assertIsNone(result.page_errors)
        self.assertIsInstance(result.selections, tuple)
        assert result.selections is not None
        self.assertEqual(
            [s.transcript_ts_id for s in result.selections],
            ["a", "b"],
        )

    def test_one_bad_page_refuses_whole_document(self):
        result = select_corrected_current_transcripts_for_document(
            [
                CorrectedCurrentPageInput(
                    page_index=1,
                    page_nr=1,
                    raw_transcripts=({"tsId": "ok"},),
                ),
                CorrectedCurrentPageInput(
                    page_index=2,
                    page_nr=2,
                    raw_transcripts=({"tsId": "1"}, {"tsId": "2"}),
                ),
            ]
        )
        self.assertTrue(result.is_refused)
        self.assertIsNone(result.selections)
        self.assertIsInstance(result.page_errors, tuple)
        assert result.page_errors is not None
        self.assertEqual(len(result.page_errors), 1)
        self.assertEqual(
            result.page_errors[0].code,
            CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS,
        )

    def test_multiple_failing_pages_collects_all_errors(self):
        result = select_corrected_current_transcripts_for_document(
            [
                CorrectedCurrentPageInput(
                    page_index=1,
                    page_nr=1,
                    raw_transcripts=(),
                ),
                CorrectedCurrentPageInput(
                    page_index=2,
                    page_nr=2,
                    raw_transcripts=({"tsId": "1"}, {"tsId": "2"}),
                ),
            ]
        )
        self.assertTrue(result.is_refused)
        self.assertIsInstance(result.page_errors, tuple)
        assert result.page_errors is not None
        codes = {e.code for e in result.page_errors}
        self.assertEqual(
            codes,
            {
                CorrectedCurrentSelectionErrorCode.ZERO_TRANSCRIPTS,
                CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS,
            },
        )

    def test_empty_document_pages_raises_value_error(self):
        with self.assertRaises(ValueError) as ctx:
            select_corrected_current_transcripts_for_document([])
        self.assertIn("empty pages", str(ctx.exception).lower())


class CorrectedCurrentSelectionResultInvariantTests(SimpleTestCase):
    def test_valid_ok_result(self):
        row = CorrectedCurrentTranscriptSelection(
            page_index=1,
            page_nr=1,
            transcript_ts_id="1",
            remote_transcript_status=None,
            in_progress_warning=None,
        )
        result = CorrectedCurrentSelectionResult(selections=(row,), page_errors=None)
        self.assertEqual(result.selections, (row,))

    def test_valid_refused_result(self):
        err = CorrectedCurrentPageSelectionError(
            page_index=1,
            page_nr=1,
            code=CorrectedCurrentSelectionErrorCode.ZERO_TRANSCRIPTS,
            message="msg",
        )
        result = CorrectedCurrentSelectionResult(selections=None, page_errors=(err,))
        self.assertEqual(result.page_errors, (err,))

    def test_both_none_raises(self):
        with self.assertRaises(ValueError):
            CorrectedCurrentSelectionResult(selections=None, page_errors=None)

    def test_both_set_raises(self):
        row = CorrectedCurrentTranscriptSelection(
            page_index=1,
            page_nr=1,
            transcript_ts_id="1",
            remote_transcript_status=None,
            in_progress_warning=None,
        )
        err = CorrectedCurrentPageSelectionError(
            page_index=2,
            page_nr=2,
            code=CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS,
            message="msg",
        )
        with self.assertRaises(ValueError):
            CorrectedCurrentSelectionResult(
                selections=(row,),
                page_errors=(err,),
            )


class CorrectedCurrentSelectionIsolationTests(SimpleTestCase):
    def test_module_ast_has_no_engine_or_pick_transcript_imports(self):
        path = (
            Path(__file__).resolve().parent
            / "services"
            / "transkribus_corrected_current_selection.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(
                        "transkribus_engine",
                        alias.name,
                        msg=f"unexpected import {alias.name!r}",
                    )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                self.assertNotIn(
                    "transkribus_engine",
                    module,
                    msg=f"unexpected import from {module!r}",
                )
                for alias in node.names:
                    self.assertNotEqual(
                        alias.name,
                        "pick_transcript",
                        msg="must not import pick_transcript",
                    )
