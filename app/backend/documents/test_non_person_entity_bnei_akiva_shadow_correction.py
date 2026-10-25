"""Focused tests for the approved entity 103 → 655 merge."""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceProposal,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_bnei_akiva_shadow_correction import (
    ALIAS_KIND,
    APPROVAL_REFERENCE,
    APPROVE_DECISION_ID,
    CONFIRM_TOKEN,
    MERGE_DECISION_ID,
    OCCURRENCE_IDENTITIES,
    OCCURRENCE_IDS,
    RETIRED_ENTITY_ID,
    SHORT_NAME,
    SURVIVOR_DECISION_ID,
    SURVIVOR_ENTITY_ID,
    SURVIVOR_NAME,
    ShadowMergeApplyError,
    _DECISION_SPECS,
    apply_bnei_akiva_shadow_merge,
    dry_run_bnei_akiva_shadow_merge,
    manifest_sha256,
)
from documents.services.non_person_entity_correction import (
    CONFIRM_TOKEN as ELEVEN_OPERATION_CONFIRM_TOKEN,
)
from documents.services.non_person_entity_correction import (
    OPERATIONS as ELEVEN_OPERATION_MANIFEST,
)
from documents.services.non_person_entity_correction import (
    effective_entity_for_decision,
)
from documents.services.non_person_entity_search import registry_index_queryset
from documents.services.non_person_entity_staff import staff_entity_index_queryset


def _decision(spec, *, entity, **overrides):
    fields = {
        "id": spec.decision_id,
        "source": spec.source,
        "candidate_id": spec.candidate_id,
        "workbook_sha256": spec.workbook_sha256,
        "decision": spec.decision,
        "review_status": ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
        "final_canonical": spec.final_canonical,
        "display_name": "",
        "merge_target_candidate_id": spec.merge_target_candidate_id,
        "result_entity": entity,
        "contextual_surfaces": "",
        "note": "",
    }
    fields.update(overrides)
    return ReviewedNonPersonEntityDecision.objects.create(**fields)


