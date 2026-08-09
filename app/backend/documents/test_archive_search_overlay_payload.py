from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.archive_search_overlay_payload import (
    build_archive_search_overlay_targets,
)


class ArchiveSearchOverlayPayloadTests(SimpleTestCase):
    def _geometry(
        self,
        *,
        page_index=1,
        min_x=100.0,
        min_y=50.0,
        max_x=300.0,
        max_y=150.0,
    ):
        return SimpleNamespace(
            page_index=page_index,
            bbox_min_x=min_x,
            bbox_min_y=min_y,
            bbox_max_x=max_x,
            bbox_max_y=max_y,
        )

    def _match(self, *geometry, term="alpha", text_result=None):
        if text_result is None:
            text_result = SimpleNamespace(pk=17)
        return SimpleNamespace(
            term=term,
            text_result=text_result,
            geometry=geometry,
        )

    def _page_row(
        self,
        *,
        text_result_id=17,
        page_index=1,
        width=1000,
        height=500,
    ):
        return {
            "snapshot__text_result_bindings__text_result_id": text_result_id,
            "page_index": page_index,
            "image_width": width,
            "image_height": height,
        }

    def _set_page_rows(self, objects, rows):
        objects.filter.return_value.values.return_value.distinct.return_value = rows

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_converts_bbox_to_page_relative_percentages(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row()],
        )

        targets = build_archive_search_overlay_targets((self._match(self._geometry()),))

        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target.match_index, 0)
        self.assertEqual(target.term, "alpha")
        self.assertEqual(target.page_index, 1)
        self.assertAlmostEqual(target.left_pct, 10.0)
        self.assertAlmostEqual(target.top_pct, 10.0)
        self.assertAlmostEqual(target.width_pct, 20.0)
        self.assertAlmostEqual(target.height_pct, 20.0)

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_multi_line_match_keeps_same_match_index(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row()],
        )

        targets = build_archive_search_overlay_targets(
            (
                self._match(
                    self._geometry(min_y=50, max_y=100),
                    self._geometry(min_y=120, max_y=170),
                ),
            )
        )

        self.assertEqual(len(targets), 2)
        self.assertEqual([target.match_index for target in targets], [0, 0])

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_different_matches_get_different_match_indexes(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row()],
        )

        targets = build_archive_search_overlay_targets(
            (
                self._match(self._geometry(), term="alpha"),
                self._match(self._geometry(), term="beta"),
            )
        )

        self.assertEqual(
            [(target.match_index, target.term) for target in targets],
            [(0, "alpha"), (1, "beta")],
        )

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_missing_page_fails_closed_for_match(self, objects):
        self._set_page_rows(objects, [])

        targets = build_archive_search_overlay_targets((self._match(self._geometry()),))

        self.assertEqual(targets, ())

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_missing_dimensions_fail_closed(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row(width=None)],
        )

        targets = build_archive_search_overlay_targets((self._match(self._geometry()),))

        self.assertEqual(targets, ())

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_out_of_bounds_geometry_fails_closed(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row(width=200, height=200)],
        )

        targets = build_archive_search_overlay_targets(
            (
                self._match(
                    self._geometry(
                        min_x=100,
                        min_y=50,
                        max_x=300,
                        max_y=150,
                    )
                ),
            )
        )

        self.assertEqual(targets, ())

    @patch(
        "documents.services.archive_search_overlay_payload."
        "TranskribusSnapshotPage.objects"
    )
    def test_one_bad_line_drops_entire_match(self, objects):
        self._set_page_rows(
            objects,
            [self._page_row(page_index=1)],
        )

        targets = build_archive_search_overlay_targets(
            (
                self._match(
                    self._geometry(page_index=1, min_y=50, max_y=100),
                    self._geometry(page_index=2, min_y=120, max_y=170),
                ),
            )
        )

        self.assertEqual(targets, ())


class ArchiveSearchOverlayPayloadQueryCountTests(TestCase):
    def setUp(self):
        self.doc = create_ocr_document(
            title="Overlay query count",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PRIVATE,
        )
        self.text_result = DocumentTextResult.objects.create(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="alpha beta gamma",
            source_revision=1,
        )
        self.snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=self.doc,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            provider_identity_fingerprint="provider-fingerprint",
            raw_xml_fingerprint="raw-fingerprint",
            canonical_text_sha256="a" * 64,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=self.text_result,
            snapshot=self.snapshot,
            bound_text_sha256="b" * 64,
            bound_source_revision=1,
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=self.snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-1",
            image_width=1000,
            image_height=1000,
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=self.snapshot,
            page_index=2,
            page_nr=2,
            transcript_ts_id="ts-2",
            image_width=1000,
            image_height=1000,
        )

    def _geometry(self, page_index, min_y):
        return SimpleNamespace(
            page_index=page_index,
            bbox_min_x=100.0,
            bbox_min_y=float(min_y),
            bbox_max_x=300.0,
            bbox_max_y=float(min_y + 50),
        )

    def _match(self, *geometry, term="alpha"):
        return SimpleNamespace(
            term=term,
            text_result=self.text_result,
            geometry=geometry,
        )

    def test_multiple_matches_and_lines_use_one_snapshot_page_query(self):
        matches = (
            self._match(
                self._geometry(1, 100),
                self._geometry(1, 200),
                term="alpha",
            ),
            self._match(
                self._geometry(2, 100),
                self._geometry(2, 200),
                term="beta",
            ),
        )

        with self.assertNumQueries(1):
            targets = build_archive_search_overlay_targets(matches)

        self.assertEqual(len(targets), 4)
        self.assertEqual(
            [target.match_index for target in targets],
            [0, 0, 1, 1],
        )
