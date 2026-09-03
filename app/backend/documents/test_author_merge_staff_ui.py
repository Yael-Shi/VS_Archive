"""Staff Author merge confirmation UI and access control."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import Resolver404, resolve, reverse

from documents.models import ArchiveItem, ArchiveItemAuthor, Author
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_authors import AUTHOR_NOT_FOUND_ERROR
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.author_merge import (
    AUTHOR_MERGE_ID_REQUIRED_ERROR,
    AUTHOR_MERGE_SAME_ID_ERROR,
)
from documents.views import AUTHOR_MERGED_MSG


def _edit_url(author: Author) -> str:
    return reverse("archive-manage-author-edit", kwargs={"author_id": author.id})


def _merge_url(author: Author) -> str:
    return reverse("archive-manage-author-merge", kwargs={"author_id": author.id})


def _item(*, title: str, author_name: str = "") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
    )


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


class AuthorMergeRouteTests(TestCase):
    def test_merge_route_resolves_and_delete_remains_absent(self):
        match = resolve("/archive/manage/authors/1/merge/")
        self.assertEqual(match.url_name, "archive-manage-author-merge")
        self.assertEqual(
            reverse("archive-manage-author-merge", kwargs={"author_id": 1}),
            "/archive/manage/authors/1/merge/",
        )
        with self.assertRaises(Resolver404):
            resolve("/archive/manage/authors/1/delete/")

    def test_no_public_author_merge_routes(self):
        for path in (
            "/archive/authors/1/merge/",
            "/archive/author/1/merge/",
        ):
            with self.assertRaises(Resolver404):
                resolve(path)


class AuthorMergeAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_merge_staff",
            password="test-pass",
            is_staff=True,
        )
        self.keeper = Author.objects.create(name="Keeper UI")
        self.url = _merge_url(self.keeper)

    def test_staff_can_open_merge_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אישור מיזוג רשומת מחבר/ת")
        self.assertContains(resp, 'name="duplicate_id"')

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    def test_non_staff_is_forbidden(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="author_merge_family",
            password="test-pass",
        )
        user.groups.add(family_group)
        self.client.force_login(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_keeper_is_404(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-manage-author-merge", kwargs={"author_id": 999999})
        )
        self.assertEqual(resp.status_code, 404)


class AuthorMergeStaffFlowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_merge_flow_staff",
            password="test-pass",
            is_staff=True,
        )
        self.keeper = Author.objects.create(name="Keeper Flow")
        self.duplicate = Author.objects.create(name="Duplicate Flow")
        self.item = _item(title="Affected Flow Item", author_name="Duplicate Flow")
        _link(self.item, self.duplicate, position=0)
        self.client.force_login(self.staff)

    def test_edit_page_has_merge_section_and_index_does_not(self):
        edit = self.client.get(_edit_url(self.keeper))
        self.assertEqual(edit.status_code, 200)
        self.assertContains(edit, "מיזוג רשומת מחבר/ת כפולה")
        self.assertContains(edit, _merge_url(self.keeper))
        self.assertContains(edit, 'name="duplicate_id"')
        self.assertContains(edit, 'method="get"')

        index = self.client.get(reverse("archive-manage-authors"))
        self.assertEqual(index.status_code, 200)
        self.assertContains(index, _edit_url(self.keeper))
        self.assertNotContains(index, _merge_url(self.keeper))
        self.assertNotContains(index, "מיזוג רשומת מחבר/ת כפולה")
        self.assertNotIn("/merge/", index.content.decode())

    def test_get_without_duplicate_id_does_not_write(self):
        resp = self.client.get(_merge_url(self.keeper))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, AUTHOR_MERGE_ID_REQUIRED_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(
            ArchiveItemAuthor.objects.filter(author=self.duplicate).count(), 1
        )

    def test_get_with_duplicate_id_shows_preview_and_does_not_write(self):
        resp = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": str(self.duplicate.id)}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, str(self.keeper.id))
        self.assertContains(resp, "Keeper Flow")
        self.assertContains(resp, str(self.duplicate.id))
        self.assertContains(resp, "Duplicate Flow")
        self.assertContains(resp, "Affected Flow Item")
        self.assertContains(resp, 'name="confirm_merge"')
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())

    def test_invalid_missing_and_self_duplicate_are_rejected_safely(self):
        missing = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": "999999"}
        )
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, AUTHOR_NOT_FOUND_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())

        invalid = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": "abc"}
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())

        same = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": str(self.keeper.id)}
        )
        self.assertEqual(same.status_code, 200)
        self.assertContains(same, AUTHOR_MERGE_SAME_ID_ERROR)
        self.assertTrue(Author.objects.filter(pk=self.keeper.pk).exists())

    def test_post_without_confirm_does_not_merge(self):
        resp = self.client.post(
            _merge_url(self.keeper),
            data={"duplicate_id": str(self.duplicate.id)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Author.objects.filter(pk=self.duplicate.pk).exists())

    def test_successful_post_redirects_to_keeper_edit_with_success(self):
        resp = self.client.post(
            _merge_url(self.keeper),
            data={
                "duplicate_id": str(self.duplicate.id),
                "confirm_merge": "1",
            },
            follow=True,
        )
        self.assertContains(resp, AUTHOR_MERGED_MSG)
        self.assertEqual(resp.resolver_match.url_name, "archive-manage-author-edit")
        self.assertEqual(resp.resolver_match.kwargs["author_id"], self.keeper.id)
        self.assertTrue(Author.objects.filter(pk=self.keeper.pk).exists())
        self.assertFalse(Author.objects.filter(pk=self.duplicate.pk).exists())
        self.keeper.refresh_from_db()
        self.assertEqual(self.keeper.name, "Keeper Flow")
        self.item.refresh_from_db()
        self.assertEqual(self.item.author_name, "Keeper Flow")

        index = self.client.get(reverse("archive-manage-authors"))
        self.assertEqual(index.status_code, 200)
        self.assertContains(index, "Keeper Flow")
        self.assertNotContains(index, "Duplicate Flow")

    def test_public_surfaces_do_not_expose_author_merge(self):
        item = create_manual_text_archive_item(
            title="Public merge isolation",
            body="public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[self.keeper.id],
        )
        self.client.logout()
        public_list = self.client.get(reverse("archive-list"))
        public_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        )
        self.assertEqual(public_list.status_code, 200)
        self.assertEqual(public_detail.status_code, 200)
        merge_path = _merge_url(self.keeper)
        self.assertNotContains(public_list, merge_path)
        self.assertNotContains(public_detail, merge_path)
        self.assertNotContains(public_list, "אישור מיזוג רשומת מחבר/ת")
        self.assertNotContains(public_detail, "אישור מיזוג רשומת מחבר/ת")
