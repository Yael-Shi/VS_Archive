"""Public Transkribus paragraph rendering (PR2) tests."""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
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
from documents.services.archive_search_transcription_presentation import (
    ArchiveSearchTranscriptionPresentation,
    build_archive_search_transcription_presentation,
)
from documents.services.text_line_hover_presentation import (
    TextLineHoverPresentation,
    build_text_line_hover_presentation,
)
from documents.services.text_presentation import (
    DisplayTextBlock,
    TextBlockDisplayMeta,
)
from documents.services.transkribus_paragraph_mapping import save_paragraph_mapping
from documents.services.transkribus_paragraph_presentation import (
    build_transkribus_paragraph_presentation,
)
from documents.test_archive_item import create_viewable_ocr_document


_ENGINE = "transkribus-pylaia:42"
_THREE = "Alpha\nBeta\nGamma"
_PAGED = "Alpha\nBeta\n\nGamma"
_SPACED = "Aa  aa\nBb"
_HEBREW = "שלום  עולם\nשורה"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_from_paragraphs(presentation) -> str:
    return "".join(
        fragment.text
        for paragraph in presentation.paragraphs
        for fragment in paragraph.fragments
    )


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _text_from_html(html: str) -> str:
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)


def _reading_prose_inner_html(html: str) -> str:
    match = re.search(
        r'<div class="detail-text-block__body document-detail-reading-prose[^"]*"[^>]*>(.*?)</div>',
        html,
        flags=re.DOTALL,
    )
    if match is None:
        return ""
    return match.group(1)


def _match(
    text_result: DocumentTextResult,
    *,
    start: int,
    end: int,
    term: str = "x",
):
    return SimpleNamespace(
        term=term,
        text_result=text_result,
        start=start,
        end=end,
        geometry=(SimpleNamespace(page_index=1),),
    )


