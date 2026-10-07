"""Read-only dry-run classification for the v6 non-person review contract."""

from __future__ import annotations

import tempfile
import unicodedata
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_dry_run import (
    DECISION_SOURCE,
    STATE_ALREADY_APPLIED,
    STATE_BLOCKED,
    STATE_DRIFT,
    STATE_READY_TO_APPLY,
    classify_preflight,
    format_dry_run_report,
    locate_surface_occurrences,
    run_non_person_entity_review_dry_run,
    source_text_sha256,
)
from documents.services.non_person_entity_review_preflight import (
    PreflightError,
    normalize_surface_v1,
    preflight_authoritative_workbook,
    sha256_path,
    validate_review_contract,
)
from documents.test_non_person_entity_review_preflight import (
    _write_valid_workbook,
    valid_aliases,
    valid_routes,
    valid_rows,
)

SHA = "c" * 64
MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION


def _counts() -> dict[str, int]:
    return {
        "entity": NonPersonEntity.objects.count(),
        "alias": NonPersonEntityAlias.objects.count(),
        "decision": ReviewedNonPersonEntityDecision.objects.count(),
        "occurrence": ArchiveItemEntityOccurrence.objects.count(),
    }


def _candidate(result, candidate_id: str):
    return next(item for item in result.candidates if item.candidate_id == candidate_id)


def _bodies() -> dict[int, str]:
    return {
        108: "ministère de la propagande — ministère de la propagande",
        285: "Palestine",
        340: "  Palestine  ",
        353: "Palestine; Palestine",
    }


def _preflight(rows=None, texts=None, *, text_kinds=None, sha_overrides=None):
    stored_rows = valid_rows() if rows is None else rows
    if texts is None:
        routes = valid_routes()
    else:
        routes = []
        for route in valid_routes():
            item_id = int(route.archive_item_id)
            digest = source_text_sha256(texts[item_id])
            if sha_overrides and item_id in sha_overrides:
                digest = sha_overrides[item_id]
            kind = MANUAL
            if text_kinds and item_id in text_kinds:
                kind = text_kinds[item_id]
            routes.append(replace(route, source_text_sha256=digest, text_kind=kind))
    result = validate_review_contract(
        stored_rows,
        valid_aliases(stored_rows),
        routes,
    )
    return replace(result, workbook_sha256=SHA)


def _same_canonical(candidate_ids: set[str], canonical: str):
    return [
        replace(row, final_canonical=canonical)
        if row.candidate_id in candidate_ids or row.merge_target in candidate_ids
        else row
        for row in valid_rows()
    ]


def _point_merge_at(rows, merge_id: str, target_id: str):
    target = next(row for row in rows if row.candidate_id == target_id)
    return [
        replace(
            row,
            merge_target=target_id,
            final_canonical=target.final_canonical,
        )
        if row.candidate_id == merge_id
        else row
        for row in rows
    ]


