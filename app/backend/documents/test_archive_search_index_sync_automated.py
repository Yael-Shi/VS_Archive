"""PR2b-2 ArchiveItemSearchIndex sync for automated DocumentTextResult mutations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.management.commands.run_worker import Command
from documents.models import (
    ArchiveItem,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusTextResultBinding,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_ocr_document
from documents.services.archive_search_index import (
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.gemini_engine import GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.hebrew_translation_retry import run_hebrew_translation_retry
from documents.services.text_presentation import get_displayed_transcription_text
from documents.services.transkribus_local_completion import (
    complete_transkribus_local_success,
)
from documents.services.verified_text_result_edit import edit_pending_text_result
from documents.test_hebrew_translation_retry import (
    ENGINE,
    _failed_hebrew,
    _non_hebrew_doc,
    _usable_source,
    _worker_env_config,
)
from documents.test_transkribus_automatic_snapshot import (
    _create_he_doc,
    _ready_snapshot,
    _route,
)


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


def _worker_command() -> Command:
    command = Command()
    command._cfg = _worker_env_config()
    return command


@override_settings(UPLOADS_BUCKET_NAME="")
class WorkerOcrSearchIndexSyncTests(TestCase):
    def setUp(self):
        self.command = _worker_command()
        self._translation_patcher = patch(
            "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
            return_value=GeminiResult(
                text="translated hebrew text long enough",
                engine_name="gemini-2.0-flash",
            ),
        )
        self.mock_translate = self._translation_patcher.start()

    def tearDown(self):
        self._translation_patcher.stop()

    def _english_doc(self) -> Document:
        return create_ocr_document(
            title="Worker OCR body sync EN",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="worker-en.pdf",
            mime_type="application/pdf",
        )

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Worker OCR body sync HE",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="worker-he.pdf",
            mime_type="application/pdf",
        )

    def _message(self, doc: Document) -> dict:
        return {"Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": doc.id})}

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_non_hebrew_worker_success_updates_indexed_source_body(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        doc = self._english_doc()
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="recognized source for index",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mock_sync:
            self.assertTrue(self.command._process_message(self._message(doc)))
            self.assertEqual(mock_sync.call_count, 1)
            mock_sync.assert_called_once_with(doc.archive_item_id)

        displayed = get_displayed_transcription_text(doc)
        self.assertEqual(displayed, "recognized source for index")
        self.assertEqual(_index_for(doc.archive_item_id).body_text, displayed)
        self.assertNotEqual(
            _index_for(doc.archive_item_id).body_text,
            "translated hebrew text long enough",
        )

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_hebrew_worker_success_indexes_hebrew_display_selector(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        doc = self._hebrew_doc()
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="טקסט עברי לאינדקס",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        self.assertTrue(self.command._process_message(self._message(doc)))
        self.mock_translate.assert_not_called()

        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        source = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        self.assertEqual(hebrew.text, "טקסט עברי לאינדקס")
        self.assertEqual(source.text, "טקסט עברי לאינדקס")
        displayed = get_displayed_transcription_text(doc)
        self.assertEqual(displayed, hebrew.text)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, displayed)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_ocr_failure_demotion_rebuilds_index(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        doc = self._english_doc()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="previous displayed ocr body",
        )
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "previous displayed ocr body",
        )

        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.side_effect = RuntimeError("adapter boom")

        self.assertTrue(self.command._process_message(self._message(doc)))

        failed = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
        )
        self.assertEqual(failed.status, DocumentTextResult.Status.FAILED)
        self.assertIsNone(failed.text)
        self.assertEqual(get_displayed_transcription_text(doc), "")
        self.assertEqual(_index_for(doc.archive_item_id).body_text, "")

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_ocr_failure_demotion_sync_failure_rolls_back_source_state(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        doc = self._english_doc()
        existing = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="ocr-dispatch",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="previous displayed ocr body",
        )
        # Match create_ocr_document default so Phase 1 PROCESSING write is not a
        # distinct committed transition for this assertion.
        doc.processing_state_user = Document.ProcessingState.PROCESSING
        doc.save(update_fields=["processing_state_user", "updated_at"])
        rebuild_archive_item_search_index(doc.archive_item)
        baseline_body = "previous displayed ocr body"
        self.assertEqual(_index_for(doc.archive_item_id).body_text, baseline_body)
        pre_processing_state = doc.processing_state_user

        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.side_effect = RuntimeError("adapter boom")

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                self.command._process_message(self._message(doc))

        existing.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(existing.text, "previous displayed ocr body")
        self.assertEqual(existing.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(doc.processing_state_user, pre_processing_state)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, baseline_body)

    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.transcribe_pages")
    def test_worker_sync_failure_rolls_back_ocr_and_translation(
        self,
        mock_transcribe,
        mock_extract_pages,
        mock_get_object_bytes,
    ):
        doc = self._english_doc()
        pre_processing_state = doc.processing_state_user
        mock_get_object_bytes.return_value = (b"%PDF-1.4", "application/pdf")
        mock_extract_pages.return_value = [SimpleNamespace(page_index=1)]
        mock_transcribe.return_value = HtrResult(
            text="recognized text that must roll back",
            needs_review=False,
            engine_name="gemini-2.0-flash",
            review_reasons=[],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                self.command._process_message(self._message(doc))

        doc.refresh_from_db()
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())
        self.assertEqual(doc.processing_state_user, pre_processing_state)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, "")


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class TranskribusLocalCompletionSearchIndexSyncTests(TestCase):
    def _started_run(self, doc: Document, **kwargs) -> TranskribusRun:
        defaults = dict(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        defaults.update(kwargs)
        return TranskribusRun.objects.create(**defaults)

    def test_local_completion_updates_indexed_hebrew_display_body(self):
        doc = _create_he_doc(title="Local completion index HE")
        run = self._started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="תעתוק מקומי לאינדקס")

        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="תעתוק מקומי לאינדקס",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )

        displayed = get_displayed_transcription_text(doc)
        self.assertEqual(displayed, "תעתוק מקומי לאינדקס")
        self.assertEqual(_index_for(doc.archive_item_id).body_text, displayed)
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        self.assertEqual(hebrew.text, displayed)

    def test_local_completion_sync_failure_rolls_back_dtr_and_bindings(self):
        doc = _create_he_doc(title="Local completion rollback")
        run = self._started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello local")
        baseline_body = _index_for(doc.archive_item_id).body_text
        self.assertEqual(baseline_body, "")

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                complete_transkribus_local_success(
                    document_id=doc.id,
                    run_id=run.id,
                    snapshot_id=snap.id,
                    text="Hello local",
                    engine="transkribus-pylaia:42",
                    route=_route(),
                    needs_review=False,
                    review_reasons=[],
                    min_text_length=1,
                )

        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result__document=doc
            ).exists()
        )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, baseline_body)

    def test_local_completion_early_no_overwrite_does_not_call_sync(self):
        doc = _create_he_doc(title="Local completion early skip")
        run = self._started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            complete_transkribus_local_success(
                document_id=doc.id,
                run_id=run.id,
                snapshot_id=snap.id,
                text="Hello",
                engine="transkribus-pylaia:42",
                route=_route(),
                needs_review=False,
                review_reasons=[],
                min_text_length=1,
            )
            mock_sync.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="")
class HebrewTranslationRetrySearchIndexSyncTests(TestCase):
    def setUp(self):
        self.worker_env = _worker_env_config()

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_translation_retry_syncs_final_displayed_body_once(self, mock_translate):
        doc = _non_hebrew_doc(title="Translation retry index")
        source = _usable_source(doc, text="recognized source text")
        _failed_hebrew(doc)
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "recognized source text",
        )
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mock_sync:
            self.assertTrue(
                run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)
            )
            self.assertEqual(mock_sync.call_count, 1)
            mock_sync.assert_called_once_with(doc.archive_item_id)

        source.refresh_from_db()
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
        )
        self.assertEqual(hebrew.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(hebrew.text, "translated hebrew text long enough")
        # Non-Hebrew display prefers SOURCE_TEXT.
        displayed = get_displayed_transcription_text(doc)
        self.assertEqual(displayed, source.text)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, displayed)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_translation_retry_sync_failure_rolls_back_hebrew_persist(
        self, mock_translate
    ):
        doc = _non_hebrew_doc(title="Translation retry rollback")
        _usable_source(doc, text="recognized source text")
        failed = _failed_hebrew(doc)
        rebuild_archive_item_search_index(doc.archive_item)
        baseline_body = _index_for(doc.archive_item_id).body_text
        self.assertEqual(baseline_body, "recognized source text")
        baseline_error_details = failed.error_details
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            # Persist TX rolls back; claim TX is separate and may have set
            # PROCESSING. Abort restoration recomputes from remaining rows.
            self.assertFalse(
                run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)
            )

        failed.refresh_from_db()
        doc.refresh_from_db()
        self.assertEqual(failed.status, DocumentTextResult.Status.FAILED)
        self.assertIsNone(failed.text)
        self.assertEqual(failed.error_code, "HEBREW_TRANSLATION_FAILED")
        self.assertEqual(failed.error_details, baseline_error_details)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, baseline_body)
        # Usable SOURCE + failed HEBREW → PARTIAL via retry-abort restoration.
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.PARTIAL)

    @patch(
        "documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini"
    )
    def test_duplicate_translation_retry_does_not_resync(self, mock_translate):
        doc = _non_hebrew_doc(title="Translation retry duplicate")
        _usable_source(doc)
        _failed_hebrew(doc)
        mock_translate.return_value = GeminiResult(
            text="translated hebrew text long enough",
            engine_name=ENGINE,
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mock_sync:
            self.assertTrue(
                run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)
            )
            self.assertEqual(mock_sync.call_count, 1)
            self.assertTrue(
                run_hebrew_translation_retry(doc.id, worker_env=self.worker_env)
            )
            self.assertEqual(mock_sync.call_count, 1)


@override_settings(UPLOADS_BUCKET_NAME="")
class Pr2aPr2b1AndPublicSearchRegressionTests(TestCase):
    def test_public_archive_q_still_ignores_worker_ocr_body(self):
        command = _worker_command()
        doc = create_ocr_document(
            title="Visible automated OCR title",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            visibility=Document.Visibility.PUBLIC,
            file_s3_key="public-auto.pdf",
            mime_type="application/pdf",
        )
        with (
            patch(
                "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
                return_value=GeminiResult(
                    text="translated hebrew text long enough",
                    engine_name="gemini-2.0-flash",
                ),
            ),
            patch(
                "documents.management.commands.run_worker.get_object_bytes",
                return_value=(b"%PDF-1.4", "application/pdf"),
            ),
            patch(
                "documents.management.commands.run_worker.extract_pages",
                return_value=[SimpleNamespace(page_index=1)],
            ),
            patch(
                "documents.management.commands.run_worker.transcribe_pages",
                return_value=HtrResult(
                    text="unique-automated-ocr-body-token-pr2b2",
                    needs_review=False,
                    engine_name="gemini-2.0-flash",
                    review_reasons=[],
                ),
            ),
        ):
            self.assertTrue(
                command._process_message(
                    {
                        "Body": json.dumps(
                            {"type": "PROCESS_DOCUMENT", "document_id": doc.id}
                        )
                    }
                )
            )

        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "unique-automated-ocr-body-token-pr2b2",
        )
        qs = ArchiveItem.objects.all()
        self.assertFalse(
            filter_archive_items_by_search_query(
                qs, "unique-automated-ocr-body-token-pr2b2"
            ).exists()
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "unique-automated-ocr-body-token-pr2b2"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Visible automated OCR title")

    def test_pr2b1_pending_edit_sync_still_works(self):
        doc = create_ocr_document(
            title="PR2b-1 intact with PR2b-2",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/pr2b2-regression/original.jpg",
            mime_type="image/jpeg",
        )
        staff = User.objects.create_user(
            username="pr2b2_regression_staff",
            password="test-pass",
            is_staff=True,
        )
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="before human edit",
        )
        edit_pending_text_result(
            result_id=source.id,
            new_text="after human edit indexed",
            editor=staff,
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "after human edit indexed",
        )

    def test_pr2a_ocr_create_still_syncs_empty_body(self):
        doc = create_ocr_document(
            title="PR2a create intact under PR2b-2",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        index = _index_for(doc.archive_item_id)
        self.assertEqual(index.title_text, "PR2a create intact under PR2b-2")
        self.assertEqual(index.body_text, "")
