"""Public Authors index: membership, search, counts, pagination."""

from __future__ import annotations

from pathlib import Path

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    Author,
    Person,
    PersonAlias,
    PhotoContent,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.author_public import author_public_page_url


def _index_url() -> str:
    return reverse("archive-authors-index")


def _public_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _private_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Private body",
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _restricted_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Restricted body",
        visibility=ArchiveItem.Visibility.RESTRICTED,
    )


def _link(item: ArchiveItem, author: Author, *, position: int = 0) -> None:
    ArchiveItemAuthor.objects.create(
        archive_item=item, author=author, position=position
    )


def _create_photo_item(
    *,
    title: str,
    visibility: str = ArchiveItem.Visibility.PUBLIC,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int = 1,
    uploaded: bool = True,
    failed: bool = False,
) -> PhotoContent:
    if failed:
        status = PhotoContent.UploadStatus.FAILED
    elif uploaded:
        status = PhotoContent.UploadStatus.UPLOADED
    else:
        status = PhotoContent.UploadStatus.PENDING
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=status,
        upload_error="",
    )
    if uploaded and not failed:
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


def _grant_restricted_permission(user: User) -> User:
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    if hasattr(user, "_perm_cache"):
        delattr(user, "_perm_cache")
    if hasattr(user, "_user_perm_cache"):
        delattr(user, "_user_perm_cache")
    return user


def _row_names(response) -> list[str]:
    return [row.name for row in response.context["author_rows"]]


def _row_hrefs(response) -> list[str]:
    return [row.href for row in response.context["author_rows"]]


def _count_for(response, name: str) -> int:
    for row in response.context["author_rows"]:
        if row.name == name:
            return row.item_count
    raise AssertionError(f"no row named {name!r}")


def _authors_index_list_html(response) -> str:
    html = response.content.decode("utf-8")
    start = html.find('<ul class="archive-authors-index-list">')
    if start == -1:
        return ""
    end = html.find("</ul>", start)
    if end == -1:
        raise AssertionError("authors index list is not closed")
    return html[start : end + len("</ul>")]


class AuthorsPublicIndexRouteTests(TestCase):
    def test_named_route_and_empty_state(self):
        self.assertEqual(_index_url(), "/archive/authors/")
        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחברים")
        self.assertContains(resp, "אין מחברים להצגה.")
        self.assertNotContains(resp, reverse("archive-manage-authors"))
        self.assertNotContains(resp, "ניהול מחברים")
        self.assertNotContains(resp, "מזהה")


class AuthorsPublicIndexVisibilityTests(TestCase):
    def setUp(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family = User.objects.create_user(
            username="authors-index-family", password="x"
        )
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="authors-index-restricted", password="x")
        )

    def test_visibility_and_renderability_filter_membership(self):
        public_author = Author.objects.create(name="Index Public Author")
        _link(_public_manual("Public letter"), public_author)
        private_author = Author.objects.create(name="Index Private Author")
        _link(_private_manual("Private letter"), private_author)
        restricted_author = Author.objects.create(name="Index Restricted Author")
        _link(_restricted_manual("Restricted letter"), restricted_author)
        pending_author = Author.objects.create(name="Index Pending Author")
        pending_item = _create_photo_item(title="Pending album")
        _add_photo(pending_item, uploaded=False)
        _link(pending_item, pending_author)
        unlinked = Author.objects.create(name="Index Unlinked Author")

        anon = self.client.get(_index_url())
        self.assertEqual(anon.status_code, 200)
        self.assertEqual(_row_names(anon), ["Index Public Author"])
        self.assertNotIn("Index Private Author", _row_names(anon))
        self.assertNotIn("Index Restricted Author", _row_names(anon))
        self.assertNotIn("Index Pending Author", _row_names(anon))
        self.assertNotIn("Index Unlinked Author", _row_names(anon))
        self.assertNotContains(anon, unlinked.name)

        self.client.force_login(self.family)
        family = self.client.get(_index_url())
        self.assertEqual(
            set(_row_names(family)),
            {"Index Public Author", "Index Private Author"},
        )
        self.assertNotIn("Index Restricted Author", _row_names(family))

        self.client.force_login(self.restricted_user)
        restricted = self.client.get(_index_url())
        self.assertEqual(
            set(_row_names(restricted)),
            {"Index Public Author", "Index Restricted Author"},
        )


