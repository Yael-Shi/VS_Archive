"""Staff ArchiveItemPerson management on ArchiveItem create and edit (C1)."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.contrib.messages import get_messages
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_people import (
    ARCHIVE_ITEM_PERSON_IDS_FIELD,
    NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD,
    set_archive_item_people,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR
from documents.test_archive_date_payloads import merge_default_date_fields
from documents.test_archive_item import create_viewable_ocr_document
from documents.views import (
    ARCHIVE_ITEM_PEOPLE_HEADING,
    ARCHIVE_ITEM_UPDATED_MSG,
    PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING,
    PHOTO_ARCHIVE_ITEM_PEOPLE_HINT,
)

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"


def _create_photo_item(*, title: str) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    filename: str = "photo.jpg",
) -> PhotoContent:
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=f"photos/{item.pk}-{position}/original.jpg",
        original_filename=filename,
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        upload_error="",
    )
    return photo


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


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


def _option_ids_for_field(html: str, field_name: str) -> list[str]:
    match = re.search(
        rf'<select[^>]*name="{field_name}"[^>]*>(.*?)</select>',
        html,
        re.DOTALL,
    )
    assert match is not None, f"missing select {field_name}"
    return re.findall(r'<option value="(\d+)"', match.group(1))


def _alias_sql(captured_queries) -> list[str]:
    return [
        query["sql"]
        for query in captured_queries
        if "documents_personalias" in query["sql"].lower()
    ]


def _presign_url(*, bucket, key, expires_in=3600):
    return f"https://example.test/{key}"


def _edit_url(item: ArchiveItem) -> str:
    return EDIT_URL_TEMPLATE.format(item_id=item.id)


class ArchiveItemPersonStaffUiHarness:
    def _create_family_user(self, username="item_people_family"):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(family_group)
        return user

    def _create_manual(self, *, title: str) -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="Typed body.",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _create_ocr(self, *, title: str) -> ArchiveItem:
        doc = create_viewable_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        return doc.archive_item

    def _create_video(self, *, title: str) -> ArchiveItem:
        return create_video_archive_item(
            title=title,
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _create_photo(self, *, title: str, photo_count: int = 1) -> ArchiveItem:
        item = _create_photo_item(title=title)
        for position in range(1, photo_count + 1):
            _add_photo(item, position=position, filename=f"p{position}.jpg")
        return item

    def _payload_for(self, item: ArchiveItem, **overrides):
        if item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
            payload = {
                "title": item.title,
                "body": item.manual_text_content.body,
                "visibility": item.visibility,
                "metadata_status": item.metadata_status,
                "date_precision": item.date_precision,
                "author_name": item.author_name,
                "source_title": item.source_title,
                "public_note": item.public_note,
                "categories": "",
                "events": "",
                "tags": "",
            }
        elif item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
            payload = {
                "title": item.title,
                "visibility": item.visibility,
                "metadata_status": item.metadata_status,
                "date_precision": item.date_precision,
                "author_name": item.author_name,
                "source_title": item.source_title,
                "public_note": item.public_note,
                "donor": "",
                "collection": "",
                "original_location": "",
                "notes": "",
                "categories": "",
                "events": "",
                "discovery_tags": "",
            }
        elif item.item_type == ArchiveItem.ItemType.PHOTO:
            payload = {
                "title": item.title,
                "visibility": item.visibility,
                "metadata_status": item.metadata_status,
                "date_precision": item.date_precision,
                "public_note": item.public_note,
                "categories": "",
                "events": "",
                "tags": "",
            }
        elif item.item_type == ArchiveItem.ItemType.VIDEO:
            payload = {
                "title": item.title,
                "source_url": item.video_content.source_url,
                "visibility": item.visibility,
                "metadata_status": item.metadata_status,
                "date_precision": item.date_precision,
                "author_name": item.author_name,
                "source_title": item.source_title,
                "public_note": item.public_note,
                "start_seconds": "",
                "end_seconds": "",
                "categories": "",
                "events": "",
                "tags": "",
            }
        else:
            raise AssertionError(item.item_type)
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def _items_of_each_type(self) -> list[ArchiveItem]:
        return [
            self._create_manual(title="People manual"),
            self._create_ocr(title="People OCR"),
            self._create_video(title="People video"),
            self._create_photo(title="People photo"),
        ]


class ArchiveItemPersonBatchServiceTests(TestCase):
    def test_multiple_add_and_remove_refresh_index_once(self):
        item = create_manual_text_archive_item(
            title="Batch people",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        keep = Person.objects.create(name="KeepPersonToken")
        remove = Person.objects.create(name="RemovePersonToken")
        add = Person.objects.create(name="AddPersonToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=keep)
        ArchiveItemPerson.objects.create(archive_item=item, person=remove)
        _rebuild(item.pk)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            set_archive_item_people(
                archive_item=item,
                person_ids=[keep.pk, add.pk],
            )
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args, (item.pk,))
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {keep.pk, add.pk},
        )
        metadata = _index_for(item.pk).metadata_text
        self.assertIn("AddPersonToken", metadata)
        self.assertNotIn("RemovePersonToken", metadata)

    def test_unchanged_links_do_not_refresh_index(self):
        item = create_manual_text_archive_item(
            title="Unchanged people",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="SamePersonToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            set_archive_item_people(archive_item=item, person_ids=[person.pk])
        mocked.assert_not_called()

    def test_new_canonical_name_always_creates_distinct_person(self):
        item = create_manual_text_archive_item(
            title="Dup name item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        existing = Person.objects.create(name="יעקב כהן")
        links = set_archive_item_people(
            archive_item=item,
            person_ids=[existing.pk],
            new_person_name="  יעקב כהן  ",
        )
        created = Person.objects.exclude(pk=existing.pk).get(name="יעקב כהן")
        self.assertNotEqual(created.pk, existing.pk)
        self.assertEqual(Person.objects.filter(name="יעקב כהן").count(), 2)
        self.assertEqual({link.person_id for link in links}, {existing.pk, created.pk})
        self.assertEqual(PersonAlias.objects.count(), 0)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class ArchiveItemPersonStaffUiTests(ArchiveItemPersonStaffUiHarness, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="item_people_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_all_four_types_show_picker_and_linked_people(self):
        linked = Person.objects.create(name="יעקב כהן")
        other = Person.objects.create(name="רחל כהן")
        PersonAlias.objects.create(person=linked, name="Yaakov Cohen")
        PersonAlias.objects.create(person=linked, name="Jacob Cohen")
        for item in self._items_of_each_type():
            with self.subTest(item_type=item.item_type):
                ArchiveItemPerson.objects.create(archive_item=item, person=linked)
                resp = self.client.get(_edit_url(item))
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertContains(resp, 'name="archive_item_person_ids"')
                self.assertContains(resp, f'value="{linked.id}"')
                self.assertContains(resp, f'value="{other.id}"')
                self.assertEqual(
                    _option_text(html, linked.id),
                    "יעקב כהן (Jacob Cohen, Yaakov Cohen)",
                )
                self.assertEqual(_option_text(html, other.id), "רחל כהן")
                self.assertNotIn(str(linked.id), _option_text(html, linked.id))
                option_ids = _option_ids_for_field(html, "archive_item_person_ids")
                self.assertEqual(len(option_ids), len(set(option_ids)))
                self.assertNotIn("Jacob Cohen", option_ids)
                self.assertContains(resp, "אנשים קשורים לפריט זה")
                self.assertContains(
                    resp,
                    reverse(
                        "archive-manage-person-edit",
                        kwargs={"person_id": linked.id},
                    ),
                )
                if item.item_type == ArchiveItem.ItemType.PHOTO:
                    self.assertContains(resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING)
                    self.assertContains(resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HINT)
                    self.assertNotContains(resp, 'name="person_ids"')
                else:
                    self.assertContains(resp, ARCHIVE_ITEM_PEOPLE_HEADING)

    def test_create_pages_show_item_people_manager(self):
        person = Person.objects.create(name="CreatePickerPerson")
        PersonAlias.objects.create(person=person, name="CreatePickerAlias")
        pages = [
            (
                "legacy-manual",
                self.client.get("/archive/manage/new/manual-text/"),
                ArchiveItem.ItemType.MANUAL_TEXT,
            ),
            (
                "unified-manual",
                self.client.get("/archive/manage/new/", {"item_type": "manual_text"}),
                ArchiveItem.ItemType.MANUAL_TEXT,
            ),
            (
                "video",
                self.client.get("/archive/manage/new/", {"item_type": "video"}),
                ArchiveItem.ItemType.VIDEO,
            ),
            (
                "ocr",
                self.client.get("/archive/manage/new/", {"item_type": "ocr_document"}),
                ArchiveItem.ItemType.OCR_DOCUMENT,
            ),
            (
                "photo",
                self.client.get("/archive/manage/new/", {"item_type": "photo"}),
                ArchiveItem.ItemType.PHOTO,
            ),
        ]
        for label, resp, item_type in pages:
            with self.subTest(label=label):
                self.assertEqual(resp.status_code, 200)
                html = resp.content.decode()
                self.assertContains(resp, 'name="archive_item_person_ids"')
                self.assertContains(resp, 'name="new_archive_item_person_name"')
                self.assertContains(resp, f'value="{person.id}"')
                self.assertEqual(
                    _option_text(html, person.id),
                    "CreatePickerPerson (CreatePickerAlias)",
                )
                if item_type == ArchiveItem.ItemType.PHOTO:
                    self.assertContains(resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING)
                    self.assertContains(resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HINT)
                    self.assertNotContains(resp, 'name="person_ids"')
                else:
                    self.assertContains(resp, ARCHIVE_ITEM_PEOPLE_HEADING)

    def test_add_existing_person_on_all_types(self):
        person = Person.objects.create(name="AddExistingToken")
        for item in self._items_of_each_type():
            with self.subTest(item_type=item.item_type):
                resp = self.client.post(
                    _edit_url(item),
                    data=self._payload_for(
                        item,
                        **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
                    ),
                )
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(
                    ArchiveItemPerson.objects.filter(
                        archive_item=item, person=person
                    ).exists()
                )
                self.assertIn(
                    ARCHIVE_ITEM_UPDATED_MSG,
                    [m.message for m in get_messages(resp.wsgi_request)],
                )

    def test_remove_linked_person_on_all_types(self):
        keep = Person.objects.create(name="KeepLinkedToken")
        remove = Person.objects.create(name="RemoveLinkedToken")
        for item in self._items_of_each_type():
            with self.subTest(item_type=item.item_type):
                ArchiveItemPerson.objects.create(archive_item=item, person=keep)
                ArchiveItemPerson.objects.create(archive_item=item, person=remove)
                resp = self.client.post(
                    _edit_url(item),
                    data=self._payload_for(
                        item,
                        **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(keep.id)]},
                    ),
                )
                self.assertEqual(resp.status_code, 302)
                self.assertEqual(
                    set(item.people.values_list("id", flat=True)),
                    {keep.id},
                )
                self.assertTrue(Person.objects.filter(pk=remove.pk).exists())

    def test_create_new_person_and_link_it(self):
        item = self._create_manual(title="Create person item")
        resp = self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "  אדם חדש  "},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        created = Person.objects.get(name="אדם חדש")
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=created).exists()
        )
        self.assertEqual(PersonAlias.objects.filter(person=created).count(), 0)

    def test_duplicate_canonical_names_remain_distinct(self):
        item = self._create_manual(title="Dup identities")
        first = Person.objects.create(name="שם כפול")
        ArchiveItemPerson.objects.create(archive_item=item, person=first)
        resp = self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{
                    ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(first.id)],
                    NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "שם כפול",
                },
            ),
        )
        self.assertEqual(resp.status_code, 302)
        people = list(Person.objects.filter(name="שם כפול").order_by("id"))
        self.assertEqual(len(people), 2)
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {people[0].id, people[1].id},
        )

    def test_invalid_person_id_is_rejected_and_state_preserved(self):
        item = self._create_manual(title="Invalid id item")
        existing = Person.objects.create(name="ValidPerson")
        ArchiveItemPerson.objects.create(archive_item=item, person=existing)
        resp = self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{
                    ARCHIVE_ITEM_PERSON_IDS_FIELD: ["not-an-id"],
                    NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "ShouldStay",
                },
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_NOT_FOUND_ERROR)
        self.assertContains(resp, 'value="ShouldStay"')
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {existing.id},
        )
        self.assertFalse(Person.objects.filter(name="ShouldStay").exists())

        unknown = self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{ARCHIVE_ITEM_PERSON_IDS_FIELD: ["999999"]},
            ),
        )
        self.assertEqual(unknown.status_code, 200)
        self.assertContains(unknown, PERSON_NOT_FOUND_ERROR)
        self.assertEqual(
            set(item.people.values_list("id", flat=True)),
            {existing.id},
        )

    def test_permissions_match_existing_archive_manage_edit(self):
        item = self._create_manual(title="Perm item")
        person = Person.objects.create(name="PermPerson")
        payload = self._payload_for(
            item,
            **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
        )
        self.client.logout()
        anonymous = self.client.post(_edit_url(item), data=payload)
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/accounts/login/", anonymous["Location"])
        self.assertFalse(ArchiveItemPerson.objects.filter(archive_item=item).exists())

        self.client.force_login(self._create_family_user())
        family = self.client.post(_edit_url(item), data=payload)
        self.assertEqual(family.status_code, 403)
        self.assertFalse(ArchiveItemPerson.objects.filter(archive_item=item).exists())

        other = User.objects.create_user(
            username="item_people_user",
            password="test-pass",
            is_staff=False,
        )
        self.client.force_login(other)
        forbidden = self.client.post(_edit_url(item), data=payload)
        self.assertEqual(forbidden.status_code, 403)

        self.client.force_login(self.staff)
        allowed = self.client.post(_edit_url(item), data=payload)
        self.assertEqual(allowed.status_code, 302)
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_photo_video_success_message_renders_after_prg(self):
        photo = self._create_photo(title="PRG photo")
        video = self._create_video(title="PRG video")
        person = Person.objects.create(name="PrgPerson")
        for item in (photo, video):
            with self.subTest(item_type=item.item_type):
                resp = self.client.post(
                    _edit_url(item),
                    data=self._payload_for(
                        item,
                        **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
                    ),
                    follow=True,
                )
                self.assertContains(resp, ARCHIVE_ITEM_UPDATED_MSG)
                self.assertEqual(resp.resolver_match.url_name, "archive-manage-list")

    def test_search_index_refresh_on_add_and_remove(self):
        item = self._create_manual(title="Search people item")
        person = Person.objects.create(name="StaffSearchPersonToken")
        PersonAlias.objects.create(person=person, name="StaffSearchAliasToken")
        _rebuild(item.pk)
        self.assertNotIn(item.pk, _search_ids("StaffSearchPersonToken"))

        self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
            ),
        )
        self.assertIn(item.pk, _search_ids("StaffSearchPersonToken"))
        self.assertIn(item.pk, _search_ids("StaffSearchAliasToken"))

        self.client.post(
            _edit_url(item),
            data=self._payload_for(item),
        )
        self.assertNotIn(item.pk, _search_ids("StaffSearchPersonToken"))
        self.assertNotIn(item.pk, _search_ids("StaffSearchAliasToken"))
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())
        self.assertTrue(PersonAlias.objects.filter(person=person).exists())

    def test_removing_item_link_keeps_identity_when_photo_person_remains(self):
        item = self._create_photo(title="Keep photo identity")
        photo = item.photo_contents.get()
        person = Person.objects.create(name="StillInPhotoToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        _rebuild(item.pk)
        self.client.post(
            _edit_url(item),
            data=self._payload_for(item),
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertIn(item.pk, _search_ids("StillInPhotoToken"))

    def test_no_tag_or_document_tags_m2m_mutation(self):
        item_tag = Tag.objects.create(name="KeepItemTag")
        doc_tag = Tag.objects.create(name="KeepDocumentTag")
        item = self._create_ocr(title="Tagged OCR")
        item.tags.add(item_tag)
        document = item.ocr_document
        document.tags_m2m.add(doc_tag)
        person = Person.objects.create(name="TagSafePerson")
        self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                selected_tags=[str(item_tag.id)],
                **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
            ),
        )
        self.assertEqual(
            set(item.tags.values_list("id", flat=True)),
            {item_tag.id},
        )
        self.assertEqual(
            set(document.tags_m2m.values_list("id", flat=True)),
            {doc_tag.id},
        )

    def test_photo_item_person_does_not_create_photo_person(self):
        item = self._create_photo(title="Item only photo", photo_count=2)
        person = Person.objects.create(name="ItemLevelOnly")
        resp = self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)
        self.assertEqual(PhotoPerson.objects.count(), 0)
        self.assertEqual(item.photo_contents.count(), 2)

    def test_photo_person_does_not_create_archive_item_person(self):
        item = self._create_photo(title="Photo only")
        photo = item.photo_contents.get()
        person = Person.objects.create(name="AppearsOnly")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        resp = self.client.get(_edit_url(item))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        option = re.search(
            rf'<option value="{person.id}"([^>]*)>',
            html,
        )
        self.assertIsNotNone(option)
        self.assertNotIn("selected", option.group(1))
        self.assertFalse(ArchiveItemPerson.objects.filter(archive_item=item).exists())

    def test_same_person_may_exist_in_both_relations(self):
        item = self._create_photo(title="Both relations")
        photo = item.photo_contents.get()
        person = Person.objects.create(name="BothRelations")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        person_count = Person.objects.count()
        self.client.post(
            _edit_url(item),
            data=self._payload_for(
                item,
                **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(person.id)]},
            ),
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertEqual(Person.objects.count(), person_count)

    def test_photo_content_editor_stays_photo_person_only(self):
        item = self._create_photo(title="Per photo editor")
        photo = item.photo_contents.get()
        person = Person.objects.create(name="PhotoEditorPerson")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        url = reverse(
            "archive-manage-photo-edit",
            kwargs={"item_id": item.id, "photo_id": photo.id},
        )
        get_resp = self.client.get(url)
        self.assertContains(get_resp, "אנשים מזוהים בתמונה")
        self.assertContains(get_resp, 'name="person_ids"')
        self.assertNotContains(get_resp, 'name="archive_item_person_ids"')
        self.assertNotContains(get_resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING)

        post_resp = self.client.post(
            url,
            data={
                "description": "",
                "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
                "person_ids": [str(person.id)],
            },
        )
        self.assertEqual(post_resp.status_code, 302)
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_picker_loading_does_not_n_plus_one_on_aliases(self):
        item = self._create_manual(title="Picker queries")
        people = [Person.objects.create(name=f"Picker {index}") for index in range(4)]
        for person in people:
            PersonAlias.objects.create(person=person, name=f"{person.name} alias")
        self.client.get(_edit_url(item))

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(_edit_url(item))
        self.assertEqual(few_resp.status_code, 200)

        extra = [Person.objects.create(name=f"Extra {index}") for index in range(4)]
        for person in [*people, *extra]:
            PersonAlias.objects.create(person=person, name=f"{person.name} alias 2")
            PersonAlias.objects.create(person=person, name=f"{person.name} alias 3")

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(_edit_url(item))
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(len(_alias_sql(few_ctx.captured_queries)), 1)
        self.assertEqual(len(_alias_sql(many_ctx.captured_queries)), 1)

    def test_one_submit_add_and_remove_updates_search_without_per_link_rebuilds(self):
        item = self._create_manual(title="One submit people")
        keep = Person.objects.create(name="KeepSearchToken")
        remove = Person.objects.create(name="RemoveSearchToken")
        add = Person.objects.create(name="AddSearchToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=keep)
        ArchiveItemPerson.objects.create(archive_item=item, person=remove)
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            resp = self.client.post(
                _edit_url(item),
                data=self._payload_for(
                    item,
                    **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [str(keep.id), str(add.id)]},
                ),
            )
        self.assertEqual(resp.status_code, 302)
        self.assertGreaterEqual(wrapped.call_count, 1)
        self.assertLessEqual(wrapped.call_count, 3)
        self.assertIn(item.pk, _search_ids("AddSearchToken"))
        self.assertIn(item.pk, _search_ids("KeepSearchToken"))
        self.assertNotIn(item.pk, _search_ids("RemoveSearchToken"))

    @patch("documents.views.create_presigned_get", side_effect=_presign_url)
    def test_public_pages_do_not_show_archive_item_person_ui(self, _mock_presign):
        person = Person.objects.create(name="PublicHiddenPersonToken")
        manual = self._create_manual(title="Public manual")
        photo = self._create_photo(title="Public photo")
        ArchiveItemPerson.objects.create(archive_item=manual, person=person)
        ArchiveItemPerson.objects.create(archive_item=photo, person=person)
        self.client.logout()

        list_resp = self.client.get(reverse("archive-list"))
        manual_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": manual.id})
        )
        photo_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": photo.id})
        )
        self.assertEqual(manual_detail.status_code, 200)
        self.assertEqual(photo_detail.status_code, 200)
        for resp in (list_resp, manual_detail, photo_detail):
            self.assertNotContains(resp, 'name="archive_item_person_ids"')
            self.assertNotContains(resp, PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING)
            self.assertNotContains(resp, "אנשים קשורים לפריט זה")
        self.assertNotContains(manual_detail, "PublicHiddenPersonToken")
        self.assertNotContains(photo_detail, "PublicHiddenPersonToken")
        self.assertNotContains(photo_detail, "אנשים מזוהים:")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class ArchiveItemPersonStaffCreateTests(ArchiveItemPersonStaffUiHarness, TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="item_people_create_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        ocr_presign = patch(
            "documents.views.create_presigned_put",
            return_value="https://example.test/put",
        )
        photo_presign = patch(
            "documents.services.photo_upload.create_presigned_put",
            return_value="https://example.test/photo-put",
        )
        ocr_presign.start()
        photo_presign.start()
        self.addCleanup(ocr_presign.stop)
        self.addCleanup(photo_presign.stop)

    def _create_kinds(self) -> tuple[str, ...]:
        return (
            ArchiveItem.ItemType.MANUAL_TEXT,
            ArchiveItem.ItemType.OCR_DOCUMENT,
            ArchiveItem.ItemType.VIDEO,
            ArchiveItem.ItemType.PHOTO,
        )

    def _manual_payload(self, **overrides):
        payload = {
            "item_type": "manual_text",
            "title": "Create people manual",
            "body": "Typed body.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def _video_payload(self, **overrides):
        payload = {
            "item_type": "video",
            "title": "Create people video",
            "source_url": YOUTUBE_URL,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "start_seconds": "",
            "end_seconds": "",
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def _ocr_payload(self, **overrides):
        payload = {
            "title": "Create people OCR",
            "doc_type": "IMAGE",
            "text_input_type": "HANDWRITTEN",
            "original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
            "visibility": ArchiveItem.Visibility.PUBLIC,
        }
        payload.update(overrides)
        return payload

    def _photo_payload(self, **overrides):
        payload = {
            "title": "Create people photo",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "original_name": "photo.jpg",
            "mime_type": "image/jpeg",
        }
        payload.update(overrides)
        return payload

    def _post_create(self, item_type: str, **overrides):
        title = overrides.get("title") or f"Create people {item_type}"
        people = {
            key: overrides[key]
            for key in (
                ARCHIVE_ITEM_PERSON_IDS_FIELD,
                NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD,
            )
            if key in overrides
        }
        extra = {
            key: value
            for key, value in overrides.items()
            if key
            not in {
                ARCHIVE_ITEM_PERSON_IDS_FIELD,
                NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD,
                "title",
            }
        }
        if item_type == ArchiveItem.ItemType.MANUAL_TEXT:
            resp = self.client.post(
                "/archive/manage/new/",
                data=self._manual_payload(title=title, **people, **extra),
            )
            item = ArchiveItem.objects.filter(title=title).first()
            return item, resp
        if item_type == ArchiveItem.ItemType.VIDEO:
            resp = self.client.post(
                "/archive/manage/new/",
                data=self._video_payload(title=title, **people, **extra),
            )
            item = ArchiveItem.objects.filter(title=title).first()
            return item, resp
        if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
            resp = self.client.post(
                "/api/uploads/create/",
                data=json.dumps(self._ocr_payload(title=title, **people, **extra)),
                content_type="application/json",
            )
            if resp.status_code != 201:
                return None, resp
            doc = Document.objects.get(id=resp.json()["document_id"])
            return doc.archive_item, resp
        if item_type == ArchiveItem.ItemType.PHOTO:
            resp = self.client.post(
                "/api/photo-uploads/create/",
                data=json.dumps(self._photo_payload(title=title, **people, **extra)),
                content_type="application/json",
            )
            if resp.status_code != 201:
                return None, resp
            return ArchiveItem.objects.get(id=resp.json()["archive_item_id"]), resp
        raise AssertionError(item_type)

    def _assert_created(self, item: ArchiveItem | None, resp, item_type: str):
        if item_type in (
            ArchiveItem.ItemType.MANUAL_TEXT,
            ArchiveItem.ItemType.VIDEO,
        ):
            self.assertEqual(resp.status_code, 302)
        else:
            self.assertEqual(resp.status_code, 201)
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.item_type, item_type)

    def test_create_with_zero_persons_on_all_types(self):
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                item, resp = self._post_create(item_type)
                self._assert_created(item, resp, item_type)
                self.assertEqual(
                    ArchiveItemPerson.objects.filter(archive_item=item).count(),
                    0,
                )
                self.assertEqual(PhotoPerson.objects.count(), 0)

    def test_create_with_existing_person_on_all_types(self):
        person = Person.objects.create(name="CreateExistingToken")
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                item, resp = self._post_create(
                    item_type,
                    **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [person.id]},
                )
                self._assert_created(item, resp, item_type)
                self.assertEqual(
                    set(item.people.values_list("id", flat=True)),
                    {person.id},
                )
                self.assertEqual(PersonAlias.objects.filter(person=person).count(), 0)

    def test_create_with_multiple_existing_persons_on_all_types(self):
        first = Person.objects.create(name="CreateMultiOne")
        second = Person.objects.create(name="CreateMultiTwo")
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                item, resp = self._post_create(
                    item_type,
                    **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [first.id, second.id]},
                )
                self._assert_created(item, resp, item_type)
                self.assertEqual(
                    set(item.people.values_list("id", flat=True)),
                    {first.id, second.id},
                )

    def test_create_new_person_and_link_on_all_types(self):
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                new_name = f"אדם חדש {item_type}"
                item, resp = self._post_create(
                    item_type,
                    **{NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: f"  {new_name}  "},
                )
                self._assert_created(item, resp, item_type)
                created = Person.objects.get(name=new_name)
                self.assertTrue(
                    ArchiveItemPerson.objects.filter(
                        archive_item=item, person=created
                    ).exists()
                )
                self.assertEqual(PersonAlias.objects.filter(person=created).count(), 0)

    def test_duplicate_canonical_names_remain_distinct_on_create(self):
        existing = Person.objects.create(name="שם כפול יצירה")
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                before = Person.objects.filter(name="שם כפול יצירה").count()
                item, resp = self._post_create(
                    item_type,
                    **{
                        ARCHIVE_ITEM_PERSON_IDS_FIELD: [existing.id],
                        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "שם כפול יצירה",
                    },
                )
                self._assert_created(item, resp, item_type)
                people = list(
                    Person.objects.filter(name="שם כפול יצירה").order_by("id")
                )
                self.assertEqual(len(people), before + 1)
                self.assertEqual(item.people.count(), 2)
                self.assertIn(existing.id, item.people.values_list("id", flat=True))
                created = [person for person in people if person.id != existing.id][-1]
                self.assertTrue(
                    ArchiveItemPerson.objects.filter(
                        archive_item=item, person=created
                    ).exists()
                )
                self.assertEqual(PersonAlias.objects.filter(person=created).count(), 0)

    def test_validation_error_preserves_create_form_state(self):
        existing = Person.objects.create(name="ValidCreatePerson")
        html_cases = [
            (ArchiveItem.ItemType.MANUAL_TEXT, self._manual_payload),
            (ArchiveItem.ItemType.VIDEO, self._video_payload),
        ]
        for item_type, payload_fn in html_cases:
            with self.subTest(item_type=item_type):
                resp = self.client.post(
                    "/archive/manage/new/",
                    data=payload_fn(
                        **{
                            ARCHIVE_ITEM_PERSON_IDS_FIELD: ["not-an-id"],
                            NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "ShouldStay",
                        }
                    ),
                )
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, PERSON_NOT_FOUND_ERROR)
                self.assertContains(resp, 'value="ShouldStay"')
                self.assertFalse(Person.objects.filter(name="ShouldStay").exists())
                self.assertFalse(
                    ArchiveItem.objects.filter(
                        item_type=item_type,
                        title=payload_fn()["title"],
                    ).exists()
                )

                unknown = self.client.post(
                    "/archive/manage/new/",
                    data=payload_fn(
                        **{
                            ARCHIVE_ITEM_PERSON_IDS_FIELD: [
                                str(existing.id),
                                "999999",
                            ],
                            NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "KeepNewName",
                        }
                    ),
                )
                self.assertEqual(unknown.status_code, 200)
                self.assertContains(unknown, PERSON_NOT_FOUND_ERROR)
                self.assertContains(unknown, 'value="KeepNewName"')
                html = unknown.content.decode()
                option = re.search(
                    rf'<option value="{existing.id}"([^>]*)>',
                    html,
                )
                self.assertIsNotNone(option)
                self.assertIn("selected", option.group(1))
                self.assertFalse(Person.objects.filter(name="KeepNewName").exists())

        json_cases = [
            (
                ArchiveItem.ItemType.OCR_DOCUMENT,
                "/api/uploads/create/",
                self._ocr_payload,
            ),
            (
                ArchiveItem.ItemType.PHOTO,
                "/api/photo-uploads/create/",
                self._photo_payload,
            ),
        ]
        for item_type, url, payload_fn in json_cases:
            with self.subTest(item_type=item_type):
                resp = self.client.post(
                    url,
                    data=json.dumps(
                        payload_fn(
                            **{
                                ARCHIVE_ITEM_PERSON_IDS_FIELD: [existing.id, 999999],
                                NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "JsonShouldStay",
                            }
                        )
                    ),
                    content_type="application/json",
                )
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(Person.objects.filter(name="JsonShouldStay").exists())
                self.assertFalse(
                    ArchiveItem.objects.filter(
                        item_type=item_type,
                        title=payload_fn()["title"],
                    ).exists()
                )

    def test_photo_create_writes_item_person_not_photo_person(self):
        person = Person.objects.create(name="PhotoCreateItemOnly")
        item, resp = self._post_create(
            ArchiveItem.ItemType.PHOTO,
            **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [person.id]},
        )
        self._assert_created(item, resp, ArchiveItem.ItemType.PHOTO)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)
        self.assertEqual(PhotoPerson.objects.count(), 0)
        self.assertEqual(item.photo_contents.count(), 1)

    def test_create_does_not_mutate_tags(self):
        item_tag = Tag.objects.create(name="KeepCreateItemTag")
        doc_tag = Tag.objects.create(name="KeepCreateDocumentTag")
        person = Person.objects.create(name="CreateTagSafePerson")
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                extra = {}
                if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
                    extra["selected_tags"] = [item_tag.id]
                elif item_type == ArchiveItem.ItemType.PHOTO:
                    extra["selected_tags"] = [item_tag.id]
                else:
                    extra["selected_tags"] = [str(item_tag.id)]
                item, resp = self._post_create(
                    item_type,
                    **{
                        ARCHIVE_ITEM_PERSON_IDS_FIELD: [person.id],
                        **extra,
                    },
                )
                self._assert_created(item, resp, item_type)
                self.assertEqual(
                    set(item.tags.values_list("id", flat=True)),
                    {item_tag.id},
                )
                self.assertFalse(Tag.objects.filter(name=person.name).exists())
                if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
                    self.assertEqual(
                        set(item.ocr_document.tags_m2m.values_list("id", flat=True)),
                        set(),
                    )
                    self.assertTrue(Tag.objects.filter(pk=doc_tag.pk).exists())

    def test_created_item_is_searchable_by_linked_person(self):
        person = Person.objects.create(name="CreateSearchPersonToken")
        PersonAlias.objects.create(person=person, name="CreateSearchAliasToken")
        for item_type in self._create_kinds():
            with self.subTest(item_type=item_type):
                item, resp = self._post_create(
                    item_type,
                    **{ARCHIVE_ITEM_PERSON_IDS_FIELD: [person.id]},
                )
                self._assert_created(item, resp, item_type)
                self.assertIn(item.pk, _search_ids("CreateSearchPersonToken"))
                self.assertIn(item.pk, _search_ids("CreateSearchAliasToken"))