class _ParagraphFixtureMixin:
    def _create_doc(self, **kwargs) -> Document:
        defaults = dict(
            title="Paragraph render doc",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/paragraph-render/source.jpg",
            mime_type="image/jpeg",
            visibility=Document.Visibility.PUBLIC,
        )
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _ready_snapshot(
        self,
        document: Document,
        *,
        text: str,
        hover_eligible: bool = True,
    ) -> TranskribusTranscriptSnapshot:
        unique = f"{document.pk}:{TranskribusTranscriptSnapshot.objects.count()}:{text}"
        return TranskribusTranscriptSnapshot.objects.create(
            document=document,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint=_sha(f"prov:{unique}"),
            raw_xml_fingerprint=_sha(f"raw:{unique}"),
            canonical_text=text,
            canonical_text_sha256=_sha(text),
            geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
            hover_eligible=hover_eligible,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )

    def _add_page(
        self,
        snapshot: TranskribusTranscriptSnapshot,
        page_index: int,
    ) -> TranskribusSnapshotPage:
        return TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=page_index,
            page_nr=page_index,
            transcript_ts_id=f"ts-{snapshot.pk}-{page_index}",
            page_geometry_capability=TranskribusSnapshotPage.GeometryCapability.VERIFIED,
            image_width=1000,
            image_height=1500,
        )

    def _add_line(
        self,
        page: TranskribusSnapshotPage,
        order: int,
        text: str,
        *,
        start: int,
        end: int,
        y: int = 10,
    ) -> TranskribusSnapshotLine:
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
                [10.0, float(y)],
                [100.0, float(y)],
                [100.0, float(y + 10)],
                [10.0, float(y + 10)],
            ],
            bbox_min_x=10.0,
            bbox_min_y=float(y),
            bbox_max_x=100.0,
            bbox_max_y=float(y + 10),
            coords_valid=True,
            has_meaningful_geometry=True,
        )

    def _source_row(self, doc: Document, *, text: str) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            source_revision=1,
        )

    def _bind(
        self,
        *,
        text_result: DocumentTextResult,
        snapshot: TranskribusTranscriptSnapshot,
    ) -> TranskribusTextResultBinding:
        return TranskribusTextResultBinding.objects.create(
            text_result=text_result,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=_sha(text_result.text or ""),
            bound_source_revision=1,
        )

    def _three_line_bound(self, *, text: str = _THREE, hover_eligible: bool = True):
        doc = self._create_doc()
        snapshot = self._ready_snapshot(doc, text=text, hover_eligible=hover_eligible)
        page = self._add_page(snapshot, 1)
        parts = text.split("\n")
        # Single-page three-line "Alpha\nBeta\nGamma"
        alpha = self._add_line(page, 0, parts[0], start=0, end=len(parts[0]), y=10)
        beta_start = len(parts[0]) + 1
        beta = self._add_line(
            page,
            1,
            parts[1],
            start=beta_start,
            end=beta_start + len(parts[1]),
            y=30,
        )
        gamma_start = beta_start + len(parts[1]) + 1
        gamma = self._add_line(
            page,
            2,
            parts[2],
            start=gamma_start,
            end=gamma_start + len(parts[2]),
            y=50,
        )
        result = self._source_row(doc, text=text)
        binding = self._bind(text_result=result, snapshot=snapshot)
        return doc, snapshot, result, binding, alpha, beta, gamma

    def _paged_bound(self, *, hover_eligible: bool = True):
        text = _PAGED
        doc = self._create_doc()
        snapshot = self._ready_snapshot(doc, text=text, hover_eligible=hover_eligible)
        page1 = self._add_page(snapshot, 1)
        page2 = self._add_page(snapshot, 2)
        alpha = self._add_line(page1, 0, "Alpha", start=0, end=5, y=10)
        beta = self._add_line(page1, 1, "Beta", start=6, end=10, y=30)
        gamma = self._add_line(page2, 0, "Gamma", start=12, end=17, y=10)
        result = self._source_row(doc, text=text)
        binding = self._bind(text_result=result, snapshot=snapshot)
        return doc, snapshot, result, binding, alpha, beta, gamma

    def _hover(
        self,
        doc: Document,
        *,
        source_preview_items: list[dict] | None = None,
    ) -> TextLineHoverPresentation:
        return build_text_line_hover_presentation(
            doc,
            source_preview_items=list(source_preview_items or []),
            content_url="https://example.test/source.jpg",
        )

    def _paragraphs(
        self,
        doc: Document,
        *,
        hover: TextLineHoverPresentation | None = None,
        search: ArchiveSearchTranscriptionPresentation | None = None,
        source_preview_items: list[dict] | None = None,
    ):
        if hover is None and search is None:
            hover = self._hover(doc, source_preview_items=source_preview_items)
        return build_transkribus_paragraph_presentation(
            doc,
            text_line_hover=hover,
            archive_search_transcription=search,
        )

    def _render_block(
        self,
        doc: Document,
        *,
        text: str,
        result_type: str = "SOURCE_TEXT",
        paragraph=None,
        hover=None,
        search=None,
    ) -> str:
        block = DisplayTextBlock(
            result_type=result_type,  # type: ignore[arg-type]
            text=text,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            created_at="2026-01-01T00:00:00",
            engine=_ENGINE,
        )
        meta = TextBlockDisplayMeta(
            label="תעתוק מקור",
            description="",
            empty_message="",
        )
        return render_to_string(
            "documents/partials/detail_text_block.html",
            {
                "block": block,
                "meta": meta,
                "doc": doc,
                "is_admin": False,
                "transkribus_paragraph_presentation": paragraph,
                "text_line_hover": hover,
                "archive_search_transcription": search,
            },
        )


