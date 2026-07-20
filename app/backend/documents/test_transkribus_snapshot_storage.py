"""Service-level tests for Transkribus snapshot PAGE XML persistence."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.s3 import (
    S3DeleteObjectResult,
    build_transkribus_snapshot_page_xml_s3_key,
    is_transkribus_snapshot_page_xml_s3_key,
)
from documents.services.archive_items import create_ocr_document
from documents.services.document_s3_orphan_cleanup import (
    DOCUMENT_S3_REFERENCE_FIELDS,
    TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS,
    S3ListedObject,
    collect_referenced_document_s3_keys,
    collect_referenced_transkribus_snapshot_page_xml_s3_keys,
)
from documents.services import transkribus_engine as tr
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION,
    SnapshotPageInput,
    compute_sha256_hex,
    parse_document_pages_for_snapshot,
)
from documents.services.transkribus_snapshot_storage import (
    SnapshotStorageOutcome,
    TranskribusSnapshotStorageUploadError,
    TranskribusSnapshotStorageValidationError,
    store_transkribus_transcript_snapshot,
)


def _page_xml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        f"{body}\n"
        "</PcGts>"
    ).encode("utf-8")


FULL_GEOMETRY_BODY = """
  <Page imageFilename="page-1.png" imageWidth="3000" imageHeight="4000">
    <ReadingOrder>
      <OrderedGroup id="ro1">
        <RegionRef regionRef="r1"/>
      </OrderedGroup>
    </ReadingOrder>
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,20 100,20 100,80 10,80"/>
        <Baseline points="10,75 100,75"/>
        <TextEquiv><Unicode>First line text</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="10,100 100,100 100,140 10,140"/>
        <Baseline points="10,135 100,135"/>
        <TextEquiv><Unicode>Second line</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""


def _geometry_body(
    *,
    line1: str = "First line text",
    line2: str = "Second line",
    coords1: str = "10,20 100,20 100,80 10,80",
) -> str:
    return f"""
  <Page imageFilename="page-1.png" imageWidth="3000" imageHeight="4000">
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="{coords1}"/>
        <Baseline points="10,75 100,75"/>
        <TextEquiv><Unicode>{line1}</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="10,100 100,100 100,140 10,140"/>
        <Baseline points="10,135 100,135"/>
        <TextEquiv><Unicode>{line2}</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class TranskribusSnapshotStorageTests(TestCase):
    STORAGE_MODULE = "documents.services.transkribus_snapshot_storage"

    def setUp(self):
        self.doc = create_ocr_document(
            title="Snapshot storage doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
        )
        self.put_calls: list[dict] = []
        self.delete_calls: list[str] = []

    def _create_run(self, document: Document | None = None) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=document or self.doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id="100",
            model_id="564149",
            remote_doc_id="999",
            recognition_job_id="job-1",
        )

    def _page_input(
        self,
        *,
        page_index: int = 1,
        page_nr: int = 1,
        transcript_ts_id: str = "7",
        body: str | None = None,
        page_xml: bytes | None = None,
        remote_transcript_status: str | None = "GT",
        provider_page_id: int | None = 42,
    ) -> SnapshotPageInput:
        xml = (
            page_xml if page_xml is not None else _page_xml(body or FULL_GEOMETRY_BODY)
        )
        return SnapshotPageInput(
            page_index=page_index,
            page_nr=page_nr,
            transcript_ts_id=transcript_ts_id,
            page_xml=xml,
            provider_page_id=provider_page_id,
            remote_transcript_status=remote_transcript_status,
        )

    def _mock_s3(self, *, fail_on_key: str | None = None, fail_cleanup: bool = False):
        def put_side_effect(*, bucket, key, body, content_type):
            self.put_calls.append(
                {
                    "bucket": bucket,
                    "key": key,
                    "body": body,
                    "content_type": content_type,
                }
            )
            if fail_on_key and key == fail_on_key:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "upload boom"}},
                    "PutObject",
                )
            return len(body)

        def delete_side_effect(bucket, key):
            self.delete_calls.append(key)
            if fail_cleanup:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "delete boom"}},
                    "DeleteObject",
                )
            return S3DeleteObjectResult(deleted=True)

        return (
            patch(
                f"{self.STORAGE_MODULE}.put_object_bytes", side_effect=put_side_effect
            ),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                side_effect=delete_side_effect,
            ),
        )

    def test_successful_one_page_snapshot_persistence(self):
        page = self._page_input()
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page],
                remote_doc_id="999",
                collection_id="100",
                model_id="564149",
                recognition_job_id="job-1",
            )

        self.assertEqual(result.outcome, SnapshotStorageOutcome.CREATED)
        self.assertFalse(result.reused)
        snap = result.snapshot
        self.assertEqual(
            snap.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        self.assertEqual(snap.parser_version, PARSER_VERSION)
        self.assertEqual(snap.pages.count(), 1)
        page_row = snap.pages.get()
        expected_key = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, snap.pk, 1
        )
        self.assertEqual(page_row.page_xml_s3_key, expected_key)
        self.assertEqual(len(self.put_calls), 1)
        self.assertEqual(self.put_calls[0]["key"], expected_key)
        self.assertEqual(self.put_calls[0]["content_type"], "application/xml")
        self.assertEqual(self.put_calls[0]["body"], page.page_xml)
        self.assertEqual(self.delete_calls, [])

    def test_successful_multi_page_persistence_and_line_offsets(self):
        page1 = self._page_input(
            page_index=1,
            page_nr=1,
            transcript_ts_id="10",
            body="""
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="a">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>AA</Unicode></TextEquiv>
    </TextLine>
  </Page>
