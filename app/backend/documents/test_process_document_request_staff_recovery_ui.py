from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
)
from documents.services.archive_items import create_ocr_document
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
)
from documents.test_hebrew_translation_retry import (
    ENGINE,
    _failed_hebrew,
    _usable_source,
)
from documents.test_restricted_visibility_access import _grant_restricted_permission


def _hebrew_doc(**kwargs) -> Document:
    defaults = {
        "title": "Staff abandon UI OCR",
        "doc_type": Document.DocType.PDF,
        "language": Document.Language.HEBREW,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.RECOVERY_REQUIRED,
        "file_s3_key": "staff-abandon-ui.pdf",
        "mime_type": "application/pdf",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _rr_request(document: Document, **overrides) -> ProcessDocumentRequest:
    now = timezone.now()
    values = {
        "document": document,
        "status": ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        "operation": ProcessDocumentRequest.Operation.OCR,
        "origin": ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
        "ocr_retry_mode": ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
        "lease_token": uuid.uuid4(),
        "started_at": now - timedelta(hours=1),
    }
    values.update(overrides)
    return ProcessDocumentRequest.objects.create(**values)


@override_settings(UPLOADS_BUCKET_NAME="")
class ProcessDocumentRequestStaffAbandonUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="staff_abandon_ui",
            password="test-pass",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            username="viewer_abandon_ui",
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

    def _reprocess_url(self, doc_id: int) -> str:
        return reverse("documents-ocr-reprocess", kwargs={"doc_id": doc_id})

    def _translation_retry_url(self, doc_id: int) -> str:
        return reverse(
            "documents-hebrew-translation-retry",
            kwargs={"doc_id": doc_id},
        )

    def test_staff_get_shows_abandon_control_for_valid_rr_request(self):
        document = _hebrew_doc()
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._abandon_url(document.id, request.pk))
        self.assertContains(resp, "שחרור בקשת עיבוד תקועה")
        self.assertContains(
            resp,
            "פעולה זו מסיימת את בקשת העיבוד התקועה ואינה מתחילה עיבוד חדש.",
        )

    def test_staff_get_shows_abandon_when_document_overlay_is_ready(self):
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.READY,
        )
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="usable unverified hebrew",
        )
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._abandon_url(document.id, request.pk))
        self.assertContains(resp, "שחרור בקשת עיבוד תקועה")
        self.assertNotContains(resp, "נסה עיבוד מחדש")

    def test_overlay_recovery_required_without_request_hides_abandon_control(self):
        document = _hebrew_doc()
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "שחרור בקשת עיבוד תקועה")
        self.assertNotContains(resp, "process-document-requests")

    def test_anonymous_and_non_staff_do_not_see_abandon_control(self):
        document = _hebrew_doc(visibility=Document.Visibility.PUBLIC)
        request = _rr_request(document)

        anonymous = self.client.get(self._detail_url(document.id))
        self.assertEqual(anonymous.status_code, 200)
        self.assertNotContains(anonymous, "שחרור בקשת עיבוד תקועה")
        self.assertNotContains(
            anonymous,
            self._abandon_url(document.id, request.pk),
        )

        self.client.force_login(self.user)
        viewer = self.client.get(self._detail_url(document.id))
        self.assertEqual(viewer.status_code, 200)
        self.assertNotContains(viewer, "שחרור בקשת עיבוד תקועה")
        self.assertNotContains(
            viewer,
            self._abandon_url(document.id, request.pk),
        )

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_staff_post_abandons_parked_request(self, mock_send):
        document = _hebrew_doc()
        request = _rr_request(document)
        started_at = request.started_at
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._abandon_url(document.id, request.pk),
            follow=True,
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._detail_url(document.id))
        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertIsNone(request.lease_token)
        self.assertIsNone(request.lease_expires_at)
        self.assertIsNotNone(request.completed_at)
        self.assertEqual(request.started_at, started_at)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        mock_send.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, ["בקשת העיבוד התקועה שוחררה. לא נשלח עיבוד חדש."])
        self.assertNotContains(resp, "שחרור בקשת עיבוד תקועה")

    @patch(
        "documents.services.process_document_request_enqueue."
        "send_process_document_request_message"
    )
    def test_second_post_is_already_terminal(self, mock_send):
        document = _hebrew_doc()
        request = _rr_request(document)
        self.client.force_login(self.staff)
        first = self.client.post(
            self._abandon_url(document.id, request.pk),
            follow=True,
        )
        first_messages = [str(m) for m in get_messages(first.wsgi_request)]
        self.assertEqual(
            first_messages,
            ["בקשת העיבוד התקועה שוחררה. לא נשלח עיבוד חדש."],
        )

        resp = self.client.post(
            self._abandon_url(document.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        mock_send.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, ["בקשת העיבוד כבר הסתיימה."])

    def test_non_abandonable_request_fails_closed(self):
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        now = timezone.now()
        running = ProcessDocumentRequest.objects.create(
            document=document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._abandon_url(document.id, running.pk),
            follow=True,
        )

        running.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(running.status, ProcessDocumentRequest.Status.RUNNING)
        self.assertIsNotNone(running.lease_token)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, ["לא ניתן לשחרר בקשה שאינה במצב שחזור."])

    def test_invalid_recovery_shape_post_is_refused(self):
        # Malformed RR lease rows cannot be stored (proc_req_recovery_shape).
        # The persistable INVALID_RECOVERY_SHAPE path is a constraint-valid
        # RR Request whose Document overlay is still PROCESSING.
        document = _hebrew_doc(
            processing_state_user=Document.ProcessingState.PROCESSING,
        )
        request = _rr_request(document)
        lease_token = request.lease_token
        started_at = request.started_at
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._abandon_url(document.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertEqual(request.lease_token, lease_token)
        self.assertIsNone(request.lease_expires_at)
        self.assertEqual(request.started_at, started_at)
        self.assertIsNone(request.completed_at)
        self.assertEqual(request.failure_code, "")
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            messages,
            ["לא ניתן לשחרר את בקשת העיבוד במצבה הנוכחי."],
        )

    def test_request_document_mismatch_fails_closed(self):
        document = _hebrew_doc()
        other = _hebrew_doc(
            title="Other abandon UI doc",
            file_s3_key="other-abandon-ui.pdf",
        )
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._abandon_url(other.id, request.pk),
            follow=True,
        )

        request.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )
        self.assertIsNotNone(request.lease_token)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, ["לא נמצאה בקשת עיבוד תואמת למסמך זה."])

    def test_restricted_document_post_404_without_permission(self):
        document = _hebrew_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.post(self._abandon_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 404)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_restricted_document_post_succeeds_with_permission(self):
        staff_with_perm = User.objects.create_user(
            username="staff_abandon_restricted",
            password="test-pass",
            is_staff=True,
        )
        _grant_restricted_permission(staff_with_perm)
        document = _hebrew_doc(visibility=ArchiveItem.Visibility.RESTRICTED)
        request = _rr_request(document)
        self.client.force_login(staff_with_perm)

        resp = self.client.post(self._abandon_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 302)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)

    def test_post_requires_csrf(self):
        document = _hebrew_doc()
        request = _rr_request(document)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)

        resp = csrf_client.post(self._abandon_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 403)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_anonymous_post_redirects_to_login_without_mutation(self):
        document = _hebrew_doc()
        request = _rr_request(document)

        resp = self.client.post(self._abandon_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_non_staff_post_is_forbidden_without_mutation(self):
        document = _hebrew_doc(visibility=Document.Visibility.PUBLIC)
        request = _rr_request(document)
        self.client.force_login(self.user)

        resp = self.client.post(self._abandon_url(document.id, request.pk))

        self.assertEqual(resp.status_code, 403)
        request.refresh_from_db()
        self.assertEqual(
            request.status,
            ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        )

    def test_parked_request_hides_reprocess_and_translation_retry(self):
        document = create_ocr_document(
            title="Parked request hides retries",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.FAILED,
            file_s3_key="parked-retry-hide.pdf",
            mime_type="application/pdf",
        )
        _usable_source(document)
        _failed_hebrew(document)
        request = _rr_request(document)
        self.client.force_login(self.staff)

        resp = self.client.get(self._detail_url(document.id))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._abandon_url(document.id, request.pk))
        self.assertNotContains(resp, "נסה עיבוד מחדש")
        self.assertNotContains(resp, "נסה תרגום לעברית מחדש")
        self.assertNotContains(resp, self._reprocess_url(document.id))
        self.assertNotContains(resp, self._translation_retry_url(document.id))