class ParagraphPresentationFallbackTests(_ParagraphFixtureMixin, TestCase):
    def test_no_mapping_disables_paragraph_overlay(self):
        doc, *_rest = self._three_line_bound()
        presentation = self._paragraphs(doc)
        self.assertFalse(presentation.enabled)
        self.assertEqual(presentation.paragraphs, ())

    def test_stale_other_snapshot_mapping_is_ignored(self):
        doc, snapshot, result, binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        new_text = "New\nBeta\nGamma"
        new_snapshot = self._ready_snapshot(doc, text=new_text)
        new_page = self._add_page(new_snapshot, 1)
        self._add_line(new_page, 0, "New", start=0, end=3, y=10)
        self._add_line(new_page, 1, "Beta", start=4, end=8, y=30)
        self._add_line(new_page, 2, "Gamma", start=9, end=14, y=50)
        result.text = new_text
        result.save(update_fields=["text", "updated_at"])
        binding.snapshot = new_snapshot
        binding.bound_text_sha256 = _sha(new_text)
        binding.save(update_fields=["snapshot", "bound_text_sha256"])

        presentation = self._paragraphs(doc)
        self.assertFalse(presentation.enabled)

    def test_locally_drifted_text_disables_paragraph_overlay(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        result.text = "Alpha\nBeta\nGamma edited"
        result.source_revision = 2
        result.save(update_fields=["text", "source_revision", "updated_at"])

        presentation = self._paragraphs(doc)
        self.assertFalse(presentation.enabled)

    def test_no_mapping_template_matches_legacy_hover_markup(self):
        doc, *_rest = self._three_line_bound()
        hover = self._hover(doc)
        self.assertTrue(hover.enabled)
        disabled = self._paragraphs(doc, hover=hover)
        self.assertFalse(disabled.enabled)
        html = self._render_block(
            doc,
            text=_THREE,
            hover=hover,
            paragraph=disabled,
        )
        inner = _reading_prose_inner_html(html)
        self.assertNotIn("document-detail-reading-prose--paragraphs", html)
        self.assertNotIn("document-detail-transcription-paragraph", inner)
        self.assertIn('data-text-line-hover-id="p1-o0"', inner)
        self.assertIn(">Alpha</span>", inner)
        self.assertIn("\n", inner)
        self.assertEqual(_text_from_html(inner), _THREE)

    def test_stale_mapping_template_matches_no_mapping_html(self):
        doc, snapshot, result, binding, alpha, _beta, _gamma = self._three_line_bound()
        hover = self._hover(doc)
        html_before = self._render_block(
            doc,
            text=_THREE,
            hover=hover,
            paragraph=self._paragraphs(doc, hover=hover),
        )
        other = self._ready_snapshot(doc, text="Other\nBeta\nGamma")
        other_page = self._add_page(other, 1)
        other_alpha = self._add_line(other_page, 0, "Other", start=0, end=5, y=10)
        self._add_line(other_page, 1, "Beta", start=6, end=10, y=30)
        self._add_line(other_page, 2, "Gamma", start=11, end=16, y=50)
        save_paragraph_mapping(other, [other_alpha.pk])
        html_after = self._render_block(
            doc,
            text=_THREE,
            hover=hover,
            paragraph=self._paragraphs(doc, hover=hover),
        )
        self.assertEqual(
            _reading_prose_inner_html(html_before),
            _reading_prose_inner_html(html_after),
        )
        self.assertFalse(self._paragraphs(doc, hover=hover).enabled)
        self.assertEqual(binding.snapshot_id, snapshot.pk)
        self.assertEqual(result.text, _THREE)


class ParagraphPresentationGroupingTests(_ParagraphFixtureMixin, TestCase):
    def test_zero_break_mapping_is_one_flowing_paragraph(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = (
            self._three_line_bound()
        )
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc)
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 1)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)
        hover_ids = [
            fragment.hover_line_id
            for fragment in presentation.paragraphs[0].fragments
            if fragment.hover_line_id
        ]
        self.assertEqual(hover_ids, ["p1-o0", "p1-o1", "p1-o2"])

    def test_hover_ineligible_current_mapping_still_activates_one_paragraph(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = self._three_line_bound(
            hover_eligible=False,
        )
        save_paragraph_mapping(snapshot, [])
        hover = self._hover(doc)
        self.assertFalse(hover.enabled)
        presentation = self._paragraphs(doc, hover=hover)
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 1)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)
        html = self._render_block(doc, text=result.text, paragraph=presentation)
        inner = _reading_prose_inner_html(html)
        self.assertEqual(inner.count("document-detail-transcription-paragraph"), 1)
        self.assertNotIn("data-text-line-hover-id", inner)
        self.assertEqual(_text_from_html(inner), result.text)

    def test_one_break_yields_two_paragraphs(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        presentation = self._paragraphs(doc)
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 2)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)
        first = "".join(
            fragment.text for fragment in presentation.paragraphs[0].fragments
        )
        second = "".join(
            fragment.text for fragment in presentation.paragraphs[1].fragments
        )
        self.assertTrue(first.startswith("Alpha"))
        self.assertTrue(second.startswith("Beta"))
        self.assertEqual(first + second, result.text)

    def test_multiple_breaks_yield_expected_grouping(self):
        doc, snapshot, result, _binding, alpha, beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk, beta.pk])
        presentation = self._paragraphs(doc)
        self.assertEqual(len(presentation.paragraphs), 3)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)
        texts = [
            "".join(fragment.text for fragment in paragraph.fragments).strip("\n")
            for paragraph in presentation.paragraphs
        ]
        self.assertEqual(texts, ["Alpha", "Beta", "Gamma"])


