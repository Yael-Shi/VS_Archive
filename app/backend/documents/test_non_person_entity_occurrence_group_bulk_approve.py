"""Grouped bulk approval of pending non-person occurrence candidates."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import Client, TestCase
from django.urls import reverse

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
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrence_grouped_review import (
    CONTEXT_UNAVAILABLE,
    GROUP_BLOCKED_MESSAGE,
    GROUP_DETAIL_PAGE_SIZE,
    GROUP_MEMBERSHIP_CHANGED_MESSAGE,
    GROUP_WARNINGS_UNCONFIRMED_MESSAGE,
    MANY_ARCHIVE_ITEMS,
    MATCHED_TEXT_UNAVAILABLE,
    MULTIPLE_ALIAS_KINDS,
    MULTIPLE_MATCH_METHODS,
    OCR_VARIANT,
    PRIOR_REVIEW_HISTORY,
    SHORT_SURFACE,
    SOURCE_STALE,
    _context_parts,
    approve_pending_occurrence_group,
    staff_grouped_occurrence_review_page,
    staff_occurrence_group_approve_preview,
)
from documents.services.non_person_entity_occurrence_review import (
    OCCURRENCE_CONFLICT_MESSAGE,
    StaleSourceReviewError,
    approve_candidate,
)
from documents.services.non_person_entity_occurrences import source_text_sha256

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
NEEDS_RESEARCH = NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
APPROVED = NonPersonEntityOccurrenceCandidate.Status.APPROVED
REJECTED = NonPersonEntityOccurrenceCandidate.Status.REJECTED
REMOVED = NonPersonEntityOccurrenceCandidate.Status.REMOVED
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
CANONICAL = NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME
ALIAS = NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS
DISPLAY = NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME
APPROVE = NonPersonEntityOccurrenceReviewEvent.Action.APPROVE
PROPOSE = NonPersonEntityOccurrenceReviewEvent.Action.PROPOSE
GROUPS = "archive-manage-entity-occurrence-groups"
GROUP = "archive-manage-entity-occurrence-group"
APPROVE_URL = "archive-manage-entity-occurrence-group-approve"
BODY = "ביקור בקהיר אחר הצהריים"
SURFACE = "קהיר"
NOTE = "הערה לקבוצה"


def _entity(name: str) -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.CITY,
    )


def _item(title: str, body: str = BODY) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _proposal(
    item: ArchiveItem,
    body: str,
    *,
    ordinal: int = 1,
    surface: str = SURFACE,
    matched_text: str = SURFACE,
    normalization_version: str = "surface-v1",
    sha: str | None = None,
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
    matches: tuple[tuple[str, str], ...] = ((CANONICAL, ""),),
    status: str = PENDING,
    resolved: NonPersonEntity | None = None,
) -> NonPersonEntityOccurrenceCandidate:
    candidate = NonPersonEntityOccurrenceCandidate.objects.create(
        proposal=proposal,
        candidate_entity=entity,
        status=status,
        resolved_entity=resolved,
    )
    for method, alias_kind in matches:
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=method,
            matched_value=f"{method}:{alias_kind or entity.canonical_name}:{candidate.pk}",
            alias_kind=alias_kind,
        )
    return candidate


def _staff(username: str = "group_approver") -> User:
    return User.objects.create_user(
        username=username,
        password="test-pass",
        is_staff=True,
    )


def _groups():
    return list(staff_grouped_occurrence_review_page().groups)


def _group_for(candidate_ids: list[int]):
    wanted = tuple(sorted(candidate_ids))
    for group in _groups():
        if tuple(sorted(group.candidate_ids)) == wanted:
            return group
    raise AssertionError(wanted)


def _counts() -> dict[str, int]:
    return {
        "occurrences": ArchiveItemEntityOccurrence.objects.count(),
        "events": NonPersonEntityOccurrenceReviewEvent.objects.count(),
        "aliases": NonPersonEntityAlias.objects.count(),
        "entities": NonPersonEntity.objects.count(),
        "decisions": ReviewedNonPersonEntityDecision.objects.count(),
        "search": ArchiveItemSearchIndex.objects.count(),
    }


class GroupBulkApproveTests(TestCase):
    def setUp(self):
        self.staff = _staff()
        self.client.force_login(self.staff)
        self.entity = _entity("קהיר")

    def _pending_group(self, count: int = 3, **proposal_kwargs):
        rows = []
        for index in range(count):
            item = _item(f"פריט {index}")
            rows.append(
                _candidate(
                    _proposal(item, BODY, **proposal_kwargs),
                    self.entity,
                )
            )
        return rows

    def _post(self, group_id: str, candidate_ids: list[int], **extra):
        payload = {"candidate_id": [str(pk) for pk in candidate_ids]}
        payload.update(extra)
        return self.client.post(
            reverse(APPROVE_URL, kwargs={"group_id": group_id}),
            payload,
        )

    def _statuses(self, rows) -> list[str]:
        for row in rows:
            row.refresh_from_db()
        return [row.status for row in rows]

    def test_clean_group_approves_every_member_from_current_text(self):
        rows = []
        for index in range(3):
            item = _item(f"פריט {index}")
            rows.append(
                _candidate(
                    _proposal(item, BODY, matched_text="היסטורי"),
                    self.entity,
                )
            )
        sibling = _candidate(
            _proposal(_item("שכן"), BODY),
            self.entity,
            matches=((DISPLAY, ""),),
        )
        group = _group_for([row.pk for row in rows])
        before = _counts()

        response = self._post(group.group_id, [row.pk for row in rows], note=NOTE)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse(GROUPS))
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.status, APPROVED)
            self.assertEqual(row.resolved_entity_id, self.entity.pk)
            self.assertEqual(row.reviewed_by_id, self.staff.pk)
        sibling.refresh_from_db()
        self.assertEqual(sibling.status, PENDING)
        occurrences = list(
            ArchiveItemEntityOccurrence.objects.order_by("archive_item_id")
        )
        self.assertEqual(len(occurrences), 3)
        for occurrence in occurrences:
            self.assertEqual(occurrence.matched_text, SURFACE)
            self.assertNotEqual(occurrence.matched_text, "היסטורי")
            self.assertIsNone(occurrence.decision_id)
            self.assertEqual(occurrence.entity_id, self.entity.pk)
            self.assertEqual(occurrence.resolution_status, RESOLVED)
        events = NonPersonEntityOccurrenceReviewEvent.objects.filter(action=APPROVE)
        self.assertEqual(events.count(), 3)
        self.assertEqual({event.note for event in events}, {NOTE})
        self.assertEqual({event.to_entity_id for event in events}, {self.entity.pk})
        self.assertEqual({event.actor_id for event in events}, {self.staff.pk})
        after = _counts()
        self.assertEqual(after["aliases"], before["aliases"])
        self.assertEqual(after["entities"], before["entities"])
        self.assertEqual(after["decisions"], before["decisions"])
        self.assertEqual(after["search"], before["search"])
        landed = self.client.get(response.url)
        self.assertContains(landed, "אושרו 3 אזכורים.")
        later = self.client.get(reverse("archive-manage-list"))
        self.assertNotContains(later, "אושרו 3 אזכורים.")

    def test_one_stale_member_blocks_the_whole_group(self):
        rows = self._pending_group(2)
        stale = _candidate(
            _proposal(_item("ישן"), BODY, sha="0" * 64),
            self.entity,
        )
        rows.append(stale)
        group = _group_for([row.pk for row in rows])
        self.assertIn(SOURCE_STALE, group.risk_flags)
        before = _counts()

        response = self._post(group.group_id, [row.pk for row in rows])

        self.assertEqual(response.status_code, 302)
        self.assertEqual(_counts(), before)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING, PENDING])
        landed = self.client.get(response.url)
        self.assertContains(landed, GROUP_BLOCKED_MESSAGE)
        self.assertNotContains(self.client.get(reverse(GROUPS)), GROUP_BLOCKED_MESSAGE)

    def test_one_unlocatable_member_blocks_the_whole_group(self):
        rows = self._pending_group(2)
        missing = _candidate(
            _proposal(_item("חסר"), BODY, ordinal=4),
            self.entity,
        )
        rows.append(missing)
        group = _group_for([row.pk for row in rows])
        self.assertIn(CONTEXT_UNAVAILABLE, group.risk_flags)
        self.assertNotIn(SOURCE_STALE, group.risk_flags)

        self._post(group.group_id, [row.pk for row in rows])

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING, PENDING])

    def test_unsupported_normalization_blocks_the_whole_group(self):
        rows = self._pending_group(2)
        unknown = _candidate(
            _proposal(_item("גרסה"), BODY, normalization_version="surface-v9"),
            self.entity,
        )
        rows.append(unknown)
        group = _group_for([row.pk for row in rows])

        self._post(group.group_id, [row.pk for row in rows], confirm_warnings="1")

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING, PENDING])
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_occurrence_conflict_rolls_back_the_group(self):
        rows = self._pending_group(2)
        other = _entity("אלכסנדריה")
        conflict_item = _item("התנגשות")
        conflict = _candidate(_proposal(conflict_item, BODY), self.entity)
        pin = ArchiveItemEntityOccurrence.objects.create(
            archive_item=conflict_item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(BODY),
            normalization_version="surface-v1",
            normalized_surface=SURFACE,
            occurrence_ordinal=1,
            resolution_status=RESOLVED,
            entity=other,
            matched_text="שמור",
        )
        rows.append(conflict)
        group = _group_for([row.pk for row in rows])

        response = self._post(group.group_id, [row.pk for row in rows])

        pin.refresh_from_db()
        self.assertEqual(pin.entity_id, other.pk)
        self.assertEqual(pin.matched_text, "שמור")
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 1)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING, PENDING])
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        landed = self.client.get(response.url)
        self.assertContains(landed, OCCURRENCE_CONFLICT_MESSAGE)
        self.assertNotContains(
            self.client.get(reverse(GROUPS)),
            OCCURRENCE_CONFLICT_MESSAGE,
        )

    def test_same_entity_occurrence_is_kept_and_the_group_still_approves(self):
        pinned_item = _item("קיים")
        pinned = _candidate(_proposal(pinned_item, BODY), self.entity)
        pin = ArchiveItemEntityOccurrence.objects.create(
            archive_item=pinned_item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(BODY),
            normalization_version="surface-v1",
            normalized_surface=SURFACE,
            occurrence_ordinal=1,
            resolution_status=RESOLVED,
            entity=self.entity,
            matched_text="שמור",
        )
        others = self._pending_group(2)
        rows = [pinned, *others]
        group = _group_for([row.pk for row in rows])

        self._post(group.group_id, [row.pk for row in rows])

        pin.refresh_from_db()
        self.assertEqual(pin.matched_text, "שמור")
        self.assertEqual(pin.entity_id, self.entity.pk)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 3)
        self.assertEqual(self._statuses(rows), [APPROVED, APPROVED, APPROVED])
        created = ArchiveItemEntityOccurrence.objects.exclude(pk=pin.pk)
        self.assertEqual({row.matched_text for row in created}, {SURFACE})

    def test_missing_extra_and_duplicate_ids_write_nothing(self):
        rows = self._pending_group(2)
        extra = _candidate(_proposal(_item("נוסף"), BODY), _entity("אחר"))
        group = _group_for([row.pk for row in rows])
        ids = [row.pk for row in rows]

        self._post(group.group_id, ids[:1])
        self._post(group.group_id, [*ids, extra.pk])
        self._post(group.group_id, [ids[0], ids[0], ids[1]])
        self._post(group.group_id, ["nope", *ids])

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses([*rows, extra]), [PENDING, PENDING, PENDING])
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_status_change_after_confirmation_writes_nothing(self):
        rows = self._pending_group(2)
        group = _group_for([row.pk for row in rows])
        rows[0].status = NEEDS_RESEARCH
        rows[0].save(update_fields=["status", "updated_at"])

        response = self._post(group.group_id, [row.pk for row in rows])

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [NEEDS_RESEARCH, PENDING])
        landed = self.client.get(response.url)
        self.assertContains(landed, GROUP_MEMBERSHIP_CHANGED_MESSAGE)
        self.assertNotContains(
            self.client.get(reverse(GROUPS)),
            GROUP_MEMBERSHIP_CHANGED_MESSAGE,
        )

    def test_group_key_change_after_confirmation_writes_nothing(self):
        rows = self._pending_group(2)
        group = _group_for([row.pk for row in rows])
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=rows[1],
            method=DISPLAY,
            matched_value="שינוי",
            alias_kind="",
        )

        self._post(group.group_id, [row.pk for row in rows])

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING])

    def test_second_post_does_not_report_success_or_duplicate_rows(self):
        rows = self._pending_group(2)
        group = _group_for([row.pk for row in rows])
        ids = [row.pk for row in rows]

        first = self._post(group.group_id, ids, note=NOTE)
        self.assertEqual(first.status_code, 302)
        self.client.get(first.url)
        event_count = NonPersonEntityOccurrenceReviewEvent.objects.count()
        occurrence_count = ArchiveItemEntityOccurrence.objects.count()

        second = self._post(group.group_id, ids, note=NOTE)
        landed = self.client.get(second.url)

        self.assertEqual(second.status_code, 302)
        self.assertEqual(second.url, reverse(GROUPS))
        self.assertContains(landed, GROUP_MEMBERSHIP_CHANGED_MESSAGE)
        self.assertNotContains(landed, "אושרו")
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.count(),
            event_count,
        )
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), occurrence_count)
        self.assertNotContains(
            self.client.get(reverse(GROUPS)),
            GROUP_MEMBERSHIP_CHANGED_MESSAGE,
        )

    def test_later_approve_candidate_failure_rolls_back_earlier_members(self):
        rows = self._pending_group(2)
        group = _group_for([row.pk for row in rows])
        real = approve_candidate
        calls = {"count": 0}

        def _flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] > 1:
                raise StaleSourceReviewError("stale")
            return real(*args, **kwargs)

        module = "documents.services.non_person_entity_occurrence_grouped_review"
        with patch(f"{module}.approve_candidate", side_effect=_flaky):
            with self.assertRaises(StaleSourceReviewError):
                approve_pending_occurrence_group(
                    group.group_id,
                    [row.pk for row in rows],
                    actor=self.staff,
                )

        self.assertEqual(calls["count"], 2)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [PENDING, PENDING])

    def test_each_blocking_flag_hides_submit_and_refuses_post(self):
        cases = {
            OCR_VARIANT: dict(
                matches=((ALIAS, NonPersonEntityAlias.Kind.OCR_VARIANT),)
            ),
            SHORT_SURFACE: dict(surface="עיר", body="עיר גדולה", matched_text="עיר"),
            MULTIPLE_MATCH_METHODS: dict(
                matches=((CANONICAL, ""), (DISPLAY, "")),
            ),
            MULTIPLE_ALIAS_KINDS: dict(
                matches=(
                    (ALIAS, NonPersonEntityAlias.Kind.LANGUAGE_VARIANT),
                    (ALIAS, NonPersonEntityAlias.Kind.SPELLING_VARIANT),
                ),
            ),
            SOURCE_STALE: dict(sha="0" * 64),
            CONTEXT_UNAVAILABLE: dict(ordinal=4),
            PRIOR_REVIEW_HISTORY: dict(prior_event=True),
        }
        self.assertEqual(
            set(cases),
            {
                OCR_VARIANT,
                SHORT_SURFACE,
                MULTIPLE_MATCH_METHODS,
                MULTIPLE_ALIAS_KINDS,
                SOURCE_STALE,
                CONTEXT_UNAVAILABLE,
                PRIOR_REVIEW_HISTORY,
            },
        )
        for flag, options in cases.items():
            with self.subTest(flag=flag):
                self._assert_flag_blocks(flag, **options)

    def _assert_flag_blocks(self, flag: str, **options):
        matches = options.get("matches", ((CANONICAL, ""),))
        body = options.get("body", BODY)
        surface = options.get("surface", SURFACE)
        entity = _entity(f"ישות {flag}")
        item = _item(f"סיכון {flag}", body)
        candidate = _candidate(
            _proposal(
                item,
                body,
                surface=surface,
                matched_text=options.get("matched_text", surface),
                ordinal=options.get("ordinal", 1),
                sha=options.get("sha"),
            ),
            entity,
            matches=matches,
        )
        if options.get("prior_event"):
            NonPersonEntityOccurrenceReviewEvent.objects.create(
                proposal=candidate.proposal,
                candidate=candidate,
                action=PROPOSE,
            )
        group = _group_for([candidate.pk])
        self.assertIn(flag, group.risk_flags)
        page = self.client.get(
            reverse(APPROVE_URL, kwargs={"group_id": group.group_id})
        )
        detail = self.client.get(reverse(GROUP, kwargs={"group_id": group.group_id}))
        self.assertNotContains(page, "אישור כל האזכורים בקבוצה")
        self.assertNotContains(detail, "אישור כל הקבוצה")
        self.assertContains(page, GROUP_BLOCKED_MESSAGE)
        before = ArchiveItemEntityOccurrence.objects.count()
        response = self._post(
            group.group_id,
            [candidate.pk],
            confirm_warnings="1",
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), before)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)

    def test_blank_matched_text_requires_confirmation_then_uses_relocated_slice(self):
        candidate = _candidate(
            _proposal(_item("ריק"), BODY, matched_text=""),
            self.entity,
        )
        group = _group_for([candidate.pk])
        self.assertIn(MATCHED_TEXT_UNAVAILABLE, group.risk_flags)
        self.assertEqual(group.risk_flags, (MATCHED_TEXT_UNAVAILABLE,))
        page = self.client.get(
            reverse(APPROVE_URL, kwargs={"group_id": group.group_id})
        )
        self.assertContains(page, "אישור כל האזכורים בקבוצה")
        self.assertContains(page, 'name="confirm_warnings"')

        refused = self._post(group.group_id, [candidate.pk])
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertContains(
            self.client.get(refused.url), GROUP_WARNINGS_UNCONFIRMED_MESSAGE
        )
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)

        accepted = self._post(
            group.group_id,
            [candidate.pk],
            confirm_warnings="1",
        )
        self.assertEqual(accepted.status_code, 302)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.matched_text, SURFACE)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, APPROVED)
        self.assertNotContains(
            self.client.get(reverse(GROUPS)),
            GROUP_WARNINGS_UNCONFIRMED_MESSAGE,
        )

    def test_many_archive_items_requires_confirmation(self):
        rows = self._pending_group(5)
        group = _group_for([row.pk for row in rows])
        self.assertEqual(group.risk_flags, (MANY_ARCHIVE_ITEMS,))
        ids = [row.pk for row in rows]

        self._post(group.group_id, ids)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(self._statuses(rows), [PENDING] * 5)

        self._post(group.group_id, ids, confirm_warnings="1")
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 5)
        self.assertEqual(self._statuses(rows), [APPROVED] * 5)

    def test_blocking_flag_is_not_overridden_by_warning_confirmation(self):
        candidate = _candidate(
            _proposal(_item("חסום"), BODY, matched_text=""),
            self.entity,
            matches=((ALIAS, NonPersonEntityAlias.Kind.OCR_VARIANT),),
        )
        group = _group_for([candidate.pk])
        self.assertIn(OCR_VARIANT, group.risk_flags)
        self.assertIn(MATCHED_TEXT_UNAVAILABLE, group.risk_flags)

        response = self._post(
            group.group_id,
            [candidate.pk],
            confirm_warnings="1",
        )

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertContains(self.client.get(response.url), GROUP_BLOCKED_MESSAGE)

    def test_non_pending_statuses_are_not_part_of_the_approved_group(self):
        pending = self._pending_group(2)
        other_item = _item("אחר")
        proposal = _proposal(other_item, BODY)
        needs = _candidate(proposal, self.entity, status=NEEDS_RESEARCH)
        rejected = _candidate(
            _proposal(_item("נדחה"), BODY),
            self.entity,
            status=REJECTED,
        )
        approved = _candidate(
            _proposal(_item("כבר"), BODY),
            self.entity,
            status=APPROVED,
            resolved=self.entity,
        )
        removed = _candidate(
            _proposal(_item("הוסר"), BODY),
            self.entity,
            status=REMOVED,
            resolved=self.entity,
        )
        group = _group_for([row.pk for row in pending])
        self.assertNotIn(needs.pk, group.candidate_ids)

        self._post(group.group_id, [row.pk for row in pending])

        self.assertEqual(self._statuses(pending), [APPROVED, APPROVED])
        self.assertEqual(self._statuses([needs]), [NEEDS_RESEARCH])
        self.assertEqual(self._statuses([rejected]), [REJECTED])
        self.assertEqual(self._statuses([approved]), [APPROVED])
        self.assertEqual(self._statuses([removed]), [REMOVED])
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 2)
        self.assertFalse(
            ArchiveItemEntityOccurrence.objects.filter(archive_item=other_item).exists()
        )

    def test_confirm_page_lists_members_beyond_the_first_detail_page(self):
        count = GROUP_DETAIL_PAGE_SIZE + 1
        body = "קהיר " * count
        item = _item("פריט ארוך", body)
        rows = [
            _candidate(_proposal(item, body, ordinal=ordinal), self.entity)
            for ordinal in range(1, count + 1)
        ]
        group = _group_for([row.pk for row in rows])
        last = rows[-1]
        review_url = reverse(
            "archive-manage-entity-occurrence-proposal",
            kwargs={"candidate_id": last.pk},
        )
        module = "documents.services.non_person_entity_occurrence_grouped_review"
        with patch(f"{module}._context_parts", wraps=_context_parts) as windows:
            preview = staff_occurrence_group_approve_preview(group.group_id)
            page = self.client.get(
                reverse(APPROVE_URL, kwargs={"group_id": group.group_id})
            )
        detail = self.client.get(reverse(GROUP, kwargs={"group_id": group.group_id}))

        self.assertIsNotNone(preview)
        self.assertEqual(len(preview.members), count)
        self.assertEqual(preview.members[-1].candidate_id, last.pk)
        self.assertEqual(windows.call_count, 0)
        self.assertContains(page, f'value="{last.pk}"')
        self.assertContains(page, review_url)
        self.assertContains(page, "האישור בודק מחדש את הטקסט הנוכחי")
        self.assertContains(page, 'name="candidate_id"', count=count)
        self.assertNotContains(detail, review_url)

    def test_note_is_copied_onto_each_approve_event(self):
        rows = self._pending_group(2)
        group = _group_for([row.pk for row in rows])

        self._post(group.group_id, [row.pk for row in rows], note=f"  {NOTE}  ")

        notes = list(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(action=APPROVE)
            .order_by("candidate_id")
            .values_list("note", flat=True)
        )
        self.assertEqual(notes, [NOTE, NOTE])

    def test_auth_methods_and_get_do_not_write(self):
        rows = self._pending_group(1)
        group = _group_for([row.pk for row in rows])
        url = reverse(APPROVE_URL, kwargs={"group_id": group.group_id})
        before = _counts()

        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(
            self.client.post(reverse(GROUPS), {"action": "approve"}).status_code, 405
        )
        self.assertEqual(
            self.client.post(
                reverse(GROUP, kwargs={"group_id": group.group_id})
            ).status_code,
            405,
        )
        self.assertEqual(self.client.put(url).status_code, 405)
        self.assertEqual(
            self.client.get(
                reverse(APPROVE_URL, kwargs={"group_id": "zz"})
            ).status_code,
            404,
        )
        self.assertEqual(_counts(), before)

        self.client.logout()
        anonymous = self.client.get(url)
        self.assertEqual(anonymous.status_code, 302)
        self.assertTrue(anonymous.url.startswith("/accounts/login/"))

        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(username="family_group", password="test-pass")
        family.groups.add(family_group)
        self.client.force_login(family)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self._post(group.group_id, [rows[0].pk]).status_code, 403)

        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        denied = csrf_client.post(url, {"candidate_id": [str(rows[0].pk)]})
        self.assertEqual(denied.status_code, 403)
        rows[0].refresh_from_db()
        self.assertEqual(rows[0].status, PENDING)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)

    def test_queue_has_no_group_approve_control(self):
        self._pending_group(1)
        page = self.client.get(reverse(GROUPS))
        self.assertContains(page, "אישור קבוצה, כשהוא אפשרי")
        self.assertNotContains(page, "אישור כל האזכורים בקבוצה")
        self.assertNotContains(page, "אישור כל הקבוצה")
        self.assertNotContains(page, 'name="candidate_id"')

    def test_eligible_detail_links_to_whole_group_confirmation(self):
        rows = self._pending_group(1)
        group = _group_for([row.pk for row in rows])
        detail = self.client.get(reverse(GROUP, kwargs={"group_id": group.group_id}))
        self.assertContains(detail, "לא רק על העמוד הזה")
        self.assertContains(
            detail,
            reverse(APPROVE_URL, kwargs={"group_id": group.group_id}),
        )
