from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import Document, DocumentTextResult
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import EnvConfigError, WorkerEnvConfig
from documents.services.ocr_reprocess import (
    OcrReprocessAssessment,
    OcrReprocessError,
    OcrRetryMode,
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


def _worker_env_config() -> WorkerEnvConfig:
    return WorkerEnvConfig(
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


@override_settings(UPLOADS_BUCKET_NAME="")
class OcrReprocessUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_reprocess_staff",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="ocr_reprocess_user",
            password="test-pass",
            is_staff=False,
        )

    def _detail_url(self, doc_id: int) -> str:
        return reverse("documents-detail-page", kwargs={"doc_id": doc_id})

    def _reprocess_url(self, doc_id: int) -> str:
        return reverse("documents-ocr-reprocess", kwargs={"doc_id": doc_id})

    def test_staff_sees_retry_button_for_eligible_failed_ocr_document(self):
        doc = _failed_ocr_document()
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._reprocess_url(doc.id))
        self.assertContains(resp, "נסה עיבוד מחדש")

    def test_anonymous_does_not_see_retry_button(self):
        doc = _failed_ocr_document(visibility=Document.Visibility.PUBLIC)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "נסה עיבוד מחדש")

    def test_non_staff_does_not_see_retry_button(self):
        doc = _failed_ocr_document(visibility=Document.Visibility.PUBLIC)
        self.client.force_login(self.user)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "נסה עיבוד מחדש")

    def test_ineligible_not_failed_hides_retry_button(self):
        doc = _failed_ocr_document(
            processing_state_user=Document.ProcessingState.READY,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertNotContains(resp, "נסה עיבוד מחדש")

    def test_ineligible_verified_text_hides_retry_button(self):
        doc = _failed_ocr_document()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-test",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified text",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id))
        self.assertNotContains(resp, "נסה עיבוד מחדש")

    @patch("documents.views.validate_required_env")
    @patch("documents.views.apply_ocr_reprocess")
    def test_post_success_calls_apply_and_redirects_with_success_message(
        self, mock_apply, mock_validate_env
    ):
        doc = _failed_ocr_document()
        mock_validate_env.return_value = _worker_env_config()
        mock_apply.return_value = OcrReprocessAssessment(
            document_id=doc.id,
            retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
        )

        self.client.force_login(self.staff)
        resp = self.client.post(self._reprocess_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._detail_url(doc.id))
        mock_apply.assert_called_once_with(
            doc.id,
            collection_id=COLLECTION_ID,
            model_id=MODEL_ID,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("normal_reenqueue", messages[0])

    def test_post_blocked_for_non_staff(self):
        doc = _failed_ocr_document()
        self.client.force_login(self.user)
        resp = self.client.post(self._reprocess_url(doc.id))
        self.assertEqual(resp.status_code, 403)

    @patch("documents.views.apply_ocr_reprocess")
    def test_anonymous_post_redirects_to_login_without_mutation(self, mock_apply):
        doc = _failed_ocr_document()
        resp = self.client.post(self._reprocess_url(doc.id))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        mock_apply.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.views.validate_required_env")
    @patch("documents.services.ocr_reprocess.send_process_document_message")
    def test_post_enqueue_failure_redirects_with_error_and_keeps_failed_state(
        self, mock_enqueue, mock_validate_env
    ):
        doc = _failed_ocr_document()
        mock_validate_env.return_value = _worker_env_config()
        mock_enqueue.side_effect = RuntimeError("sqs down for test")

        self.client.force_login(self.staff)
        resp = self.client.post(self._reprocess_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._detail_url(doc.id))
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("sqs down for test", messages[0])
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.views.validate_required_env")
    @patch("documents.views.apply_ocr_reprocess")
    def test_post_service_error_redirects_with_error_message(
        self, mock_apply, mock_validate_env
    ):
        doc = _failed_ocr_document()
        mock_validate_env.return_value = _worker_env_config()
        mock_apply.side_effect = OcrReprocessError("reprocess blocked for test")

        self.client.force_login(self.staff)
        resp = self.client.post(self._reprocess_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("reprocess blocked for test", messages[0])

    @patch("documents.views.apply_ocr_reprocess")
    @patch("documents.views.validate_required_env")
    def test_post_env_config_error_redirects_with_message_without_mutation(
        self, mock_validate_env, mock_apply
    ):
        doc = _failed_ocr_document()
        mock_validate_env.side_effect = EnvConfigError("env missing for test")

        self.client.force_login(self.staff)
        resp = self.client.post(self._reprocess_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._detail_url(doc.id))
        mock_apply.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertTrue(
            "שגיאת תצורה" in messages[0] or "env missing for test" in messages[0]
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)

    @patch("documents.views.apply_ocr_reprocess")
    def test_get_to_action_endpoint_does_not_mutate(self, mock_apply):
        doc = _failed_ocr_document()
        self.client.force_login(self.staff)
        resp = self.client.get(self._reprocess_url(doc.id))
        self.assertEqual(resp.status_code, 405)
        mock_apply.assert_not_called()
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.FAILED)
