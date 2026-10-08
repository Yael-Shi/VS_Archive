"""Explicit batch detector for non-person occurrence proposals."""

from __future__ import annotations

from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
    ReviewedNonPersonEntityDecision,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services import non_person_entity_detector as detector
from documents.services.non_person_entity_detector import (
    NonPersonEntityCorpusRun,
    _AcceptedHit,
    _accepted_surface_slices,
    _bounded_surface_slices,
    _hit_is_strictly_contained,
    _surviving_hits,
    detect_non_person_entities_for_item,
    detect_non_person_entities_for_items,
)
from documents.services.non_person_entity_occurrence_review import approve_candidate
from documents.services.non_person_entity_occurrences import (
    _locate_prepared_surface,
    _normalize_with_spans,
    _prepare_normalized_source,
    locate_surface_occurrences,
    normalize_surface_v1,
    occurrence_is_currently_valid,
    source_text_sha256,
)
from documents.services.non_person_entity_presentation import (
    public_mentioned_object_links,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
CANONICAL = NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME
DISPLAY = NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME
ALIAS = NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS
DETECT = NonPersonEntityOccurrenceReviewEvent.Action.DETECT
PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
NEEDS_RESEARCH = NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
APPROVED = NonPersonEntityOccurrenceCandidate.Status.APPROVED
REJECTED = NonPersonEntityOccurrenceCandidate.Status.REJECTED
REMOVED = NonPersonEntityOccurrenceCandidate.Status.REMOVED


def _entity(name: str, **overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": name,
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _manual(body: str, *, title: str = "פריט") -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title=title,
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _ocr(
    *,
    language: str,
    source_text: str | None,
    hebrew_text: str | None,
    title: str = "ocr",
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title=title,
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    document = Document.objects.create(
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        language=language,
        archive_item=item,
        upload_status=Document.UploadStatus.UPLOADED,
    )
    shared = {
        "document": document,
        "status": DocumentTextResult.Status.NEEDS_REVIEW,
        "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
        "engine": "gemini-test",
    }
    if source_text is not None:
        DocumentTextResult.objects.create(
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            text=source_text,
            **shared,
        )
    if hebrew_text is not None:
        DocumentTextResult.objects.create(
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            text=hebrew_text,
            **shared,
        )
    return item


def _alias(entity: NonPersonEntity, name: str, kind: str) -> NonPersonEntityAlias:
    return NonPersonEntityAlias.objects.create(entity=entity, name=name, kind=kind)


class DetectorMatchingTests(TestCase):
    def test_canonical_name_match_creates_one_proposal_and_candidate(self):
        item = _manual("ביקור קהיר אחר הצהריים")
        entity = _entity("קהיר")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.new_proposals, 1)
        self.assertEqual(report.new_candidates, 1)
        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.archive_item_id, item.pk)
        self.assertEqual(proposal.text_kind, MANUAL)
        self.assertEqual(proposal.normalization_version, "surface-v1")
        self.assertEqual(proposal.normalized_surface, "קהיר")
        self.assertEqual(proposal.occurrence_ordinal, 1)
        self.assertEqual(proposal.matched_text, "קהיר")
        candidate = proposal.candidates.get()
        self.assertEqual(candidate.candidate_entity_id, entity.pk)
        self.assertEqual(candidate.status, PENDING)
        self.assertIsNone(candidate.resolved_entity_id)
        self.assertIsNone(candidate.reviewed_by_id)
        self.assertIsNone(candidate.reviewed_at)
        match = candidate.matches.get()
        self.assertEqual(match.method, CANONICAL)
        self.assertEqual(match.matched_value, "קהיר")
        self.assertEqual(match.alias_kind, "")

    def test_display_name_match(self):
        item = _manual("העיר בגדאד בלילה")
        entity = _entity("Baghdad", display_name="בגדאד")

        detect_non_person_entities_for_item(item, apply=True)

        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        self.assertEqual(candidate.candidate_entity_id, entity.pk)
        match = candidate.matches.get()
        self.assertEqual(match.method, DISPLAY)
        self.assertEqual(match.matched_value, "בגדאד")
        self.assertEqual(candidate.proposal.normalized_surface, "בגדאד")

    def test_alias_match(self):
        item = _manual("נמל יפו הישן")
        entity = _entity("Jaffa")
        _alias(
            entity,
            "יפו",
            NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )

        detect_non_person_entities_for_item(item, apply=True)

        match = NonPersonEntityOccurrenceCandidateMatch.objects.get()
        self.assertEqual(match.method, ALIAS)
        self.assertEqual(match.matched_value, "יפו")
        self.assertEqual(match.alias_kind, NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)
        self.assertEqual(match.candidate.candidate_entity_id, entity.pk)

    def test_ocr_variant_alias_participates(self):
        item = _manual("כתוב קהיר בטעות הדפוס")
        entity = _entity("Cairo")
        _alias(entity, "קהיר", NonPersonEntityAlias.Kind.OCR_VARIANT)

        detect_non_person_entities_for_item(item, apply=True)

        match = NonPersonEntityOccurrenceCandidateMatch.objects.get()
        self.assertEqual(match.method, ALIAS)
        self.assertEqual(match.alias_kind, NonPersonEntityAlias.Kind.OCR_VARIANT)
        self.assertEqual(match.matched_value, "קהיר")

    def test_blank_display_and_alias_are_ignored(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר", display_name="   ")
        _alias(entity, "\u200f", NonPersonEntityAlias.Kind.SPELLING_VARIANT)

        detect_non_person_entities_for_item(item, apply=True)

        match = NonPersonEntityOccurrenceCandidateMatch.objects.get()
        self.assertEqual(match.method, CANONICAL)
        self.assertEqual(match.matched_value, "קהיר")

    def test_no_fuzzy_or_prefix_match(self):
        item = _manual("Palestne and Pal only")
        _entity("Palestine")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 0)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_normalized_case_match_keeps_original_slice(self):
        item = _manual("Visited PARIS yesterday")
        _entity("Paris")

        detect_non_person_entities_for_item(item, apply=True)

        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.normalized_surface, "paris")
        self.assertEqual(proposal.matched_text, "PARIS")

    def test_same_entity_canonical_and_alias_are_one_candidate(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר")
        _alias(entity, "קהיר\u200f", NonPersonEntityAlias.Kind.SPELLING_VARIANT)

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.new_candidates, 1)
        self.assertEqual(report.new_match_rows, 2)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        methods = set(
            NonPersonEntityOccurrenceCandidateMatch.objects.values_list(
                "method", "matched_value", "alias_kind"
            )
        )
        self.assertEqual(
            methods,
            {
                (CANONICAL, "קהיר", ""),
                (ALIAS, "קהיר\u200f", NonPersonEntityAlias.Kind.SPELLING_VARIANT),
            },
        )

    def test_same_surface_on_two_entities_creates_two_candidates(self):
        item = _manual("קהיר")
        first = _entity("קהיר")
        second = _entity("קהיר")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.new_proposals, 1)
        self.assertEqual(report.new_candidates, 2)
        self.assertEqual(report.ambiguous_occurrences, 1)
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceCandidate.objects.values_list(
                    "candidate_entity_id", flat=True
                )
            ),
            {first.pk, second.pk},
        )
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceCandidate.objects.values_list(
                    "status", flat=True
                )
            ),
            {PENDING},
        )


