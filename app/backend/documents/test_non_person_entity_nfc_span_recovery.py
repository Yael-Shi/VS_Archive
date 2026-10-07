"""Original-slice recovery across canonical NFC reordering and composition.

Match discovery stays on ``normalize_surface_v1`` and non-overlapping
``str.find``. These tests pin that contract and the recovered original slices.
"""

from __future__ import annotations

import unicodedata
from random import Random

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
)
from documents.services.non_person_entity_detector import (
    _bounded_surface_slices,
    detect_non_person_entities_for_item,
)
from documents.services.non_person_entity_occurrence_review import (
    approve_candidate,
    staff_occurrence_review_detail,
)
from documents.services.non_person_entity_occurrences import (
    _nfc_with_spans,
    locate_surface_occurrences,
    normalize_surface_v1,
    occurrence_is_currently_valid,
    source_text_sha256,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
BET = "\u05d1"
DAGESH = "\u05bc"
TSERE = "\u05b5"
YOD = "\u05d9"
TAV = "\u05ea"
# Dagesh (class 21) before tsere (class 15). NFC swaps those two marks.
ORIGINAL_BAYIT = BET + DAGESH + TSERE + YOD + TAV
NFC_BAYIT = BET + TSERE + DAGESH + YOD + TAV
DOC = f"מוסקי. מוסקי. מוסקי. הליופוליס. רחוב פואד הראשון. {ORIGINAL_BAYIT}."
ACUTE = "\u0301"
DOT_BELOW = "\u0323"
COMPOSED_E = "\u00e9"
COMPOSED_DOT = "\u1eb9"


def _find_starts(text: str, surface: str) -> list[int]:
    normalized = normalize_surface_v1(text)
    needle = normalize_surface_v1(surface)
    if needle == "":
        return []
    starts: list[int] = []
    cursor = 0
    while True:
        found = normalized.find(needle, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + len(needle)


def _assert_same_discovery(test: SimpleTestCase, text: str, surface: str) -> None:
    located = locate_surface_occurrences(text, surface)
    starts = _find_starts(text, surface)
    test.assertEqual(located.count, len(starts))
    if located.occurrences is None:
        return
    test.assertEqual(len(located.occurrences), located.count)
    test.assertEqual(
        [item.ordinal for item in located.occurrences],
        list(range(1, located.count + 1)),
    )
    needle = normalize_surface_v1(surface)
    for item in located.occurrences:
        test.assertEqual(text[item.start : item.end], item.matched_text)
        test.assertEqual(normalize_surface_v1(item.matched_text), needle)
        test.assertNotEqual(item.matched_text, "")


class AlreadyNfcCompatibilityTests(SimpleTestCase):
    def test_recoverable_matrix_keeps_offsets_and_text(self):
        stable_bet = BET + TSERE + DAGESH + YOD + TAV
        self.assertEqual(stable_bet, unicodedata.normalize("NFC", stable_bet))
        cases = [
            (
                "Palestine then Palestine",
                "Palestine",
                [(1, 0, 9, "Palestine"), (2, 15, 24, "Palestine")],
            ),
            ("  PARIS  ", "paris", [(1, 2, 7, "PARIS")]),
            ("Pal\u200festine", "Palestine", [(1, 0, 10, "Pal\u200festine")]),
            ("\u200eParis\u200f", "Paris", [(1, 1, 6, "Paris")]),
            (
                "קהיר ואז קהיר",
                "קהיר",
                [(1, 0, 4, "קהיר"), (2, 9, 13, "קהיר")],
            ),
            (
                "לבון ואז בון",
                "בון",
                [(1, 1, 4, "בון"), (2, 9, 12, "בון")],
            ),
            (
                "בון ואז לבון ואז בון",
                "בון",
                [(1, 0, 3, "בון"), (2, 9, 12, "בון"), (3, 17, 20, "בון")],
            ),
            (
                "במוסקי מוסקי",
                "מוסקי",
                [(1, 1, 6, "מוסקי"), (2, 7, 12, "מוסקי")],
            ),
            (
                "רחוב פואד הראשון.",
                "רחוב פואד הראשון",
                [(1, 0, 16, "רחוב פואד הראשון")],
            ),
            (
                "Visit Israel. Not Israelite.",
                "Israel",
                [(1, 6, 12, "Israel"), (2, 18, 24, "Israel")],
            ),
            ("a  b\tc", "a b c", [(1, 0, 6, "a  b\tc")]),
            ("MOSKI", "moski", [(1, 0, 5, "MOSKI")]),
            (
                f"ראה {stable_bet} כאן",
                stable_bet,
                [(1, 4, 9, stable_bet)],
            ),
            ("é and é", "é", [(1, 0, 1, "é"), (2, 6, 7, "é")]),
            ("hello   world", "hello world", [(1, 0, 13, "hello   world")]),
            (
                "בון; בון; בון.",
                "בון",
                [(1, 0, 3, "בון"), (2, 5, 8, "בון"), (3, 10, 13, "בון")],
            ),
            (
                "(בון), בון.",
                "בון",
                [(1, 1, 4, "בון"), (2, 7, 10, "בון")],
            ),
            (
                "Alias ALIAS alias",
                "alias",
                [(1, 0, 5, "Alias"), (2, 6, 11, "ALIAS"), (3, 12, 17, "alias")],
            ),
        ]
        for text, surface, expected in cases:
            with self.subTest(text=text, surface=surface):
                self.assertEqual(text, unicodedata.normalize("NFC", text))
                located = locate_surface_occurrences(text, surface)
                self.assertIsNotNone(located.occurrences)
                assert located.occurrences is not None
                self.assertEqual(located.count, len(expected))
                self.assertEqual(
                    [
                        (item.ordinal, item.start, item.end, item.matched_text)
                        for item in located.occurrences
                    ],
                    expected,
                )
                _assert_same_discovery(self, text, surface)

    def test_token_boundary_ordinals_and_accepted_hits_stay(self):
        cases = [
            ("לבון ואז בון", "בון", (2, "בון")),
            ("בון ואז לבון ואז בון", "בון", (1, "בון"), (3, "בון")),
            ("במוסקי מוסקי", "מוסקי", (2, "מוסקי")),
        ]
        for text, surface, *accepted in cases:
            with self.subTest(text=text):
                located = locate_surface_occurrences(text, surface)
                assert located.occurrences is not None
                self.assertEqual(
                    _bounded_surface_slices(text, surface),
                    tuple(accepted),
                )
                for ordinal, matched in accepted:
                    found = located.occurrences[ordinal - 1]
                    self.assertEqual(found.ordinal, ordinal)
                    self.assertEqual(found.matched_text, matched)


class CombiningMarkReorderTests(SimpleTestCase):
    def test_niqqud_document_recovers_the_three_surfaces(self):
        self.assertNotEqual(DOC, unicodedata.normalize("NFC", DOC))
        self.assertEqual(normalize_surface_v1(ORIGINAL_BAYIT), NFC_BAYIT)
        expected = {
            "מוסקי": ["מוסקי", "מוסקי", "מוסקי"],
            "הליופוליס": ["הליופוליס"],
            "רחוב פואד הראשון": ["רחוב פואד הראשון"],
        }
        for surface, slices in expected.items():
            with self.subTest(surface=surface):
                located = locate_surface_occurrences(DOC, surface)
                assert located.occurrences is not None
                self.assertEqual(located.count, len(slices))
                self.assertEqual(
                    [item.ordinal for item in located.occurrences],
                    list(range(1, len(slices) + 1)),
                )
                self.assertEqual(
                    [item.matched_text for item in located.occurrences],
                    slices,
                )
                self.assertEqual(
                    [DOC[item.start : item.end] for item in located.occurrences],
                    slices,
                )
                _assert_same_discovery(self, DOC, surface)

    def test_reordered_hebrew_word_recovers_the_original_points(self):
        text = f"ראה {ORIGINAL_BAYIT} כאן"
        located = locate_surface_occurrences(text, NFC_BAYIT)
        assert located.occurrences is not None
        self.assertEqual(located.count, 1)
        found = located.occurrences[0]
        self.assertEqual(found.ordinal, 1)
        self.assertEqual(found.matched_text, ORIGINAL_BAYIT)
        self.assertNotEqual(found.matched_text, NFC_BAYIT)
        self.assertEqual(text[found.start : found.end], ORIGINAL_BAYIT)
        self.assertEqual(normalize_surface_v1(found.matched_text), NFC_BAYIT)


class DecomposedLatinTests(SimpleTestCase):
    def test_decomposed_acute_recovers_the_original_pair(self):
        decomposed = "e" + ACUTE
        self.assertEqual(unicodedata.normalize("NFC", decomposed), COMPOSED_E)
        text = f"See {decomposed} now"
        located = locate_surface_occurrences(text, COMPOSED_E)
        assert located.occurrences is not None
        self.assertEqual(located.count, 1)
        found = located.occurrences[0]
        self.assertEqual(found.matched_text, decomposed)
        self.assertEqual(text[found.start : found.end], decomposed)
        self.assertEqual(normalize_surface_v1(found.matched_text), COMPOSED_E)
        self.assertNotEqual(found.matched_text, COMPOSED_E)

    def test_capital_decomposed_acute_casefolds_without_copying_nfc(self):
        decomposed = "E" + ACUTE
        text = f"See {decomposed}."
        located = locate_surface_occurrences(text, COMPOSED_E)
        assert located.occurrences is not None
        found = located.occurrences[0]
        self.assertEqual(found.matched_text, decomposed)
        self.assertEqual(normalize_surface_v1(found.matched_text), COMPOSED_E)


class HebrewCanonicalTests(SimpleTestCase):
    def test_presentation_bet_with_dagesh_recovers_one_source_character(self):
        presentation = "\ufb31"
        self.assertEqual(unicodedata.normalize("NFC", presentation), BET + DAGESH)
        self.assertNotIn("<", unicodedata.decomposition(presentation))
        text = f"ראה {presentation} כאן"
        located = locate_surface_occurrences(text, BET + DAGESH)
        assert located.occurrences is not None
        found = located.occurrences[0]
        self.assertEqual(found.matched_text, presentation)
        self.assertEqual(len(found.matched_text), 1)
        self.assertEqual(normalize_surface_v1(found.matched_text), BET + DAGESH)

    def test_presentation_vav_with_holam_recovers_one_source_character(self):
        presentation = "\ufb4b"
        holam = "\u05b9"
        self.assertEqual(unicodedata.normalize("NFC", presentation), "\u05d5" + holam)
        text = f"אות {presentation}."
        located = locate_surface_occurrences(text, "\u05d5" + holam)
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].matched_text, presentation)

    def test_half_of_a_presentation_character_is_not_sliced(self):
        presentation = "\ufb31"
        located = locate_surface_occurrences(presentation, BET)
        self.assertEqual(located.count, 1)
        self.assertIsNone(located.occurrences)


