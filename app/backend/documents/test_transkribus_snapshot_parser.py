"""Tests for Transkribus snapshot schema and pure PAGE XML parser (PR1)."""

from __future__ import annotations

from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services import transkribus_engine as tr
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION,
    SnapshotPageInput,
    compute_provider_identity_fingerprint,
    compute_raw_xml_fingerprint,
    compute_sha256_hex,
    parse_document_pages_for_snapshot,
    parse_page_xml_for_snapshot,
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


class TranskribusSnapshotParserParityTests(SimpleTestCase):
    def test_single_page_canonical_text_matches_production(self):
        xml = _page_xml(FULL_GEOMETRY_BODY)
        parsed = parse_page_xml_for_snapshot(
            xml,
            page_index=1,
            page_nr=1,
            transcript_ts_id="7",
        )
        self.assertEqual(parsed.canonical_text, tr.parse_page_xml_to_text(xml))
        self.assertEqual(parsed.canonical_text, "First line text\nSecond line")

    def test_multi_page_joins_with_double_newline(self):
        page1 = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="a">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Alpha</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        page2 = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="b">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Beta</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="c">
      <Coords points="1,3 2,3 2,4 1,4"/>
      <TextEquiv><Unicode>Gamma</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        doc = parse_document_pages_for_snapshot(
            [
                SnapshotPageInput(1, 1, "10", page1),
                SnapshotPageInput(2, 2, "11", page2),
            ]
        )
        expected = (
            tr.parse_page_xml_to_text(page1) + "\n\n" + tr.parse_page_xml_to_text(page2)
        )
        self.assertEqual(doc.canonical_text, expected)
        self.assertEqual(doc.canonical_text, "Alpha\n\nBeta\nGamma")
        self.assertEqual(doc.parser_version, PARSER_VERSION)

    def test_exact_newline_joining_parity(self):
        xml = _page_xml(
            """
  <Page imageWidth="10" imageHeight="10">
    <TextLine id="l1"><TextEquiv><Unicode>one</Unicode></TextEquiv></TextLine>
    <TextLine id="l2"><TextEquiv><Unicode>two</Unicode></TextEquiv></TextLine>
    <TextLine id="l3"><TextEquiv><Unicode>three</Unicode></TextEquiv></TextLine>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        self.assertEqual(parsed.canonical_text, "one\ntwo\nthree")
        self.assertEqual(parsed.canonical_text, tr.parse_page_xml_to_text(xml))

    def test_deterministic_offsets_including_empty_geometry_line(self):
        xml = _page_xml(
            """
  <Page imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,10 50,10 50,50 10,50"/>
        <TextEquiv><Unicode>Hello</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l_empty">
        <Coords points="10,60 50,60 50,90 10,90"/>
        <Baseline points="10,85 50,85"/>
        <TextEquiv><Unicode></Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="10,100 50,100 50,140 10,140"/>
        <TextEquiv><Unicode>World</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="9"
        )
        self.assertEqual(parsed.canonical_text, "Hello\nWorld")
        self.assertEqual(parsed.canonical_text, tr.parse_page_xml_to_text(xml))
        self.assertEqual(len(parsed.lines), 3)

        hello, empty, world = parsed.lines
        self.assertEqual(hello.text, "Hello")
        self.assertEqual(hello.char_start, 0)
        self.assertEqual(hello.char_end, 5)
        self.assertTrue(hello.contributes_to_canonical)

        self.assertEqual(empty.text, "")
        self.assertFalse(empty.contributes_to_canonical)
        self.assertTrue(empty.has_meaningful_geometry)
        self.assertEqual(empty.char_start, empty.char_end)
        self.assertEqual(empty.char_start, 5)

        self.assertEqual(world.text, "World")
        self.assertEqual(world.char_start, 6)
        self.assertEqual(world.char_end, 11)
        self.assertEqual(
            parsed.canonical_text[world.char_start : world.char_end], "World"
        )

    def test_multi_page_offsets_account_for_page_separator(self):
        page1 = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="a">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>AA</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        page2 = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="b">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>BB</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        doc = parse_document_pages_for_snapshot(
            [
                SnapshotPageInput(1, 1, "1", page1),
                SnapshotPageInput(2, 2, "2", page2),
            ]
        )
        self.assertEqual(doc.canonical_text, "AA\n\nBB")
        line_b = doc.pages[1].lines[0]
        self.assertEqual(line_b.char_start, 4)
        self.assertEqual(line_b.char_end, 6)
        self.assertEqual(doc.canonical_text[line_b.char_start : line_b.char_end], "BB")


