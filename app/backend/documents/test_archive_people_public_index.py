"""Public People index: membership, search, counts, pagination."""

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
    ArchiveItemPerson,
    Author,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    person_public_page_url,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.photo_gallery import public_photo_detail_url


def _index_url() -> str:
    return reverse("archive-people-index")


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


def _link(item: ArchiveItem, person: Person) -> None:
    ArchiveItemPerson.objects.create(archive_item=item, person=person)


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
    return [row.name for row in response.context["people_rows"]]


def _row_hrefs(response) -> list[str]:
    return [row.href for row in response.context["people_rows"]]


def _count_for(response, name: str) -> int:
    for row in response.context["people_rows"]:
        if row.name == name:
            return row.item_count
    raise AssertionError(f"no row named {name!r}")


def _people_index_list_html(response) -> str:
    html = response.content.decode("utf-8")
    start = html.find('<ul class="archive-people-index-list">')
    if start == -1:
        return ""
    end = html.find("</ul>", start)
    if end == -1:
        raise AssertionError("people index list is not closed")
    return html[start : end + len("</ul>")]


class PeoplePublicIndexRouteTests(TestCase):
    def test_named_route_and_empty_state(self):
        self.assertEqual(_index_url(), "/archive/people/")
        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים")
        self.assertContains(resp, "אין אנשים להצגה.")
        self.assertNotContains(resp, reverse("archive-manage-people"))
        self.assertNotContains(resp, "ניהול אנשים")
        self.assertNotContains(resp, "מזהה")


class PeoplePublicIndexVisibilityTests(TestCase):
    def setUp(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family = User.objects.create_user(
            username="people-index-family", password="x"
        )
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="people-index-restricted", password="x")
        )

    def test_visibility_and_renderability_filter_membership(self):
        public_person = Person.objects.create(name="Index Public Person")
        _link(_public_manual("Public letter"), public_person)
        private_person = Person.objects.create(name="Index Private Person")
        _link(_private_manual("Private letter"), private_person)
        restricted_person = Person.objects.create(name="Index Restricted Person")
        _link(_restricted_manual("Restricted letter"), restricted_person)
        pending_person = Person.objects.create(name="Index Pending Person")
        pending_item = _create_photo_item(title="Pending album")
        _add_photo(pending_item, uploaded=False)
        _link(pending_item, pending_person)
        unlinked = Person.objects.create(name="Index Unlinked Person")

        anon = self.client.get(_index_url())
        self.assertEqual(anon.status_code, 200)
        self.assertEqual(_row_names(anon), ["Index Public Person"])
        self.assertNotIn("Index Private Person", _row_names(anon))
        self.assertNotIn("Index Restricted Person", _row_names(anon))
        self.assertNotIn("Index Pending Person", _row_names(anon))
        self.assertNotIn("Index Unlinked Person", _row_names(anon))
        self.assertNotContains(anon, unlinked.name)

        self.client.force_login(self.family)
        family = self.client.get(_index_url())
        self.assertEqual(
            set(_row_names(family)),
            {"Index Public Person", "Index Private Person"},
        )
        self.assertNotIn("Index Restricted Person", _row_names(family))

        self.client.force_login(self.restricted_user)
        restricted = self.client.get(_index_url())
        self.assertEqual(
            set(_row_names(restricted)),
            {"Index Public Person", "Index Restricted Person"},
        )

    def test_photoperson_only_is_included(self):
        person = Person.objects.create(name="Index PP Only")
        item = _create_photo_item(title="PP only album")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        resp = self.client.get(_index_url())
        self.assertEqual(_row_names(resp), ["Index PP Only"])
        self.assertEqual(_count_for(resp, "Index PP Only"), 1)
        self.assertEqual(_row_hrefs(resp), [person_public_page_url(person.id)])
        self.assertNotIn(public_photo_detail_url(item.id, photo.id), _row_hrefs(resp))


