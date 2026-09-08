from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from documents.management.commands.run_worker import Command
from documents.models import (
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
)
from documents.services.archive_items import create_ocr_document
from documents.services.archive_search_index import rebuild_archive_item_search_index
from documents.services.gemini_engine import GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.hebrew_translation_retry import (
    PROCESS_DOCUMENT_OPERATION_KEY,
    RETRY_HEBREW_TRANSLATION_OPERATION,
    execute_hebrew_translation_retry,
)
from documents.services.page_extraction import PageImage
from documents.services.process_document_outcome import ProcessDocumentDisposition
from documents.services.process_document_request_persist import (
    ProcessDocumentExecutionIdentity,
    ProcessDocumentExecutionIdentityKind,
    automated_process_document_persist_is_allowed,
    resolve_process_document_execution_identity,
)
from documents.services.process_document_request_staff_recovery import (
    abandon_process_document_request,
)
from documents.services.process_document_request_worker import (
    LEASE_TOKEN_PAYLOAD_KEY,
    PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY,
)
from documents.test_hebrew_translation_retry import (
    ENGINE,
    _failed_hebrew,
    _non_hebrew_doc,
    _usable_source,
    _worker_env_config,
)


_OMIT = object()
_DEFAULT = object()


def _ocr_document() -> Document:
    return create_ocr_document(
        title="Persist fence OCR",
        doc_type=Document.DocType.PDF,
        language=Document.Language.ENGLISH,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.PROCESSING,
        file_s3_key="persist-fence.pdf",
        mime_type="application/pdf",
    )


class ProcessDocumentExecutionIdentityResolveTests(SimpleTestCase):
    def test_both_identity_keys_absent_is_legacy(self):
        identity = resolve_process_document_execution_identity(
            {"type": "PROCESS_DOCUMENT", "document_id": 1}
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.LEGACY)
        self.assertIsNone(identity.request_id)
        self.assertIsNone(identity.lease_token)

    def test_valid_request_id_and_lease_token_is_request_aware(self):
        token = uuid.uuid4()
        identity = resolve_process_document_execution_identity(
            {
                PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY: 12,
                LEASE_TOKEN_PAYLOAD_KEY: token,
            }
        )
        self.assertEqual(
            identity.kind, ProcessDocumentExecutionIdentityKind.REQUEST_AWARE
        )
        self.assertEqual(identity.request_id, 12)
        self.assertEqual(identity.lease_token, token)

    def test_malformed_request_id_without_lease_token_is_invalid(self):
        identity = resolve_process_document_execution_identity(
            {PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY: "not-an-id"}
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.INVALID)

    def test_malformed_lease_token_without_request_id_is_invalid(self):
        identity = resolve_process_document_execution_identity(
            {LEASE_TOKEN_PAYLOAD_KEY: "not-a-uuid"}
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.INVALID)

    def test_both_present_malformed_is_invalid(self):
        identity = resolve_process_document_execution_identity(
            {
                PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY: "bad",
                LEASE_TOKEN_PAYLOAD_KEY: "also-bad",
            }
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.INVALID)

    def test_one_valid_and_one_malformed_is_invalid(self):
        identity = resolve_process_document_execution_identity(
            {
                PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY: 12,
                LEASE_TOKEN_PAYLOAD_KEY: "not-a-uuid",
            }
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.INVALID)
        identity = resolve_process_document_execution_identity(
            {
                PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY: "12",
                LEASE_TOKEN_PAYLOAD_KEY: str(uuid.uuid4()),
            }
        )
        self.assertEqual(identity.kind, ProcessDocumentExecutionIdentityKind.INVALID)