class AuthorsPublicIndexCountAndOrderTests(TestCase):
    def test_distinct_counts_and_duplicate_names(self):
        once = Author.objects.create(name="Count Ada")
        _link(_public_manual("Ada letter"), once)

        twice = Author.objects.create(name="Count Bess")
        _link(_public_manual("Bess first"), twice, position=0)
        _link(_public_manual("Bess second"), twice, position=0)

        coauthor_item = _public_manual("Shared letter")
        first_co = Author.objects.create(name="Count Cara")
        second_co = Author.objects.create(name="Count Dana")
        _link(coauthor_item, first_co, position=0)
        _link(coauthor_item, second_co, position=1)

        first_same = Author.objects.create(name="Same Name")
        second_same = Author.objects.create(name="Same Name")
        _link(_public_manual("First same"), first_same)
        _link(_public_manual("Second same"), second_same)

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_count_for(resp, "Count Ada"), 1)
        self.assertEqual(_count_for(resp, "Count Bess"), 2)
        self.assertEqual(_count_for(resp, "Count Cara"), 1)
        self.assertEqual(_count_for(resp, "Count Dana"), 1)
        names = _row_names(resp)
        hrefs = _row_hrefs(resp)
        self.assertEqual(
            names,
            [
                "Count Ada",
                "Count Bess",
                "Count Cara",
                "Count Dana",
                "Same Name",
                "Same Name",
            ],
        )
        self.assertEqual(hrefs.count(author_public_page_url(first_same.id)), 1)
        self.assertEqual(hrefs.count(author_public_page_url(second_same.id)), 1)
        self.assertNotEqual(
            author_public_page_url(first_same.id),
            author_public_page_url(second_same.id),
        )
        same_indexes = [i for i, name in enumerate(names) if name == "Same Name"]
        self.assertEqual(
            [hrefs[i] for i in same_indexes],
            [
                author_public_page_url(first_same.id),
                author_public_page_url(second_same.id),
            ],
        )

    def test_count_copy_uses_singular_and_plural(self):
        one = Author.objects.create(name="Singular Count Author")
        _link(_public_manual("One letter"), one)
        two = Author.objects.create(name="Plural Count Author")
        _link(_public_manual("First of two"), two)
        _link(_public_manual("Second of two"), two)

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_count_for(resp, "Singular Count Author"), 1)
        self.assertEqual(_count_for(resp, "Plural Count Author"), 2)
        self.assertContains(resp, "1 פריט")
        self.assertNotContains(resp, "1 פריטים")
        self.assertNotContains(resp, "פריט אחד")
        self.assertContains(resp, "2 פריטים")

    def test_order_is_name_then_id(self):
        first_alpha = Author.objects.create(name="Alpha")
        _link(_public_manual("First alpha letter"), first_alpha)
        beta = Author.objects.create(name="Beta")
        _link(_public_manual("Beta letter"), beta)
        second_alpha = Author.objects.create(name="Alpha")
        _link(_public_manual("Second alpha letter"), second_alpha)
        resp = self.client.get(_index_url())
        self.assertEqual(
            _row_hrefs(resp),
            [
                author_public_page_url(first_alpha.id),
                author_public_page_url(second_alpha.id),
                author_public_page_url(beta.id),
            ],
        )


