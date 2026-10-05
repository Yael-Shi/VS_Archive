"""Transactional apply tests for the v6 non-person entity review contract."""

from __future__ import annotations

import inspect
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection
from django.test import TestCase

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    Author,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
    ReviewedNonPersonEntityDecision,
    Tag,
)
from documents.services import non_person_entity_review_apply as apply_module
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_apply import (
    APPLY_CONFIRM_TOKEN,
    ApplyError,
    apply_non_person_entity_review,
    format_apply_report,
)
from documents.services.non_person_entity_review_dry_run import (
    DECISION_SOURCE,
    STATE_ALREADY_APPLIED,
    STATE_DRIFT,
    SurfaceLocation,
    classify_preflight,
)
from documents.services.non_person_entity_review_preflight import (
    PreflightError,
    preflight_authoritative_workbook,
    sha256_path,
)
from documents.test_non_person_entity_review_dry_run import (
    _bodies,
    _candidate,
    _decision_from_plan,
    _install_bodies,
    _materialize_approve,
    _preflight,
    _same_canonical,
)
from documents.test_non_person_entity_review_preflight import _write_valid_workbook


def _tracked_counts() -> dict[str, int]:
    return {
        "entity": NonPersonEntity.objects.count(),
        "alias": NonPersonEntityAlias.objects.count(),
        "decision": ReviewedNonPersonEntityDecision.objects.count(),
        "occurrence": ArchiveItemEntityOccurrence.objects.count(),
        "person": Person.objects.count(),
        "author": Author.objects.count(),
        "tag": Tag.objects.count(),
        "category": ArchiveCategory.objects.count(),
        "event": ArchiveEvent.objects.count(),
        "search": ArchiveItemSearchIndex.objects.count(),
    }


def _ready_contract():
    bodies = _bodies()
    _install_bodies(bodies)
    return _preflight(texts=bodies)


@contextmanager
def _authoritative_workbook(preflight):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "workbook.xlsx"
        _write_valid_workbook(
            path,
            list(preflight.rows),
            list(preflight.aliases),
            list(preflight.split_routes),
        )
        digest = sha256_path(path)
        with patch.object(preflight_module, "AUTHORITATIVE_WORKBOOK_SHA256", digest):
            yield path


def _apply(preflight):
    with _authoritative_workbook(preflight) as path:
        return apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)


