"""Read-side validity for ArchiveItemEntityOccurrence. No writes."""

from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
)
from documents.services.non_person_entity_occurrences import (
    authoritative_displayed_text,
    authoritative_text_context_for_item,
    deduped_valid_entity_links_for_item,
    locate_surface_occurrences,
    occurrence_is_currently_valid,
    source_text_sha256,
    valid_occurrences_for_item,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
UNRESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED


def _entity(name: str = "פלסטינה") -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
    )


def _manual(body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="manual",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _ocr(
    *,
    language: str,
    source_text: str | None,
    hebrew_text: str | None,
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="ocr",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PRIVATE,
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


def _pin(
    item: ArchiveItem,
    *,
    text_kind: str,
    body: str,
    entity: NonPersonEntity | None,
    ordinal: int = 1,
    normalization_version: str = "surface-v1",
    resolution_status: str = RESOLVED,
    sha: str | None = None,
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version=normalization_version,
        normalized_surface="palestine",
        occurrence_ordinal=ordinal,
        resolution_status=resolution_status,
        entity=entity,
        matched_text="Palestine",
    )


class SurfaceOffsetTests(SimpleTestCase):
    def test_repeated_surface_keeps_distinct_ordinals_and_offsets(self):
        source = "Palestine then Palestine"
        located = locate_surface_occurrences(source, "Palestine")

        self.assertEqual(located.count, 2)
        assert located.occurrences is not None
        self.assertEqual(
            [(item.ordinal, item.start, item.end) for item in located.occurrences],
            [(1, 0, 9), (2, 15, 24)],
        )
        self.assertLess(located.occurrences[0].start, located.occurrences[1].start)


class OccurrenceValidityTests(TestCase):
    def test_resolved_matching_manual_pin_is_valid(self):
        body = "See Palestine."
        item = _manual(body)
        entity = _entity()
        occurrence = _pin(item, text_kind=MANUAL, body=body, entity=entity)
        pinned_sha = occurrence.source_text_sha256

        self.assertTrue(occurrence_is_currently_valid(occurrence))
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.source_text_sha256, pinned_sha)

    def test_hash_mismatch_is_not_valid(self):
        item = _manual("See Palestine.")
        occurrence = _pin(
            item,
            text_kind=MANUAL,
            body="See Palestine.",
            entity=_entity(),
            sha="c" * 64,
        )

        self.assertFalse(occurrence_is_currently_valid(occurrence))
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.source_text_sha256, "c" * 64)

    def test_missing_manual_text_is_not_valid(self):
        item = ArchiveItem.objects.create(
            title="bare",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        occurrence = _pin(
            item,
            text_kind=MANUAL,
            body="missing",
            entity=_entity(),
        )

        self.assertIsNone(authoritative_displayed_text(item, MANUAL))
        self.assertFalse(occurrence_is_currently_valid(occurrence))

    def test_missing_ocr_transcription_is_not_valid(self):
        item = _ocr(
            language=Document.Language.ENGLISH,
            source_text=None,
            hebrew_text=None,
        )
        occurrence = _pin(item, text_kind=OCR, body="missing", entity=_entity())

        self.assertIsNone(authoritative_displayed_text(item, OCR))
        self.assertFalse(occurrence_is_currently_valid(occurrence))

    def test_unresolved_occurrence_is_not_valid(self):
        body = "See Palestine."
        item = _manual(body)
        occurrence = _pin(
            item,
            text_kind=MANUAL,
            body=body,
            entity=None,
            resolution_status=UNRESOLVED,
        )

        self.assertFalse(occurrence_is_currently_valid(occurrence))

    def test_null_entity_on_resolved_shape_is_not_valid(self):
        body = "See Palestine."
        item = _manual(body)
        occurrence = ArchiveItemEntityOccurrence(
            archive_item=item,
            text_kind=MANUAL,
            source_text_sha256=source_text_sha256(body),
            normalization_version="surface-v1",
            normalized_surface="palestine",
            occurrence_ordinal=1,
            resolution_status=RESOLVED,
            entity=None,
        )

        with patch(
            "documents.services.non_person_entity_occurrences.authoritative_displayed_text",
            side_effect=AssertionError("text was resolved"),
        ):
            self.assertFalse(occurrence_is_currently_valid(occurrence))

    def test_other_normalization_version_is_not_valid(self):
        body = "See Palestine."
        item = _manual(body)
        occurrence = _pin(
            item,
            text_kind=MANUAL,
            body=body,
            entity=_entity(),
            normalization_version="surface-v2",
        )

        self.assertFalse(occurrence_is_currently_valid(occurrence))

    def test_manual_authoritative_text_is_the_body(self):
        body = "גוף ידני"
        item = _manual(body)

        self.assertEqual(authoritative_displayed_text(item, MANUAL), body)
        self.assertEqual(
            authoritative_text_context_for_item(item).texts[MANUAL],
            body,
        )

    def test_ocr_authoritative_text_follows_displayed_transcription(self):
        english = _ocr(
            language=Document.Language.ENGLISH,
            source_text="printed source",
            hebrew_text="תרגום",
        )
        hebrew = _ocr(
            language=Document.Language.HEBREW,
            source_text="מקור שלא מוצג",
            hebrew_text="תעתוק עברי",
        )

        self.assertEqual(authoritative_displayed_text(english, OCR), "printed source")
        self.assertEqual(authoritative_displayed_text(hebrew, OCR), "תעתוק עברי")

    def test_changed_displayed_ocr_text_makes_old_pin_invalid(self):
        item = _ocr(
            language=Document.Language.ENGLISH,
            source_text="printed source",
            hebrew_text="תרגום",
        )
        occurrence = _pin(
            item,
            text_kind=OCR,
            body="printed source",
            entity=_entity(),
        )
        pinned_sha = occurrence.source_text_sha256
        self.assertTrue(occurrence_is_currently_valid(occurrence))

        displayed = item.ocr_document.text_results.get(
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT
        )
        displayed.text = "edited source"
        displayed.save(update_fields=["text"])

        self.assertFalse(occurrence_is_currently_valid(occurrence))
        occurrence.refresh_from_db()
        self.assertEqual(occurrence.source_text_sha256, pinned_sha)

        hebrew_doc = _ocr(
            language=Document.Language.HEBREW,
            source_text="מקור",
            hebrew_text="תעתוק עברי",
        )
        hebrew_pin = _pin(
            hebrew_doc,
            text_kind=OCR,
            body="תעתוק עברי",
            entity=_entity("קהיר"),
        )
        hebrew_displayed = hebrew_doc.ocr_document.text_results.get(
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT
        )
        hebrew_displayed.text = "תעתוק חדש"
        hebrew_displayed.save(update_fields=["text"])

        self.assertFalse(occurrence_is_currently_valid(hebrew_pin))
        hebrew_pin.refresh_from_db()
        self.assertEqual(
            hebrew_pin.source_text_sha256,
            source_text_sha256("תעתוק עברי"),
        )

    def test_precomputed_context_is_reused_across_occurrences(self):
        body = "Palestine and Palestine"
        item = _manual(body)
        entity = _entity()
        first = _pin(item, text_kind=MANUAL, body=body, entity=entity, ordinal=1)
        second = _pin(item, text_kind=MANUAL, body=body, entity=entity, ordinal=2)
        context = authoritative_text_context_for_item(item)

        with patch(
            "documents.services.non_person_entity_occurrences.authoritative_displayed_text",
            side_effect=AssertionError("text was resolved again"),
        ):
            self.assertTrue(occurrence_is_currently_valid(first, context=context))
            self.assertTrue(occurrence_is_currently_valid(second, context=context))

        with patch(
            "documents.services.non_person_entity_occurrences.authoritative_displayed_text",
            wraps=authoritative_displayed_text,
        ) as displayed:
            rows = valid_occurrences_for_item(item)

        self.assertEqual([row.pk for row in rows], [first.pk, second.pk])
        self.assertEqual(displayed.call_count, 2)

    def test_context_for_another_item_is_not_valid(self):
        body = "See Palestine."
        item = _manual(body)
        other = _manual(body)
        occurrence = _pin(item, text_kind=MANUAL, body=body, entity=_entity())
        other_context = authoritative_text_context_for_item(other)

        self.assertFalse(
            occurrence_is_currently_valid(occurrence, context=other_context)
        )

    def test_valid_occurrences_keep_separate_rows_and_drop_unusable_ones(self):
        body = "Palestine and Palestine"
        item = _manual(body)
        entity = _entity()
        other = _entity("קהיר")
        first = _pin(item, text_kind=MANUAL, body=body, entity=entity, ordinal=1)
        second = _pin(item, text_kind=MANUAL, body=body, entity=entity, ordinal=2)
        stale = _pin(
            item,
            text_kind=MANUAL,
            body=body,
            entity=other,
            ordinal=1,
            sha="d" * 64,
        )
        unresolved = _pin(
            item,
            text_kind=MANUAL,
            body=body,
            entity=None,
            ordinal=3,
            resolution_status=UNRESOLVED,
        )

        rows = valid_occurrences_for_item(item)

        self.assertEqual([row.pk for row in rows], [first.pk, second.pk])
        self.assertNotIn(stale.pk, [row.pk for row in rows])
        self.assertNotIn(unresolved.pk, [row.pk for row in rows])

    def test_deduped_links_keep_distinct_entities_and_drop_stale_only(self):
        body = "Palestine and Cairo"
        item = _manual(body)
        palestine = _entity("פלסטינה")
        cairo = _entity("קהיר")
        stale_only = _entity("ביירות")
        _pin(item, text_kind=MANUAL, body=body, entity=palestine, ordinal=1)
        _pin(item, text_kind=MANUAL, body=body, entity=palestine, ordinal=2)
        _pin(item, text_kind=MANUAL, body=body, entity=cairo, ordinal=3)
        _pin(
            item,
            text_kind=MANUAL,
            body=body,
            entity=stale_only,
            ordinal=4,
            sha="e" * 64,
        )

        links = deduped_valid_entity_links_for_item(item)

        self.assertEqual([link.entity_id for link in links], [palestine.pk, cairo.pk])
