from __future__ import annotations

import inspect
import json
import os
import tempfile
from datetime import timedelta
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from PIL import Image

from documents.management.commands.run_worker import Command
from documents.models import Document, DocumentSourceFile, DocumentTextResult, TranskribusRun
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import validate_required_env
from documents.services.transkribus_engine import PylaiaTranscriptionOutcome
from documents.services.gemini_engine import (
    GeminiError,
    GeminiResult,
    _HTR_EXPERT_PROMPT,
    _PRINTED_TEXT_PROMPT,
)
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
from documents.services.ocr_routing import (
    DEFAULT_GEMINI_MODEL_CANDIDATES,
    OcrRouteConfig,
    OCR_ROUTES,
    gemini_model_candidates,
    select_ocr_route,
)


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
    def test_hebrew_printed_with_worker_env_passes_route_model_candidates(
        self, mock_select_route, mock_get_adapter
    ):
        from documents.services.env_validation import WorkerEnvConfig

        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        adapter = Mock()
        adapter.execute.return_value = HtrResult(
            text="ok",
            engine_name="gemini-3.1-flash-lite",
        )
        mock_get_adapter.return_value = adapter
        worker_env = WorkerEnvConfig(
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
            gemini_hebrew_printed_model="gemini-3.1-flash-lite",
        )

        transcribe_pages(
            pages=[],
            language_hint="he",
            text_input_type=Document.TextInputType.PRINTED,
            worker_env=worker_env,
        )

        adapter.execute.assert_called_once_with(
            pages=[],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=worker_env,
            model_candidates=["gemini-3.1-flash-lite"],
        )

    @patch("documents.services.htr_engine.get_htr_adapter")
    @patch("documents.services.htr_engine.select_ocr_route")
    def test_explicit_model_candidates_not_overwritten_for_hebrew_printed(
        self, mock_select_route, mock_get_adapter
    ):
        from documents.services.env_validation import WorkerEnvConfig

        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        adapter = Mock()
        adapter.execute.return_value = HtrResult(
            text="ok",
            engine_name="caller-chosen-model",
        )
        mock_get_adapter.return_value = adapter
        worker_env = WorkerEnvConfig(
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
            gemini_hebrew_printed_model="gemini-3.1-flash-lite",
        )
        explicit_candidates = ["caller-chosen-model"]

        transcribe_pages(
            pages=[],
            language_hint="he",
            text_input_type=Document.TextInputType.PRINTED,
            worker_env=worker_env,
            model_candidates=explicit_candidates,
        )

        adapter.execute.assert_called_once_with(
            pages=[],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=worker_env,
            model_candidates=explicit_candidates,
        )

    @patch("documents.services.htr_engine.get_htr_adapter")
    @patch("documents.services.htr_engine.select_ocr_route")
    def test_english_printed_with_worker_env_keeps_default_model_candidates(
        self, mock_select_route, mock_get_adapter
    ):
        from documents.services.env_validation import WorkerEnvConfig

        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        adapter = Mock()
        adapter.execute.return_value = HtrResult(text="ok", engine_name="gemini-2.0-flash")
        mock_get_adapter.return_value = adapter
        worker_env = WorkerEnvConfig(
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

        transcribe_pages(
            pages=[],
            language_hint="en",
            text_input_type=Document.TextInputType.PRINTED,
            worker_env=worker_env,
        )

        adapter.execute.assert_called_once_with(
            pages=[],
            language_hint="en",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=worker_env,
            model_candidates=list(DEFAULT_GEMINI_MODEL_CANDIDATES),
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


class TranskribusAdapterTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Transkribus adapter test doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_execute_requires_worker_env(self):
        adapter = TranskribusAdapter()
        with self.assertRaises(EnginePermanentError) as ctx:
            adapter.execute(
                pages=[SimpleNamespace(page_index=1)],
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            )
        self.assertIn("worker_env", str(ctx.exception).lower())

    def test_execute_fails_fast_when_no_dev_mode_enabled(self):
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
        msg = str(ctx.exception)
        self.assertIn("TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT", msg)
        self.assertIn("TRANSKRIBUS_DEV_UPLOAD_MODE", msg)

    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.upload_then_transcribe_page_images_with_pylaia"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.transcribe_existing_server_document")
    def test_execute_mutually_exclusive_dev_modes_raises_without_engine_calls(
        self, mock_existing, mock_upload
    ):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

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
            transkribus_dev_upload_mode=True,
            transkribus_dev_existing_document_id="1",
            transkribus_collection_id="2",
            transkribus_model_id="3",
            transkribus_dev_existing_pages="1",
        )
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        doc = self._create_document()
        with self.assertRaises(EnginePermanentError) as ctx:
            adapter.execute(
                pages=pages,
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                worker_env=cfg,
                document_id=doc.id,
            )
        self.assertIn("mutually exclusive", str(ctx.exception).lower())
        mock_existing.assert_not_called()
        mock_upload.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_execute_success_maps_htr_result(self, m_login, m_start, m_complete):
        from documents.services.env_validation import WorkerEnvConfig

        m_start.return_value = "job-1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="line one\nline two",
            review_reasons=[],
            recognition_job_id="job-1",
        )
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

        doc = self._create_document()
        result = adapter.execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="en",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=cfg,
            document_id=doc.id,
        )
        self.assertEqual(result.text, "line one\nline two")
        self.assertFalse(result.needs_review)
        self.assertEqual(result.engine_name, "transkribus-pylaia:42")
        m_login.assert_called_once()
        m_start.assert_called_once()
        m_complete.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_execute_maps_retryable_engine_error(self, m_login, m_start):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.transkribus_engine import TranskribusRetryableError

        m_start.side_effect = TranskribusRetryableError("slow")
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

        doc = self._create_document()
        with self.assertRaises(EngineRetryableError):
            adapter.execute(
                pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
                language_hint="en",
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                worker_env=cfg,
                document_id=doc.id,
            )

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_execute_dev_upload_mode_calls_stepwise_engine(self, m_login, m_upload, m_start, m_complete):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

        from documents.services.transkribus_engine import TrpUploadOutcome

        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="999",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_start.return_value = "recog-1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="uploaded",
            review_reasons=[],
            recognition_job_id="recog-1",
        )
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
            transkribus_api_token="tok",
            transkribus_username="u",
            transkribus_password="p",
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            transkribus_dev_upload_mode=True,
            transkribus_collection_id="col",
            transkribus_model_id="42",
        )
        pages = [
            PageImage(page_index=2, image_bytes=b"a", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"b", mime_type="image/png"),
        ]
        doc = self._create_document()
        result = adapter.execute(
            pages=pages,
            language_hint="en",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=cfg,
            document_id=doc.id,
        )
        self.assertEqual(result.text, "uploaded")
        m_upload.assert_called_once()
        self.assertEqual(m_upload.call_args.kwargs["pages"], pages)
        m_start.assert_called_once()
        m_complete.assert_called_once()

    def test_execute_dev_upload_mode_missing_required_env_raises(self):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

        adapter = TranskribusAdapter()
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]

        def base_cfg(**kwargs):
            defaults = dict(
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
                transkribus_dev_upload_mode=True,
                transkribus_collection_id="c",
                transkribus_model_id="m",
            )
            defaults.update(kwargs)
            return WorkerEnvConfig(**defaults)

        cases = [
            ("username", dict(transkribus_username=None)),
            ("password", dict(transkribus_password=None)),
            ("api token", dict(transkribus_api_token=None)),
            ("collection", dict(transkribus_collection_id=None)),
            ("model", dict(transkribus_model_id=None)),
        ]
        doc = self._create_document()
        for label, override in cases:
            with self.subTest(missing=label):
                with self.assertRaises(EnginePermanentError) as ctx:
                    adapter.execute(
                        pages=pages,
                        language_hint="en",
                        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                        worker_env=base_cfg(**override),
                        document_id=doc.id,
                    )
                self.assertIn("dev upload mode configuration incomplete", str(ctx.exception).lower())

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_execute_dev_upload_mode_does_not_require_dev_existing_document_env(
        self, m_login, m_upload, m_start, m_complete
    ):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

        from documents.services.transkribus_engine import TrpUploadOutcome

        m_upload.return_value = TrpUploadOutcome(
            collection_id="c",
            doc_id="1",
            upload_id=1,
            ingest_job_id="j",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_start.return_value = "r1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="ok",
            review_reasons=[],
            recognition_job_id="r1",
        )
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
            transkribus_dev_upload_mode=True,
            transkribus_collection_id="c",
            transkribus_model_id="9",
            transkribus_dev_existing_document_id=None,
            transkribus_dev_existing_pages=None,
        )
        doc = self._create_document()
        adapter.execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="en",
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            worker_env=cfg,
            document_id=doc.id,
        )
        m_upload.assert_called_once()

    def test_execute_missing_document_id_raises_before_engine(self):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

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
            transkribus_dev_upload_mode=True,
            transkribus_collection_id="c",
            transkribus_model_id="m",
        )
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        with patch(
            "documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server"
        ) as m_login:
            with self.assertRaises(EnginePermanentError) as ctx:
                adapter.execute(
                    pages=pages,
                    language_hint="en",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                    worker_env=cfg,
                )
            self.assertIn("document_id", str(ctx.exception).lower())
            m_login.assert_not_called()

    def test_execute_existing_mode_still_requires_dev_document_and_pages(self):
        from documents.services.env_validation import WorkerEnvConfig
        from documents.services.page_extraction import PageImage

        adapter = TranskribusAdapter()

        cfg_no_doc = WorkerEnvConfig(
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
            transkribus_collection_id="1",
            transkribus_model_id="2",
            transkribus_dev_existing_pages="1",
            transkribus_dev_existing_document_id=None,
        )
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        doc = self._create_document()
        with patch(
            "documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server"
        ) as m_login:
            with self.assertRaises(EnginePermanentError) as ctx:
                adapter.execute(
                    pages=pages,
                    language_hint="en",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                    worker_env=cfg_no_doc,
                    document_id=doc.id,
                )
            self.assertIn("TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID", str(ctx.exception))
            m_login.assert_not_called()

        cfg_no_pages = WorkerEnvConfig(
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
            transkribus_collection_id="1",
            transkribus_model_id="2",
            transkribus_dev_existing_document_id="9",
            transkribus_dev_existing_pages=None,
        )
        doc2 = self._create_document()
        with patch(
            "documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server"
        ) as m_login2:
            with self.assertRaises(EnginePermanentError) as ctx2:
                adapter.execute(
                    pages=pages,
                    language_hint="en",
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                    worker_env=cfg_no_pages,
                    document_id=doc2.id,
                )
            self.assertIn("TRANSKRIBUS_DEV_EXISTING_PAGES", str(ctx2.exception))
            m_login2.assert_not_called()


class TranskribusUploadHelpersTests(SimpleTestCase):
    """PR #3 Legacy /uploads helpers — no live TrpServer calls."""

    def test_build_document_upload_descriptor_json_img_only_pages(self):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import build_document_upload_descriptor_json

        pages = [
            PageImage(page_index=2, image_bytes=b"x", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"y", mime_type="image/png"),
        ]
        body = build_document_upload_descriptor_json(pages, title="  Doc A  ")
        self.assertEqual(
            body["pageList"]["pages"],
            [
                {"fileName": "vs_archive_p000001.png", "pageNr": 1},
                {"fileName": "vs_archive_p000002.png", "pageNr": 2},
            ],
        )
        self.assertEqual(body["md"]["title"], "Doc A")
        for p in body["pageList"]["pages"]:
            self.assertNotIn("pageXmlName", p)

    def test_build_document_upload_descriptor_stable_order_by_page_index(self):
        """pageList.pages must follow ascending page_index regardless of input list order."""
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import build_document_upload_descriptor_json

        pages = [
            PageImage(page_index=5, image_bytes=b"e", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"a", mime_type="image/png"),
            PageImage(page_index=3, image_bytes=b"c", mime_type="image/png"),
        ]
        body = build_document_upload_descriptor_json(pages)
        nrs = [p["pageNr"] for p in body["pageList"]["pages"]]
        names = [p["fileName"] for p in body["pageList"]["pages"]]
        self.assertEqual(nrs, [1, 3, 5])
        self.assertEqual(
            names,
            [
                "vs_archive_p000001.png",
                "vs_archive_p000003.png",
                "vs_archive_p000005.png",
            ],
        )

    def test_build_document_upload_descriptor_rejects_duplicate_page_index(self):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            build_document_upload_descriptor_json,
        )

        pages = [
            PageImage(page_index=1, image_bytes=b"a", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"b", mime_type="image/png"),
        ]
        with self.assertRaises(TranskribusPermanentError) as ctx:
            build_document_upload_descriptor_json(pages)
        self.assertIn("Duplicate", str(ctx.exception))

    def test_parse_upload_create_json_upload_id_redacted_fixture(self):
        # Shape aligned with Transkribus REST upload article (redacted / minimal).
        from documents.services.transkribus_engine import parse_upload_create_json_upload_id

        payload = {
            "uploadId": 1234567,
            "pageList": {
                "pages": [
                    {"fileName": "vs_archive_p000001.png", "pageNr": 1},
                ]
            },
        }
        self.assertEqual(parse_upload_create_json_upload_id(payload), 1234567)
        self.assertEqual(parse_upload_create_json_upload_id({"uploadId": "42"}), 42)

    def test_parse_upload_put_json_job_id_if_present(self):
        from documents.services.transkribus_engine import parse_upload_put_json_job_id_if_present

        r_ok = requests.Response()
        r_ok.status_code = 200
        r_ok._content = b'{"jobId": "ingest-7"}'
        r_ok.encoding = "utf-8"
        self.assertEqual(parse_upload_put_json_job_id_if_present(r_ok), "ingest-7")

        r_empty = requests.Response()
        r_empty.status_code = 200
        r_empty._content = b""
        r_empty.encoding = "utf-8"
        self.assertIsNone(parse_upload_put_json_job_id_if_present(r_empty))

        r_no_job = requests.Response()
        r_no_job.status_code = 200
        r_no_job._content = b'{"status":"ok"}'
        r_no_job.encoding = "utf-8"
        self.assertIsNone(parse_upload_put_json_job_id_if_present(r_no_job))

        r_non_json = requests.Response()
        r_non_json.status_code = 200
        r_non_json._content = b"OK"
        r_non_json.encoding = "utf-8"
        self.assertIsNone(parse_upload_put_json_job_id_if_present(r_non_json))

    def test_parse_upload_put_json_non_object_still_raises(self):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            parse_upload_put_json_job_id_if_present,
        )

        r = requests.Response()
        r.status_code = 200
        r._content = b'["jobId"]'
        r.encoding = "utf-8"
        with self.assertRaises(TranskribusPermanentError) as ctx:
            parse_upload_put_json_job_id_if_present(r)
        self.assertIn("not an object", str(ctx.exception).lower())

    def test_parse_doc_id_from_successful_trp_job_top_level(self):
        from documents.services.transkribus_engine import parse_doc_id_from_successful_trp_job

        job = {
            "success": True,
            "state": "FINISHED",
            "type": "Create Document",
            "jobImpl": "UploadImportJob",
            "docId": 987654,
        }
        self.assertEqual(parse_doc_id_from_successful_trp_job(job), "987654")

    def test_parse_doc_id_from_successful_trp_job_accepts_done_state(self):
        from documents.services.transkribus_engine import parse_doc_id_from_successful_trp_job

        job = {"success": True, "state": "DONE", "docId": 1}
        self.assertEqual(parse_doc_id_from_successful_trp_job(job), "1")

    def test_parse_doc_id_rejects_success_true_while_running(self):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            parse_doc_id_from_successful_trp_job,
        )

        with self.assertRaises(TranskribusPermanentError):
            parse_doc_id_from_successful_trp_job(
                {"success": True, "state": "RUNNING", "docId": 1}
            )

    def test_parse_doc_id_from_job_fails_when_not_success(self):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            parse_doc_id_from_successful_trp_job,
        )

        with self.assertRaises(TranskribusPermanentError):
            parse_doc_id_from_successful_trp_job({"success": False, "docId": 1})

    def test_strict_map_page_index_to_trp_page_nr_orders_by_sort(self):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TrpPageMetadata,
            strict_map_page_index_to_trp_page_nr,
        )

        imgs = [
            PageImage(page_index=2, image_bytes=b"x", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"y", mime_type="image/png"),
        ]
        meta = [
            TrpPageMetadata(1, 10, 100, None, []),
            TrpPageMetadata(2, 20, 100, None, []),
        ]
        m = strict_map_page_index_to_trp_page_nr(imgs, meta)
        self.assertEqual(m, {1: 1, 2: 2})

    def test_strict_map_rejects_count_mismatch(self):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            strict_map_page_index_to_trp_page_nr,
        )

        imgs = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        meta: list = []
        with self.assertRaises(TranskribusPermanentError) as ctx:
            strict_map_page_index_to_trp_page_nr(imgs, meta)
        self.assertIn("mismatch", str(ctx.exception).lower())

    def test_format_trp_pages_query_from_page_nrs(self):
        from documents.services.transkribus_engine import format_trp_pages_query_from_page_nrs

        self.assertEqual(format_trp_pages_query_from_page_nrs([3]), "3")
        self.assertEqual(format_trp_pages_query_from_page_nrs([1, 2, 3]), "1-3")
        self.assertEqual(format_trp_pages_query_from_page_nrs([1, 3]), "1,3")

    @patch("documents.services.transkribus_engine.fetch_pages_metadata")
    @patch("documents.services.transkribus_engine.poll_job_until_done")
    @patch("documents.services.transkribus_engine.get_trp_upload_resource_json_job_id")
    @patch("documents.services.transkribus_engine.put_trp_upload_page_image_only")
    @patch("documents.services.transkribus_engine.create_trp_upload_doc_structure")
    def test_run_trp_upload_page_images_through_ingest_falls_back_to_get_when_put_has_no_job(
        self, m_create, m_put, m_get_upload, m_poll, m_fetch
    ):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TrpPageMetadata,
            run_trp_upload_page_images_through_ingest,
        )

        m_create.return_value = 555
        m_put.return_value = None
        m_get_upload.return_value = "ingest-from-get"
        m_poll.return_value = {
            "success": True,
            "state": "FINISHED",
            "type": "Create Document",
            "jobImpl": "UploadImportJob",
            "docId": 1001,
        }
        m_fetch.return_value = [TrpPageMetadata(1, 10, 1001, None, [])]
        session = requests.Session()
        pages = [PageImage(page_index=1, image_bytes=b"\x89PNG", mime_type="image/png")]
        out = run_trp_upload_page_images_through_ingest(
            session,
            collection_id="42",
            pages=pages,
            title="t",
            poll_interval_sec=0.0,
            max_wait_sec=30.0,
        )
        self.assertEqual(out.ingest_job_id, "ingest-from-get")
        m_get_upload.assert_called_once()

    @patch("documents.services.transkribus_engine.fetch_pages_metadata")
    @patch("documents.services.transkribus_engine.poll_job_until_done")
    @patch("documents.services.transkribus_engine.put_trp_upload_page_image_only")
    @patch("documents.services.transkribus_engine.create_trp_upload_doc_structure")
    def test_run_trp_upload_page_images_through_ingest_wires_steps(
        self, m_create, m_put, m_poll, m_fetch
    ):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TrpPageMetadata,
            run_trp_upload_page_images_through_ingest,
        )

        m_create.return_value = 555
        m_put.return_value = "ingest-job-1"
        m_poll.return_value = {
            "success": True,
            "state": "FINISHED",
            "type": "Create Document",
            "jobImpl": "UploadImportJob",
            "docId": 999,
        }
        m_fetch.return_value = [
            TrpPageMetadata(1, 10, 999, None, []),
        ]
        session = requests.Session()
        pages = [PageImage(page_index=1, image_bytes=b"\x89PNG", mime_type="image/png")]
        out = run_trp_upload_page_images_through_ingest(
            session,
            collection_id="42",
            pages=pages,
            title="t",
            poll_interval_sec=0.0,
            max_wait_sec=30.0,
        )
        self.assertEqual(out.collection_id, "42")
        self.assertEqual(out.doc_id, "999")
        self.assertEqual(out.upload_id, 555)
        self.assertEqual(out.ingest_job_id, "ingest-job-1")
        self.assertEqual(out.pages_query, "1")
        self.assertEqual(out.page_index_to_page_nr, {1: 1})
        m_create.assert_called_once()
        m_put.assert_called_once()
        m_poll.assert_called_once_with(
            session,
            "ingest-job-1",
            poll_interval_sec=0.0,
            max_wait_sec=30.0,
            timeout_sec=60,
        )
        m_fetch.assert_called_once()


