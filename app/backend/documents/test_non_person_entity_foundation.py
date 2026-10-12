"""Model/constraint tests for the non-person entity foundation."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import CASCADE
from django.db.models.deletion import PROTECT, ProtectedError
from django.db.models.fields.related import ForeignKey
from django.test import TestCase

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    Author,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
    ReviewedNonPersonEntityDecision,
    Tag,
)

WORKBOOK_SHA = "1fc88b15e38729a3f173aa92817ce84eb27a601a45f7855ee83bf0c4e53e0145"
SOURCE = "vs_archive_non_person_final_recon_2026_10_05"


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "המשטרה המצרית",
        "entity_type": NonPersonEntity.EntityType.ORGANIZATION,
        "entity_subtype": NonPersonEntity.EntitySubtype.GOVERNMENT_BODY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _decision(**overrides) -> ReviewedNonPersonEntityDecision:
    fields = {
        "source": SOURCE,
        "candidate_id": "EC0615",
        "workbook_sha256": WORKBOOK_SHA,
        "decision": ReviewedNonPersonEntityDecision.Decision.APPROVE,
        "review_status": ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
    }
    fields.update(overrides)
    return ReviewedNonPersonEntityDecision.objects.create(**fields)


class NonPersonEntityModelTests(TestCase):
    def test_two_entities_may_share_canonical_name(self):
        first = _entity(canonical_name="קהיר")
        second = _entity(
            canonical_name="קהיר",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(
            NonPersonEntity.objects.filter(canonical_name="קהיר").count(),
            2,
        )

    def test_entity_type_choices_reject_unsupported_values(self):
        entity = NonPersonEntity(
            canonical_name="קהיר",
            entity_type="LATIN",
        )

        with self.assertRaises(ValidationError) as caught:
            entity.full_clean()

        self.assertIn("entity_type", caught.exception.message_dict)

    def test_entity_subtype_may_be_blank(self):
        entity = _entity(entity_subtype="")
        entity.full_clean()

        self.assertEqual(entity.entity_subtype, "")

    def test_approved_subtype_expansion_is_valid_and_existing_values_remain(self):
        added = {
            NonPersonEntity.EntitySubtype.MILITARY_ORGANIZATION,
            NonPersonEntity.EntitySubtype.INTERNATIONAL_ORGANIZATION,
            NonPersonEntity.EntitySubtype.POLITICAL_PARTY_OR_MOVEMENT,
            NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
            NonPersonEntity.EntitySubtype.PORT,
            NonPersonEntity.EntitySubtype.DIPLOMATIC_SITE,
            NonPersonEntity.EntitySubtype.TRIAL_OR_LEGAL_PROCEEDING,
            NonPersonEntity.EntitySubtype.POLITICAL_AFFAIR_OR_CRISIS,
        }
        preserved = {
            NonPersonEntity.EntitySubtype.GOVERNMENT_BODY,
            NonPersonEntity.EntitySubtype.CITY,
            NonPersonEntity.EntitySubtype.WAR_OR_CAMPAIGN,
            NonPersonEntity.EntitySubtype.COVERT_OPERATION,
            NonPersonEntity.EntitySubtype.MILITARY_OR_SECURITY_SITE,
            NonPersonEntity.EntitySubtype.OTHER,
        }
        self.assertTrue(added <= set(NonPersonEntity.EntitySubtype.values))
        self.assertTrue(preserved <= set(NonPersonEntity.EntitySubtype.values))
        self.assertEqual(
            NonPersonEntity._meta.get_field("entity_subtype").max_length,
            40,
        )

        for subtype in sorted(added | preserved):
            entity = _entity(
                canonical_name=f"subtype-{subtype}",
                entity_subtype=subtype,
            )
            entity.full_clean()
            entity.refresh_from_db()
            self.assertEqual(entity.entity_subtype, subtype)

    def test_named_group_taxonomy_extension_is_valid(self):
        added_subtypes = {
            NonPersonEntity.EntitySubtype.ARTICLE_OR_SERIES,
            NonPersonEntity.EntitySubtype.BROADCAST_OR_RADIO_TV,
            NonPersonEntity.EntitySubtype.CULTURAL_ORGANIZATION,
            NonPersonEntity.EntitySubtype.EXECUTION_OR_HANGING,
            NonPersonEntity.EntitySubtype.FILM_OR_VIDEO,
            NonPersonEntity.EntitySubtype.HISTORICAL_COHORT,
            NonPersonEntity.EntitySubtype.IMMIGRANT_COHORT,
            NonPersonEntity.EntitySubtype.IMMIGRATION_WAVE_OR_ALIYAH_ACTION,
            NonPersonEntity.EntitySubtype.JEWISH_COMMUNAL_BODY,
            NonPersonEntity.EntitySubtype.MEDICAL_INSTITUTION,
            NonPersonEntity.EntitySubtype.PRISONER_OR_DEFENDANT_GROUP,
            NonPersonEntity.EntitySubtype.RELIEF_ORGANIZATION,
            NonPersonEntity.EntitySubtype.RELIGIOUS_INSTITUTION,
            NonPersonEntity.EntitySubtype.RIOT_OR_POGROM,
            NonPersonEntity.EntitySubtype.SETTLEMENT_OR_KIBBUTZ,
            NonPersonEntity.EntitySubtype.TREATY_OR_AGREEMENT,
            NonPersonEntity.EntitySubtype.TREATY_OR_DIPLOMATIC_EVENT,
        }
        self.assertEqual(len(added_subtypes), 17)
        self.assertIn(
            NonPersonEntity.EntityType.NAMED_GROUP,
            NonPersonEntity.EntityType.values,
        )
        self.assertTrue(added_subtypes <= set(NonPersonEntity.EntitySubtype.values))
        self.assertLessEqual(
            max(len(value) for value in added_subtypes),
            NonPersonEntity._meta.get_field("entity_subtype").max_length,
        )
        self.assertLessEqual(
            len(NonPersonEntity.EntityType.NAMED_GROUP),
            NonPersonEntity._meta.get_field("entity_type").max_length,
        )

        named_group = _entity(
            canonical_name="הפורשים",
            entity_type=NonPersonEntity.EntityType.NAMED_GROUP,
            entity_subtype="",
        )
        named_group.full_clean()
        named_group.refresh_from_db()
        self.assertEqual(named_group.entity_subtype, "")

        cohort = _entity(
            canonical_name="עולי הגרדום בקהיר",
            entity_type=NonPersonEntity.EntityType.NAMED_GROUP,
            entity_subtype=NonPersonEntity.EntitySubtype.HISTORICAL_COHORT,
        )
        cohort.full_clean()
        cohort.refresh_from_db()
        self.assertEqual(
            cohort.entity_subtype,
            NonPersonEntity.EntitySubtype.HISTORICAL_COHORT,
        )

    def test_entity_subtype_rejects_unsupported_values(self):
        entity = NonPersonEntity(
            canonical_name="עולי הגרדום בקהיר",
            entity_type=NonPersonEntity.EntityType.COMMUNITY,
            entity_subtype="NOT_A_SUBTYPE",
        )

        with self.assertRaises(ValidationError) as caught:
            entity.full_clean()

        self.assertIn("entity_subtype", caught.exception.message_dict)

    def test_display_name_may_differ_from_canonical_name(self):
        entity = _entity(
            canonical_name="ההסתדרות הכללית של העובדים בארץ ישראל",
            display_name="ההסתדרות",
        )
        entity.full_clean()

        self.assertEqual(entity.display_name, "ההסתדרות")
        self.assertNotEqual(entity.display_name, entity.canonical_name)

    def test_display_name_may_be_blank(self):
        entity = _entity(display_name="")
        entity.full_clean()

        self.assertEqual(entity.display_name, "")
        self.assertEqual(entity.canonical_name, "המשטרה המצרית")


class NonPersonEntityAliasModelTests(TestCase):
    def test_duplicate_alias_on_the_same_entity_is_rejected(self):
        entity = _entity()
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="Egyptian Police",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NonPersonEntityAlias.objects.create(
                    entity=entity,
                    name="Egyptian Police",
                    kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
                )

        self.assertEqual(entity.aliases.count(), 1)

    def test_same_alias_string_is_allowed_on_two_entities(self):
        first = _entity(canonical_name="קהיר")
        second = _entity(
            canonical_name="אלכסנדריה",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        NonPersonEntityAlias.objects.create(
            entity=first,
            name="Caire",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        NonPersonEntityAlias.objects.create(
            entity=second,
            name="Caire",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )

        self.assertEqual(NonPersonEntityAlias.objects.filter(name="Caire").count(), 2)

    def test_allowed_alias_kinds(self):
        self.assertEqual(
            set(NonPersonEntityAlias.Kind.values),
            {
                "LANGUAGE_VARIANT",
                "OCR_VARIANT",
                "SPELLING_VARIANT",
                "TRANSLITERATION_VARIANT",
                "ABBREVIATION",
                "CURRENT_NAME",
            },
        )
        entity = _entity()
        for kind in NonPersonEntityAlias.Kind.values:
            alias = NonPersonEntityAlias(
                entity=entity,
                name=f"name-{kind}",
                kind=kind,
            )
            alias.full_clean()

    def test_unsupported_alias_kind_fails_validation(self):
        alias = NonPersonEntityAlias(
            entity=_entity(),
            name="Egyptian Police",
            kind="NICKNAME",
        )

        with self.assertRaises(ValidationError) as caught:
            alias.full_clean()

        self.assertIn("kind", caught.exception.message_dict)

    def test_deleting_entity_cascades_aliases(self):
        entity = _entity()
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="Egyptian Police",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        entity_field = NonPersonEntityAlias._meta.get_field("entity")
        self.assertIsInstance(entity_field, ForeignKey)
        self.assertIs(entity_field.remote_field.on_delete, CASCADE)

        entity.delete()

        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(NonPersonEntity.objects.count(), 0)


class ReviewedNonPersonEntityDecisionModelTests(TestCase):
    def test_source_and_candidate_id_are_unique_together(self):
        _decision()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _decision(
                    decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
                )

        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 1)
        _decision(candidate_id="EC0009")
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 2)

    def test_approve_may_point_to_result_entity(self):
        entity = _entity(canonical_name="המשטרה המצרית")
        decision = _decision(result_entity=entity)
        decision.full_clean()

        self.assertEqual(decision.result_entity_id, entity.pk)
        self.assertEqual(
            decision.decision, ReviewedNonPersonEntityDecision.Decision.APPROVE
        )

    def test_merge_can_store_merge_target_candidate_id(self):
        decision = _decision(
            candidate_id="EC0044",
            decision=ReviewedNonPersonEntityDecision.Decision.MERGE,
            merge_target_candidate_id="EC0615",
            final_canonical="המשטרה המצרית",
        )
        decision.full_clean()

        self.assertEqual(decision.merge_target_candidate_id, "EC0615")
        self.assertIsNone(decision.result_entity_id)

    def test_skip_can_exist_without_result_entity(self):
        decision = _decision(
            candidate_id="EC0183",
            decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
            result_entity=None,
            final_canonical="",
        )
        decision.full_clean()

        self.assertIsNone(decision.result_entity_id)

    def test_needs_research_can_stay_unresolved_without_entity(self):
        decision = _decision(
            candidate_id="EC0025",
            decision=ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH,
            review_status=ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED,
            result_entity=None,
        )
        decision.full_clean()

        self.assertEqual(
            decision.review_status,
            ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED,
        )
        self.assertIsNone(decision.result_entity_id)

    def test_contextual_surfaces_do_not_create_an_alias(self):
        decision = _decision(
            candidate_id="EC0009",
            decision=ReviewedNonPersonEntityDecision.Decision.SPLIT,
            contextual_surfaces="Palestine",
            note="No global alias for Palestine.",
        )

        self.assertEqual(decision.contextual_surfaces, "Palestine")
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(NonPersonEntity.objects.count(), 0)

    def test_result_entity_uses_protect(self):
        entity = _entity()
        _decision(result_entity=entity)
        result_field = ReviewedNonPersonEntityDecision._meta.get_field("result_entity")
        self.assertIsInstance(result_field, ForeignKey)
        self.assertIs(result_field.remote_field.on_delete, PROTECT)

        with self.assertRaises(ProtectedError):
            entity.delete()

        self.assertEqual(NonPersonEntity.objects.filter(pk=entity.pk).count(), 1)

    def test_creating_a_decision_does_not_create_other_identity_rows(self):
        before = _identity_counts()

        _decision(
            candidate_id="EC0183",
            decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
            contextual_surfaces="وكيل أول نيابة",
            note="NOT_INDEXABLE",
        )

        self.assertEqual(_identity_counts(), before)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), before["tag"])
        self.assertEqual(ArchiveCategory.objects.count(), before["category"])
        self.assertEqual(ArchiveEvent.objects.count(), before["event"])
        self.assertEqual(Person.objects.count(), before["person"])
        self.assertEqual(Author.objects.count(), before["author"])


def _identity_counts() -> dict[str, int]:
    return {
        "tag": Tag.objects.count(),
        "category": ArchiveCategory.objects.count(),
        "event": ArchiveEvent.objects.count(),
        "person": Person.objects.count(),
        "author": Author.objects.count(),
        "alias": NonPersonEntityAlias.objects.count(),
    }
