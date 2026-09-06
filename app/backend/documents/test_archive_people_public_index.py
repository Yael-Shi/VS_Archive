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
    ArchiveItemAuthor,
    ArchiveItemPerson,
    Author,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.author_public import author_public_page_url
from documents.services.public_people_directory import PublicDirectoryIdentityKind
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


def _link_author(item: ArchiveItem, author: Author, *, position: int = 0) -> None:
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


def _row_by_href(response, href: str):
    for row in response.context["people_rows"]:
        if row.href == href:
            return row
    raise AssertionError(f"no row with href {href!r}")


class UnifiedPeopleDirectoryMembershipTests(TestCase):
    def test_photo_only_linked_author_aia_is_a_person_row(self):
        person = Person.objects.create(name="Photo AIA Directory Person")
        author = Author.objects.create(name="Photo AIA Directory Author", person=person)
        item = _create_photo_item(title="Directory authored album")
        _add_photo(item)
        _link_author(item, author)
        resp = self.client.get(_index_url())
        self.assertEqual(_row_names(resp), ["Photo AIA Directory Person"])
        self.assertEqual(_row_hrefs(resp), [person_public_page_url(person.id)])
        self.assertEqual(_count_for(resp, "Photo AIA Directory Person"), 1)

    def test_aip_photoperson_and_linked_author_membership_shapes(self):
        aip_only = Person.objects.create(name="Unified AIP Only")
        _link(_public_manual("AIP letter"), aip_only)

        pp_only = Person.objects.create(name="Unified PP Only")
        pp_item = _create_photo_item(title="PP album")
        PhotoPerson.objects.create(photo_content=_add_photo(pp_item), person=pp_only)

        authored_person = Person.objects.create(name="Unified Authored Person")
        linked_author = Author.objects.create(
            name="Linked Author Display Name", person=authored_person
        )
        _link_author(_public_manual("Authored letter"), linked_author)

        triple = Person.objects.create(name="Unified Triple Person")
        triple_item = _create_photo_item(title="Triple album")
        triple_photo = _add_photo(triple_item)
        _link(triple_item, triple)
        PhotoPerson.objects.create(photo_content=triple_photo, person=triple)
        extra_author = Author.objects.create(name="Second Linked Author", person=triple)
        _link_author(_public_manual("Triple authored letter"), extra_author)
        _link_author(triple_item, Author.objects.create(name="Overlap Author", person=triple))

        unlinked_author = Author.objects.create(name="Unified Author Only")
        _link_author(_public_manual("Author-only letter"), unlinked_author)

        same_name_person = Person.objects.create(name="Exact Same Directory Name")
        _link(_public_manual("Same-name person letter"), same_name_person)
        same_name_author = Author.objects.create(name="Exact Same Directory Name")
        _link_author(_public_manual("Same-name author letter"), same_name_author)

        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 200)
        hrefs = _row_hrefs(resp)
        names = _row_names(resp)

        self.assertIn(person_public_page_url(aip_only.id), hrefs)
        self.assertIn(person_public_page_url(pp_only.id), hrefs)
        self.assertIn(person_public_page_url(authored_person.id), hrefs)
        self.assertEqual(hrefs.count(person_public_page_url(triple.id)), 1)
        self.assertNotIn(author_public_page_url(linked_author.id), hrefs)
        self.assertNotIn(author_public_page_url(extra_author.id), hrefs)
        self.assertIn(author_public_page_url(unlinked_author.id), hrefs)
        self.assertIn(person_public_page_url(same_name_person.id), hrefs)
        self.assertIn(author_public_page_url(same_name_author.id), hrefs)

        triple_row = _row_by_href(resp, person_public_page_url(triple.id))
        self.assertEqual(triple_row.name, "Unified Triple Person")
        self.assertEqual(triple_row.item_count, 2)
        self.assertEqual(triple_row.identity_kind, PublicDirectoryIdentityKind.PERSON)

        authored_row = _row_by_href(resp, person_public_page_url(authored_person.id))
        self.assertEqual(authored_row.name, "Unified Authored Person")
        self.assertNotEqual(authored_row.name, linked_author.name)
        self.assertEqual(authored_row.item_count, 1)

        author_only_row = _row_by_href(resp, author_public_page_url(unlinked_author.id))
        self.assertEqual(author_only_row.name, "Unified Author Only")
        self.assertEqual(author_only_row.identity_kind, PublicDirectoryIdentityKind.AUTHOR)

        html = _people_index_list_html(resp)
        self.assertNotIn("Linked Author Display Name", html)
        self.assertIn("Exact Same Directory Name", names)
        self.assertEqual(names.count("Exact Same Directory Name"), 2)

    def test_item_counts_are_distinct_across_overlapping_relations(self):
        person = Person.objects.create(name="Overlap Count Person")
        item = _create_photo_item(title="Overlap album")
        photo = _add_photo(item)
        _link(item, person)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        author = Author.objects.create(name="Overlap Count Author", person=person)
        _link_author(item, author)
        other = _public_manual("Second overlap letter")
        _link(other, person)
        _link_author(other, author)

        resp = self.client.get(_index_url())
        self.assertEqual(_count_for(resp, "Overlap Count Person"), 2)
        self.assertNotIn(author_public_page_url(author.id), _row_hrefs(resp))

    def test_authorization_still_hides_private_and_restricted(self):
        public_person = Person.objects.create(name="Dir Public Person")
        _link(_public_manual("Public letter"), public_person)
        private_author = Author.objects.create(name="Dir Private Author")
        _link_author(_private_manual("Private authored"), private_author)
        private_linked_person = Person.objects.create(name="Dir Private Linked Person")
        private_linked_author = Author.objects.create(
            name="Dir Private Linked Author", person=private_linked_person
        )
        _link_author(_private_manual("Private linked authored"), private_linked_author)

        resp = self.client.get(_index_url())
        names = _row_names(resp)
        self.assertEqual(names, ["Dir Public Person"])
        self.assertNotIn("Dir Private Author", names)
        self.assertNotIn("Dir Private Linked Person", names)
        self.assertNotIn(author_public_page_url(private_author.id), _row_hrefs(resp))