def _approved_snapshot():
    retired = NonPersonEntity.objects.create(
        id=RETIRED_ENTITY_ID,
        canonical_name=SHORT_NAME,
        display_name="",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        entity_subtype=NonPersonEntity.EntitySubtype.YOUTH_MOVEMENT,
    )
    survivor = NonPersonEntity.objects.create(
        id=SURVIVOR_ENTITY_ID,
        canonical_name=SURVIVOR_NAME,
        display_name="",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        entity_subtype=NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
    )
    neighbor = NonPersonEntity.objects.create(
        id=654,
        canonical_name="הנהלת בני עקיבא במצרים",
        display_name="",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        entity_subtype=NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
    )
    israel = NonPersonEntity.objects.create(
        canonical_name="ישראל",
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.COUNTRY,
    )
    specs = {spec.decision_id: spec for spec in _DECISION_SPECS}
    approve = _decision(specs[APPROVE_DECISION_ID], entity=retired)
    merge = _decision(specs[MERGE_DECISION_ID], entity=retired)
    survivor_decision = _decision(specs[SURVIVOR_DECISION_ID], entity=survivor)
    unrelated_decision = ReviewedNonPersonEntityDecision.objects.create(
        source="unrelated-source",
        candidate_id="EC0999",
        workbook_sha256="c" * 64,
        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
        review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
        final_canonical="ישראל",
        result_entity=israel,
    )
    NonPersonEntityCorrection.objects.create(
        decision=unrelated_decision,
        effective_entity=israel,
        canonical_name="ישראל",
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.COUNTRY,
        approval_reference="existing eleven-operation stand-in",
        manifest_sha256="d" * 64,
    )
    item = ArchiveItem.objects.create(
        id=112,
        title="פריט",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    for identity in OCCURRENCE_IDENTITIES:
        ArchiveItemEntityOccurrence.objects.create(
            id=identity.occurrence_id,
            archive_item_id=identity.archive_item_id,
            text_kind=identity.text_kind,
            source_text_sha256=identity.source_text_sha256,
            normalization_version=identity.normalization_version,
            normalized_surface=identity.normalized_surface,
            occurrence_ordinal=identity.occurrence_ordinal,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=survivor,
            matched_text=identity.normalized_surface,
        )
    ArchiveItemEntityOccurrence.objects.create(
        id=97,
        archive_item=item,
        text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        source_text_sha256="ab" * 32,
        normalization_version="surface-v1",
        normalized_surface="hanhala",
        occurrence_ordinal=2,
        resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        entity=neighbor,
        matched_text="Hanhala",
    )
    proposal = NonPersonEntityOccurrenceProposal.objects.create(
        archive_item=item,
        text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        source_text_sha256="e" * 64,
        normalization_version="surface-v1",
        normalized_surface="ישראל",
        occurrence_ordinal=1,
        matched_text="ישראל",
    )
    candidate = NonPersonEntityOccurrenceCandidate.objects.create(
        proposal=proposal,
        candidate_entity=israel,
        status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
    )
    return {
        "retired": retired,
        "survivor": survivor,
        "neighbor": neighbor,
        "israel": israel,
        "approve": approve,
        "merge": merge,
        "survivor_decision": survivor_decision,
        "unrelated_decision": unrelated_decision,
        "candidate": candidate,
    }


def _rows(model, fields):
    return list(model.objects.order_by("pk").values_list(*fields))


class ManifestIsolationTests(TestCase):
    def test_eleven_operation_manifest_is_not_this_merge(self):
        identifiers = {
            operation["candidate_id"] for operation in ELEVEN_OPERATION_MANIFEST
        }
        self.assertEqual(len(ELEVEN_OPERATION_MANIFEST), 11)
        self.assertTrue({"EC0414", "EC0181", "EC0852"}.isdisjoint(identifiers))
        self.assertNotEqual(ELEVEN_OPERATION_CONFIRM_TOKEN, CONFIRM_TOKEN)


class ApplyTests(TestCase):
    def test_dry_run_is_ready_and_writes_nothing(self):
        _approved_snapshot()
        before = _database_rows()

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "ready")
        self.assertEqual(assessment.alias_recommendation, "add")
        self.assertEqual(assessment.entity_delta, 0)
        self.assertEqual(assessment.alias_delta, 1)
        self.assertEqual(assessment.correction_delta, 2)
        self.assertEqual(assessment.decision_delta, 0)
        self.assertEqual(assessment.occurrence_delta, 0)
        self.assertEqual(_database_rows(), before)

    def test_apply_resolves_both_decisions_and_retires_103_once(self):
        created = _approved_snapshot()
        decisions_before = _rows(
            ReviewedNonPersonEntityDecision,
            ("id", "candidate_id", "decision", "final_canonical", "result_entity_id"),
        )
        pins_before = _rows(
            ArchiveItemEntityOccurrence,
            ("id", "entity_id", "normalized_surface", "matched_text"),
        )

        result = apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)

        self.assertTrue(result.applied)
        self.assertEqual(
            _rows(
                ReviewedNonPersonEntityDecision,
                (
                    "id",
                    "candidate_id",
                    "decision",
                    "final_canonical",
                    "result_entity_id",
                ),
            ),
            decisions_before,
        )
        self.assertEqual(
            _rows(
                ArchiveItemEntityOccurrence,
                ("id", "entity_id", "normalized_surface", "matched_text"),
            ),
            pins_before,
        )
        self.assertEqual(created["approve"].result_entity_id, RETIRED_ENTITY_ID)
        self.assertEqual(created["merge"].result_entity_id, RETIRED_ENTITY_ID)
        created["approve"].refresh_from_db()
        created["merge"].refresh_from_db()
        created["survivor_decision"].refresh_from_db()
        self.assertEqual(
            effective_entity_for_decision(created["approve"]).pk, SURVIVOR_ENTITY_ID
        )
        self.assertEqual(
            effective_entity_for_decision(created["merge"]).pk, SURVIVOR_ENTITY_ID
        )
        self.assertEqual(
            effective_entity_for_decision(created["survivor_decision"]).pk,
            SURVIVOR_ENTITY_ID,
        )
        self.assertFalse(
            NonPersonEntityCorrection.objects.filter(
                decision_id=SURVIVOR_DECISION_ID
            ).exists()
        )
        retirements = list(
            NonPersonEntityCorrection.objects.filter(
                retired_entity_id=RETIRED_ENTITY_ID
            )
        )
        self.assertEqual(
            [row.decision_id for row in retirements], [APPROVE_DECISION_ID]
        )
        merge_correction = NonPersonEntityCorrection.objects.get(
            decision_id=MERGE_DECISION_ID
        )
        self.assertIsNone(merge_correction.retired_entity_id)
        self.assertEqual(merge_correction.effective_entity_id, SURVIVOR_ENTITY_ID)
        self.assertEqual(
            NonPersonEntityAlias.objects.get(entity_id=SURVIVOR_ENTITY_ID).name,
            SHORT_NAME,
        )
        self.assertEqual(
            NonPersonEntityAlias.objects.get(entity_id=SURVIVOR_ENTITY_ID).kind,
            ALIAS_KIND,
        )
        created["retired"].refresh_from_db()
        created["survivor"].refresh_from_db()
        self.assertEqual(created["retired"].entity_subtype, "YOUTH_MOVEMENT")
        self.assertEqual(created["survivor"].canonical_name, SURVIVOR_NAME)
        self.assertEqual(
            created["survivor"].entity_subtype, "COMMITTEE_OR_INTERNAL_BODY"
        )
        self.assertEqual(
            set(
                NonPersonEntityCorrection.objects.filter(
                    decision_id__in=(APPROVE_DECISION_ID, MERGE_DECISION_ID)
                ).values_list("manifest_sha256", "approval_reference")
            ),
            {(manifest_sha256(), APPROVAL_REFERENCE)},
        )

    def test_replay_allows_an_additional_resolved_pin_on_entity_655(self):
        created = _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        ArchiveItemEntityOccurrence.objects.create(
            archive_item_id=112,
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            source_text_sha256="cd" * 32,
            normalization_version="surface-v1",
            normalized_surface="extra shadow",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=created["survivor"],
            matched_text="extra shadow",
        )
        before = _database_rows()

        assessment = dry_run_bnei_akiva_shadow_merge()
        again = apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)

        self.assertEqual(assessment.status, "already_applied")
        self.assertFalse(again.applied)
        self.assertEqual(again.assessment.status, "already_applied")
        self.assertEqual(_database_rows(), before)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.get(
                pk=APPROVE_DECISION_ID
            ).result_entity_id,
            RETIRED_ENTITY_ID,
        )

    def test_replay_rejects_a_new_pin_on_retired_entity_103(self):
        created = _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        ArchiveItemEntityOccurrence.objects.create(
            archive_item_id=112,
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            source_text_sha256="ef" * 32,
            normalization_version="surface-v1",
            normalized_surface="retired pin",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=created["retired"],
            matched_text="retired pin",
        )
        before = _database_rows()

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertIn("entity 103 has occurrence pins", assessment.reasons)
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(_database_rows(), before)

    def test_replay_writes_nothing(self):
        _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        before = _database_rows()

        again = apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)

        self.assertFalse(again.applied)
        self.assertEqual(again.assessment.status, "already_applied")
        self.assertEqual(again.assessment.alias_recommendation, "present")
        self.assertEqual(_database_rows(), before)

    def test_failed_insert_rolls_back_the_alias(self):
        _approved_snapshot()
        with patch(
            "documents.services.non_person_entity_bnei_akiva_shadow_correction."
            "NonPersonEntityCorrection.objects.create",
            side_effect=RuntimeError("stop"),
        ):
            with self.assertRaises(RuntimeError):
                apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(
            NonPersonEntityCorrection.objects.exclude(
                approval_reference="existing eleven-operation stand-in"
            ).count(),
            0,
        )
        self.assertEqual(
            NonPersonEntity.objects.get(pk=RETIRED_ENTITY_ID).canonical_name,
            SHORT_NAME,
        )

    def test_partial_correction_does_not_continue(self):
        created = _approved_snapshot()
        NonPersonEntityCorrection.objects.create(
            decision=created["approve"],
            effective_entity=created["survivor"],
            retired_entity=created["retired"],
            canonical_name=SURVIVOR_NAME,
            display_name="",
            entity_subtype="COMMITTEE_OR_INTERNAL_BODY",
            approval_reference=APPROVAL_REFERENCE,
            manifest_sha256=manifest_sha256(),
        )

        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)

        self.assertFalse(
            NonPersonEntityCorrection.objects.filter(
                decision_id=MERGE_DECISION_ID
            ).exists()
        )
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)

    def test_rejected_token_writes_nothing(self):
        _approved_snapshot()
        before = _database_rows()
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm="no")
        self.assertEqual(_database_rows(), before)

    def test_command_dry_run_does_not_apply(self):
        _approved_snapshot()
        before = _database_rows()
        call_command("non_person_entity_bnei_akiva_shadow_correction")
        self.assertEqual(_database_rows(), before)
        with self.assertRaises(CommandError):
            call_command(
                "non_person_entity_bnei_akiva_shadow_correction",
                "--apply",
                "--confirm",
                "no",
            )
        self.assertEqual(_database_rows(), before)