class PeoplePublicIndexCountAndOrderTests(TestCase):
    def test_distinct_union_counts_and_duplicate_names(self):
        aip_only = Person.objects.create(name="Count Ada")
        _link(_public_manual("AIP letter"), aip_only)

        pp_only = Person.objects.create(name="Count Bess")
        pp_item = _create_photo_item(title="PP album")
        PhotoPerson.objects.create(photo_content=_add_photo(pp_item), person=pp_only)

        dual = Person.objects.create(name="Count Cara")
        dual_item = _create_photo_item(title="Dual album")
        dual_photo = _add_photo(dual_item)
        _link(dual_item, dual)
        PhotoPerson.objects.create(photo_content=dual_photo, person=dual)

        multi_photo = Person.objects.create(name="Count Dana")
        album = _create_photo_item(title="Multi photo album")
        first = _add_photo(album, position=1)
        second = _add_photo(album, position=2)
        PhotoPerson.objects.create(photo_content=first, person=multi_photo)
        PhotoPerson.objects.create(photo_content=second, person=multi_photo)

        first_same = Person.objects.create(name="Same Name")
        second_same = Person.objects.create(name="Same Name")
        _link(_public_manual("First same"), first_same)
        _link(_public_manual("Second same"), second_same)

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_count_for(resp, "Count Ada"), 1)
        self.assertEqual(_count_for(resp, "Count Bess"), 1)
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
        self.assertEqual(hrefs.count(person_public_page_url(first_same.id)), 1)
        self.assertEqual(hrefs.count(person_public_page_url(second_same.id)), 1)
        self.assertNotEqual(
            person_public_page_url(first_same.id),
            person_public_page_url(second_same.id),
        )
        same_indexes = [i for i, name in enumerate(names) if name == "Same Name"]
        self.assertEqual(
            [hrefs[i] for i in same_indexes],
            [
                person_public_page_url(first_same.id),
                person_public_page_url(second_same.id),
            ],
        )

    def test_count_copy_uses_singular_and_plural(self):
        one = Person.objects.create(name="Singular Count Person")
        _link(_public_manual("One letter"), one)
        two = Person.objects.create(name="Plural Count Person")
        _link(_public_manual("First of two"), two)
        _link(_public_manual("Second of two"), two)

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_count_for(resp, "Singular Count Person"), 1)
        self.assertEqual(_count_for(resp, "Plural Count Person"), 2)
        self.assertContains(resp, "1 פריט")
        self.assertNotContains(resp, "1 פריטים")
        self.assertNotContains(resp, "פריט אחד")
        self.assertContains(resp, "2 פריטים")

    def test_order_is_name_then_id(self):
        first_alpha = Person.objects.create(name="Alpha")
        _link(_public_manual("First alpha letter"), first_alpha)
        beta = Person.objects.create(name="Beta")
        _link(_public_manual("Beta letter"), beta)
        second_alpha = Person.objects.create(name="Alpha")
        _link(_public_manual("Second alpha letter"), second_alpha)
        resp = self.client.get(_index_url())
        self.assertEqual(
            _row_hrefs(resp),
            [
                person_public_page_url(first_alpha.id),
                person_public_page_url(second_alpha.id),
                person_public_page_url(beta.id),
            ],
        )


class PeoplePublicIndexSearchTests(TestCase):
    def test_canonical_and_alias_search_without_displaying_aliases(self):
        matched = Person.objects.create(name="CanonicalMatched")
        PersonAlias.objects.create(person=matched, name="HiddenAliasToken")
        alias_only = Person.objects.create(name="OtherCanonical")
        PersonAlias.objects.create(person=alias_only, name="AliasSearchToken")
        unrelated = Person.objects.create(name="UnrelatedPerson")
        _link(_public_manual("Matched letter"), matched)
        _link(_public_manual("Alias letter"), alias_only)
        _link(_public_manual("Unrelated letter"), unrelated)

        canonical = self.client.get(_index_url(), {"q": "CanonicalMatched"})
        self.assertEqual(_row_names(canonical), ["CanonicalMatched"])
        canonical_list = _people_index_list_html(canonical)
        self.assertIn("CanonicalMatched", canonical_list)
        self.assertNotIn("HiddenAliasToken", canonical_list)
        self.assertNotIn("AliasSearchToken", canonical_list)
        self.assertNotIn("HiddenAliasToken", canonical.content.decode("utf-8"))

        alias = self.client.get(_index_url(), {"q": "AliasSearchToken"})
        self.assertEqual(_row_names(alias), ["OtherCanonical"])
        alias_list = _people_index_list_html(alias)
        self.assertIn("OtherCanonical", alias_list)
        self.assertNotIn("AliasSearchToken", alias_list)
        self.assertNotIn("HiddenAliasToken", alias_list)
        self.assertContains(alias, 'value="AliasSearchToken"')

        missing = self.client.get(_index_url(), {"q": "NoSuchPersonToken"})
        self.assertEqual(_row_names(missing), [])
        self.assertContains(missing, "לא נמצאו אנשים תואמים.")

    def test_search_does_not_leak_unauthorized_or_alias_only_private_people(self):
        public_person = Person.objects.create(name="Visible Search Person")
        PersonAlias.objects.create(person=public_person, name="SharedToken")
        _link(_public_manual("Visible letter"), public_person)
        private_person = Person.objects.create(name="Hidden Search Person")
        PersonAlias.objects.create(person=private_person, name="SharedToken")
        _link(_private_manual("Hidden letter"), private_person)

        resp = self.client.get(_index_url(), {"q": "SharedToken"})
        self.assertEqual(_row_names(resp), ["Visible Search Person"])
        html = resp.content.decode("utf-8")
        self.assertNotIn("Hidden Search Person", html)
        self.assertNotIn(person_public_page_url(private_person.id), html)
        list_html = _people_index_list_html(resp)
        self.assertIn("Visible Search Person", list_html)
        self.assertNotIn("Hidden Search Person", list_html)
        self.assertNotIn("SharedToken", list_html)
        self.assertContains(resp, 'value="SharedToken"')