class TranskribusJobPollingTests(SimpleTestCase):
    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_continues_past_upload_import_created_success_false(
        self, m_get
    ):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.side_effect = [
            {
                "success": False,
                "state": "CREATED",
                "nrOfErrors": 0,
                "description": "1 in Queue",
            },
            {
                "success": True,
                "state": "FINISHED",
                "type": "Create Document",
                "jobImpl": "UploadImportJob",
                "docId": 1,
            },
        ]
        session = requests.Session()
        job = poll_job_until_done(
            session,
            "j1",
            poll_interval_sec=0.0,
            max_wait_sec=10.0,
        )
        self.assertEqual(job.get("state"), "FINISHED")
        self.assertTrue(job.get("success"))

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_finished_success_true_single_response(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.return_value = {"success": True, "state": "FINISHED", "docId": 9}
        session = requests.Session()
        job = poll_job_until_done(
            session, "jfin", poll_interval_sec=0.0, max_wait_sec=5.0
        )
        self.assertTrue(job.get("success"))
        self.assertEqual(job.get("docId"), 9)

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_success_true_while_running_is_not_terminal(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.side_effect = [
            {"success": True, "state": "RUNNING"},
            {"success": True, "state": "FINISHED", "docId": 99},
        ]
        session = requests.Session()
        job = poll_job_until_done(
            session,
            "j2",
            poll_interval_sec=0.0,
            max_wait_sec=10.0,
        )
        self.assertEqual(job.get("state"), "FINISHED")

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_failed_state_raises(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {
            "success": False,
            "state": "FAILED",
            "description": "boom",
        }
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError) as ctx:
            poll_job_until_done(session, "j3", poll_interval_sec=0.0, max_wait_sec=5.0)
        self.assertIn("boom", str(ctx.exception))

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_finished_without_success_raises(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {"success": False, "state": "FINISHED"}
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError):
            poll_job_until_done(session, "j4", poll_interval_sec=0.0, max_wait_sec=5.0)

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_queued_success_false_keeps_polling(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.side_effect = [
            {"success": False, "state": "QUEUED", "nrOfErrors": 0},
            {"success": True, "state": "FINISHED", "docId": 2},
        ]
        session = requests.Session()
        job = poll_job_until_done(
            session, "jq", poll_interval_sec=0.0, max_wait_sec=10.0
        )
        self.assertEqual(job.get("state"), "FINISHED")

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_waiting_success_false_keeps_polling(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.side_effect = [
            {"success": False, "state": "WAITING", "nrOfErrors": 0},
            {"success": True, "state": "FINISHED"},
        ]
        session = requests.Session()
        job = poll_job_until_done(
            session, "jw", poll_interval_sec=0.0, max_wait_sec=10.0
        )
        self.assertTrue(job.get("success"))

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_completed_success_true(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.return_value = {"success": True, "state": "COMPLETED", "docId": 3}
        session = requests.Session()
        job = poll_job_until_done(session, "jc", poll_interval_sec=0.0, max_wait_sec=5.0)
        self.assertEqual(job.get("state"), "COMPLETED")

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_done_success_true_pylaia_style(self, m_get):
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.return_value = {"success": True, "state": "DONE", "description": "done"}
        session = requests.Session()
        job = poll_job_until_done(session, "jd", poll_interval_sec=0.0, max_wait_sec=5.0)
        self.assertEqual(job.get("state"), "DONE")

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_error_state_raises(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {"success": False, "state": "ERROR", "description": "bad"}
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError):
            poll_job_until_done(session, "je", poll_interval_sec=0.0, max_wait_sec=5.0)

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_cancelled_state_raises(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {"success": False, "state": "CANCELLED"}
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError):
            poll_job_until_done(session, "jcan", poll_interval_sec=0.0, max_wait_sec=5.0)

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_canceled_us_spelling_raises(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {"success": False, "state": "CANCELED"}
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError):
            poll_job_until_done(session, "jus", poll_interval_sec=0.0, max_wait_sec=5.0)

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_running_nr_of_errors_positive_keeps_polling_then_success(self, m_get):
        """nrOfErrors>0 is ignored while state is still RUNNING (non-terminal)."""
        from documents.services.transkribus_engine import poll_job_until_done

        m_get.side_effect = [
            {"success": False, "state": "RUNNING", "nrOfErrors": 2},
            {"success": True, "state": "DONE"},
        ]
        session = requests.Session()
        job = poll_job_until_done(
            session, "jnr", poll_interval_sec=0.0, max_wait_sec=10.0
        )
        self.assertEqual(job.get("state"), "DONE")

    @patch("documents.services.transkribus_engine.fetch_transcript_xml")
    @patch("documents.services.transkribus_engine.fetch_pages_metadata")
    @patch("documents.services.transkribus_engine.get_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    @patch("documents.services.transkribus_engine.login_trp_server")
    def test_transcribe_existing_server_document_uses_poll_until_pylaia_done(
        self, m_login, m_start, m_get, m_fetch_pages, m_fetch_xml
    ):
        """PR #2 path still completes when poll sees in-progress then DONE + success."""
        from documents.services.transkribus_engine import (
            TrpPageMetadata,
            transcribe_existing_server_document,
        )

        m_start.return_value = "j99"
        m_get.side_effect = [
            {"success": False, "state": "WAITING", "nrOfErrors": 0},
            {"success": True, "state": "DONE"},
        ]
        m_fetch_pages.return_value = [
            TrpPageMetadata(
                1,
                10,
                1,
                None,
                [
                    {
                        "jobId": "j99",
                        "modelId": "42",
                        "url": "https://example.invalid/transcript",
                    }
                ],
            )
        ]
        m_fetch_xml.return_value = b"""<?xml version="1.0" encoding="UTF-8"?>
        <PcGts xmlns="http://schema.primaresearch.org/PAGE/gts/pagecontent/2013-07-15">
          <Page><TextLine><TextEquiv><Unicode>hi</Unicode></TextEquiv></TextLine></Page>
        </PcGts>"""
        text, reasons = transcribe_existing_server_document(
            username="dummy",
            password="dummy",
            bearer_token="dummy",
            collection_id="1",
            model_id="42",
            dev_document_id="9",
            dev_pages_query="1",
        )
        self.assertEqual(text, "hi")
        self.assertEqual(reasons, [])
        self.assertEqual(m_get.call_count, 2)

    @patch("documents.services.transkribus_engine.pylaia_transcribe_document_with_session")
    @patch("documents.services.transkribus_engine.login_trp_server")
    def test_transcribe_existing_server_document_delegates_to_shared_pylaia_helper(
        self, m_login, m_pylaia
    ):
        from documents.services import transkribus_engine as tr

        m_pylaia.return_value = PylaiaTranscriptionOutcome(
            text="plain",
            review_reasons=[],
            recognition_job_id="job-1",
        )
        text, reasons = tr.transcribe_existing_server_document(
            username="a",
            password="b",
            bearer_token="c",
            collection_id="1",
            model_id="7",
            dev_document_id="88",
            dev_pages_query="1-3",
        )
        self.assertEqual(text, "plain")
        self.assertEqual(reasons, [])
        m_pylaia.assert_called_once()
        kw = m_pylaia.call_args.kwargs
        self.assertEqual(kw["document_id"], "88")
        self.assertEqual(kw["pages_query"], "1-3")
        self.assertEqual(kw["model_id"], "7")
        self.assertEqual(kw["collection_id"], "1")

    @patch("documents.services.transkribus_engine.pylaia_transcribe_document_with_session")
    @patch("documents.services.transkribus_engine.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.transkribus_engine.login_trp_server")
    def test_upload_then_transcribe_wires_upload_outcome_into_pylaia_and_htr_result(
        self, m_login, m_upload, m_pylaia
    ):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TrpUploadOutcome,
            upload_then_transcribe_page_images_with_pylaia,
        )

        m_upload.return_value = TrpUploadOutcome(
            collection_id="1",
            doc_id="999",
            upload_id=1,
            ingest_job_id="j",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_pylaia.return_value = PylaiaTranscriptionOutcome(
            text="hello",
            review_reasons=["EMPTY_TRANSCRIPT_PAGE"],
            recognition_job_id="job-1",
        )
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        htr = upload_then_transcribe_page_images_with_pylaia(
            username="u",
            password="p",
            bearer_token="t",
            collection_id="1",
            model_id="42",
            pages=pages,
            upload_title="doc",
            poll_interval_sec=0.0,
        )
        self.assertEqual(htr.text, "hello")
        self.assertTrue(htr.needs_review)
        self.assertEqual(htr.engine_name, "transkribus-pylaia:42")
        self.assertEqual(htr.review_reasons, ["EMPTY_TRANSCRIPT_PAGE"])
        m_upload.assert_called_once()
        self.assertEqual(m_upload.call_args.kwargs["pages"], pages)
        m_pylaia.assert_called_once()
        pkw = m_pylaia.call_args.kwargs
        self.assertEqual(pkw["document_id"], "999")
        self.assertEqual(pkw["pages_query"], "1")
        self.assertIs(m_upload.call_args[0][0], m_pylaia.call_args[0][0])


class StartPylaiaRecognitionTests(SimpleTestCase):
    """
    Legacy TrpServer PyLaia ``/pylaia/.../recognition`` POST auth (real account probe):

    Session after login **with** ``Authorization: Bearer`` → HTTP 401. Bearer-only → 401.
    **Session cookies only** (no Bearer on this POST) → HTTP 200 and plain-text job id.
    Transcript fetches still use Bearer elsewhere in ``transkribus_engine`` — not this call.
    """

    @patch("documents.services.transkribus_engine._session_request")
    def test_start_pylaia_post_uses_session_auth_accept_only_no_body(self, m_req):
        from documents.services.transkribus_engine import start_pylaia_recognition

        m_resp = Mock()
        m_resp.text = "  job-from-server  \n"
        m_req.return_value = m_resp
        session = requests.Session()
        jid = start_pylaia_recognition(
            session,
            collection_id="10",
            model_id="20",
            document_id="1001",
            pages_query="1",
        )
        self.assertEqual(jid, "job-from-server")
        m_req.assert_called_once()
        args, kwargs = m_req.call_args
        self.assertIs(args[0], session)
        self.assertEqual(args[1], "POST")
        self.assertTrue(args[2].endswith("/pylaia/10/20/recognition"))
        self.assertEqual(kwargs["params"]["id"], "1001")
        self.assertEqual(kwargs["params"]["pages"], "1")
        self.assertEqual(kwargs["params"]["credits"], "USER_ONLY")
        self.assertEqual(kwargs["params"]["writeKwsIndex"], "false")
        self.assertEqual(kwargs["params"]["clearLines"], "false")
        self.assertEqual(kwargs["params"]["doWordSeg"], "false")
        self.assertEqual(kwargs["params"]["useExistingLinePolygons"], "false")
        self.assertEqual(kwargs["params"]["doLinePolygonSimplification"], "true")
        self.assertEqual(kwargs["params"]["languageModel"], "")
        self.assertNotIn("data", kwargs)
        self.assertNotIn("json", kwargs)
        hdrs = kwargs["headers"]
        self.assertEqual(hdrs["Accept"], "application/json, text/plain, */*")
        self.assertEqual({k.lower() for k in hdrs}, {"accept"})
        self.assertNotIn("authorization", {k.lower() for k in hdrs})
        self.assertNotIn("content-type", {k.lower() for k in hdrs})

    @patch("documents.services.transkribus_engine._session_request")
    def test_start_pylaia_accepts_plain_text_job_id_not_json(self, m_req):
        from documents.services.transkribus_engine import start_pylaia_recognition

        m_resp = Mock()
        m_resp.text = "plain-text-job-id-only"
        m_req.return_value = m_resp
        session = requests.Session()
        jid = start_pylaia_recognition(
            session,
            collection_id="1",
            model_id="2",
            document_id="3",
            pages_query="1-2",
        )
        self.assertEqual(jid, "plain-text-job-id-only")


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


class TranskribusWorkdirRetryEngineTests(SimpleTestCase):
    """Classification + bounded recognition retry for the transient PyLaia workdir failure."""

    def test_classifier_true_for_workdir_signature(self):
        from documents.services.transkribus_engine import (
            is_retryable_pylaia_workdir_failure,
        )

        desc = (
            "Could not create workdir at: "
            "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_564149"
        )
        self.assertTrue(is_retryable_pylaia_workdir_failure(desc))

    def test_classifier_false_for_other_descriptions(self):
        from documents.services.transkribus_engine import (
            is_retryable_pylaia_workdir_failure,
        )

        self.assertFalse(is_retryable_pylaia_workdir_failure(None))
        self.assertFalse(is_retryable_pylaia_workdir_failure(""))
        self.assertFalse(is_retryable_pylaia_workdir_failure("model not found"))
        self.assertFalse(
            is_retryable_pylaia_workdir_failure("Could not create workdir at: /other/path")
        )

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_workdir_failure_is_retryable(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusRetryableError,
            poll_job_until_done,
        )

        m_get.return_value = {
            "success": False,
            "state": "FAILED",
            "description": (
                "Could not create workdir at: "
                "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_999"
            ),
        }
        session = requests.Session()
        with self.assertRaises(TranskribusRetryableError) as ctx:
            poll_job_until_done(session, "w1", poll_interval_sec=0.0, max_wait_sec=5.0)
        self.assertIn("workdir", str(ctx.exception).lower())

    @patch("documents.services.transkribus_engine.get_job")
    def test_poll_other_job_failure_remains_permanent(self, m_get):
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            poll_job_until_done,
        )

        m_get.return_value = {
            "success": False,
            "state": "FAILED",
            "description": "PyLaia model could not be loaded",
        }
        session = requests.Session()
        with self.assertRaises(TranskribusPermanentError):
            poll_job_until_done(session, "p1", poll_interval_sec=0.0, max_wait_sec=5.0)

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    def test_recognition_retries_workdir_then_succeeds(self, m_start, m_complete, m_sleep):
        from documents.services.transkribus_engine import (
            PylaiaTranscriptionOutcome,
            TranskribusRetryableError,
            run_recognition_with_workdir_retry,
        )

        m_start.side_effect = ["job-1", "job-2"]
        m_complete.side_effect = [
            TranskribusRetryableError(
                "Transkribus job job-1 failed: Could not create workdir at: "
                "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_job-1"
            ),
            PylaiaTranscriptionOutcome(
                text="recovered", review_reasons=[], recognition_job_id="job-2"
            ),
        ]
        started: list[str] = []
        session = requests.Session()
        outcome = run_recognition_with_workdir_retry(
            session,
            collection_id="col",
            model_id="42",
            document_id="16537736",
            pages_query="1-4",
            bearer_token="b",
            max_attempts=3,
            retry_delays=(30, 300),
            on_recognition_started=started.append,
        )
        self.assertEqual(outcome.text, "recovered")
        self.assertEqual(m_start.call_count, 2)
        self.assertEqual(started, ["job-1", "job-2"])
        m_sleep.assert_called_once_with(30.0)

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    def test_recognition_workdir_failure_reraised_after_budget(
        self, m_start, m_complete, m_sleep
    ):
        from documents.services.transkribus_engine import (
            TranskribusRetryableError,
            run_recognition_with_workdir_retry,
        )

        m_start.side_effect = ["j1", "j2"]
        workdir_exc = TranskribusRetryableError(
            "Transkribus job failed: Could not create workdir at: "
            "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_x"
        )
        m_complete.side_effect = [workdir_exc, workdir_exc]
        session = requests.Session()
        with self.assertRaises(TranskribusRetryableError):
            run_recognition_with_workdir_retry(
                session,
                collection_id="col",
                model_id="42",
                document_id="16539496",
                pages_query="1-4",
                bearer_token="b",
                max_attempts=2,
                retry_delays=(30, 300),
            )
        self.assertEqual(m_start.call_count, 2)
        m_sleep.assert_called_once_with(30.0)

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    def test_recognition_non_workdir_retryable_not_retried(
        self, m_start, m_complete, m_sleep
    ):
        from documents.services.transkribus_engine import (
            TranskribusRetryableError,
            run_recognition_with_workdir_retry,
        )

        m_start.return_value = "j1"
        m_complete.side_effect = TranskribusRetryableError(
            "Transkribus get job: request timed out"
        )
        session = requests.Session()
        with self.assertRaises(TranskribusRetryableError):
            run_recognition_with_workdir_retry(
                session,
                collection_id="col",
                model_id="42",
                document_id="1",
                pages_query="1",
                bearer_token="b",
                max_attempts=3,
                retry_delays=(30, 300),
            )
        self.assertEqual(m_start.call_count, 1)
        m_sleep.assert_not_called()


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
    def test_document_id_not_forwarded_to_gemini_engine(self, mock_gemini_transcribe):
        mock_gemini_transcribe.return_value = GeminiResult(
            text="text",
            engine_name="gemini-2.0-flash",
        )
        adapter = GeminiAdapter()

        adapter.execute(
            pages=[],
            language_hint="he",
            prompt_variant="printed",
            document_id=42,
        )

        kwargs = mock_gemini_transcribe.call_args.kwargs
        self.assertNotIn("document_id", kwargs)

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
        self.doc = create_ocr_document(
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
    def test_gemini_success_persists_needs_review_policy_non_hebrew_stays_partial(
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
        self.assertEqual(result.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            result.verification_status, DocumentTextResult.VerificationStatus.UNVERIFIED
        )
        self.assertEqual(result.text, "recognized text")
        reasons = json.loads(result.review_reasons or "[]")
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
        self.assertNotIn("NEEDS_REVIEW_FLAG", reasons)
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
    def test_gemini_success_adapter_needs_review_adds_needs_review_flag(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="recognized text long enough for min length",
            needs_review=True,
            engine_name="gemini-2.0-flash",
            review_reasons=["ENGINE_SUPPLIED_REASON"],
        )

        self.assertTrue(self.command._process_message(self._message()))

        result = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
        )
        self.assertEqual(result.status, DocumentTextResult.Status.NEEDS_REVIEW)
        reasons = json.loads(result.review_reasons or "[]")
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
        self.assertIn("NEEDS_REVIEW_FLAG", reasons)
        self.assertIn("ENGINE_SUPPLIED_REASON", reasons)
        self.assertNotIn("MIN_TEXT_LENGTH", reasons)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_gemini_success_review_reasons_include_min_text_length_when_short(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="hi",
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
        reasons = json.loads(result.review_reasons or "[]")
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
        self.assertIn("MIN_TEXT_LENGTH", reasons)
        self.assertNotIn("NEEDS_REVIEW_FLAG", reasons)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_gemini_success_review_reasons_include_has_unclear_when_marker_present(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="plain text long enough then [UNCLEAR] end",
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
        reasons = json.loads(result.review_reasons or "[]")
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
        self.assertIn("HAS_UNCLEAR", reasons)
        self.assertNotIn("NEEDS_REVIEW_FLAG", reasons)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_hebrew_printed_gemini_success_ready_when_hebrew_text_usable_needs_review(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        he_doc = create_ocr_document(
            title="Hebrew doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="he.pdf",
            mime_type="application/pdf",
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="Hebrew transcript body long enough",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        msg = {
            "Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": he_doc.id})
        }
        self.assertTrue(self.command._process_message(msg))

        he_doc.refresh_from_db()
        self.assertEqual(he_doc.processing_state_user, Document.ProcessingState.READY)

        hb = DocumentTextResult.objects.get(
            document=he_doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-2.0-flash",
        )
        self.assertEqual(hb.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            hb.verification_status, DocumentTextResult.VerificationStatus.UNVERIFIED
        )
        reasons = json.loads(hb.review_reasons or "[]")
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
        self.assertNotIn("NEEDS_REVIEW_FLAG", reasons)

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
        doc = create_ocr_document(
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

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_enabled_hebrew_handwritten_transkribus_route_used_and_persisted_by_worker(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        """
        Real select_ocr_route + worker persistence; Transkribus engine mocked (no HTTP).
        """
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        engine_runtime = "transkribus-pylaia:999"
        mock_transcribe.return_value = HtrResult(
            text="mock trp text",
            needs_review=False,
            engine_name=engine_runtime,
            review_reasons=[],
        )

        he_doc = create_ocr_document(
            title="Hebrew HTR",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="he-doc.pdf",
            mime_type="application/pdf",
        )
        msg = {
            "Body": json.dumps(
                {"type": "PROCESS_DOCUMENT", "document_id": he_doc.id}
            )
        }

        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true",
            },
            clear=False,
        ):
            self.assertTrue(self.command._process_message(msg))

        mock_transcribe.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        self.assertEqual(
            call_kw["route"].engine_key,
            DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        )
        self.assertEqual(
            call_kw["route"].prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        self.assertIn("worker_env", call_kw)
        self.assertEqual(call_kw["document_id"], he_doc.id)

        he_doc.refresh_from_db()
        self.assertEqual(he_doc.processing_state_user, Document.ProcessingState.READY)

        for r_type in (
            DocumentTextResult.ResultType.SOURCE_TEXT,
            DocumentTextResult.ResultType.HEBREW_TEXT,
        ):
            with self.subTest(result_type=r_type):
                row = DocumentTextResult.objects.get(
                    document=he_doc,
                    result_type=r_type,
                    engine=engine_runtime,
                )
                self.assertEqual(row.status, DocumentTextResult.Status.NEEDS_REVIEW)
                self.assertEqual(
                    row.verification_status,
                    DocumentTextResult.VerificationStatus.UNVERIFIED,
                )
                reasons = json.loads(row.review_reasons or "[]")
                self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", reasons)
                self.assertNotIn("NEEDS_REVIEW_FLAG", reasons)
                self.assertEqual(row.text, "mock trp text")
                self.assertEqual(
                    row.engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS
                )
                self.assertEqual(
                    row.prompt_variant,
                    DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                )
                self.assertIsNone(row.error_code)

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_disabled_hebrew_handwritten_route_fails_fast_without_gemini_fallback(
        self,
        mock_get_object_bytes,
        mock_extract_pages,
        mock_transcribe,
    ):
        he_doc = create_ocr_document(
            title="Hebrew HTR disabled",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="he-disabled.pdf",
            mime_type="application/pdf",
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]

        msg = {
            "Body": json.dumps(
                {"type": "PROCESS_DOCUMENT", "document_id": he_doc.id}
            )
        }

        with patch.dict(
            os.environ,
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
            clear=False,
        ):
            self.assertTrue(self.command._process_message(msg))

        mock_transcribe.assert_not_called()

        for r_type in (
            DocumentTextResult.ResultType.SOURCE_TEXT,
            DocumentTextResult.ResultType.HEBREW_TEXT,
        ):
            with self.subTest(result_type=r_type):
                failure = DocumentTextResult.objects.get(
                    document=he_doc,
                    result_type=r_type,
                    engine="ocr-dispatch",
                )
                self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
                self.assertEqual(failure.error_code, "OCR_ROUTING_INVALID")
                self.assertEqual(failure.engine_key, "UNRESOLVED")
                self.assertEqual(failure.prompt_variant, "UNRESOLVED")
                self.assertIn(
                    "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN",
                    failure.error_details or "",
                )
                self.assertIn("Gemini fallback", failure.error_details or "")

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_transkribus_failure_persists_transkribus_route_metadata_without_gemini_fallback(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        he_doc = create_ocr_document(
            title="Hebrew HTR failure",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="he-failure.pdf",
            mime_type="application/pdf",
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.side_effect = EnginePermanentError(
            "Transkribus upload failed in test"
        )

        msg = {
            "Body": json.dumps(
                {"type": "PROCESS_DOCUMENT", "document_id": he_doc.id}
            )
        }

        with patch.dict(
            os.environ,
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
            clear=False,
        ):
            self.assertTrue(self.command._process_message(msg))

        for r_type in (
            DocumentTextResult.ResultType.SOURCE_TEXT,
            DocumentTextResult.ResultType.HEBREW_TEXT,
        ):
            with self.subTest(result_type=r_type):
                failure = DocumentTextResult.objects.get(
                    document=he_doc,
                    result_type=r_type,
                    engine="ocr-dispatch",
                )
                self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
                self.assertEqual(failure.error_code, "OCR_FAILED")
                self.assertEqual(
                    failure.engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS
                )
                self.assertEqual(
                    failure.prompt_variant,
                    DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                )
                self.assertIn("Transkribus upload failed", failure.error_details or "")

    def test_run_worker_source_does_not_reference_transkribus_route_flags(self):
        import documents.management.commands.run_worker as mod

        src = inspect.getsource(mod)
        self.assertNotIn("ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN", src)
        self.assertNotIn("TRANSKRIBUS_DEV_OCR_ROUTE", src)


def _png_bytes(color=(255, 0, 0)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (4, 4), color).save(buf, format="PNG")
    return buf.getvalue()


class MultiImageWorkerTests(TestCase):
    def setUp(self):
        self.command = Command()
        self.command._cfg = SimpleNamespace(
            min_text_length=5,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.85,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=8192,
        )

    def _make_doc(self, *, language, text_input_type, expected_count):
        return create_ocr_document(
            title="Multi-image doc",
            doc_type=Document.DocType.IMAGE,
            language=language,
            text_input_type=text_input_type,
            upload_status=Document.UploadStatus.UPLOADED,
            expected_source_file_count=expected_count,
        )

    def _add_source(
        self,
        doc,
        order_index,
        *,
        upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        file_s3_key=None,
        mime_type="image/png",
    ):
        return DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=file_s3_key
            if file_s3_key is not None
            else f"documents/{doc.id}/source/{order_index}.png",
            file_original_name=f"page-{order_index}.png",
            mime_type=mime_type,
            size_bytes=100 + order_index,
            upload_status=upload_status,
        )

    def _message(self, doc) -> dict:
        return {
            "Body": json.dumps(
                {"type": "PROCESS_DOCUMENT", "document_id": doc.id}
            )
        }

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_multi_image_worker_builds_ordered_pages_with_one_based_page_index(
        self, mock_transcribe, mock_get_object_bytes
    ):
        doc = self._make_doc(
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            expected_count=3,
        )
        # Create rows out of order to prove the worker orders by order_index.
        self._add_source(doc, 2)
        self._add_source(doc, 0)
        self._add_source(doc, 1)

        mock_get_object_bytes.side_effect = lambda bucket, key: (_png_bytes(), "image/png")
        mock_transcribe.return_value = HtrResult(
            text="combined text",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        self.assertTrue(self.command._process_message(self._message(doc)))

        # Each S3 key is read, in order_index order.
        read_keys = [call.kwargs["key"] for call in mock_get_object_bytes.call_args_list]
        self.assertEqual(
            read_keys,
            [
                f"documents/{doc.id}/source/0.png",
                f"documents/{doc.id}/source/1.png",
                f"documents/{doc.id}/source/2.png",
            ],
        )

        # The adapter receives the combined, ordered pages.
        mock_transcribe.assert_called_once()
        pages = mock_transcribe.call_args.kwargs["pages"]
        self.assertEqual([p.page_index for p in pages], [1, 2, 3])
        self.assertTrue(all(p.mime_type == "image/png" for p in pages))

        result = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
        )
        self.assertEqual(result.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(result.text, "combined text")
        doc.refresh_from_db()
        # Non-Hebrew: HEBREW_TEXT missing -> PARTIAL (intentional current policy).
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_multi_image_hebrew_success_is_ready(
        self, mock_transcribe, mock_get_object_bytes
    ):
        doc = self._make_doc(
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.PRINTED,
            expected_count=2,
        )
        self._add_source(doc, 0)
        self._add_source(doc, 1)

        mock_get_object_bytes.side_effect = lambda bucket, key: (_png_bytes(), "image/png")
        mock_transcribe.return_value = HtrResult(
            text="טקסט עברי",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        self.assertTrue(self.command._process_message(self._message(doc)))

        self.assertTrue(
            DocumentTextResult.objects.filter(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            ).exists()
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_multi_image_hebrew_handwritten_does_not_fall_back_to_gemini(
        self, mock_transcribe, mock_get_object_bytes
    ):
        doc = self._make_doc(
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            expected_count=2,
        )
        self._add_source(doc, 0)
        self._add_source(doc, 1)

        mock_get_object_bytes.side_effect = lambda bucket, key: (_png_bytes(), "image/png")

        # Flag disabled (default): routing must fail explicitly, not route to Gemini.
        with patch.dict(
            os.environ,
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
            clear=False,
        ):
            self.assertTrue(self.command._process_message(self._message(doc)))

        mock_transcribe.assert_not_called()
        for r_type in (
            DocumentTextResult.ResultType.SOURCE_TEXT,
            DocumentTextResult.ResultType.HEBREW_TEXT,
        ):
            failure = DocumentTextResult.objects.get(
                document=doc, result_type=r_type, engine="ocr-dispatch"
            )
            self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
            self.assertEqual(failure.error_code, "OCR_ROUTING_INVALID")
            self.assertEqual(failure.engine_key, "UNRESOLVED")
            self.assertNotEqual(
                failure.engine_key, DocumentTextResult.OcrEngineKey.GEMINI
            )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_multi_image_validation_failures_mark_failed_without_ocr(
        self, mock_transcribe, mock_get_object_bytes
    ):
        scenarios = {
            "missing_row": "missing",
            "pending": DocumentSourceFile.UploadStatus.PENDING,
            "failed": DocumentSourceFile.UploadStatus.FAILED,
            "empty_key": "empty_key",
            "non_image": "non_image",
        }

        for label, mode in scenarios.items():
            with self.subTest(scenario=label):
                mock_transcribe.reset_mock()
                mock_get_object_bytes.reset_mock()
                doc = self._make_doc(
                    language=Document.Language.ENGLISH,
                    text_input_type=Document.TextInputType.PRINTED,
                    expected_count=2,
                )
                self._add_source(doc, 0)
                if mode == "missing":
                    pass  # order_index=1 intentionally absent
                elif mode == "empty_key":
                    self._add_source(doc, 1, file_s3_key="")
                elif mode == "non_image":
                    self._add_source(doc, 1, mime_type="application/pdf")
                else:
                    self._add_source(doc, 1, upload_status=mode)

                self.assertTrue(self.command._process_message(self._message(doc)))

                mock_transcribe.assert_not_called()
                mock_get_object_bytes.assert_not_called()
                self.assertEqual(
                    DocumentTextResult.objects.filter(document=doc).count(), 0
                )
                doc.refresh_from_db()
                self.assertEqual(
                    doc.processing_state_user, Document.ProcessingState.FAILED
                )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_multi_image_extra_out_of_range_source_file_marks_failed_without_ocr(
        self, mock_transcribe, mock_get_object_bytes
    ):
        doc = self._make_doc(
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            expected_count=2,
        )
        # Valid 0..N-1 rows, all uploaded, plus an extra out-of-range row.
        self._add_source(doc, 0)
        self._add_source(doc, 1)
        self._add_source(doc, 99)

        self.assertTrue(self.command._process_message(self._message(doc)))

        mock_transcribe.assert_not_called()
        mock_get_object_bytes.assert_not_called()
        self.assertEqual(DocumentTextResult.objects.filter(document=doc).count(), 0)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)


class WorkerEnvConfigTests(SimpleTestCase):
    def test_validate_required_env_defaults_transkribus_hebrew_handwritten_flag_to_false(
        self,
    ):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "test-gemini-key"},
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertFalse(cfg.enable_transkribus_hebrew_handwritten)

    def test_validate_required_env_defaults_gemini_hebrew_printed_model(self):
        with patch.dict(
            os.environ,
            {"GEMINI_API_KEY": "test-gemini-key"},
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertEqual(cfg.gemini_hebrew_printed_model, "gemini-3.1-flash-lite")

    def test_validate_required_env_gemini_hebrew_printed_model_override(self):
        with patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "test-gemini-key",
                "GEMINI_HEBREW_PRINTED_MODEL": "custom-hebrew-printed-model",
            },
            clear=True,
        ):
            cfg = validate_required_env()

        self.assertEqual(cfg.gemini_hebrew_printed_model, "custom-hebrew-printed-model")


def _worker_env_for_dev_transkribus_upload_command(**overrides):
    from documents.services.env_validation import WorkerEnvConfig

    base = dict(
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
        transkribus_api_token="tok",
        transkribus_username="u",
        transkribus_password="p",
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=2048,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.7,
        transkribus_use_existing_server_document=False,
        transkribus_dev_upload_mode=True,
        transkribus_dev_existing_document_id=None,
        transkribus_collection_id="col",
        transkribus_model_id="42",
        transkribus_dev_existing_pages=None,
    )
    base.update(overrides)
    return WorkerEnvConfig(**base)


class DevTranskribusTranscribeCommandTests(SimpleTestCase):
    @patch("documents.management.commands.dev_transkribus_transcribe.transcribe_pages")
    @patch("documents.management.commands.dev_transkribus_transcribe.validate_required_env")
    def test_missing_confirm_fails_before_transcribe_pages(
        self, mock_validate_env, mock_transcribe
    ):
        with self.assertRaises(CommandError) as ctx:
            call_command("dev_transkribus_transcribe", "/nonexistent/path.png")
        self.assertIn("clean it up", str(ctx.exception).lower())
        mock_transcribe.assert_not_called()
        mock_validate_env.assert_not_called()

    @patch("documents.management.commands.dev_transkribus_transcribe.transcribe_pages")
    @patch("documents.management.commands.dev_transkribus_transcribe.validate_required_env")
    def test_confirm_calls_transcribe_pages_with_transkribus_route_and_worker_env(
        self, mock_validate_env, mock_transcribe
    ):
        cfg = _worker_env_for_dev_transkribus_upload_command()
        mock_validate_env.return_value = cfg
        mock_transcribe.return_value = HtrResult(
            text="hello" * 200,
            needs_review=True,
            engine_name="transkribus-pylaia:42",
            review_reasons=["x"],
        )

        buf = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(buf, format="PNG")
        png_bytes = buf.getvalue()
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            os.write(fd, png_bytes)
            os.close(fd)
            call_command(
                "dev_transkribus_transcribe",
                path,
                confirm_create_transkribus_doc=True,
                text_preview_limit=10,
            )
        finally:
            os.unlink(path)

        mock_transcribe.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        self.assertEqual(
            call_kw["route"].engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS
        )
        self.assertEqual(
            call_kw["route"].prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        self.assertIs(call_kw["worker_env"], cfg)
        self.assertEqual(call_kw["language_hint"], "he")
        self.assertEqual(call_kw["text_input_type"], "HANDWRITTEN")
        self.assertEqual(len(call_kw["pages"]), 1)

    @patch("documents.management.commands.dev_transkribus_transcribe.transcribe_pages")
    @patch("documents.management.commands.dev_transkribus_transcribe.validate_required_env")
    def test_transkribus_dev_upload_mode_required_before_transcribe(
        self, mock_validate_env, mock_transcribe
    ):
        cfg = _worker_env_for_dev_transkribus_upload_command(
            transkribus_dev_upload_mode=False
        )
        mock_validate_env.return_value = cfg

        buf = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(buf, format="PNG")
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            os.write(fd, buf.getvalue())
            os.close(fd)
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "dev_transkribus_transcribe",
                    path,
                    confirm_create_transkribus_doc=True,
                )
        finally:
            os.unlink(path)

        self.assertIn("TRANSKRIBUS_DEV_UPLOAD_MODE", str(ctx.exception))
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.dev_transkribus_transcribe.transcribe_pages")
    @patch("documents.management.commands.dev_transkribus_transcribe.validate_required_env")
    def test_ocr_routes_unchanged_after_command(self, mock_validate_env, mock_transcribe):
        import documents.services.ocr_routing as ocr_routing

        before = {k: (v.engine_key, v.prompt_variant) for k, v in ocr_routing.OCR_ROUTES.items()}
        cfg = _worker_env_for_dev_transkribus_upload_command()
        mock_validate_env.return_value = cfg
        mock_transcribe.return_value = HtrResult(text="ok", engine_name="transkribus-pylaia:1")

        buf = BytesIO()
        Image.new("RGB", (2, 2), color="white").save(buf, format="PNG")
        fd, path = tempfile.mkstemp(suffix=".png")
        try:
            os.write(fd, buf.getvalue())
            os.close(fd)
            call_command(
                "dev_transkribus_transcribe",
                path,
                confirm_create_transkribus_doc=True,
                prompt_variant="PRINTED",
            )
        finally:
            os.unlink(path)

        after = {k: (v.engine_key, v.prompt_variant) for k, v in ocr_routing.OCR_ROUTES.items()}
        self.assertEqual(before, after)
        self.assertEqual(
            mock_transcribe.call_args.kwargs["route"].prompt_variant,
            DocumentTextResult.OcrPromptVariant.PRINTED,
        )

    def test_dev_command_source_does_not_import_worker_routing_selector(self):
        import documents.management.commands.dev_transkribus_transcribe as mod

        src = inspect.getsource(mod)
        self.assertNotIn("select_ocr_route", src)
        self.assertNotIn("from documents.management.commands.run_worker", src)


class TranskribusCleanupReportTests(TestCase):
    def _create_document(self, *, title: str = "Cleanup report doc") -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            language=Document.Language.HEBREW,
        )

    def _create_run(
        self,
        doc: Document,
        *,
        status: str,
        mode: str = TranskribusRun.Mode.UPLOAD_CREATED,
        remote_doc_id: str | None = None,
        pages_query: str | None = "1",
        collection_id: str = "col",
        model_id: str = "42",
        upload_id: int | None = 10,
        ingest_job_id: str | None = "ingest-1",
        recognition_job_id: str | None = None,
        age_hours: int | None = None,
    ) -> TranskribusRun:
        run = TranskribusRun.objects.create(
            document=doc,
            mode=mode,
            status=status,
            collection_id=collection_id,
            model_id=model_id,
            remote_doc_id=remote_doc_id,
            pages_query=pages_query,
            page_index_to_page_nr={0: 1} if pages_query else None,
            upload_id=upload_id,
            ingest_job_id=ingest_job_id,
            recognition_job_id=recognition_job_id,
        )
        if age_hours is not None:
            ts = timezone.now() - timedelta(hours=age_hours)
            TranskribusRun.objects.filter(id=run.id).update(created_at=ts, updated_at=ts)
            run.refresh_from_db()
        return run

    def test_report_retains_remote_doc_shared_by_recognition_only_history(self):
        from documents.services.transkribus_cleanup_report import (
            RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        failed_run = self._create_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            recognition_job_id="recog-old",
        )
        succeeded_run = self._create_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="555",
            recognition_job_id="recog-new",
        )

        report = build_transkribus_cleanup_report()
        remote_doc = report["remote_docs"][0]

        self.assertEqual(remote_doc["bucket"], RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC)
        self.assertEqual(remote_doc["remote_doc_id"], "555")
        self.assertEqual(remote_doc["run_ids"], [failed_run.id, succeeded_run.id])

    def test_report_retains_existing_server_remote_doc(self):
        from documents.services.transkribus_cleanup_report import (
            RETAIN_EXISTING_SERVER,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        self._create_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            remote_doc_id="existing-99",
            upload_id=None,
            ingest_job_id=None,
        )

        report = build_transkribus_cleanup_report()

        self.assertEqual(report["remote_docs"][0]["bucket"], RETAIN_EXISTING_SERVER)

    def test_report_retains_verified_document_remote_doc(self):
        from documents.services.transkribus_cleanup_report import (
            RETAIN_VERIFIED_DOCUMENT,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        self._create_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="approved text",
        )

        report = build_transkribus_cleanup_report()

        self.assertEqual(report["remote_docs"][0]["bucket"], RETAIN_VERIFIED_DOCUMENT)

    def test_report_flags_superseded_successful_remote_doc(self):
        from documents.services.transkribus_cleanup_report import (
            RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC,
            REVIEW_SUPERSEDED_FORCE_REPROCESS_REMOTE_DOC,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        self._create_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="111",
        )
        self._create_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="222",
        )

        report = build_transkribus_cleanup_report()
        remote_docs = {item["remote_doc_id"]: item["bucket"] for item in report["remote_docs"]}

        self.assertEqual(
            remote_docs["111"],
            REVIEW_SUPERSEDED_FORCE_REPROCESS_REMOTE_DOC,
        )
        self.assertEqual(
            remote_docs["222"],
            RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC,
        )

    def test_report_flags_failed_after_upload_when_remote_doc_not_reusable(self):
        from documents.services.transkribus_cleanup_report import (
            REVIEW_FAILED_AFTER_UPLOAD_REMOTE_DOC,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        self._create_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="333",
            pages_query=None,
            upload_id=20,
            ingest_job_id="ingest-20",
        )

        report = build_transkribus_cleanup_report()

        self.assertEqual(
            report["remote_docs"][0]["bucket"],
            REVIEW_FAILED_AFTER_UPLOAD_REMOTE_DOC,
        )

    def test_report_marks_stale_in_progress_run_without_reclassifying_remote_doc(self):
        from documents.services.transkribus_cleanup_report import (
            RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC,
            REVIEW_STALE_IN_PROGRESS_RUN,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        stale_run = self._create_run(
            doc,
            status=TranskribusRun.Status.UPLOADED,
            remote_doc_id="444",
            age_hours=72,
        )

        report = build_transkribus_cleanup_report(stale_hours=24)
        remote_doc = report["remote_docs"][0]
        run_row = next(item for item in report["runs"] if item["run_id"] == stale_run.id)

        self.assertEqual(remote_doc["bucket"], RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC)
        self.assertEqual(run_row["bucket"], REVIEW_STALE_IN_PROGRESS_RUN)

    def test_report_classifies_failed_without_remote_doc_as_local_only(self):
        from documents.services.transkribus_cleanup_report import (
            LOCAL_ONLY_FAILED_WITHOUT_REMOTE_DOC,
            build_transkribus_cleanup_report,
        )

        doc = self._create_document()
        failed_run = self._create_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            pages_query=None,
            upload_id=None,
            ingest_job_id=None,
        )

        report = build_transkribus_cleanup_report()
        run_row = next(item for item in report["runs"] if item["run_id"] == failed_run.id)

        self.assertEqual(run_row["bucket"], LOCAL_ONLY_FAILED_WITHOUT_REMOTE_DOC)


class ReportTranskribusCleanupCommandTests(TestCase):
    def test_command_outputs_json_without_mutating_rows(self):
        doc = create_ocr_document(
            title="Cleanup command doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            status=TranskribusRun.Status.SUCCEEDED,
            collection_id="col",
            model_id="42",
            remote_doc_id="555",
            pages_query="1",
            page_index_to_page_nr={0: 1},
            upload_id=10,
            ingest_job_id="ingest-1",
            recognition_job_id="recog-1",
        )
        before = {
            "run_count": TranskribusRun.objects.count(),
            "status": run.status,
            "updated_at": run.updated_at,
        }

        stdout = StringIO()
        call_command("report_transkribus_cleanup", "--json", stdout=stdout)
        payload = json.loads(stdout.getvalue())

        run.refresh_from_db()
        self.assertEqual(TranskribusRun.objects.count(), before["run_count"])
        self.assertEqual(run.status, before["status"])
        self.assertEqual(run.updated_at, before["updated_at"])
        self.assertEqual(payload["summary"]["remote_doc_count"], 1)
        self.assertEqual(payload["summary"]["run_count"], 1)

    def test_cleanup_command_source_does_not_import_transkribus_engine_or_requests(self):
        import documents.management.commands.report_transkribus_cleanup as mod

        src = inspect.getsource(mod)
        self.assertNotIn("transkribus_engine", src)
        self.assertNotIn("requests", src)


class TranskribusRunModelTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Transkribus run test doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_create_minimal_upload_run_started(self):
        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        self.assertEqual(run.status, TranskribusRun.Status.STARTED)
        self.assertIsNone(run.remote_doc_id)

    def test_create_existing_server_run_with_remote_doc_id(self):
        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id="1",
            model_id="99",
            remote_doc_id="12345",
            pages_query="1-3",
        )
        self.assertEqual(run.remote_doc_id, "12345")
        self.assertEqual(run.pages_query, "1-3")
        self.assertEqual(run.mode, TranskribusRun.Mode.EXISTING_SERVER)

    def test_page_index_to_page_nr_json_roundtrip(self):
        doc = self._create_document()
        mapping = {"0": 1, "1": 2, "2": 3}
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
            page_index_to_page_nr=mapping,
        )
        run.refresh_from_db()
        self.assertEqual(run.page_index_to_page_nr, mapping)

    def test_default_status_is_started(self):
        doc = self._create_document()
        run = TranskribusRun(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        self.assertEqual(run.status, TranskribusRun.Status.STARTED)

    def test_cascade_delete_from_document(self):
        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        run_id = run.id
        doc.delete()
        self.assertFalse(TranskribusRun.objects.filter(id=run_id).exists())

    def test_status_choices_include_lifecycle_values(self):
        expected = {
            "STARTED",
            "UPLOADED",
            "RECOGNITION_STARTED",
            "SUCCEEDED",
            "FAILED",
        }
        self.assertEqual(
            {choice.value for choice in TranskribusRun.Status},
            expected,
        )

    def test_mode_choices(self):
        expected = {"UPLOAD_CREATED", "EXISTING_SERVER"}
        self.assertEqual(
            {choice.value for choice in TranskribusRun.Mode},
            expected,
        )


class DocumentSourceFileModelTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Source file test doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_create_source_file_with_metadata(self):
        doc = self._create_document()
        source = DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="uploads/1/page-0.jpg",
            file_original_name="scan-001.jpg",
            mime_type="image/jpeg",
            size_bytes=12345,
        )
        self.assertEqual(source.document_id, doc.id)
        self.assertEqual(source.order_index, 0)
        self.assertEqual(source.file_s3_key, "uploads/1/page-0.jpg")
        self.assertEqual(source.file_original_name, "scan-001.jpg")
        self.assertEqual(source.mime_type, "image/jpeg")
        self.assertEqual(source.size_bytes, 12345)

    def test_default_ordering_by_order_index(self):
        doc = self._create_document()
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=2,
            file_s3_key="uploads/1/page-2.jpg",
        )
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="uploads/1/page-0.jpg",
        )
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=1,
            file_s3_key="uploads/1/page-1.jpg",
        )
        ordered = list(doc.source_files.all())
        self.assertEqual([row.order_index for row in ordered], [0, 1, 2])

    def test_unique_order_index_per_document(self):
        doc = self._create_document()
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="uploads/1/page-0.jpg",
        )
        with self.assertRaises(IntegrityError):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=0,
                file_s3_key="uploads/1/page-0-dup.jpg",
            )

    def test_same_order_index_allowed_across_documents(self):
        doc_a = self._create_document()
        doc_b = self._create_document()
        DocumentSourceFile.objects.create(
            document=doc_a,
            order_index=0,
            file_s3_key="uploads/a/page-0.jpg",
        )
        DocumentSourceFile.objects.create(
            document=doc_b,
            order_index=0,
            file_s3_key="uploads/b/page-0.jpg",
        )
        self.assertEqual(DocumentSourceFile.objects.filter(order_index=0).count(), 2)

    def test_unique_file_s3_key_per_document(self):
        doc = self._create_document()
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="uploads/1/shared.jpg",
        )
        with self.assertRaises(IntegrityError):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=1,
                file_s3_key="uploads/1/shared.jpg",
            )

    def test_same_file_s3_key_allowed_across_documents(self):
        doc_a = self._create_document()
        doc_b = self._create_document()
        shared_key = "uploads/shared-key.jpg"
        DocumentSourceFile.objects.create(
            document=doc_a,
            order_index=0,
            file_s3_key=shared_key,
        )
        DocumentSourceFile.objects.create(
            document=doc_b,
            order_index=0,
            file_s3_key=shared_key,
        )
        self.assertEqual(
            DocumentSourceFile.objects.filter(file_s3_key=shared_key).count(),
            2,
        )

    def test_cascade_delete_from_document(self):
        doc = self._create_document()
        source = DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key="uploads/1/page-0.jpg",
        )
        source_id = source.id
        doc.delete()
        self.assertFalse(DocumentSourceFile.objects.filter(id=source_id).exists())

    def test_negative_order_index_rejected_by_db_constraint(self):
        doc = self._create_document()
        with self.assertRaises(IntegrityError):
            DocumentSourceFile.objects.create(
                document=doc,
                order_index=-1,
                file_s3_key="uploads/1/page-invalid.jpg",
            )


class UploadCompleteSourceFileTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        from documents.s3 import S3HeadObjectResult

        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.staff = User.objects.create_user(
            username="upload_complete_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_uploading_document(self, **kwargs):
        defaults = {
            "title": "Upload complete test doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "upload_status": Document.UploadStatus.UPLOADING,
            "file_s3_key": "documents/99/original.jpg",
            "file_original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _post_complete(self, doc_id: int, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    @patch("documents.views.send_process_document_message")
    def test_successful_upload_complete_creates_primary_source_file(
        self, mock_enqueue
    ):
        doc = self._create_uploading_document()

        resp = self._post_complete(
            doc.id,
            {
                "success": True,
                "file_size": 2048,
                "file_mime": "image/jpeg",
            },
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["upload_status"], Document.UploadStatus.UPLOADED)
        self.assertEqual(
            body["processing_state_user"],
            Document.ProcessingState.PROCESSING,
        )
        mock_enqueue.assert_called_once_with(document_id=doc.id)

        doc.refresh_from_db()
        self.assertEqual(doc.size_bytes, 2048)
        self.assertEqual(doc.mime_type, "image/jpeg")

        sources = list(DocumentSourceFile.objects.filter(document=doc))
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source.order_index, 0)
        self.assertEqual(source.file_s3_key, doc.file_s3_key)
        self.assertEqual(source.file_original_name, doc.file_original_name)
        self.assertEqual(source.mime_type, doc.mime_type)
        self.assertEqual(source.size_bytes, doc.size_bytes)

    @patch("documents.views.send_process_document_message")
    def test_repeated_upload_complete_does_not_duplicate_source_file(
        self, mock_enqueue
    ):
        doc = self._create_uploading_document()

        first = self._post_complete(doc.id, {"success": True})
        self.assertEqual(first.status_code, 200)
        mock_enqueue.assert_called_once()

        second = self._post_complete(
            doc.id,
            {
                "success": True,
                "file_size": 4096,
                "file_mime": "image/jpeg",
            },
        )
        self.assertEqual(second.status_code, 200)
        mock_enqueue.assert_called_once()

        doc.refresh_from_db()
        self.assertEqual(doc.size_bytes, 4096)
        self.assertEqual(doc.mime_type, "image/jpeg")

        sources = list(DocumentSourceFile.objects.filter(document=doc))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].order_index, 0)
        self.assertEqual(sources[0].size_bytes, 4096)
        self.assertEqual(sources[0].mime_type, "image/jpeg")

    @patch("documents.views.send_process_document_message")
    def test_failed_upload_complete_does_not_create_source_file(self, mock_enqueue):
        doc = self._create_uploading_document()

        resp = self._post_complete(doc.id, {"success": False, "error": "s3 put failed"})

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["upload_status"], Document.UploadStatus.FAILED)
        self.assertEqual(
            body["processing_state_user"],
            Document.ProcessingState.FAILED,
        )
        mock_enqueue.assert_not_called()
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 0)

    @patch("documents.views.send_process_document_message")
    def test_success_without_file_s3_key_returns_400_and_no_source_file(
        self, mock_enqueue
    ):
        doc = self._create_uploading_document(file_s3_key="")

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "file_s3_key missing")
        mock_enqueue.assert_not_called()
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 0)

    @patch("documents.views.send_process_document_message")
    def test_already_uploaded_retry_does_not_re_enqueue(self, mock_enqueue):
        doc = self._create_uploading_document(
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
        )

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_not_called()
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 1)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_missing_s3_object_returns_400_and_does_not_enqueue(
        self, mock_enqueue
    ):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(exists=False)
        doc = self._create_uploading_document()

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 object not found")
        self.mock_s3_head.assert_called_once_with("test-bucket", doc.file_s3_key)
        mock_enqueue.assert_not_called()

        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)
        self.assertEqual(DocumentSourceFile.objects.filter(document=doc).count(), 0)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_succeeds_when_s3_content_type_matches(
        self, mock_enqueue
    ):
        doc = self._create_uploading_document()

        resp = self._post_complete(
            doc.id,
            {"success": True, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 200)
        self.mock_s3_head.assert_called_once_with("test-bucket", doc.file_s3_key)
        mock_enqueue.assert_called_once_with(document_id=doc.id)
        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_rejects_s3_content_type_mismatch(self, mock_enqueue):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True, content_type="image/png"
        )
        doc = self._create_uploading_document()

        resp = self._post_complete(
            doc.id,
            {"success": True, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 content type mismatch")
        self.assertEqual(resp.json()["document_id"], doc.id)
        mock_enqueue.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_rejects_missing_s3_content_type(self, mock_enqueue):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(exists=True, content_type=None)
        doc = self._create_uploading_document()

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 content type missing")
        mock_enqueue.assert_not_called()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_uses_document_mime_when_file_mime_omitted(
        self, mock_enqueue
    ):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True, content_type="image/jpeg"
        )
        doc = self._create_uploading_document(mime_type="image/jpeg")

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc.id)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_accepts_s3_content_type_with_charset_suffix(
        self, mock_enqueue
    ):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True, content_type="image/jpeg; charset=binary"
        )
        doc = self._create_uploading_document()

        resp = self._post_complete(
            doc.id,
            {"success": True, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_accepts_s3_jpg_alias_as_jpeg(self, mock_enqueue):
        from documents.s3 import S3HeadObjectResult

        for s3_mime in ("image/jpg", "image/pjpeg"):
            with self.subTest(s3_mime=s3_mime):
                mock_enqueue.reset_mock()
                self.mock_s3_head.return_value = S3HeadObjectResult(
                    exists=True, content_type=s3_mime
                )
                doc = self._create_uploading_document(
                    file_s3_key=f"documents/{s3_mime.replace('/', '-')}/original.jpg",
                )

                resp = self._post_complete(
                    doc.id,
                    {"success": True, "file_mime": "image/jpeg"},
                )

                self.assertEqual(resp.status_code, 200)
                mock_enqueue.assert_called_once()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.send_process_document_message")
    def test_upload_complete_s3_verification_failure_returns_502(self, mock_enqueue):
        from botocore.exceptions import ClientError

        self.mock_s3_head.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "HeadObject",
        )
        doc = self._create_uploading_document()

        resp = self._post_complete(doc.id, {"success": True})

        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"], "s3 verification failed")
        mock_enqueue.assert_not_called()


class UploadApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        from documents.s3 import S3HeadObjectResult

        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.staff = User.objects.create_user(
            username="upload_api_staff",
            password="test-pass",
            is_staff=True,
        )

    def _post_create(self, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            "/api/uploads/create/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_part_complete(self, doc_id: int, order_index: int, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/parts/{order_index}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _post_finalize(self, doc_id: int, payload: dict | None = None):
        self.client.force_login(self.staff)
        body = payload if payload is not None else {"success": True}
        return self.client.post(
            f"/api/uploads/{doc_id}/finalize/",
            data=json.dumps(body),
            content_type="application/json",
        )

    def _post_complete(self, doc_id: int, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            f"/api/uploads/{doc_id}/complete/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _base_create_payload(self, **overrides):
        payload = {
            "title": "Upload API test",
            "doc_type": "IMAGE",
            "text_input_type": "HANDWRITTEN",
            "original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        }
        payload.update(overrides)
        return payload

    def _multi_files_payload(self, count: int = 2, **overrides):
        files = [
            {
                "original_name": f"page-{i + 1}.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 1000 + i,
            }
            for i in range(count)
        ]
        payload = {
            "title": "Multi-image upload test",
            "text_input_type": "HANDWRITTEN",
            "files": files,
        }
        payload.update(overrides)
        return payload

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_single_file_create_response_shape_unchanged(self, _mock_put):
        resp = self._post_create(self._base_create_payload())
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(
            set(body.keys()),
            {"document_id", "upload_status", "s3_key", "upload_url"},
        )
        self.assertEqual(body["upload_status"], Document.UploadStatus.UPLOADING)
        self.assertTrue(body["s3_key"].startswith("documents/"))
        self.assertTrue(body["s3_key"].endswith("/original.jpeg"))

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_defaults_visibility_private_when_omitted(self, _mock_put):
        resp = self._post_create(self._base_create_payload())
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.visibility, Document.Visibility.PRIVATE)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_single_file_create_accepts_year_date_precision(self, _mock_put):
        resp = self._post_create(
            self._base_create_payload(date_precision=Document.DatePrecision.YEAR)
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.date_precision, Document.DatePrecision.YEAR)
        self.assertEqual(doc.archive_item.date_precision, Document.DatePrecision.YEAR)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_multi_image_create_accepts_year_date_precision(self, _mock_put):
        resp = self._post_create(
            self._multi_files_payload(date_precision=Document.DatePrecision.YEAR)
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.date_precision, Document.DatePrecision.YEAR)
        self.assertEqual(doc.archive_item.date_precision, Document.DatePrecision.YEAR)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_defaults_date_precision_unknown_when_omitted(self, _mock_put):
        resp = self._post_create(self._base_create_payload())
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.date_precision, Document.DatePrecision.UNKNOWN)
        self.assertEqual(doc.archive_item.date_precision, Document.DatePrecision.UNKNOWN)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_rejects_invalid_date_precision(self, _mock_put):
        resp = self._post_create(self._base_create_payload(date_precision="GUESS"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"date_precision is invalid", resp.content)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_saves_author_name_and_source_title_on_archive_item(
        self, _mock_put
    ):
        resp = self._post_create(
            self._base_create_payload(
                author_name="רחל כהן",
                source_title="הארץ",
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "רחל כהן")
        self.assertEqual(doc.archive_item.source_title, "הארץ")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_strips_author_name_and_source_title_whitespace(
        self, _mock_put
    ):
        resp = self._post_create(
            self._base_create_payload(
                author_name="  יוסף לוי  ",
                source_title="  דבר  ",
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "יוסף לוי")
        self.assertEqual(doc.archive_item.source_title, "דבר")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_omitted_source_metadata_saves_empty_strings(
        self, _mock_put
    ):
        resp = self._post_create(self._base_create_payload())
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "")
        self.assertEqual(doc.archive_item.source_title, "")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_whitespace_only_source_metadata_saves_empty_strings(
        self, _mock_put
    ):
        resp = self._post_create(
            self._base_create_payload(
                author_name="   ",
                source_title="\t",
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "")
        self.assertEqual(doc.archive_item.source_title, "")

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_rejects_over_255_author_name(self, _mock_put):
        before_count = Document.objects.count()
        resp = self._post_create(
            self._base_create_payload(author_name="א" * 256)
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "מחבר/ת חייב להיות עד 255 תווים".encode("utf-8"),
            resp.content,
        )
        self.assertEqual(Document.objects.count(), before_count)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_rejects_over_255_source_title(self, _mock_put):
        before_count = Document.objects.count()
        resp = self._post_create(
            self._base_create_payload(source_title="מ" * 256)
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(
            "מקור חייב להיות עד 255 תווים".encode("utf-8"),
            resp.content,
        )
        self.assertEqual(Document.objects.count(), before_count)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_multi_image_create_saves_source_metadata_on_archive_item(
        self, _mock_put
    ):
        resp = self._post_create(
            self._multi_files_payload(
                author_name="דוד בן-גוריון",
                source_title="מגילת העצמאות",
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "דוד בן-גוריון")
        self.assertEqual(doc.archive_item.source_title, "מגילת העצמאות")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_saves_archive_item_discovery_metadata(self, _mock_put):
        resp = self._post_create(
            self._base_create_payload(
                categories=["יהדות מצרים", "הפרשה"],
                events=["חתונה של דוד"],
                discovery_tags=["משפחה", "ירושלים"],
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        item = doc.archive_item
        self.assertEqual(
            list(item.categories.order_by("name").values_list("name", flat=True)),
            ["הפרשה", "יהדות מצרים"],
        )
        self.assertEqual(
            list(item.events.order_by("name").values_list("name", flat=True)),
            ["חתונה של דוד"],
        )
        self.assertEqual(
            list(item.tags.order_by("name").values_list("name", flat=True)),
            ["ירושלים", "משפחה"],
        )
        self.assertIsNone(doc.category_event)
        self.assertEqual(list(doc.tags_m2m.values_list("name", flat=True)), [])

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_saves_discovery_metadata_from_selected_ids_and_new_names(
        self, _mock_put
    ):
        from documents.models import ArchiveCategory, Tag

        existing_cat = ArchiveCategory.objects.create(
            name="Existing OCR upload category",
            slug="existing-ocr-upload-category",
        )
        existing_tag = Tag.objects.create(name="existing-ocr-upload-tag")

        resp = self._post_create(
            self._base_create_payload(
                selected_categories=[existing_cat.id],
                selected_tags=[existing_tag.id],
                categories="New OCR upload category",
                events="New OCR upload event",
                discovery_tags="new-ocr-upload-tag",
            )
        )
        self.assertEqual(resp.status_code, 201)
        item = Document.objects.get(id=resp.json()["document_id"]).archive_item
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"Existing OCR upload category", "New OCR upload category"},
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["New OCR upload event"],
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"existing-ocr-upload-tag", "new-ocr-upload-tag"},
        )

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_multi_image_create_saves_archive_item_discovery_metadata(self, _mock_put):
        resp = self._post_create(
            self._multi_files_payload(
                categories="קטגוריה אחת",
                events="אירוע אחד",
                discovery_tags=["תגית-א", "תגית-ב"],
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        item = doc.archive_item
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["קטגוריה אחת"],
        )
        self.assertEqual(
            list(item.events.values_list("name", flat=True)),
            ["אירוע אחד"],
        )
        self.assertEqual(
            list(item.tags.order_by("name").values_list("name", flat=True)),
            ["תגית-א", "תגית-ב"],
        )
        self.assertIsNone(doc.category_event)
        self.assertEqual(list(doc.tags_m2m.values_list("name", flat=True)), [])

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_does_not_set_legacy_document_discovery_fields(
        self, _mock_put
    ):
        resp = self._post_create(
            self._base_create_payload(
                category_event="Legacy event",
                tags=["legacy-tag"],
                categories=["Archive category"],
                discovery_tags=["archive-tag"],
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertIsNone(doc.category_event)
        self.assertEqual(list(doc.tags_m2m.values_list("name", flat=True)), [])
        self.assertEqual(
            list(doc.archive_item.categories.values_list("name", flat=True)),
            ["Archive category"],
        )
        self.assertEqual(
            list(doc.archive_item.tags.values_list("name", flat=True)),
            ["archive-tag"],
        )

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_create_upload_saves_admin_meta(self, _mock_put):
        resp = self._post_create(
            self._base_create_payload(
                admin_meta={
                    "donor": "יעל שיפמן",
                    "collection": "ארכיון משפחתי",
                    "original_location": "ירושלים",
                    "notes": "הערות פנימיות",
                }
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        meta = doc.admin_meta
        self.assertEqual(meta.donor, "יעל שיפמן")
        self.assertEqual(meta.collection, "ארכיון משפחתי")
        self.assertEqual(meta.original_location, "ירושלים")
        self.assertEqual(meta.notes, "הערות פנימיות")

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_still_enqueues_and_dual_writes(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._base_create_payload())
        doc_id = create_resp.json()["document_id"]

        complete_resp = self._post_complete(
            doc_id,
            {"success": True, "file_size": 2048, "file_mime": "image/jpeg"},
        )
        self.assertEqual(complete_resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)

        doc = Document.objects.get(id=doc_id)
        sources = list(DocumentSourceFile.objects.filter(document=doc))
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0].order_index, 0)
        self.assertEqual(sources[0].upload_status, DocumentSourceFile.UploadStatus.UPLOADED)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_retry_does_not_re_enqueue(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._base_create_payload())
        doc_id = create_resp.json()["document_id"]

        self._post_complete(doc_id, {"success": True})
        self._post_complete(doc_id, {"success": True})
        mock_enqueue.assert_called_once()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_valid_image_succeeds_when_s3_exists(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._base_create_payload())
        doc_id = create_resp.json()["document_id"]
        doc = Document.objects.get(id=doc_id)

        resp = self._post_complete(
            doc_id,
            {"success": True, "file_size": 2048, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 200)
        self.mock_s3_head.assert_called_once_with("test-bucket", doc.file_s3_key)
        mock_enqueue.assert_called_once_with(document_id=doc_id)
        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_rejects_unsupported_file_mime(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._base_create_payload())
        doc_id = create_resp.json()["document_id"]

        resp = self._post_complete(
            doc_id,
            {"success": True, "file_mime": "text/plain"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())
        self.mock_s3_head.assert_not_called()
        mock_enqueue.assert_not_called()
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_rejects_mime_extension_mismatch(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(
            self._base_create_payload(
                original_name="scan.png",
                mime_type="image/png",
            )
        )
        doc_id = create_resp.json()["document_id"]

        resp = self._post_complete(
            doc_id,
            {"success": True, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("does not match", resp.content.decode())
        self.mock_s3_head.assert_not_called()
        mock_enqueue.assert_not_called()
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_pdf_rejects_non_pdf_mime(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(
            self._base_create_payload(
                doc_type="PDF",
                mime_type="application/pdf",
                original_name="document.pdf",
            )
        )
        doc_id = create_resp.json()["document_id"]

        resp = self._post_complete(
            doc_id,
            {"success": True, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/pdf", resp.content.decode())
        self.mock_s3_head.assert_not_called()
        mock_enqueue.assert_not_called()
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_single_file_complete_mime_validation_failure_does_not_enqueue(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._base_create_payload())
        doc_id = create_resp.json()["document_id"]
        doc = Document.objects.get(id=doc_id)
        initial_mime_type = doc.mime_type

        resp = self._post_complete(
            doc_id,
            {"success": True, "file_mime": "application/pdf"},
        )

        self.assertEqual(resp.status_code, 400)
        self.mock_s3_head.assert_not_called()
        mock_enqueue.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)
        self.assertEqual(doc.mime_type, initial_mime_type)
        self.assertEqual(DocumentSourceFile.objects.filter(document_id=doc_id).count(), 0)

    @patch("documents.views.create_presigned_put")
    def test_multi_image_create_returns_ordered_uploads(self, mock_put):
        mock_put.side_effect = lambda **kwargs: f"https://example/{kwargs['key']}"

        resp = self._post_create(self._multi_files_payload(count=3))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["doc_type"], Document.DocType.IMAGE)
        self.assertEqual(body["expected_source_file_count"], 3)
        self.assertEqual(len(body["uploads"]), 3)
        self.assertEqual(
            [row["order_index"] for row in body["uploads"]],
            [0, 1, 2],
        )
        for row in body["uploads"]:
            self.assertTrue(row["s3_key"].startswith(f"documents/{body['document_id']}/source/"))
            self.assertTrue(row["upload_url"].startswith("https://example/"))

        doc = Document.objects.get(id=body["document_id"])
        self.assertEqual(doc.expected_source_file_count, 3)
        sources = list(doc.source_files.order_by("order_index"))
        self.assertEqual(len(sources), 3)
        for index, source in enumerate(sources):
            self.assertEqual(source.order_index, index)
            self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.PENDING)
            self.assertEqual(source.file_s3_key, body["uploads"][index]["s3_key"])

    def test_multi_image_create_rejects_empty_files(self):
        resp = self._post_create(
            {
                "title": "Bad multi",
                "text_input_type": "HANDWRITTEN",
                "files": [],
            }
        )
        self.assertEqual(resp.status_code, 400)

    def test_multi_image_create_rejects_single_file_in_files(self):
        resp = self._post_create(self._multi_files_payload(count=1))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("single-file", resp.content.decode())

    def test_multi_image_create_rejects_more_than_max_files(self):
        resp = self._post_create(self._multi_files_payload(count=31))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("at most 30 images", resp.content.decode())

    @patch("documents.views.create_presigned_put")
    def test_multi_image_create_accepts_max_files(self, mock_put):
        mock_put.side_effect = lambda **kwargs: f"https://example/{kwargs['key']}"

        resp = self._post_create(self._multi_files_payload(count=30))
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["expected_source_file_count"], 30)
        self.assertEqual(len(body["uploads"]), 30)

    def test_multi_image_create_rejects_non_image_mime(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][1]["mime_type"] = "application/pdf"
        payload["files"][1]["original_name"] = "page-2.pdf"
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_single_image_accepts_jpeg_jpg(self, _mock_put):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="IMAGE",
                mime_type="image/jpeg",
                original_name="scan.jpg",
            )
        )
        self.assertEqual(resp.status_code, 201)

    def test_single_image_rejects_pdf_mime_with_jpg_extension(self):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="IMAGE",
                mime_type="application/pdf",
                original_name="scan.jpg",
            )
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())

    def test_single_image_rejects_jpeg_mime_with_pdf_extension(self):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="IMAGE",
                mime_type="image/jpeg",
                original_name="scan.pdf",
            )
        )
        self.assertEqual(resp.status_code, 400)
        body = resp.content.decode()
        self.assertTrue(
            "does not match" in body or "allowed image extension" in body,
            msg=body,
        )

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_single_pdf_accepts_pdf_mime_and_extension(self, _mock_put):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="PDF",
                mime_type="application/pdf",
                original_name="document.pdf",
            )
        )
        self.assertEqual(resp.status_code, 201)

    def test_single_pdf_rejects_png_mime_with_pdf_extension(self):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="PDF",
                mime_type="image/png",
                original_name="document.pdf",
            )
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("application/pdf", resp.content.decode())

    def test_single_pdf_rejects_pdf_mime_with_png_extension(self):
        resp = self._post_create(
            self._base_create_payload(
                doc_type="PDF",
                mime_type="application/pdf",
                original_name="document.png",
            )
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn(".pdf", resp.content.decode())

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_multi_image_accepts_allowed_image_types(self, _mock_put):
        payload = self._multi_files_payload(count=2)
        payload["files"] = [
            {
                "original_name": "a.jpg",
                "mime_type": "image/jpeg",
                "size_bytes": 100,
            },
            {
                "original_name": "b.png",
                "mime_type": "image/png",
                "size_bytes": 200,
            },
        ]
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 201)

    def test_multi_image_rejects_pdf_in_files(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][1] = {
            "original_name": "page-2.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 1000,
        }
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)

    def test_multi_image_rejects_unsupported_mime(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][0]["mime_type"] = "text/plain"
        payload["files"][0]["original_name"] = "page-1.txt"
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())

    def test_multi_image_rejects_unsupported_extension(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][0]["original_name"] = "page-1.exe"
        payload["files"][0]["mime_type"] = "image/jpeg"
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("allowed image extension", resp.content.decode())

    def test_multi_image_rejects_mime_extension_mismatch(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][0]["mime_type"] = "image/jpeg"
        payload["files"][0]["original_name"] = "page-1.png"
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("does not match", resp.content.decode())

    def test_multi_image_create_rejects_client_order_index(self):
        payload = self._multi_files_payload(count=2)
        payload["files"][0]["order_index"] = 5
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("order_index", resp.content.decode())

    def test_multi_image_create_rejects_mixed_legacy_file_fields(self):
        payload = self._multi_files_payload(count=2)
        payload["original_name"] = "legacy.jpg"
        payload["mime_type"] = "image/jpeg"
        resp = self._post_create(payload)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("top-level single-file fields", resp.content.decode())

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_success_does_not_enqueue(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_size": 111, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_not_called()

        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=0)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.UPLOADED)
        self.assertEqual(source.size_bytes, 111)
        self.assertEqual(source.mime_type, "image/jpeg")

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_succeeds_when_s3_content_type_matches(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=0)

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_size": 111, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 200)
        self.mock_s3_head.assert_called_once_with("test-bucket", source.file_s3_key)
        mock_enqueue.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.UPLOADED)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_rejects_s3_content_type_mismatch(
        self, mock_enqueue, _mock_put
    ):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(
            exists=True, content_type="image/png"
        )
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=0)

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_size": 111, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 content type mismatch")
        self.assertEqual(resp.json()["document_id"], doc_id)
        self.assertEqual(resp.json()["order_index"], 0)
        mock_enqueue.assert_not_called()
        source.refresh_from_db()
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.PENDING)
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_missing_s3_object_leaves_part_pending(
        self, mock_enqueue, _mock_put
    ):
        from documents.s3 import S3HeadObjectResult

        self.mock_s3_head.return_value = S3HeadObjectResult(exists=False)
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]
        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=0)

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_size": 111, "file_mime": "image/jpeg"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], "s3 object not found")
        self.assertEqual(resp.json()["order_index"], 0)
        self.mock_s3_head.assert_called_once_with("test-bucket", source.file_s3_key)
        mock_enqueue.assert_not_called()

        source.refresh_from_db()
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.PENDING)
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_rejects_non_image_file_mime(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_mime": "application/pdf"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())
        mock_enqueue.assert_not_called()

        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=0)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.PENDING)

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADING)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_accepts_mime_matching_stored_extension(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            1,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_not_called()
        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=1)
        self.assertEqual(source.mime_type, "image/jpeg")
        self.assertEqual(source.file_original_name, "page-2.jpg")

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_part_complete_rejects_unsupported_mime(self, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_mime": "text/plain"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("must be one of", resp.content.decode())

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_part_complete_rejects_mime_extension_mismatch(self, _mock_put):
        payload = self._multi_files_payload(count=2)
        payload["files"][0]["original_name"] = "page-1.png"
        payload["files"][0]["mime_type"] = "image/png"
        create_resp = self._post_create(payload)
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            0,
            {"success": True, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("does not match", resp.content.decode())

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_failure_marks_document_failed(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(
            doc_id,
            1,
            {"success": False, "error": "s3 put failed"},
        )
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_not_called()

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.FAILED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=1)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.FAILED)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_retry_after_document_failed_is_rejected(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        fail_resp = self._post_part_complete(
            doc_id,
            1,
            {"success": False, "error": "s3 put failed"},
        )
        self.assertEqual(fail_resp.status_code, 200)

        source = DocumentSourceFile.objects.get(document_id=doc_id, order_index=1)
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.FAILED)
        self.assertEqual(source.upload_error, "s3 put failed")

        retry_resp = self._post_part_complete(
            doc_id,
            1,
            {"success": True, "file_size": 999, "file_mime": "image/jpeg"},
        )
        self.assertEqual(retry_resp.status_code, 400)
        mock_enqueue.assert_not_called()

        source.refresh_from_db()
        self.assertEqual(source.upload_status, DocumentSourceFile.UploadStatus.FAILED)
        self.assertEqual(source.upload_error, "s3 put failed")
        self.assertNotEqual(source.size_bytes, 999)

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.FAILED)
        self.assertEqual(doc.upload_error, "s3 put failed")
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_after_document_failed_is_rejected(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": False, "error": "failed"})

        resp = self._post_finalize(doc_id)
        self.assertEqual(resp.status_code, 400)
        mock_enqueue.assert_not_called()

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.upload_status, Document.UploadStatus.FAILED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)
        self.assertNotEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)
        self.assertEqual(doc.file_s3_key, "")

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_part_complete_invalid_order_index_returns_400(self, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_part_complete(doc_id, 9, {"success": True})
        self.assertEqual(resp.status_code, 400)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_fails_when_parts_pending(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True})

        resp = self._post_finalize(doc_id)
        self.assertEqual(resp.status_code, 400)
        mock_enqueue.assert_not_called()

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_fails_when_part_failed(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": False, "error": "failed"})

        resp = self._post_finalize(doc_id)
        self.assertEqual(resp.status_code, 400)
        mock_enqueue.assert_not_called()

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_success_mirrors_primary_sets_processing_and_enqueues(
        self, mock_enqueue, _mock_put
    ):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True, "file_size": 100})
        self._post_part_complete(doc_id, 1, {"success": True, "file_size": 200})

        resp = self._post_finalize(doc_id)
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)

        body = resp.json()
        self.assertEqual(body["upload_status"], Document.UploadStatus.UPLOADED)
        self.assertEqual(
            body["processing_state_user"], Document.ProcessingState.PROCESSING
        )

        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        primary = DocumentSourceFile.objects.get(document=doc, order_index=0)
        self.assertEqual(doc.file_s3_key, primary.file_s3_key)
        self.assertEqual(doc.file_original_name, primary.file_original_name)
        self.assertEqual(doc.mime_type, primary.mime_type)
        self.assertEqual(doc.size_bytes, primary.size_bytes)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_enqueue_failure_marks_document_failed(
        self, mock_enqueue, _mock_put
    ):
        mock_enqueue.side_effect = RuntimeError("sqs down")
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": True})

        resp = self._post_finalize(doc_id)
        self.assertEqual(resp.status_code, 500)

        doc = Document.objects.get(id=doc_id)
        # Matches legacy upload_complete enqueue-failure behavior: the files are
        # uploaded/finalized, so upload_status stays UPLOADED; only the worker enqueue
        # failed, so processing_state_user becomes FAILED and upload_error records it.
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)
        self.assertIn("enqueue failed", doc.upload_error or "")

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_finalize_idempotent_retry_enqueues_once(self, mock_enqueue, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        self._post_part_complete(doc_id, 0, {"success": True})
        self._post_part_complete(doc_id, 1, {"success": True})

        first = self._post_finalize(doc_id)
        second = self._post_finalize(doc_id)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mock_enqueue.assert_called_once_with(document_id=doc_id)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_legacy_complete_rejects_multi_image_document(self, _mock_put):
        create_resp = self._post_create(self._multi_files_payload(count=2))
        doc_id = create_resp.json()["document_id"]

        resp = self._post_complete(doc_id, {"success": True})
        self.assertEqual(resp.status_code, 400)


class UploadApiCsrfTests(TestCase):
    """Upload JSON endpoints use session auth + Django CSRF (same as other admin POSTs)."""

    def setUp(self):
        from django.contrib.auth.models import User
        from django.test import Client

        from documents.s3 import S3HeadObjectResult

        self.s3_head_patcher = patch(
            "documents.views.head_s3_object",
            return_value=S3HeadObjectResult(exists=True, content_type="image/jpeg"),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)

        self.staff = User.objects.create_user(
            username="upload_csrf_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="upload_csrf_viewer",
            password="test-pass",
            is_staff=False,
        )
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _csrf_token_for_user(self, user):
        self.csrf_client.force_login(user)
        resp = self.csrf_client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        return resp.cookies["csrftoken"].value

    def _post_json(self, url, payload, *, csrf_token=None, user=None):
        if user is not None:
            self.csrf_client.force_login(user)
        kwargs = {}
        if csrf_token is not None:
            kwargs["HTTP_X_CSRFTOKEN"] = csrf_token
        return self.csrf_client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            **kwargs,
        )

    def _single_create_payload(self):
        return {
            "title": "Upload CSRF test",
            "doc_type": "IMAGE",
            "text_input_type": "HANDWRITTEN",
            "original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        }

    def test_unauthenticated_create_without_csrf_is_rejected(self):
        resp = self._post_json("/api/uploads/create/", self._single_create_payload())
        self.assertEqual(resp.status_code, 403)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_authenticated_create_without_csrf_is_rejected(self, _mock_put):
        self.csrf_client.force_login(self.staff)
        resp = self._post_json("/api/uploads/create/", self._single_create_payload())
        self.assertEqual(resp.status_code, 403)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_authenticated_create_with_csrf_succeeds(self, _mock_put):
        token = self._csrf_token_for_user(self.staff)
        resp = self._post_json(
            "/api/uploads/create/",
            self._single_create_payload(),
            csrf_token=token,
            user=self.staff,
        )
        self.assertEqual(resp.status_code, 201)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_non_staff_create_forbidden_even_with_csrf(self, _mock_put):
        token = self._csrf_token_for_user(self.viewer)
        resp = self._post_json(
            "/api/uploads/create/",
            self._single_create_payload(),
            csrf_token=token,
            user=self.viewer,
        )
        self.assertEqual(resp.status_code, 403)

    @patch("documents.views.send_process_document_message")
    def test_complete_without_csrf_is_rejected(self, _mock_enqueue):
        doc = create_ocr_document(
            title="CSRF complete test",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key="documents/1/original.jpg",
            file_original_name="original.jpg",
        )
        self.csrf_client.force_login(self.staff)
        resp = self._post_json(
            f"/api/uploads/{doc.id}/complete/",
            {"success": True, "file_size": 100, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 403)
        _mock_enqueue.assert_not_called()

    @patch("documents.views.send_process_document_message")
    def test_complete_with_csrf_succeeds(self, _mock_enqueue):
        doc = create_ocr_document(
            title="CSRF complete test",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADING,
            file_s3_key="documents/1/original.jpg",
            file_original_name="original.jpg",
        )
        token = self._csrf_token_for_user(self.staff)
        resp = self._post_json(
            f"/api/uploads/{doc.id}/complete/",
            {"success": True, "file_size": 100, "file_mime": "image/jpeg"},
            csrf_token=token,
            user=self.staff,
        )
        self.assertEqual(resp.status_code, 200)
        _mock_enqueue.assert_called_once_with(document_id=doc.id)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_part_complete_without_csrf_is_rejected(self, _mock_put):
        create_token = self._csrf_token_for_user(self.staff)
        create_resp = self._post_json(
            "/api/uploads/create/",
            {
                "title": "Multi CSRF test",
                "text_input_type": "HANDWRITTEN",
                "files": [
                    {
                        "original_name": "page-1.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 1000,
                    },
                    {
                        "original_name": "page-2.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 1001,
                    },
                ],
            },
            csrf_token=create_token,
            user=self.staff,
        )
        doc_id = create_resp.json()["document_id"]

        self.csrf_client.force_login(self.staff)
        resp = self._post_json(
            f"/api/uploads/{doc_id}/parts/0/complete/",
            {"success": True, "file_size": 1000, "file_mime": "image/jpeg"},
        )
        self.assertEqual(resp.status_code, 403)

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    @patch("documents.views.send_process_document_message")
    def test_part_complete_and_finalize_with_csrf_succeed(
        self, _mock_enqueue, _mock_put
    ):
        create_token = self._csrf_token_for_user(self.staff)
        create_resp = self._post_json(
            "/api/uploads/create/",
            {
                "title": "Multi CSRF test",
                "text_input_type": "HANDWRITTEN",
                "files": [
                    {
                        "original_name": "page-1.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 1000,
                    },
                    {
                        "original_name": "page-2.jpg",
                        "mime_type": "image/jpeg",
                        "size_bytes": 1001,
                    },
                ],
            },
            csrf_token=create_token,
            user=self.staff,
        )
        doc_id = create_resp.json()["document_id"]
        token = self._csrf_token_for_user(self.staff)

        part0 = self._post_json(
            f"/api/uploads/{doc_id}/parts/0/complete/",
            {"success": True, "file_size": 1000, "file_mime": "image/jpeg"},
            csrf_token=token,
            user=self.staff,
        )
        part1 = self._post_json(
            f"/api/uploads/{doc_id}/parts/1/complete/",
            {"success": True, "file_size": 1001, "file_mime": "image/jpeg"},
            csrf_token=token,
            user=self.staff,
        )
        finalize = self._post_json(
            f"/api/uploads/{doc_id}/finalize/",
            {"success": True},
            csrf_token=token,
            user=self.staff,
        )

        self.assertEqual(part0.status_code, 200)
        self.assertEqual(part1.status_code, 200)
        self.assertEqual(finalize.status_code, 200)
        _mock_enqueue.assert_called_once_with(document_id=doc_id)


class TranskribusRunPersistenceServiceTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Persistence test doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_start_run_creates_started_row(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = trp.start_run(
            document_id=doc.id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        self.assertEqual(run.status, TranskribusRun.Status.STARTED)
        self.assertIsNone(run.remote_doc_id)

    def test_mark_uploaded_sets_remote_and_upload_fields(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = trp.start_run(
            document_id=doc.id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        run = trp.mark_uploaded(
            run,
            remote_doc_id="999",
            upload_id=555,
            ingest_job_id="ingest-1",
            pages_query="1-2",
            page_index_to_page_nr={0: 1, 1: 2},
        )
        self.assertEqual(run.status, TranskribusRun.Status.UPLOADED)
        self.assertEqual(run.remote_doc_id, "999")
        self.assertEqual(run.upload_id, 555)
        self.assertEqual(run.ingest_job_id, "ingest-1")
        self.assertEqual(run.pages_query, "1-2")
        self.assertEqual(run.page_index_to_page_nr, {0: 1, 1: 2})

    def test_mark_recognition_started_sets_job_id_and_status(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = trp.start_run(
            document_id=doc.id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        run = trp.mark_recognition_started(run, recognition_job_id="recog-77")
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertEqual(run.recognition_job_id, "recog-77")

    def test_mark_succeeded_sets_status_and_engine_runtime(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = trp.start_run(
            document_id=doc.id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        run = trp.mark_succeeded(run, engine_runtime="transkribus-pylaia:564149")
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(run.engine_runtime, "transkribus-pylaia:564149")
        self.assertIsNone(run.error_code)

    def test_mark_failed_preserves_remote_doc_id_and_sanitizes_error_details(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = trp.start_run(
            document_id=doc.id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="42",
            model_id="564149",
        )
        run = trp.mark_uploaded(
            run,
            remote_doc_id="888",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        run = trp.mark_recognition_started(run, recognition_job_id="recog-1")
        run = trp.mark_failed(
            run,
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
            error_details="password=secret transcript fetch failed",
        )
        self.assertEqual(run.status, TranskribusRun.Status.FAILED)
        self.assertEqual(run.remote_doc_id, "888")
        self.assertEqual(run.recognition_job_id, "recog-1")
        self.assertEqual(run.error_code, "TRANSKRIBUS_RECOGNITION_FAILED")
        self.assertEqual(run.error_details, "Transkribus attempt failed.")


def _transkribus_adapter_worker_env(**overrides):
    from documents.services.env_validation import WorkerEnvConfig

    base = dict(
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
        transkribus_api_token="tok",
        transkribus_username="u",
        transkribus_password="p",
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=2048,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.7,
        transkribus_collection_id="col",
        transkribus_model_id="42",
        transkribus_force_reprocess=False,
        transkribus_recognition_only_retry=False,
    )
    base.update(overrides)
    return WorkerEnvConfig(**base)


class TranskribusAdapterPersistenceTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Adapter persistence doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_dev_upload_success_persists_succeeded_run(
        self, m_login, m_upload, m_start, m_complete
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="777",
            upload_id=10,
            ingest_job_id="ingest-9",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-9"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="text",
            review_reasons=[],
            recognition_job_id="recog-9",
        )
        adapter = TranskribusAdapter()
        result = adapter.execute(
            pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(transkribus_dev_upload_mode=True),
            document_id=doc.id,
        )
        self.assertEqual(result.engine_name, "transkribus-pylaia:42")
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(run.mode, TranskribusRun.Mode.UPLOAD_CREATED)
        self.assertEqual(run.remote_doc_id, "777")
        self.assertEqual(run.upload_id, 10)
        self.assertEqual(run.ingest_job_id, "ingest-9")
        self.assertEqual(run.recognition_job_id, "recog-9")
        self.assertEqual(run.engine_runtime, "transkribus-pylaia:42")

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_dev_upload_ingest_failure_marks_failed_without_remote_doc_id(
        self, m_login, m_upload
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import TranskribusPermanentError

        doc = self._create_document()
        m_upload.side_effect = TranskribusPermanentError("ingest failed")
        adapter = TranskribusAdapter()
        with self.assertRaises(EnginePermanentError):
            adapter.execute(
                pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_transkribus_adapter_worker_env(transkribus_dev_upload_mode=True),
                document_id=doc.id,
            )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.FAILED)
        self.assertIsNone(run.remote_doc_id)
        self.assertEqual(run.error_code, "TRANSKRIBUS_UPLOAD_FAILED")

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_dev_upload_recognition_failure_preserves_remote_doc_id(
        self, m_login, m_upload, m_start, m_complete
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import (
            TranskribusPermanentError,
            TrpUploadOutcome,
        )

        doc = self._create_document()
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="555",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-fail"
        m_complete.side_effect = TranskribusPermanentError("transcript fetch failed")
        adapter = TranskribusAdapter()
        with self.assertRaises(EnginePermanentError):
            adapter.execute(
                pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_transkribus_adapter_worker_env(transkribus_dev_upload_mode=True),
                document_id=doc.id,
            )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.FAILED)
        self.assertEqual(run.remote_doc_id, "555")
        self.assertEqual(run.recognition_job_id, "recog-fail")
        self.assertEqual(run.error_code, "TRANSKRIBUS_RECOGNITION_FAILED")

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_existing_server_success_persists_without_upload_fields(
        self, m_login, m_start, m_complete
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        m_start.return_value = "recog-es"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="es text",
            review_reasons=[],
            recognition_job_id="recog-es",
        )
        adapter = TranskribusAdapter()
        adapter.execute(
            pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(
                transkribus_use_existing_server_document=True,
                transkribus_dev_existing_document_id="99",
                transkribus_dev_existing_pages="1-2",
            ),
            document_id=doc.id,
        )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(run.mode, TranskribusRun.Mode.EXISTING_SERVER)
        self.assertEqual(run.remote_doc_id, "99")
        self.assertEqual(run.pages_query, "1-2")
        self.assertIsNone(run.upload_id)
        self.assertIsNone(run.ingest_job_id)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_existing_server_failure_marks_failed(self, m_login, m_start):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import TranskribusPermanentError

        doc = self._create_document()
        m_start.side_effect = TranskribusPermanentError("recognition start failed")
        adapter = TranskribusAdapter()
        with self.assertRaises(EnginePermanentError):
            adapter.execute(
                pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_transkribus_adapter_worker_env(
                    transkribus_use_existing_server_document=True,
                    transkribus_dev_existing_document_id="99",
                    transkribus_dev_existing_pages="1",
                ),
                document_id=doc.id,
            )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.FAILED)
        self.assertEqual(run.remote_doc_id, "99")


class TranskribusWorkdirRetryAdapterTests(TestCase):
    """Adapter dev-upload recovers from the transient PyLaia workdir failure via retry."""

    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Workdir retry doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def _upload_outcome(self):
        from documents.services.transkribus_engine import TrpUploadOutcome

        return TrpUploadOutcome(
            collection_id="col",
            doc_id="16537736",
            upload_id=10,
            ingest_job_id="ingest-1",
            pages_query="1-4",
            page_index_to_page_nr={1: 1, 2: 2, 3: 3, 4: 4},
        )

    @staticmethod
    def _workdir_exc():
        from documents.services.transkribus_engine import TranskribusRetryableError

        return TranskribusRetryableError(
            "Transkribus job j1 failed: Could not create workdir at: "
            "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_j1"
        )

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_dev_upload_recovers_workdir_failure_without_new_upload(
        self, m_login, m_upload, m_start, m_complete, m_sleep
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        m_upload.return_value = self._upload_outcome()
        m_start.side_effect = ["recog-1", "recog-2"]
        m_complete.side_effect = [
            self._workdir_exc(),
            PylaiaTranscriptionOutcome(
                text="recovered text", review_reasons=[], recognition_job_id="recog-2"
            ),
        ]

        adapter = TranskribusAdapter()
        result = adapter.execute(
            pages=[
                PageImage(page_index=i, image_bytes=b"x", mime_type="image/png")
                for i in (1, 2, 3, 4)
            ],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            # max_retries=2 = deployed .env default → 2 recognition attempts.
            worker_env=_transkribus_adapter_worker_env(
                transkribus_dev_upload_mode=True, max_retries=2
            ),
            document_id=doc.id,
        )

        self.assertEqual(result.text, "recovered text")
        self.assertEqual(result.engine_name, "transkribus-pylaia:42")
        # Single upload; recognition retried once on the same remote doc.
        m_upload.assert_called_once()
        self.assertEqual(m_start.call_count, 2)
        runs = list(TranskribusRun.objects.filter(document=doc))
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(run.remote_doc_id, "16537736")
        self.assertEqual(run.recognition_job_id, "recog-2")

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_dev_upload_workdir_failure_after_budget_raises_retryable_and_marks_failed(
        self, m_login, m_upload, m_start, m_complete, m_sleep
    ):
        from documents.services.htr_adapters.base import EngineRetryableError
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        m_upload.return_value = self._upload_outcome()
        # max_retries=2 = deployed .env default → exactly 2 recognition attempts.
        m_start.side_effect = ["recog-1", "recog-2"]
        m_complete.side_effect = [self._workdir_exc() for _ in range(2)]

        adapter = TranskribusAdapter()
        with self.assertRaises(EngineRetryableError):
            adapter.execute(
                pages=[
                    PageImage(page_index=i, image_bytes=b"x", mime_type="image/png")
                    for i in (1, 2, 3, 4)
                ],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_transkribus_adapter_worker_env(
                    transkribus_dev_upload_mode=True, max_retries=2
                ),
                document_id=doc.id,
            )

        self.assertEqual(m_start.call_count, 2)
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.FAILED)
        self.assertEqual(run.error_code, "TRANSKRIBUS_RECOGNITION_FAILED")


class TranskribusWorkdirRetryWorkerTests(TestCase):
    """
    End-to-end worker persistence for the transient PyLaia workdir failure (Transkribus
    HTTP + S3 + page extraction mocked; route selection, adapter/engine retry, and
    DocumentTextResult persistence are real).

    Budget-exhausted FAILED persistence and no-Gemini-fallback on Transkribus failure are
    already covered by ``TranskribusWorkdirRetryAdapterTests`` (adapter raises after the
    budget) plus the existing ``RunWorkerBehaviorTests`` Transkribus-failure tests, so only
    the recovery path needs this heavier integration test.
    """

    def _make_worker_command(self):
        cmd = Command()
        # max_retries=2 = deployed .env default → 2 recognition attempts.
        cmd._cfg = _transkribus_adapter_worker_env(
            transkribus_dev_upload_mode=True, max_retries=2
        )
        return cmd

    def _he_doc(self):
        return create_ocr_document(
            title="Hebrew workdir retry",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="he-workdir.pdf",
            mime_type="application/pdf",
        )

    @patch("documents.services.transkribus_engine.time.sleep")
    @patch("documents.services.transkribus_engine.complete_pylaia_transcription_after_job")
    @patch("documents.services.transkribus_engine.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_worker_recovers_workdir_failure_persists_needs_review_not_failed(
        self,
        m_get_object_bytes,
        m_extract_pages,
        m_login,
        m_upload,
        m_start,
        m_complete,
        m_sleep,
    ):
        from documents.services.page_extraction import PageImage
        from documents.services.transkribus_engine import TranskribusRetryableError, TrpUploadOutcome

        m_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        m_extract_pages.return_value = [
            PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
        ]
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="16539496",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_start.side_effect = ["recog-1", "recog-2"]
        m_complete.side_effect = [
            TranskribusRetryableError(
                "Transkribus job recog-1 failed: Could not create workdir at: "
                "/tmp/HTR/PyLaia/trpProd/Decode/pylaiaDecode_recog-1"
            ),
            PylaiaTranscriptionOutcome(
                text="worker recovered text", review_reasons=[], recognition_job_id="recog-2"
            ),
        ]

        cmd = self._make_worker_command()
        he_doc = self._he_doc()
        msg = {"Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": he_doc.id})}

        with patch.dict(
            os.environ, {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"}, clear=False
        ):
            self.assertTrue(cmd._process_message(msg))

        # No terminal FAILED text rows; recovered text persisted as NEEDS_REVIEW.
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=he_doc, status=DocumentTextResult.Status.FAILED
            ).exists()
        )
        row = DocumentTextResult.objects.get(
            document=he_doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
        )
        self.assertEqual(row.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(row.text, "worker recovered text")
        self.assertEqual(row.engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS)
        # Recovery stays on the Transkribus route; no Gemini fallback row.
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=he_doc, engine_key=DocumentTextResult.OcrEngineKey.GEMINI
            ).exists()
        )


class TranskribusRunPersistenceGuardTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Guard persistence doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_find_blocking_returns_none_when_no_runs(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        self.assertIsNone(
            trp.find_blocking_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_blocking_returns_older_succeeded_when_newer_failed_has_no_remote_doc_id(
        self,
    ):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        succeeded = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="100",
        )
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
        )
        blocking = trp.find_blocking_upload_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
        )
        self.assertIsNotNone(blocking)
        self.assertEqual(blocking.id, succeeded.id)

    def test_find_blocking_failed_with_blank_remote_doc_id_is_not_blocking(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="   ",
        )
        self.assertIsNone(
            trp.find_blocking_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )


class TranskribusRunPersistenceReusableRunTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Reusable run doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def test_find_reusable_returns_none_when_no_runs(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_reusable_returns_failed_with_remote_doc_id_and_pages_query(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
        )
        found = trp.find_reusable_upload_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found.id, run.id)

    def test_find_reusable_failed_without_remote_doc_id_returns_none(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
        )
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_reusable_failed_without_pages_query_returns_none(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query=None,
        )
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_reusable_returns_uploaded_with_remote_doc_id_and_pages_query(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.UPLOADED,
            remote_doc_id="111",
            pages_query="1-2",
        )
        found = trp.find_reusable_upload_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
        )
        self.assertEqual(found.id, run.id)

    def test_find_reusable_returns_recognition_started_with_remote_doc_id(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        run = TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            remote_doc_id="222",
            pages_query="1",
            recognition_job_id="recog-old",
        )
        found = trp.find_reusable_upload_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
        )
        self.assertEqual(found.id, run.id)

    def test_find_reusable_succeeded_with_remote_doc_id_returns_none(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
            pages_query="1",
        )
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_reusable_collection_mismatch_returns_none(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="other-col",
            model_id="42",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
        )
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )

    def test_find_reusable_model_mismatch_returns_none(self):
        from documents.services import transkribus_run_persistence as trp

        doc = self._create_document()
        TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="99",
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
        )
        self.assertIsNone(
            trp.find_reusable_upload_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
            )
        )


class TranskribusUploadDuplicateGuardTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Duplicate guard doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def _seed_upload_run(
        self,
        doc: Document,
        *,
        status: str,
        remote_doc_id: str | None = None,
        collection_id: str = "col",
        model_id: str = "42",
    ) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id=collection_id,
            model_id=model_id,
            status=status,
            remote_doc_id=remote_doc_id,
        )

    def _execute_dev_upload(
        self,
        doc: Document,
        *,
        worker_env_overrides: dict | None = None,
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        overrides = {"transkribus_dev_upload_mode": True}
        if worker_env_overrides:
            overrides.update(worker_env_overrides)
        adapter = TranskribusAdapter()
        return adapter.execute(
            pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(**overrides),
            document_id=doc.id,
        )

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_succeeded_blocks_before_upload(self, m_login, m_upload):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
        )
        run_count_before = TranskribusRun.objects.filter(document=doc).count()
        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(doc)
        self.assertIn("Transkribus upload blocked", str(ctx.exception))
        self.assertIn("TRANSKRIBUS_FORCE_REPROCESS", str(ctx.exception))
        m_upload.assert_not_called()
        m_login.assert_not_called()
        self.assertEqual(
            TranskribusRun.objects.filter(document=doc).count(),
            run_count_before,
        )

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_failed_with_remote_doc_id_blocks(self, m_login, m_upload):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
        )
        with self.assertRaises(EnginePermanentError):
            self._execute_dev_upload(doc)
        m_upload.assert_not_called()
        m_login.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_failed_without_remote_doc_id_allows_upload(
        self, m_login, m_upload, m_start, m_complete
    ):
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
        )
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="888",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="ok",
            review_reasons=[],
            recognition_job_id="recog-1",
        )
        self._execute_dev_upload(doc)
        m_upload.assert_called_once()
        self.assertEqual(TranskribusRun.objects.filter(document=doc).count(), 2)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_started_blocks(self, m_login, m_upload):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_upload_run(doc, status=TranskribusRun.Status.STARTED)
        with self.assertRaises(EnginePermanentError):
            self._execute_dev_upload(doc)
        m_upload.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_uploaded_blocks(self, m_login, m_upload):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.UPLOADED,
            remote_doc_id="111",
        )
        with self.assertRaises(EnginePermanentError):
            self._execute_dev_upload(doc)
        m_upload.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_prior_recognition_started_blocks(self, m_login, m_upload):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            remote_doc_id="222",
        )
        with self.assertRaises(EnginePermanentError):
            self._execute_dev_upload(doc)
        m_upload.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_different_collection_id_does_not_block(self, m_login, m_upload, m_start, m_complete):
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
            collection_id="other-col",
        )
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="888",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="ok",
            review_reasons=[],
            recognition_job_id="recog-1",
        )
        self._execute_dev_upload(doc)
        m_upload.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_different_model_id_does_not_block(self, m_login, m_upload, m_start, m_complete):
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
            model_id="99",
        )
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="888",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-1"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="ok",
            review_reasons=[],
            recognition_job_id="recog-1",
        )
        self._execute_dev_upload(doc)
        m_upload.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_force_reprocess_bypasses_guard(self, m_login, m_upload, m_start, m_complete):
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
        )
        m_upload.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="999",
            upload_id=2,
            ingest_job_id="ingest-2",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-2"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="forced",
            review_reasons=[],
            recognition_job_id="recog-2",
        )
        self._execute_dev_upload(
            doc,
            worker_env_overrides={"transkribus_force_reprocess": True},
        )
        m_upload.assert_called_once()
        self.assertEqual(TranskribusRun.objects.filter(document=doc).count(), 2)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_existing_server_mode_not_blocked_by_prior_upload_run(
        self, m_login, m_start, m_complete
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        self._seed_upload_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
        )
        m_start.return_value = "recog-es"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="es text",
            review_reasons=[],
            recognition_job_id="recog-es",
        )
        adapter = TranskribusAdapter()
        adapter.execute(
            pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(
                transkribus_use_existing_server_document=True,
                transkribus_dev_existing_document_id="99",
                transkribus_dev_existing_pages="1",
            ),
            document_id=doc.id,
        )
        upload_runs = TranskribusRun.objects.filter(
            document=doc, mode=TranskribusRun.Mode.UPLOAD_CREATED
        ).count()
        self.assertEqual(upload_runs, 1)
        self.assertTrue(
            TranskribusRun.objects.filter(
                document=doc, mode=TranskribusRun.Mode.EXISTING_SERVER
            ).exists()
        )