class UnsafeMappingTests(SimpleTestCase):
    def test_noncontiguous_composition_does_not_guess_a_slice(self):
        source = "e" + ACUTE + DOT_BELOW
        normalized = unicodedata.normalize("NFC", source)
        self.assertEqual(normalized, COMPOSED_DOT + ACUTE)
        located = locate_surface_occurrences(source, COMPOSED_DOT)
        self.assertGreater(located.count, 0)
        self.assertIsNone(located.occurrences)
        _assert_same_discovery(self, source, COMPOSED_DOT)

    def test_safe_surface_beside_an_unsafe_cluster_still_recovers(self):
        source = f"מוסקי e{ACUTE}{DOT_BELOW}"
        located = locate_surface_occurrences(source, "מוסקי")
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].matched_text, "מוסקי")
        unsafe = locate_surface_occurrences(source, COMPOSED_DOT)
        self.assertGreater(unsafe.count, 0)
        self.assertIsNone(unsafe.occurrences)

    def test_nfc_reconstruction_matches_unicodedata(self):
        samples = [
            ORIGINAL_BAYIT,
            "e" + ACUTE,
            "E" + ACUTE,
            "e" + ACUTE + DOT_BELOW,
            "e" + DOT_BELOW + ACUTE,
            "\u212b",
            "\u0344",
            "\ufb31",
            "\ufb4b",
            "\u1100\u1161\u11a8",
            "\uac00\u11a8",
            DOC,
            "",
            "\u0301\u0323",
        ]
        pool = list("אבגדהוזחטיכלמנסעפצקרשתeaoA \u05bc\u05b5\u0301\u0323\u200f")
        rng = Random(7)
        for _ in range(200):
            samples.append("".join(rng.choice(pool) for _ in range(rng.randint(1, 24))))
        for sample in samples:
            with self.subTest(sample=sample):
                mapped = _nfc_with_spans(sample)
                self.assertIsNotNone(mapped)
                assert mapped is not None
                self.assertEqual(mapped[0], unicodedata.normalize("NFC", sample))
                self.assertEqual(len(mapped[1]), len(mapped[0]))