""",
            remote_transcript_status=None,
            provider_page_id=None,
        )
        page2 = self._page_input(
            page_index=2,
            page_nr=2,
            transcript_ts_id="11",
            body="""
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="b">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>BB</Unicode></TextEquiv>
    </TextLine>
  </Page>
""",
            remote_transcript_status=None,
            provider_page_id=None,
        )
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page1, page2],
            )

        snap = result.snapshot
        self.assertEqual(snap.canonical_text, "AA\n\nBB")
        lines = list(
            TranskribusSnapshotLine.objects.filter(page__snapshot=snap).order_by(
                "page__page_index", "order_index"
            )
        )
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].char_start, 0)
        self.assertEqual(lines[0].char_end, 2)
        self.assertEqual(lines[1].char_start, 4)
        self.assertEqual(lines[1].char_end, 6)
        self.assertEqual(len(self.put_calls), 2)
        self.assertEqual(
            {call["key"] for call in self.put_calls},
            {
                build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, snap.pk, 1),
                build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, snap.pk, 2),
            },
        )

    def test_exact_mapping_of_parsed_geometry_and_metadata(self):
        page = self._page_input()
        parsed = parse_document_pages_for_snapshot([page])
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
                pages=[page],
                remote_doc_id="doc-9",
                collection_id="col-1",
                model_id="model-1",
                recognition_job_id="job-9",
            )

        snap = result.snapshot
        parsed_page = parsed.pages[0]
        page_row = snap.pages.get()
        self.assertEqual(snap.canonical_text, parsed.canonical_text)
        self.assertEqual(snap.canonical_text_sha256, parsed.canonical_text_sha256)
        self.assertEqual(
            snap.provider_identity_fingerprint, parsed.provider_identity_fingerprint
        )
        self.assertEqual(snap.raw_xml_fingerprint, parsed.raw_xml_fingerprint)
        self.assertEqual(snap.geometry_capability, parsed.geometry_capability)
        self.assertEqual(snap.hover_eligible, parsed.hover_eligible)
        self.assertEqual(snap.remote_doc_id, "doc-9")
        self.assertEqual(page_row.transcript_ts_id, parsed_page.transcript_ts_id)
        self.assertEqual(page_row.provider_page_id, parsed_page.provider_page_id)
        self.assertEqual(page_row.image_width, parsed_page.image_width)
        self.assertEqual(page_row.image_height, parsed_page.image_height)
        self.assertEqual(page_row.image_filename, parsed_page.image_filename)
        self.assertEqual(page_row.page_namespace, parsed_page.page_namespace)
        self.assertEqual(page_row.page_xml_sha256, parsed_page.page_xml_sha256)
        self.assertEqual(
            page_row.remote_transcript_status, parsed_page.remote_transcript_status
        )
        self.assertEqual(
            page_row.page_geometry_capability, parsed_page.page_geometry_capability
        )

        line_rows = list(page_row.lines.order_by("order_index"))
        self.assertEqual(len(line_rows), len(parsed_page.lines))
        for line_row, parsed_line in zip(line_rows, parsed_page.lines, strict=True):
            self.assertEqual(line_row.order_index, parsed_line.order_index)
            self.assertEqual(line_row.provider_line_id, parsed_line.provider_line_id)
            self.assertEqual(
                line_row.provider_region_id, parsed_line.provider_region_id
            )
            self.assertEqual(line_row.text, parsed_line.text)
            self.assertEqual(line_row.char_start, parsed_line.char_start)
            self.assertEqual(line_row.char_end, parsed_line.char_end)
            self.assertEqual(
                line_row.polygon_points,
                [[float(x), float(y)] for x, y in parsed_line.polygon_points],
            )
            self.assertEqual(
                line_row.baseline_points,
                [[float(x), float(y)] for x, y in parsed_line.baseline_points],
            )
            self.assertEqual(line_row.coords_valid, parsed_line.coords_valid)
            self.assertEqual(line_row.baseline_valid, parsed_line.baseline_valid)
            if parsed_line.bbox is None:
                self.assertIsNone(line_row.bbox_min_x)
            else:
                self.assertEqual(line_row.bbox_min_x, parsed_line.bbox.min_x)
                self.assertEqual(line_row.bbox_min_y, parsed_line.bbox.min_y)
                self.assertEqual(line_row.bbox_max_x, parsed_line.bbox.max_x)
                self.assertEqual(line_row.bbox_max_y, parsed_line.bbox.max_y)

    def test_existing_ready_snapshot_returns_noop_without_s3_upload(self):
        page = self._page_input()
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            first = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page],
            )
            put_count_after_first = len(self.put_calls)
            second = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
                pages=[page],
            )

        self.assertEqual(first.outcome, SnapshotStorageOutcome.CREATED)
        self.assertEqual(second.outcome, SnapshotStorageOutcome.REUSED_EXISTING)
        self.assertEqual(second.snapshot.pk, first.snapshot.pk)
        self.assertEqual(len(self.put_calls), put_count_after_first)
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document=self.doc).count(),
            1,
        )

    def test_same_provider_identity_changed_page_xml_creates_new_snapshot(self):
        page_a = self._page_input(
            transcript_ts_id="7",
            body=_geometry_body(line1="First line text", line2="Second line"),
        )
        page_b = self._page_input(
            transcript_ts_id="7",
            body=_geometry_body(line1="First line text", line2="Changed second"),
        )
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            first = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page_a],
            )
            second = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page_b],
            )

        self.assertNotEqual(first.snapshot.pk, second.snapshot.pk)
        self.assertEqual(
            first.snapshot.provider_identity_fingerprint,
            second.snapshot.provider_identity_fingerprint,
        )
        self.assertNotEqual(
            first.snapshot.raw_xml_fingerprint,
            second.snapshot.raw_xml_fingerprint,
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(
                document=self.doc,
                storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            ).count(),
            2,
        )

    def test_same_canonical_text_changed_geometry_creates_new_snapshot(self):
        page_a = self._page_input(
            body=_geometry_body(coords1="10,20 100,20 100,80 10,80")
        )
        page_b = self._page_input(
            body=_geometry_body(coords1="20,30 110,30 110,90 20,90")
        )
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            first = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page_a],
            )
            second = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page_b],
            )

        self.assertEqual(first.snapshot.canonical_text, second.snapshot.canonical_text)
        self.assertEqual(
            first.snapshot.canonical_text_sha256,
            second.snapshot.canonical_text_sha256,
        )
        self.assertNotEqual(
            first.snapshot.raw_xml_fingerprint,
            second.snapshot.raw_xml_fingerprint,
        )
        self.assertNotEqual(first.snapshot.pk, second.snapshot.pk)

    def test_partial_s3_failure_marks_failed_and_deletes_uploaded_objects(self):
        page1 = self._page_input(page_index=1, page_nr=1, transcript_ts_id="1")
        page2 = self._page_input(
            page_index=2,
            page_nr=2,
            transcript_ts_id="2",
            body="""
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="b">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Beta</Unicode></TextEquiv>
    </TextLine>
  </Page>