class DetectorTokenBoundaryTests(TestCase):
    def test_bonn_surface_does_not_match_inside_lavon(self):
        body = (
            "תוקפניות מצד צהל (ולבון) ודיין\n"
            "עד ל1/11/54 (3.5 חודשים) לבון\n"
            "לבון אמר חיקה לסוף המשפט"
        )
        item = _manual(body)
        _entity("בון")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(locate_surface_occurrences(body, "בון").count, 3)
        self.assertEqual(report.detected_textual_occurrences, 0)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_hebrew_surface_inside_a_larger_word_does_not_match(self):
        item = _manual("ולבון לבון בבון")
        _entity("בון")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 0)

    def test_lavon_sentence_is_not_standalone_bonn(self):
        item = _manual("נסע לבון.")
        _entity("בון")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 0)

    def test_standalone_hebrew_surface_matches_once(self):
        item = _manual("הגיע אל בון.")
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.normalized_surface, "בון")
        self.assertEqual(proposal.occurrence_ordinal, 1)
        self.assertEqual(proposal.matched_text, "בון")

    def test_punctuation_around_hebrew_surface_still_matches(self):
        item = _manual("(בון), בון.")
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        proposals = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("occurrence_ordinal")
        )
        self.assertEqual([row.occurrence_ordinal for row in proposals], [1, 2])
        self.assertEqual([row.matched_text for row in proposals], ["בון", "בון"])

    def test_bare_hebrew_surface_and_trailing_comma_match(self):
        item = _manual("בון, בון")
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 2)

    def test_multiword_surface_still_matches(self):
        item = _manual("הכתובת היא רחוב פואד הראשון.")
        _entity("רחוב פואד הראשון")

        detect_non_person_entities_for_item(item, apply=True)

        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.normalized_surface, "רחוב פואד הראשון")
        self.assertEqual(proposal.matched_text, "רחוב פואד הראשון")
        self.assertEqual(proposal.occurrence_ordinal, 1)

    def test_hebrew_canonical_surface_still_matches_standalone(self):
        item = _manual("מדינת ישראל.")
        _entity("ישראל")

        detect_non_person_entities_for_item(item, apply=True)

        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.normalized_surface, "ישראל")
        self.assertEqual(proposal.matched_text, "ישראל")

    def test_latin_alias_matches_standalone_not_inside_a_larger_token(self):
        item = _manual("Visit Israel. Not Israelite or xIsrael.")
        entity = _entity("מדינת ישראל")
        _alias(entity, "Israel", NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)

        detect_non_person_entities_for_item(item, apply=True)

        proposals = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("occurrence_ordinal")
        )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].normalized_surface, "israel")
        self.assertEqual(proposals[0].occurrence_ordinal, 1)
        self.assertEqual(proposals[0].matched_text, "Israel")

    def test_repeated_standalone_matches_keep_ordinals(self):
        item = _manual("בון; בון; בון.")
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        proposals = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("occurrence_ordinal")
        )
        self.assertEqual([row.occurrence_ordinal for row in proposals], [1, 2, 3])
        self.assertEqual([row.matched_text for row in proposals], ["בון", "בון", "בון"])

    def test_mixed_hit_keeps_the_locator_ordinal_of_the_standalone_match(self):
        body = "לבון ואז בון"
        item = _manual(body)
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        located = locate_surface_occurrences(body, "בון")
        self.assertEqual([found.ordinal for found in located.occurrences], [1, 2])
        self.assertEqual(body[located.occurrences[0].start - 1], "ל")
        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.occurrence_ordinal, 2)
        self.assertEqual(proposal.matched_text, "בון")
        self.assertEqual(proposal.matched_text, located.occurrences[1].matched_text)

    def test_mixed_hits_keep_gaps_in_locator_ordinals(self):
        body = "בון ואז לבון ואז בון"
        item = _manual(body)
        _entity("בון")

        detect_non_person_entities_for_item(item, apply=True)

        proposals = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("occurrence_ordinal")
        )
        self.assertEqual([row.occurrence_ordinal for row in proposals], [1, 3])
        self.assertEqual([row.matched_text for row in proposals], ["בון", "בון"])

    def test_approve_mixed_hit_revalidates_the_standalone_locator_ordinal(self):
        body = "לבון ואז בון"
        item = _manual(body)
        entity = _entity("בון")
        actor = User.objects.create_user(
            username="boundary-reviewer", password="test-pass"
        )
        detect_non_person_entities_for_item(item, apply=True)
        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        self.assertEqual(candidate.proposal.occurrence_ordinal, 2)

        result = approve_candidate(candidate.pk, actor=actor)

        self.assertTrue(result.applied)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.entity_id, entity.pk)
        self.assertEqual(occurrence.occurrence_ordinal, 2)
        self.assertEqual(occurrence.matched_text, "בון")
        self.assertTrue(occurrence_is_currently_valid(occurrence))
        located = locate_surface_occurrences(body, "בון")
        standalone = located.occurrences[1]
        self.assertEqual(standalone.ordinal, 2)
        self.assertEqual(occurrence.matched_text, standalone.matched_text)
        self.assertEqual(body[standalone.start - 1], " ")

    def test_number_or_combining_mark_continues_the_token(self):
        item = _manual("בון1 1בון בון\u0301")
        _entity("בון")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 0)


class DetectorOccurrenceIdentityTests(TestCase):
    def test_repeated_surface_uses_separate_ordinals(self):
        body = "קהיר ואז קהיר"
        item = _manual(body)
        _entity("קהיר")

        detect_non_person_entities_for_item(item, apply=True)

        proposals = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("occurrence_ordinal")
        )
        self.assertEqual([row.occurrence_ordinal for row in proposals], [1, 2])
        self.assertEqual(
            {row.normalized_surface for row in proposals},
            {"קהיר"},
        )
        self.assertEqual(len({row.pk for row in proposals}), 2)

    def test_exact_current_sha_is_stored(self):
        body = "ביקור קהיר"
        item = _manual(body)
        _entity("קהיר")

        detect_non_person_entities_for_item(item, apply=True)

        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.source_text_sha256, source_text_sha256(body))

    def test_changed_source_creates_a_new_identity_and_keeps_the_old(self):
        body = "ביקור קהיר"
        item = _manual(body)
        _entity("קהיר")
        detect_non_person_entities_for_item(item, apply=True)
        original = NonPersonEntityOccurrenceProposal.objects.get()
        original_sha = original.source_text_sha256
        content = item.manual_text_content
        content.body = "ביקור קהיר סוף"
        content.save(update_fields=["body"])

        detect_non_person_entities_for_item(item, apply=True)

        original.refresh_from_db()
        self.assertEqual(original.source_text_sha256, original_sha)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 2)
        current = NonPersonEntityOccurrenceProposal.objects.exclude(
            pk=original.pk
        ).get()
        self.assertEqual(
            current.source_text_sha256,
            source_text_sha256("ביקור קהיר סוף"),
        )
        self.assertNotEqual(current.source_text_sha256, original_sha)

    def test_manual_and_ocr_are_separate_identities(self):
        body = "ביקור קהיר"
        manual = _manual(body)
        ocr = _ocr(language="he", source_text="other", hebrew_text=body)
        _entity("קהיר")

        detect_non_person_entities_for_items([manual, ocr], apply=True)

        kinds = set(
            NonPersonEntityOccurrenceProposal.objects.values_list(
                "text_kind", flat=True
            )
        )
        self.assertEqual(kinds, {MANUAL, OCR})
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 2)
        shas = set(
            NonPersonEntityOccurrenceProposal.objects.values_list(
                "source_text_sha256", flat=True
            )
        )
        self.assertEqual(shas, {source_text_sha256(body)})


