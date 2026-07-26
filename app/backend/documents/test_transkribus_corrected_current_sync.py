"""Tests for corrected/current Transkribus sync orchestration service."""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from documents.models import (
    Document,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusCorrectedCurrentSyncRequest,
    TranskribusRun,
    TranskribusTranscriptSnapshot,
)
from documents.services import transkribus_engine as tr
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_corrected_current_sync import (
    CorrectedCurrentSyncError,
    CorrectedCurrentSyncFailureCode,
    CorrectedCurrentSyncFencedOutError,
    CorrectedCurrentSyncTerminalConflictError,
    _transition_attempt_terminal,
    run_corrected_current_transkribus_sync,
)
from documents.services.transkribus_snapshot_storage import (
    SnapshotStorageOutcome,
    SnapshotStorageResult,
    TranskribusSnapshotStorageUploadError,
)

User = get_user_model()
_TEST_PARSER_VERSION = "test_parser_v1"
_SYNC_LOGGER = "documents.services.transkribus_corrected_current_sync"


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def _page_xml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        f"{body}\n"
        "</PcGts>"
    ).encode("utf-8")


_SIMPLE_PAGE_BODY = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Hello</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""


def _create_he_doc(**kwargs) -> Document:
    defaults = dict(
        title="Corrected-current sync orchestration doc",
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
        pages_query="1-2",
        recognition_job_id="job-1",
        page_index_to_page_nr={1: 1, 2: 2},
    )
    defaults.update(kwargs)
    return TranskribusRun.objects.create(**defaults)


def _staff_user() -> Any:
    return User.objects.create_user(username="cc_sync_orchestration", password="x")


def _trp_meta(
    page_nr: int,
    *,
    ts_id: str = "ts-1",
    status: str = "FINISHED",
    url: str | None = None,
    extra_transcripts: list[dict] | None = None,
) -> tr.TrpPageMetadata:
    transcripts = list(extra_transcripts or [])
    transcripts.insert(
        0,
        {
            "tsId": ts_id,
            "status": status,
            "url": url or f"https://example.test/transcript/{page_nr}/{ts_id}",
        },
    )
    return tr.TrpPageMetadata(
        page_nr=page_nr,
        page_id=100 + page_nr,
        doc_id=777,
        page_url=None,
        transcripts=transcripts,
    )


def _ready_snap_with_pages(
    *,
    document: Document,
    run: TranskribusRun,
    page_ts: list[tuple[int, int, str]],
) -> TranskribusTranscriptSnapshot:
    from documents.models import TranskribusSnapshotPage

    snap = TranskribusTranscriptSnapshot.objects.create(
        document=document,
        transkribus_run=run,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
        parser_version=_TEST_PARSER_VERSION,
        provider_identity_fingerprint=_sha256_hex("prov"),
        raw_xml_fingerprint=_sha256_hex("raw"),
        canonical_text="Hello",
        canonical_text_sha256=_sha256_hex("Hello"),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )
    for page_index, page_nr, ts_id in page_ts:
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=page_index,
            page_nr=page_nr,
            transcript_ts_id=ts_id,
            lines_with_non_empty_text=1,
            text_region_count=1,
            text_line_count=1,
            page_xml_sha256=_sha256_hex(f"xml-{ts_id}"),
            page_xml_s3_key=f"key-{ts_id}",
        )
    return snap


class CorrectedCurrentSyncOrchestrationTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()
        self._xml_counter = 0

    def _fetch_xml(self, url: str, *, bearer_token: str = "") -> bytes:
        self._xml_counter += 1
        return _page_xml(
            _SIMPLE_PAGE_BODY.replace("Hello", f"Page-{self._xml_counter}")
        )

    def _run_sync(self, **kwargs):
        defaults = dict(
            document_id=self.doc.pk,
            initiated_by=self.user,
            username="u",
            password="p",
            bearer_token="token",
            login=lambda *a, **k: None,
            session_factory=MagicMock,
        )
        defaults.update(kwargs)
        return run_corrected_current_transkribus_sync(**defaults)

    def test_success_created(self):
        pages_meta = [_trp_meta(1, ts_id="a"), _trp_meta(2, ts_id="b")]

        with patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes",
            return_value=None,
        ):
            with override_settings(UPLOADS_BUCKET_NAME="test-bucket"):
                result = self._run_sync(
                    fetch_pages_metadata=lambda *a, **k: pages_meta,
                    fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(
                        url, bearer_token=bearer_token
                    ),
                )

        self.assertFalse(result.refused)
        self.assertIsNotNone(result.snapshot)
        assert result.snapshot is not None
        self.assertEqual(result.storage_outcome, SnapshotStorageOutcome.CREATED)
        attempt = result.attempt
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED
        )
        self.assertEqual(attempt.pages.filter(outcome="SELECTED").count(), 2)

    def test_storage_reuse_outcomes(self):
        for outcome in (
            SnapshotStorageOutcome.REUSED_EXISTING,
            SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER,
        ):
            with self.subTest(outcome=outcome.value):
                doc = _create_he_doc(title=f"reuse-{outcome.value}")
                run = _upload_run(doc, page_index_to_page_nr={1: 1}, pages_query="1")
                snap = _ready_snap_with_pages(
                    document=doc,
                    run=run,
                    page_ts=[(1, 1, "ts-1")],
                )

                def store(**kwargs):
                    return SnapshotStorageResult(outcome=outcome, snapshot=snap)

                result = run_corrected_current_transkribus_sync(
                    document_id=doc.pk,
                    initiated_by=self.user,
                    username="u",
                    password="p",
                    bearer_token="t",
                    login=lambda *a, **k: None,
                    session_factory=MagicMock,
                    fetch_pages_metadata=lambda *a, **k: [_trp_meta(1)],
                    fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(
                        url
                    ),
                    store_snapshot=store,
                )
                self.assertEqual(result.storage_outcome, outcome)

    def test_refusal_persists_errors_without_fetch_or_storage(self):
        pages_meta = [
            _trp_meta(1),
            tr.TrpPageMetadata(
                page_nr=2,
                page_id=102,
                doc_id=777,
                page_url=None,
                transcripts=[],
            ),
        ]
        fetch_xml = MagicMock()
        store = MagicMock()

        result = self._run_sync(
            fetch_pages_metadata=lambda *a, **k: pages_meta,
            fetch_transcript_xml=fetch_xml,
            store_snapshot=store,
        )

        self.assertTrue(result.refused)
        attempt = result.attempt
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED
        )
        refused = attempt.pages.filter(
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED
        )
        self.assertEqual(refused.count(), 1)
        self.assertEqual(
            refused.get().selection_error_code,
            TranskribusCorrectedCurrentSyncPage.SelectionErrorCode.ZERO_TRANSCRIPTS,
        )
        fetch_xml.assert_not_called()
        store.assert_not_called()

    def test_in_progress_warning_on_selected_page(self):
        pages_meta = [
            _trp_meta(1, ts_id="a", status="IN_PROGRESS"),
            _trp_meta(2, ts_id="b"),
        ]
        snap = _ready_snap_with_pages(
            document=self.doc,
            run=self.transkribus_run,
            page_ts=[(1, 1, "a"), (2, 2, "b")],
        )

        result = self._run_sync(
            fetch_pages_metadata=lambda *a, **k: pages_meta,
            fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(url),
            store_snapshot=lambda **kwargs: SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.CREATED, snapshot=snap
            ),
        )
        page1 = result.attempt.pages.get(page_index=1)
        self.assertTrue(page1.in_progress_warning)
        self.assertEqual(page1.remote_transcript_status, "IN_PROGRESS")

    def test_multi_page_mapping(self):
        pages_meta = [_trp_meta(1, ts_id="p1"), _trp_meta(2, ts_id="p2")]
        snap = _ready_snap_with_pages(
            document=self.doc,
            run=self.transkribus_run,
            page_ts=[(1, 1, "p1"), (2, 2, "p2")],
        )

        result = self._run_sync(
            fetch_pages_metadata=lambda *a, **k: pages_meta,
            fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(url),
            store_snapshot=lambda **kwargs: SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.CREATED, snapshot=snap
            ),
        )
        self.assertEqual(result.attempt.pages.filter(outcome="SELECTED").count(), 2)

    def test_partial_fetch_failure_preserves_selected_pages(self):
        secret = "Bearer SECRET-XML-TOKEN"
        leak_url = "https://leak.example/transcript/2/p2"
        pages_meta = [
            _trp_meta(1, ts_id="p1"),
            _trp_meta(2, ts_id="p2", url=leak_url),
        ]

        def fetch_xml(url: str, *, bearer_token: str = "") -> bytes:
            if leak_url in url:
                raise tr.TranskribusPermanentError(
                    f"fetch failed url={leak_url} auth={secret}"
                )
            return self._fetch_xml(url, bearer_token=bearer_token)

        with self.assertLogs(_SYNC_LOGGER, level="ERROR") as logs:
            with self.assertRaises(CorrectedCurrentSyncError) as ctx:
                self._run_sync(
                    fetch_pages_metadata=lambda *a, **k: pages_meta,
                    fetch_transcript_xml=fetch_xml,
                )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML,
        )
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(leak_url, str(ctx.exception))
        log_blob = "\n".join(logs.output)
        self.assertNotIn(secret, log_blob)
        self.assertNotIn(leak_url, log_blob)
        self.assertIn("TranskribusPermanentError", log_blob)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.latest("id")
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
        )
        self.assertEqual(
            attempt.failure_code, CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML
        )
        self.assertNotIn(secret, attempt.failure_message or "")
        self.assertNotIn(leak_url, attempt.failure_message or "")
        self.assertEqual(attempt.pages.filter(outcome="SELECTED").count(), 2)

    def test_metadata_login_failure_marks_failed_with_safe_message(self):
        secret = "super-secret-password"

        def login(*args, **kwargs):
            raise tr.TranskribusPermanentError(f"login failed password={secret}")

        with self.assertRaises(CorrectedCurrentSyncError) as ctx:
            self._run_sync(
                login=login,
                fetch_pages_metadata=lambda *a, **k: [_trp_meta(1), _trp_meta(2)],
            )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.HTTP_METADATA,
        )
        self.assertNotIn(secret, str(ctx.exception))
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.latest("id")
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
        )
        self.assertEqual(
            attempt.failure_message,
            "Transkribus login or pages metadata request failed.",
        )

    def test_run_resolution_without_attempt(self):
        doc = _create_he_doc(title="no-run")
        with self.assertRaises(CorrectedCurrentSyncError) as ctx:
            run_corrected_current_transkribus_sync(
                document_id=doc.pk,
                initiated_by=self.user,
                username="u",
                password="p",
                bearer_token="t",
                login=lambda *a, **k: None,
                session_factory=MagicMock,
            )
        self.assertIsNone(ctx.exception.attempt_id)
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.RUN_RESOLUTION,
        )
        self.assertFalse(
            TranskribusCorrectedCurrentSyncAttempt.objects.filter(document=doc).exists()
        )

    def test_snapshot_mismatch_marks_failed(self):
        pages_meta = [_trp_meta(1, ts_id="p1"), _trp_meta(2, ts_id="p2")]
        snap = _ready_snap_with_pages(
            document=self.doc,
            run=self.transkribus_run,
            page_ts=[(1, 1, "wrong"), (2, 2, "p2")],
        )

        with self.assertRaises(CorrectedCurrentSyncError) as ctx:
            self._run_sync(
                fetch_pages_metadata=lambda *a, **k: pages_meta,
                fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(url),
                store_snapshot=lambda **kwargs: SnapshotStorageResult(
                    outcome=SnapshotStorageOutcome.CREATED, snapshot=snap
                ),
            )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.SNAPSHOT_PAGE_MISMATCH,
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.latest("id")
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
        )
        self.assertEqual(
            attempt.failure_code,
            CorrectedCurrentSyncFailureCode.SNAPSHOT_PAGE_MISMATCH,
        )

    def test_unsafe_external_error_not_persisted_or_raised(self):
        pages_meta = [_trp_meta(1, ts_id="p1"), _trp_meta(2, ts_id="p2")]
        token = "SECRET-STORAGE-TOKEN"
        leak = "https://evil.example/s3/object"

        def store(**kwargs):
            raise TranskribusSnapshotStorageUploadError(
                f"upload failed url={leak} token={token}"
            )

        with self.assertLogs(_SYNC_LOGGER, level="ERROR") as logs:
            with self.assertRaises(CorrectedCurrentSyncError) as ctx:
                self._run_sync(
                    fetch_pages_metadata=lambda *a, **k: pages_meta,
                    fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(
                        url
                    ),
                    store_snapshot=store,
                )
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE,
        )
        self.assertNotIn(token, str(ctx.exception))
        self.assertNotIn(leak, str(ctx.exception))
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.latest("id")
        self.assertNotIn(token, attempt.failure_message or "")
        self.assertNotIn(leak, attempt.failure_message or "")
        log_blob = "\n".join(logs.output)
        self.assertNotIn(token, log_blob)
        self.assertNotIn(leak, log_blob)
        self.assertIn("TranskribusSnapshotStorageUploadError", log_blob)

    def test_failed_transition_update_error_does_not_mask_sync_error(self):
        pages_meta = [_trp_meta(1, ts_id="p1"), _trp_meta(2, ts_id="p2")]
        token = "DB-UPDATE-LEAK-TOKEN"
        leak = "https://db-leak.example/internal"

        def store(**kwargs):
            raise TranskribusSnapshotStorageUploadError(
                f"upload failed url={leak} token={token}"
            )

        real_transition = _transition_attempt_terminal

        def transition_side_effect(attempt_id, **kwargs):
            if (
                kwargs.get("target_status")
                == TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
            ):
                raise DatabaseError(f"update failed {leak} {token}")
            return real_transition(attempt_id, **kwargs)

        with patch(
            "documents.services.transkribus_corrected_current_sync._transition_attempt_terminal",
            side_effect=transition_side_effect,
        ):
            with self.assertLogs(_SYNC_LOGGER, level="WARNING") as warn_logs:
                with self.assertRaises(CorrectedCurrentSyncError) as ctx:
                    self._run_sync(
                        fetch_pages_metadata=lambda *a, **k: pages_meta,
                        fetch_transcript_xml=lambda url,
                        *,
                        bearer_token: self._fetch_xml(url),
                        store_snapshot=store,
                    )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE,
        )
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn(token, str(ctx.exception))
        warn_blob = "\n".join(warn_logs.output)
        self.assertNotIn(token, warn_blob)
        self.assertNotIn(leak, warn_blob)
        self.assertIn("DatabaseError", warn_blob)

    def test_value_error_outside_mapping_phases_is_unexpected(self):
        pages_meta = [_trp_meta(1, ts_id="p1"), _trp_meta(2, ts_id="p2")]
        secret = "VALUE-ERROR-LEAK"

        with patch(
            "documents.services.transkribus_corrected_current_sync.select_corrected_current_transcripts_for_document",
            side_effect=ValueError(f"unexpected {secret}"),
        ):
            with self.assertRaises(CorrectedCurrentSyncError) as ctx:
                self._run_sync(
                    fetch_pages_metadata=lambda *a, **k: pages_meta,
                    fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(
                        url
                    ),
                )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.UNEXPECTED,
        )
        self.assertIsNone(ctx.exception.__cause__)
        self.assertNotIn(secret, str(ctx.exception))

    def test_matching_terminal_failed_retry_is_idempotent(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        code = CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE
        msg = "Transkribus transcript snapshot storage failed."
        first = _transition_attempt_terminal(
            attempt.pk,
            target_status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code=code,
            failure_message=msg,
        )
        second = _transition_attempt_terminal(
            attempt.pk,
            target_status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code=code,
            failure_message=msg,
        )
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.failure_code, code)
        self.assertEqual(second.failure_message, msg)

    def test_conflicting_terminal_retry_leaves_original_unchanged(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        original_code = CorrectedCurrentSyncFailureCode.HTTP_METADATA
        original_msg = "Transkribus login or pages metadata request failed."
        _transition_attempt_terminal(
            attempt.pk,
            target_status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code=original_code,
            failure_message=original_msg,
        )
        with self.assertRaises(CorrectedCurrentSyncTerminalConflictError):
            _transition_attempt_terminal(
                attempt.pk,
                target_status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
                failure_code=CorrectedCurrentSyncFailureCode.HTTP_TRANSCRIPT_XML,
                failure_message="Transkribus transcript PAGE XML request failed.",
            )
        attempt.refresh_from_db()
        self.assertEqual(attempt.failure_code, original_code)
        self.assertEqual(attempt.failure_message, original_msg)

    def test_hover_eligible_follows_parsed_geometry_not_forced_true(self):
        pages_meta = [_trp_meta(1, ts_id="a"), _trp_meta(2, ts_id="b")]
        invalid_body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <TextEquiv><Unicode>No geometry</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        invalid_xml = _page_xml(invalid_body)

        def fetch_xml(url: str, *, bearer_token: str = "") -> bytes:
            return invalid_xml

        with patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes",
            return_value=None,
        ):
            with override_settings(UPLOADS_BUCKET_NAME="test-bucket"):
                result = self._run_sync(
                    fetch_pages_metadata=lambda *a, **k: pages_meta,
                    fetch_transcript_xml=fetch_xml,
                )
        assert result.snapshot is not None
        self.assertFalse(result.snapshot.hover_eligible)

    def test_storage_failure_marks_failed(self):
        pages_meta = [_trp_meta(1), _trp_meta(2)]

        def store(**kwargs):
            raise TranskribusSnapshotStorageUploadError("upload failed")

        with self.assertRaises(CorrectedCurrentSyncError) as ctx:
            self._run_sync(
                fetch_pages_metadata=lambda *a, **k: pages_meta,
                fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(url),
                store_snapshot=store,
            )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.SNAPSHOT_STORAGE,
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.latest("id")
        self.assertEqual(
            attempt.status, TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
        )
        self.assertEqual(
            attempt.failure_message,
            "Transkribus transcript snapshot storage failed.",
        )

    def test_terminal_transition_guard_conflict(self):
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.doc,
            transkribus_run=self.transkribus_run,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
            completed_at=timezone.now(),
        )
        with self.assertRaises(CorrectedCurrentSyncTerminalConflictError):
            _transition_attempt_terminal(
                attempt.pk,
                target_status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
                resolved_snapshot=TranskribusTranscriptSnapshot.objects.create(
                    document=self.doc,
                    transkribus_run=self.transkribus_run,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
                    parser_version=_TEST_PARSER_VERSION,
                    provider_identity_fingerprint=_sha256_hex("p"),
                    raw_xml_fingerprint=_sha256_hex("r"),
                    canonical_text="Hello",
                    canonical_text_sha256=_sha256_hex("Hello"),
                    geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
                    storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
                ),
                storage_outcome=SnapshotStorageOutcome.CREATED.value,
            )

    def test_each_invocation_creates_new_attempt(self):
        pages_meta = [_trp_meta(1, ts_id="a"), _trp_meta(2, ts_id="b")]
        snap = _ready_snap_with_pages(
            document=self.doc,
            run=self.transkribus_run,
            page_ts=[(1, 1, "a"), (2, 2, "b")],
        )
        kwargs = dict(
            fetch_pages_metadata=lambda *a, **k: pages_meta,
            fetch_transcript_xml=lambda url, *, bearer_token: self._fetch_xml(url),
            store_snapshot=lambda **kwargs: SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.CREATED, snapshot=snap
            ),
        )
        r1 = self._run_sync(**kwargs)
        r2 = self._run_sync(**kwargs)
        self.assertNotEqual(r1.attempt.pk, r2.attempt.pk)