class TranskribusSnapshotParserGeometryTests(SimpleTestCase):
    def test_valid_polygon_baseline_and_bbox(self):
        parsed = parse_page_xml_for_snapshot(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
            transcript_ts_id="7",
        )
        line = parsed.lines[0]
        self.assertTrue(line.coords_valid)
        self.assertTrue(line.baseline_valid)
        assert line.bbox is not None
        self.assertEqual(line.bbox.min_x, 10.0)
        self.assertEqual(line.bbox.min_y, 20.0)
        self.assertEqual(line.bbox.max_x, 100.0)
        self.assertEqual(line.bbox.max_y, 80.0)
        self.assertEqual(parsed.page_geometry_capability, "VERIFIED")

    def test_malformed_and_degenerate_polygons(self):
        xml = _page_xml(
            """
  <Page imageWidth="1000" imageHeight="1000">
    <TextLine id="bad">
      <Coords points="not-a-point"/>
      <TextEquiv><Unicode>Bad</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="degen">
      <Coords points="1,1 1,1 1,1"/>
      <TextEquiv><Unicode>Degen</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        bad, degen = parsed.lines
        self.assertFalse(bad.coords_valid)
        self.assertEqual(bad.polygon_points, ())
        self.assertFalse(degen.coords_valid)
        self.assertEqual(len(degen.polygon_points), 3)

    def test_negative_and_out_of_bounds_geometry(self):
        xml = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="neg">
      <Coords points="-1,10 10,10 10,20 -1,20"/>
      <TextEquiv><Unicode>Neg</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="oob">
      <Coords points="10,10 200,10 200,20 10,20"/>
      <TextEquiv><Unicode>Oob</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        self.assertFalse(parsed.lines[0].coords_valid)
        self.assertFalse(parsed.lines[1].coords_valid)
        self.assertEqual(parsed.page_geometry_capability, "PARTIAL")

    def test_negative_coords_invalid_without_page_dimensions(self):
        xml = _page_xml(
            """
  <Page>
    <TextLine id="neg">
      <Coords points="-1,10 10,10 10,20 -1,20"/>
      <Baseline points="0,18 10,18"/>
      <TextEquiv><Unicode>Neg</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        page = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        self.assertIsNone(page.image_width)
        self.assertIsNone(page.image_height)
        line = page.lines[0]
        self.assertFalse(line.coords_valid)
        self.assertIsNone(line.bbox)
        self.assertNotEqual(page.page_geometry_capability, "VERIFIED")

        doc = parse_document_pages_for_snapshot([SnapshotPageInput(1, 1, "1", xml)])
        self.assertFalse(doc.hover_eligible)
        self.assertNotEqual(doc.geometry_capability, "VERIFIED")
        self.assertFalse(doc.pages[0].lines[0].coords_valid)
        self.assertIsNone(doc.pages[0].lines[0].bbox)

    def test_duplicate_and_missing_provider_line_ids(self):
        xml = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="dup">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>One</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="dup">
      <Coords points="1,3 2,3 2,4 1,4"/>
      <TextEquiv><Unicode>Two</Unicode></TextEquiv>
    </TextLine>
    <TextLine>
      <Coords points="1,5 2,5 2,6 1,6"/>
      <TextEquiv><Unicode>Three</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        self.assertEqual(parsed.duplicate_line_ids, 1)
        self.assertIsNone(parsed.lines[2].provider_line_id)

    def test_reading_order_divergence_reported_without_reordering(self):
        xml = _page_xml(
            """
  <Page imageWidth="1000" imageHeight="1000">
    <ReadingOrder>
      <OrderedGroup id="ro1">
        <RegionRef regionRef="l2"/>
        <RegionRef regionRef="l1"/>
      </OrderedGroup>
    </ReadingOrder>
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,10 50,10 50,50 10,50"/>
        <TextEquiv><Unicode>First</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="10,60 50,60 50,90 10,90"/>
        <TextEquiv><Unicode>Second</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=1, page_nr=1, transcript_ts_id="1"
        )
        self.assertEqual(parsed.canonical_text, "First\nSecond")
        self.assertGreater(parsed.lines_xml_order_differs_from_reading_order, 0)
        self.assertTrue(
            any("ReadingOrder diverges" in warning for warning in parsed.warnings)
        )
        self.assertEqual(
            [line.provider_line_id for line in parsed.lines],
            ["l1", "l2"],
        )

    def test_region_id_captured(self):
        parsed = parse_page_xml_for_snapshot(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
            transcript_ts_id="7",
        )
        self.assertEqual(parsed.lines[0].provider_region_id, "r1")


class TranskribusSnapshotFingerprintTests(SimpleTestCase):
    def test_fingerprint_stability(self):
        page1 = _page_xml(FULL_GEOMETRY_BODY)
        page2 = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="x">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>X</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        inputs = [
            SnapshotPageInput(1, 1, "100", page1),
            SnapshotPageInput(2, 2, "200", page2),
        ]
        a = parse_document_pages_for_snapshot(inputs)
        b = parse_document_pages_for_snapshot(inputs)
        self.assertEqual(
            a.provider_identity_fingerprint, b.provider_identity_fingerprint
        )
        self.assertEqual(a.raw_xml_fingerprint, b.raw_xml_fingerprint)
        self.assertEqual(a.canonical_text_sha256, b.canonical_text_sha256)
        self.assertEqual(
            a.provider_identity_fingerprint,
            compute_provider_identity_fingerprint([(1, "100"), (2, "200")]),
        )
        self.assertEqual(
            a.raw_xml_fingerprint,
            compute_raw_xml_fingerprint(
                [compute_sha256_hex(page1), compute_sha256_hex(page2)]
            ),
        )

    def test_same_provider_identity_different_xml_different_raw_fingerprint(self):
        xml_a = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>A</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        xml_b = _page_xml(
            """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>B</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        doc_a = parse_document_pages_for_snapshot(
            [SnapshotPageInput(1, 1, "ts-same", xml_a)]
        )
        doc_b = parse_document_pages_for_snapshot(
            [SnapshotPageInput(1, 1, "ts-same", xml_b)]
        )
        self.assertEqual(
            doc_a.provider_identity_fingerprint, doc_b.provider_identity_fingerprint
        )
        self.assertNotEqual(doc_a.raw_xml_fingerprint, doc_b.raw_xml_fingerprint)
        self.assertNotEqual(doc_a.canonical_text_sha256, doc_b.canonical_text_sha256)

    def test_pure_parser_performs_no_database_writes(self):
        xml = _page_xml(FULL_GEOMETRY_BODY)
        with patch("django.db.models.Model.save") as mock_save:
            parse_document_pages_for_snapshot([SnapshotPageInput(1, 1, "7", xml)])
        mock_save.assert_not_called()


@override_settings(UPLOADS_BUCKET_NAME="")
class TranskribusSnapshotModelTests(TestCase):
    def _create_doc(self, *, title: str = "Snapshot schema doc") -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PRIVATE,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/snapshot/source/0.jpg",
            mime_type="image/jpeg",
        )

    def _create_text_result(
        self, doc: Document, *, engine: str = "tr-1"
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="Hello\nWorld",
            source_revision=1,
        )

    def _create_run(self, doc: Document) -> TranskribusRun:
        return TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col-1",
            model_id="42",
            remote_doc_id="999",
            recognition_job_id="job-1",
        )

    def _create_ready_snapshot(
        self,
        doc: Document,
        *,
        raw_xml_fingerprint: str,
        canonical_text: str = "Hello",
        provider_identity_fingerprint: str | None = None,
        source_kind: str = TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        parser_version: str = PARSER_VERSION,
        transkribus_run: TranskribusRun | None = None,
    ) -> TranskribusTranscriptSnapshot:
        return TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            transkribus_run=transkribus_run,
            source_kind=source_kind,
            parser_version=parser_version,
            provider_identity_fingerprint=(
                provider_identity_fingerprint
                or compute_provider_identity_fingerprint([(1, "ts-1")])
            ),
            raw_xml_fingerprint=raw_xml_fingerprint,
            canonical_text=canonical_text,
            canonical_text_sha256=compute_sha256_hex(canonical_text),
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )

    def test_same_provider_identity_different_raw_xml_allowed(self):
        doc = self._create_doc()
        common_provider_fp = compute_provider_identity_fingerprint([(1, "ts-1")])
        self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint="a" * 64,
            canonical_text="A",
            provider_identity_fingerprint=common_provider_fp,
        )
        self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint="b" * 64,
            canonical_text="B",
            provider_identity_fingerprint=common_provider_fp,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document=doc).count(), 2
        )

    def test_same_raw_xml_different_parser_version_allowed(self):
        doc = self._create_doc(title="Reparse version doc")
        raw_fp = "c" * 64
        self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint=raw_fp,
            canonical_text="X",
        )
        self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint=raw_fp,
            canonical_text="X",
            parser_version="page_xml_snapshot_v2",
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document=doc).count(), 2
        )

    def test_ready_raw_xml_fingerprint_unique_per_document_parser(self):
        doc = self._create_doc(title="Unique raw xml doc")
        raw_fp = "d" * 64
        self._create_ready_snapshot(doc, raw_xml_fingerprint=raw_fp)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._create_ready_snapshot(
                    doc,
                    raw_xml_fingerprint=raw_fp,
                    source_kind=(
                        TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC
                    ),
                )

    def test_ready_requires_complete_fingerprints(self):
        doc = self._create_doc(title="Incomplete READY rejected")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusTranscriptSnapshot.objects.create(
                    document=doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    parser_version=PARSER_VERSION,
                    provider_identity_fingerprint="p" * 64,
                    raw_xml_fingerprint="r" * 64,
                    canonical_text_sha256="",
                    storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusTranscriptSnapshot.objects.create(
                    document=doc,
                    source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                    parser_version=PARSER_VERSION,
                    provider_identity_fingerprint=None,
                    raw_xml_fingerprint="r" * 64,
                    canonical_text_sha256=compute_sha256_hex("X"),
                    storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
                )

    def test_pending_incomplete_fingerprints_allowed(self):
        doc = self._create_doc(title="Pending fingerprint doc")
        TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version=PARSER_VERSION,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
        )
        TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
            parser_version=PARSER_VERSION,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document=doc).count(), 2
        )

    def test_page_index_unique_and_positive(self):
        doc = self._create_doc(title="Page constraint doc")
        snap = self._create_ready_snapshot(doc, raw_xml_fingerprint="e" * 64)
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=1,
            transcript_ts_id="1",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusSnapshotPage.objects.create(
                    snapshot=snap,
                    page_index=1,
                    page_nr=2,
                    transcript_ts_id="2",
                )

    def test_page_nr_unique_within_snapshot(self):
        doc = self._create_doc(title="Page nr unique doc")
        snap = self._create_ready_snapshot(doc, raw_xml_fingerprint="e1" * 32)
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=7,
            transcript_ts_id="1",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusSnapshotPage.objects.create(
                    snapshot=snap,
                    page_index=2,
                    page_nr=7,
                    transcript_ts_id="2",
                )

    def test_empty_transcript_ts_id_rejected(self):
        doc = self._create_doc(title="Empty ts id doc")
        snap = self._create_ready_snapshot(doc, raw_xml_fingerprint="e2" * 32)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusSnapshotPage.objects.create(
                    snapshot=snap,
                    page_index=1,
                    page_nr=1,
                    transcript_ts_id="",
                )

    def test_zero_image_dimensions_rejected(self):
        doc = self._create_doc(title="Zero dims doc")
        snap = self._create_ready_snapshot(doc, raw_xml_fingerprint="e3" * 32)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusSnapshotPage.objects.create(
                    snapshot=snap,
                    page_index=1,
                    page_nr=1,
                    transcript_ts_id="1",
                    image_width=0,
                    image_height=100,
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TranskribusSnapshotPage.objects.create(
                    snapshot=snap,
                    page_index=1,
                    page_nr=1,
                    transcript_ts_id="1",
                    image_width=100,
                    image_height=0,
                )
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=1,
            transcript_ts_id="1",
            image_width=None,
            image_height=None,
        )

    def test_cross_document_run_rejected_on_create(self):
        doc_a = self._create_doc(title="Snap run A")
        doc_b = self._create_doc(title="Snap run B")
        run_b = self._create_run(doc_b)
        with self.assertRaises(ValidationError):
            TranskribusTranscriptSnapshot.objects.create(
                document=doc_a,
                transkribus_run=run_b,
                source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
                parser_version=PARSER_VERSION,
                provider_identity_fingerprint="p" * 64,
                raw_xml_fingerprint="r" * 64,
                canonical_text="X",
                canonical_text_sha256=compute_sha256_hex("X"),
                storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            )

    def test_same_document_run_accepted(self):
        doc = self._create_doc(title="Snap run same")
        run = self._create_run(doc)
        snap = self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint="g0" * 32,
            transkribus_run=run,
        )
        self.assertEqual(snap.transkribus_run_id, run.id)

    def test_cross_document_binding_rejected_on_create(self):
        doc_a = self._create_doc(title="Bind A")
        doc_b = self._create_doc(title="Bind B")
        result_a = self._create_text_result(doc_a)
        snap_b = self._create_ready_snapshot(
            doc_b,
            raw_xml_fingerprint="f" * 64,
            canonical_text="Hello\nWorld",
        )
        with self.assertRaises(ValidationError):
            TranskribusTextResultBinding.objects.create(
                text_result=result_a,
                snapshot=snap_b,
                binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
                bound_text_sha256=compute_sha256_hex("Hello\nWorld"),
                bound_source_revision=1,
            )

    def test_same_document_binding_accepted(self):
        doc = self._create_doc(title="Bind same")
        result = self._create_text_result(doc)
        snap = self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint="g" * 64,
            canonical_text="Hello\nWorld",
        )
        binding = TranskribusTextResultBinding.objects.create(
            text_result=result,
            snapshot=snap,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=compute_sha256_hex("Hello\nWorld"),
            bound_source_revision=1,
        )
        self.assertEqual(binding.snapshot_id, snap.id)
        self.assertEqual(result.transkribus_snapshot_binding.snapshot_id, snap.id)

    def test_document_delete_cascades_snapshots_pages_lines_bindings(self):
        doc = self._create_doc(title="Cascade doc")
        result = self._create_text_result(doc)
        snap = self._create_ready_snapshot(
            doc,
            raw_xml_fingerprint="h" * 64,
            canonical_text="Hello",
        )
        page = TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=1,
            transcript_ts_id="9",
            page_xml_sha256="i" * 64,
        )
        line = TranskribusSnapshotLine.objects.create(
            page=page,
            order_index=0,
            text="Hello",
            char_start=0,
            char_end=5,
            contributes_to_canonical=True,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=result,
            snapshot=snap,
            bound_text_sha256=compute_sha256_hex("Hello"),
            bound_source_revision=1,
        )
        snap_id = snap.id
        page_id = page.id
        line_id = line.id
        result_id = result.id

        doc.delete()

        self.assertFalse(
            TranskribusTranscriptSnapshot.objects.filter(pk=snap_id).exists()
        )
        self.assertFalse(TranskribusSnapshotPage.objects.filter(pk=page_id).exists())
        self.assertFalse(TranskribusSnapshotLine.objects.filter(pk=line_id).exists())
        self.assertFalse(DocumentTextResult.objects.filter(pk=result_id).exists())
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(snapshot_id=snap_id).exists()
        )