class DriftTests(TestCase):
    def test_survivor_metadata_drift_blocks(self):
        created = _approved_snapshot()
        created[
            "survivor"
        ].entity_subtype = NonPersonEntity.EntitySubtype.YOUTH_MOVEMENT
        created["survivor"].save(update_fields=["entity_subtype", "updated_at"])

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertIn("entity 655 metadata drifted", assessment.reasons)
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)

    def test_preapply_still_rejects_an_additional_pin_on_entity_655(self):
        created = _approved_snapshot()
        ArchiveItemEntityOccurrence.objects.create(
            archive_item_id=112,
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            source_text_sha256="cd" * 32,
            normalization_version="surface-v1",
            normalized_surface="extra shadow",
            occurrence_ordinal=1,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            entity=created["survivor"],
            matched_text="extra shadow",
        )
        before = _database_rows()

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertIn("entity 655 occurrence pins drifted", assessment.reasons)
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(_database_rows(), before)

    def test_replay_blocks_when_an_original_occurrence_identity_drifts(self):
        _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        pin = ArchiveItemEntityOccurrence.objects.get(pk=98)
        pin.occurrence_ordinal = 9
        pin.save(update_fields=["occurrence_ordinal", "updated_at"])
        before = _database_rows()

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertIn("occurrence 98 identity drifted", assessment.reasons)
        self.assertNotIn("entity 655 occurrence pins drifted", assessment.reasons)
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(_database_rows(), before)
        self.assertEqual(
            NonPersonEntityCorrection.objects.filter(
                decision_id__in=(APPROVE_DECISION_ID, MERGE_DECISION_ID)
            ).count(),
            2,
        )

    def test_occurrence_identity_drift_blocks_and_writes_nothing(self):
        _approved_snapshot()
        pin = ArchiveItemEntityOccurrence.objects.get(pk=96)
        other_item = ArchiveItem.objects.create(
            title="פריט אחר",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        cases = (
            ("archive_item", other_item),
            (
                "text_kind",
                ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
            ),
            ("source_text_sha256", "f" * 64),
            ("normalization_version", "surface-v2"),
            ("normalized_surface", "drifted surface"),
            ("occurrence_ordinal", 9),
        )
        for field_name, value in cases:
            with self.subTest(field=field_name):
                original = getattr(pin, field_name)
                setattr(pin, field_name, value)
                pin.save()
                before = _database_rows()
                assessment = dry_run_bnei_akiva_shadow_merge()
                self.assertEqual(assessment.status, "blocked")
                self.assertIn("occurrence 96 identity drifted", assessment.reasons)
                with self.assertRaises(ShadowMergeApplyError):
                    apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
                self.assertEqual(_database_rows(), before)
                setattr(pin, field_name, original)
                pin.save()

    def test_occurrence_reassignment_blocks(self):
        created = _approved_snapshot()
        pin = ArchiveItemEntityOccurrence.objects.get(pk=96)
        pin.entity = created["neighbor"]
        pin.save(update_fields=["entity", "updated_at"])

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertTrue(any("occurrence" in reason for reason in assessment.reasons))

    def test_historical_decision_pointer_drift_blocks(self):
        created = _approved_snapshot()
        created["merge"].result_entity = created["survivor"]
        created["merge"].save(update_fields=["result_entity"])

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertTrue(
            any(
                "EC0181" in reason or "pointers to 103" in reason
                for reason in assessment.reasons
            )
        )

    def test_existing_alias_blocks_before_recommendation(self):
        _approved_snapshot()
        NonPersonEntityAlias.objects.create(
            entity_id=654,
            name=SHORT_NAME,
            kind=ALIAS_KIND,
        )

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertEqual(assessment.alias_recommendation, "withheld")
        self.assertIn("short name collision; alias not recommended", assessment.reasons)
        with self.assertRaises(ShadowMergeApplyError):
            apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 1)

    def test_replay_blocks_when_a_historical_decision_field_changes(self):
        _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)
        decision = ReviewedNonPersonEntityDecision.objects.get(pk=APPROVE_DECISION_ID)
        decision.final_canonical = "שונה"
        decision.save(update_fields=["final_canonical"])

        assessment = dry_run_bnei_akiva_shadow_merge()

        self.assertEqual(assessment.status, "blocked")
        self.assertIn("final_canonical mismatch EC0414", assessment.reasons)
        decision.result_entity.refresh_from_db()
        self.assertEqual(decision.result_entity_id, RETIRED_ENTITY_ID)


