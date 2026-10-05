"""Schema tests for ArchiveItemEntityOccurrence."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Author,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
    ReviewedNonPersonEntityDecision,
    Tag,
)

VALID_SHA = "a" * 64
OTHER_SHA = "b" * 64


def _item(**overrides) -> ArchiveItem:
    fields = {
        "title": "Synthetic item",
        "item_type": ArchiveItem.ItemType.MANUAL_TEXT,
        "visibility": ArchiveItem.Visibility.PRIVATE,
    }
    fields.update(overrides)
    return ArchiveItem.objects.create(**fields)


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "פלסטינה",
        "entity_type": NonPersonEntity.EntityType.PLACE,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _decision(**overrides) -> ReviewedNonPersonEntityDecision:
    fields = {
        "source": "synthetic-occurrence-test",
        "candidate_id": "EC0009",
        "workbook_sha256": VALID_SHA,
        "decision": ReviewedNonPersonEntityDecision.Decision.SPLIT,
        "review_status": ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
    }
    fields.update(overrides)
    return ReviewedNonPersonEntityDecision.objects.create(**fields)


def _occurrence(**overrides) -> ArchiveItemEntityOccurrence:
    if "entity" in overrides:
        entity = overrides.pop("entity")
    else:
        entity = _entity()
    fields = {
        "archive_item": _item(),
        "text_kind": ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        "source_text_sha256": VALID_SHA,
        "normalization_version": "surface-v1",
        "normalized_surface": "palestine",
        "occurrence_ordinal": 1,
        "resolution_status": ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        "entity": entity,
        "matched_text": "Palestine",
    }
    fields.update(overrides)
    return ArchiveItemEntityOccurrence.objects.create(**fields)


def _identity_counts() -> dict[str, int]:
    return {
        "alias": NonPersonEntityAlias.objects.count(),
        "tag": Tag.objects.count(),
        "category": ArchiveCategory.objects.count(),
        "event": ArchiveEvent.objects.count(),
        "person": Person.objects.count(),
        "author": Author.objects.count(),
    }


class ArchiveItemEntityOccurrenceModelTests(TestCase):
    def test_resolved_occurrence_with_entity_is_valid(self):
        entity = _entity()
        occurrence = _occurrence(entity=entity)
        occurrence.full_clean()

        self.assertEqual(occurrence.resolution_status, "RESOLVED")
        self.assertEqual(occurrence.entity_id, entity.pk)
        self.assertEqual(occurrence.normalization_version, "surface-v1")

    def test_unresolved_occurrence_with_null_entity_is_valid(self):
        occurrence = _occurrence(
            entity=None,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED,
            matched_text="",
        )
        occurrence.full_clean()

        self.assertIsNone(occurrence.entity_id)
        self.assertEqual(occurrence.resolution_status, "UNRESOLVED")

    def test_resolved_without_entity_fails_model_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            source_text_sha256=VALID_SHA,
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=None,
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("entity", caught.exception.message_dict)

    def test_resolved_without_entity_fails_database_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemEntityOccurrence.objects.create(
                    archive_item=_item(),
                    text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
                    source_text_sha256=VALID_SHA,
                    normalized_surface="palestine",
                    occurrence_ordinal=1,
                    resolution_status=(
                        ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
                    ),
                    entity=None,
                )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_unresolved_with_entity_fails_model_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256=VALID_SHA,
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED,
            entity=_entity(),
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("entity", caught.exception.message_dict)

    def test_unresolved_with_entity_fails_database_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemEntityOccurrence.objects.create(
                    archive_item=_item(),
                    text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
                    source_text_sha256=VALID_SHA,
                    normalized_surface="palestine",
                    occurrence_ordinal=1,
                    resolution_status=(
                        ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED
                    ),
                    entity=_entity(),
                )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_ordinal_zero_is_rejected_by_model_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256=VALID_SHA,
            normalized_surface="palestine",
            occurrence_ordinal=0,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=_entity(),
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("occurrence_ordinal", caught.exception.message_dict)

    def test_ordinal_zero_is_rejected_by_database_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemEntityOccurrence.objects.create(
                    archive_item=_item(),
                    text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
                    source_text_sha256=VALID_SHA,
                    normalized_surface="palestine",
                    occurrence_ordinal=0,
                    resolution_status=(
                        ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
                    ),
                    entity=_entity(),
                )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_lowercase_sha256_passes_validation(self):
        occurrence = _occurrence(source_text_sha256=VALID_SHA)
        occurrence.full_clean()

        self.assertEqual(len(occurrence.source_text_sha256), 64)

    def test_uppercase_sha256_fails_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256="A" * 64,
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=_entity(),
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("source_text_sha256", caught.exception.message_dict)

    def test_non_hex_sha256_fails_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256="g" * 64,
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=_entity(),
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("source_text_sha256", caught.exception.message_dict)

    def test_wrong_length_sha256_fails_validation(self):
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256="a" * 63,
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=_entity(),
        )

        with self.assertRaises(ValidationError) as caught:
            occurrence.full_clean()

        self.assertIn("source_text_sha256", caught.exception.message_dict)

    def test_identity_cannot_be_inserted_twice(self):
        item = _item()
        entity = _entity()
        _occurrence(archive_item=item, entity=entity, matched_text="Palestine")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _occurrence(
                    archive_item=item,
                    entity=entity,
                    matched_text="Palestine again",
                )

        self.assertEqual(item.entity_occurrences.count(), 1)

    def test_same_surface_may_have_two_ordinals(self):
        item = _item()
        entity = _entity()
        first = _occurrence(archive_item=item, entity=entity, occurrence_ordinal=1)
        second = _occurrence(archive_item=item, entity=entity, occurrence_ordinal=2)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(item.entity_occurrences.count(), 2)

    def test_different_source_text_sha256_is_a_separate_pin(self):
        item = _item()
        entity = _entity()
        _occurrence(archive_item=item, entity=entity, source_text_sha256=VALID_SHA)
        other = _occurrence(
            archive_item=item,
            entity=entity,
            source_text_sha256=OTHER_SHA,
        )

        self.assertEqual(other.source_text_sha256, OTHER_SHA)
        self.assertEqual(item.entity_occurrences.count(), 2)

    def test_same_identity_cannot_be_duplicated_by_changing_entity(self):
        item = _item()
        first_entity = _entity(canonical_name="פלסטינה")
        second_entity = _entity(canonical_name="ארץ ישראל")
        _occurrence(archive_item=item, entity=first_entity)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _occurrence(archive_item=item, entity=second_entity)

        self.assertEqual(item.entity_occurrences.get().entity_id, first_entity.pk)

    def test_deleting_archive_item_cascades_occurrences(self):
        item = _item()
        entity = _entity()
        _occurrence(archive_item=item, entity=entity)
        item_id = item.pk

        item.delete()

        self.assertFalse(
            ArchiveItemEntityOccurrence.objects.filter(archive_item_id=item_id).exists()
        )
        self.assertTrue(NonPersonEntity.objects.filter(pk=entity.pk).exists())

    def test_deleting_referenced_entity_is_protected(self):
        entity = _entity()
        _occurrence(entity=entity)

        with self.assertRaises(ProtectedError):
            entity.delete()

        self.assertTrue(NonPersonEntity.objects.filter(pk=entity.pk).exists())

    def test_decision_may_be_null_at_schema_layer(self):
        decision_count = ReviewedNonPersonEntityDecision.objects.count()
        occurrence = _occurrence(decision=None)
        occurrence.full_clean()

        occurrence.refresh_from_db()
        self.assertIsNone(occurrence.decision_id)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.count(),
            decision_count,
        )

    def test_deleting_referenced_decision_is_protected(self):
        decision = _decision()
        _occurrence(decision=decision)

        with self.assertRaises(ProtectedError):
            decision.delete()

        self.assertTrue(
            ReviewedNonPersonEntityDecision.objects.filter(pk=decision.pk).exists()
        )

    def test_creating_occurrence_does_not_create_other_identity_rows(self):
        before = _identity_counts()

        _occurrence(
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED,
            entity=None,
            matched_text="Palestine",
        )

        self.assertEqual(_identity_counts(), before)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)

    def test_matched_text_is_not_part_of_uniqueness(self):
        item = _item()
        entity = _entity()
        _occurrence(archive_item=item, entity=entity, matched_text="Palestine")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _occurrence(archive_item=item, entity=entity, matched_text="palestine")

        self.assertEqual(item.entity_occurrences.get().matched_text, "Palestine")

    def test_normalization_version_is_part_of_uniqueness(self):
        item = _item()
        entity = _entity()
        _occurrence(
            archive_item=item,
            entity=entity,
            normalization_version="surface-v1",
        )
        other = _occurrence(
            archive_item=item,
            entity=entity,
            normalization_version="surface-v2",
        )

        self.assertEqual(other.normalization_version, "surface-v2")
        self.assertEqual(item.entity_occurrences.count(), 2)
