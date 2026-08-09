"""Tests for trusted text-range -> Transkribus line geometry resolution."""

import hashlib

from django.test import TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_text_range_geometry import (
    resolve_text_range_geometry,
)


class TranskribusTextRangeGeometryTests(TestCase):
    def setUp(self):
        self.doc = create_ocr_document(
            title="Range geometry",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/range-geometry/source.jpg",
            mime_type="image/jpeg",
        )
        self.text = "Alpha\nBeta\n\nGamma"
        self.result = DocumentTextResult.objects.create(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text=self.text,
            source_revision=1,
        )
        sha = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        self.snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=self.doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="p" * 64,
            raw_xml_fingerprint="r" * 64,
            canonical_text=self.text,
            canonical_text_sha256=sha,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=True,
        )
        self.binding = TranskribusTextResultBinding.objects.create(
            text_result=self.result,
            snapshot=self.snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=sha,
            bound_source_revision=1,
        )
        self.page1 = TranskribusSnapshotPage.objects.create(
            snapshot=self.snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-1",
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        self.page2 = TranskribusSnapshotPage.objects.create(
            snapshot=self.snapshot,
            page_index=2,
            page_nr=2,
            transcript_ts_id="ts-2",
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        self.alpha = self._line(self.page1, 0, "Alpha", 0, 5, 10)
        self.beta = self._line(self.page1, 1, "Beta", 6, 10, 30)
        self.gamma = self._line(self.page2, 0, "Gamma", 12, 17, 50)

    def _line(self, page, order, text, start, end, y):
        return TranskribusSnapshotLine.objects.create(
            page=page,
            order_index=order,
            provider_region_id=f"region-{page.page_index}",
            provider_line_id=f"line-{page.page_index}-{order}",
            text=text,
            contributes_to_canonical=True,
            char_start=start,
            char_end=end,
            polygon_points=[
                [10, y],
                [100, y],
                [100, y + 10],
                [10, y + 10],
            ],
            bbox_min_x=10,
            bbox_min_y=y,
            bbox_max_x=100,
            bbox_max_y=y + 10,
            coords_valid=True,
            has_meaningful_geometry=True,
        )

    def _resolve(self, start, end):
        return resolve_text_range_geometry(
            self.result,
            start=start,
            end=end,
            binding=self.binding,
        )

    def test_single_line_range(self):
        resolved = self._resolve(1, 4)

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].provider_line_id, "line-1-0")
        self.assertEqual((resolved[0].char_start, resolved[0].char_end), (0, 5))

    def test_exact_half_open_boundary_does_not_touch_next_line(self):
        resolved = self._resolve(0, 5)

        self.assertEqual(
            [target.provider_line_id for target in resolved],
            ["line-1-0"],
        )

    def test_range_crossing_line_separator_returns_both_lines(self):
        resolved = self._resolve(4, 7)

        self.assertEqual(
            [target.provider_line_id for target in resolved],
            ["line-1-0", "line-1-1"],
        )

    def test_separator_only_range_returns_empty(self):
        self.assertEqual(self._resolve(5, 6), ())

    def test_range_crossing_page_separator_returns_both_pages(self):
        resolved = self._resolve(9, 13)

        self.assertEqual(
            [(target.page_index, target.provider_line_id) for target in resolved],
            [(1, "line-1-1"), (2, "line-2-0")],
        )

    def test_page_separator_only_range_returns_empty(self):
        self.assertEqual(self._resolve(10, 12), ())

    def test_invalid_ranges_fail_closed(self):
        for start, end in (
            (-1, 1),
            (0, 0),
            (3, 2),
            (0, len(self.text) + 1),
        ):
            with self.subTest(start=start, end=end):
                self.assertEqual(self._resolve(start, end), ())

        self.assertEqual(
            resolve_text_range_geometry(
                self.result,
                start=True,
                end=1,
                binding=self.binding,
            ),
            (),
        )

    def test_stale_binding_fails_closed(self):
        self.result.text = self.text + " edited"
        self.result.source_revision = 2
        self.result.save(update_fields=["text", "source_revision"])

        self.assertEqual(self._resolve(0, 5), ())

    def test_hover_ineligible_snapshot_fails_closed(self):
        self.snapshot.hover_eligible = False
        self.snapshot.save(update_fields=["hover_eligible"])

        self.assertEqual(self._resolve(0, 5), ())

    def test_invalid_intersecting_geometry_fails_whole_resolution_closed(self):
        self.beta.coords_valid = False
        self.beta.save(update_fields=["coords_valid"])

        self.assertEqual(self._resolve(4, 7), ())

    def test_binding_for_different_text_result_fails_closed(self):
        other_result = DocumentTextResult.objects.create(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="other-engine",
            text=self.text,
            source_revision=1,
        )

        self.assertEqual(
            resolve_text_range_geometry(
                other_result,
                start=0,
                end=5,
                binding=self.binding,
            ),
            (),
        )

    def test_malformed_polygon_fails_closed(self):
        self.alpha.polygon_points = [[10, 10], [100, 10]]
        self.alpha.save(update_fields=["polygon_points"])

        self.assertEqual(self._resolve(0, 5), ())

    def test_invalid_bbox_fails_closed(self):
        self.alpha.bbox_max_x = self.alpha.bbox_min_x
        self.alpha.save(update_fields=["bbox_max_x"])

        self.assertEqual(self._resolve(0, 5), ())

    def test_empty_geometry_bearing_line_cannot_match_text(self):
        TranskribusSnapshotLine.objects.create(
            page=self.page1,
            order_index=2,
            provider_line_id="empty",
            text="",
            contributes_to_canonical=False,
            char_start=5,
            char_end=5,
            polygon_points=[[1, 1], [2, 1], [2, 2], [1, 2]],
            bbox_min_x=1,
            bbox_min_y=1,
            bbox_max_x=2,
            bbox_max_y=2,
            coords_valid=True,
            has_meaningful_geometry=True,
        )

        self.assertEqual(self._resolve(5, 6), ())

    def test_without_explicit_binding_uses_current_result_binding(self):
        resolved = resolve_text_range_geometry(
            self.result,
            start=12,
            end=17,
        )

        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].page_index, 2)
        self.assertEqual(resolved[0].provider_line_id, "line-2-0")

    def test_geometry_payload_is_immutable_normalized_tuple(self):
        resolved = self._resolve(0, 5)

        self.assertEqual(
            resolved[0].polygon_points,
            (
                (10.0, 10.0),
                (100.0, 10.0),
                (100.0, 20.0),
                (10.0, 20.0),
            ),
        )
        self.assertEqual(
            (
                resolved[0].bbox_min_x,
                resolved[0].bbox_min_y,
                resolved[0].bbox_max_x,
                resolved[0].bbox_max_y,
            ),
            (10.0, 10.0, 100.0, 20.0),
        )