def _manual(item_id: int, body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        id=item_id,
        title=f"item {item_id}",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    ManualTextContent.objects.create(archive_item=item, body=body)
    return item


def _ocr(item_id: int, body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        id=item_id,
        title=f"item {item_id}",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    document = Document.objects.create(
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        language=Document.Language.ENGLISH,
        archive_item=item,
        upload_status=Document.UploadStatus.UPLOADED,
    )
    shared = {
        "document": document,
        "status": DocumentTextResult.Status.NEEDS_REVIEW,
        "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
    }
    DocumentTextResult.objects.create(
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        text=body,
        **shared,
    )
    DocumentTextResult.objects.create(
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
        text="תרגום שלא אמור להיבחר",
        **shared,
    )
    return item


def _install_bodies(bodies: dict[int, str], *, ocr_ids: set[int] | None = None) -> None:
    ocr_items = ocr_ids or set()
    for item_id, body in bodies.items():
        if item_id in ocr_items:
            _ocr(item_id, body)
        else:
            _manual(item_id, body)


def _decision_from_plan(plan, **overrides):
    fields = {
        "source": DECISION_SOURCE,
        "candidate_id": plan.candidate_id,
        "workbook_sha256": plan.workbook_sha256,
        "decision": plan.decision,
        "review_status": plan.review_status,
        "final_canonical": plan.final_canonical,
        "display_name": plan.display_name,
        "merge_target_candidate_id": plan.merge_target_candidate_id,
        "contextual_surfaces": plan.contextual_surfaces,
        "note": plan.note,
        "result_entity": None,
    }
    fields.update(overrides)
    return ReviewedNonPersonEntityDecision.objects.create(**fields)


def _entity_from_plan(plan) -> NonPersonEntity:
    entity = plan.entity
    assert entity is not None
    return NonPersonEntity.objects.create(
        canonical_name=entity.canonical_name,
        display_name=entity.display_name,
        entity_type=entity.entity_type,
        entity_subtype=entity.entity_subtype,
    )


def _aliases_from_plan(entity: NonPersonEntity, plan) -> None:
    for alias in plan.aliases:
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name=alias.alias_name,
            kind=alias.alias_kind,
        )


def _materialize_approve(result, candidate_id: str):
    plan = _candidate(result, candidate_id).plan
    entity = _entity_from_plan(plan)
    _aliases_from_plan(entity, plan)
    decision = _decision_from_plan(plan, result_entity=entity)
    return entity, decision


def _occurrence_from_plan(occurrence, *, decision, entity):
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item_id=occurrence.archive_item_id,
        text_kind=occurrence.text_kind,
        source_text_sha256=occurrence.source_text_sha256,
        normalization_version=occurrence.normalization_version,
        normalized_surface=occurrence.normalized_surface,
        occurrence_ordinal=occurrence.occurrence_ordinal,
        resolution_status=occurrence.resolution_status,
        entity=entity,
        decision=decision,
        matched_text=occurrence.matched_text,
    )


class SurfaceLocationTests(SimpleTestCase):
    def test_matched_text_is_original_slice_after_surface_v1(self):
        source = " Pales\u200etine   PALESTINE "
        located = locate_surface_occurrences(source, "Palestine")

        self.assertEqual(located.count, 2)
        self.assertIsNotNone(located.occurrences)
        assert located.occurrences is not None
        self.assertEqual(
            [item.matched_text for item in located.occurrences],
            ["Pales\u200etine", "PALESTINE"],
        )
        self.assertTrue(
            all(
                normalize_surface_v1(item.matched_text) == "palestine"
                for item in located.occurrences
            )
        )

    def test_already_nfc_long_text_returns_original_matched_text(self):
        source = ("א" * 20000) + " Palestine " + ("ב" * 1000)
        self.assertEqual(source, unicodedata.normalize("NFC", source))

        located = locate_surface_occurrences(source, "Palestine")

        self.assertEqual(located.count, 1)
        self.assertIsNotNone(located.occurrences)
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].matched_text, "Palestine")

    def test_casefold_expansion_round_trips_original_slice(self):
        source = "  Straße  "
        located = locate_surface_occurrences(source, "strasse")

        self.assertEqual(source, unicodedata.normalize("NFC", source))
        self.assertEqual(located.count, 1)
        self.assertIsNotNone(located.occurrences)
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].matched_text, "Straße")
        self.assertEqual(
            normalize_surface_v1(located.occurrences[0].matched_text), "strasse"
        )

    def test_canonical_composition_elsewhere_recovers_the_original_slice(self):
        source = "e\u0301 Palestine"
        self.assertNotEqual(source, unicodedata.normalize("NFC", source))

        located = locate_surface_occurrences(source, "Palestine")

        self.assertEqual(located.count, 1)
        assert located.occurrences is not None
        self.assertEqual(located.occurrences[0].matched_text, "Palestine")
        self.assertEqual(located.occurrences[0].start, 3)
        self.assertEqual(located.occurrences[0].end, 12)
        unsafe = locate_surface_occurrences("e\u0301\u0323", "\u1eb9")
        self.assertGreater(unsafe.count, 0)
        self.assertIsNone(unsafe.occurrences)