_PAGE_PREVIEWS = [
    {"display_number": 1, "url": "https://example.test/p1.jpg"},
    {"display_number": 2, "url": "https://example.test/p2.jpg"},
]


class ParagraphPresentationPageTests(_ParagraphFixtureMixin, TestCase):
    def test_page_boundary_alone_does_not_create_paragraph(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = self._paged_bound()
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc, source_preview_items=_PAGE_PREVIEWS)
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 1)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)
        self.assertIn("\n\n", _canonical_from_paragraphs(presentation))
        hover_ids = [
            fragment.hover_line_id
            for fragment in presentation.paragraphs[0].fragments
            if fragment.hover_line_id
        ]
        self.assertEqual(hover_ids, ["p1-o0", "p1-o1", "p2-o0"])

    def test_paragraph_crosses_page_when_no_break_after_page1(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._paged_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        presentation = self._paragraphs(doc, source_preview_items=_PAGE_PREVIEWS)
        self.assertEqual(len(presentation.paragraphs), 2)
        second = "".join(
            fragment.text for fragment in presentation.paragraphs[1].fragments
        )
        self.assertIn("Beta", second)
        self.assertIn("Gamma", second)
        self.assertIn("\n\n", second)
        second_ids = [
            fragment.hover_line_id
            for fragment in presentation.paragraphs[1].fragments
            if fragment.hover_line_id
        ]
        self.assertEqual(second_ids, ["p1-o1", "p2-o0"])
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)

    def test_explicit_break_near_page_transition(self):
        doc, snapshot, result, _binding, _alpha, beta, _gamma = self._paged_bound()
        save_paragraph_mapping(snapshot, [beta.pk])
        presentation = self._paragraphs(doc)
        self.assertEqual(len(presentation.paragraphs), 2)
        first = "".join(
            fragment.text for fragment in presentation.paragraphs[0].fragments
        )
        second = "".join(
            fragment.text for fragment in presentation.paragraphs[1].fragments
        )
        self.assertIn("Alpha", first)
        self.assertIn("Beta", first)
        self.assertEqual(second.strip("\n"), "Gamma")
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)


