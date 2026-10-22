"""Focused tests for the one approved non-person correction manifest."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    NonPersonEntityOccurrenceCandidate,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_correction import (
    APPROVAL_REFERENCE,
    CONFIRM_TOKEN,
    EXCLUDED_CANDIDATES,
    HOSPITAL_CANONICAL,
    OPERATIONS,
    CorrectionApplyError,
    apply_non_person_entity_corrections,
    dry_run_non_person_entity_corrections,
    effective_entity_for_decision,
    manifest_sha256,
)
from documents.services.non_person_entity_detector import (
    detect_non_person_entities_for_item,
)
from documents.services.non_person_entity_review_dry_run import (
    STATE_ALREADY_APPLIED,
    STATE_DRIFT,
    classify_preflight,
)
from documents.services.non_person_entity_search import registry_index_queryset
from documents.services.non_person_entity_staff import staff_entity_index_queryset
from documents.test_non_person_entity_review_dry_run import (
    _bodies,
    _candidate,
    _decision_from_plan,
    _install_bodies,
    _materialize_approve,
    _occurrence_from_plan,
    _preflight,
)

_SCHOOL = "בית הספר היסודי היהודי בניהול סלומון כהן באלכסנדריה"
_FUAD = "הפקולטה לרפואה קאסר אל־עיני, אוניברסיטת פואד הראשון"
_FACULTY = "הפקולטה לרפואה קסר אל־עיני"


def _decision(operation, *, entity, source=None, candidate_id=None, opcode=None, final=None, merge=None):
    return ReviewedNonPersonEntityDecision.objects.create(
        source=source or operation["source"],
        candidate_id=candidate_id or operation["candidate_id"],
        workbook_sha256="a" * 64,
        decision=opcode or operation["expected_decision"],
        review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
        final_canonical=operation["expected_final_canonical"] if final is None else final,
        display_name="",
        merge_target_candidate_id=(
            operation["expected_merge_target_candidate_id"] if merge is None else merge
        ),
        result_entity=entity,
        contextual_surfaces="",
        note="",
    )


def _entity_from_expected(operation) -> NonPersonEntity:
    entity = NonPersonEntity.objects.create(
        canonical_name=operation["expected_result_canonical_name"],
        display_name=operation["expected_result_display_name"],
        entity_type=operation["expected_result_entity_type"],
        entity_subtype=operation["expected_result_entity_subtype"],
    )
    for name, kind in operation.get("expected_aliases", []):
        NonPersonEntityAlias.objects.create(entity=entity, name=name, kind=kind)
    return entity


def _build_historical_registry() -> dict[str, NonPersonEntity]:
    created: dict[str, NonPersonEntity] = {}
    for operation in OPERATIONS:
        kind = operation["kind"]
        if kind == "metadata":
            entity = _entity_from_expected(operation)
            _decision(operation, entity=entity)
            created[operation["candidate_id"]] = entity
        elif operation["candidate_id"] == "EC0251":
            _decision(operation, entity=None)
        elif operation["candidate_id"] == "EC0521":
            daher = _entity_from_expected(operation)
            _decision(
                operation,
                entity=daher,
                candidate_id="EC0520",
                opcode=ReviewedNonPersonEntityDecision.Decision.APPROVE,
                final=operation["expected_final_canonical"],
                merge="",
            )
            _decision(operation, entity=daher)
            created["EC0520"] = daher
        elif kind == "retire_duplicate":
            duplicate = _entity_from_expected(operation)
            survivor = NonPersonEntity.objects.create(
                canonical_name=operation["survivor_expected_canonical_name"],
                display_name=operation["survivor_expected_display_name"],
                entity_type=operation["survivor_expected_entity_type"],
                entity_subtype=operation["survivor_expected_entity_subtype"],
            )
            _decision(operation, entity=duplicate)
            _decision(
                operation,
                entity=survivor,
                source=operation["survivor_source"],
                candidate_id=operation["survivor_candidate_id"],
                opcode=ReviewedNonPersonEntityDecision.Decision.APPROVE,
                final=operation["survivor_expected_canonical_name"],
                merge="",
            )
            for candidate_id in operation["also_pointed_at_survivor"]:
                _decision(
                    operation,
                    entity=survivor,
                    source=operation["survivor_source"],
                    candidate_id=candidate_id,
                    opcode=ReviewedNonPersonEntityDecision.Decision.MERGE,
                    final=operation["survivor_expected_canonical_name"],
                    merge="EC0225",
                )
            created["EC0455"] = duplicate
            created["EC0225"] = survivor
    created["hospital"] = NonPersonEntity.objects.create(
        canonical_name=HOSPITAL_CANONICAL,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.OTHER,
    )
    pinned = NonPersonEntity.objects.create(
        canonical_name="מקום לא קשור",
        entity_type=NonPersonEntity.EntityType.PLACE,
    )
    item = ArchiveItem.objects.create(
        title="פריט",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    ManualTextContent.objects.create(archive_item=item, body="מקום לא קשור")
    ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        source_text_sha256="b" * 64,
        normalization_version="surface-v1",
        normalized_surface="מקום לא קשור",
        occurrence_ordinal=1,
        resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        entity=pinned,
        matched_text="מקום לא קשור",
    )
    created["pinned"] = pinned
    return created


def _decision_rows():
    return list(
        ReviewedNonPersonEntityDecision.objects.order_by("pk").values_list(
            "id",
            "source",
            "candidate_id",
            "decision",
            "final_canonical",
            "result_entity_id",
            "merge_target_candidate_id",
            "workbook_sha256",
            "note",
        )
    )


def _pin_rows():
    return list(
        ArchiveItemEntityOccurrence.objects.order_by("pk").values_list(
            "id",
            "entity_id",
            "decision_id",
            "normalized_surface",
        )
    )


class ManifestTests(TestCase):
    def test_excluded_candidates_are_not_operations(self):
        identifiers = {operation["candidate_id"] for operation in OPERATIONS}
        self.assertTrue(set(EXCLUDED_CANDIDATES).isdisjoint(identifiers))
        self.assertNotIn("EC0204", identifiers)
        self.assertEqual(len(OPERATIONS), 11)

    def test_dry_run_is_ready_and_writes_nothing(self):
        _build_historical_registry()
        before_entities = NonPersonEntity.objects.count()

        assessment = dry_run_non_person_entity_corrections()

        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.entity_delta, 2)
        self.assertEqual(assessment.alias_delta, 1)
        self.assertEqual(assessment.correction_delta, 11)
        self.assertEqual(assessment.decision_delta, 0)
        self.assertEqual(assessment.occurrence_delta, 0)
        self.assertEqual(NonPersonEntity.objects.count(), before_entities)
        self.assertEqual(NonPersonEntityCorrection.objects.count(), 0)

    def test_apply_keeps_historical_targets_and_pins(self):
        created = _build_historical_registry()
        decisions_before = _decision_rows()
        pins_before = _pin_rows()
        hospital_before = (
            created["hospital"].canonical_name,
            created["hospital"].entity_subtype,
        )

        result = apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)

        self.assertTrue(result.applied)
        self.assertEqual(_decision_rows(), decisions_before)
        self.assertEqual(_pin_rows(), pins_before)
        created["hospital"].refresh_from_db()
        self.assertEqual(
            (created["hospital"].canonical_name, created["hospital"].entity_subtype),
            hospital_before,
        )
        school = ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0251")
        self.assertIsNone(school.result_entity_id)
        self.assertEqual(
            effective_entity_for_decision(school).canonical_name,
            _SCHOOL,
        )
        self.assertEqual(
            effective_entity_for_decision(school).entity_subtype,
            NonPersonEntity.EntitySubtype.SCHOOL,
        )
        faculty = ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0455")
        self.assertEqual(faculty.result_entity_id, created["EC0455"].pk)
        self.assertEqual(
            effective_entity_for_decision(faculty).pk,
            created["EC0225"].pk,
        )
        self.assertTrue(
            NonPersonEntityAlias.objects.filter(
                entity_id=created["EC0225"].pk,
                name=_FUAD,
                kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
            ).exists()
        )
        daker = ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0521")
        self.assertEqual(daker.result_entity_id, created["EC0520"].pk)
        self.assertEqual(effective_entity_for_decision(daker).canonical_name, "דאקר (קהיר)")
        self.assertEqual(effective_entity_for_decision(daker).entity_subtype, "")
        self.assertEqual(NonPersonEntityCorrection.objects.count(), 11)
        self.assertEqual(
            set(NonPersonEntityCorrection.objects.values_list("manifest_sha256", flat=True)),
            {manifest_sha256()},
        )
        self.assertEqual(
            set(
                NonPersonEntityCorrection.objects.values_list(
                    "approval_reference", flat=True
                )
            ),
            {APPROVAL_REFERENCE},
        )

    def test_replay_writes_nothing(self):
        _build_historical_registry()
        apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)
        entities = NonPersonEntity.objects.count()
        aliases = NonPersonEntityAlias.objects.count()
        corrections = NonPersonEntityCorrection.objects.count()

        again = apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)

        self.assertFalse(again.applied)
        self.assertEqual(again.assessment.status, "already_applied")
        self.assertEqual(NonPersonEntity.objects.count(), entities)
        self.assertEqual(NonPersonEntityAlias.objects.count(), aliases)
        self.assertEqual(NonPersonEntityCorrection.objects.count(), corrections)

    def test_failed_insert_rolls_back_metadata(self):
        created = _build_historical_registry()
        before = created["EC0019"].canonical_name
        with patch(
            "documents.services.non_person_entity_correction.NonPersonEntityCorrection.objects.create",
            side_effect=RuntimeError("stop"),
        ):
            with self.assertRaises(RuntimeError):
                apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)
        created["EC0019"].refresh_from_db()
        self.assertEqual(created["EC0019"].canonical_name, before)
        self.assertFalse(NonPersonEntity.objects.filter(canonical_name=_SCHOOL).exists())
        self.assertEqual(NonPersonEntityCorrection.objects.count(), 0)

    def test_partial_correction_does_not_continue(self):
        created = _build_historical_registry()
        decision = ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0019")
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=created["EC0019"],
            canonical_name="פרידה (קהיר)",
            display_name="",
            entity_subtype="",
            approval_reference=APPROVAL_REFERENCE,
            manifest_sha256=manifest_sha256(),
        )

        with self.assertRaises(CorrectionApplyError):
            apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)

        created["EC0200"].refresh_from_db()
        self.assertIn("SOE", created["EC0200"].canonical_name)
        self.assertEqual(NonPersonEntityCorrection.objects.count(), 1)

    def test_name_collision_blocks(self):
        _build_historical_registry()
        NonPersonEntity.objects.create(
            canonical_name=_SCHOOL,
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.SCHOOL,
        )

        assessment = dry_run_non_person_entity_corrections()

        self.assertEqual(assessment.status, "blocked")
        self.assertTrue(any("name collision EC0251" in reason for reason in assessment.reasons))
        with self.assertRaises(CorrectionApplyError):
            apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)

    def test_rejected_token_writes_nothing(self):
        _build_historical_registry()
        with self.assertRaises(CorrectionApplyError):
            apply_non_person_entity_corrections(confirm="no")
        self.assertEqual(NonPersonEntityCorrection.objects.count(), 0)

    def test_replay_blocks_when_a_historical_decision_field_changes(self):
        _build_historical_registry()
        apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)
        decision = ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0019")
        decision.final_canonical = "שונה"
        decision.save(update_fields=["final_canonical"])

        assessment = dry_run_non_person_entity_corrections()

        self.assertEqual(assessment.status, "blocked")
        self.assertNotEqual(assessment.status, "already_applied")
        self.assertTrue(
            any("final_canonical mismatch EC0019" in reason for reason in assessment.reasons)
        )
        decision.result_entity.refresh_from_db()
        self.assertEqual(decision.result_entity.canonical_name, "פרידה (קהיר)")


class ResolutionAndIndexTests(TestCase):
    def setUp(self):
        self.created = _build_historical_registry()
        apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)

    def test_retired_entity_is_hidden_and_direct_pages_redirect(self):
        duplicate = self.created["EC0455"]
        survivor = self.created["EC0225"]
        self.assertNotIn(
            duplicate.pk,
            set(registry_index_queryset("").values_list("pk", flat=True)),
        )
        self.assertNotIn(
            duplicate.pk,
            set(staff_entity_index_queryset(search_query=_FUAD).values_list("pk", flat=True)),
        )
        self.assertEqual(
            list(registry_index_queryset(_FUAD).values_list("pk", flat=True)),
            [survivor.pk],
        )
        public_url = reverse("archive-non-person-detail", kwargs={"entity_id": duplicate.pk})
        response = self.client.get(public_url)
        self.assertRedirects(
            response,
            reverse("archive-non-person-detail", kwargs={"entity_id": survivor.pk}),
            fetch_redirect_response=False,
        )
        staff = User.objects.create_user(
            username="correction_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        staff_response = self.client.get(
            reverse("archive-manage-entity-edit", kwargs={"entity_id": duplicate.pk})
        )
        self.assertRedirects(
            staff_response,
            reverse("archive-manage-entity-edit", kwargs={"entity_id": survivor.pk}),
            fetch_redirect_response=False,
        )

    def test_new_names_are_detected_and_the_duplicate_is_not(self):
        school = effective_entity_for_decision(
            ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0251")
        )
        item = ArchiveItem.objects.create(
            title="בית ספר",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=item, body=_SCHOOL)
        detect_non_person_entities_for_item(item, apply=True)
        candidate = NonPersonEntityOccurrenceCandidate.objects.get()
        self.assertEqual(candidate.candidate_entity_id, school.pk)

        faculty_item = ArchiveItem.objects.create(
            title="פקולטה",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=faculty_item, body=_FUAD)
        detect_non_person_entities_for_item(faculty_item, apply=True)
        faculty_ids = set(
            NonPersonEntityOccurrenceCandidate.objects.filter(
                proposal__archive_item=faculty_item
            ).values_list("candidate_entity_id", flat=True)
        )
        self.assertEqual(faculty_ids, {self.created["EC0225"].pk})

    def test_correction_row_cannot_be_updated(self):
        row = NonPersonEntityCorrection.objects.get(
            decision__candidate_id="EC0251"
        )
        row.canonical_name = "אחר"
        with self.assertRaises(ValidationError):
            row.save()
        with self.assertRaises(ValidationError):
            NonPersonEntityCorrection.objects.filter(pk=row.pk).update(canonical_name="אחר")
        with self.assertRaises(ValidationError):
            row.delete()
        row.refresh_from_db()
        self.assertEqual(row.canonical_name, _SCHOOL)


class HistoricalDriftTests(TestCase):
    def test_exact_recorded_metadata_is_not_drift(self):
        entity, decision = _materialize_approve(classify_preflight(_preflight()), "EC0100")
        entity.canonical_name = "שם מתוקן"
        entity.save(update_fields=["canonical_name", "updated_at"])
        drifted = classify_preflight(_preflight())
        self.assertEqual(_candidate(drifted, "EC0100").state, STATE_DRIFT)

        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=entity,
            canonical_name="שם מתוקן",
            display_name=entity.display_name,
            entity_subtype=entity.entity_subtype,
            approval_reference="test",
            manifest_sha256="c" * 64,
        )
        matched = classify_preflight(_preflight())
        self.assertEqual(_candidate(matched, "EC0100").state, STATE_ALREADY_APPLIED)

    def test_unrelated_type_change_stays_drift(self):
        entity, decision = _materialize_approve(classify_preflight(_preflight()), "EC0100")
        entity.canonical_name = "שם מתוקן"
        entity.entity_type = NonPersonEntity.EntityType.ORGANIZATION
        entity.save(update_fields=["canonical_name", "entity_type", "updated_at"])
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=entity,
            canonical_name="שם מתוקן",
            display_name=entity.display_name,
            entity_subtype=entity.entity_subtype,
            approval_reference="test",
            manifest_sha256="c" * 64,
        )

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0100")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("result_entity field mismatch: entity_type", candidate.reasons)

    def test_correction_for_a_different_entity_does_not_suppress_drift(self):
        entity, decision = _materialize_approve(classify_preflight(_preflight()), "EC0100")
        entity.canonical_name = "שם מתוקן"
        entity.save(update_fields=["canonical_name", "updated_at"])
        other = NonPersonEntity.objects.create(
            canonical_name="שם מתוקן",
            display_name=entity.display_name,
            entity_type=entity.entity_type,
            entity_subtype=entity.entity_subtype,
        )
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=other,
            canonical_name="שם מתוקן",
            display_name=other.display_name,
            entity_subtype=other.entity_subtype,
            approval_reference="test",
            manifest_sha256="c" * 64,
        )

        result = classify_preflight(_preflight())

        self.assertEqual(_candidate(result, "EC0100").state, STATE_DRIFT)

    def test_decision_column_change_stays_drift(self):
        entity, decision = _materialize_approve(classify_preflight(_preflight()), "EC0100")
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=entity,
            canonical_name=entity.canonical_name,
            display_name=entity.display_name,
            entity_subtype=entity.entity_subtype,
            approval_reference="test",
            manifest_sha256="c" * 64,
        )
        decision.note = "changed"
        decision.save(update_fields=["note"])

        result = classify_preflight(_preflight())
        candidate = _candidate(result, "EC0100")

        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("decision field mismatch: note", candidate.reasons)


class ContractDryRunAfterManifestTests(TestCase):
    def test_applied_manifest_leaves_merge_and_split_routes_applied(self):
        bodies = _bodies()
        _install_bodies(bodies)
        preflight = _preflight(texts=bodies)
        first = classify_preflight(preflight)
        merged_entity, _target = _materialize_approve(first, "EC0006")
        _decision_from_plan(_candidate(first, "EC2000").plan, result_entity=merged_entity)
        split_entities = {
            candidate_id: _materialize_approve(first, candidate_id)[0]
            for candidate_id in ("EC0802", "EC0803")
        }
        split = _decision_from_plan(_candidate(first, "EC0045").plan)
        for occurrence in _candidate(first, "EC0045").plan.occurrences:
            _occurrence_from_plan(
                occurrence,
                decision=split,
                entity=split_entities[occurrence.target_candidate_id],
            )
        _build_historical_registry()
        decisions_before = _decision_rows()

        apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)
        result = classify_preflight(preflight)

        self.assertEqual(_decision_rows(), decisions_before)
        self.assertEqual(_candidate(result, "EC0006").state, STATE_ALREADY_APPLIED)
        self.assertEqual(_candidate(result, "EC2000").state, STATE_ALREADY_APPLIED)
        self.assertEqual(_candidate(result, "EC0045").state, STATE_ALREADY_APPLIED)

    def test_retirement_and_extra_alias_do_not_hide_or_invent_drift(self):
        _build_historical_registry()
        apply_non_person_entity_corrections(confirm=CONFIRM_TOKEN)
        entity, decision = _materialize_approve(classify_preflight(_preflight()), "EC0100")
        survivor = NonPersonEntity.objects.get(canonical_name=_FACULTY)
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=survivor,
            retired_entity=entity,
            canonical_name=survivor.canonical_name,
            display_name=survivor.display_name,
            entity_subtype=survivor.entity_subtype,
            approval_reference="test",
            manifest_sha256="d" * 64,
        )

        applied = classify_preflight(_preflight())
        self.assertEqual(_candidate(applied, "EC0100").state, STATE_ALREADY_APPLIED)
        self.assertTrue(
            NonPersonEntityAlias.objects.filter(entity=survivor, name=_FUAD).exists()
        )

        entity.entity_type = NonPersonEntity.EntityType.ORGANIZATION
        entity.save(update_fields=["entity_type", "updated_at"])
        drifted = classify_preflight(_preflight())
        candidate = _candidate(drifted, "EC0100")
        self.assertEqual(candidate.state, STATE_DRIFT)
        self.assertIn("result_entity field mismatch: entity_type", candidate.reasons)