class TranskribusRecognitionOnlyRetryTests(TestCase):
    def _create_document(self) -> Document:
        return create_ocr_document(
            title="Recognition-only retry doc",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def _seed_source_run(
        self,
        doc: Document,
        *,
        status: str,
        remote_doc_id: str = "555",
        pages_query: str = "1",
        page_index_to_page_nr: dict | None = None,
        upload_id: int = 10,
        ingest_job_id: str = "ingest-1",
    ) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=doc,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            status=status,
            remote_doc_id=remote_doc_id,
            pages_query=pages_query,
            page_index_to_page_nr=page_index_to_page_nr if page_index_to_page_nr is not None else {0: 1},
            upload_id=upload_id,
            ingest_job_id=ingest_job_id,
        )

    def _execute_dev_upload(
        self,
        doc: Document,
        *,
        pages: list | None = None,
        worker_env_overrides: dict | None = None,
        source_transkribus_run_id: int | None = None,
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        if pages is None:
            pages = [PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")]
        overrides = {"transkribus_dev_upload_mode": True}
        if worker_env_overrides:
            overrides.update(worker_env_overrides)
        adapter = TranskribusAdapter()
        execute_kwargs = {
            "pages": pages,
            "language_hint": "he",
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "worker_env": _transkribus_adapter_worker_env(**overrides),
            "document_id": doc.id,
        }
        if source_transkribus_run_id is not None:
            execute_kwargs["source_transkribus_run_id"] = source_transkribus_run_id
        return adapter.execute(**execute_kwargs)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_explicit_source_run_id_uses_exact_run_not_rediscovery(
        self, m_login, m_upload, m_start, m_complete
    ):
        doc = self._create_document()
        older = self._seed_source_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="111",
            pages_query="1",
            upload_id=10,
            ingest_job_id="ingest-old",
        )
        newer = self._seed_source_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="222",
            pages_query="1",
            upload_id=11,
            ingest_job_id="ingest-new",
        )
        self.assertLess(older.id, newer.id)
        m_start.return_value = "recog-explicit"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="from explicit source run",
            review_reasons=[],
            recognition_job_id="recog-explicit",
        )

        result = self._execute_dev_upload(
            doc,
            worker_env_overrides={"transkribus_recognition_only_retry": True},
            source_transkribus_run_id=older.id,
        )

        m_upload.assert_not_called()
        m_start.assert_called_once()
        self.assertEqual(result.text, "from explicit source run")
        new_run = (
            TranskribusRun.objects.filter(document=doc)
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertEqual(new_run.remote_doc_id, "111")
        self.assertEqual(new_run.upload_id, 10)
        self.assertEqual(new_run.ingest_job_id, "ingest-old")
        older.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(older.status, TranskribusRun.Status.FAILED)
        self.assertEqual(newer.status, TranskribusRun.Status.FAILED)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_explicit_source_run_id_wrong_document_rejected(
        self, m_login, m_upload
    ):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        other_doc = self._create_document()
        other_run = self._seed_source_run(
            other_doc,
            status=TranskribusRun.Status.FAILED,
        )

        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                worker_env_overrides={"transkribus_recognition_only_retry": True},
                source_transkribus_run_id=other_run.id,
            )

        self.assertIn(f"document_id={other_doc.id}", str(ctx.exception))
        self.assertIn(f"document_id={doc.id}", str(ctx.exception))
        m_upload.assert_not_called()
        m_login.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_explicit_source_run_id_succeeded_run_rejected(
        self, m_login, m_upload
    ):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        succeeded = self._seed_source_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
        )

        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                worker_env_overrides={"transkribus_recognition_only_retry": True},
                source_transkribus_run_id=succeeded.id,
            )

        self.assertIn("not reusable for recognition-only retry", str(ctx.exception))
        self.assertIn(f"TranskribusRun id={succeeded.id}", str(ctx.exception))
        m_upload.assert_not_called()
        m_login.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_flag_off_prior_failed_with_remote_doc_id_still_blocks(
        self, m_login, m_upload
    ):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.FAILED)
        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                worker_env_overrides={"transkribus_recognition_only_retry": False},
            )
        self.assertIn("Transkribus upload blocked", str(ctx.exception))
        m_upload.assert_not_called()
        m_login.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_flag_on_reusable_failed_run_skips_upload_and_returns_htr_result(
        self, m_login, m_upload, m_start, m_complete
    ):
        doc = self._create_document()
        source = self._seed_source_run(doc, status=TranskribusRun.Status.FAILED)
        m_start.return_value = "recog-retry"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="recovered text",
            review_reasons=[],
            recognition_job_id="recog-retry",
        )
        run_count_before = TranskribusRun.objects.filter(document=doc).count()
        result = self._execute_dev_upload(
            doc,
            worker_env_overrides={"transkribus_recognition_only_retry": True},
        )
        m_upload.assert_not_called()
        m_start.assert_called_once()
        self.assertEqual(result.text, "recovered text")
        self.assertEqual(result.engine_name, "transkribus-pylaia:42")
        self.assertEqual(
            TranskribusRun.objects.filter(document=doc).count(),
            run_count_before + 1,
        )
        new_run = (
            TranskribusRun.objects.filter(document=doc)
            .order_by("-created_at", "-id")
            .first()
        )
        self.assertNotEqual(new_run.id, source.id)
        self.assertEqual(new_run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(new_run.remote_doc_id, "555")
        self.assertEqual(new_run.pages_query, "1")
        self.assertEqual(new_run.upload_id, 10)
        self.assertEqual(new_run.ingest_job_id, "ingest-1")
        source.refresh_from_db()
        self.assertEqual(source.status, TranskribusRun.Status.FAILED)

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_flag_on_reusable_uploaded_run_works(self, m_login, m_upload, m_start, m_complete):
        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.UPLOADED)
        m_start.return_value = "recog-u"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="from uploaded",
            review_reasons=[],
            recognition_job_id="recog-u",
        )
        self._execute_dev_upload(
            doc,
            worker_env_overrides={"transkribus_recognition_only_retry": True},
        )
        m_upload.assert_not_called()
        m_start.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_flag_on_reusable_recognition_started_run_works(
        self, m_login, m_upload, m_start, m_complete
    ):
        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.RECOGNITION_STARTED)
        m_start.return_value = "recog-rs"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="from recognition started",
            review_reasons=[],
            recognition_job_id="recog-rs",
        )
        self._execute_dev_upload(
            doc,
            worker_env_overrides={"transkribus_recognition_only_retry": True},
        )
        m_upload.assert_not_called()
        m_start.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_flag_on_prior_succeeded_does_not_trigger_recognition_only_blocks(
        self, m_login, m_upload
    ):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.SUCCEEDED)
        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                worker_env_overrides={"transkribus_recognition_only_retry": True},
            )
        self.assertIn("Transkribus upload blocked", str(ctx.exception))
        m_upload.assert_not_called()
        m_login.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_force_reprocess_wins_over_recognition_only_retry(
        self, m_login, m_upload_ingest, m_start, m_complete
    ):
        from documents.services.transkribus_engine import TrpUploadOutcome

        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.FAILED)
        m_upload_ingest.return_value = TrpUploadOutcome(
            collection_id="col",
            doc_id="999",
            upload_id=2,
            ingest_job_id="ingest-2",
            pages_query="1",
            page_index_to_page_nr={0: 1},
        )
        m_start.return_value = "recog-force"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="forced upload",
            review_reasons=[],
            recognition_job_id="recog-force",
        )
        self._execute_dev_upload(
            doc,
            worker_env_overrides={
                "transkribus_recognition_only_retry": True,
                "transkribus_force_reprocess": True,
            },
        )
        m_upload_ingest.assert_called_once()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_verified_document_text_result_blocks_before_http(
        self, m_login, m_start
    ):
        from documents.services.htr_adapters.base import EnginePermanentError

        doc = self._create_document()
        self._seed_source_run(doc, status=TranskribusRun.Status.FAILED)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified ground truth",
        )
        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                worker_env_overrides={"transkribus_recognition_only_retry": True},
            )
        self.assertIn("VERIFIED", str(ctx.exception))
        m_login.assert_not_called()
        m_start.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_page_count_mismatch_blocks_before_http(self, m_login, m_start):
        from documents.services.htr_adapters.base import EnginePermanentError
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        self._seed_source_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            page_index_to_page_nr={0: 1, 1: 2},
        )
        two_pages = [
            PageImage(page_index=0, image_bytes=b"a", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"b", mime_type="image/png"),
            PageImage(page_index=2, image_bytes=b"c", mime_type="image/png"),
        ]
        with self.assertRaises(EnginePermanentError) as ctx:
            self._execute_dev_upload(
                doc,
                pages=two_pages,
                worker_env_overrides={"transkribus_recognition_only_retry": True},
            )
        self.assertIn("page mapping count", str(ctx.exception))
        m_login.assert_not_called()
        m_start.assert_not_called()

    @patch("documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition")
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_existing_server_mode_unchanged_by_recognition_only_flag(
        self, m_login, m_start, m_complete
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        doc = self._create_document()
        self._seed_source_run(
            doc,
            status=TranskribusRun.Status.SUCCEEDED,
            remote_doc_id="777",
        )
        m_start.return_value = "recog-es"
        m_complete.return_value = PylaiaTranscriptionOutcome(
            text="es text",
            review_reasons=[],
            recognition_job_id="recog-es",
        )
        adapter = TranskribusAdapter()
        adapter.execute(
            pages=[PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(
                transkribus_use_existing_server_document=True,
                transkribus_dev_existing_document_id="99",
                transkribus_dev_existing_pages="1",
                transkribus_recognition_only_retry=True,
            ),
            document_id=doc.id,
        )
        upload_runs = TranskribusRun.objects.filter(
            document=doc, mode=TranskribusRun.Mode.UPLOAD_CREATED
        ).count()
        self.assertEqual(upload_runs, 1)
        self.assertTrue(
            TranskribusRun.objects.filter(
                document=doc, mode=TranskribusRun.Mode.EXISTING_SERVER
            ).exists()
        )


# These tests intentionally inspect internal prompt constants to guard
# archival OCR behavior without adding production-only accessors.
class GeminiEnginePromptTests(SimpleTestCase):
    def test_printed_prompt_includes_archival_transcription_guardrails(self):
        guardrails = (
            "Do NOT silently omit visible words",
            "Preserve typos",
            "Do NOT add Hebrew vowel marks",
            "Do NOT omit readable URLs",
            "Ignore purely decorative UI icons",
        )
        for phrase in guardrails:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, _PRINTED_TEXT_PROMPT)

    def test_handwritten_prompt_unchanged(self):
        self.assertIn("expert paleographer and historian", _HTR_EXPERT_PROMPT)
        self.assertIn("This is handwritten text", _HTR_EXPERT_PROMPT)
        self.assertNotIn("Do NOT silently omit visible words", _HTR_EXPERT_PROMPT)


class GeminiModelCandidatesTests(SimpleTestCase):
    def test_hebrew_printed_gemini_route_uses_configured_single_model(self):
        route = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        candidates = gemini_model_candidates(
            route,
            language="he",
            text_input_type=Document.TextInputType.PRINTED,
            gemini_hebrew_printed_model="gemini-3.1-flash-lite",
        )
        self.assertEqual(candidates, ("gemini-3.1-flash-lite",))

    def test_hebrew_printed_gemini_route_respects_env_override_model(self):
        route = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        candidates = gemini_model_candidates(
            route,
            language="he",
            text_input_type=Document.TextInputType.PRINTED,
            gemini_hebrew_printed_model="custom-model",
        )
        self.assertEqual(candidates, ("custom-model",))

    def test_non_hebrew_printed_gemini_route_keeps_default_candidates(self):
        route = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        candidates = gemini_model_candidates(
            route,
            language="en",
            text_input_type=Document.TextInputType.PRINTED,
            gemini_hebrew_printed_model="gemini-3.1-flash-lite",
        )
        self.assertEqual(candidates, DEFAULT_GEMINI_MODEL_CANDIDATES)


class OcrRoutingTranskribusHebrewHandwrittenTests(SimpleTestCase):
    """Hebrew handwritten routing policy; no live Transkribus."""

    def test_ocr_routes_table_keeps_hebrew_handwritten_out_of_gemini_table(self):
        self.assertNotIn(
            (Document.Language.HEBREW, Document.TextInputType.HANDWRITTEN),
            OCR_ROUTES,
        )
        for cfg in OCR_ROUTES.values():
            self.assertEqual(cfg.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)

    def test_flag_on_he_handwritten_returns_transkribus(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true",
            },
            clear=False,
        ):
            route = select_ocr_route("he", Document.TextInputType.HANDWRITTEN)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.TRANSKRIBUS)
        self.assertEqual(
            route.prompt_variant, DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        )

    def test_flag_off_he_handwritten_raises_clear_error(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError) as ctx:
                select_ocr_route("he", Document.TextInputType.HANDWRITTEN)
        self.assertIn("ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN", str(ctx.exception))
        self.assertIn("Gemini fallback", str(ctx.exception))

    def test_legacy_dev_ocr_route_flag_does_not_select_hebrew_handwritten_route(self):
        with patch.dict(
            os.environ,
            {
                "TRANSKRIBUS_DEV_OCR_ROUTE": "true",
                "TRANSKRIBUS_DEV_UPLOAD_MODE": "true",
                "TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT": "false",
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError) as ctx:
                select_ocr_route("he", Document.TextInputType.HANDWRITTEN)
        self.assertIn("ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN", str(ctx.exception))

    def test_hebrew_printed_route_remains_gemini(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true",
            },
            clear=False,
        ):
            route = select_ocr_route("he", Document.TextInputType.PRINTED)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            route.prompt_variant, DocumentTextResult.OcrPromptVariant.PRINTED
        )

    def test_non_hebrew_handwritten_route_remains_gemini(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true",
            },
            clear=False,
        ):
            route = select_ocr_route("en", Document.TextInputType.HANDWRITTEN)
        self.assertEqual(route.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)

    def test_invalid_language_unchanged_before_dev_gate(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true",
            },
            clear=False,
        ):
            with self.assertRaises(ValueError) as ctx:
                select_ocr_route(None, Document.TextInputType.HANDWRITTEN)
        self.assertIn("language", str(ctx.exception).lower())


