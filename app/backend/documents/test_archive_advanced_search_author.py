"""Public /archive/ advanced Author filter: Author.id + fail-closed legacy fallback."""

from __future__ import annotations

from urllib.parse import parse_qs

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    Author,
    PhotoContent,
)
from documents.services.archive_advanced_search import (
    archive_advanced_filter_choice_context,
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_items import create_manual_text_archive_item

User = get_user_model()


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _public_manual(title: str, **kwargs) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="body",
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


def _author_id(item: ArchiveItem) -> int:
    return ArchiveItemAuthor.objects.get(archive_item=item).author_id


class ArchiveAdvancedAuthorFilterTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family = User.objects.create_user(
            username="adv-author-family", password="x"
        )
        self.family.groups.add(self.family_group)
        self.url = reverse("archive-list")

    def test_structured_author_id_filters_without_join_fanout(self):
        first = Author.objects.create(name="Shared Filter Author")
        second = Author.objects.create(name="Other Filter Author")
        match = _public_manual("Multi-author match")
        _link(match, first, position=0)
        _link(match, second, position=1)
        other = _public_manual("Other structured")
        _link(other, second, position=0)

        filters = normalize_archive_advanced_filters({"author": str(first.id)})
        qs = filter_archive_items_by_advanced_filters(ArchiveItem.objects.all(), filters)
        self.assertEqual(_ids(qs), [match.pk])

        resp = self.client.get(self.url, {"author": str(first.id), "advanced": "1"})
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual([item.pk for item in resp.context["items"]], [match.pk])

    def test_renderable_photo_aia_matches_author_filter(self):
        author = Author.objects.create(name="Photo Advanced Author")
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo advanced album",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=item,
            position=1,
            original_file_key="photos/adv/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        _link(item, author, position=0)
        other = _public_manual("Not this photo author")
        resp = self.client.get(self.url, {"author": str(author.id), "advanced": "1"})
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual([row.pk for row in resp.context["items"]], [item.pk])
        self.assertContains(resp, "Photo advanced album")
        self.assertNotContains(resp, other.title)

    def test_duplicate_author_names_remain_distinct_filter_identities(self):
        earlier = Author.objects.create(name="Same Filter Name")
        later = Author.objects.create(name="Same Filter Name")
        item_a = _public_manual("Dup A")
        item_b = _public_manual("Dup B")
        _link(item_a, earlier, position=0)
        _link(item_b, later, position=0)

        a_ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"author": str(earlier.id)}),
            )
        )
        b_ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"author": str(later.id)}),
            )
        )
        self.assertEqual(a_ids, [item_a.pk])
        self.assertEqual(b_ids, [item_b.pk])

        resp = self.client.get(self.url, {"advanced": "1"})
        choices = list(resp.context["advanced_filter_author_choices"])
        same_name = [author for author in choices if author.name == "Same Filter Name"]
        self.assertEqual({author.pk for author in same_name}, {earlier.id, later.id})
        html = resp.content.decode("utf-8")
        self.assertIn(f'value="{earlier.id}"', html)
        self.assertIn(f'value="{later.id}"', html)

    def test_ambiguous_legacy_author_name_is_fail_closed(self):
        earlier = Author.objects.create(name="Ambiguous Ada")
        later = Author.objects.create(name="Ambiguous Ada")
        linked_a = _public_manual("Linked A")
        linked_b = _public_manual("Linked B")
        _link(linked_a, earlier, position=0)
        _link(linked_b, later, position=0)
        legacy = _public_manual("Legacy Ada")
        _clear_author_links(legacy)
        _set_author_name_only(legacy, "Ambiguous Ada")

        for author in (earlier, later):
            ids = _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"author": str(author.id)}),
                )
            )
            self.assertNotIn(legacy.pk, ids)
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"author": str(earlier.id)}),
                )
            ),
            [linked_a.pk],
        )

    def test_unique_legacy_author_name_matches_zero_link_items_only(self):
        author = Author.objects.create(name="Unique Bob")
        linked = _public_manual("Linked Bob")
        _link(linked, author, position=0)
        _set_author_name_only(linked, "Stale Bob")
        legacy = _public_manual("Legacy Bob")
        _clear_author_links(legacy)
        _set_author_name_only(legacy, "Unique Bob")
        other_legacy = _public_manual("Other leftover")
        _clear_author_links(other_legacy)
        _set_author_name_only(other_legacy, "Someone Else")

        ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"author": str(author.id)}),
                )
            )
        )
        self.assertEqual(ids, {linked.pk, legacy.pk})
        self.assertNotIn(other_legacy.pk, ids)

    def test_author_name_only_item_is_not_a_choice(self):
        linked = _public_manual("Choice linked", author_name="Choice Linked Author")
        legacy = _public_manual("Choice leftover")
        _clear_author_links(legacy)
        _set_author_name_only(legacy, "Orphan Legacy Name")

        resp = self.client.get(self.url, {"advanced": "1"})
        names = [author.name for author in resp.context["advanced_filter_author_choices"]]
        self.assertEqual(names, ["Choice Linked Author"])
        self.assertNotIn("Orphan Legacy Name", names)
        self.assertEqual(
            [author.pk for author in resp.context["advanced_filter_author_choices"]],
            [_author_id(linked)],
        )

    def test_unauthorized_authors_are_omitted_from_choices(self):
        public_item = _public_manual("Public author item", author_name="Public Choice Author")
        private_item = create_manual_text_archive_item(
            title="Private author item",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="Private Choice Author",
        )

        anon = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(
            [author.name for author in anon.context["advanced_filter_author_choices"]],
            ["Public Choice Author"],
        )
        self.assertNotIn(
            _author_id(private_item),
            [author.pk for author in anon.context["advanced_filter_author_choices"]],
        )

        self.client.force_login(self.family)
        family = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(
            {author.name for author in family.context["advanced_filter_author_choices"]},
            {"Public Choice Author", "Private Choice Author"},
        )
        self.assertIn(public_item.title, family.content.decode("utf-8"))

    def test_unauthorized_author_id_does_not_match_public_legacy_author_name(self):
        private_item = create_manual_text_archive_item(
            title="SECRET-PRIVATE-AUTHOR-ONLY",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="Shared Legacy Author Name",
        )
        private_author_id = _author_id(private_item)
        public_legacy = _public_manual("Public leftover same name")
        _clear_author_links(public_legacy)
        _set_author_name_only(public_legacy, "Shared Legacy Author Name")

        authorized = archive_browse_queryset_for_user(None)
        ids = _ids(
            filter_archive_items_by_advanced_filters(
                authorized,
                normalize_archive_advanced_filters(
                    {"author": str(private_author_id)}
                ),
            )
        )
        self.assertNotIn(public_legacy.pk, ids)
        self.assertNotIn(private_item.pk, ids)

        resp = self.client.get(
            self.url,
            {"author": str(private_author_id), "advanced": "1"},
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertNotIn(public_legacy.title, html)
        self.assertNotIn("SECRET-PRIVATE-AUTHOR-ONLY", html)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])

        self.client.force_login(self.family)
        family_ids = {
            item.pk
            for item in self.client.get(
                self.url, {"author": str(private_author_id)}
            ).context["items"]
        }
        self.assertEqual(family_ids, {private_item.pk, public_legacy.pk})

    def test_selected_value_chip_and_removal_use_author_id(self):
        item = _public_manual("Chip author item", author_name="Chip Structured Author")
        author_id = _author_id(item)
        resp = self.client.get(
            self.url,
            {"author": str(author_id), "q": "Chip author item", "advanced": "1"},
        )
        self.assertEqual(resp.context["advanced_filter_author"], author_id)
        html = resp.content.decode("utf-8")
        self.assertRegex(html, rf'value="{author_id}"\s+selected')
        chips = resp.context["active_filter_chips"]
        author_chip = next(chip for chip in chips if chip["kind"] == "author")
        self.assertEqual(author_chip["value"], "Chip Structured Author")
        parsed = parse_qs(str(author_chip["remove_href_suffix"]).lstrip("?"))
        self.assertNotIn("author", parsed)
        self.assertEqual(parsed["q"], ["Chip author item"])
        self.assertIn(item.title, html)

        removed = self.client.get(f"{self.url}{author_chip['remove_href_suffix']}")
        self.assertIsNone(removed.context["advanced_filter_author"])
        self.assertEqual(removed.context["q"], "Chip author item")

    def test_pagination_and_other_filters_preserve_author_id(self):
        first = _public_manual("AUTHORPAGE-00", author_name="Page Structured Author")
        author_id = _author_id(first)
        for index in range(1, 50):
            _public_manual(
                f"AUTHORPAGE-{index:02d}",
                author_name="Page Structured Author",
            )
        resp = self.client.get(
            self.url,
            {
                "author": str(author_id),
                "q": "AUTHORPAGE",
                "per_page": "24",
                "page": "2",
            },
        )
        self.assertEqual(resp.status_code, 200)
        parsed = parse_qs(str(resp.context["prev_href_suffix"]).lstrip("?"))
        self.assertEqual(parsed["author"], [str(author_id)])
        self.assertEqual(parsed["q"], ["AUTHORPAGE"])
        self.assertIn(f"author={author_id}", resp.content.decode("utf-8"))

    def test_ordinary_and_q_only_skip_author_choice_queries(self):
        _public_manual("Skip author choices", author_name="Skip Author")
        authorized = archive_browse_queryset_for_user(None)
        with CaptureQueriesContext(connection) as loaded:
            archive_advanced_filter_choice_context(authorized)
        self.assertTrue(
            any(
                "documents_author" in query["sql"].lower().replace('"', "")
                and "documents_archiveitemauthor" in query["sql"].lower().replace('"', "")
                for query in loaded.captured_queries
            )
        )

        plain = self.client.get(self.url)
        self.assertFalse(plain.context["load_advanced_choices"])
        self.assertEqual(plain.context["advanced_filter_author_choices"], ())

        q_only = self.client.get(self.url, {"q": "Skip"})
        self.assertFalse(q_only.context["load_advanced_choices"])
        self.assertEqual(q_only.context["advanced_filter_author_choices"], ())
        self.assertNotIn('id="archive-filter-author"', q_only.content.decode("utf-8"))
