"""Approved merge of non-person entity 103 into entity 655.

Historical decision rows stay as the contracts stored them. Two correction
rows make EC0414 and EC0181 resolve to the survivor. Only the EC0414 row
retires entity 103. This module does not read or write the 2026-10-21
eleven-operation manifest.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from django.db import transaction

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceProposal,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_review_preflight import (
    SECOND_CONTRACT_SHA256,
    SECOND_DECISION_SOURCE,
    SIXTH_CONTRACT_SHA256,
    SIXTH_DECISION_SOURCE,
    THIRD_CONTRACT_SHA256,
    THIRD_DECISION_SOURCE,
)

APPROVAL_REFERENCE = "approved merge of entity 103 into entity 655; 2026-10-25"
CONFIRM_TOKEN = "APPLY_NON_PERSON_BNEI_AKIVA_SHADOW_MERGE_2026_10_25"
RETIRED_ENTITY_ID = 103
SURVIVOR_ENTITY_ID = 655
APPROVE_DECISION_ID = 141
MERGE_DECISION_ID = 633
SURVIVOR_DECISION_ID = 810
SHORT_NAME = "הנהלת הצללים של בני עקיבא"
SURVIVOR_NAME = "הנהלת הצללים של בני עקיבא במצרים"
ALIAS_KIND = NonPersonEntityAlias.Kind.SPELLING_VARIANT
_CORRECTION_DECISION_IDS = (APPROVE_DECISION_ID, MERGE_DECISION_ID)
_WATCHED_DECISION_IDS = (
    APPROVE_DECISION_ID,
    MERGE_DECISION_ID,
    SURVIVOR_DECISION_ID,
)


@dataclass(frozen=True)
class OccurrenceIdentity:
    """Six-field pin identity from the verified DEV occurrence snapshot."""

    occurrence_id: int
    archive_item_id: int
    text_kind: str
    source_text_sha256: str
    normalization_version: str
    normalized_surface: str
    occurrence_ordinal: int


# manual_completion_audit_01/dev_verification/occurrences.json, ids 96, 98, 100.
_VERIFIED_SOURCE_TEXT_SHA256 = (
    "1c3ff8dfcddd483987c28170b53fbe50539878a021423857aad1048920b8a808"
)
OCCURRENCE_IDENTITIES: tuple[OccurrenceIdentity, ...] = (
    OccurrenceIdentity(
        occurrence_id=96,
        archive_item_id=112,
        text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        source_text_sha256=_VERIFIED_SOURCE_TEXT_SHA256,
        normalization_version="surface-v1",
        normalized_surface="shadow hanhala",
        occurrence_ordinal=1,
    ),
    OccurrenceIdentity(
        occurrence_id=98,
        archive_item_id=112,
        text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        source_text_sha256=_VERIFIED_SOURCE_TEXT_SHA256,
        normalization_version="surface-v1",
        normalized_surface="shadow hanhala",
        occurrence_ordinal=2,
    ),
    OccurrenceIdentity(
        occurrence_id=100,
        archive_item_id=112,
        text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        source_text_sha256=_VERIFIED_SOURCE_TEXT_SHA256,
        normalization_version="surface-v1",
        normalized_surface="hanhala",
        occurrence_ordinal=4,
    ),
)
OCCURRENCE_IDS = tuple(identity.occurrence_id for identity in OCCURRENCE_IDENTITIES)
_OCCURRENCE_IDENTITIES_BY_ID = {
    identity.occurrence_id: identity for identity in OCCURRENCE_IDENTITIES
}


class ShadowMergeApplyError(Exception):
    """The approved snapshot does not match the database. Nothing is written."""

    def __init__(self, reasons: tuple[str, ...] | list[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class _DecisionSpec:
    decision_id: int
    source: str
    candidate_id: str
    workbook_sha256: str
    decision: str
    final_canonical: str
    merge_target_candidate_id: str
    result_entity_id: int


_DECISION_SPECS: tuple[_DecisionSpec, ...] = (
    _DecisionSpec(
        decision_id=APPROVE_DECISION_ID,
        source=SECOND_DECISION_SOURCE,
        candidate_id="EC0414",
        workbook_sha256=SECOND_CONTRACT_SHA256,
        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
        final_canonical=SHORT_NAME,
        merge_target_candidate_id="",
        result_entity_id=RETIRED_ENTITY_ID,
    ),
    _DecisionSpec(
        decision_id=MERGE_DECISION_ID,
        source=THIRD_DECISION_SOURCE,
        candidate_id="EC0181",
        workbook_sha256=THIRD_CONTRACT_SHA256,
        decision=ReviewedNonPersonEntityDecision.Decision.MERGE,
        final_canonical=SHORT_NAME,
        merge_target_candidate_id="EC0414",
        result_entity_id=RETIRED_ENTITY_ID,
    ),
    _DecisionSpec(
        decision_id=SURVIVOR_DECISION_ID,
        source=SIXTH_DECISION_SOURCE,
        candidate_id="EC0852",
        workbook_sha256=SIXTH_CONTRACT_SHA256,
        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
        final_canonical=SURVIVOR_NAME,
        merge_target_candidate_id="",
        result_entity_id=SURVIVOR_ENTITY_ID,
    ),
)


@dataclass(frozen=True)
class ShadowMergeAssessment:
    status: str
    reasons: tuple[str, ...]
    entity_delta: int
    alias_delta: int
    correction_delta: int
    decision_delta: int
    occurrence_delta: int
    alias_recommendation: str


@dataclass(frozen=True)
class ShadowMergeApplyResult:
    applied: bool
    assessment: ShadowMergeAssessment


@dataclass
class _Loaded:
    decisions: dict[int, ReviewedNonPersonEntityDecision]
    entities: dict[int, NonPersonEntity]
    aliases: dict[int, tuple[tuple[str, str], ...]]
    corrections: dict[int, NonPersonEntityCorrection]
    occurrence_ids_by_entity: dict[int, tuple[int, ...]]
    decisions_pointing_at: dict[int, tuple[int, ...]]
    retirement_decision_ids: tuple[int, ...]


@dataclass(frozen=True)
class _Snapshots:
    decisions: str
    occurrences: str
    proposals: str
    candidates: str
    entities: str
    aliases: tuple[tuple[int, int, str, str], ...]
    corrections: tuple[tuple[int, int, int, int | None, str, str, str, str, str], ...]


def manifest_document() -> dict[str, object]:
    """Stable description of this merge. Independent of the 2026-10-21 manifest."""

    return {
        "approval_reference": APPROVAL_REFERENCE,
        "retired_entity_id": RETIRED_ENTITY_ID,
        "survivor_entity_id": SURVIVOR_ENTITY_ID,
        "retirement_decision_id": APPROVE_DECISION_ID,
        "merge_decision_id": MERGE_DECISION_ID,
        "survivor_decision_id": SURVIVOR_DECISION_ID,
        "occurrence_ids": list(OCCURRENCE_IDS),
        "alias_name": SHORT_NAME,
        "alias_kind": ALIAS_KIND,
        "decisions": [
            {
                "decision_id": spec.decision_id,
                "source": spec.source,
                "candidate_id": spec.candidate_id,
                "workbook_sha256": spec.workbook_sha256,
                "decision": spec.decision,
                "final_canonical": spec.final_canonical,
                "merge_target_candidate_id": spec.merge_target_candidate_id,
                "result_entity_id": spec.result_entity_id,
            }
            for spec in _DECISION_SPECS
        ],
    }


def manifest_sha256() -> str:
    payload = json.dumps(
        manifest_document(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dry_run_bnei_akiva_shadow_merge() -> ShadowMergeAssessment:
    """Read the approved snapshot. Does not write."""

    return _assess(_load(for_update=False))


def apply_bnei_akiva_shadow_merge(*, confirm: str) -> ShadowMergeApplyResult:
    """Apply the merge in one transaction, or leave the database unchanged."""

    if confirm != CONFIRM_TOKEN:
        raise ShadowMergeApplyError(("confirmation token rejected",))
    with transaction.atomic():
        state = _load(for_update=True)
        assessment = _assess(state)
        if assessment.status == "blocked":
            raise ShadowMergeApplyError(assessment.reasons)
        if assessment.status == "already_applied":
            return ShadowMergeApplyResult(applied=False, assessment=assessment)
        before = _snapshots()
        _write(state)
        _require_unchanged(before)
        return ShadowMergeApplyResult(applied=True, assessment=assessment)


def format_shadow_merge_report(
    assessment: ShadowMergeAssessment, *, applied: bool | None
) -> str:
    lines = [
        f"status: {assessment.status}",
        f"alias_recommendation: {assessment.alias_recommendation}",
        f"entity_delta: {assessment.entity_delta}",
        f"alias_delta: {assessment.alias_delta}",
        f"correction_delta: {assessment.correction_delta}",
        f"decision_delta: {assessment.decision_delta}",
        f"occurrence_delta: {assessment.occurrence_delta}",
    ]
    if applied is not None:
        lines.insert(0, f"applied: {str(applied).lower()}")
    if assessment.reasons:
        lines.append("reasons:")
        lines.extend(f"- {reason}" for reason in assessment.reasons)
    return "\n".join(lines)


def _load(*, for_update: bool) -> _Loaded:
    decision_queryset = ReviewedNonPersonEntityDecision.objects.filter(
        pk__in=_WATCHED_DECISION_IDS
    )
    if for_update:
        decision_queryset = decision_queryset.select_for_update(of=("self",))
    decisions = {row.pk: row for row in decision_queryset.order_by("pk")}
    entity_queryset = NonPersonEntity.objects.filter(
        pk__in=(RETIRED_ENTITY_ID, SURVIVOR_ENTITY_ID)
    )
    if for_update:
        entity_queryset = entity_queryset.select_for_update(of=("self",))
    entities = {row.pk: row for row in entity_queryset.order_by("pk")}
    aliases: dict[int, list[tuple[str, str]]] = {
        RETIRED_ENTITY_ID: [],
        SURVIVOR_ENTITY_ID: [],
    }
    for alias in NonPersonEntityAlias.objects.filter(
        entity_id__in=(RETIRED_ENTITY_ID, SURVIVOR_ENTITY_ID)
    ).order_by("name", "id"):
        aliases[alias.entity_id].append((alias.name, alias.kind))
    corrections = {
        row.decision_id: row
        for row in NonPersonEntityCorrection.objects.filter(
            decision_id__in=_WATCHED_DECISION_IDS
        )
    }
    occurrence_ids_by_entity = {
        entity_id: tuple(
            ArchiveItemEntityOccurrence.objects.filter(entity_id=entity_id)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for entity_id in (RETIRED_ENTITY_ID, SURVIVOR_ENTITY_ID)
    }
    decisions_pointing_at = {
        entity_id: tuple(
            ReviewedNonPersonEntityDecision.objects.filter(result_entity_id=entity_id)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        for entity_id in (RETIRED_ENTITY_ID, SURVIVOR_ENTITY_ID)
    }
    retirement_decision_ids = tuple(
        NonPersonEntityCorrection.objects.filter(retired_entity_id=RETIRED_ENTITY_ID)
        .order_by("decision_id")
        .values_list("decision_id", flat=True)
    )
    return _Loaded(
        decisions=decisions,
        entities=entities,
        aliases={key: tuple(value) for key, value in aliases.items()},
        corrections=corrections,
        occurrence_ids_by_entity=occurrence_ids_by_entity,
        decisions_pointing_at=decisions_pointing_at,
        retirement_decision_ids=retirement_decision_ids,
    )


def _assess(state: _Loaded) -> ShadowMergeAssessment:
    missing = _missing_reasons(state)
    if missing:
        return _blocked(missing, alias_recommendation="not_evaluated")
    if _both_corrections_present(state):
        reasons = _applied_shape_reasons(state)
        reasons.extend(_replay_reasons(state))
        if reasons:
            return _blocked(reasons, alias_recommendation="withheld")
        return ShadowMergeAssessment(
            status="already_applied",
            reasons=(),
            entity_delta=0,
            alias_delta=0,
            correction_delta=0,
            decision_delta=0,
            occurrence_delta=0,
            alias_recommendation="present",
        )
    present = [
        spec.candidate_id
        for spec in _DECISION_SPECS
        if spec.decision_id in _CORRECTION_DECISION_IDS
        and spec.decision_id in state.corrections
    ]
    if present or SURVIVOR_DECISION_ID in state.corrections:
        return _blocked(
            ["partial corrections: " + ", ".join(present or ["EC0852"])],
            alias_recommendation="withheld",
        )
    reasons = _preimage_reasons(state)
    collision = _short_name_collision_reasons()
    reasons.extend(collision)
    if reasons:
        recommendation = "withheld" if collision else "not_evaluated"
        return _blocked(reasons, alias_recommendation=recommendation)
    return ShadowMergeAssessment(
        status="ready",
        reasons=(),
        entity_delta=0,
        alias_delta=1,
        correction_delta=2,
        decision_delta=0,
        occurrence_delta=0,
        alias_recommendation="add",
    )


def _blocked(reasons: list[str], *, alias_recommendation: str) -> ShadowMergeAssessment:
    return ShadowMergeAssessment(
        status="blocked",
        reasons=tuple(reasons),
        entity_delta=0,
        alias_delta=0,
        correction_delta=0,
        decision_delta=0,
        occurrence_delta=0,
        alias_recommendation=alias_recommendation,
    )


def _missing_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    for spec in _DECISION_SPECS:
        if spec.decision_id not in state.decisions:
            reasons.append(f"missing decision {spec.decision_id} {spec.candidate_id}")
    if RETIRED_ENTITY_ID not in state.entities:
        reasons.append("missing entity 103")
    if SURVIVOR_ENTITY_ID not in state.entities:
        reasons.append("missing entity 655")
    return reasons


def _both_corrections_present(state: _Loaded) -> bool:
    return all(
        decision_id in state.corrections for decision_id in _CORRECTION_DECISION_IDS
    )


def _preimage_reasons(state: _Loaded) -> list[str]:
    reasons = _identity_reasons(state)
    reasons.extend(_alias_inventory_reasons(state, expected_empty=True))
    reasons.extend(_occurrence_reasons(state))
    reasons.extend(_pointer_reasons(state))
    if state.retirement_decision_ids:
        reasons.append("entity 103 is already retired")
    return reasons


def _replay_reasons(state: _Loaded) -> list[str]:
    reasons = _identity_reasons(state)
    reasons.extend(_occurrence_reasons(state))
    reasons.extend(_pointer_reasons(state))
    survivor_aliases = state.aliases.get(SURVIVOR_ENTITY_ID, ())
    if (SHORT_NAME, ALIAS_KIND) not in survivor_aliases:
        reasons.append("short-name alias missing on entity 655")
    if state.aliases.get(RETIRED_ENTITY_ID, ()):
        reasons.append("entity 103 gained aliases")
    if state.retirement_decision_ids != (APPROVE_DECISION_ID,):
        reasons.append("entity 103 retirement is not only EC0414")
    return reasons


def _identity_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    for spec in _DECISION_SPECS:
        decision = state.decisions[spec.decision_id]
        reasons.extend(_decision_reasons(decision, spec))
    retired = state.entities[RETIRED_ENTITY_ID]
    survivor = state.entities[SURVIVOR_ENTITY_ID]
    if not _entity_is(
        retired,
        canonical=SHORT_NAME,
        display="",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        subtype=NonPersonEntity.EntitySubtype.YOUTH_MOVEMENT,
    ):
        reasons.append("entity 103 metadata drifted")
    if not _entity_is(
        survivor,
        canonical=SURVIVOR_NAME,
        display="",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        subtype=NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
    ):
        reasons.append("entity 655 metadata drifted")
    return reasons


def _decision_reasons(
    decision: ReviewedNonPersonEntityDecision, spec: _DecisionSpec
) -> list[str]:
    reasons: list[str] = []
    label = spec.candidate_id
    if decision.source != spec.source or decision.candidate_id != spec.candidate_id:
        reasons.append(f"decision identity mismatch {label}")
    if decision.workbook_sha256 != spec.workbook_sha256:
        reasons.append(f"workbook mismatch {label}")
    if decision.decision != spec.decision:
        reasons.append(f"opcode mismatch {label}")
    if decision.review_status != ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED:
        reasons.append(f"review status mismatch {label}")
    if decision.final_canonical != spec.final_canonical:
        reasons.append(f"final_canonical mismatch {label}")
    if decision.display_name != "":
        reasons.append(f"decision display_name mismatch {label}")
    if decision.merge_target_candidate_id != spec.merge_target_candidate_id:
        reasons.append(f"merge target mismatch {label}")
    if decision.result_entity_id != spec.result_entity_id:
        reasons.append(f"result_entity mismatch {label}")
    return reasons


def _alias_inventory_reasons(state: _Loaded, *, expected_empty: bool) -> list[str]:
    if not expected_empty:
        return []
    reasons: list[str] = []
    if state.aliases.get(RETIRED_ENTITY_ID, ()):
        reasons.append("entity 103 has aliases")
    if state.aliases.get(SURVIVOR_ENTITY_ID, ()):
        reasons.append("entity 655 has aliases")
    return reasons


def _occurrence_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    retired_ids = state.occurrence_ids_by_entity.get(RETIRED_ENTITY_ID, ())
    survivor_ids = state.occurrence_ids_by_entity.get(SURVIVOR_ENTITY_ID, ())
    if retired_ids:
        reasons.append("entity 103 has occurrence pins")
    if survivor_ids != OCCURRENCE_IDS:
        reasons.append("entity 655 occurrence pins drifted")
    pinned = ArchiveItemEntityOccurrence.objects.filter(pk__in=OCCURRENCE_IDS)
    by_id = {row.pk: row for row in pinned}
    for occurrence_id in OCCURRENCE_IDS:
        row = by_id.get(occurrence_id)
        if row is None:
            reasons.append(f"missing occurrence {occurrence_id}")
            continue
        if (
            row.entity_id != SURVIVOR_ENTITY_ID
            or row.resolution_status
            != ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
        ):
            reasons.append(
                f"occurrence {occurrence_id} is not a resolved pin of entity 655"
            )
        expected = _OCCURRENCE_IDENTITIES_BY_ID[occurrence_id]
        actual = (
            row.archive_item_id,
            row.text_kind,
            row.source_text_sha256,
            row.normalization_version,
            row.normalized_surface,
            row.occurrence_ordinal,
        )
        if actual != _identity_fields(expected):
            reasons.append(f"occurrence {occurrence_id} identity drifted")
    return reasons


def _identity_fields(
    identity: OccurrenceIdentity,
) -> tuple[int, str, str, str, str, int]:
    return (
        identity.archive_item_id,
        identity.text_kind,
        identity.source_text_sha256,
        identity.normalization_version,
        identity.normalized_surface,
        identity.occurrence_ordinal,
    )


def _pointer_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    if state.decisions_pointing_at.get(RETIRED_ENTITY_ID, ()) != (
        APPROVE_DECISION_ID,
        MERGE_DECISION_ID,
    ):
        reasons.append("result_entity pointers to 103 drifted")
    if state.decisions_pointing_at.get(SURVIVOR_ENTITY_ID, ()) != (
        SURVIVOR_DECISION_ID,
    ):
        reasons.append("result_entity pointers to 655 drifted")
    return reasons


def _applied_shape_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    sha = manifest_sha256()
    approve = state.corrections[APPROVE_DECISION_ID]
    merge = state.corrections[MERGE_DECISION_ID]
    if SURVIVOR_DECISION_ID in state.corrections:
        reasons.append("EC0852 has an unexpected correction")
    reasons.extend(
        _correction_shape(
            approve,
            candidate_id="EC0414",
            retired_entity_id=RETIRED_ENTITY_ID,
            sha=sha,
        )
    )
    reasons.extend(
        _correction_shape(
            merge,
            candidate_id="EC0181",
            retired_entity_id=None,
            sha=sha,
        )
    )
    return reasons


def _correction_shape(
    correction: NonPersonEntityCorrection,
    *,
    candidate_id: str,
    retired_entity_id: int | None,
    sha: str,
) -> list[str]:
    reasons: list[str] = []
    if correction.effective_entity_id != SURVIVOR_ENTITY_ID:
        reasons.append(f"{candidate_id} does not resolve to entity 655")
    if correction.retired_entity_id != retired_entity_id:
        reasons.append(f"{candidate_id} retirement shape drifted")
    if (
        correction.canonical_name != SURVIVOR_NAME
        or correction.display_name != ""
        or correction.entity_subtype
        != NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY
    ):
        reasons.append(f"{candidate_id} correction metadata drifted")
    if (
        correction.approval_reference != APPROVAL_REFERENCE
        or correction.manifest_sha256 != sha
    ):
        reasons.append(f"{candidate_id} correction provenance drifted")
    return reasons


def _short_name_collision_reasons() -> list[str]:
    """Exact name hits other than entity 103's own canonical block the alias."""

    allowed = [RETIRED_ENTITY_ID]
    entity_hit = (
        NonPersonEntity.objects.filter(canonical_name=SHORT_NAME)
        .exclude(pk__in=allowed)
        .exists()
    )
    display_hit = (
        NonPersonEntity.objects.filter(display_name=SHORT_NAME)
        .exclude(pk__in=allowed)
        .exists()
    )
    alias_hit = NonPersonEntityAlias.objects.filter(name=SHORT_NAME).exists()
    if entity_hit or display_hit or alias_hit:
        return ["short name collision; alias not recommended"]
    return []