class CorrectedCurrentSyncTransactionBoundaryTests(TransactionTestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()
        self._xml_counter = 0

    def _fetch_xml(self, url: str, *, bearer_token: str = "") -> bytes:
        self._xml_counter += 1
        return _page_xml(
            _SIMPLE_PAGE_BODY.replace("Hello", f"Page-{self._xml_counter}")
        )

    def _run_sync(self, **kwargs):
        defaults = dict(
            document_id=self.doc.pk,
            initiated_by=self.user,
            username="u",
            password="p",
            bearer_token="token",
            login=lambda *a, **k: None,
            session_factory=MagicMock,
        )
        defaults.update(kwargs)
        return run_corrected_current_transkribus_sync(**defaults)

    def test_http_steps_run_outside_atomic_blocks(self):
        pages_meta = [_trp_meta(1, ts_id="a"), _trp_meta(2, ts_id="b")]

        def login(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)

        def fetch_pages(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            return pages_meta

        def fetch_xml(url: str, *, bearer_token: str = "") -> bytes:
            self.assertFalse(connection.in_atomic_block)
            return self._fetch_xml(url, bearer_token=bearer_token)

        def store(**kwargs):
            self.assertFalse(connection.in_atomic_block)
            snap = _ready_snap_with_pages(
                document=self.doc,
                run=self.transkribus_run,
                page_ts=[(1, 1, "a"), (2, 2, "b")],
            )
            return SnapshotStorageResult(
                outcome=SnapshotStorageOutcome.CREATED, snapshot=snap
            )

        self._run_sync(
            login=login,
            fetch_pages_metadata=fetch_pages,
            fetch_transcript_xml=fetch_xml,
            store_snapshot=store,
        )


class CorrectedCurrentSyncRequestFencingTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_he_doc()
        self.transkribus_run = _upload_run(self.doc)
        self.user = _staff_user()
        self.lease_token = uuid.uuid4()
        now = timezone.now()
        self.sync_request = TranskribusCorrectedCurrentSyncRequest.objects.create(
            document=self.doc,
            initiated_by=self.user,
            status=TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
            lease_token=self.lease_token,
            lease_expires_at=now + timedelta(minutes=45),
            started_at=now,
        )

    def _run_request_backed(self, **kwargs):
        defaults = dict(
            document_id=self.doc.pk,
            initiated_by=self.user,
            username="u",
            password="p",
            bearer_token="token",
            sync_request_id=self.sync_request.pk,
            lease_token=self.lease_token,
            login=lambda *a, **k: None,
            session_factory=MagicMock,
            fetch_pages_metadata=lambda *a, **k: [_trp_meta(1), _trp_meta(2)],
            fetch_transcript_xml=lambda url, *, bearer_token: _page_xml(
                _SIMPLE_PAGE_BODY
            ),
        )
        defaults.update(kwargs)
        return run_corrected_current_transkribus_sync(**defaults)

    def test_request_backed_links_attempt_before_provider_io(self):
        login_calls: list[int] = []

        def login(*args, **kwargs):
            login_calls.append(1)
            self.sync_request.refresh_from_db()
            self.assertIsNotNone(self.sync_request.attempt_id)
            attempt = self.sync_request.attempt
            assert attempt is not None
            self.assertEqual(
                attempt.status,
                TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
            )

        with patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes",
            return_value=None,
        ):
            with override_settings(UPLOADS_BUCKET_NAME="test-bucket"):
                result = self._run_request_backed(login=login)

        self.assertEqual(len(login_calls), 1)
        self.sync_request.refresh_from_db()
        self.assertEqual(self.sync_request.attempt_id, result.attempt.pk)
        self.assertEqual(
            result.attempt.status,
            TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
        )

    def test_stale_lease_token_fenced_before_provider_io(self):
        login = MagicMock()
        fetch_pages = MagicMock(return_value=[_trp_meta(1)])
        fetch_xml = MagicMock(return_value=_page_xml(_SIMPLE_PAGE_BODY))
        store = MagicMock()

        with self.assertRaises(CorrectedCurrentSyncFencedOutError):
            self._run_request_backed(
                lease_token=uuid.uuid4(),
                login=login,
                fetch_pages_metadata=fetch_pages,
                fetch_transcript_xml=fetch_xml,
                store_snapshot=store,
            )

        login.assert_not_called()
        fetch_pages.assert_not_called()
        fetch_xml.assert_not_called()
        store.assert_not_called()
        self.sync_request.refresh_from_db()
        self.assertIsNone(self.sync_request.attempt_id)
        self.assertEqual(
            self.sync_request.status,
            TranskribusCorrectedCurrentSyncRequest.Status.RUNNING,
        )
        self.assertEqual(
            TranskribusCorrectedCurrentSyncAttempt.objects.filter(
                document=self.doc
            ).count(),
            0,
        )

    def test_partial_correlation_args_rejected_without_io(self):
        login = MagicMock()
        with self.assertRaises(CorrectedCurrentSyncError) as ctx:
            run_corrected_current_transkribus_sync(
                document_id=self.doc.pk,
                initiated_by=self.user,
                username="u",
                password="p",
                bearer_token="t",
                sync_request_id=self.sync_request.pk,
                login=login,
            )
        self.assertEqual(
            ctx.exception.failure_code,
            CorrectedCurrentSyncFailureCode.UNEXPECTED,
        )
        login.assert_not_called()
        self.sync_request.refresh_from_db()
        self.assertIsNone(self.sync_request.attempt_id)

    def test_management_path_without_request_still_creates_attempt(self):
        with patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes",
            return_value=None,
        ):
            with override_settings(UPLOADS_BUCKET_NAME="test-bucket"):
                result = run_corrected_current_transkribus_sync(
                    document_id=self.doc.pk,
                    initiated_by=self.user,
                    username="u",
                    password="p",
                    bearer_token="token",
                    login=lambda *a, **k: None,
                    session_factory=MagicMock,
                    fetch_pages_metadata=lambda *a, **k: [
                        _trp_meta(1),
                        _trp_meta(2),
                    ],
                    fetch_transcript_xml=lambda url, *, bearer_token: _page_xml(
                        _SIMPLE_PAGE_BODY
                    ),
                )
        self.assertIsNotNone(result.attempt.pk)
        self.sync_request.refresh_from_db()
        self.assertIsNone(self.sync_request.attempt_id)
