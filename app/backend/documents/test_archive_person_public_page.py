"""Public Person page: ArchiveItemPerson items and PhotoPerson appearances."""

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
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
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
from documents.services.photo_gallery import public_photo_detail_url
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


def _titles(response) -> set[str]:
    return {item.title for item in response.context["items"]}


def _card_urls(response) -> list[str]:
    return [card.detail_url for card in response.context["browse_cards"]]


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
        self.assertContains(resp, "פריטים קשורים")
        self.assertNotContains(resp, "תמונות שבהן האדם מופיע")
        self.assertContains(resp, "נמצאו 2 תוצאות")
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(_titles(resp), {"Ada public letter", "Ada public note"})
        self.assertContains(resp, "Ada public letter")
        self.assertContains(resp, "Ada public note")
        self.assertContains(resp, "חזרה לארכיון")
        html = resp.content.decode("utf-8")
        self.assertNotIn("SecretAliasToken", html)
        self.assertNotIn("עריכת אדם", html)
        self.assertNotIn("archive-detail-meta-block--person-biography", html)
        self.assertNotIn(
            reverse("archive-manage-person-edit", kwargs={"person_id": person.id}),
            html,
        )

    def test_empty_and_whitespace_biography_are_omitted(self):
        person = Person.objects.create(name="Empty Bio Person")
        _link(_public_manual("Empty bio letter"), person)
        empty = self.client.get(_person_page(person))
        self.assertEqual(empty.status_code, 200)
        self.assertNotContains(empty, "archive-detail-meta-block--person-biography")
        self.assertNotContains(empty, "תקציר")

        person.biography = "  \n  "
        person.save(update_fields=["biography", "updated_at"])
        whitespace = self.client.get(_person_page(person))
        self.assertEqual(whitespace.status_code, 200)
        self.assertContains(whitespace, "Empty Bio Person")
        self.assertNotContains(
            whitespace, "archive-detail-meta-block--person-biography"
        )

    def test_nonempty_biography_renders_after_h1_with_breaks_and_escaping(self):
        person = Person.objects.create(name="Bio Person")
        PersonAlias.objects.create(person=person, name="HiddenBioAlias")
        _link(_public_manual("Bio public letter"), person)
        person.biography = "<script>alert(1)</script>\nsecond line"
        person.save(update_fields=["biography", "updated_at"])

        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        name_idx = html.find("<h1")
        bio_idx = html.find("archive-detail-meta-block--person-biography")
        self.assertNotEqual(name_idx, -1)
        self.assertNotEqual(bio_idx, -1)
        self.assertLess(name_idx, bio_idx)
        self.assertContains(
            resp, "&lt;script&gt;alert(1)&lt;/script&gt;<br>second line", html=True
        )
        self.assertNotContains(resp, "<script>alert(1)</script>")
        self.assertNotContains(resp, "HiddenBioAlias")
        self.assertNotContains(resp, "תקציר")
        self.assertNotContains(resp, "עריכת אדם")

    def test_biography_does_not_open_inaccessible_person_pages(self):
        unlinked = Person.objects.create(
            name="Unlinked Bio Person",
            biography="UnlinkedBioToken",
        )
        self.assertEqual(self.client.get(_person_page(unlinked)).status_code, 404)

        appearance = Person.objects.create(
            name="Appearance Bio Person",
            biography="AppearanceBioToken",
        )
        item = _create_photo_item(title="PhotoPerson only album")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        appearance_resp = self.client.get(_person_page(appearance))
        self.assertEqual(appearance_resp.status_code, 200)
        self.assertContains(appearance_resp, "AppearanceBioToken")
        self.assertContains(appearance_resp, "פריטים קשורים")
        self.assertNotContains(appearance_resp, "תמונות שבהן האדם מופיע")

        private_only = Person.objects.create(
            name="Private Bio Person",
            biography="PrivateBioToken",
        )
        _link(_private_manual("Hidden private letter"), private_only)
        self.assertEqual(self.client.get(_person_page(private_only)).status_code, 404)

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
    def test_photoperson_only_is_accessible(self):
        person = Person.objects.create(name="Appearance Only")
        PersonAlias.objects.create(person=person, name="HiddenAppearanceAlias")
        item = _create_photo_item(title="PhotoPerson only album")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(_titles(resp), {"PhotoPerson only album"})
        self.assertEqual(len(resp.context["browse_cards"]), 1)
        self.assertNotIn("photo_appearance_cards", resp.context)
        self.assertContains(resp, "Appearance Only")
        self.assertContains(resp, "פריטים קשורים")
        self.assertContains(resp, "PhotoPerson only album")
        self.assertNotContains(resp, "תמונות שבהן האדם מופיע")
        self.assertNotContains(resp, "HiddenAppearanceAlias")
        self.assertEqual(
            _card_urls(resp),
            [public_photo_detail_url(item.id, photo.id)],
        )

    def test_neither_visible_relation_is_404(self):
        person = Person.objects.create(name="No Visible Relations")
        private_item = _private_manual("Hidden related letter")
        _link(private_item, person)
        private_photo_item = _create_photo_item(
            title="Hidden appearance album",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        private_photo = _add_photo(private_photo_item)
        PhotoPerson.objects.create(photo_content=private_photo, person=person)
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


class PersonPublicPagePhotoPersonTests(TestCase):
    def test_public_user_sees_public_appearance(self):
        person = Person.objects.create(name="Public Appearance Person")
        item = _create_photo_item(title="Public appearance album")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)

        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(len(resp.context["browse_cards"]), 1)
        self.assertEqual(
            resp.context["browse_cards"][0].title,
            "Public appearance album",
        )
        self.assertEqual(
            resp.context["browse_cards"][0].detail_url,
            f"/archive/{item.id}/?photo={photo.id}",
        )

    def test_family_sees_private_appearance_anonymous_does_not(self):
        person = Person.objects.create(name="Private Appearance Person")
        item = _create_photo_item(
            title="Private appearance album",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(username="person-photo-family", password="x")
        family.groups.add(family_group)

        anon = self.client.get(_person_page(person))
        self.assertEqual(anon.status_code, 404)

        self.client.force_login(family)
        family_resp = self.client.get(_person_page(person))
        self.assertEqual(family_resp.status_code, 200)
        self.assertEqual(
            _card_urls(family_resp),
            [public_photo_detail_url(item.id, photo.id)],
        )

    def test_unauthorized_user_does_not_see_restricted_appearance(self):
        person = Person.objects.create(name="Restricted Appearance Person")
        item = _create_photo_item(
            title="Restricted appearance album",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(
            username="person-photo-restricted-family", password="x"
        )
        family.groups.add(family_group)
        restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="person-photo-restricted", password="x")
        )

        self.assertEqual(self.client.get(_person_page(person)).status_code, 404)
        self.client.force_login(family)
        self.assertEqual(self.client.get(_person_page(person)).status_code, 404)
        self.client.force_login(restricted_user)
        allowed = self.client.get(_person_page(person))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            _card_urls(allowed),
            [public_photo_detail_url(item.id, photo.id)],
        )

    def test_pending_failed_and_empty_key_photos_do_not_open_page(self):
        pending_person = Person.objects.create(name="Pending Appearance")
        pending_item = _create_photo_item(title="Pending appearance album")
        pending_photo = _add_photo(pending_item, uploaded=False)
        PhotoPerson.objects.create(photo_content=pending_photo, person=pending_person)
        self.assertEqual(self.client.get(_person_page(pending_person)).status_code, 404)

        failed_person = Person.objects.create(name="Failed Appearance")
        failed_item = _create_photo_item(title="Failed appearance album")
        failed_photo = _add_photo(failed_item, failed=True)
        PhotoPerson.objects.create(photo_content=failed_photo, person=failed_person)
        self.assertEqual(self.client.get(_person_page(failed_person)).status_code, 404)

        empty_key_person = Person.objects.create(name="Empty Key Appearance")
        empty_item = _create_photo_item(title="Empty key appearance album")
        empty_photo = PhotoContent.objects.create(
            archive_item=empty_item,
            position=1,
            original_file_key="",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            upload_error="",
        )
        PhotoPerson.objects.create(photo_content=empty_photo, person=empty_key_person)
        self.assertEqual(
            self.client.get(_person_page(empty_key_person)).status_code, 404
        )

    def test_non_renderable_second_photo_does_not_appear(self):
        person = Person.objects.create(name="Mixed Render Appearance")
        item = _create_photo_item(title="Mixed render album")
        first = _add_photo(item, position=1)
        pending = _add_photo(item, position=2, uploaded=False)
        PhotoPerson.objects.create(photo_content=pending, person=person)
        self.assertEqual(self.client.get(_person_page(person)).status_code, 404)

        PhotoPerson.objects.create(photo_content=first, person=person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(
            _card_urls(resp),
            [public_photo_detail_url(item.id, first.id)],
        )
        self.assertNotIn(
            public_photo_detail_url(item.id, pending.id),
            _card_urls(resp),
        )

    def test_item_not_browse_renderable_hides_later_renderable_appearance(self):
        person = Person.objects.create(name="Later Photo Person")
        item = _create_photo_item(title="First pending album")
        _add_photo(item, position=1, uploaded=False)
        later = _add_photo(item, position=2)
        PhotoPerson.objects.create(photo_content=later, person=person)
        self.assertEqual(self.client.get(_person_page(person)).status_code, 404)

    def test_deep_link_uses_earliest_matching_photo_not_primary(self):
        person = Person.objects.create(name="Later Match Person")
        item = _create_photo_item(title="Later match album")
        _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        PhotoPerson.objects.create(photo_content=second, person=person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(
            _card_urls(resp),
            [public_photo_detail_url(item.id, second.id)],
        )

    def test_aip_only_photo_item_uses_normal_detail_url(self):
        person = Person.objects.create(name="AIP Photo Person")
        item = _create_photo_item(title="Related photo album")
        _add_photo(item)
        _link(item, person)
        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        expected = reverse("archive-detail", kwargs={"item_id": item.id})
        self.assertEqual(_card_urls(resp), [expected])
        self.assertNotIn("photo=", expected)
        self.assertNotIn("photo=", _card_urls(resp)[0])

    def test_multiple_photos_in_one_item_appear_once_with_earliest_deep_link(self):
        person = Person.objects.create(name="Two Photos Person")
        item = _create_photo_item(title="Two photo album")
        first = _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        PhotoPerson.objects.create(photo_content=first, person=person)
        PhotoPerson.objects.create(photo_content=second, person=person)

        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(len(resp.context["browse_cards"]), 1)
        self.assertEqual(
            _card_urls(resp),
            [public_photo_detail_url(item.id, first.id)],
        )
        self.assertNotIn(
            public_photo_detail_url(item.id, second.id),
            _card_urls(resp),
        )

    def test_unified_list_dedupes_dual_item_and_deep_links_matching_photo(self):
        person = Person.objects.create(name="Both Relations Person")
        related = _public_manual("Related letter")
        _link(related, person)
        photo_item = _create_photo_item(title="Appearance album")
        photo = _add_photo(photo_item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        both_item = _create_photo_item(title="Linked and appearing album")
        both_photo = _add_photo(both_item)
        _link(both_item, person)
        PhotoPerson.objects.create(photo_content=both_photo, person=person)

        resp = self.client.get(_person_page(person))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 3)
        self.assertEqual(
            _titles(resp),
            {"Related letter", "Appearance album", "Linked and appearing album"},
        )
        self.assertNotIn("photo_appearance_cards", resp.context)
        self.assertContains(resp, "פריטים קשורים")
        self.assertNotContains(resp, "תמונות שבהן האדם מופיע")
        html = resp.content.decode("utf-8")
        self.assertEqual(html.count("פריטים קשורים"), 1)
        letter_url = reverse("archive-detail", kwargs={"item_id": related.id})
        self.assertIn(letter_url, _card_urls(resp))
        self.assertIn(
            public_photo_detail_url(photo_item.id, photo.id), _card_urls(resp)
        )
        self.assertIn(
            public_photo_detail_url(both_item.id, both_photo.id),
            _card_urls(resp),
        )
        self.assertEqual(_card_urls(resp).count(letter_url), 1)

    def test_advanced_person_filter_includes_photoperson_only(self):
        person = Person.objects.create(name="Filter Unified Person")
        photo_item = _create_photo_item(title="PhotoPerson filter decoy")
        photo = _add_photo(photo_item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        linked = _public_manual("ArchiveItemPerson filter match")
        _link(linked, person)

        page = self.client.get(_person_page(person))
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            {card.title for card in page.context["browse_cards"]},
            {"ArchiveItemPerson filter match", "PhotoPerson filter decoy"},
        )

        ids = list(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            ).values_list("pk", flat=True)
        )
        self.assertEqual(set(ids), {linked.pk, photo_item.pk})
        self.assertEqual(len(ids), 2)

        list_resp = self.client.get(
            reverse("archive-list"), {"person": str(person.id), "advanced": "1"}
        )
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(
            {item.title for item in list_resp.context["items"]},
            {"ArchiveItemPerson filter match", "PhotoPerson filter decoy"},
        )

    def test_appearance_queries_do_not_grow_with_photo_count(self):
        person = Person.objects.create(name="Appearance Query Person")
        first_item = _create_photo_item(title="Few appearance 0")
        PhotoPerson.objects.create(photo_content=_add_photo(first_item), person=person)
        second_item = _create_photo_item(title="Few appearance 1")
        PhotoPerson.objects.create(photo_content=_add_photo(second_item), person=person)

        self.client.get(_person_page(person))

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(_person_page(person))
        self.assertEqual(few_resp.status_code, 200)
        few_cards = len(few_resp.context["browse_cards"])
        few_total = len(few_ctx.captured_queries)
        few_photo_queries = sum(
            1
            for query in few_ctx
            if "documents_photoperson" in query["sql"].lower().replace('"', "")
        )

        for index in range(4):
            item = _create_photo_item(title=f"Many appearance {index}")
            PhotoPerson.objects.create(photo_content=_add_photo(item), person=person)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(_person_page(person))
        self.assertEqual(many_resp.status_code, 200)
        many_cards = len(many_resp.context["browse_cards"])
        many_total = len(many_ctx.captured_queries)
        many_photo_queries = sum(
            1
            for query in many_ctx
            if "documents_photoperson" in query["sql"].lower().replace('"', "")
        )
        extra_cards = many_cards - few_cards
        self.assertEqual(few_cards, 2)
        self.assertGreaterEqual(extra_cards, 4)
        self.assertEqual(few_photo_queries, many_photo_queries)
        self.assertGreaterEqual(few_photo_queries, 1)
        self.assertLessEqual(many_total - few_total, 1)
        self.assertLess(many_total - few_total, extra_cards)


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

    def test_photoperson_only_items_share_unified_pagination(self):
        person = Person.objects.create(name="Paged With Photos Person")
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1):
            _link(_public_manual(f"RELATEDPAGE-{index:02d}"), person)
        photo_item = _create_photo_item(title="Unpaged appearance album")
        photo = _add_photo(photo_item)
        PhotoPerson.objects.create(photo_content=photo, person=person)

        page1 = self.client.get(_person_page(person))
        page2 = self.client.get(_person_page(person), {"page": "2"})
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(page1.context["total_count"], 50)
        self.assertEqual(len(page1.context["items"]), 48)
        self.assertEqual(len(page2.context["items"]), 2)
        href = public_photo_detail_url(photo_item.id, photo.id)
        all_urls = _card_urls(page1) + _card_urls(page2)
        self.assertEqual(all_urls.count(href), 1)
        self.assertNotIn("photo_appearance_cards", page1.context)
        self.assertNotIn("photo_appearance_cards", page2.context)
        self.assertNotContains(page1, "תמונות שבהן האדם מופיע")
        self.assertNotContains(page2, "תמונות שבהן האדם מופיע")
        self.assertContains(page1, "פריטים קשורים")


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
    def test_photoperson_names_link_to_person_page(self, _mock_presign):
        related = Person.objects.create(name="Item Related Person")
        identified = Person.objects.create(name="Photo Identified Person")
        item = _create_photo_item(title="Split people photo")
        photo = _add_photo(item)
        _link(item, related)
        PhotoPerson.objects.create(photo_content=photo, person=identified)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        html = resp.content.decode("utf-8")
        identified_href = person_public_page_url(identified.id)
        related_href = person_public_page_url(related.id)
        self.assertContains(resp, "אנשים מזוהים:")
        self.assertContains(resp, "Photo Identified Person")
        self.assertContains(
            resp,
            f'<a href="{identified_href}">Photo Identified Person</a>',
        )
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, "Item Related Person")
        self.assertContains(resp, related_href)
        identified_idx = html.index("אנשים מזוהים:")
        related_idx = html.index("אנשים קשורים")
        self.assertLess(identified_idx, related_idx)
        identified_block = html[identified_idx:related_idx]
        related_block = html[related_idx:]
        self.assertIn(identified_href, identified_block)
        self.assertIn("Photo Identified Person", identified_block)
        self.assertNotIn("Item Related Person", identified_block)
        self.assertNotIn(related_href, identified_block)
        self.assertIn(related_href, related_block)
        self.assertIn("Item Related Person", related_block)
        self.assertNotIn("Photo Identified Person", related_block)
        self.assertNotIn(identified_href, related_block)


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