class PeoplePublicIndexIsolationTests(TestCase):
    def test_no_staff_urls_author_data_or_id_column(self):
        person = Person.objects.create(name="Public Isolation Person")
        _link(_public_manual("Isolation letter"), person)
        Author.objects.create(name="UniqueAuthorTokenXYZ")
        resp = self.client.get(_index_url())
        html = resp.content.decode("utf-8")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(reverse("archive-manage-people"), html)
        self.assertNotIn(
            reverse("archive-manage-person-edit", kwargs={"person_id": person.id}),
            html,
        )
        self.assertNotIn("ניהול אנשים", html)
        self.assertNotIn("UniqueAuthorTokenXYZ", html)
        self.assertNotIn("מזהה", html)
        self.assertNotIn(f">{person.id}<", html)
        self.assertIn(person_public_page_url(person.id), html)


class PeoplePublicIndexPaginationTests(TestCase):
    def test_paginates_at_48_and_preserves_q(self):
        token = "PeopleIndexSearchToken"
        people = []
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1):
            person = Person.objects.create(name=f"{token} {index:02d}")
            _link(_public_manual(f"Letter {index:02d}"), person)
            people.append(person)
        decoy = Person.objects.create(name="Unrelated decoy")
        _link(_public_manual("Decoy letter"), decoy)

        page1 = self.client.get(_index_url(), {"q": token})
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.context["total_count"], 49)
        self.assertEqual(len(page1.context["people_rows"]), 48)
        self.assertTrue(page1.context["show_page_nav"])
        self.assertContains(page1, "?q=PeopleIndexSearchToken&amp;page=2")
        self.assertNotIn(decoy.name, _row_names(page1))

        page2 = self.client.get(_index_url(), {"q": token, "page": "2"})
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.context["people_rows"]), 1)
        self.assertContains(page2, "הקודם")
        html2 = page2.content.decode("utf-8")
        self.assertIn("q=PeopleIndexSearchToken", html2)
        self.assertNotIn(decoy.name, html2)

        clamped = self.client.get(_index_url(), {"q": token, "page": "999"})
        self.assertEqual(clamped.context["page"], 2)
        self.assertEqual(_row_hrefs(clamped), _row_hrefs(page2))


class PeoplePublicIndexLayoutTests(TestCase):
    def test_css_defines_two_column_people_grid_without_browse_list(self):
        css = (
            Path(__file__).resolve().parents[1] / "public/static/public/app.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".archive-people-index-list", css)
        self.assertIn(
            ".archive-people-index-list {\n    grid-template-columns: repeat(2, minmax(0, 1fr));",
            css,
        )
        self.assertNotIn(
            ".archive-people-index-list {\n    grid-template-columns: repeat(3,",
            css,
        )


class PeoplePublicIndexQueryCountTests(TestCase):
    def test_query_count_does_not_grow_with_listed_people(self):
        for index in range(2):
            person = Person.objects.create(name=f"Query Person {index}")
            _link(_public_manual(f"Query letter {index}"), person)

        self.client.get(_index_url())
        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(_index_url())
        self.assertEqual(few_resp.status_code, 200)
        few_total = len(few_ctx.captured_queries)

        for index in range(4):
            person = Person.objects.create(name=f"Query Person extra {index}")
            _link(_public_manual(f"Query extra {index}"), person)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(_index_url())
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(len(many_resp.context["people_rows"]), 6)
        many_total = len(many_ctx.captured_queries)
        self.assertLessEqual(many_total - few_total, 1)
