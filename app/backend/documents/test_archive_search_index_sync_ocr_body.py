"""PR2b-1 ArchiveItemSearchIndex sync for human-controlled displayed-text mutations."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import DatabaseError
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranscriptionEditSuggestion,
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
from documents.services.text_presentation import get_displayed_transcription_text
from documents.services.transcription_suggestion_review import (
    approve_suggestion,
    reject_suggestion,
)
from documents.services.transkribus_corrected_current_activation import (
    activate_corrected_current_sync_attempt,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.verified_text_result_edit import (
    edit_pending_text_result,
    edit_verified_text_result,
)
from documents.test_transkribus_corrected_current_activation import (
    _CANONICAL,
    _OLD_TEXT,
    _add_snapshot_page,
    _completed_attempt,
    _create_doc,
    _hebrew_row,
    _ready_snapshot,
    _source_row,
    _upload_run,
)


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


def _create_text_result(
    doc: Document,
    *,
    result_type: str,
    text: str,
    engine: str = "engine-a",
    verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
    source_revision: int = 1,
    based_on_source_revision: int | None = None,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=result_type,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=verification_status,
        text=text,
        source_revision=source_revision,
        based_on_source_revision=based_on_source_revision,
    )


@override_settings(UPLOADS_BUCKET_NAME="")
class PendingAndVerifiedEditSearchIndexSyncTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_body_edit_staff",
            password="test-pass",
            is_staff=True,
        )

    def _english_doc(self) -> Document:
        return create_ocr_document(
            title="English OCR body sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ocr-body-en/original.jpg",
            mime_type="image/jpeg",
        )

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Hebrew OCR body sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ocr-body-he/original.jpg",
            mime_type="image/jpeg",
        )

    def test_pending_edit_updates_indexed_displayed_body(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Pending before",
        )
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, "Pending before")

        edit_pending_text_result(
            result_id=source.id,
            new_text="Pending after indexed",
            editor=self.staff,
        )

        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "Pending after indexed",
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            get_displayed_transcription_text(doc),
        )

    def test_non_hebrew_hebrew_text_edit_updates_indexed_translation(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="English source",
            source_revision=1,
        )
        hebrew = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום לפני",
            engine=source.engine,
            based_on_source_revision=source.source_revision,
        )
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(
            _index_for(doc.archive_item_id).hebrew_translation_text,
            "תרגום לפני",
        )

        edit_pending_text_result(
            result_id=hebrew.id,
            new_text="תרגום אחרי עריכה",
            editor=self.staff,
        )

        index = _index_for(doc.archive_item_id)
        self.assertEqual(index.body_text, "English source")
        self.assertEqual(index.hebrew_translation_text, "תרגום אחרי עריכה")

    def test_non_hebrew_source_edit_keeps_displayed_stale_translation_indexed(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Source before",
            source_revision=1,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום שיהפוך למיושן",
            engine=source.engine,
            based_on_source_revision=source.source_revision,
        )
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(
            _index_for(doc.archive_item_id).hebrew_translation_text,
            "תרגום שיהפוך למיושן",
        )

        edit_pending_text_result(
            result_id=source.id,
            new_text="Source after revision bump",
            editor=self.staff,
        )

        index = _index_for(doc.archive_item_id)
        self.assertEqual(index.body_text, "Source after revision bump")
        # Public detail still shows the stale translation; index must follow.
        self.assertEqual(index.hebrew_translation_text, "תרגום שיהפוך למיושן")

    def test_verified_edit_updates_indexed_displayed_body(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Verified before",
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        rebuild_archive_item_search_index(doc.archive_item)
        self.assertEqual(_index_for(doc.archive_item_id).body_text, "Verified before")

        edit_verified_text_result(
            result_id=source.id,
            new_text="Verified after indexed",
            editor=self.staff,
        )

        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "Verified after indexed",
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            get_displayed_transcription_text(doc),
        )

    def test_hebrew_mirror_edit_follows_display_selector(self):
        doc = self._hebrew_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מקור ישן",
        )
        hebrew = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="עברית ישנה",
            based_on_source_revision=1,
        )
        rebuild_archive_item_search_index(doc.archive_item)
        # Hebrew docs prefer displayable HEBREW_TEXT over SOURCE_TEXT.
        self.assertEqual(_index_for(doc.archive_item_id).body_text, "עברית ישנה")
        self.assertEqual(get_displayed_transcription_text(doc), "עברית ישנה")

        edit_pending_text_result(
            result_id=source.id,
            new_text="טקסט מעודכן משותף",
            editor=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "טקסט מעודכן משותף")
        self.assertEqual(hebrew.text, "טקסט מעודכן משותף")
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "טקסט מעודכן משותף",
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            get_displayed_transcription_text(doc),
        )

    def test_pending_edit_sync_failure_rolls_back_text_change(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Pending rollback before",
            source_revision=1,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                edit_pending_text_result(
                    result_id=source.id,
                    new_text="Pending rollback after",
                    editor=self.staff,
                )
        source.refresh_from_db()
        self.assertEqual(source.text, "Pending rollback before")
        self.assertEqual(source.source_revision, 1)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_hebrew_pending_edit_sync_failure_rolls_back_mirror_and_revisions(self):
        doc = self._hebrew_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מקור לפני כשל",
            source_revision=4,
        )
        hebrew = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="עברית לפני כשל",
            based_on_source_revision=4,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                edit_pending_text_result(
                    result_id=source.id,
                    new_text="טקסט שלא אמור להישמר",
                    editor=self.staff,
                )
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "מקור לפני כשל")
        self.assertEqual(hebrew.text, "עברית לפני כשל")
        self.assertEqual(source.source_revision, 4)
        self.assertEqual(hebrew.based_on_source_revision, 4)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_verified_edit_sync_failure_rolls_back_text_change(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Verified rollback before",
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            source_revision=1,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                edit_verified_text_result(
                    result_id=source.id,
                    new_text="Verified rollback after",
                    editor=self.staff,
                )
        source.refresh_from_db()
        self.assertEqual(source.text, "Verified rollback before")
        self.assertEqual(source.source_revision, 1)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_pending_noop_does_not_call_sync(self):
        doc = self._english_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Same pending text",
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            edit_pending_text_result(
                result_id=source.id,
                new_text="Same pending text",
                editor=self.staff,
            )
            mock_sync.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="")
class TranscriptionSuggestionSearchIndexSyncTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_body_suggestion_staff",
            password="test-pass",
            is_staff=True,
        )

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Suggestion OCR body sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ocr-body-suggest/original.jpg",
            mime_type="image/jpeg",
        )

    def _paired(self, doc: Document, text: str):
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text=text,
        )
        hebrew = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text=text,
            based_on_source_revision=1,
        )
        return source, hebrew

    def test_approve_suggestion_updates_indexed_displayed_body(self):
        doc = self._hebrew_doc()
        source, hebrew = self._paired(doc, "טקסט בסיס")
        rebuild_archive_item_search_index(doc.archive_item)
        suggestion = TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot="טקסט בסיס",
            suggested_text="טקסט מתוקן",
            submitter_name="מציע/ה",
            status=TranscriptionEditSuggestion.Status.PENDING,
        )

        approve_suggestion(
            suggestion.id,
            approved_text="טקסט מאושר לאינדקס",
            reviewer=self.staff,
        )

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "טקסט מאושר לאינדקס")
        self.assertEqual(hebrew.text, "טקסט מאושר לאינדקס")
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "טקסט מאושר לאינדקס",
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            get_displayed_transcription_text(doc),
        )

    def test_approve_suggestion_sync_failure_rolls_back_approval_and_text(self):
        doc = self._hebrew_doc()
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="טקסט בסיס",
            source_revision=3,
        )
        hebrew = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="טקסט בסיס",
            based_on_source_revision=3,
        )
        initial_source_revision = source.source_revision
        initial_hebrew_based_on = hebrew.based_on_source_revision
        self.assertEqual(initial_source_revision, 3)
        self.assertEqual(initial_hebrew_based_on, 3)
        suggestion = TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot="טקסט בסיס",
            suggested_text="טקסט מתוקן",
            submitter_name="מציע/ה",
            status=TranscriptionEditSuggestion.Status.PENDING,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                approve_suggestion(
                    suggestion.id,
                    approved_text="טקסט שלא אמור להישמר",
                    reviewer=self.staff,
                )

        suggestion.refresh_from_db()
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(suggestion.status, TranscriptionEditSuggestion.Status.PENDING)
        self.assertIsNone(suggestion.approved_text)
        self.assertIsNone(suggestion.applied_text_result_id)
        self.assertEqual(source.text, "טקסט בסיס")
        self.assertEqual(hebrew.text, "טקסט בסיס")
        self.assertEqual(source.source_revision, initial_source_revision)
        self.assertEqual(hebrew.based_on_source_revision, initial_hebrew_based_on)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)

    def test_reject_suggestion_does_not_call_sync(self):
        doc = self._hebrew_doc()
        self._paired(doc, "טקסט בסיס")
        suggestion = TranscriptionEditSuggestion.objects.create(
            document=doc,
            current_text_snapshot="טקסט בסיס",
            suggested_text="טקסט מתוקן",
            submitter_name="מציע/ה",
            status=TranscriptionEditSuggestion.Status.PENDING,
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            reject_suggestion(suggestion.id, reviewer=self.staff)
            mock_sync.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="")
class CorrectedCurrentActivationSearchIndexSyncTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="ocr_body_activation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.doc = _create_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.snapshot = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        _add_snapshot_page(self.snapshot)
        self.attempt = _completed_attempt(
            doc=self.doc,
            run=self.transkribus_run,
            snapshot=self.snapshot,
            user=self.user,
        )
        self.source = _source_row(self.doc)
        self.hebrew = _hebrew_row(self.doc)

    def _activate(self, **kwargs):
        defaults = dict(
            document_id=self.doc.pk,
            attempt_id=self.attempt.pk,
            source_text_result_id=self.source.pk,
            activated_by=self.user,
            expected_source_revision=self.source.source_revision,
            expected_source_sha256=compute_sha256_hex(self.source.text or ""),
        )
        defaults.update(kwargs)
        return activate_corrected_current_sync_attempt(**defaults)

    def test_source_change_activation_updates_index(self):
        rebuild_archive_item_search_index(self.doc.archive_item)
        self.assertEqual(_index_for(self.doc.archive_item_id).body_text, _OLD_TEXT)

        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")
        self.assertTrue(result.source_text_changed)
        self.assertTrue(result.hebrew_mirror_updated)
        self.assertEqual(_index_for(self.doc.archive_item_id).body_text, _CANONICAL)
        self.assertEqual(
            _index_for(self.doc.archive_item_id).body_text,
            get_displayed_transcription_text(self.doc),
        )

    def test_hebrew_mirror_only_activation_updates_index(self):
        self.source.text = _CANONICAL
        self.source.source_revision = 2
        self.source.save(update_fields=["text", "source_revision", "updated_at"])
        self.hebrew.text = "Stale hebrew mirror"
        self.hebrew.based_on_source_revision = 1
        self.hebrew.save(
            update_fields=["text", "based_on_source_revision", "updated_at"]
        )
        rebuild_archive_item_search_index(self.doc.archive_item)
        self.assertEqual(
            _index_for(self.doc.archive_item_id).body_text,
            "Stale hebrew mirror",
        )

        result = self._activate(
            expected_source_revision=2,
            expected_source_sha256=compute_sha256_hex(_CANONICAL),
        )
        self.assertEqual(result.outcome, "APPLIED")
        self.assertFalse(result.source_text_changed)
        self.assertTrue(result.hebrew_mirror_updated)
        self.assertEqual(_index_for(self.doc.archive_item_id).body_text, _CANONICAL)
        self.assertEqual(
            _index_for(self.doc.archive_item_id).body_text,
            get_displayed_transcription_text(self.doc),
        )

    def test_binding_only_activation_does_not_call_sync(self):
        self.source.text = _CANONICAL
        self.source.source_revision = 2
        self.source.save(update_fields=["text", "source_revision", "updated_at"])
        self.hebrew.text = _CANONICAL
        self.hebrew.based_on_source_revision = 2
        self.hebrew.save(
            update_fields=["text", "based_on_source_revision", "updated_at"]
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            result = self._activate(
                expected_source_revision=2,
                expected_source_sha256=compute_sha256_hex(_CANONICAL),
            )
            self.assertEqual(result.outcome, "APPLIED")
            self.assertFalse(result.source_text_changed)
            self.assertFalse(result.hebrew_mirror_updated)
            mock_sync.assert_not_called()

    def test_already_active_does_not_call_sync(self):
        first = self._activate()
        self.assertEqual(first.outcome, "APPLIED")
        original_revision = 2
        original_sha = compute_sha256_hex(_OLD_TEXT)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            second = self._activate(
                expected_source_revision=original_revision,
                expected_source_sha256=original_sha,
            )
            self.assertEqual(second.outcome, "ALREADY_ACTIVE")
            mock_sync.assert_not_called()

    def test_activation_sync_failure_rolls_back_text_and_bindings(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                self._activate()

        self.source.refresh_from_db()
        self.hebrew.refresh_from_db()
        self.assertEqual(self.source.text, _OLD_TEXT)
        self.assertEqual(self.hebrew.text, _OLD_TEXT)
        self.assertEqual(self.source.source_revision, 2)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result_id__in=[self.source.pk, self.hebrew.pk]
            ).exists()
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class PublicSearchAndPr2aRegressionTests(TestCase):
    def test_public_archive_q_finds_synced_ocr_body(self):
        from documents.models import ArchiveItem

        doc = create_ocr_document(
            title="Visible OCR title body sync",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ocr-body-public/original.jpg",
            mime_type="image/jpeg",
        )
        staff = User.objects.create_user(
            username="ocr_body_public_staff",
            password="test-pass",
            is_staff=True,
        )
        source = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="initial ocr body",
        )
        edit_pending_text_result(
            result_id=source.id,
            new_text="unique-ocr-body-token-pr2b1-aaa",
            editor=staff,
        )
        self.assertEqual(
            _index_for(doc.archive_item_id).body_text,
            "unique-ocr-body-token-pr2b1-aaa",
        )
        qs = ArchiveItem.objects.all()
        self.assertTrue(
            filter_archive_items_by_search_query(qs, "unique-ocr-body-token-pr2b1-aaa")
            .filter(pk=doc.archive_item_id)
            .exists()
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "unique-ocr-body-token-pr2b1-aaa"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Visible OCR title body sync")

    def test_pr2a_ocr_create_still_syncs_empty_body_without_dtr(self):
        doc = create_ocr_document(
            title="PR2a OCR create intact",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        index = _index_for(doc.archive_item_id)
        self.assertEqual(index.title_text, "PR2a OCR create intact")
        self.assertEqual(index.body_text, "")

    def test_id_based_sync_api_still_reloads_fresh_body(self):
        doc = create_ocr_document(
            title="Fresh body reload",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/ocr-body-reload/original.jpg",
            mime_type="image/jpeg",
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="fresh-body-via-id-sync",
        )
        # Direct DTR create is out of PR2b-1 scope; id-based API still rebuilds.
        index = sync_archive_item_search_index(doc.archive_item_id)
        self.assertIsNotNone(index)
        assert index is not None
        self.assertEqual(index.body_text, "fresh-body-via-id-sync")
