from __future__ import annotations

import json
from dataclasses import replace
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from documents.management.commands.run_worker import Command as RunWorkerCommand
from documents.models import Document, DocumentTextResult, TranskribusRun
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import WorkerEnvConfig
from documents.services.htr_adapters.base import HtrResult
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    OcrRetryMode,
    apply_ocr_reprocess,
    assess_ocr_reprocess,
)

COLLECTION_ID = "col"
MODEL_ID = "42"

_TRANSKRIBUS_WORKER_ENV_FIELDS = {
    "transkribus_api_token": "tok",
    "transkribus_username": "u",
    "transkribus_password": "p",
    "transkribus_collection_id": COLLECTION_ID,
    "transkribus_model_id": MODEL_ID,
}


def _failed_ocr_document(**kwargs) -> Document:
    defaults = {
        "title": "Failed OCR doc",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.HEBREW,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.FAILED,
        "file_s3_key": "doc.pdf",
        "mime_type": "application/pdf",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _seed_transkribus_run(
    doc: Document,
    *,
    status: str,
    remote_doc_id: str | None = None,
    pages_query: str | None = None,
    error_code: str | None = None,
) -> TranskribusRun:
    return TranskribusRun.objects.create(
        document=doc,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id=COLLECTION_ID,
        model_id=MODEL_ID,
        status=status,
        remote_doc_id=remote_doc_id,
        pages_query=pages_query,
        page_index_to_page_nr={0: 1} if remote_doc_id else None,
        upload_id=10 if remote_doc_id else None,
        ingest_job_id="ingest-1" if remote_doc_id else None,
        error_code=error_code,
    )


class OcrReprocessServiceTests(TestCase):
    def test_dry_run_upload_failure_classifies_normal_reenqueue(self):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertIsNone(assessment.source_transkribus_run_id)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.services.ocr_reprocess.send_process_document_message")
    def test_apply_enqueue_failure_rolls_back_processing_state(self, mock_enqueue):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )
        mock_enqueue.side_effect = RuntimeError("sqs down for test")

        with self.assertRaises(OcrReprocessError) as ctx:
            apply_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )

        self.assertIn("sqs down for test", str(ctx.exception))
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.services.ocr_reprocess.send_process_document_message")
    def test_apply_normal_sets_processing_and_enqueues_default_payload(
        self, mock_enqueue
    ):
        doc = _failed_ocr_document(upload_error="prior enqueue failed")
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )

        assessment = apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PROCESSING)
        self.assertIsNone(doc.upload_error)
        mock_enqueue.assert_called_once_with(doc.id)

    def test_dry_run_recognition_failure_classifies_recognition_only(self):
        doc = _failed_ocr_document()
        source = _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        assessment = assess_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        self.assertEqual(
            assessment.retry_mode, OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        )
        self.assertEqual(assessment.source_transkribus_run_id, source.id)

    @patch("documents.services.ocr_reprocess.send_process_document_message")
    def test_apply_recognition_only_enqueues_explicit_retry_mode(self, mock_enqueue):
        doc = _failed_ocr_document()
        source = _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id="555",
            pages_query="1",
            error_code="TRANSKRIBUS_RECOGNITION_FAILED",
        )

        apply_ocr_reprocess(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )

        mock_enqueue.assert_called_once_with(
            doc.id,
            ocr_retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value,
            source_transkribus_run_id=source.id,
        )

    def test_verified_text_blocks_reprocess(self):
        doc = _failed_ocr_document()
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

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("VERIFIED", str(ctx.exception))

    def test_non_uploaded_document_blocks(self):
        doc = _failed_ocr_document(upload_status=Document.UploadStatus.UPLOADING)

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("UPLOADED", str(ctx.exception))

    def test_non_failed_document_blocks(self):
        doc = _failed_ocr_document(
            processing_state_user=Document.ProcessingState.PARTIAL
        )

        with self.assertRaises(OcrReprocessError) as ctx:
            assess_ocr_reprocess(
                doc.id,
                collection_id=COLLECTION_ID,
                model_id=MODEL_ID,
            )
        self.assertIn("FAILED", str(ctx.exception))

class SendProcessDocumentMessageTests(SimpleTestCase):
    @patch.dict("os.environ", {"SQS_QUEUE_URL": "https://sqs.example/queue"}, clear=False)
    @patch("documents.services.sqs.boto3.client")
    def test_normal_payload_omits_retry_fields(self, mock_boto_client):
        from documents.services.sqs import send_process_document_message

        mock_sqs = mock_boto_client.return_value
        send_process_document_message(123)
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(body, {"type": "PROCESS_DOCUMENT", "document_id": 123})

    @patch.dict("os.environ", {"SQS_QUEUE_URL": "https://sqs.example/queue"}, clear=False)
    @patch("documents.services.sqs.boto3.client")
    def test_recognition_only_payload_includes_source_run_id(self, mock_boto_client):
        from documents.services.sqs import send_process_document_message

        mock_sqs = mock_boto_client.return_value
        send_process_document_message(
            123,
            ocr_retry_mode="transkribus_recognition_only",
            source_transkribus_run_id=456,
        )
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        self.assertEqual(
            body,
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": 123,
                "ocr_retry_mode": "transkribus_recognition_only",
                "source_transkribus_run_id": 456,
            },
        )


