"""Staff review actions for non-person occurrence proposals."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
    ReviewedNonPersonEntityDecision,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrence_review import (
    ALREADY_APPLIED_MESSAGE,
    APPROVE_APPLIED_MESSAGE,
    INVALID_TRANSITION_MESSAGE,
    NEEDS_RESEARCH_APPLIED_MESSAGE,
    REASSIGN_APPLIED_MESSAGE,
    REJECT_APPLIED_MESSAGE,
    REMOVE_APPLIED_MESSAGE,
    STALE_SOURCE_MESSAGE,
    STATE_STALE_LABEL,
    InvalidTransitionReviewError,
    OccurrenceConflictReviewError,
    PROTECTED_DECISION_OCCURRENCE_MESSAGE,
    ProtectedDecisionOccurrenceReviewError,
    SourceLocationReviewError,
    StaleSourceReviewError,
    UnsupportedNormalizationReviewError,
    approve_candidate,
    mark_needs_research,
    reassign_candidate,
    reject_candidate,
    remove_approved_occurrence,
)
from documents.services.non_person_entity_occurrences import (
    occurrence_is_currently_valid,
    source_text_sha256,
)
from documents.services.non_person_entity_presentation import (
    MENTIONED_OBJECTS_PUBLIC_HEADING,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
NEEDS_RESEARCH = NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
APPROVED = NonPersonEntityOccurrenceCandidate.Status.APPROVED
REJECTED = NonPersonEntityOccurrenceCandidate.Status.REJECTED
REMOVED = NonPersonEntityOccurrenceCandidate.Status.REMOVED
APPROVE = NonPersonEntityOccurrenceReviewEvent.Action.APPROVE
REJECT = NonPersonEntityOccurrenceReviewEvent.Action.REJECT
RESEARCH = NonPersonEntityOccurrenceReviewEvent.Action.NEEDS_RESEARCH
REASSIGN = NonPersonEntityOccurrenceReviewEvent.Action.REASSIGN
REMOVE = NonPersonEntityOccurrenceReviewEvent.Action.REMOVE
QUEUE = "archive-manage-entity-occurrence-proposals"
REVIEW = "archive-manage-entity-occurrence-proposal"
BODY = "ביקור בקהיר אחר הצהריים"
SURFACE = "קהיר"


def _entity(name: str, **overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": name,
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _item(title: str = "פריט בדיקה", body: str = BODY) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _proposal(
    item: ArchiveItem,
    body: str = BODY,
    *,
    ordinal: int = 1,
    surface: str = SURFACE,
    matched_text: str = SURFACE,
    sha: str | None = None,
    normalization_version: str = "surface-v1",
) -> NonPersonEntityOccurrenceProposal:
    return NonPersonEntityOccurrenceProposal.objects.create(
        archive_item=item,
        text_kind=MANUAL,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version=normalization_version,
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        matched_text=matched_text,
    )


def _candidate(
    proposal: NonPersonEntityOccurrenceProposal,
    entity: NonPersonEntity,
    *,
    status: str = PENDING,
    resolved: NonPersonEntity | None = None,
) -> NonPersonEntityOccurrenceCandidate:
    candidate = NonPersonEntityOccurrenceCandidate.objects.create(
        proposal=proposal,
        candidate_entity=entity,
        status=status,
        resolved_entity=resolved,
    )
    NonPersonEntityOccurrenceCandidateMatch.objects.create(
        candidate=candidate,
        method=NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME,
        matched_value=entity.canonical_name,
    )
    return candidate


def _pin(
    item: ArchiveItem,
    entity: NonPersonEntity,
    body: str,
    *,
    ordinal: int = 1,
    surface: str = SURFACE,
    decision: ReviewedNonPersonEntityDecision | None = None,
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=MANUAL,
        source_text_sha256=source_text_sha256(body),
        normalization_version="surface-v1",
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        resolution_status=RESOLVED,
        entity=entity,
        decision=decision,
        matched_text=surface,
    )


class OccurrenceReviewServiceTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username="reviewer", password="test-pass")
        self.entity = _entity("קהיר")
        self.other = _entity("אלכסנדריה")
        self.item = _item()
        self.proposal = _proposal(self.item, matched_text="טקסט היסטורי")
        self.candidate = _candidate(self.proposal, self.entity)

    def _events(self, action: str | None = None):
        queryset = NonPersonEntityOccurrenceReviewEvent.objects.filter(
            candidate=self.candidate
        )
        if action is not None:
            queryset = queryset.filter(action=action)
        return queryset

    def _refresh(self) -> NonPersonEntityOccurrenceCandidate:
        self.candidate.refresh_from_db()
        return self.candidate

    def test_approve_creates_one_resolved_occurrence_with_proposal_identity(self):
        result = approve_candidate(self.candidate.pk, actor=self.actor, note="בדקתי")

        self.assertTrue(result.applied)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.resolution_status, RESOLVED)
        self.assertEqual(occurrence.archive_item_id, self.proposal.archive_item_id)
        self.assertEqual(occurrence.text_kind, self.proposal.text_kind)
        self.assertEqual(
            occurrence.source_text_sha256, self.proposal.source_text_sha256
        )
        self.assertEqual(
            occurrence.normalization_version, self.proposal.normalization_version
        )
        self.assertEqual(
            occurrence.normalized_surface, self.proposal.normalized_surface
        )
        self.assertEqual(
            occurrence.occurrence_ordinal, self.proposal.occurrence_ordinal
        )
        self.assertIsNone(occurrence.decision_id)
        self.assertEqual(occurrence.matched_text, SURFACE)
        self.assertNotEqual(occurrence.matched_text, "טקסט היסטורי")
        self._refresh()
        self.assertEqual(self.candidate.status, APPROVED)
        self.assertEqual(self.candidate.resolved_entity_id, self.entity.pk)
        self.assertEqual(self.candidate.reviewed_by_id, self.actor.pk)
        self.assertIsNotNone(self.candidate.reviewed_at)
        event = self._events(APPROVE).get()
        self.assertEqual(event.to_entity_id, self.entity.pk)
        self.assertEqual(event.note, "בדקתי")
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)

    def test_approve_replay_does_not_duplicate_occurrence_or_event(self):
        approve_candidate(self.candidate.pk, actor=self.actor)
        approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
        self.assertEqual(self._events(APPROVE).count(), 1)

    def test_existing_same_entity_occurrence_is_idempotent_after_approval(self):
        _pin(self.item, self.entity, BODY)
        approve_candidate(self.candidate.pk, actor=self.actor)
        approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
        self.assertEqual(
            ArchiveItemEntityOccurrence.objects.get().entity_id, self.entity.pk
        )
        self.assertEqual(self._events(APPROVE).count(), 1)
        self.assertEqual(self._refresh().status, APPROVED)

    def test_existing_different_entity_occurrence_conflicts(self):
        pin = _pin(self.item, self.other, BODY)

        with self.assertRaises(OccurrenceConflictReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        pin.refresh_from_db()
        self.assertEqual(pin.entity_id, self.other.pk)
        self.assertEqual(self._refresh().status, PENDING)
        self.assertEqual(self._events().count(), 0)

    def test_stale_source_approve_writes_nothing(self):
        content = ManualTextContent.objects.get(archive_item=self.item)
        content.body = "טקסט שהוחלף"
        content.save(update_fields=["body", "updated_at"])
        stored_sha = self.proposal.source_text_sha256

        with self.assertRaises(StaleSourceReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.source_text_sha256, stored_sha)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._refresh().status, PENDING)
        self.assertEqual(self._events().count(), 0)

    def test_unknown_normalization_version_writes_nothing(self):
        self.proposal.normalization_version = "surface-v9"
        self.proposal.save(update_fields=["normalization_version", "updated_at"])

        with self.assertRaises(UnsupportedNormalizationReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._refresh().status, PENDING)

    def test_missing_ordinal_writes_nothing(self):
        self.proposal.occurrence_ordinal = 2
        self.proposal.save(update_fields=["occurrence_ordinal", "updated_at"])

        with self.assertRaises(SourceLocationReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._refresh().status, PENDING)

    def test_removed_candidate_cannot_be_approved(self):
        self.candidate.status = REMOVED
        self.candidate.resolved_entity = self.entity
        self.candidate.save(update_fields=["status", "resolved_entity", "updated_at"])
        before = (
            self.candidate.status,
            self.candidate.resolved_entity_id,
            self.candidate.reviewed_by_id,
            self.candidate.reviewed_at,
        )

        with self.assertRaises(InvalidTransitionReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        self._refresh()
        self.assertEqual(
            (
                self.candidate.status,
                self.candidate.resolved_entity_id,
                self.candidate.reviewed_by_id,
                self.candidate.reviewed_at,
            ),
            before,
        )
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._events().count(), 0)

    def test_reject_pending_candidate_creates_no_occurrence(self):
        result = reject_candidate(self.candidate.pk, actor=self.actor, note="לא זה")

        self.assertTrue(result.applied)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self._refresh()
        self.assertEqual(self.candidate.status, REJECTED)
        self.assertIsNone(self.candidate.resolved_entity_id)
        event = self._events(REJECT).get()
        self.assertEqual(event.note, "לא זה")
        self.assertIsNone(event.from_entity_id)
        self.assertIsNone(event.to_entity_id)

    def test_reject_replay_does_not_duplicate_event(self):
        reject_candidate(self.candidate.pk, actor=self.actor)
        result = reject_candidate(self.candidate.pk, actor=self.actor)

        self.assertFalse(result.applied)
        self.assertEqual(self._events(REJECT).count(), 1)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_approved_candidate_cannot_be_rejected_directly(self):
        approve_candidate(self.candidate.pk, actor=self.actor)

        with self.assertRaises(InvalidTransitionReviewError):
            reject_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
        self.assertEqual(self._refresh().status, APPROVED)
        self.assertEqual(self._events(REJECT).count(), 0)

    def test_needs_research_creates_no_occurrence(self):
        result = mark_needs_research(self.candidate.pk, actor=self.actor, note="לבדוק")

        self.assertTrue(result.applied)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self._refresh()
        self.assertEqual(self.candidate.status, NEEDS_RESEARCH)
        self.assertIsNone(self.candidate.resolved_entity_id)
        self.assertEqual(self._events(RESEARCH).get().note, "לבדוק")

    def test_needs_research_replay_does_not_duplicate_event(self):
        mark_needs_research(self.candidate.pk, actor=self.actor)
        result = mark_needs_research(self.candidate.pk, actor=self.actor)

        self.assertFalse(result.applied)
        self.assertEqual(self._events(RESEARCH).count(), 1)

    def test_reassign_pending_candidate_to_another_entity(self):
        result = reassign_candidate(
            self.candidate.pk,
            target_entity_id=self.other.pk,
            actor=self.actor,
            note="זו הרשומה",
        )

        self.assertTrue(result.applied)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.entity_id, self.other.pk)
        self.assertIsNone(occurrence.decision_id)
        self.assertEqual(occurrence.matched_text, SURFACE)
        self._refresh()
        self.assertEqual(self.candidate.status, APPROVED)
        self.assertEqual(self.candidate.resolved_entity_id, self.other.pk)
        event = self._events(REASSIGN).get()
        self.assertIsNone(event.from_entity_id)
        self.assertEqual(event.to_entity_id, self.other.pk)
        self.assertEqual(event.note, "זו הרשומה")
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)

    def test_reassign_changes_existing_occurrence_and_replay_is_idempotent(self):
        _pin(self.item, self.entity, BODY)
        reassign_candidate(
            self.candidate.pk,
            target_entity_id=self.other.pk,
            actor=self.actor,
        )
        result = reassign_candidate(
            self.candidate.pk,
            target_entity_id=self.other.pk,
            actor=self.actor,
        )

        self.assertFalse(result.applied)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.entity_id, self.other.pk)
        event = self._events(REASSIGN).get()
        self.assertEqual(event.from_entity_id, self.entity.pk)
        self.assertEqual(event.to_entity_id, self.other.pk)
        self.assertEqual(self._refresh().resolved_entity_id, self.other.pk)

    def test_stale_proposal_cannot_reassign(self):
        content = ManualTextContent.objects.get(archive_item=self.item)
        content.body = "טקסט שהוחלף"
        content.save(update_fields=["body", "updated_at"])

        with self.assertRaises(StaleSourceReviewError):
            reassign_candidate(
                self.candidate.pk,
                target_entity_id=self.other.pk,
                actor=self.actor,
            )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._refresh().status, PENDING)

    def test_remove_deletes_occurrence_and_keeps_resolved_entity(self):
        approve_candidate(self.candidate.pk, actor=self.actor)
        result = remove_approved_occurrence(
            self.candidate.pk,
            actor=self.actor,
            note="טעות",
        )

        self.assertTrue(result.applied)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self._refresh()
        self.assertEqual(self.candidate.status, REMOVED)
        self.assertEqual(self.candidate.resolved_entity_id, self.entity.pk)
        event = self._events(REMOVE).get()
        self.assertEqual(event.from_entity_id, self.entity.pk)
        self.assertIsNone(event.to_entity_id)
        self.assertEqual(event.note, "טעות")
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)

    def test_remove_replay_is_idempotent(self):
        approve_candidate(self.candidate.pk, actor=self.actor)
        remove_approved_occurrence(self.candidate.pk, actor=self.actor)
        result = remove_approved_occurrence(self.candidate.pk, actor=self.actor)

        self.assertFalse(result.applied)
        self.assertEqual(self._events(REMOVE).count(), 1)
        self.assertEqual(self._refresh().status, REMOVED)
        self.assertEqual(self.candidate.resolved_entity_id, self.entity.pk)

    def test_remove_does_not_delete_a_different_entity_occurrence(self):
        approve_candidate(self.candidate.pk, actor=self.actor)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        occurrence.entity = self.other
        occurrence.save(update_fields=["entity", "updated_at"])

        with self.assertRaises(OccurrenceConflictReviewError):
            remove_approved_occurrence(self.candidate.pk, actor=self.actor)

        self.assertEqual(
            ArchiveItemEntityOccurrence.objects.get().entity_id, self.other.pk
        )
        self.assertEqual(self._refresh().status, APPROVED)
        self.assertEqual(self._events(REMOVE).count(), 0)

    def test_stale_source_remove_fails_closed(self):
        approve_candidate(self.candidate.pk, actor=self.actor)
        content = ManualTextContent.objects.get(archive_item=self.item)
        content.body = "טקסט שהוחלף"
        content.save(update_fields=["body", "updated_at"])

        with self.assertRaises(StaleSourceReviewError):
            remove_approved_occurrence(self.candidate.pk, actor=self.actor)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
        self.assertEqual(self._refresh().status, APPROVED)

    def test_occurrence_insert_failure_rolls_back_candidate_and_event(self):
        with patch(
            "documents.services.non_person_entity_occurrence_review."
            "ArchiveItemEntityOccurrence.objects.create",
            side_effect=RuntimeError("induced occurrence failure"),
        ):
            with self.assertRaises(RuntimeError):
                approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(self._refresh().status, PENDING)
        self.assertEqual(self._events().count(), 0)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_event_failure_rolls_back_occurrence(self):
        with patch(
            "documents.services.non_person_entity_occurrence_review."
            "NonPersonEntityOccurrenceReviewEvent.objects.create",
            side_effect=RuntimeError("induced event failure"),
        ):
            with self.assertRaises(RuntimeError):
                approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(self._refresh().status, PENDING)
        self.assertIsNone(self.candidate.resolved_entity_id)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)


class OccurrenceReviewStaffUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="occurrence_staff",
            password="test-pass",
            is_staff=True,
        )
        self.entity = _entity("קהיר")
        self.other = _entity(
            "אלכסנדריה",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        self.item = _item(title="כותרת לבדיקה")
        self.pending = _candidate(_proposal(self.item), self.entity)
        research_item = _item(title="פריט בירור", body="עיר אלכסנדריה")
        self.research = _candidate(
            _proposal(
                research_item,
                "עיר אלכסנדריה",
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
            ),
            self.other,
            status=NEEDS_RESEARCH,
        )
        self.client.force_login(self.staff)

    def test_non_admin_is_denied(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="occurrence_family", password="test-pass"
        )
        user.groups.add(family_group)
        self.client.force_login(user)

        queue = self.client.get(reverse(QUEUE))
        detail = self.client.get(
            reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})
        )

        self.assertEqual(queue.status_code, 403)
        self.assertEqual(detail.status_code, 403)

    def test_admin_queue_shows_open_candidates_and_hides_sha(self):
        approved_item = _item(title="פריט מאושר", body="עוד קהיר")
        approved = _candidate(
            _proposal(approved_item, "עוד קהיר"),
            self.entity,
            status=APPROVED,
            resolved=self.entity,
        )
        resp = self.client.get(reverse(QUEUE))
        html = resp.content.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "בדיקת אזכורי מקומות וארגונים")
        self.assertContains(resp, "כותרת לבדיקה")
        self.assertContains(resp, "פריט בירור")
        self.assertContains(resp, "ממתין לבדיקה")
        self.assertContains(resp, "נדרש בירור")
        self.assertNotContains(resp, "פריט מאושר")
        self.assertContains(resp, "שם קנוני: קהיר")
        self.assertContains(resp, "מקום · עיר")
        self.assertNotIn(self.pending.proposal.source_text_sha256, html)
        self.assertNotIn(approved.proposal.source_text_sha256, html)
        manage = self.client.get(reverse("archive-manage-list"))
        self.assertContains(manage, reverse(QUEUE))
        self.assertContains(
            manage,
            f"{reverse(QUEUE)}?item={self.item.pk}",
        )

    def test_stale_candidate_is_marked_and_approve_post_is_rejected(self):
        content = ManualTextContent.objects.get(archive_item=self.item)
        content.body = "טקסט שהוחלף"
        content.save(update_fields=["body", "updated_at"])
        queue = self.client.get(reverse(QUEUE))
        self.assertContains(queue, STATE_STALE_LABEL)
        self.assertContains(queue, "כותרת לבדיקה")

        detail_url = reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})
        detail = self.client.get(detail_url)
        self.assertContains(detail, STALE_SOURCE_MESSAGE)
        self.assertNotContains(detail, "אשר אזכור")
        self.assertNotIn(
            self.pending.proposal.source_text_sha256, detail.content.decode()
        )

        posted = self.client.post(detail_url, {"action": "approve"}, follow=True)

        self.assertRedirects(posted, detail_url)
        self.assertContains(posted, STALE_SOURCE_MESSAGE)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, PENDING)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_approve_reject_and_needs_research_posts_redirect(self):
        detail_url = reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})
        page = self.client.get(detail_url)
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertContains(page, "אשר אזכור")
        self.assertContains(page, "דחה")
        self.assertContains(page, "סמן לבדיקה נוספת")
        self.assertNotContains(page, "הסר קישור מאושר")
        self.assertContains(page, "אלכסנדריה")
        self.assertContains(page, "מקום · עיר")

        research = self.client.post(
            detail_url,
            {"action": "needs_research", "note": "עוד מקור"},
            follow=True,
        )
        self.assertRedirects(research, detail_url)
        self.assertContains(research, NEEDS_RESEARCH_APPLIED_MESSAGE)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, NEEDS_RESEARCH)

        rejected = self.client.post(detail_url, {"action": "reject"}, follow=True)
        self.assertRedirects(rejected, detail_url)
        self.assertContains(rejected, REJECT_APPLIED_MESSAGE)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, REJECTED)

        approved = self.client.post(detail_url, {"action": "approve"}, follow=True)
        self.assertRedirects(approved, detail_url)
        self.assertContains(approved, APPROVE_APPLIED_MESSAGE)
        self.assertContains(approved, "הסר קישור מאושר")
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, APPROVED)
        self.assertEqual(
            ArchiveItemEntityOccurrence.objects.get().entity_id,
            self.entity.pk,
        )

    def test_reassign_post_uses_an_existing_registry_row(self):
        detail_url = reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})
        posted = self.client.post(
            detail_url,
            {"action": "reassign", "target_entity_id": self.other.pk},
            follow=True,
        )

        self.assertRedirects(posted, detail_url)
        self.assertContains(posted, REASSIGN_APPLIED_MESSAGE)
        self.assertEqual(
            ArchiveItemEntityOccurrence.objects.get().entity_id,
            self.other.pk,
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.resolved_entity_id, self.other.pk)

    def test_remove_post_is_available_for_the_approved_candidate(self):
        approve_candidate(self.pending.pk, actor=self.staff)
        detail_url = reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})
        posted = self.client.post(detail_url, {"action": "remove"}, follow=True)

        self.assertRedirects(posted, detail_url)
        self.assertContains(posted, REMOVE_APPLIED_MESSAGE)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, REMOVED)
        self.assertEqual(self.pending.resolved_entity_id, self.entity.pk)

    def test_forged_approve_post_does_not_reopen_removed(self):
        self.pending.status = REMOVED
        self.pending.resolved_entity = self.entity
        self.pending.save(update_fields=["status", "resolved_entity", "updated_at"])
        detail_url = reverse(REVIEW, kwargs={"candidate_id": self.pending.pk})

        posted = self.client.post(detail_url, {"action": "approve"}, follow=True)

        self.assertRedirects(posted, detail_url)
        self.assertContains(posted, INVALID_TRANSITION_MESSAGE)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, REMOVED)
        self.assertEqual(self.pending.resolved_entity_id, self.entity.pk)
        self.assertIsNone(self.pending.reviewed_at)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                candidate=self.pending
            ).count(),
            0,
        )


class OccurrenceReviewPublicRegressionTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username="public_reviewer", password="x")
        self.entity = _entity("UniqueRegistryZz")
        self.item = _item(title="VisibleArchiveTitle", body=BODY)
        self.candidate = _candidate(_proposal(self.item), self.entity)
        self.index = ArchiveItemSearchIndex.objects.get(archive_item=self.item)
        self.index_before = (
            self.index.title_text,
            self.index.metadata_text,
            self.index.body_text,
            self.index.hebrew_translation_text,
            self.index.updated_at,
        )

    def _search_ids(self, query: str) -> list[int]:
        return list(
            filter_archive_items_by_search_query(
                ArchiveItem.objects.all(),
                query,
            ).values_list("id", flat=True)
        )

    def test_approve_and_remove_drive_public_mentions_without_search_index_writes(self):
        title_before = self._search_ids("VisibleArchiveTitle")
        registry_before = self._search_ids("UniqueRegistryZz")

        approve_candidate(self.candidate.pk, actor=self.actor)
        detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.item.pk})
        )

        self.assertContains(detail, MENTIONED_OBJECTS_PUBLIC_HEADING)
        self.assertContains(detail, "UniqueRegistryZz")
        self.assertContains(detail, f"/archive/entities/{self.entity.pk}/")
        self.assertEqual(self._search_ids("VisibleArchiveTitle"), title_before)
        self.assertEqual(self._search_ids("UniqueRegistryZz"), registry_before)
        self.assertNotIn(self.item.pk, registry_before)
        self.index.refresh_from_db()
        self.assertEqual(
            (
                self.index.title_text,
                self.index.metadata_text,
                self.index.body_text,
                self.index.hebrew_translation_text,
                self.index.updated_at,
            ),
            self.index_before,
        )

        remove_approved_occurrence(self.candidate.pk, actor=self.actor)
        after_remove = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.item.pk})
        )
        self.assertNotContains(after_remove, MENTIONED_OBJECTS_PUBLIC_HEADING)
        self.assertNotContains(after_remove, "UniqueRegistryZz")
        self.index.refresh_from_db()
        self.assertEqual(self.index.updated_at, self.index_before[4])

    def test_v6_decision_occurrence_stays_unchanged(self):
        decision = ReviewedNonPersonEntityDecision.objects.create(
            source="v6-workbook",
            candidate_id="EC0099",
            workbook_sha256="c" * 64,
            decision=ReviewedNonPersonEntityDecision.Decision.SPLIT,
            review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
            result_entity=self.entity,
        )
        v6_item = _item(title="v6 item", body=BODY)
        pin = _pin(v6_item, self.entity, BODY, decision=decision)
        self.assertTrue(occurrence_is_currently_valid(pin))

        approve_candidate(self.candidate.pk, actor=self.actor)

        pin.refresh_from_db()
        decision.refresh_from_db()
        self.assertEqual(pin.decision_id, decision.pk)
        self.assertEqual(pin.entity_id, self.entity.pk)
        self.assertTrue(occurrence_is_currently_valid(pin))
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 1)
        staff_pin = ArchiveItemEntityOccurrence.objects.exclude(pk=pin.pk).get()
        self.assertIsNone(staff_pin.decision_id)
        self.assertEqual(decision.candidate_id, "EC0099")
        self.assertEqual(
            decision.decision, ReviewedNonPersonEntityDecision.Decision.SPLIT
        )


class WorkbookDecisionPinProtectionTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(
            username="workbook_reviewer",
            password="test-pass",
        )
        self.entity = _entity("קהיר")
        self.other = _entity("אלכסנדריה")
        self.item = _item(title="פריט חוברת")
        self.decision = ReviewedNonPersonEntityDecision.objects.create(
            source="v6-workbook",
            candidate_id="EC0101",
            workbook_sha256="d" * 64,
            decision=ReviewedNonPersonEntityDecision.Decision.SPLIT,
            review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
            result_entity=self.entity,
        )
        self.pin = _pin(self.item, self.entity, BODY, decision=self.decision)
        self.proposal = _proposal(self.item)
        self.candidate = _candidate(self.proposal, self.entity)

    def _pin_fields(self) -> tuple:
        self.pin.refresh_from_db()
        return (
            self.pin.pk,
            self.pin.entity_id,
            self.pin.decision_id,
            self.pin.matched_text,
            self.pin.resolution_status,
            self.pin.updated_at,
        )

    def _candidate_fields(self) -> tuple:
        self.candidate.refresh_from_db()
        return (
            self.candidate.status,
            self.candidate.resolved_entity_id,
            self.candidate.reviewed_by_id,
            self.candidate.reviewed_at,
        )

    def test_reassign_refuses_a_decision_backed_pin(self):
        before_pin = self._pin_fields()
        before_candidate = self._candidate_fields()

        with self.assertRaises(ProtectedDecisionOccurrenceReviewError):
            reassign_candidate(
                self.candidate.pk,
                target_entity_id=self.other.pk,
                actor=self.actor,
            )

        self.assertEqual(self._pin_fields(), before_pin)
        self.assertEqual(self._candidate_fields(), before_candidate)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        self.assertEqual(self.pin.decision_id, self.decision.pk)
        self.assertEqual(self.pin.entity_id, self.entity.pk)

    def test_remove_refuses_a_decision_backed_pin(self):
        self.candidate.status = APPROVED
        self.candidate.resolved_entity = self.entity
        self.candidate.save(update_fields=["status", "resolved_entity", "updated_at"])
        before_pin = self._pin_fields()
        before_candidate = self._candidate_fields()

        with self.assertRaises(ProtectedDecisionOccurrenceReviewError):
            remove_approved_occurrence(self.candidate.pk, actor=self.actor)

        self.assertEqual(self._pin_fields(), before_pin)
        self.assertEqual(self._candidate_fields(), before_candidate)
        self.assertTrue(
            ArchiveItemEntityOccurrence.objects.filter(pk=self.pin.pk).exists()
        )
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_approve_same_entity_records_confirmation_without_writing_the_pin(self):
        before_pin = self._pin_fields()

        result = approve_candidate(self.candidate.pk, actor=self.actor, note="אישור")

        self.assertTrue(result.applied)
        self.assertEqual(self._pin_fields(), before_pin)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.status, APPROVED)
        self.assertEqual(self.candidate.resolved_entity_id, self.entity.pk)
        event = NonPersonEntityOccurrenceReviewEvent.objects.get()
        self.assertEqual(event.action, APPROVE)
        self.assertEqual(event.to_entity_id, self.entity.pk)
        self.assertEqual(event.note, "אישור")
        self.decision.refresh_from_db()
        self.assertEqual(self.decision.result_entity_id, self.entity.pk)

    def test_approve_different_entity_conflicts_and_leaves_the_pin(self):
        other_candidate = _candidate(self.proposal, self.other)
        before_pin = self._pin_fields()
        before_candidate = (
            other_candidate.status,
            other_candidate.resolved_entity_id,
            other_candidate.reviewed_by_id,
            other_candidate.reviewed_at,
        )

        with self.assertRaises(OccurrenceConflictReviewError):
            approve_candidate(other_candidate.pk, actor=self.actor)

        self.assertEqual(self._pin_fields(), before_pin)
        other_candidate.refresh_from_db()
        self.assertEqual(
            (
                other_candidate.status,
                other_candidate.resolved_entity_id,
                other_candidate.reviewed_by_id,
                other_candidate.reviewed_at,
            ),
            before_candidate,
        )
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_approve_does_not_reopen_removed_over_a_decision_backed_pin(self):
        self.candidate.status = REMOVED
        self.candidate.resolved_entity = self.entity
        self.candidate.save(update_fields=["status", "resolved_entity", "updated_at"])
        before_pin = self._pin_fields()
        before_candidate = self._candidate_fields()

        with self.assertRaises(InvalidTransitionReviewError):
            approve_candidate(self.candidate.pk, actor=self.actor)

        self.assertEqual(self._pin_fields(), before_pin)
        self.assertEqual(self._candidate_fields(), before_candidate)
        self.assertEqual(self.candidate.status, REMOVED)
        self.assertEqual(self.pin.decision_id, self.decision.pk)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_review_page_hides_controls_and_forged_posts_are_refused(self):
        self.candidate.status = APPROVED
        self.candidate.resolved_entity = self.entity
        self.candidate.save(update_fields=["status", "resolved_entity", "updated_at"])
        staff = User.objects.create_user(
            username="workbook_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        url = reverse(REVIEW, kwargs={"candidate_id": self.candidate.pk})
        page = self.client.get(url)
        self.assertNotContains(page, "שייך לרשומה אחרת")
        self.assertNotContains(page, "הסר קישור מאושר")

        before_pin = self._pin_fields()
        before_candidate = self._candidate_fields()
        reassigned = self.client.post(
            url,
            {"action": "reassign", "target_entity_id": str(self.other.pk)},
            follow=True,
        )
        removed = self.client.post(url, {"action": "remove"}, follow=True)

        self.assertContains(reassigned, PROTECTED_DECISION_OCCURRENCE_MESSAGE)
        self.assertContains(removed, PROTECTED_DECISION_OCCURRENCE_MESSAGE)
        self.assertEqual(self._pin_fields(), before_pin)
        self.assertEqual(self._candidate_fields(), before_candidate)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        self.assertEqual(self.pin.decision_id, self.decision.pk)


class OccurrenceReviewIdempotentMessageTests(TestCase):
    def test_repeated_approve_post_reports_already_applied(self):
        staff = User.objects.create_user(
            username="repeat_staff",
            password="test-pass",
            is_staff=True,
        )
        entity = _entity("קהיר")
        item = _item()
        candidate = _candidate(_proposal(item), entity)
        self.client.force_login(staff)
        url = reverse(REVIEW, kwargs={"candidate_id": candidate.pk})
        self.client.post(url, {"action": "approve"})
        replay = self.client.post(url, {"action": "approve"}, follow=True)

        self.assertContains(replay, ALREADY_APPLIED_MESSAGE)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
