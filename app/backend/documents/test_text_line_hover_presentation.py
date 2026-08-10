"""Tests for trusted text-line hover → source-image presentation."""

import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.overlay_bbox_percent import page_bbox_to_percent
from documents.services.text_line_hover_presentation import (
    TextLineHoverOverlayTarget,
    apply_text_line_hover_overlay_to_source_previews,
    build_text_line_hover_overlay_pages,
    build_text_line_hover_presentation,
    build_text_line_hover_single_image_overlay,
)
from documents.test_archive_item import create_viewable_ocr_document


class OverlayBBoxPercentTests(SimpleTestCase):
    def test_converts_in_bounds_bbox(self):
        self.assertEqual(
            page_bbox_to_percent(
                min_x=100,
                min_y=50,
                max_x=300,
                max_y=150,
                image_width=1000,
                image_height=500,
            ),
            (10.0, 10.0, 20.0, 20.0),
        )

    def test_out_of_bounds_fails_closed(self):
        self.assertIsNone(
            page_bbox_to_percent(
                min_x=100,
                min_y=50,
                max_x=300,
                max_y=150,
                image_width=200,
                image_height=200,
            )
        )


class TextLineHoverPresentationTests(TestCase):
    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _line(
        self,
        page,
        *,
        order,
        text,
        start,
        end,
        y,
        provider_line_id=None,
        coords_valid=True,
        has_meaningful_geometry=True,
        polygon_points=None,
        bbox=None,
    ):
        min_x, min_y, max_x, max_y = bbox or (10.0, float(y), 100.0, float(y + 10))
        return TranskribusSnapshotLine.objects.create(
            page=page,
            order_index=order,
            provider_region_id=f"region-{page.page_index}",
            provider_line_id=provider_line_id or f"line-{page.page_index}-{order}",
            text=text,
            contributes_to_canonical=True,
            char_start=start,
            char_end=end,
            polygon_points=polygon_points
            or [
                [min_x, min_y],
                [max_x, min_y],
                [max_x, max_y],
                [min_x, max_y],
            ],
            bbox_min_x=min_x,
            bbox_min_y=min_y,
            bbox_max_x=max_x,
            bbox_max_y=max_y,
            coords_valid=coords_valid,
            has_meaningful_geometry=has_meaningful_geometry,
        )

    def _trusted_fixture(
        self,
        *,
        text="Alpha\nBeta",
        language=Document.Language.HEBREW,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        hover_eligible=True,
        source_revision=1,
        based_on_source_revision=None,
        binding_role=None,
        image_width=1000,
        image_height=500,
        page2=False,
        gamma_on_page2=False,
    ):
        doc = create_ocr_document(
            title="Hover presentation",
            doc_type=Document.DocType.IMAGE,
            language=language,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/hover-presentation/source.jpg",
            mime_type="image/jpeg",
            visibility=Document.Visibility.PUBLIC,
        )
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            result_kwargs = {
                "based_on_source_revision": (
                    1 if based_on_source_revision is None else based_on_source_revision
                ),
            }
            role = (
                TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
                if binding_role is None
                else binding_role
            )
        else:
            result_kwargs = {"source_revision": source_revision}
            role = (
                TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE
                if binding_role is None
                else binding_role
            )

        result = DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine="transkribus-test",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            **result_kwargs,
        )
        sha = self._sha(text)
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="p" * 64,
            raw_xml_fingerprint="r" * 64,
            canonical_text=text,
            canonical_text_sha256=sha,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=hover_eligible,
        )
        binding = TranskribusTextResultBinding.objects.create(
            text_result=result,
            snapshot=snapshot,
            binding_role=role,
            bound_text_sha256=sha,
            bound_source_revision=source_revision
            if result_type == DocumentTextResult.ResultType.SOURCE_TEXT
            else (1 if based_on_source_revision is None else based_on_source_revision),
        )
        page1 = TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-1",
            image_width=image_width,
            image_height=image_height,
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        lines = {}
        if text.startswith("Alpha"):
            lines["alpha"] = self._line(
                page1, order=0, text="Alpha", start=0, end=5, y=10
            )
            if "\nBeta" in text or text.endswith("Beta"):
                lines["beta"] = self._line(
                    page1, order=1, text="Beta", start=6, end=10, y=30
                )
        if gamma_on_page2 or page2:
            page2_row = TranskribusSnapshotPage.objects.create(
                snapshot=snapshot,
                page_index=2,
                page_nr=2,
                transcript_ts_id="ts-2",
                image_width=image_width,
                image_height=image_height,
                page_geometry_capability=(
                    TranskribusSnapshotPage.GeometryCapability.VERIFIED
                ),
            )
            if gamma_on_page2:
                # "Alpha\nBeta\n\nGamma"
                lines["gamma"] = self._line(
                    page2_row, order=0, text="Gamma", start=12, end=17, y=50
                )

        return doc, result, snapshot, binding, page1, lines

    def test_trusted_fresh_binding_produces_hoverable_mapping(self):
        doc, result, *_rest = self._trusted_fixture()

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(presentation.text_result_id, result.pk)
        self.assertEqual(presentation.result_type, "SOURCE_TEXT")
        joined = "".join(segment.text for segment in presentation.segments)
        self.assertEqual(joined, result.text)
        hoverable = [
            segment for segment in presentation.segments if segment.hover_line_id
        ]
        self.assertEqual([segment.text for segment in hoverable], ["Alpha", "Beta"])
        self.assertEqual(
            {target.hover_line_id for target in presentation.overlay_targets},
            {"p1-o0", "p1-o1"},
        )
        alpha = presentation.overlay_targets[0]
        self.assertEqual(alpha.page_index, 1)
        self.assertAlmostEqual(alpha.left_pct, 1.0)
        self.assertAlmostEqual(alpha.top_pct, 2.0)
        self.assertAlmostEqual(alpha.width_pct, 9.0)
        self.assertAlmostEqual(alpha.height_pct, 2.0)

    def test_visible_text_preserved_exactly_including_separators(self):
        doc, result, *_rest = self._trusted_fixture(
            text="Alpha\nBeta\n\nGamma", gamma_on_page2=True
        )

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": "https://example.test/2"},
            ],
            content_url=None,
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            result.text,
        )
        plain = [
            segment.text
            for segment in presentation.segments
            if segment.hover_line_id is None
        ]
        self.assertEqual(plain, ["\n", "\n\n"])

    def test_stale_binding_fails_closed(self):
        doc, result, *_rest = self._trusted_fixture()
        result.source_revision = 2
        result.save(update_fields=["source_revision"])

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertFalse(presentation.enabled)
        self.assertEqual(presentation.segments, ())
        self.assertEqual(presentation.overlay_targets, ())

    def test_missing_binding_fails_closed(self):
        doc, result, *_rest = self._trusted_fixture()
        TranskribusTextResultBinding.objects.filter(text_result=result).delete()

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertFalse(presentation.enabled)

    def test_hover_ineligible_snapshot_fails_closed(self):
        doc, *_rest = self._trusted_fixture(hover_eligible=False)

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertFalse(presentation.enabled)

    def test_invalid_geometry_line_stays_plain_while_valid_line_remains_hoverable(
        self,
    ):
        doc, result, *_rest = self._trusted_fixture()
        bad = TranskribusSnapshotLine.objects.get(
            page__snapshot__text_result_bindings__text_result=result,
            order_index=1,
        )
        bad.coords_valid = False
        bad.save(update_fields=["coords_valid"])

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            result.text,
        )
        by_text = {
            segment.text: segment.hover_line_id for segment in presentation.segments
        }
        self.assertEqual(by_text["Alpha"], "p1-o0")
        self.assertIsNone(by_text["Beta"])
        self.assertEqual(
            {target.hover_line_id for target in presentation.overlay_targets},
            {"p1-o0"},
        )

    def test_out_of_bounds_bbox_percent_makes_line_non_hoverable(self):
        doc, result, *_rest = self._trusted_fixture(
            image_width=50,
            image_height=50,
        )
        # Default bbox max_x=100 exceeds width=50 → percent conversion fails per line.
        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertFalse(presentation.enabled)
        self.assertEqual(presentation.overlay_targets, ())
        # Binding was trusted; segments are empty only because no usable overlays
        # remain after per-line percent failure disables hover entirely.
        self.assertEqual(presentation.segments, ())

    def test_non_renderable_page_geometry_is_not_exposed(self):
        doc, result, *_rest = self._trusted_fixture(
            text="Alpha\nBeta\n\nGamma",
            gamma_on_page2=True,
        )

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": None},
            ],
            content_url=None,
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            result.text,
        )
        self.assertEqual(
            {target.page_index for target in presentation.overlay_targets},
            {1},
        )
        gamma_segments = [
            segment for segment in presentation.segments if segment.text == "Gamma"
        ]
        self.assertEqual(len(gamma_segments), 1)
        self.assertIsNone(gamma_segments[0].hover_line_id)

    def test_correct_source_page_receives_hover_overlay(self):
        doc, *_rest = self._trusted_fixture(
            text="Alpha\nBeta\n\nGamma",
            gamma_on_page2=True,
        )

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": "https://example.test/2"},
            ],
            content_url=None,
        )
        pages = build_text_line_hover_overlay_pages(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": "https://example.test/2"},
            ],
            content_url=None,
            overlay_targets=presentation.overlay_targets,
        )

        self.assertEqual([page.page_index for page in pages], [1, 2])
        self.assertEqual(
            {target.hover_line_id for target in pages[0].targets},
            {"p1-o0", "p1-o1"},
        )
        self.assertEqual(
            {target.hover_line_id for target in pages[1].targets},
            {"p2-o0"},
        )

    def test_hebrew_prefers_hebrew_text_result_for_hover_identity(self):
        doc = create_ocr_document(
            title="Hebrew hover identity",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/hebrew-hover/source.jpg",
            mime_type="image/jpeg",
            visibility=Document.Visibility.PUBLIC,
        )
        text = "שורה"
        sha = self._sha(text)
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-source",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            source_revision=1,
        )
        hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-hebrew",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            based_on_source_revision=1,
        )
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="h" * 64,
            raw_xml_fingerprint="i" * 64,
            canonical_text=text,
            canonical_text_sha256=sha,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=True,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=source,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=sha,
            bound_source_revision=1,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=hebrew,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_text_sha256=sha,
            bound_source_revision=1,
        )
        page = TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-he",
            image_width=200,
            image_height=100,
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        self._line(page, order=0, text=text, start=0, end=len(text), y=10)

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(presentation.result_type, "HEBREW_TEXT")
        self.assertEqual(presentation.text_result_id, hebrew.pk)
        self.assertNotEqual(presentation.text_result_id, source.pk)

    def test_pdf_exposes_no_hover(self):
        doc, *_rest = self._trusted_fixture()
        doc.doc_type = Document.DocType.PDF
        doc.save(update_fields=["doc_type"])

        presentation = build_text_line_hover_presentation(
            doc,
            source_preview_items=[],
            content_url="https://example.test/doc.pdf",
        )

        self.assertFalse(presentation.enabled)


