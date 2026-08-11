from django.test import SimpleTestCase

from documents.models import Document
from documents.services.source_image_renderability import (
    renderable_source_page_indexes,
)


class RenderableSourcePageIndexesTests(SimpleTestCase):
    def test_multi_image_returns_only_preview_items_with_urls(self):
        document = Document(doc_type=Document.DocType.IMAGE)

        result = renderable_source_page_indexes(
            document,
            source_preview_items=[
                {"display_number": 1, "url": "https://example.test/1.jpg"},
                {"display_number": 2, "url": None},
                {"display_number": 3, "url": "https://example.test/3.jpg"},
            ],
            content_url="https://example.test/fallback.jpg",
        )

        self.assertEqual(result, (1, 3))

    def test_single_image_with_content_url_maps_to_page_one(self):
        document = Document(doc_type=Document.DocType.IMAGE)

        result = renderable_source_page_indexes(
            document,
            source_preview_items=[],
            content_url="https://example.test/source.jpg",
        )

        self.assertEqual(result, (1,))

    def test_single_image_without_content_url_is_not_renderable(self):
        document = Document(doc_type=Document.DocType.IMAGE)

        result = renderable_source_page_indexes(
            document,
            source_preview_items=[],
            content_url=None,
        )

        self.assertEqual(result, ())

    def test_pdf_content_url_does_not_expose_source_image_page(self):
        document = Document(doc_type=Document.DocType.PDF)

        result = renderable_source_page_indexes(
            document,
            source_preview_items=[],
            content_url="https://example.test/source.pdf",
        )

        self.assertEqual(result, ())

    def test_preview_items_take_precedence_over_single_image_content_url(self):
        document = Document(doc_type=Document.DocType.IMAGE)

        result = renderable_source_page_indexes(
            document,
            source_preview_items=[
                {"display_number": 4, "url": "https://example.test/4.jpg"},
            ],
            content_url="https://example.test/fallback.jpg",
        )

        self.assertEqual(result, (4,))
