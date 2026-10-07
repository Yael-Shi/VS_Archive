"""Explicit batch detector for non-person occurrence proposals."""

from __future__ import annotations

from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
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
from documents.services.non_person_entity_detector import (
    detect_non_person_entities_for_item,
    detect_non_person_entities_for_items,
)
from documents.services.non_person_entity_occurrence_review import approve_candidate
from documents.services.non_person_entity_occurrences import (
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
            "Pass at least one --item ID. This command does not scan the corpus.",
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

    def test_no_corpus_option(self):
        item = _manual("ביקור קהיר")
        _entity("קהיר")

        with self.assertRaises(CommandError):
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
