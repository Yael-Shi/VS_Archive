"""Read-only dry-run planner for the v6 non-person entity review contract.

Classifies the validated workbook against current database rows and, for SPLIT
routes, the authoritative displayed source text. This module does not create,
update, or delete rows. Transactional apply lives in
``non_person_entity_review_apply`` and must re-run this classifier before
writing.

Decision identity is ``(DECISION_SOURCE, candidate_id)``. Entities are never
resolved by ``canonical_name``.

Alias comparison is not global exact-set equality. ``NonPersonEntityAlias``
has no source column and is unique on ``(entity, name)`` only. For an
already-applied entity this planner checks that every planned
``(alias_name, alias_kind)`` is present. A planned name with a different kind
is drift, and a missing planned alias is drift. Extra aliases on that entity
are left alone: they may belong to another workflow, and this dry-run must
not treat them as something to delete.

``matched_text`` is optional on ``ArchiveItemEntityOccurrence``. Normalization
can change length, so normalized offsets are not used as original offsets.
The plan stores the original slice from ``locate_surface_occurrences`` when
that slice round-trips through surface-v1, including canonical NFC
reordering and composition. When a safe slice is unavailable, the SPLIT
candidate is BLOCKED rather than given an invented slice.

Distinct APPROVE decisions for this source must not share ``result_entity``.
That sharing is STATE_DRIFT. A MERGE decision may point at the same entity
as its APPROVE target. Sharing is detected by entity id, not by name.

Dependency rule: a candidate with no persisted conflict becomes BLOCKED when
a required target is BLOCKED or STATE_DRIFT. Its own persisted mismatch is
STATE_DRIFT. STATE_DRIFT is never repaired here.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path

from django.db.models import Q

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_occurrences import (
    SurfaceLocation,
    authoritative_displayed_text,
    item_supports_occurrence_text_kind,
    locate_surface_occurrences,
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_review_preflight import (
    PreflightError,
    PreflightResult,
    preflight_authoritative_workbook,
)

DECISION_SOURCE = "vs_archive_non_person_final_recon_2026_10_05"

STATE_READY_TO_APPLY = "READY_TO_APPLY"
STATE_ALREADY_APPLIED = "ALREADY_APPLIED"
STATE_DRIFT = "STATE_DRIFT"
STATE_BLOCKED = "BLOCKED"

_DECISION_FIELDS = (
    "decision",
    "review_status",
    "final_canonical",
    "display_name",
    "merge_target_candidate_id",
    "contextual_surfaces",
    "note",
    "workbook_sha256",
)
_ENTITY_FIELDS = (
    "canonical_name",
    "display_name",
    "entity_type",
    "entity_subtype",
)


class DryRunError(ValueError):
    """Fail-closed dry-run error. Never indicates that a write occurred."""


@dataclass(frozen=True)
class AliasPlan:
    alias_name: str
    alias_kind: str


@dataclass(frozen=True)
class EntityPlan:
    canonical_name: str
    display_name: str
    entity_type: str
    entity_subtype: str


@dataclass(frozen=True)
class OccurrencePlan:
    archive_item_id: int
    text_kind: str
    source_text_sha256: str
    normalization_version: str
    normalized_surface: str
    occurrence_ordinal: int
    resolution_status: str
    target_candidate_id: str
    matched_text: str


@dataclass(frozen=True)
class CandidatePlan:
    candidate_id: str
    decision: str
    review_status: str
    final_canonical: str
    display_name: str
    merge_target_candidate_id: str
    contextual_surfaces: str
    note: str
    workbook_sha256: str
    entity: EntityPlan | None
    aliases: tuple[AliasPlan, ...]
    occurrences: tuple[OccurrencePlan, ...]
    result_entity_required: bool


@dataclass(frozen=True)
class CandidateDryRunResult:
    candidate_id: str
    state: str
    reasons: tuple[str, ...]
    plan: CandidatePlan


@dataclass(frozen=True)
class DryRunResult:
    workbook_sha256: str
    parser_valid: bool
    candidates: tuple[CandidateDryRunResult, ...]

    def state_counts(self) -> dict[str, int]:
        counts = {
            STATE_READY_TO_APPLY: 0,
            STATE_ALREADY_APPLIED: 0,
            STATE_DRIFT: 0,
            STATE_BLOCKED: 0,
        }
        for candidate in self.candidates:
            counts[candidate.state] += 1
        return counts

    @property
    def planned_entity_creates(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.state == STATE_READY_TO_APPLY and candidate.plan.entity
        )

    @property
    def planned_alias_creates(self) -> int:
        return sum(
            len(candidate.plan.aliases)
            for candidate in self.candidates
            if candidate.state == STATE_READY_TO_APPLY
        )

    @property
    def planned_decision_creates(self) -> int:
        return sum(
            1
            for candidate in self.candidates
            if candidate.state == STATE_READY_TO_APPLY
        )

    @property
    def planned_occurrence_creates(self) -> int:
        return sum(
            len(candidate.plan.occurrences)
            for candidate in self.candidates
            if candidate.state == STATE_READY_TO_APPLY
        )


def run_non_person_entity_review_dry_run(path: str | Path) -> DryRunResult:
    """Parse the authoritative workbook, then classify it without writing."""

    try:
        preflight = preflight_authoritative_workbook(path)
    except PreflightError:
        raise
    return classify_preflight(preflight)


def classify_preflight(preflight: PreflightResult) -> DryRunResult:
    """Classify one already validated preflight result. Performs no writes."""

    if preflight.apply_blockers:
        raise DryRunError("workbook apply blockers are present; dry-run stopped")
    plans = _plans_from_preflight(preflight)
    return DryRunResult(
        workbook_sha256=preflight.workbook_sha256,
        parser_valid=True,
        candidates=tuple(_classify_plans(plans)),
    )


def format_dry_run_report(result: DryRunResult) -> str:
    counts = result.state_counts()
    lines = [
        "non_person_entity_review_dry_run",
        "parser_valid: yes" if result.parser_valid else "parser_valid: no",
        f"workbook_sha256: {result.workbook_sha256}",
        f"candidates: {len(result.candidates)}",
        f"{STATE_READY_TO_APPLY}: {counts[STATE_READY_TO_APPLY]}",
        f"{STATE_ALREADY_APPLIED}: {counts[STATE_ALREADY_APPLIED]}",
        f"{STATE_DRIFT}: {counts[STATE_DRIFT]}",
        f"{STATE_BLOCKED}: {counts[STATE_BLOCKED]}",
        f"planned_entity_creates: {result.planned_entity_creates}",
        f"planned_alias_creates: {result.planned_alias_creates}",
        f"planned_decision_creates: {result.planned_decision_creates}",
        f"planned_occurrence_creates: {result.planned_occurrence_creates}",
    ]
    for candidate in sorted(
        (
            item
            for item in result.candidates
            if item.state not in {STATE_READY_TO_APPLY, STATE_ALREADY_APPLIED}
        ),
        key=lambda item: item.candidate_id,
    ):
        lines.append(f"candidate: {candidate.candidate_id}")
        lines.append(f"state: {candidate.state}")
        for reason in candidate.reasons:
            lines.append(f"reason: {reason}")
    return "\n".join(lines) + "\n"


def _plans_from_preflight(
    preflight: PreflightResult,
) -> tuple[CandidatePlan, ...]:
    aliases_by_target: dict[str, list[AliasPlan]] = defaultdict(list)
    for alias in preflight.aliases:
        aliases_by_target[alias.target_candidate_id].append(
            AliasPlan(alias_name=alias.alias_name, alias_kind=alias.alias_kind)
        )
    occurrences_by_split: dict[str, list[OccurrencePlan]] = defaultdict(list)
    for route in preflight.split_routes:
        occurrences_by_split[route.split_candidate_id].append(
            OccurrencePlan(
                archive_item_id=int(route.archive_item_id),
                text_kind=route.text_kind,
                source_text_sha256=route.source_text_sha256,
                normalization_version=route.normalization_version,
                normalized_surface=normalize_surface_v1(route.surface),
                occurrence_ordinal=int(route.occurrence_ordinal),
                resolution_status=route.resolution_status,
                target_candidate_id=route.target_candidate_id,
                matched_text="",
            )
        )
    plans: list[CandidatePlan] = []
    decisions = ReviewedNonPersonEntityDecision.Decision
    for row in preflight.rows:
        entity = None
        if row.final_decision == decisions.APPROVE:
            entity = EntityPlan(
                canonical_name=row.final_canonical,
                display_name="",
                entity_type=row.entity_type,
                entity_subtype=row.entity_subtype,
            )
        plans.append(
            CandidatePlan(
                candidate_id=row.candidate_id,
                decision=row.final_decision,
                review_status=row.status,
                final_canonical=row.final_canonical,
                display_name="",
                merge_target_candidate_id=row.merge_target,
                contextual_surfaces=row.surface,
                note=row.final_notes,
                workbook_sha256=preflight.workbook_sha256,
                entity=entity,
                aliases=tuple(aliases_by_target.get(row.candidate_id, ())),
                occurrences=tuple(occurrences_by_split.get(row.candidate_id, ())),
                result_entity_required=row.final_decision
                in {decisions.APPROVE, decisions.MERGE},
            )
        )
    return tuple(plans)


def _classify_plans(
    plans: tuple[CandidatePlan, ...],
) -> list[CandidateDryRunResult]:
    by_id = {plan.candidate_id: plan for plan in plans}
    loaded = _load_state(plans)
    source_issues, matched_text = _verify_split_sources(plans, loaded.items)
    states: dict[str, str] = {}
    results: dict[str, CandidateDryRunResult] = {}
    for plan in plans:
        if plan.decision in {
            ReviewedNonPersonEntityDecision.Decision.MERGE,
            ReviewedNonPersonEntityDecision.Decision.SPLIT,
        }:
            continue
        result = _classify_independent(plan, loaded)
        states[plan.candidate_id] = result.state
        results[plan.candidate_id] = result
    ordered: list[CandidateDryRunResult] = []
    for plan in plans:
        if plan.candidate_id in results:
            ordered.append(results[plan.candidate_id])
            continue
        if plan.decision == ReviewedNonPersonEntityDecision.Decision.MERGE:
            result = _classify_merge(plan, loaded, states, by_id)
        else:
            result = _classify_split(
                plan,
                loaded,
                states,
                by_id,
                source_issues,
                matched_text,
            )
        states[plan.candidate_id] = result.state
        ordered.append(result)
    return ordered


@dataclass(frozen=True)
class _LoadedState:
    decisions: dict[str, ReviewedNonPersonEntityDecision]
    aliases_by_entity: dict[int, tuple[NonPersonEntityAlias, ...]]
    occurrences_by_identity: dict[tuple[object, ...], ArchiveItemEntityOccurrence]
    occurrences_by_decision: dict[int, tuple[ArchiveItemEntityOccurrence, ...]]
    items: dict[int, ArchiveItem]
    shared_approve_entities: dict[int, tuple[str, ...]]


def _load_state(plans: tuple[CandidatePlan, ...]) -> _LoadedState:
    candidate_ids = [plan.candidate_id for plan in plans]
    decisions = {
        row.candidate_id: row
        for row in ReviewedNonPersonEntityDecision.objects.filter(
            source=DECISION_SOURCE,
            candidate_id__in=candidate_ids,
        ).select_related("result_entity")
    }
    entity_ids = [
        row.result_entity_id
        for row in decisions.values()
        if row.result_entity_id is not None
    ]
    aliases_by_entity: dict[int, list[NonPersonEntityAlias]] = defaultdict(list)
    if entity_ids:
        for alias in NonPersonEntityAlias.objects.filter(entity_id__in=entity_ids):
            aliases_by_entity[alias.entity_id].append(alias)
    item_ids = [
        occurrence.archive_item_id for plan in plans for occurrence in plan.occurrences
    ]
    decision_ids = [row.pk for row in decisions.values()]
    occurrence_filter = Q()
    if item_ids:
        occurrence_filter |= Q(archive_item_id__in=item_ids)
    if decision_ids:
        occurrence_filter |= Q(decision_id__in=decision_ids)
    occurrences: list[ArchiveItemEntityOccurrence] = []
    if item_ids or decision_ids:
        occurrences = list(
            ArchiveItemEntityOccurrence.objects.filter(occurrence_filter)
        )
    by_identity: dict[tuple[object, ...], ArchiveItemEntityOccurrence] = {}
    by_decision: dict[int, list[ArchiveItemEntityOccurrence]] = defaultdict(list)
    for occurrence in occurrences:
        by_identity[_occurrence_identity(occurrence)] = occurrence
        if occurrence.decision_id is not None:
            by_decision[occurrence.decision_id].append(occurrence)
    items = {
        item.pk: item
        for item in ArchiveItem.objects.filter(pk__in=item_ids).select_related(
            "manual_text_content",
            "ocr_document",
        )
    }
    return _LoadedState(
        decisions=decisions,
        aliases_by_entity={
            entity_id: tuple(rows) for entity_id, rows in aliases_by_entity.items()
        },
        occurrences_by_identity=by_identity,
        occurrences_by_decision={
            decision_id: tuple(rows) for decision_id, rows in by_decision.items()
        },
        items=items,
        shared_approve_entities=_shared_approve_entities(decisions),
    )


def _classify_independent(
    plan: CandidatePlan,
    loaded: _LoadedState,
) -> CandidateDryRunResult:
    drift: list[str] = []
    blocked: list[str] = []
    blocked.extend(_plan_shape_blockers(plan))
    decision = loaded.decisions.get(plan.candidate_id)
    if decision is not None:
        drift.extend(_decision_field_drifts(decision, plan))
        drift.extend(_result_entity_drifts(decision, plan, loaded))
        drift.extend(_unexpected_decision_occurrences(decision, loaded, ()))
    return _result(plan, drift, blocked, decision is not None)


def _classify_merge(
    plan: CandidatePlan,
    loaded: _LoadedState,
    states: dict[str, str],
    plans_by_id: dict[str, CandidatePlan],
) -> CandidateDryRunResult:
    drift: list[str] = []
    blocked: list[str] = []
    blocked.extend(_plan_shape_blockers(plan))
    target_id = plan.merge_target_candidate_id
    target_state = states.get(target_id)
    if target_id not in plans_by_id:
        blocked.append(f"merge target {target_id} is absent")
    elif target_state not in {STATE_READY_TO_APPLY, STATE_ALREADY_APPLIED}:
        blocked.append(f"merge target {target_id} is {target_state}")
    decision = loaded.decisions.get(plan.candidate_id)
    if decision is not None:
        drift.extend(_decision_field_drifts(decision, plan))
        if decision.result_entity_id is None:
            drift.append("result_entity required")
        elif target_state != STATE_ALREADY_APPLIED:
            drift.append(f"merge target {target_id} is {target_state}")
        else:
            target_decision = loaded.decisions.get(target_id)
            target_entity_id = (
                None if target_decision is None else target_decision.result_entity_id
            )
            if decision.result_entity_id != target_entity_id:
                drift.append("merge result_entity does not match target")
        drift.extend(_unexpected_decision_occurrences(decision, loaded, ()))
    return _result(plan, drift, blocked, decision is not None)


def _classify_split(
    plan: CandidatePlan,
    loaded: _LoadedState,
    states: dict[str, str],
    plans_by_id: dict[str, CandidatePlan],
    source_issues: dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    matched_text: dict[tuple[object, ...], str],
) -> CandidateDryRunResult:
    drift: list[str] = []
    blocked: list[str] = []
    blocked.extend(_plan_shape_blockers(plan))
    source_drift, source_blocked = source_issues.get(plan.candidate_id, ((), ()))
    drift.extend(source_drift)
    blocked.extend(source_blocked)
    occurrences = tuple(
        replace(
            occurrence,
            matched_text=matched_text.get(_plan_identity(occurrence), ""),
        )
        for occurrence in plan.occurrences
    )
    plan = replace(plan, occurrences=occurrences)
    for occurrence in plan.occurrences:
        target_state = states.get(occurrence.target_candidate_id)
        if occurrence.target_candidate_id not in plans_by_id:
            blocked.append(f"route target {occurrence.target_candidate_id} is absent")
        elif target_state not in {STATE_READY_TO_APPLY, STATE_ALREADY_APPLIED}:
            blocked.append(
                f"route target {occurrence.target_candidate_id} is {target_state}"
            )
    decision = loaded.decisions.get(plan.candidate_id)
    matched_ids: set[int] = set()
    if decision is not None:
        drift.extend(_decision_field_drifts(decision, plan))
        if decision.result_entity_id is not None:
            drift.append("result_entity must be null")
    for occurrence in plan.occurrences:
        if _occurrence_source_failed(occurrence, source_drift, source_blocked):
            continue
        identity = _plan_identity(occurrence)
        row = loaded.occurrences_by_identity.get(identity)
        label = (
            f"item {occurrence.archive_item_id} ordinal {occurrence.occurrence_ordinal}"
        )
        if decision is None:
            if row is not None:
                drift.append(f"occurrence identity already stored {label}")
            continue
        if row is None:
            if _hash_mismatch(loaded, decision.pk, occurrence):
                drift.append(f"occurrence hash mismatch {label}")
            else:
                drift.append(f"planned occurrence missing {label}")
            continue
        matched_ids.add(row.pk)
        if (
            row.resolution_status
            != ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
        ):
            drift.append(f"occurrence resolution_status mismatch {label}")
        if row.decision_id != decision.pk:
            drift.append(f"occurrence decision mismatch {label}")
        if row.matched_text != occurrence.matched_text:
            drift.append(f"occurrence matched_text mismatch {label}")
        if row.source_text_sha256 != occurrence.source_text_sha256:
            drift.append(f"occurrence hash mismatch {label}")
        target_decision = loaded.decisions.get(occurrence.target_candidate_id)
        target_state = states.get(occurrence.target_candidate_id)
        expected_entity_id = (
            None
            if target_decision is None or target_state != STATE_ALREADY_APPLIED
            else target_decision.result_entity_id
        )
        if row.entity_id != expected_entity_id:
            drift.append(f"occurrence target mismatch {label}")
    if decision is not None:
        drift.extend(_unexpected_decision_occurrences(decision, loaded, matched_ids))
    return _result(plan, drift, blocked, decision is not None)


def _occurrence_source_failed(
    occurrence: OccurrencePlan,
    source_drift: tuple[str, ...],
    source_blocked: tuple[str, ...],
) -> bool:
    marker = f"item {occurrence.archive_item_id} "
    return any(marker in reason for reason in (*source_drift, *source_blocked))


def _result(
    plan: CandidatePlan,
    drift: list[str],
    blocked: list[str],
    decision_exists: bool,
) -> CandidateDryRunResult:
    drift_reasons = tuple(sorted(set(drift)))
    blocked_reasons = tuple(sorted(set(blocked)))
    if drift_reasons:
        state = STATE_DRIFT
        reasons = tuple(sorted(set(drift_reasons + blocked_reasons)))
    elif blocked_reasons:
        state = STATE_BLOCKED
        reasons = blocked_reasons
    elif decision_exists:
        state = STATE_ALREADY_APPLIED
        reasons = ()
    else:
        state = STATE_READY_TO_APPLY
        reasons = ()
    return CandidateDryRunResult(
        candidate_id=plan.candidate_id,
        state=state,
        reasons=reasons,
        plan=plan,
    )


def _plan_shape_blockers(plan: CandidatePlan) -> list[str]:
    blocked: list[str] = []
    decisions = ReviewedNonPersonEntityDecision.Decision
    if (
        plan.decision == decisions.NEEDS_RESEARCH
        and plan.review_status
        != ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED
    ):
        blocked.append("needs_research review_status must be UNRESOLVED")
    if plan.decision == decisions.APPROVE and plan.entity is None:
        blocked.append("approve entity plan missing")
    if plan.decision == decisions.APPROVE and plan.final_canonical == "":
        blocked.append("approve canonical missing")
    if plan.decision == decisions.MERGE and plan.merge_target_candidate_id == "":
        blocked.append("merge target missing")
    if plan.decision == decisions.SPLIT and not plan.occurrences:
        blocked.append("split occurrence plan missing")
    blocked.extend(_length_blockers(plan))
    if plan.decision != decisions.APPROVE and plan.aliases:
        blocked.append("aliases are only planned for approve candidates")
    if plan.decision != decisions.SPLIT and plan.occurrences:
        blocked.append("occurrences are only planned for split candidates")
    return blocked


def _length_blockers(plan: CandidatePlan) -> list[str]:
    blocked: list[str] = []
    limits = {
        "final_canonical": _max_length(
            ReviewedNonPersonEntityDecision, "final_canonical"
        ),
        "display_name": _max_length(ReviewedNonPersonEntityDecision, "display_name"),
        "merge_target_candidate_id": _max_length(
            ReviewedNonPersonEntityDecision,
            "merge_target_candidate_id",
        ),
        "candidate_id": _max_length(ReviewedNonPersonEntityDecision, "candidate_id"),
    }
    values = {
        "final_canonical": plan.final_canonical,
        "display_name": plan.display_name,
        "merge_target_candidate_id": plan.merge_target_candidate_id,
        "candidate_id": plan.candidate_id,
    }
    for field, limit in limits.items():
        if limit is not None and len(values[field]) > limit:
            blocked.append(f"{field} exceeds max length")
    if plan.entity is not None:
        entity_values = {
            "canonical_name": plan.entity.canonical_name,
            "display_name": plan.entity.display_name,
            "entity_type": plan.entity.entity_type,
            "entity_subtype": plan.entity.entity_subtype,
        }
        for field, value in entity_values.items():
            limit = _max_length(NonPersonEntity, field)
            if limit is not None and len(value) > limit:
                blocked.append(f"{field} exceeds max length")
    for alias in plan.aliases:
        limit = _max_length(NonPersonEntityAlias, "name")
        if limit is not None and len(alias.alias_name) > limit:
            blocked.append(f"alias_name exceeds max length: {alias.alias_name}")
    surface_limit = _max_length(ArchiveItemEntityOccurrence, "normalized_surface")
    for occurrence in plan.occurrences:
        if (
            surface_limit is not None
            and len(occurrence.normalized_surface) > surface_limit
        ):
            blocked.append(
                "normalized_surface exceeds max length "
                f"item {occurrence.archive_item_id}"
            )
    return blocked


def _max_length(model: type, field_name: str) -> int | None:
    max_length = model._meta.get_field(field_name).max_length
    return None if max_length is None else int(max_length)


def _decision_field_drifts(
    decision: ReviewedNonPersonEntityDecision,
    plan: CandidatePlan,
) -> list[str]:
    mismatched = [
        field
        for field in _DECISION_FIELDS
        if getattr(decision, field) != getattr(plan, field)
    ]
    if not mismatched:
        return []
    return ["decision field mismatch: " + ", ".join(mismatched)]


def _result_entity_drifts(
    decision: ReviewedNonPersonEntityDecision,
    plan: CandidatePlan,
    loaded: _LoadedState,
) -> list[str]:
    if plan.result_entity_required:
        if decision.result_entity_id is None or decision.result_entity is None:
            return ["result_entity required"]
        return (
            _entity_field_drifts(decision.result_entity, plan)
            + _alias_drifts(decision.result_entity_id, plan, loaded)
            + _shared_approve_drifts(decision, plan, loaded)
        )
    if decision.result_entity_id is not None:
        return ["result_entity must be null"]
    return []


def _shared_approve_entities(
    decisions: dict[str, ReviewedNonPersonEntityDecision],
) -> dict[int, tuple[str, ...]]:
    """Entity ids used as result_entity by more than one APPROVE decision."""

    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    owners: dict[int, list[str]] = defaultdict(list)
    for candidate_id, decision in decisions.items():
        if decision.decision != approve or decision.result_entity_id is None:
            continue
        owners[decision.result_entity_id].append(candidate_id)
    return {
        entity_id: tuple(sorted(candidate_ids))
        for entity_id, candidate_ids in owners.items()
        if len(candidate_ids) > 1
    }


def _shared_approve_drifts(
    decision: ReviewedNonPersonEntityDecision,
    plan: CandidatePlan,
    loaded: _LoadedState,
) -> list[str]:
    if plan.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE:
        return []
    if decision.result_entity_id is None:
        return []
    owners = loaded.shared_approve_entities.get(decision.result_entity_id, ())
    others = [
        candidate_id for candidate_id in owners if candidate_id != plan.candidate_id
    ]
    if not others:
        return []
    return ["approve result_entity shared with " + ", ".join(others)]


def _entity_field_drifts(entity: NonPersonEntity, plan: CandidatePlan) -> list[str]:
    if plan.entity is None:
        return ["result_entity must be null"]
    mismatched = [
        field
        for field in _ENTITY_FIELDS
        if getattr(entity, field) != getattr(plan.entity, field)
    ]
    if not mismatched:
        return []
    return ["result_entity field mismatch: " + ", ".join(mismatched)]


def _alias_drifts(
    entity_id: int,
    plan: CandidatePlan,
    loaded: _LoadedState,
) -> list[str]:
    existing = {
        alias.name: alias.kind for alias in loaded.aliases_by_entity.get(entity_id, ())
    }
    drifts: list[str] = []
    for alias in plan.aliases:
        kind = existing.get(alias.alias_name)
        if kind is None:
            drifts.append(f"planned alias missing: {alias.alias_name}")
        elif kind != alias.alias_kind:
            drifts.append(f"alias kind conflict: {alias.alias_name}")
    return drifts


def _unexpected_decision_occurrences(
    decision: ReviewedNonPersonEntityDecision,
    loaded: _LoadedState,
    expected_ids: set[int],
) -> list[str]:
    extras = [
        row
        for row in loaded.occurrences_by_decision.get(decision.pk, ())
        if row.pk not in expected_ids
    ]
    if not extras:
        return []
    return ["extra stored occurrence"]


def _verify_split_sources(
    plans: tuple[CandidatePlan, ...],
    items: dict[int, ArchiveItem],
) -> tuple[
    dict[str, tuple[tuple[str, ...], tuple[str, ...]]],
    dict[tuple[object, ...], str],
]:
    grouped: dict[tuple[object, ...], list[tuple[str, OccurrencePlan]]] = defaultdict(
        list
    )
    for plan in plans:
        for occurrence in plan.occurrences:
            grouped[_group_key(occurrence)].append((plan.candidate_id, occurrence))
    issues: dict[str, list[list[str]]] = defaultdict(lambda: [[], []])
    matched: dict[tuple[object, ...], str] = {}
    for key, members in grouped.items():
        drift, blocked, located = _verify_surface_group(key, members, items)
        for candidate_id, occurrence in members:
            issues[candidate_id][0].extend(drift)
            issues[candidate_id][1].extend(blocked)
            if located is None or located.occurrences is None:
                continue
            if drift or blocked:
                continue
            found = {item.ordinal: item.matched_text for item in located.occurrences}
            text = found.get(occurrence.occurrence_ordinal)
            if text is None:
                continue
            matched[_plan_identity(occurrence)] = text
    return (
        {
            candidate_id: (tuple(pair[0]), tuple(pair[1]))
            for candidate_id, pair in issues.items()
        },
        matched,
    )


def _verify_surface_group(
    key: tuple[object, ...],
    members: list[tuple[str, OccurrencePlan]],
    items: dict[int, ArchiveItem],
) -> tuple[list[str], list[str], SurfaceLocation | None]:
    item_id = int(key[0])
    text_kind = str(key[1])
    pinned_sha = str(key[2])
    normalized_surface = str(key[4])
    label = f"item {item_id} surface {normalized_surface}"
    item = items.get(item_id)
    if item is None:
        return [], [f"archive item {item_id} not found"], None
    if not item_supports_occurrence_text_kind(item, text_kind):
        return [], [f"unsupported item type {item.item_type} {label}"], None
    source_text = authoritative_displayed_text(item, text_kind)
    if source_text is None:
        return [], [f"authoritative source text unavailable {label}"], None
    if source_text_sha256(source_text) != pinned_sha:
        return [f"source text sha256 mismatch {label}"], [], None
    surface = members[0][1].normalized_surface
    located = locate_surface_occurrences(source_text, surface)
    expected = len(members)
    ordinals = {occurrence.occurrence_ordinal for _, occurrence in members}
    drift: list[str] = []
    blocked: list[str] = []
    if located.count == 0:
        blocked.append(f"zero surface occurrences {label}")
    elif located.count < expected:
        blocked.append(
            f"fewer surface occurrences {label} "
            f"found {located.count} reviewed {expected}"
        )
    elif located.count > expected:
        blocked.append(
            f"extra unreviewed surface occurrences {label} "
            f"found {located.count} reviewed {expected}"
        )
    elif ordinals != set(range(1, expected + 1)):
        blocked.append(f"reviewed occurrence ordinal missing {label}")
    elif located.occurrences is None:
        blocked.append(f"matched_text not safely derivable {label}")
    return drift, blocked, located


def _group_key(occurrence: OccurrencePlan) -> tuple[object, ...]:
    return (
        occurrence.archive_item_id,
        occurrence.text_kind,
        occurrence.source_text_sha256,
        occurrence.normalization_version,
        occurrence.normalized_surface,
    )


def _plan_identity(occurrence: OccurrencePlan) -> tuple[object, ...]:
    return (
        occurrence.archive_item_id,
        occurrence.text_kind,
        occurrence.source_text_sha256,
        occurrence.normalization_version,
        occurrence.normalized_surface,
        occurrence.occurrence_ordinal,
    )


def _occurrence_identity(
    occurrence: ArchiveItemEntityOccurrence,
) -> tuple[object, ...]:
    return (
        occurrence.archive_item_id,
        occurrence.text_kind,
        occurrence.source_text_sha256,
        occurrence.normalization_version,
        occurrence.normalized_surface,
        occurrence.occurrence_ordinal,
    )


def _hash_mismatch(
    loaded: _LoadedState,
    decision_id: int,
    occurrence: OccurrencePlan,
) -> bool:
    return any(
        row.archive_item_id == occurrence.archive_item_id
        and row.text_kind == occurrence.text_kind
        and row.normalization_version == occurrence.normalization_version
        and row.normalized_surface == occurrence.normalized_surface
        and row.occurrence_ordinal == occurrence.occurrence_ordinal
        and row.source_text_sha256 != occurrence.source_text_sha256
        for row in loaded.occurrences_by_decision.get(decision_id, ())
    )