class TextLineHoverOverlayPagesTests(SimpleTestCase):
    def test_target_for_nonexistent_page_is_not_exposed(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        target = TextLineHoverOverlayTarget(
            hover_line_id="p3-o0",
            page_index=3,
            left_pct=1.0,
            top_pct=2.0,
            width_pct=3.0,
            height_pct=4.0,
        )

        pages = build_text_line_hover_overlay_pages(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": "https://example.test/2"},
            ],
            content_url=None,
            overlay_targets=(target,),
        )

        self.assertEqual(len(pages), 2)
        self.assertTrue(all(page.targets == () for page in pages))

    def test_apply_and_single_image_helpers(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        target = TextLineHoverOverlayTarget(
            hover_line_id="p1-o0",
            page_index=1,
            left_pct=10.0,
            top_pct=20.0,
            width_pct=30.0,
            height_pct=40.0,
        )
        pages = build_text_line_hover_overlay_pages(
            doc,
            source_preview_items=[],
            content_url="https://example.test/image",
            overlay_targets=(target,),
        )
        single = build_text_line_hover_single_image_overlay(
            doc,
            content_url="https://example.test/image",
            overlay_pages=pages,
        )
        applied = apply_text_line_hover_overlay_to_source_previews(
            [{"display_number": 1, "url": "https://example.test/1"}],
            pages,
        )

        self.assertEqual(single.page_index, 1)
        self.assertEqual(single.targets, (target,))
        self.assertEqual(applied[0]["text_line_hover_overlay_targets"], (target,))


class TextLineHoverDetailRenderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="text_line_hover_render_user",
            password="x",
        )

    def _sha(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _doc_with_trusted_hover(self):
        doc = create_viewable_ocr_document(
            title="Hover render",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        doc.file_s3_key = "documents/hover-render.png"
        doc.save(update_fields=["file_s3_key"])
        text = "Alpha\nBeta"
        sha = self._sha(text)
        result = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-test",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            source_revision=1,
        )
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="j" * 64,
            raw_xml_fingerprint="k" * 64,
            canonical_text=text,
            canonical_text_sha256=sha,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=True,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=result,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=sha,
            bound_source_revision=1,
        )
        page = TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-render",
            image_width=1000,
            image_height=500,
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        for order, line_text, start, end, y in (
            (0, "Alpha", 0, 5, 10),
            (1, "Beta", 6, 10, 30),
        ):
            TranskribusSnapshotLine.objects.create(
                page=page,
                order_index=order,
                provider_line_id=f"line-{order}",
                text=line_text,
                contributes_to_canonical=True,
                char_start=start,
                char_end=end,
                polygon_points=[[10, y], [100, y], [100, y + 10], [10, y + 10]],
                bbox_min_x=10,
                bbox_min_y=y,
                bbox_max_x=100,
                bbox_max_y=y + 10,
                coords_valid=True,
                has_meaningful_geometry=True,
            )
        return doc, result

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.png",
    )
    def test_detail_renders_hover_markup_and_preserves_visible_text(self, _presign):
        doc, result = self._doc_with_trusted_hover()
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["text_line_hover"].enabled)
        self.assertContains(response, 'data-text-line-hover-id="p1-o0"', html=False)
        self.assertContains(
            response, 'class="text-line-hover-overlay-target"', html=False
        )
        self.assertContains(response, "text-line-hover-source", html=False)
        body = response.content.decode("utf-8")
        self.assertIn("Alpha", body)
        self.assertIn("Beta", body)
        self.assertIn('data-text-line-hover-id="p1-o0"', body)
        self.assertIn("text-line-hover-overlay-target--active", body)

    @patch("documents.views.apply_text_line_hover_overlay_to_source_previews")
    @patch("documents.views.build_text_line_hover_single_image_overlay")
    @patch("documents.views.build_text_line_hover_overlay_pages")
    @patch("documents.views.build_text_line_hover_presentation")
    @patch("documents.views.apply_archive_search_overlay_to_source_previews")
    @patch("documents.views.build_archive_search_overlay_pages")
    @patch("documents.views.build_archive_search_overlay_targets")
    @patch("documents.views.resolve_archive_search_geometry_matches")
    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.png",
    )
    def test_search_and_hover_overlays_coexist_in_detail_html(
        self,
        _presign,
        mock_resolve,
        mock_build_search_targets,
        mock_build_search_pages,
        mock_apply_search_previews,
        mock_build_hover,
        mock_build_hover_pages,
        mock_build_hover_single,
        mock_apply_hover_previews,
    ):
        doc = create_viewable_ocr_document(
            title="Coexist overlays",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        doc.file_s3_key = "documents/coexist.png"
        doc.save(update_fields=["file_s3_key"])
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Alpha\nBeta",
            source_revision=1,
        )

        search_target = SimpleNamespace(
            match_index=0,
            term="Alpha",
            left_pct=10.0,
            top_pct=20.0,
            width_pct=30.0,
            height_pct=40.0,
        )
        hover_target = TextLineHoverOverlayTarget(
            hover_line_id="p1-o0",
            page_index=1,
            left_pct=11.0,
            top_pct=21.0,
            width_pct=31.0,
            height_pct=41.0,
        )
        hover_presentation = SimpleNamespace(
            enabled=True,
            result_type="SOURCE_TEXT",
            text_result_id=1,
            segments=(
                SimpleNamespace(text="Alpha", hover_line_id="p1-o0"),
                SimpleNamespace(text="\n", hover_line_id=None),
                SimpleNamespace(text="Beta", hover_line_id="p1-o1"),
            ),
            overlay_targets=(hover_target,),
        )

        mock_resolve.return_value = (object(),)
        mock_build_search_targets.return_value = (search_target,)
        mock_build_search_pages.return_value = (
            SimpleNamespace(page_index=1, targets=(search_target,)),
        )
        mock_apply_search_previews.return_value = []
        mock_build_hover.return_value = hover_presentation
        mock_build_hover_pages.return_value = (
            SimpleNamespace(page_index=1, targets=(hover_target,)),
        )
        mock_build_hover_single.return_value = SimpleNamespace(
            page_index=1,
            targets=(hover_target,),
        )
        mock_apply_hover_previews.side_effect = lambda items, pages: items

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id}),
            {"q": "Alpha"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'data-archive-search-match-index="0"',
            html=False,
        )
        self.assertContains(
            response,
            'data-text-line-hover-id="p1-o0"',
            html=False,
        )
        self.assertContains(response, "archive-search-overlay-target", html=False)
        self.assertContains(response, "text-line-hover-overlay-target", html=False)
        # Search active-class wiring remains present and distinct from hover.
        self.assertContains(
            response,
            "archive-search-overlay-target--active",
            html=False,
        )
        self.assertContains(
            response,
            "text-line-hover-overlay-target--active",
            html=False,
        )