class ReviewBacklogServiceTests(SimpleTestCase):
    def test_parse_review_reasons_json_list(self):
        from documents.services.review_backlog import parse_review_reasons

        self.assertEqual(
            parse_review_reasons('["A","B"]'),
            ["A", "B"],
        )

    def test_parse_review_reasons_plain_text_fallback(self):
        from documents.services.review_backlog import parse_review_reasons

        self.assertEqual(parse_review_reasons("legacy"), ["legacy"])

    def test_is_review_pending_text_result(self):
        from documents.services.review_backlog import is_review_pending_text_result

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="x",
        )
        self.assertTrue(is_review_pending_text_result(row))

        row.status = DocumentTextResult.Status.SUCCEEDED
        self.assertFalse(is_review_pending_text_result(row))

    def test_is_review_pending_rejects_whitespace_only_text(self):
        from documents.services.review_backlog import is_review_pending_text_result

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="  \n\t  ",
        )
        self.assertFalse(is_review_pending_text_result(row))

    def test_is_review_editable_matches_pending(self):
        from documents.services.review_backlog import (
            is_review_editable_text_result,
            is_review_pending_text_result,
        )

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="x",
        )
        self.assertEqual(
            is_review_editable_text_result(row),
            is_review_pending_text_result(row),
        )
        row.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
        self.assertFalse(is_review_editable_text_result(row))


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class SourcePreviewTests(TestCase):
    """Read-only multi-image source preview on detail + review detail (PR5)."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="preview_staff",
            password="test-pass",
            is_staff=True,
        )

    def _detail_url(self, doc_id: int) -> str:
        return f"/api/ui/documents/{doc_id}/"

    def _review_url(self, doc_id: int) -> str:
        return f"/api/ui/admin/review/{doc_id}/"

    def _single_file_doc(self):
        doc = create_ocr_document(
            title="Legacy single",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/1/original.jpg",
            file_original_name="original.jpg",
            mime_type="image/jpeg",
        )
        # PR2 dual-write leaves a source_files[0] row even for single-file docs.
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=0,
            file_s3_key=doc.file_s3_key,
            file_original_name=doc.file_original_name,
            mime_type=doc.mime_type,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )
        return doc

    def _multi_image_doc(self, *, count=3, expected=None, language=Document.Language.HEBREW):
        doc = create_ocr_document(
            title="Multi image",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=language,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/9/source/0.png",
            file_original_name="page-0.png",
            mime_type="image/png",
            expected_source_file_count=expected if expected is not None else count,
        )
        return doc

    def _add_source(self, doc, order_index, *, upload_status=DocumentSourceFile.UploadStatus.UPLOADED):
        return DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=f"documents/{doc.id}/source/{order_index}.png",
            file_original_name=f"page-{order_index}.png",
            mime_type="image/png",
            upload_status=upload_status,
        )

    # ----- legacy single-file (fallback path unchanged) -----

    @patch("documents.services.source_files.create_presigned_get")
    @patch("documents.views.create_presigned_get", return_value="https://example/legacy")
    def test_legacy_single_file_detail_preview_unchanged(self, mock_view_get, mock_helper_get):
        doc = self._single_file_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "https://example/legacy")
        # legacy single-file documents must NOT use the source_files preview path
        self.assertNotContains(resp, "עמוד 1")
        mock_view_get.assert_called_once_with(
            bucket="test-bucket", key=doc.file_s3_key, expires_in=3600
        )
        mock_helper_get.assert_not_called()

    @patch("documents.services.source_files.create_presigned_get")
    @patch("documents.views.create_presigned_get", return_value="https://example/legacy")
    def test_legacy_single_file_review_detail_preview_unchanged(
        self, mock_view_get, mock_helper_get
    ):
        doc = self._single_file_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(self._review_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "https://example/legacy")
        self.assertNotContains(resp, "עמוד 1")
        mock_view_get.assert_called_once_with(
            bucket="test-bucket", key=doc.file_s3_key, expires_in=3600
        )
        mock_helper_get.assert_not_called()

    def test_single_file_source_files_not_used_avoids_duplicate_first_preview(self):
        doc = self._single_file_doc()
        with patch(
            "documents.views.create_presigned_get", return_value="https://example/legacy"
        ):
            self.client.force_login(self.staff)
            resp = self.client.get(self._detail_url(doc.id))
        body = resp.content.decode()
        # legacy single-file path is used (no source_files preview), so the first file
        # is not previewed twice via both Document.file_s3_key and source_files[0].
        self.assertEqual(resp.context["source_preview_items"], [])
        self.assertNotIn("עמוד 1", body)
        # legacy single <img> still rendered from content_url
        self.assertContains(resp, "https://example/legacy")

    # ----- multi-image preview path -----

    @patch("documents.views.create_presigned_get")
    @patch("documents.services.source_files.create_presigned_get")
    def test_multi_image_detail_shows_ordered_preview_items(
        self, mock_helper_get, mock_view_get
    ):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=3)
        for i in range(3):
            self._add_source(doc, i)
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("עמוד 1", body)
        self.assertIn("עמוד 2", body)
        self.assertIn("עמוד 3", body)
        self.assertIn("page-0.png", body)
        self.assertIn("page-1.png", body)
        self.assertIn("page-2.png", body)
        # ordered by order_index
        self.assertLess(body.index("עמוד 1"), body.index("עמוד 2"))
        self.assertLess(body.index("עמוד 2"), body.index("עמוד 3"))
        # multi-image documents do not generate the legacy single content_url
        mock_view_get.assert_not_called()
        self.assertEqual(mock_helper_get.call_count, 3)

    @patch("documents.views.create_presigned_get")
    @patch("documents.services.source_files.create_presigned_get")
    def test_multi_image_detail_hides_source_filenames_for_viewers(
        self, mock_helper_get, mock_view_get
    ):
        from django.contrib.auth.models import User

        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        viewer = User.objects.create_user(
            username="preview_viewer",
            password="test-pass",
            is_staff=False,
        )
        doc = self._multi_image_doc(count=3, language=Document.Language.HEBREW)
        doc.archive_item.visibility = Document.Visibility.PUBLIC
        doc.archive_item.save(update_fields=["visibility"])
        for i in range(3):
            self._add_source(doc, i)
        self.client.force_login(viewer)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("עמוד 1", body)
        self.assertNotIn("page-0.png", body)
        self.assertNotIn("page-1.png", body)
        self.assertNotIn("page-2.png", body)
        mock_view_get.assert_not_called()

    @patch("documents.views.create_presigned_get")
    @patch("documents.services.source_files.create_presigned_get")
    def test_multi_image_review_detail_shows_ordered_preview_items(
        self, mock_helper_get, mock_view_get
    ):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=3)
        for i in range(3):
            self._add_source(doc, i)
        self.client.force_login(self.staff)
        resp = self.client.get(self._review_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("עמוד 1 — page-0.png", body)
        self.assertIn("עמוד 2 — page-1.png", body)
        self.assertIn("עמוד 3 — page-2.png", body)
        self.assertLess(body.index("עמוד 1"), body.index("עמוד 2"))
        self.assertLess(body.index("עמוד 2"), body.index("עמוד 3"))
        mock_view_get.assert_not_called()
        self.assertEqual(mock_helper_get.call_count, 3)

    def test_display_numbers_are_one_based(self):
        from documents.services.source_files import build_source_preview

        doc = self._multi_image_doc(count=2)
        self._add_source(doc, 0)
        self._add_source(doc, 1)
        with patch(
            "documents.services.source_files.create_presigned_get",
            side_effect=lambda **kw: f"https://example/{kw['key']}",
        ):
            preview = build_source_preview(doc, "test-bucket")
        self.assertEqual([i["display_number"] for i in preview.items], [1, 2])
        self.assertEqual([i["order_index"] for i in preview.items], [0, 1])

    @patch("documents.services.source_files.create_presigned_get")
    def test_only_uploaded_source_files_are_previewed(self, mock_helper_get):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=3, expected=3)
        self._add_source(doc, 0, upload_status=DocumentSourceFile.UploadStatus.UPLOADED)
        self._add_source(doc, 1, upload_status=DocumentSourceFile.UploadStatus.PENDING)
        self._add_source(doc, 2, upload_status=DocumentSourceFile.UploadStatus.FAILED)
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["source_preview_items"]), 1)
        self.assertEqual(resp.context["source_preview_unavailable_count"], 2)
        # one presigned GET per displayed uploaded source file
        self.assertEqual(mock_helper_get.call_count, 1)

    @patch("documents.services.source_files.create_presigned_get")
    def test_pending_failed_source_files_show_single_unavailable_note(self, mock_helper_get):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=3, expected=3)
        self._add_source(doc, 0, upload_status=DocumentSourceFile.UploadStatus.UPLOADED)
        self._add_source(doc, 1, upload_status=DocumentSourceFile.UploadStatus.PENDING)
        self._add_source(doc, 2, upload_status=DocumentSourceFile.UploadStatus.FAILED)
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        body = resp.content.decode()
        note = "חלק מהעמודים אינם זמינים לתצוגה מקדימה עדיין."
        self.assertEqual(body.count(note), 1)

    @patch("documents.views.create_presigned_get")
    @patch("documents.services.source_files.create_presigned_get")
    def test_no_uploaded_source_files_still_shows_unavailable_note(
        self, mock_helper_get, mock_view_get
    ):
        doc = self._multi_image_doc(count=2, expected=2)
        self._add_source(doc, 0, upload_status=DocumentSourceFile.UploadStatus.PENDING)
        self._add_source(doc, 1, upload_status=DocumentSourceFile.UploadStatus.FAILED)
        self.client.force_login(self.staff)
        note = "חלק מהעמודים אינם זמינים לתצוגה מקדימה עדיין."

        for url in (self._detail_url(doc.id), self._review_url(doc.id)):
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.context["source_preview_items"], [])
                self.assertEqual(resp.context["source_preview_unavailable_count"], 2)
                body = resp.content.decode()
                self.assertEqual(body.count(note), 1)

        # no uploaded files -> no presigned GET, and legacy content_url not used
        mock_helper_get.assert_not_called()
        mock_view_get.assert_not_called()

    @patch("documents.services.source_files.create_presigned_get")
    def test_presigned_get_called_once_per_displayed_uploaded_source_file(
        self, mock_helper_get
    ):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=4, expected=4)
        for i in range(4):
            self._add_source(doc, i)
        self.client.force_login(self.staff)
        self.client.get(self._detail_url(doc.id))
        self.assertEqual(mock_helper_get.call_count, 4)
        called_keys = {c.kwargs["key"] for c in mock_helper_get.call_args_list}
        self.assertEqual(
            called_keys,
            {f"documents/{doc.id}/source/{i}.png" for i in range(4)},
        )

    @patch("documents.services.source_files.create_presigned_get")
    def test_presigned_get_failure_for_one_item_does_not_500(self, mock_helper_get):
        def _fake(**kw):
            if kw["key"].endswith("/1.png"):
                raise RuntimeError("boom")
            return f"https://example/{kw['key']}"

        mock_helper_get.side_effect = _fake
        doc = self._multi_image_doc(count=3, expected=3)
        for i in range(3):
            self._add_source(doc, i)
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        # other items still render; the failing one shows a muted per-item placeholder
        self.assertIn(f"https://example/documents/{doc.id}/source/0.png", body)
        self.assertIn("לא ניתן לטעון תצוגה מקדימה לעמוד זה.", body)

    @patch("documents.services.source_files.create_presigned_get")
    def test_multi_image_preview_get_does_not_mutate_state(self, mock_helper_get):
        mock_helper_get.side_effect = lambda **kw: f"https://example/{kw['key']}"
        doc = self._multi_image_doc(count=2, expected=2)
        for i in range(2):
            self._add_source(doc, i)
        row = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="שורה",
        )
        before_results = DocumentTextResult.objects.count()
        before_sources = DocumentSourceFile.objects.count()
        self.client.force_login(self.staff)
        self.client.get(self._detail_url(doc.id))
        self.client.get(self._review_url(doc.id))
        self.assertEqual(DocumentTextResult.objects.count(), before_results)
        self.assertEqual(DocumentSourceFile.objects.count(), before_sources)
        row.refresh_from_db()
        self.assertEqual(row.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.UNVERIFIED
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)
        self.assertEqual(doc.upload_status, Document.UploadStatus.UPLOADED)


class ReviewUiTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="review_staff",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="review_user",
            password="test-pass",
            is_staff=False,
        )

    def _create_document(self, **kwargs):
        defaults = {
            "title": "Review doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_text_result(self, doc, **kwargs):
        defaults = {
            "result_type": DocumentTextResult.ResultType.HEBREW_TEXT,
            "engine": "transkribus-pylaia:1",
            "engine_key": DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
            "text": "שורת בדיקה",
            "review_reasons": '["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
        }
        defaults.update(kwargs)
        return DocumentTextResult.objects.create(document=doc, **defaults)

    def test_review_backlog_requires_staff(self):
        self.client.force_login(self.user)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 403)

    def test_review_backlog_redirects_anonymous(self):
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 302)

    def test_review_backlog_includes_needs_review_unverified(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, str(doc.id))
        self.assertContains(resp, "בקרת תמלול")

    def test_review_backlog_excludes_verified_pending_status(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f'href="/api/ui/admin/review/{doc.id}/"')

    def test_review_backlog_excludes_legacy_succeeded_unverified(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f'href="/api/ui/admin/review/{doc.id}/"')

    def test_review_backlog_excludes_whitespace_only_text(self):
        doc = self._create_document()
        self._create_text_result(doc, text="   \n\t  ")
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f'href="/api/ui/admin/review/{doc.id}/"')

        from documents.services.review_backlog import (
            attach_review_summaries,
            documents_in_review_backlog,
        )

        self.assertNotIn(
            doc.id,
            set(documents_in_review_backlog().values_list("id", flat=True)),
        )
        doc.refresh_from_db()
        _doc, summary = attach_review_summaries([doc])[0]
        self.assertEqual(summary.pending_count, 0)

    def test_review_backlog_multiple_pending_rows(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:1",
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמלול מקור")
        self.assertContains(resp, "טקסט עברי")
        self.assertContains(resp, 'value="SOURCE_TEXT"')
        self.assertContains(resp, 'value="HEBREW_TEXT"')
        self.assertContains(resp, "Transkribus")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "SOURCE_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")
        self.assertContains(resp, "<strong>2</strong>")

    def test_review_detail_shows_text_result_metadata(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            review_reasons='["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW","MIN_TEXT_LENGTH"]',
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ממתין לבקרת תמלול")
        self.assertContains(resp, "טרם אושר")
        self.assertContains(resp, "טקסט עברי")
        self.assertContains(resp, "TRANSKRIBUS")
        self.assertContains(resp, "נדרשת בקרת תמלול אנושית")
        self.assertContains(resp, "טקסט קצר מדי")
        self.assertContains(resp, "טקסט עברי לבדיקה")
        self.assertContains(resp, "שורת בדיקה")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "NEEDS_REVIEW")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "UNVERIFIED")
        _assert_raw_enum_not_in_visible_badge_text(
            self, resp, "AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"
        )

    def test_review_detail_shows_transkribus_run(self):
        doc = self._create_document()
        self._create_text_result(doc)
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="564149",
            remote_doc_id="777",
            pages_query="1",
            page_index_to_page_nr={1: 1},
            engine_runtime="transkribus-pylaia:564149",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ריצת Transkribus אחרונה")
        self.assertContains(resp, "777")
        self.assertContains(resp, "transkribus-pylaia:564149")

    def test_review_detail_staff_only(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.user)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 403)

    def test_review_pages_no_mutation_on_get(self):
        doc = self._create_document()
        self._create_text_result(doc)
        before_results = DocumentTextResult.objects.count()
        before_docs = Document.objects.count()
        self.client.force_login(self.staff)
        self.client.get("/api/ui/admin/review/")
        self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(DocumentTextResult.objects.count(), before_results)
        self.assertEqual(Document.objects.count(), before_docs)

    def test_documents_in_review_backlog_language_filter(self):
        from documents.services.review_backlog import documents_in_review_backlog

        he_doc = self._create_document(language=Document.Language.HEBREW)
        en_doc = self._create_document(language=Document.Language.ENGLISH, title="En")
        self._create_text_result(he_doc)
        self._create_text_result(en_doc)

        ids = set(
            documents_in_review_backlog(language=Document.Language.HEBREW).values_list(
                "id", flat=True
            )
        )
        self.assertIn(he_doc.id, ids)
        self.assertNotIn(en_doc.id, ids)

    def _verify_url(self, result_id: int) -> str:
        return f"/api/ui/admin/review/text-results/{result_id}/verify/"

    def _reject_url(self, result_id: int) -> str:
        return f"/api/ui/admin/review/text-results/{result_id}/reject/"

    def _text_url(self, result_id: int) -> str:
        return f"/api/ui/admin/review/text-results/{result_id}/text/"

    def test_staff_can_verify_pending_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.post(self._verify_url(row.id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/api/ui/admin/review/{doc.id}/")
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.VERIFIED
        )

    def test_staff_can_reject_pending_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.post(self._reject_url(row.id))
        self.assertEqual(resp.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.REJECTED
        )

    def test_verify_reject_post_requires_staff(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self._verify_url(row.id)).status_code, 403)
        self.assertEqual(self.client.post(self._reject_url(row.id)).status_code, 403)

    def test_verify_reject_post_redirects_anonymous(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.assertEqual(self.client.post(self._verify_url(row.id)).status_code, 302)
        self.assertEqual(self.client.post(self._reject_url(row.id)).status_code, 302)

    def test_verify_changes_only_verification_status(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            review_reasons='["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW","MIN_TEXT_LENGTH"]',
        )
        before = {
            "status": row.status,
            "text": row.text,
            "review_reasons": row.review_reasons,
            "processing_state_user": doc.processing_state_user,
        }
        self.client.force_login(self.staff)
        self.client.post(self._verify_url(row.id))
        row.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.VERIFIED
        )
        self.assertEqual(row.status, before["status"])
        self.assertEqual(row.text, before["text"])
        self.assertEqual(row.review_reasons, before["review_reasons"])
        self.assertEqual(doc.processing_state_user, before["processing_state_user"])

    def test_reject_changes_only_verification_status(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        before_status = row.status
        before_text = row.text
        before_reasons = row.review_reasons
        before_processing = doc.processing_state_user
        self.client.force_login(self.staff)
        self.client.post(self._reject_url(row.id))
        row.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.REJECTED
        )
        self.assertEqual(row.status, before_status)
        self.assertEqual(row.text, before_text)
        self.assertEqual(row.review_reasons, before_reasons)
        self.assertEqual(doc.processing_state_user, before_processing)

    def test_verify_all_pending_rows_removes_document_from_backlog(self):
        from documents.services.review_backlog import documents_in_review_backlog

        doc = self._create_document()
        r1 = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:1",
        )
        r2 = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
        )
        self.client.force_login(self.staff)
        self.client.post(self._verify_url(r1.id))
        self.assertIn(doc.id, set(documents_in_review_backlog().values_list("id", flat=True)))
        self.client.post(self._verify_url(r2.id))
        self.assertNotIn(
            doc.id, set(documents_in_review_backlog().values_list("id", flat=True))
        )

    def test_verify_one_of_two_pending_rows_keeps_document_in_backlog(self):
        from documents.services.review_backlog import (
            attach_review_summaries,
            documents_in_review_backlog,
        )

        doc = self._create_document()
        r1 = self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:1",
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
        )
        self.client.force_login(self.staff)
        self.client.post(self._verify_url(r1.id))
        self.assertIn(doc.id, set(documents_in_review_backlog().values_list("id", flat=True)))
        doc.refresh_from_db()
        _doc, summary = attach_review_summaries([doc])[0]
        self.assertEqual(summary.pending_count, 1)

    def test_rejected_row_remains_in_review_backlog(self):
        from documents.services.review_backlog import documents_in_review_backlog

        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.staff)
        self.client.post(self._reject_url(row.id))
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.REJECTED
        )
        self.assertIn(doc.id, set(documents_in_review_backlog().values_list("id", flat=True)))

    def test_cannot_verify_failed_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            status=DocumentTextResult.Status.FAILED,
            text="failed text",
            error_code="OCR_DISPATCH",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(self._verify_url(row.id))
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.UNVERIFIED
        )

    def test_cannot_reject_failed_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            status=DocumentTextResult.Status.FAILED,
            text="failed text",
        )
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._reject_url(row.id)).status_code, 400)

    def test_cannot_verify_succeeded_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            status=DocumentTextResult.Status.SUCCEEDED,
            text="legacy succeeded",
        )
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._verify_url(row.id)).status_code, 400)

    def test_cannot_verify_whitespace_only_text(self):
        doc = self._create_document()
        row = self._create_text_result(doc, text="  \n\t  ")
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._verify_url(row.id)).status_code, 400)

    def test_cannot_verify_already_verified_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._verify_url(row.id)).status_code, 400)

    def test_invalid_result_id_returns_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(self._verify_url(999999)).status_code, 404)
        self.assertEqual(self.client.post(self._reject_url(999999)).status_code, 404)

    def test_review_detail_shows_verify_reject_for_pending_unverified(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אשר תמלול")
        self.assertContains(resp, "דחה תמלול")

    def test_review_detail_rejected_pending_shows_verify_only(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אשר תמלול")
        self.assertNotContains(resp, "דחה תמלול")

    def test_review_detail_no_actions_for_verified(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertNotContains(resp, "אשר תמלול")
        self.assertNotContains(resp, "/verify/")

    def test_staff_can_edit_pending_unverified_text(self):
        doc = self._create_document()
        row = self._create_text_result(doc, text="טקסט מקורי")
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": "טקסט מתוקן"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], f"/api/ui/admin/review/{doc.id}/")
        row.refresh_from_db()
        self.assertEqual(row.text, "טקסט מתוקן")

    def test_staff_can_edit_pending_rejected_text(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
            text="נדחה",
        )
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": "תיקון אחרי דחייה"})
        row.refresh_from_db()
        self.assertEqual(row.text, "תיקון אחרי דחייה")
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.REJECTED
        )

    def test_edit_redirects_to_review_detail(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": "חדש"})
        self.assertEqual(resp["Location"], f"/api/ui/admin/review/{doc.id}/")

    def test_edit_preserves_multiline_text(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        multiline = "שורה א\n\nשורה ב\t עם רווח"
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": multiline})
        row.refresh_from_db()
        self.assertEqual(row.text, multiline)

    def test_edit_does_not_strip_whole_text_before_saving(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        leading_trailing = "  שורה עם רווחים  \n"
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": leading_trailing})
        row.refresh_from_db()
        self.assertEqual(row.text, leading_trailing)

    def test_edit_does_not_change_status_verification_reasons_or_processing(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            review_reasons='["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW","MIN_TEXT_LENGTH"]',
        )
        before = {
            "status": row.status,
            "verification_status": row.verification_status,
            "review_reasons": row.review_reasons,
            "processing_state_user": doc.processing_state_user,
        }
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": "עודכן"})
        row.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(row.status, before["status"])
        self.assertEqual(row.verification_status, before["verification_status"])
        self.assertEqual(row.review_reasons, before["review_reasons"])
        self.assertEqual(doc.processing_state_user, before["processing_state_user"])

    def test_edit_empty_submitted_text_returns_400(self):
        doc = self._create_document()
        row = self._create_text_result(doc, text="לפני")
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": ""})
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.text, "לפני")

    def test_edit_whitespace_only_submitted_text_returns_400(self):
        doc = self._create_document()
        row = self._create_text_result(doc, text="לפני")
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": "  \n\t  "})
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.text, "לפני")

    def test_edit_post_requires_staff(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.post(self._text_url(row.id), {"text": "x"}).status_code, 403
        )

    def test_edit_post_redirects_anonymous(self):
        doc = self._create_document()
        row = self._create_text_result(doc)
        self.assertEqual(
            self.client.post(self._text_url(row.id), {"text": "x"}).status_code, 302
        )

    def test_edit_invalid_result_id_returns_404(self):
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(self._text_url(999999), {"text": "x"}).status_code, 404
        )

    def test_cannot_edit_failed_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            status=DocumentTextResult.Status.FAILED,
            text="failed text",
        )
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": "new"})
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.text, "failed text")

    def test_cannot_edit_verified_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(self._text_url(row.id), {"text": "new"})
        self.assertEqual(resp.status_code, 400)
        row.refresh_from_db()
        self.assertEqual(row.text, "שורת בדיקה")

    def test_cannot_edit_succeeded_transcription_result(self):
        doc = self._create_document()
        row = self._create_text_result(
            doc,
            status=DocumentTextResult.Status.SUCCEEDED,
            text="legacy succeeded",
        )
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.post(self._text_url(row.id), {"text": "new"}).status_code, 400
        )

    def test_edited_unverified_row_remains_in_review_backlog(self):
        from documents.services.review_backlog import documents_in_review_backlog

        doc = self._create_document()
        row = self._create_text_result(doc)
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": "עודכן"})
        self.assertIn(doc.id, set(documents_in_review_backlog().values_list("id", flat=True)))

    def test_edited_rejected_row_remains_in_review_backlog(self):
        from documents.services.review_backlog import documents_in_review_backlog

        doc = self._create_document()
        row = self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.client.force_login(self.staff)
        self.client.post(self._text_url(row.id), {"text": "עודכן"})
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status, DocumentTextResult.VerificationStatus.REJECTED
        )
        self.assertIn(doc.id, set(documents_in_review_backlog().values_list("id", flat=True)))

    def test_review_detail_shows_textarea_for_editable_row(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertContains(resp, "טקסט שחולץ")
        self.assertContains(resp, "שמור טקסט")
        self.assertContains(resp, 'name="text"')
        self.assertContains(resp, "review-textarea")
        self.assertContains(resp, "/text/")

    def test_review_detail_workspace_section_titles(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מסמך מקור")
        self.assertContains(resp, "בדיקת תמלול")
        self.assertContains(resp, "איך לבדוק:")
        self.assertContains(resp, "משווים בין קובץ המקור לתמלול")
        self.assertContains(resp, "אימות תמלול")
        self.assertContains(resp, "פרטים טכניים")

    def test_review_detail_verified_shows_non_actionable_reason(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "התמלול כבר אושר אנושית")
        self.assertNotContains(resp, "אשר תמלול")

    def test_review_detail_failed_row_shows_non_actionable_reason(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            status=DocumentTextResult.Status.FAILED,
            text="",
            error_code="OCR_FAILED",
            error_details="test failure",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמלול זה נכשל בעיבוד")

    def test_review_detail_transkribus_in_collapsible_section(self):
        doc = self._create_document()
        self._create_text_result(doc)
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="564149",
            remote_doc_id="888",
            pages_query="1",
            engine_runtime="transkribus-pylaia:564149",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertContains(resp, "<details")
        self.assertContains(resp, "ריצת Transkribus אחרונה")
        self.assertContains(resp, "888")

    def test_review_detail_no_textarea_for_verified_row(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertNotContains(resp, "שמור טקסט")
        self.assertNotContains(resp, 'name="text"')
        self.assertNotContains(resp, "/text/")


class UploadPageTemplateTests(TestCase):
    """PR6 — multi-image upload UI on the admin upload page (template render only).

    These pin the upload page markup/copy and that the multi-image client flow is
    wired. They do not duplicate the backend multi-image API tests in UploadApiTests.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="upload_page_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="upload_page_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _get_page(self):
        self.client.force_login(self.staff)
        return self.client.get("/api/ui/upload/")

    def test_upload_page_file_input_allows_multiple(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="file" type="file" multiple')

    def test_upload_page_contains_multi_image_explanatory_copy(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "לבחור כמה תמונות יחד")
        self.assertContains(resp, "2–30 תמונות / עמודים / חלקים = מסמך אחד לפי סדר הבחירה")
        self.assertContains(resp, "ריבוי קבצים תומך בתמונות בלבד")
        self.assertContains(resp, "PDF יש להעלות כקובץ יחיד")
        self.assertContains(resp, "מומלץ להעלות כמה תמונות חלקיות לפי סדר הקריאה")
        self.assertContains(resp, "סדר ההעלאה קובע את סדר התעתוק")
        self.assertContains(resp, "מהטור הימני לשמאלי")
        self.assertContains(resp, "המערכת תתעתק כל תמונה לפי הסדר ותחבר את הטקסט")

    def test_upload_page_still_renders_key_metadata_fields(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        for needle in (
            'id="title"',
            'id="author_name"',
            'id="source_title"',
            'id="doc_type"',
            'id="text_input_type"',
            'id="language"',
            'id="visibility"',
            'id="date_precision"',
            'id="categories"',
            'id="events"',
            'id="discovery_tags"',
        ):
            self.assertContains(resp, needle)

    def test_upload_page_hides_legacy_discovery_fields(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'name="category_event"')
        self.assertNotContains(resp, 'id="category_event"')
        self.assertNotContains(resp, 'name="tags"')

    def test_upload_page_renders_archive_item_discovery_metadata_section(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קטגוריות, אירועים ותגיות")
        self.assertContains(resp, 'name="categories"')
        self.assertContains(resp, 'name="events"')
        self.assertContains(resp, 'name="discovery_tags"')
        self.assertContains(resp, "discovery_tags")

    def test_upload_page_renders_hebrew_date_precision_labels(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "דיוק תאריך")
        for label in ("ללא תאריך", "שנה בלבד", "חודש", "יום מדויק", "טווח"):
            self.assertContains(resp, label)
        self.assertContains(resp, 'getElementById("date_precision")')
        self.assertContains(resp, "date_precision")

    def test_upload_page_renders_hebrew_text_input_type_labels(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "סוג טקסט")
        for label in ("כתב יד", "מודפס"):
            self.assertContains(resp, label)
        self.assertNotContains(resp, "Handwritten")
        self.assertNotContains(resp, "Printed")
        self.assertContains(resp, 'value="HANDWRITTEN"')
        self.assertContains(resp, 'value="PRINTED"')

    def test_upload_page_js_references_multi_image_endpoints(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "/parts/")
        self.assertContains(resp, "/finalize/")

    def test_upload_page_js_sends_csrf_token_on_json_fetch(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "X-CSRFToken")
        self.assertContains(resp, "getCsrfToken")
        self.assertContains(resp, '#uploadForm input[name=csrfmiddlewaretoken]')

    def test_upload_page_renders_own_csrf_token_in_upload_form(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="uploadForm"')
        self.assertContains(resp, 'name="csrfmiddlewaretoken"')
        # Token must come from the upload form itself, not only global nav markup.
        form_start = resp.content.index(b'id="uploadForm"')
        csrf_pos = resp.content.index(b'name="csrfmiddlewaretoken"', form_start)
        nav_pos = resp.content.index(b"nav-shell")
        self.assertGreater(csrf_pos, form_start)
        self.assertGreater(csrf_pos, nav_pos)

    def test_upload_page_shows_terminal_restart_copy_for_failed_multi_image(self):
        resp = self._get_page()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ויש להתחיל העלאה חדשה")

    def test_upload_page_requires_admin(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ui/upload/")
        self.assertEqual(resp.status_code, 403)


class StatusLabelFilterTests(SimpleTestCase):
    """PR1 — centralized user-facing Hebrew status labels (status_labels tags).

    These pin the single source of truth so labels cannot silently drift back
    apart across pages. Enum values/semantics are unchanged.
    """

    def test_processing_state_label_ready_is_ready_for_viewing(self):
        from documents.templatetags.status_labels import processing_state_label

        self.assertEqual(processing_state_label("READY"), "מוכן לצפייה")

    def test_processing_state_label_ready_is_not_bare_moochan(self):
        from documents.templatetags.status_labels import processing_state_label

        self.assertNotEqual(processing_state_label("READY"), "מוכן")

    def test_processing_state_labels_for_other_states(self):
        from documents.templatetags.status_labels import processing_state_label

        self.assertEqual(processing_state_label("PROCESSING"), "בעיבוד")
        self.assertEqual(processing_state_label("PARTIAL"), "חלקי")
        self.assertEqual(processing_state_label("FAILED"), "עיבוד נכשל")

    def test_processing_state_label_unknown_falls_back_to_raw(self):
        from documents.templatetags.status_labels import processing_state_label

        self.assertEqual(processing_state_label("SOMETHING_NEW"), "SOMETHING_NEW")

    def test_upload_status_labels(self):
        from documents.templatetags.status_labels import upload_status_label

        self.assertEqual(upload_status_label("UPLOADED"), "הועלה")
        self.assertEqual(upload_status_label("UPLOADING"), "בהעלאה")
        self.assertEqual(upload_status_label("FAILED"), "העלאה נכשלה")

    def test_metadata_status_labels_are_hebrew(self):
        from documents.templatetags.status_labels import metadata_status_label

        self.assertEqual(metadata_status_label("NEEDS_COMPLETION"), "דרושה השלמת פרטים")
        self.assertEqual(metadata_status_label("COMPLETED"), "פרטים הושלמו")
        # Regression: the raw English label must not be user-facing.
        self.assertNotEqual(metadata_status_label("NEEDS_COMPLETION"), "Needs completion")

    def test_verification_status_labels_are_separate_from_processing(self):
        from documents.templatetags.status_labels import verification_status_label

        self.assertEqual(verification_status_label("UNVERIFIED"), "טרם אושר")
        self.assertEqual(verification_status_label("VERIFIED"), "אושר")
        self.assertEqual(verification_status_label("REJECTED"), "נדחה בבקרה")

    def test_text_result_status_labels_are_hebrew(self):
        from documents.templatetags.status_labels import text_result_status_label

        self.assertEqual(text_result_status_label("NEEDS_REVIEW"), "ממתין לבקרת תמלול")
        self.assertEqual(text_result_status_label("FAILED"), "עיבוד נכשל")

    def test_review_reason_labels_map_known_codes(self):
        from documents.templatetags.status_labels import review_reason_label

        self.assertEqual(
            review_reason_label("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"),
            "נדרשת בקרת תמלול אנושית",
        )
        self.assertNotEqual(review_reason_label("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"), "AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW")

    def test_text_input_type_labels_match_upload_ui_choices(self):
        from documents.templatetags.status_labels import text_input_type_label

        self.assertEqual(text_input_type_label("HANDWRITTEN"), "כתב יד")
        self.assertEqual(text_input_type_label("PRINTED"), "מודפס")
        self.assertNotEqual(text_input_type_label("HANDWRITTEN"), "Handwritten")
        self.assertNotEqual(text_input_type_label("PRINTED"), "Printed")


class StatusLabelPresentationTests(TestCase):
    """PR1 — pages render the centralized labels (no raw-enum/legacy leakage)."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="status_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_document(self, **kwargs):
        defaults = {
            "title": "Status doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_hebrew_text_result(self, doc, **kwargs):
        defaults = {
            "result_type": DocumentTextResult.ResultType.HEBREW_TEXT,
            "engine": "transkribus-pylaia:1",
            "engine_key": DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
            "text": "שורת בדיקה",
            "review_reasons": '["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
        }
        defaults.update(kwargs)
        return DocumentTextResult.objects.create(document=doc, **defaults)

    def test_list_page_ready_uses_ready_for_viewing_label(self):
        self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מוכן לצפייה")
        self.assertNotContains(resp, ">READY<")

    def test_list_page_metadata_status_is_hebrew(self):
        self._create_document(metadata_status=Document.MetadataStatus.NEEDS_COMPLETION)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "דרושה השלמת פרטים")
        # Neither the badge nor the metadata_status filter <option> may show the
        # raw English enum label.
        self.assertNotContains(resp, "Needs completion")

    def test_list_page_visibility_filter_uses_hebrew_labels_not_raw_english_placeholder(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'placeholder="private / public"')
        self.assertNotContains(resp, "private = פרטי")
        self.assertContains(resp, 'id="filter-visibility"')
        self.assertContains(resp, ">פרטי<")
        self.assertContains(resp, ">ציבורי<")

    def test_list_page_upload_status_filter_is_select_with_hebrew_labels(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="filter-upload-status"')
        self.assertContains(resp, "<select")
        self.assertContains(resp, ">הועלה<")
        self.assertContains(resp, ">בהעלאה<")
        self.assertContains(resp, ">העלאה נכשלה<")
        self.assertContains(resp, 'value="UPLOADED"')
        self.assertNotContains(resp, "ניתן להזין את הערך הפנימי")

        resp = self.client.get(
            "/api/ui/documents/",
            {"upload_status": Document.UploadStatus.UPLOADING},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'value="UPLOADING" selected')
        self.assertContains(resp, "בהעלאה")

    def test_detail_page_ready_uses_ready_for_viewing_label(self):
        doc = self._create_document()
        self._create_hebrew_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מוכן לצפייה")
        self.assertNotContains(resp, ">READY<")

    def test_detail_page_verification_label_separate_from_processing(self):
        doc = self._create_document()
        self._create_hebrew_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        # Processing readiness and human approval stay distinct on the detail page.
        self.assertContains(resp, "מוכן לצפייה")
        self.assertContains(resp, "הטקסט חולץ אוטומטית ועדיין לא עבר בדיקה ידנית. ייתכנו שגיאות.")
        self.assertContains(resp, "פרטים")
        self.assertContains(resp, "טרם אושר")
        self.assertContains(resp, "ממתין לבקרת תמלול")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "UNVERIFIED")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "NEEDS_REVIEW")

    def test_admin_backlog_page_ready_uses_ready_for_viewing_label(self):
        self._create_document(
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מוכן לצפייה")
        self.assertNotContains(resp, ">READY<")

    def test_review_backlog_page_ready_uses_ready_for_viewing_label(self):
        doc = self._create_document()
        self._create_hebrew_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מוכן לצפייה")
        self.assertNotContains(resp, ">READY<")
        # The processing_state_user and verification_status filter <option>s must
        # render centralized Hebrew labels, not raw English enum choice labels.
        self.assertNotContains(resp, ">Ready<")
        self.assertNotContains(resp, ">Unverified<")

    def test_review_backlog_page_renders_hebrew_text_input_type_filter_labels(self):
        doc = self._create_document(text_input_type=Document.TextInputType.PRINTED)
        self._create_hebrew_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="filter-text-input-type"')
        for label in ("כתב יד", "מודפס"):
            self.assertContains(resp, label)
        self.assertNotContains(resp, ">Handwritten<")
        self.assertNotContains(resp, ">Printed<")
        self.assertContains(resp, 'value="HANDWRITTEN"')
        self.assertContains(resp, 'value="PRINTED"')

    def test_review_backlog_text_input_type_filter_preserves_query_values(self):
        from documents.services.review_backlog import documents_in_review_backlog

        handwritten = self._create_document(text_input_type=Document.TextInputType.HANDWRITTEN)
        printed = self._create_document(
            title="Printed review doc",
            text_input_type=Document.TextInputType.PRINTED,
        )
        self._create_hebrew_text_result(handwritten)
        self._create_hebrew_text_result(printed)
        self.client.force_login(self.staff)
        resp = self.client.get(
            "/api/ui/admin/review/",
            {"text_input_type": Document.TextInputType.PRINTED},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מודפס")
        self.assertContains(resp, printed.archive_item.title)
        self.assertNotContains(resp, handwritten.archive_item.title)
        ids = set(
            documents_in_review_backlog(
                text_input_type=Document.TextInputType.PRINTED,
            ).values_list("id", flat=True)
        )
        self.assertEqual(ids, {printed.id})

    def test_review_detail_uses_centralized_hebrew_labels_in_technical_details(self):
        doc = self._create_document()
        self._create_hebrew_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מוכן לצפייה")
        self.assertContains(resp, "טרם אושר")
        self.assertContains(resp, "ממתין לבקרת תמלול")
        self.assertContains(resp, "טקסט עברי")
        self.assertNotContains(resp, "לא מאומת")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "NEEDS_REVIEW")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "UNVERIFIED")

    def test_review_detail_verified_row_shows_human_approved_label(self):
        doc = self._create_document()
        self._create_hebrew_text_result(
            doc,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אושר")


class AdminBacklogMetadataEditLinkTests(TestCase):
    """Metadata completion backlog — first-party OCR edit as primary row action (PR3)."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="backlog_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="backlog_edit_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_document(self, **kwargs):
        defaults = {
            "title": "Backlog edit doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "metadata_status": Document.MetadataStatus.NEEDS_COMPLETION,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _link_opening_tag(self, html: str, href: str) -> str:
        marker = f'href="{href}"'
        href_pos = html.find(marker)
        self.assertNotEqual(href_pos, -1, f"missing link href={href!r}")
        tag_start = html.rfind("<a", 0, href_pos)
        tag_end = html.find(">", href_pos)
        self.assertNotEqual(tag_start, -1)
        self.assertGreater(tag_end, href_pos)
        return html[tag_start : tag_end + 1]

    def _link_label(self, html: str, href: str) -> str:
        marker = f'href="{href}"'
        href_pos = html.find(marker)
        self.assertNotEqual(href_pos, -1, f"missing link href={href!r}")
        tag_end = html.find(">", href_pos)
        close_start = html.find("</a>", tag_end)
        self.assertNotEqual(close_start, -1)
        return html[tag_end + 1 : close_start].strip()

    def test_backlog_row_links_to_first_party_ocr_edit(self):
        doc = self._create_document(title="First-party edit target")
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        edit_href = f"/archive/manage/{doc.archive_item_id}/edit/"
        self.assertEqual(self._link_label(html, edit_href), "עריכת מטא־דאטה")
        self.assertIn("btn-primary", self._link_opening_tag(html, edit_href))

    def test_backlog_django_admin_link_is_secondary(self):
        doc = self._create_document(title="Secondary admin link")
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        admin_href = f"/admin/documents/document/{doc.id}/change/"
        self.assertEqual(self._link_label(html, admin_href), "עריכה טכנית (Django Admin)")
        admin_tag = self._link_opening_tag(html, admin_href)
        self.assertIn("btn", admin_tag)
        self.assertNotIn("btn-primary", admin_tag)

    def test_backlog_includes_needs_completion_only(self):
        needs = self._create_document(
            title="Needs completion visible",
            metadata_status=Document.MetadataStatus.NEEDS_COMPLETION,
        )
        completed = self._create_document(
            title="Completed hidden",
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, needs.title)
        self.assertNotContains(resp, completed.title)

    def test_completed_document_excluded_even_with_empty_tags_and_admin_meta(self):
        from documents.models import DocumentMetadata

        doc = self._create_document(
            title="Completed despite empty catalog",
            metadata_status=Document.MetadataStatus.COMPLETED,
        )
        DocumentMetadata.objects.create(document=doc)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, doc.title)

    def test_only_missing_tags_filter_unchanged(self):
        from documents.models import Tag

        missing = self._create_document(title="Missing tags doc")
        tagged = self._create_document(title="Tagged doc")
        tag = Tag.objects.create(name="backlog-filter-tag")
        tagged.tags_m2m.add(tag)

        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/?only_missing_tags=1")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, missing.title)
        self.assertNotContains(resp, tagged.title)

    def test_only_missing_admin_meta_filter_unchanged(self):
        from documents.models import DocumentMetadata

        empty_meta = self._create_document(title="Empty admin meta doc")
        DocumentMetadata.objects.create(
            document=empty_meta,
            donor="",
            collection="",
            original_location="",
            notes="",
        )
        filled_meta = self._create_document(title="Filled admin meta doc")
        DocumentMetadata.objects.create(
            document=filled_meta,
            donor="Donor present",
        )

        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/backlog/?only_missing_admin_meta=1")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, empty_meta.title)
        self.assertNotContains(resp, filled_meta.title)

    def test_review_backlog_does_not_link_to_archive_manage_edit(self):
        doc = self._create_document(title="Review only doc")
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:1",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="שורת בדיקה",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/admin/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f"/archive/manage/{doc.archive_item_id}/edit/")

    def test_admin_backlog_requires_staff(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 403)

    def test_admin_backlog_redirects_anonymous(self):
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 302)


class NavigationLabelTests(TestCase):
    """Navigation/action label cleanup (presentation-only).

    These pin the distinction between *global* list/backlog navigation and
    *current-document* actions, and that link targets + admin gating are
    unchanged.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="nav_staff",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="nav_user",
            password="test-pass",
            is_staff=False,
        )

    def _create_document(self, **kwargs):
        defaults = {
            "title": "Nav doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_text_result(self, doc, **kwargs):
        defaults = {
            "result_type": DocumentTextResult.ResultType.HEBREW_TEXT,
            "engine": "transkribus-pylaia:1",
            "engine_key": DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
            "text": "שורת בדיקה",
            "review_reasons": '["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
        }
        defaults.update(kwargs)
        return DocumentTextResult.objects.create(document=doc, **defaults)

    def _link_opening_tag(self, html: str, href: str) -> str:
        marker = f'href="{href}"'
        href_pos = html.find(marker)
        self.assertNotEqual(href_pos, -1, f"missing link href={href!r}")
        tag_start = html.rfind("<a", 0, href_pos)
        tag_end = html.find(">", href_pos)
        self.assertNotEqual(tag_start, -1)
        self.assertGreater(tag_end, href_pos)
        return html[tag_start : tag_end + 1]

    def _link_label(self, html: str, href: str) -> str:
        marker = f'href="{href}"'
        href_pos = html.find(marker)
        self.assertNotEqual(href_pos, -1, f"missing link href={href!r}")
        tag_end = html.find(">", href_pos)
        close_start = html.find("</a>", tag_end)
        self.assertNotEqual(close_start, -1)
        return html[tag_end + 1 : close_start].strip()

    def test_global_nav_uses_list_wording_for_admin(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "רשימת בקרת תמלול")
        self.assertContains(resp, "רשימת השלמת פרטים")
        self.assertContains(resp, 'href="/api/ui/admin/review/"')
        self.assertContains(resp, 'href="/api/ui/admin/backlog/"')

    def test_detail_distinguishes_global_list_from_current_doc_action(self):
        doc = self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        # Global list link and current-document action use distinct labels...
        self.assertContains(resp, "רשימת בקרת תמלול")
        self.assertContains(resp, "בקרת תמלול למסמך זה")
        # ...the old ambiguous label is gone...
        self.assertNotContains(resp, "בדיקת תמלול למסמך זה")
        # ...and both still point at their original, distinct hrefs.
        self.assertContains(resp, 'href="/api/ui/admin/review/"')
        self.assertContains(resp, f'href="/api/ui/admin/review/{doc.id}/"')
        self.assertContains(resp, "רשימת השלמת פרטים")

    def test_detail_admin_action_links_hidden_for_non_admin(self):
        doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        self.client.force_login(self.user)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        # Non-admin keeps the global "back to list" link...
        self.assertContains(resp, "חזרה לרשימה")
        # ...but admin navigation/actions stay hidden (gating unchanged).
        self.assertNotContains(resp, "בקרת תמלול למסמך זה")
        self.assertNotContains(resp, "רשימת בקרת תמלול")
        self.assertNotContains(resp, "/api/ui/admin/review/")

    def test_detail_metadata_edit_is_primary_staff_action(self):
        doc = self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        edit_href = f"/archive/manage/{doc.archive_item_id}/edit/"
        self.assertEqual(self._link_label(html, edit_href), "עריכת מטא־דאטה")
        self.assertIn("btn-primary", self._link_opening_tag(html, edit_href))

    def test_detail_review_link_is_secondary_not_primary(self):
        doc = self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        html = resp.content.decode()
        review_href = f"/api/ui/admin/review/{doc.id}/"
        self.assertEqual(self._link_label(html, review_href), "בקרת תמלול למסמך זה")
        review_tag = self._link_opening_tag(html, review_href)
        self.assertIn("btn", review_tag)
        self.assertNotIn("btn-primary", review_tag)

    def test_detail_django_admin_is_technical_secondary(self):
        doc = self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        html = resp.content.decode()
        admin_href = f"/admin/documents/document/{doc.id}/change/"
        self.assertEqual(
            self._link_label(html, admin_href), "עריכה טכנית (Django Admin)"
        )
        admin_tag = self._link_opening_tag(html, admin_href)
        self.assertIn("btn", admin_tag)
        self.assertNotIn("btn-primary", admin_tag)

    def test_detail_no_delete_action_for_ocr_document(self):
        from django.urls import reverse

        doc = self._create_document()
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(
            resp,
            reverse("archive-manage-delete", kwargs={"item_id": doc.archive_item_id}),
        )

    def test_detail_family_user_sees_no_staff_actions(self):
        from django.contrib.auth.models import Group

        from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME

        group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        doc = self._create_document(visibility=Document.Visibility.PRIVATE)
        family_user = self.user.__class__.objects.create_user(
            username="nav_family_user",
            password="test-pass",
            is_staff=False,
        )
        family_user.groups.add(group)
        self.client.force_login(family_user)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "חזרה לרשימה")
        self.assertNotContains(resp, "עריכת מטא־דאטה")
        self.assertNotContains(resp, "בקרת תמלול למסמך זה")
        self.assertNotContains(resp, "עריכה טכנית (Django Admin)")

    def test_review_detail_has_back_to_review_list_link(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "חזרה לרשימת בקרת תמלול")
        # The old ambiguous "back" label is gone.
        self.assertNotContains(resp, "חזרה לבקרת תמלול<")
        # Global list + current-document links keep their original hrefs.
        self.assertContains(resp, 'href="/api/ui/admin/review/"')
        self.assertContains(resp, "תצוגת מסמך")
        self.assertContains(resp, f'href="/api/ui/documents/{doc.id}/"')
        self.assertContains(resp, "רשימת השלמת פרטים")

    def test_review_detail_metadata_edit_link_for_staff(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        edit_href = f"/archive/manage/{doc.archive_item_id}/edit/"
        self.assertEqual(self._link_label(html, edit_href), "עריכת מטא־דאטה")
        edit_tag = self._link_opening_tag(html, edit_href)
        self.assertIn("btn", edit_tag)
        self.assertNotIn("btn-primary", edit_tag)

    def test_review_detail_django_admin_is_technical_secondary(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        html = resp.content.decode()
        admin_href = f"/admin/documents/document/{doc.id}/change/"
        self.assertEqual(
            self._link_label(html, admin_href), "עריכה טכנית (Django Admin)"
        )
        admin_tag = self._link_opening_tag(html, admin_href)
        self.assertIn("btn", admin_tag)
        self.assertNotIn("btn-primary", admin_tag)

    def test_review_detail_has_no_redundant_open_review_action(self):
        doc = self._create_document()
        self._create_text_result(doc)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/admin/review/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "בקרת תמלול למסמך זה")
        self.assertNotContains(resp, "פתח לבדיקה")
        self.assertNotContains(resp, "פתח בבקרת תמלול")


class ReviewDetailHierarchyTests(SimpleTestCase):
    """Review detail information hierarchy — presentation helpers (no DB)."""

    def test_non_actionable_reason_none_when_pending(self):
        from documents.views import _review_non_actionable_reason

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="שורה",
        )
        self.assertIsNone(_review_non_actionable_reason(row))

    def test_non_actionable_reason_verified(self):
        from documents.views import _review_non_actionable_reason

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="שורה",
        )
        reason = _review_non_actionable_reason(row)
        self.assertIsNotNone(reason)
        self.assertIn("אושר אנושית", reason)

    def test_non_actionable_reason_empty_text(self):
        from documents.views import _review_non_actionable_reason

        row = DocumentTextResult(
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="   ",
        )
        reason = _review_non_actionable_reason(row)
        self.assertEqual(reason, "אין טקסט זמין לבדיקה.")

    def test_result_type_description_hebrew_hebrew_text(self):
        from documents.views import _review_result_type_description

        doc = Document(language=Document.Language.HEBREW)
        desc = _review_result_type_description(
            doc, DocumentTextResult.ResultType.HEBREW_TEXT
        )
        self.assertIn("בדיקה", desc)


def _assert_raw_enum_not_in_visible_badge_text(test_case, response, raw_enum: str) -> None:
    """Raw enum values in form ``value=`` attributes are OK; badge text must be Hebrew."""
    html = response.content.decode()
    for tone in ("", "badge-warn", "badge-ok", "badge-bad"):
        css_class = "badge" if not tone else f"badge {tone}"
        test_case.assertNotIn(
            f'<span class="{css_class}">{raw_enum}</span>',
            html,
            msg=f"raw enum {raw_enum!r} must not appear as visible badge text",
        )


class DocumentDetailTextGroupingTests(TestCase):
    """Document detail text-result grouping and labeling (presentation-only)."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="detail_text_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="detail_text_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_document(self, **kwargs):
        defaults = {
            "title": "Detail text doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _create_text_result(self, doc, **kwargs):
        defaults = {
            "engine": "transkribus-pylaia:1",
            "engine_key": DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
            "text": "שורת בדיקה",
            "review_reasons": '["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
        }
        defaults.update(kwargs)
        return DocumentTextResult.objects.create(document=doc, **defaults)

    def _detail_url(self, doc_id: int) -> str:
        return f"/api/ui/documents/{doc_id}/"

    def test_hebrew_detail_prefers_single_hebrew_text_panel(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט מקור עברי",
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט מקור עברי",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טקסט עברי לבדיקה")
        self.assertNotContains(resp, "תמלול מקור")
        self.assertContains(resp, "טקסט מקור עברי", count=1)
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "SOURCE_TEXT")

    def test_hebrew_detail_uses_hebrew_text_label_when_both_rows_exist(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="א",
        )
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="ב",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טקסט עברי לבדיקה")
        self.assertNotContains(resp, "הטקסט העברי שמיועד לבדיקה, עריכה ואישור.")
        self.assertNotContains(resp, "תמלול מקור")
        self.assertNotContains(resp, "תמלול מקור (עברית כפי שחולצה)")

    def test_hebrew_detail_falls_back_to_source_text_when_hebrew_missing(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט מקור בלבד",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמלול מקור")
        self.assertNotContains(resp, "טקסט עברי לבדיקה")
        self.assertContains(resp, "טקסט מקור בלבד")
        self.assertContains(resp, "חסרים פלטים:")
        self.assertContains(resp, "טקסט עברי")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "SOURCE_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")

    def test_non_hebrew_detail_preserves_source_and_translation_sections(self):
        doc = self._create_document(language=Document.Language.ENGLISH)
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source line",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמלול מקור")
        self.assertNotContains(resp, "טקסט בשפת המקור כפי שחולץ אוטומטית.")
        self.assertContains(resp, "תרגום לעברית")
        self.assertContains(resp, "אין תרגום לעברית עדיין.")
        self.assertContains(resp, "English source line")

    def test_non_hebrew_missing_translation_still_shows_missing_section(self):
        doc = self._create_document(language=Document.Language.FRENCH)
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Texte source",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "חסרים פלטים:")
        self.assertContains(resp, "טקסט עברי")
        self.assertContains(resp, "אין תרגום לעברית עדיין.")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")

    def test_detail_technical_details_remain_collapsed_with_hebrew_labels(self):
        doc = self._create_document()
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="שורה",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "טקסט עברי לבדיקה")
        self.assertContains(resp, "פרטים")
        self.assertContains(resp, "<details")
        self.assertContains(resp, "טקסט עברי")
        self.assertContains(resp, "ממתין לבקרת תמלול")
        self.assertContains(resp, "טרם אושר")
        self.assertContains(resp, "transkribus-pylaia:1")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "SOURCE_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "NEEDS_REVIEW")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "UNVERIFIED")

    def test_viewer_detail_hides_internal_text_labels_and_shows_auto_disclaimer(self):
        doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        self._create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט לצופה",
        )
        self.client.force_login(self.viewer)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תמלול")
        self.assertContains(resp, "טקסט לצופה")
        self.assertContains(resp, "פרטים")
        self.assertContains(resp, "הטקסט חולץ אוטומטית ועדיין לא עבר בדיקה ידנית. ייתכנו שגיאות.")
        self.assertNotContains(resp, "תמלול אוטומטי")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "HEBREW_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "SOURCE_TEXT")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "NEEDS_REVIEW")
        _assert_raw_enum_not_in_visible_badge_text(self, resp, "UNVERIFIED")
        self.assertNotContains(resp, "transkribus-pylaia:1")
        self.assertNotContains(resp, "טקסט עברי לבדיקה")


