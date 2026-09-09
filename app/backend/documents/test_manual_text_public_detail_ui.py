"""Public MANUAL_TEXT archive detail presentation (heading, badge, header, body)."""

from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, PhotoContent
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.services.manual_text_body_display import (
    format_manual_text_body_for_display,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PHOTO_PRESIGNED_URL = "https://s3.example/presigned-photo"


def _create_photo_archive_item(*, title: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key="photos/42/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        upload_error="",
        thumbnail_file_key="",
        thumbnail_mime_type="",
        thumbnail_size_bytes=None,
    )
    return item


class ManualTextPublicDetailUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_text_detail_ui_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = create_manual_text_archive_item(
            title="Public manual detail UI",
            body="First line\nSecond line\n\nAfter blank line.",
            visibility=ArchiveItem.Visibility.PUBLIC,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        self.detail_url = reverse("archive-detail", kwargs={"item_id": self.item.id})

    def test_public_detail_omits_standalone_text_heading(self):
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "archive-detail-text-heading")
        self.assertNotContains(
            resp,
            '<div class="section-title archive-detail-text-heading">טקסט</div>',
            html=True,
        )

    def test_public_detail_omits_manual_text_type_badge(self):
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, '<span class="badge">טקסט</span>', html=True)
        self.assertNotContains(resp, "archive-detail-badges")

    def test_public_detail_keeps_required_public_actions(self):
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertContains(resp, "חזרה לארכיון")
        self.assertContains(resp, reverse("archive-list"))
        self.assertContains(resp, "הוספת מידע על הפריט")
        self.assertContains(
            resp,
            reverse(
                "archive-metadata-suggestion-new",
                kwargs={"item_id": self.item.id},
            ),
        )
        self.assertNotContains(resp, "עריכה")
        self.assertNotContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.item.id}),
        )
        self.assertNotContains(
            resp,
            reverse("archive-manage-delete", kwargs={"item_id": self.item.id}),
        )
        self.assertNotContains(resp, "document-detail-staff-management-actions")
        self.assertNotContains(resp, "פרטים טכניים")
        self.assertNotContains(resp, "document-detail-technical")
        self.assertNotContains(
            resp, "הטקסט חולץ אוטומטית ועדיין לא עבר בדיקה ידנית. ייתכנו שגיאות."
        )

        public_start = html.index("document-detail-navigation-actions")
        public_end = html.index("</div>", public_start)
        public_column = html[public_start:public_end]
        self.assertLess(
            public_column.index("חזרה לארכיון"),
            public_column.index("הוספת מידע על הפריט"),
        )
        self.assertIn("btn-primary", public_column)
        self.assertIn("←", public_column)
        self.assertNotIn("מידע והשתתפות", html)
        self.assertNotIn("btn-secondary", public_column)

    def test_staff_detail_keeps_management_actions_and_status_badge(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        edit_href = reverse("archive-manage-edit", kwargs={"item_id": self.item.id})
        delete_href = reverse("archive-manage-delete", kwargs={"item_id": self.item.id})

        self.assertContains(resp, "חזרה לארכיון")
        self.assertContains(resp, "הוספת מידע על הפריט")
        self.assertContains(resp, "עריכה")
        self.assertContains(resp, "מחיקה")
        self.assertContains(resp, edit_href)
        self.assertContains(resp, delete_href)
        self.assertContains(resp, f"פריט ארכיון #{self.item.id}")
        self.assertContains(resp, "ציבורי")
        self.assertContains(resp, "פרטים הושלמו")
        self.assertNotContains(resp, '<span class="badge">טקסט</span>', html=True)
        self.assertNotContains(resp, "archive-detail-text-heading")

        public_start = html.index("document-detail-navigation-actions")
        public_end = html.index("</div>", public_start)
        public_column = html[public_start:public_end]
        self.assertIn("חזרה לארכיון", public_column)
        self.assertIn("הוספת מידע על הפריט", public_column)
        self.assertNotIn(edit_href, public_column)
        self.assertNotIn("עריכה", public_column)
        self.assertNotIn("מחיקה", public_column)

        staff_start = html.index("document-detail-staff-management-actions")
        staff_end = html.index("</div>", staff_start)
        staff_section = html[staff_start:staff_end]
        self.assertIn("עריכה", staff_section)
        self.assertIn("מחיקה", staff_section)
        self.assertNotIn("חזרה לארכיון", staff_section)
        self.assertNotIn("הוספת מידע על הפריט", staff_section)

        badges_start = html.index("archive-detail-manual-text-badges")
        badges_section = html[badges_start : html.index("</div>", badges_start)]
        self.assertIn("פרטים הושלמו", badges_section)
        self.assertNotIn("טקסט", badges_section)

    def test_public_detail_renders_manual_text_structural_classes(self):
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-detail-page--manual-text")
        self.assertContains(resp, "document-detail-header")
        self.assertContains(resp, "document-detail-header-main")
        self.assertContains(resp, "document-detail-toolbar")
        self.assertContains(resp, "document-detail-navigation-actions")
        self.assertContains(resp, "archive-detail-manual-text-status")
        self.assertContains(resp, "archive-detail-manual-text-quality")
        self.assertContains(resp, "archive-detail-manual-text-body")
        self.assertContains(resp, "archive-detail-text")
        self.assertContains(
            resp,
            '<div class="archive-detail-manual-text-signature" aria-hidden="true"></div>',
            html=True,
        )
        self.assertContains(resp, "manual_text_signature.js")
        self.assertNotContains(resp, "archive-detail-page--photo")
        self.assertNotContains(resp, "spacer-sm")
        html = resp.content.decode("utf-8")
        header = html[html.index("document-detail-header") : html.index("</header>")]
        self.assertIn("archive-detail-manual-text-quality", header)
        self.assertIn("archive-detail-meta", header)
        self.assertNotIn('class="spacer"', header)
        self.assertNotIn("spacer-sm", header)

    def test_manual_text_signature_choice_is_deterministic_from_item_id(self):
        items = [
            self.item,
            create_manual_text_archive_item(
                title="Signature deterministic second item",
                body="Second body",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
            create_manual_text_archive_item(
                title="Signature deterministic third item",
                body="Third body",
                visibility=ArchiveItem.Visibility.PUBLIC,
            ),
        ]

        for item in items:
            if item.id % 3 == 0:
                expected = 3
            elif item.id % 2 == 0:
                expected = 2
            else:
                expected = 1

            resp = self.client.get(
                reverse("archive-detail", kwargs={"item_id": item.id})
            )
            self.assertEqual(resp.status_code, 200)

            expected_class = f"archive-detail-page--manual-text-signature-{expected}"
            self.assertContains(resp, expected_class)

            for number in (1, 2, 3):
                if number != expected:
                    self.assertNotContains(
                        resp,
                        f"archive-detail-page--manual-text-signature-{number}",
                    )

    def test_public_body_remains_html_safe_and_preserves_newlines(self):
        item = create_manual_text_archive_item(
            title="Unsafe manual body",
            body="<script>alert(1)</script>\nnext line\n\nAfter blank.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(resp, "<script>alert(1)</script>")
        self.assertContains(
            resp,
            "&lt;script&gt;alert(1)&lt;/script&gt;<br />next line<br /><br />After blank.",
            html=True,
        )


class ManualTextBodyNewlineSemanticsTests(SimpleTestCase):
    def test_single_newline_becomes_one_break_without_paragraph_markup(self):
        rendered = format_manual_text_body_for_display("line one\nline two")
        self.assertEqual(rendered, "line one<br />line two")
        self.assertNotIn("<p", rendered)

    def test_blank_line_becomes_two_breaks(self):
        rendered = format_manual_text_body_for_display("line one\n\nline two")
        self.assertEqual(rendered, "line one<br /><br />line two")
        self.assertNotIn("<p", rendered)

    def test_html_is_escaped_before_breaks(self):
        rendered = format_manual_text_body_for_display("<b>bold</b>\nnext")
        self.assertEqual(rendered, "&lt;b&gt;bold&lt;/b&gt;<br />next")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class ManualTextDetailDoesNotChangeOtherTypesTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="manual_text_ui_other_staff",
            password="test-pass",
            is_staff=True,
        )
        self.photo = _create_photo_archive_item(title="Photo contract unchanged")
        self.video = create_video_archive_item(
            title="Video contract unchanged",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    @patch(
        "documents.views.create_presigned_get",
        return_value=PHOTO_PRESIGNED_URL,
    )
    def test_photo_detail_keeps_photo_header_and_type_badge_for_staff(
        self, _mock_presigned_get
    ):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.photo.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-detail-page--photo")
        self.assertContains(resp, "document-detail-header")
        self.assertContains(resp, "document-detail-navigation-actions")
        self.assertContains(resp, "document-detail-staff-management-actions")
        self.assertContains(resp, '<span class="badge">תמונה</span>', html=True)
        self.assertNotContains(resp, "archive-detail-page--manual-text")
        self.assertNotContains(resp, "archive-detail-manual-text-status")
        self.assertNotContains(resp, "archive-detail-manual-text-quality")
        self.assertNotContains(resp, "archive-detail-manual-text-body")
        self.assertNotContains(resp, "archive-detail-manual-text-signature")
        self.assertNotContains(resp, "manual_text_signature.js")

    def test_video_detail_uses_canonical_nav_without_public_type_badge(self):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.video.id})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertContains(resp, "document-detail-header")
        self.assertContains(resp, "document-detail-navigation-actions")
        self.assertNotContains(resp, '<span class="badge">סרטון</span>', html=True)
        self.assertContains(resp, "הוספת מידע על הפריט")
        public_start = html.index("document-detail-navigation-actions")
        public_column = html[public_start : html.index("</div>", public_start)]
        self.assertIn("btn-primary", public_column)
        self.assertIn("←", public_column)
        self.assertLess(
            public_column.index("חזרה לארכיון"),
            public_column.index("הוספת מידע על הפריט"),
        )
        self.assertNotContains(resp, "archive-detail-page--manual-text")
        self.assertNotContains(resp, "archive-detail-manual-text-status")
        self.assertNotContains(resp, "archive-detail-manual-text-quality")
        self.assertNotContains(resp, "archive-detail-manual-text-body")
        self.assertNotContains(resp, "archive-detail-manual-text-signature")
        self.assertNotContains(resp, "manual_text_signature.js")


class ManualTextPublicQualityBadgePlacementTests(TestCase):
    def setUp(self):
        self.item = create_manual_text_archive_item(
            title="Manual quality badge placement",
            body="Body that must not include the quality badge.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.detail_url = reverse("archive-detail", kwargs={"item_id": self.item.id})

    def test_quality_badge_renders_outside_manual_text_block_once(self):
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertEqual(html.count('class="text-quality-indicator__info"'), 1)
        self.assertEqual(html.count("archive-detail-manual-text-quality"), 1)
        self.assertContains(resp, "archive-detail-manual-text-status")
        self.assertContains(resp, "text-quality-indicator__badge--human-verified")
        self.assertContains(resp, "נבדק ואושר")

        status_start = html.index("archive-detail-manual-text-status")
        body_start = html.index(
            'class="archive-detail-text archive-detail-manual-text-body"'
        )
        self.assertLess(status_start, body_start)

        status_html = html[status_start:body_start]
        self.assertIn("data-text-quality-indicator", status_html)
        self.assertIn("archive-detail-manual-text-quality", status_html)

        body_end = html.index("archive-detail-manual-text-signature", body_start)
        body_html = html[body_start:body_end]
        self.assertIn("text-block", body_html)
        self.assertNotIn("text-quality-indicator", body_html)
        self.assertNotIn("data-text-quality-indicator", body_html)

    def test_staff_status_badge_row_does_not_duplicate_quality_indicator(self):
        staff = User.objects.create_user(
            username="manual_text_quality_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        resp = self.client.get(self.detail_url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")

        self.assertEqual(html.count("data-text-quality-indicator"), 1)
        self.assertContains(resp, "archive-detail-manual-text-badges")
        badges_start = html.index("archive-detail-manual-text-badges")
        badges_section = html[badges_start : html.index("</div>", badges_start)]
        self.assertIn('<span class="badge">', badges_section)
        self.assertNotIn("text-quality-indicator", badges_section)


class ManualTextPublicDetailLayoutStyleTests(SimpleTestCase):
    def _css(self) -> str:
        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        return css_path.read_text(encoding="utf-8")

    def test_manual_text_header_actions_reuse_document_detail_column_pattern(self):
        css = self._css()

        self.assertNotIn(".archive-detail-manual-text-header {", css)
        self.assertNotIn(".archive-detail-manual-text-navigation-actions {", css)
        self.assertNotIn(
            ".archive-detail-manual-text-staff-management-actions {",
            css,
        )

        header_start = css.index(".document-detail-header {")
        header_rule = css[header_start : css.index("}", header_start)]
        self.assertIn("display: flex;", header_rule)
        self.assertIn("flex-wrap: wrap;", header_rule)
        self.assertIn("align-items: flex-start;", header_rule)

        main_start = css.index(".document-detail-header-main {")
        main_rule = css[main_start : css.index("}", main_start)]
        self.assertIn("flex: 1 1 16rem;", main_rule)

        top_start = css.index(".document-detail-toolbar {")
        top_rule = css[top_start : css.index("}", top_start)]
        self.assertIn("justify-content: flex-end;", top_rule)
        self.assertIn("align-items: flex-start;", top_rule)

        public_start = css.index(".document-detail-navigation-actions {")
        public_rule = css[public_start : css.index("}", public_start)]
        self.assertIn("flex-direction: column;", public_rule)
        self.assertIn("inline-size: max-content;", public_rule)

        archive_staff_start = css.index(
            ".archive-detail-page .document-detail-staff-management-actions {"
        )
        archive_staff_rule = css[
            archive_staff_start : css.index("}", archive_staff_start)
        ]
        self.assertIn("inline-size: max-content;", archive_staff_rule)
        self.assertIn("max-inline-size: 100%;", archive_staff_rule)
        self.assertNotIn("display: grid", archive_staff_rule)

        staff_start = css.index("\n.document-detail-staff-management-actions {")
        staff_rule = css[staff_start : css.index("}", staff_start)]
        self.assertIn("display: flex;", staff_rule)
        self.assertIn("flex-direction: column;", staff_rule)
        self.assertIn("align-items: stretch;", staff_rule)
        self.assertNotIn("display: grid", staff_rule)

    def test_manual_text_quality_badge_css_is_scoped_outside_text_body(self):
        css = self._css()

        self.assertNotIn(
            ".archive-detail-manual-text-body .text-quality-indicator",
            css,
        )

        status_start = css.index(".archive-detail-manual-text-status {")
        status_rule = css[status_start : css.index("}", status_start)]
        self.assertIn("display: flex;", status_rule)

        quality_start = css.index(".archive-detail-manual-text-quality {")
        quality_rule = css[quality_start : css.index("}", quality_start)]
        self.assertIn("position: relative;", quality_rule)
        self.assertIn("min-width: 0;", quality_rule)
        self.assertIn("max-width: 100%;", quality_rule)

        popover_start = css.index(
            ".archive-detail-page--manual-text "
            ".archive-detail-manual-text-quality "
            ".text-quality-indicator__popover {"
        )
        popover_rule = css[popover_start : css.index("}", popover_start)]
        self.assertIn("inset-inline-start: 0;", popover_rule)
        self.assertIn("inset-inline-end: auto;", popover_rule)
        self.assertIn("width: min(22rem, 100%);", popover_rule)
        self.assertIn("max-width: 100%;", popover_rule)

        wrap_start = css.index(
            ".archive-detail-page--manual-text "
            ".archive-detail-manual-text-quality "
            ".text-quality-indicator__info-wrap {"
        )
        wrap_rule = css[wrap_start : css.index("}", wrap_start)]
        self.assertIn("position: static;", wrap_rule)

        shared_popover_start = css.index("\n.text-quality-indicator__popover {")
        shared_popover = css[
            shared_popover_start : css.index("}", shared_popover_start)
        ]
        self.assertIn("inset-inline-end: 0;", shared_popover)
        self.assertIn("width: min(22rem, calc(100vw - 2rem));", shared_popover)
        self.assertIn(
            ".text-quality-indicator__info-wrap:hover .text-quality-indicator__popover",
            css,
        )
        self.assertIn(
            ".text-quality-indicator__info-wrap:focus-within .text-quality-indicator__popover",
            css,
        )

    def test_manual_text_body_reading_width_and_newline_css(self):
        css = self._css()

        body_start = css.index(
            ".archive-detail-page--manual-text .archive-detail-manual-text-body {"
        )
        body_rule = css[body_start : css.index("}", body_start)]
        self.assertIn("max-width: min(82ch, 100%);", body_rule)
        self.assertIn("margin-inline-end: auto;", body_rule)
        self.assertNotIn("margin: 0 auto", body_rule)
        self.assertNotIn("margin-inline-start: auto", body_rule)

        text_start = css.index(
            ".archive-detail-page--manual-text .archive-detail-manual-text-body .text-block {"
        )
        text_rule = css[text_start : css.index("}", text_start)]
        self.assertIn("white-space: normal;", text_rule)
        self.assertIn("line-height: var(--line-relaxed);", text_rule)
        self.assertNotIn("white-space: pre-wrap;", text_rule)
        self.assertNotIn("line-height: var(--line-ocr);", text_rule)

    def test_manual_text_signature_watermark_is_desktop_only(self):
        css = self._css()
        signature_css = css[css.index(".archive-detail-manual-text-signature {") :]

        self.assertNotIn(
            ".archive-detail-page--manual-text .archive-detail-manual-text-body::before",
            css,
        )
        self.assertNotIn(
            ".archive-detail-manual-text-body::before",
            signature_css,
        )
        self.assertNotIn("left: calc(", signature_css)
        self.assertNotIn("right: calc(", signature_css)
        self.assertNotIn("min(1320px", signature_css)
        self.assertNotIn("* 3 / 8", signature_css)
        self.assertNotIn("27vw", signature_css)
        self.assertNotIn("7vw", signature_css)
        self.assertNotIn("width: min(", signature_css)
        self.assertNotIn("position: fixed;", signature_css)
        self.assertNotIn("::before", signature_css)

        desktop_start = signature_css.index("@media (min-width: 1180px)")
        desktop_end = signature_css.index("@media (max-width: 1179px)", desktop_start)
        desktop_rule = signature_css[desktop_start:desktop_end]
        self.assertIn(
            ".archive-detail-page--manual-text .archive-detail-manual-text-signature",
            desktop_rule,
        )
        self.assertIn("display: block;", desktop_rule)

        mobile_start = signature_css.index("@media (max-width: 1179px)", desktop_end)
        mobile_rule = signature_css[
            mobile_start : signature_css.index("}", mobile_start) + 1
        ]
        self.assertIn(".archive-detail-manual-text-signature", mobile_rule)
        self.assertIn("display: none;", mobile_rule)

        signature_rule = signature_css[
            : signature_css.index(
                "}", signature_css.index(".archive-detail-manual-text-signature {")
            )
        ]
        self.assertIn("position: absolute;", signature_rule)
        self.assertNotIn("position: fixed;", signature_rule)
        self.assertIn("max-width: 410px;", signature_rule)
        self.assertNotIn("width: min(27vw, 410px);", signature_rule)
        self.assertNotIn("27vw", signature_rule)
        self.assertIn("aspect-ratio: 16 / 9;", signature_rule)
        self.assertIn("opacity: 0.22;", signature_rule)
        self.assertIn("pointer-events: none;", signature_rule)
        self.assertNotIn("left:", signature_rule)

        card_start = css.index(".archive-detail-page--manual-text {")
        card_rule = css[card_start : css.index("}", card_start)]
        self.assertIn("position: relative;", card_rule)

    def test_manual_text_signature_assets_are_referenced(self):
        css = self._css()

        for number in (1, 2, 3):
            self.assertIn(
                ".archive-detail-page--manual-text-signature-"
                f"{number}\n"
                "  .archive-detail-manual-text-signature {",
                css,
            )
            self.assertIn(
                f"/static/public/images/manual-text-signatures/"
                f"saadia-signature-{number}.png",
                css,
            )

    def test_manual_text_signature_script_positions_from_measured_layout(self):
        js_path = (
            settings.BASE_DIR
            / "public"
            / "static"
            / "public"
            / "manual_text_signature.js"
        )
        js = js_path.read_text(encoding="utf-8")

        self.assertIn(".archive-detail-page--manual-text", js)
        self.assertIn(".archive-detail-manual-text-body", js)
        self.assertIn(".document-detail-navigation-actions", js)
        self.assertIn(".document-detail-staff-management-actions", js)
        self.assertIn(".archive-detail-manual-text-signature", js)
        self.assertIn("getBoundingClientRect()", js)
        self.assertIn("sideLeft + sideWidth / 2", js)
        self.assertIn("sideCenterInCard", js)
        self.assertIn("var MAX_SIGNATURE_WIDTH_PX = 410;", js)
        self.assertIn("var MIN_SIGNATURE_WIDTH_PX = 160;", js)
        self.assertIn("availableWidth = sideWidth - 2 * spacing", js)
        self.assertIn("availableHeight = maxBottomInCard - safeTopInCard", js)
        self.assertIn("availableHeight * 16 / 9", js)
        self.assertIn("MAX_SIGNATURE_WIDTH_PX,", js)
        self.assertIn("availableWidth,", js)
        self.assertIn('signature.style.width = fittingWidth + "px"', js)
        self.assertIn("safeTopInCard", js)
        self.assertIn("cardHeight = cardRect.height - borders.top - borders.bottom", js)
        self.assertIn("signatureHeight = fittingWidth * 9 / 16", js)
        self.assertIn("cardHeight - signatureHeight - spacing", js)
        self.assertNotIn("page.clientHeight", js)
        self.assertNotIn("signature.offsetHeight", js)
        self.assertIn("desiredTopInCard", js)
        self.assertIn("finalTopInCard", js)
        self.assertIn("translateX(-50%)", js)
        self.assertIn("(min-width: 1180px)", js)
        self.assertIn("ResizeObserver", js)
        self.assertIn('addEventListener("resize"', js)
        self.assertIn('addEventListener("scroll"', js)
        self.assertIn("{ passive: true }", js)
        self.assertIn("requestAnimationFrame", js)
        self.assertNotIn("position: fixed", js)
        self.assertNotIn("viewportMaxBottom", js)
        self.assertNotIn("viewportMaxTop", js)
        self.assertNotIn("cardMaxBottom", js)
        self.assertNotIn("console.debug", js)
        self.assertNotIn("[manual-text-signature]", js)
        self.assertNotIn("devicePixelRatio", js)
        self.assertNotIn("27vw", js)
        self.assertNotIn("7vw", js)
        self.assertNotIn("1320px", js)
        self.assertNotIn("82ch", js)
        self.assertNotIn("left: calc", js)
        self.assertNotIn("right: calc", js)
        self.assertNotIn("signature-1", js)
        self.assertNotIn("signature-2", js)
        self.assertNotIn("signature-3", js)

    def _template(self) -> str:
        path = (
            settings.BASE_DIR
            / "documents"
            / "templates"
            / "documents"
            / "archive"
            / "detail.html"
        )
        return path.read_text(encoding="utf-8")

    def test_manual_text_template_scopes_signature_markup_and_script(self):
        template = self._template()

        self.assertEqual(template.count("archive-detail-manual-text-signature"), 1)
        self.assertEqual(template.count("manual_text_signature.js"), 1)
        self.assertIn(
            '<div class="archive-detail-manual-text-signature" aria-hidden="true"></div>',
            template,
        )
        self.assertIn("{% static 'public/manual_text_signature.js' %}", template)

        signature_idx = template.index("archive-detail-manual-text-signature")
        signature_if = template.rfind(
            '{% if item.item_type == "MANUAL_TEXT" %}', 0, signature_idx
        )
        signature_endif = template.find("{% endif %}", signature_idx)
        self.assertGreater(signature_if, -1)
        self.assertGreater(signature_endif, signature_idx)

        script_idx = template.index("manual_text_signature.js")
        script_if = template.rfind(
            '{% if item.item_type == "MANUAL_TEXT" %}', 0, script_idx
        )
        script_endif = template.find("{% endif %}", script_idx)
        self.assertGreater(script_if, -1)
        self.assertGreater(script_endif, script_idx)

        header = template[
            template.index("<header") : template.index("</header>") + len("</header>")
        ]
        self.assertNotIn("archive-detail-manual-text-signature", header)
        self.assertNotIn("manual_text_signature.js", header)

    def test_manual_text_quality_indicator_is_only_in_status_area(self):
        template = self._template()

        self.assertEqual(
            template.count(
                '{% include "documents/partials/text_quality_indicator.html"'
            ),
            1,
        )
        include_idx = template.index(
            '{% include "documents/partials/text_quality_indicator.html"'
        )
        quality_wrap_idx = template.rfind(
            "archive-detail-manual-text-quality", 0, include_idx
        )
        self.assertGreater(quality_wrap_idx, -1)

        body_idx = template.index("archive-detail-manual-text-body")
        self.assertLess(include_idx, body_idx)
        body_end = template.index("archive-detail-manual-text-signature", body_idx)
        body = template[body_idx:body_end]
        self.assertNotIn("text_quality_indicator.html", body)
        self.assertNotIn("text_quality_indicator", body)

    def test_photo_and_manual_text_share_document_header_selectors(self):
        css = self._css()
        self.assertNotIn(".archive-detail-photo-header {", css)
        header = css[
            css.index(".document-detail-header {") : css.index(
                "}", css.index(".document-detail-header {")
            )
        ]
        self.assertIn("display: flex;", header)
        self.assertNotIn("archive-detail-manual-text", header)