class ParagraphPresentationCanonicalTests(_ParagraphFixtureMixin, TestCase):
    def test_rendered_html_reconstructs_canonical_including_separators(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        presentation = self._paragraphs(doc)
        html = self._render_block(
            doc,
            text=result.text,
            paragraph=presentation,
            hover=self._hover(doc),
        )
        inner = _reading_prose_inner_html(html)
        reconstructed = _text_from_html(inner)
        self.assertEqual(reconstructed, result.text)
        self.assertIn("\n", reconstructed)
        self.assertNotIn("document-detail-reading-prose--paragraphs", inner)
        self.assertIn("document-detail-reading-prose--paragraphs", html)
        self.assertNotIn("Alpha Beta", reconstructed)
        self.assertEqual(reconstructed.count("\n"), 2)

    def test_page_separator_newlines_are_preserved_exactly(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = self._paged_bound()
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc)
        html = self._render_block(doc, text=result.text, paragraph=presentation)
        reconstructed = _text_from_html(_reading_prose_inner_html(html))
        self.assertEqual(reconstructed, "Alpha\nBeta\n\nGamma")
        self.assertIn("\n\n", reconstructed)
        self.assertNotIn("Beta  Gamma", reconstructed)
        self.assertNotIn("Beta Gamma", reconstructed)

    def test_no_replacement_spaces_inserted_into_canonical_text(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        presentation = self._paragraphs(doc)
        joined = _canonical_from_paragraphs(presentation)
        self.assertEqual(joined, result.text)
        self.assertNotIn("Alpha Beta", joined)


class ParagraphPresentationHoverTests(_ParagraphFixtureMixin, TestCase):
    def test_source_line_hover_ids_preserved_inside_one_paragraph(self):
        doc, snapshot, _result, _binding, _alpha, _beta, _gamma = (
            self._three_line_bound()
        )
        save_paragraph_mapping(snapshot, [])
        hover = self._hover(doc)
        presentation = self._paragraphs(doc, hover=hover)
        html = self._render_block(
            doc,
            text=_THREE,
            paragraph=presentation,
            hover=hover,
        )
        inner = _reading_prose_inner_html(html)
        self.assertEqual(
            re.findall(r'data-text-line-hover-id="([^"]+)"', inner),
            ["p1-o0", "p1-o1", "p1-o2"],
        )
        self.assertEqual(inner.count("document-detail-transcription-paragraph"), 1)
        self.assertIn("text-line-hover-source", inner)

    def test_wrapped_source_line_markup_stays_source_line_scoped(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = (
            self._three_line_bound()
        )
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc)
        line_fragments = [
            fragment
            for fragment in presentation.paragraphs[0].fragments
            if fragment.hover_line_id == "p1-o0"
        ]
        self.assertEqual(len(line_fragments), 1)
        self.assertTrue(line_fragments[0].is_source_line)
        self.assertEqual(line_fragments[0].text, "Alpha")
        self.assertEqual(result.text, _THREE)


class ParagraphPresentationSearchTests(_ParagraphFixtureMixin, TestCase):
    def test_match_wholly_inside_one_source_line(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        hover = self._hover(doc)
        search = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=(_match(result, start=1, end=4, term="lph"),),
            text_line_hover=hover,
        )
        presentation = self._paragraphs(doc, hover=hover, search=search)
        self.assertTrue(presentation.enabled)
        self.assertEqual(search.match_indexes, (0,))
        match_fragments = [
            fragment
            for paragraph in presentation.paragraphs
            for fragment in paragraph.fragments
            if fragment.archive_search_match_index == 0
        ]
        self.assertEqual([fragment.text for fragment in match_fragments], ["lph"])
        self.assertEqual(match_fragments[0].hover_line_id, "p1-o0")
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)

        html = self._render_block(
            doc,
            text=result.text,
            paragraph=presentation,
            hover=hover,
            search=search,
        )
        inner = _reading_prose_inner_html(html)
        self.assertIn('data-archive-search-transcription-match-index="0"', inner)
        self.assertIn('data-text-line-hover-id="p1-o0"', inner)
        self.assertEqual(
            re.findall(r'data-archive-search-transcription-match-index="(\d+)"', inner),
            ["0"],
        )

    def test_match_across_source_line_separator(self):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = (
            self._three_line_bound()
        )
        save_paragraph_mapping(snapshot, [])
        hover = self._hover(doc)
        search = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=(_match(result, start=0, end=8, term="Alpha\nBe"),),
            text_line_hover=hover,
        )
        presentation = self._paragraphs(doc, hover=hover, search=search)
        match_indexes = [
            fragment.archive_search_match_index
            for fragment in presentation.paragraphs[0].fragments
            if fragment.archive_search_match_index is not None
        ]
        self.assertEqual(set(match_indexes), {0})
        self.assertGreater(len(match_indexes), 1)
        self.assertEqual(_canonical_from_paragraphs(presentation), result.text)

    def test_matches_in_separate_paragraphs_keep_order(self):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        hover = self._hover(doc)
        search = build_archive_search_transcription_presentation(
            doc,
            geometry_matches=(
                _match(result, start=0, end=5, term="Alpha"),
                _match(result, start=11, end=16, term="Gamma"),
            ),
            text_line_hover=hover,
        )
        presentation = self._paragraphs(doc, hover=hover, search=search)
        ordered = [
            fragment.archive_search_match_index
            for paragraph in presentation.paragraphs
            for fragment in paragraph.fragments
            if fragment.archive_search_match_index is not None
        ]
        self.assertEqual(ordered, [0, 1])
        first_para_indexes = {
            fragment.archive_search_match_index
            for fragment in presentation.paragraphs[0].fragments
            if fragment.archive_search_match_index is not None
        }
        second_para_indexes = {
            fragment.archive_search_match_index
            for fragment in presentation.paragraphs[1].fragments
            if fragment.archive_search_match_index is not None
        }
        self.assertEqual(first_para_indexes, {0})
        self.assertEqual(second_para_indexes, {1})

        html = self._render_block(
            doc,
            text=result.text,
            paragraph=presentation,
            search=search,
        )
        inner = _reading_prose_inner_html(html)
        self.assertEqual(
            re.findall(r'data-archive-search-transcription-match-index="(\d+)"', inner),
            ["0", "1"],
        )
        self.assertIn('role="button"', inner)
        self.assertIn("archive-search-transcription-match", inner)


