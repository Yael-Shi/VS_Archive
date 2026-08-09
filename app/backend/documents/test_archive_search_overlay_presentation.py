from types import SimpleNamespace

from django.test import SimpleTestCase

from documents.models import Document
from documents.services.archive_search_overlay_presentation import (
    apply_archive_search_overlay_to_source_previews,
    build_archive_search_single_image_overlay,
)


class ArchiveSearchOverlayPresentationTests(SimpleTestCase):
    def test_multi_image_preview_items_receive_matching_page_targets(self):
        first = object()
        second = object()

        items = [
            {"display_number": 1, "url": "one"},
            {"display_number": 2, "url": "two"},
        ]
        pages = (
            SimpleNamespace(page_index=1, targets=(first,)),
            SimpleNamespace(page_index=2, targets=(second,)),
        )

        rendered = apply_archive_search_overlay_to_source_previews(items, pages)

        self.assertEqual(
            rendered[0]["archive_search_overlay_targets"],
            (first,),
        )
        self.assertEqual(
            rendered[1]["archive_search_overlay_targets"],
            (second,),
        )

    def test_multi_image_preview_without_match_gets_empty_tuple(self):
        items = [
            {"display_number": 1, "url": "one"},
            {"display_number": 2, "url": "two"},
        ]
        pages = (SimpleNamespace(page_index=1, targets=(object(),)),)

        rendered = apply_archive_search_overlay_to_source_previews(items, pages)

        self.assertEqual(
            rendered[1]["archive_search_overlay_targets"],
            (),
        )

    def test_multi_image_source_preview_input_is_not_mutated(self):
        item = {"display_number": 1, "url": "one"}

        rendered = apply_archive_search_overlay_to_source_previews(
            [item],
            (),
        )

        self.assertNotIn("archive_search_overlay_targets", item)
        self.assertIn("archive_search_overlay_targets", rendered[0])

    def test_single_image_page_one_overlay_is_exposed(self):
        target = object()
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)

        overlay = build_archive_search_single_image_overlay(
            doc,
            content_url="https://example.test/image",
            overlay_pages=(SimpleNamespace(page_index=1, targets=(target,)),),
        )

        self.assertIsNotNone(overlay)
        self.assertEqual(overlay.page_index, 1)
        self.assertEqual(overlay.targets, (target,))

    def test_single_image_without_match_still_has_empty_page_one_overlay(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)

        overlay = build_archive_search_single_image_overlay(
            doc,
            content_url="https://example.test/image",
            overlay_pages=(),
        )

        self.assertIsNotNone(overlay)
        self.assertEqual(overlay.page_index, 1)
        self.assertEqual(overlay.targets, ())

    def test_pdf_has_no_single_image_overlay(self):
        doc = SimpleNamespace(doc_type=Document.DocType.PDF)

        overlay = build_archive_search_single_image_overlay(
            doc,
            content_url="https://example.test/document.pdf",
            overlay_pages=(),
        )

        self.assertIsNone(overlay)
