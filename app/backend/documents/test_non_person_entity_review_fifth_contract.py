"""Fifth non-person contract. Earlier contracts stay closed.

The reviewed workbook covers the representable remainder of the 53 pending
candidates. Nine splits and two dependent merges stay outside it.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
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
    ALIAS_REVIEW_STATUS_APPROVED,
    FINAL_ALIAS_SHEET_NAME,
    FIFTH_APPLY_CONFIRM_TOKEN,
    FIFTH_CARRIED_CANDIDATE_IDS,
    FIFTH_CONTRACT_DECISION_COUNTS,
    FIFTH_CONTRACT_FINAL_SHEET_NAME,
    FIFTH_CONTRACT_ROW_COUNT,
    FIFTH_CONTRACT_SHA256,
    FIFTH_DECISION_SOURCE,
    FOURTH_CONTRACT_SHA256,
    REQUIRED_COLUMNS,
    PreflightError,
    ReviewedAliasRow,
    contract_spec_for_sha,
    fifth_contract_spec,
    fourth_contract_spec,
    preflight_authoritative_workbook,
    sha256_path,
)
from documents.test_non_person_entity_review_preflight import (
    _alias_cells,
    _cells_for,
    _header,
    _row,
    _write_workbook,
)

_REVIEWED_FIFTH_SHA = "968614527bd29095cc371c00515f21d758ce9b7375be1611db1101ac02a6f01a"
_REVIEWED_WORKBOOK = Path(
    "/mnt/c/Users/User/vs-archive-people-agent/outputs/"
    "non_person_registry_reconciliation/fifth_contract/fifth_contract.xlsx"
)
_CARRIED_OWNER_IDS = frozenset(
    {
        "EC0001",
        "EC0002",
        "EC0003",
        "EC0004",
        "EC0006",
        "EC0014",
        "EC0059",
        "EC0063",
        "EC0110",
        "EC0162",
        "EC0178",
        "EC0223",
        "EC0231",
        "EC0234",
        "EC0278",
        "EC0301",
        "EC0426",
        "EC0433",
        "EC0476",
        "EC0479",
        "EC0488",
        "EC0528",
        "EC0574",
        "EC0606",
        "EC0750",
    }
)
_NEW_APPROVE_IDS = frozenset({"EC0147", "EC0554", "EC0575", "EC0620", "EC0625"})
# Pending decisions copied from the approved candidate matrix. Canonical text
# is the importer value, not a source-specific alias.
_PENDING_DECISIONS = (
    ("EC0016", "MERGE", "פורט סעיד", "EC0476"),
    ("EC0147", "APPROVE", "היישוב", ""),
    ("EC0148", "MERGE", "היישוב", "EC0147"),
    ("EC0156", "MERGE", "יהדות מצרים", "EC0059"),
    ("EC0159", "MERGE", "מלחמת העצמאות", "EC0110"),
    ("EC0171", "MERGE", "מבצע פסח", "EC0162"),
    ("EC0180", "MERGE", "הליגה נגד אנטישמיות", "EC0606"),
    ("EC0190", "MERGE", "ממשלת מצרים", "EC0625"),
    ("EC0195", "MERGE", "ממשלת מצרים", "EC0625"),
    ("EC0196", "MERGE", "שירות המידע של הלשכה הערבית של שגרירות צרפת בקהיר", "EC0178"),
    ("EC0198", "MERGE", "שירות המידע של הלשכה הערבית של שגרירות צרפת בקהיר", "EC0178"),
    ("EC0227", "MERGE", "ירושלים", "EC0014"),
    ("EC0228", "MERGE", "פריז", "EC0426"),
    ("EC0229", "MERGE", "ארץ ישראל", "EC0006"),
    ("EC0230", "MERGE", "מצרים", "EC0001"),
    ("EC0279", "MERGE", "היהודי זיס", "EC0278"),
    ("EC0308", "MERGE", "פולין", "EC0488"),
    ("EC0327", "MERGE", "ועדת השבעה", "EC0620"),
    ("EC0343", "MERGE", "מלחמת העצמאות", "EC0110"),
    ("EC0399", "MERGE", "ועדת השבעה", "EC0620"),
    ("EC0428", "MERGE", "ציריך", "EC0479"),
    ("EC0478", "MERGE", "קהיר", "EC0002"),
    ("EC0480", "MERGE", "אלכסנדריה", "EC0003"),
    ("EC0483", "MERGE", "בריטניה", "EC0301"),
    ("EC0494", "MERGE", "ישראל", "EC0004"),
    ("EC0497", "MERGE", "לוב", "EC0750"),
    ("EC0498", "MERGE", "איראן", "EC0433"),
    ("EC0509", "MERGE", "בית החולים היהודי בקהיר", "EC0231"),
    ("EC0525", "MERGE", "אל־אזריטה", "EC0528"),
    ("EC0554", "APPROVE", "כלא אסתיאנאף בקהיר", ""),
    ("EC0573", "MERGE", "האוניברסיטה העברית בירושלים", "EC0574"),
    ("EC0575", "APPROVE", "אוניברסיטת אלכסנדריה", ""),
    ("EC0579", "MERGE", "אוניברסיטת אלכסנדריה", "EC0575"),
    ("EC0605", "MERGE", "הליגה נגד אנטישמיות", "EC0606"),
    ("EC0620", "APPROVE", "ועדת השבעה", ""),
    ("EC0625", "APPROVE", "ממשלת מצרים", ""),
    ("EC0638", "MERGE", "הקהילה היהודית באלכסנדריה", "EC0063"),
    ("EC0752", "MERGE", "מצרים", "EC0001"),
    ("EC0765", "MERGE", "הרובע היהודי בקהיר", "EC0234"),
    ("EC0782", "MERGE", "רחוב פארוק בקהיר", "EC0223"),
    ("EC0814", "NEEDS_RESEARCH", "", ""),
    ("EC0822", "NEEDS_RESEARCH", "", ""),
)
_CONTEXTUAL_NAMES_NOT_ALIASES = frozenset(
    {
        "Mazzarita",
        "רחוב פארוק אל־אוול",
        "יציאת מצרים",
        "l'Egypte",
        "Angleterre",
        "juif errant",
        "אנגליהם",
        "מצרי",
        "جامعة الاسكندرية",
        "אוניברסיטות אלכסנדריה",
        "L.I.C.A",
    }
)
_EXCLUDED = frozenset(
    {
        "EC0026",
        "EC0033",
        "EC0036",
        "EC0050",
        "EC0062",
        "EC0084",
        "EC0432",
        "EC0499",
        "EC0600",
        "EC0633",
        "EC0743",
    }
)


def _fifth_rows():
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    merge = ReviewedNonPersonEntityDecision.Decision.MERGE
    research = ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH
    unresolved = ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED
    place = NonPersonEntity.EntityType.PLACE
    city = NonPersonEntity.EntitySubtype.CITY
    rows = [
        _row(
            candidate_id,
            approve,
            surface=candidate_id,
            final_canonical=f"carried-{candidate_id}",
            entity_type=place,
            entity_subtype=city,
        )
        for candidate_id in sorted(FIFTH_CARRIED_CANDIDATE_IDS)
    ]
    rows.extend(
        _row(
            f"EC9{index:03d}",
            approve,
            surface=f"EC9{index:03d}",
            final_canonical=f"canonical-EC9{index:03d}",
            entity_type=place,
            entity_subtype=city,
        )
        for index in range(5)
    )
    rows.extend(
        _row(
            f"EC8{index:03d}",
            merge,
            surface=f"EC8{index:03d}",
            final_canonical="carried-EC0001",
            merge_target="EC0001",
        )
        for index in range(35)
    )
    rows.extend(
        _row(
            candidate_id,
            research,
            surface=candidate_id,
            status=unresolved,
        )
        for candidate_id in ("EC0814", "EC0822")
    )
    return rows


def _write_fifth_workbook(path: Path, rows) -> None:
    _write_workbook(
        path,
        [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
        final_sheet_name=FIFTH_CONTRACT_FINAL_SHEET_NAME,
        extra_sheets=[(FINAL_ALIAS_SHEET_NAME, [_header(ALIAS_COLUMNS)])],
    )


@contextmanager
def _recognized_fifth_workbook(rows=None):
    stored_rows = _fifth_rows() if rows is None else rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fifth.xlsx"
        _write_fifth_workbook(path, stored_rows)
        digest = sha256_path(path)
        with patch.object(preflight_module, "FIFTH_CONTRACT_SHA256", digest):
            yield path


class FifthContractPreflightTests(SimpleTestCase):
    def test_production_spec_keeps_the_reviewed_sha_counts_and_owners(self):
        self.assertEqual(FIFTH_CONTRACT_SHA256, _REVIEWED_FIFTH_SHA)
        spec = contract_spec_for_sha(_REVIEWED_FIFTH_SHA)
        assert spec is not None
        self.assertEqual(spec, fifth_contract_spec())
        self.assertEqual(spec.final_sheet_name, "FINAL_RECON_2026-10-14")
        self.assertEqual(spec.row_count, 67)
        self.assertEqual(spec.row_count, FIFTH_CONTRACT_ROW_COUNT)
        self.assertEqual(
            dict(spec.decision_counts), dict(FIFTH_CONTRACT_DECISION_COUNTS)
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.APPROVE],
            30,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.MERGE],
            35,
        )
        self.assertEqual(
            spec.decision_counts[
                ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH
            ],
            2,
        )
        self.assertEqual(spec.alias_count, 0)
        self.assertEqual(dict(spec.split_route_counts), {})
        self.assertFalse(spec.read_split_routes)
        self.assertEqual(spec.confirm_token, FIFTH_APPLY_CONFIRM_TOKEN)
        self.assertEqual(spec.decision_source, FIFTH_DECISION_SOURCE)
        self.assertEqual(len(spec.carried_candidate_ids), 25)
        self.assertEqual(
            {target.candidate_id for target in spec.carried_targets},
            set(spec.carried_candidate_ids),
        )
        self.assertIn("EC0162", spec.carried_candidate_ids)
        self.assertNotIn("EC0057", spec.carried_candidate_ids)
        self.assertTrue(_EXCLUDED.isdisjoint(spec.carried_candidate_ids))
        self.assertEqual(
            contract_spec_for_sha(FOURTH_CONTRACT_SHA256),
            fourth_contract_spec(),
        )

    def test_synthetic_workbook_accepts_carried_merges_and_research(self):
        with _recognized_fifth_workbook() as path:
            result = preflight_authoritative_workbook(path)

        self.assertEqual(len(result.rows), 67)
        self.assertEqual(result.decision_counts()["MERGE"], 35)
        self.assertEqual(result.decision_counts()["NEEDS_RESEARCH"], 2)
        self.assertEqual(result.aliases, ())
        self.assertEqual(result.split_routes, ())
        self.assertEqual(result.apply_blockers, ())

    def test_split_row_without_routes_is_rejected(self):
        rows = _fifth_rows()
        rows[0] = _row(
            rows[0].candidate_id,
            ReviewedNonPersonEntityDecision.Decision.SPLIT,
            surface=rows[0].surface,
        )
        with _recognized_fifth_workbook(rows) as path:
            with self.assertRaises(PreflightError) as caught:
                preflight_authoritative_workbook(path)

        self.assertIn("DECISION_COUNT", str(caught.exception))
        self.assertIn("SPLIT_ROUTES_MISSING", str(caught.exception))

    def test_alias_row_is_rejected(self):
        rows = _fifth_rows()
        alias = ReviewedAliasRow(
            target_candidate_id="EC0001",
            target_canonical="carried-EC0001",
            alias_name="Mazzarita",
            alias_kind="TRANSLITERATION_VARIANT",
            source_candidate_id="EC0525",
            review_status=ALIAS_REVIEW_STATUS_APPROVED,
            basis="approved variant",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fifth-alias.xlsx"
            _write_workbook(
                path,
                [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
                final_sheet_name=FIFTH_CONTRACT_FINAL_SHEET_NAME,
                extra_sheets=[
                    (
                        FINAL_ALIAS_SHEET_NAME,
                        [_header(ALIAS_COLUMNS), _alias_cells(alias)],
                    )
                ],
            )
            digest = sha256_path(path)
            with patch.object(preflight_module, "FIFTH_CONTRACT_SHA256", digest):
                with self.assertRaises(PreflightError) as caught:
                    preflight_authoritative_workbook(path)

        self.assertIn("ALIAS_COUNT: expected 0 got 1", str(caught.exception))

    def test_confirm_token_matches_only_this_contract(self):
        _require_known_confirm(FIFTH_APPLY_CONFIRM_TOKEN)
        _require_confirm(FIFTH_APPLY_CONFIRM_TOKEN, FIFTH_CONTRACT_SHA256)
        with self.assertRaises(ApplyError):
            _require_known_confirm("APPLY_NON_PERSON_FIFTH_CONTRACT_WRONG")
        with self.assertRaises(ApplyError):
            _require_confirm(FIFTH_APPLY_CONFIRM_TOKEN, FOURTH_CONTRACT_SHA256)


class FifthContractReviewedWorkbookTests(SimpleTestCase):
    """Read-only check of the reviewed workbook. It is not stored in git."""

    def setUp(self):
        super().setUp()
        if not _REVIEWED_WORKBOOK.is_file():
            self.skipTest(
                "fifth-contract workbook is not available in this environment: "
                f"{_REVIEWED_WORKBOOK}"
            )

    def test_reviewed_workbook_matches_the_approved_matrix(self):
        decisions = ReviewedNonPersonEntityDecision.Decision
        digest = sha256_path(_REVIEWED_WORKBOOK)
        self.assertEqual(digest, _REVIEWED_FIFTH_SHA)
        self.assertEqual(digest, FIFTH_CONTRACT_SHA256)

        result = preflight_authoritative_workbook(_REVIEWED_WORKBOOK)
        by_id = {row.candidate_id: row for row in result.rows}
        pending_ids = frozenset(row[0] for row in _PENDING_DECISIONS)
        carried_ids = frozenset(
            row.candidate_id
            for row in result.rows
            if row.candidate_id in _CARRIED_OWNER_IDS
        )
        workbook_pending_ids = frozenset(by_id) - _CARRIED_OWNER_IDS

        self.assertEqual(result.workbook_sha256, _REVIEWED_FIFTH_SHA)
        self.assertEqual(len(result.rows), 67)
        self.assertEqual(len(by_id), 67)
        self.assertEqual(carried_ids, _CARRIED_OWNER_IDS)
        self.assertEqual(carried_ids, frozenset(FIFTH_CARRIED_CANDIDATE_IDS))
        self.assertEqual(len(carried_ids), 25)
        self.assertEqual(workbook_pending_ids, pending_ids)
        self.assertEqual(len(pending_ids), 42)
        self.assertTrue(carried_ids.isdisjoint(pending_ids))
        self.assertTrue(_EXCLUDED.isdisjoint(by_id))
        self.assertNotIn("EC0057", by_id)
        self.assertEqual(result.aliases, ())
        self.assertEqual(result.split_routes, ())
        self.assertEqual(result.apply_blockers, ())
        self.assertTrue(
            _CONTEXTUAL_NAMES_NOT_ALIASES.isdisjoint(
                alias.alias_name for alias in result.aliases
            )
        )
        self.assertEqual(
            result.decision_counts()[decisions.MERGE],
            35,
        )
        self.assertEqual(
            result.decision_counts()[decisions.NEEDS_RESEARCH],
            2,
        )

        new_approve_ids = {
            row.candidate_id
            for row in result.rows
            if row.final_decision == decisions.APPROVE
            and row.candidate_id not in _CARRIED_OWNER_IDS
        }
        self.assertEqual(new_approve_ids, _NEW_APPROVE_IDS)
        merge_ids = {
            row.candidate_id
            for row in result.rows
            if row.final_decision == decisions.MERGE
        }
        self.assertEqual(len(merge_ids), 35)
        research_ids = {
            row.candidate_id
            for row in result.rows
            if row.final_decision == decisions.NEEDS_RESEARCH
        }
        self.assertEqual(research_ids, {"EC0814", "EC0822"})

        for candidate_id, decision, canonical, merge_target in _PENDING_DECISIONS:
            with self.subTest(candidate_id=candidate_id):
                row = by_id[candidate_id]
                self.assertEqual(row.final_decision, decision)
                self.assertEqual(row.final_canonical, canonical)
                self.assertEqual(row.merge_target, merge_target)
                if decision == decisions.MERGE:
                    target = by_id[merge_target]
                    self.assertEqual(target.final_decision, decisions.APPROVE)
                    self.assertEqual(row.final_canonical, target.final_canonical)

        self.assertEqual(by_id["EC0554"].final_canonical, "כלא אסתיאנאף בקהיר")
        self.assertEqual(by_id["EC0554"].final_decision, decisions.APPROVE)
        self.assertEqual(by_id["EC0554"].merge_target, "")
        self.assertEqual(by_id["EC0575"].final_canonical, "אוניברסיטת אלכסנדריה")
        self.assertEqual(by_id["EC0575"].final_decision, decisions.APPROVE)
        self.assertEqual(by_id["EC0579"].merge_target, "EC0575")
        self.assertEqual(
            by_id["EC0579"].final_canonical, by_id["EC0575"].final_canonical
        )
        self.assertEqual(by_id["EC0180"].merge_target, "EC0606")
        self.assertEqual(by_id["EC0605"].merge_target, "EC0606")
        self.assertEqual(by_id["EC0171"].merge_target, "EC0162")
        self.assertNotEqual(by_id["EC0171"].merge_target, "EC0057")
        self.assertEqual(by_id["EC0782"].merge_target, "EC0223")
        for candidate_id in ("EC0814", "EC0822"):
            with self.subTest(candidate_id=candidate_id):
                row = by_id[candidate_id]
                self.assertEqual(row.final_decision, decisions.NEEDS_RESEARCH)
                self.assertEqual(row.merge_target, "")
                self.assertNotIn("מצליח", row.final_canonical)
                self.assertNotIn("מצליח", row.surface)

        for candidate_id in carried_ids:
            with self.subTest(candidate_id=candidate_id):
                row = by_id[candidate_id]
                self.assertEqual(row.final_decision, decisions.APPROVE)
                self.assertEqual(row.merge_target, "")
