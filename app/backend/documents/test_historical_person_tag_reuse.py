"""Block reuse of frozen historical person-name Tags (ID membership only)."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.historical_person_tag_map import historical_person_name_tag_ids
from documents.models import (
    ArchiveItem,
    ArchiveMetadataSuggestion,
    Tag,
)
from documents.services.archive_discovery_metadata_validation import (
    HISTORICAL_PERSON_TAG_REUSE_ERROR,
    discovery_metadata_option_querysets,
    parse_archive_item_discovery_metadata_form,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    discovery_metadata_form_data_from_item,
    update_archive_item_discovery_metadata,
)
from documents.services.archive_metadata_suggestion_review import (
    ArchiveMetadataSuggestionReviewError,
    approve_suggestion,
)
from documents.tag_pk_sequence_support import (
    ensure_tag_pk_sequence_past_historical_ids,
    reset_pk_sequence,
)
from public.services.registration import HONEYPOT_FIELD_NAME

BLOCKED_TAG_ID = 29
EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"
_SAFE_TAG_PK_START = 1000


def _reset_pk_sequence(model):
    """Reset PK sequence from existing rows; Tag also skips frozen historical ids."""
    reset_pk_sequence(model)


def _advance_tag_pk_sequence_past_historical_ids() -> None:
    """Ensure the next auto Tag PK is above ``max(historical_person_name_tag_ids())``."""
    ensure_tag_pk_sequence_past_historical_ids()


def _next_safe_tag_pk() -> int:
    blocked = historical_person_name_tag_ids()
    used = set(Tag.objects.values_list("pk", flat=True))
    pk = _SAFE_TAG_PK_START
    while pk in blocked or pk in used:
        pk += 1
    return pk


def _create_tag(*, pk: int | None = None, name: str) -> Tag:
    if pk is None:
        pk = _next_safe_tag_pk()
    tag = Tag.objects.create(pk=pk, name=name)
    _reset_pk_sequence(Tag)
    return tag


def _blocked_tag() -> Tag:
    return _create_tag(pk=BLOCKED_TAG_ID, name="blocked-tag-id-only-label")


def _manual_text_payload(**overrides) -> dict:
    payload = {
        "title": "Manual discovery",
        "body": "Body text.",
        "visibility": ArchiveItem.Visibility.PUBLIC,
        "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        "categories": "",
        "events": "",
        "tags": "",
    }
    payload.update(overrides)
    return payload


class HistoricalPersonTagChoiceTests(TestCase):
    def test_blocked_ids_are_excluded_from_discovery_tag_choices(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="allowed-topic")
        same_label_unmapped = _create_tag(name="שלמה הלל")

        choice_ids = list(
            discovery_metadata_option_querysets()["discovery_all_tags"].values_list(
                "pk",
                flat=True,
            )
        )
        self.assertNotIn(blocked.pk, choice_ids)
        self.assertIn(allowed.pk, choice_ids)
        self.assertIn(same_label_unmapped.pk, choice_ids)
        self.assertNotEqual(same_label_unmapped.pk, BLOCKED_TAG_ID)

    def test_edit_form_seed_omits_blocked_ids_but_keeps_allowed_ids(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="keep-visible")
        item = create_manual_text_archive_item(title="Seed item", body="Body")
        item.tags.add(blocked, allowed)

        form_data = discovery_metadata_form_data_from_item(item)
        self.assertNotIn(blocked.pk, form_data["selected_tag_ids"])
        self.assertIn(allowed.pk, form_data["selected_tag_ids"])

    def test_staff_edit_get_excludes_blocked_tag_from_selector(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="shown-topic")
        item = create_manual_text_archive_item(title="Selector item", body="Body")
        item.tags.add(blocked, allowed)
        staff = User.objects.create_user(
            username="historical_tag_choice_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        self.assertEqual(resp.status_code, 200)
        choice_ids = [tag.pk for tag in resp.context["discovery_all_tags"]]
        self.assertNotIn(blocked.pk, choice_ids)
        self.assertIn(allowed.pk, choice_ids)


class HistoricalPersonTagWritePathTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="historical_tag_staff",
            password="test-pass",
            is_staff=True,
        )

    def test_parse_rejects_tampered_blocked_tag_ids(self):
        allowed = _create_tag(name="allowed-parse")
        parsed, errors = parse_archive_item_discovery_metadata_form(
            {
                "selected_tags": [str(BLOCKED_TAG_ID), str(allowed.pk)],
                "tags": "also-new",
                "categories": "keep-category",
            }
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors)
        self.assertIn(BLOCKED_TAG_ID, parsed["selected_tag_ids"])

    def test_parse_rejects_name_that_resolves_to_a_blocked_tag_id(self):
        blocked = _blocked_tag()
        parsed, errors = parse_archive_item_discovery_metadata_form(
            {"tags": blocked.name, "categories": "unrelated-category"}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors)
        self.assertIn(blocked.name, parsed["tag_names"])

    def test_staff_tampered_selected_tag_id_is_rejected_and_writes_nothing(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="staff-allowed")
        item = create_manual_text_archive_item(title="Staff tamper", body="Body")
        item.tags.add(allowed)
        self.client.force_login(self.staff)

        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=_manual_text_payload(
                selected_tags=[str(allowed.id), str(blocked.id)],
                tags="new-topic",
                categories="new-category",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)
        item.refresh_from_db()
        self.assertEqual(set(item.tags.values_list("pk", flat=True)), {allowed.pk})
        self.assertFalse(item.categories.filter(name="new-category").exists())
        self.assertFalse(Tag.objects.filter(name="new-topic").exists())

    def test_ordinary_edit_preserves_existing_blocked_tag_relations(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="keep-allowed")
        item = create_manual_text_archive_item(title="Preserve blocked", body="Body")
        item.tags.add(blocked, allowed)
        self.client.force_login(self.staff)

        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=_manual_text_payload(
                selected_tags=[str(allowed.id)],
                tags="added-topic",
            ),
        )
        self.assertEqual(resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            set(item.tags.values_list("pk", flat=True)),
            {blocked.pk, allowed.pk, Tag.objects.get(name="added-topic").pk},
        )

    def test_service_replace_all_preserves_blocked_and_applies_allowed_changes(self):
        blocked = _blocked_tag()
        allowed = _create_tag(name="service-allowed")
        item = create_manual_text_archive_item(title="Service preserve", body="Body")
        item.tags.add(blocked, allowed)

        update_archive_item_discovery_metadata(
            item,
            category_names=["kept-category"],
            event_names=[],
            tag_names=["replacement-topic"],
        )
        item.refresh_from_db()
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {blocked.name, "replacement-topic"},
        )
        self.assertEqual(
            list(item.categories.values_list("name", flat=True)),
            ["kept-category"],
        )

    def test_service_fails_closed_when_adding_a_new_blocked_tag(self):
        blocked = _blocked_tag()
        item = create_manual_text_archive_item(title="Service reject", body="Body")

        with self.assertRaises(ValueError) as caught:
            update_archive_item_discovery_metadata(
                item,
                category_names=["should-not-stick"],
                event_names=[],
                tag_names=[blocked.name, "allowed-after-block"],
            )
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(item.categories.count(), 0)

    def test_unmapped_id_with_retired_historical_tag_name_is_blocked(self):
        unmapped = _create_tag(name="שלמה הלל")
        self.assertNotEqual(unmapped.pk, BLOCKED_TAG_ID)
        self.assertNotIn(unmapped.pk, historical_person_name_tag_ids())
        parsed, errors = parse_archive_item_discovery_metadata_form(
            {"selected_tags": [str(unmapped.pk)]}
        )
        self.assertIn(HISTORICAL_PERSON_TAG_REUSE_ERROR, errors)
        self.assertEqual(parsed["selected_tag_ids"], [unmapped.pk])


@override_settings(UPLOADS_BUCKET_NAME="")
class HistoricalPersonTagSuggestionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="historical_tag_suggestion_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_item(self) -> ArchiveItem:
        return create_manual_text_archive_item(
            title="Suggestion item",
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def test_public_tampered_selected_tag_ids_are_rejected(self):
        blocked = _blocked_tag()
        item = self._create_item()
        url = reverse(
            "archive-metadata-suggestion-new",
            kwargs={"item_id": item.id},
        )
        resp = self.client.post(
            url,
            data={
                "submitter_name": "מציע/ה",
                "submitter_email": "suggester@example.com",
                "submitter_note": "",
                "suggested_categories": "קטגוריה חדשה",
                "suggested_events": "",
                "suggested_tags": "תגית-חדשה",
                "selected_tags": [str(blocked.id)],
                HONEYPOT_FIELD_NAME: "",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HISTORICAL_PERSON_TAG_REUSE_ERROR)
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)
        item.refresh_from_db()
        self.assertEqual(item.tags.count(), 0)

    def test_approve_fails_closed_and_does_not_add_or_remove_blocked_tags(self):
        blocked = _blocked_tag()
        allowed_existing = _create_tag(name="already-on-item")
        item = self._create_item()
        item.tags.add(blocked, allowed_existing)
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories="קטגוריה-חדשה",
            suggested_events="",
            suggested_tags=f"{blocked.name}, תגית-חדשה",
        )

        with self.assertRaises(ArchiveMetadataSuggestionReviewError) as caught:
            approve_suggestion(suggestion.id, reviewer=self.staff)
        self.assertEqual(str(caught.exception), HISTORICAL_PERSON_TAG_REUSE_ERROR)

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.PENDING)
        item.refresh_from_db()
        self.assertEqual(
            set(item.tags.values_list("pk", flat=True)),
            {blocked.pk, allowed_existing.pk},
        )
        self.assertFalse(item.categories.filter(name="קטגוריה-חדשה").exists())
        self.assertFalse(Tag.objects.filter(name="תגית-חדשה").exists())

    def test_approve_of_unrelated_tags_does_not_remove_blocked_relations(self):
        blocked = _blocked_tag()
        item = self._create_item()
        item.tags.add(blocked)
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories="",
            suggested_events="",
            suggested_tags="תגית-מותרת",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)
        item.refresh_from_db()
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {blocked.name, "תגית-מותרת"},
        )
