"""Staff Person merge confirmation UI and access control."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from documents.historical_person_tag_map import HISTORICAL_PERSON_NAME_TAG_RECORDS
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.person_merge import (
    PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR,
    PERSON_MERGE_FROZEN_DUPLICATE_ERROR,
    PERSON_MERGE_ID_REQUIRED_ERROR,
    PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR,
    PERSON_MERGE_SAME_ID_ERROR,
)
from documents.views import PERSON_MERGED_MSG


FROZEN_PERSON_IDS = frozenset(
    person_id for _tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS
)


def _next_ordinary_person_id() -> int:
    """Return an unused Person.id above frozen historical ids and existing PKs."""
    max_frozen = max(FROZEN_PERSON_IDS)
    max_existing = Person.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    candidate = max(max_frozen, max_existing) + 1
    while candidate in FROZEN_PERSON_IDS or Person.objects.filter(pk=candidate).exists():
        candidate += 1
    return candidate


def _create_ordinary_person(*, name: str, biography: str = "") -> Person:
    """Create a test Person whose id is not a frozen historical Person id."""
    return Person.objects.create(
        id=_next_ordinary_person_id(),
        name=name,
        biography=biography,
    )


def _create_frozen_person(*, name: str) -> Person:
    for frozen_id in sorted(FROZEN_PERSON_IDS):
        if not Person.objects.filter(pk=frozen_id).exists():
            return Person.objects.create(id=frozen_id, name=name)
    raise AssertionError("no unused frozen historical Person id")


def _edit_url(person: Person) -> str:
    return reverse("archive-manage-person-edit", kwargs={"person_id": person.id})


def _merge_url(person: Person) -> str:
    return reverse("archive-manage-person-merge", kwargs={"person_id": person.id})


def _create_photo_item(*, title: str) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _add_photo(item: ArchiveItem) -> PhotoContent:
    return PhotoContent.objects.create(
        archive_item=item,
        position=1,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )


class PersonMergeStaffAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_merge_staff",
            password="test-pass",
            is_staff=True,
        )
        self.keeper = _create_ordinary_person(name="Keeper Person")
        self.duplicate = _create_ordinary_person(name="Duplicate Person")
        self.url = _merge_url(self.keeper)

    def test_staff_can_open_confirmation(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url, data={"duplicate_id": str(self.duplicate.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אישור מיזוג רשומת אדם")
        self.assertContains(resp, "יישאר")
        self.assertContains(resp, "יימחק")
        self.assertContains(resp, str(self.keeper.id))
        self.assertContains(resp, str(self.duplicate.id))
        self.assertContains(resp, "Keeper Person")
        self.assertContains(resp, "Duplicate Person")
        self.assertContains(resp, "יחדל להתקיים")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url, data={"duplicate_id": str(self.duplicate.id)})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    def test_non_staff_is_forbidden(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="person_merge_family",
            password="test-pass",
        )
        user.groups.add(family_group)
        self.client.force_login(user)
        resp = self.client.get(self.url, data={"duplicate_id": str(self.duplicate.id)})
        self.assertEqual(resp.status_code, 403)

    def test_get_confirmation_never_mutates(self):
        self.client.force_login(self.staff)
        PersonAlias.objects.create(person=self.duplicate, name="StayOnDuplicate")
        resp = self.client.get(self.url, data={"duplicate_id": str(self.duplicate.id)})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())
        self.assertTrue(
            PersonAlias.objects.filter(
                person=self.duplicate, name="StayOnDuplicate"
            ).exists()
        )
        self.assertFalse(PersonAlias.objects.filter(person=self.keeper).exists())

    def test_unsupported_method_returns_405_and_does_not_mutate(self):
        self.client.force_login(self.staff)
        PersonAlias.objects.create(person=self.duplicate, name="StayOnDuplicate")
        payload = {
            "duplicate_id": str(self.duplicate.id),
            "confirm_merge": "1",
        }

        for method in ("put", "patch", "delete"):
            resp = getattr(self.client, method)(self.url, data=payload)
            self.assertEqual(resp.status_code, 405, method)

        self.assertTrue(Person.objects.filter(pk=self.keeper.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(self.keeper.name, "Keeper Person")
        self.assertEqual(self.duplicate.name, "Duplicate Person")
        self.assertTrue(
            PersonAlias.objects.filter(
                person=self.duplicate, name="StayOnDuplicate"
            ).exists()
        )
        self.assertFalse(PersonAlias.objects.filter(person=self.keeper).exists())


class PersonMergeStaffFlowTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_merge_flow_staff",
            password="test-pass",
            is_staff=True,
        )
        self.keeper = _create_ordinary_person(name="Keeper Person")
        self.duplicate = _create_ordinary_person(name="Duplicate Person")
        self.client.force_login(self.staff)

    def test_edit_page_has_merge_section_and_index_does_not(self):
        edit = self.client.get(_edit_url(self.keeper))
        self.assertEqual(edit.status_code, 200)
        self.assertContains(edit, "מיזוג רשומת אדם כפולה")
        self.assertContains(edit, _merge_url(self.keeper))
        self.assertContains(edit, 'name="duplicate_id"')
        self.assertContains(edit, 'method="get"')

        index = self.client.get(reverse("archive-manage-people"))
        self.assertEqual(index.status_code, 200)
        self.assertContains(index, _edit_url(self.keeper))
        self.assertNotContains(index, _merge_url(self.keeper))
        self.assertNotContains(index, "מיזוג רשומת אדם כפולה")
        html = index.content.decode()
        self.assertNotIn("/merge/", html)

    def test_successful_post_redirects_to_keeper_edit_with_success(self):
        resp = self.client.post(
            _merge_url(self.keeper),
            data={
                "duplicate_id": str(self.duplicate.id),
                "confirm_merge": "1",
            },
            follow=True,
        )
        self.assertContains(resp, PERSON_MERGED_MSG)
        self.assertEqual(resp.resolver_match.url_name, "archive-manage-person-edit")
        self.assertEqual(resp.resolver_match.kwargs["person_id"], self.keeper.id)
        self.assertTrue(Person.objects.filter(pk=self.keeper.pk).exists())
        self.assertFalse(Person.objects.filter(pk=self.duplicate.pk).exists())
        self.keeper.refresh_from_db()
        self.assertEqual(self.keeper.name, "Keeper Person")

    def test_post_without_confirm_does_not_merge(self):
        resp = self.client.post(
            _merge_url(self.keeper),
            data={"duplicate_id": str(self.duplicate.id)},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())

    def test_same_id_leaves_person_intact(self):
        resp = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": str(self.keeper.id)}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_MERGE_SAME_ID_ERROR)
        self.assertTrue(Person.objects.filter(pk=self.keeper.pk).exists())

    def test_missing_and_invalid_ids_leave_both_intact(self):
        missing = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": "999999"}
        )
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, "אדם מזוהה לא נמצא")

        invalid = self.client.get(
            _merge_url(self.keeper), data={"duplicate_id": "abc"}
        )
        self.assertEqual(invalid.status_code, 200)

        empty = self.client.get(_merge_url(self.keeper))
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, PERSON_MERGE_ID_REQUIRED_ERROR)

        self.assertTrue(Person.objects.filter(pk=self.keeper.pk).exists())
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())

    def test_frozen_duplicate_and_biography_conflict_do_not_mutate(self):
        frozen = _create_frozen_person(name="Frozen Dup")
        frozen_resp = self.client.post(
            _merge_url(self.keeper),
            data={"duplicate_id": str(frozen.id), "confirm_merge": "1"},
        )
        self.assertEqual(frozen_resp.status_code, 200)
        self.assertContains(frozen_resp, PERSON_MERGE_FROZEN_DUPLICATE_ERROR)
        self.assertTrue(Person.objects.filter(pk=frozen.pk).exists())

        self.keeper.biography = "Alpha"
        self.keeper.save(update_fields=["biography"])
        self.duplicate.biography = "Beta"
        self.duplicate.save(update_fields=["biography"])
        bio_resp = self.client.post(
            _merge_url(self.keeper),
            data={"duplicate_id": str(self.duplicate.id), "confirm_merge": "1"},
        )
        self.assertEqual(bio_resp.status_code, 200)
        self.assertContains(bio_resp, PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR)
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())
        self.keeper.refresh_from_db()
        self.duplicate.refresh_from_db()
        self.assertEqual(self.keeper.biography, "Alpha")
        self.assertEqual(self.duplicate.biography, "Beta")

    def test_pending_suggestion_conflict_leaves_both_intact(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Conflict",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=self.keeper,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=self.duplicate,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        resp = self.client.post(
            _merge_url(self.keeper),
            data={"duplicate_id": str(self.duplicate.id), "confirm_merge": "1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR)
        self.assertTrue(Person.objects.filter(pk=self.duplicate.pk).exists())
        self.assertEqual(
            ArchiveItemPersonSuggestion.objects.filter(person=self.duplicate).count(),
            1,
        )


class PersonMergePublicIsolationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="person_merge_public_staff",
            password="test-pass",
            is_staff=True,
        )
        self.person = _create_ordinary_person(name="Public Isolation Person")
        self.item = _create_photo_item(title="Public isolation album")
        photo = _add_photo(self.item)
        PhotoPerson.objects.create(photo_content=photo, person=self.person)
        ArchiveItemPerson.objects.create(archive_item=self.item, person=self.person)

    def test_public_pages_never_expose_staff_merge_urls_or_actions(self):
        public_list = self.client.get(reverse("archive-list"))
        public_person = self.client.get(
            reverse("archive-person-detail", kwargs={"person_id": self.person.id})
        )
        public_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(public_list.status_code, 200)
        self.assertEqual(public_person.status_code, 200)
        self.assertEqual(public_detail.status_code, 200)
        merge_url = _merge_url(self.person)
        for resp in (public_list, public_person, public_detail):
            self.assertNotContains(resp, merge_url)
            self.assertNotContains(resp, "מיזוג רשומת אדם כפולה")
            self.assertNotContains(resp, "confirm_merge")