class ResolutionTests(TestCase):
    def setUp(self):
        self.created = _approved_snapshot()
        apply_bnei_akiva_shadow_merge(confirm=CONFIRM_TOKEN)

    def test_retired_entity_is_hidden_and_direct_pages_redirect(self):
        retired = self.created["retired"]
        survivor = self.created["survivor"]
        self.assertNotIn(
            retired.pk,
            set(registry_index_queryset("").values_list("pk", flat=True)),
        )
        self.assertNotIn(
            retired.pk,
            set(
                staff_entity_index_queryset(search_query=SHORT_NAME).values_list(
                    "pk", flat=True
                )
            ),
        )
        self.assertIn(
            survivor.pk,
            set(registry_index_queryset(SHORT_NAME).values_list("pk", flat=True)),
        )
        response = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": retired.pk})
        )
        self.assertRedirects(
            response,
            reverse("archive-non-person-detail", kwargs={"entity_id": survivor.pk}),
            fetch_redirect_response=False,
        )
        staff = User.objects.create_user(
            username="shadow_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        staff_response = self.client.get(
            reverse("archive-manage-entity-edit", kwargs={"entity_id": retired.pk})
        )
        self.assertRedirects(
            staff_response,
            reverse("archive-manage-entity-edit", kwargs={"entity_id": survivor.pk}),
            fetch_redirect_response=False,
        )

    def test_unrelated_records_stay_unchanged(self):
        self.created["neighbor"].refresh_from_db()
        self.created["israel"].refresh_from_db()
        self.created["unrelated_decision"].refresh_from_db()
        self.created["candidate"].refresh_from_db()
        self.assertEqual(
            self.created["neighbor"].canonical_name, "הנהלת בני עקיבא במצרים"
        )
        self.assertEqual(
            ArchiveItemEntityOccurrence.objects.get(pk=97).entity_id,
            654,
        )
        self.assertEqual(self.created["candidate"].status, "PENDING")
        self.assertEqual(
            self.created["candidate"].candidate_entity_id, self.created["israel"].pk
        )
        self.assertIsNone(self.created["candidate"].resolved_entity_id)
        self.assertEqual(
            self.created["unrelated_decision"].result_entity_id,
            self.created["israel"].pk,
        )
        stand_in = NonPersonEntityCorrection.objects.get(
            approval_reference="existing eleven-operation stand-in"
        )
        self.assertEqual(stand_in.effective_entity_id, self.created["israel"].pk)
        self.assertIsNone(stand_in.retired_entity_id)
        self.assertEqual(
            list(
                ArchiveItemEntityOccurrence.objects.filter(entity_id=SURVIVOR_ENTITY_ID)
                .order_by("pk")
                .values_list("pk", flat=True)
            ),
            list(OCCURRENCE_IDS),
        )


def _database_rows():
    return (
        _rows(
            NonPersonEntity,
            ("id", "canonical_name", "display_name", "entity_type", "entity_subtype"),
        ),
        _rows(NonPersonEntityAlias, ("id", "entity_id", "name", "kind")),
        _rows(
            NonPersonEntityCorrection,
            (
                "id",
                "decision_id",
                "effective_entity_id",
                "retired_entity_id",
                "canonical_name",
            ),
        ),
        _rows(
            ReviewedNonPersonEntityDecision,
            ("id", "candidate_id", "decision", "result_entity_id", "final_canonical"),
        ),
        _rows(ArchiveItemEntityOccurrence, ("id", "entity_id", "normalized_surface")),
        _rows(
            NonPersonEntityOccurrenceCandidate,
            (
                "id",
                "proposal_id",
                "candidate_entity_id",
                "status",
                "resolved_entity_id",
            ),
        ),
    )