class DetectorDryRunTests(TestCase):
    def test_default_service_dry_run_writes_nothing_and_reports_creates(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")

        report = detect_non_person_entities_for_item(item)

        self.assertFalse(report.apply)
        self.assertEqual(report.mode, "dry-run")
        self.assertEqual(report.items_scanned, 1)
        self.assertEqual(report.text_sources_scanned, 1)
        self.assertEqual(report.detected_textual_occurrences, 1)
        self.assertEqual(report.new_proposals, 1)
        self.assertEqual(report.new_candidates, 1)
        self.assertEqual(report.new_match_rows, 1)
        self.assertEqual(report.new_detect_events, 1)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_dry_run_does_not_fill_blank_matched_text(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר")
        proposal = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256("ביקור קהיר"),
            normalization_version="surface-v1",
            normalized_surface="קהיר",
            occurrence_ordinal=1,
            matched_text="",
        )
        NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=proposal,
            candidate_entity=entity,
            status=PENDING,
        )

        detect_non_person_entities_for_item(item)

        proposal.refresh_from_db()
        self.assertEqual(proposal.matched_text, "")


class DetectorCommandTests(TestCase):
    def test_command_without_apply_writes_nothing(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")
        out = StringIO()

        call_command(
            "detect_non_person_entities",
            "--item",
            str(item.pk),
            stdout=out,
        )

        text = out.getvalue()
        self.assertIn("mode: dry-run", text)
        self.assertIn("new_proposals: 1", text)
        self.assertIn("new_candidates: 1", text)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_command_apply_writes_expected_rows(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")
        out = StringIO()

        call_command(
            "detect_non_person_entities",
            "--item",
            str(item.pk),
            "--apply",
            stdout=out,
        )

        self.assertIn("mode: apply", out.getvalue())
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.get().action,
            DETECT,
        )

    def test_repeated_item_arguments(self):
        first = _manual("ביקור קהיר")
        second = _manual("גם בגדאד")
        _entity("קהיר")
        _entity("בגדאד")
        out = StringIO()

        call_command(
            "detect_non_person_entities",
            "--item",
            str(first.pk),
            "--item",
            str(second.pk),
            "--apply",
            stdout=out,
        )

        self.assertIn("items_scanned: 2", out.getvalue())
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 2)

    def test_duplicate_item_argument_is_scanned_once(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")

        call_command(
            "detect_non_person_entities",
            "--item",
            str(item.pk),
            "--item",
            str(item.pk),
            "--apply",
            stdout=StringIO(),
        )

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)

    def test_missing_scope_fails_and_does_not_scan(self):
        _manual("ביקור קהיר")
        _entity("קהיר")

        with self.assertRaisesMessage(
            CommandError,
            "Pass at least one --item ID, or pass --all. "
            "This command does not scan the corpus unless --all is set.",
        ):
            call_command("detect_non_person_entities", stdout=StringIO())

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_invalid_item_id_fails_before_writes(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")

        with self.assertRaisesMessage(CommandError, "Unknown archive item id: 999999"):
            call_command(
                "detect_non_person_entities",
                "--item",
                str(item.pk),
                "--item",
                "999999",
                "--apply",
                stdout=StringIO(),
            )

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_non_positive_item_id_fails(self):
        with self.assertRaisesMessage(CommandError, "Invalid archive item id: 0"):
            call_command(
                "detect_non_person_entities",
                "--item",
                "0",
                stdout=StringIO(),
            )

    def test_all_and_item_are_rejected(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")

        with self.assertRaisesMessage(
            CommandError,
            "--all and --item cannot be used together.",
        ):
            call_command(
                "detect_non_person_entities",
                "--item",
                str(item.pk),
                "--all",
                stdout=StringIO(),
            )

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_no_matches_is_success(self):
        item = _manual("אין שם כאן")
        _entity("קהיר")
        out = StringIO()

        call_command(
            "detect_non_person_entities",
            "--item",
            str(item.pk),
            stdout=out,
        )

        self.assertIn("detected_textual_occurrences: 0", out.getvalue())
        self.assertIn("errors: 0", out.getvalue())


class DetectorIdempotencyTests(TestCase):
    def test_apply_replay_creates_no_duplicates_and_splits_stats(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר")

        first = detect_non_person_entities_for_item(item, apply=True)
        second = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(first.new_proposals, 1)
        self.assertEqual(first.new_candidates, 1)
        self.assertEqual(first.new_match_rows, 1)
        self.assertEqual(first.new_detect_events, 1)
        self.assertEqual(second.new_proposals, 0)
        self.assertEqual(second.existing_proposals, 1)
        self.assertEqual(second.new_candidates, 0)
        self.assertEqual(second.existing_candidates, 1)
        self.assertEqual(second.suppressed_candidates, 0)
        self.assertEqual(second.new_match_rows, 0)
        self.assertEqual(second.existing_match_rows, 1)
        self.assertEqual(second.new_detect_events, 0)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 1)
        event = NonPersonEntityOccurrenceReviewEvent.objects.get()
        self.assertEqual(event.action, DETECT)
        self.assertEqual(event.to_entity_id, entity.pk)
        self.assertIsNone(event.actor_id)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 1)

    def test_later_alias_adds_match_without_another_event_or_status_change(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר")
        detect_non_person_entities_for_item(item, apply=True)
        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        _alias(entity, "קהיר\u200f", NonPersonEntityAlias.Kind.OCR_VARIANT)

        report = detect_non_person_entities_for_item(item, apply=True)

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.existing_candidates, 1)
        self.assertEqual(report.new_candidates, 0)
        self.assertEqual(report.new_match_rows, 1)
        self.assertEqual(report.new_detect_events, 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 2)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 1)

    def test_apply_fills_blank_matched_text_and_preserves_nonblank(self):
        body = "ביקור קהיר"
        item = _manual(body)
        entity = _entity("קהיר")
        blank = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(body),
            normalization_version="surface-v1",
            normalized_surface="קהיר",
            occurrence_ordinal=1,
            matched_text="",
        )
        kept = _manual("קהיר מוקדם")
        preserved = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=kept,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256("קהיר מוקדם"),
            normalization_version="surface-v1",
            normalized_surface="קהיר",
            occurrence_ordinal=1,
            matched_text="ידני",
        )
        stored_sha = preserved.source_text_sha256

        detect_non_person_entities_for_items([item, kept], apply=True)

        blank.refresh_from_db()
        preserved.refresh_from_db()
        self.assertEqual(blank.matched_text, "קהיר")
        self.assertEqual(preserved.matched_text, "ידני")
        self.assertEqual(preserved.source_text_sha256, stored_sha)
        self.assertEqual(
            NonPersonEntityOccurrenceCandidate.objects.filter(
                candidate_entity=entity
            ).count(),
            2,
        )


