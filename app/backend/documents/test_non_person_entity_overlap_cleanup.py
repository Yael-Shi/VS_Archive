"""Cleanup of pending contained hits stored before overlap suppression."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_detector import _AcceptedHit
from documents.services.non_person_entity_occurrence_review import (
    StaleSourceReviewError,
    _read_authoritative_text,
    reject_candidate,
)
from documents.services.non_person_entity_occurrences import (
    authoritative_displayed_text,
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_overlap_cleanup import (
    OVERLAP_CLEANUP_NOTE,
    cleanup_non_person_overlap_candidates,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
NEEDS_RESEARCH = NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
APPROVED = NonPersonEntityOccurrenceCandidate.Status.APPROVED
REJECTED = NonPersonEntityOccurrenceCandidate.Status.REJECTED
REMOVED = NonPersonEntityOccurrenceCandidate.Status.REMOVED
REJECT_ACTION = NonPersonEntityOccurrenceReviewEvent.Action.REJECT
CANONICAL = NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME
COMMAND = "cleanup_non_person_overlap_candidates"


def _place(name: str, subtype: str) -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=subtype,
    )


def _manual(body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="פריט",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _ocr(body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="ocr",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    document = Document.objects.create(
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        language="he",
        archive_item=item,
        upload_status=Document.UploadStatus.UPLOADED,
    )
    shared = {
        "document": document,
        "status": DocumentTextResult.Status.NEEDS_REVIEW,
        "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
        "engine": "gemini-test",
    }
    DocumentTextResult.objects.create(
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        text="other",
        **shared,
    )
    DocumentTextResult.objects.create(
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
        text=body,
        **shared,
    )
    return item


def _proposal(
    item: ArchiveItem,
    body: str,
    surface: str,
    ordinal: int,
    *,
    normalization: str = "surface-v1",
    sha: str | None = None,
    text_kind: str | None = None,
) -> NonPersonEntityOccurrenceProposal:
    if text_kind is None:
        text_kind = (
            OCR if item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT else MANUAL
        )
    return NonPersonEntityOccurrenceProposal.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version=normalization,
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        matched_text=surface,
    )


def _candidate(
    proposal: NonPersonEntityOccurrenceProposal,
    entity: NonPersonEntity,
    *,
    status: str = PENDING,
    resolved: NonPersonEntity | None = None,
    detect: bool = True,
) -> NonPersonEntityOccurrenceCandidate:
    row = NonPersonEntityOccurrenceCandidate.objects.create(
        proposal=proposal,
        candidate_entity=entity,
        status=status,
        resolved_entity=resolved,
    )
    NonPersonEntityOccurrenceCandidateMatch.objects.create(
        candidate=row,
        method=CANONICAL,
        matched_value=entity.canonical_name,
    )
    if detect:
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=row,
            action=NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
            to_entity=entity,
        )
    return row


def _contained_pair(body: str, *, ocr: bool = False):
    item = _ocr(body) if ocr else _manual(body)
    country = _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
    region = _place(
        "ארץ ישראל",
        NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA,
    )
    return item, country, region


class OverlapCleanupTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username="cleaner", password="x")
        self.staff = User.objects.create_user(
            username="staff-cleaner",
            password="x",
            is_staff=True,
        )

    def test_dry_run_reports_contained_pending_hit_and_writes_nothing(self):
        body = "ארץ ישראל"
        item, country, region = _contained_pair(body)
        contained = _proposal(item, body, "ישראל", 1)
        contained_candidate = _candidate(contained, country)
        survivor = _proposal(item, body, "ארץ ישראל", 1)
        survivor_candidate = _candidate(survivor, region)
        before = _row_counts()

        out = StringIO()
        call_command(COMMAND, "--item", str(item.pk), stdout=out)
        text = out.getvalue()

        contained_candidate.refresh_from_db()
        survivor_candidate.refresh_from_db()
        self.assertEqual(contained_candidate.status, PENDING)
        self.assertEqual(survivor_candidate.status, PENDING)
        self.assertEqual(_row_counts(), before)
        self.assertFalse(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).exists()
        )
        self.assertIn(f"proposal_id: {contained.pk}", text)
        self.assertNotIn(f"proposal_id: {survivor.pk}", text)
        self.assertIn(f"archive_item_id: {item.pk}", text)
        self.assertIn("text_kind: MANUAL_TEXT", text)
        self.assertIn("normalized_surface: ישראל", text)
        self.assertIn("occurrence_ordinal: 1", text)
        self.assertIn(f"source_text_sha256: {contained.source_text_sha256}", text)
        self.assertIn("covering_surface: ארץ ישראל", text)
        self.assertIn("covering_ordinal: 1", text)
        self.assertIn(f"candidate_ids: {contained_candidate.pk}", text)
        self.assertIn(f"candidate_entity_ids: {country.pk}", text)
        self.assertIn("mode: dry-run", text)
        self.assertIn("proposals_examined: 2", text)
        self.assertIn("not_contained: 1", text)
        self.assertIn("eligible_proposals: 1", text)
        self.assertIn("eligible_candidates: 1", text)
        self.assertIn("rejected_candidates: 0", text)
        self.assertIn("errors: 0", text)

    def test_apply_rejects_through_reject_candidate_and_second_apply_is_idempotent(
        self,
    ):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)
        sha = proposal.source_text_sha256
        before = _row_counts()

        with patch(
            "documents.services.non_person_entity_overlap_cleanup.reject_candidate",
            wraps=reject_candidate,
        ) as spy:
            first = cleanup_non_person_overlap_candidates(
                [item.pk],
                apply=True,
                actor=self.actor,
            )

        candidate.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args.kwargs["note"], OVERLAP_CLEANUP_NOTE)
        self.assertEqual(spy.call_args.kwargs["actor"], self.actor)
        self.assertEqual(candidate.status, REJECTED)
        self.assertIsNone(candidate.resolved_entity_id)
        self.assertEqual(candidate.reviewed_by_id, self.actor.pk)
        self.assertEqual(proposal.source_text_sha256, sha)
        self.assertEqual(proposal.normalized_surface, "ישראל")
        self.assertEqual(proposal.occurrence_ordinal, 1)
        self.assertEqual(proposal.normalization_version, "surface-v1")
        self.assertEqual(first.eligible_proposals, 1)
        self.assertEqual(first.eligible_candidates, 1)
        self.assertEqual(first.rejected_candidates, 1)
        self.assertEqual(_row_counts()[0:3], before[0:3])
        self.assertEqual(_row_counts()[3], before[3] + 1)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        reject_events = NonPersonEntityOccurrenceReviewEvent.objects.filter(
            action=REJECT_ACTION
        )
        self.assertEqual(reject_events.count(), 1)
        self.assertEqual(reject_events.get().note, OVERLAP_CLEANUP_NOTE)
        self.assertEqual(reject_events.get().actor_id, self.actor.pk)
        self.assertEqual(reject_events.get().candidate_id, candidate.pk)
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=NonPersonEntityOccurrenceReviewEvent.Action.DETECT
            ).count(),
            1,
        )

        out = StringIO()
        call_command(
            COMMAND,
            "--item",
            str(item.pk),
            "--apply",
            "--actor-id",
            str(self.staff.pk),
            stdout=out,
        )
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, REJECTED)
        self.assertEqual(reject_events.count(), 1)
        self.assertIn("mixed_or_nonpending_status: 1", out.getvalue())
        self.assertIn("eligible_proposals: 0", out.getvalue())
        self.assertIn("rejected_candidates: 0", out.getvalue())
        self.assertIn("mode: apply", out.getvalue())

    def test_ordinary_user_cannot_apply(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)
        before = _row_counts()

        with patch(
            "documents.management.commands.cleanup_non_person_overlap_candidates."
            "cleanup_non_person_overlap_candidates"
        ) as service:
            with self.assertRaises(CommandError):
                call_command(
                    COMMAND,
                    "--item",
                    str(item.pk),
                    "--apply",
                    "--actor-id",
                    str(self.actor.pk),
                )

        service.assert_not_called()
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(_row_counts(), before)
        self.assertFalse(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).exists()
        )

    def test_staff_user_can_apply(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)

        call_command(
            COMMAND,
            "--item",
            str(item.pk),
            "--apply",
            "--actor-id",
            str(self.staff.pk),
        )

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, REJECTED)
        self.assertEqual(candidate.reviewed_by_id, self.staff.pk)
        reject_events = NonPersonEntityOccurrenceReviewEvent.objects.filter(
            action=REJECT_ACTION
        )
        self.assertEqual(reject_events.count(), 1)
        self.assertEqual(reject_events.get().actor_id, self.staff.pk)
        self.assertEqual(reject_events.get().candidate_id, candidate.pk)

    def test_detect_event_on_another_proposal_is_not_provenance(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country, detect=False)
        other_item = _manual("אחר")
        other = _proposal(other_item, "אחר", "ישראל", 1)
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=other,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
            to_entity=country,
        )

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.proposals_examined, 1)
        self.assertEqual(report.missing_detect_event, 1)
        self.assertEqual(report.eligible_proposals, 0)
        self.assertEqual(report.rejected_candidates, 0)
        self.assertFalse(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).exists()
        )

    def test_apply_rejects_ocr_contained_hit(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body, ocr=True)
        displayed = authoritative_displayed_text(item, OCR)
        self.assertEqual(displayed, body)
        proposal = _proposal(item, displayed, "ישראל", 1)
        candidate = _candidate(proposal, country)

        report = cleanup_non_person_overlap_candidates(
            [item.pk],
            apply=True,
            actor=self.actor,
        )

        candidate.refresh_from_db()
        self.assertEqual(proposal.text_kind, OCR)
        self.assertEqual(candidate.status, REJECTED)
        self.assertEqual(report.eligible_proposals, 1)
        self.assertEqual(report.eligible[0].text_kind, OCR)
        self.assertEqual(report.eligible[0].covering_surface, "ארץ ישראל")

    def test_standalone_short_surface_is_not_eligible(self):
        body = "ישראל"
        item = _manual(body)
        country = _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place(
            "ארץ ישראל",
            NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA,
        )
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.not_contained, 1)
        self.assertEqual(report.eligible_proposals, 0)
        self.assertEqual(report.rejected_candidates, 0)

    def test_stale_sha_is_not_eligible(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1, sha="a" * 64)
        candidate = _candidate(proposal, country)

        report = self._apply(item)

        candidate.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(proposal.source_text_sha256, "a" * 64)
        self.assertEqual(report.stale_source, 1)
        self.assertEqual(report.eligible_proposals, 0)

    def test_wrong_normalization_version_is_not_eligible(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1, normalization="surface-v2")
        candidate = _candidate(proposal, country)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.wrong_normalization_version, 1)
        self.assertEqual(report.rejected_candidates, 0)

    def test_stored_ordinal_that_is_not_accepted_is_not_eligible(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 2)
        candidate = _candidate(proposal, country)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.not_currently_accepted, 1)

    def test_nonpending_and_mixed_status_skip_the_whole_proposal(self):
        cases = (
            (APPROVED, True),
            (NEEDS_RESEARCH, False),
            (REJECTED, False),
            (REMOVED, False),
        )
        for status, needs_resolved in cases:
            with self.subTest(status=status):
                body = "ארץ ישראל"
                item, country, _region = _contained_pair(body)
                proposal = _proposal(item, body, "ישראל", 1)
                candidate = _candidate(
                    proposal,
                    country,
                    status=status,
                    resolved=country if needs_resolved else None,
                )
                report = self._apply(item)
                candidate.refresh_from_db()
                self.assertEqual(candidate.status, status)
                self.assertEqual(report.mixed_or_nonpending_status, 1)
                self.assertEqual(report.rejected_candidates, 0)
                self.assertFalse(
                    NonPersonEntityOccurrenceReviewEvent.objects.filter(
                        proposal=proposal,
                        action=REJECT_ACTION,
                    ).exists()
                )

        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        other = _place("מדינת ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        proposal = _proposal(item, body, "ישראל", 1)
        pending = _candidate(proposal, country)
        researched = _candidate(proposal, other, status=NEEDS_RESEARCH)
        report = self._apply(item)
        pending.refresh_from_db()
        researched.refresh_from_db()
        self.assertEqual(pending.status, PENDING)
        self.assertEqual(researched.status, NEEDS_RESEARCH)
        self.assertEqual(report.mixed_or_nonpending_status, 1)
        self.assertEqual(report.rejected_candidates, 0)

    def test_existing_occurrence_skips_the_proposal(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)
        occurrence = ArchiveItemEntityOccurrence.objects.create(
            archive_item=item,
            text_kind=proposal.text_kind,
            source_text_sha256=proposal.source_text_sha256,
            normalization_version=proposal.normalization_version,
            normalized_surface=proposal.normalized_surface,
            occurrence_ordinal=proposal.occurrence_ordinal,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=country,
            matched_text="ישראל",
        )

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertTrue(
            ArchiveItemEntityOccurrence.objects.filter(pk=occurrence.pk).exists()
        )
        self.assertEqual(report.has_authoritative_occurrence, 1)
        self.assertEqual(report.rejected_candidates, 0)

    def test_missing_detect_event_skips_the_whole_proposal(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country, detect=False)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.missing_detect_event, 1)
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).count(),
            0,
        )

        other_item, other_country, _region_again = _contained_pair(body)
        sibling = _place("מדינת ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        mixed = _proposal(other_item, body, "ישראל", 1)
        with_event = _candidate(mixed, other_country)
        without_event = _candidate(mixed, sibling, detect=False)
        report = self._apply(other_item)
        with_event.refresh_from_db()
        without_event.refresh_from_db()
        self.assertEqual(with_event.status, PENDING)
        self.assertEqual(without_event.status, PENDING)
        self.assertEqual(report.missing_detect_event, 1)
        self.assertEqual(report.rejected_candidates, 0)

    def test_same_surface_ambiguity_is_not_eligible(self):
        country = _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        NonPersonEntityAlias.objects.create(
            entity=country,
            name="Israël",
            kind=NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT,
        )
        newspaper = NonPersonEntity.objects.create(
            canonical_name="Israël",
            entity_type=NonPersonEntity.EntityType.PUBLICATION_WORK,
            entity_subtype=NonPersonEntity.EntitySubtype.NEWSPAPER,
        )
        body = "La Tribune Juive, ISRAËL, L'Aurore"
        item = _manual(body)
        surface = normalize_surface_v1("Israël")
        proposal = _proposal(item, body, surface, 1)
        first = _candidate(proposal, country)
        second = _candidate(proposal, newspaper)

        report = self._apply(item)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, PENDING)
        self.assertEqual(second.status, PENDING)
        self.assertEqual(report.not_contained, 1)
        self.assertEqual(report.eligible_proposals, 0)

    def test_partial_overlap_is_not_eligible(self):
        body = "ארץ ישראל הגדולה"
        item = _manual(body)
        region = _place(
            "ארץ ישראל",
            NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA,
        )
        _place(
            "ישראל הגדולה",
            NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA,
        )
        proposal = _proposal(item, body, "ארץ ישראל", 1)
        candidate = _candidate(proposal, region)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.not_contained, 1)

    def test_equal_original_span_is_not_eligible(self):
        shared = {
            "matched_text": "a",
            "original_start": 0,
            "original_end": 1,
            "reasons_by_entity": {},
        }
        shorter = _AcceptedHit(
            surface="aa",
            ordinal=1,
            normalized_start=0,
            normalized_end=2,
            **shared,
        )
        longer = _AcceptedHit(
            surface="aaaa",
            ordinal=1,
            normalized_start=0,
            normalized_end=4,
            **shared,
        )
        body = "a"
        item = _manual(body)
        short_entity = _place("aa", NonPersonEntity.EntitySubtype.CITY)
        _place("aaaa", NonPersonEntity.EntitySubtype.CITY)
        proposal = _proposal(item, body, "aa", 1)
        candidate = _candidate(proposal, short_entity)

        def fake_accepted(_prepared, surface, _reasons):
            if surface == "aa":
                return (shorter,)
            if surface == "aaaa":
                return (longer,)
            return ()

        with patch(
            "documents.services.non_person_entity_detector._accepted_hits",
            side_effect=fake_accepted,
        ):
            report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.not_contained, 1)
        self.assertEqual(report.eligible_proposals, 0)
        self.assertEqual(report.rejected_candidates, 0)

    def test_hebrew_prefix_without_longer_accepted_hit_is_not_eligible(self):
        body = "נסע לארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)

        report = self._apply(item)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.not_contained, 1)

    def test_apply_revalidation_skips_when_authoritative_text_drifts(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)
        depth_before = len(connection.savepoint_ids)
        calls = {"n": 0}

        def drift(archive_item, text_kind):
            calls["n"] += 1
            if len(connection.savepoint_ids) <= depth_before:
                raise AssertionError(
                    "authoritative text was read outside the apply transaction"
                )
            self.assertEqual(
                _read_authoritative_text(archive_item, text_kind),
                body,
            )
            return "טקסט אחר"

        with patch(
            "documents.services.non_person_entity_overlap_cleanup._read_authoritative_text",
            side_effect=drift,
        ):
            report = self._apply(item)

        candidate.refresh_from_db()
        proposal.refresh_from_db()
        self.assertEqual(calls["n"], 1)
        self.assertEqual(
            ManualTextContent.objects.get(archive_item=item).body,
            body,
        )
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(proposal.source_text_sha256, source_text_sha256(body))
        self.assertEqual(report.stale_source, 1)
        self.assertEqual(report.errors, 0)
        self.assertEqual(report.rejected_candidates, 0)
        self.assertFalse(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).exists()
        )

    def test_failed_reject_rolls_back_the_whole_proposal(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        other = _place("מדינת ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        proposal = _proposal(item, body, "ישראל", 1)
        first = _candidate(proposal, country)
        second = _candidate(proposal, other)
        real = reject_candidate
        calls = {"n": 0}

        def fail_second(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise StaleSourceReviewError("drift")
            return real(*args, **kwargs)

        with patch(
            "documents.services.non_person_entity_overlap_cleanup.reject_candidate",
            side_effect=fail_second,
        ):
            report = self._apply(item)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, PENDING)
        self.assertEqual(second.status, PENDING)
        self.assertEqual(report.stale_source, 1)
        self.assertEqual(report.errors, 0)
        self.assertEqual(report.rejected_candidates, 0)
        self.assertFalse(
            NonPersonEntityOccurrenceReviewEvent.objects.filter(
                action=REJECT_ACTION
            ).exists()
        )

    def test_error_on_one_proposal_does_not_reject_it_or_stop_the_next(self):
        body = "ארץ ישראל"
        first_item, first_country, _region = _contained_pair(body)
        second_item, second_country, _region_again = _contained_pair(body)
        first_proposal = _proposal(first_item, body, "ישראל", 1)
        second_proposal = _proposal(second_item, body, "ישראל", 1)
        first_candidate = _candidate(first_proposal, first_country)
        second_candidate = _candidate(second_proposal, second_country)
        real = cleanup_classify()

        def boom(proposal, candidates):
            if proposal.pk == first_proposal.pk:
                raise RuntimeError("boom")
            return real(proposal, candidates)

        with patch(
            "documents.services.non_person_entity_overlap_cleanup._classify",
            side_effect=boom,
        ):
            report = cleanup_non_person_overlap_candidates(
                [first_item.pk, second_item.pk],
                apply=True,
                actor=self.actor,
            )

        first_candidate.refresh_from_db()
        second_candidate.refresh_from_db()
        self.assertEqual(first_candidate.status, PENDING)
        self.assertEqual(second_candidate.status, REJECTED)
        self.assertEqual(report.errors, 1)
        self.assertEqual(report.rejected_candidates, 1)
        self.assertIn("boom", report.error_lines[0])

    def test_other_item_is_not_examined(self):
        body = "ארץ ישראל"
        chosen, country, _region = _contained_pair(body)
        other, other_country, _region_again = _contained_pair(body)
        chosen_proposal = _proposal(chosen, body, "ישראל", 1)
        other_proposal = _proposal(other, body, "ישראל", 1)
        _candidate(chosen_proposal, country)
        other_candidate = _candidate(other_proposal, other_country)

        report = self._apply(chosen)

        other_candidate.refresh_from_db()
        self.assertEqual(other_candidate.status, PENDING)
        self.assertEqual(report.proposals_examined, 1)
        self.assertEqual(report.rejected_candidates, 1)

    def test_command_requires_known_items_and_actor_for_apply(self):
        item = _manual("ארץ ישראל")
        with self.assertRaises(CommandError):
            call_command(COMMAND)
        with self.assertRaises(CommandError):
            call_command(COMMAND, "--item", "0")
        with self.assertRaises(CommandError):
            call_command(COMMAND, "--item", "999999")
        with self.assertRaises(CommandError):
            call_command(COMMAND, "--item", str(item.pk), "--all")
        with self.assertRaises(CommandError):
            call_command(COMMAND, "--item", str(item.pk), "--apply")
        with self.assertRaises(CommandError):
            call_command(
                COMMAND,
                "--item",
                str(item.pk),
                "--apply",
                "--actor-id",
                "999999",
            )

    def test_duplicate_item_argument_examines_proposals_once(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        _candidate(proposal, country)
        out = StringIO()
        call_command(
            COMMAND,
            "--item",
            str(item.pk),
            "--item",
            str(item.pk),
            stdout=out,
        )
        self.assertIn("proposals_examined: 1", out.getvalue())
        self.assertIn("eligible_proposals: 1", out.getvalue())

    def test_command_exits_nonzero_when_a_proposal_errors(self):
        body = "ארץ ישראל"
        item, country, _region = _contained_pair(body)
        proposal = _proposal(item, body, "ישראל", 1)
        candidate = _candidate(proposal, country)
        out = StringIO()
        with patch(
            "documents.services.non_person_entity_overlap_cleanup._classify",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    COMMAND,
                    "--item",
                    str(item.pk),
                    "--apply",
                    "--actor-id",
                    str(self.staff.pk),
                    stdout=out,
                )
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertIn("errors: 1", out.getvalue())

    def _apply(self, item: ArchiveItem):
        return cleanup_non_person_overlap_candidates(
            [item.pk],
            apply=True,
            actor=self.actor,
        )


def _row_counts() -> tuple[int, int, int, int]:
    return (
        NonPersonEntityOccurrenceProposal.objects.count(),
        NonPersonEntityOccurrenceCandidate.objects.count(),
        NonPersonEntityOccurrenceCandidateMatch.objects.count(),
        NonPersonEntityOccurrenceReviewEvent.objects.count(),
    )


def cleanup_classify():
    from documents.services.non_person_entity_overlap_cleanup import _classify

    return _classify