def _entity_is(
    entity: NonPersonEntity,
    *,
    canonical: str,
    display: str,
    entity_type: str,
    subtype: str,
) -> bool:
    return (
        entity.canonical_name == canonical
        and entity.display_name == display
        and entity.entity_type == entity_type
        and entity.entity_subtype == subtype
    )


def _write(state: _Loaded) -> None:
    from documents.services.non_person_entity_staff import create_non_person_alias

    survivor = state.entities[SURVIVOR_ENTITY_ID]
    retired = state.entities[RETIRED_ENTITY_ID]
    create_non_person_alias(survivor, name=SHORT_NAME, kind=ALIAS_KIND)
    sha = manifest_sha256()
    _insert_correction(
        decision=state.decisions[APPROVE_DECISION_ID],
        retired_entity=retired,
        sha=sha,
    )
    _insert_correction(
        decision=state.decisions[MERGE_DECISION_ID],
        retired_entity=None,
        sha=sha,
    )


def _insert_correction(
    *,
    decision: ReviewedNonPersonEntityDecision,
    retired_entity: NonPersonEntity | None,
    sha: str,
) -> None:
    NonPersonEntityCorrection.objects.create(
        decision=decision,
        effective_entity_id=SURVIVOR_ENTITY_ID,
        retired_entity=retired_entity,
        canonical_name=SURVIVOR_NAME,
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
        approval_reference=APPROVAL_REFERENCE,
        manifest_sha256=sha,
    )