class DetectorSuppressionTests(TestCase):
    def _reviewed(
        self,
        status: str,
        *,
        resolved: NonPersonEntity | None = None,
    ):
        body = "ביקור קהיר"
        item = _manual(body)
        entity = _entity("קהיר")
        reviewer = User.objects.create_user(username="reviewer", password="test-pass")
        reviewed_at = timezone.now()
        proposal = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(body),
            normalization_version="surface-v1",
            normalized_surface=normalize_surface_v1("קהיר"),
            occurrence_ordinal=1,
            matched_text="קהיר",
        )
        candidate = NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=proposal,
            candidate_entity=entity,
            status=status,
            resolved_entity=resolved,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
        )
        candidate.refresh_from_db()
        return candidate

    def test_rejected_candidate_stays_rejected(self):
        candidate = self._reviewed(REJECTED)
        updated_at = candidate.updated_at

        report = detect_non_person_entities_for_item(
            candidate.proposal.archive_item,
            apply=True,
        )

        candidate.refresh_from_db()
        self.assertEqual(report.suppressed_candidates, 1)
        self.assertEqual(report.new_candidates, 0)
        self.assertEqual(candidate.status, REJECTED)
        self.assertIsNone(candidate.resolved_entity_id)
        self.assertEqual(candidate.updated_at, updated_at)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_removed_candidate_stays_removed(self):
        other = _entity("אלכסנדריה")
        candidate = self._reviewed(REMOVED, resolved=other)

        detect_non_person_entities_for_item(
            candidate.proposal.archive_item,
            apply=True,
        )

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, REMOVED)
        self.assertEqual(candidate.resolved_entity_id, other.pk)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)

    def test_approved_candidate_keeps_resolved_entity_and_review_metadata(self):
        other = _entity("אלכסנדריה")
        candidate = self._reviewed(APPROVED, resolved=other)
        reviewer_id = candidate.reviewed_by_id
        reviewed_at = candidate.reviewed_at

        report = detect_non_person_entities_for_item(
            candidate.proposal.archive_item,
            apply=True,
        )

        candidate.refresh_from_db()
        self.assertEqual(report.existing_candidates, 1)
        self.assertEqual(report.suppressed_candidates, 0)
        self.assertEqual(candidate.status, APPROVED)
        self.assertEqual(candidate.resolved_entity_id, other.pk)
        self.assertEqual(candidate.reviewed_by_id, reviewer_id)
        self.assertEqual(candidate.reviewed_at, reviewed_at)

    def test_needs_research_candidate_stays_unchanged(self):
        candidate = self._reviewed(NEEDS_RESEARCH)
        reviewer_id = candidate.reviewed_by_id
        reviewed_at = candidate.reviewed_at

        detect_non_person_entities_for_item(
            candidate.proposal.archive_item,
            apply=True,
        )

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, NEEDS_RESEARCH)
        self.assertIsNone(candidate.resolved_entity_id)
        self.assertEqual(candidate.reviewed_by_id, reviewer_id)
        self.assertEqual(candidate.reviewed_at, reviewed_at)

    def test_detector_does_not_create_occurrences_decisions_aliases_or_entities(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")
        entity_count = NonPersonEntity.objects.count()
        alias_count = NonPersonEntityAlias.objects.count()

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)
        self.assertEqual(NonPersonEntity.objects.count(), entity_count)
        self.assertEqual(NonPersonEntityAlias.objects.count(), alias_count)
        self.assertEqual(
            NonPersonEntityOccurrenceCandidate.objects.get().status,
            PENDING,
        )


class DetectorTextSourceTests(TestCase):
    def test_uses_only_authoritative_manual_and_ocr_bodies(self):
        manual = _manual("ביקור קהיר", title="פריז")
        hebrew = _ocr(
            language="he",
            source_text="Paris in the source",
            hebrew_text="ביקור קהיר",
            title="metadata",
        )
        english = _ocr(
            language="en",
            source_text="a visit to קהיר",
            hebrew_text="תרגום בלי השם",
        )
        _entity("קהיר")
        _entity("Paris")

        detect_non_person_entities_for_items([manual, hebrew, english], apply=True)

        rows = list(
            NonPersonEntityOccurrenceProposal.objects.order_by(
                "archive_item_id", "text_kind"
            )
        )
        self.assertEqual(len(rows), 3)
        by_item = {row.archive_item_id: row for row in rows}
        self.assertEqual(by_item[manual.pk].text_kind, MANUAL)
        self.assertEqual(by_item[manual.pk].normalized_surface, "קהיר")
        self.assertEqual(by_item[hebrew.pk].text_kind, OCR)
        self.assertEqual(by_item[hebrew.pk].normalized_surface, "קהיר")
        self.assertEqual(
            by_item[hebrew.pk].source_text_sha256,
            source_text_sha256("ביקור קהיר"),
        )
        self.assertEqual(by_item[english.pk].normalized_surface, "קהיר")
        self.assertNotIn(
            "paris",
            NonPersonEntityOccurrenceProposal.objects.values_list(
                "normalized_surface", flat=True
            ),
        )

    def test_title_and_metadata_do_not_create_a_proposal(self):
        item = create_manual_text_archive_item(
            title="Paris Gazette",
            body="שלום בלבד",
            source_title="Paris Gazette",
            author_name="Paris",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _entity("Paris")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 0)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_unsupported_and_missing_sources_are_skipped(self):
        photo = ArchiveItem.objects.create(
            title="קהיר",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        empty_manual = ArchiveItem.objects.create(
            title="בלי גוף",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _entity("קהיר")

        report = detect_non_person_entities_for_items([photo, empty_manual], apply=True)

        self.assertEqual(report.items_scanned, 2)
        self.assertEqual(report.text_sources_scanned, 0)
        self.assertGreater(report.text_sources_skipped, 0)
        self.assertEqual(report.errors, ())
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)


class DetectorPublicSearchTests(TestCase):
    def test_detector_does_not_link_public_occurrence_or_change_search(self):
        item = create_manual_text_archive_item(
            title="יומן",
            body="ביקור קהיר",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _entity("קהיר", display_name="אלקאהרה")
        before_q = _search_ids("קהיר")
        before_title = _search_ids("יומן")
        before_alias = _search_ids("אלקאהרה")
        index = ArchiveItemSearchIndex.objects.get(archive_item=item)
        before_index = (
            index.title_text,
            index.metadata_text,
            index.body_text,
            index.hebrew_translation_text,
            index.updated_at,
        )

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(public_mentioned_object_links(item), ())
        self.assertEqual(item.entity_occurrences.count(), 0)
        self.assertEqual(_search_ids("קהיר"), before_q)
        self.assertEqual(_search_ids("יומן"), before_title)
        self.assertEqual(_search_ids("אלקאהרה"), before_alias)
        index.refresh_from_db()
        self.assertEqual(
            (
                index.title_text,
                index.metadata_text,
                index.body_text,
                index.hebrew_translation_text,
                index.updated_at,
            ),
            before_index,
        )

    def test_detector_candidate_remains_reviewable(self):
        item = _manual("ביקור קהיר")
        entity = _entity("קהיר")
        actor = User.objects.create_user(username="reviewer", password="test-pass")
        detect_non_person_entities_for_item(item, apply=True)
        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        self.assertEqual(public_mentioned_object_links(item), ())

        result = approve_candidate(candidate.pk, actor=actor)

        self.assertTrue(result.applied)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, APPROVED)
        self.assertEqual(candidate.resolved_entity_id, entity.pk)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.entity_id, entity.pk)
        self.assertIsNone(occurrence.decision_id)
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceReviewEvent.objects.order_by(
                    "created_at", "pk"
                ).values_list("action", flat=True)
            ),
            [DETECT, NonPersonEntityOccurrenceReviewEvent.Action.APPROVE],
        )
        self.assertEqual(len(public_mentioned_object_links(item)), 1)


def _search_ids(term: str) -> list[int]:
    return list(
        filter_archive_items_by_search_query(ArchiveItem.objects.all(), term)
        .order_by("id")
        .values_list("id", flat=True)
    )