class NonPersonEntityReviewApplyTests(TestCase):
    def test_wrong_confirmation_token_writes_nothing(self):
        preflight = _ready_contract()
        before = _tracked_counts()

        with (
            _authoritative_workbook(preflight) as path,
            self.assertRaises(ApplyError) as caught,
        ):
            apply_non_person_entity_review(path, confirm="nope")

        self.assertIn("confirmation token rejected", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)
        with self.assertRaises(CommandError) as command_caught:
            call_command(
                "non_person_entity_review_apply",
                workbook="/tmp/does-not-matter.xlsx",
                confirm="wrong",
            )
        self.assertIn("confirmation token rejected", str(command_caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_preflight_failure_writes_nothing(self):
        before = _tracked_counts()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.xlsx"
            path.write_bytes(b"not a workbook")
            with self.assertRaises(PreflightError):
                apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)
        self.assertEqual(_tracked_counts(), before)

    def test_state_drift_before_apply_writes_nothing(self):
        rows = _same_canonical({"EC0101", "EC0106"}, "shared-canonical")
        preflight = _preflight(rows)
        first = classify_preflight(preflight)
        entity, _decision = _materialize_approve(first, "EC0101")
        _decision_from_plan(_candidate(first, "EC0106").plan, result_entity=entity)
        before = _tracked_counts()

        with self.assertRaises(ApplyError) as caught:
            _apply(preflight)

        self.assertIn("STATE_DRIFT", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_blocked_before_apply_writes_nothing(self):
        before = _tracked_counts()

        with self.assertRaises(ApplyError) as caught:
            _apply(_preflight())

        self.assertIn("BLOCKED", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_successful_apply_creates_expected_rows_and_replays_as_noop(self):
        preflight = _ready_contract()
        unrelated = _tracked_counts()
        first_plan = classify_preflight(preflight)
        workbook = _authoritative_workbook(preflight)
        path = workbook.__enter__()
        self.addCleanup(workbook.__exit__, None, None, None)

        result = apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)

        self.assertEqual(result.entities_created, 70)
        self.assertEqual(result.aliases_created, 63)
        self.assertEqual(result.decisions_created, 109)
        self.assertEqual(result.occurrences_created, 6)
        self.assertEqual(result.after_counts[STATE_ALREADY_APPLIED], 109)
        self.assertEqual(result.after_counts["READY_TO_APPLY"], 0)
        self.assertEqual(NonPersonEntity.objects.count(), unrelated["entity"] + 70)
        self.assertEqual(NonPersonEntityAlias.objects.count(), unrelated["alias"] + 63)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=DECISION_SOURCE
            ).count(),
            109,
        )
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 6)
        self.assertEqual(Person.objects.count(), unrelated["person"])
        self.assertEqual(Author.objects.count(), unrelated["author"])
        self.assertEqual(Tag.objects.count(), unrelated["tag"])
        self.assertEqual(ArchiveCategory.objects.count(), unrelated["category"])
        self.assertEqual(ArchiveEvent.objects.count(), unrelated["event"])
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), unrelated["search"])
        report = format_apply_report(result)
        self.assertIn("transaction: committed", report)
        self.assertIn("confirmation: accepted", report)

        after = classify_preflight(preflight_authoritative_workbook(path))
        self.assertEqual(after.state_counts()[STATE_ALREADY_APPLIED], 109)
        approve = _candidate(first_plan, "EC0100").plan
        entity = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC0100",
        ).result_entity
        assert approve.entity is not None
        assert entity is not None
        self.assertEqual(entity.canonical_name, approve.entity.canonical_name)
        self.assertEqual(entity.display_name, "")
        self.assertEqual(entity.entity_type, approve.entity.entity_type)
        self.assertEqual(entity.entity_subtype, approve.entity.entity_subtype)
        stored_aliases = set(
            NonPersonEntityAlias.objects.filter(
                entity__review_decisions__candidate_id="EC0006",
                entity__review_decisions__source=DECISION_SOURCE,
            ).values_list("name", "kind")
        )
        self.assertEqual(
            stored_aliases,
            {
                (alias.alias_name, alias.alias_kind)
                for alias in _candidate(first_plan, "EC0006").plan.aliases
            },
        )
        self.assertEqual(len(stored_aliases), 63)
        approve_entity_ids = set(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=DECISION_SOURCE,
                decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
            ).values_list("result_entity_id", flat=True)
        )
        self.assertEqual(len(approve_entity_ids), 70)
        merge = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC2000",
        )
        target = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC0006",
        )
        self.assertEqual(merge.result_entity_id, target.result_entity_id)
        self.assertEqual(
            NonPersonEntity.objects.filter(pk=merge.result_entity_id).count(),
            1,
        )
        skip = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC3000",
        )
        self.assertIsNone(skip.result_entity_id)
        self.assertEqual(skip.decision, ReviewedNonPersonEntityDecision.Decision.SKIP)
        research = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC0025",
        )
        self.assertIsNone(research.result_entity_id)
        self.assertEqual(
            research.review_status,
            ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED,
        )
        for candidate_id in ("EC0009", "EC0045"):
            split = ReviewedNonPersonEntityDecision.objects.get(
                source=DECISION_SOURCE,
                candidate_id=candidate_id,
            )
            self.assertIsNone(split.result_entity_id)
        occurrences = list(
            ArchiveItemEntityOccurrence.objects.order_by(
                "archive_item_id",
                "occurrence_ordinal",
            )
        )
        planned = {
            (
                item.archive_item_id,
                item.occurrence_ordinal,
                item.target_candidate_id,
                item.matched_text,
            )
            for candidate in first_plan.candidates
            for item in candidate.plan.occurrences
        }
        self.assertEqual(
            {
                (
                    row.archive_item_id,
                    row.occurrence_ordinal,
                    row.entity.review_decisions.get(
                        source=DECISION_SOURCE,
                        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
                    ).candidate_id,
                    row.matched_text,
                )
                for row in occurrences
            },
            planned,
        )
        alias_names = set(NonPersonEntityAlias.objects.values_list("name", flat=True))
        self.assertNotIn("Palestine", alias_names)
        self.assertNotIn("ministère de la propagande", alias_names)

        stamps = {
            entity.pk: entity.updated_at for entity in NonPersonEntity.objects.all()
        }
        decision_ids = set(
            ReviewedNonPersonEntityDecision.objects.values_list("pk", flat=True)
        )
        replay = apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)
        self.assertEqual(replay.entities_created, 0)
        self.assertEqual(replay.aliases_created, 0)
        self.assertEqual(replay.decisions_created, 0)
        self.assertEqual(replay.occurrences_created, 0)
        self.assertEqual(
            {entity.pk: entity.updated_at for entity in NonPersonEntity.objects.all()},
            stamps,
        )
        self.assertEqual(
            set(ReviewedNonPersonEntityDecision.objects.values_list("pk", flat=True)),
            decision_ids,
        )
        self.assertEqual(_tracked_counts()["entity"], unrelated["entity"] + 70)

    def test_apply_does_not_reuse_same_canonical_name_entity(self):
        preflight = _ready_contract()
        preexisting = NonPersonEntity.objects.create(
            canonical_name="canonical-EC0100",
            display_name="",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )

        _apply(preflight)

        decision = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC0100",
        )
        self.assertNotEqual(decision.result_entity_id, preexisting.pk)
        self.assertEqual(
            NonPersonEntity.objects.filter(canonical_name="canonical-EC0100").count(),
            2,
        )

    def test_source_hash_drift_inside_transaction_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_create = NonPersonEntity.objects.create
        changed = {"done": False}

        def create_and_drift(**kwargs):
            entity = real_create(**kwargs)
            if not changed["done"]:
                changed["done"] = True
                ManualTextContent.objects.filter(archive_item_id=285).update(
                    body="drifted source"
                )
            return entity

        with (
            patch.object(
                apply_module.NonPersonEntity.objects,
                "create",
                side_effect=create_and_drift,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("source text sha256 mismatch", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_surface_count_drift_inside_transaction_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_locate = apply_module.locate_surface_occurrences

        def extra_occurrence(text, surface):
            located = real_locate(text, surface)
            if surface == "palestine":
                return SurfaceLocation(count=located.count + 1, occurrences=None)
            return located

        with (
            patch.object(
                apply_module,
                "locate_surface_occurrences",
                side_effect=extra_occurrence,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("surface occurrence count drift", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_missing_target_inside_transaction_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_create = ReviewedNonPersonEntityDecision.objects.create

        def create_then_drop_target(**kwargs):
            row = real_create(**kwargs)
            if kwargs.get("candidate_id") == "EC0100":
                ReviewedNonPersonEntityDecision.objects.filter(
                    source=DECISION_SOURCE,
                    candidate_id="EC0006",
                ).delete()
            return row

        with (
            patch.object(
                apply_module.ReviewedNonPersonEntityDecision.objects,
                "create",
                side_effect=create_then_drop_target,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("target missing: EC0006", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_alias_conflict_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_create = NonPersonEntityAlias.objects.create

        def create_twice(**kwargs):
            row = real_create(**kwargs)
            if kwargs.get("name") == "alias-00":
                real_create(**kwargs)
            return row

        with (
            patch.object(
                apply_module.NonPersonEntityAlias.objects,
                "create",
                side_effect=create_twice,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("alias conflict", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_occurrence_unique_collision_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_create = ArchiveItemEntityOccurrence.objects.create

        def create_twice(**kwargs):
            row = real_create(**kwargs)
            if kwargs.get("archive_item_id") == 285:
                real_create(**kwargs)
            return row

        with (
            patch.object(
                apply_module.ArchiveItemEntityOccurrence.objects,
                "create",
                side_effect=create_twice,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("occurrence unique collision", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_final_verification_failure_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        real_classify = apply_module.classify_preflight
        calls = {"n": 0}

        def classify_then_fail(preflight_result):
            calls["n"] += 1
            result = real_classify(preflight_result)
            if calls["n"] < 3:
                return result
            bad = replace(
                result.candidates[0],
                state=STATE_DRIFT,
                reasons=("forced final mismatch",),
            )
            return replace(result, candidates=(bad, *result.candidates[1:]))

        with (
            patch.object(
                apply_module,
                "classify_preflight",
                side_effect=classify_then_fail,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("final verification failed", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)

    def test_reversed_workbook_row_order_applies_the_same_contract(self):
        preflight = _ready_contract()
        reversed_preflight = replace(
            preflight,
            rows=tuple(reversed(preflight.rows)),
        )

        _apply(reversed_preflight)

        merge = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC2000",
        )
        target = ReviewedNonPersonEntityDecision.objects.get(
            source=DECISION_SOURCE,
            candidate_id="EC0006",
        )
        self.assertEqual(merge.result_entity_id, target.result_entity_id)
        self.assertEqual(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=DECISION_SOURCE
            ).count(),
            109,
        )

    def test_command_apply_uses_real_parser_and_token(self):
        preflight = _ready_contract()
        before_people = Person.objects.count()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workbook.xlsx"
            _write_valid_workbook(
                path,
                list(preflight.rows),
                list(preflight.aliases),
                list(preflight.split_routes),
            )
            digest = sha256_path(path)
            output = StringIO()
            with (
                patch.object(
                    preflight_module,
                    "AUTHORITATIVE_WORKBOOK_SHA256",
                    digest,
                ),
                patch(
                    "documents.services.non_person_entity_review_apply."
                    "preflight_authoritative_workbook",
                    wraps=preflight_authoritative_workbook,
                ) as parsed,
            ):
                call_command(
                    "non_person_entity_review_apply",
                    workbook=str(path),
                    confirm=APPLY_CONFIRM_TOKEN,
                    stdout=output,
                )
            self.assertEqual(parsed.call_count, 2)
        report = output.getvalue()
        self.assertIn("entities_created: 70", report)
        self.assertIn("occurrences_created: 6", report)
        self.assertIn("ALREADY_APPLIED: 109", report)
        self.assertEqual(Person.objects.count(), before_people)

    def test_fabricated_preflight_is_not_a_public_write_path(self):
        signature = inspect.signature(apply_non_person_entity_review)
        self.assertEqual(tuple(signature.parameters), ("path", "confirm"))
        self.assertFalse(hasattr(apply_module, "apply_reviewed_preflight"))
        preflight = _ready_contract()
        before = _tracked_counts()
        with self.assertRaises(TypeError):
            apply_non_person_entity_review(preflight, confirm=APPLY_CONFIRM_TOKEN)
        self.assertEqual(_tracked_counts(), before)

    def test_preflight_runs_again_immediately_before_writes(self):
        preflight = _ready_contract()
        starting = NonPersonEntity.objects.count()
        seen = []

        def track(path):
            seen.append(NonPersonEntity.objects.count())
            return preflight_authoritative_workbook(path)

        with (
            _authoritative_workbook(preflight) as path,
            patch.object(
                apply_module,
                "preflight_authoritative_workbook",
                side_effect=track,
            ),
        ):
            apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)

        self.assertEqual(seen, [starting, starting])
        self.assertEqual(NonPersonEntity.objects.count(), starting + 70)

    def test_second_preflight_failure_writes_nothing(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        calls = {"n": 0}

        def fail_second(path):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise PreflightError(("SHA256_MISMATCH: second parse",))
            return preflight_authoritative_workbook(path)

        with (
            _authoritative_workbook(preflight) as path,
            patch.object(
                apply_module,
                "preflight_authoritative_workbook",
                side_effect=fail_second,
            ),
            self.assertRaises(PreflightError),
        ):
            apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)

        self.assertEqual(calls["n"], 2)
        self.assertEqual(_tracked_counts(), before)

    def test_second_preflight_sha_change_writes_nothing(self):
        preflight = _ready_contract()
        before = _tracked_counts()
        calls = {"n": 0}

        def change_second(path):
            calls["n"] += 1
            result = preflight_authoritative_workbook(path)
            if calls["n"] >= 2:
                return replace(result, workbook_sha256="a" * 64)
            return result

        with (
            _authoritative_workbook(preflight) as path,
            patch.object(
                apply_module,
                "preflight_authoritative_workbook",
                side_effect=change_second,
            ),
            self.assertRaises(ApplyError) as caught,
        ):
            apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)

        self.assertIn("workbook sha256 changed before write", str(caught.exception))
        self.assertEqual(calls["n"], 2)
        self.assertEqual(_tracked_counts(), before)

    def test_replay_locks_existing_result_entities_and_aliases(self):
        preflight = _ready_contract()
        workbook = _authoritative_workbook(preflight)
        path = workbook.__enter__()
        self.addCleanup(workbook.__exit__, None, None, None)
        apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)
        unrelated = NonPersonEntity.objects.create(
            canonical_name="unrelated-lock-probe",
            display_name="",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        NonPersonEntityAlias.objects.create(
            entity=unrelated,
            name="unrelated-alias",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        expected_entities = set(
            ReviewedNonPersonEntityDecision.objects.filter(
                source=DECISION_SOURCE,
                result_entity_id__isnull=False,
            ).values_list("result_entity_id", flat=True)
        )
        expected_aliases = set(
            NonPersonEntityAlias.objects.filter(
                entity_id__in=expected_entities
            ).values_list("pk", flat=True)
        )
        self.assertEqual(len(expected_entities), 70)
        self.assertIn(
            unrelated.pk, set(NonPersonEntity.objects.values_list("pk", flat=True))
        )
        self.assertNotIn(unrelated.pk, expected_entities)
        locked = {"entities": None, "aliases": None}

        def track_entities(*args, **kwargs):
            queryset = NonPersonEntity.objects.get_queryset().select_for_update(
                *args,
                **kwargs,
            )
            real_filter = queryset.filter

            def capture(*filter_args, **filter_kwargs):
                locked["entities"] = filter_kwargs.get("pk__in")
                return real_filter(*filter_args, **filter_kwargs)

            queryset.filter = capture
            return queryset

        def track_aliases(*args, **kwargs):
            queryset = NonPersonEntityAlias.objects.get_queryset().select_for_update(
                *args,
                **kwargs,
            )
            real_filter = queryset.filter

            def capture(*filter_args, **filter_kwargs):
                locked["aliases"] = list(
                    real_filter(*filter_args, **filter_kwargs).values_list(
                        "pk", flat=True
                    )
                )
                return real_filter(*filter_args, **filter_kwargs)

            queryset.filter = capture
            return queryset

        with (
            patch.object(
                apply_module.NonPersonEntity.objects,
                "select_for_update",
                side_effect=track_entities,
            ),
            patch.object(
                apply_module.NonPersonEntityAlias.objects,
                "select_for_update",
                side_effect=track_aliases,
            ),
        ):
            replay = apply_non_person_entity_review(path, confirm=APPLY_CONFIRM_TOKEN)

        self.assertEqual(replay.entities_created, 0)
        self.assertEqual(set(locked["entities"]), expected_entities)
        self.assertEqual(set(locked["aliases"]), expected_aliases)

    def test_integrity_error_at_transaction_exit_rolls_back(self):
        preflight = _ready_contract()
        before = _tracked_counts()

        real_commit = connection.savepoint_commit
        commits = {"n": 0}

        def fail_commit(sid):
            commits["n"] += 1
            if commits["n"] == 1:
                raise IntegrityError(
                    "duplicate key value violates unique constraint "
                    '"uniq_non_person_entity_alias_entity_name"'
                )
            return real_commit(sid)

        with (
            patch.object(connection, "savepoint_commit", side_effect=fail_commit),
            self.assertRaises(ApplyError) as caught,
        ):
            _apply(preflight)

        self.assertIn("alias conflict", str(caught.exception))
        self.assertEqual(_tracked_counts(), before)