""",
        )

        # Fail after the first page upload succeeds.
        def put_then_fail(*, bucket, key, body, content_type):
            self.put_calls.append(
                {
                    "bucket": bucket,
                    "key": key,
                    "body": body,
                    "content_type": content_type,
                }
            )
            if len(self.put_calls) >= 2:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "upload boom"}},
                    "PutObject",
                )
            return len(body)

        with (
            patch(f"{self.STORAGE_MODULE}.put_object_bytes", side_effect=put_then_fail),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                side_effect=lambda bucket, key: (
                    self.delete_calls.append(key) or S3DeleteObjectResult(deleted=True)
                ),
            ),
        ):
            with self.assertRaises(TranskribusSnapshotStorageUploadError) as ctx:
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[page1, page2],
                )

        snap = TranskribusTranscriptSnapshot.objects.get(pk=ctx.exception.snapshot_id)
        self.assertEqual(
            snap.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.FAILED,
        )
        self.assertEqual(len(self.put_calls), 2)
        successful_key = self.put_calls[0]["key"]
        failed_key = self.put_calls[1]["key"]
        # Regression: caller must retain the successful key even when a later
        # upload raises before _upload_page_xml_objects returns.
        self.assertEqual(self.delete_calls, [successful_key])
        self.assertNotIn(failed_key, self.delete_calls)
        self.assertNotEqual(
            snap.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        self.assertIn("Failed to upload PAGE XML", str(ctx.exception))

    def test_cleanup_failure_does_not_hide_original_upload_failure(self):
        page1 = self._page_input(page_index=1, page_nr=1, transcript_ts_id="1")
        page2 = self._page_input(
            page_index=2,
            page_nr=2,
            transcript_ts_id="2",
            body="""
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="b">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Beta</Unicode></TextEquiv>
    </TextLine>
  </Page>
