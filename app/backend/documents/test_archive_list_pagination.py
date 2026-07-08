"""Public /archive/ list pagination and per-page controls."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from documents.models import ArchiveItem
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    archive_public_list_pagination_context,
    build_archive_public_list_query,
    build_archive_public_list_type_filter_links,
    normalize_archive_public_list_page,
    normalize_archive_public_list_per_page,
)
from documents.services.archive_items import create_manual_text_archive_item


class ArchivePublicListPaginationHelperTests(SimpleTestCase):
    def test_default_per_page_is_48(self):
        self.assertEqual(normalize_archive_public_list_per_page(None), 48)
        self.assertEqual(normalize_archive_public_list_per_page(""), 48)

    def test_supported_per_page_values(self):
        for value in (24, 48, 96):
            self.assertEqual(normalize_archive_public_list_per_page(str(value)), value)

    def test_invalid_per_page_falls_back_to_48(self):
        for raw in ("50", "0", "-1", "abc", "100"):
            self.assertEqual(
                normalize_archive_public_list_per_page(raw),
                ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
            )

    def test_page_number_is_bounded(self):
        self.assertEqual(
            normalize_archive_public_list_page("0", total_count=100, per_page=24),
            1,
        )
        self.assertEqual(
            normalize_archive_public_list_page("999", total_count=50, per_page=24),
            3,
        )
        self.assertEqual(
            normalize_archive_public_list_page("abc", total_count=50, per_page=24),
            1,
        )

    def test_build_query_preserves_search_filter_and_per_page(self):
        query = build_archive_public_list_query(
            q="family search",
            item_type_filter="documents_and_texts",
            page=2,
            per_page=24,
        )
        self.assertIn("q=family+search", query)
        self.assertIn("item_type=documents_and_texts", query)
        self.assertIn("page=2", query)
        self.assertIn("per_page=24", query)

    def test_type_filter_links_omit_default_per_page(self):
        links = build_archive_public_list_type_filter_links(
            q="family search",
            per_page=ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
            active_item_type_filter="documents_and_texts",
        )
        photo_link = next(link for link in links if link["label"] == "תמונות")
        self.assertEqual(
            photo_link["href_suffix"],
            "?q=family+search&item_type=photo",
        )


class ArchivePublicListPaginationViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.items = []
        for index in range(55):
            cls.items.append(
                create_manual_text_archive_item(
                    title=f"PAGVIEW-{index:02d}",
                    body="pagination body",
                    visibility=ArchiveItem.Visibility.PUBLIC,
                )
            )

    def test_default_page_size_is_48(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        shown = sum(1 for index in range(55) if f"PAGVIEW-{index:02d}" in html)
        self.assertEqual(shown, 48)
        self.assertIn(
            'class="archive-list-pagination__per-page-option is-active">48',
            html,
        )

    def test_per_page_24_and_96(self):
        resp_24 = self.client.get(
            reverse("archive-list"),
            {"per_page": "24", "page": "2"},
        )
        self.assertEqual(resp_24.status_code, 200)
        html_24 = resp_24.content.decode("utf-8")
        self.assertIn("PAGVIEW-30", html_24)
        self.assertNotIn("PAGVIEW-31", html_24)

        resp_96 = self.client.get(reverse("archive-list"), {"per_page": "96"})
        self.assertEqual(resp_96.status_code, 200)
        html_96 = resp_96.content.decode("utf-8")
        for index in range(55):
            self.assertIn(f"PAGVIEW-{index:02d}", html_96)

    def test_invalid_per_page_falls_back_to_48(self):
        resp = self.client.get(reverse("archive-list"), {"per_page": "50"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        shown = sum(1 for index in range(55) if f"PAGVIEW-{index:02d}" in html)
        self.assertEqual(shown, 48)
        self.assertIn(
            'class="archive-list-pagination__per-page-option is-active">48',
            html,
        )

    def test_pagination_links_preserve_search_filter_and_per_page(self):
        resp = self.client.get(
            reverse("archive-list"),
            {
                "q": "PAGVIEW",
                "item_type": "documents_and_texts",
                "per_page": "24",
                "page": "2",
            },
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("q=PAGVIEW", html)
        self.assertIn("item_type=documents_and_texts", html)
        self.assertIn("per_page=24", html)
        self.assertIn(
            'href="/archive/?q=PAGVIEW&amp;item_type=documents_and_texts&amp;per_page=24"',
            html,
        )
        self.assertNotIn("page=1", html)
        self.assertIn(
            'href="/archive/?q=PAGVIEW&amp;item_type=documents_and_texts&amp;page=3&amp;per_page=24"',
            html,
        )

    def test_top_anchor_and_link_render_with_pagination_controls(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('id="archive-top"', html)
        self.assertIn('href="#archive-top"', html)
        self.assertIn("חזרה לראש העמוד", html)
        self.assertIn("archive-list-pagination", html)

    def test_pagination_context_omits_default_per_page_from_links(self):
        context = archive_public_list_pagination_context(
            total_count=100,
            page=2,
            per_page=48,
            q="",
            item_type_filter="",
        )
        prev_href_suffix = str(context["prev_href_suffix"])
        next_href_suffix = str(context["next_href_suffix"])
        self.assertEqual(prev_href_suffix, "")
        self.assertEqual(next_href_suffix, "?page=3")
        self.assertNotIn("per_page", prev_href_suffix)
        self.assertNotIn("page=1", prev_href_suffix)
