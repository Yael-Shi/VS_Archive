"""Reviewed PhotoPerson import v1: binding idempotency, ADD-only, fail-closed."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    ReviewedPersonImportBinding,
)
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.services.person_merge import merge_persons
from documents.test_person_merge import _create_ordinary_person
from documents.services.photo_person_reviewed_import import (
    CANONICAL_NAME_STALE_ERROR,
    CREATE_PERSON_CANDIDATES_ERROR,
    NON_PHOTO_PARENT_ERROR,
    OPERATION_ID_DUPLICATE_ERROR,
    PHOTO_BINDING_ERROR,
    PHOTO_NOT_RENDERABLE_ERROR,
    ReviewedPhotoPersonImportError,
    apply_reviewed_photo_person_import,
    plan_reviewed_photo_person_import,
)


def _photo_item(*, title: str = "Album") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _photo(
    archive_item: ArchiveItem,
    *,
    position: int = 1,
    key: str = "photos/test/original.jpg",
    upload_status: str = PhotoContent.UploadStatus.UPLOADED,
    people_present: str = "free text names",
) -> PhotoContent:
    return PhotoContent.objects.create(
        archive_item=archive_item,
        position=position,
        original_file_key=key,
        original_filename="scan.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=upload_status,
        people_present=people_present,
    )


def _artifact(*operations) -> dict:
    return {
        "schema": "photo-person-reviewed-import-v1",
        "source": "test",
        "operations": list(operations),
    }


class ReviewedPhotoPersonImportTests(TestCase):
    def test_existing_person_photo_person_add(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada Lovelace")
        payload = _artifact(
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "person_id": person.pk,
                "expected_canonical_name": "Ada Lovelace",
            }
        )
        result = apply_reviewed_photo_person_import(payload)
        self.assertEqual(result.add_count, 1)
        self.assertTrue(
            PhotoPerson.objects.filter(photo_content=photo, person=person).exists()
        )
        self.assertEqual(ArchiveItemPerson.objects.count(), 0)

    def test_create_person_and_local_ref_photo_person(self):
        item = _photo_item()
        photo = _photo(item)
        payload = _artifact(
            {
                "id": "create-amos",
                "op": "create_person",
                "local_person_ref": "p-amos",
                "canonical_name": "עמוס עוז",
            },
            {
                "id": "pp-amos",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-amos",
                "observed_people_present": "ignored",
            },
        )
        result = apply_reviewed_photo_person_import(payload)
        self.assertEqual(result.create_count, 1)
        self.assertEqual(result.add_count, 1)
        person = Person.objects.get(name="עמוס עוז")
        binding = ReviewedPersonImportBinding.objects.get(operation_id="create-amos")
        self.assertEqual(binding.person_id, person.pk)
        self.assertEqual(
            PhotoPerson.objects.get(photo_content=photo).person_id, person.pk
        )
        self.assertEqual(result.operations[1].person_id, person.pk)

    def test_repeated_apply_resolves_create_person_through_binding_only(self):
        item = _photo_item()
        photo = _photo(item)
        payload = _artifact(
            {
                "id": "create-amos",
                "op": "create_person",
                "local_person_ref": "p-amos",
                "canonical_name": "עמוס עוז",
            },
            {
                "id": "pp-amos",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-amos",
            },
        )
        apply_reviewed_photo_person_import(payload)
        Person.objects.create(name="עמוס עוז")
        person_count = Person.objects.count()
        bound = ReviewedPersonImportBinding.objects.get(operation_id="create-amos")
        result = apply_reviewed_photo_person_import(payload)
        self.assertEqual(result.create_count, 0)
        self.assertEqual(result.noop_count, 2)
        self.assertEqual(Person.objects.count(), person_count)
        self.assertEqual(
            ReviewedPersonImportBinding.objects.get(operation_id="create-amos").person_id,
            bound.person_id,
        )
        self.assertEqual(
            PhotoPerson.objects.filter(person_id=bound.person_id).count(), 1
        )

    def test_same_name_unrelated_person_cannot_satisfy_reapply(self):
        unrelated = Person.objects.create(name="Same Name")
        apply_reviewed_photo_person_import(
            _artifact(
                {
                    "id": "create-same",
                    "op": "create_person",
                    "local_person_ref": "p-new",
                    "canonical_name": "Same Name Unique Import",
                }
            )
        )
        bound = ReviewedPersonImportBinding.objects.get(operation_id="create-same")
        Person.objects.filter(pk=bound.person_id).update(name="Same Name")
        result = apply_reviewed_photo_person_import(
            _artifact(
                {
                    "id": "create-same",
                    "op": "create_person",
                    "local_person_ref": "p-new",
                    "canonical_name": "Same Name",
                }
            )
        )
        self.assertEqual(result.operations[0].person_id, bound.person_id)
        self.assertNotEqual(result.operations[0].person_id, unrelated.pk)
        self.assertEqual(result.noop_count, 1)

    def test_binding_canonical_stale_mismatch_fails_closed(self):
        apply_reviewed_photo_person_import(
            _artifact(
                {
                    "id": "create-1",
                    "op": "create_person",
                    "local_person_ref": "p-1",
                    "canonical_name": "Original Name",
                }
            )
        )
        bound = ReviewedPersonImportBinding.objects.get(operation_id="create-1")
        Person.objects.filter(pk=bound.person_id).update(name="Renamed")
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            apply_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "create-1",
                        "op": "create_person",
                        "local_person_ref": "p-1",
                        "canonical_name": "Original Name",
                    }
                )
            )
        self.assertEqual(str(ctx.exception), CANONICAL_NAME_STALE_ERROR)
        self.assertEqual(Person.objects.get(pk=bound.person_id).name, "Renamed")

    def test_duplicate_operation_ids_fail_closed(self):
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "dup",
                        "op": "create_person",
                        "local_person_ref": "a",
                        "canonical_name": "A",
                    },
                    {
                        "id": "dup",
                        "op": "create_person",
                        "local_person_ref": "b",
                        "canonical_name": "B",
                    },
                )
            )
        self.assertEqual(str(ctx.exception), OPERATION_ID_DUPLICATE_ERROR)

    def test_conflicting_local_refs_fail_closed(self):
        with self.assertRaises(ReviewedPhotoPersonImportError):
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "c1",
                        "op": "create_person",
                        "local_person_ref": "same",
                        "canonical_name": "A",
                    },
                    {
                        "id": "c2",
                        "op": "create_person",
                        "local_person_ref": "same",
                        "canonical_name": "B",
                    },
                )
            )

    def test_missing_person_and_unknown_local_ref_fail(self):
        item = _photo_item()
        photo = _photo(item)
        with self.assertRaises(ReviewedPhotoPersonImportError):
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-1",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": photo.original_file_key,
                        "person_id": 999999,
                        "expected_canonical_name": "Missing",
                    }
                )
            )
        with self.assertRaises(ReviewedPhotoPersonImportError):
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-2",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": photo.original_file_key,
                        "local_person_ref": "no-such-ref",
                    }
                )
            )

    def test_duplicate_new_person_candidate_fails_before_writes(self):
        Person.objects.create(name="Existing")
        before_people = Person.objects.count()
        before_bindings = ReviewedPersonImportBinding.objects.count()
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            apply_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "create-existing",
                        "op": "create_person",
                        "local_person_ref": "p-x",
                        "canonical_name": "Existing",
                    }
                )
            )
        self.assertEqual(str(ctx.exception), CREATE_PERSON_CANDIDATES_ERROR)
        self.assertEqual(Person.objects.count(), before_people)
        self.assertEqual(ReviewedPersonImportBinding.objects.count(), before_bindings)

    def test_alias_add_and_noop(self):
        person = Person.objects.create(name="Canonical")
        payload = _artifact(
            {
                "id": "alias-1",
                "op": "add_alias",
                "person_id": person.pk,
                "expected_canonical_name": "Canonical",
                "alias_name": "Also Known",
            }
        )
        first = apply_reviewed_photo_person_import(payload)
        self.assertEqual(first.add_count, 1)
        self.assertTrue(
            PersonAlias.objects.filter(person=person, name="Also Known").exists()
        )
        second = apply_reviewed_photo_person_import(payload)
        self.assertEqual(second.noop_count, 1)
        self.assertEqual(PersonAlias.objects.filter(person=person).count(), 1)

    def test_photo_person_noop_and_stale_name(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        payload = _artifact(
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "person_id": person.pk,
                "expected_canonical_name": "Ada",
            }
        )
        result = apply_reviewed_photo_person_import(payload)
        self.assertEqual(result.noop_count, 1)
        self.assertEqual(PhotoPerson.objects.count(), 1)
        with self.assertRaises(ReviewedPhotoPersonImportError):
            apply_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-2",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": photo.original_file_key,
                        "person_id": person.pk,
                        "expected_canonical_name": "ADA",
                    }
                )
            )

    def test_stale_original_file_key(self):
        item = _photo_item()
        photo = _photo(item, key="photos/real.jpg")
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-1",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": "photos/other.jpg",
                        "person_id": person.pk,
                        "expected_canonical_name": "Ada",
                    }
                )
            )
        self.assertEqual(str(ctx.exception), PHOTO_BINDING_ERROR)

    def test_wrong_item_photo_pair(self):
        first = _photo_item(title="First")
        second = _photo_item(title="Second")
        photo = _photo(second, key="photos/second.jpg")
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-1",
                        "op": "add_photo_person",
                        "archive_item_id": first.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": photo.original_file_key,
                        "person_id": person.pk,
                        "expected_canonical_name": "Ada",
                    }
                )
            )
        self.assertEqual(str(ctx.exception), PHOTO_BINDING_ERROR)

    def test_non_photo_parent_rejected(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Note",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo_item = _photo_item()
        photo = _photo(photo_item)
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-1",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": photo.pk,
                        "expected_original_file_key": photo.original_file_key,
                        "person_id": person.pk,
                        "expected_canonical_name": "Ada",
                    }
                )
            )
        self.assertIn(str(ctx.exception), {PHOTO_BINDING_ERROR, NON_PHOTO_PARENT_ERROR})

    def test_non_renderable_photo_rejected(self):
        item = _photo_item()
        pending = _photo(item, upload_status=PhotoContent.UploadStatus.PENDING)
        person = Person.objects.create(name="Ada")
        with self.assertRaises(ReviewedPhotoPersonImportError) as ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-1",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": pending.pk,
                        "expected_original_file_key": pending.original_file_key,
                        "person_id": person.pk,
                        "expected_canonical_name": "Ada",
                    }
                )
            )
        self.assertEqual(str(ctx.exception), PHOTO_NOT_RENDERABLE_ERROR)

        failed = _photo(
            item,
            position=2,
            key="photos/failed.jpg",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )
        with self.assertRaises(ReviewedPhotoPersonImportError) as failed_ctx:
            plan_reviewed_photo_person_import(
                _artifact(
                    {
                        "id": "pp-2",
                        "op": "add_photo_person",
                        "archive_item_id": item.pk,
                        "photo_content_id": failed.pk,
                        "expected_original_file_key": failed.original_file_key,
                        "person_id": person.pk,
                        "expected_canonical_name": "Ada",
                    }
                )
            )
        self.assertEqual(str(failed_ctx.exception), PHOTO_NOT_RENDERABLE_ERROR)

    def test_no_aip_writes_and_people_present_unchanged(self):
        item = _photo_item()
        photo = _photo(item, people_present="שלמה")
        ArchiveItemPerson.objects.create(
            archive_item=item, person=Person.objects.create(name="Item Person")
        )
        aip_ids = set(ArchiveItemPerson.objects.values_list("pk", flat=True))
        payload = _artifact(
            {
                "id": "create-1",
                "op": "create_person",
                "local_person_ref": "p-1",
                "canonical_name": "Photo Only",
            },
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-1",
            },
        )
        apply_reviewed_photo_person_import(payload)
        photo.refresh_from_db()
        self.assertEqual(photo.people_present, "שלמה")
        self.assertEqual(
            set(ArchiveItemPerson.objects.values_list("pk", flat=True)), aip_ids
        )

    def test_dry_run_zero_writes_including_bindings(self):
        item = _photo_item()
        photo = _photo(item)
        payload = _artifact(
            {
                "id": "create-1",
                "op": "create_person",
                "local_person_ref": "p-1",
                "canonical_name": "New Person",
            },
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-1",
            },
        )
        counts = (
            Person.objects.count(),
            ReviewedPersonImportBinding.objects.count(),
            PersonAlias.objects.count(),
            PhotoPerson.objects.count(),
            ArchiveItemPerson.objects.count(),
        )
        plan = plan_reviewed_photo_person_import(payload)
        self.assertEqual(plan.create_count, 1)
        self.assertEqual(plan.add_count, 1)
        self.assertFalse(plan.applied)
        self.assertEqual(
            (
                Person.objects.count(),
                ReviewedPersonImportBinding.objects.count(),
                PersonAlias.objects.count(),
                PhotoPerson.objects.count(),
                ArchiveItemPerson.objects.count(),
            ),
            counts,
        )

    def test_apply_rollback_includes_person_binding_alias_and_photo_person(self):
        item = _photo_item()
        photo = _photo(item)
        payload = _artifact(
            {
                "id": "create-1",
                "op": "create_person",
                "local_person_ref": "p-1",
                "canonical_name": "Rollback Person",
            },
            {
                "id": "alias-1",
                "op": "add_alias",
                "local_person_ref": "p-1",
                "alias_name": "RB",
            },
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-1",
            },
        )
        with patch.object(PhotoPerson.objects, "create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                apply_reviewed_photo_person_import(payload)
        self.assertFalse(Person.objects.filter(name="Rollback Person").exists())
        self.assertEqual(ReviewedPersonImportBinding.objects.count(), 0)
        self.assertEqual(PersonAlias.objects.count(), 0)
        self.assertEqual(PhotoPerson.objects.count(), 0)

    def test_search_index_refresh_only_for_affected_items(self):
        first = _photo_item(title="First")
        second = _photo_item(title="Second")
        photo = _photo(first, key="photos/first.jpg")
        _photo(second, position=1, key="photos/second.jpg")
        person = Person.objects.create(name="Indexed Person")
        sync_archive_item_search_index(first.pk)
        sync_archive_item_search_index(second.pk)
        refreshed: list[list[int]] = []

        def _capture(ids):
            refreshed.append(list(ids))
            return []

        payload = _artifact(
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": first.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "person_id": person.pk,
                "expected_canonical_name": "Indexed Person",
            }
        )
        with patch(
            "documents.services.photo_person_reviewed_import.sync_archive_item_search_indexes",
            side_effect=_capture,
        ):
            apply_reviewed_photo_person_import(payload)
        self.assertEqual(refreshed, [[first.pk]])

    def test_pure_reapply_noop_does_not_refresh(self):
        item = _photo_item()
        photo = _photo(item)
        person = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        payload = _artifact(
            {
                "id": "pp-1",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "person_id": person.pk,
                "expected_canonical_name": "Ada",
            }
        )
        with patch(
            "documents.services.photo_person_reviewed_import.sync_archive_item_search_indexes"
        ) as mocked:
            apply_reviewed_photo_person_import(payload)
        mocked.assert_not_called()

    def test_command_dry_run_and_apply(self):
        item = _photo_item()
        photo = _photo(item)
        payload = _artifact(
            {
                "id": "create-cmd",
                "op": "create_person",
                "local_person_ref": "p-cmd",
                "canonical_name": "Command Person",
            },
            {
                "id": "pp-cmd",
                "op": "add_photo_person",
                "archive_item_id": item.pk,
                "photo_content_id": photo.pk,
                "expected_original_file_key": photo.original_file_key,
                "local_person_ref": "p-cmd",
            },
        )
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            path = Path(handle.name)
        try:
            call_command("import_reviewed_photo_people", str(path))
            self.assertFalse(Person.objects.filter(name="Command Person").exists())
            call_command("import_reviewed_photo_people", str(path), apply=True)
            self.assertTrue(Person.objects.filter(name="Command Person").exists())
        finally:
            path.unlink(missing_ok=True)

    def test_command_error_on_invalid_artifact(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("{}")
            path = Path(handle.name)
        try:
            with self.assertRaises(CommandError):
                call_command("import_reviewed_photo_people", str(path), apply=True)
        finally:
            path.unlink(missing_ok=True)

    def test_merge_repoints_import_binding(self):
        keeper = _create_ordinary_person(name="Keeper")
        duplicate = _create_ordinary_person(name="Duplicate Import")
        ReviewedPersonImportBinding.objects.create(
            operation_id="create-dup",
            person=duplicate,
        )
        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
        self.assertEqual(result.import_bindings_repointed, 1)
        self.assertEqual(
            ReviewedPersonImportBinding.objects.get(operation_id="create-dup").person_id,
            keeper.pk,
        )
        self.assertFalse(Person.objects.filter(pk=duplicate.pk).exists())
