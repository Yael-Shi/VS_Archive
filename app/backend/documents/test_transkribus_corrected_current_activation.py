"""Tests for corrected/current Transkribus sync activation (PR1 service)."""

from __future__ import annotations

import hashlib

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusRunAutomaticSnapshot,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_corrected_current_activation import (
    CorrectedCurrentActivationError,
    CorrectedCurrentActivationErrorCode,
    CorrectedCurrentActivationResult,
    activate_corrected_current_sync_attempt,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex

User = get_user_model()

_TEST_PARSER_VERSION = "test_parser_v1"
_ENGINE = "transkribus-pylaia:42"
_CANONICAL = "Corrected canonical text"
_OLD_TEXT = "Old displayed source text"
_ACTIVATED_BY_DEFAULT: object = object()


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def _create_doc(*, language: str = Document.Language.HEBREW, **kwargs) -> Document:
    defaults = dict(
        title="Activation doc",
        doc_type=Document.DocType.PDF,
        language=language,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key=f"activation-{language}.pdf",
        mime_type="application/pdf",
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _upload_run(doc: Document, **kwargs) -> TranskribusRun:
    defaults = dict(
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
    defaults.update(kwargs)
    return TranskribusRun.objects.create(**defaults)


def _ready_snapshot(
    *,
    document: Document,
    run: TranskribusRun,
    text: str = _CANONICAL,
    source_kind: str = TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
    hover_eligible: bool = False,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{run.pk}:{TranskribusTranscriptSnapshot.objects.count()}"
    return TranskribusTranscriptSnapshot.objects.create(
        document=document,
        transkribus_run=run,
        source_kind=source_kind,
        remote_doc_id=str(run.remote_doc_id or ""),
        collection_id=str(run.collection_id or ""),
        model_id=str(run.model_id or ""),
        recognition_job_id=str(run.recognition_job_id or ""),
        parser_version=_TEST_PARSER_VERSION,
        provider_identity_fingerprint=_sha256_hex(f"prov:{unique}"),
        raw_xml_fingerprint=_sha256_hex(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha256_hex(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.PARTIAL,
        hover_eligible=hover_eligible,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )


def _add_snapshot_page(
    snapshot: TranskribusTranscriptSnapshot,
    *,
    page_index: int = 1,
    page_nr: int = 1,
    transcript_ts_id: str = "ts-1",
) -> TranskribusSnapshotPage:
    return TranskribusSnapshotPage.objects.create(
        snapshot=snapshot,
        page_index=page_index,
        page_nr=page_nr,
        transcript_ts_id=transcript_ts_id,
        page_xml_sha256=_sha256_hex(f"xml:{snapshot.pk}:{page_index}"),
        page_xml_s3_key=f"s3://test/{snapshot.pk}/{page_index}.xml",
    )


def _completed_attempt(
    *,
    doc: Document,
    run: TranskribusRun,
    snapshot: TranskribusTranscriptSnapshot,
    user,
    transcript_ts_id: str = "ts-1",
) -> TranskribusCorrectedCurrentSyncAttempt:
    attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
        document=doc,
        transkribus_run=run,
        initiated_by=user,
        status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
        resolved_snapshot=snapshot,
        storage_outcome=TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED,
        completed_at=timezone.now(),
    )
    TranskribusCorrectedCurrentSyncPage.objects.create(
        attempt=attempt,
        page_index=1,
        page_nr=1,
        outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
        transcript_ts_id=transcript_ts_id,
    )
    return attempt


def _source_row(
    doc: Document,
    *,
    text: str = _OLD_TEXT,
    engine: str = _ENGINE,
    source_revision: int = 2,
    verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
    status: str = DocumentTextResult.Status.NEEDS_REVIEW,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=status,
        verification_status=verification_status,
        text=text,
        source_revision=source_revision,
    )


def _hebrew_row(
    doc: Document,
    *,
    text: str = _OLD_TEXT,
    engine: str = _ENGINE,
    based_on_source_revision: int = 2,
    verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=verification_status,
        text=text,
        based_on_source_revision=based_on_source_revision,
    )


def _bind(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
    role: str,
    bound_source_revision: int,
    text_for_hash: str | None = None,
) -> TranskribusTextResultBinding:
    text = text_for_hash if text_for_hash is not None else (text_result.text or "")
    return TranskribusTextResultBinding.objects.create(
        text_result=text_result,
        snapshot=snapshot,
        binding_role=role,
        bound_text_sha256=compute_sha256_hex(text),
        bound_source_revision=bound_source_revision,
    )


class CorrectedCurrentActivationTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="activation_staff", password="test-pass", is_staff=True
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

    def _activate(
        self,
        *,
        document_id: int | None = None,
        attempt_id: int | None = None,
        source_text_result_id: int | None = None,
        activated_by: object | None = _ACTIVATED_BY_DEFAULT,
        expected_source_revision: int | None = None,
        expected_source_sha256: str | None = None,
    ) -> CorrectedCurrentActivationResult:
        return activate_corrected_current_sync_attempt(
            document_id=self.doc.pk if document_id is None else document_id,
            attempt_id=self.attempt.pk if attempt_id is None else attempt_id,
            source_text_result_id=(
                self.source.pk
                if source_text_result_id is None
                else source_text_result_id
            ),
            activated_by=(
                self.user if activated_by is _ACTIVATED_BY_DEFAULT else activated_by
            ),
            expected_source_revision=(
                self.source.source_revision
                if expected_source_revision is None
                else expected_source_revision
            ),
            expected_source_sha256=(
                compute_sha256_hex(self.source.text or "")
                if expected_source_sha256 is None
                else expected_source_sha256
            ),
        )

    def test_hebrew_happy_path_applies_text_mirror_bindings_and_audit(self):
        prior_state = self.doc.processing_state_user
        prior_status = self.source.status
        prior_verification = self.source.verification_status
        result = self._activate()

        self.assertEqual(result.outcome, "APPLIED")
        self.assertTrue(result.source_text_changed)
        self.assertTrue(result.hebrew_mirror_updated)
        self.assertEqual(result.bound_source_revision, 3)
        self.assertEqual(result.hebrew_result_id, self.hebrew.pk)

        self.source.refresh_from_db()
        self.hebrew.refresh_from_db()
        self.assertEqual(self.source.text, _CANONICAL)
        self.assertEqual(self.hebrew.text, _CANONICAL)
        self.assertEqual(self.source.source_revision, 3)
        self.assertEqual(self.hebrew.based_on_source_revision, 3)
        self.assertEqual(self.source.status, prior_status)
        self.assertEqual(self.source.verification_status, prior_verification)
        self.assertEqual(self.hebrew.verification_status, prior_verification)

        src_bind = TranskribusTextResultBinding.objects.get(text_result=self.source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=self.hebrew)
        self.assertEqual(src_bind.snapshot_id, self.snapshot.pk)
        self.assertEqual(he_bind.snapshot_id, self.snapshot.pk)
        self.assertEqual(
            src_bind.binding_role,
            TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        )
        self.assertEqual(
            he_bind.binding_role,
            TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
        )
        self.assertEqual(src_bind.bound_by_id, self.user.pk)
        self.assertEqual(he_bind.bound_by_id, self.user.pk)
        self.assertEqual(src_bind.bound_source_revision, 3)
        self.assertEqual(he_bind.bound_source_revision, 3)
        self.assertEqual(src_bind.bound_text_sha256, _sha256_hex(_CANONICAL))

        edits = DocumentTextResultEdit.objects.filter(text_result=self.source)
        self.assertEqual(edits.count(), 1)
        edit = edits.get()
        self.assertEqual(edit.old_text, _OLD_TEXT)
        self.assertEqual(edit.new_text, _CANONICAL)
        self.assertEqual(edit.editor_id, self.user.pk)
        self.assertEqual(edit.edit_type, DocumentTextResultEdit.EditType.SOURCE_TEXT)
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=self.hebrew).exists()
        )

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.processing_state_user, prior_state)
        self.assertFalse(
            TranskribusRunAutomaticSnapshot.objects.filter(
                run=self.transkribus_run
            ).exists()
        )

    def test_non_hebrew_updates_source_only_and_leaves_hebrew_stale(self):
        doc = _create_doc(
            language=Document.Language.ENGLISH,
            title="EN activation",
            file_s3_key="en-activation.pdf",
        )
        run = _upload_run(doc, remote_doc_id="888")
        snapshot = _ready_snapshot(document=doc, run=run)
        _add_snapshot_page(snapshot)
        attempt = _completed_attempt(
            doc=doc, run=run, snapshot=snapshot, user=self.user
        )
        source = _source_row(doc, source_revision=4)
        hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="תרגום ישן",
            based_on_source_revision=4,
        )

        result = activate_corrected_current_sync_attempt(
            document_id=doc.pk,
            attempt_id=attempt.pk,
            source_text_result_id=source.pk,
            activated_by=self.user,
            expected_source_revision=4,
            expected_source_sha256=compute_sha256_hex(_OLD_TEXT),
        )
        self.assertEqual(result.outcome, "APPLIED")
        self.assertIsNone(result.hebrew_result_id)
        self.assertTrue(result.source_text_changed)
        self.assertFalse(result.hebrew_mirror_updated)

        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, _CANONICAL)
        self.assertEqual(source.source_revision, 5)
        self.assertEqual(hebrew.text, "תרגום ישן")
        self.assertEqual(hebrew.based_on_source_revision, 4)
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(text_result=source).exists()
        )
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(text_result=hebrew).exists()
        )

    def test_idempotent_already_active_reuses_original_preview_tokens(self):
        original_revision = self.source.source_revision
        original_sha = compute_sha256_hex(self.source.text or "")
        first = self._activate(
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(first.outcome, "APPLIED")
        self.assertTrue(first.source_text_changed)

        # Replay exact same request tokens (pre-activation); must be ALREADY_ACTIVE.
        second = self._activate(
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(second.outcome, "ALREADY_ACTIVE")
        self.assertFalse(second.source_text_changed)
        self.assertFalse(second.hebrew_mirror_updated)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.source_revision, 3)
        self.assertEqual(self.source.text, _CANONICAL)

    def test_binding_repair_without_revision_or_audit(self):
        self.source.text = _CANONICAL
        self.source.source_revision = 2
        self.source.save(update_fields=["text", "source_revision", "updated_at"])
        self.hebrew.text = _CANONICAL
        self.hebrew.based_on_source_revision = 2
        self.hebrew.save(
            update_fields=["text", "based_on_source_revision", "updated_at"]
        )

        result = self._activate(
            expected_source_revision=2,
            expected_source_sha256=compute_sha256_hex(_CANONICAL),
        )
        self.assertEqual(result.outcome, "APPLIED")
        self.assertFalse(result.source_text_changed)
        self.assertFalse(result.hebrew_mirror_updated)
        self.assertEqual(result.bound_source_revision, 2)
        self.source.refresh_from_db()
        self.assertEqual(self.source.source_revision, 2)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)
        bind = TranskribusTextResultBinding.objects.get(text_result=self.source)
        self.assertEqual(bind.snapshot_id, self.snapshot.pk)
        self.assertEqual(bind.bound_by_id, self.user.pk)

    def test_hebrew_mirror_only_repair_does_not_bump_source_or_audit(self):
        self.source.text = _CANONICAL
        self.source.source_revision = 2
        self.source.save(update_fields=["text", "source_revision", "updated_at"])
        self.hebrew.text = "Stale hebrew mirror"
        self.hebrew.based_on_source_revision = 1
        self.hebrew.save(
            update_fields=["text", "based_on_source_revision", "updated_at"]
        )

        result = self._activate(
            expected_source_revision=2,
            expected_source_sha256=compute_sha256_hex(_CANONICAL),
        )
        self.assertEqual(result.outcome, "APPLIED")
        self.assertFalse(result.source_text_changed)
        self.assertTrue(result.hebrew_mirror_updated)
        self.assertEqual(result.bound_source_revision, 2)

        self.source.refresh_from_db()
        self.hebrew.refresh_from_db()
        self.assertEqual(self.source.text, _CANONICAL)
        self.assertEqual(self.source.source_revision, 2)
        self.assertEqual(self.hebrew.text, _CANONICAL)
        self.assertEqual(self.hebrew.based_on_source_revision, 2)
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=self.hebrew)
        self.assertEqual(he_bind.bound_source_revision, 2)
        self.assertEqual(he_bind.bound_text_sha256, _sha256_hex(_CANONICAL))

    def test_stale_preview_revision_blocked(self):
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(expected_source_revision=99)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.STALE_PREVIEW
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, _OLD_TEXT)

    def test_stale_preview_hash_blocked(self):
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(expected_source_sha256="0" * 64)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.STALE_PREVIEW
        )

    def test_verified_source_blocked(self):
        self.source.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
        self.source.save(update_fields=["verification_status", "updated_at"])
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED
        )

    def test_verified_hebrew_mirror_blocked(self):
        self.hebrew.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
        self.hebrew.save(update_fields=["verification_status", "updated_at"])
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED
        )

    def test_human_edited_after_bind_blocked(self):
        other_snap = _ready_snapshot(
            document=self.doc,
            run=self.transkribus_run,
            text=_OLD_TEXT,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        )
        _add_snapshot_page(other_snap, transcript_ts_id="ts-old")
        _bind(
            text_result=self.source,
            snapshot=other_snap,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=2,
            text_for_hash=_OLD_TEXT,
        )
        # Drift: revision bumped after bind while text hash still matches bound text.
        self.source.source_revision = 3
        self.source.save(update_fields=["source_revision", "updated_at"])

        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(
                expected_source_revision=3,
                expected_source_sha256=compute_sha256_hex(_OLD_TEXT),
            )
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED,
        )

    def test_wrong_attempt_document_mismatch(self):
        other = _create_doc(title="Other", file_s3_key="other.pdf")
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(document_id=other.pk)
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.ATTEMPT_DOCUMENT_MISMATCH,
        )

    def test_attempt_not_completed(self):
        self.attempt.status = TranskribusCorrectedCurrentSyncAttempt.Status.STARTED
        self.attempt.resolved_snapshot = None
        self.attempt.storage_outcome = None
        self.attempt.completed_at = None
        self.attempt.save()
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_COMPLETED,
        )

    def test_wrong_result_type(self):
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(source_text_result_id=self.hebrew.pk)
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.TARGET_NOT_SOURCE_TEXT,
        )

    def test_target_from_other_document(self):
        other = _create_doc(title="Other2", file_s3_key="other2.pdf")
        other_source = _source_row(other, engine="other-engine")
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(source_text_result_id=other_source.pk)
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.TARGET_DOCUMENT_MISMATCH,
        )

    def test_snapshot_page_mismatch(self):
        page = self.attempt.pages.get()
        page.transcript_ts_id = "ts-mismatch"
        page.save(update_fields=["transcript_ts_id"])
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.SNAPSHOT_PAGE_MISMATCH,
        )

    def test_canonical_hash_mismatch(self):
        self.snapshot.canonical_text_sha256 = "0" * 64
        self.snapshot.save(update_fields=["canonical_text_sha256"])
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.CANONICAL_HASH_MISMATCH,
        )

    def test_empty_canonical_text_rejected(self):
        self.snapshot.canonical_text = "   "
        self.snapshot.canonical_text_sha256 = _sha256_hex("   ")
        self.snapshot.save(update_fields=["canonical_text", "canonical_text_sha256"])
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.CANONICAL_TEXT_EMPTY,
        )

    def test_reused_automatic_htr_snapshot_allowed(self):
        auto_snap = _ready_snapshot(
            document=self.doc,
            run=self.transkribus_run,
            text=_CANONICAL,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        )
        _add_snapshot_page(auto_snap, transcript_ts_id="ts-1")
        self.attempt.resolved_snapshot = auto_snap
        self.attempt.storage_outcome = (
            TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.REUSED_EXISTING
        )
        self.attempt.save(update_fields=["resolved_snapshot", "storage_outcome"])
        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")
        self.assertEqual(result.snapshot_id, auto_snap.pk)
        bind = TranskribusTextResultBinding.objects.get(text_result=self.source)
        self.assertEqual(bind.snapshot_id, auto_snap.pk)

    def test_hover_eligible_false_still_activates(self):
        self.assertFalse(self.snapshot.hover_eligible)
        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")

    def test_missing_hebrew_mirror_blocked(self):
        self.hebrew.delete()
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HEBREW_MIRROR_MISSING,
        )

    def test_preserves_rejected_verification_status(self):
        self.source.verification_status = DocumentTextResult.VerificationStatus.REJECTED
        self.source.save(update_fields=["verification_status", "updated_at"])
        self.hebrew.verification_status = DocumentTextResult.VerificationStatus.REJECTED
        self.hebrew.save(update_fields=["verification_status", "updated_at"])
        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")
        self.source.refresh_from_db()
        self.hebrew.refresh_from_db()
        self.assertEqual(
            self.source.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertEqual(
            self.hebrew.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )

    def test_attempt_not_found(self):
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(attempt_id=9_999_999)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_FOUND
        )

    def test_no_automatic_snapshot_association_created(self):
        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")
        self.assertFalse(
            TranskribusRunAutomaticSnapshot.objects.filter(
                run=self.transkribus_run
            ).exists()
        )

    def test_pre_binding_human_edit_history_blocked(self):
        DocumentTextResultEdit.objects.create(
            text_result=self.source,
            editor=self.user,
            old_text="earlier",
            new_text=_OLD_TEXT,
            edit_type=DocumentTextResultEdit.EditType.SOURCE_TEXT,
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED,
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, _OLD_TEXT)
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.source
            ).exists()
        )

    def test_malformed_binding_with_edit_history_blocked(self):
        DocumentTextResultEdit.objects.create(
            text_result=self.source,
            editor=self.user,
            old_text="earlier",
            new_text=_OLD_TEXT,
            edit_type=DocumentTextResultEdit.EditType.SOURCE_TEXT,
        )
        # Malformed: bound hash does not match the binding's snapshot canonical.
        TranskribusTextResultBinding.objects.create(
            text_result=self.source,
            snapshot=self.snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha256_hex(_OLD_TEXT),
            bound_source_revision=2,
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED,
        )

    def test_activation_edit_plus_fresh_binding_retry_is_already_active(self):
        original_revision = self.source.source_revision
        original_sha = compute_sha256_hex(self.source.text or "")
        first = self._activate(
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(first.outcome, "APPLIED")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.source
            ).exists()
        )

        second = self._activate(
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(second.outcome, "ALREADY_ACTIVE")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)

    def test_authorized_actor_written_to_bound_by_and_edit(self):
        result = self._activate()
        self.assertEqual(result.outcome, "APPLIED")
        edit = DocumentTextResultEdit.objects.get(text_result=self.source)
        self.assertEqual(edit.editor_id, self.user.pk)
        src_bind = TranskribusTextResultBinding.objects.get(text_result=self.source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=self.hebrew)
        self.assertEqual(src_bind.bound_by_id, self.user.pk)
        self.assertEqual(he_bind.bound_by_id, self.user.pk)

    def test_non_admin_actor_rejected(self):
        non_admin = User.objects.create_user(
            username="activation_user", password="test-pass", is_staff=False
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(activated_by=non_admin)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED
        )
        self.source.refresh_from_db()
        self.assertEqual(self.source.text, _OLD_TEXT)

    def test_anonymous_actor_rejected(self):
        from django.contrib.auth.models import AnonymousUser

        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(activated_by=AnonymousUser())
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED
        )

    def test_inactive_staff_actor_rejected(self):
        inactive = User.objects.create_user(
            username="inactive_staff",
            password="test-pass",
            is_staff=True,
            is_active=False,
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(activated_by=inactive)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED
        )

    def test_missing_actor_rejected(self):
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(activated_by=None)
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED
        )

    def test_unauthorized_cannot_use_already_active_fast_path(self):
        original_revision = self.source.source_revision
        original_sha = compute_sha256_hex(self.source.text or "")
        first = self._activate(
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(first.outcome, "APPLIED")

        non_admin = User.objects.create_user(
            username="activation_user2", password="test-pass", is_staff=False
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate(
                activated_by=non_admin,
                expected_source_revision=original_revision,
                expected_source_sha256=original_sha,
            )
        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED
        )

    def test_edit_history_blocked_when_binding_snapshot_not_ready(self):
        pending = TranskribusTranscriptSnapshot.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version=_TEST_PARSER_VERSION,
            provider_identity_fingerprint=_sha256_hex("pending-prov"),
            raw_xml_fingerprint=_sha256_hex("pending-raw"),
            canonical_text=_OLD_TEXT,
            canonical_text_sha256=_sha256_hex(_OLD_TEXT),
            geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.PARTIAL,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
        )
        DocumentTextResultEdit.objects.create(
            text_result=self.source,
            editor=self.user,
            old_text="earlier",
            new_text=_OLD_TEXT,
            edit_type=DocumentTextResultEdit.EditType.SOURCE_TEXT,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=self.source,
            snapshot=pending,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha256_hex(_OLD_TEXT),
            bound_source_revision=2,
        )
        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED,
        )

    def test_edit_history_blocked_when_binding_snapshot_hash_integrity_broken(self):
        DocumentTextResultEdit.objects.create(
            text_result=self.source,
            editor=self.user,
            old_text="earlier",
            new_text=_OLD_TEXT,
            edit_type=DocumentTextResultEdit.EditType.SOURCE_TEXT,
        )
        other = _ready_snapshot(
            document=self.doc,
            run=self.transkribus_run,
            text=_OLD_TEXT,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=self.source,
            snapshot=other,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha256_hex(_OLD_TEXT),
            bound_source_revision=2,
        )
        # Break stored SHA integrity after bind.
        other.canonical_text_sha256 = "0" * 64
        other.save(update_fields=["canonical_text_sha256"])

        with self.assertRaises(CorrectedCurrentActivationError) as ctx:
            self._activate()
        self.assertEqual(
            ctx.exception.code,
            CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED,
        )

    def test_hebrew_mirror_binding_failure_rolls_back_all_writes(self):
        from unittest import mock

        from documents.services import transkribus_corrected_current_activation as mod
        from documents.services.transkribus_snapshot_binding import (
            TranskribusSnapshotBindingError,
            bind_text_result_to_snapshot,
        )

        real_bind = bind_text_result_to_snapshot
        call_count = {"n": 0}

        def _bind_side_effect(**kwargs):
            call_count["n"] += 1
            role = kwargs.get("binding_role")
            if role == TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR:
                raise TranskribusSnapshotBindingError("simulated hebrew bind failure")
            return real_bind(**kwargs)

        source_before = (
            self.source.text,
            self.source.source_revision,
            self.source.verification_status,
        )
        hebrew_before = (
            self.hebrew.text,
            self.hebrew.based_on_source_revision,
            self.hebrew.verification_status,
        )

        with mock.patch.object(
            mod, "bind_text_result_to_snapshot", side_effect=_bind_side_effect
        ):
            with self.assertRaises(CorrectedCurrentActivationError) as ctx:
                self._activate()

        self.assertEqual(
            ctx.exception.code, CorrectedCurrentActivationErrorCode.BINDING_FAILED
        )
        self.assertIsNone(ctx.exception.__cause__)

        self.source.refresh_from_db()
        self.hebrew.refresh_from_db()
        self.assertEqual(
            (
                self.source.text,
                self.source.source_revision,
                self.source.verification_status,
            ),
            source_before,
        )
        self.assertEqual(
            (
                self.hebrew.text,
                self.hebrew.based_on_source_revision,
                self.hebrew.verification_status,
            ),
            hebrew_before,
        )
        self.assertEqual(DocumentTextResultEdit.objects.count(), 0)
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result__in=[self.source, self.hebrew]
            ).exists()
        )
        self.assertGreaterEqual(call_count["n"], 2)


