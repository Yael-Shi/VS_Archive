"""Staff enqueue UI for corrected/current Transkribus sync (fetch/sync only)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from documents.models import Document, TranskribusRun
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_corrected_current_sync_enqueue import (
    EnqueueResult,
)
from documents.views import (
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_QUEUED,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_RUNNING,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_TERMINAL,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_BLOCKED_RECOVERY,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_CREATED,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_FAILED,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_INELIGIBLE,
    _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_OUTCOME_UNKNOWN,
)

User = get_user_model()

_ENQUEUE_LABEL = "משיכת תעתוק עדכני מ־Transkribus"


def _enqueue_result(*, outcome: str, request_id: int = 99) -> EnqueueResult:
    if outcome in {"CREATED_AND_ENQUEUED", "REENQUEUED", "ALREADY_TERMINAL"}:
        message_sent: bool | None = True
    elif outcome == "ENQUEUE_OUTCOME_UNKNOWN":
        message_sent = None
    else:
        message_sent = False
    return EnqueueResult(
        outcome=outcome,  # type: ignore[arg-type]
        request=SimpleNamespace(pk=request_id),  # type: ignore[arg-type]
        created=outcome == "CREATED_AND_ENQUEUED",
        message_sent=message_sent,
        observed_status="QUEUED",
        send_attempted=outcome
        not in {
            "ALREADY_QUEUED",
            "ALREADY_RUNNING",
            "BLOCKED_RECOVERY_REQUIRED",
        },
    )


@override_settings(UPLOADS_BUCKET_NAME="")
class CorrectedCurrentSyncStaffEnqueueUITests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="cc_enqueue_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="cc_enqueue_viewer",
            password="test-pass",
            is_staff=False,
        )
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _create_doc(self, **kwargs) -> Document:
        defaults: dict[str, Any] = dict(
            title="Corrected sync enqueue doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/cc-enqueue/original.pdf",
            mime_type="application/pdf",
            visibility=Document.Visibility.PUBLIC,
        )
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _upload_run(self, doc: Document) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
            engine_runtime="transkribus-pylaia:42",
        )

    def _list_url(self, doc_id: int) -> str:
        return reverse("corrected-current-sync-attempts", kwargs={"doc_id": doc_id})

    def _enqueue_url(self, doc_id: int) -> str:
        return reverse("corrected-current-sync-enqueue", kwargs={"doc_id": doc_id})

    def _eligible_doc(self) -> Document:
        doc = self._create_doc()
        self._upload_run(doc)
        return doc

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_eligible_staff_post_enqueues_and_redirects(self, mock_enqueue):
        doc = self._eligible_doc()
        mock_enqueue.return_value = _enqueue_result(outcome="CREATED_AND_ENQUEUED")
        self.client.force_login(self.staff)

        resp = self.client.post(self._enqueue_url(doc.id))

        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._list_url(doc.id))
        mock_enqueue.assert_called_once_with(
            document_id=doc.id,
            initiated_by=self.staff,
        )

    def test_get_enqueue_endpoint_does_not_mutate(self):
        doc = self._eligible_doc()
        self.client.force_login(self.staff)
        with patch(
            "documents.views.enqueue_transkribus_corrected_current_sync"
        ) as mock_enqueue:
            resp = self.client.get(self._enqueue_url(doc.id))
        self.assertEqual(resp.status_code, 405)
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_non_staff_cannot_enqueue(self, mock_enqueue):
        doc = self._eligible_doc()
        self.client.force_login(self.viewer)
        resp = self.client.post(self._enqueue_url(doc.id))
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_anonymous_post_redirects_to_login(self, mock_enqueue):
        doc = self._eligible_doc()
        resp = self.client.post(self._enqueue_url(doc.id))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_ineligible_document_cannot_enqueue(self, mock_enqueue):
        doc = self._create_doc(
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(self._enqueue_url(doc.id), follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._list_url(doc.id))
        mock_enqueue.assert_not_called()
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_INELIGIBLE])

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_list_page_renders_csrf_enqueue_form(self, mock_enqueue):
        doc = self._eligible_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp.context["show_transkribus_corrected_current_sync_enqueue_action"]
        )
        self.assertContains(resp, _ENQUEUE_LABEL)
        self.assertContains(resp, 'name="csrfmiddlewaretoken"')
        self.assertContains(resp, 'class="corrected-sync-enqueue-form"')
        self.assertContains(resp, self._enqueue_url(doc.id))
        self.assertNotContains(resp, "corrected-sync-activation-form")
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_ineligible_list_page_hides_enqueue_form(self, mock_enqueue):
        doc = self._create_doc(
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            resp.context["show_transkribus_corrected_current_sync_enqueue_action"]
        )
        self.assertNotContains(resp, _ENQUEUE_LABEL)
        self.assertNotContains(resp, "corrected-sync-enqueue-form")
        self.assertNotContains(resp, self._enqueue_url(doc.id))
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_post_requires_csrf(self, mock_enqueue):
        doc = self._eligible_doc()
        self.csrf_client.force_login(self.staff)
        resp = self.csrf_client.post(self._enqueue_url(doc.id))
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    def _assert_outcome_message(self, *, outcome: str, expected: str, level: str):
        doc = self._eligible_doc()
        with patch(
            "documents.views.enqueue_transkribus_corrected_current_sync"
        ) as mock_enqueue:
            mock_enqueue.return_value = _enqueue_result(outcome=outcome)
            self.client.force_login(self.staff)
            resp = self.client.post(self._enqueue_url(doc.id), follow=True)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.request["PATH_INFO"], self._list_url(doc.id))
        stored = list(get_messages(resp.wsgi_request))
        self.assertEqual(len(stored), 1)
        self.assertEqual(str(stored[0]), expected)
        self.assertEqual(stored[0].level_tag, level)
        mock_enqueue.assert_called_once()

    def test_created_and_enqueued_success_message(self):
        self._assert_outcome_message(
            outcome="CREATED_AND_ENQUEUED",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_CREATED,
            level="success",
        )

    def test_reenqueued_success_message(self):
        self._assert_outcome_message(
            outcome="REENQUEUED",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_CREATED,
            level="success",
        )

    def test_already_queued_message(self):
        self._assert_outcome_message(
            outcome="ALREADY_QUEUED",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_QUEUED,
            level="success",
        )

    def test_already_running_message(self):
        self._assert_outcome_message(
            outcome="ALREADY_RUNNING",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_RUNNING,
            level="success",
        )

    def test_blocked_recovery_required_message(self):
        self._assert_outcome_message(
            outcome="BLOCKED_RECOVERY_REQUIRED",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_BLOCKED_RECOVERY,
            level="error",
        )

    def test_enqueue_failed_message(self):
        self._assert_outcome_message(
            outcome="ENQUEUE_FAILED",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_FAILED,
            level="error",
        )

    def test_enqueue_outcome_unknown_message(self):
        self._assert_outcome_message(
            outcome="ENQUEUE_OUTCOME_UNKNOWN",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_OUTCOME_UNKNOWN,
            level="warning",
        )

    def test_already_terminal_message_does_not_claim_activation(self):
        """ALREADY_TERMINAL is returned when send is accepted and the worker
        terminalizes before post-send CAS reload (see enqueue service tests)."""
        self._assert_outcome_message(
            outcome="ALREADY_TERMINAL",
            expected=_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_TERMINAL,
            level="success",
        )
        self.assertIn("לא הוחלף", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_TERMINAL)

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_coalescing_delegated_to_enqueue_service(self, mock_enqueue):
        """View must call the shared enqueue service; not reimplement coalesce."""
        doc = self._eligible_doc()
        mock_enqueue.return_value = _enqueue_result(outcome="ALREADY_QUEUED")
        self.client.force_login(self.staff)

        for _ in range(2):
            resp = self.client.post(self._enqueue_url(doc.id))
            self.assertEqual(resp.status_code, 302)
            self.assertEqual(resp["Location"], self._list_url(doc.id))

        self.assertEqual(mock_enqueue.call_count, 2)
        for call in mock_enqueue.call_args_list:
            self.assertEqual(call.kwargs["document_id"], doc.id)
            self.assertEqual(call.kwargs["initiated_by"], self.staff)

    @patch("documents.views.activate_corrected_current_sync_attempt")
    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    def test_enqueue_does_not_activate(self, mock_enqueue, mock_activate):
        doc = self._eligible_doc()
        mock_enqueue.return_value = _enqueue_result(outcome="CREATED_AND_ENQUEUED")
        self.client.force_login(self.staff)
        self.client.post(self._enqueue_url(doc.id))
        mock_activate.assert_not_called()
        mock_enqueue.assert_called_once()
