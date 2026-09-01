"""PR3: staff management of multiple PhotoContent rows under one PHOTO item."""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from unittest.mock import call, patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveItem,
    ArchiveItemPerson,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.s3 import S3HeadObjectResult, build_photo_original_s3_key
from documents.services.photo_content_management import (
    LAST_PHOTO_DELETE_ERROR,
    PERSON_NAME_TOO_LONG_ERROR,
    PERSON_NAMES_COMMAS_ONLY_ERROR,
    PERSON_NOT_FOUND_ERROR,
    next_photo_position,
    reorder_photo_contents,
)
from documents.services.photo_upload import create_additional_photo_upload_plan


def _create_photo_item(*, title: str = "Album") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        public_note="Shared note",
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    filename: str = "photo.jpg",
    description: str = "",
    people_present: str = "",
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
    thumbnail_file_key: str = "",
) -> PhotoContent:
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=original_file_key or f"photos/{position}/original.jpg",
        original_filename=filename,
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
        upload_error="",
        description=description,
        people_present=people_present,
        thumbnail_file_key=thumbnail_file_key,
    )
    if original_file_key is None:
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoMultiManagePageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="multi_photo_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Family album")
        self.first = _add_photo(
            self.item, position=1, filename="one.jpg", description="First"
        )
        self.second = _add_photo(
            self.item, position=2, filename="two.jpg", description="Second"
        )
        self.third = _add_photo(
            self.item, position=3, filename="three.jpg", description="Third"
        )
        self.client.force_login(self.staff)

    def test_manage_page_lists_photos_in_position_id_order(self):
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn("מטא־דאטה משותף לפריט", content)
        self.assertIn("תמונות בפריט זה", content)
        self.assertLess(content.index("one.jpg"), content.index("two.jpg"))
        self.assertLess(content.index("two.jpg"), content.index("three.jpg"))
        self.assertContains(resp, "הוספת תמונה")
        self.assertContains(resp, "למעלה")
        self.assertContains(resp, "למטה")

    def test_one_photo_manage_page_stays_compact_without_reorder(self):
        solo = _create_photo_item(title="Single photo")
        photo = _add_photo(solo, position=1, filename="only.jpg")
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": solo.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "only.jpg")
        self.assertContains(
            resp,
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": solo.id, "photo_id": photo.id},
            ),
        )
        self.assertNotContains(resp, ">למעלה<")
        self.assertNotContains(resp, ">למטה<")

    def test_manage_page_prefetches_photo_people_without_n_plus_one(self):
        person = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=self.first, person=person)
        PhotoPerson.objects.create(photo_content=self.second, person=person)
        url = reverse("archive-manage-edit", kwargs={"item_id": self.item.id})
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Ada")
        photo_people_queries = [
            query["sql"]
            for query in ctx.captured_queries
            if "documents_photoperson" in query["sql"].lower()
        ]
        self.assertLessEqual(len(photo_people_queries), 1)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoAddUploadTests(TestCase):
    ADD_URL = "/api/photo-uploads/add/"

    def setUp(self):
        self.staff = User.objects.create_user(
            username="add_photo_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item()
        self.first = _add_photo(self.item, position=1, filename="one.jpg")
        self.client.force_login(self.staff)
        self.presigned_patcher = patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://s3.example/presigned-put",
        )
        self.mock_presigned = self.presigned_patcher.start()
        self.addCleanup(self.presigned_patcher.stop)
        self.s3_head_patcher = patch(
            "documents.services.photo_upload.head_s3_object",
            return_value=S3HeadObjectResult(
                exists=True,
                content_type="image/jpeg",
                content_length=4096,
            ),
        )
        self.mock_s3_head = self.s3_head_patcher.start()
        self.addCleanup(self.s3_head_patcher.stop)
        self.thumbnail_patcher = patch(
            "documents.services.photo_upload.generate_and_persist_photo_thumbnail",
            return_value=None,
        )
        self.thumbnail_patcher.start()
        self.addCleanup(self.thumbnail_patcher.stop)

    def _payload(self, **overrides):
        payload = {
            "archive_item_id": self.item.id,
            "original_name": "two.jpg",
            "mime_type": "image/jpeg",
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return payload

    def test_add_page_reuses_photo_upload_architecture(self):
        resp = self.client.get(
            reverse("archive-manage-photo-add", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="photoUploadForm"')
        self.assertContains(resp, "/api/photo-uploads/add/")
        self.assertContains(resp, 'name="description"')
        self.assertContains(resp, 'name="person_ids"')
        self.assertContains(resp, 'name="new_person_name"')
        self.assertContains(resp, 'name="people_present"')
        self.assertContains(resp, 'readSelectedIds("person_ids")')
        self.assertContains(resp, 'getElementById("new_person_name")')
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.item.id}),
        )
        match = re.search(
            r'<input[^>]*id="new_person_name"[^>]*>',
            resp.content.decode(),
        )
        self.assertIsNotNone(match)
        self.assertNotIn("maxlength", match.group(0))
        self.assertNotContains(resp, 'name="title"')
        self.assertNotContains(resp, 'name="categories"')
        self.assertNotContains(resp, 'name="archive_item_person_ids"')
        self.assertNotContains(resp, 'name="new_archive_item_person_name"')

    def test_add_page_file_input_stays_single_file(self):
        resp = self.client.get(
            reverse("archive-manage-photo-add", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        match = re.search(r'<input[^>]*id="file"[^>]*>', html)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertNotIn("multiple", match.group(0))
        self.assertIn("תמונה אחת", html)
        # Add mode keeps its single-file slice even though the shared script
        # supports multi-file create.
        self.assertIn('uploadMode === "add" ? files.slice(0, 1) : files', html)

    @patch("documents.views.enqueue_uploaded_document_processing")
    def test_second_photo_is_allocated_position_two(self, mock_enqueue):
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload()),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["archive_item_id"], self.item.id)
        self.assertEqual(body["position"], 2)
        photo = PhotoContent.objects.get(pk=body["photo_content_id"])
        self.assertEqual(photo.position, 2)
        self.assertEqual(photo.archive_item_id, self.item.id)
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.PENDING)
        self.assertEqual(
            photo.original_file_key,
            build_photo_original_s3_key(photo.id, "image/jpeg"),
        )
        self.assertEqual(self.item.photo_contents.count(), 2)
        self.assertEqual(ArchiveItem.objects.filter(pk=self.item.id).count(), 1)
        self.assertFalse(Document.objects.exists())
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_uploaded_document_processing")
    def test_third_photo_position_is_deterministic(self, mock_enqueue):
        self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(original_name="two.jpg")),
            content_type="application/json",
        )
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(original_name="three.jpg")),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["position"], 3)
        positions = list(
            self.item.photo_contents.order_by("position").values_list(
                "position", flat=True
            )
        )
        self.assertEqual(positions, [1, 2, 3])
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_uploaded_document_processing")
    def test_invalid_add_does_not_create_photo_or_document(self, mock_enqueue):
        before = PhotoContent.objects.count()
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(
                self._payload(mime_type="image/gif", original_name="x.gif")
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(PhotoContent.objects.count(), before)
        self.assertFalse(Document.objects.exists())
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_uploaded_document_processing")
    def test_failed_finalize_marks_added_photo_failed(self, mock_enqueue):
        created = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload()),
            content_type="application/json",
        ).json()
        resp = self.client.post(
            f"/api/photo-uploads/{created['photo_content_id']}/complete/",
            data=json.dumps({"success": False, "error": "S3 PUT failed"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        photo = PhotoContent.objects.get(pk=created["photo_content_id"])
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.FAILED)
        self.assertEqual(photo.position, 2)
        self.assertTrue(ArchiveItem.objects.filter(pk=self.item.id).exists())
        mock_enqueue.assert_not_called()

    def test_add_does_not_use_model_default_position_when_max_is_higher(self):
        PhotoContent.objects.filter(pk=self.first.pk).update(position=4)
        self.assertEqual(next_photo_position(self.item), 5)
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload()),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["position"], 5)

    def test_add_rejects_non_photo_archive_item(self):
        manual = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Note",
        )
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(archive_item_id=manual.id)),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_add_with_existing_person_creates_photo_person_only(self):
        existing = Person.objects.create(name="Ada")
        ArchiveItemPerson.objects.create(archive_item=self.item, person=existing)
        before_item_people = ArchiveItemPerson.objects.filter(
            archive_item=self.item
        ).count()
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(
                self._payload(
                    person_ids=[existing.id],
                    people_present="maybe uncle, crowd",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        self.assertEqual(list(photo.people.values_list("id", flat=True)), [existing.id])
        self.assertEqual(photo.people_present, "maybe uncle, crowd")
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=self.item).count(),
            before_item_people,
        )
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(self.first.people.count(), 0)

    def test_add_combines_selected_and_new_people_as_photo_person_only(self):
        existing = Person.objects.create(name="Selected")
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(
                self._payload(
                    person_ids=[existing.id],
                    new_person_name="Newly Added",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        created = Person.objects.get(name="Newly Added")
        self.assertEqual(
            set(photo.people.values_list("id", flat=True)),
            {existing.id, created.id},
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=self.item).exists()
        )
        self.assertEqual(self.first.people.count(), 0)

    def test_add_comma_separated_new_names_link_only_the_new_photo(self):
        existing = Person.objects.create(name="רחל כהן")
        PersonAlias.objects.create(person=existing, name="Ada Lovelace")
        ArchiveItemPerson.objects.create(archive_item=self.item, person=existing)
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(
                self._payload(
                    new_person_name="  רחל כהן , ,Ada Lovelace, רחל כהן ",
                    people_present="crowd",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        created = list(
            Person.objects.filter(name__in=["רחל כהן", "Ada Lovelace"])
            .exclude(pk=existing.pk)
            .order_by("id")
        )
        self.assertEqual(
            [person.name for person in created], ["רחל כהן", "Ada Lovelace"]
        )
        self.assertEqual(
            list(photo.people.order_by("id").values_list("name", flat=True)),
            ["רחל כהן", "Ada Lovelace"],
        )
        for person in created:
            self.assertTrue(
                PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
            )
            self.assertFalse(
                ArchiveItemPerson.objects.filter(
                    archive_item=self.item, person=person
                ).exists()
            )
            self.assertEqual(person.aliases.count(), 0)
        self.assertEqual(photo.people_present, "crowd")
        self.assertEqual(
            set(self.item.people.values_list("id", flat=True)),
            {existing.id},
        )
        self.assertEqual(self.first.people.count(), 0)

    def test_add_exact_in_input_dedupe_keeps_case_distinct(self):
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(new_person_name="Ada, ada, Ada")),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        self.assertEqual(
            list(photo.people.order_by("id").values_list("name", flat=True)),
            ["Ada", "ada"],
        )
        self.assertEqual(Person.objects.filter(name__in=["Ada", "ada"]).count(), 2)
        self.assertFalse(ArchiveItemPerson.objects.exists())

    def test_add_rejects_commas_only_and_overlong_new_names_without_rows(self):
        before_photos = PhotoContent.objects.count()
        before_people = Person.objects.count()
        commas_only = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(new_person_name=", , ,")),
            content_type="application/json",
        )
        self.assertEqual(commas_only.status_code, 400)
        self.assertEqual(commas_only.json()["error"], PERSON_NAMES_COMMAS_ONLY_ERROR)
        self.assertEqual(PhotoContent.objects.count(), before_photos)
        self.assertEqual(Person.objects.count(), before_people)

        too_long = "y" * 256
        length_resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(new_person_name=f"{'a' * 200}, {too_long}")),
            content_type="application/json",
        )
        self.assertEqual(length_resp.status_code, 400)
        self.assertEqual(length_resp.json()["error"], PERSON_NAME_TOO_LONG_ERROR)
        self.assertEqual(PhotoContent.objects.count(), before_photos)
        self.assertEqual(Person.objects.count(), before_people)

    def test_add_rejects_unknown_person_id_before_creating_rows(self):
        before_photos = PhotoContent.objects.count()
        before_people = Person.objects.count()
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(person_ids=[999_999])),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["error"], PERSON_NOT_FOUND_ERROR)
        self.assertEqual(PhotoContent.objects.count(), before_photos)
        self.assertEqual(Person.objects.count(), before_people)

    def test_add_people_present_stays_free_text(self):
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(self._payload(people_present="Ada, Charles, Ada Lovelace")),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        self.assertEqual(photo.people_present, "Ada, Charles, Ada Lovelace")
        self.assertEqual(photo.people.count(), 0)
        self.assertFalse(Person.objects.exists())
        self.assertFalse(ArchiveItemPerson.objects.exists())

    def test_add_ignores_archive_item_person_payload_keys(self):
        person = Person.objects.create(name="ShouldStayItemOnly")
        resp = self.client.post(
            self.ADD_URL,
            data=json.dumps(
                self._payload(
                    archive_item_person_ids=[person.id],
                    new_archive_item_person_name="ShouldNotCreate",
                )
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        self.assertEqual(photo.people.count(), 0)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=self.item).exists()
        )
        self.assertFalse(Person.objects.filter(name="ShouldNotCreate").exists())

    def test_photo_create_still_writes_archive_item_person_only(self):
        existing = Person.objects.create(name="CreateExisting")
        resp = self.client.post(
            "/api/photo-uploads/create/",
            data=json.dumps(
                {
                    "title": "Create people photo",
                    "visibility": ArchiveItem.Visibility.PUBLIC,
                    "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                    "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                    "original_name": "photo.jpg",
                    "mime_type": "image/jpeg",
                    "archive_item_person_ids": [existing.id],
                    "new_archive_item_person_name": "Create New Token",
                    "person_ids": [existing.id],
                    "new_person_name": "ShouldNotBePhotoPerson",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        item = ArchiveItem.objects.get(id=resp.json()["archive_item_id"])
        photo = PhotoContent.objects.get(pk=resp.json()["photo_content_id"])
        created = Person.objects.get(name="Create New Token")
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {existing.id, created.id},
        )
        self.assertEqual(photo.people.count(), 0)
        self.assertFalse(Person.objects.filter(name="ShouldNotBePhotoPerson").exists())
        self.assertEqual(PhotoPerson.objects.count(), 0)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoComponentEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_component_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Shared title")
        self.item.date_precision = ArchiveItem.DatePrecision.YEAR
        self.item.date_start = date(1950, 1, 1)
        self.item.date_end = date(1950, 12, 31)
        self.item.save()
        self.category = ArchiveCategory.objects.create(name="Weddings", slug="weddings")
        self.item.categories.add(self.category)
        self.first = _add_photo(
            self.item,
            position=1,
            filename="one.jpg",
            description="First caption",
            people_present="maybe uncle",
        )
        self.second = _add_photo(
            self.item,
            position=2,
            filename="two.jpg",
            description="Second caption",
            people_present="crowd",
        )
        self.client.force_login(self.staff)

    def _edit_url(self, photo: PhotoContent) -> str:
        return reverse(
            "archive-manage-photo-edit",
            kwargs={"item_id": self.item.id, "photo_id": photo.id},
        )

    def test_edit_affects_only_selected_photo(self):
        resp = self.client.post(
            self._edit_url(self.second),
            data={
                "description": "Updated second",
                "location": "Haifa",
                "context": "After lunch",
                "people_present": "crowd",
                "notes": "scan 2",
                "date_precision": ArchiveItem.DatePrecision.YEAR,
                "date_start_year": "1948",
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._edit_url(self.second))
        self.first.refresh_from_db()
        self.second.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.second.description, "Updated second")
        self.assertEqual(self.second.location, "Haifa")
        self.assertEqual(self.second.date_start, date(1948, 1, 1))
        self.assertEqual(self.first.description, "First caption")
        self.assertEqual(self.first.location, "")
        self.assertEqual(self.item.title, "Shared title")
        self.assertEqual(self.item.date_start, date(1950, 1, 1))
        self.assertEqual(
            list(self.item.categories.values_list("name", flat=True)),
            ["Weddings"],
        )

    def test_photo_edit_form_omits_shared_discovery_fields(self):
        resp = self.client.get(self._edit_url(self.first))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="description"')
        self.assertContains(resp, 'name="people_present"')
        self.assertContains(resp, 'name="person_ids"')
        self.assertNotContains(resp, 'name="title"')
        self.assertNotContains(resp, 'name="categories"')
        self.assertNotContains(resp, 'name="events"')
        self.assertNotContains(resp, 'name="tags"')
        self.assertNotContains(resp, 'name="visibility"')
        public_url = (
            reverse("archive-detail", kwargs={"item_id": self.item.id})
            + f"?photo={self.first.id}"
        )
        self.assertContains(resp, f'href="{public_url}"')
        self.assertContains(resp, ">צפייה<")
        manage_edit = reverse("archive-manage-edit", kwargs={"item_id": self.item.id})
        self.assertContains(resp, f'href="{manage_edit}"')
        self.assertContains(resp, ">חזרה לפריט<")

    def test_invalid_photo_date_range_is_rejected(self):
        resp = self.client.post(
            self._edit_url(self.first),
            data={
                "description": "Bad dates",
                "date_precision": ArchiveItem.DatePrecision.RANGE,
                "date_start": "1960-01-02",
                "date_end": "1960-01-01",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.has_header("Location"))
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, "First caption")
        self.assertContains(resp, "date_end must not be before date_start")

    def test_person_selection_adds_and_removes_photo_person_only(self):
        ada = Person.objects.create(name="Ada")
        charles = Person.objects.create(name="Charles")
        PhotoPerson.objects.create(photo_content=self.first, person=ada)
        ArchiveItemPerson.objects.create(archive_item=self.item, person=charles)

        resp = self.client.post(
            self._edit_url(self.first),
            data={
                "description": "First caption",
                "people_present": "maybe uncle",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "person_ids": [str(charles.id)],
            },
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._edit_url(self.first))
        self.assertEqual(
            set(self.first.people.values_list("id", flat=True)),
            {charles.id},
        )
        self.assertEqual(self.first.people_present, "maybe uncle")
        self.assertEqual(
            set(self.item.people.values_list("id", flat=True)),
            {charles.id},
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=self.item, person=ada
            ).exists()
        )

    def test_minimal_person_create_links_only_the_photo(self):
        resp = self.client.post(
            self._edit_url(self.second),
            data={
                "description": "Second caption",
                "people_present": "crowd",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "new_person_name": "  רחל כהן  ",
            },
        )
        self.assertEqual(resp.status_code, 302)
        person = Person.objects.get(name="רחל כהן")
        self.assertTrue(
            PhotoPerson.objects.filter(
                photo_content=self.second, person=person
            ).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=self.item, person=person
            ).exists()
        )
        self.assertEqual(self.second.people_present, "crowd")

    def test_comma_separated_new_names_link_only_the_photo(self):
        existing = Person.objects.create(name="רחל כהן")
        PersonAlias.objects.create(person=existing, name="Ada Lovelace")
        ArchiveItemPerson.objects.create(archive_item=self.item, person=existing)
        resp = self.client.post(
            self._edit_url(self.second),
            data={
                "description": "Second caption",
                "people_present": "crowd",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "new_person_name": "  רחל כהן , ,Ada Lovelace, רחל כהן ",
            },
        )
        self.assertEqual(resp.status_code, 302)
        created = list(
            Person.objects.filter(name__in=["רחל כהן", "Ada Lovelace"])
            .exclude(pk=existing.pk)
            .order_by("id")
        )
        self.assertEqual(
            [person.name for person in created], ["רחל כהן", "Ada Lovelace"]
        )
        self.assertEqual(
            list(self.second.people.order_by("id").values_list("name", flat=True)),
            ["רחל כהן", "Ada Lovelace"],
        )
        for person in created:
            self.assertTrue(
                PhotoPerson.objects.filter(
                    photo_content=self.second, person=person
                ).exists()
            )
            self.assertFalse(
                ArchiveItemPerson.objects.filter(
                    archive_item=self.item, person=person
                ).exists()
            )
            self.assertEqual(person.aliases.count(), 0)
        self.assertEqual(self.second.people_present, "crowd")
        self.assertEqual(
            set(self.item.people.values_list("id", flat=True)),
            {existing.id},
        )
        self.assertEqual(self.first.people.count(), 0)

    def test_photo_new_person_name_validation_and_maxlength(self):
        resp = self.client.get(self._edit_url(self.first))
        html = resp.content.decode()
        match = re.search(r'<input[^>]*id="new_person_name"[^>]*>', html)
        self.assertIsNotNone(match)
        self.assertNotIn("maxlength", match.group(0))
        self.assertContains(
            resp,
            "ניתן להזין כמה שמות מופרדים בפסיקים. כל שם יוצר רשומת אדם חדשה ומקושרת לתמונה זו.",
        )

        commas_only = self.client.post(
            self._edit_url(self.first),
            data={
                "description": "First caption",
                "people_present": "maybe uncle",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "new_person_name": ", , ,",
            },
        )
        self.assertEqual(commas_only.status_code, 200)
        self.assertContains(commas_only, PERSON_NAMES_COMMAS_ONLY_ERROR)
        self.assertEqual(Person.objects.count(), 0)
        self.assertEqual(self.first.people_present, "maybe uncle")

        too_long = "y" * 256
        length_resp = self.client.post(
            self._edit_url(self.first),
            data={
                "description": "First caption",
                "people_present": "maybe uncle",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "new_person_name": f"{'a' * 200}, {too_long}",
            },
        )
        self.assertEqual(length_resp.status_code, 200)
        self.assertContains(length_resp, PERSON_NAME_TOO_LONG_ERROR)
        self.assertFalse(Person.objects.exists())

        ok_resp = self.client.post(
            self._edit_url(self.first),
            data={
                "description": "First caption",
                "people_present": "maybe uncle",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "new_person_name": f"{'a' * 200}, {'b' * 200}",
            },
        )
        self.assertEqual(ok_resp.status_code, 302)
        self.assertEqual(
            list(self.first.people.order_by("id").values_list("name", flat=True)),
            ["a" * 200, "b" * 200],
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=self.item).count(), 0
        )
        self.assertEqual(self.first.people_present, "maybe uncle")

    def test_cannot_edit_photo_from_another_item_via_this_item_url(self):
        other = _create_photo_item(title="Other")
        other_photo = _add_photo(other, position=1, filename="other.jpg")
        resp = self.client.get(
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.item.id, "photo_id": other_photo.id},
            )
        )
        self.assertEqual(resp.status_code, 404)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoReorderTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="reorder_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item()
        self.p1 = _add_photo(self.item, position=1, filename="one.jpg")
        self.p2 = _add_photo(self.item, position=2, filename="two.jpg")
        self.p3 = _add_photo(self.item, position=3, filename="three.jpg")
        self.client.force_login(self.staff)
        self.url = reverse(
            "archive-manage-photo-reorder", kwargs={"item_id": self.item.id}
        )

    def _positions(self) -> list[int]:
        return list(
            self.item.photo_contents.order_by("position").values_list("id", flat=True)
        )

    def test_reorder_persists_exact_requested_order_and_compacts_positions(self):
        resp = self.client.post(
            self.url,
            data={"photo_ids": [self.p3.id, self.p1.id, self.p2.id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._positions(), [self.p3.id, self.p1.id, self.p2.id])
        self.assertEqual(
            list(
                self.item.photo_contents.order_by("id").values_list(
                    "position", flat=True
                )
            ),
            [
                PhotoContent.objects.get(pk=self.p1.id).position,
                PhotoContent.objects.get(pk=self.p2.id).position,
                PhotoContent.objects.get(pk=self.p3.id).position,
            ],
        )
        self.assertEqual(
            set(self.item.photo_contents.values_list("position", flat=True)),
            {1, 2, 3},
        )

    def test_swap_does_not_violate_unique_constraint(self):
        reorder_photo_contents(self.item, [self.p2.id, self.p1.id, self.p3.id])
        self.assertEqual(self._positions(), [self.p2.id, self.p1.id, self.p3.id])

    def test_reorder_succeeds_when_a_position_is_in_the_old_fixed_temp_range(self):
        PhotoContent.objects.filter(pk=self.p2.pk).update(position=1_000_001)
        PhotoContent.objects.filter(pk=self.p3.pk).update(position=1_000_002)
        self.p2.refresh_from_db()
        self.p3.refresh_from_db()
        self.assertEqual(self.p2.position, 1_000_001)
        self.assertEqual(self.p3.position, 1_000_002)

        resp = self.client.post(
            self.url,
            data={"photo_ids": [self.p3.id, self.p1.id, self.p2.id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._positions(), [self.p3.id, self.p1.id, self.p2.id])
        self.assertEqual(
            list(
                self.item.photo_contents.order_by("position").values_list(
                    "position", flat=True
                )
            ),
            [1, 2, 3],
        )

    def test_duplicate_ids_are_rejected(self):
        resp = self.client.post(
            self.url,
            data={"photo_ids": [self.p1.id, self.p1.id, self.p2.id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._positions(), [self.p1.id, self.p2.id, self.p3.id])

    def test_missing_ids_are_rejected(self):
        resp = self.client.post(
            self.url,
            data={"photo_ids": [self.p3.id, self.p1.id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._positions(), [self.p1.id, self.p2.id, self.p3.id])

    def test_foreign_photo_id_is_rejected(self):
        other = _create_photo_item(title="Other album")
        foreign = _add_photo(other, position=1, filename="foreign.jpg")
        resp = self.client.post(
            self.url,
            data={"photo_ids": [self.p1.id, self.p2.id, foreign.id]},
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._positions(), [self.p1.id, self.p2.id, self.p3.id])
        self.assertEqual(foreign.archive_item_id, other.id)
        foreign.refresh_from_db()
        self.assertEqual(foreign.position, 1)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoDeleteOneTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="delete_one_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item()
        self.p1 = _add_photo(
            self.item,
            position=1,
            filename="one.jpg",
            thumbnail_file_key="photos/keep/thumb_400.jpg",
        )
        self.p2 = _add_photo(
            self.item,
            position=2,
            filename="two.jpg",
            thumbnail_file_key="photos/gone/thumb_400.jpg",
        )
        self.p3 = _add_photo(self.item, position=3, filename="three.jpg")
        self.person = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=self.p2, person=self.person)
        PhotoPerson.objects.create(photo_content=self.p1, person=self.person)
        self.client.force_login(self.staff)

    def _delete_url(self, photo: PhotoContent) -> str:
        return reverse(
            "archive-manage-photo-delete",
            kwargs={"item_id": self.item.id, "photo_id": photo.id},
        )

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_delete_one_of_multiple_removes_only_that_photo(
        self, mock_delete_s3_object
    ):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            resp = self.client.post(self._delete_url(self.p2))

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PhotoContent.objects.filter(pk=self.p2.id).exists())
        self.assertTrue(ArchiveItem.objects.filter(pk=self.item.id).exists())
        self.assertFalse(
            PhotoPerson.objects.filter(photo_content_id=self.p2.id).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(
                photo_content_id=self.p1.id, person=self.person
            ).exists()
        )
        remaining = list(
            self.item.photo_contents.order_by("position").values_list("id", "position")
        )
        self.assertEqual(remaining, [(self.p1.id, 1), (self.p3.id, 2)])
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(
            mock_delete_s3_object.call_args_list,
            [
                call("test-uploads-bucket", self.p2.original_file_key),
                call("test-uploads-bucket", "photos/gone/thumb_400.jpg"),
            ],
        )

    def test_delete_one_renumbers_unusually_high_noncontiguous_positions(self):
        PhotoContent.objects.filter(pk=self.p2.pk).update(position=2)
        PhotoContent.objects.filter(pk=self.p3.pk).update(position=1_000_001)
        self.p3.refresh_from_db()
        self.assertEqual(self.p3.position, 1_000_001)

        resp = self.client.post(self._delete_url(self.p1))

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(PhotoContent.objects.filter(pk=self.p1.id).exists())
        remaining = list(
            self.item.photo_contents.order_by("position").values_list("id", "position")
        )
        self.assertEqual(remaining, [(self.p2.id, 1), (self.p3.id, 2)])

    def test_deleting_last_remaining_photo_is_rejected(self):
        solo = _create_photo_item(title="Solo")
        only = _add_photo(solo, position=1, filename="only.jpg")
        resp = self.client.post(
            reverse(
                "archive-manage-photo-delete",
                kwargs={"item_id": solo.id, "photo_id": only.id},
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, LAST_PHOTO_DELETE_ERROR)
        self.assertTrue(PhotoContent.objects.filter(pk=only.id).exists())
        self.assertTrue(ArchiveItem.objects.filter(pk=solo.id).exists())

    @patch("documents.services.photo_s3_cleanup.delete_s3_object")
    def test_whole_item_delete_still_cleans_all_photos(self, mock_delete_s3_object):
        item_id = self.item.id
        keys = [
            (self.p1.original_file_key, self.p1.thumbnail_file_key),
            (self.p2.original_file_key, self.p2.thumbnail_file_key),
            (self.p3.original_file_key, self.p3.thumbnail_file_key),
        ]
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(
                reverse("archive-manage-delete", kwargs={"item_id": item_id})
            )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(ArchiveItem.objects.filter(pk=item_id).exists())
        self.assertFalse(PhotoContent.objects.filter(archive_item_id=item_id).exists())
        deleted_keys = [c.args[1] for c in mock_delete_s3_object.call_args_list]
        for original, thumb in keys:
            self.assertIn(original, deleted_keys)
            if thumb:
                self.assertIn(thumb, deleted_keys)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoMultiPublicCompatibilityTests(TestCase):
    def setUp(self):
        self.item = _create_photo_item(title="Public album")
        self.first = _add_photo(self.item, position=1, filename="first.jpg")
        self.second = _add_photo(self.item, position=2, filename="second.jpg")

    @patch("documents.views.create_presigned_get", return_value="https://img/first")
    def test_public_detail_still_uses_primary_photo(self, mock_presign):
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.item.id})
        )
        self.assertEqual(resp.status_code, 200)
        mock_presign.assert_called()
        self.assertEqual(
            mock_presign.call_args.kwargs["key"], self.first.original_file_key
        )
        self.assertNotIn("second.jpg", resp.content.decode())

    def test_browse_eligibility_still_uses_first_photo_upload_state(self):
        self.first.upload_status = PhotoContent.UploadStatus.PENDING
        self.first.save(update_fields=["upload_status", "updated_at"])
        resp = self.client.get(reverse("archive-list"))
        self.assertNotContains(resp, self.item.title)


class AdditionalPhotoPositionLockTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Photo position concurrency test requires PostgreSQL")
        self.item = _create_photo_item(title="Concurrent album")
        _add_photo(self.item, position=1, filename="one.jpg")

    def test_concurrent_adds_get_distinct_positions(self):
        barrier = threading.Barrier(2, timeout=10)
        results: list[int] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker() -> None:
            connections.close_all()
            try:
                barrier.wait()
                _item, photo, _url = create_additional_photo_upload_plan(
                    archive_item=self.item,
                    bucket="test-uploads-bucket",
                    original_name="extra.jpg",
                    mime_type="image/jpeg",
                )
                with lock:
                    results.append(photo.position)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            finally:
                connections.close_all()

        with patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://s3.example/put",
        ):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [2, 3])
        self.assertEqual(
            sorted(self.item.photo_contents.values_list("position", flat=True)),
            [1, 2, 3],
        )

    def test_duplicate_position_still_rejected_by_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _add_photo(self.item, position=1, filename="dup.jpg")
        self.assertEqual(self.item.photo_contents.count(), 1)
