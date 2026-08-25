"""Public Person page: authorized ArchiveItemPerson items only."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Document,
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
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.test_archive_item import create_viewable_ocr_document

PRESIGNED_URL = "https://s3.example/presigned/photo"


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


def _person_page(person: Person) -> str:
    return reverse("archive-person-detail", kwargs={"person_id": person.id})


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
) -> PhotoContent:
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=(
            PhotoContent.UploadStatus.UPLOADED
            if uploaded
            else PhotoContent.UploadStatus.PENDING
        ),
        upload_error="",
    )
    if uploaded:
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


def _titles(response) -> set[str]:
    return {item.title for item in response.context["items"]}


def _people_select_query_count(captured_queries) -> int:
    count = 0
    for query in captured_queries:
        sql = query["sql"].lower().replace('"', "")
        if not sql.lstrip().startswith("select"):
            continue
        if "documents_photoperson" in sql:
            continue
        if "documents_archiveitemperson" in sql or "documents_person" in sql:
            count += 1
    return count


class PersonPublicPageUrlTests(TestCase):
    def test_helper_and_named_route_match(self):
        person = Person.objects.create(name="URL Person")
        self.assertEqual(
            person_public_page_url(person.id),
            f"/archive/people/{person.id}/",
        )
        self.assertEqual(person_public_page_url(person.id), _person_page(person))


class PersonPublicPageAuthorizedTests(TestCase):
    def test_authorized_page_shows_h1_count_and_cards(self):
        person = Person.objects.create(name="Ada Lovelace")
        PersonAlias.objects.create(person=person, name="SecretAliasToken")
        first = _public_manual("Ada public letter")
        second = _public_manual("Ada public note")
        _link(first, person)
        _link(second, person)

        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<h1", html=False)
        self.assertContains(resp, "Ada Lovelace")
        self.assertContains(resp, "נמצאו 2 תוצאות")
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(_titles(resp), {"Ada public letter", "Ada public note"})
        self.assertContains(resp, "Ada public letter")
        self.assertContains(resp, "Ada public note")
        self.assertContains(resp, "חזרה לארכיון")
        html = resp.content.decode("utf-8")
        self.assertNotIn("SecretAliasToken", html)
        self.assertNotIn("עריכת אדם", html)
        self.assertNotIn(
            reverse("archive-manage-person-edit", kwargs={"person_id": person.id}),
            html,
        )

    def test_duplicate_names_use_distinct_id_urls(self):
        first = Person.objects.create(name="Same Name")
        second = Person.objects.create(name="Same Name")
        _link(_public_manual("First same-name item"), first)
        _link(_public_manual("Second same-name item"), second)

        first_resp = self.client.get(_person_page(first))
        second_resp = self.client.get(_person_page(second))
        self.assertEqual(first_resp.status_code, 200)
        self.assertEqual(second_resp.status_code, 200)
        self.assertNotEqual(_person_page(first), _person_page(second))
        self.assertEqual(_titles(first_resp), {"First same-name item"})
        self.assertEqual(_titles(second_resp), {"Second same-name item"})

    def test_missing_person_is_404(self):
        resp = self.client.get(
            reverse("archive-person-detail", kwargs={"person_id": 999999})
        )
        self.assertEqual(resp.status_code, 404)

    def test_unlinked_person_is_404(self):
        person = Person.objects.create(name="Unlinked Person")
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 404)


class PersonPublicPageVisibilityTests(TestCase):
    def setUp(self):
        self.person = Person.objects.create(name="Visibility Person")
        self.public = _public_manual("PERSON-PUBLIC-TITLE")
        self.private = _private_manual("PERSON-PRIVATE-TITLE")
        self.restricted = _restricted_manual("PERSON-RESTRICTED-TITLE")
        _link(self.public, self.person)
        _link(self.private, self.person)
        _link(self.restricted, self.person)
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family = User.objects.create_user(
            username="person-page-family", password="x"
        )
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="person-page-restricted", password="x")
        )

    def test_anonymous_sees_public_count_only(self):
        resp = self.client.get(_person_page(self.person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(_titles(resp), {"PERSON-PUBLIC-TITLE"})
        html = resp.content.decode("utf-8")
        self.assertIn("PERSON-PUBLIC-TITLE", html)
        self.assertNotIn("PERSON-PRIVATE-TITLE", html)
        self.assertNotIn("PERSON-RESTRICTED-TITLE", html)
        self.assertContains(resp, "נמצאו 1 תוצאות")

    def test_family_sees_private_not_restricted(self):
        self.client.force_login(self.family)
        resp = self.client.get(_person_page(self.person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(_titles(resp), {"PERSON-PUBLIC-TITLE", "PERSON-PRIVATE-TITLE"})
        self.assertNotIn("PERSON-RESTRICTED-TITLE", _titles(resp))

    def test_restricted_user_sees_restricted_not_private(self):
        self.client.force_login(self.restricted_user)
        resp = self.client.get(_person_page(self.person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(
            _titles(resp), {"PERSON-PUBLIC-TITLE", "PERSON-RESTRICTED-TITLE"}
        )
        self.assertNotIn("PERSON-PRIVATE-TITLE", _titles(resp))

    def test_private_only_is_404_for_anonymous(self):
        person = Person.objects.create(name="Private Only Person")
        _link(_private_manual("Hidden private letter"), person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 404)

    def test_restricted_only_is_404_for_anonymous_and_family(self):
        person = Person.objects.create(name="Restricted Only Person")
        _link(_restricted_manual("Hidden restricted letter"), person)
        anon = self.client.get(_person_page(person))
        self.assertEqual(anon.status_code, 404)
        self.client.force_login(self.family)
        family = self.client.get(_person_page(person))
        self.assertEqual(family.status_code, 404)


class PersonPublicPageRelationAndRenderabilityTests(TestCase):
    def test_photoperson_only_is_404(self):
        person = Person.objects.create(name="Appearance Only")
        item = _create_photo_item(title="PhotoPerson only album")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 404)

    def test_non_renderable_photo_item_only_is_404(self):
        person = Person.objects.create(name="Pending Photo Person")
        item = _create_photo_item(title="Pending photo album")
        _add_photo(item, uploaded=False)
        _link(item, person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 404)

    def test_non_renderable_ocr_item_only_is_404(self):
        person = Person.objects.create(name="Pending OCR Person")
        doc = create_ocr_document(
            title="Pending OCR",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADING,
        )
        _link(doc.archive_item, person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 404)


class PersonPublicPagePaginationTests(TestCase):
    def test_paginates_at_fixed_48_and_clamps_out_of_range_pages(self):
        person = Person.objects.create(name="Paged Person")
        items = []
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1):
            item = _public_manual(f"PERSONPAGE-{index:02d}")
            _link(item, person)
            items.append(item)

        page1 = self.client.get(_person_page(person))
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.context["total_count"], 49)
        self.assertEqual(len(page1.context["items"]), 48)
        self.assertTrue(page1.context["show_page_nav"])
        self.assertContains(page1, "נמצאו 49 תוצאות")
        self.assertContains(page1, "?page=2")
        html1 = page1.content.decode("utf-8")
        self.assertNotIn('name="per_page"', html1)
        self.assertNotIn("archive-type-filter", html1)
        self.assertNotIn('id="archive-filter-q"', html1)
        self.assertNotIn("archive-search-form", html1)

        page2 = self.client.get(_person_page(person), {"page": "2"})
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.context["items"]), 1)
        self.assertContains(page2, "הקודם")
        self.assertContains(
            page2,
            f'class="archive-list-pagination__nav-link" href="{_person_page(person)}"',
        )
        self.assertEqual(
            {item.pk for item in page1.context["items"]}
            | {item.pk for item in page2.context["items"]},
            {item.pk for item in items},
        )
        self.assertFalse(
            {item.pk for item in page1.context["items"]}
            & {item.pk for item in page2.context["items"]}
        )

        clamped = self.client.get(_person_page(person), {"page": "999"})
        self.assertEqual(clamped.status_code, 200)
        self.assertEqual(clamped.context["page"], 2)
        self.assertEqual(
            [item.pk for item in clamped.context["items"]],
            [item.pk for item in page2.context["items"]],
        )

        invalid = self.client.get(_person_page(person), {"page": "abc"})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context["page"], 1)
        self.assertEqual(
            [item.pk for item in invalid.context["items"]],
            [item.pk for item in page1.context["items"]],
        )

        ignored = self.client.get(
            _person_page(person),
            {"per_page": "24", "q": "ignored", "item_type": "photo"},
        )
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(len(ignored.context["items"]), 48)
        self.assertEqual(ignored.context["per_page"], 48)

    def test_single_page_omits_page_nav(self):
        person = Person.objects.create(name="One Page Person")
        _link(_public_manual("Only item"), person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["show_page_nav"])
        self.assertNotContains(resp, "הבא")


class PersonPublicPageLinkRetargetTests(TestCase):
    def test_cards_and_homepage_use_person_page_url(self):
        person = Person.objects.create(name="Linked Card Person")
        item = _public_manual("Homepage person card")
        _link(item, person)
        href = person_public_page_url(person.id)

        list_resp = self.client.get(reverse("archive-list"))
        self.assertContains(list_resp, href)
        self.assertEqual(
            list_resp.context["browse_cards"][0].person_links[0].href, href
        )

        home = self.client.get(reverse("public-home"))
        self.assertEqual(home.status_code, 200)
        self.assertContains(home, href)
        self.assertEqual(
            home.context["homepage_archive_cards"][0].person_links[0].href, href
        )

    def test_archive_and_ocr_detail_use_person_page_url(self):
        person = Person.objects.create(name="Linked Detail Person")
        item = _public_manual("Detail person item")
        _link(item, person)
        href = person_public_page_url(person.id)

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertContains(detail, href)

        doc = create_viewable_ocr_document(
            title="OCR person detail page",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=Document.Visibility.PUBLIC,
        )
        _link(doc.archive_item, person)
        ocr = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertContains(ocr, href)

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_photoperson_names_remain_unlinked(self, _mock_presign):
        related = Person.objects.create(name="Item Related Person")
        identified = Person.objects.create(name="Photo Identified Person")
        item = _create_photo_item(title="Split people photo")
        photo = _add_photo(item)
        _link(item, related)
        PhotoPerson.objects.create(photo_content=photo, person=identified)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        html = resp.content.decode("utf-8")
        self.assertContains(resp, "אנשים מזוהים:")
        self.assertContains(resp, "Photo Identified Person")
        self.assertNotIn(person_public_page_url(identified.id), html)
        self.assertIn(person_public_page_url(related.id), html)


class PersonPublicPageQueryCountTests(TestCase):
    def test_people_queries_do_not_grow_with_related_item_count(self):
        person = Person.objects.create(name="Query Count Person")
        for index in range(2):
            _link(_public_manual(f"Few person page {index}"), person)

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(_person_page(person))
        self.assertEqual(few_resp.status_code, 200)

        for index in range(4):
            _link(_public_manual(f"Many person page {index}"), person)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(_person_page(person))
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(
            _people_select_query_count(few_ctx),
            _people_select_query_count(many_ctx),
        )
        self.assertGreaterEqual(_people_select_query_count(few_ctx), 1)
