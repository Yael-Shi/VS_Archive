from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import ArchiveItem, Document, ProcessDocumentRequest
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
)
from documents.services.process_document_request_staff_retry import (
    LIVE_PAGE_LEASE_PUBLIC_MESSAGE,
    REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE,
)
from documents.services.sqs import SqsConfigurationError
from documents.test_hebrew_translation_retry import (
    _failed_hebrew,
    _non_hebrew_doc,
    _usable_source,
)
from documents.test_process_document_request_staff_retry import (
    _live_arabic_page,
    _ocr_doc,
    _rr_request,
)
from documents.test_restricted_visibility_access import _grant_restricted_permission


def _worker_env():
    return SimpleNamespace(transkribus_collection_id="", transkribus_model_id="")


SEND_PATCH = (
    "documents.services.process_document_request_enqueue."
    "send_process_document_request_message"
)
ENV_PATCH = (
    "documents.services.process_document_request_staff_retry.validate_required_env"
)


@override_settings(UPLOADS_BUCKET_NAME="")
class ProcessDocumentRequestStaffRetryUiTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff_retry_ui",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="viewer_retry_ui",
            password="test-pass",
            is_staff=False,
        )

    def _detail_url(self, doc_id: int) -> str:
        return reverse("documents-detail-page", kwargs={"doc_id": doc_id})

    def _abandon_url(self, doc_id: int, request_id: int) -> str:
        return reverse(
            "documents-process-document-request-abandon",
            kwargs={"doc_id": doc_id, "request_id": request_id},
        )

    def _retry_url(self, doc_id: int, request_id: int) -> str:
        return reverse(
            "documents-process-document-request-retry",
            kwargs={"doc_id": doc_id, "request_id": request_id},
        )

    def _reprocess_url(self, doc_id: int) -> str:
        return reverse("documents-ocr-reprocess", kwargs={"doc_id": doc_id})

    def _translation_retry_url(self, doc_id: int) -> str:
        return reverse(
            "documents-hebrew-translation-retry",
            kwargs={"doc_id": doc_id},
        )

    def test_staff_get_shows_abandon_and_ocr_retry(self):
        document = _ocr_doc()
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._abandon_url(document.id, request.pk))
        self.assertContains(resp, "שחרור בקשת עיבוד תקועה")
        self.assertContains(resp, self._retry_url(document.id, request.pk))
        self.assertContains(resp, "התחל עיבוד OCR חדש")
        self.assertContains(resp, "בקשת עיבוד OCR חדשה")
        self.assertNotContains(resp, "נסה עיבוד מחדש")
        self.assertNotContains(resp, "נסה תרגום לעברית מחדש")
        self.assertNotContains(resp, self._reprocess_url(document.id))
        self.assertNotContains(resp, self._translation_retry_url(document.id))

    def test_staff_get_shows_hebrew_retry_copy(self):
        document = _non_hebrew_doc(
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
        )
        _usable_source(document)
        _failed_hebrew(document)
        _rr_request(
            document,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "שחרור בקשת עיבוד תקועה")
        self.assertContains(resp, "התחל תרגום לעברית חדש")
        self.assertContains(resp, "בקשת תרגום לעברית חדשה")
        self.assertNotContains(resp, "התחל עיבוד OCR חדש")
        self.assertNotContains(resp, "נסה תרגום לעברית מחדש")
        self.assertNotContains(resp, self._translation_retry_url(document.id))

    @patch(ENV_PATCH, return_value=_worker_env())
    @patch(SEND_PATCH)
    def test_staff_post_retries_ocr(self, mock_send, _mock_env):
        document = _ocr_doc()
        request = _rr_request(document)
        old_pk = request.pk
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._retry_url(document.id, request.pk),
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        new_request = ProcessDocumentRequest.objects.exclude(pk=old_pk).get(
            document=document
        )
        self.assertEqual(
            new_request.origin,
            ProcessDocumentRequest.Origin.OCR_REPROCESS,
        )
        mock_send.assert_called_once_with(new_request.pk)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            messages,
            ["בקשת העיבוד התקועה נסגרה. עיבוד OCR חדש תוזמן."],
        )

    @patch(ENV_PATCH, return_value=_worker_env())
    @patch(SEND_PATCH)
    def test_retry_control_remains_after_abandon_and_enqueue_failure(
        self, mock_send, _mock_env
    ):
        document = _ocr_doc()
        request = _rr_request(document)
        mock_send.side_effect = SqsConfigurationError("queue missing")
        self.client.force_login(self.staff)

        first = self.client.post(
            self._retry_url(document.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.FAILED,
        )
        stranded = ProcessDocumentRequest.objects.exclude(pk=request.pk).get(
            document=document
        )
        self.assertEqual(
            stranded.status,
            ProcessDocumentRequest.Status.ENQUEUE_FAILED,
        )
        self.assertContains(first, self._retry_url(document.id, request.pk))
        self.assertContains(first, "התחל עיבוד OCR חדש")
        self.assertNotContains(first, self._abandon_url(document.id, request.pk))
        self.assertNotContains(first, "שחרור בקשת עיבוד תקועה")
        self.assertNotContains(first, "נסה עיבוד מחדש")
        self.assertNotContains(first, self._reprocess_url(document.id))

        detail = self.client.get(self._detail_url(document.id))
        self.assertContains(detail, self._retry_url(document.id, request.pk))
        self.assertNotContains(detail, self._abandon_url(document.id, request.pk))

        mock_send.side_effect = None
        mock_send.reset_mock()
        second = self.client.post(
            self._retry_url(document.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertEqual(second.status_code, 200)
        mock_send.assert_called_once()
        stranded.refresh_from_db()
        self.assertEqual(
            stranded.status,
            ProcessDocumentRequest.Status.QUEUED,
        )
        messages = [str(m) for m in get_messages(second.wsgi_request)]
        self.assertEqual(
            messages,
            ["בקשת העיבוד התקועה נסגרה. עיבוד OCR חדש תוזמן."],
        )

    @patch(SEND_PATCH)
    def test_staff_post_retries_hebrew_without_ocr_env(self, mock_send):
        document = _non_hebrew_doc(
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
        )
        _usable_source(document)
        _failed_hebrew(document)
        request = _rr_request(
            document,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
        )
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._retry_url(document.id, request.pk),
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        new_request = ProcessDocumentRequest.objects.exclude(pk=request.pk).get(
            document=document
        )
        self.assertEqual(
            new_request.operation,
            ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
        )
        mock_send.assert_called_once_with(new_request.pk)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            messages,
            ["בקשת העיבוד התקועה נסגרה. תרגום לעברית חדש תוזמן."],
        )

    @patch(ENV_PATCH, return_value=_worker_env())
    @patch(SEND_PATCH)
    def test_live_lease_http_does_not_abandon(self, mock_send, _mock_env):
        document = _ocr_doc()
        request = _rr_request(document)
        _live_arabic_page(
            document,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._retry_url(document.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        mock_send.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [LIVE_PAGE_LEASE_PUBLIC_MESSAGE])

    @patch(ENV_PATCH, return_value=_worker_env())
    @patch(SEND_PATCH)
    def test_completed_request_http_does_not_enqueue(self, mock_send, _mock_env):
        document = _ocr_doc(
            processing_state_user=Document.ProcessingState.FAILED,
        )
        request = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.COMPLETED,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            completed_at=timezone.now(),
        )
        self.client.force_login(self.staff)

        resp = self.client.post(self._retry_url(document.id, request.pk), follow=True)

        mock_send.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [REQUEST_ALREADY_FINISHED_PUBLIC_MESSAGE])

    def test_restricted_document_post_404_without_permission(self):
        document = _ocr_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.post(self._retry_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 404)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    @patch(ENV_PATCH, return_value=_worker_env())
    @patch(SEND_PATCH)
    def test_restricted_document_post_succeeds_with_permission(
        self, mock_send, _mock_env
    ):
        staff_with_perm = User.objects.create_user(
            username="staff_retry_restricted",
            password="test-pass",
            is_staff=True,
        )
        _grant_restricted_permission(staff_with_perm)
        document = _ocr_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        request = _rr_request(document)
        self.client.force_login(staff_with_perm)

        resp = self.client.post(self._retry_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 302)
        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        mock_send.assert_called_once()

    def test_post_requires_csrf(self):
        document = _ocr_doc()
        request = _rr_request(document)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)

        resp = csrf_client.post(self._retry_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 403)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_anonymous_post_redirects_to_login_without_mutation(self):
        document = _ocr_doc()
        request = _rr_request(document)

        resp = self.client.post(self._retry_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_non_staff_post_is_forbidden_without_mutation(self):
        document = create_ocr_document(
            title="Public retry UI OCR",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.RECOVERY_REQUIRED,
            file_s3_key="public-retry-ui.pdf",
            mime_type="application/pdf",
            visibility=Document.Visibility.PUBLIC,
        )
        request = _rr_request(document)
        self.client.force_login(self.user)

        resp = self.client.post(self._retry_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 403)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
