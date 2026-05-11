from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
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
from documents.services.htr_adapters.registry import get_htr_adapter
from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
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
    def test_route_provided_skips_select_ocr_route(self, mock_select_route, mock_get_adapter):
        route = OcrRouteConfig(engine_key="GEMINI", prompt_variant="handwritten")
        adapter = Mock()
        adapter.execute.return_value = HtrResult(text="x", engine_name="gemini-2.0-flash")
        mock_get_adapter.return_value = adapter

        transcribe_pages(
            pages=[],
            language_hint="en",
            text_input_type=Document.TextInputType.HANDWRITTEN,
            route=route,
        )

        mock_select_route.assert_not_called()
        mock_get_adapter.assert_called_once_with("GEMINI")
        adapter.execute.assert_called_once_with(
            pages=[],
            language_hint="en",
            prompt_variant="handwritten",
        )

    @patch("documents.services.htr_engine.get_htr_adapter")
    @patch("documents.services.htr_engine.select_ocr_route")
    def test_raises_on_unsupported_engine(self, mock_select_route, mock_get_adapter):
        mock_select_route.return_value = OcrRouteConfig(
            engine_key="NOT_A_REGISTERED_ENGINE",
            prompt_variant="handwritten",
        )
        mock_get_adapter.side_effect = UnsupportedEngineError("NOT_A_REGISTERED_ENGINE")

        with self.assertRaises(UnsupportedEngineError):
            transcribe_pages(
                pages=[],
                language_hint="en",
                text_input_type=Document.TextInputType.HANDWRITTEN,
            )

    def test_transkribus_route_requires_worker_env(self):
        route = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        with self.assertRaises(EnginePermanentError) as ctx:
            transcribe_pages(
                pages=[SimpleNamespace(page_index=1)],
                language_hint="en",
                text_input_type=Document.TextInputType.HANDWRITTEN,
                route=route,
            )
        self.assertIn("worker_env", str(ctx.exception).lower())


class HtrRegistryTests(SimpleTestCase):
    def test_get_htr_adapter_resolves_transkribus(self):
        adapter = get_htr_adapter("TRANSKRIBUS")
        self.assertIsInstance(adapter, TranskribusAdapter)
        self.assertEqual(get_htr_adapter(" transkribus ").engine_key, "TRANSKRIBUS")