class OcrReprocessCommandTests(TestCase):
    @patch(
        "documents.management.commands.reprocess_failed_ocr_document.validate_required_env"
    )
    def test_command_dry_run_reports_retry_mode(self, mock_validate_env):
        doc = _failed_ocr_document()
        _seed_transkribus_run(
            doc,
            status=TranskribusRun.Status.FAILED,
            remote_doc_id=None,
            error_code="TRANSKRIBUS_UPLOAD_FAILED",
        )
        mock_validate_env.return_value = WorkerEnvConfig(
            gemini_api_key="key",
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
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            **_TRANSKRIBUS_WORKER_ENV_FIELDS,
        )

        stdout = StringIO()
        call_command("reprocess_failed_ocr_document", doc.id, stdout=stdout)
        output = stdout.getvalue()

        self.assertIn("mode=dry-run", output)
        self.assertIn("retry_mode=normal_reenqueue", output)
        self.assertIn(f"collection_id={COLLECTION_ID!r}", output)
        self.assertIn(f"model_id={MODEL_ID!r}", output)
        self.assertIn("no changes made", output)


class RunWorkerOcrRetryModeTests(TestCase):
    def setUp(self):
        self.command = RunWorkerCommand()
        self.base_cfg = WorkerEnvConfig(
            gemini_api_key="key",
            gemini_confidence_threshold=0.7,
            min_text_length=5,
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
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=2048,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.7,
            transkribus_recognition_only_retry=False,
            **_TRANSKRIBUS_WORKER_ENV_FIELDS,
        )
        self.command._cfg = self.base_cfg
        self.doc = create_ocr_document(
            title="Worker retry doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="doc.pdf",
            mime_type="application/pdf",
        )

    def _message(
        self,
        *,
        ocr_retry_mode: str | None = None,
        source_transkribus_run_id: int | None = None,
    ) -> dict:
        payload = {"type": "PROCESS_DOCUMENT", "document_id": self.doc.id}
        if ocr_retry_mode is not None:
            payload["ocr_retry_mode"] = ocr_retry_mode
        if source_transkribus_run_id is not None:
            payload["source_transkribus_run_id"] = source_transkribus_run_id
        return {"Body": json.dumps(payload)}

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_normal_payload_unchanged_worker_env(
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

        call_kw = mock_transcribe.call_args.kwargs
        self.assertIs(call_kw["worker_env"], self.base_cfg)
        self.assertFalse(call_kw["worker_env"].transkribus_recognition_only_retry)
        self.assertFalse(call_kw["worker_env"].transkribus_force_reprocess)
        self.assertIsNone(call_kw.get("source_transkribus_run_id"))

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"},
        clear=False,
    )
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_payload_sets_per_job_retry_flag(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.language = Document.Language.HEBREW
        self.doc.text_input_type = Document.TextInputType.HANDWRITTEN
        self.doc.save(update_fields=["language", "text_input_type"])
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="recognized text",
            needs_review=False,
            engine_name="transkribus-pylaia:42",
            review_reasons=[],
        )

        self.assertTrue(
            self.command._process_message(
                self._message(
                    ocr_retry_mode="transkribus_recognition_only",
                    source_transkribus_run_id=456,
                )
            )
        )

        mock_transcribe.assert_called_once()
        call_kw = mock_transcribe.call_args.kwargs
        worker_env = call_kw["worker_env"]
        expected = replace(self.base_cfg, transkribus_recognition_only_retry=True)
        self.assertEqual(worker_env, expected)
        self.assertIsNot(worker_env, self.base_cfg)
        self.assertFalse(self.base_cfg.transkribus_recognition_only_retry)
        self.assertEqual(call_kw["source_transkribus_run_id"], 456)

    @patch("documents.management.commands.run_worker.select_ocr_route")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_non_transkribus_route_fails_before_transcribe(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_select_route,
    ):
        from documents.services.ocr_routing import OcrRouteConfig

        self.doc.language = Document.Language.HEBREW
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["language", "processing_state_user"])
        mock_select_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]

        self.assertTrue(
            self.command._process_message(
                self._message(
                    ocr_retry_mode="transkribus_recognition_only",
                    source_transkribus_run_id=456,
                )
            )
        )

        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, Document.ProcessingState.FAILED)
        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertIn("transkribus_recognition_only", failure.error_details or "")
        self.assertIn(DocumentTextResult.OcrEngineKey.GEMINI, failure.error_details or "")

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_unknown_ocr_retry_mode_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])

        self.assertTrue(
            self.command._process_message(
                self._message(ocr_retry_mode="not_a_real_mode")
            )
        )

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_missing_source_run_id_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])

        self.assertTrue(
            self.command._process_message(
                self._message(ocr_retry_mode="transkribus_recognition_only")
            )
        )

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_recognition_only_non_int_source_run_id_skips_processing_and_acks(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        self.doc.processing_state_user = Document.ProcessingState.FAILED
        self.doc.save(update_fields=["processing_state_user"])
        payload = {
            "type": "PROCESS_DOCUMENT",
            "document_id": self.doc.id,
            "ocr_retry_mode": "transkribus_recognition_only",
            "source_transkribus_run_id": "456",
        }

        self.assertTrue(self.command._process_message({"Body": json.dumps(payload)}))

        mock_get_object_bytes.assert_not_called()
        mock_extract_pages.assert_not_called()
        mock_transcribe.assert_not_called()
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, Document.ProcessingState.FAILED)