""",
        )

        def put_then_fail(*, bucket, key, body, content_type):
            self.put_calls.append({"key": key})
            if len(self.put_calls) >= 2:
                raise ClientError(
                    {"Error": {"Code": "InternalError", "Message": "upload boom"}},
                    "PutObject",
                )
            return len(body)

        with (
            patch(f"{self.STORAGE_MODULE}.put_object_bytes", side_effect=put_then_fail),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                side_effect=ClientError(
                    {"Error": {"Code": "InternalError", "Message": "delete boom"}},
                    "DeleteObject",
                ),
            ),
        ):
            with self.assertRaises(TranskribusSnapshotStorageUploadError) as ctx:
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[page1, page2],
                )

        exc = ctx.exception
        successful_key = self.put_calls[0]["key"]
        failed_key = self.put_calls[1]["key"]
        self.assertIn("Failed to upload PAGE XML", str(exc))
        self.assertNotIn("delete boom", str(exc))
        # Must attempt cleanup of the successful key (would be skipped if the
        # uploaded_keys accumulator were empty after a mid-batch raise).
        self.assertEqual(len(exc.cleanup_errors), 1)
        self.assertIn(successful_key, exc.cleanup_errors[0])
        self.assertNotIn(failed_key, exc.cleanup_errors[0])

    def test_failed_state_update_does_not_replace_primary_upload_error(self):
        page = self._page_input()

        def fail_put(*, bucket, key, body, content_type):
            self.put_calls.append({"key": key})
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "upload boom"}},
                "PutObject",
            )

        with (
            patch(f"{self.STORAGE_MODULE}.put_object_bytes", side_effect=fail_put),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ),
            patch(
                f"{self.STORAGE_MODULE}._best_effort_mark_snapshot_failed",
                return_value="failed_state_update category=DatabaseError",
            ) as mock_mark,
        ):
            with self.assertRaises(TranskribusSnapshotStorageUploadError) as ctx:
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[page],
                )

        exc = ctx.exception
        self.assertIn("Failed to upload PAGE XML", str(exc))
        self.assertEqual(
            exc.state_update_errors,
            ("failed_state_update category=DatabaseError",),
        )
        mock_mark.assert_called_once()
        # Snapshot may still be PENDING if mark was mocked; never report READY.
        snap = TranskribusTranscriptSnapshot.objects.get(pk=exc.snapshot_id)
        self.assertNotEqual(
            snap.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )

    def test_failed_state_update_db_error_is_swallowed_by_helper(self):
        from documents.services.transkribus_snapshot_storage import (
            _best_effort_mark_snapshot_failed,
        )

        mock_qs = patch(
            f"{self.STORAGE_MODULE}.TranskribusTranscriptSnapshot.objects.filter"
        )
        with mock_qs as mock_filter:
            mock_filter.return_value.update.side_effect = DatabaseError(
                "connection lost"
            )
            err = _best_effort_mark_snapshot_failed(12345)

        self.assertEqual(err, "failed_state_update category=DatabaseError")

    def test_string_and_bool_page_indexes_rejected_before_upload(self):
        put_mock, delete_mock = self._mock_s3()
        xml = _page_xml(FULL_GEOMETRY_BODY)
        with put_mock, delete_mock:
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        SnapshotPageInput(
                            page_index="1",  # type: ignore[arg-type]
                            page_nr=1,
                            transcript_ts_id="7",
                            page_xml=xml,
                        )
                    ],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        SnapshotPageInput(
                            page_index=1,
                            page_nr="1",  # type: ignore[arg-type]
                            transcript_ts_id="7",
                            page_xml=xml,
                        )
                    ],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        SnapshotPageInput(
                            page_index=True,  # type: ignore[arg-type]
                            page_nr=1,
                            transcript_ts_id="7",
                            page_xml=xml,
                        )
                    ],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        SnapshotPageInput(
                            page_index=1,
                            page_nr=True,  # type: ignore[arg-type]
                            transcript_ts_id="7",
                            page_xml=xml,
                        )
                    ],
                )
        self.assertEqual(self.put_calls, [])
        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(document=self.doc).exists()
        )

    def test_failed_attempt_does_not_block_retry(self):
        page = self._page_input()
        put_mock, delete_mock = self._mock_s3()

        def fail_put(*, bucket, key, body, content_type):
            self.put_calls.append({"key": key})
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "upload boom"}},
                "PutObject",
            )

        with (
            patch(f"{self.STORAGE_MODULE}.put_object_bytes", side_effect=fail_put),
            delete_mock,
        ):
            with self.assertRaises(TranskribusSnapshotStorageUploadError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[page],
                )

        failed = TranskribusTranscriptSnapshot.objects.get(document=self.doc)
        self.assertEqual(
            failed.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.FAILED,
        )

        with put_mock, delete_mock:
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page],
            )

        self.assertEqual(result.outcome, SnapshotStorageOutcome.CREATED)
        self.assertEqual(
            result.snapshot.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        self.assertNotEqual(result.snapshot.pk, failed.pk)

    def test_snapshot_never_ready_before_all_uploads_succeed(self):
        page = self._page_input()
        statuses_during_upload: list[str] = []

        def put_and_observe(*, bucket, key, body, content_type):
            snap = TranskribusTranscriptSnapshot.objects.get(document=self.doc)
            statuses_during_upload.append(snap.storage_status)
            self.put_calls.append({"key": key})
            return len(body)

        with (
            patch(
                f"{self.STORAGE_MODULE}.put_object_bytes",
                side_effect=put_and_observe,
            ),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                return_value=S3DeleteObjectResult(deleted=True),
            ),
        ):
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page],
            )

        self.assertEqual(
            statuses_during_upload,
            [TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD],
        )
        self.assertEqual(
            result.snapshot.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )

    def test_concurrent_identical_finalization_reuses_winner_safely(self):
        page = self._page_input()
        parsed = parse_document_pages_for_snapshot([page])
        pending_result_holder: dict = {}

        def upload_then_insert_winner(*, bucket, key, body, content_type):
            self.put_calls.append({"key": key, "body": body})
            # Simulate another attempt winning READY uniqueness after uploads.
            if not pending_result_holder.get("winner_created"):
                winner = TranskribusTranscriptSnapshot.objects.create(
                    document=self.doc,
                    source_kind=(
                        TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC
                    ),
                    parser_version=parsed.parser_version,
                    provider_identity_fingerprint=(
                        parsed.provider_identity_fingerprint
                    ),
                    raw_xml_fingerprint=parsed.raw_xml_fingerprint,
                    canonical_text=parsed.canonical_text,
                    canonical_text_sha256=parsed.canonical_text_sha256,
                    geometry_capability=parsed.geometry_capability,
                    hover_eligible=parsed.hover_eligible,
                    storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
                )
                TranskribusSnapshotPage.objects.create(
                    snapshot=winner,
                    page_index=1,
                    page_nr=1,
                    transcript_ts_id="7",
                    page_xml_s3_key=build_transkribus_snapshot_page_xml_s3_key(
                        self.doc.pk, winner.pk, 1
                    ),
                    page_xml_sha256=parsed.pages[0].page_xml_sha256,
                )
                pending_result_holder["winner"] = winner
                pending_result_holder["winner_created"] = True
            return len(body)

        with (
            patch(
                f"{self.STORAGE_MODULE}.put_object_bytes",
                side_effect=upload_then_insert_winner,
            ),
            patch(
                f"{self.STORAGE_MODULE}.delete_s3_object",
                side_effect=lambda bucket, key: (
                    self.delete_calls.append(key) or S3DeleteObjectResult(deleted=True)
                ),
            ),
        ):
            result = store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[page],
            )

        self.assertEqual(
            result.outcome, SnapshotStorageOutcome.REUSED_CONCURRENT_WINNER
        )
        self.assertEqual(result.snapshot.pk, pending_result_holder["winner"].pk)
        losing = (
            TranskribusTranscriptSnapshot.objects.exclude(pk=result.snapshot.pk)
            .filter(document=self.doc)
            .get()
        )
        self.assertEqual(
            losing.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.FAILED,
        )
        self.assertTrue(self.delete_calls)
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(
                document=self.doc,
                storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            ).count(),
            1,
        )

    def test_cross_document_transkribus_run_rejected_before_upload(self):
        other = create_ocr_document(
            title="Other doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
        )
        other_run = self._create_run(other)
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[self._page_input()],
                    transkribus_run=other_run,
                )
        self.assertEqual(self.put_calls, [])
        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(document=self.doc).exists()
        )

    def test_invalid_and_duplicate_page_mapping_rejected_before_upload(self):
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        self._page_input(page_index=1, page_nr=1, transcript_ts_id="1"),
                        self._page_input(page_index=1, page_nr=2, transcript_ts_id="2"),
                    ],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        self._page_input(page_index=1, page_nr=5, transcript_ts_id="1"),
                        self._page_input(page_index=2, page_nr=5, transcript_ts_id="2"),
                    ],
                )
            with self.assertRaises(TranskribusSnapshotStorageValidationError):
                store_transkribus_transcript_snapshot(
                    document=self.doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    pages=[
                        self._page_input(page_index=0, page_nr=1, transcript_ts_id="1"),
                    ],
                )
        self.assertEqual(self.put_calls, [])
        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(document=self.doc).exists()
        )

    def test_no_document_text_result_or_binding_created_or_modified(self):
        existing_result = DocumentTextResult.objects.create(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="preexisting",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            text="preexisting",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        before_count = DocumentTextResult.objects.filter(document=self.doc).count()
        put_mock, delete_mock = self._mock_s3()
        with put_mock, delete_mock:
            store_transkribus_transcript_snapshot(
                document=self.doc,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                pages=[self._page_input()],
            )

        self.assertEqual(
            DocumentTextResult.objects.filter(document=self.doc).count(),
            before_count,
        )
        existing_result.refresh_from_db()
        self.assertEqual(existing_result.text, "preexisting")
        self.assertFalse(TranskribusTextResultBinding.objects.exists())


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class TranskribusSnapshotOrphanCleanupIntegrationTests(TestCase):
    SERVICE_MODULE = "documents.services.document_s3_orphan_cleanup"

    def setUp(self):
        self.now = timezone.now()
        self.stale_modified = self.now - timedelta(hours=30)
        self.doc = create_ocr_document(
            title="Snapshot orphan doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            upload_status=Document.UploadStatus.UPLOADED,
        )

    def _listed(self, key: str) -> S3ListedObject:
        return S3ListedObject(key=key, last_modified=self.stale_modified, size=128)

    def _create_snapshot(
        self,
        *,
        status: str,
        page_key: str,
    ) -> TranskribusTranscriptSnapshot:
        snap = TranskribusTranscriptSnapshot.objects.create(
            document=self.doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version=PARSER_VERSION,
            provider_identity_fingerprint="p" * 64,
            raw_xml_fingerprint=("r" + status[:1]) * 32,
            canonical_text="Hello",
            canonical_text_sha256=compute_sha256_hex("Hello"),
            storage_status=status,
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=1,
            transcript_ts_id="9",
            page_xml_s3_key=page_key,
            page_xml_sha256="a" * 64,
        )
        return snap

    def test_ready_and_recent_pending_keys_protected_from_orphan_cleanup(self):
        ready = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            page_key="tmp",
        )
        pending = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
            page_key="tmp2",
        )
        ready_key = build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, ready.pk, 1)
        pending_key = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, pending.pk, 1
        )
        ready.pages.update(page_xml_s3_key=ready_key)
        pending.pages.update(page_xml_s3_key=pending_key)

        referenced = collect_referenced_document_s3_keys(now=self.now)
        self.assertIn(ready_key, referenced)
        self.assertIn(pending_key, referenced)
        self.assertTrue(is_transkribus_snapshot_page_xml_s3_key(ready_key))

        objects = [
            self._listed(ready_key),
            self._listed(pending_key),
            self._listed(f"documents/{self.doc.pk}/orphan.jpg"),
        ]
        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            return_value=objects,
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )
        output = stdout.getvalue()
        self.assertNotIn(ready_key, output)
        self.assertNotIn(pending_key, output)
        self.assertIn(f"documents/{self.doc.pk}/orphan.jpg", output)

    def test_recent_pending_protected_stale_pending_not_protected(self):
        self.assertEqual(TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS, 24)

        recent = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
            page_key="tmp-recent",
        )
        stale = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
            page_key="tmp-stale",
        )
        recent_key = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, recent.pk, 1
        )
        stale_key = build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, stale.pk, 1)
        recent.pages.update(page_xml_s3_key=recent_key)
        stale.pages.update(page_xml_s3_key=stale_key)

        TranskribusTranscriptSnapshot.objects.filter(pk=stale.pk).update(
            created_at=self.now
            - timedelta(hours=TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS + 1)
        )
        # Keep recent inside the protection window.
        TranskribusTranscriptSnapshot.objects.filter(pk=recent.pk).update(
            created_at=self.now - timedelta(hours=1)
        )

        protected = collect_referenced_transkribus_snapshot_page_xml_s3_keys(
            now=self.now
        )
        self.assertIn(recent_key, protected)
        self.assertNotIn(stale_key, protected)
        stale.refresh_from_db()
        self.assertEqual(
            stale.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
        )

        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            return_value=[self._listed(stale_key), self._listed(recent_key)],
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )
        output = stdout.getvalue()
        self.assertIn(stale_key, output)
        self.assertNotIn(recent_key, output)

    def test_exact_key_identity_required_for_orphan_protection(self):
        snap = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            page_key="tmp",
        )
        exact_key = build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, snap.pk, 1)
        snap.pages.update(page_xml_s3_key=exact_key)
        self.assertIn(
            exact_key,
            collect_referenced_transkribus_snapshot_page_xml_s3_keys(now=self.now),
        )

        mismatched_document = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk + 999, snap.pk, 1
        )
        snap.pages.update(page_xml_s3_key=mismatched_document)
        self.assertNotIn(
            mismatched_document,
            collect_referenced_transkribus_snapshot_page_xml_s3_keys(now=self.now),
        )

        mismatched_snapshot = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, snap.pk + 999, 1
        )
        snap.pages.update(page_xml_s3_key=mismatched_snapshot)
        self.assertNotIn(
            mismatched_snapshot,
            collect_referenced_transkribus_snapshot_page_xml_s3_keys(now=self.now),
        )

        mismatched_page = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, snap.pk, 2
        )
        snap.pages.update(page_xml_s3_key=mismatched_page)
        self.assertNotIn(
            mismatched_page,
            collect_referenced_transkribus_snapshot_page_xml_s3_keys(now=self.now),
        )

    def test_failed_residual_keys_are_orphan_candidates_under_age_rules(self):
        failed = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.FAILED,
            page_key="tmp",
        )
        failed_key = build_transkribus_snapshot_page_xml_s3_key(
            self.doc.pk, failed.pk, 1
        )
        failed.pages.update(page_xml_s3_key=failed_key)

        protected = collect_referenced_transkribus_snapshot_page_xml_s3_keys(
            now=self.now
        )
        self.assertNotIn(failed_key, protected)

        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            return_value=[self._listed(failed_key)],
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )
        self.assertIn(failed_key, stdout.getvalue())

    def test_unrelated_documents_and_photos_keys_outside_new_scope(self):
        # Non-matching documents/ key stored on a READY page must not be protected.
        snap = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            page_key=f"documents/{self.doc.pk}/transkribus/other/not-page.xml",
        )
        protected = collect_referenced_transkribus_snapshot_page_xml_s3_keys(
            now=self.now
        )
        self.assertNotIn(snap.pages.get().page_xml_s3_key, protected)

        # photos/ remains outside the document orphan command prefix scope.
        with self.assertRaises(CommandError):
            call_command(
                "cleanup_document_s3_orphans",
                "--prefix=photos/1/",
                stdout=StringIO(),
            )

        # Classic reference fields constant unchanged.
        self.assertEqual(
            DOCUMENT_S3_REFERENCE_FIELDS,
            (
                ("Document", "file_s3_key"),
                ("Document", "thumbnail_file_key"),
                ("DocumentSourceFile", "file_s3_key"),
            ),
        )

    def test_document_deletion_leaves_snapshot_keys_for_orphan_cleanup(self):
        """Match existing OCR document convention: DB cascade, S3 via orphans."""
        snap = self._create_snapshot(
            status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            page_key="tmp",
        )
        key = build_transkribus_snapshot_page_xml_s3_key(self.doc.pk, snap.pk, 1)
        snap.pages.update(page_xml_s3_key=key)
        self.assertIn(key, collect_referenced_document_s3_keys(now=self.now))

        self.doc.delete()

        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(pk=snap.pk).exists()
        )
        self.assertNotIn(key, collect_referenced_document_s3_keys(now=self.now))

        stdout = StringIO()
        with patch(
            f"{self.SERVICE_MODULE}.list_s3_objects_under_prefix",
            return_value=[self._listed(key)],
        ):
            call_command(
                "cleanup_document_s3_orphans",
                "--older-than-hours=24",
                stdout=stdout,
            )
        self.assertIn(key, stdout.getvalue())


class TranskribusSnapshotPageXmlS3KeyTests(SimpleTestCase):
    def test_builder_rejects_zero_ids(self):
        with self.assertRaises(ValueError):
            build_transkribus_snapshot_page_xml_s3_key(0, 1, 1)
        with self.assertRaises(ValueError):
            build_transkribus_snapshot_page_xml_s3_key(1, 0, 1)
        with self.assertRaises(ValueError):
            build_transkribus_snapshot_page_xml_s3_key(1, 1, 0)

    def test_matcher_accepts_positive_ids_and_rejects_zero(self):
        self.assertTrue(
            is_transkribus_snapshot_page_xml_s3_key(
                "documents/12/transkribus/snapshots/34/pages/5.page.xml"
            )
        )
        self.assertFalse(
            is_transkribus_snapshot_page_xml_s3_key(
                "documents/0/transkribus/snapshots/34/pages/5.page.xml"
            )
        )
        self.assertFalse(
            is_transkribus_snapshot_page_xml_s3_key(
                "documents/12/transkribus/snapshots/0/pages/5.page.xml"
            )
        )
        self.assertFalse(
            is_transkribus_snapshot_page_xml_s3_key(
                "documents/12/transkribus/snapshots/34/pages/0.page.xml"
            )
        )
        self.assertFalse(
            is_transkribus_snapshot_page_xml_s3_key(
                "documents/12/transkribus/snapshots/34/pages/5.xml"
            )
        )