def _manual_pk(pk: int, body: str = "ביקור קהיר") -> ArchiveItem:
    item = ArchiveItem.objects.create(
        pk=pk,
        title=f"item-{pk}",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _run_detect(*args: str) -> str:
    out = StringIO()
    call_command("detect_non_person_entities", *args, stdout=out)
    return out.getvalue()


def _assert_no_sql_writes(queries) -> None:
    for query in queries:
        sql = " ".join(query["sql"].split()).lstrip().upper()
        for verb in ("INSERT", "UPDATE", "DELETE"):
            if sql.startswith(verb):
                raise AssertionError(query["sql"])


def _boom_after_record(item_id: int):
    original = detector._record_occurrence

    def wrapped(**kwargs):
        original(**kwargs)
        if kwargs["item"].pk == item_id:
            raise RuntimeError("boom after write")

    return patch.object(detector, "_record_occurrence", wrapped)


class DetectorCorpusCommandTests(TestCase):
    def test_all_dry_run_has_no_write_statements(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")
        proposal = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256("ביקור קהיר"),
            normalization_version="surface-v1",
            normalized_surface="קהיר",
            occurrence_ordinal=1,
            matched_text="",
        )
        out = StringIO()

        with CaptureQueriesContext(connection) as captured:
            call_command("detect_non_person_entities", "--all", stdout=out)

        text = out.getvalue()
        _assert_no_sql_writes(captured.captured_queries)
        self.assertIn("mode: dry-run", text)
        self.assertIn("selection: corpus", text)
        self.assertIn("scope: unbounded", text)
        self.assertIn("new_proposals: 0", text)
        self.assertIn("existing_proposals: 1", text)
        self.assertIn("new_candidates: 1", text)
        self.assertIn("new_detect_events: 1", text)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)
        proposal.refresh_from_db()
        self.assertEqual(proposal.matched_text, "")

    def test_corpus_options_without_all_are_rejected_before_detection(self):
        _manual("ביקור קהיר")
        cases = (
            (("--text-kind", "MANUAL_TEXT"), "--text-kind"),
            (("--min-id", "4"), "--min-id"),
            (("--max-id", "4"), "--max-id"),
            (("--start-after", "4"), "--start-after"),
            (("--limit", "4"), "--limit"),
        )
        for args, flag in cases:
            with self.subTest(args=args):
                with patch(
                    "documents.management.commands.detect_non_person_entities."
                    "NonPersonEntityCorpusRun",
                    side_effect=AssertionError("detection started"),
                ):
                    with self.assertRaisesMessage(CommandError, flag):
                        call_command(
                            "detect_non_person_entities",
                            *args,
                            stdout=StringIO(),
                        )
                self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_invalid_ids_and_range_are_rejected_before_detection(self):
        _manual("ביקור קהיר")
        cases = (
            (("--all", "--min-id", "0"), "Invalid --min-id: 0"),
            (("--all", "--max-id", "-1"), "Invalid --max-id: -1"),
            (("--all", "--start-after", "0"), "Invalid --start-after: 0"),
            (("--all", "--limit", "0"), "Invalid --limit: 0"),
            (
                ("--all", "--min-id", "8", "--max-id", "3"),
                "Invalid id range: --min-id 8 is greater than --max-id 3.",
            ),
            (
                ("--all", "--start-after", "2", "--min-id", "3"),
                "--start-after and --min-id cannot be used together.",
            ),
        )
        for args, message in cases:
            with self.subTest(args=args):
                with patch(
                    "documents.management.commands.detect_non_person_entities."
                    "NonPersonEntityCorpusRun",
                    side_effect=AssertionError("detection started"),
                ):
                    with self.assertRaisesMessage(CommandError, message):
                        call_command(
                            "detect_non_person_entities",
                            *args,
                            stdout=StringIO(),
                        )
                self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

    def test_text_kind_selects_one_source_and_mixed_items_are_scanned_once(self):
        manual = _manual("ביקור קהיר")
        ocr = _ocr(
            language="he",
            source_text="other",
            hebrew_text="ביקור קהיר",
        )
        _entity("קהיר")

        manual_out = _run_detect("--all", "--text-kind", "MANUAL_TEXT", "--apply")
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    "text_kind",
                )
            ),
            [(manual.pk, MANUAL)],
        )
        self.assertIn("manual_sources_scanned: 1", manual_out)
        self.assertIn("ocr_sources_scanned: 0", manual_out)
        NonPersonEntityOccurrenceProposal.objects.all().delete()

        ocr_out = _run_detect("--all", "--text-kind", "OCR_TRANSCRIPTION", "--apply")
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    "text_kind",
                )
            ),
            [(ocr.pk, OCR)],
        )
        self.assertIn("ocr_sources_scanned: 1", ocr_out)
        self.assertIn("manual_sources_scanned: 0", ocr_out)
        NonPersonEntityOccurrenceProposal.objects.all().delete()

        mixed = _run_detect("--all", "--apply")
        rows = list(
            NonPersonEntityOccurrenceProposal.objects.order_by("archive_item_id")
        )
        self.assertEqual(
            [(row.archive_item_id, row.text_kind) for row in rows],
            [(manual.pk, MANUAL), (ocr.pk, OCR)],
        )
        self.assertIn("items_examined: 2", mixed)
        self.assertIn("manual_sources_scanned: 1", mixed)
        self.assertIn("ocr_sources_scanned: 1", mixed)
        self.assertEqual(
            NonPersonEntityOccurrenceCandidate.objects.get(
                proposal__archive_item=manual
            ).status,
            PENDING,
        )

    def test_unsupported_and_relationless_items_are_excluded(self):
        ArchiveItem.objects.create(
            title="קהיר",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItem.objects.create(
            title="קהיר",
            item_type=ArchiveItem.ItemType.VIDEO,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItem.objects.create(
            title="בלי גוף",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        ArchiveItem.objects.create(
            title="בלי מסמך",
            item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        kept = _manual("ביקור קהיר")
        _entity("קהיר")

        text = _run_detect("--all", "--apply")

        self.assertIn("excluded_photo: 1", text)
        self.assertIn("excluded_video: 1", text)
        self.assertIn("excluded_manual_without_body: 1", text)
        self.assertIn("excluded_ocr_without_document: 1", text)
        self.assertIn("items_examined: 1", text)
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            [kept.pk],
        )

    def test_missing_displayable_text_counts_toward_limit(self):
        _ocr(language="en", source_text=None, hebrew_text=None)
        first = _manual("ביקור קהיר")
        _manual("ביקור קהיר")
        _entity("קהיר")

        capped = _run_detect("--all", "--limit", "1", "--apply")

        self.assertIn("scope: bounded", capped)
        self.assertIn("items_examined: 1", capped)
        self.assertIn("items_missing_authoritative_text: 1", capped)
        self.assertIn("ocr_sources_scanned: 0", capped)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

        wider = _run_detect("--all", "--limit", "2", "--apply")
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            [first.pk],
        )
        self.assertIn("items_examined: 2", wider)
        self.assertIn("items_missing_authoritative_text: 1", wider)
        self.assertIn("manual_sources_scanned: 1", wider)

    def test_pk_order_limit_and_inclusive_exclusive_bounds(self):
        high = _manual_pk(91003)
        low = _manual_pk(91001)
        mid = _manual_pk(91002)
        _entity("קהיר")

        limited = _run_detect("--all", "--limit", "2", "--apply")
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            {low.pk, mid.pk},
        )
        self.assertNotIn(str(high.pk), limited)
        NonPersonEntityOccurrenceProposal.objects.all().delete()

        bounded = _run_detect(
            "--all", "--min-id", "91002", "--max-id", "91003", "--apply"
        )
        self.assertIn("scope: bounded", bounded)
        self.assertIn("min_id: 91002", bounded)
        self.assertIn("max_id: 91003", bounded)
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            {mid.pk, high.pk},
        )
        NonPersonEntityOccurrenceProposal.objects.all().delete()

        resumed = _run_detect("--all", "--start-after", "91002", "--apply")
        self.assertEqual(
            list(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            [high.pk],
        )
        self.assertIn("start_after: 91002", resumed)

    def test_apply_writes_proposal_side_rows_only_and_rerun_is_idempotent(self):
        item = create_manual_text_archive_item(
            title="יומן",
            body="ביקור קהיר",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _entity("קהיר", display_name="אלקאהרה")
        entity_count = NonPersonEntity.objects.count()
        alias_count = NonPersonEntityAlias.objects.count()
        index = ArchiveItemSearchIndex.objects.get(archive_item=item)
        before_index = (
            index.title_text,
            index.metadata_text,
            index.body_text,
            index.hebrew_translation_text,
            index.updated_at,
        )

        first = _run_detect("--all", "--apply")
        self.assertIn("mode: apply", first)
        self.assertIn("new_proposals: 1", first)
        self.assertIn("new_detect_events: 1", first)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 1)
        self.assertEqual(
            NonPersonEntityOccurrenceReviewEvent.objects.get().action,
            DETECT,
        )
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)
        self.assertEqual(NonPersonEntity.objects.count(), entity_count)
        self.assertEqual(NonPersonEntityAlias.objects.count(), alias_count)
        self.assertEqual(item.entity_occurrences.count(), 0)
        index.refresh_from_db()
        self.assertEqual(
            (
                index.title_text,
                index.metadata_text,
                index.body_text,
                index.hebrew_translation_text,
                index.updated_at,
            ),
            before_index,
        )

        second = _run_detect("--all", "--apply")
        self.assertIn("new_proposals: 0", second)
        self.assertIn("existing_proposals: 1", second)
        self.assertIn("new_candidates: 0", second)
        self.assertIn("existing_candidates: 1", second)
        self.assertIn("new_match_rows: 0", second)
        self.assertIn("existing_match_rows: 1", second)
        self.assertIn("new_detect_events: 0", second)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 1)

    def test_rerun_preserves_rejected_candidate(self):
        _manual("ביקור קהיר")
        _entity("קהיר")
        _run_detect("--all", "--apply")
        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        candidate.status = REJECTED
        candidate.save(update_fields=["status", "updated_at"])

        text = _run_detect("--all", "--apply")

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, REJECTED)
        self.assertIsNone(candidate.resolved_entity_id)
        self.assertIn("suppressed_candidates: 1", text)
        self.assertIn("new_candidates: 0", text)
        self.assertIn("new_detect_events: 0", text)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 1)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 1)

    def test_changed_source_sha_adds_a_proposal_and_keeps_the_old_one(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")
        _run_detect("--all", "--apply")
        original = NonPersonEntityOccurrenceProposal.objects.get()
        item.manual_text_content.body = "ביקור קהיר מאוחר"
        item.manual_text_content.save(update_fields=["body", "updated_at"])

        _run_detect("--all", "--apply")

        rows = list(NonPersonEntityOccurrenceProposal.objects.order_by("pk"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].pk, original.pk)
        self.assertEqual(rows[0].source_text_sha256, original.source_text_sha256)
        self.assertNotEqual(rows[1].source_text_sha256, original.source_text_sha256)

    def test_item_failure_isolates_rollback_and_still_fails_the_command(self):
        first = _manual("ביקור קהיר")
        failed = _manual("ביקור קהיר")
        third = _manual("ביקור קהיר")
        _entity("קהיר")

        out = StringIO()
        with _boom_after_record(failed.pk):
            with self.assertRaisesMessage(
                CommandError,
                "Corpus detection finished with item failures.",
            ):
                call_command(
                    "detect_non_person_entities",
                    "--all",
                    "--apply",
                    stdout=out,
                )

        text = out.getvalue()
        self.assertIn("errors: 1", text)
        self.assertIn(
            f"failure: item_id={failed.pk} error=RuntimeError message=boom after write",
            text,
        )
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            {first.pk, third.pk},
        )
        self.assertFalse(
            NonPersonEntityOccurrenceProposal.objects.filter(
                archive_item=failed
            ).exists()
        )

    def test_resume_cursor_stays_before_the_first_failure(self):
        first = _manual_pk(92001)
        failed = _manual_pk(92002)
        later = _manual_pk(92003)
        _entity("קהיר")
        out = StringIO()

        with patch(
            "documents.management.commands.detect_non_person_entities.CORPUS_CHUNK_SIZE",
            1,
        ):
            with _boom_after_record(failed.pk):
                with self.assertRaises(CommandError):
                    call_command(
                        "detect_non_person_entities",
                        "--all",
                        "--apply",
                        stdout=out,
                    )

        text = out.getvalue()
        progress = [line for line in text.splitlines() if line.startswith("progress:")]
        self.assertEqual(len(progress), 3)
        for line in progress:
            self.assertIn(f"last_completed_item_id={first.pk}", line)
            self.assertNotIn(f"last_completed_item_id={later.pk}", line)
            self.assertNotIn(f"last_completed_item_id={failed.pk}", line)
        self.assertIn(f"last_completed_item_id: {first.pk}", text)
        self.assertNotIn(f"last_completed_item_id: {later.pk}", text)
        failure = (
            f"failure: item_id={failed.pk} error=RuntimeError message=boom after write"
        )
        lines = text.splitlines()
        failure_at = [index for index, line in enumerate(lines) if line == failure]
        summary_at = lines.index("items_scanned: 2")
        repeated_after = lines.index("items_examined: 3")
        failed_progress = (
            f"progress: last_completed_item_id={first.pk} "
            "examined=2 manual_scanned=1 ocr_scanned=0 "
            "missing_text=0 failures=1"
        )
        self.assertEqual(len(failure_at), 2)
        self.assertLess(failure_at[0], summary_at)
        self.assertGreater(failure_at[1], repeated_after)
        self.assertLess(failure_at[0], lines.index(failed_progress))

        rerun = _run_detect("--all", "--start-after", str(first.pk), "--apply")
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            {first.pk, failed.pk, later.pk},
        )
        self.assertEqual(
            NonPersonEntityOccurrenceProposal.objects.filter(
                archive_item=later
            ).count(),
            1,
        )
        self.assertIn("existing_proposals: 1", rerun)
        self.assertIn("new_proposals: 1", rerun)

    def test_dry_run_resume_cursor_matches_apply_and_writes_nothing(self):
        first = _manual_pk(93001)
        failed = _manual_pk(93002)
        later = _manual_pk(93003)
        _entity("קהיר")
        out = StringIO()

        with _boom_after_record(failed.pk):
            with CaptureQueriesContext(connection) as captured:
                with self.assertRaises(CommandError):
                    call_command(
                        "detect_non_person_entities",
                        "--all",
                        stdout=out,
                    )

        _assert_no_sql_writes(captured.captured_queries)
        self.assertIn(f"last_completed_item_id: {first.pk}", out.getvalue())
        self.assertNotIn(f"last_completed_item_id: {later.pk}", out.getvalue())
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_first_item_failure_leaves_the_resume_cursor_empty(self):
        failed = _manual("ביקור קהיר")
        _manual("ביקור קהיר")
        _entity("קהיר")
        out = StringIO()

        with _boom_after_record(failed.pk):
            with self.assertRaises(CommandError):
                call_command(
                    "detect_non_person_entities",
                    "--all",
                    "--apply",
                    stdout=out,
                )

        self.assertIn("last_completed_item_id: -", out.getvalue())
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 1)

    def test_explicit_multi_item_apply_rolls_back_the_whole_call(self):
        first = _manual("ביקור קהיר")
        second = _manual("ביקור קהיר")
        _entity("קהיר")

        with _boom_after_record(second.pk):
            with self.assertRaisesMessage(RuntimeError, "boom after write"):
                call_command(
                    "detect_non_person_entities",
                    "--item",
                    str(first.pk),
                    "--item",
                    str(second.pk),
                    "--apply",
                    stdout=StringIO(),
                )

        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_keyboard_interrupt_and_system_exit_propagate(self):
        _manual("ביקור קהיר")
        _entity("קהיר")
        command_run = (
            "documents.management.commands.detect_non_person_entities."
            "NonPersonEntityCorpusRun.process_item"
        )

        out = StringIO()
        with patch(command_run, side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                call_command("detect_non_person_entities", "--all", stdout=out)
        self.assertIn("scope: unbounded", out.getvalue())
        self.assertIn("last_completed_item_id: -", out.getvalue())
        self.assertNotIn("failure:", out.getvalue())

        with patch(command_run, side_effect=SystemExit(3)):
            with self.assertRaises(SystemExit) as raised:
                call_command(
                    "detect_non_person_entities",
                    "--all",
                    stdout=StringIO(),
                )
        self.assertEqual(raised.exception.code, 3)

    def test_service_cursor_name_freezes_after_the_first_failure(self):
        first = _manual("ביקור קהיר")
        failed = _manual("ביקור קהיר")
        later = _manual("ביקור קהיר")
        _entity("קהיר")
        run = NonPersonEntityCorpusRun(apply=True)

        with _boom_after_record(failed.pk):
            run.process_item(first)
            run.process_item(failed)
            run.process_item(later)

        self.assertIsNone(run._on_item_failure)
        self.assertEqual(run.last_contiguous_completed_item_id, first.pk)
        self.assertEqual(run.failure_count, 1)
        self.assertEqual(run.stored_failures[0], run.build_report().errors[0])
        self.assertEqual(
            set(
                NonPersonEntityOccurrenceProposal.objects.values_list(
                    "archive_item_id",
                    flat=True,
                )
            ),
            {first.pk, later.pk},
        )

    def test_stored_failure_lines_are_capped_and_messages_are_single_line(self):
        reported: list[str] = []
        run = NonPersonEntityCorpusRun(
            apply=False,
            on_item_failure=reported.append,
        )
        noisy = RuntimeError("line one\nline two " + ("x" * 400))

        for item_id in range(1, 202):
            run._record_failure(ArchiveItem(pk=item_id), noisy)

        report = run.build_report()
        message = " ".join(str(noisy).split())[:300]
        self.assertEqual(report.failure_count, 201)
        self.assertEqual(report.reported_error_count, 201)
        self.assertEqual(len(report.errors), 200)
        self.assertTrue(report.failures_truncated)
        self.assertEqual(len(reported), 201)
        self.assertEqual(list(report.errors), reported[:200])
        self.assertEqual(len(message), 300)
        self.assertNotIn("\n", report.errors[0])
        self.assertIn(f"message={message}", report.errors[0])
        self.assertNotIn(message + "x", report.errors[0])
        self.assertTrue(reported[200].startswith("failure: item_id=201 "))

    def test_unbounded_scope_is_printed_before_registry_load(self):
        out = StringIO()
        with patch(
            "documents.management.commands.detect_non_person_entities."
            "NonPersonEntityCorpusRun",
            side_effect=RuntimeError("stop before scan"),
        ):
            with self.assertRaisesMessage(RuntimeError, "stop before scan"):
                call_command("detect_non_person_entities", "--all", stdout=out)

        text = out.getvalue()
        self.assertIn("scope: unbounded\n", text)
        self.assertNotIn("items_examined:", text)


class PreparedTokenOrdinalTests(SimpleTestCase):
    def test_rejected_middle_find_keeps_locator_ordinals_1_and_3(self):
        text = "בון ואז לבון ואז בון"
        prepared = _prepare_normalized_source(text)
        located = _locate_prepared_surface(prepared, "בון")
        assert located.occurrences is not None
        self.assertEqual([item.ordinal for item in located.occurrences], [1, 2, 3])
        self.assertEqual(
            _accepted_surface_slices(prepared, "בון"),
            ((1, "בון"), (3, "בון")),
        )
        self.assertEqual(
            _bounded_surface_slices(text, "בון"),
            ((1, "בון"), (3, "בון")),
        )


_SPAN_NORMALIZE = (
    "documents.services.non_person_entity_occurrences._normalize_with_spans"
)


class PreparedSourceCallCountTests(TestCase):
    def test_manual_item_normalizes_spans_once_for_several_surfaces(self):
        item = _manual("Paris. קהיר. Lyon.")
        _entity("Paris")
        _entity("קהיר")
        _entity("Lyon")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_item(item)

        self.assertEqual(spans.call_count, 1)
        self.assertEqual(report.detected_textual_occurrences, 3)
        self.assertEqual(report.text_sources_scanned, 1)

    def test_ocr_item_normalizes_spans_once_for_several_surfaces(self):
        item = _ocr(
            language="en",
            source_text="Paris. קהיר. Lyon.",
            hebrew_text=None,
        )
        _entity("Paris")
        _entity("קהיר")
        _entity("Lyon")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_item(item)

        self.assertEqual(spans.call_count, 1)
        self.assertEqual(report.detected_textual_occurrences, 3)
        self.assertEqual(report.text_sources_scanned, 1)

    def test_missing_authoritative_text_does_not_normalize_spans(self):
        item = ArchiveItem.objects.create(
            title="בלי גוף",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _entity("Paris")
        _entity("קהיר")
        _entity("Lyon")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_item(item)

        self.assertEqual(spans.call_count, 0)
        self.assertEqual(report.text_sources_scanned, 0)
        self.assertEqual(report.detected_textual_occurrences, 0)

    def test_two_scanned_items_normalize_spans_once_each(self):
        manual = _manual("Paris. קהיר. Lyon.")
        ocr = _ocr(
            language="en",
            source_text="Paris. קהיר. Lyon.",
            hebrew_text=None,
        )
        _entity("Paris")
        _entity("קהיר")
        _entity("Lyon")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_items([manual, ocr])

        self.assertEqual(spans.call_count, 2)
        self.assertEqual(report.text_sources_scanned, 2)
        self.assertEqual(report.detected_textual_occurrences, 6)

    def test_empty_registry_does_not_prepare_a_scanned_body(self):
        item = _manual("Paris. קהיר. Lyon.")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_item(item)

        self.assertEqual(spans.call_count, 0)
        self.assertEqual(report.text_sources_scanned, 1)
        self.assertEqual(report.detected_textual_occurrences, 0)


def _proposal_rows() -> list[tuple[str, int, str]]:
    return list(
        NonPersonEntityOccurrenceProposal.objects.order_by(
            "normalized_surface",
            "occurrence_ordinal",
        ).values_list("normalized_surface", "occurrence_ordinal", "matched_text")
    )


def _place(name: str, subtype: str) -> NonPersonEntity:
    return _entity(name, entity_subtype=subtype)


class OverlapSuppressionTests(TestCase):
    def test_contained_short_surface_is_not_proposed_on_manual_text(self):
        item = _manual("ארץ ישראל")
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        dry_run = detect_non_person_entities_for_item(item)
        self.assertFalse(dry_run.apply)
        self.assertEqual(dry_run.detected_textual_occurrences, 1)
        self.assertEqual(dry_run.new_proposals, 1)
        self.assertEqual(dry_run.new_candidates, 1)
        self.assertEqual(dry_run.ambiguous_occurrences, 0)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

        applied = detect_non_person_entities_for_item(item, apply=True)
        self.assertEqual(applied.detected_textual_occurrences, 1)
        self.assertEqual(applied.new_proposals, 1)
        self.assertEqual(applied.new_candidates, 1)
        self.assertEqual(
            _proposal_rows(),
            [("ארץ ישראל", 1, "ארץ ישראל")],
        )
        located = locate_surface_occurrences("ארץ ישראל", "ישראל")
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].ordinal, 1)

    def test_contained_short_surface_is_not_proposed_on_ocr_text(self):
        body = "ארץ ישראל"
        item = _ocr(language="he", source_text="other", hebrew_text=body)
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.text_sources_scanned, 1)
        self.assertEqual(report.detected_textual_occurrences, 1)
        self.assertEqual(_proposal_rows(), [("ארץ ישראל", 1, "ארץ ישראל")])
        self.assertEqual(
            NonPersonEntityOccurrenceProposal.objects.get().text_kind,
            OCR,
        )

    def test_standalone_short_surface_remains_beside_a_contained_hit(self):
        body = "ישראל ואז ארץ ישראל"
        item = _manual(body)
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(
            _proposal_rows(),
            [("ארץ ישראל", 1, "ארץ ישראל"), ("ישראל", 1, "ישראל")],
        )
        located = locate_surface_occurrences(body, "ישראל")
        assert located.occurrences is not None
        self.assertEqual([hit.ordinal for hit in located.occurrences], [1, 2])
        self.assertNotIn(
            ("ישראל", 2, "ישראל"),
            _proposal_rows(),
        )

    def test_suppressed_middle_hit_keeps_locator_ordinals_1_and_3(self):
        body = "ישראל ואז ארץ ישראל ואז ישראל"
        item = _manual(body)
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        detect_non_person_entities_for_item(item, apply=True)

        israel = list(
            NonPersonEntityOccurrenceProposal.objects.filter(
                normalized_surface="ישראל"
            ).order_by("occurrence_ordinal")
        )
        self.assertEqual([row.occurrence_ordinal for row in israel], [1, 3])
        self.assertEqual([row.matched_text for row in israel], ["ישראל", "ישראל"])
        self.assertEqual(
            NonPersonEntityOccurrenceProposal.objects.get(
                normalized_surface="ארץ ישראל"
            ).occurrence_ordinal,
            1,
        )
        located = locate_surface_occurrences(body, "ישראל")
        assert located.occurrences is not None
        self.assertEqual([hit.ordinal for hit in located.occurrences], [1, 2, 3])

    def test_repeated_longer_surface_suppresses_each_contained_hit(self):
        item = _manual("ארץ ישראל ואז ארץ ישראל")
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(
            _proposal_rows(),
            [("ארץ ישראל", 1, "ארץ ישראל"), ("ארץ ישראל", 2, "ארץ ישראל")],
        )

    def test_same_normalized_surface_stays_one_ambiguous_proposal(self):
        country = _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _alias(
            country,
            "Israël",
            NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT,
        )
        newspaper = _entity(
            "Israël",
            entity_type=NonPersonEntity.EntityType.PUBLICATION_WORK,
            entity_subtype=NonPersonEntity.EntitySubtype.NEWSPAPER,
        )
        item = _manual("La Tribune Juive, ISRAËL, L'Aurore")

        report = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(report.detected_textual_occurrences, 1)
        self.assertEqual(report.ambiguous_occurrences, 1)
        self.assertEqual(report.new_proposals, 1)
        self.assertEqual(report.new_candidates, 2)
        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.normalized_surface, normalize_surface_v1("Israël"))
        self.assertEqual(proposal.occurrence_ordinal, 1)
        self.assertEqual(
            set(proposal.candidates.values_list("candidate_entity_id", flat=True)),
            {country.pk, newspaper.pk},
        )

    def test_partial_overlap_keeps_both_surfaces(self):
        item = _manual("ארץ ישראל הגדולה")
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)
        _place("ישראל הגדולה", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(
            _proposal_rows(),
            [
                ("ארץ ישראל", 1, "ארץ ישראל"),
                ("ישראל הגדולה", 1, "ישראל הגדולה"),
            ],
        )

    def test_attached_hebrew_prefix_does_not_invent_the_longer_hit(self):
        body = "נסע לארץ ישראל"
        item = _manual(body)
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(_proposal_rows(), [("ישראל", 1, "ישראל")])
        self.assertEqual(_bounded_surface_slices(body, "ארץ ישראל"), ())
        self.assertEqual(
            _bounded_surface_slices(body, "ישראל"),
            ((1, "ישראל"),),
        )

    def test_unsafe_surface_cannot_suppress_a_proved_hit(self):
        acute = "\u0301"
        dot_below = "\u0323"
        composed_dot = "\u1eb9"
        body = f"ישראל e{acute}{dot_below}"
        item = _manual(body)
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _entity(composed_dot)
        unsafe = locate_surface_occurrences(body, composed_dot)
        self.assertGreater(unsafe.count, 0)
        self.assertIsNone(unsafe.occurrences)

        detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(_proposal_rows(), [("ישראל", 1, "ישראל")])

    def test_apply_rerun_does_not_duplicate_the_survivor(self):
        item = _manual("ארץ ישראל")
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)
        detect_non_person_entities_for_item(item, apply=True)

        again = detect_non_person_entities_for_item(item, apply=True)

        self.assertEqual(again.new_proposals, 0)
        self.assertEqual(again.existing_proposals, 1)
        self.assertEqual(again.new_candidates, 0)
        self.assertEqual(_proposal_rows(), [("ארץ ישראל", 1, "ארץ ישראל")])

    def test_planted_pending_contained_proposal_stays_untouched(self):
        body = "ארץ ישראל"
        item = _manual(body)
        country = _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)
        planted = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(body),
            normalization_version="surface-v1",
            normalized_surface="ישראל",
            occurrence_ordinal=1,
            matched_text="ישראל",
        )
        candidate = NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=planted,
            candidate_entity=country,
            status=PENDING,
        )

        report = detect_non_person_entities_for_item(item, apply=True)

        planted.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(planted.matched_text, "ישראל")
        self.assertEqual(planted.occurrence_ordinal, 1)
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(report.new_proposals, 1)
        self.assertEqual(report.existing_proposals, 0)
        self.assertEqual(
            _proposal_rows(),
            [("ארץ ישראל", 1, "ארץ ישראל"), ("ישראל", 1, "ישראל")],
        )
        self.assertEqual(candidate.proposal_id, planted.pk)

    def test_overlapping_body_normalizes_spans_once(self):
        item = _manual("ארץ ישראל")
        _place("ישראל", NonPersonEntity.EntitySubtype.COUNTRY)
        _place("ארץ ישראל", NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA)
        _entity("קהיר")

        with patch(_SPAN_NORMALIZE, wraps=_normalize_with_spans) as spans:
            report = detect_non_person_entities_for_item(item)

        self.assertEqual(spans.call_count, 1)
        self.assertEqual(report.detected_textual_occurrences, 1)
        self.assertEqual(report.new_proposals, 1)