class NonPersonEntityReviewDryRunTests(TestCase):
    def test_dry_run_does_not_write_rows(self):
        _manual(1, "untouched")
        NonPersonEntity.objects.create(
            canonical_name="קהיר",
            entity_type=NonPersonEntity.EntityType.PLACE,
        )
        before = _counts()

        result = classify_preflight(_preflight())

        self.assertEqual(_counts(), before)
        self.assertEqual(len(result.candidates), 109)
        self.assertIn("non_person_entity_review_dry_run", format_dry_run_report(result))

    def test_approve_without_decision_is_ready(self):
        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0100")

        self.assertEqual(candidate.state, STATE_READY_TO_APPLY)
        assert candidate.plan.entity is not None
        self.assertEqual(candidate.plan.entity.canonical_name, "canonical-EC0100")
        self.assertEqual(candidate.plan.entity.display_name, "")
        self.assertEqual(candidate.plan.display_name, "")
        self.assertEqual(candidate.plan.aliases, ())

    def test_approve_exact_decision_and_entity_is_already_applied(self):
        first = classify_preflight(_preflight())
        _materialize_approve(first, "EC0100")

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0100").state, STATE_ALREADY_APPLIED)

    def test_approve_entity_field_mismatch_is_drift(self):
        first = classify_preflight(_preflight())
        entity, _decision = _materialize_approve(first, "EC0100")
        entity.entity_type = NonPersonEntity.EntityType.ORGANIZATION
        entity.save(update_fields=["entity_type"])

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0100")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn(
            "result_entity field mismatch: entity_type",
            candidate.reasons,
        )

    def test_decision_workbook_sha_mismatch_is_drift(self):
        first = classify_preflight(_preflight())
        _entity, decision = _materialize_approve(first, "EC0100")
        decision.workbook_sha256 = "d" * 64
        decision.save(update_fields=["workbook_sha256"])

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0100")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn(
            "decision field mismatch: workbook_sha256",
            candidate.reasons,
        )

    def test_skip_without_decision_is_ready(self):
        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC3000")

        self.assertEqual(candidate.state, STATE_READY_TO_APPLY)
        self.assertIsNone(candidate.plan.entity)
        self.assertFalse(candidate.plan.result_entity_required)
        self.assertEqual(candidate.plan.occurrences, ())

    def test_skip_exact_decision_is_already_applied(self):
        first = classify_preflight(_preflight())
        _decision_from_plan(_candidate(first, "EC3000").plan)

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC3000").state, STATE_ALREADY_APPLIED)

    def test_skip_with_result_entity_is_drift(self):
        first = classify_preflight(_preflight())
        entity = NonPersonEntity.objects.create(
            canonical_name="not-this-skip",
            entity_type=NonPersonEntity.EntityType.PLACE,
        )
        _decision_from_plan(_candidate(first, "EC3000").plan, result_entity=entity)

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC3000")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("result_entity must be null", candidate.reasons)

    def test_needs_research_without_decision_is_ready(self):
        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0025")

        self.assertEqual(candidate.state, STATE_READY_TO_APPLY)
        self.assertEqual(
            candidate.plan.review_status,
            ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED,
        )
        self.assertIsNone(candidate.plan.entity)

    def test_needs_research_exact_unresolved_decision_is_already_applied(self):
        first = classify_preflight(_preflight())
        _decision_from_plan(_candidate(first, "EC0025").plan)

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0025").state, STATE_ALREADY_APPLIED)

    def test_needs_research_with_result_entity_is_drift(self):
        first = classify_preflight(_preflight())
        entity = NonPersonEntity.objects.create(
            canonical_name="not-research",
            entity_type=NonPersonEntity.EntityType.EVENT,
        )
        _decision_from_plan(_candidate(first, "EC0025").plan, result_entity=entity)

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0025")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("result_entity must be null", candidate.reasons)

    def test_merge_is_ready_when_target_is_ready(self):
        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0006").state, STATE_READY_TO_APPLY)
        candidate = _candidate(result, "EC2000")
        self.assertEqual(candidate.state, STATE_READY_TO_APPLY)
        self.assertEqual(candidate.plan.merge_target_candidate_id, "EC0006")
        self.assertIsNone(candidate.plan.entity)

    def test_merge_is_ready_when_target_is_already_applied(self):
        first = classify_preflight(_preflight())
        _materialize_approve(first, "EC0006")

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0006").state, STATE_ALREADY_APPLIED)
        self.assertEqual(_candidate(result, "EC2000").state, STATE_READY_TO_APPLY)

    def test_merge_is_blocked_when_target_is_blocked(self):
        long_name = "א" * 256
        rows = []
        for row in valid_rows():
            if row.candidate_id == "EC0100" or row.merge_target == "EC0100":
                rows.append(replace(row, final_canonical=long_name))
            else:
                rows.append(row)

        result = classify_preflight(_preflight(rows))
        merge = _candidate(result, "EC2004")

        self.assertEqual(_candidate(result, "EC0100").state, STATE_BLOCKED)
        self.assertEqual(merge.plan.merge_target_candidate_id, "EC0100")
        self.assertEqual(merge.state, STATE_BLOCKED)
        self.assertIn("merge target EC0100 is BLOCKED", merge.reasons)

    def test_merge_is_blocked_when_target_has_state_drift(self):
        rows = _point_merge_at(valid_rows(), "EC2000", "EC0100")
        first = classify_preflight(_preflight(rows))
        entity, _decision = _materialize_approve(first, "EC0100")
        entity.entity_subtype = NonPersonEntity.EntitySubtype.COUNTRY
        entity.save(update_fields=["entity_subtype"])

        result = classify_preflight(_preflight(rows))
        merge = _candidate(result, "EC2000")

        self.assertEqual(_candidate(result, "EC0100").state, STATE_DRIFT)
        self.assertEqual(merge.state, STATE_BLOCKED)
        self.assertIn("merge target EC0100 is STATE_DRIFT", merge.reasons)

    def test_merge_exact_result_entity_is_already_applied(self):
        first = classify_preflight(_preflight())
        entity, _target = _materialize_approve(first, "EC0006")
        _decision_from_plan(
            _candidate(first, "EC2000").plan,
            result_entity=entity,
        )

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC2000").state, STATE_ALREADY_APPLIED)

    def test_merge_wrong_result_entity_is_drift(self):
        first = classify_preflight(_preflight())
        _materialize_approve(first, "EC0006")
        other = NonPersonEntity.objects.create(
            canonical_name="canonical-EC0006",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA,
        )
        _decision_from_plan(_candidate(first, "EC2000").plan, result_entity=other)

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC2000")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("merge result_entity does not match target", candidate.reasons)

    def test_two_approve_decisions_sharing_one_entity_are_drift(self):
        rows = _same_canonical({"EC0101", "EC0106"}, "shared-canonical")
        preflight = _preflight(rows)
        first = classify_preflight(preflight)
        entity, _decision = _materialize_approve(first, "EC0101")
        _decision_from_plan(_candidate(first, "EC0106").plan, result_entity=entity)

        result = classify_preflight(preflight)

        for candidate_id, other_id in (("EC0101", "EC0106"), ("EC0106", "EC0101")):
            candidate = _candidate(result, candidate_id)
            self.assertEqual(candidate.state, STATE_DRIFT)
            self.assertIn(
                f"approve result_entity shared with {other_id}",
                candidate.reasons,
            )

    def test_approve_and_its_merge_may_share_result_entity(self):
        first = classify_preflight(_preflight())
        entity, _target = _materialize_approve(first, "EC0006")
        _decision_from_plan(_candidate(first, "EC2000").plan, result_entity=entity)

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0006").state, STATE_ALREADY_APPLIED)
        self.assertEqual(_candidate(result, "EC2000").state, STATE_ALREADY_APPLIED)
        self.assertFalse(
            any(
                "approve result_entity shared" in reason
                for reason in _candidate(result, "EC0006").reasons
            )
        )

    def test_two_approve_entities_with_the_same_canonical_name_stay_applied(self):
        rows = _same_canonical({"EC0101", "EC0106"}, "shared-canonical")
        preflight = _preflight(rows)
        first = classify_preflight(preflight)
        _materialize_approve(first, "EC0101")
        _materialize_approve(first, "EC0106")

        result = classify_preflight(preflight)

        self.assertEqual(_candidate(result, "EC0101").state, STATE_ALREADY_APPLIED)
        self.assertEqual(_candidate(result, "EC0106").state, STATE_ALREADY_APPLIED)
        self.assertEqual(
            NonPersonEntity.objects.filter(canonical_name="shared-canonical").count(),
            2,
        )

    def test_missing_planned_alias_on_applied_entity_is_drift(self):
        first = classify_preflight(_preflight())
        entity, _decision = _materialize_approve(first, "EC0006")
        entity.aliases.get(name="alias-00").delete()

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0006")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("planned alias missing: alias-00", candidate.reasons)

    def test_exact_planned_aliases_are_accepted(self):
        first = classify_preflight(_preflight())
        _materialize_approve(first, "EC0006")

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0006").state, STATE_ALREADY_APPLIED)
        self.assertEqual(len(_candidate(result, "EC0006").plan.aliases), 63)

    def test_alias_kind_conflict_is_drift(self):
        first = classify_preflight(_preflight())
        entity, _decision = _materialize_approve(first, "EC0006")
        alias = entity.aliases.get(name="alias-01")
        alias.kind = NonPersonEntityAlias.Kind.ABBREVIATION
        alias.save(update_fields=["kind"])

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0006")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("alias kind conflict: alias-01", candidate.reasons)

    def test_unrelated_alias_is_not_drift(self):
        first = classify_preflight(_preflight())
        entity, _decision = _materialize_approve(first, "EC0006")
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="not-in-this-workbook",
            kind=NonPersonEntityAlias.Kind.CURRENT_NAME,
        )
        other = NonPersonEntity.objects.create(
            canonical_name="someone-else",
            entity_type=NonPersonEntity.EntityType.PLACE,
        )
        NonPersonEntityAlias.objects.create(
            entity=other,
            name="alias-00",
            kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
        )

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0006").state, STATE_ALREADY_APPLIED)

    def test_split_source_hash_exact_is_ready(self):
        bodies = _bodies()
        _install_bodies(bodies, ocr_ids={108})
        result = classify_preflight(_preflight(texts=bodies, text_kinds={108: OCR}))

        self.assertEqual(_candidate(result, "EC0009").state, STATE_READY_TO_APPLY)
        self.assertEqual(_candidate(result, "EC0045").state, STATE_READY_TO_APPLY)
        self.assertEqual(result.planned_occurrence_creates, 6)

    def test_canonical_composition_elsewhere_does_not_block_a_recoverable_split(self):
        bodies = _bodies()
        bodies[285] = "e\u0301 Palestine"
        self.assertNotEqual(bodies[285], unicodedata.normalize("NFC", bodies[285]))
        _install_bodies(bodies)

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0009")
        item_285 = next(
            item for item in candidate.plan.occurrences if item.archive_item_id == 285
        )

        self.assertEqual(candidate.state, STATE_READY_TO_APPLY)
        self.assertFalse(
            any(
                "matched_text not safely derivable item 285" in reason
                for reason in candidate.reasons
            )
        )
        self.assertEqual(item_285.matched_text, "Palestine")
        self.assertEqual(_candidate(result, "EC0045").state, STATE_READY_TO_APPLY)

    def test_split_source_hash_drift_is_state_drift(self):
        bodies = _bodies()
        _install_bodies(bodies)
        result = classify_preflight(
            _preflight(texts=bodies, sha_overrides={108: "d" * 64})
        )
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertTrue(
            any(
                "source text sha256 mismatch item 108" in reason
                for reason in candidate.reasons
            )
        )
        self.assertEqual(_candidate(result, "EC0009").state, STATE_READY_TO_APPLY)

    def test_split_source_text_unavailable_is_blocked(self):
        bodies = _bodies()
        _install_bodies(
            {item_id: body for item_id, body in bodies.items() if item_id != 108}
        )
        ArchiveItem.objects.create(
            id=108,
            title="item 108",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_BLOCKED)
        self.assertTrue(
            any(
                "authoritative source text unavailable item 108" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_unsupported_item_type_is_blocked(self):
        bodies = _bodies()
        _install_bodies(
            {item_id: body for item_id, body in bodies.items() if item_id != 108}
        )
        ArchiveItem.objects.create(
            id=108,
            title="item 108",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_BLOCKED)
        self.assertTrue(
            any(
                "unsupported item type PHOTO item 108" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_zero_surface_occurrences_is_blocked(self):
        bodies = _bodies()
        bodies[285] = "no matching surface"
        _install_bodies(bodies)

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0009")

        self.assertEqual(candidate.state, STATE_BLOCKED)
        self.assertTrue(
            any(
                reason.startswith("zero surface occurrences item 285")
                for reason in candidate.reasons
            )
        )

    def test_split_fewer_occurrences_than_reviewed_is_blocked(self):
        bodies = _bodies()
        bodies[353] = "Palestine only"
        _install_bodies(bodies)

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0009")

        self.assertEqual(candidate.state, STATE_BLOCKED)
        self.assertTrue(
            any(
                "fewer surface occurrences item 353" in reason
                and "found 1 reviewed 2" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_extra_unreviewed_occurrence_is_blocked(self):
        bodies = _bodies()
        bodies[353] = "Palestine, Palestine, Palestine"
        _install_bodies(bodies)

        result = classify_preflight(_preflight(texts=bodies))
        candidate = _candidate(result, "EC0009")

        self.assertEqual(candidate.state, STATE_BLOCKED)
        self.assertTrue(
            any(
                "extra unreviewed surface occurrences item 353" in reason
                and "found 3 reviewed 2" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_exact_ordinal_routing_is_ready(self):
        bodies = _bodies()
        _install_bodies(bodies)
        result = classify_preflight(_preflight(texts=bodies))
        routed = {
            (
                item.archive_item_id,
                item.occurrence_ordinal,
                item.target_candidate_id,
            )
            for item in _candidate(result, "EC0009").plan.occurrences
        }

        self.assertEqual(
            routed,
            {
                (285, 1, "EC0219"),
                (340, 1, "EC0219"),
                (353, 1, "EC0006"),
                (353, 2, "EC0006"),
            },
        )
        self.assertEqual(
            {
                (
                    item.archive_item_id,
                    item.occurrence_ordinal,
                    item.target_candidate_id,
                )
                for item in _candidate(result, "EC0045").plan.occurrences
            },
            {(108, 1, "EC0802"), (108, 2, "EC0803")},
        )
        self.assertTrue(
            all(
                item.matched_text
                for item in _candidate(result, "EC0045").plan.occurrences
            )
        )

    def test_split_exact_occurrence_rows_are_already_applied(self):
        bodies = _bodies()
        _install_bodies(bodies)
        preflight = _preflight(texts=bodies)
        first = classify_preflight(preflight)
        entities = {
            candidate_id: _materialize_approve(first, candidate_id)[0]
            for candidate_id in ("EC0802", "EC0803")
        }
        decision = _decision_from_plan(_candidate(first, "EC0045").plan)
        for occurrence in _candidate(first, "EC0045").plan.occurrences:
            _occurrence_from_plan(
                occurrence,
                decision=decision,
                entity=entities[occurrence.target_candidate_id],
            )

        result = classify_preflight(preflight)

        self.assertEqual(_candidate(result, "EC0045").state, STATE_ALREADY_APPLIED)

    def test_split_occurrence_wrong_target_is_drift(self):
        bodies = _bodies()
        _install_bodies(bodies)
        preflight = _preflight(texts=bodies)
        first = classify_preflight(preflight)
        entities = {
            candidate_id: _materialize_approve(first, candidate_id)[0]
            for candidate_id in ("EC0802", "EC0803")
        }
        other = NonPersonEntity.objects.create(
            canonical_name="wrong-target",
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        )
        decision = _decision_from_plan(_candidate(first, "EC0045").plan)
        for occurrence in _candidate(first, "EC0045").plan.occurrences:
            entity = entities[occurrence.target_candidate_id]
            if occurrence.occurrence_ordinal == 2:
                entity = other
            _occurrence_from_plan(occurrence, decision=decision, entity=entity)

        result = classify_preflight(preflight)
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertTrue(
            any(
                "occurrence target mismatch item 108 ordinal 2" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_occurrence_wrong_hash_is_drift(self):
        bodies = _bodies()
        _install_bodies(bodies)
        preflight = _preflight(texts=bodies)
        first = classify_preflight(preflight)
        entities = {
            candidate_id: _materialize_approve(first, candidate_id)[0]
            for candidate_id in ("EC0802", "EC0803")
        }
        decision = _decision_from_plan(_candidate(first, "EC0045").plan)
        for occurrence in _candidate(first, "EC0045").plan.occurrences:
            stored = _occurrence_from_plan(
                occurrence,
                decision=decision,
                entity=entities[occurrence.target_candidate_id],
            )
            if occurrence.occurrence_ordinal == 1:
                stored.source_text_sha256 = "e" * 64
                stored.save(update_fields=["source_text_sha256"])

        result = classify_preflight(preflight)
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertTrue(
            any(
                "occurrence hash mismatch item 108 ordinal 1" in reason
                for reason in candidate.reasons
            )
        )

    def test_split_extra_stored_occurrence_is_drift(self):
        bodies = _bodies()
        _install_bodies(bodies)
        preflight = _preflight(texts=bodies)
        first = classify_preflight(preflight)
        entities = {
            candidate_id: _materialize_approve(first, candidate_id)[0]
            for candidate_id in ("EC0802", "EC0803")
        }
        decision = _decision_from_plan(_candidate(first, "EC0045").plan)
        planned = _candidate(first, "EC0045").plan.occurrences
        for occurrence in planned:
            _occurrence_from_plan(
                occurrence,
                decision=decision,
                entity=entities[occurrence.target_candidate_id],
            )
        extra = planned[0]
        _occurrence_from_plan(
            replace(extra, occurrence_ordinal=3),
            decision=decision,
            entity=entities[extra.target_candidate_id],
        )

        result = classify_preflight(preflight)
        candidate = _candidate(result, "EC0045")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("extra stored occurrence", candidate.reasons)

    def test_split_surfaces_do_not_become_global_aliases(self):
        result = classify_preflight(_preflight())
        alias_names = {
            alias.alias_name
            for candidate in result.candidates
            for alias in candidate.plan.aliases
        }

        self.assertNotIn("Palestine", alias_names)
        self.assertNotIn("ministère de la propagande", alias_names)
        self.assertEqual(_candidate(result, "EC0009").plan.aliases, ())
        self.assertEqual(_candidate(result, "EC0045").plan.aliases, ())
        self.assertEqual(len(alias_names), 63)

    def test_candidate_identity_does_not_resolve_by_canonical_name(self):
        entity = NonPersonEntity.objects.create(
            canonical_name="canonical-EC0100",
            display_name="",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        ReviewedNonPersonEntityDecision.objects.create(
            source="other-source",
            candidate_id="EC0100",
            workbook_sha256=SHA,
            decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
            review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
            final_canonical="canonical-EC0100",
            result_entity=entity,
        )

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0100").state, STATE_READY_TO_APPLY)

    def test_same_canonical_name_is_not_reused(self):
        NonPersonEntity.objects.create(
            canonical_name="canonical-EC0100",
            display_name="",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        before = _counts()

        result = classify_preflight(_preflight())

        self.assertEqual(_counts(), before)
        self.assertEqual(_candidate(result, "EC0100").state, STATE_READY_TO_APPLY)
        self.assertEqual(result.planned_entity_creates, 70)

    def test_real_workbook_parser_is_reused_and_fail_closed(self):
        before = _counts()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workbook.xlsx"
            _write_valid_workbook(path, valid_rows())
            digest = sha256_path(path)
            with self.assertRaises(PreflightError):
                run_non_person_entity_review_dry_run(path)
            self.assertEqual(_counts(), before)

            with (
                patch.object(preflight_module, "AUTHORITATIVE_WORKBOOK_SHA256", digest),
                patch(
                    "documents.services.non_person_entity_review_dry_run."
                    "preflight_authoritative_workbook",
                    wraps=preflight_authoritative_workbook,
                ) as parsed,
            ):
                result = run_non_person_entity_review_dry_run(path)
            parsed.assert_called_once()

            output = StringIO()
            with patch.object(
                preflight_module, "AUTHORITATIVE_WORKBOOK_SHA256", digest
            ):
                call_command(
                    "non_person_entity_review_dry_run",
                    workbook=str(path),
                    stdout=output,
                )
        self.assertEqual(_counts(), before)
        self.assertEqual(result.workbook_sha256, digest)
        self.assertEqual(len(result.candidates), 109)
        report = output.getvalue()
        self.assertIn("parser_valid: yes", report)
        self.assertIn(f"workbook_sha256: {digest}", report)
        self.assertIn("candidates: 109", report)
        self.assertNotIn("--apply", report)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            path.write_bytes(b"not a workbook")
            with self.assertRaises(CommandError):
                call_command("non_person_entity_review_dry_run", workbook=str(path))
        self.assertEqual(_counts(), before)
