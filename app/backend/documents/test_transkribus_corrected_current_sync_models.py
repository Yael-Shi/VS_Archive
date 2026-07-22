"""Model tests for corrected/current Transkribus sync provenance (schema PR)."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import RestrictedError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from documents.models import (
    Document,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_corrected_current_selection import (
    CorrectedCurrentSelectionErrorCode,
)

User = get_user_model()

_TEST_PARSER_VERSION = "test_parser_v1"


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def _snapshot_storage_outcome_values_from_source() -> frozenset[str]:
    """Read SnapshotStorageOutcome string values without importing the service module."""
    path = (
        Path(__file__).resolve().parent / "services" / "transkribus_snapshot_storage.py"
    )
    if not path.is_file():
        raise RuntimeError(
            "Cannot locate transkribus_snapshot_storage.py for AST parity check: "
            f"{path}"
        )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "SnapshotStorageOutcome":
            continue
        values: list[str] = []
        for item in node.body:
            value_node = None
            if isinstance(item, ast.Assign) and len(item.targets) == 1:
                if isinstance(item.targets[0], ast.Name):
                    value_node = item.value
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                value_node = item.value
            if isinstance(value_node, ast.Constant) and isinstance(
                value_node.value, str
            ):
                values.append(value_node.value)
        if not values:
            raise RuntimeError(
                "SnapshotStorageOutcome in transkribus_snapshot_storage.py has no "
                "string-valued enum assignments."
            )
        return frozenset(values)
    raise RuntimeError(
        "SnapshotStorageOutcome class not found in transkribus_snapshot_storage.py"
    )


_SNAPSHOT_STORAGE_OUTCOME_VALUES = _snapshot_storage_outcome_values_from_source()


def _create_he_doc(**kwargs) -> Document:
    defaults = dict(
        title="Corrected-current sync doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key="he.pdf",
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
    )
    defaults.update(kwargs)
    return TranskribusRun.objects.create(**defaults)


def _ready_snapshot(
    *, document: Document, run: TranskribusRun
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{run.pk}:{TranskribusTranscriptSnapshot.objects.count()}"
    return TranskribusTranscriptSnapshot.objects.create(
        document=document,
        transkribus_run=run,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        remote_doc_id=str(run.remote_doc_id or ""),
        collection_id=str(run.collection_id or ""),
        model_id=str(run.model_id or ""),
        recognition_job_id=str(run.recognition_job_id or ""),
        parser_version=_TEST_PARSER_VERSION,
        provider_identity_fingerprint=_sha256_hex(f"prov:{unique}"),
        raw_xml_fingerprint=_sha256_hex(f"raw:{unique}"),
        canonical_text="Hello",
        canonical_text_sha256=_sha256_hex("Hello"),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )


def _staff_user() -> Any:
    return User.objects.create_user(username="cc_sync_staff", password="test-pass")


class TranskribusCorrectedCurrentSyncAttemptTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()

    def _started_attempt(self) -> TranskribusCorrectedCurrentSyncAttempt:
        return TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )

    def test_multiple_attempts_per_document_allowed(self):
        self._started_attempt()
        self._started_attempt()
        self.assertEqual(
            TranskribusCorrectedCurrentSyncAttempt.objects.filter(
                document=self.doc
            ).count(),
            2,
        )

    def test_started_shape_persists(self):
        attempt = self._started_attempt()
        self.assertIsNone(attempt.completed_at)
        self.assertIsNone(attempt.resolved_snapshot_id)

    def test_completed_requires_snapshot_and_storage_outcome(self):
        snap = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        now = timezone.now()
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            completed_at=now,
            resolved_snapshot=snap,
            storage_outcome=TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED,
        )
        self.assertEqual(attempt.resolved_snapshot_id, snap.pk)

    def test_refused_shape_persists(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
            completed_at=timezone.now(),
        )
        self.assertIsNone(attempt.resolved_snapshot_id)

    def test_failed_shape_persists(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            completed_at=timezone.now(),
            failure_code="SNAPSHOT_STORAGE_FAILED",
            failure_message="Storage failed.",
        )
        self.assertIsNone(attempt.resolved_snapshot_id)

    def test_started_with_completed_at_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncAttempt.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
                    completed_at=timezone.now(),
                )

    def test_completed_without_snapshot_rejected_by_db(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncAttempt.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
                    completed_at=timezone.now(),
                    storage_outcome=(
                        TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
                    ),
                )

    def test_run_must_match_document_and_upload_created_mapping(self):
        other_doc = _create_he_doc(title="Other")
        other_run = _upload_run(other_doc)
        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncAttempt(
                document=self.doc,
                transkribus_run=other_run,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
            ).save()

        bad_mode = _upload_run(
            self.doc,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            page_index_to_page_nr={1: 1},
        )
        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncAttempt(
                document=self.doc,
                transkribus_run=bad_mode,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
            ).save()

        empty_map = _upload_run(self.doc, page_index_to_page_nr={})
        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncAttempt(
                document=self.doc,
                transkribus_run=empty_map,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
            ).save()

    def test_resolved_snapshot_must_be_ready_and_same_document(self):
        snap = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        other_doc = _create_he_doc(title="Snap other")
        other_run = _upload_run(other_doc)
        other_snap = _ready_snapshot(document=other_doc, run=other_run)

        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncAttempt(
                document=self.doc,
                transkribus_run=self.transkribus_run,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
                completed_at=timezone.now(),
                resolved_snapshot=other_snap,
                storage_outcome=(
                    TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
                ),
            ).save()

        snap.storage_status = TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD
        snap.save(update_fields=["storage_status"])
        with self.assertRaises(ValidationError):
            TranskribusCorrectedCurrentSyncAttempt(
                document=self.doc,
                transkribus_run=self.transkribus_run,
                initiated_by=self.user,
                status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
                completed_at=timezone.now(),
                resolved_snapshot=snap,
                storage_outcome=(
                    TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
                ),
            ).save()


class TranskribusCorrectedCurrentSyncEnumParityTests(SimpleTestCase):
    def test_storage_outcome_values_match_snapshot_storage_outcome(self):
        model_values = set(TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.values)
        self.assertEqual(model_values, set(_SNAPSHOT_STORAGE_OUTCOME_VALUES))

    def test_selection_error_code_values_match_selector_constants(self):
        model_values = set(
            TranskribusCorrectedCurrentSyncPage.SelectionErrorCode.values
        )
        selector_values = {
            CorrectedCurrentSelectionErrorCode.ZERO_TRANSCRIPTS,
            CorrectedCurrentSelectionErrorCode.MULTIPLE_TRANSCRIPTS,
            CorrectedCurrentSelectionErrorCode.MISSING_TS_ID,
        }
        self.assertEqual(model_values, selector_values)


class TranskribusCorrectedCurrentSyncPageTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()
        self.attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )

    def test_selected_page_shape(self):
        page = TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=self.attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-42",
            remote_transcript_status="IN_PROGRESS",
            in_progress_warning=True,
        )
        self.assertEqual(page.selection_error_code, "")

    def test_refused_page_shape(self):
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=self.attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED,
            selection_error_code=(
                TranskribusCorrectedCurrentSyncPage.SelectionErrorCode.ZERO_TRANSCRIPTS
            ),
            selection_error_message="No transcripts on page.",
        )

    def test_selected_without_ts_id_rejected(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=self.attempt,
                    page_index=1,
                    page_nr=1,
                    outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
                    transcript_ts_id="",
                )

    def test_refused_with_ts_id_rejected(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=self.attempt,
                    page_index=1,
                    page_nr=1,
                    outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED,
                    transcript_ts_id="ts-1",
                    selection_error_code=(
                        TranskribusCorrectedCurrentSyncPage.SelectionErrorCode.MULTIPLE_TRANSCRIPTS
                    ),
                    selection_error_message="More than one transcript.",
                )

    def test_unique_page_index_and_page_nr(self):
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=self.attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-1",
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=self.attempt,
                    page_index=1,
                    page_nr=2,
                    outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
                    transcript_ts_id="ts-2",
                )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=self.attempt,
                    page_index=2,
                    page_nr=1,
                    outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
                    transcript_ts_id="ts-3",
                )

    def test_failed_attempt_may_retain_selected_pages(self):
        failed = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            completed_at=timezone.now(),
            failure_code="HTTP_ERROR",
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=failed,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-partial",
        )
        self.assertEqual(failed.pages.count(), 1)


class TranskribusCorrectedCurrentSyncDbConstraintTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()

    def _completed_attempt(self, **kwargs) -> TranskribusCorrectedCurrentSyncAttempt:
        snap = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        defaults = dict(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            completed_at=timezone.now(),
            resolved_snapshot=snap,
            storage_outcome=TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED,
        )
        defaults.update(kwargs)
        return TranskribusCorrectedCurrentSyncAttempt.objects.create(**defaults)

    def test_unknown_attempt_status_rejected(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncAttempt.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    initiated_by=self.user,
                    status="UNKNOWN",
                )

    def test_unknown_storage_outcome_rejected(self):
        snap = _ready_snapshot(document=self.doc, run=self.transkribus_run)
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncAttempt.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
                    completed_at=timezone.now(),
                    resolved_snapshot=snap,
                    storage_outcome="NOT_A_REAL_OUTCOME",
                )

    def test_unknown_page_outcome_rejected(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=attempt,
                    page_index=1,
                    page_nr=1,
                    outcome="MAYBE",
                    transcript_ts_id="ts-1",
                )

    def test_unknown_selection_error_code_rejected(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncPage.objects.create(
                    attempt=attempt,
                    page_index=1,
                    page_nr=1,
                    outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED,
                    selection_error_code="NO_TRANSCRIPTS",
                    selection_error_message="Invalid legacy code.",
                )

    def test_empty_failed_failure_code_rejected(self):
        with transaction.atomic():
            with self.assertRaises(IntegrityError):
                TranskribusCorrectedCurrentSyncAttempt.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    initiated_by=self.user,
                    status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
                    completed_at=timezone.now(),
                    failure_code="",
                )

    def test_deleting_initiated_by_sets_null(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        user_id = self.user.pk
        self.user.delete()
        attempt.refresh_from_db()
        self.assertIsNone(attempt.initiated_by_id)
        self.assertFalse(User.objects.filter(pk=user_id).exists())

    def test_document_delete_cascades_attempt_pages_run_and_snapshot(self):
        attempt = self._completed_attempt()
        snap_id = attempt.resolved_snapshot_id
        run_id = self.transkribus_run.pk
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="ts-1",
        )
        doc_id = self.doc.pk
        self.doc.delete()
        self.assertFalse(
            TranskribusCorrectedCurrentSyncAttempt.objects.filter(
                pk=attempt.pk
            ).exists()
        )
        self.assertFalse(TranskribusCorrectedCurrentSyncPage.objects.exists())
        self.assertFalse(Document.objects.filter(pk=doc_id).exists())
        self.assertFalse(TranskribusRun.objects.filter(pk=run_id).exists())
        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(pk=snap_id).exists()
        )

    def test_referenced_run_delete_restricted(self):
        TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        with transaction.atomic():
            with self.assertRaises(RestrictedError):
                self.transkribus_run.delete()

    def test_referenced_snapshot_delete_restricted(self):
        attempt = self._completed_attempt()
        snap = attempt.resolved_snapshot
        assert snap is not None
        with transaction.atomic():
            with self.assertRaises(RestrictedError):
                snap.delete()
