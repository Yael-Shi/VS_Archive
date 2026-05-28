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
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image

from documents.management.commands.run_worker import Command
from documents.models import Document, DocumentSourceFile, DocumentTextResult, TranskribusRun
from documents.services.env_validation import validate_required_env
from documents.services.transkribus_engine import PylaiaTranscriptionOutcome
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
from documents.services.ocr_routing import OcrRouteConfig, OCR_ROUTES, select_ocr_route


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


class TranskribusAdapterTests(TestCase):
    def _create_document(self) -> Document:
        return Document.objects.create(
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
        he_doc = Document.objects.create(
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

        he_doc = Document.objects.create(
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
        he_doc = Document.objects.create(
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
        he_doc = Document.objects.create(
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
        return Document.objects.create(
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
        doc = Document.objects.create(
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
        return Document.objects.create(
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
        return Document.objects.create(
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


class TranskribusRunPersistenceServiceTests(TestCase):
    def _create_document(self) -> Document:
        return Document.objects.create(
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
        return Document.objects.create(
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


class TranskribusRunPersistenceGuardTests(TestCase):
    def _create_document(self) -> Document:
        return Document.objects.create(
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
        return Document.objects.create(
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
        return Document.objects.create(
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
        return Document.objects.create(
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
    ):
        from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
        from documents.services.page_extraction import PageImage

        if pages is None:
            pages = [PageImage(page_index=0, image_bytes=b"x", mime_type="image/png")]
        overrides = {"transkribus_dev_upload_mode": True}
        if worker_env_overrides:
            overrides.update(worker_env_overrides)
        adapter = TranskribusAdapter()
        return adapter.execute(
            pages=pages,
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_transkribus_adapter_worker_env(**overrides),
            document_id=doc.id,
        )

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
        return Document.objects.create(**defaults)

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
        self.assertContains(resp, "SOURCE_TEXT")
        self.assertContains(resp, "HEBREW_TEXT")
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
        self.assertContains(resp, "NEEDS_REVIEW")
        self.assertContains(resp, "UNVERIFIED")
        self.assertContains(resp, "TRANSKRIBUS")
        self.assertContains(resp, "AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW")
        self.assertContains(resp, "טקסט עברי לבדיקה")
        self.assertContains(resp, "שורת בדיקה")

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
        self.assertContains(resp, "עריכת טקסט")
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
        self.assertContains(resp, "השווי בין קובץ המקור לתמלול")
        self.assertContains(resp, "אימות תמלול")
        self.assertContains(resp, "פרטים טכניים")

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
