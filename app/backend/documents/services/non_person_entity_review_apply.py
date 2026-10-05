"""Transactional apply for the reviewed v6 non-person entity contract.

Re-runs the authoritative workbook preflight and the Phase 3A classifier
immediately before writing. One transaction covers the whole batch. Any
STATE_DRIFT, BLOCKED candidate, source-text change, or final read-back
mismatch rolls the batch back.

Entities are created only for READY_TO_APPLY APPROVE candidates and are never
resolved by canonical_name. The dry-run command is not an apply mode.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from django.db import IntegrityError, transaction

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
from documents.services.non_person_entity_review_dry_run import (
    DECISION_SOURCE,
    STATE_ALREADY_APPLIED,
    STATE_BLOCKED,
    STATE_DRIFT,
    STATE_READY_TO_APPLY,
    CandidateDryRunResult,
    CandidatePlan,
    DryRunResult,
    OccurrencePlan,
    authoritative_displayed_text,
    classify_preflight,
    locate_surface_occurrences,
    source_text_sha256,
)
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_preflight import (
    PreflightResult,
    preflight_authoritative_workbook,
)

APPLY_CONFIRM_TOKEN = "APPLY_NON_PERSON_FINAL_RECON_2026_10_05"

_APPLYABLE = {STATE_READY_TO_APPLY, STATE_ALREADY_APPLIED}


class ApplyError(ValueError):
    """Fail-closed apply error. The transaction is rolled back."""


@dataclass(frozen=True)
class ApplyResult:
    workbook_sha256: str
    before_counts: dict[str, int]
    entities_created: int
    aliases_created: int
    decisions_created: int
    occurrences_created: int
    after_counts: dict[str, int]


def apply_non_person_entity_review(path: str | Path, *, confirm: str) -> ApplyResult:
    """Apply one authoritative workbook. The confirmation token is required.

    This is the only production write entry point. It parses the workbook before
    the transaction and parses it again inside the transaction, immediately
    before any write.
    """

    _require_confirm(confirm)
    workbook_path = Path(path)
    preflight = _read_authoritative_workbook(workbook_path)
    before = classify_preflight(preflight)
    _require_applyable(before, stage="pre-apply")
    try:
        with transaction.atomic():
            current = _read_authoritative_workbook(workbook_path)
            _require_reparsed_workbook(preflight, current)
            _lock_apply_rows(current)
            locked = classify_preflight(current)
            _require_applyable(locked, stage="locked")
            writes = _apply_ready(locked)
            final = classify_preflight(current)
            _require_fully_applied(final)
            _verify_persisted_totals(locked)
    except IntegrityError as exc:
        raise ApplyError(_integrity_message(exc)) from exc
    return ApplyResult(
        workbook_sha256=preflight.workbook_sha256,
        before_counts=before.state_counts(),
        entities_created=writes[0],
        aliases_created=writes[1],
        decisions_created=writes[2],
        occurrences_created=writes[3],
        after_counts=final.state_counts(),
    )


def format_apply_report(result: ApplyResult) -> str:
    before = result.before_counts
    after = result.after_counts
    lines = [
        "non_person_entity_review_apply",
        f"workbook_sha256: {result.workbook_sha256}",
        "confirmation: accepted",
        "before:",
        f"  {STATE_READY_TO_APPLY}: {before[STATE_READY_TO_APPLY]}",
        f"  {STATE_ALREADY_APPLIED}: {before[STATE_ALREADY_APPLIED]}",
        f"  {STATE_DRIFT}: {before[STATE_DRIFT]}",
        f"  {STATE_BLOCKED}: {before[STATE_BLOCKED]}",
        "writes:",
        f"  entities_created: {result.entities_created}",
        f"  aliases_created: {result.aliases_created}",
        f"  decisions_created: {result.decisions_created}",
        f"  occurrences_created: {result.occurrences_created}",
        "after:",
        f"  {STATE_READY_TO_APPLY}: {after[STATE_READY_TO_APPLY]}",
        f"  {STATE_ALREADY_APPLIED}: {after[STATE_ALREADY_APPLIED]}",
        f"  {STATE_DRIFT}: {after[STATE_DRIFT]}",
        f"  {STATE_BLOCKED}: {after[STATE_BLOCKED]}",
        "transaction: committed",
    ]
    return "\n".join(lines) + "\n"


def _read_authoritative_workbook(path: Path) -> PreflightResult:
    preflight = preflight_authoritative_workbook(path)
    if preflight.apply_blockers:
        raise ApplyError("workbook apply blockers are present")
    return preflight


def _require_reparsed_workbook(first: PreflightResult, second: PreflightResult) -> None:
    expected = preflight_module.AUTHORITATIVE_WORKBOOK_SHA256
    if (
        second.workbook_sha256 != first.workbook_sha256
        or second.workbook_sha256 != expected
    ):
        raise ApplyError("workbook sha256 changed before write")


def _require_confirm(confirm: str) -> None:
    if confirm != APPLY_CONFIRM_TOKEN:
        raise ApplyError("confirmation token rejected")


def _require_applyable(result: DryRunResult, *, stage: str) -> None:
    refused = [
        candidate
        for candidate in result.candidates
        if candidate.state not in _APPLYABLE
    ]
    if not refused:
        return
    details = [
        f"{candidate.candidate_id} {candidate.state}: {'; '.join(candidate.reasons)}"
        for candidate in sorted(refused, key=lambda item: item.candidate_id)
    ]
    raise ApplyError(f"{stage} classification refused apply:\n" + "\n".join(details))


def _require_fully_applied(result: DryRunResult) -> None:
    counts = result.state_counts()
    if (
        counts[STATE_ALREADY_APPLIED] == len(result.candidates)
        and counts[STATE_READY_TO_APPLY] == 0
        and counts[STATE_DRIFT] == 0
        and counts[STATE_BLOCKED] == 0
        and len(result.candidates) > 0
    ):
        return
    raise ApplyError(
        "final verification failed: "
        f"READY_TO_APPLY={counts[STATE_READY_TO_APPLY]} "
        f"ALREADY_APPLIED={counts[STATE_ALREADY_APPLIED]} "
        f"STATE_DRIFT={counts[STATE_DRIFT]} "
        f"BLOCKED={counts[STATE_BLOCKED]}"
    )


def _lock_apply_rows(preflight: PreflightResult) -> None:
    candidate_ids = sorted({row.candidate_id for row in preflight.rows})
    decisions = list(
        ReviewedNonPersonEntityDecision.objects.select_for_update()
        .filter(source=DECISION_SOURCE, candidate_id__in=candidate_ids)
        .order_by("candidate_id")
    )
    entity_ids = sorted(
        {
            decision.result_entity_id
            for decision in decisions
            if decision.result_entity_id is not None
        }
    )
    if entity_ids:
        list(
            NonPersonEntity.objects.select_for_update()
            .filter(pk__in=entity_ids)
            .order_by("pk")
        )
        list(
            NonPersonEntityAlias.objects.select_for_update()
            .filter(entity_id__in=entity_ids)
            .order_by("pk")
        )
    item_ids = sorted({int(route.archive_item_id) for route in preflight.split_routes})
    if not item_ids:
        return
    list(ArchiveItem.objects.select_for_update().filter(pk__in=item_ids).order_by("pk"))
    list(
        ManualTextContent.objects.select_for_update()
        .filter(archive_item_id__in=item_ids)
        .order_by("pk")
    )
    list(
        Document.objects.select_for_update()
        .filter(archive_item_id__in=item_ids)
        .order_by("pk")
    )
    list(
        DocumentTextResult.objects.select_for_update()
        .filter(document__archive_item_id__in=item_ids)
        .order_by("pk")
    )
    list(
        ArchiveItemEntityOccurrence.objects.select_for_update()
        .filter(archive_item_id__in=item_ids)
        .order_by("pk")
    )


def _apply_ready(locked: DryRunResult) -> tuple[int, int, int, int]:
    ready = {
        candidate.candidate_id: candidate
        for candidate in locked.candidates
        if candidate.state == STATE_READY_TO_APPLY
    }
    if not ready:
        _recheck_split_sources(
            tuple(
                candidate.plan
                for candidate in locked.candidates
                if candidate.plan.occurrences
            )
        )
        return (0, 0, 0, 0)

    decisions = ReviewedNonPersonEntityDecision.Decision
    created_entity_ids: dict[str, int] = {}
    entities_created = _create_approve_entities(locked, ready, created_entity_ids)
    aliases_created = _create_approve_aliases(locked, ready, created_entity_ids)
    decisions_created = 0
    decisions_created += _create_decisions(
        locked,
        ready,
        decisions.APPROVE,
        result_entity_for=lambda plan: created_entity_ids[plan.candidate_id],
    )
    decisions_created += _create_decisions(
        locked,
        ready,
        decisions.SKIP,
        result_entity_for=lambda _plan: None,
    )
    decisions_created += _create_decisions(
        locked,
        ready,
        decisions.NEEDS_RESEARCH,
        result_entity_for=lambda _plan: None,
    )
    decisions_created += _create_merge_decisions(locked, ready)
    split_decisions = _create_split_decisions(locked, ready)
    decisions_created += split_decisions
    _recheck_split_sources(
        tuple(
            candidate.plan
            for candidate in locked.candidates
            if candidate.plan.decision == decisions.SPLIT
        )
    )
    occurrences_created = _create_split_occurrences(locked, ready)
    return (
        entities_created,
        aliases_created,
        decisions_created,
        occurrences_created,
    )


def _ready_plans(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
    decision: str,
) -> tuple[CandidatePlan, ...]:
    return tuple(
        sorted(
            (
                candidate.plan
                for candidate in locked.candidates
                if candidate.plan.decision == decision
                and candidate.candidate_id in ready
            ),
            key=lambda plan: plan.candidate_id,
        )
    )


def _create_approve_entities(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
    created_entity_ids: dict[str, int],
) -> int:
    created = 0
    for plan in _ready_plans(
        locked,
        ready,
        ReviewedNonPersonEntityDecision.Decision.APPROVE,
    ):
        if plan.entity is None:
            raise ApplyError(f"approve entity plan missing: {plan.candidate_id}")
        entity = NonPersonEntity.objects.create(
            canonical_name=plan.entity.canonical_name,
            display_name=plan.entity.display_name,
            entity_type=plan.entity.entity_type,
            entity_subtype=plan.entity.entity_subtype,
        )
        created_entity_ids[plan.candidate_id] = entity.pk
        created += 1
    return created


def _create_approve_aliases(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
    created_entity_ids: dict[str, int],
) -> int:
    created = 0
    for plan in _ready_plans(
        locked,
        ready,
        ReviewedNonPersonEntityDecision.Decision.APPROVE,
    ):
        entity_id = created_entity_ids[plan.candidate_id]
        for alias in plan.aliases:
            NonPersonEntityAlias.objects.create(
                entity_id=entity_id,
                name=alias.alias_name,
                kind=alias.alias_kind,
            )
            created += 1
    return created


def _create_decisions(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
    decision: str,
    *,
    result_entity_for,
) -> int:
    created = 0
    for plan in _ready_plans(locked, ready, decision):
        _create_decision(plan, result_entity_id=result_entity_for(plan))
        created += 1
    return created


def _create_merge_decisions(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
) -> int:
    created = 0
    for plan in _ready_plans(
        locked,
        ready,
        ReviewedNonPersonEntityDecision.Decision.MERGE,
    ):
        target = _locked_target_decision(plan.merge_target_candidate_id)
        if target.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE:
            raise ApplyError(f"merge target is not approve: {plan.candidate_id}")
        if target.result_entity_id is None:
            raise ApplyError(f"merge target missing entity: {plan.candidate_id}")
        _create_decision(plan, result_entity_id=target.result_entity_id)
        created += 1
    return created


def _create_split_decisions(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
) -> int:
    created = 0
    for plan in _ready_plans(
        locked,
        ready,
        ReviewedNonPersonEntityDecision.Decision.SPLIT,
    ):
        _create_decision(plan, result_entity_id=None)
        created += 1
    return created


def _create_split_occurrences(
    locked: DryRunResult,
    ready: dict[str, CandidateDryRunResult],
) -> int:
    created = 0
    for plan in _ready_plans(
        locked,
        ready,
        ReviewedNonPersonEntityDecision.Decision.SPLIT,
    ):
        decision = _decision_for(plan.candidate_id)
        if decision.result_entity_id is not None:
            raise ApplyError(f"split result_entity must be null: {plan.candidate_id}")
        for occurrence in sorted(
            plan.occurrences,
            key=lambda item: (item.archive_item_id, item.occurrence_ordinal),
        ):
            if occurrence.matched_text == "":
                raise ApplyError(
                    "matched_text missing "
                    f"item {occurrence.archive_item_id} "
                    f"ordinal {occurrence.occurrence_ordinal}"
                )
            target = _locked_target_decision(occurrence.target_candidate_id)
            if target.result_entity_id is None:
                raise ApplyError(
                    f"route target missing entity: {occurrence.target_candidate_id}"
                )
            ArchiveItemEntityOccurrence.objects.create(
                archive_item_id=occurrence.archive_item_id,
                text_kind=occurrence.text_kind,
                source_text_sha256=occurrence.source_text_sha256,
                normalization_version=occurrence.normalization_version,
                normalized_surface=occurrence.normalized_surface,
                occurrence_ordinal=occurrence.occurrence_ordinal,
                resolution_status=(
                    ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
                ),
                entity_id=target.result_entity_id,
                decision=decision,
                matched_text=occurrence.matched_text,
            )
            created += 1
    return created


def _create_decision(plan: CandidatePlan, *, result_entity_id: int | None) -> None:
    if (
        plan.decision == ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH
        and plan.review_status
        != ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED
    ):
        raise ApplyError(
            f"needs_research review_status must be UNRESOLVED: {plan.candidate_id}"
        )
    ReviewedNonPersonEntityDecision.objects.create(
        source=DECISION_SOURCE,
        candidate_id=plan.candidate_id,
        workbook_sha256=plan.workbook_sha256,
        decision=plan.decision,
        review_status=plan.review_status,
        final_canonical=plan.final_canonical,
        display_name=plan.display_name,
        merge_target_candidate_id=plan.merge_target_candidate_id,
        contextual_surfaces=plan.contextual_surfaces,
        note=plan.note,
        result_entity_id=result_entity_id,
    )


def _locked_target_decision(candidate_id: str) -> ReviewedNonPersonEntityDecision:
    try:
        return ReviewedNonPersonEntityDecision.objects.select_for_update().get(
            source=DECISION_SOURCE, candidate_id=candidate_id
        )
    except ReviewedNonPersonEntityDecision.DoesNotExist as exc:
        raise ApplyError(f"target missing: {candidate_id}") from exc


def _decision_for(candidate_id: str) -> ReviewedNonPersonEntityDecision:
    try:
        return ReviewedNonPersonEntityDecision.objects.select_for_update().get(
            source=DECISION_SOURCE,
            candidate_id=candidate_id,
        )
    except ReviewedNonPersonEntityDecision.DoesNotExist as exc:
        raise ApplyError(f"decision missing: {candidate_id}") from exc


def _recheck_split_sources(plans: tuple[CandidatePlan, ...]) -> None:
    grouped: dict[tuple[object, ...], list[OccurrencePlan]] = defaultdict(list)
    for plan in plans:
        for occurrence in plan.occurrences:
            grouped[_occurrence_group(occurrence)].append(occurrence)
    if not grouped:
        return
    item_ids = {int(key[0]) for key in grouped}
    items = {
        item.pk: item
        for item in ArchiveItem.objects.filter(pk__in=item_ids).select_related(
            "manual_text_content",
            "ocr_document",
        )
    }
    reasons: list[str] = []
    for key in sorted(grouped, key=lambda item: tuple(str(part) for part in item)):
        reasons.extend(_recheck_group(key, grouped[key], items))
    if reasons:
        raise ApplyError("split source recheck failed: " + "; ".join(reasons))


def _recheck_group(
    key: tuple[object, ...],
    members: list[OccurrencePlan],
    items: dict[int, ArchiveItem],
) -> list[str]:
    item_id = int(key[0])
    text_kind = str(key[1])
    pinned_sha = str(key[2])
    normalized_surface = str(key[4])
    label = f"item {item_id} surface {normalized_surface}"
    item = items.get(item_id)
    if item is None:
        return [f"archive item {item_id} not found"]
    source_text = authoritative_displayed_text(item, text_kind)
    if source_text is None:
        return [f"authoritative source text unavailable {label}"]
    if source_text_sha256(source_text) != pinned_sha:
        return [f"source text sha256 mismatch {label}"]
    located = locate_surface_occurrences(source_text, normalized_surface)
    expected = len(members)
    if located.count != expected:
        return [
            f"surface occurrence count drift {label} "
            f"found {located.count} reviewed {expected}"
        ]
    if located.occurrences is None:
        return [f"matched_text not safely derivable {label}"]
    by_ordinal = {occurrence.occurrence_ordinal: occurrence for occurrence in members}
    reasons: list[str] = []
    for found in located.occurrences:
        planned = by_ordinal.get(found.ordinal)
        if planned is None or planned.matched_text != found.matched_text:
            reasons.append(f"matched_text drift {label} ordinal {found.ordinal}")
    return reasons


def _occurrence_group(occurrence: OccurrencePlan) -> tuple[object, ...]:
    return (
        occurrence.archive_item_id,
        occurrence.text_kind,
        occurrence.source_text_sha256,
        occurrence.normalization_version,
        occurrence.normalized_surface,
    )


def _verify_persisted_totals(locked: DryRunResult) -> None:
    candidate_ids = [candidate.candidate_id for candidate in locked.candidates]
    decisions = ReviewedNonPersonEntityDecision.objects.filter(
        source=DECISION_SOURCE,
        candidate_id__in=candidate_ids,
    )
    if decisions.count() != len(candidate_ids):
        raise ApplyError(
            f"final decision count mismatch: {decisions.count()} != {len(candidate_ids)}"
        )
    approve = decisions.filter(
        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE
    )
    approve_entity_ids = list(approve.values_list("result_entity_id", flat=True))
    if any(entity_id is None for entity_id in approve_entity_ids):
        raise ApplyError("approve decision missing result_entity")
    if len(set(approve_entity_ids)) != len(approve_entity_ids):
        raise ApplyError("distinct approve candidates share result_entity")
    planned_approves = [
        candidate.plan
        for candidate in locked.candidates
        if candidate.plan.decision == ReviewedNonPersonEntityDecision.Decision.APPROVE
    ]
    if len(approve_entity_ids) != len(planned_approves):
        raise ApplyError("approve result entity count mismatch")
    alias_matches = 0
    for plan in planned_approves:
        decision = decisions.get(candidate_id=plan.candidate_id)
        for alias in plan.aliases:
            if NonPersonEntityAlias.objects.filter(
                entity_id=decision.result_entity_id,
                name=alias.alias_name,
                kind=alias.alias_kind,
            ).exists():
                alias_matches += 1
    planned_aliases = sum(len(plan.aliases) for plan in planned_approves)
    if alias_matches != planned_aliases:
        raise ApplyError(
            f"planned alias count mismatch: {alias_matches} != {planned_aliases}"
        )
    split_ids = [
        candidate.candidate_id
        for candidate in locked.candidates
        if candidate.plan.decision == ReviewedNonPersonEntityDecision.Decision.SPLIT
    ]
    occurrence_count = ArchiveItemEntityOccurrence.objects.filter(
        decision__source=DECISION_SOURCE,
        decision__candidate_id__in=split_ids,
    ).count()
    planned_occurrences = sum(
        len(candidate.plan.occurrences)
        for candidate in locked.candidates
        if candidate.plan.decision == ReviewedNonPersonEntityDecision.Decision.SPLIT
    )
    if occurrence_count != planned_occurrences:
        raise ApplyError(
            "split occurrence count mismatch: "
            f"{occurrence_count} != {planned_occurrences}"
        )
    shared = Counter(approve_entity_ids)
    if any(count > 1 for count in shared.values()):
        raise ApplyError("distinct approve candidates share result_entity")


def _integrity_message(exc: IntegrityError) -> str:
    text = str(exc).lower()
    if "uniq_non_person_entity_alias" in text:
        return "alias conflict"
    if "uniq_archive_item_entity_occurrence" in text:
        return "occurrence unique collision"
    if "uniq_reviewed_non_person_entity_decision" in text:
        return "decision identity conflict"
    return "apply aborted due to integrity conflict"
