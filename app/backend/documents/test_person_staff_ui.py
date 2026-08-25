"""Staff Person canonical-name and alias management UI (PR6b)."""

from __future__ import annotations

import re
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.services.photo_content_management import (
    PERSON_ALIAS_DUPLICATE_ERROR,
    PERSON_ALIAS_MATCHES_CANONICAL_ERROR,
    PERSON_ALIAS_REQUIRED_ERROR,
    PERSON_ALIAS_TOO_LONG_ERROR,
    PERSON_NAME_REQUIRED_ERROR,
    PERSON_NAME_TOO_LONG_ERROR,
    create_person_alias,
    delete_person_alias,
    person_staff_picker_label,
    update_person_alias,
    update_person_name,
)
from documents.views import (
    PERSON_ALIAS_ADDED_MSG,
    PERSON_ALIAS_DELETED_MSG,
    PERSON_ALIAS_UPDATED_MSG,
    PERSON_NAME_UPDATED_MSG,
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
    position: int,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
) -> PhotoContent:
    if original_file_key is None:
        original_file_key = f"photos/{item.pk}-{position}/original.jpg"
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=original_file_key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
    )


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


def _search_ids(token: str) -> list[int]:
    return list(
        filter_archive_items_by_search_query(
            ArchiveItem.objects.all(), token
        ).values_list("pk", flat=True)
    )


