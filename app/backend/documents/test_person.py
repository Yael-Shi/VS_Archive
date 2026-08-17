"""Model/constraint tests for Person identity foundation."""

from __future__ import annotations

from django.contrib import admin as django_admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from documents.admin import ArchiveItemAdmin, PhotoContentAdmin
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PhotoContent,
    PhotoPerson,
)


def _create_archive_item(
    *,
    title: str = "Letter",
    item_type: str = ArchiveItem.ItemType.MANUAL_TEXT,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=item_type,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _create_photo_content(*, title: str = "Family photo") -> PhotoContent:
    archive_item = _create_archive_item(
        title=title,
        item_type=ArchiveItem.ItemType.PHOTO,
    )
    return PhotoContent.objects.create(
        archive_item=archive_item,
        original_file_key=f"photos/{archive_item.id}/original.jpg",
        original_filename="scan.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )


class PersonModelTests(TestCase):
    def test_person_can_be_created(self):
        person = Person.objects.create(name="רחל כהן")
        self.assertEqual(person.name, "רחל כהן")
        self.assertIsNotNone(person.created_at)
        self.assertIsNotNone(person.updated_at)
        self.assertEqual(str(person), "רחל כהן")

    def test_same_display_name_may_identify_distinct_people(self):
        first = Person.objects.create(name="משה כהן")
        second = Person.objects.create(name="משה כהן")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Person.objects.filter(name="משה כהן").count(), 2)


class ArchiveItemPersonRelationTests(TestCase):
    def test_one_person_can_relate_to_multiple_archive_items(self):
        person = Person.objects.create(name="Ada Lovelace")
        first_item = _create_archive_item(title="Letter A")
        second_item = _create_archive_item(title="Letter B")

        ArchiveItemPerson.objects.create(archive_item=first_item, person=person)
        ArchiveItemPerson.objects.create(archive_item=second_item, person=person)

        self.assertEqual(person.archive_items.count(), 2)
        self.assertCountEqual(
            person.archive_items.values_list("pk", flat=True),
            [first_item.pk, second_item.pk],
        )

    def test_one_archive_item_can_relate_to_multiple_persons(self):
        item = _create_archive_item(title="Shared letter")
        first_person = Person.objects.create(name="Ada")
        second_person = Person.objects.create(name="Charles")

        ArchiveItemPerson.objects.create(archive_item=item, person=first_person)
        ArchiveItemPerson.objects.create(archive_item=item, person=second_person)

        self.assertEqual(item.people.count(), 2)
        self.assertCountEqual(
            item.people.values_list("pk", flat=True),
            [first_person.pk, second_person.pk],
        )

    def test_duplicate_archive_item_person_relation_is_rejected(self):
        item = _create_archive_item(title="Duplicate letter")
        person = Person.objects.create(name="Ada")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemPerson.objects.create(archive_item=item, person=person)

        self.assertEqual(ArchiveItemPerson.objects.count(), 1)


class PhotoPersonRelationTests(TestCase):
    def test_one_person_can_appear_in_multiple_photo_content_rows(self):
        person = Person.objects.create(name="Grandpa")
        first_photo = _create_photo_content(title="Wedding")
        second_photo = _create_photo_content(title="Picnic")

        PhotoPerson.objects.create(photo_content=first_photo, person=person)
        PhotoPerson.objects.create(photo_content=second_photo, person=person)

        self.assertEqual(person.photo_contents.count(), 2)
        self.assertCountEqual(
            person.photo_contents.values_list("pk", flat=True),
            [first_photo.pk, second_photo.pk],
        )

    def test_one_photo_content_can_contain_multiple_identified_persons(self):
        photo = _create_photo_content(title="Family portrait")
        first_person = Person.objects.create(name="Grandpa")
        second_person = Person.objects.create(name="Grandma")

        PhotoPerson.objects.create(photo_content=photo, person=first_person)
        PhotoPerson.objects.create(photo_content=photo, person=second_person)

        self.assertEqual(photo.people.count(), 2)
        self.assertCountEqual(
            photo.people.values_list("pk", flat=True),
            [first_person.pk, second_person.pk],
        )

    def test_duplicate_photo_person_relation_is_rejected(self):
        photo = _create_photo_content(title="Duplicate portrait")
        person = Person.objects.create(name="Grandpa")
        PhotoPerson.objects.create(photo_content=photo, person=person)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PhotoPerson.objects.create(photo_content=photo, person=person)

        self.assertEqual(PhotoPerson.objects.count(), 1)


class PhotoContentCompatibilityTests(TestCase):
    def test_people_present_free_text_continues_to_work_unchanged(self):
        photo = _create_photo_content()
        self.assertEqual(photo.people_present, "")

        photo.people_present = "Uncle Moshe, maybe Aunt Rivka"
        photo.save(update_fields=["people_present", "updated_at"])
        photo.refresh_from_db()

        self.assertEqual(photo.people_present, "Uncle Moshe, maybe Aunt Rivka")
        self.assertEqual(photo.people.count(), 0)

    def test_people_present_coexists_with_identified_photo_people(self):
        photo = _create_photo_content()
        identified = Person.objects.create(name="רחל כהן")
        PhotoPerson.objects.create(photo_content=photo, person=identified)
        photo.people_present = "someone in the back, unidentified"
        photo.save(update_fields=["people_present", "updated_at"])
        photo.refresh_from_db()

        self.assertEqual(photo.people_present, "someone in the back, unidentified")
        self.assertEqual(list(photo.people.all()), [identified])

    def test_photo_content_one_to_one_and_metadata_defaults_remain(self):
        photo = _create_photo_content()
        archive_item = photo.archive_item

        self.assertEqual(archive_item.photo_content.id, photo.id)
        self.assertEqual(photo.description, "")
        self.assertEqual(photo.location, "")
        self.assertEqual(photo.context, "")
        self.assertEqual(photo.people_present, "")
        self.assertEqual(photo.notes, "")
        self.assertEqual(photo.people.count(), 0)
        self.assertEqual(archive_item.people.count(), 0)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PhotoContent.objects.create(
                    archive_item=archive_item,
                    original_file_key="photos/dup/original.jpg",
                    original_filename="dup.jpg",
                    original_mime_type="image/jpeg",
                    original_size_bytes=1024,
                )


class PersonAdminExposureTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = User.objects.create_superuser(
            username="person_admin_exposure",
            password="test-pass",
            email="person-admin@example.com",
        )
        self.site = AdminSite()

    def test_archive_item_admin_form_does_not_include_people(self):
        item = _create_archive_item()
        admin = ArchiveItemAdmin(ArchiveItem, self.site)
        add_form = admin.get_form(self.request)
        change_form = admin.get_form(self.request, obj=item)

        self.assertNotIn("people", add_form.base_fields)
        self.assertNotIn("people", change_form.base_fields)
        self.assertNotIn("people", admin.get_fields(self.request, obj=item))
        self.assertIn("people", admin.get_exclude(self.request) or ())
        self.assertIn("tags", add_form.base_fields)
        self.assertIn("categories", add_form.base_fields)
        self.assertIn("events", add_form.base_fields)

    def test_photo_content_admin_form_does_not_include_people(self):
        photo = _create_photo_content()
        admin = PhotoContentAdmin(PhotoContent, self.site)
        add_form = admin.get_form(self.request)
        change_form = admin.get_form(self.request, obj=photo)

        self.assertNotIn("people", add_form.base_fields)
        self.assertNotIn("people", change_form.base_fields)
        self.assertNotIn("people", admin.get_fields(self.request, obj=photo))
        self.assertIn("people", admin.get_exclude(self.request) or ())
        self.assertIn("people_present", add_form.base_fields)
        self.assertNotIn("people", admin.readonly_fields)

    def test_person_models_are_not_registered_in_django_admin(self):
        self.assertFalse(django_admin.site.is_registered(Person))
        self.assertFalse(django_admin.site.is_registered(ArchiveItemPerson))
        self.assertFalse(django_admin.site.is_registered(PhotoPerson))
