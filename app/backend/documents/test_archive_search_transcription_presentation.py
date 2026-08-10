"""Tests for archive-search → displayed-transcription sync presentation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from documents.models import Document, DocumentTextResult
from documents.services.archive_search_transcription_presentation import (
    build_archive_search_transcription_presentation,
)
from documents.services.text_line_hover_presentation import (
    TextLineHoverPresentation,
    TextLineHoverSegment,
)
from documents.test_archive_item import create_viewable_ocr_document


def _archive_search_overlay_script_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "templates"
        / "documents"
        / "partials"
        / "_archive_search_overlay_script.html"
    )


def _match(
    text_result: DocumentTextResult,
    *,
    term: str,
    start: int,
    end: int,
):
    """Geometry payload is unused by transcription presentation; keep a stub."""
    return SimpleNamespace(
        term=term,
        text_result=text_result,
        start=start,
        end=end,
        geometry=(SimpleNamespace(page_index=1),),
    )


def _result(
    doc: Document,
    *,
    result_type: str,
    text: str,
    engine: str = "test-engine",
) -> DocumentTextResult:
    kwargs: dict = {
        "document": doc,
        "result_type": result_type,
        "engine": engine,
        "engine_key": DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        "status": DocumentTextResult.Status.NEEDS_REVIEW,
        "text": text,
    }
    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        kwargs["source_revision"] = 1
    else:
        kwargs["based_on_source_revision"] = 1
    return DocumentTextResult.objects.create(**kwargs)


class ArchiveSearchTranscriptionPresentationTests(TestCase):
    def test_displayed_transcription_match_gets_target_for_match_index(self):
        doc = create_viewable_ocr_document(
            title="Sync target",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Alpha Beta Alpha",
        )
        matches = (
            _match(source, term="Alpha", start=0, end=5),
            _match(source, term="Alpha", start=11, end=16),
        )

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(presentation.result_type, "SOURCE_TEXT")
        self.assertEqual(presentation.text_result_id, source.pk)
        self.assertEqual(presentation.match_indexes, (0, 1))
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            "Alpha Beta Alpha",
        )
        indexed = {
            segment.archive_search_match_index: segment.text
            for segment in presentation.segments
            if segment.archive_search_match_index is not None
        }
        self.assertEqual(indexed[0], "Alpha")
        self.assertEqual(indexed[1], "Alpha")

    def test_multiple_matches_map_to_distinct_positions(self):
        doc = create_viewable_ocr_document(
            title="Distinct positions",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="one two one",
        )
        matches = (
            _match(source, term="one", start=0, end=3),
            _match(source, term="one", start=8, end=11),
        )

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
        )

        self.assertEqual(presentation.match_indexes, (0, 1))
        positions = [
            (segment.archive_search_match_index, segment.text)
            for segment in presentation.segments
        ]
        self.assertIn((0, "one"), positions)
        self.assertIn((1, "one"), positions)
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            "one two one",
        )

    def test_hebrew_displayed_result_selection(self):
        doc = create_viewable_ocr_document(
            title="Hebrew displayed",
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="source word",
        )
        hebrew = _result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="לפני מילה אחרי",
        )
        matches = (_match(hebrew, term="מילה", start=5, end=9),)

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(presentation.result_type, "HEBREW_TEXT")
        self.assertEqual(presentation.text_result_id, hebrew.pk)
        self.assertEqual(presentation.match_indexes, (0,))

    def test_non_displayed_result_gets_no_transcription_target(self):
        doc = create_viewable_ocr_document(
            title="Non displayed",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="source alpha",
        )
        translation = _result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="תרגום אלפא",
        )
        # Geometry match only on the translation surface (index 0).
        matches = (_match(translation, term="אלפא", start=6, end=10),)

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
        )

        self.assertFalse(presentation.enabled)
        self.assertEqual(presentation.segments, ())
        self.assertEqual(presentation.match_indexes, ())
        # Displayed transcription remains source.
        self.assertEqual(source.text, "source alpha")

    def test_invalid_offsets_fail_closed(self):
        doc = create_viewable_ocr_document(
            title="Invalid offsets",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="short",
        )
        matches = (
            _match(source, term="short", start=-1, end=2),
            _match(source, term="short", start=0, end=99),
            _match(source, term="short", start=3, end=3),
        )

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
        )

        self.assertFalse(presentation.enabled)

    def test_hover_and_search_segments_coexist_and_preserve_text(self):
        doc = create_viewable_ocr_document(
            title="Hover coexist",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Alpha\nBeta",
        )
        hover = TextLineHoverPresentation(
            enabled=True,
            result_type="SOURCE_TEXT",
            text_result_id=source.pk,
            segments=(
                TextLineHoverSegment(text="Alpha", hover_line_id="p1-o0"),
                TextLineHoverSegment(text="\n", hover_line_id=None),
                TextLineHoverSegment(text="Beta", hover_line_id="p1-o1"),
            ),
            overlay_targets=(),
        )
        matches = (_match(source, term="lph", start=1, end=4),)

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
            text_line_hover=hover,
        )

        self.assertTrue(presentation.enabled)
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            "Alpha\nBeta",
        )
        hover_ids = {
            segment.text: segment.hover_line_id for segment in presentation.segments
        }
        self.assertEqual(hover_ids["A"], "p1-o0")
        self.assertEqual(hover_ids["lph"], "p1-o0")
        self.assertEqual(hover_ids["a"], "p1-o0")
        self.assertEqual(hover_ids["\n"], None)
        self.assertEqual(hover_ids["Beta"], "p1-o1")
        match_piece = next(
            segment
            for segment in presentation.segments
            if segment.archive_search_match_index == 0
        )
        self.assertEqual(match_piece.text, "lph")
        self.assertEqual(match_piece.hover_line_id, "p1-o0")

    def test_match_crossing_hover_boundary_emits_multiple_spans_same_index(self):
        doc = create_viewable_ocr_document(
            title="Multi-span match",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Alpha\nBeta",
        )
        hover = TextLineHoverPresentation(
            enabled=True,
            result_type="SOURCE_TEXT",
            text_result_id=source.pk,
            segments=(
                TextLineHoverSegment(text="Alpha", hover_line_id="p1-o0"),
                TextLineHoverSegment(text="\n", hover_line_id=None),
                TextLineHoverSegment(text="Beta", hover_line_id="p1-o1"),
            ),
            overlay_targets=(),
        )
        # Match crosses the hover line boundary (Alpha + newline + Be).
        matches = (_match(source, term="Alpha\nBe", start=0, end=8),)

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=matches,
            text_line_hover=hover,
        )

        self.assertTrue(presentation.enabled)
        match_segments = [
            segment
            for segment in presentation.segments
            if segment.archive_search_match_index == 0
        ]
        self.assertGreaterEqual(len(match_segments), 2)
        self.assertEqual(
            "".join(segment.text for segment in match_segments),
            "Alpha\nBe",
        )
        self.assertEqual(
            "".join(segment.text for segment in presentation.segments),
            "Alpha\nBeta",
        )

    def test_no_geometry_matches_disables_presentation(self):
        doc = create_viewable_ocr_document(
            title="No matches",
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Alpha",
        )

        presentation = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=(),
        )

        self.assertFalse(presentation.enabled)


class ArchiveSearchTranscriptionDetailRenderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="archive_search_transcription_user",
            password="x",
        )

    @patch("documents.views.build_archive_search_transcription_presentation")
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
    def test_detail_html_includes_image_and_transcription_match_targets(
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
        mock_build_transcription,
    ):
        doc = create_viewable_ocr_document(
            title="Detail sync render",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        doc.file_s3_key = "documents/sync-render.png"
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
        mock_resolve.return_value = (object(),)
        mock_build_search_targets.return_value = (search_target,)
        mock_build_search_pages.return_value = (
            SimpleNamespace(page_index=1, targets=(search_target,)),
        )
        mock_apply_search_previews.return_value = []
        mock_build_hover.return_value = SimpleNamespace(
            enabled=False,
            result_type=None,
            text_result_id=None,
            segments=(),
            overlay_targets=(),
        )
        mock_build_hover_pages.return_value = ()
        mock_build_hover_single.return_value = None
        mock_apply_hover_previews.side_effect = lambda items, pages: items
        mock_build_transcription.return_value = SimpleNamespace(
            enabled=True,
            result_type="SOURCE_TEXT",
            text_result_id=1,
            match_indexes=(0,),
            segments=(
                SimpleNamespace(
                    text="Alpha",
                    hover_line_id=None,
                    archive_search_match_index=0,
                ),
                SimpleNamespace(
                    text="\nBeta",
                    hover_line_id=None,
                    archive_search_match_index=None,
                ),
            ),
        )

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
            'data-archive-search-transcription-match-index="0"',
            html=False,
        )
        self.assertContains(
            response,
            'role="button"',
            html=False,
        )
        self.assertContains(
            response,
            'tabindex="0"',
            html=False,
        )
        self.assertContains(
            response,
            "archive-search-transcription-match--active",
            html=False,
        )
        self.assertContains(
            response,
            "archive-search-overlay-target--active",
            html=False,
        )
        self.assertContains(
            response,
            "data-archive-search-match-previous",
            html=False,
        )
        self.assertContains(
            response,
            "data-archive-search-match-next",
            html=False,
        )
        body = response.content.decode("utf-8")
        self.assertIn("Alpha", body)
        self.assertIn("Beta", body)
        # Visible transcription text remains exact (no injected link chrome).
        self.assertIn(
            'data-archive-search-transcription-match-index="0">Alpha</span>',
            body,
        )

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.png",
    )
    def test_detail_without_search_query_has_no_transcription_targets(self, _presign):
        doc = create_viewable_ocr_document(
            title="No query sync",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Alpha Beta",
            source_revision=1,
        )

        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id}),
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["archive_search_transcription"].enabled)
        self.assertNotContains(
            response,
            "data-archive-search-transcription-match-index",
            html=False,
        )


class ArchiveSearchOverlayScrollContractTests(SimpleTestCase):
    """Source-level contract checks for bidirectional match scroll intents."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js_source = _archive_search_overlay_script_path().read_text(
            encoding="utf-8"
        )

    def test_previous_next_prefers_transcription_then_source_fallback(self):
        self.assertIn('scrollSide: "preferTranscription"', self.js_source)
        self.assertIn("scrollTranscriptionForMatch", self.js_source)
        self.assertIn("scrollSourceForMatch", self.js_source)
        self.assertIn(
            'if (scrollSide === "preferTranscription") {\n'
            "      if (scrollTranscriptionForMatch(matchValue)) {\n"
            "        return;\n"
            "      }\n"
            "      scrollSourceForMatch(matchValue);\n"
            "    }",
            self.js_source,
        )

    def test_source_overlay_click_requests_transcription_only_scroll(self):
        self.assertIn(
            'setActiveMatch(matchIndex, { scrollSide: "transcription" });',
            self.js_source,
        )
        self.assertIn(
            'if (scrollSide === "transcription") {\n'
            "      scrollTranscriptionForMatch(matchValue);\n"
            "      return;\n"
            "    }",
            self.js_source,
        )
        overlay_handler = self.js_source[
            self.js_source.index(
                "// Source overlay click: activate + scroll transcription only."
            ) : self.js_source.index(
                "// All spans sharing a match index are valid click sources"
            )
        ]
        self.assertIn('scrollSide: "transcription"', overlay_handler)
        self.assertNotIn('scrollSide: "source"', overlay_handler)
        self.assertNotIn('scrollSide: "preferTranscription"', overlay_handler)

    def test_transcription_match_click_requests_source_only_scroll(self):
        self.assertIn(
            'setActiveMatch(matchIndex, { scrollSide: "source" });',
            self.js_source,
        )
        self.assertIn(
            'if (scrollSide === "source") {\n'
            "      scrollSourceForMatch(matchValue);\n"
            "      return;\n"
            "    }",
            self.js_source,
        )
        # Transcription click path must not request transcription scroll.
        transcription_handler = self.js_source[
            self.js_source.index(
                "// All spans sharing a match index are valid click sources"
            ) : self.js_source.index("previousButton?.addEventListener")
        ]
        self.assertIn('scrollSide: "source"', transcription_handler)
        self.assertNotIn('scrollSide: "transcription"', transcription_handler)
        self.assertNotIn('scrollSide: "preferTranscription"', transcription_handler)

    def test_all_transcription_spans_are_valid_click_sources(self):
        # Handlers bind over every transcriptionTargets entry, not only the
        # first span retained in transcriptionTargetByMatchIndex for scrolling.
        self.assertIn(
            "transcriptionTargets.forEach((target) => {\n"
            "    const activateFromTranscription = () => {",
            self.js_source,
        )
        self.assertIn(
            'target.addEventListener("click", activateFromTranscription);',
            self.js_source,
        )

    def test_initial_set_active_match_uses_source_scroll_only_when_multipage(self):
        self.assertIn(
            "setActiveMatch(orderedMatchIndexes[0], {\n"
            '    scrollSide: pages.length > 1 ? "source" : "none",\n'
            "  });",
            self.js_source,
        )
        self.assertNotIn(
            'scrollSide: pages.length > 1 ? "preferTranscription"',
            self.js_source,
        )
        self.assertNotIn(
            'scrollSide: pages.length > 1 ? "transcription"',
            self.js_source,
        )

    def test_missing_opposite_side_does_not_guess_scroll_target(self):
        # Fail-closed: scroll helpers return false when no mapped target exists.
        self.assertIn(
            'if (!activeTarget || typeof activeTarget.scrollIntoView !== "function") {\n'
            "      return false;\n"
            "    }",
            self.js_source,
        )
        self.assertIn(
            "if (\n"
            "      !transcriptionTarget ||\n"
            '      typeof transcriptionTarget.scrollIntoView !== "function"\n'
            "    ) {\n"
            "      return false;\n"
            "    }",
            self.js_source,
        )
        # No DOM text search / query-string reconstruction of matches.
        self.assertNotIn(".textContent.indexOf", self.js_source)
        self.assertNotIn("new RegExp", self.js_source)
        self.assertNotIn('querySelectorAll("*"', self.js_source)

    def test_scroll_sides_are_mutually_exclusive_per_activation(self):
        # Each scrollSide branch returns after at most one scroll helper call.
        self.assertIn(
            'if (scrollSide === "none") {\n      return;\n    }', self.js_source
        )
        self.assertIn(
            'if (scrollSide === "transcription") {\n'
            "      scrollTranscriptionForMatch(matchValue);\n"
            "      return;\n"
            "    }",
            self.js_source,
        )
        self.assertIn(
            'if (scrollSide === "source") {\n'
            "      scrollSourceForMatch(matchValue);\n"
            "      return;\n"
            "    }",
            self.js_source,
        )
        # No reciprocal programmatic click that could loop.
        self.assertNotIn(".click()", self.js_source)

    def test_transcription_keyboard_activation_uses_enter_and_space(self):
        self.assertIn('event.key !== "Enter" && event.key !== " "', self.js_source)
        self.assertIn("event.preventDefault();", self.js_source)
        self.assertIn("activateFromTranscription();", self.js_source)

    def test_search_activation_does_not_clear_text_line_hover_classes(self):
        self.assertNotIn("text-line-hover-source--active", self.js_source)
        self.assertNotIn("text-line-hover-overlay-target--active", self.js_source)