class ParagraphPresentationWhitespaceTests(_ParagraphFixtureMixin, TestCase):
    def test_intra_line_repeated_spaces_are_kept_on_source_line_span(self):
        text = _SPACED
        doc = self._create_doc()
        snapshot = self._ready_snapshot(doc, text=text)
        page = self._add_page(snapshot, 1)
        self._add_line(page, 0, "Aa  aa", start=0, end=6, y=10)
        self._add_line(page, 1, "Bb", start=7, end=9, y=30)
        result = self._source_row(doc, text=text)
        self._bind(text_result=result, snapshot=snapshot)
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc)
        self.assertEqual(_canonical_from_paragraphs(presentation), "Aa  aa\nBb")
        line = next(
            fragment
            for fragment in presentation.paragraphs[0].fragments
            if fragment.is_source_line and "Aa" in fragment.text
        )
        self.assertEqual(line.text, "Aa  aa")
        html = self._render_block(doc, text=text, paragraph=presentation)
        inner = _reading_prose_inner_html(html)
        self.assertIn("Aa  aa", inner)
        self.assertIn("document-detail-source-line", inner)
        self.assertIn("document-detail-canonical-separator", inner)
        self.assertEqual(_text_from_html(inner), text)

    def test_hebrew_rtl_structure_is_preserved(self):
        text = _HEBREW
        doc = self._create_doc()
        snapshot = self._ready_snapshot(doc, text=text)
        page = self._add_page(snapshot, 1)
        first, _newline, second = text.partition("\n")
        self._add_line(page, 0, first, start=0, end=len(first), y=10)
        self._add_line(
            page,
            1,
            second,
            start=len(first) + 1,
            end=len(text),
            y=30,
        )
        result = self._source_row(doc, text=text)
        self._bind(text_result=result, snapshot=snapshot)
        save_paragraph_mapping(snapshot, [])
        presentation = self._paragraphs(doc)
        html = self._render_block(doc, text=text, paragraph=presentation)
        self.assertIn("detail-text-block__body--rtl", html)
        self.assertIn("document-detail-reading-prose--paragraphs", html)
        inner = _reading_prose_inner_html(html)
        self.assertEqual(_text_from_html(inner), text)
        self.assertIn("שלום  עולם", inner)
        self.assertEqual(inner.count("document-detail-transcription-paragraph"), 1)


class ParagraphPresentationNonTranskribusTests(_ParagraphFixtureMixin, TestCase):
    def test_gemini_path_does_not_activate_paragraphs(self):
        doc = create_viewable_ocr_document(
            title="Gemini printed",
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-test",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Hello\nWorld",
            source_revision=1,
        )
        presentation = build_transkribus_paragraph_presentation(doc)
        self.assertFalse(presentation.enabled)
        html = self._render_block(
            doc,
            text="Hello\nWorld",
            paragraph=presentation,
        )
        inner = _reading_prose_inner_html(html)
        self.assertNotIn("document-detail-transcription-paragraph", inner)
        self.assertNotIn("document-detail-reading-prose--paragraphs", html)
        self.assertEqual(_text_from_html(inner), "Hello\nWorld")


