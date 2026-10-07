"""Detector fail-closed when an original slice cannot be proved.

Canonical NFC reordering elsewhere in the source no longer blocks recovery.
A match whose contiguous cover does not normalize back to the surface still
returns a count with ``occurrences is None``. The detector emits no hits for
that surface and does not store an empty ``matched_text``.
"""

from __future__ import annotations

import unicodedata

from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_detector import (
    _bounded_surface_slices,
    detect_non_person_entities_for_item,
)
from documents.services.non_person_entity_occurrence_review import (
    staff_occurrence_review_detail,
)
from documents.services.non_person_entity_occurrences import (
    _normalize_with_spans,
    locate_surface_occurrences,
)

# Dagesh (combining class 21) before tsere (class 15). NFC swaps them.
NIQQUD = "בֵּית"
PLAIN = "מוסקי. מוסקי. מוסקי. הליופוליס. רחוב פואד הראשון."
NON_NFC = f"{PLAIN} {NIQQUD}."
PREFIXED = "במוסקי מוסקי. בֵּית."
RECOVERABLE_PREFIX = "במוסקי מוסקי."
# Acute (class 230) sits between e and dot-below (class 220). NFC reorders
# the dot next to e and composes them, leaving the acute inside that cover.
UNSAFE = "e\u0301\u0323"
COMPOSED_DOT = "\u1eb9"


def _entity(name: str) -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.CITY,
    )


def _manual(body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="פריט",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _names() -> None:
    _entity("מוסקי")
    _entity("הליופוליס")
    _entity("רחוב פואד הראשון")


class EmptyMatchedTextReviewTests(TestCase):
    def test_plain_hebrew_recovers_slices_and_is_reviewable(self):
        _names()
        item = _manual(PLAIN)

        detect_non_person_entities_for_item(item, apply=True)

        rows = list(
            NonPersonEntityOccurrenceProposal.objects.order_by(
                "normalized_surface",
                "occurrence_ordinal",
            )
        )
        by_surface: dict[str, list[str]] = {}
        for row in rows:
            by_surface.setdefault(row.normalized_surface, []).append(row.matched_text)
        self.assertEqual(by_surface["מוסקי"], ["מוסקי", "מוסקי", "מוסקי"])
        self.assertEqual(by_surface["הליופוליס"], ["הליופוליס"])
        self.assertEqual(by_surface["רחוב פואד הראשון"], ["רחוב פואד הראשון"])
        located = locate_surface_occurrences(PLAIN, "מוסקי")
        self.assertIsNotNone(located.occurrences)
        detail = staff_occurrence_review_detail(
            NonPersonEntityOccurrenceCandidate.objects.get(
                proposal__normalized_surface="מוסקי",
                proposal__occurrence_ordinal=1,
            ).pk
        )
        self.assertTrue(detail.is_reviewable)
        self.assertTrue(detail.has_current_context)
        self.assertEqual(detail.context_match, "מוסקי")

    def test_unmappable_slice_creates_no_detector_rows(self):
        self.assertNotEqual(UNSAFE, unicodedata.normalize("NFC", UNSAFE))
        self.assertIsNotNone(_normalize_with_spans(UNSAFE))
        located = locate_surface_occurrences(UNSAFE, COMPOSED_DOT)
        self.assertGreater(located.count, 0)
        self.assertIsNone(located.occurrences)
        self.assertEqual(_bounded_surface_slices(UNSAFE, COMPOSED_DOT), ())

        _entity(COMPOSED_DOT)
        item = _manual(UNSAFE)
        dry_run = detect_non_person_entities_for_item(item)
        applied = detect_non_person_entities_for_item(item, apply=True)

        for report in (dry_run, applied):
            self.assertEqual(report.detected_textual_occurrences, 0)
            self.assertEqual(report.new_proposals, 0)
            self.assertEqual(report.new_candidates, 0)
            self.assertEqual(report.new_match_rows, 0)
            self.assertEqual(report.new_detect_events, 0)
        self.assertFalse(dry_run.apply)
        self.assertTrue(applied.apply)
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidate.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceCandidateMatch.objects.count(), 0)
        self.assertEqual(NonPersonEntityOccurrenceReviewEvent.objects.count(), 0)

    def test_recoverable_token_boundary_keeps_locator_ordinal(self):
        _entity("מוסקי")
        recoverable = _manual(RECOVERABLE_PREFIX)
        detect_non_person_entities_for_item(recoverable, apply=True)
        proposal = NonPersonEntityOccurrenceProposal.objects.get()
        self.assertEqual(proposal.occurrence_ordinal, 2)
        self.assertEqual(proposal.matched_text, "מוסקי")

        located = locate_surface_occurrences(PREFIXED, "מוסקי")
        self.assertEqual(located.count, 2)
        assert located.occurrences is not None
        self.assertEqual(
            [(item.ordinal, item.matched_text) for item in located.occurrences],
            [(1, "מוסקי"), (2, "מוסקי")],
        )
        self.assertEqual(_bounded_surface_slices(PREFIXED, "מוסקי"), ((2, "מוסקי"),))
        prefixed = _manual(PREFIXED)
        report = detect_non_person_entities_for_item(prefixed, apply=True)
        self.assertEqual(report.detected_textual_occurrences, 1)
        self.assertEqual(report.new_proposals, 1)
        rows = list(NonPersonEntityOccurrenceProposal.objects.order_by("id"))
        self.assertEqual([row.occurrence_ordinal for row in rows], [2, 2])
        self.assertEqual([row.matched_text for row in rows], ["מוסקי", "מוסקי"])