class TranskribusAdapterTests(SimpleTestCase):
    def test_execute_requires_worker_env(self):
        adapter = TranskribusAdapter()
        with self.assertRaises(EnginePermanentError) as ctx:
            adapter.execute(
                pages=[SimpleNamespace(page_index=1)],
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            )
        self.assertIn("worker_env", str(ctx.exception).lower())

    def test_execute_fails_fast_when_existing_doc_gate_disabled(self):
        from documents.services.env_validation import WorkerEnvConfig

        adapter = TranskribusAdapter()
        cfg = WorkerEnvConfig(
            gemini_api_key="k",
            gemini_confidence_threshold=0.7,
            min_text_length=20,
            max_retries=3,
            retry_delay_seconds_1=30,
            retry_delay_seconds_2=300,
            report_window_start="00:00",
            report_send_time="08:00",
            free_tier_alert_pct=80,
            gemini_free_daily_request_limit=1500,
            gemini_free_daily_image_limit=1000,
            transkribus_free_monthly_credits=500,
            enable_hybrid_htr=False,
            enable_daily_report=False,
            smtp_host=None,
            smtp_port=None,
            smtp_username=None,
            smtp_password=None,
            default_from_email=None,
            transkribus_api_token=None,
            transkribus_username=None,
            transkribus_password=None,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
        )
        with self.assertRaises(EnginePermanentError) as ctx:
            adapter.execute(
                pages=[SimpleNamespace(page_index=1)],
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                worker_env=cfg,
            )
        msg = str(ctx.exception).lower()
        self.assertIn("existing-server-document", msg.replace("_", "-"))

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.transcribe_existing_server_document")
    def test_execute_success_maps_htr_result(self, mock_tr):
        from documents.services.env_validation import WorkerEnvConfig

        mock_tr.return_value = ("line one\nline two", [])
        adapter = TranskribusAdapter()
        cfg = WorkerEnvConfig(
            gemini_api_key="k",
            gemini_confidence_threshold=0.7,
            min_text_length=20,
            max_retries=3,
            retry_delay_seconds_1=30,
            retry_delay_seconds_2=300,
            report_window_start="00:00",
            report_send_time="08:00",
            free_tier_alert_pct=80,
            gemini_free_daily_request_limit=1500,
            gemini_free_daily_image_limit=1000,
            transkribus_free_monthly_credits=500,
            enable_hybrid_htr=False,
            enable_daily_report=False,
            smtp_host=None,
            smtp_port=None,
            smtp_username=None,
            smtp_password=None,
            default_from_email=None,
            transkribus_api_token="token",
            transkribus_username="u",
            transkribus_password="p",
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            transkribus_use_existing_server_document=True,
            transkribus_dev_existing_document_id="99",
            transkribus_collection_id="1",
            transkribus_model_id="42",
            transkribus_dev_existing_pages="1",
        )
        from documents.services.page_extraction import PageImage

        result = adapter.execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="en",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=cfg,
        )
        self.assertEqual(result.text, "line one\nline two")
        self.assertFalse(result.needs_review)
        self.assertEqual(result.engine_name, "transkribus-pylaia:42")
        mock_tr.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.transcribe_existing_server_document")
    def test_execute_maps_retryable_engine_error(self, mock_tr):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.transkribus_engine import TranskribusRetryableError

        mock_tr.side_effect = TranskribusRetryableError("slow")
        adapter = TranskribusAdapter()
        cfg = WorkerEnvConfig(
            gemini_api_key="k",
            gemini_confidence_threshold=0.7,
            min_text_length=20,
            max_retries=3,
            retry_delay_seconds_1=30,
            retry_delay_seconds_2=300,
            report_window_start="00:00",
            report_send_time="08:00",
            free_tier_alert_pct=80,
            gemini_free_daily_request_limit=1500,
            gemini_free_daily_image_limit=1000,
            transkribus_free_monthly_credits=500,
            enable_hybrid_htr=False,
            enable_daily_report=False,
            smtp_host=None,
            smtp_port=None,
            smtp_username=None,
            smtp_password=None,
            default_from_email=None,
            transkribus_api_token="t",
            transkribus_username="u",
            transkribus_password="p",
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            transkribus_use_existing_server_document=True,
            transkribus_dev_existing_document_id="1",
            transkribus_collection_id="2",
            transkribus_model_id="3",
            transkribus_dev_existing_pages="1",
        )
        from documents.services.page_extraction import PageImage

        with self.assertRaises(EngineRetryableError):
            adapter.execute(
                pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                worker_env=cfg,
            )


