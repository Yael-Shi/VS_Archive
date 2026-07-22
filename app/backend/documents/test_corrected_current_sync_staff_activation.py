"""Staff activation UI for corrected/current Transkribus sync attempts (PR2)."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_corrected_current_activation import (
    CorrectedCurrentActivationError,
    CorrectedCurrentActivationErrorCode,
    CorrectedCurrentActivationResult,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.test_corrected_current_sync_staff_preview import (
    _assert_technical_details_collapsed,
    _split_primary_and_technical,
)
from documents.views import (
    _CORRECTED_CURRENT_ACTIVATION_MSG_ALREADY_ACTIVE,
    _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_BINDING_ONLY,
    _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_HEBREW_MIRROR,
    _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE,
    _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC,
    _CORRECTED_CURRENT_ACTIVATION_MSG_HUMAN_EDITED,
    _CORRECTED_CURRENT_ACTIVATION_MSG_MISSING_CONFIRM,
    _CORRECTED_CURRENT_ACTIVATION_MSG_STALE,
    _CORRECTED_CURRENT_ACTIVATION_MSG_VERIFIED,
)

User = get_user_model()

_TEST_PARSER_VERSION = "test_parser_activation_ui_v1"
_ENGINE = "transkribus-pylaia:42"
_CANONICAL = "Corrected canonical text"
_OLD_TEXT = "Old displayed source text"
_ACTION_LABEL = "החלפת התעתוק המוצג בגרסת Transkribus"
_NO_BASELINE_ACTIVATION = "אין תעתוק מקור שמור שאפשר להחליף כרגע."


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


@override_settings(UPLOADS_BUCKET_NAME="")
class CorrectedCurrentSyncStaffActivationUITests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="cc_activation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="cc_activation_viewer",
            password="test-pass",
            is_staff=False,
        )
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _create_doc(self, **kwargs) -> Document:
        defaults: dict[str, Any] = dict(
            title="Corrected sync activation doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/cc-activation/original.pdf",
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
            engine_runtime=_ENGINE,
        )

    def _ready_snapshot(
        self,
        *,
        document: Document,
        run: TranskribusRun,
        canonical_text: str = _CANONICAL,
    ) -> TranskribusTranscriptSnapshot:
        unique = (
            f"{document.pk}:{run.pk}:{TranskribusTranscriptSnapshot.objects.count()}"
        )
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=document,
            transkribus_run=run,
            source_kind=(
                TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC
            ),
            remote_doc_id=str(run.remote_doc_id or ""),
            collection_id=str(run.collection_id or ""),
            model_id=str(run.model_id or ""),
            recognition_job_id=str(run.recognition_job_id or ""),
            parser_version=_TEST_PARSER_VERSION,
            provider_identity_fingerprint=_sha256_hex(f"prov:{unique}"),
            raw_xml_fingerprint=_sha256_hex(f"raw:{unique}"),
            canonical_text=canonical_text,
            canonical_text_sha256=_sha256_hex(canonical_text),
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.PARTIAL
            ),
            hover_eligible=False,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-1",
            page_xml_sha256=_sha256_hex(f"xml:{snapshot.pk}:1"),
            page_xml_s3_key=f"s3://test/{snapshot.pk}/1.xml",
        )
        return snapshot

    def _completed_attempt(
        self,
        *,
        doc: Document,
        run: TranskribusRun,
        snapshot: TranskribusTranscriptSnapshot,
    ) -> TranskribusCorrectedCurrentSyncAttempt:
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-1",
            remote_transcript_status="DONE",
        )
        return attempt

    def _source_row(
        self,
        doc: Document,
        *,
        text: str = _OLD_TEXT,
        source_revision: int = 2,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=verification_status,
            text=text,
            source_revision=source_revision,
        )

    def _hebrew_row(
        self, doc: Document, *, text: str = _OLD_TEXT
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            based_on_source_revision=2,
        )

    def _eligible_fixture(self, *, language: str = Document.Language.HEBREW):
        doc = self._create_doc(language=language)
        run = self._upload_run(doc)
        snapshot = self._ready_snapshot(document=doc, run=run)
        attempt = self._completed_attempt(doc=doc, run=run, snapshot=snapshot)
        source = self._source_row(doc)
        hebrew = None
        if language == Document.Language.HEBREW:
            hebrew = self._hebrew_row(doc)
        return doc, attempt, source, hebrew, snapshot

    def _detail_url(self, doc_id: int, attempt_id: int) -> str:
        return reverse(
            "corrected-current-sync-attempt-detail",
            kwargs={"doc_id": doc_id, "attempt_id": attempt_id},
        )

    def _list_url(self, doc_id: int) -> str:
        return reverse("corrected-current-sync-attempts", kwargs={"doc_id": doc_id})

    def _activate_url(self, doc_id: int, attempt_id: int) -> str:
        return reverse(
            "corrected-current-sync-attempt-activate",
            kwargs={"doc_id": doc_id, "attempt_id": attempt_id},
        )

    def _post_data(
        self,
        source: DocumentTextResult,
        *,
        confirm: str | None = "1",
        source_text_result_id: int | None = None,
        expected_source_revision: int | None = None,
        expected_source_sha256: str | None = None,
    ) -> dict[str, str]:
        data: dict[str, str] = {
            "source_text_result_id": str(
                source.id if source_text_result_id is None else source_text_result_id
            ),
            "expected_source_revision": str(
                source.source_revision
                if expected_source_revision is None
                else expected_source_revision
            ),
            "expected_source_sha256": (
                compute_sha256_hex(source.text or "")
                if expected_source_sha256 is None
                else expected_source_sha256
            ),
        }
        if confirm is not None:
            data["confirm_replace"] = confirm
        return data

    def test_get_detail_is_read_only(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        before_text = source.text
        before_revision = source.source_revision

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, _ACTION_LABEL)

        source.refresh_from_db()
        self.assertEqual(source.text, before_text)
        self.assertEqual(source.source_revision, before_revision)

    def test_form_shown_only_for_eligible_preview_baseline(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["activation_form_available"])
        self.assertContains(resp, "corrected-sync-activation-form")
        self.assertContains(resp, _ACTION_LABEL)
        self.assertContains(resp, 'name="confirm_replace"')
        self.assertContains(resp, f'value="{source.id}"')
        self.assertContains(resp, f'value="{source.source_revision}"')
        self.assertContains(resp, compute_sha256_hex(source.text or ""))
        self.assertContains(
            resp,
            "אינה מסמנת את התעתוק כמאומת על ידי אדם",
        )
        self.assertNotContains(resp, "STALE_PREVIEW")
        self.assertNotContains(resp, "VERIFIED_BLOCKED")

    def test_no_form_on_attempts_list(self):
        doc, attempt, _source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{attempt.id}")
        self.assertNotContains(resp, "corrected-sync-activation-form")
        self.assertNotContains(resp, _ACTION_LABEL)
        self.assertNotContains(resp, "/activate/")
        self.assertNotContains(resp, "confirm_replace")

    def test_no_form_without_source_baseline(self):
        doc = self._create_doc()
        run = self._upload_run(doc)
        snapshot = self._ready_snapshot(document=doc, run=run)
        attempt = self._completed_attempt(doc=doc, run=run, snapshot=snapshot)
        self._hebrew_row(doc)

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["show_activation_section"])
        self.assertFalse(resp.context["activation_form_available"])
        self.assertContains(resp, _NO_BASELINE_ACTIVATION)
        self.assertNotContains(resp, "corrected-sync-activation-form")
        self.assertNotContains(resp, _ACTION_LABEL)

    def test_non_admin_denied_get_and_post(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.viewer)

        get_resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(get_resp.status_code, 403)

        post_resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
        )
        self.assertEqual(post_resp.status_code, 403)
        source.refresh_from_db()
        self.assertEqual(source.text, _OLD_TEXT)

    def test_non_post_method_rejected(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)

        get_resp = self.client.get(self._activate_url(doc.id, attempt.id))
        self.assertEqual(get_resp.status_code, 405)

        put_resp = self.client.put(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
        )
        self.assertEqual(put_resp.status_code, 405)
        source.refresh_from_db()
        self.assertEqual(source.text, _OLD_TEXT)

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_missing_confirmation_does_not_call_service(self, mock_activate):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source, confirm=None),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._detail_url(doc.id, attempt.id))
        mock_activate.assert_not_called()

        follow = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertContains(follow, _CORRECTED_CURRENT_ACTIVATION_MSG_MISSING_CONFIRM)

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_successful_post_passes_exact_baseline_and_actor(self, mock_activate):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=attempt.id,
            snapshot_id=_snapshot.id,
            source_result_id=source.id,
            hebrew_result_id=_hebrew.id if _hebrew else None,
            engine=_ENGINE,
            bound_source_revision=3,
            outcome="APPLIED",
            source_text_changed=True,
            hebrew_mirror_updated=True,
        )
        expected_sha = compute_sha256_hex(source.text or "")
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._detail_url(doc.id, attempt.id))
        mock_activate.assert_called_once_with(
            document_id=doc.id,
            attempt_id=attempt.id,
            source_text_result_id=source.id,
            activated_by=self.staff,
            expected_source_revision=source.source_revision,
            expected_source_sha256=expected_sha,
        )

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_applied_source_change_message_and_redirect(self, mock_activate):
        doc, attempt, source, hebrew, snapshot = self._eligible_fixture()
        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            source_result_id=source.id,
            hebrew_result_id=hebrew.id if hebrew else None,
            engine=_ENGINE,
            bound_source_revision=3,
            outcome="APPLIED",
            source_text_changed=True,
            hebrew_mirror_updated=True,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.request["PATH_INFO"],
            self._detail_url(doc.id, attempt.id),
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE])
        self.assertContains(resp, _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE)

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_applied_hebrew_mirror_message(self, mock_activate):
        doc, attempt, source, hebrew, snapshot = self._eligible_fixture()
        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            source_result_id=source.id,
            hebrew_result_id=hebrew.id if hebrew else None,
            engine=_ENGINE,
            bound_source_revision=source.source_revision,
            outcome="APPLIED",
            source_text_changed=False,
            hebrew_mirror_updated=True,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            messages,
            [_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_HEBREW_MIRROR],
        )
        html = resp.content.decode()
        primary, technical = _split_primary_and_technical(html)
        self.assertIn(_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_HEBREW_MIRROR, primary)
        self.assertNotIn("hebrew_mirror_updated", primary)
        self.assertNotIn("SOURCE_TEXT", primary)
        self.assertNotIn("APPLIED", primary)
        _assert_technical_details_collapsed(self, html)
        self.assertIn("SOURCE_TEXT", technical)

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_applied_binding_only_message(self, mock_activate):
        doc, attempt, source, hebrew, snapshot = self._eligible_fixture()
        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            source_result_id=source.id,
            hebrew_result_id=hebrew.id if hebrew else None,
            engine=_ENGINE,
            bound_source_revision=source.source_revision,
            outcome="APPLIED",
            source_text_changed=False,
            hebrew_mirror_updated=False,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(
            messages,
            [_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_BINDING_ONLY],
        )
        self.assertContains(
            resp, _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_BINDING_ONLY
        )

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_already_active_message_and_redirect(self, mock_activate):
        doc, attempt, source, hebrew, snapshot = self._eligible_fixture()
        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            source_result_id=source.id,
            hebrew_result_id=hebrew.id if hebrew else None,
            engine=_ENGINE,
            bound_source_revision=source.source_revision,
            outcome="ALREADY_ACTIVE",
            source_text_changed=False,
            hebrew_mirror_updated=False,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_ACTIVATION_MSG_ALREADY_ACTIVE])
        self.assertContains(resp, _CORRECTED_CURRENT_ACTIVATION_MSG_ALREADY_ACTIVE)

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_stale_preview_error_mapped_safely(self, mock_activate):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        mock_activate.side_effect = CorrectedCurrentActivationError(
            CorrectedCurrentActivationErrorCode.STALE_PREVIEW
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_ACTIVATION_MSG_STALE])
        self.assertNotContains(resp, "STALE_PREVIEW")
        self.assertNotContains(resp, "Source text revision or hash")

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_verified_and_human_edited_errors_mapped_safely(self, mock_activate):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)

        mock_activate.side_effect = CorrectedCurrentActivationError(
            CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED
        )
        verified_resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        verified_messages = [str(m) for m in get_messages(verified_resp.wsgi_request)]
        self.assertEqual(
            verified_messages,
            [_CORRECTED_CURRENT_ACTIVATION_MSG_VERIFIED],
        )
        self.assertNotContains(verified_resp, "VERIFIED_BLOCKED")

        mock_activate.side_effect = CorrectedCurrentActivationError(
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED
        )
        human_resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        human_messages = [str(m) for m in get_messages(human_resp.wsgi_request)]
        self.assertEqual(
            human_messages,
            [_CORRECTED_CURRENT_ACTIVATION_MSG_HUMAN_EDITED],
        )
        self.assertNotContains(human_resp, "HUMAN_EDITED_BLOCKED")

    @patch(
        "documents.views.activate_corrected_current_sync_attempt",
    )
    def test_unknown_safe_service_failure_does_not_leak_exception_text(
        self, mock_activate
    ):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        leak = "secret traceback /tmp/provider-token-xyz"
        mock_activate.side_effect = CorrectedCurrentActivationError(
            CorrectedCurrentActivationErrorCode.BINDING_FAILED,
            message=leak,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC])
        self.assertNotContains(resp, leak)
        self.assertNotContains(resp, "BINDING_FAILED")
        self.assertNotContains(resp, "traceback")

    def test_tampered_baseline_rejected_with_no_mutation(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        before_text = source.text
        before_revision = source.source_revision
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(
                source,
                expected_source_sha256="0" * 64,
            ),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        messages = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertEqual(messages, [_CORRECTED_CURRENT_ACTIVATION_MSG_STALE])
        source.refresh_from_db()
        self.assertEqual(source.text, before_text)
        self.assertEqual(source.source_revision, before_revision)

    def test_csrf_required(self):
        doc, attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.csrf_client.force_login(self.staff)
        resp = self.csrf_client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
        )
        self.assertEqual(resp.status_code, 403)
        source.refresh_from_db()
        self.assertEqual(source.text, _OLD_TEXT)

    @patch("documents.views.activate_corrected_current_sync_attempt")
    def test_document_attempt_mismatch_returns_404_with_valid_form(self, mock_activate):
        doc_a, _attempt_a, source_a, _hebrew_a, _snapshot_a = self._eligible_fixture()
        doc_b = self._create_doc(title="Mismatch other doc")
        run_b = self._upload_run(doc_b)
        snapshot_b = self._ready_snapshot(document=doc_b, run=run_b)
        attempt_b = self._completed_attempt(doc=doc_b, run=run_b, snapshot=snapshot_b)
        self._source_row(doc_b)
        self._hebrew_row(doc_b)
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc_a.id, attempt_b.id),
            data=self._post_data(source_a),
        )
        self.assertEqual(resp.status_code, 404)
        mock_activate.assert_not_called()
        self.assertEqual([str(m) for m in get_messages(resp.wsgi_request)], [])
        source_a.refresh_from_db()
        self.assertEqual(source_a.text, _OLD_TEXT)

    @patch("documents.views.activate_corrected_current_sync_attempt")
    def test_document_attempt_mismatch_returns_404_with_missing_form_fields(
        self, mock_activate
    ):
        doc_a, _attempt_a, _source_a, _hebrew_a, _snapshot_a = self._eligible_fixture()
        doc_b = self._create_doc(title="Mismatch empty-form doc")
        run_b = self._upload_run(doc_b)
        snapshot_b = self._ready_snapshot(document=doc_b, run=run_b)
        attempt_b = self._completed_attempt(doc=doc_b, run=run_b, snapshot=snapshot_b)
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc_a.id, attempt_b.id),
            data={},
        )
        self.assertEqual(resp.status_code, 404)
        mock_activate.assert_not_called()
        self.assertEqual([str(m) for m in get_messages(resp.wsgi_request)], [])

    @patch("documents.views.activate_corrected_current_sync_attempt")
    def test_nonexistent_attempt_returns_404_without_messages(self, mock_activate):
        doc, _attempt, source, _hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)

        resp = self.client.post(
            self._activate_url(doc.id, 9_999_999),
            data=self._post_data(source),
        )
        self.assertEqual(resp.status_code, 404)
        mock_activate.assert_not_called()
        self.assertEqual([str(m) for m in get_messages(resp.wsgi_request)], [])

    def test_real_activation_updates_baseline_after_redirect(self):
        doc, attempt, source, hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE)
        self.assertContains(resp, _CANONICAL)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertEqual(resp.context["source_text"], _CANONICAL)

    @patch(
        "documents.services.transkribus_corrected_current_sync.run_corrected_current_transkribus_sync"
    )
    @patch("documents.services.sqs.send_process_document_message")
    def test_real_activation_path_does_not_call_sync_or_sqs(
        self,
        mock_sqs,
        mock_sync,
    ):
        doc, attempt, source, hebrew, _snapshot = self._eligible_fixture()
        self.client.force_login(self.staff)
        resp = self.client.post(
            self._activate_url(doc.id, attempt.id),
            data=self._post_data(source),
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(hebrew.text, _CANONICAL)
        self.assertContains(resp, _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE)
        mock_sync.assert_not_called()
        mock_sqs.assert_not_called()
