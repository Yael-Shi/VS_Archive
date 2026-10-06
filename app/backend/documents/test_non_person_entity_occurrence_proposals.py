"""Schema tests for non-person occurrence proposal persistence."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import (
    CASCADE,
    PROTECT,
    RESTRICT,
    SET_NULL,
    ProtectedError,
    RestrictedError,
)
from django.test import TestCase
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
    ReviewedNonPersonEntityDecision,
)

VALID_SHA = "a" * 64
OTHER_SHA = "b" * 64


def _item() -> ArchiveItem:
    return ArchiveItem.objects.create(
        title="Synthetic item",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _entity(name: str = "פלסטינה") -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
    )


def _proposal(**overrides) -> NonPersonEntityOccurrenceProposal:
    fields = {
        "archive_item": _item(),
        "text_kind": ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        "source_text_sha256": VALID_SHA,
        "normalization_version": "surface-v1",
        "normalized_surface": "palestine",
        "occurrence_ordinal": 1,
        "matched_text": "Palestine",
    }
    fields.update(overrides)
    return NonPersonEntityOccurrenceProposal.objects.create(**fields)


def _candidate(proposal=None, entity=None, **overrides):
    fields = {
        "proposal": proposal or _proposal(),
        "candidate_entity": entity or _entity(),
        "status": NonPersonEntityOccurrenceCandidate.Status.PENDING,
    }
    fields.update(overrides)
    return NonPersonEntityOccurrenceCandidate.objects.create(**fields)


class NonPersonEntityOccurrenceProposalModelTests(TestCase):
    def test_proposal_can_be_created(self):
        proposal = _proposal()
        proposal.full_clean()

        self.assertEqual(proposal.normalized_surface, "palestine")
        self.assertEqual(proposal.occurrence_ordinal, 1)
        self.assertEqual(proposal.candidates.count(), 0)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)

    def test_duplicate_six_field_identity_is_rejected(self):
        item = _item()
        _proposal(archive_item=item, matched_text="Palestine")

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _proposal(archive_item=item, matched_text="Palestine again")

        self.assertEqual(item.entity_occurrence_proposals.count(), 1)

    def test_same_surface_different_ordinal_is_allowed(self):
        item = _item()
        first = _proposal(archive_item=item, occurrence_ordinal=1)
        second = _proposal(archive_item=item, occurrence_ordinal=2)

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(item.entity_occurrence_proposals.count(), 2)

    def test_same_surface_and_ordinal_with_different_source_sha_is_allowed(self):
        item = _item()
        _proposal(archive_item=item, source_text_sha256=VALID_SHA)
        other = _proposal(archive_item=item, source_text_sha256=OTHER_SHA)

        self.assertEqual(other.source_text_sha256, OTHER_SHA)
        self.assertEqual(item.entity_occurrence_proposals.count(), 2)

    def test_ordinal_below_one_is_rejected(self):
        proposal = NonPersonEntityOccurrenceProposal(
            archive_item=_item(),
            text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            source_text_sha256=VALID_SHA,
            normalized_surface="palestine",
            occurrence_ordinal=0,
        )

        with self.assertRaises(ValidationError) as caught:
            proposal.full_clean()

        self.assertIn("occurrence_ordinal", caught.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NonPersonEntityOccurrenceProposal.objects.create(
                    archive_item=_item(),
                    text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
                    source_text_sha256=VALID_SHA,
                    normalized_surface="palestine",
                    occurrence_ordinal=0,
                )

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_invalid_sha_is_rejected_by_model_validation(self):
        item = _item()
        for bad_sha in ("A" * 64, "g" * 64, "a" * 63):
            proposal = NonPersonEntityOccurrenceProposal(
                archive_item=item,
                text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
                source_text_sha256=bad_sha,
                normalized_surface="palestine",
                occurrence_ordinal=1,
            )
            with self.assertRaises(ValidationError) as caught:
                proposal.full_clean()
            self.assertIn("source_text_sha256", caught.exception.message_dict)

        valid = _proposal(archive_item=item, source_text_sha256=VALID_SHA)
        valid.full_clean()
        self.assertEqual(len(valid.source_text_sha256), 64)


class NonPersonEntityOccurrenceCandidateModelTests(TestCase):
    def test_one_proposal_may_have_two_candidate_entities(self):
        proposal = _proposal()
        first = _candidate(proposal=proposal, entity=_entity("פלסטינה"))
        second = _candidate(proposal=proposal, entity=_entity("ארץ ישראל"))

        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(proposal.candidates.count(), 2)

    def test_duplicate_proposal_and_candidate_entity_is_rejected(self):
        proposal = _proposal()
        entity = _entity()
        _candidate(
            proposal=proposal,
            entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.REJECTED,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                _candidate(
                    proposal=proposal,
                    entity=entity,
                    status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
                )

        stored = proposal.candidates.get()
        self.assertEqual(
            stored.status, NonPersonEntityOccurrenceCandidate.Status.REJECTED
        )

    def test_same_entity_may_be_a_candidate_on_another_proposal(self):
        entity = _entity()
        first = _candidate(entity=entity)
        second = _candidate(entity=entity)

        self.assertNotEqual(first.proposal_id, second.proposal_id)
        self.assertEqual(
            NonPersonEntityOccurrenceCandidate.objects.filter(
                candidate_entity=entity
            ).count(),
            2,
        )

    def test_pending_candidate_is_valid(self):
        candidate = _candidate()
        candidate.full_clean()

        self.assertEqual(
            candidate.status, NonPersonEntityOccurrenceCandidate.Status.PENDING
        )
        self.assertIsNone(candidate.resolved_entity_id)

    def test_needs_research_candidate_is_valid(self):
        candidate = _candidate(
            status=NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
        )
        candidate.full_clean()

        self.assertEqual(
            candidate.status,
            NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH,
        )
        self.assertIsNone(candidate.resolved_entity_id)

    def test_rejected_candidate_is_valid(self):
        candidate = _candidate(
            status=NonPersonEntityOccurrenceCandidate.Status.REJECTED
        )
        candidate.full_clean()

        self.assertEqual(
            candidate.status, NonPersonEntityOccurrenceCandidate.Status.REJECTED
        )
        self.assertIsNone(candidate.resolved_entity_id)

    def test_approved_candidate_can_store_resolved_entity(self):
        entity = _entity()
        candidate = _candidate(
            entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity=entity,
        )
        candidate.full_clean()

        self.assertEqual(candidate.resolved_entity_id, entity.pk)

    def test_resolved_entity_may_differ_from_candidate_entity(self):
        candidate_entity = _entity("פלסטינה")
        resolved_entity = _entity("ארץ ישראל")
        candidate = _candidate(
            entity=candidate_entity,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity=resolved_entity,
        )
        candidate.full_clean()

        self.assertNotEqual(candidate.candidate_entity_id, candidate.resolved_entity_id)
        self.assertEqual(candidate.resolved_entity_id, resolved_entity.pk)

    def test_status_resolution_constraints_reject_impossible_combinations(self):
        entity = _entity()
        other = _entity("ארץ ישראל")
        proposal = _proposal()

        approved_without_resolution = NonPersonEntityOccurrenceCandidate(
            proposal=proposal,
            candidate_entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity=None,
        )
        with self.assertRaises(ValidationError) as caught:
            approved_without_resolution.full_clean()
        self.assertIn("resolved_entity", caught.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NonPersonEntityOccurrenceCandidate.objects.create(
                    proposal=proposal,
                    candidate_entity=entity,
                    status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
                    resolved_entity=other,
                )

        removed = _candidate(
            proposal=proposal,
            entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.REMOVED,
            resolved_entity=other,
        )
        removed.full_clean()
        self.assertEqual(removed.resolved_entity_id, other.pk)

        removed_without = _candidate(
            status=NonPersonEntityOccurrenceCandidate.Status.REMOVED,
            resolved_entity=None,
        )
        removed_without.full_clean()
        self.assertIsNone(removed_without.resolved_entity_id)


class NonPersonEntityOccurrenceCandidateMatchModelTests(TestCase):
    def test_one_candidate_may_have_multiple_match_methods(self):
        candidate = _candidate()
        canonical = NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME,
            matched_value="פלסטינה",
        )
        alias = NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS,
            matched_value="Palestine",
            alias_kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )

        self.assertNotEqual(canonical.pk, alias.pk)
        self.assertEqual(candidate.matches.count(), 2)
        self.assertEqual(candidate.proposal.candidates.count(), 1)

    def test_duplicate_identical_match_provenance_is_rejected(self):
        candidate = _candidate()
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS,
            matched_value="Palestine",
            alias_kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NonPersonEntityOccurrenceCandidateMatch.objects.create(
                    candidate=candidate,
                    method=NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS,
                    matched_value="Palestine",
                    alias_kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
                )

        stored = candidate.matches.get()
        self.assertEqual(stored.alias_kind, NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)

    def test_alias_registry_changes_do_not_erase_match_provenance(self):
        entity = _entity()
        alias = NonPersonEntityAlias.objects.create(
            entity=entity,
            name="Palestine",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        match = NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=_candidate(entity=entity),
            method=NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS,
            matched_value=alias.name,
            alias_kind=alias.kind,
        )
        match_id = match.pk

        alias.kind = NonPersonEntityAlias.Kind.SPELLING_VARIANT
        alias.name = "Palestine revised"
        alias.save()
        alias.delete()

        stored = NonPersonEntityOccurrenceCandidateMatch.objects.get(pk=match_id)
        self.assertEqual(stored.matched_value, "Palestine")
        self.assertEqual(stored.alias_kind, NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        alias_fields = {
            field
            for field in NonPersonEntityOccurrenceCandidateMatch._meta.get_fields()
            if getattr(field, "is_relation", False) and not field.auto_created
        }
        self.assertFalse(
            any(field.related_model is NonPersonEntityAlias for field in alias_fields)
        )


class NonPersonEntityOccurrenceReviewEventModelTests(TestCase):
    def _event(self, proposal, action, **overrides):
        fields = {
            "proposal": proposal,
            "action": action,
        }
        fields.update(overrides)
        return NonPersonEntityOccurrenceReviewEvent.objects.create(**fields)

    def test_event_can_record_approve_reject_needs_research_reassign_and_remove(self):
        proposal = _proposal()
        candidate_entity = _entity("פלסטינה")
        resolved_entity = _entity("ארץ ישראל")
        candidate = _candidate(
            proposal=proposal,
            entity=candidate_entity,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity=resolved_entity,
        )
        reviewer = User.objects.create_user(username="reviewer", password="test-pass")

        approve = self._event(
            proposal,
            NonPersonEntityOccurrenceReviewEvent.Action.APPROVE,
            candidate=candidate,
            to_entity=resolved_entity,
            actor=reviewer,
            note="Approved to the resolved entity.",
        )
        reject = self._event(
            proposal,
            NonPersonEntityOccurrenceReviewEvent.Action.REJECT,
            candidate=candidate,
            from_entity=candidate_entity,
            actor=reviewer,
        )
        needs_research = self._event(
            proposal,
            NonPersonEntityOccurrenceReviewEvent.Action.NEEDS_RESEARCH,
            candidate=candidate,
            actor=reviewer,
        )
        reassign = self._event(
            proposal,
            NonPersonEntityOccurrenceReviewEvent.Action.REASSIGN,
            candidate=candidate,
            from_entity=candidate_entity,
            to_entity=resolved_entity,
            actor=reviewer,
        )
        remove = self._event(
            proposal,
            NonPersonEntityOccurrenceReviewEvent.Action.REMOVE,
            candidate=candidate,
            from_entity=resolved_entity,
            actor=reviewer,
        )

        self.assertEqual(
            [
                approve.action,
                reject.action,
                needs_research.action,
                reassign.action,
                remove.action,
            ],
            [
                "APPROVE",
                "REJECT",
                "NEEDS_RESEARCH",
                "REASSIGN",
                "REMOVE",
            ],
        )
        self.assertEqual(reassign.from_entity_id, candidate_entity.pk)
        self.assertEqual(reassign.to_entity_id, resolved_entity.pk)
        self.assertEqual(proposal.review_events.count(), 5)

    def test_actor_may_be_null_for_a_system_event(self):
        actor_field = NonPersonEntityOccurrenceReviewEvent._meta.get_field("actor")
        self.assertTrue(actor_field.null)
        self.assertTrue(actor_field.blank)
        self.assertIs(actor_field.remote_field.on_delete, SET_NULL)

        system_event = self._event(
            _proposal(),
            NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
            actor=None,
        )
        system_event.full_clean()
        self.assertIsNone(system_event.actor_id)

        reviewer = User.objects.create_user(
            username="staff-actor", password="test-pass"
        )
        staff_event = self._event(
            _proposal(),
            NonPersonEntityOccurrenceReviewEvent.Action.PROPOSE,
            actor=reviewer,
        )
        self.assertEqual(staff_event.actor_id, reviewer.pk)

    def test_events_order_by_created_at_then_pk(self):
        proposal = _proposal()
        first = self._event(
            proposal, NonPersonEntityOccurrenceReviewEvent.Action.DETECT
        )
        second = self._event(
            proposal, NonPersonEntityOccurrenceReviewEvent.Action.PROPOSE
        )
        third = self._event(
            proposal, NonPersonEntityOccurrenceReviewEvent.Action.REJECT
        )
        later = timezone.now() + timedelta(hours=1)
        earlier = timezone.now() - timedelta(hours=1)
        NonPersonEntityOccurrenceReviewEvent.objects.filter(pk=third.pk).update(
            created_at=earlier
        )
        NonPersonEntityOccurrenceReviewEvent.objects.filter(pk=first.pk).update(
            created_at=later
        )

        self.assertEqual(
            list(proposal.review_events.values_list("pk", flat=True)),
            [third.pk, second.pk, first.pk],
        )

    def test_existing_event_cannot_be_updated(self):
        event = self._event(
            _proposal(),
            NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
            note="original",
        )
        event.note = "changed"

        with self.assertRaises(ValidationError):
            event.save()

        event.refresh_from_db()
        self.assertEqual(event.note, "original")
        self.assertFalse(
            any(
                field.name == "updated_at"
                for field in NonPersonEntityOccurrenceReviewEvent._meta.fields
            )
        )


class NonPersonEntityOccurrenceProposalDeleteTests(TestCase):
    def test_deleting_archive_item_cascades_the_proposal_tree(self):
        item = _item()
        entity = _entity()
        resolved = _entity("ארץ ישראל")
        proposal = _proposal(archive_item=item)
        candidate = _candidate(
            proposal=proposal,
            entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.REMOVED,
            resolved_entity=resolved,
        )
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME,
            matched_value=entity.canonical_name,
        )
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.REMOVE,
            from_entity=entity,
            to_entity=resolved,
        )

        item.delete()

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        self.assertTrue(NonPersonEntity.objects.filter(pk=entity.pk).exists())
        self.assertTrue(NonPersonEntity.objects.filter(pk=resolved.pk).exists())

    def test_referenced_entities_are_protected(self):
        candidate_entity = _entity("פלסטינה")
        resolved_entity = _entity("ארץ ישראל")
        from_entity = _entity("קהיר")
        to_entity = _entity("אלכסנדריה")
        unused = _entity("חיפה")
        proposal = _proposal()
        candidate = _candidate(
            proposal=proposal,
            entity=candidate_entity,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity=resolved_entity,
        )
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.REASSIGN,
            from_entity=from_entity,
            to_entity=to_entity,
        )

        for field_name, model in (
            ("candidate_entity", NonPersonEntityOccurrenceCandidate),
            ("resolved_entity", NonPersonEntityOccurrenceCandidate),
            ("from_entity", NonPersonEntityOccurrenceReviewEvent),
            ("to_entity", NonPersonEntityOccurrenceReviewEvent),
        ):
            field = model._meta.get_field(field_name)
            self.assertIs(field.remote_field.on_delete, PROTECT)

        for entity in (candidate_entity, resolved_entity, from_entity, to_entity):
            with self.assertRaises(ProtectedError):
                entity.delete()
            self.assertTrue(NonPersonEntity.objects.filter(pk=entity.pk).exists())

        unused.delete()
        self.assertFalse(NonPersonEntity.objects.filter(pk=unused.pk).exists())

        proposal_field = NonPersonEntityOccurrenceProposal._meta.get_field(
            "archive_item"
        )
        candidate_proposal = NonPersonEntityOccurrenceCandidate._meta.get_field(
            "proposal"
        )
        match_candidate = NonPersonEntityOccurrenceCandidateMatch._meta.get_field(
            "candidate"
        )
        event_proposal = NonPersonEntityOccurrenceReviewEvent._meta.get_field(
            "proposal"
        )
        event_candidate = NonPersonEntityOccurrenceReviewEvent._meta.get_field(
            "candidate"
        )
        self.assertIs(proposal_field.remote_field.on_delete, CASCADE)
        self.assertIs(candidate_proposal.remote_field.on_delete, CASCADE)
        self.assertIs(match_candidate.remote_field.on_delete, CASCADE)
        self.assertIs(event_proposal.remote_field.on_delete, CASCADE)
        self.assertIs(event_candidate.remote_field.on_delete, RESTRICT)
        self.assertTrue(event_candidate.null)

    def test_candidate_referenced_by_review_event_cannot_be_deleted(self):
        proposal = _proposal()
        entity = _entity()
        candidate = _candidate(proposal=proposal, entity=entity)
        event = NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.NEEDS_RESEARCH,
        )
        event_id = event.pk
        candidate_id = candidate.pk

        with self.assertRaises(RestrictedError):
            candidate.delete()

        event.refresh_from_db()
        self.assertEqual(event.pk, event_id)
        self.assertEqual(event.candidate_id, candidate_id)
        self.assertTrue(
            NonPersonEntityOccurrenceCandidate.objects.filter(pk=candidate_id).exists()
        )


class NonPersonEntityOccurrenceProposalNonRegressionTests(TestCase):
    def test_existing_occurrence_and_decision_contracts_stay_in_place(self):
        occurrence_constraints = {
            constraint.name
            for constraint in ArchiveItemEntityOccurrence._meta.constraints
        }
        self.assertEqual(
            occurrence_constraints,
            {
                "uniq_archive_item_entity_occurrence_identity",
                "archive_item_entity_occurrence_resolution_entity",
                "archive_item_entity_occurrence_ordinal_gte_1",
            },
        )
        decision_constraints = {
            constraint.name
            for constraint in ReviewedNonPersonEntityDecision._meta.constraints
        }
        self.assertIn(
            "uniq_reviewed_non_person_entity_decision_source_candidate",
            decision_constraints,
        )
        self.assertEqual(
            ArchiveItemEntityOccurrence._meta.get_field("decision").related_model,
            ReviewedNonPersonEntityDecision,
        )

    def test_proposal_rows_do_not_write_occurrence_decision_or_search_rows(self):
        entity = _entity()
        proposal = _proposal()
        candidate = _candidate(proposal=proposal, entity=entity)
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME,
            matched_value="Palestine",
        )
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
        )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), 0)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