class TranskribusEngineUnitTests(SimpleTestCase):
    def test_parse_page_xml_extracts_unicode_lines(self):
        from documents.services.transkribus_engine import parse_page_xml_to_text

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
          <Page>
            <TextLine><TextEquiv><Unicode>alpha</Unicode></TextEquiv></TextLine>
            <TextLine><TextEquiv><Unicode>beta</Unicode></TextEquiv></TextLine>
            <TextLine><TextEquiv><Unicode></Unicode></TextEquiv></TextLine>
          </Page>
        </PcGts>"""
        text = parse_page_xml_to_text(xml)
        self.assertEqual(text, "alpha\nbeta")

    def test_parse_page_xml_skips_missing_unicode_in_namespace(self):
        from documents.services.transkribus_engine import parse_page_xml_to_text

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
          <Page>
            <TextLine><TextEquiv xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15"></TextEquiv></TextLine>
            <TextLine><TextEquiv><Unicode>only</Unicode></TextEquiv></TextLine>
          </Page>
        </PcGts>"""
        text = parse_page_xml_to_text(xml)
        self.assertEqual(text, "only")

    def test_pick_transcript_prefers_job_and_model(self):
        from documents.services.transkribus_engine import pick_transcript

        transcripts = [
            {"tsId": "1", "jobId": "9", "modelId": "8", "url": "http://wrong"},
            {"tsId": "2", "jobId": "10", "modelId": "20", "url": "http://ok"},
        ]
        chosen = pick_transcript(transcripts, job_id="10", model_id="20")
        self.assertEqual(chosen["url"], "http://ok")

    def test_pick_transcript_newest_by_timestamp_then_ts_id(self):
        from documents.services.transkribus_engine import pick_transcript

        transcripts = [
            {
                "tsId": "1",
                "jobId": "10",
                "modelId": "20",
                "url": "http://old",
                "timestamp": 100,
            },
            {
                "tsId": "2",
                "jobId": "10",
                "modelId": "20",
                "url": "http://new",
                "timestamp": 200,
            },
        ]
        chosen = pick_transcript(transcripts, job_id="10", model_id="20")
        self.assertEqual(chosen["url"], "http://new")

        tie = [
            {
                "tsId": "5",
                "jobId": "10",
                "modelId": "20",
                "url": "http://a",
                "timestamp": 300,
            },
            {
                "tsId": "9",
                "jobId": "10",
                "modelId": "20",
                "url": "http://b",
                "timestamp": 300,
            },
        ]
        chosen_tie = pick_transcript(tie, job_id="10", model_id="20")
        self.assertEqual(chosen_tie["url"], "http://b")

    def test_get_job_maps_timeout_to_retryable(self):
        from documents.services.transkribus_engine import TranskribusRetryableError, get_job

        session = requests.Session()
        with patch.object(session, "request", side_effect=requests.Timeout):
            with self.assertRaises(TranskribusRetryableError) as ctx:
                get_job(session, "99")
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_get_job_maps_connection_error_to_retryable(self):
        from documents.services.transkribus_engine import TranskribusRetryableError, get_job

        session = requests.Session()
        with patch.object(
            session,
            "request",
            side_effect=requests.ConnectionError("refused"),
        ):
            with self.assertRaises(TranskribusRetryableError) as ctx:
                get_job(session, "99")
        self.assertIn("connection", str(ctx.exception).lower())

    def test_login_fails_when_no_session_cookie_or_session_id(self):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            login_trp_server,
        )

        session = requests.Session()
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"<user><id>1</id></user>"
        resp.encoding = "utf-8"

        with patch.object(session, "request", return_value=resp):
            with self.assertRaises(TranskribusPermanentError) as ctx:
                login_trp_server(session, username="u", password="p")
        self.assertIn("usable session", str(ctx.exception).lower())

    def test_login_accepts_session_id_in_xml_without_cookie(self):
        from documents.services.transkribus_engine import login_trp_server

        session = requests.Session()
        resp = requests.Response()
        resp.status_code = 200
        resp._content = b"<trp><sessionId>sess-token-value</sessionId></trp>"
        resp.encoding = "utf-8"

        with patch.object(session, "request", return_value=resp):
            login_trp_server(session, username="u", password="p")

        jsession_values = [
            c.value for c in session.cookies if c.name == "JSESSIONID"
        ]
        self.assertEqual(jsession_values, ["sess-token-value"])

    def test_fetch_pages_metadata_parses_json_array(self):
        from documents.services.transkribus_engine import TrpPageMetadata

        item = {
            "pageNr": 5,
            "pageId": 50,
            "docId": 500,
            "url": "http://page",
            "tsList": {"transcripts": [{"jobId": "1", "modelId": "2", "url": "http://t"}]},
        }
        meta = TrpPageMetadata.from_item(item)
        self.assertEqual(meta.page_nr, 5)
        self.assertEqual(len(meta.transcripts), 1)