class AutomatedProcessDocumentPersistHelperTests(TestCase):
    def setUp(self) -> None:
        self.document = _ocr_document()
        self.token = uuid.uuid4()
        now = timezone.now()
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=self.token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

    def _matching_identity(self) -> ProcessDocumentExecutionIdentity:
        return ProcessDocumentExecutionIdentity.request_aware(
            self.request.pk, self.token
        )

    def _allowed(
        self, identity: ProcessDocumentExecutionIdentity | None = None
    ) -> bool:
        if identity is None:
            identity = self._matching_identity()
        with transaction.atomic():
            document = Document.objects.select_for_update().get(pk=self.document.pk)
            return automated_process_document_persist_is_allowed(
                document=document,
                identity=identity,
            )

    def test_legacy_payload_without_identity_is_allowed(self):
        self.assertTrue(self._allowed(ProcessDocumentExecutionIdentity.legacy()))

    def test_invalid_identity_is_denied(self):
        self.assertFalse(self._allowed(ProcessDocumentExecutionIdentity.invalid()))

    def test_matching_running_token_is_allowed(self):
        self.assertTrue(self._allowed())

    def test_matching_recovery_required_token_is_allowed(self):
        self.request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        self.request.lease_expires_at = None
        self.request.save(update_fields=["status", "lease_expires_at", "updated_at"])
        self.assertTrue(self._allowed())

    def test_wrong_token_is_denied(self):
        self.assertFalse(
            self._allowed(
                ProcessDocumentExecutionIdentity.request_aware(
                    self.request.pk, uuid.uuid4()
                )
            )
        )

    def test_missing_request_is_denied(self):
        self.assertFalse(
            self._allowed(
                ProcessDocumentExecutionIdentity.request_aware(999999999, self.token)
            )
        )

    def test_terminal_request_is_denied(self):
        self.request.status = ProcessDocumentRequest.Status.FAILED
        self.request.lease_token = None
        self.request.lease_expires_at = None
        self.request.completed_at = timezone.now()
        self.request.failure_code = "STAFF_ABANDONED"
        self.request.save(
            update_fields=[
                "status",
                "lease_token",
                "lease_expires_at",
                "completed_at",
                "failure_code",
                "updated_at",
            ]
        )
        self.assertFalse(self._allowed())


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class ProcessDocumentPersistFenceWorkerTests(TestCase):
    def setUp(self) -> None:
        self.command = Command()
        self.command._cfg = _worker_env_config()
        self.document = _ocr_document()
        now = timezone.now()
        self.token = uuid.uuid4()
        self.request = ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.UPLOAD_FINALIZE,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=self.token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

    def _payload(self, *, token=_DEFAULT, request_id=_DEFAULT) -> dict:
        payload = {
            "type": "PROCESS_DOCUMENT",
            "document_id": self.document.id,
        }
        if request_id is not _OMIT:
            payload[PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY] = (
                self.request.pk if request_id is _DEFAULT else request_id
            )
        if token is not _OMIT:
            payload[LEASE_TOKEN_PAYLOAD_KEY] = (
                self.token if token is _DEFAULT else token
            )
        return payload

    def _execute(self, payload: dict, mock_transcribe, mock_extract, mock_get):
        mock_get.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.return_value = HtrResult(
            text="recognized persist fence source",
            needs_review=False,
            engine_name=ENGINE,
            review_reasons=[],
        )
        with patch(
            "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
            return_value=GeminiResult(
                text="translated hebrew long enough",
                engine_name=ENGINE,
            ),
        ):
            return self.command._execute_process_document_payload(payload)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_matching_running_token_persists(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(), mock_transcribe, mock_extract, mock_get
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        self.assertTrue(
            DocumentTextResult.objects.filter(
                document=self.document,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                text="recognized persist fence source",
            ).exists()
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_matching_recovery_required_token_persists(
        self, mock_transcribe, mock_extract, mock_get
    ):
        def transcribe_then_fence(*_args, **_kwargs):
            self.request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
            self.request.lease_expires_at = None
            self.request.save(
                update_fields=["status", "lease_expires_at", "updated_at"]
            )
            self.document.processing_state_user = (
                Document.ProcessingState.RECOVERY_REQUIRED
            )
            self.document.save(update_fields=["processing_state_user", "updated_at"])
            return HtrResult(
                text="recognized persist fence source",
                needs_review=False,
                engine_name=ENGINE,
                review_reasons=[],
            )

        mock_get.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.side_effect = transcribe_then_fence
        with patch(
            "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
            return_value=GeminiResult(
                text="translated hebrew long enough",
                engine_name=ENGINE,
            ),
        ):
            outcome = self.command._execute_process_document_payload(self._payload())

        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        self.assertTrue(
            DocumentTextResult.objects.filter(
                document=self.document,
                text="recognized persist fence source",
            ).exists()
        )
        self.document.refresh_from_db()
        self.assertNotEqual(
            self.document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_abandon_during_ocr_skips_persist_index_and_rollup(
        self, mock_transcribe, mock_extract, mock_get
    ):
        rebuild_archive_item_search_index(self.document.archive_item)
        baseline = ArchiveItemSearchIndex.objects.get(
            archive_item_id=self.document.archive_item_id
        ).body_text

        def transcribe_then_abandon(*_args, **_kwargs):
            self.request.status = ProcessDocumentRequest.Status.RECOVERY_REQUIRED
            self.request.lease_expires_at = None
            self.request.save(
                update_fields=["status", "lease_expires_at", "updated_at"]
            )
            self.document.processing_state_user = (
                Document.ProcessingState.RECOVERY_REQUIRED
            )
            self.document.save(update_fields=["processing_state_user", "updated_at"])
            abandon_process_document_request(
                request_id=self.request.pk,
                document_id=self.document.pk,
            )
            return HtrResult(
                text="stale recognized text that must not persist",
                needs_review=False,
                engine_name=ENGINE,
                review_reasons=[],
            )

        mock_get.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract.return_value = [
            PageImage(page_index=1, image_bytes=b"page", mime_type="image/png")
        ]
        mock_transcribe.side_effect = transcribe_then_abandon
        with patch(
            "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
            return_value=GeminiResult(text="stale hebrew", engine_name=ENGINE),
        ):
            with patch(
                "documents.services.archive_search_index.sync_archive_item_search_index"
            ) as mock_sync:
                outcome = self.command._execute_process_document_payload(
                    self._payload()
                )
                mock_sync.assert_not_called()

        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.FAILED,
        )
        self.assertEqual(
            ArchiveItemSearchIndex.objects.get(
                archive_item_id=self.document.archive_item_id
            ).body_text,
            baseline,
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_wrong_token_skips_persist(self, mock_transcribe, mock_extract, mock_get):
        outcome = self._execute(
            self._payload(token=uuid.uuid4()),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_legacy_payload_without_identity_keys_persists(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(token=_OMIT, request_id=_OMIT),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        self.assertTrue(
            DocumentTextResult.objects.filter(
                document=self.document,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                text="recognized persist fence source",
            ).exists()
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_malformed_request_id_without_lease_token_is_denied(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(request_id="not-an-id", token=_OMIT),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_malformed_lease_token_without_request_id_is_denied(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(request_id=_OMIT, token="not-a-uuid"),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_both_identity_fields_malformed_is_denied(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(request_id="bad", token="also-bad"),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_valid_request_id_with_malformed_lease_token_is_denied(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(token="not-a-uuid"),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_malformed_request_id_with_valid_lease_token_is_denied(
        self, mock_transcribe, mock_extract, mock_get
    ):
        outcome = self._execute(
            self._payload(request_id="12"),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_transcribe.assert_not_called()

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_old_token_skips_after_new_request_exists(
        self, mock_transcribe, mock_extract, mock_get
    ):
        old_token = self.token
        old_request_id = self.request.pk
        self.request.status = ProcessDocumentRequest.Status.FAILED
        self.request.lease_token = None
        self.request.lease_expires_at = None
        self.request.completed_at = timezone.now()
        self.request.failure_code = "STAFF_ABANDONED"
        self.request.save(
            update_fields=[
                "status",
                "lease_token",
                "lease_expires_at",
                "completed_at",
                "failure_code",
                "updated_at",
            ]
        )
        now = timezone.now()
        ProcessDocumentRequest.objects.create(
            document=self.document,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.OCR,
            origin=ProcessDocumentRequest.Origin.OCR_REPROCESS,
            ocr_retry_mode=ProcessDocumentRequest.OcrRetryMode.NORMAL_REENQUEUE,
            lease_token=uuid.uuid4(),
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )
        outcome = self._execute(
            self._payload(token=old_token, request_id=old_request_id),
            mock_transcribe,
            mock_extract,
            mock_get,
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_verified_fence_still_blocks_matching_token(
        self, mock_transcribe, mock_extract, mock_get
    ):
        self.document.processing_state_user = Document.ProcessingState.READY
        self.document.save(update_fields=["processing_state_user", "updated_at"])
        source = DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="human reviewed source",
        )
        hebrew = DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="human reviewed hebrew",
        )
        outcome = self._execute(
            self._payload(), mock_transcribe, mock_extract, mock_get
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "human reviewed source")
        self.assertEqual(hebrew.text, "human reviewed hebrew")
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=self.document,
                text="recognized persist fence source",
            ).exists()
        )


class HebrewTranslationPersistFenceTests(TestCase):
    def setUp(self) -> None:
        self.worker_env = _worker_env_config()
        self.doc = _non_hebrew_doc()
        _usable_source(self.doc)
        _failed_hebrew(self.doc)
        now = timezone.now()
        self.token = uuid.uuid4()
        self.request = ProcessDocumentRequest.objects.create(
            document=self.doc,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
            lease_token=self.token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_matching_token_persists_hebrew(self, mock_translate):
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )
        outcome = execute_hebrew_translation_retry(
            self.doc.id,
            worker_env=self.worker_env,
            execution_identity=ProcessDocumentExecutionIdentity.request_aware(
                self.request.pk, self.token
            ),
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        hebrew = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.text, "translated hebrew text long enough")

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_wrong_token_skips_hebrew_persist_and_provider(self, mock_translate):
        outcome = execute_hebrew_translation_retry(
            self.doc.id,
            worker_env=self.worker_env,
            execution_identity=ProcessDocumentExecutionIdentity.request_aware(
                self.request.pk, uuid.uuid4()
            ),
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()
        hebrew = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.status, DocumentTextResult.Status.FAILED)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_invalid_identity_skips_hebrew_provider(self, mock_translate):
        outcome = execute_hebrew_translation_retry(
            self.doc.id,
            worker_env=self.worker_env,
            execution_identity=ProcessDocumentExecutionIdentity.invalid(),
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_missing_request_skips_hebrew_provider(self, mock_translate):
        outcome = execute_hebrew_translation_retry(
            self.doc.id,
            worker_env=self.worker_env,
            execution_identity=ProcessDocumentExecutionIdentity.request_aware(
                999999999, self.token
            ),
        )
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()


class HebrewTranslationPersistFenceWorkerTests(TestCase):
    def setUp(self) -> None:
        self.command = Command()
        self.command._cfg = _worker_env_config()
        self.doc = _non_hebrew_doc()
        _usable_source(self.doc)
        _failed_hebrew(self.doc)
        now = timezone.now()
        self.token = uuid.uuid4()
        self.request = ProcessDocumentRequest.objects.create(
            document=self.doc,
            status=ProcessDocumentRequest.Status.RUNNING,
            operation=ProcessDocumentRequest.Operation.HEBREW_TRANSLATION,
            origin=ProcessDocumentRequest.Origin.HEBREW_TRANSLATION_RETRY,
            ocr_retry_mode="",
            lease_token=self.token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

    def _payload(self, *, token=_DEFAULT, request_id=_DEFAULT) -> dict:
        payload = {
            "type": "PROCESS_DOCUMENT",
            "document_id": self.doc.id,
            PROCESS_DOCUMENT_OPERATION_KEY: RETRY_HEBREW_TRANSLATION_OPERATION,
        }
        if request_id is not _OMIT:
            payload[PROCESS_DOCUMENT_REQUEST_ID_PAYLOAD_KEY] = (
                self.request.pk if request_id is _DEFAULT else request_id
            )
        if token is not _OMIT:
            payload[LEASE_TOKEN_PAYLOAD_KEY] = (
                self.token if token is _DEFAULT else token
            )
        return payload

    def _execute(self, payload: dict):
        return self.command._execute_process_document_payload(payload)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_legacy_payload_without_identity_keys_runs(self, mock_translate):
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )
        outcome = self._execute(self._payload(token=_OMIT, request_id=_OMIT))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        mock_translate.assert_called_once()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_valid_request_identity_runs(self, mock_translate):
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )
        outcome = self._execute(self._payload())
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.COMPLETED)
        mock_translate.assert_called_once()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_malformed_request_id_without_lease_token_is_denied(self, mock_translate):
        outcome = self._execute(self._payload(request_id="not-an-id", token=_OMIT))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_malformed_lease_token_without_request_id_is_denied(self, mock_translate):
        outcome = self._execute(self._payload(request_id=_OMIT, token="not-a-uuid"))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_both_identity_fields_malformed_is_denied(self, mock_translate):
        outcome = self._execute(self._payload(request_id="bad", token="also-bad"))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_one_valid_and_one_malformed_is_denied(self, mock_translate):
        outcome = self._execute(self._payload(token="not-a-uuid"))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()
        outcome = self._execute(self._payload(request_id="12"))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_wrong_token_is_denied(self, mock_translate):
        outcome = self._execute(self._payload(token=uuid.uuid4()))
        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.NOOP)
        mock_translate.assert_not_called()