def _option_text(html: str, person_id: int) -> str:
    match = re.search(
        rf'<option value="{person_id}"[^>]*>(.*?)</option>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"missing option for person {person_id}"
    return " ".join(match.group(1).split())


def _presign_url(*, bucket, key, expires_in=3600):
    return f"https://example.test/{key}"


def _alias_sql(captured_queries) -> list[str]:
    return [
        query["sql"]
        for query in captured_queries
        if "documents_personalias" in query["sql"].lower()
    ]


def _edit_url(person: Person) -> str:
    return reverse("archive-manage-person-edit", kwargs={"person_id": person.id})


def _alias_edit_url(person: Person, alias: PersonAlias) -> str:
    return reverse(
        "archive-manage-person-alias-edit",
        kwargs={"person_id": person.id, "alias_id": alias.id},
    )


def _alias_delete_url(person: Person, alias: PersonAlias) -> str:
    return reverse(
        "archive-manage-person-alias-delete",
        kwargs={"person_id": person.id, "alias_id": alias.id},
    )


class PersonStaffPickerLabelTests(TestCase):
    def test_label_without_aliases_is_canonical_name_only(self):
        person = Person.objects.create(name="רחל כהן")
        self.assertEqual(person_staff_picker_label(person), "רחל כהן")

    def test_label_with_aliases_is_canonical_then_aliases(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Yaakov Cohen")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        person = Person.objects.prefetch_related("aliases").get(pk=person.pk)
        self.assertEqual(
            person_staff_picker_label(person),
            "יעקב כהן (Jacob Cohen, Yaakov Cohen)",
        )

    def test_alias_order_is_deterministic_by_name_then_id(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Yaakov Cohen")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        PersonAlias.objects.create(person=person, name="Jacob Cohen II")
        person = Person.objects.prefetch_related("aliases").get(pk=person.pk)
        self.assertEqual(
            person_staff_picker_label(person),
            "יעקב כהן (Jacob Cohen, Jacob Cohen II, Yaakov Cohen)",
        )
        self.assertNotIn(str(person.pk), person_staff_picker_label(person))


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffPickerPageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_picker_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Picker album")
        self.photo = _add_photo(self.item, position=1)
        self.selected = Person.objects.create(name="יעקב כהן")
        self.unselected = Person.objects.create(name="רחל כהן")
        PhotoPerson.objects.create(photo_content=self.photo, person=self.selected)
        self.client.force_login(self.staff)
        self.url = reverse(
            "archive-manage-photo-edit",
            kwargs={"item_id": self.item.id, "photo_id": self.photo.id},
        )

    def test_option_label_without_aliases_is_canonical_only(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertEqual(_option_text(html, self.unselected.id), "רחל כהן")
        self.assertNotIn(
            str(self.unselected.id), _option_text(html, self.unselected.id)
        )

    def test_option_label_with_aliases_includes_canonical_and_aliases(self):
        PersonAlias.objects.create(person=self.selected, name="Yaakov Cohen")
        PersonAlias.objects.create(person=self.selected, name="Jacob Cohen")
        resp = self.client.get(self.url)
        html = resp.content.decode()
        self.assertEqual(
            _option_text(html, self.selected.id),
            "יעקב כהן (Jacob Cohen, Yaakov Cohen)",
        )
        self.assertNotIn(str(self.selected.id), _option_text(html, self.selected.id))
        self.assertContains(resp, f'value="{self.selected.id}"')

    def test_selected_person_edit_links_appear_and_unselected_do_not(self):
        PersonAlias.objects.create(person=self.selected, name="Jacob Cohen")
        PersonAlias.objects.create(person=self.unselected, name="Rachel Cohen")
        resp = self.client.get(self.url)
        self.assertContains(resp, _edit_url(self.selected))
        self.assertContains(resp, "עריכת אדם")
        self.assertContains(resp, "יעקב כהן (Jacob Cohen)")
        self.assertNotContains(resp, _edit_url(self.unselected))

    def test_picker_loading_does_not_n_plus_one_on_aliases(self):
        people = [
            Person.objects.create(name=f"Picker person {index}") for index in range(4)
        ]
        for person in people:
            PersonAlias.objects.create(person=person, name=f"{person.name} alias")
        self.client.get(self.url)

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(self.url)
        self.assertEqual(few_resp.status_code, 200)

        extra_people = [
            Person.objects.create(name=f"Extra picker {index}") for index in range(4)
        ]
        for person in [*people, *extra_people]:
            PersonAlias.objects.create(person=person, name=f"{person.name} alias 2")
            PersonAlias.objects.create(person=person, name=f"{person.name} alias 3")

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(self.url)
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(len(_alias_sql(few_ctx.captured_queries)), 1)
        self.assertEqual(len(_alias_sql(many_ctx.captured_queries)), 1)


class PersonStaffEditAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.person = Person.objects.create(name="יעקב כהן")
        self.url = _edit_url(self.person)

    def test_staff_can_open_person_edit(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת אדם")
        self.assertContains(resp, "יעקב כהן")
        self.assertContains(resp, 'name="action"')

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    def test_non_staff_is_forbidden(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="person_edit_family",
            password="test-pass",
        )
        user.groups.add(family_group)
        self.client.force_login(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_person_is_404(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-manage-person-edit", kwargs={"person_id": 999999})
        )
        self.assertEqual(resp.status_code, 404)

    def test_no_public_person_catalog_or_alternate_routes(self):
        for path in (
            "/archive/person/1/",
            "/archive/manage/people/",
        ):
            with self.assertRaises(Resolver404):
                resolve(path)
        match = resolve("/archive/people/1/")
        self.assertEqual(match.url_name, "archive-person-detail")
        match = resolve("/archive/manage/people/1/edit/")
        self.assertEqual(match.url_name, "archive-manage-person-edit")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffCanonicalNameTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_rename_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Rename album")
        self.photo = _add_photo(self.item, position=1)
        self.person = Person.objects.create(name="CanonicalBeforeToken")
        self.alias = PersonAlias.objects.create(
            person=self.person, name="ExistingAliasToken"
        )
        PhotoPerson.objects.create(photo_content=self.photo, person=self.person)
        _rebuild(self.item.pk)
        self.client.force_login(self.staff)
        self.url = _edit_url(self.person)

    def test_successful_rename_uses_service_and_refreshes_search(self):
        with patch(
            "documents.views.update_person_name", wraps=update_person_name
        ) as mocked:
            resp = self.client.post(
                self.url,
                data={"action": "update_name", "name": "  CanonicalAfterToken  "},
                follow=True,
            )
        mocked.assert_called_once()
        self.assertContains(resp, PERSON_NAME_UPDATED_MSG)
        self.person.refresh_from_db()
        self.alias.refresh_from_db()
        self.assertEqual(self.person.name, "CanonicalAfterToken")
        self.assertEqual(self.alias.name, "ExistingAliasToken")
        self.assertEqual(_search_ids("CanonicalAfterToken"), [self.item.pk])
        self.assertEqual(_search_ids("CanonicalBeforeToken"), [])
        self.assertEqual(_search_ids("ExistingAliasToken"), [self.item.pk])

    def test_empty_and_too_long_rename_are_rejected(self):
        for payload in ("", "   ", "x" * 256):
            resp = self.client.post(
                self.url, data={"action": "update_name", "name": payload}
            )
            self.assertEqual(resp.status_code, 200)
            self.person.refresh_from_db()
            self.assertEqual(self.person.name, "CanonicalBeforeToken")
            expected = (
                PERSON_NAME_TOO_LONG_ERROR
                if len(payload) > 255
                else PERSON_NAME_REQUIRED_ERROR
            )
            self.assertContains(resp, expected)

    def test_rename_to_existing_alias_keeps_both_rows(self):
        resp = self.client.post(
            self.url,
            data={"action": "update_name", "name": "ExistingAliasToken"},
            follow=True,
        )
        self.assertContains(resp, PERSON_NAME_UPDATED_MSG)
        self.person.refresh_from_db()
        self.alias.refresh_from_db()
        self.assertEqual(self.person.name, "ExistingAliasToken")
        self.assertEqual(self.alias.name, "ExistingAliasToken")
        self.assertContains(resp, "ExistingAliasToken")
        self.assertEqual(self.person.aliases.count(), 1)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffAliasAddTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_alias_add_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Alias add album")
        self.photo = _add_photo(self.item, position=1)
        self.person = Person.objects.create(name="CanonicalPersonToken")
        PhotoPerson.objects.create(photo_content=self.photo, person=self.person)
        _rebuild(self.item.pk)
        self.client.force_login(self.staff)
        self.url = _edit_url(self.person)

    def test_successful_add_strips_whitespace_and_refreshes_search(self):
        with patch(
            "documents.views.create_person_alias", wraps=create_person_alias
        ) as mocked:
            resp = self.client.post(
                self.url,
                data={"action": "add_alias", "alias_name": "  AddedAliasToken  "},
                follow=True,
            )
        mocked.assert_called_once()
        self.assertContains(resp, PERSON_ALIAS_ADDED_MSG)
        alias = PersonAlias.objects.get(person=self.person, name="AddedAliasToken")
        self.assertEqual(alias.name, "AddedAliasToken")
        self.assertEqual(_search_ids("AddedAliasToken"), [self.item.pk])
        self.assertEqual(_search_ids("CanonicalPersonToken"), [self.item.pk])

    def test_add_validation_errors(self):
        PersonAlias.objects.create(person=self.person, name="DuplicateAliasToken")
        cases = [
            ("", PERSON_ALIAS_REQUIRED_ERROR),
            ("   ", PERSON_ALIAS_REQUIRED_ERROR),
            ("x" * 256, PERSON_ALIAS_TOO_LONG_ERROR),
            ("CanonicalPersonToken", PERSON_ALIAS_MATCHES_CANONICAL_ERROR),
            ("DuplicateAliasToken", PERSON_ALIAS_DUPLICATE_ERROR),
        ]
        for payload, message in cases:
            resp = self.client.post(
                self.url, data={"action": "add_alias", "alias_name": payload}
            )
            self.assertEqual(resp.status_code, 200, payload)
            self.assertContains(resp, message)
        self.assertEqual(self.person.aliases.count(), 1)

    def test_invalid_action_is_bad_request(self):
        resp = self.client.post(self.url, data={"action": "merge", "name": "x"})
        self.assertEqual(resp.status_code, 400)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffAliasEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_alias_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Alias edit album")
        self.photo = _add_photo(self.item, position=1)
        self.person = Person.objects.create(name="CanonicalEditToken")
        self.alias = PersonAlias.objects.create(
            person=self.person, name="OldAliasToken"
        )
        PhotoPerson.objects.create(photo_content=self.photo, person=self.person)
        _rebuild(self.item.pk)
        self.client.force_login(self.staff)
        self.url = _alias_edit_url(self.person, self.alias)

    def test_successful_edit_uses_service_and_refreshes_search(self):
        with patch(
            "documents.views.update_person_alias", wraps=update_person_alias
        ) as mocked:
            resp = self.client.post(
                self.url, data={"name": "  NewAliasToken  "}, follow=True
            )
        mocked.assert_called_once()
        self.assertContains(resp, PERSON_ALIAS_UPDATED_MSG)
        self.alias.refresh_from_db()
        self.person.refresh_from_db()
        self.assertEqual(self.alias.name, "NewAliasToken")
        self.assertEqual(self.person.name, "CanonicalEditToken")
        self.assertEqual(_search_ids("NewAliasToken"), [self.item.pk])
        self.assertEqual(_search_ids("OldAliasToken"), [])

    def test_edit_validation_errors(self):
        PersonAlias.objects.create(person=self.person, name="OtherAliasToken")
        cases = [
            ("", PERSON_ALIAS_REQUIRED_ERROR),
            ("CanonicalEditToken", PERSON_ALIAS_MATCHES_CANONICAL_ERROR),
            ("OtherAliasToken", PERSON_ALIAS_DUPLICATE_ERROR),
            ("x" * 256, PERSON_ALIAS_TOO_LONG_ERROR),
        ]
        for payload, message in cases:
            resp = self.client.post(self.url, data={"name": payload})
            self.assertEqual(resp.status_code, 200, payload)
            self.assertContains(resp, message)
        self.alias.refresh_from_db()
        self.assertEqual(self.alias.name, "OldAliasToken")

    def test_alias_from_another_person_is_404(self):
        other = Person.objects.create(name="Other person")
        resp = self.client.get(_alias_edit_url(other, self.alias))
        self.assertEqual(resp.status_code, 404)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffAliasDeleteTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_alias_delete_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Alias delete album")
        self.photo = _add_photo(self.item, position=1)
        self.person = Person.objects.create(name="CanonicalDeleteToken")
        self.alias = PersonAlias.objects.create(
            person=self.person, name="DeletedAliasToken"
        )
        PhotoPerson.objects.create(photo_content=self.photo, person=self.person)
        _rebuild(self.item.pk)
        self.client.force_login(self.staff)
        self.url = _alias_delete_url(self.person, self.alias)

    def test_get_shows_confirmation_and_does_not_delete(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחיקת שם חלופי")
        self.assertTrue(PersonAlias.objects.filter(pk=self.alias.pk).exists())

    def test_post_deletes_alias_keeps_person_and_refreshes_search(self):
        with patch(
            "documents.views.delete_person_alias", wraps=delete_person_alias
        ) as mocked:
            resp = self.client.post(self.url, follow=True)
        mocked.assert_called_once()
        self.assertContains(resp, PERSON_ALIAS_DELETED_MSG)
        self.assertFalse(PersonAlias.objects.filter(pk=self.alias.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.person.pk).exists())
        self.person.refresh_from_db()
        self.assertEqual(self.person.name, "CanonicalDeleteToken")
        self.assertEqual(_search_ids("DeletedAliasToken"), [])
        self.assertEqual(_search_ids("CanonicalDeleteToken"), [self.item.pk])


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffIsolationAndPublicTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_isolation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Isolation album")
        self.photo = _add_photo(self.item, position=1)
        self.person = Person.objects.create(name="IsolationPerson")
        PhotoPerson.objects.create(photo_content=self.photo, person=self.person)
        ArchiveItemPerson.objects.create(archive_item=self.item, person=self.person)
        self.tag = Tag.objects.create(name="family-tag")
        self.item.tags.add(self.tag)
        self.client.force_login(self.staff)
        self.presign_view = patch(
            "documents.views.create_presigned_get",
            side_effect=_presign_url,
        )
        self.presign_thumbs = patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            side_effect=_presign_url,
        )
        self.presign_view.start()
        self.presign_thumbs.start()
        self.addCleanup(self.presign_view.stop)
        self.addCleanup(self.presign_thumbs.stop)

    def test_alias_crud_does_not_touch_relations_or_public_display(self):
        add_resp = self.client.post(
            _edit_url(self.person),
            data={"action": "add_alias", "alias_name": "PublicHiddenAlias"},
            follow=True,
        )
        self.assertContains(add_resp, PERSON_ALIAS_ADDED_MSG)
        alias = PersonAlias.objects.get(person=self.person, name="PublicHiddenAlias")
        self.client.post(
            _alias_edit_url(self.person, alias), data={"name": "StillHiddenAlias"}
        )
        alias.refresh_from_db()
        self.client.post(_alias_delete_url(self.person, alias))

        self.assertEqual(PhotoPerson.objects.filter(person=self.person).count(), 1)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(person=self.person).count(), 1
        )
        self.assertEqual(list(self.item.tags.all()), [self.tag])
        self.assertEqual(self.photo.people.count(), 1)
        self.assertEqual(self.item.people.count(), 1)

        PersonAlias.objects.create(person=self.person, name="PublicHiddenAlias")
        self.client.logout()
        public = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(public.status_code, 200)
        self.assertContains(public, "IsolationPerson")
        self.assertNotContains(public, "PublicHiddenAlias")
        self.assertNotContains(public, _edit_url(self.person))
        self.assertNotContains(public, "עריכת אדם")

    def test_public_detail_query_count_does_not_grow_with_aliases(self):
        public_url = reverse("archive-detail", kwargs={"item_id": self.item.id})
        self.client.logout()
        self.client.get(public_url)

        def _counts() -> tuple[int, int]:
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.get(public_url)
            self.assertEqual(resp.status_code, 200)
            return len(_alias_sql(ctx.captured_queries)), len(ctx.captured_queries)

        alias_before, total_before = _counts()
        self.assertEqual(alias_before, 0)
        PersonAlias.objects.create(person=self.person, name="UnusedPublicAlias")
        PersonAlias.objects.create(person=self.person, name="SecondUnusedPublicAlias")
        alias_after, total_after = _counts()
        self.assertEqual(alias_after, 0)
        self.assertEqual(total_before, total_after)

    def test_person_models_remain_unregistered_in_admin(self):
        self.assertFalse(django_admin.site.is_registered(Person))
        self.assertFalse(django_admin.site.is_registered(PersonAlias))


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PersonStaffCompatibilityTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_compat_staff",
            password="test-pass",
            is_staff=True,
        )
        self.multi = _create_photo_item(title="Multi photo")
        self.p1 = _add_photo(self.multi, position=1)
        self.p2 = _add_photo(self.multi, position=2)
        self.solo = _create_photo_item(title="Solo photo")
        self.solo_photo = _add_photo(self.solo, position=1)
        self.existing = Person.objects.create(name="ExistingPerson")
        self.client.force_login(self.staff)

    def test_new_person_name_still_creates_name_only_person(self):
        url = reverse(
            "archive-manage-photo-edit",
            kwargs={"item_id": self.multi.id, "photo_id": self.p2.id},
        )
        resp = self.client.post(
            url,
            data={
                "description": "",
                "people_present": "",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "person_ids": [str(self.existing.id)],
                "new_person_name": "  NewPersonToken  ",
            },
        )
        self.assertEqual(resp.status_code, 302)
        created = Person.objects.get(name="NewPersonToken")
        self.assertEqual(created.aliases.count(), 0)
        self.assertEqual(
            set(self.p2.people.values_list("id", flat=True)),
            {self.existing.id, created.id},
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=self.multi, person=created
            ).exists()
        )
        edit_page = self.client.get(url)
        self.assertContains(edit_page, _edit_url(created))
        self.assertContains(edit_page, _edit_url(self.existing))

    def test_one_photo_and_multi_photo_edit_pages_remain_functional(self):
        PhotoPerson.objects.create(photo_content=self.p1, person=self.existing)
        multi_edit = self.client.get(
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.multi.id, "photo_id": self.p1.id},
            )
        )
        solo_edit = self.client.get(
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.solo.id, "photo_id": self.solo_photo.id},
            )
        )
        item_edit = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": self.multi.id})
        )
        self.assertEqual(multi_edit.status_code, 200)
        self.assertEqual(solo_edit.status_code, 200)
        self.assertEqual(item_edit.status_code, 200)
        self.assertContains(multi_edit, 'name="person_ids"')
        self.assertContains(solo_edit, 'name="person_ids"')
        self.assertContains(multi_edit, _edit_url(self.existing))
        self.assertNotContains(solo_edit, _edit_url(self.existing))
