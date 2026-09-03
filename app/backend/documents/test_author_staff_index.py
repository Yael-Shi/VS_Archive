"""Staff Author index at /archive/manage/authors/."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse

from documents.models import ArchiveItem, ArchiveItemAuthor, Author, Person, PersonAlias
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_manual_text_archive_item


def _create_item(*, title: str) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


def _edit_url(author: Author) -> str:
    return reverse("archive-manage-author-edit", kwargs={"author_id": author.id})


def _table_sql(captured_queries, table: str) -> list[str]:
    needle = table.lower()
    return [
        query["sql"] for query in captured_queries if needle in query["sql"].lower()
    ]


class AuthorStaffIndexRouteTests(TestCase):
    def test_index_route_resolves(self):
        match = resolve("/archive/manage/authors/")
        self.assertEqual(match.url_name, "archive-manage-authors")
        self.assertEqual(reverse("archive-manage-authors"), "/archive/manage/authors/")

    def test_delete_route_remains_absent_and_merge_route_resolves(self):
        match = resolve("/archive/manage/authors/")
        self.assertEqual(match.url_name, "archive-manage-authors")
        self.assertEqual(reverse("archive-manage-authors"), "/archive/manage/authors/")
        merge = resolve("/archive/manage/authors/1/merge/")
        self.assertEqual(merge.url_name, "archive-manage-author-merge")
        with self.assertRaises(Resolver404):
            resolve("/archive/manage/authors/1/delete/")

    def test_public_author_catalog_and_detail_routes_exist(self):
        index = resolve("/archive/authors/")
        self.assertEqual(index.url_name, "archive-authors-index")
        self.assertEqual(reverse("archive-authors-index"), "/archive/authors/")
        detail = resolve("/archive/authors/1/")
        self.assertEqual(detail.url_name, "archive-author-detail")
        self.assertEqual(
            reverse("archive-author-detail", kwargs={"author_id": 1}),
            "/archive/authors/1/",
        )
        with self.assertRaises(Resolver404):
            resolve("/archive/author/1/")


class AuthorStaffIndexAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_index_staff",
            password="test-pass",
            is_staff=True,
        )
        self.url = reverse("archive-manage-authors")

    def test_staff_can_open_authors_index(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ניהול מחברים")
        self.assertContains(resp, "אין רשומות מחבר.")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    def test_non_staff_is_forbidden(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="author_index_family",
            password="test-pass",
        )
        user.groups.add(family_group)
        self.client.force_login(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_post_is_method_not_allowed(self):
        self.client.force_login(self.staff)
        author = Author.objects.create(name="Post Guard Author")
        resp = self.client.post(self.url, data={"q": "ignored", "name": "mutated"})
        self.assertEqual(resp.status_code, 405)
        author.refresh_from_db()
        self.assertEqual(author.name, "Post Guard Author")
        self.assertEqual(Author.objects.count(), 1)


class AuthorStaffIndexPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_index_page_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.url = reverse("archive-manage-authors")

    def test_rows_are_ordered_by_name_then_id(self):
        zeta = Author.objects.create(name="Zeta Author")
        alpha_later = Author.objects.create(name="Alpha Author")
        alpha_earlier = Author.objects.create(name="Alpha Author")
        beta = Author.objects.create(name="Beta Author")
        if alpha_earlier.id > alpha_later.id:
            alpha_earlier, alpha_later = alpha_later, alpha_earlier
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        authors = list(resp.context["authors"])
        self.assertEqual(
            [author.id for author in authors],
            [alpha_earlier.id, alpha_later.id, beta.id, zeta.id],
        )
        self.assertContains(resp, _edit_url(alpha_earlier))
        self.assertContains(resp, _edit_url(alpha_later))
        self.assertContains(resp, _edit_url(beta))
        self.assertContains(resp, _edit_url(zeta))

    def test_search_is_case_insensitive_substring_on_author_name_only(self):
        matched = Author.objects.create(name="Yaakov Cohen")
        unrelated = Author.objects.create(name="Rachel Levy")
        person = Person.objects.create(name="Cohen Person")
        PersonAlias.objects.create(person=person, name="CohenAliasToken")
        item = _create_item(title="Unrelated item")
        item.author_name = "Cohen on item only"
        item.save(update_fields=["author_name"])

        resp = self.client.get(self.url, data={"q": "cohEN"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, matched.name)
        self.assertContains(resp, str(matched.id))
        self.assertContains(resp, _edit_url(matched))
        self.assertNotContains(resp, unrelated.name)
        self.assertNotContains(resp, _edit_url(unrelated))
        self.assertNotContains(resp, person.name)
        self.assertNotContains(resp, "CohenAliasToken")
        self.assertNotContains(resp, "Cohen on item only")
        self.assertEqual(list(resp.context["authors"]), [matched])
        self.assertContains(resp, 'value="cohEN"')

    def test_search_trims_whitespace_and_whitespace_only_q_lists_all(self):
        matched = Author.objects.create(name="Trimmed Cohen")
        other = Author.objects.create(name="Other Author")

        trimmed = self.client.get(self.url, data={"q": "  cohEN  "})
        self.assertEqual(trimmed.status_code, 200)
        self.assertEqual(list(trimmed.context["authors"]), [matched])
        self.assertContains(trimmed, 'value="cohEN"')
        self.assertContains(trimmed, "ניקוי")
        self.assertNotContains(trimmed, other.name)

        blank = self.client.get(self.url, data={"q": "   "})
        self.assertEqual(blank.status_code, 200)
        expected = sorted((matched, other), key=lambda author: (author.name, author.id))
        self.assertEqual(
            [author.id for author in blank.context["authors"]],
            [author.id for author in expected],
        )
        self.assertEqual(blank.context["q"], "")
        self.assertNotContains(blank, "ניקוי")

    def test_empty_filtered_state_is_clear_and_preserves_q(self):
        Author.objects.create(name="Existing Author")
        resp = self.client.get(self.url, data={"q": "NoSuchAuthorToken"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "לא נמצאו מחברים תואמים.")
        self.assertContains(resp, 'value="NoSuchAuthorToken"')
        self.assertContains(resp, "ניקוי")
        self.assertNotContains(resp, "Existing Author")
        self.assertEqual(list(resp.context["authors"]), [])

    def test_archive_item_author_count_includes_multiple_items(self):
        item_a = _create_item(title="Author count A")
        item_b = _create_item(title="Author count B")
        both = Author.objects.create(name="Linked Twice")
        once = Author.objects.create(name="Linked Once")
        unlinked = Author.objects.create(name="Unlinked Author")
        coauthor = Author.objects.create(name="Coauthor On A")
        _link(item_a, both, position=0)
        _link(item_b, both, position=0)
        _link(item_a, coauthor, position=1)
        _link(item_b, once, position=1)

        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        by_id = {author.id: author for author in resp.context["authors"]}
        self.assertEqual(by_id[both.id].archive_item_author_count, 2)
        self.assertEqual(by_id[once.id].archive_item_author_count, 1)
        self.assertEqual(by_id[coauthor.id].archive_item_author_count, 1)
        self.assertEqual(by_id[unlinked.id].archive_item_author_count, 0)
        html = resp.content.decode()
        self.assertIn(_edit_url(both), html)
        self.assertEqual(html.count(_edit_url(both)), 1)

    def test_index_does_not_n_plus_one_on_archive_item_author_counts(self):
        authors = [
            Author.objects.create(name=f"Index author {index}") for index in range(4)
        ]
        item = _create_item(title="Index query item")
        for position, author in enumerate(authors):
            _link(item, author, position=position)

        self.client.get(self.url)

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(self.url)
        self.assertEqual(few_resp.status_code, 200)

        extra_item = _create_item(title="Index query item extra")
        extra_authors = [
            Author.objects.create(name=f"Extra index {index}") for index in range(4)
        ]
        for position, author in enumerate(extra_authors):
            _link(extra_item, author, position=position)
        for position, author in enumerate(authors):
            _link(extra_item, author, position=len(extra_authors) + position)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(self.url)
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(
            len(_table_sql(few_ctx.captured_queries, "documents_archiveitemauthor")),
            len(_table_sql(many_ctx.captured_queries, "documents_archiveitemauthor")),
        )
        self.assertGreaterEqual(
            len(_table_sql(few_ctx.captured_queries, "documents_archiveitemauthor")),
            1,
        )
        by_id = {author.id: author for author in many_resp.context["authors"]}
        self.assertEqual(by_id[authors[0].id].archive_item_author_count, 2)

    def test_management_page_links_to_authors_index(self):
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("archive-manage-authors"))
        self.assertContains(resp, "ניהול מחברים")
        self.assertContains(resp, reverse("archive-manage-people"))
        self.assertContains(resp, "ניהול אנשים")

    def test_public_surfaces_do_not_link_to_staff_authors_index(self):
        author = Author.objects.create(name="Public Index Author")
        item = create_manual_text_archive_item(
            title="Public index item",
            body="public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[author.id],
        )
        self.client.logout()
        public_list = self.client.get(reverse("archive-list"))
        public_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        )
        self.assertEqual(public_list.status_code, 200)
        self.assertEqual(public_detail.status_code, 200)
        self.assertNotContains(public_list, reverse("archive-manage-authors"))
        self.assertNotContains(public_detail, reverse("archive-manage-authors"))
        self.assertNotContains(public_list, "ניהול מחברים")
        self.assertNotContains(public_detail, "ניהול מחברים")