class ParagraphPresentationQueryTests(_ParagraphFixtureMixin, TestCase):
    def _fixture_with_counts(self, *, n_lines: int, n_breaks: int):
        words = [f"L{index:02d}" for index in range(n_lines)]
        text = "\n".join(words)
        doc = self._create_doc(title=f"Query {n_lines}")
        snapshot = self._ready_snapshot(doc, text=text)
        page = self._add_page(snapshot, 1)
        cursor = 0
        line_ids: list[int] = []
        for index, word in enumerate(words):
            line = self._add_line(
                page,
                index,
                word,
                start=cursor,
                end=cursor + len(word),
                y=10 + index * 12,
            )
            line_ids.append(line.pk)
            cursor = cursor + len(word) + 1
        result = self._source_row(doc, text=text)
        self._bind(text_result=result, snapshot=snapshot)
        save_paragraph_mapping(snapshot, line_ids[:n_breaks])
        return doc

    def _query_counts(self, *, n_lines: int, n_breaks: int) -> tuple[int, int, int]:
        doc = self._fixture_with_counts(n_lines=n_lines, n_breaks=n_breaks)
        with CaptureQueriesContext(connection) as context:
            presentation = build_transkribus_paragraph_presentation(doc)
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), n_breaks + 1)
        sql = [query["sql"] for query in context.captured_queries]
        line_queries = sum(
            1 for item in sql if "documents_transkribussnapshotline" in item
        )
        break_queries = sum(
            1 for item in sql if "documents_transkribusparagraphbreak" in item
        )
        return len(context), line_queries, break_queries

    def test_query_count_does_not_grow_with_lines_or_breaks(self):
        total_small, lines_small, breaks_small = self._query_counts(
            n_lines=3,
            n_breaks=1,
        )
        total_large, lines_large, breaks_large = self._query_counts(
            n_lines=40,
            n_breaks=20,
        )
        self.assertEqual(lines_small, 1)
        self.assertEqual(lines_large, 1)
        self.assertEqual(breaks_small, 1)
        self.assertEqual(breaks_large, 1)
        self.assertEqual(total_small, total_large)
        # Bounded lookup: displayed result + binding + mapping + lines + breaks.
        # Far below a per-line/per-break N+1 path.
        self.assertLessEqual(total_large, 14)


class ParagraphPresentationDetailViewTests(_ParagraphFixtureMixin, TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="paragraph-render-user",
            password="x",
        )

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.jpg",
    )
    def test_detail_view_uses_legacy_markup_without_mapping(self, _presign):
        doc, *_rest = self._three_line_bound()
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["transkribus_paragraph_presentation"].enabled)
        body = response.content.decode("utf-8")
        self.assertNotIn("document-detail-transcription-paragraph", body)
        self.assertNotIn("document-detail-reading-prose--paragraphs", body)
        self.assertIn('data-text-line-hover-id="p1-o0"', body)

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.jpg",
    )
    def test_detail_view_renders_paragraphs_for_current_mapping(self, _presign):
        doc, snapshot, result, _binding, alpha, _beta, _gamma = self._three_line_bound()
        save_paragraph_mapping(snapshot, [alpha.pk])
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(response.status_code, 200)
        presentation = response.context["transkribus_paragraph_presentation"]
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 2)
        body = response.content.decode("utf-8")
        self.assertIn("document-detail-reading-prose--paragraphs", body)
        self.assertIn("document-detail-transcription-paragraph", body)
        inner = _reading_prose_inner_html(body)
        self.assertEqual(_text_from_html(inner), result.text)
        self.assertEqual(
            re.findall(r'data-text-line-hover-id="([^"]+)"', inner),
            ["p1-o0", "p1-o1", "p1-o2"],
        )

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.jpg",
    )
    def test_zero_break_mapping_is_not_legacy_fallback_in_detail(self, _presign):
        doc, snapshot, result, _binding, _alpha, _beta, _gamma = (
            self._three_line_bound()
        )
        save_paragraph_mapping(snapshot, [])
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        presentation = response.context["transkribus_paragraph_presentation"]
        self.assertTrue(presentation.enabled)
        self.assertEqual(len(presentation.paragraphs), 1)
        inner = _reading_prose_inner_html(response.content.decode("utf-8"))
        self.assertEqual(inner.count("document-detail-transcription-paragraph"), 1)
        self.assertEqual(_text_from_html(inner), result.text)

    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/gemini.jpg",
    )
    def test_gemini_detail_is_unchanged(self, _presign):
        doc = create_viewable_ocr_document(
            title="Gemini detail",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        doc.file_s3_key = "documents/gemini-detail.jpg"
        doc.save(update_fields=["file_s3_key"])
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-test",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Hello\nWorld",
            source_revision=1,
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["transkribus_paragraph_presentation"].enabled)
        body = response.content.decode("utf-8")
        self.assertNotIn("document-detail-transcription-paragraph", body)
        self.assertIn("Hello", body)
        self.assertIn("World", body)