class GeminiAdapterTests(SimpleTestCase):
    @patch("documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini")
    def test_worker_env_applies_gemini_defaults(self, mock_gemini_transcribe):
        from documents.services.env_validation import WorkerEnvConfig

        mock_gemini_transcribe.return_value = GeminiResult(text="t", engine_name="gemini-2.0-flash")
        cfg = WorkerEnvConfig(
            gemini_api_key="k",
            gemini_confidence_threshold=0.7,
            min_text_length=42,
            max_retries=3,
            retry_delay_seconds_1=30,
            retry_delay_seconds_2=300,
            report_window_start="00:00",
            report_send_time="08:00",
            free_tier_alert_pct=80,
            gemini_free_daily_request_limit=1500,
            gemini_free_daily_image_limit=1000,
            transkribus_free_monthly_credits=500,
            enable_hybrid_htr=False,
            enable_daily_report=False,
            smtp_host=None,
            smtp_port=None,
            smtp_username=None,
            smtp_password=None,
            default_from_email=None,
            transkribus_api_token=None,
            transkribus_username=None,
            transkribus_password=None,
            gemini_temperature=0.11,
            gemini_top_k=41,
            gemini_top_p=0.91,
            gemini_max_output_tokens=2048,
            gemini_double_pass=True,
            gemini_consistency_min_ratio=0.88,
        )
        adapter = GeminiAdapter()
        adapter.execute(
            pages=[],
            language_hint="en",
            prompt_variant="printed",
            worker_env=cfg,
        )
        kwargs = mock_gemini_transcribe.call_args.kwargs
        self.assertEqual(kwargs["min_text_length"], 42)
        self.assertEqual(kwargs["temperature"], 0.11)
        self.assertEqual(kwargs["top_k"], 41)
        self.assertEqual(kwargs["top_p"], 0.91)
        self.assertTrue(kwargs["double_pass"])
        self.assertEqual(kwargs["consistency_min_ratio"], 0.88)
        self.assertEqual(kwargs["max_output_tokens"], 2048)

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
        self.assertEqual(result.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            result.prompt_variant, DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        )
        self.assertIsNone(result.error_code)
        mock_transcribe.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        self.assertIn("route", call_kw)
        self.assertEqual(call_kw["route"].engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            call_kw["route"].prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        self.assertIn("worker_env", call_kw)
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
        mock_transcribe.side_effect = UnsupportedEngineError("NOT_A_REGISTERED_ENGINE")

        self.assertTrue(self.command._process_message(self._message()))

        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="unsupported:NOT_A_REGISTERED_ENGINE",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(failure.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            failure.prompt_variant, DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        )
        self.assertEqual(failure.error_code, "OCR_FAILED")

    @patch("documents.management.commands.run_worker.select_ocr_route")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    def test_transkribus_skeleton_failure_persists_route_metadata(
        self,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_select_route,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        self.assertTrue(self.command._process_message(self._message()))

        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(failure.engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS)
        self.assertEqual(
            failure.prompt_variant, DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        )
        self.assertEqual(failure.error_code, "OCR_FAILED")
        self.assertIn(
            "existing-server-document",
            (failure.error_details or "").lower().replace("_", "-"),
        )
        mock_select_route.assert_called()

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
        self.assertEqual(failure.error_code, "OCR_FAILED")
        self.assertEqual(failure.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            failure.prompt_variant, DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        )
        self.assertIn("Invalid or missing language for OCR routing", failure.error_details)

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_routing_invalid_persists_before_transcribe_and_skips_transcribe(
        self,
        mock_get_object_bytes,
        mock_extract_pages,
        mock_transcribe,
    ):
        doc = Document.objects.create(
            title="BadLang",
            doc_type=Document.DocType.PDF,
            language=None,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="doc.pdf",
            mime_type="application/pdf",
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        cmd = Command()
        cmd._cfg = SimpleNamespace(
            min_text_length=5,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.85,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
        )
        msg = {"Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": doc.id})}
        self.assertTrue(cmd._process_message(msg))
        mock_transcribe.assert_not_called()
        failure = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failure.error_code, "OCR_ROUTING_INVALID")
        self.assertEqual(failure.engine_key, "UNRESOLVED")
        self.assertEqual(failure.prompt_variant, "UNRESOLVED")