def _snapshots() -> _Snapshots:
    return _Snapshots(
        decisions=_decision_digest(),
        occurrences=_occurrence_digest(),
        proposals=_proposal_digest(),
        candidates=_candidate_digest(),
        entities=_entity_digest(),
        aliases=_alias_rows(),
        corrections=_correction_rows(),
    )


def _require_unchanged(before: _Snapshots) -> None:
    if _decision_digest() != before.decisions:
        raise ShadowMergeApplyError(("historical decision rows changed",))
    if _occurrence_digest() != before.occurrences:
        raise ShadowMergeApplyError(("occurrence pins changed",))
    if (
        _proposal_digest() != before.proposals
        or _candidate_digest() != before.candidates
    ):
        raise ShadowMergeApplyError(("occurrence proposals changed",))
    if _entity_digest() != before.entities:
        raise ShadowMergeApplyError(("entity metadata changed",))
    _require_alias_insert(before.aliases)
    _require_correction_insert(before.corrections)


def _require_alias_insert(before: tuple[tuple[int, int, str, str], ...]) -> None:
    after = _alias_rows()
    before_ids = {row[0] for row in before}
    created = [row for row in after if row[0] not in before_ids]
    kept = [row for row in after if row[0] in before_ids]
    if kept != list(before) or len(created) != 1:
        raise ShadowMergeApplyError(("existing aliases changed",))
    if created[0][1:] != (SURVIVOR_ENTITY_ID, SHORT_NAME, ALIAS_KIND):
        raise ShadowMergeApplyError(
            ("alias insert was not the short name on entity 655",)
        )