class UnifiedPeopleDirectorySearchTests(TestCase):
    def test_q_matches_person_alias_linked_author_and_author_only(self):
        canonical = Person.objects.create(name="CanonicalDirectoryPerson")
        _link(_public_manual("Canonical letter"), canonical)
        PersonAlias.objects.create(person=canonical, name="DirectoryAliasToken")

        alias_person = Person.objects.create(name="Other Directory Canonical")
        PersonAlias.objects.create(person=alias_person, name="AliasOnlyDirectoryToken")
        _link(_public_manual("Alias letter"), alias_person)

        linked_person = Person.objects.create(name="Person With Linked Author")
        linked_author = Author.objects.create(
            name="LinkedAuthorSearchToken", person=linked_person
        )
        _link(_public_manual("Linked person AIP"), linked_person)
        _link_author(_public_manual("Linked authored"), linked_author)

        unlinked_author = Author.objects.create(name="IndependentAuthorOnlySearchToken")
        _link_author(_public_manual("Unlinked authored"), unlinked_author)

        same_person = Person.objects.create(name="SharedSearchName")
        _link(_public_manual("Shared person letter"), same_person)
        same_author = Author.objects.create(name="SharedSearchName")
        _link_author(_public_manual("Shared author letter"), same_author)

        canonical_resp = self.client.get(_index_url(), {"q": "CanonicalDirectoryPerson"})
        self.assertEqual(_row_names(canonical_resp), ["CanonicalDirectoryPerson"])

        alias_resp = self.client.get(_index_url(), {"q": "AliasOnlyDirectoryToken"})
        self.assertEqual(_row_names(alias_resp), ["Other Directory Canonical"])
        self.assertNotIn("AliasOnlyDirectoryToken", _people_index_list_html(alias_resp))

        linked_resp = self.client.get(_index_url(), {"q": "LinkedAuthorSearchToken"})
        self.assertEqual(_row_hrefs(linked_resp), [person_public_page_url(linked_person.id)])
        self.assertEqual(_row_names(linked_resp), ["Person With Linked Author"])
        self.assertNotIn(author_public_page_url(linked_author.id), _row_hrefs(linked_resp))

        unlinked_resp = self.client.get(
            _index_url(), {"q": "IndependentAuthorOnlySearchToken"}
        )
        self.assertEqual(
            _row_hrefs(unlinked_resp), [author_public_page_url(unlinked_author.id)]
        )

        shared = self.client.get(_index_url(), {"q": "SharedSearchName"})
        self.assertEqual(
            _row_hrefs(shared),
            [
                person_public_page_url(same_person.id),
                author_public_page_url(same_author.id),
            ],
        )

    def test_linked_author_name_search_requires_authorized_author_membership(self):
        person = Person.objects.create(name="VisibleAipPerson")
        PersonAlias.objects.create(person=person, name="VisibleAipAliasToken")
        _link(_public_manual("Public AIP letter"), person)
        author = Author.objects.create(
            name="PrivateOnlyLinkedAuthorToken", person=person
        )
        _link_author(_private_manual("Private authored letter"), author)

        hidden = self.client.get(_index_url(), {"q": "PrivateOnlyLinkedAuthorToken"})
        self.assertEqual(_row_names(hidden), [])
        self.assertNotIn(person_public_page_url(person.id), _row_hrefs(hidden))
        self.assertNotIn("VisibleAipPerson", hidden.content.decode("utf-8"))
        self.assertNotIn("PrivateOnlyLinkedAuthorToken", _people_index_list_html(hidden))

        by_name = self.client.get(_index_url(), {"q": "VisibleAipPerson"})
        self.assertEqual(_row_hrefs(by_name), [person_public_page_url(person.id)])
        by_alias = self.client.get(_index_url(), {"q": "VisibleAipAliasToken"})
        self.assertEqual(_row_hrefs(by_alias), [person_public_page_url(person.id)])
        self.assertNotIn("VisibleAipAliasToken", _people_index_list_html(by_alias))

        _link_author(_public_manual("Public authored letter"), author)
        public_author_q = self.client.get(
            _index_url(), {"q": "PrivateOnlyLinkedAuthorToken"}
        )
        self.assertEqual(
            _row_hrefs(public_author_q), [person_public_page_url(person.id)]
        )
        self.assertEqual(_row_names(public_author_q), ["VisibleAipPerson"])

    def test_linked_author_name_search_follows_family_and_restricted_access(self):
        person = Person.objects.create(name="VisibilitySearchPerson")
        _link(_public_manual("Public AIP for search person"), person)
        private_author = Author.objects.create(
            name="FamilyLinkedAuthorToken", person=person
        )
        _link_author(_private_manual("Family authored letter"), private_author)
        restricted_author = Author.objects.create(
            name="RestrictedLinkedAuthorToken", person=person
        )
        _link_author(
            _restricted_manual("Restricted authored letter"), restricted_author
        )

        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(
            username="people-linked-author-family", password="x"
        )
        family.groups.add(family_group)
        restricted_user = _grant_restricted_permission(
            User.objects.create_user(
                username="people-linked-author-restricted", password="x"
            )
        )

        anon_family = self.client.get(_index_url(), {"q": "FamilyLinkedAuthorToken"})
        self.assertEqual(_row_names(anon_family), [])
        anon_restricted = self.client.get(
            _index_url(), {"q": "RestrictedLinkedAuthorToken"}
        )
        self.assertEqual(_row_names(anon_restricted), [])

        self.client.force_login(family)
        family_private = self.client.get(
            _index_url(), {"q": "FamilyLinkedAuthorToken"}
        )
        self.assertEqual(
            _row_hrefs(family_private), [person_public_page_url(person.id)]
        )
        family_restricted = self.client.get(
            _index_url(), {"q": "RestrictedLinkedAuthorToken"}
        )
        self.assertEqual(_row_names(family_restricted), [])

        self.client.force_login(restricted_user)
        restricted_private = self.client.get(
            _index_url(), {"q": "FamilyLinkedAuthorToken"}
        )
        self.assertEqual(_row_names(restricted_private), [])
        restricted_ok = self.client.get(
            _index_url(), {"q": "RestrictedLinkedAuthorToken"}
        )
        self.assertEqual(
            _row_hrefs(restricted_ok), [person_public_page_url(person.id)]
        )


