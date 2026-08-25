"""D2a: retired historical Person-Tag names stay blocked after Tag-row deletion."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.forms import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.admin import DocumentAdminForm, TagAdminForm
from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_RECORDS,
    historical_person_name_tag_ids,
    historical_person_tag_retired_names,
)
from documents.models import (
    ArchiveItem,
    ArchiveItemPersonSuggestion,
    ArchiveMetadataSuggestion,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_discovery_metadata_validation import (
    HISTORICAL_PERSON_TAG_REUSE_ERROR,
    parse_archive_item_discovery_metadata_form,
)
from documents.services.archive_item_person_suggestions import (
    existing_person_universe_ids,
    submit_archive_item_person_suggestion,
)
from documents.services.archive_items import (
    _get_or_create_tag_by_name,
    create_manual_text_archive_item,
    create_ocr_document,
    update_archive_item_discovery_metadata,
    update_ocr_document_tags,
)
from documents.services.archive_metadata_suggestion_review import (
    ArchiveMetadataSuggestionReviewError,
    approve_suggestion,
    pending_archive_metadata_suggestions_with_retired_tag_names,
)
from documents.test_historical_person_tag_reuse import (
    BLOCKED_TAG_ID,
    EDIT_URL_TEMPLATE,
    _blocked_tag,
    _create_tag,
    _manual_text_payload,
    _reset_pk_sequence,
)
from public.services.registration import HONEYPOT_FIELD_NAME

RETIRED_TAG_ID = 29
RETIRED_NAME = "שלמה הלל"
ORDINARY_NAME = "משפחה"
NEAR_MISS_NAME = "שלמה  הלל"
CREATE_URL = "/archive/manage/new/"


def _seed_mapped_tag(*, tag_id: int = RETIRED_TAG_ID, name: str = RETIRED_NAME) -> Tag:
    tag = Tag.objects.create(pk=tag_id, name=name)
    _reset_pk_sequence(Tag)
    return tag


def _delete_mapped_tag_row(
    *, tag_id: int = RETIRED_TAG_ID, name: str = RETIRED_NAME
) -> None:
    _seed_mapped_tag(tag_id=tag_id, name=name)
    Tag.objects.filter(pk=tag_id).delete()
    _reset_pk_sequence(Tag)


class RetiredHistoricalPersonTagNameMapCoverageTests(TestCase):
    def test_all_twenty_nine_frozen_names_are_unique_and_rejected_after_delete(self):
        self.assertEqual(len(historical_person_tag_retired_names()), 29)
        self.assertEqual(len(historical_person_name_tag_ids()), 29)
        for tag_id, _person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS:
            _delete_mapped_tag_row(tag_id=tag_id, name=name)
            parsed, errors = parse_archive_item_discovery_metadata_form({"tags": name})
            self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors, name)
            self.assertIn(name, parsed["tag_names"])
            self.assertFalse(Tag.objects.filter(name=name).exists(), name)
            self.assertFalse(Tag.objects.filter(pk=tag_id).exists(), name)

    def test_match_is_exact_after_parse_trim_not_fuzzy_or_casefold(self):
        _delete_mapped_tag_row()
        _padded, padded_errors = parse_archive_item_discovery_metadata_form(
            {"tags": f"  {RETIRED_NAME}  "}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, padded_errors)
        near, near_errors = parse_archive_item_discovery_metadata_form(
            {"tags": NEAR_MISS_NAME}
        )
        self.assertEqual(near_errors, [])
        self.assertEqual(near["tag_names"], [NEAR_MISS_NAME])


class RetiredHistoricalPersonTagNameWritePathTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="retired_tag_name_staff",
            password="test-pass",
            is_staff=True,
        )
        _delete_mapped_tag_row()

    def test_parse_and_staff_edit_refuse_retired_name_and_do_not_recreate_tag(self):
        allowed = _create_tag(name="keep-allowed")
        item = create_manual_text_archive_item(title="Edit retired name", body="Body")
        item.tags.add(allowed)
        self.client.force_login(self.staff)

        parsed, errors = parse_archive_item_discovery_metadata_form(
            {"tags": RETIRED_NAME, "categories": "should-not-stick"}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors)
        self.assertIn(RETIRED_NAME, parsed["tag_names"])

        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=_manual_text_payload(
                selected_tags=[str(allowed.id)],
                tags=RETIRED_NAME,
                categories="should-not-stick",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)
        item.refresh_from_db()
        self.assertEqual(set(item.tags.values_list("pk", flat=True)), {allowed.pk})
        self.assertFalse(item.categories.filter(name="should-not-stick").exists())
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        self.assertFalse(Tag.objects.filter(pk=RETIRED_TAG_ID).exists())

    def test_posted_mapped_tag_id_is_still_blocked_after_row_delete(self):
        parsed, errors = parse_archive_item_discovery_metadata_form(
            {"selected_tags": [str(RETIRED_TAG_ID)]}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors)
        self.assertIn(RETIRED_TAG_ID, parsed["selected_tag_ids"])
        self.assertFalse(Tag.objects.filter(pk=RETIRED_TAG_ID).exists())
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

    def test_staff_create_refuses_retired_name_and_does_not_create_item_or_tag(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            CREATE_URL,
            data=_manual_text_payload(
                item_type="manual_text",
                title="Create retired name",
                tags=RETIRED_NAME,
                categories="create-should-not-stick",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)
        self.assertFalse(
            ArchiveItem.objects.filter(title="Create retired name").exists()
        )
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

    def test_service_replace_refuses_retired_name_without_partial_writes(self):
        item = create_manual_text_archive_item(
            title="Service retired name", body="Body"
        )
        with self.assertRaises(ValueError) as caught:
            update_archive_item_discovery_metadata(
                item,
                category_names=["should-not-stick"],
                event_names=[],
                tag_names=[RETIRED_NAME, "allowed-after-block"],
            )
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(item.categories.count(), 0)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        self.assertFalse(Tag.objects.filter(name="allowed-after-block").exists())

    def test_get_or_create_tag_by_name_refuses_retired_name(self):
        with self.assertRaises(ValueError) as caught:
            _get_or_create_tag_by_name(RETIRED_NAME)
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

    def test_update_ocr_document_tags_refuses_retired_name_after_delete(self):
        ordinary = _create_tag(name="ocr-ordinary")
        doc = create_ocr_document(
            title="OCR retired name",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        doc.tags_m2m.add(ordinary)
        with self.assertRaises(ValueError) as caught:
            update_ocr_document_tags(doc, tag_names=[RETIRED_NAME, "also-new"])
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        doc.refresh_from_db()
        self.assertEqual(list(doc.tags_m2m.values_list("pk", flat=True)), [ordinary.pk])
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        self.assertFalse(Tag.objects.filter(name="also-new").exists())

    def test_tag_admin_add_and_rename_refuse_retired_name_after_delete(self):
        add_form = TagAdminForm(data={"name": RETIRED_NAME})
        self.assertFalse(add_form.is_valid())
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, add_form.errors["name"])
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

        ordinary = _create_tag(name="rename-me")
        change_form = TagAdminForm(data={"name": RETIRED_NAME}, instance=ordinary)
        self.assertFalse(change_form.is_valid())
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, change_form.errors["name"])
        ordinary.refresh_from_db()
        self.assertEqual(ordinary.name, "rename-me")
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

        ordinary_form = TagAdminForm(data={"name": ORDINARY_NAME})
        self.assertTrue(ordinary_form.is_valid())

    def test_document_admin_form_refuses_retired_name_even_on_unmapped_pk(self):
        leftover = _create_tag(name=RETIRED_NAME)
        self.assertNotEqual(leftover.pk, RETIRED_TAG_ID)
        ordinary = _create_tag(name="form-ordinary")
        doc = create_ocr_document(
            title="Admin retired name",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        form = DocumentAdminForm(instance=doc)
        form.cleaned_data = {"tags_m2m": [leftover, ordinary]}
        with self.assertRaises(ValidationError) as caught:
            form.clean_tags_m2m()
        self.assertEqual(
            caught.exception.messages[0], HISTORICAL_PERSON_TAG_REUSE_ERROR
        )
        doc.refresh_from_db()
        self.assertFalse(doc.tags_m2m.exists())

    def test_ordinary_tag_creation_and_reuse_remain_unchanged(self):
        existing = _create_tag(name=ORDINARY_NAME)
        item = create_manual_text_archive_item(title="Ordinary tags", body="Body")
        update_archive_item_discovery_metadata(
            item,
            category_names=[],
            event_names=[],
            tag_names=[ORDINARY_NAME, "new-ordinary"],
        )
        item.refresh_from_db()
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {ORDINARY_NAME, "new-ordinary"},
        )
        self.assertEqual(Tag.objects.get(name=ORDINARY_NAME).pk, existing.pk)


class RetiredHistoricalPersonTagMappedIdStillBlockedTests(TestCase):
    def test_mapped_id_block_still_works_while_tag_row_exists(self):
        blocked = _blocked_tag()
        parsed_ids, id_errors = parse_archive_item_discovery_metadata_form(
            {"selected_tags": [str(blocked.pk)]}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, id_errors)
        self.assertIn(blocked.pk, parsed_ids["selected_tag_ids"])

        parsed_name, name_errors = parse_archive_item_discovery_metadata_form(
            {"tags": blocked.name}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, name_errors)
        self.assertIn(blocked.name, parsed_name["tag_names"])
        self.assertTrue(Tag.objects.filter(pk=BLOCKED_TAG_ID).exists())


class RetiredHistoricalPersonTagAdminRenameTests(TestCase):
    def test_unchanged_mapped_row_save_is_allowed(self):
        mapped = _seed_mapped_tag()
        form = TagAdminForm(data={"name": RETIRED_NAME}, instance=mapped)
        self.assertTrue(form.is_valid())
        self.assertEqual(Tag.objects.get(pk=RETIRED_TAG_ID).name, RETIRED_NAME)

    def test_mapped_row_rename_is_rejected(self):
        mapped = _seed_mapped_tag()
        form = TagAdminForm(data={"name": ORDINARY_NAME}, instance=mapped)
        self.assertFalse(form.is_valid())
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, form.errors["name"])
        mapped.refresh_from_db()
        self.assertEqual(mapped.pk, RETIRED_TAG_ID)
        self.assertEqual(mapped.name, RETIRED_NAME)

    def test_ordinary_to_ordinary_rename_is_allowed(self):
        ordinary = _create_tag(name="rename-from")
        form = TagAdminForm(data={"name": "rename-to"}, instance=ordinary)
        self.assertTrue(form.is_valid())
        updated = form.save()
        self.assertEqual(updated.pk, ordinary.pk)
        self.assertEqual(updated.name, "rename-to")

    def test_ordinary_to_retired_rename_is_rejected(self):
        ordinary = _create_tag(name="rename-me")
        form = TagAdminForm(data={"name": RETIRED_NAME}, instance=ordinary)
        self.assertFalse(form.is_valid())
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, form.errors["name"])
        ordinary.refresh_from_db()
        self.assertEqual(ordinary.name, "rename-me")
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())


@override_settings(UPLOADS_BUCKET_NAME="")
class RetiredHistoricalPersonTagSuggestionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="retired_tag_suggestion_staff",
            password="test-pass",
            is_staff=True,
        )
        _delete_mapped_tag_row()

    def _create_item(self) -> ArchiveItem:
        return create_manual_text_archive_item(
            title="Suggestion retired name",
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def test_public_submission_of_retired_name_fails_clearly_and_creates_nothing(self):
        item = self._create_item()
        url = reverse("archive-metadata-suggestion-new", kwargs={"item_id": item.id})
        resp = self.client.post(
            url,
            data={
                "submitter_name": "מציע/ה",
                "submitter_email": "suggester@example.com",
                "submitter_note": "",
                "suggested_categories": "קטגוריה חדשה",
                "suggested_events": "",
                "suggested_tags": f"  {RETIRED_NAME}  , תגית-חדשה",
                HONEYPOT_FIELD_NAME: "",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())

    def test_existing_pending_suggestion_is_not_auto_rejected_or_mutated(self):
        item = self._create_item()
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories="קטגוריה-קיימת",
            suggested_events="",
            suggested_tags=RETIRED_NAME,
        )
        inventory = pending_archive_metadata_suggestions_with_retired_tag_names()
        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].suggestion_id, suggestion.id)
        self.assertEqual(inventory[0].archive_item_id, item.id)
        self.assertEqual(inventory[0].retired_names, (RETIRED_NAME,))
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.PENDING)
        self.assertEqual(suggestion.suggested_tags, RETIRED_NAME)
        self.assertIsNone(suggestion.reviewed_at)

    def test_inventory_ignores_non_pending_and_ordinary_tag_suggestions(self):
        item = self._create_item()
        ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_tags=ORDINARY_NAME,
        )
        ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_tags=RETIRED_NAME,
            status=ArchiveMetadataSuggestion.Status.APPROVED,
        )
        ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_tags=RETIRED_NAME,
            status=ArchiveMetadataSuggestion.Status.REJECTED,
        )
        self.assertEqual(
            pending_archive_metadata_suggestions_with_retired_tag_names(), []
        )

    def test_approve_rechecks_retired_name_and_rolls_back_mixed_writes(self):
        item = self._create_item()
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories="קטגוריה-חדשה",
            suggested_events="אירוע-חדש",
            suggested_tags=f"{RETIRED_NAME}, תגית-חדשה",
        )
        with self.assertRaises(ArchiveMetadataSuggestionReviewError) as caught:
            approve_suggestion(suggestion.id, reviewer=self.staff)
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.PENDING)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(item.categories.count(), 0)
        self.assertEqual(item.events.count(), 0)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        self.assertFalse(Tag.objects.filter(name="תגית-חדשה").exists())
        self.assertFalse(item.categories.filter(name="קטגוריה-חדשה").exists())

    def test_approve_of_ordinary_tags_still_succeeds_after_mapped_tag_delete(self):
        item = self._create_item()
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories="",
            suggested_events="",
            suggested_tags="תגית-מותרת",
        )
        approve_suggestion(suggestion.id, reviewer=self.staff)
        item.refresh_from_db()
        self.assertEqual(list(item.tags.values_list("name", flat=True)), ["תגית-מותרת"])
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())


@override_settings(UPLOADS_BUCKET_NAME="")
class RetiredHistoricalPersonTagUntouchedRelationsTests(TestCase):
    def setUp(self):
        _delete_mapped_tag_row()

    def test_archive_item_person_suggestion_and_photo_person_are_untouched(self):
        item = create_manual_text_archive_item(
            title="Person relations untouched",
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name=RETIRED_NAME)
        alias = PersonAlias.objects.create(person=person, name="שלמה")
        person_suggestion = submit_archive_item_person_suggestion(
            archive_item=item,
            person_id=person.pk,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
            authorized_person_ids=existing_person_universe_ids(),
        )
        photo_item = ArchiveItem.objects.create(
            title="Photo relations untouched",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = PhotoContent.objects.create(
            archive_item=photo_item,
            position=1,
            original_file_key="photos/untouched/1.jpg",
            original_filename="1.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        appearance = PhotoPerson.objects.create(photo_content=photo, person=person)

        url = reverse("archive-metadata-suggestion-new", kwargs={"item_id": item.id})
        resp = self.client.post(
            url,
            data={
                "submitter_name": "מציע/ה",
                "submitter_email": "suggester@example.com",
                "submitter_note": "",
                "suggested_categories": "",
                "suggested_events": "",
                "suggested_tags": RETIRED_NAME,
                HONEYPOT_FIELD_NAME: "",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)

        person_suggestion.refresh_from_db()
        alias.refresh_from_db()
        appearance.refresh_from_db()
        self.assertEqual(
            person_suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING
        )
        self.assertEqual(PersonAlias.objects.get(pk=alias.pk).name, "שלמה")
        self.assertEqual(PhotoPerson.objects.get(pk=appearance.pk).person_id, person.pk)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)
        self.assertEqual(PhotoPerson.objects.count(), 1)
        self.assertFalse(Tag.objects.filter(name=RETIRED_NAME).exists())
        self.assertEqual(Person.objects.get(pk=person.pk).name, RETIRED_NAME)
