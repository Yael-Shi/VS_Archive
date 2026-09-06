"""Public q indexing and match-source attribution for structured Authors."""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_item_authors import searchable_author_names_for_item
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    SEARCH_SEGMENT_SEPARATOR,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
)
from documents.services.archive_search_snippets import (
    MATCH_SOURCE_AUTHOR,
    build_archive_search_match_presentation,
)


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _rebuild(archive_item_id: int) -> None:
    rebuild_archive_item_search_index(_load_item(archive_item_id))


def _public_manual(title: str, **kwargs) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="unrelated body text",
        visibility=ArchiveItem.Visibility.PUBLIC,
        **kwargs,
    )


def _set_author_name_only(item: ArchiveItem, author_name: str) -> None:
    ArchiveItem.objects.filter(pk=item.pk).update(author_name=author_name)
    item.refresh_from_db()


def _clear_author_links(item: ArchiveItem) -> None:
    ArchiveItemAuthor.objects.filter(archive_item=item).delete()


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


class ArchiveSearchAuthorContractTests(TestCase):
    def test_structured_names_win_over_stale_author_name_for_q_and_index(self):
        item = _public_manual("Stale vs structured")
        first = Author.objects.create(name="CanonicalFirstToken")
        second = Author.objects.create(name="CanonicalSecondToken")
        _link(item, second, position=1)
        _link(item, first, position=0)
        _set_author_name_only(item, "StaleAuthorToken")
        _rebuild(item.pk)

        self.assertEqual(
            searchable_author_names_for_item(_load_item(item.pk)),
            ("CanonicalFirstToken", "CanonicalSecondToken"),
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("CanonicalFirstToken", content.metadata_text)
        self.assertIn("CanonicalSecondToken", content.metadata_text)
        self.assertLess(
            content.metadata_text.index("CanonicalFirstToken"),
            content.metadata_text.index("CanonicalSecondToken"),
        )
        self.assertNotIn("StaleAuthorToken", content.metadata_text)

        public_qs = ArchiveItem.objects.filter(visibility=ArchiveItem.Visibility.PUBLIC)
        self.assertTrue(
            filter_archive_items_by_search_query(
                public_qs, "CanonicalFirstToken"
            )
            .filter(pk=item.pk)
            .exists()
        )
        self.assertFalse(
            filter_archive_items_by_search_query(public_qs, "StaleAuthorToken")
            .filter(pk=item.pk)
            .exists()
        )

        resp = self.client.get(reverse("archive-list"), {"q": "CanonicalFirstToken"})
        self.assertContains(resp, item.title)
        card = next(
            card for card in resp.context["browse_cards"] if card.item.pk == item.pk
        )
        self.assertEqual(card.search_match_source_label, MATCH_SOURCE_AUTHOR)

        stale_resp = self.client.get(reverse("archive-list"), {"q": "StaleAuthorToken"})
        self.assertNotContains(stale_resp, item.title)

    def test_zero_link_item_falls_back_to_trimmed_author_name(self):
        item = _public_manual("Legacy q item")
        _clear_author_links(item)
        _set_author_name_only(item, "  LegacyAuthorQToken  ")
        _rebuild(item.pk)

        loaded = _load_item(item.pk)
        self.assertEqual(
            searchable_author_names_for_item(loaded),
            ("LegacyAuthorQToken",),
        )
        content = build_archive_item_search_content(loaded)
        self.assertEqual(
            content.metadata_text.split(SEARCH_SEGMENT_SEPARATOR)[0],
            "LegacyAuthorQToken",
        )

        resp = self.client.get(reverse("archive-list"), {"q": "LegacyAuthorQToken"})
        self.assertContains(resp, item.title)
        card = next(
            card for card in resp.context["browse_cards"] if card.item.pk == item.pk
        )
        self.assertEqual(card.search_match_source_label, MATCH_SOURCE_AUTHOR)

    def test_match_source_uses_same_contract_as_index(self):
        item = _public_manual("Snippet contract")
        author = Author.objects.create(name="SnippetAuthorToken")
        _link(item, author, position=0)
        _set_author_name_only(item, "SnippetStaleToken")
        _rebuild(item.pk)
        loaded = _load_item(item.pk)
        from documents.models import ArchiveItemSearchIndex

        index = ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk)
        structured = build_archive_search_match_presentation(
            archive_item=loaded,
            search_index=index,
            terms=["SnippetAuthorToken"],
        )
        stale = build_archive_search_match_presentation(
            archive_item=loaded,
            search_index=index,
            terms=["SnippetStaleToken"],
        )
        self.assertIsNotNone(structured)
        self.assertEqual(structured.match_source_label, MATCH_SOURCE_AUTHOR)
        self.assertIsNone(stale)

    def test_search_index_build_prefetches_author_links(self):
        items = []
        for index in range(3):
            item = _public_manual(f"Prefetch author search {index}")
            author = Author.objects.create(name=f"PrefetchSearchAuthor{index}")
            _link(item, author, position=0)
            items.append(item)

        with CaptureQueriesContext(connection) as ctx:
            loaded = list(
                archive_items_for_search_index_build(
                    archive_item_ids=[item.pk for item in items]
                )
            )
            for item in loaded:
                build_archive_item_search_content(item)
        author_link_queries = [
            query["sql"]
            for query in ctx.captured_queries
            if "documents_archiveitemauthor" in query["sql"].lower()
        ]
        self.assertLessEqual(len(author_link_queries), 1)
        names = {
            list(item.author_links.all())[0].author.name for item in loaded
        }
        self.assertEqual(
            names,
            {f"PrefetchSearchAuthor{index}" for index in range(3)},
        )