class CorrectedCurrentActivationConcurrencyTests(TransactionTestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="activation_concurrent", password="test-pass", is_staff=True
        )
        self.doc = _create_doc(title="Concurrent", file_s3_key="concurrent.pdf")
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

    def test_sequential_retry_reuses_original_preview_tokens(self):
        original_revision = self.source.source_revision
        original_sha = compute_sha256_hex(self.source.text or "")
        first = activate_corrected_current_sync_attempt(
            document_id=self.doc.pk,
            attempt_id=self.attempt.pk,
            source_text_result_id=self.source.pk,
            activated_by=self.user,
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        second = activate_corrected_current_sync_attempt(
            document_id=self.doc.pk,
            attempt_id=self.attempt.pk,
            source_text_result_id=self.source.pk,
            activated_by=self.user,
            expected_source_revision=original_revision,
            expected_source_sha256=original_sha,
        )
        self.assertEqual(first.outcome, "APPLIED")
        self.assertEqual(second.outcome, "ALREADY_ACTIVE")
        self.assertEqual(DocumentTextResultEdit.objects.count(), 1)
        self.source.refresh_from_db()
        self.assertEqual(self.source.source_revision, 3)
        self.assertEqual(self.source.text, _CANONICAL)

    def test_binding_helper_sets_bound_by(self):
        from documents.services.transkribus_snapshot_binding import (
            bind_text_result_to_snapshot,
        )

        self.source.text = _CANONICAL
        self.source.save(update_fields=["text", "updated_at"])
        binding = bind_text_result_to_snapshot(
            text_result=self.source,
            snapshot=self.snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=2,
            bound_by=self.user,
        )
        self.assertEqual(binding.bound_by_id, self.user.pk)
