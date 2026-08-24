"""C2b: public ArchiveItemPerson suggestion form and staff backlog/review."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, Group, User
from django.contrib.messages import get_messages
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    ArchiveMetadataSuggestion,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_people import (
    create_archive_item_person,
    delete_archive_item_person,
)
from documents.services.archive_item_person_suggestion_review import (
    ALREADY_REVIEWED_ERROR,
)
from documents.services.archive_item_person_suggestions import (
    ADD_PERSON_IDS_FIELD,
    CONTRADICTORY_PERSON_ACTIONS_ERROR,
    DUPLICATE_PENDING_SUGGESTION_ERROR,
    PERSON_ALREADY_LINKED_ERROR,
    PERSON_NOT_LINKED_ERROR,
    REMOVE_PERSON_IDS_FIELD,
    authorized_person_universe_ids_for_user,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.services.archive_metadata_suggestions import NAME_REQUIRED_ERROR
from documents.services.photo_content_management import (
    PERSON_NOT_FOUND_ERROR,
    person_staff_picker_label,
)
from documents.test_archive_item import create_viewable_ocr_document
from documents.test_restricted_visibility_access import _grant_restricted_permission
from documents.views import (
    PERSON_SUGGESTION_ADD_SUCCESS_MSG,
    PERSON_SUGGESTION_REMOVE_SUCCESS_MSG,
    PERSON_SUGGESTION_STALE_ADD_MSG,
    PERSON_SUGGESTION_STALE_REMOVE_MSG,
)
from public.services.registration import HONEYPOT_FIELD_NAME

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

PHOTO_ITEM_LEVEL_HINT = "קשר כללי לפריט הארכיון, לא הופעה בתמונה."
PHOTO_IDENTIFIED_PEOPLE_HINT = "אנשים שמופיעים בתמונה מוצגים בנפרד כ״אנשים מזוהים״."


def _create_photo_item(
    *, title: str, visibility: str
) -> tuple[ArchiveItem, PhotoContent]:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=1,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="scan.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item, photo


@override_settings(UPLOADS_BUCKET_NAME="")
class ArchiveItemPersonSuggestionUiHarness(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.staff = User.objects.create_user(
            username="person_sugg_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="person_sugg_viewer",
            password="test-pass",
        )
        self.family = User.objects.create_user(
            username="person_sugg_family",
            password="test-pass",
        )
        self.family.groups.add(Group.objects.get(name=ARCHIVE_FAMILY_GROUP_NAME))

    def _form_url(self, item_id: int) -> str:
        return reverse("archive-metadata-suggestion-new", kwargs={"item_id": item_id})

    def _thanks_url(self, item_id: int) -> str:
        return reverse(
            "archive-metadata-suggestion-thanks", kwargs={"item_id": item_id}
        )

    def _backlog_url(self) -> str:
        return reverse("archive-item-person-suggestion-backlog")

    def _approve_url(self, suggestion_id: int) -> str:
        return reverse(
            "archive-item-person-suggestion-approve",
            kwargs={"suggestion_id": suggestion_id},
        )

    def _reject_url(self, suggestion_id: int) -> str:
        return reverse(
            "archive-item-person-suggestion-reject",
            kwargs={"suggestion_id": suggestion_id},
        )

    def _manual(self, *, title: str = "Public manual", visibility=None) -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="גוף",
            visibility=visibility or ArchiveItem.Visibility.PUBLIC,
        )

    def _valid_post_data(self, **overrides) -> dict:
        data = {
            "submitter_name": "מציע/ה",
            "submitter_email": "suggester@example.com",
            "submitter_note": "",
            "suggested_categories": "",
            "suggested_events": "",
            "suggested_tags": "",
            HONEYPOT_FIELD_NAME: "",
        }
        data.update(overrides)
        return data

    def _messages(self, response) -> list[str]:
        return [str(message) for message in get_messages(response.wsgi_request)]


class ArchiveItemPersonSuggestionPublicFormTests(ArchiveItemPersonSuggestionUiHarness):
    def _item_of_type(self, item_type: str) -> ArchiveItem:
        if item_type == ArchiveItem.ItemType.MANUAL_TEXT:
            return self._manual(title="Form manual")
        if item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
            doc = create_viewable_ocr_document(
                title="Form OCR",
                doc_type=Document.DocType.IMAGE,
                text_input_type=Document.TextInputType.HANDWRITTEN,
                visibility=ArchiveItem.Visibility.PUBLIC,
            )
            return doc.archive_item
        if item_type == ArchiveItem.ItemType.VIDEO:
            return create_video_archive_item(
                title="Form video",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PUBLIC,
            )
        item, _photo = _create_photo_item(
            title="Form photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        return item

    def test_people_section_on_all_four_item_types(self):
        for item_type in (
            ArchiveItem.ItemType.MANUAL_TEXT,
            ArchiveItem.ItemType.OCR_DOCUMENT,
            ArchiveItem.ItemType.VIDEO,
            ArchiveItem.ItemType.PHOTO,
        ):
            with self.subTest(item_type=item_type):
                item = self._item_of_type(item_type)
                resp = self.client.get(self._form_url(item.id))
                self.assertEqual(resp.status_code, 200)
                self.assertContains(resp, "אנשים קשורים לפריט")
                self.assertContains(resp, "אנשים הקשורים כעת לפריט")
                self.assertContains(resp, "הצעת הוספת אדם קיים")
                self.assertContains(resp, "ההצעה תיבדק לפני שינוי באתר.")
                if item_type == ArchiveItem.ItemType.PHOTO:
                    self.assertContains(resp, PHOTO_ITEM_LEVEL_HINT)
                    self.assertContains(resp, PHOTO_IDENTIFIED_PEOPLE_HINT)
                else:
                    self.assertNotContains(resp, PHOTO_ITEM_LEVEL_HINT)
                    self.assertNotContains(resp, PHOTO_IDENTIFIED_PEOPLE_HINT)

    def test_current_people_and_aliases_displayed_for_remove(self):
        item = self._manual()
        person = Person.objects.create(name="Ada")
        PersonAlias.objects.create(person=person, name="Ada Lovelace")
        create_archive_item_person(archive_item=item, person=person)

        resp = self.client.get(self._form_url(item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, person_staff_picker_label(person))
        self.assertContains(resp, "הצע הסרת שיוך")
        self.assertContains(resp, "הסרת השיוך לפריט זה אינה מוחקת את רשומת האדם.")
        self.assertContains(resp, f'value="{person.id}"')
        html = resp.content.decode()
        add_block = html.split('id="metadata-suggestion-add-person-ids"', 1)[1]
        self.assertNotIn(f'value="{person.id}"', add_block)

    def test_authorized_add_picker_excludes_inaccessible_and_photoperson_only(self):
        public_item = self._manual(title="Public picker item")
        other_public = self._manual(title="Other public")
        private_item = self._manual(
            title="Private picker item",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted_item = self._manual(
            title="Restricted picker item",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        photo_item, photo = _create_photo_item(
            title="Photo picker item",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        visible_person = Person.objects.create(name="VisiblePickerPerson")
        PersonAlias.objects.create(person=visible_person, name="VisibleAlias")
        private_person = Person.objects.create(name="PrivateOnlyPerson")
        restricted_person = Person.objects.create(name="RestrictedOnlyPerson")
        photo_only = Person.objects.create(name="PhotoPersonOnlySecret")
        create_archive_item_person(archive_item=other_public, person=visible_person)
        create_archive_item_person(archive_item=private_item, person=private_person)
        create_archive_item_person(
            archive_item=restricted_item, person=restricted_person
        )
        PhotoPerson.objects.create(photo_content=photo, person=photo_only)

        resp = self.client.get(self._form_url(public_item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, person_staff_picker_label(visible_person))
        self.assertNotContains(resp, "PrivateOnlyPerson")
        self.assertNotContains(resp, "RestrictedOnlyPerson")
        self.assertNotContains(resp, "PhotoPersonOnlySecret")
        self.assertNotContains(resp, f'value="{private_person.id}"')
        self.assertNotContains(resp, f'value="{restricted_person.id}"')
        self.assertNotContains(resp, f'value="{photo_only.id}"')

        html = resp.content.decode()
        self.assertNotIn("אנשים מזוהים", html.split("אנשים קשורים לפריט", 1)[0])

    def test_photo_current_people_are_item_level_only(self):
        item, photo = _create_photo_item(
            title="Photo current people",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item_person = Person.objects.create(name="ItemLevelPerson")
        appearance = Person.objects.create(name="AppearanceOnlyPerson")
        create_archive_item_person(archive_item=item, person=item_person)
        PhotoPerson.objects.create(photo_content=photo, person=appearance)

        resp = self.client.get(self._form_url(item.id))
        self.assertContains(resp, "ItemLevelPerson")
        self.assertContains(resp, PHOTO_ITEM_LEVEL_HINT)
        self.assertNotContains(resp, "AppearanceOnlyPerson")


class ArchiveItemPersonSuggestionPublicSubmitTests(
    ArchiveItemPersonSuggestionUiHarness
):
    def test_add_creates_pending_suggestion_only(self):
        item = self._manual()
        other = self._manual(title="Universe item")
        person = Person.objects.create(name="AddPerson")
        create_archive_item_person(archive_item=other, person=person)
        tag = Tag.objects.create(name="untouched-tag")
        item.tags.add(tag)

        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(item.id))

        suggestion = ArchiveItemPersonSuggestion.objects.get()
        self.assertEqual(suggestion.archive_item_id, item.id)
        self.assertEqual(suggestion.person_id, person.id)
        self.assertEqual(suggestion.action, ArchiveItemPersonSuggestion.Action.ADD)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertIsNone(suggestion.submitter_user)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 0)
        self.assertEqual(list(item.tags.values_list("name", flat=True)), [tag.name])

    def test_remove_creates_pending_suggestion_only(self):
        item = self._manual()
        person = Person.objects.create(name="RemovePerson")
        create_archive_item_person(archive_item=item, person=person)

        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{REMOVE_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 302)
        suggestion = ArchiveItemPersonSuggestion.objects.get()
        self.assertEqual(suggestion.action, ArchiveItemPersonSuggestion.Action.REMOVE)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)

    def test_people_only_submit_is_valid(self):
        item = self._manual()
        other = self._manual(title="Other")
        person = Person.objects.create(name="PeopleOnly")
        create_archive_item_person(archive_item=other, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)

    def test_taxonomy_only_regression(self):
        item = self._manual()
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="קטגוריה חדשה",
                suggested_tags="תגית חדשה",
                submitter_note="הערה",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 1)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_mixed_person_validation_failure_rolls_back_taxonomy_and_partial_people(
        self,
    ):
        item = self._manual()
        other = self._manual(title="Universe")
        valid_add = Person.objects.create(name="ValidAddFirst")
        already_linked = Person.objects.create(name="AlreadyLinkedSecond")
        create_archive_item_person(archive_item=other, person=valid_add)
        create_archive_item_person(archive_item=item, person=already_linked)

        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="לא צריך להישמר",
                **{ADD_PERSON_IDS_FIELD: [valid_add.id, already_linked.id]},
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_ALREADY_LINKED_ERROR)
        self.assertContains(resp, "לא צריך להישמר")
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)

    def test_mixed_taxonomy_and_multiple_person_actions(self):
        item = self._manual()
        other = self._manual(title="Universe")
        add_a = Person.objects.create(name="AddA")
        add_b = Person.objects.create(name="AddB")
        remove = Person.objects.create(name="RemoveMixed")
        create_archive_item_person(archive_item=other, person=add_a)
        create_archive_item_person(archive_item=other, person=add_b)
        create_archive_item_person(archive_item=item, person=remove)

        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="קטגוריה",
                submitter_note="הערה",
                **{
                    ADD_PERSON_IDS_FIELD: [add_a.id, add_b.id],
                    REMOVE_PERSON_IDS_FIELD: [remove.id],
                },
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 1)
        rows = list(ArchiveItemPersonSuggestion.objects.order_by("person_id", "action"))
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            {(row.person_id, row.action) for row in rows},
            {
                (add_a.id, ArchiveItemPersonSuggestion.Action.ADD),
                (add_b.id, ArchiveItemPersonSuggestion.Action.ADD),
                (remove.id, ArchiveItemPersonSuggestion.Action.REMOVE),
            },
        )
        for row in rows:
            self.assertEqual(row.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)

    def test_duplicate_pending_rejected_and_preserves_state(self):
        item = self._manual()
        other = self._manual(title="Universe")
        person = Person.objects.create(name="DupPerson")
        create_archive_item_person(archive_item=other, person=person)
        self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
        )
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_tags="שמור",
                **{ADD_PERSON_IDS_FIELD: [person.id]},
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, DUPLICATE_PENDING_SUGGESTION_ERROR)
        self.assertContains(resp, "שמור")
        self.assertContains(resp, f'value="{person.id}"')
        self.assertContains(resp, "selected")
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)

    def test_add_already_linked_rejected(self):
        item = self._manual()
        person = Person.objects.create(name="AlreadyLinked")
        create_archive_item_person(archive_item=item, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_ALREADY_LINKED_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_remove_not_linked_rejected(self):
        item = self._manual()
        other = self._manual(title="Universe")
        person = Person.objects.create(name="NotLinked")
        create_archive_item_person(archive_item=other, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{REMOVE_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PERSON_NOT_LINKED_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_contradictory_add_and_remove_rejected(self):
        item = self._manual()
        person = Person.objects.create(name="BothActions")
        create_archive_item_person(archive_item=item, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="לא צריך להישמר",
                **{
                    ADD_PERSON_IDS_FIELD: [person.id],
                    REMOVE_PERSON_IDS_FIELD: [person.id],
                },
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CONTRADICTORY_PERSON_ACTIONS_ERROR)
        self.assertContains(resp, "לא צריך להישמר")
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)

    def test_invalid_and_out_of_universe_id_generic_error(self):
        item = self._manual()
        restricted = self._manual(
            title="Restricted secret",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        secret = Person.objects.create(name="SecretOutOfUniverse")
        create_archive_item_person(archive_item=restricted, person=secret)

        invalid = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: ["abc"]}),
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, PERSON_NOT_FOUND_ERROR)
        self.assertNotContains(invalid, "SecretOutOfUniverse")

        missing = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [secret.id]}),
        )
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, PERSON_NOT_FOUND_ERROR)
        self.assertNotContains(missing, "SecretOutOfUniverse")
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_name_required_preserves_person_state(self):
        item = self._manual()
        other = self._manual(title="Universe")
        person = Person.objects.create(name="NeedsName")
        create_archive_item_person(archive_item=other, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                submitter_name="",
                **{ADD_PERSON_IDS_FIELD: [person.id]},
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, NAME_REQUIRED_ERROR)
        self.assertContains(resp, f'value="{person.id}"')
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_anonymous_public_and_family_private_and_restricted_404(self):
        public_item = self._manual()
        other = self._manual(title="Universe")
        person = Person.objects.create(name="AnonPerson")
        create_archive_item_person(archive_item=other, person=person)
        resp = self.client.post(
            self._form_url(public_item.id),
            self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertIsNone(ArchiveItemPersonSuggestion.objects.get().submitter_user)

        private_item = self._manual(
            title="Private family item",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        create_archive_item_person(archive_item=private_item, person=person)
        self.assertEqual(
            self.client.get(self._form_url(private_item.id)).status_code, 404
        )

        self.client.force_login(self.family)
        family_resp = self.client.post(
            self._form_url(private_item.id),
            self._valid_post_data(**{REMOVE_PERSON_IDS_FIELD: [person.id]}),
        )
        self.assertEqual(family_resp.status_code, 302)
        family_row = ArchiveItemPersonSuggestion.objects.get(archive_item=private_item)
        self.assertEqual(family_row.submitter_user, self.family)

        restricted = self._manual(
            title="Restricted form",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.assertEqual(
            self.client.get(self._form_url(restricted.id)).status_code, 404
        )
        self.assertEqual(
            self.client.post(
                self._form_url(restricted.id),
                self._valid_post_data(**{ADD_PERSON_IDS_FIELD: [person.id]}),
            ).status_code,
            404,
        )

    def test_honeypot_creates_nothing(self):
        item = self._manual()
        other = self._manual(title="Universe")
        person = Person.objects.create(name="HoneypotPerson")
        create_archive_item_person(archive_item=other, person=person)
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="קטגוריה",
                **{
                    HONEYPOT_FIELD_NAME: "bot corp",
                    ADD_PERSON_IDS_FIELD: [person.id],
                },
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(item.id))
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_submit_does_not_mutate_photoperson_or_tags(self):
        item, photo = _create_photo_item(
            title="Photo submit isolation",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        linked = Person.objects.create(name="LinkedForRemove")
        appearance = Person.objects.create(name="PhotoAppearance")
        add_person = Person.objects.create(name="AddOnPhotoItem")
        other = self._manual(title="Universe")
        create_archive_item_person(archive_item=item, person=linked)
        create_archive_item_person(archive_item=other, person=add_person)
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        tag = Tag.objects.create(name="photo-item-tag")
        item.tags.add(tag)

        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                **{
                    ADD_PERSON_IDS_FIELD: [add_person.id],
                    REMOVE_PERSON_IDS_FIELD: [linked.id],
                },
            ),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=appearance).exists()
        )
        self.assertEqual(list(item.tags.values_list("name", flat=True)), [tag.name])


class ArchiveItemPersonSuggestionStaffBacklogTests(
    ArchiveItemPersonSuggestionUiHarness
):
    def test_staff_sees_authorized_rows_pending_first_with_labels(self):
        public_item = self._manual(title="Public person suggestion item")
        restricted_item = self._manual(
            title="Restricted person suggestion item",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        add_person = Person.objects.create(name="BacklogAda")
        PersonAlias.objects.create(person=add_person, name="BacklogAlias")
        remove_person = Person.objects.create(name="BacklogRemove")
        hidden_person = Person.objects.create(name="HiddenRestrictedPerson")
        pending_old = ArchiveItemPersonSuggestion.objects.create(
            archive_item=public_item,
            person=add_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע-ישן-ממתין",
            submitter_email="old@example.com",
            submitter_note="הערה ישנה",
        )
        approved_newer = ArchiveItemPersonSuggestion.objects.create(
            archive_item=public_item,
            person=remove_person,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            submitter_name="מציע-חדש-מאושר",
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
        )
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=restricted_item,
            person=hidden_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מוסתר",
        )
        ArchiveItemPersonSuggestion.objects.filter(pk=pending_old.pk).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        ArchiveItemPersonSuggestion.objects.filter(pk=approved_newer.pk).update(
            created_at=timezone.now() - timedelta(hours=1)
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצעות שיוך אנשים")
        self.assertContains(resp, public_item.title)
        self.assertNotContains(resp, restricted_item.title)
        self.assertNotContains(resp, "HiddenRestrictedPerson")
        self.assertContains(resp, person_staff_picker_label(add_person))
        self.assertContains(resp, "הוספת שיוך")
        self.assertContains(resp, "הסרת שיוך")
        self.assertContains(resp, "מציע-ישן-ממתין")
        self.assertContains(resp, "old@example.com")
        html = resp.content.decode()
        self.assertLess(html.index("מציע-ישן-ממתין"), html.index("מציע-חדש-מאושר"))
        self.assertContains(resp, self._approve_url(pending_old.id))
        self.assertNotContains(resp, self._approve_url(approved_newer.id))

        staff_with_perm = User.objects.create_user(
            username="person_sugg_staff_perm",
            password="test-pass",
            is_staff=True,
        )
        _grant_restricted_permission(staff_with_perm)
        self.client.force_login(staff_with_perm)
        permitted = self.client.get(self._backlog_url())
        self.assertContains(permitted, restricted_item.title)
        self.assertContains(permitted, "HiddenRestrictedPerson")

    def test_nonstaff_403_and_anonymous_login_redirect(self):
        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(self._backlog_url()).status_code, 403)
        self.client.logout()
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_nonstaff_and_anonymous_cannot_approve_or_reject(self):
        item = self._manual()
        person = Person.objects.create(name="PermPerson")
        row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )

        self.client.force_login(self.viewer)
        self.assertEqual(self.client.post(self._approve_url(row.id)).status_code, 403)
        self.assertEqual(self.client.post(self._reject_url(row.id)).status_code, 403)

        self.client.logout()
        approve = self.client.post(self._approve_url(row.id))
        self.assertEqual(approve.status_code, 302)
        self.assertIn("/accounts/login/", approve["Location"])
        reject = self.client.post(self._reject_url(row.id))
        self.assertEqual(reject.status_code, 302)
        self.assertIn("/accounts/login/", reject["Location"])

        row.refresh_from_db()
        self.assertEqual(row.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )

    def test_staff_nav_contains_person_suggestion_link(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, "הצעות שיוך אנשים")
        self.assertContains(resp, self._backlog_url())


class ArchiveItemPersonSuggestionStaffReviewTests(ArchiveItemPersonSuggestionUiHarness):
    def test_approve_add_and_remove(self):
        item = self._manual()
        add_person = Person.objects.create(name="ApproveAdd")
        remove_person = Person.objects.create(name="ApproveRemove")
        create_archive_item_person(archive_item=item, person=remove_person)
        add_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=add_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        remove_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=remove_person,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            submitter_name="מציע/ה",
        )

        self.client.force_login(self.staff)
        add_resp = self.client.post(self._approve_url(add_row.id))
        self.assertEqual(add_resp.status_code, 302)
        self.assertEqual(add_resp["Location"], self._backlog_url())
        self.assertIn(PERSON_SUGGESTION_ADD_SUCCESS_MSG, self._messages(add_resp))
        add_row.refresh_from_db()
        self.assertEqual(add_row.status, ArchiveItemPersonSuggestion.Status.APPROVED)
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=add_person
            ).exists()
        )

        remove_resp = self.client.post(self._approve_url(remove_row.id))
        self.assertEqual(remove_resp.status_code, 302)
        self.assertIn(PERSON_SUGGESTION_REMOVE_SUCCESS_MSG, self._messages(remove_resp))
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=remove_person
            ).exists()
        )

    def test_reject_add_and_remove_do_not_mutate(self):
        item = self._manual()
        add_person = Person.objects.create(name="RejectAdd")
        remove_person = Person.objects.create(name="RejectRemove")
        create_archive_item_person(archive_item=item, person=remove_person)
        add_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=add_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        remove_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=remove_person,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            submitter_name="מציע/ה",
        )
        self.client.force_login(self.staff)
        add_resp = self.client.post(self._reject_url(add_row.id))
        self.assertEqual(add_resp.status_code, 302)
        self.assertEqual(add_resp["Location"], self._backlog_url())
        remove_resp = self.client.post(self._reject_url(remove_row.id))
        self.assertEqual(remove_resp.status_code, 302)
        self.assertEqual(remove_resp["Location"], self._backlog_url())
        add_row.refresh_from_db()
        remove_row.refresh_from_db()
        self.assertEqual(add_row.status, ArchiveItemPersonSuggestion.Status.REJECTED)
        self.assertEqual(remove_row.status, ArchiveItemPersonSuggestion.Status.REJECTED)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=add_person
            ).exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=remove_person
            ).exists()
        )

    def test_stale_add_and_remove_messages(self):
        item = self._manual()
        add_person = Person.objects.create(name="StaleAdd")
        remove_person = Person.objects.create(name="StaleRemove")
        add_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=add_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        create_archive_item_person(archive_item=item, person=add_person)
        create_archive_item_person(archive_item=item, person=remove_person)
        remove_row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=remove_person,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            submitter_name="מציע/ה",
        )
        delete_archive_item_person(
            ArchiveItemPerson.objects.get(archive_item=item, person=remove_person)
        )

        self.client.force_login(self.staff)
        add_resp = self.client.post(self._approve_url(add_row.id))
        self.assertIn(PERSON_SUGGESTION_STALE_ADD_MSG, self._messages(add_resp))
        add_row.refresh_from_db()
        self.assertEqual(add_row.status, ArchiveItemPersonSuggestion.Status.APPROVED)

        remove_resp = self.client.post(self._approve_url(remove_row.id))
        self.assertIn(PERSON_SUGGESTION_STALE_REMOVE_MSG, self._messages(remove_resp))
        remove_row.refresh_from_db()
        self.assertEqual(remove_row.status, ArchiveItemPersonSuggestion.Status.APPROVED)

    def test_already_reviewed_error_and_get_is_not_allowed(self):
        item = self._manual()
        person = Person.objects.create(name="AlreadyReviewed")
        row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
        )
        self.client.force_login(self.staff)
        resp = self.client.post(self._approve_url(row.id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._backlog_url())
        self.assertIn(ALREADY_REVIEWED_ERROR, self._messages(resp))
        self.assertEqual(self.client.get(self._approve_url(row.id)).status_code, 405)
        self.assertEqual(self.client.get(self._reject_url(row.id)).status_code, 405)

    def test_approve_photo_add_does_not_create_photoperson(self):
        item, photo = _create_photo_item(
            title="Photo approve isolation",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        appearance = Person.objects.create(name="StillAppearance")
        add_person = Person.objects.create(name="ItemOnlyAdd")
        PhotoPerson.objects.create(photo_content=photo, person=appearance)
        row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=add_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        self.client.force_login(self.staff)
        self.client.post(self._approve_url(row.id))
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=add_person
            ).exists()
        )
        self.assertEqual(PhotoPerson.objects.count(), 1)
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=appearance).exists()
        )

    def test_approve_photo_remove_leaves_photoperson_intact(self):
        item, photo = _create_photo_item(
            title="Photo remove isolation",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="BothRelations")
        create_archive_item_person(archive_item=item, person=person)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        row = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            submitter_name="מציע/ה",
        )
        self.client.force_login(self.staff)
        self.client.post(self._approve_url(row.id))
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )


class AuthorizedPersonUniverseHelperTests(TestCase):
    def test_universe_matches_viewable_archive_item_person_links_only(self):
        Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(username="universe_family", password="x")
        family.groups.add(Group.objects.get(name=ARCHIVE_FAMILY_GROUP_NAME))
        public_item = create_manual_text_archive_item(
            title="Public universe",
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private_item = create_manual_text_archive_item(
            title="Private universe",
            body="גוף",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted_item = create_manual_text_archive_item(
            title="Restricted universe",
            body="גוף",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        photo_item, photo = _create_photo_item(
            title="Photo universe",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        public_person = Person.objects.create(name="PublicUniverse")
        private_person = Person.objects.create(name="PrivateUniverse")
        restricted_person = Person.objects.create(name="RestrictedUniverse")
        photo_only = Person.objects.create(name="PhotoOnlyUniverse")
        create_archive_item_person(archive_item=public_item, person=public_person)
        create_archive_item_person(archive_item=private_item, person=private_person)
        create_archive_item_person(
            archive_item=restricted_item, person=restricted_person
        )
        PhotoPerson.objects.create(photo_content=photo, person=photo_only)

        self.assertEqual(
            authorized_person_universe_ids_for_user(AnonymousUser()),
            frozenset({public_person.id}),
        )
        self.assertEqual(
            authorized_person_universe_ids_for_user(family),
            frozenset({public_person.id, private_person.id}),
        )
        self.assertNotIn(photo_only.id, authorized_person_universe_ids_for_user(family))
        self.assertNotIn(
            restricted_person.id, authorized_person_universe_ids_for_user(family)
        )