class NonNfcReviewEndToEndTests(TestCase):
    def test_detector_and_review_recover_the_niqqud_document(self):
        names = {
            "מוסקי": 3,
            "הליופוליס": 1,
            "רחוב פואד הראשון": 1,
        }
        entities = {
            name: NonPersonEntity.objects.create(
                canonical_name=name,
                entity_type=NonPersonEntity.EntityType.PLACE,
                entity_subtype=NonPersonEntity.EntitySubtype.CITY,
            )
            for name in names
        }
        item = ArchiveItem.objects.create(
            title="פריט",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=item, body=DOC)

        dry_run = detect_non_person_entities_for_item(item)
        self.assertFalse(dry_run.apply)
        self.assertEqual(dry_run.detected_textual_occurrences, 5)
        self.assertEqual(dry_run.new_proposals, 5)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)

        applied = detect_non_person_entities_for_item(item, apply=True)
        self.assertTrue(applied.apply)
        self.assertEqual(applied.detected_textual_occurrences, 5)
        self.assertEqual(applied.new_proposals, 5)
        self.assertEqual(applied.new_candidates, 5)

        rows = list(
            NonPersonEntityOccurrenceProposal.objects.order_by(
                "normalized_surface",
                "occurrence_ordinal",
            )
        )
        self.assertEqual(len(rows), 5)
        by_surface: dict[str, list[NonPersonEntityOccurrenceProposal]] = {}
        for row in rows:
            by_surface.setdefault(row.normalized_surface, []).append(row)
            self.assertNotEqual(row.matched_text, "")
            self.assertEqual(
                normalize_surface_v1(row.matched_text),
                row.normalized_surface,
            )
            self.assertEqual(row.source_text_sha256, source_text_sha256(DOC))
            self.assertEqual(row.normalization_version, "surface-v1")
        self.assertEqual(
            [row.occurrence_ordinal for row in by_surface["מוסקי"]],
            [1, 2, 3],
        )
        self.assertEqual(
            [row.matched_text for row in by_surface["מוסקי"]],
            ["מוסקי", "מוסקי", "מוסקי"],
        )
        self.assertEqual(by_surface["הליופוליס"][0].occurrence_ordinal, 1)
        self.assertEqual(by_surface["הליופוליס"][0].matched_text, "הליופוליס")
        street = by_surface["רחוב פואד הראשון"][0]
        self.assertEqual(street.occurrence_ordinal, 1)
        self.assertEqual(street.matched_text, "רחוב פואד הראשון")

        candidate = NonPersonEntityOccurrenceCandidate.objects.get(
            proposal=by_surface["מוסקי"][0],
            candidate_entity=entities["מוסקי"],
        )
        detail = staff_occurrence_review_detail(candidate.pk)
        self.assertTrue(detail.is_reviewable)
        self.assertTrue(detail.has_current_context)
        self.assertEqual(detail.context_match, "מוסקי")
        self.assertEqual(detail.context_before, "")
        self.assertTrue(detail.context_after.startswith(". מוסקי"))
        self.assertEqual(detail.occurrence_ordinal, 1)

        actor = User.objects.create_user(username="nfc-reviewer", password="test-pass")
        result = approve_candidate(candidate.pk, actor=actor)
        self.assertTrue(result.applied)
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.entity_id, entities["מוסקי"].pk)
        self.assertEqual(occurrence.occurrence_ordinal, 1)
        self.assertEqual(occurrence.normalized_surface, "מוסקי")
        self.assertEqual(occurrence.matched_text, "מוסקי")
        self.assertEqual(occurrence.normalization_version, "surface-v1")
        self.assertTrue(occurrence_is_currently_valid(occurrence))

    def test_prefixed_token_stays_rejected_on_the_niqqud_source(self):
        NonPersonEntity.objects.create(
            canonical_name="מוסקי",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        body = f"במוסקי מוסקי. {ORIGINAL_BAYIT}."
        item = ArchiveItem.objects.create(
            title="קידומת",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=item, body=body)
        located = locate_surface_occurrences(body, "מוסקי")
        assert located.occurrences is not None
        self.assertEqual(
            [(item.ordinal, item.matched_text) for item in located.occurrences],
            [(1, "מוסקי"), (2, "מוסקי")],
        )
        self.assertEqual(body[located.occurrences[0].start - 1], "ב")

        detect_non_person_entities_for_item(item, apply=True)
        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.occurrence_ordinal, 2)
        self.assertEqual(proposal.matched_text, "מוסקי")

    def test_blank_stored_matched_text_is_reviewable_without_rewriting_it(self):
        entity = NonPersonEntity.objects.create(
            canonical_name="מוסקי",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        item = ArchiveItem.objects.create(
            title="היסטורי",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=item, body=DOC)
        proposal = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(DOC),
            normalization_version="surface-v1",
            normalized_surface="מוסקי",
            occurrence_ordinal=2,
            matched_text="",
        )
        candidate = NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=proposal,
            candidate_entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
        )
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME,
            matched_value="מוסקי",
        )

        detail = staff_occurrence_review_detail(candidate.pk)
        self.assertTrue(detail.is_reviewable)
        self.assertTrue(detail.has_current_context)
        self.assertEqual(detail.historical_matched_text, "")
        self.assertEqual(detail.context_match, "מוסקי")
        self.assertEqual(detail.occurrence_ordinal, 2)

        actor = User.objects.create_user(
            username="blank-reviewer",
            password="test-pass",
        )
        result = approve_candidate(candidate.pk, actor=actor)
        self.assertTrue(result.applied)
        proposal.refresh_from_db()
        self.assertEqual(proposal.matched_text, "")
        occurrence = ArchiveItemEntityOccurrence.objects.get()
        self.assertEqual(occurrence.occurrence_ordinal, 2)
        self.assertEqual(occurrence.matched_text, "מוסקי")
        self.assertTrue(occurrence_is_currently_valid(occurrence))
