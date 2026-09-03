"""Public OCR/MANUAL_TEXT quality indicator presentation (PR2)."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, Document, DocumentTextResult
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.text_presentation import get_text_presentation_for_document
from documents.services.text_quality import (
    HUMAN_VERIFIED,
    NEEDS_CORRECTION,
    PUBLIC_TEXT_QUALITY_HEADING,
    PUBLIC_TEXT_QUALITY_LABELS,
)
from documents.services.text_quality_presentation import (
    TEXT_QUALITY_TOOLTIP_FOOTER,
    TEXT_QUALITY_TOOLTIP_INTRO,
    TEXT_QUALITY_TOOLTIP_TITLE,
    TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE,
    public_text_quality_indicator_for_manual_text,
    public_text_quality_indicator_for_result,
)


@override_settings(UPLOADS_BUCKET_NAME="")
class TextQualityPublicUiTests(TestCase):
    def _create_doc(self, *, language: str = Document.Language.ENGLISH) -> Document:
        return create_ocr_document(
            title="Quality public UI doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=language,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/quality-ui/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_result(
        self,
        doc: Document,
        *,
        result_type: str,
        quality: str = DocumentTextResult.Quality.UNKNOWN,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
        status: str = DocumentTextResult.Status.NEEDS_REVIEW,
        text: str | None = "displayed text",
        engine: str = "engine-quality-ui",
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=status,
            verification_status=verification_status,
            quality=quality,
            text=text,
        )

    def _detail(self, doc: Document):
        return self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )

    def _html(self, doc: Document) -> str:
        response = self._detail(doc)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def _panel(self, html: str, panel_id: str, *, until_id: str | None = None) -> str:
        marker = f'id="{panel_id}"'
        start = html.index(marker)
        if until_id:
            end = html.index(f'id="{until_id}"', start + 1)
            return html[start:end]
        return html[start:]

    def _top_meta_html(self, html: str) -> str:
        marker = 'class="document-detail-top-meta"'
        start = html.index(marker)
        source_panel = html.find('id="document-detail-source-panel"', start)
        if source_panel != -1:
            return html[start:source_panel]
        end = html.index("</div>", start)
        return html[start:end]

    def _text_blocks_html(self, html: str) -> str:
        start = html.find('class="document-detail-text-content"')
        if start == -1:
            return html
        return html[start:]

    def _badge_html(self, html: str) -> str:
        marker = 'class="text-quality-indicator__badge '
        start = html.index(marker)
        wrap = html.index('class="text-quality-indicator__info-wrap"', start)
        return html[start:wrap]

    def test_unknown_renders_hebrew_not_evaluated_label(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.UNKNOWN,
        )
        html = self._html(doc)
        nav = self._top_meta_html(html)
        self.assertIn("טרם הוערך", nav)
        self.assertIn("text-quality-indicator__badge--unknown", nav)
        self.assertIn(f"{PUBLIC_TEXT_QUALITY_HEADING}: טרם הוערך", nav)
        self.assertIn('class="text-quality-indicator__context"', html)
        self.assertIn(">התעתוק:<", html)
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))
        self.assertNotIn('class="document-detail-jump-nav"', html)
        self.assertNotIn('aria-label="קפיצה לחלקי העמוד"', html)

    def test_low_medium_good_render_correct_labels(self):
        cases = (
            (DocumentTextResult.Quality.LOW, "איכות נמוכה", "low"),
            (DocumentTextResult.Quality.MEDIUM, "איכות בינונית", "medium"),
            (DocumentTextResult.Quality.GOOD, "איכות טובה", "good"),
        )
        for quality, label, modifier in cases:
            with self.subTest(quality=quality):
                doc = self._create_doc()
                self._create_result(
                    doc,
                    result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                    quality=quality,
                    engine=f"engine-{quality.lower()}",
                )
                html = self._html(doc)
                nav = self._top_meta_html(html)
                self.assertIn(label, nav)
                self.assertIn(f"text-quality-indicator__badge--{modifier}", nav)
                self.assertIn(f"{PUBLIC_TEXT_QUALITY_HEADING}: {label}", nav)
                self.assertNotIn('class="text-quality-indicator__heading"', html)
                self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))

    def test_verified_renders_human_verified_label(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        html = self._html(doc)
        badge = self._badge_html(html)
        self.assertIn("text-quality-indicator__badge--human-verified", badge)
        self.assertIn("נבדק ואושר", badge)
        self.assertIn("text-quality-indicator", self._top_meta_html(html))
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))

    def test_rejected_renders_needs_correction_not_low(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        html = self._html(doc)
        badge = self._badge_html(html)
        self.assertIn("text-quality-indicator__badge--needs-correction", badge)
        self.assertIn("נדרש תיקון", badge)
        self.assertIn("text-quality-indicator", self._top_meta_html(html))
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))

    def test_non_hebrew_source_and_hebrew_has_one_transcription_indicator_and_note(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            text="English source transcription",
            engine="engine-pair",
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            text="תרגום עברי",
            engine="engine-pair",
        )
        html = self._html(doc)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        nav = self._top_meta_html(html)
        self.assertIn("text-quality-indicator", nav)
        self.assertIn("text-quality-indicator__badge--good", nav)
        self.assertIn("איכות טובה", self._badge_html(html))
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        source = self._panel(
            html,
            "document-detail-source-text",
            until_id="document-detail-hebrew-text",
        )
        hebrew = self._panel(html, "document-detail-hebrew-text")
        self.assertIn("תעתוק", source)
        self.assertNotIn("text-quality-indicator", source)
        self.assertNotIn("text-quality-indicator", hebrew)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))
        self.assertIn("תרגום עברי", hebrew)
        self.assertIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)

        presentation = get_text_presentation_for_document(doc)
        self.assertTrue(presentation.show_source)
        self.assertTrue(presentation.show_hebrew)
        self.assertEqual(presentation.source.quality_indicator.quality, "GOOD")
        self.assertEqual(
            presentation.source.quality_indicator.tooltip_translation_note,
            TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE,
        )
        self.assertIsNone(presentation.hebrew.quality_indicator)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_get",
        return_value="https://example.test/source.jpg",
    )
    def test_jump_nav_and_quality_are_siblings_when_nav_exists(self, _mock_get):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            text="English source transcription",
            engine="engine-nav-pair",
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            text="תרגום עברי",
            engine="engine-nav-pair",
        )
        html = self._html(doc)
        meta = self._top_meta_html(html)
        self.assertIn('class="document-detail-jump-nav"', meta)
        self.assertIn("text-quality-indicator", meta)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        nav_start = html.index('class="document-detail-jump-nav"')
        nav = html[nav_start : html.index("</nav>", nav_start)]
        self.assertNotIn("text-quality-indicator", nav)
        self.assertIn('aria-label="קפיצה לחלקי העמוד"', html)

    def test_quality_indicator_absent_when_no_displayable_transcription(self):
        empty_doc = self._create_doc()
        empty_html = self._html(empty_doc)
        self.assertNotIn("text-quality-indicator", empty_html)
        self.assertNotIn(PUBLIC_TEXT_QUALITY_HEADING, empty_html)

        failed_doc = self._create_doc()
        self._create_result(
            failed_doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            status=DocumentTextResult.Status.FAILED,
            text="",
            engine="engine-failed-source",
        )
        self._create_result(
            failed_doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            status=DocumentTextResult.Status.FAILED,
            text="",
            engine="engine-failed-hebrew",
        )
        failed_html = self._html(failed_doc)
        self.assertNotIn("text-quality-indicator", failed_html)
        self.assertNotIn(PUBLIC_TEXT_QUALITY_HEADING, failed_html)

    def test_non_hebrew_hebrew_text_alone_is_translation_not_transcription(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            text="רק תרגום",
        )
        html = self._html(doc)
        self.assertIn("רק תרגום", html)
        self.assertNotIn("text-quality-indicator", html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        presentation = get_text_presentation_for_document(doc)
        self.assertTrue(presentation.show_source)
        self.assertIsNone(presentation.source)
        self.assertIsNone(presentation.hebrew.quality_indicator)

    def test_hebrew_language_displayed_hebrew_transcription_has_indicator_without_note(
        self,
    ):
        doc = self._create_doc(language=Document.Language.HEBREW)
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            text="מקור",
            engine="engine-he",
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            quality=DocumentTextResult.Quality.MEDIUM,
            text="תעתוק עברי מוצג",
            engine="engine-he",
        )
        html = self._html(doc)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        nav = self._top_meta_html(html)
        self.assertIn("text-quality-indicator", nav)
        self.assertIn("איכות בינונית", nav)
        self.assertIn("text-quality-indicator__badge--medium", nav)
        self.assertNotIn("text-quality-indicator__badge--good", html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        self.assertNotIn('id="document-detail-source-text"', html)
        self.assertIn("תעתוק עברי מוצג", html)
        hebrew = self._panel(html, "document-detail-hebrew-text")
        self.assertNotIn("text-quality-indicator", hebrew)
        self.assertIn("תעתוק", hebrew)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))

        presentation = get_text_presentation_for_document(doc)
        self.assertFalse(presentation.show_source)
        self.assertTrue(presentation.show_hebrew)
        self.assertIsNone(presentation.source.quality_indicator)
        self.assertEqual(presentation.hebrew.quality_indicator.quality, "MEDIUM")
        self.assertEqual(
            presentation.hebrew.quality_indicator.tooltip_translation_note, ""
        )

    def test_hebrew_language_source_fallback_transcription_has_indicator_without_note(
        self,
    ):
        doc = self._create_doc(language=Document.Language.HEBREW)
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            text="תעתוק מקור נופל",
        )
        html = self._html(doc)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        nav = self._top_meta_html(html)
        self.assertIn("text-quality-indicator", nav)
        self.assertIn("איכות נמוכה", self._badge_html(html))
        source = self._panel(html, "document-detail-source-text")
        self.assertNotIn("text-quality-indicator", source)
        self.assertNotIn('id="document-detail-hebrew-text"', html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        self.assertNotIn('class="text-quality-indicator__heading"', html)
        self.assertNotIn("text-quality-indicator", self._text_blocks_html(html))

        presentation = get_text_presentation_for_document(doc)
        self.assertTrue(presentation.show_source)
        self.assertFalse(presentation.show_hebrew)
        self.assertEqual(presentation.source.quality_indicator.quality, "LOW")
        self.assertIsNone(presentation.hebrew)

    def test_manual_text_renders_human_verified(self):
        item = create_manual_text_archive_item(
            title="Manual quality UI",
            body="Staff-entered public body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        response = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("נבדק ואושר", html)
        self.assertIn("text-quality-indicator__badge--human-verified", html)
        self.assertIn(f"{PUBLIC_TEXT_QUALITY_HEADING}: נבדק ואושר", html)
        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        self.assertNotIn('class="text-quality-indicator__context"', html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        indicator = public_text_quality_indicator_for_manual_text(
            item.manual_text_content
        )
        self.assertIsNotNone(indicator)
        self.assertEqual(indicator.quality, HUMAN_VERIFIED)
        self.assertEqual(indicator.tooltip_translation_note, "")

    def test_info_control_exposes_approved_explanation_including_translation_note(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.MEDIUM,
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            text="תרגום",
            engine="engine-quality-ui",
        )
        html = self._html(doc)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        self.assertIn('aria-label="מה המשמעות של דירוג האיכות?"', html)
        self.assertIn(TEXT_QUALITY_TOOLTIP_TITLE, html)
        self.assertIn(TEXT_QUALITY_TOOLTIP_INTRO, html)
        self.assertIn(TEXT_QUALITY_TOOLTIP_FOOTER, html)
        self.assertIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        self.assertNotIn(
            "הדירוג מביא בחשבון גם את איכות התעתוק שעליו מבוסס התרגום",
            html,
        )
        for quality, label in PUBLIC_TEXT_QUALITY_LABELS.items():
            self.assertIn(label, html)
            if quality == HUMAN_VERIFIED:
                self.assertIn("הטקסט הוזן או נבדק על ידי אדם.", html)
            elif quality == NEEDS_CORRECTION:
                self.assertIn("הטקסט נבדק ונמצא שהוא דורש תיקון.", html)

    def test_quality_is_represented_by_visible_text_not_only_css(self):
        doc = self._create_doc()
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.MEDIUM,
        )
        html = self._html(doc)
        badge_start = html.index("text-quality-indicator__badge--medium")
        badge_html = html[badge_start : badge_start + 400]
        self.assertIn("איכות בינונית", badge_html)
        self.assertNotIn(TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE, html)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        self.assertIn("text-quality-indicator", self._top_meta_html(html))
        self.assertNotIn('class="text-quality-indicator__heading"', html)

    def test_helper_skips_blank_text_and_uses_effective_quality(self):
        doc = self._create_doc()
        blank = self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.GOOD,
            text="   ",
            engine="engine-blank",
        )
        self.assertIsNone(
            public_text_quality_indicator_for_result(blank, help_dom_id="x")
        )
        verified = self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            quality=DocumentTextResult.Quality.LOW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            engine="engine-verified",
        )
        indicator = public_text_quality_indicator_for_result(verified)
        self.assertEqual(indicator.quality, HUMAN_VERIFIED)
        self.assertEqual(indicator.label, "נבדק ואושר")
        self.assertEqual(indicator.css_modifier, "human-verified")
        self.assertTrue(indicator.show_verified_mark)
        self.assertEqual(indicator.tooltip_translation_note, "")
        with_note = public_text_quality_indicator_for_result(
            verified, include_translation_note=True
        )
        self.assertEqual(
            with_note.tooltip_translation_note,
            TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE,
        )