def _require_correction_insert(
    before: tuple[tuple[int, int, int, int | None, str, str, str, str, str], ...],
) -> None:
    after = _correction_rows()
    before_ids = {row[0] for row in before}
    created = [row for row in after if row[0] not in before_ids]
    kept = [row for row in after if row[0] in before_ids]
    if kept != list(before) or len(created) != 2:
        raise ShadowMergeApplyError(("existing corrections changed",))
    by_decision = {row[1]: row for row in created}
    approve = by_decision.get(APPROVE_DECISION_ID)
    merge = by_decision.get(MERGE_DECISION_ID)
    if approve is None or merge is None:
        raise ShadowMergeApplyError(("corrections were not EC0414 and EC0181",))
    if approve[2:] != _expected_correction_tail(RETIRED_ENTITY_ID):
        raise ShadowMergeApplyError(("EC0414 did not retire entity 103 onto 655",))
    if merge[2:] != _expected_correction_tail(None):
        raise ShadowMergeApplyError(("EC0181 retired an entity",))
    retirements = NonPersonEntityCorrection.objects.filter(
        retired_entity_id=RETIRED_ENTITY_ID
    ).count()
    if retirements != 1:
        raise ShadowMergeApplyError(("entity 103 was retired more than once",))


def _expected_correction_tail(
    retired_entity_id: int | None,
) -> tuple[int, int | None, str, str, str, str, str]:
    return (
        SURVIVOR_ENTITY_ID,
        retired_entity_id,
        SURVIVOR_NAME,
        "",
        NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
        APPROVAL_REFERENCE,
        manifest_sha256(),
    )


