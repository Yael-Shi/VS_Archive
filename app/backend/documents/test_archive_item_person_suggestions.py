"""ArchiveItemPersonSuggestion model, submission, and review (C2a)."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, models, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    ArchiveItemSearchIndex,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_people import (
    create_archive_item_person,
    delete_archive_item_person,
)
from documents.services.archive_item_person_suggestion_review import (
    ALREADY_REVIEWED_ERROR,
    ArchiveItemPersonSuggestionReviewError,
    approve_suggestion,
    reject_suggestion,
)
from documents.services.archive_item_person_suggestions import (
    DUPLICATE_PENDING_SUGGESTION_ERROR,
    PERSON_ALREADY_LINKED_ERROR,
    PERSON_NOT_LINKED_ERROR,
    ArchiveItemPersonSuggestionError,
    existing_person_universe_ids,
    linked_person_ids_for_archive_item,
    submit_archive_item_person_suggestion,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.archive_metadata_suggestions import NAME_REQUIRED_ERROR
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


def _create_photo_item(*, title: str) -> tuple[ArchiveItem, PhotoContent]:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
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


def _person_ids(item: ArchiveItem) -> list[int]:
    return list(
        ArchiveItemPerson.objects.filter(archive_item=item)
        .order_by("person_id")
        .values_list("person_id", flat=True)
    )


class ArchiveItemPersonSuggestionHarness:
    def setUp(self):
        self.reviewer = User.objects.create_user(
            username="person_suggestion_reviewer",
            password="test-pass",
            is_staff=True,
        )
        self.submitter = User.objects.create_user(
            username="person_suggestion_submitter",
            password="test-pass",
        )

    def _item(self, *, title: str = "Suggestion item") -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _submit(
        self,
        item: ArchiveItem,
        person: Person,
        *,
        action: str,
        authorized_person_ids=None,
        submitter_name: str = "מציע/ה",
        submitter_email: str = "suggester@example.com",
        submitter_note: str = "",
        submitter_user=None,
    ) -> ArchiveItemPersonSuggestion:
        if authorized_person_ids is None:
            authorized_person_ids = existing_person_universe_ids()
        return submit_archive_item_person_suggestion(
            archive_item=item,
            person_id=person.pk,
            action=action,
            submitter_name=submitter_name,
            authorized_person_ids=authorized_person_ids,
            submitter_email=submitter_email,
            submitter_note=submitter_note,
            submitter_user=submitter_user,
        )


class ArchiveItemPersonSuggestionModelTests(TestCase):
    def test_required_identity_and_action_fields(self):
        item = create_manual_text_archive_item(title="Required fields", body="גוף")
        person = Person.objects.create(name="Ada")
        action_field = ArchiveItemPersonSuggestion._meta.get_field("action")
        self.assertFalse(action_field.blank)
        self.assertIs(action_field.default, models.NOT_PROVIDED)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemPersonSuggestion.objects.create(
                    person=person,
                    action=ArchiveItemPersonSuggestion.Action.ADD,
                    submitter_name="מציע/ה",
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemPersonSuggestion.objects.create(
                    archive_item=item,
                    action=ArchiveItemPersonSuggestion.Action.ADD,
                    submitter_name="מציע/ה",
                )

        suggestion = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        self.assertEqual(suggestion.archive_item_id, item.pk)
        self.assertEqual(suggestion.person_id, person.pk)
        self.assertEqual(suggestion.action, ArchiveItemPersonSuggestion.Action.ADD)

    def test_action_choices_are_add_and_remove_only(self):
        values = {value for value, _label in ArchiveItemPersonSuggestion.Action.choices}
        self.assertEqual(values, {"ADD", "REMOVE"})

    def test_status_defaults_to_pending_and_review_fields_empty(self):
        item = create_manual_text_archive_item(title="Defaults", body="גוף")
        person = Person.objects.create(name="Ada")
        suggestion = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertIsNone(suggestion.reviewed_at)
        self.assertIsNone(suggestion.reviewed_by_id)
        self.assertEqual(suggestion.submitter_email, "")
        self.assertEqual(suggestion.submitter_note, "")

    def test_person_fk_protects_delete(self):
        item = create_manual_text_archive_item(title="Protect person", body="גוף")
        person = Person.objects.create(name="Ada")
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        with self.assertRaises(ProtectedError):
            person.delete()
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)

    def test_archive_item_cascade_deletes_suggestions(self):
        item = create_manual_text_archive_item(title="Cascade item", body="גוף")
        person = Person.objects.create(name="Ada")
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="מציע/ה",
        )
        item.delete()
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())

    def test_pending_duplicate_is_globally_unique_across_submitters(self):
        item = create_manual_text_archive_item(title="Dup pending", body="גוף")
        person = Person.objects.create(name="Ada")
        first_user = User.objects.create_user(username="one", password="x")
        second_user = User.objects.create_user(username="two", password="x")
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="ראשון",
            submitter_user=first_user,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemPersonSuggestion.objects.create(
                    archive_item=item,
                    person=person,
                    action=ArchiveItemPersonSuggestion.Action.ADD,
                    submitter_name="שני",
                    submitter_user=second_user,
                )
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)

    def test_historical_approved_or_rejected_does_not_block_new_pending(self):
        item = create_manual_text_archive_item(title="History", body="גוף")
        person = Person.objects.create(name="Ada")
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="ישן",
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
        )
        ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="נדחה",
            status=ArchiveItemPersonSuggestion.Status.REJECTED,
        )
        pending = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="חדש",
        )
        self.assertEqual(pending.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 3)

    def test_ordering_is_newest_first(self):
        item = create_manual_text_archive_item(title="Order", body="גוף")
        first_person = Person.objects.create(name="Ada")
        second_person = Person.objects.create(name="Charles")
        older = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=first_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="ישן",
        )
        newer = ArchiveItemPersonSuggestion.objects.create(
            archive_item=item,
            person=second_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="חדש",
        )
        self.assertEqual(
            list(ArchiveItemPersonSuggestion.objects.values_list("pk", flat=True)),
            [newer.pk, older.pk],
        )


class ArchiveItemPersonSuggestionSubmitTests(
    ArchiveItemPersonSuggestionHarness, TestCase
):
    def test_valid_add_creates_one_pending_suggestion_only(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item,
            person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_note="חסר בפריט",
            submitter_user=self.submitter,
        )
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(suggestion.action, ArchiveItemPersonSuggestion.Action.ADD)
        self.assertEqual(suggestion.person_id, person.pk)
        self.assertEqual(suggestion.archive_item_id, item.pk)
        self.assertEqual(suggestion.submitter_user, self.submitter)
        self.assertEqual(suggestion.submitter_note, "חסר בפריט")
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_valid_remove_creates_one_pending_suggestion_only(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        create_archive_item_person(archive_item=item, person=person)
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        self.assertEqual(suggestion.action, ArchiveItemPersonSuggestion.Action.REMOVE)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(ArchiveItemPerson.objects.count(), 1)

    def test_add_already_linked_is_rejected(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        create_archive_item_person(archive_item=item, person=person)
        with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
            self._submit(item, person, action=ArchiveItemPersonSuggestion.Action.ADD)
        self.assertEqual(str(ctx.exception), PERSON_ALREADY_LINKED_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_remove_not_currently_linked_is_rejected(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
            self._submit(item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE)
        self.assertEqual(str(ctx.exception), PERSON_NOT_LINKED_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_duplicate_pending_is_rejected_deterministically(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        self._submit(item, person, action=ArchiveItemPersonSuggestion.Action.ADD)
        with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
            self._submit(
                item,
                person,
                action=ArchiveItemPersonSuggestion.Action.ADD,
                submitter_name="מישהו אחר",
            )
        self.assertEqual(str(ctx.exception), DUPLICATE_PENDING_SUGGESTION_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)

    def test_person_outside_authorized_universe_is_rejected(self):
        item = self._item()
        allowed = Person.objects.create(name="Allowed")
        other = Person.objects.create(name="Other")
        with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
            self._submit(
                item,
                other,
                action=ArchiveItemPersonSuggestion.Action.ADD,
                authorized_person_ids={allowed.pk},
            )
        self.assertEqual(str(ctx.exception), PERSON_NOT_FOUND_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 0)

    def test_empty_submitter_name_is_rejected(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
            self._submit(
                item,
                person,
                action=ArchiveItemPersonSuggestion.Action.ADD,
                submitter_name="   ",
            )
        self.assertEqual(str(ctx.exception), NAME_REQUIRED_ERROR)

    def test_duplicate_pending_integrity_error_becomes_domain_error(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        self._submit(item, person, action=ArchiveItemPersonSuggestion.Action.ADD)
        with patch(
            "documents.services.archive_item_person_suggestions._pending_duplicate_exists",
            return_value=False,
        ):
            with self.assertRaises(ArchiveItemPersonSuggestionError) as ctx:
                self._submit(
                    item, person, action=ArchiveItemPersonSuggestion.Action.ADD
                )
        self.assertEqual(str(ctx.exception), DUPLICATE_PENDING_SUGGESTION_ERROR)
        self.assertEqual(ArchiveItemPersonSuggestion.objects.count(), 1)
        self.assertNotIsInstance(ctx.exception, IntegrityError)

    def test_submit_does_not_mutate_relationships_index_or_side_tables(self):
        item = self._item()
        ocr_doc = create_ocr_document(
            title="OCR tags item",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        tag = Tag.objects.create(name="historical-person-tag")
        ocr_doc.tags_m2m.add(tag)
        person = Person.objects.create(name="Ada")
        PersonAlias.objects.create(person=person, name="Ada Lovelace")
        photo_item, photo = _create_photo_item(title="Photo isolation")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        _rebuild(item.pk)
        index_updated_at = _index_for(item.pk).updated_at
        through_before = set(
            Document.tags_m2m.through.objects.values_list("document_id", "tag_id")
        )

        self._submit(item, person, action=ArchiveItemPersonSuggestion.Action.ADD)

        self.assertEqual(ArchiveItemPerson.objects.count(), 0)
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(PersonAlias.objects.count(), 1)
        self.assertEqual(PhotoPerson.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)
        self.assertEqual(
            set(Document.tags_m2m.through.objects.values_list("document_id", "tag_id")),
            through_before,
        )
        self.assertEqual(_index_for(item.pk).updated_at, index_updated_at)
        self.assertEqual(photo.archive_item_id, photo_item.pk)


class ArchiveItemPersonSuggestionReviewTests(
    ArchiveItemPersonSuggestionHarness, TestCase
):
    def test_approve_add_creates_archive_item_person(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        suggestion.refresh_from_db()
        self.assertTrue(result.relationship_changed)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.APPROVED)
        self.assertEqual(suggestion.reviewed_by, self.reviewer)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertEqual(_person_ids(item), [person.pk])

    def test_approve_remove_deletes_only_exact_archive_item_person(self):
        item = self._item()
        keep = Person.objects.create(name="Keep")
        remove = Person.objects.create(name="Remove")
        create_archive_item_person(archive_item=item, person=keep)
        create_archive_item_person(archive_item=item, person=remove)
        suggestion = self._submit(
            item, remove, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertTrue(result.relationship_changed)
        self.assertEqual(_person_ids(item), [keep.pk])
        self.assertTrue(Person.objects.filter(pk=remove.pk).exists())

    def test_reject_add_changes_no_relationship(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        result = reject_suggestion(suggestion.pk, reviewer=self.reviewer)
        suggestion.refresh_from_db()
        self.assertFalse(result.relationship_changed)
        self.assertEqual(suggestion.status, ArchiveItemPersonSuggestion.Status.REJECTED)
        self.assertEqual(suggestion.reviewed_by, self.reviewer)
        self.assertIsNotNone(suggestion.reviewed_at)
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_reject_remove_changes_no_relationship(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        create_archive_item_person(archive_item=item, person=person)
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        result = reject_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertFalse(result.relationship_changed)
        self.assertEqual(_person_ids(item), [person.pk])

    def test_already_added_add_approves_as_noop(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        create_archive_item_person(archive_item=item, person=person)
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertFalse(result.relationship_changed)
        self.assertEqual(
            result.suggestion.status, ArchiveItemPersonSuggestion.Status.APPROVED
        )
        self.assertEqual(ArchiveItemPerson.objects.filter(archive_item=item).count(), 1)

    def test_already_removed_remove_approves_as_noop(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        link = create_archive_item_person(archive_item=item, person=person)
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        delete_archive_item_person(link)
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertFalse(result.relationship_changed)
        self.assertEqual(
            result.suggestion.status, ArchiveItemPersonSuggestion.Status.APPROVED
        )
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_remove_b_after_later_d_keeps_a_and_d(self):
        item = self._item()
        person_a = Person.objects.create(name="A")
        person_b = Person.objects.create(name="B")
        person_d = Person.objects.create(name="D")
        create_archive_item_person(archive_item=item, person=person_a)
        create_archive_item_person(archive_item=item, person=person_b)
        suggestion = self._submit(
            item, person_b, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        create_archive_item_person(archive_item=item, person=person_d)
        with patch(
            "documents.services.archive_item_people.set_archive_item_people"
        ) as set_people:
            result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        set_people.assert_not_called()
        self.assertTrue(result.relationship_changed)
        self.assertEqual(set(_person_ids(item)), {person_a.pk, person_d.pk})

    def test_already_reviewed_approve_and_reject_are_refused(self):
        item = self._item()
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        with self.assertRaises(ArchiveItemPersonSuggestionReviewError) as ctx:
            approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertEqual(str(ctx.exception), ALREADY_REVIEWED_ERROR)
        with self.assertRaises(ArchiveItemPersonSuggestionReviewError) as ctx:
            reject_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertEqual(str(ctx.exception), ALREADY_REVIEWED_ERROR)

    def test_real_add_updates_search_index(self):
        item = self._item(title="Index add")
        person = Person.objects.create(name="AdaIndexToken")
        _rebuild(item.pk)
        self.assertNotIn("AdaIndexToken", _index_for(item.pk).metadata_text)
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertTrue(result.relationship_changed)
        self.assertEqual(wrapped.call_count, 1)
        self.assertIn("AdaIndexToken", _index_for(item.pk).metadata_text)

    def test_real_remove_updates_search_index(self):
        item = self._item(title="Index remove")
        person = Person.objects.create(name="RemoveIndexToken")
        create_archive_item_person(archive_item=item, person=person)
        _rebuild(item.pk)
        self.assertIn("RemoveIndexToken", _index_for(item.pk).metadata_text)
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertTrue(result.relationship_changed)
        self.assertEqual(wrapped.call_count, 1)
        self.assertNotIn("RemoveIndexToken", _index_for(item.pk).metadata_text)

    def test_stale_noop_does_not_refresh_search_index(self):
        item = self._item(title="Index noop")
        person = Person.objects.create(name="NoopIndexToken")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        create_archive_item_person(archive_item=item, person=person)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertFalse(result.relationship_changed)
        mocked.assert_not_called()


class ArchiveItemPersonSuggestionPhotoIsolationTests(
    ArchiveItemPersonSuggestionHarness, TestCase
):
    def test_add_approval_on_photo_creates_archive_item_person_only(self):
        item, photo = _create_photo_item(title="PHOTO add")
        person = Person.objects.create(name="Ada")
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.ADD
        )
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertTrue(result.relationship_changed)
        self.assertEqual(_person_ids(item), [person.pk])
        self.assertEqual(PhotoPerson.objects.count(), 0)
        self.assertEqual(photo.person_links.count(), 0)

    def test_remove_approval_on_photo_leaves_photo_person_untouched(self):
        item, photo = _create_photo_item(title="PHOTO both relations")
        person = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        create_archive_item_person(archive_item=item, person=person)
        self.assertEqual(
            linked_person_ids_for_archive_item(item), frozenset({person.pk})
        )
        suggestion = self._submit(
            item, person, action=ArchiveItemPersonSuggestion.Action.REMOVE
        )
        result = approve_suggestion(suggestion.pk, reviewer=self.reviewer)
        self.assertTrue(result.relationship_changed)
        self.assertEqual(_person_ids(item), [])
        self.assertEqual(PhotoPerson.objects.count(), 1)
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())
