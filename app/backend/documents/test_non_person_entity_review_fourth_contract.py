"""Fourth non-person contract v2. Earlier contracts stay closed.

Synthetic workbooks stand in for the reviewed file. Production recognition
still requires the exact reviewed v2 SHA. The superseded v1 SHA is not a spec.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from documents.models import NonPersonEntity, ReviewedNonPersonEntityDecision
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_apply import (
    ApplyError,
    _require_confirm,
    _require_known_confirm,
)
from documents.services.non_person_entity_review_preflight import (
    ALIAS_COLUMNS,
    AUTHORITATIVE_WORKBOOK_SHA256,
    FINAL_ALIAS_SHEET_NAME,
    FOURTH_APPLY_CONFIRM_TOKEN,
    FOURTH_CONTRACT_DECISION_COUNTS,
    FOURTH_CONTRACT_FINAL_SHEET_NAME,
    FOURTH_CONTRACT_ROW_COUNT,
    FOURTH_CONTRACT_SHA256,
    FOURTH_DECISION_SOURCE,
    REQUIRED_COLUMNS,
    SECOND_CONTRACT_SHA256,
    THIRD_APPLY_CONFIRM_TOKEN,
    THIRD_CONTRACT_SHA256,
    PreflightError,
    ReviewedAliasRow,
    contract_spec_for_sha,
    fourth_contract_spec,
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

_REVIEWED_FOURTH_SHA = (
    "278f7b8c5bbd5d0530c9a1bb8d742d2d650c28652bf649db539ec17bb33f7350"
)
_SUPERSEDED_V1_SHA = "a769f59e082967fe49497004034fdbb2b5a9ced908fed0877e73b7ec37c273cd"


def _fourth_rows():
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    merge = ReviewedNonPersonEntityDecision.Decision.MERGE
    place = NonPersonEntity.EntityType.PLACE
    city = NonPersonEntity.EntitySubtype.CITY
    rows = [
        _row(
            f"EC{index:04d}",
            approve,
            surface=f"EC{index:04d}",
            final_canonical=f"canonical-EC{index:04d}",
            entity_type=place,
            entity_subtype=city,
        )
        for index in range(9000, 9099)
    ]
    rows.append(
        _row(
            "EC9100",
            merge,
            surface="EC9100",
            final_canonical="canonical-EC9000",
            merge_target="EC9000",
        )
    )
    return rows


def _write_fourth_workbook(path: Path, rows) -> None:
    _write_workbook(
        path,
        [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
        final_sheet_name=FOURTH_CONTRACT_FINAL_SHEET_NAME,
        extra_sheets=[(FINAL_ALIAS_SHEET_NAME, [_header(ALIAS_COLUMNS)])],
    )


@contextmanager
def _recognized_fourth_workbook(rows=None):
    stored_rows = _fourth_rows() if rows is None else rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fourth.xlsx"
        _write_fourth_workbook(path, stored_rows)
        digest = sha256_path(path)
        with patch.object(preflight_module, "FOURTH_CONTRACT_SHA256", digest):
            yield path


class FourthContractPreflightTests(SimpleTestCase):
    def test_production_spec_keeps_the_reviewed_v2_sha_and_counts(self):
        self.assertEqual(FOURTH_CONTRACT_SHA256, _REVIEWED_FOURTH_SHA)
        spec = contract_spec_for_sha(_REVIEWED_FOURTH_SHA)
        assert spec is not None
        self.assertEqual(spec, fourth_contract_spec())
        self.assertEqual(spec.final_sheet_name, FOURTH_CONTRACT_FINAL_SHEET_NAME)
        self.assertEqual(spec.final_sheet_name, "FINAL_RECON_2026-10-12_v4")
        self.assertEqual(spec.row_count, 100)
        self.assertEqual(spec.row_count, FOURTH_CONTRACT_ROW_COUNT)
        self.assertEqual(
            dict(spec.decision_counts), dict(FOURTH_CONTRACT_DECISION_COUNTS)
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.APPROVE],
            99,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.MERGE],
            1,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.SKIP],
            0,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.SPLIT],
            0,
        )
        self.assertEqual(
            spec.decision_counts[
                ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH
            ],
            0,
        )
        self.assertEqual(spec.alias_count, 0)
        self.assertEqual(dict(spec.split_route_counts), {})
        self.assertFalse(spec.read_split_routes)
        self.assertFalse(spec.read_alias_review)
        self.assertEqual(spec.confirm_token, FOURTH_APPLY_CONFIRM_TOKEN)
        self.assertEqual(spec.decision_source, FOURTH_DECISION_SOURCE)
        self.assertEqual(
            spec.decision_source,
            "vs_archive_non_person_fourth_contract_2026_10_12",
        )
        self.assertEqual(spec.carried_candidate_ids, frozenset())
        self.assertEqual(spec.carried_targets, ())
        self.assertEqual(spec.carried_decision_source, "")
        self.assertEqual(spec.carried_workbook_sha256, "")

    def test_superseded_v1_sha_is_not_recognized(self):
        self.assertIsNone(contract_spec_for_sha(_SUPERSEDED_V1_SHA))
        self.assertIsNone(contract_spec_for_sha("0" * 64))
        self.assertNotEqual(_SUPERSEDED_V1_SHA, FOURTH_CONTRACT_SHA256)

    def test_earlier_contract_specs_are_unchanged(self):
        v6 = v6_contract_spec()
        second = second_contract_spec()
        third = third_contract_spec()
        self.assertEqual(v6.sha256, AUTHORITATIVE_WORKBOOK_SHA256)
        self.assertEqual(second.sha256, SECOND_CONTRACT_SHA256)
        self.assertEqual(third.sha256, THIRD_CONTRACT_SHA256)
        self.assertEqual(contract_spec_for_sha(v6.sha256), v6)
        self.assertEqual(contract_spec_for_sha(second.sha256), second)
        self.assertEqual(contract_spec_for_sha(third.sha256), third)
        self.assertNotEqual(v6.sha256, FOURTH_CONTRACT_SHA256)
        self.assertNotEqual(second.sha256, FOURTH_CONTRACT_SHA256)
        self.assertNotEqual(third.sha256, FOURTH_CONTRACT_SHA256)

    def test_synthetic_workbook_matches_fourth_contract_counts(self):
        self.assertEqual(len(_fourth_rows()), 100)
        with _recognized_fourth_workbook() as path:
            result = preflight_authoritative_workbook(path)

        self.assertEqual(len(result.rows), 100)
        self.assertEqual(len(result.entity_creation_candidate_ids), 99)
        self.assertEqual(
            result.decision_counts(),
            {
                "APPROVE": 99,
                "MERGE": 1,
                "SKIP": 0,
                "SPLIT": 0,
                "NEEDS_RESEARCH": 0,
            },
        )
        self.assertEqual(result.aliases, ())
        self.assertEqual(result.split_routes, ())
        self.assertEqual(result.decision_source, FOURTH_DECISION_SOURCE)
        self.assertEqual(result.carried_candidate_ids, ())
        self.assertEqual(result.carried_targets, ())
        self.assertEqual(result.final_sheet_name, FOURTH_CONTRACT_FINAL_SHEET_NAME)
        self.assertEqual(FOURTH_CONTRACT_SHA256, _REVIEWED_FOURTH_SHA)

    def test_unpatched_synthetic_workbook_is_unrecognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fourth.xlsx"
            _write_fourth_workbook(path, _fourth_rows())
            with self.assertRaises(PreflightError) as caught:
                preflight_authoritative_workbook(path)
        self.assertIn("SHA256_UNRECOGNIZED", str(caught.exception))
        self.assertNotIn(FOURTH_CONTRACT_SHA256, str(caught.exception))
        self.assertEqual(FOURTH_CONTRACT_SHA256, _REVIEWED_FOURTH_SHA)

    def test_wrong_decision_count_fails_closed(self):
        rows = _fourth_rows()
        rows[0] = replace(
            rows[0],
            final_decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
            final_canonical="",
            entity_type="",
            entity_subtype="",
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(rows, (), (), spec=fourth_contract_spec())
        message = str(caught.exception)
        self.assertIn("DECISION_COUNT: APPROVE expected 99 got 98", message)
        self.assertIn("DECISION_COUNT: SKIP expected 0 got 1", message)

    def test_alias_row_fails_closed(self):
        alias = ReviewedAliasRow(
            target_candidate_id="EC9000",
            target_canonical="canonical-EC9000",
            alias_name="unused",
            alias_kind="SPELLING_VARIANT",
            source_candidate_id="EC9100",
            review_status="APPROVED",
            basis="not reviewed",
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(
                _fourth_rows(),
                (alias,),
                (),
                spec=fourth_contract_spec(),
            )
        self.assertIn("ALIAS_COUNT: expected 0 got 1", str(caught.exception))

    def test_split_row_fails_closed(self):
        rows = _fourth_rows()
        rows[1] = replace(
            rows[1],
            final_decision=ReviewedNonPersonEntityDecision.Decision.SPLIT,
            final_canonical="",
            entity_type="",
            entity_subtype="",
        )
        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(rows, (), (), spec=fourth_contract_spec())
        message = str(caught.exception)
        self.assertIn("DECISION_COUNT: SPLIT expected 0 got 1", message)
        self.assertIn(f"SPLIT_ROUTES_MISSING: {rows[1].candidate_id}", message)
        self.assertIn(f"UNEXPECTED_SPLIT_CANDIDATE: {rows[1].candidate_id}", message)

    def test_confirm_token_is_bound_to_the_v2_sha(self):
        _require_known_confirm(FOURTH_APPLY_CONFIRM_TOKEN)
        _require_confirm(FOURTH_APPLY_CONFIRM_TOKEN, FOURTH_CONTRACT_SHA256)
        with self.assertRaises(ApplyError) as unknown:
            _require_known_confirm("APPLY_NON_PERSON_FOURTH_CONTRACT_WRONG")
        self.assertIn("confirmation token rejected", str(unknown.exception))
        with self.assertRaises(ApplyError) as mismatched:
            _require_confirm(THIRD_APPLY_CONFIRM_TOKEN, FOURTH_CONTRACT_SHA256)
        self.assertIn("confirmation token rejected", str(mismatched.exception))
        with self.assertRaises(ApplyError) as unrecognized:
            _require_confirm(FOURTH_APPLY_CONFIRM_TOKEN, _SUPERSEDED_V1_SHA)
        self.assertIn("confirmation token rejected", str(unrecognized.exception))