def _decision_digest() -> str:
    rows = list(
        ReviewedNonPersonEntityDecision.objects.order_by("pk").values_list(
            "id",
            "source",
            "candidate_id",
            "decision",
            "review_status",
            "final_canonical",
            "display_name",
            "merge_target_candidate_id",
            "result_entity_id",
            "workbook_sha256",
            "note",
            "contextual_surfaces",
        )
    )
    return _digest(rows)


def _occurrence_digest() -> str:
    rows = list(
        ArchiveItemEntityOccurrence.objects.order_by("pk").values_list(
            "id",
            "archive_item_id",
            "text_kind",
            "source_text_sha256",
            "normalization_version",
            "normalized_surface",
            "occurrence_ordinal",
            "resolution_status",
            "entity_id",
            "decision_id",
            "matched_text",
        )
    )
    return _digest(rows)


def _proposal_digest() -> str:
    rows = list(
        NonPersonEntityOccurrenceProposal.objects.order_by("pk").values_list(
            "id",
            "archive_item_id",
            "text_kind",
            "source_text_sha256",
            "normalization_version",
            "normalized_surface",
            "occurrence_ordinal",
            "matched_text",
        )
    )
    return _digest(rows)


def _candidate_digest() -> str:
    rows = list(
        NonPersonEntityOccurrenceCandidate.objects.order_by("pk").values_list(
            "id",
            "proposal_id",
            "candidate_entity_id",
            "status",
            "resolved_entity_id",
        )
    )
    return _digest(rows)


def _entity_digest() -> str:
    rows = list(
        NonPersonEntity.objects.order_by("pk").values_list(
            "id",
            "canonical_name",
            "display_name",
            "entity_type",
            "entity_subtype",
        )
    )
    return _digest(rows)


def _alias_rows() -> tuple[tuple[int, int, str, str], ...]:
    return tuple(
        NonPersonEntityAlias.objects.order_by("pk").values_list(
            "id",
            "entity_id",
            "name",
            "kind",
        )
    )


def _correction_rows() -> tuple[
    tuple[int, int, int, int | None, str, str, str, str, str], ...
]:
    return tuple(
        NonPersonEntityCorrection.objects.order_by("pk").values_list(
            "id",
            "decision_id",
            "effective_entity_id",
            "retired_entity_id",
            "canonical_name",
            "display_name",
            "entity_subtype",
            "approval_reference",
            "manifest_sha256",
        )
    )


def _digest(rows) -> str:
    payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
