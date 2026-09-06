"""Controlled staff Person identity merge (explicit keeper/duplicate ids)."""

from __future__ import annotations

from unittest.mock import patch

from django.db import IntegrityError
from django.test import TestCase

from documents.historical_person_tag_map import HISTORICAL_PERSON_NAME_TAG_RECORDS
from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    Author,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.services.person_merge import (
    PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR,
    PERSON_MERGE_CONCURRENCY_ERROR,
    PERSON_MERGE_FROZEN_DUPLICATE_ERROR,
    PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR,
    PERSON_MERGE_SAME_ID_ERROR,
    BIOGRAPHY_OUTCOME_CONFLICT,
    BIOGRAPHY_OUTCOME_COPY_DUPLICATE,
    BIOGRAPHY_OUTCOME_KEEP_KEEPER,
    BIOGRAPHY_OUTCOME_UNCHANGED,
    PersonMergeError,
    merge_persons,
    preview_person_merge,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR


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


def _item(
    *, title: str, item_type: str = ArchiveItem.ItemType.MANUAL_TEXT
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=item_type,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _photo_item(*, title: str) -> tuple[ArchiveItem, PhotoContent]:
    item = _item(title=title, item_type=ArchiveItem.ItemType.PHOTO)
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=1,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item, photo


def _suggestion(
    item: ArchiveItem,
    person: Person,
    *,
    action: str = ArchiveItemPersonSuggestion.Action.ADD,
    status: str = ArchiveItemPersonSuggestion.Status.PENDING,
) -> ArchiveItemPersonSuggestion:
    return ArchiveItemPersonSuggestion.objects.create(
        archive_item=item,
        person=person,
        action=action,
        submitter_name="מציע/ה",
        status=status,
    )


class PersonMergeServiceTests(TestCase):
    def test_simple_duplicate_merge_deletes_duplicate_and_keeps_keeper(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        keeper_id = keeper.pk
        duplicate_id = duplicate.pk

        result = merge_persons(keeper_id=keeper_id, duplicate_id=duplicate_id)

        self.assertEqual(result.keeper_id, keeper_id)
        self.assertEqual(result.deleted_duplicate_id, duplicate_id)
        keeper.refresh_from_db()
        self.assertEqual(keeper.name, "Keeper")
        self.assertEqual(keeper.pk, keeper_id)
        self.assertFalse(Person.objects.filter(pk=duplicate_id).exists())

    def test_same_id_is_rejected(self):
        person = _create_ordinary_person(name="Only")
        with self.assertRaises(PersonMergeError) as ctx:
            merge_persons(keeper_id=person.pk, duplicate_id=person.pk)
        self.assertEqual(str(ctx.exception.message), PERSON_MERGE_SAME_ID_ERROR)
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())

    def test_missing_ids_are_rejected_safely(self):
        keeper = _create_ordinary_person(name="Keeper")
        with self.assertRaises(PersonMergeError) as ctx:
            merge_persons(keeper_id=keeper.pk, duplicate_id=999999)
        self.assertEqual(str(ctx.exception.message), PERSON_NOT_FOUND_ERROR)
        self.assertTrue(Person.objects.filter(pk=keeper.pk).exists())

        duplicate = _create_ordinary_person(name="Duplicate")
        with self.assertRaises(PersonMergeError):
            merge_persons(keeper_id=999998, duplicate_id=duplicate.pk)
        self.assertTrue(Person.objects.filter(pk=duplicate.pk).exists())

    def test_frozen_duplicate_is_rejected_and_frozen_keeper_is_allowed(self):
        frozen = _create_frozen_person(name="Frozen Historical")
        other = _create_ordinary_person(name="Other Person")

        with self.assertRaises(PersonMergeError) as ctx:
            merge_persons(keeper_id=other.pk, duplicate_id=frozen.pk)
        self.assertEqual(ctx.exception.message, PERSON_MERGE_FROZEN_DUPLICATE_ERROR)
        self.assertTrue(Person.objects.filter(pk=frozen.pk).exists())
        self.assertTrue(Person.objects.filter(pk=other.pk).exists())

        merge_persons(keeper_id=frozen.pk, duplicate_id=other.pk)
        frozen.refresh_from_db()
        self.assertEqual(frozen.name, "Frozen Historical")
        self.assertFalse(Person.objects.filter(pk=other.pk).exists())

    def test_archive_item_person_move_and_collision_dedupe(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        shared = _item(title="Shared")
        only_dup = _item(title="Only duplicate")
        ArchiveItemPerson.objects.create(archive_item=shared, person=keeper)
        ArchiveItemPerson.objects.create(archive_item=shared, person=duplicate)
        ArchiveItemPerson.objects.create(archive_item=only_dup, person=duplicate)

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.archive_item_person_moved, 1)
        self.assertEqual(result.archive_item_person_deduped, 1)
        linked = set(
            ArchiveItemPerson.objects.filter(person=keeper).values_list(
                "archive_item_id", flat=True
            )
        )
        self.assertEqual(linked, {shared.pk, only_dup.pk})
        self.assertEqual(ArchiveItemPerson.objects.filter(person_id=duplicate.pk).count(), 0)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=shared, person=keeper).count(),
            1,
        )

    def test_photo_person_move_and_collision_dedupe(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        shared_item, shared_photo = _photo_item(title="Shared photo item")
        only_item, only_photo = _photo_item(title="Only duplicate photo")
        PhotoPerson.objects.create(photo_content=shared_photo, person=keeper)
        PhotoPerson.objects.create(photo_content=shared_photo, person=duplicate)
        PhotoPerson.objects.create(photo_content=only_photo, person=duplicate)

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.photo_person_moved, 1)
        self.assertEqual(result.photo_person_deduped, 1)
        self.assertEqual(
            set(
                PhotoPerson.objects.filter(person=keeper).values_list(
                    "photo_content_id", flat=True
                )
            ),
            {shared_photo.pk, only_photo.pk},
        )
        self.assertEqual(PhotoPerson.objects.filter(person_id=duplicate.pk).count(), 0)
        self.assertEqual(
            PhotoPerson.objects.filter(
                photo_content=shared_photo, person=keeper
            ).count(),
            1,
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=shared_item).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=only_item).exists()
        )

    def test_no_cross_inference_between_relation_types(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        item_only = _item(title="Item only")
        photo_item, photo = _photo_item(title="Photo only")
        ArchiveItemPerson.objects.create(archive_item=item_only, person=duplicate)
        PhotoPerson.objects.create(photo_content=photo, person=duplicate)

        merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item_only, person=keeper
            ).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=keeper).exists()
        )
        self.assertFalse(
            PhotoPerson.objects.filter(
                photo_content__archive_item=item_only, person=keeper
            ).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(
                archive_item=photo_item, person=keeper
            ).exists()
        )

    def test_duplicate_canonical_name_becomes_alias(self):
        keeper = _create_ordinary_person(name="Keeper Name")
        duplicate = _create_ordinary_person(name="Duplicate Name")

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertTrue(result.canonical_name_alias_created)
        keeper.refresh_from_db()
        self.assertEqual(keeper.name, "Keeper Name")
        self.assertTrue(
            PersonAlias.objects.filter(person=keeper, name="Duplicate Name").exists()
        )

    def test_duplicate_aliases_transfer_and_collisions_dedupe(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        PersonAlias.objects.create(person=keeper, name="Shared Alias")
        PersonAlias.objects.create(person=duplicate, name="Shared Alias")
        PersonAlias.objects.create(person=duplicate, name="Moved Alias")

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.aliases_moved, 1)
        self.assertEqual(result.aliases_deduped, 1)
        names = set(
            PersonAlias.objects.filter(person=keeper).values_list("name", flat=True)
        )
        self.assertEqual(names, {"Shared Alias", "Moved Alias", "Duplicate"})
        self.assertEqual(PersonAlias.objects.filter(name="Shared Alias").count(), 1)

    def test_alias_equal_to_keeper_canonical_is_skipped(self):
        keeper = _create_ordinary_person(name="Same Display")
        duplicate = _create_ordinary_person(name="Other")
        PersonAlias.objects.create(person=duplicate, name="Same Display")

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.aliases_skipped_canonical, 1)
        self.assertFalse(
            PersonAlias.objects.filter(person=keeper, name="Same Display").exists()
        )
        keeper.refresh_from_db()
        self.assertEqual(keeper.name, "Same Display")

    def test_biography_copy_when_keeper_empty(self):
        keeper = _create_ordinary_person(name="Keeper", biography="")
        duplicate = _create_ordinary_person(name="Duplicate", biography="  Copied bio  ")

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertTrue(result.biography_copied)
        keeper.refresh_from_db()
        self.assertEqual(keeper.biography, "Copied bio")

    def test_keeper_nonempty_biography_is_kept_when_duplicate_empty(self):
        keeper = _create_ordinary_person(name="Keeper", biography="Keep this")
        duplicate = _create_ordinary_person(name="Duplicate", biography="  ")
        merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
        keeper.refresh_from_db()
        self.assertEqual(keeper.biography, "Keep this")

    def test_different_nonempty_biographies_fail_with_zero_writes(self):
        keeper = _create_ordinary_person(name="Keeper", biography="Alpha")
        duplicate = _create_ordinary_person(name="Duplicate", biography="Beta")
        PersonAlias.objects.create(person=duplicate, name="Should Stay")
        item = _item(title="Linked")
        ArchiveItemPerson.objects.create(archive_item=item, person=duplicate)

        with self.assertRaises(PersonMergeError) as ctx:
            merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
        self.assertEqual(ctx.exception.message, PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR)

        keeper.refresh_from_db()
        duplicate.refresh_from_db()
        self.assertEqual(keeper.biography, "Alpha")
        self.assertEqual(duplicate.biography, "Beta")
        self.assertTrue(Person.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(
            PersonAlias.objects.filter(person=duplicate, name="Should Stay").exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=duplicate
            ).exists()
        )
        self.assertFalse(PersonAlias.objects.filter(person=keeper).exists())

    def test_suggestions_repoint_and_reviewed_are_preserved(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        item = _item(title="Suggestion item")
        pending = _suggestion(item, duplicate)
        approved = _suggestion(
            item,
            duplicate,
            action=ArchiveItemPersonSuggestion.Action.REMOVE,
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
        )
        rejected = _suggestion(
            _item(title="Other item"),
            duplicate,
            status=ArchiveItemPersonSuggestion.Status.REJECTED,
        )

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.suggestions_repointed, 3)
        pending.refresh_from_db()
        approved.refresh_from_db()
        rejected.refresh_from_db()
        self.assertEqual(pending.person_id, keeper.pk)
        self.assertEqual(approved.person_id, keeper.pk)
        self.assertEqual(rejected.person_id, keeper.pk)
        self.assertEqual(pending.status, ArchiveItemPersonSuggestion.Status.PENDING)
        self.assertEqual(approved.status, ArchiveItemPersonSuggestion.Status.APPROVED)
        self.assertEqual(rejected.status, ArchiveItemPersonSuggestion.Status.REJECTED)

    def test_pending_suggestion_collision_fails_with_zero_writes(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        item = _item(title="Conflict item")
        keeper_pending = _suggestion(item, keeper)
        duplicate_pending = _suggestion(item, duplicate)
        PersonAlias.objects.create(person=duplicate, name="Unmoved")

        with self.assertRaises(PersonMergeError) as ctx:
            merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
        self.assertEqual(
            ctx.exception.message, PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR
        )

        keeper_pending.refresh_from_db()
        duplicate_pending.refresh_from_db()
        self.assertEqual(keeper_pending.person_id, keeper.pk)
        self.assertEqual(duplicate_pending.person_id, duplicate.pk)
        self.assertTrue(Person.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(
            PersonAlias.objects.filter(person=duplicate, name="Unmoved").exists()
        )

    def test_reviewed_pending_mix_for_same_item_action_is_allowed(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        item = _item(title="Reviewed mix")
        _suggestion(
            item,
            keeper,
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
        )
        pending = _suggestion(item, duplicate)

        merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        pending.refresh_from_db()
        self.assertEqual(pending.person_id, keeper.pk)
        self.assertEqual(
            ArchiveItemPersonSuggestion.objects.filter(
                archive_item=item, person=keeper
            ).count(),
            2,
        )

    def test_search_refresh_uses_pre_merge_union_and_refreshes_each_once(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        shared = _item(title="Shared item")
        dup_only = _item(title="Duplicate item")
        photo_item, photo = _photo_item(title="Keeper photo")
        ArchiveItemPerson.objects.create(archive_item=shared, person=keeper)
        ArchiveItemPerson.objects.create(archive_item=shared, person=duplicate)
        ArchiveItemPerson.objects.create(archive_item=dup_only, person=duplicate)
        PhotoPerson.objects.create(photo_content=photo, person=keeper)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as mocked:
            merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        called_ids = [call.args[0] for call in mocked.call_args_list]
        self.assertEqual(called_ids, sorted({shared.pk, dup_only.pk, photo_item.pk}))
        self.assertEqual(len(called_ids), len(set(called_ids)))

    def test_failure_during_search_refresh_rolls_back_every_change(self):
        keeper = _create_ordinary_person(name="Keeper", biography="")
        duplicate = _create_ordinary_person(name="Duplicate", biography="Bio")
        PersonAlias.objects.create(person=duplicate, name="DupAlias")
        item = _item(title="Linked")
        ArchiveItemPerson.objects.create(archive_item=item, person=duplicate)
        suggestion = _suggestion(item, duplicate)
        author = Author.objects.create(name="Linked bibliographic", person=duplicate)

        with patch(
            "documents.services.person_merge.sync_archive_item_search_indexes",
            side_effect=RuntimeError("index failed"),
        ):
            with self.assertRaises(RuntimeError):
                merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        duplicate.refresh_from_db()
        keeper.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual(keeper.biography, "")
        self.assertEqual(duplicate.biography, "Bio")
        self.assertEqual(duplicate.name, "Duplicate")
        self.assertTrue(
            PersonAlias.objects.filter(person=duplicate, name="DupAlias").exists()
        )
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=duplicate
            ).exists()
        )
        self.assertEqual(suggestion.person_id, duplicate.pk)
        self.assertFalse(PersonAlias.objects.filter(person=keeper).exists())
        author.refresh_from_db()
        self.assertEqual(author.person_id, duplicate.pk)

    def test_integrity_error_after_mutation_rolls_back_and_raises_concurrency_error(
        self,
    ):
        keeper = _create_ordinary_person(name="Keeper", biography="")
        duplicate = _create_ordinary_person(name="Duplicate", biography="Bio")
        PersonAlias.objects.create(person=duplicate, name="DupAlias")
        item = _item(title="Linked")
        ArchiveItemPerson.objects.create(archive_item=item, person=duplicate)
        _photo_item_row, photo = _photo_item(title="Photo linked")
        PhotoPerson.objects.create(photo_content=photo, person=duplicate)
        suggestion = _suggestion(item, duplicate)

        with patch(
            "documents.services.person_merge._merge_photo_people",
            side_effect=IntegrityError("forced uniqueness collision"),
        ):
            with self.assertRaises(PersonMergeError) as ctx:
                merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(ctx.exception.message, PERSON_MERGE_CONCURRENCY_ERROR)
        self.assertTrue(Person.objects.filter(pk=keeper.pk).exists())
        self.assertTrue(Person.objects.filter(pk=duplicate.pk).exists())
        keeper.refresh_from_db()
        duplicate.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual(keeper.biography, "")
        self.assertEqual(duplicate.biography, "Bio")
        self.assertTrue(
            PersonAlias.objects.filter(person=duplicate, name="DupAlias").exists()
        )
        self.assertFalse(PersonAlias.objects.filter(person=keeper).exists())
        self.assertTrue(
            ArchiveItemPerson.objects.filter(
                archive_item=item, person=duplicate
            ).exists()
        )
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=keeper).exists()
        )
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=duplicate).exists()
        )
        self.assertEqual(suggestion.person_id, duplicate.pk)

    def test_author_rows_are_untouched(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        author = Author.objects.create(name="Separate Author")
        item = _item(title="Authored")
        ArchiveItemAuthor.objects.create(archive_item=item, author=author, position=0)

        merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        author.refresh_from_db()
        self.assertEqual(Author.objects.count(), 1)
        self.assertEqual(ArchiveItemAuthor.objects.count(), 1)
        self.assertEqual(author.name, "Separate Author")
        self.assertIsNone(author.person_id)

    def test_explicit_author_identities_on_duplicate_are_repointed(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate")
        same_name_decoy = _create_ordinary_person(name="Duplicate")
        first = Author.objects.create(name="First bibliographic")
        second = Author.objects.create(name="Second bibliographic")
        unlinked = Author.objects.create(name="Duplicate")
        first.person = duplicate
        first.save(update_fields=["person"])
        second.person = duplicate
        second.save(update_fields=["person"])

        preview = preview_person_merge(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
        self.assertEqual(preview.author_identity_count_duplicate, 2)
        self.assertEqual(preview.author_identity_count_keeper, 0)
        self.assertEqual(
            [identity.author_id for identity in preview.duplicate_author_identities],
            [first.pk, second.pk],
        )

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.author_identities_repointed, 2)
        first.refresh_from_db()
        second.refresh_from_db()
        unlinked.refresh_from_db()
        self.assertEqual(first.person_id, keeper.pk)
        self.assertEqual(second.person_id, keeper.pk)
        self.assertIsNone(unlinked.person_id)
        self.assertEqual(keeper.author_identities.count(), 2)
        self.assertFalse(Person.objects.filter(pk=duplicate.pk).exists())
        self.assertTrue(Person.objects.filter(pk=same_name_decoy.pk).exists())
        self.assertEqual(same_name_decoy.author_identities.count(), 0)

    def test_preview_is_read_only_and_reports_outcomes(self):
        keeper = _create_ordinary_person(name="Keeper", biography="Alpha")
        duplicate = _create_ordinary_person(name="Duplicate", biography="Beta")
        PersonAlias.objects.create(person=keeper, name="KAlias")
        PersonAlias.objects.create(person=duplicate, name="DAlias")
        item = _item(title="Linked")
        ArchiveItemPerson.objects.create(archive_item=item, person=duplicate)
        _suggestion(item, duplicate)

        preview = preview_person_merge(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(preview.keeper_id, keeper.pk)
        self.assertEqual(preview.duplicate_id, duplicate.pk)
        self.assertEqual(preview.keeper_name, "Keeper")
        self.assertEqual(preview.duplicate_name, "Duplicate")
        self.assertEqual(preview.keeper_aliases, ("KAlias",))
        self.assertEqual(preview.duplicate_aliases, ("DAlias",))
        self.assertEqual(preview.duplicate_canonical_becomes_alias, "Duplicate")
        self.assertEqual(preview.archive_item_person_count_duplicate, 1)
        self.assertEqual(preview.suggestion_count, 1)
        self.assertEqual(preview.pending_suggestion_count, 1)
        self.assertEqual(preview.biography_outcome, BIOGRAPHY_OUTCOME_CONFLICT)
        self.assertFalse(preview.duplicate_is_frozen)
        self.assertEqual(preview.affected_archive_item_count, 1)
        self.assertFalse(preview.can_execute)
        self.assertTrue(Person.objects.filter(pk=duplicate.pk).exists())
        self.assertEqual(duplicate.aliases.count(), 1)

        empty_keeper = _create_ordinary_person(name="EmptyBio")
        nonempty = _create_ordinary_person(name="HasBio", biography="Text")
        self.assertEqual(
            preview_person_merge(
                keeper_id=empty_keeper.pk, duplicate_id=nonempty.pk
            ).biography_outcome,
            BIOGRAPHY_OUTCOME_COPY_DUPLICATE,
        )
        self.assertEqual(
            preview_person_merge(
                keeper_id=nonempty.pk, duplicate_id=empty_keeper.pk
            ).biography_outcome,
            BIOGRAPHY_OUTCOME_KEEP_KEEPER,
        )
        both_empty_dup = _create_ordinary_person(name="AlsoEmpty")
        self.assertEqual(
            preview_person_merge(
                keeper_id=empty_keeper.pk, duplicate_id=both_empty_dup.pk
            ).biography_outcome,
            BIOGRAPHY_OUTCOME_UNCHANGED,
        )


class PersonMergeParseAndFrozenPreviewTests(TestCase):
    def test_preview_marks_frozen_duplicate(self):
        frozen = _create_frozen_person(name="Frozen")
        keeper = _create_ordinary_person(name="Keeper")
        preview = preview_person_merge(keeper_id=keeper.pk, duplicate_id=frozen.pk)
        self.assertTrue(preview.duplicate_is_frozen)
        self.assertFalse(preview.can_execute)
        self.assertIn(PERSON_MERGE_FROZEN_DUPLICATE_ERROR, preview.blockers)
        self.assertTrue(Person.objects.filter(pk=frozen.pk).exists())