class AuthorsPublicIndexSearchTests(TestCase):
    def test_search_is_case_insensitive_substring_on_author_name_only(self):
        matched = Author.objects.create(name="Yaakov Cohen")
        _link(_public_manual("Matched letter"), matched)
        unrelated = Author.objects.create(name="Rachel Levy")
        _link(_public_manual("Unrelated letter"), unrelated)
        person = Person.objects.create(name="Cohen Person")
        PersonAlias.objects.create(person=person, name="CohenAliasToken")
        ArchiveItemPerson.objects.create(
            archive_item=_public_manual("Person letter"),
            person=person,
        )
        author_name_only = _public_manual("Author name only letter")
        author_name_only.author_name = "Cohen on item only"
        author_name_only.save(update_fields=["author_name"])

        resp = self.client.get(_index_url(), {"q": "cohEN"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_row_names(resp), ["Yaakov Cohen"])
        self.assertEqual(_row_hrefs(resp), [author_public_page_url(matched.id)])
        self.assertContains(resp, 'value="cohEN"')
        list_html = _authors_index_list_html(resp)
        self.assertIn("Yaakov Cohen", list_html)
        self.assertNotIn("Rachel Levy", list_html)
        self.assertNotIn("Cohen Person", list_html)
        self.assertNotIn("CohenAliasToken", list_html)
        self.assertNotIn("Cohen on item only", list_html)

        missing = self.client.get(_index_url(), {"q": "NoSuchAuthorToken"})
        self.assertEqual(_row_names(missing), [])
        self.assertContains(missing, "לא נמצאו מחברים תואמים.")

    def test_search_does_not_leak_unauthorized_authors(self):
        public_author = Author.objects.create(name="Visible Search Author")
        _link(_public_manual("Visible letter"), public_author)
        private_author = Author.objects.create(name="Hidden Search Author")
        _link(_private_manual("Hidden letter"), private_author)

        resp = self.client.get(_index_url(), {"q": "Search Author"})
        self.assertEqual(_row_names(resp), ["Visible Search Author"])
        html = resp.content.decode("utf-8")
        self.assertNotIn("Hidden Search Author", html)
        self.assertNotIn(author_public_page_url(private_author.id), html)


class AuthorsPublicIndexMembershipIsolationTests(TestCase):
    def test_author_name_only_item_does_not_count(self):
        linked = Author.objects.create(name="Linked Count Author")
        _link(_public_manual("Linked letter"), linked)
        name_only = _public_manual("Name only letter")
        name_only.author_name = linked.name
        name_only.save(update_fields=["author_name"])
        unlinked_same_name = Author.objects.create(name=linked.name)
        name_only_unlinked_item = _public_manual("Unlinked name only")
        name_only_unlinked_item.author_name = unlinked_same_name.name
        name_only_unlinked_item.save(update_fields=["author_name"])

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        hrefs = _row_hrefs(resp)
        self.assertEqual(hrefs.count(author_public_page_url(linked.id)), 1)
        self.assertNotIn(author_public_page_url(unlinked_same_name.id), hrefs)
        self.assertEqual(_count_for(resp, linked.name), 1)

    def test_no_person_inference(self):
        author = Author.objects.create(name="Shared Token")
        person = Person.objects.create(name="Shared Token")
        ArchiveItemPerson.objects.create(
            archive_item=_public_manual("Person only letter"),
            person=person,
        )
        unlinked = self.client.get(_index_url())
        self.assertEqual(_row_names(unlinked), [])
        self.assertNotIn(
            author_public_page_url(author.id),
            unlinked.content.decode("utf-8"),
        )

        _link(_public_manual("Author letter"), author)
        linked = self.client.get(_index_url())
        self.assertEqual(_row_hrefs(linked), [author_public_page_url(author.id)])
        self.assertEqual(_count_for(linked, "Shared Token"), 1)

    def test_no_staff_urls_or_id_column(self):
        author = Author.objects.create(name="Public Isolation Author")
        _link(_public_manual("Isolation letter"), author)
        Person.objects.create(name="UniquePersonTokenXYZ")
        resp = self.client.get(_index_url())
        html = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(reverse("archive-manage-authors"), html)
        self.assertNotIn(
            reverse("archive-manage-author-edit", kwargs={"author_id": author.id}),
            html,
        )
        self.assertNotIn("ניהול מחברים", html)
        self.assertNotIn("UniquePersonTokenXYZ", html)
        self.assertNotIn("מזהה", html)
        self.assertNotIn(f">{author.id}<", html)
        self.assertIn(author_public_page_url(author.id), html)


class AuthorsPublicIndexPaginationTests(TestCase):
    def test_paginates_at_48_and_preserves_q(self):
        token = "AuthorsIndexSearchToken"
        authors = []
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1):
            author = Author.objects.create(name=f"{token} {index:02d}")
            _link(_public_manual(f"Letter {index:02d}"), author)
            authors.append(author)
        decoy = Author.objects.create(name="Unrelated decoy")
        _link(_public_manual("Decoy letter"), decoy)

        page1 = self.client.get(_index_url(), {"q": token})
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.context["total_count"], 49)
        self.assertEqual(len(page1.context["author_rows"]), 48)
        self.assertTrue(page1.context["show_page_nav"])
        self.assertContains(page1, "?q=AuthorsIndexSearchToken&amp;page=2")
        self.assertNotIn(decoy.name, _row_names(page1))

        page2 = self.client.get(_index_url(), {"q": token, "page": "2"})
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.context["author_rows"]), 1)
        self.assertContains(page2, "הקודם")
        html2 = page2.content.decode("utf-8")
        self.assertIn("q=AuthorsIndexSearchToken", html2)
        self.assertNotIn(decoy.name, html2)

        clamped = self.client.get(_index_url(), {"q": token, "page": "999"})
        self.assertEqual(clamped.context["page"], 2)
        self.assertEqual(_row_hrefs(clamped), _row_hrefs(page2))


class AuthorsPublicIndexLayoutTests(TestCase):
    def test_css_defines_two_column_authors_grid_without_browse_list(self):
        css = (
            Path(__file__).resolve().parents[1] / "public/static/public/app.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".archive-authors-index-list", css)
        self.assertIn(
            ".archive-authors-index-list {\n    grid-template-columns: repeat(2, minmax(0, 1fr));",
            css,
        )
        self.assertNotIn(
            ".archive-authors-index-list {\n    grid-template-columns: repeat(3,",
            css,
        )


class AuthorsPublicIndexQueryCountTests(TestCase):
    def test_query_count_does_not_grow_with_listed_authors(self):
        for index in range(2):
            author = Author.objects.create(name=f"Query Author {index}")
            _link(_public_manual(f"Query letter {index}"), author)

        self.client.get(_index_url())
        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(_index_url())
        self.assertEqual(few_resp.status_code, 200)
        few_total = len(few_ctx.captured_queries)
        few_aia = sum(
            1
            for query in few_ctx.captured_queries
            if "documents_archiveitemauthor" in query["sql"].lower()
        )

        for index in range(4):
            author = Author.objects.create(name=f"Query Author extra {index}")
            _link(_public_manual(f"Query extra {index}"), author)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(_index_url())
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(len(many_resp.context["author_rows"]), 6)
        many_total = len(many_ctx.captured_queries)
        many_aia = sum(
            1
            for query in many_ctx.captured_queries
            if "documents_archiveitemauthor" in query["sql"].lower()
        )
        self.assertEqual(few_aia, many_aia)
        self.assertGreaterEqual(few_aia, 1)
        self.assertLessEqual(many_total - few_total, 1)
