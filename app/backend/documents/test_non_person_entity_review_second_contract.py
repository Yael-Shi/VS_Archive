"""Second non-person contract support. The v6 contract stays fail-closed.

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
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_apply import (
    APPLY_CONFIRM_TOKEN,
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
    SECOND_CONTRACT_ALIAS_COUNT,
    SECOND_CONTRACT_DECISION_COUNTS,
    SECOND_CONTRACT_FINAL_SHEET_NAME,
    SECOND_CONTRACT_ROW_COUNT,
    SECOND_CONTRACT_SHA256,
    SECOND_CONTRACT_SPLIT_ROUTE_COUNTS,
    SECOND_DECISION_SOURCE,
    V6_DECISION_SOURCE,
    PreflightError,
    ReviewedAliasRow,
    contract_spec_for_sha,
    preflight_authoritative_workbook,
    second_contract_spec,
    sha256_path,
    validate_review_contract,
)
from documents.test_non_person_entity_review_apply import (
    _authoritative_workbook,
    _ready_contract,
)
from documents.test_non_person_entity_review_preflight import (
    _alias_cells,
    _cells_for,
    _header,
    _row,
    _write_workbook,
)

_REVIEWED_SECOND_SHA = (
    "f2e70a380b3fcd2995b17a558cd6a754f3638706a8501e9efc1e77974eddd2d1"
)
_FARUK_CANONICAL = "רחוב פארוק בקהיר"
_PROGRES_CANONICAL = "Le Progrès Égyptien"


def _second_rows():
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    merge = ReviewedNonPersonEntityDecision.Decision.MERGE
    skip = ReviewedNonPersonEntityDecision.Decision.SKIP
    place = NonPersonEntity.EntityType.PLACE
    city = NonPersonEntity.EntitySubtype.CITY
    rows = [
        _row(
            "EC0006",
            approve,
            surface="EC0006",
            final_canonical="canonical-EC0006",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0013",
            approve,
            surface="EC0013",
            final_canonical="canonical-EC0013",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0066",
            approve,
            surface="EC0066",
            final_canonical=_PROGRES_CANONICAL,
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
            entity_subtype=NonPersonEntity.EntitySubtype.NEWSPAPER,
        ),
        _row(
            "EC0223",
            approve,
            surface="EC0223",
            final_canonical=_FARUK_CANONICAL,
            entity_type=place,
            entity_subtype=NonPersonEntity.EntitySubtype.NEIGHBORHOOD,
        ),
        _row(
            "EC0389",
            approve,
            surface="EC0389",
            final_canonical="canonical-EC0389",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0443",
            approve,
            surface="EC0443",
            final_canonical="canonical-EC0443",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0465",
            approve,
            surface="EC0465",
            final_canonical="canonical-EC0465",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0042",
            approve,
            surface="EC0042",
            final_canonical="canonical-EC0042",
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
            entity_subtype=NonPersonEntity.EntitySubtype.INTERNATIONAL_ORGANIZATION,
        ),
        _row(
            "EC0099",
            approve,
            surface="EC0099",
            final_canonical="החלוץ",
            entity_type=place,
            entity_subtype=city,
        ),
        _row(
            "EC0358",
            approve,
            surface="EC0358",
            final_canonical="canonical-EC0358",
            entity_type=place,
            entity_subtype=city,
        ),
    ]
    rows.extend(
        _row(
            f"EC1{index:03d}",
            approve,
            surface=f"EC1{index:03d}",
            final_canonical=f"canonical-EC1{index:03d}",
            entity_type=place,
            entity_subtype=city,
        )
        for index in range(73)
    )
    rows.extend(
        [
            _row(
                "EC0535",
                merge,
                surface="Rue Farouk",
                final_canonical=_FARUK_CANONICAL,
                merge_target="EC0223",
            ),
            _row(
                "EC0067",
                merge,
                surface="EC0067",
                final_canonical=_PROGRES_CANONICAL,
                merge_target="EC0066",
            ),
            _row(
                "EC0401",
                merge,
                surface="EC0401",
                final_canonical="canonical-EC0358",
                merge_target="EC0358",
            ),
        ]
    )
    rows.extend(
        _row(
            f"EC2{index:03d}",
            merge,
            surface=f"EC2{index:03d}",
            final_canonical="canonical-EC0006",
            merge_target="EC0006",
        )
        for index in range(24)
    )
    rows.append(_row("EC0175", skip, surface="EC0175"))
    rows.extend(
        _row(f"EC3{index:03d}", skip, surface=f"EC3{index:03d}") for index in range(12)
    )
    return rows


def _write_second_workbook(path: Path, rows, aliases=()) -> None:
    _write_workbook(
        path,
        [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
        final_sheet_name=SECOND_CONTRACT_FINAL_SHEET_NAME,
        extra_sheets=[
            (
                FINAL_ALIAS_SHEET_NAME,
                [_header(ALIAS_COLUMNS), *map(_alias_cells, aliases)],
            )
        ],
    )


@contextmanager
def _recognized_second_workbook(rows=None, aliases=()):
    stored_rows = _second_rows() if rows is None else rows
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "second.xlsx"
        _write_second_workbook(path, stored_rows, aliases)
        digest = sha256_path(path)
        with patch.object(preflight_module, "SECOND_CONTRACT_SHA256", digest):
            yield path


def _second_preflight(path: Path):
    return preflight_authoritative_workbook(path)


def _seed_carried(preflight, *, skip: set[str] | None = None):
    entities = {}
    omitted = skip or set()
    for row in preflight.rows:
        if row.candidate_id not in SECOND_CARRIED_CANDIDATE_IDS:
            continue
        if row.candidate_id in omitted:
            continue
        entity = NonPersonEntity.objects.create(
            canonical_name=row.final_canonical,
            entity_type=row.entity_type,
            entity_subtype=row.entity_subtype,
        )
        ReviewedNonPersonEntityDecision.objects.create(
            source=V6_DECISION_SOURCE,
            candidate_id=row.candidate_id,
            workbook_sha256=AUTHORITATIVE_WORKBOOK_SHA256,
            decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
            review_status=row.status,
            final_canonical=row.final_canonical,
            display_name="",
            merge_target_candidate_id="",
            contextual_surfaces=row.surface,
            note=row.final_notes,
            result_entity=entity,
        )
        entities[row.candidate_id] = entity
    return entities


def _tracked_counts():
    return (
        NonPersonEntity.objects.count(),
        NonPersonEntityAlias.objects.count(),
        ReviewedNonPersonEntityDecision.objects.count(),
    )


class SecondContractPreflightTests(SimpleTestCase):
    def test_production_spec_keeps_the_reviewed_sha_and_counts(self):
        self.assertEqual(SECOND_CONTRACT_SHA256, _REVIEWED_SECOND_SHA)
        spec = contract_spec_for_sha(_REVIEWED_SECOND_SHA)
        assert spec is not None
        self.assertEqual(spec.row_count, 123)
        self.assertEqual(spec.row_count, SECOND_CONTRACT_ROW_COUNT)
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.APPROVE],
            83,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.MERGE],
            27,
        )
        self.assertEqual(
            spec.decision_counts[ReviewedNonPersonEntityDecision.Decision.SKIP],
            13,
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
        self.assertEqual(
            dict(spec.decision_counts), dict(SECOND_CONTRACT_DECISION_COUNTS)
        )
        self.assertEqual(spec.alias_count, 0)
        self.assertEqual(spec.alias_count, SECOND_CONTRACT_ALIAS_COUNT)
        self.assertEqual(dict(spec.split_route_counts), {})
        self.assertEqual(
            dict(spec.split_route_counts),
            dict(SECOND_CONTRACT_SPLIT_ROUTE_COUNTS),
        )
        self.assertFalse(spec.read_split_routes)
        self.assertEqual(spec.confirm_token, SECOND_APPLY_CONFIRM_TOKEN)
        self.assertEqual(spec.decision_source, SECOND_DECISION_SOURCE)
        self.assertEqual(spec.carried_candidate_ids, SECOND_CARRIED_CANDIDATE_IDS)
        self.assertEqual(spec.carried_decision_source, V6_DECISION_SOURCE)
        self.assertIsNone(contract_spec_for_sha("0" * 64))

    def test_synthetic_workbook_matches_second_contract_counts(self):
        with _recognized_second_workbook() as path:
            result = _second_preflight(path)

        self.assertEqual(len(result.rows), 123)
        self.assertEqual(
            result.decision_counts(),
            {
                "APPROVE": 83,
                "MERGE": 27,
                "SKIP": 13,
                "SPLIT": 0,
                "NEEDS_RESEARCH": 0,
            },
        )
        self.assertEqual(result.aliases, ())
        self.assertEqual(result.split_routes, ())
        self.assertEqual(
            set(result.carried_candidate_ids),
            set(SECOND_CARRIED_CANDIDATE_IDS),
        )
        by_id = {row.candidate_id: row for row in result.rows}
        self.assertEqual(by_id["EC0042"].entity_type, "ORGANIZATION")
        self.assertEqual(by_id["EC0042"].entity_subtype, "INTERNATIONAL_ORGANIZATION")
        self.assertEqual(by_id["EC0099"].final_decision, "APPROVE")
        self.assertEqual(by_id["EC0099"].final_canonical, "החלוץ")
        self.assertEqual(by_id["EC0535"].merge_target, "EC0223")
        self.assertEqual(by_id["EC0535"].final_canonical, _FARUK_CANONICAL)
        self.assertEqual(by_id["EC0535"].surface, "Rue Farouk")
        self.assertEqual(by_id["EC0067"].merge_target, "EC0066")
        self.assertEqual(by_id["EC0067"].final_canonical, _PROGRES_CANONICAL)
        self.assertNotIn("Rue Farouk", {alias.alias_name for alias in result.aliases})
        self.assertEqual(SECOND_CONTRACT_SHA256, _REVIEWED_SECOND_SHA)

    def test_unpatched_synthetic_workbook_is_unrecognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "second.xlsx"
            _write_second_workbook(path, _second_rows())
            with self.assertRaises(PreflightError) as caught:
                preflight_authoritative_workbook(path)
        self.assertIn("SHA256_UNRECOGNIZED", str(caught.exception))
        self.assertEqual(SECOND_CONTRACT_SHA256, _REVIEWED_SECOND_SHA)

    def test_split_row_fails(self):
        rows = []
        for row in _second_rows():
            if row.candidate_id == "EC0175":
                rows.append(
                    replace(
                        row,
                        final_decision=ReviewedNonPersonEntityDecision.Decision.SPLIT,
                    )
                )
            else:
                rows.append(row)

        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(rows, (), (), spec=second_contract_spec())

        message = str(caught.exception)
        self.assertIn("SPLIT_ROUTES_MISSING: EC0175", message)
        self.assertIn("UNEXPECTED_SPLIT_CANDIDATE: EC0175", message)

    def test_alias_row_fails(self):
        alias = ReviewedAliasRow(
            target_candidate_id="EC0099",
            target_canonical="החלוץ",
            alias_name="Rue Farouk",
            alias_kind="SPELLING_VARIANT",
            source_candidate_id="EC0535",
            review_status="APPROVED",
            basis="not reviewed",
        )

        with self.assertRaises(PreflightError) as caught:
            validate_review_contract(
                _second_rows(),
                (alias,),
                (),
                spec=second_contract_spec(),
            )

        self.assertIn("ALIAS_COUNT: expected 0 got 1", str(caught.exception))


class SecondContractApplyTests(TestCase):
    def test_carried_v6_decision_is_already_applied(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            _seed_carried(preflight)
            classified = classify_preflight(preflight)

        by_id = {item.candidate_id: item for item in classified.candidates}
        for candidate_id in SECOND_CARRIED_CANDIDATE_IDS:
            self.assertEqual(by_id[candidate_id].state, STATE_ALREADY_APPLIED)
            self.assertEqual(by_id[candidate_id].reasons, ())

    def test_missing_carried_v6_decision_is_blocked(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            _seed_carried(preflight, skip={"EC0006"})
            classified = classify_preflight(preflight)

        by_id = {item.candidate_id: item for item in classified.candidates}
        self.assertEqual(by_id["EC0006"].state, STATE_BLOCKED)
        self.assertIn(
            "carried target is not already applied",
            by_id["EC0006"].reasons,
        )
        self.assertEqual(by_id["EC0223"].state, STATE_ALREADY_APPLIED)

    def test_carried_id_on_second_source_is_state_drift(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            carried = _seed_carried(preflight)
            ReviewedNonPersonEntityDecision.objects.create(
                source=SECOND_DECISION_SOURCE,
                candidate_id="EC0006",
                workbook_sha256="0" * 64,
                decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
                review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
                final_canonical="canonical-EC0006",
                result_entity=carried["EC0006"],
            )
            classified = classify_preflight(preflight)

        item = next(
            row for row in classified.candidates if row.candidate_id == "EC0006"
        )
        self.assertEqual(item.state, STATE_DRIFT)
        self.assertIn("carried decision exists on contract source", item.reasons)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.get(
                source=V6_DECISION_SOURCE,
                candidate_id="EC0006",
            ).workbook_sha256,
            AUTHORITATIVE_WORKBOOK_SHA256,
        )

    def test_apply_refuses_when_carried_id_exists_on_contract_source(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            carried = _seed_carried(preflight)
            ReviewedNonPersonEntityDecision.objects.create(
                source=SECOND_DECISION_SOURCE,
                candidate_id="EC0223",
                workbook_sha256="0" * 64,
                decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
                review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
            )
            before = _tracked_counts()
            with self.assertRaises(ApplyError) as caught:
                apply_non_person_entity_review(
                    path,
                    confirm=SECOND_APPLY_CONFIRM_TOKEN,
                )

        self.assertIn(
            "carried decision exists on contract source", str(caught.exception)
        )
        self.assertIn("EC0223", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)
        stored = ReviewedNonPersonEntityDecision.objects.get(
            source=V6_DECISION_SOURCE,
            candidate_id="EC0223",
        )
        self.assertEqual(stored.result_entity_id, carried["EC0223"].pk)
        self.assertEqual(stored.workbook_sha256, AUTHORITATIVE_WORKBOOK_SHA256)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=SECOND_DECISION_SOURCE
            ).count(),
            1,
        )

    def test_new_rows_are_ready_when_only_carried_targets_exist(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            _seed_carried(preflight)
            classified = classify_preflight(preflight)

        ready = [
            item for item in classified.candidates if item.state == STATE_READY_TO_APPLY
        ]
        self.assertEqual(len(ready), 116)
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision
                == ReviewedNonPersonEntityDecision.Decision.APPROVE
            ),
            76,
        )
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision == ReviewedNonPersonEntityDecision.Decision.MERGE
            ),
            27,
        )
        self.assertEqual(
            sum(
                1
                for item in ready
                if item.plan.decision == ReviewedNonPersonEntityDecision.Decision.SKIP
            ),
            13,
        )

    def test_apply_resolves_merges_and_replay_is_a_noop(self):
        with _recognized_second_workbook() as path:
            preflight = _second_preflight(path)
            carried = _seed_carried(preflight)
            before_aliases = NonPersonEntityAlias.objects.count()

            result = apply_non_person_entity_review(
                path,
                confirm=SECOND_APPLY_CONFIRM_TOKEN,
            )

            self.assertEqual(result.entities_created, 76)
            self.assertEqual(result.aliases_created, 0)
            self.assertEqual(result.decisions_created, 116)
            self.assertEqual(result.occurrences_created, 0)
            self.assertEqual(result.after_counts[STATE_ALREADY_APPLIED], 123)
            self.assertEqual(NonPersonEntityAlias.objects.count(), before_aliases)
            self.assertFalse(
                NonPersonEntityAlias.objects.filter(name="Rue Farouk").exists()
            )
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=V6_DECISION_SOURCE
                ).count(),
                7,
            )
            for candidate_id, entity in carried.items():
                stored = ReviewedNonPersonEntityDecision.objects.get(
                    source=V6_DECISION_SOURCE,
                    candidate_id=candidate_id,
                )
                self.assertEqual(stored.result_entity_id, entity.pk)
                self.assertEqual(stored.workbook_sha256, AUTHORITATIVE_WORKBOOK_SHA256)
                self.assertEqual(stored.source, V6_DECISION_SOURCE)
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=SECOND_DECISION_SOURCE
                ).count(),
                116,
            )

            def decision(candidate_id: str) -> ReviewedNonPersonEntityDecision:
                return ReviewedNonPersonEntityDecision.objects.get(
                    source=SECOND_DECISION_SOURCE,
                    candidate_id=candidate_id,
                )

            self.assertEqual(decision("EC0535").result_entity_id, carried["EC0223"].pk)
            self.assertEqual(decision("EC0535").final_canonical, _FARUK_CANONICAL)
            self.assertEqual(decision("EC0067").result_entity_id, carried["EC0066"].pk)
            self.assertEqual(decision("EC0067").final_canonical, _PROGRES_CANONICAL)
            created = decision("EC0099").result_entity
            assert created is not None
            self.assertEqual(created.canonical_name, "החלוץ")
            self.assertNotIn(created.pk, {entity.pk for entity in carried.values()})
            league = decision("EC0042").result_entity
            assert league is not None
            self.assertEqual(league.entity_type, "ORGANIZATION")
            self.assertEqual(league.entity_subtype, "INTERNATIONAL_ORGANIZATION")
            same_contract = decision("EC0401")
            target = decision("EC0358")
            self.assertEqual(same_contract.result_entity_id, target.result_entity_id)
            self.assertNotIn(
                target.result_entity_id,
                {entity.pk for entity in carried.values()},
            )

            replay = apply_non_person_entity_review(
                path,
                confirm=SECOND_APPLY_CONFIRM_TOKEN,
            )
            self.assertEqual(replay.entities_created, 0)
            self.assertEqual(replay.aliases_created, 0)
            self.assertEqual(replay.decisions_created, 0)
            self.assertEqual(replay.occurrences_created, 0)
            self.assertEqual(
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=SECOND_DECISION_SOURCE
                ).count(),
                116,
            )

    def test_confirmation_tokens_do_not_cross_contracts(self):
        before = ReviewedNonPersonEntityDecision.objects.count()
        with _recognized_second_workbook() as path:
            with self.assertRaises(ApplyError) as second_with_v6:
                apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)
            self.assertIn(
                "confirmation token rejected",
                str(second_with_v6.exception),
            )
            with self.assertRaises(ApplyError) as wrong:
                apply_non_person_entity_review(
                    path,
                    confirm="APPLY_NON_PERSON_SECOND_CONTRACT_WRONG",
                )
            self.assertIn("confirmation token rejected", str(wrong.exception))

        v6 = _ready_contract()
        with _authoritative_workbook(v6) as path:
            with self.assertRaises(ApplyError) as v6_with_second:
                apply_non_person_entity_review(
                    path,
                    confirm=SECOND_APPLY_CONFIRM_TOKEN,
                )
        self.assertIn("confirmation token rejected", str(v6_with_second.exception))
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), before)
        self.assertEqual(SECOND_CONTRACT_SHA256, _REVIEWED_SECOND_SHA)
