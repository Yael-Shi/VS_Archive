"""Third non-person contract support. v6 and the second contract stay closed.

Synthetic workbooks stand in for the reviewed file. Production recognition
still requires the exact reviewed SHA.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_apply import (
    ApplyError,
    apply_non_person_entity_review,
)
from documents.services.non_person_entity_review_dry_run import (
    STATE_ALREADY_APPLIED,
    STATE_BLOCKED,
    STATE_DRIFT,
    STATE_READY_TO_APPLY,
    classify_preflight,
)
from documents.services.non_person_entity_review_preflight import (
    ALIAS_COLUMNS,
    AUTHORITATIVE_WORKBOOK_SHA256,
    FINAL_ALIAS_SHEET_NAME,
    REQUIRED_COLUMNS,
    SECOND_APPLY_CONFIRM_TOKEN,
    SECOND_CARRIED_CANDIDATE_IDS,
    SECOND_CONTRACT_SHA256,
    SECOND_DECISION_SOURCE,
    THIRD_APPLY_CONFIRM_TOKEN,
    THIRD_CARRIED_CANDIDATE_IDS,
    THIRD_CARRIED_TARGETS,
    THIRD_CONTRACT_DECISION_COUNTS,
    THIRD_CONTRACT_FINAL_SHEET_NAME,
    THIRD_CONTRACT_ROW_COUNT,
    THIRD_CONTRACT_SHA256,
    THIRD_DECISION_SOURCE,
    V6_APPLY_CONFIRM_TOKEN,
    V6_DECISION_SOURCE,
    CarriedTargetOwnership,
    PreflightError,
    contract_spec_for_sha,
    preflight_authoritative_workbook,
    second_contract_spec,
    sha256_path,
    third_contract_spec,
    v6_contract_spec,
    validate_review_contract,
)
from documents.test_non_person_entity_review_preflight import (
    _cells_for,
    _header,
    _row,
    _write_workbook,
)

_REVIEWED_THIRD_SHA = "c3765a54c7400f76c02f3e63393d9817edc649dd8257f0acab0bcd465f7fa696"
_V6_CARRIED = frozenset({"EC0013", "EC0257", "EC0465"})
_SECOND_CARRIED = frozenset(
    {"EC0313", "EC0324", "EC0414", "EC0464", "EC0539", "EC0544"}
)


def _third_rows():
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    merge = ReviewedNonPersonEntityDecision.Decision.MERGE
    skip = ReviewedNonPersonEntityDecision.Decision.SKIP
    place = NonPersonEntity.EntityType.PLACE
    city = NonPersonEntity.EntitySubtype.CITY
    rows = [
        _row(
            candidate_id,
            approve,
            surface=candidate_id,
            final_canonical=f"canonical-{candidate_id}",
            entity_type=place,
            entity_subtype=city,
        )
        for candidate_id in sorted(THIRD_CARRIED_CANDIDATE_IDS)
    ]
    rows.extend(
        _row(
            f"EC{index:04d}",
            approve,
            surface=f"EC{index:04d}",
            final_canonical=f"canonical-EC{index:04d}",
            entity_type=place,
            entity_subtype=city,
        )
        for index in range(9000, 9395)
    )
    rows.extend(
        [
            _row(
                "EC8101",
                merge,
                surface="EC8101",
                final_canonical="canonical-EC0013",
                merge_target="EC0013",
            ),
            _row(
                "EC8102",
                merge,
                surface="EC8102",
                final_canonical="canonical-EC0313",
                merge_target="EC0313",
            ),
            _row(
                "EC8103",
                merge,
                surface="EC8103",
                final_canonical="canonical-EC9000",
                merge_target="EC9000",
            ),
        ]
    )
    rows.extend(
        _row(
            f"EC81{index:02d}",
            merge,
            surface=f"EC81{index:02d}",
            final_canonical="canonical-EC9000",
            merge_target="EC9000",
        )
        for index in range(4, 29)
    )
    rows.extend(
        _row(f"EC70{index:02d}", skip, surface=f"EC70{index:02d}")
        for index in range(1, 12)
    )
    return rows


def _write_third_workbook(path: Path, rows) -> None:
    _write_workbook(
        path,
        [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
        final_sheet_name=THIRD_CONTRACT_FINAL_SHEET_NAME,
        extra_sheets=[(FINAL_ALIAS_SHEET_NAME, [_header(ALIAS_COLUMNS)])],
    )


@contextmanager
def _recognized_third_workbook(rows=None):
    stored_rows = _third_rows() if rows is None else rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "third.xlsx"
        _write_third_workbook(path, stored_rows)
        digest = sha256_path(path)
        with patch.object(preflight_module, "THIRD_CONTRACT_SHA256", digest):
            yield path


def _seed_carried(preflight, *, skip=None, source_for=None, sha_for=None):
    omitted = skip or set()
    sources = source_for or {}
    shas = sha_for or {}
    by_id = {row.candidate_id: row for row in preflight.rows}
    entities = {}
    for target in preflight.carried_targets:
        if target.candidate_id in omitted:
            continue
        row = by_id[target.candidate_id]
        entity = NonPersonEntity.objects.create(
            canonical_name=row.final_canonical,
            entity_type=row.entity_type,
            entity_subtype=row.entity_subtype,
        )
        ReviewedNonPersonEntityDecision.objects.create(
            source=sources.get(target.candidate_id, target.decision_source),
            candidate_id=target.candidate_id,
            workbook_sha256=shas.get(target.candidate_id, target.workbook_sha256),
            decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
            review_status=row.status,
            final_canonical=row.final_canonical,
            display_name="",
            merge_target_candidate_id="",
            contextual_surfaces=row.surface,
            note=row.final_notes,
            result_entity=entity,
        )
        entities[target.candidate_id] = entity
    return entities


def _tracked_counts():
    return (
        NonPersonEntity.objects.count(),
        NonPersonEntityAlias.objects.count(),
        ReviewedNonPersonEntityDecision.objects.count(),
        ArchiveItemEntityOccurrence.objects.count(),
    )


class ThirdContractPreflightTests(SimpleTestCase):
    def test_production_spec_keeps_the_reviewed_sha_and_mixed_owners(self):
        self.assertEqual(THIRD_CONTRACT_SHA256, _REVIEWED_THIRD_SHA)
        spec = contract_spec_for_sha(_REVIEWED_THIRD_SHA)
        assert spec is not None
        self.assertEqual(spec.row_count, 443)
        self.assertEqual(spec.row_count, THIRD_CONTRACT_ROW_COUNT)
        self.assertEqual(
            dict(spec.decision_counts), dict(THIRD_CONTRACT_DECISION_COUNTS)
        )
        self.assertEqual(spec.alias_count, 0)
        self.assertEqual(dict(spec.split_route_counts), {})
        self.assertFalse(spec.read_split_routes)
        self.assertFalse(spec.read_alias_review)
        self.assertEqual(spec.confirm_token, THIRD_APPLY_CONFIRM_TOKEN)
        self.assertEqual(spec.decision_source, THIRD_DECISION_SOURCE)
        self.assertEqual(spec.carried_decision_source, "")
        self.assertEqual(spec.carried_workbook_sha256, "")
        self.assertEqual(spec.carried_candidate_ids, THIRD_CARRIED_CANDIDATE_IDS)
        self.assertEqual(spec.carried_targets, THIRD_CARRIED_TARGETS)
        owners = {
            target.candidate_id: (target.decision_source, target.workbook_sha256)
            for target in spec.carried_targets
        }
        for candidate_id in _V6_CARRIED:
            self.assertEqual(
                owners[candidate_id],
                (V6_DECISION_SOURCE, AUTHORITATIVE_WORKBOOK_SHA256),
            )
        for candidate_id in _SECOND_CARRIED:
            self.assertEqual(
                owners[candidate_id],
                (SECOND_DECISION_SOURCE, SECOND_CONTRACT_SHA256),
            )
        self.assertIsNone(contract_spec_for_sha("0" * 64))

    def test_v6_and_second_contract_specs_are_unchanged(self):
        v6 = v6_contract_spec()
        second = second_contract_spec()
        self.assertEqual(v6.sha256, AUTHORITATIVE_WORKBOOK_SHA256)
        self.assertEqual(v6.confirm_token, V6_APPLY_CONFIRM_TOKEN)
        self.assertEqual(v6.carried_candidate_ids, frozenset())
        self.assertEqual(v6.carried_targets, ())
        self.assertEqual(second.sha256, SECOND_CONTRACT_SHA256)
        self.assertEqual(second.confirm_token, SECOND_APPLY_CONFIRM_TOKEN)
        self.assertEqual(second.carried_candidate_ids, SECOND_CARRIED_CANDIDATE_IDS)
        self.assertEqual(second.carried_decision_source, V6_DECISION_SOURCE)
        self.assertEqual(second.carried_workbook_sha256, AUTHORITATIVE_WORKBOOK_SHA256)
        self.assertEqual(second.carried_targets, ())
        self.assertEqual(
            contract_spec_for_sha(AUTHORITATIVE_WORKBOOK_SHA256),
            v6,
        )
        self.assertEqual(contract_spec_for_sha(SECOND_CONTRACT_SHA256), second)

    def test_synthetic_workbook_matches_third_contract_counts(self):
        self.assertEqual(len(_third_rows()), 443)
        with _recognized_third_workbook() as path:
            result = preflight_authoritative_workbook(path)

        self.assertEqual(len(result.rows), 443)
        self.assertEqual(
            result.decision_counts(),
            {
                "APPROVE": 404,
                "MERGE": 28,
                "SKIP": 11,
                "SPLIT": 0,
                "NEEDS_RESEARCH": 0,
            },
        )
        self.assertEqual(result.aliases, ())
        self.assertEqual(result.split_routes, ())
        self.assertEqual(
            set(result.carried_candidate_ids), set(THIRD_CARRIED_CANDIDATE_IDS)
        )
        self.assertEqual(result.carried_targets, THIRD_CARRIED_TARGETS)
        self.assertEqual(THIRD_CONTRACT_SHA256, _REVIEWED_THIRD_SHA)

    def test_unpatched_synthetic_workbook_is_unrecognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "third.xlsx"
            _write_third_workbook(path, _third_rows())
            with self.assertRaises(PreflightError) as caught:
                preflight_authoritative_workbook(path)
        self.assertIn("SHA256_UNRECOGNIZED", str(caught.exception))
        self.assertEqual(THIRD_CONTRACT_SHA256, _REVIEWED_THIRD_SHA)

    def test_carried_ownership_must_match_the_carried_ids(self):
        spec = replace(
            third_contract_spec(),
            carried_targets=tuple(
                target
                for target in THIRD_CARRIED_TARGETS
                if target.candidate_id != "EC0544"
            ),
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(_third_rows(), (), (), spec=spec)
        message = str(caught.exception)
        self.assertIn("CARRIED_TARGET_OWNERSHIP_MISMATCH", message)
        self.assertIn("CARRIED_SOURCE_MISSING: EC0544", message)

    def test_carried_sha_and_contract_source_are_rejected(self):
        invalid_sha = tuple(
            CarriedTargetOwnership(
                candidate_id=target.candidate_id,
                decision_source=target.decision_source,
                workbook_sha256="zz"
                if target.candidate_id == "EC0013"
                else target.workbook_sha256,
            )
            for target in THIRD_CARRIED_TARGETS
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(
                _third_rows(),
                (),
                (),
                spec=replace(third_contract_spec(), carried_targets=invalid_sha),
            )
        self.assertIn("CARRIED_SHA_INVALID: EC0013", str(caught.exception))

        own_source = tuple(
            CarriedTargetOwnership(
                candidate_id=target.candidate_id,
                decision_source=(
                    THIRD_DECISION_SOURCE
                    if target.candidate_id == "EC0257"
                    else target.decision_source
                ),
                workbook_sha256=target.workbook_sha256,
            )
            for target in THIRD_CARRIED_TARGETS
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(
                _third_rows(),
                (),
                (),
                spec=replace(third_contract_spec(), carried_targets=own_source),
            )
        self.assertIn(
            "CARRIED_SOURCE_IS_CONTRACT_SOURCE: EC0257", str(caught.exception)
        )


class ThirdContractApplyTests(TestCase):
    def test_mixed_owners_are_already_applied_and_new_rows_are_ready(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(preflight)
            classified = classify_preflight(preflight)

        by_id = {item.candidate_id: item for item in classified.candidates}
        for target in THIRD_CARRIED_TARGETS:
            item = by_id[target.candidate_id]
            self.assertEqual(item.state, STATE_ALREADY_APPLIED)
            self.assertEqual(item.plan.decision_source, target.decision_source)
            self.assertEqual(item.plan.workbook_sha256, target.workbook_sha256)
            self.assertTrue(item.plan.existing_decision_required)
        ready = [
            item for item in classified.candidates if item.state == STATE_READY_TO_APPLY
        ]
        self.assertEqual(len(ready), 434)
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision
                == ReviewedNonPersonEntityDecision.Decision.APPROVE
            ),
            395,
        )
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision == ReviewedNonPersonEntityDecision.Decision.MERGE
            ),
            28,
        )
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision == ReviewedNonPersonEntityDecision.Decision.SKIP
            ),
            11,
        )

    def test_missing_carried_decision_is_blocked(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(preflight, skip={"EC0465", "EC0544"})
            classified = classify_preflight(preflight)

        by_id = {item.candidate_id: item for item in classified.candidates}
        for candidate_id in ("EC0465", "EC0544"):
            self.assertEqual(by_id[candidate_id].state, STATE_BLOCKED)
            self.assertIn(
                "carried target is not already applied",
                by_id[candidate_id].reasons,
            )
        self.assertEqual(by_id["EC0013"].state, STATE_ALREADY_APPLIED)
        self.assertEqual(by_id["EC0313"].state, STATE_ALREADY_APPLIED)

    def test_wrong_carried_source_is_not_reused(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(
                preflight,
                source_for={"EC0013": SECOND_DECISION_SOURCE},
            )
            classified = classify_preflight(preflight)

        item = next(
            row for row in classified.candidates if row.candidate_id == "EC0013"
        )
        self.assertEqual(item.state, STATE_BLOCKED)
        self.assertIn("carried target is not already applied", item.reasons)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.get(candidate_id="EC0013").source,
            SECOND_DECISION_SOURCE,
        )
        self.assertFalse(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=V6_DECISION_SOURCE,
                candidate_id="EC0013",
            ).exists()
        )

    def test_wrong_carried_workbook_sha_is_state_drift(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(preflight, sha_for={"EC0313": "0" * 64})
            classified = classify_preflight(preflight)

        item = next(
            row for row in classified.candidates if row.candidate_id == "EC0313"
        )
        self.assertEqual(item.state, STATE_DRIFT)
        self.assertIn("decision field mismatch: workbook_sha256", item.reasons)

    def test_apply_creates_only_new_rows_and_replay_is_a_noop(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            carried = _seed_carried(preflight)
            before = _tracked_counts()

            result = apply_non_person_entity_review(
                path,
                confirm=THIRD_APPLY_CONFIRM_TOKEN,
            )

            self.assertEqual(result.entities_created, 395)
            self.assertEqual(result.aliases_created, 0)
            self.assertEqual(result.decisions_created, 434)
            self.assertEqual(result.occurrences_created, 0)
            self.assertEqual(result.after_counts[STATE_ALREADY_APPLIED], 443)
            self.assertEqual(result.after_counts[STATE_READY_TO_APPLY], 0)
            self.assertEqual(_tracked_counts()[1], before[1])
            self.assertEqual(_tracked_counts()[3], before[3])
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=THIRD_DECISION_SOURCE
                ).count(),
                434,
            )
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=V6_DECISION_SOURCE
                ).count(),
                3,
            )
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=SECOND_DECISION_SOURCE
                ).count(),
                6,
            )
            for target in THIRD_CARRIED_TARGETS:
                stored = ReviewedNonPersonEntityDecision.objects.get(
                    source=target.decision_source,
                    candidate_id=target.candidate_id,
                )
                self.assertEqual(
                    stored.result_entity_id, carried[target.candidate_id].pk
                )
                self.assertEqual(stored.workbook_sha256, target.workbook_sha256)
                self.assertFalse(
                    ReviewedNonPersonEntityDecision.objects.filter(
                        source=THIRD_DECISION_SOURCE,
                        candidate_id=target.candidate_id,
                    ).exists()
                )
            v6_merge = ReviewedNonPersonEntityDecision.objects.get(
                source=THIRD_DECISION_SOURCE,
                candidate_id="EC8101",
            )
            second_merge = ReviewedNonPersonEntityDecision.objects.get(
                source=THIRD_DECISION_SOURCE,
                candidate_id="EC8102",
            )
            new_merge = ReviewedNonPersonEntityDecision.objects.get(
                source=THIRD_DECISION_SOURCE,
                candidate_id="EC8103",
            )
            self.assertEqual(v6_merge.result_entity_id, carried["EC0013"].pk)
            self.assertEqual(second_merge.result_entity_id, carried["EC0313"].pk)
            self.assertNotEqual(new_merge.result_entity_id, carried["EC0013"].pk)
            self.assertEqual(
                new_merge.result_entity.canonical_name,
                "canonical-EC9000",
            )
            self.assertEqual(NonPersonEntity.objects.count(), before[0] + 395)

            replay = apply_non_person_entity_review(
                path,
                confirm=THIRD_APPLY_CONFIRM_TOKEN,
            )
            self.assertEqual(replay.entities_created, 0)
            self.assertEqual(replay.aliases_created, 0)
            self.assertEqual(replay.decisions_created, 0)
            self.assertEqual(replay.occurrences_created, 0)
            self.assertEqual(replay.after_counts[STATE_ALREADY_APPLIED], 443)
            self.assertEqual(NonPersonEntity.objects.count(), before[0] + 395)
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=THIRD_DECISION_SOURCE
                ).count(),
                434,
            )

    def test_apply_refuses_a_missing_carried_target_without_writes(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(preflight, skip={"EC0257"})
            before = _tracked_counts()
            with self.assertRaises(ApplyError) as caught:
                apply_non_person_entity_review(
                    path,
                    confirm=THIRD_APPLY_CONFIRM_TOKEN,
                )
        self.assertIn("EC0257", str(caught.exception))
        self.assertIn("BLOCKED", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_wrong_confirmation_token_is_rejected(self):
        with _recognized_third_workbook() as path:
            preflight = preflight_authoritative_workbook(path)
            _seed_carried(preflight)
            before = _tracked_counts()
            for confirm in (
                V6_APPLY_CONFIRM_TOKEN,
                SECOND_APPLY_CONFIRM_TOKEN,
                "APPLY_NON_PERSON_THIRD_CONTRACT_WRONG",
            ):
                with self.assertRaises(ApplyError) as caught:
                    apply_non_person_entity_review(path, confirm=confirm)
                self.assertIn("confirmation token rejected", str(caught.exception))
            self.assertEqual(_tracked_counts(), before)
