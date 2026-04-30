from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from documents.management.commands.run_worker import Command
from documents.models import Document, DocumentTextResult
from documents.services.gemini_engine import GeminiError, GeminiResult
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
    UnsupportedEngineError,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.htr_engine import transcribe_pages
from documents.services.ocr_routing import OcrRouteConfig


class HtrDispatcherTests(SimpleTestCase):
    @patch("documents.services.htr_engine.get_htr_adapter")
    @patch("documents.services.htr_engine.select_ocr_route")
    def test_dispatches_by_engine_key_and_prompt_variant(self, mock_select_route, mock_get_adapter):
        pages = [SimpleNamespace(page_index=1)]
        mock_select_route.return_value = OcrRouteConfig(
            engine_key="GEMINI",
            prompt_variant="printed",
        )
        adapter = Mock()
        adapter.execute.return_value = HtrResult(
            text="ok",
            engine_name="gemini-2.0-flash",
        )
        mock_get_adapter.return_value = adapter

        result = transcribe_pages(
            pages=pages,
            language_hint="en",
            text_input_type=Document.TextInputType.PRINTED,
            min_text_length=10,
        )

        self.assertEqual(result.text, "ok")
        mock_get_adapter.assert_called_once_with("GEMINI")
        adapter.execute.assert_called_once_with(
            pages=pages,
            language_hint="en",
            prompt_variant="printed",
            min_text_length=10,
        )

    @patch("documents.services.htr_engine.get_htr_adapter")
    @patch("documents.services.htr_engine.select_ocr_route")
    def test_raises_on_unsupported_engine(self, mock_select_route, mock_get_adapter):
        mock_select_route.return_value = OcrRouteConfig(
            engine_key="TRANSKRIBUS",
            prompt_variant="handwritten",
        )
        mock_get_adapter.side_effect = UnsupportedEngineError("TRANSKRIBUS")

        with self.assertRaises(UnsupportedEngineError):
            transcribe_pages(
                pages=[],
                language_hint="en",
                text_input_type=Document.TextInputType.HANDWRITTEN,
            )


class GeminiAdapterTests(SimpleTestCase):
    @patch("documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini")
    def test_success_uses_first_model(self, mock_gemini_transcribe):
        mock_gemini_transcribe.return_value = GeminiResult(
            text="text",
            engine_name="gemini-2.0-flash",
        )
        adapter = GeminiAdapter()

        result = adapter.execute(
            pages=[],
            language_hint="en",
            prompt_variant="printed",
            model_candidates=["gemini-2.0-flash", "gemini-1.5-flash"],
        )

        self.assertEqual(result.engine_name, "gemini-2.0-flash")
        self.assertEqual(mock_gemini_transcribe.call_count, 1)
        self.assertEqual(mock_gemini_transcribe.call_args.kwargs["model_name"], "gemini-2.0-flash")

    @patch("documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini")
    def test_quota_failure_falls_back_to_next_model(self, mock_gemini_transcribe):
        mock_gemini_transcribe.side_effect = [
            GeminiError("QUOTA_EXHAUSTED: gemini-2.0-flash"),
            GeminiResult(text="text", engine_name="gemini-1.5-flash"),
        ]
        adapter = GeminiAdapter()

        result = adapter.execute(
            pages=[],
            language_hint="en",
            prompt_variant="printed",
            model_candidates=["gemini-2.0-flash", "gemini-1.5-flash"],
        )

        self.assertEqual(result.engine_name, "gemini-1.5-flash")
        self.assertEqual(mock_gemini_transcribe.call_count, 2)

    @patch("documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini")
    def test_non_quota_gemini_error_is_permanent(self, mock_gemini_transcribe):
        mock_gemini_transcribe.side_effect = GeminiError("bad request")
        adapter = GeminiAdapter()

        with self.assertRaises(EnginePermanentError):
            adapter.execute(
                pages=[],
                language_hint="en",
                prompt_variant="printed",
            )

    @patch("documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini")
    def test_all_quota_failures_raise_retryable_error(self, mock_gemini_transcribe):
        mock_gemini_transcribe.side_effect = GeminiError("QUOTA_EXHAUSTED")
        adapter = GeminiAdapter()

        with self.assertRaises(EngineRetryableError):
            adapter.execute(
                pages=[],
                language_hint="en",
                prompt_variant="printed",
                model_candidates=["gemini-2.0-flash", "gemini-1.5-flash"],
            )


class RunWorkerBehaviorTests(TestCase):
    def setUp(self):
        self.command = Command()
        self.command._cfg = SimpleNamespace(
            min_text_length=5,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.85,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
        )
        self.doc = Document.objects.create(
            title="Doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="doc.pdf",
            mime_type="application/pdf",
        )

    def _message(self) -> dict:
        return {
            "Body": json.dumps(
                {"type": "PROCESS_DOCUMENT", "document_id": self.doc.id}
            )
        }

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_success_persistence_semantics_remain_unchanged(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        self.assertTrue(self.command._process_message(self._message()))

        result = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
        )
        self.assertEqual(result.status, DocumentTextResult.Status.SUCCEEDED)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_unsupported_engine_is_persisted_explicitly(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.side_effect = UnsupportedEngineError("TRANSKRIBUS")

        self.assertTrue(self.command._process_message(self._message()))

        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="unsupported:TRANSKRIBUS",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_routing_failure_persists_failed_result_with_dispatch_engine(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.side_effect = ValueError("Invalid or missing language for OCR routing")

        self.assertTrue(self.command._process_message(self._message()))

        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertIn("Invalid or missing language for OCR routing", failure.error_details)