class EqualOriginalSpanTests(SimpleTestCase):
    def test_equal_original_endpoints_suppress_neither_hit(self):
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
        self.assertFalse(_hit_is_strictly_contained(shorter, longer))
        self.assertFalse(_hit_is_strictly_contained(longer, shorter))
        self.assertEqual(_surviving_hits((shorter, longer)), (shorter, longer))
        self.assertNotEqual(
            (shorter.normalized_start, shorter.normalized_end),
            (longer.normalized_start, longer.normalized_end),
        )

    def test_recording_loop_records_both_equal_original_span_hits(self):
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
        item = object()
        ledger = detector._Ledger(proposals={}, candidates={}, matches=set())
        counts = detector._Counts()
        with patch(
            "documents.services.non_person_entity_detector._record_occurrence"
        ) as record:
            for hit in _surviving_hits((shorter, longer)):
                detector._record_occurrence(
                    item=item,
                    text_kind=MANUAL,
                    digest="a" * 64,
                    surface=hit.surface,
                    ordinal=hit.ordinal,
                    matched_text=hit.matched_text,
                    reasons_by_entity=hit.reasons_by_entity,
                    ledger=ledger,
                    counts=counts,
                    apply=False,
                )

        self.assertEqual(record.call_count, 2)
        self.assertEqual(
            [
                (
                    call.kwargs["surface"],
                    call.kwargs["ordinal"],
                    call.kwargs["matched_text"],
                )
                for call in record.call_args_list
            ],
            [("aa", 1, "a"), ("aaaa", 1, "a")],
        )