class TextBlockDisplayMetaTests(SimpleTestCase):
    """Pure metadata label tests for document detail text blocks (no DB)."""

    def test_hebrew_source(self):
        from documents.services.text_presentation import text_block_display_meta

        doc = Document(language=Document.Language.HEBREW)
        meta = text_block_display_meta(doc, "SOURCE_TEXT")
        self.assertEqual(meta.label, "תמלול מקור")
        self.assertEqual(meta.description, "הטקסט כפי שחולץ אוטומטית מן המסמך.")

    def test_hebrew_hebrew_text(self):
        from documents.services.text_presentation import text_block_display_meta

        doc = Document(language=Document.Language.HEBREW)
        meta = text_block_display_meta(doc, "HEBREW_TEXT")
        self.assertEqual(meta.label, "טקסט עברי לבדיקה")
        self.assertIn("עריכה", meta.description)

    def test_non_hebrew_translation(self):
        from documents.services.text_presentation import text_block_display_meta

        doc = Document(language=Document.Language.ENGLISH)
        meta = text_block_display_meta(doc, "HEBREW_TEXT")
        self.assertEqual(meta.label, "תרגום לעברית")


class TextPresentationHelperTests(TestCase):
    """DB-backed tests for document detail text presentation helpers."""

    def test_get_text_presentation_hebrew_prefers_single_hebrew_panel(self):
        from documents.services.text_presentation import get_text_presentation_for_document

        doc = create_ocr_document(
            title="Presentation helper doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
        )
        shared = "טקסט זהה"
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=shared,
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=shared,
        )

        presentation = get_text_presentation_for_document(doc)
        self.assertFalse(presentation.show_source)
        self.assertTrue(presentation.show_hebrew)

    def test_get_text_presentation_hebrew_falls_back_to_source_panel(self):
        from documents.services.text_presentation import get_text_presentation_for_document

        doc = create_ocr_document(
            title="Presentation helper fallback doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="מקור בלבד",
        )

        presentation = get_text_presentation_for_document(doc)
        self.assertTrue(presentation.show_source)
        self.assertFalse(presentation.show_hebrew)


