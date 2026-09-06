"""Public archive item Author presentation: structured links vs author_name fallback."""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    Author,
    Document,
    PhotoContent,
)
from documents.services.archive_item_presentation import (
    ArchiveBrowseLink,
    author_presentation_for_item,
    build_archive_browse_card,
)
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.author_public import author_public_page_url
from documents.test_archive_item import create_viewable_ocr_document


def _public_manual(title: str, **kwargs) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
        **kwargs,
    )


def _set_author_name_only(item: ArchiveItem, author_name: str) -> None:
    """Write ``author_name`` without creating ArchiveItemAuthor rows."""
    ArchiveItem.objects.filter(pk=item.pk).update(author_name=author_name)
    item.refresh_from_db()


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


def _public_photo(*, title: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="original.jpg",
        original_mime_type="image/jpeg",
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item


class PublicArchiveItemAuthorPresentationTests(TestCase):
    def test_structured_single_author_renders_linked_canonical_name(self):
        item = _public_manual("Single structured author")
        author = Author.objects.create(name="Canonical Author")
        _link(item, author, position=0)
        _set_author_name_only(item, "Stale author_name")

        links, fallback = author_presentation_for_item(item)
        self.assertEqual(fallback, "")
        self.assertEqual(
            links,
            (
                ArchiveBrowseLink(
                    name="Canonical Author",
                    href=author_public_page_url(author.id),
                ),
            ),
        )

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "מחבר/ת:")
        self.assertContains(detail, "Canonical Author")
        self.assertContains(detail, author_public_page_url(author.id))
        self.assertNotContains(detail, "Stale author_name")

        listing = self.client.get(reverse("archive-list"))
        self.assertContains(listing, "archive-browse-card__author")
        self.assertContains(listing, "Canonical Author")
        self.assertContains(listing, author_public_page_url(author.id))
        self.assertNotContains(listing, "Stale author_name")

    def test_multiple_authors_render_in_position_order_with_distinct_urls(self):
        item = _public_manual("Multiple structured authors")
        second = Author.objects.create(name="Second Author")
        first = Author.objects.create(name="First Author")
        _link(item, second, position=1)
        _link(item, first, position=0)

        links, fallback = author_presentation_for_item(item)
        self.assertEqual(fallback, "")
        self.assertEqual(
            [link.name for link in links],
            ["First Author", "Second Author"],
        )
        self.assertEqual(
            [link.href for link in links],
            [
                author_public_page_url(first.id),
                author_public_page_url(second.id),
            ],
        )

        html = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        ).content.decode()
        first_href = author_public_page_url(first.id)
        second_href = author_public_page_url(second.id)
        self.assertLess(html.index(first_href), html.index(second_href))
        self.assertIn("First Author", html)
        self.assertIn("Second Author", html)

    def test_duplicate_author_names_retain_distinct_urls(self):
        item = _public_manual("Duplicate author names")
        earlier = Author.objects.create(name="Same Name")
        later = Author.objects.create(name="Same Name")
        _link(item, later, position=1)
        _link(item, earlier, position=0)

        links, _fallback = author_presentation_for_item(item)
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0].name, "Same Name")
        self.assertEqual(links[1].name, "Same Name")
        self.assertEqual(links[0].href, author_public_page_url(earlier.id))
        self.assertEqual(links[1].href, author_public_page_url(later.id))
        self.assertNotEqual(links[0].href, links[1].href)

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertContains(detail, author_public_page_url(earlier.id))
        self.assertContains(detail, author_public_page_url(later.id))

    def test_author_name_only_item_renders_legacy_text_fallback(self):
        item = _public_manual("Author name only")
        _set_author_name_only(item, "  Legacy Author Text  ")

        links, fallback = author_presentation_for_item(item)
        self.assertEqual(links, ())
        self.assertEqual(fallback, "Legacy Author Text")

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertContains(detail, "מחבר/ת:")
        self.assertContains(detail, "Legacy Author Text")
        self.assertNotContains(detail, "/archive/authors/")

        listing = self.client.get(reverse("archive-list"))
        self.assertContains(listing, "Legacy Author Text")
        self.assertNotContains(listing, "/archive/authors/")

    def test_no_structured_links_does_not_infer_author_from_author_name(self):
        existing = Author.objects.create(name="Ada Lovelace")
        other = _public_manual("Linked other item")
        _link(other, existing, position=0)

        item = _public_manual("Unlinked same name")
        _set_author_name_only(item, "Ada Lovelace")

        links, fallback = author_presentation_for_item(item)
        self.assertEqual(links, ())
        self.assertEqual(fallback, "Ada Lovelace")

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertContains(detail, "Ada Lovelace")
        self.assertNotContains(detail, author_public_page_url(existing.id))

    def test_ocr_detail_uses_same_structured_and_fallback_rules(self):
        linked_doc = create_viewable_ocr_document(
            title="OCR structured author",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        author = Author.objects.create(name="OCR Canonical")
        _link(linked_doc.archive_item, author, position=0)
        ArchiveItem.objects.filter(pk=linked_doc.archive_item_id).update(
            author_name="OCR stale"
        )

        linked_resp = self.client.get(f"/api/ui/documents/{linked_doc.id}/")
        self.assertContains(linked_resp, "OCR Canonical")
        self.assertContains(linked_resp, author_public_page_url(author.id))
        self.assertNotContains(linked_resp, "OCR stale")

        fallback_doc = create_viewable_ocr_document(
            title="OCR fallback author",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItem.objects.filter(pk=fallback_doc.archive_item_id).update(
            author_name="OCR fallback only",
        )
        fallback_resp = self.client.get(f"/api/ui/documents/{fallback_doc.id}/")
        self.assertContains(fallback_resp, "OCR fallback only")
        self.assertNotContains(fallback_resp, "/archive/authors/")

    def test_photo_card_can_show_existing_author_metadata_as_links(self):
        item = _public_photo(title="Photo with structured author")
        author = Author.objects.create(name="Photo Card Author")
        _link(item, author, position=0)
        _set_author_name_only(item, "Photo stale")

        card = build_archive_browse_card(item)
        self.assertEqual(card.author_display, "")
        self.assertEqual(card.author_links[0].name, "Photo Card Author")
        self.assertEqual(card.author_links[0].href, author_public_page_url(author.id))

        listing = self.client.get(reverse("archive-list"))
        self.assertContains(listing, "Photo Card Author")
        self.assertContains(listing, author_public_page_url(author.id))

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, "מחבר/ת:")
        self.assertNotContains(detail, "Photo Card Author")
        self.assertNotContains(detail, "Photo stale")

    def test_photo_detail_does_not_gain_author_surface_for_author_name_only(self):
        item = _public_photo(title="Photo author_name only")
        _set_author_name_only(item, "Hidden photo author")

        listing = self.client.get(reverse("archive-list"))
        self.assertContains(listing, "Hidden photo author")
        self.assertNotContains(listing, "/archive/authors/")

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertNotContains(detail, "מחבר/ת:")
        self.assertNotContains(detail, "Hidden photo author")

    def test_global_q_and_advanced_author_still_use_author_name(self):
        item = _public_manual("Search unchanged item")
        author = Author.objects.create(name="IndexedCanonicalUnused")
        _link(item, author, position=0)
        _set_author_name_only(item, "IndexedLegacyAuthorToken")
        sync_archive_item_search_index(item.pk)

        q_resp = self.client.get(
            reverse("archive-list"),
            {"q": "IndexedLegacyAuthorToken"},
        )
        self.assertContains(q_resp, item.title)

        unused_q = self.client.get(
            reverse("archive-list"),
            {"q": "IndexedCanonicalUnused"},
        )
        self.assertNotContains(unused_q, item.title)

        advanced_match = self.client.get(
            reverse("archive-list"),
            {
                "advanced": "1",
                "author": "IndexedLegacyAuthorToken",
            },
        )
        self.assertContains(advanced_match, item.title)

        advanced_canonical = self.client.get(
            reverse("archive-list"),
            {
                "advanced": "1",
                "author": "IndexedCanonicalUnused",
            },
        )
        self.assertNotContains(advanced_canonical, item.title)

    def test_browse_list_prefetches_author_links_without_n_plus_one(self):
        for index in range(3):
            item = _public_manual(f"Prefetch author item {index}")
            author = Author.objects.create(name=f"Prefetch Author {index}")
            _link(item, author, position=0)

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Prefetch Author 0")
        self.assertContains(resp, "Prefetch Author 2")
        author_link_queries = [
            query["sql"]
            for query in ctx.captured_queries
            if "documents_archiveitemauthor" in query["sql"].lower()
        ]
        self.assertLessEqual(len(author_link_queries), 1)
