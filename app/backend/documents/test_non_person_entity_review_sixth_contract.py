"""Sixth non-person contract. EC0026 and EC0499 stay outside it."""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from documents.models import ReviewedNonPersonEntityDecision
from documents.services import non_person_entity_review_dry_run as dry_run_module
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_occurrences import (
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_review_apply import (
    ApplyError,
    _require_confirm,
    _require_known_confirm,
)
from documents.services.non_person_entity_review_preflight import (
    AUTHORITATIVE_WORKBOOK_SHA256,
    SECOND_CONTRACT_SHA256,
    SECOND_DECISION_SOURCE,
    SIXTH_APPLY_CONFIRM_TOKEN,
    SIXTH_CARRIED_CANDIDATE_IDS,
    SIXTH_CONTRACT_ALIAS_COUNT,
    SIXTH_CONTRACT_DECISION_COUNTS,
    SIXTH_CONTRACT_FINAL_SHEET_NAME,
    SIXTH_CONTRACT_ROW_COUNT,
    SIXTH_CONTRACT_SHA256,
    SIXTH_CONTRACT_SPLIT_ROUTE_COUNTS,
    SIXTH_DECISION_SOURCE,
    V6_DECISION_SOURCE,
    PreflightError,
    contract_spec_for_sha,
    preflight_authoritative_workbook,
    sha256_path,
    sixth_contract_spec,
)

_REVIEWED_SIXTH_SHA = "d9136600842784f5fe5b09a1d35ce252150c0028cbc21ddee602139652f3231e"
_SUPERSEDED_SIXTH_SHA = (
    "b3331b16d89b7af76bd1b2a923a7b6e5bc408e8a5899c3d832ebdfa824ed8f51"
)
_SUPERSEDED_WORKBOOK = Path(
    "/mnt/c/Users/User/vs-archive-people-agent/outputs/"
    "non_person_registry_reconciliation/final_11/"
    "sixth_contract_superseded_"
    "b3331b16d89b7af76bd1b2a923a7b6e5bc408e8a5899c3d832ebdfa824ed8f51.xlsx"
)
_UNLINKED_ITEM_108 = frozenset(
    {
        ("EC0050", 108, "procès du Caire", 1),
        ("EC0050", 108, "procès du Caire", 2),
    }
)
_REVIEWED_WORKBOOK = Path(
    "/mnt/c/Users/User/vs-archive-people-agent/outputs/"
    "non_person_registry_reconciliation/final_11/sixth_contract.xlsx"
)
_EVIDENCE = Path(
    "/mnt/c/Users/User/vs-archive-people-agent/outputs/"
    "non_person_registry_reconciliation/final_11/"
    "final_11_occurrence_routes.json"
)
_INCLUDED_SPLITS = frozenset(SIXTH_CONTRACT_SPLIT_ROUTE_COUNTS)
_EXCLUDED = frozenset({"EC0026", "EC0499"})
_NEW_OWNERS = {
    "EC0844": ("חיל המודיעין", "ORGANIZATION", "INTELLIGENCE_ORGANIZATION"),
    "EC0845": ("אנגליה", "PLACE", "COUNTRY"),
    "EC0846": ("משפטי קהיר 1954–1955", "EVENT", "TRIAL_OR_LEGAL_PROCEEDING"),
    "EC0847": ("משפט קהיר 1961–1962", "EVENT", "TRIAL_OR_LEGAL_PROCEEDING"),
    "EC0848": ("הקהילה היהודית בקהיר", "COMMUNITY", "JEWISH_COMMUNITY"),
    "EC0849": ("בנק לאומי", "ORGANIZATION", "OTHER"),
    "EC0850": ("בנק לאומי\u05beצרפת", "ORGANIZATION", "OTHER"),
    "EC0851": (
        "הנהלת בני עקיבא במצרים",
        "ORGANIZATION",
        "COMMITTEE_OR_INTERNAL_BODY",
    ),
    "EC0852": (
        "הנהלת הצללים של בני עקיבא במצרים",
        "ORGANIZATION",
        "COMMITTEE_OR_INTERNAL_BODY",
    ),
}
_RENAMED_CANONICALS = frozenset({"EC0846", "EC0847", "EC0850"})
_CARRIED_CELLS = {
    "EC0004": (
        "Israel | Israël | ישראל | מדינת ישראל | اسرائيل",
        "ישראל",
        "Previously approved in spot-check; added to v3 as an explicit "
        "authoritative MERGE target.",
        "PLACE",
        "COUNTRY",
        V6_DECISION_SOURCE,
        AUTHORITATIVE_WORKBOOK_SHA256,
    ),
    "EC0006": (
        "ארץ ישראל",
        "ארץ ישראל",
        "Previously approved in spot-check; explicit SPLIT target for EC0009. "
        "Keep distinct from ישראל and פלסטינה.",
        "PLACE",
        "REGION_OR_HISTORICAL_AREA",
        V6_DECISION_SOURCE,
        AUTHORITATIVE_WORKBOOK_SHA256,
    ),
    "EC0219": (
        "פלסטינה",
        "פלסטינה",
        "Representation normalized for v3 apply contract; semantic "
        "KEEP_DISTINCT decision preserved. Historical/territorial PLACE, "
        "distinct from ארץ ישראל and ישראל; explicit SPLIT target for EC0009.",
        "PLACE",
        "REGION_OR_HISTORICAL_AREA",
        V6_DECISION_SOURCE,
        AUTHORITATIVE_WORKBOOK_SHA256,
    ),
    "EC0301": (
        "בריטניה",
        "בריטניה",
        "אושר בסבב ה-spot-check. | ambiguity: Distinct from geographic England "
        "(אנגליה). | related_to אנגליה (component / often conflated "
        "colloquially); CONTEXTUAL_HISTORICAL_USAGE of אנגליה→UK is noted on "
        "EC0084, not as alias.",
        "PLACE",
        "COUNTRY",
        SECOND_DECISION_SOURCE,
        SECOND_CONTRACT_SHA256,
    ),
    "EC0405": (
        'המודיעין בצה"ל',
        "אגף המודיעין (אמ״ן)",
        "1954 IDF intelligence context.",
        "ORGANIZATION",
        "INTELLIGENCE_ORGANIZATION",
        V6_DECISION_SOURCE,
        AUTHORITATIVE_WORKBOOK_SHA256,
    ),
    "EC0059": (
        "Jewish community of Egypt | Jews of Egypt | judaïsme égyptien | "
        "juifs d'Egypte | האוכלוסיה היהודית במצרים | היהדות המצרים | "
        "הישוב היהודי במצרים | הקהילה היהודית במצרים | "
        "הקהילות היהודיות במצרים | יהדות מצרים | יהודי מצרים | "
        "יהודים ממצרים | קהילת יהודי מצרים | קהילת מצרים",
        "יהדות מצרים",
        "COMMUNITY אחד: יהדות מצרים / יהודי מצרים / הקהילה היהודית במצרים "
        "וצורות מקבילות מתייחסות לאותה ישות. | ambiguity: יהדות מצרים מול "
        "יהודי מצרים — אפשרי KEEP_DISTINCT סמנטי.",
        "COMMUNITY",
        "JEWISH_COMMUNITY",
        SECOND_DECISION_SOURCE,
        SECOND_CONTRACT_SHA256,
    ),
    "EC0063": (
        "communauté d'Alexandrie | juifs d'Alexandrie | העדה היהודית באלכסנדריה",
        "הקהילה היהודית באלכסנדריה",
        "communauté d'Alexandrie / juifs d'Alexandrie / "
        "העדה היהודית באלכסנדריה אותה COMMUNITY; "
        "juifs d'Alexandrie contextual alternate.",
        "COMMUNITY",
        "JEWISH_COMMUNITY",
        SECOND_DECISION_SOURCE,
        SECOND_CONTRACT_SHA256,
    ),
}
_SPLIT_TARGETS = {
    "EC0033": frozenset({"EC0851", "EC0852"}),
    "EC0036": frozenset({"EC0405", "EC0844"}),
    "EC0050": frozenset({"EC0846"}),
    "EC0062": frozenset({"EC0059", "EC0063", "EC0848"}),
    "EC0084": frozenset({"EC0301", "EC0845"}),
    "EC0432": frozenset({"EC0004", "EC0006", "EC0219"}),
    "EC0600": frozenset({"EC0849", "EC0850"}),
}


def _route_sheet_bytes(path: Path) -> tuple[str, bytes]:
    with zipfile.ZipFile(path) as workbook:
        for name in workbook.namelist():
            if not name.startswith("xl/worksheets/"):
                continue
            payload = workbook.read(name)
            if b"split_candidate_id" in payload:
                return name, payload
    raise AssertionError("split route sheet missing")


def _preflight_mutated(path: Path):
    digest = sha256_path(path)
    with patch.object(preflight_module, "SIXTH_CONTRACT_SHA256", digest):
        return preflight_authoritative_workbook(path)


def _write_replaced_route_sheet(source: Path, dest: Path, payload: bytes) -> None:
    sheet_name, _original = _route_sheet_bytes(source)
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(dest, "w") as outgoing:
        for info in incoming.infolist():
            data = payload if info.filename == sheet_name else incoming.read(info)
            outgoing.writestr(info, data)


class SixthContractPreflightTests(SimpleTestCase):
    def test_production_spec_keeps_the_reviewed_sha_and_counts(self):
        self.assertEqual(SIXTH_CONTRACT_SHA256, _REVIEWED_SIXTH_SHA)
        spec = contract_spec_for_sha(_REVIEWED_SIXTH_SHA)
        assert spec is not None
        self.assertEqual(spec, sixth_contract_spec())
        self.assertEqual(spec.final_sheet_name, SIXTH_CONTRACT_FINAL_SHEET_NAME)
        self.assertEqual(spec.row_count, SIXTH_CONTRACT_ROW_COUNT)
        self.assertEqual(spec.row_count, 25)
        self.assertEqual(
            dict(spec.decision_counts),
            dict(SIXTH_CONTRACT_DECISION_COUNTS),
        )
        decisions = ReviewedNonPersonEntityDecision.Decision
        self.assertEqual(spec.decision_counts[decisions.APPROVE], 16)
        self.assertEqual(spec.decision_counts[decisions.MERGE], 2)
        self.assertEqual(spec.decision_counts[decisions.SPLIT], 7)
        self.assertEqual(spec.decision_counts[decisions.SKIP], 0)
        self.assertEqual(spec.decision_counts[decisions.NEEDS_RESEARCH], 0)
        self.assertEqual(spec.alias_count, SIXTH_CONTRACT_ALIAS_COUNT)
        self.assertEqual(spec.alias_count, 0)
        self.assertEqual(
            dict(spec.split_route_counts),
            {
                "EC0033": 48,
                "EC0036": 182,
                "EC0050": 8,
                "EC0062": 16,
                "EC0084": 28,
                "EC0432": 18,
                "EC0600": 3,
            },
        )
        self.assertEqual(sum(spec.split_route_counts.values()), 303)
        self.assertTrue(spec.read_split_routes)
        self.assertFalse(spec.read_alias_review)
        self.assertEqual(spec.confirm_token, SIXTH_APPLY_CONFIRM_TOKEN)
        self.assertEqual(spec.decision_source, SIXTH_DECISION_SOURCE)
        self.assertEqual(spec.carried_candidate_ids, SIXTH_CARRIED_CANDIDATE_IDS)
        self.assertEqual(
            {target.candidate_id for target in spec.carried_targets},
            set(spec.carried_candidate_ids),
        )
        self.assertEqual(spec.carried_decision_source, "")
        self.assertTrue(_EXCLUDED.isdisjoint(spec.carried_candidate_ids))

    def test_confirmation_token_is_separate_from_earlier_contracts(self):
        _require_known_confirm(SIXTH_APPLY_CONFIRM_TOKEN)
        _require_confirm(SIXTH_APPLY_CONFIRM_TOKEN, SIXTH_CONTRACT_SHA256)
        with self.assertRaises(ApplyError):
            _require_known_confirm("APPLY_NON_PERSON_SIXTH_CONTRACT_WRONG")
        with self.assertRaises(ApplyError):
            _require_confirm(SIXTH_APPLY_CONFIRM_TOKEN, SECOND_CONTRACT_SHA256)

    def test_reviewed_workbook_matches_verified_routes_and_owners(self):
        if not _REVIEWED_WORKBOOK.is_file() or not _EVIDENCE.is_file():
            self.skipTest("sixth-contract workbook is not available")

        decisions = ReviewedNonPersonEntityDecision.Decision
        digest = sha256_path(_REVIEWED_WORKBOOK)
        self.assertEqual(digest, _REVIEWED_SIXTH_SHA)
        result = preflight_authoritative_workbook(_REVIEWED_WORKBOOK)
        by_id = {row.candidate_id: row for row in result.rows}
        evidence = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
        verified = [
            route
            for route in evidence["routes"]
            if route["split_candidate_id"] in _INCLUDED_SPLITS
        ]

        self.assertEqual(result.workbook_sha256, _REVIEWED_SIXTH_SHA)
        self.assertEqual(result.final_sheet_name, "FINAL_RECON_2026-10-15")
        self.assertEqual(len(result.rows), 25)
        self.assertEqual(result.apply_blockers, ())
        self.assertEqual(result.aliases, ())
        self.assertEqual(len(result.split_routes), 303)
        self.assertEqual(len(verified), 305)
        self.assertTrue(_EXCLUDED.isdisjoint(by_id))
        self.assertNotIn("EC0192", by_id)
        self.assertNotIn("EC0193", by_id)
        self.assertNotIn("EC0414", by_id)

        included = {
            row.candidate_id
            for row in result.rows
            if row.final_decision in {decisions.SPLIT, decisions.MERGE}
        }
        self.assertEqual(
            included,
            _INCLUDED_SPLITS | {"EC0633", "EC0743"},
        )
        for candidate_id, expected in SIXTH_CONTRACT_SPLIT_ROUTE_COUNTS.items():
            actual = sum(
                1
                for route in result.split_routes
                if route.split_candidate_id == candidate_id
            )
            self.assertEqual(actual, expected)

        workbook_pins = {
            (
                route.split_candidate_id,
                int(route.archive_item_id),
                route.surface,
                int(route.occurrence_ordinal),
                route.source_text_sha256,
                route.text_kind,
                route.target_candidate_id,
            )
            for route in result.split_routes
        }
        evidence_pins = {
            (
                route["split_candidate_id"],
                route["archive_item_id"],
                route["surface"],
                route["occurrence_ordinal"],
                route["source_text_sha256"],
                route["text_kind"],
                route["target_candidate_id"],
            )
            for route in verified
        }
        unlinked = {pin for pin in evidence_pins if pin[:4] in _UNLINKED_ITEM_108}
        self.assertEqual({pin[:4] for pin in unlinked}, set(_UNLINKED_ITEM_108))
        self.assertEqual(workbook_pins, evidence_pins - unlinked)
        self.assertEqual(
            [
                route
                for route in result.split_routes
                if route.target_candidate_id == "EC0847"
            ],
            [],
        )
        self.assertEqual(
            by_id["EC0847"].final_canonical,
            "משפט קהיר 1961–1962",
        )
        for route in result.split_routes:
            target = by_id[route.target_candidate_id]
            self.assertEqual(target.final_decision, decisions.APPROVE)
            self.assertEqual(route.target_canonical, target.final_canonical)
            self.assertNotIn(route.target_candidate_id, _INCLUDED_SPLITS)
        for route in verified:
            if route["target_candidate_id"] in _RENAMED_CANONICALS:
                self.assertNotEqual(
                    route["target_canonical"],
                    by_id[route["target_candidate_id"]].final_canonical,
                )
            else:
                self.assertEqual(
                    route["target_canonical"],
                    by_id[route["target_candidate_id"]].final_canonical,
                )

        owners = {target.candidate_id: target for target in result.carried_targets}
        self.assertEqual(set(owners), set(_CARRIED_CELLS))
        canonicals = []
        for candidate_id, cells in _CARRIED_CELLS.items():
            row = by_id[candidate_id]
            (
                surface,
                canonical,
                notes,
                entity_type,
                entity_subtype,
                source,
                workbook_sha,
            ) = cells
            self.assertEqual(row.final_decision, decisions.APPROVE)
            self.assertEqual(row.surface, surface)
            self.assertEqual(row.final_canonical, canonical)
            self.assertEqual(row.final_notes, notes)
            self.assertEqual(row.entity_type, entity_type)
            self.assertEqual(row.entity_subtype, entity_subtype)
            self.assertEqual(row.merge_target, "")
            self.assertEqual(row.status, "CLOSED")
            self.assertEqual(owners[candidate_id].decision_source, source)
            self.assertEqual(owners[candidate_id].workbook_sha256, workbook_sha)
            self.assertNotEqual(source, SIXTH_DECISION_SOURCE)
            canonicals.append(canonical)

        for candidate_id, (
            canonical,
            entity_type,
            entity_subtype,
        ) in _NEW_OWNERS.items():
            row = by_id[candidate_id]
            self.assertEqual(row.final_decision, decisions.APPROVE)
            self.assertEqual(row.final_canonical, canonical)
            self.assertEqual(row.surface, canonical)
            self.assertEqual(row.entity_type, entity_type)
            self.assertEqual(row.entity_subtype, entity_subtype)
            self.assertEqual(row.merge_target, "")
            self.assertNotIn(canonical, canonicals)
            canonicals.append(canonical)
        self.assertEqual(len(canonicals), len(set(canonicals)))
        self.assertNotIn("אנגליהם", canonicals)

        for candidate_id, targets in _SPLIT_TARGETS.items():
            routed = {
                route.target_candidate_id
                for route in result.split_routes
                if route.split_candidate_id == candidate_id
            }
            self.assertEqual(routed, targets)
            row = by_id[candidate_id]
            self.assertEqual(row.final_decision, decisions.SPLIT)
            self.assertEqual(row.entity_type, "")
            self.assertEqual(row.entity_subtype, "")
            self.assertEqual(row.merge_target, "")

        aman = by_id["EC0405"]
        ec0633 = by_id["EC0633"]
        self.assertEqual(ec0633.final_decision, decisions.MERGE)
        self.assertEqual(ec0633.merge_target, "EC0405")
        self.assertEqual(ec0633.final_canonical, aman.final_canonical)
        self.assertEqual(ec0633.surface, "מחלקת המודיעין של צהל")
        self.assertEqual(ec0633.entity_type, "")

        england = by_id["EC0845"]
        ec0743 = by_id["EC0743"]
        self.assertEqual(ec0743.final_decision, decisions.MERGE)
        self.assertEqual(ec0743.merge_target, "EC0845")
        self.assertEqual(ec0743.final_canonical, england.final_canonical)
        self.assertEqual(ec0743.final_canonical, "אנגליה")
        self.assertEqual(ec0743.surface, "אנגליהם")
        self.assertNotEqual(ec0743.merge_target, "EC0301")
        self.assertNotIn("אנגליהם", {alias.alias_name for alias in result.aliases})

    def test_real_workbook_rejects_a_dropped_route(self):
        if not _REVIEWED_WORKBOOK.is_file():
            self.skipTest("sixth-contract workbook is not available")
        _name, payload = _route_sheet_bytes(_REVIEWED_WORKBOOK)
        start = payload.index(b'<row r="2">')
        end = payload.index(b"</row>", start) + len(b"</row>")
        mutated = payload[:start] + payload[end:]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dropped.xlsx"
            _write_replaced_route_sheet(_REVIEWED_WORKBOOK, path, mutated)
            with self.assertRaises(PreflightError) as unrecognized:
                preflight_authoritative_workbook(path)
            with self.assertRaises(PreflightError) as caught:
                _preflight_mutated(path)
        self.assertIn("SHA256_UNRECOGNIZED", str(unrecognized.exception))
        message = str(caught.exception)
        self.assertIn("SPLIT_ROUTE_COUNT", message)
        self.assertIn("SPLIT_CANDIDATE_ROUTE_COUNT", message)

    def test_real_workbook_rejects_a_malformed_route_hash(self):
        if not _REVIEWED_WORKBOOK.is_file():
            self.skipTest("sixth-contract workbook is not available")
        _name, payload = _route_sheet_bytes(_REVIEWED_WORKBOOK)
        match = re.search(rb"[0-9a-f]{64}", payload)
        assert match is not None
        mutated = payload.replace(match.group(0), b"not-a-hash", 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-hash.xlsx"
            _write_replaced_route_sheet(_REVIEWED_WORKBOOK, path, mutated)
            with self.assertRaises(PreflightError) as caught:
                _preflight_mutated(path)
        self.assertIn("MALFORMED_SOURCE_TEXT_SHA256", str(caught.exception))

    def test_real_workbook_rejects_a_changed_route_canonical(self):
        if not _REVIEWED_WORKBOOK.is_file():
            self.skipTest("sixth-contract workbook is not available")
        _name, payload = _route_sheet_bytes(_REVIEWED_WORKBOOK)
        mutated = payload.replace(
            "משפטי קהיר 1954–1955".encode(),
            "משפטי קהיר אחרים".encode(),
            1,
        )
        self.assertNotEqual(mutated, payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad-canonical.xlsx"
            _write_replaced_route_sheet(_REVIEWED_WORKBOOK, path, mutated)
            with self.assertRaises(PreflightError) as caught:
                _preflight_mutated(path)
        self.assertIn("ROUTE_CANONICAL_MISMATCH", str(caught.exception))

    def test_real_workbook_rejects_an_evidence_hash_that_was_replaced(self):
        if not _REVIEWED_WORKBOOK.is_file() or not _EVIDENCE.is_file():
            self.skipTest("sixth-contract workbook is not available")
        evidence = json.loads(_EVIDENCE.read_text(encoding="utf-8"))
        original = next(
            route["source_text_sha256"]
            for route in evidence["routes"]
            if route["split_candidate_id"] == "EC0033"
        )
        replacement = "0" * 64
        _name, payload = _route_sheet_bytes(_REVIEWED_WORKBOOK)
        mutated = payload.replace(original.encode(), replacement.encode(), 1)
        self.assertNotEqual(mutated, payload)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "altered-hash.xlsx"
            _write_replaced_route_sheet(_REVIEWED_WORKBOOK, path, mutated)
            with self.assertRaises(PreflightError) as unrecognized:
                preflight_authoritative_workbook(path)
            result = _preflight_mutated(path)
        self.assertIn("SHA256_UNRECOGNIZED", str(unrecognized.exception))
        altered = {
            route.source_text_sha256
            for route in result.split_routes
            if route.split_candidate_id == "EC0033"
        }
        verified = {
            route["source_text_sha256"]
            for route in evidence["routes"]
            if route["split_candidate_id"] == "EC0033"
        }
        self.assertNotEqual(altered, verified)
        self.assertIn(replacement, altered)
        self.assertNotIn(replacement, verified)

    def test_superseded_workbook_is_not_the_registered_contract(self):
        if not _SUPERSEDED_WORKBOOK.is_file():
            self.skipTest("superseded sixth-contract workbook is not available")
        self.assertEqual(sha256_path(_SUPERSEDED_WORKBOOK), _SUPERSEDED_SIXTH_SHA)
        self.assertIsNone(contract_spec_for_sha(_SUPERSEDED_SIXTH_SHA))
        with self.assertRaises(PreflightError) as caught:
            preflight_authoritative_workbook(_SUPERSEDED_WORKBOOK)
        self.assertIn("SHA256_UNRECOGNIZED", str(caught.exception))

    def test_partial_item_108_surface_group_is_blocked(self):
        text = (
            "Il s'agit du fameux procès du Caire intenté en 1961/1962. "
            '"Le procès du Caire" est publié en 1963.'
        )
        digest = source_text_sha256(text)
        normalized = normalize_surface_v1("procès du Caire")
        occurrence = dry_run_module.OccurrencePlan(
            archive_item_id=108,
            text_kind="OCR_TRANSCRIPTION",
            source_text_sha256=digest,
            normalization_version="surface-v1",
            normalized_surface=normalized,
            occurrence_ordinal=1,
            resolution_status="RESOLVED",
            target_candidate_id="EC0847",
            matched_text="",
        )
        with (
            patch.object(
                dry_run_module,
                "item_supports_occurrence_text_kind",
                return_value=True,
            ),
            patch.object(
                dry_run_module,
                "authoritative_displayed_text",
                return_value=text,
            ),
        ):
            drift, blocked, located = dry_run_module._verify_surface_group(
                (108, "OCR_TRANSCRIPTION", digest, "surface-v1", normalized),
                [("EC0050", occurrence)],
                {108: object()},
            )
        self.assertEqual(drift, [])
        assert located is not None
        self.assertEqual(located.count, 2)
        self.assertTrue(
            any("extra unreviewed surface occurrences" in reason for reason in blocked)
        )