class DocumentVisibilityAccessControlTests(TestCase):
    """PR1 — public archive visibility: only explicit public documents for non-staff."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="visibility_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="visibility_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_document(self, *, visibility=Document.Visibility.PRIVATE, title="Vis doc", **kwargs):
        defaults = {
            "title": title,
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "language": Document.Language.HEBREW,
            "upload_status": Document.UploadStatus.UPLOADED,
            "processing_state_user": Document.ProcessingState.READY,
            "visibility": visibility,
            "file_s3_key": "documents/99/original.jpg",
            "mime_type": "image/jpeg",
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def test_anonymous_list_api_public_only(self):
        public_doc = self._create_document(
            visibility=Document.Visibility.PUBLIC,
            title="Public list API",
        )
        self._create_document(
            visibility=Document.Visibility.PRIVATE,
            title="Private list API",
        )
        resp = self.client.get("/api/documents/")
        self.assertEqual(resp.status_code, 200)
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertEqual(ids, {public_doc.id})

    def test_anonymous_list_page_public_only(self):
        public_doc = self._create_document(
            visibility=Document.Visibility.PUBLIC,
            title="Public list UI",
        )
        self._create_document(
            visibility=Document.Visibility.PRIVATE,
            title="Private list UI",
        )
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, public_doc.title)
        self.assertNotContains(resp, "Private list UI")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_anonymous_public_detail_ok_private_detail_404(self, _mock_presign):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        private_doc = self._create_document(visibility=Document.Visibility.PRIVATE)
        self.assertEqual(
            self.client.get(f"/api/ui/documents/{public_doc.id}/").status_code,
            200,
        )
        self.assertEqual(
            self.client.get(f"/api/ui/documents/{private_doc.id}/").status_code,
            404,
        )

    def test_viewer_same_visibility_rules_as_anonymous(self):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        private_doc = self._create_document(visibility=Document.Visibility.PRIVATE)
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/documents/")
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertIn(public_doc.id, ids)
        self.assertNotIn(private_doc.id, ids)
        self.assertEqual(
            self.client.get(f"/api/ui/documents/{private_doc.id}/").status_code,
            404,
        )

    def test_staff_sees_public_and_private_in_list(self):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        private_doc = self._create_document(visibility=Document.Visibility.PRIVATE)
        self.client.force_login(self.staff)
        resp = self.client.get("/api/documents/")
        ids = {item["id"] for item in resp.json()["items"]}
        self.assertEqual(ids, {public_doc.id, private_doc.id})

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_private_detail_does_not_presign_for_anonymous(self, mock_presign):
        doc = self._create_document(visibility=Document.Visibility.PRIVATE)
        self.assertEqual(self.client.get(f"/api/ui/documents/{doc.id}/").status_code, 404)
        mock_presign.assert_not_called()

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_public_detail_presigns_for_anonymous(self, mock_presign):
        doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        mock_presign.assert_called_once()

    def test_admin_backlog_requires_staff(self):
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ui/admin/backlog/")
        self.assertEqual(resp.status_code, 403)

    def test_upload_page_still_requires_staff(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get("/api/ui/upload/").status_code, 403)

    def test_anonymous_list_page_hides_internal_document_id_display(self):
        public_doc = self._create_document(
            visibility=Document.Visibility.PUBLIC,
            title="Public list ID hidden",
        )
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, public_doc.title)
        self.assertNotContains(resp, f"· #{public_doc.id}")

    def test_viewer_list_page_hides_internal_document_id_display(self):
        public_doc = self._create_document(
            visibility=Document.Visibility.PUBLIC,
            title="Viewer list ID hidden",
        )
        self.client.force_login(self.viewer)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, public_doc.title)
        self.assertNotContains(resp, f"· #{public_doc.id}")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_anonymous_detail_page_hides_internal_document_id_display(self, _mock_presign):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        resp = self.client.get(f"/api/ui/documents/{public_doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f"מסמך #{public_doc.id}")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_viewer_detail_page_hides_internal_document_id_display(self, _mock_presign):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        self.client.force_login(self.viewer)
        resp = self.client.get(f"/api/ui/documents/{public_doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, f"מסמך #{public_doc.id}")

    def test_staff_list_page_shows_internal_document_id_display(self):
        public_doc = self._create_document(
            visibility=Document.Visibility.PUBLIC,
            title="Staff list ID visible",
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"· #{public_doc.id}")

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example.com/presigned")
    def test_staff_detail_page_shows_internal_document_id_display(self, _mock_presign):
        public_doc = self._create_document(visibility=Document.Visibility.PUBLIC)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{public_doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"מסמך #{public_doc.id}")


class DocumentDatePrecisionTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User

        self.staff = User.objects.create_user(
            username="date_precision_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="date_precision_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_doc(self, **kwargs):
        defaults = {
            "title": "Date display doc",
            "doc_type": Document.DocType.IMAGE,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "visibility": Document.Visibility.PUBLIC,
            "upload_status": Document.UploadStatus.UPLOADED,
        }
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def test_format_unknown_without_dates(self):
        from documents.services.document_date import NO_DATE_LABEL, format_document_date

        doc = self._create_doc()
        self.assertEqual(format_document_date(doc), NO_DATE_LABEL)

    def test_format_unknown_ignores_normalized_bounds(self):
        from datetime import date

        from documents.services.document_date import NO_DATE_LABEL, format_document_date

        doc = self._create_doc(
            date_start=date(1948, 5, 12),
            date_end=date(1948, 5, 12),
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        self.assertEqual(format_document_date(doc), NO_DATE_LABEL)

    def test_format_exact_day_single_label(self):
        from datetime import date

        from documents.services.document_date import format_document_date

        doc = self._create_doc(
            date_start=date(1948, 5, 12),
            date_end=date(1948, 5, 12),
            date_precision=Document.DatePrecision.EXACT_DAY,
        )
        self.assertEqual(format_document_date(doc), "12/05/1948")

    def test_format_month_year_not_day_bounds(self):
        from datetime import date

        from documents.services.document_date import format_document_date

        doc = self._create_doc(
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=Document.DatePrecision.MONTH,
        )
        self.assertEqual(format_document_date(doc), "05/1948")
        self.assertNotIn("01/05/1948", format_document_date(doc))
        self.assertNotIn("31/05/1948", format_document_date(doc))

    def test_format_year_only(self):
        from datetime import date

        from documents.services.document_date import format_document_date

        doc = self._create_doc(
            date_start=date(1948, 1, 1),
            date_end=date(1948, 12, 31),
            date_precision=Document.DatePrecision.YEAR,
        )
        self.assertEqual(format_document_date(doc), "1948")

    def test_format_range_uses_bound_labels(self):
        from datetime import date

        from documents.services.document_date import format_document_date

        doc = self._create_doc(
            date_start=date(1947, 1, 1),
            date_end=date(1949, 12, 31),
            date_precision=Document.DatePrecision.RANGE,
        )
        self.assertEqual(format_document_date(doc), "01/01/1947 - 31/12/1949")

    def test_list_page_uses_precision_aware_date_not_raw_bounds(self):
        from datetime import date

        doc = self._create_doc(
            title="List date display",
            date_precision=Document.DatePrecision.YEAR,
            date_start=date(1948, 1, 1),
            date_end=date(1948, 12, 31),
        )
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/documents/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, doc.title)
        self.assertContains(resp, "1948")
        self.assertNotContains(resp, "1948-01-01")
        self.assertNotContains(resp, "1948-12-31")

    def test_detail_unknown_without_dates_shows_no_date_label(self):
        doc = self._create_doc(
            title="Unknown no bounds detail",
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        self.assertIsNone(doc.date_start)
        self.assertIsNone(doc.date_end)
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ללא תאריך")

    def test_detail_unknown_with_dates_shows_no_date_not_raw_bounds(self):
        from datetime import date

        doc = self._create_doc(
            title="Unknown precision detail",
            date_start=date(1948, 5, 12),
            date_end=date(1948, 5, 12),
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ללא תאריך")
        self.assertNotContains(resp, "1948-05-12")

    def test_detail_public_non_admin_unknown_with_dates_same_policy(self):
        from datetime import date

        doc = self._create_doc(
            title="Public unknown date",
            date_start=date(1948, 5, 12),
            date_end=date(1948, 5, 12),
            date_precision=Document.DatePrecision.UNKNOWN,
        )
        self.client.force_login(self.viewer)
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ללא תאריך")
        self.assertNotContains(resp, "1948-05-12")

    def test_default_date_precision_is_unknown_without_dates(self):
        doc = create_ocr_document(
            title="No dates",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        self.assertIsNone(doc.date_start)
        self.assertIsNone(doc.date_end)
        self.assertEqual(doc.date_precision, Document.DatePrecision.UNKNOWN)

    def test_date_precision_choices_include_v1_values(self):
        values = {choice.value for choice in Document.DatePrecision}
        self.assertEqual(
            values,
            {"EXACT_DAY", "MONTH", "YEAR", "RANGE", "UNKNOWN"},
        )

    @patch("documents.views.create_presigned_put", return_value="https://example/upload")
    def test_upload_create_without_date_precision_succeeds(self, _mock_put):
        self.client.force_login(self.staff)
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(
                {
                    "title": "Upload without date precision",
                    "doc_type": "IMAGE",
                    "text_input_type": "HANDWRITTEN",
                    "original_name": "scan.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1000,
                    "date_start": "1948-05-12",
                    "date_end": "1948-05-12",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.date_precision, Document.DatePrecision.UNKNOWN)


class DocumentAccessServiceTests(TestCase):
    def test_document_queryset_for_user_filters_non_admin(self):
        from documents.services.document_access import document_queryset_for_user
        from django.contrib.auth.models import User

        public_doc = create_ocr_document(
            title="Public svc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        private_doc = create_ocr_document(
            title="Private svc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PRIVATE,
        )
        staff = User.objects.create_user(username="svc_staff", is_staff=True)
        viewer = User.objects.create_user(username="svc_viewer", is_staff=False)

        viewer_ids = set(document_queryset_for_user(viewer).values_list("id", flat=True))
        self.assertEqual(viewer_ids, {public_doc.id})

        staff_ids = set(document_queryset_for_user(staff).values_list("id", flat=True))
        self.assertEqual(staff_ids, {public_doc.id, private_doc.id})