class UnifiedPeopleDirectoryOrderAndPaginationTests(TestCase):
    def test_same_display_name_uses_kind_then_id_tiebreaker(self):
        later_person = Person.objects.create(name="Tie Name")
        earlier_author = Author.objects.create(name="Tie Name")
        later_author = Author.objects.create(name="Tie Name")
        earlier_person = Person.objects.create(name="Tie Name")
        _link(_public_manual("Later person letter"), later_person)
        _link(_public_manual("Earlier person letter"), earlier_person)
        _link_author(_public_manual("Earlier author letter"), earlier_author)
        _link_author(_public_manual("Later author letter"), later_author)

        resp = self.client.get(_index_url(), {"q": "Tie Name"})
        self.assertEqual(
            _row_hrefs(resp),
            [
                person_public_page_url(later_person.id),
                person_public_page_url(earlier_person.id),
                author_public_page_url(earlier_author.id),
                author_public_page_url(later_author.id),
            ],
        )

    def test_pagination_is_global_across_person_and_author_only_rows(self):
        identities: list[tuple[str, str, int]] = []
        for index in range(25):
            author = Author.objects.create(name=f"MixPage Author {index:02d}")
            _link_author(_public_manual(f"Author letter {index:02d}"), author)
            identities.append(("author", author.name, author.id))
        for index in range(24):
            person = Person.objects.create(name=f"MixPage Person {index:02d}")
            _link(_public_manual(f"Person letter {index:02d}"), person)
            identities.append(("person", person.name, person.id))
        identities.sort(
            key=lambda row: (
                row[1],
                0 if row[0] == "person" else 1,
                row[2],
            )
        )
        expected_hrefs = [
            person_public_page_url(source_id)
            if kind == "person"
            else author_public_page_url(source_id)
            for kind, _name, source_id in identities
        ]

        page1 = self.client.get(_index_url(), {"q": "MixPage"})
        self.assertEqual(page1.context["total_count"], 49)
        self.assertEqual(len(page1.context["people_rows"]), 48)
        self.assertEqual(_row_hrefs(page1), expected_hrefs[:48])
        self.assertContains(page1, "?q=MixPage&amp;page=2")

        page2 = self.client.get(_index_url(), {"q": "MixPage", "page": "2"})
        self.assertEqual(_row_hrefs(page2), expected_hrefs[48:])
        self.assertEqual(len(page2.context["people_rows"]), 1)
