from types import SimpleNamespace

from django.test import SimpleTestCase

from documents.models import Document
from documents.services.archive_search_overlay_pages import (
    build_archive_search_overlay_pages,
)


def _target(page_index: int):
    return SimpleNamespace(page_index=page_index)


class ArchiveSearchOverlayPagesTests(SimpleTestCase):
    def test_multi_image_maps_display_number_to_transkribus_page_index(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        first = _target(1)
        second = _target(2)

        pages = build_archive_search_overlay_pages(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": "https://example.test/2"},
            ],
            content_url=None,
            overlay_targets=(first, second),
        )

        self.assertEqual([page.page_index for page in pages], [1, 2])
        self.assertEqual(pages[0].targets, (first,))
        self.assertEqual(pages[1].targets, (second,))

    def test_multi_image_unavailable_preview_page_fails_closed(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        target = _target(2)

        pages = build_archive_search_overlay_pages(
            doc,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1"},
                {"display_number": 2, "url": None},
            ],
            content_url=None,
            overlay_targets=(target,),
        )

        self.assertEqual([page.page_index for page in pages], [1])
        self.assertEqual(pages[0].targets, ())

    def test_target_for_nonexistent_multi_image_page_is_not_exposed(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        target = _target(3)

        pages = build_archive_search_overlay_pages(
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

    def test_single_image_maps_only_to_page_one(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)
        first = _target(1)
        second = _target(2)

        pages = build_archive_search_overlay_pages(
            doc,
            source_preview_items=[],
            content_url="https://example.test/image",
            overlay_targets=(first, second),
        )

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].page_index, 1)
        self.assertEqual(pages[0].targets, (first,))

    def test_single_image_without_content_url_exposes_nothing(self):
        doc = SimpleNamespace(doc_type=Document.DocType.IMAGE)

        pages = build_archive_search_overlay_pages(
            doc,
            source_preview_items=[],
            content_url=None,
            overlay_targets=(_target(1),),
        )

        self.assertEqual(pages, ())

    def test_pdf_deliberately_exposes_no_overlay_pages(self):
        doc = SimpleNamespace(doc_type=Document.DocType.PDF)

        pages = build_archive_search_overlay_pages(
            doc,
            source_preview_items=[],
            content_url="https://example.test/document.pdf",
            overlay_targets=(_target(1),),
        )

        self.assertEqual(pages, ())
