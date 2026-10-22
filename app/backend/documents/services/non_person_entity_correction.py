"""One approved non-person correction manifest.

Historical ``ReviewedNonPersonEntityDecision`` rows stay as the six contracts
applied them. A correction row records the current entity. This is not a
general correction framework: the manifest below is the only payload.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_review_preflight import (
    THIRD_DECISION_SOURCE,
    V6_DECISION_SOURCE,
)

APPROVAL_REFERENCE = (
    "blocked_resolution 2026-10-21; review_decisions.json; "
    "british_intelligence_source_review.md"
)
CONFIRM_TOKEN = "APPLY_NON_PERSON_CORRECTIONS_2026_10_21"
HOSPITAL_CANONICAL = "בית החולים קסר אל־עיני"
EXCLUDED_CANDIDATES = ("EC0385", "EC0489", "EC0524", "EC0562")
COVERED_BY_EC0200 = ("EC0204",)

_FUAD = "הפקולטה לרפואה קאסר אל־עיני, אוניברסיטת פואד הראשון"
_FACULTY = "הפקולטה לרפואה קסר אל־עיני"
_SCHOOL = "בית הספר היסודי היהודי בניהול סלומון כהן באלכסנדריה"
_DAKER = "דאקר (קהיר)"
_DAHER = "א־דאהר"


def _metadata(
    candidate_id: str,
    *,
    final_canonical: str,
    before_canonical: str,
    before_display: str,
    before_type: str,
    before_subtype: str,
    canonical_name: str,
    display_name: str,
    entity_subtype: str,
    aliases: tuple[tuple[str, str], ...] = (),
) -> dict[str, object]:
    return {
        "kind": "metadata",
        "candidate_id": candidate_id,
        "source": V6_DECISION_SOURCE,
        "expected_decision": ReviewedNonPersonEntityDecision.Decision.APPROVE,
        "expected_final_canonical": final_canonical,
        "expected_merge_target_candidate_id": "",
        "expected_result_canonical_name": before_canonical,
        "expected_result_display_name": before_display,
        "expected_result_entity_type": before_type,
        "expected_result_entity_subtype": before_subtype,
        "expected_aliases": [list(pair) for pair in aliases],
        "canonical_name": canonical_name,
        "display_name": display_name,
        "entity_subtype": entity_subtype,
    }


OPERATIONS: tuple[dict[str, object], ...] = (
    _metadata(
        "EC0019",
        final_canonical="כיכר המלכה פרידה בקהיר",
        before_canonical="כיכר המלכה פרידה בקהיר",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype="",
        canonical_name="פרידה (קהיר)",
        display_name="",
        entity_subtype="",
    ),
    _metadata(
        "EC0200",
        final_canonical="מנהלת המבצעים המיוחדים הבריטית (SOE)",
        before_canonical="מנהלת המבצעים המיוחדים הבריטית (SOE)",
        before_display="",
        before_type=NonPersonEntity.EntityType.ORGANIZATION,
        before_subtype=NonPersonEntity.EntitySubtype.INTELLIGENCE_ORGANIZATION,
        canonical_name="שירות המודיעין הבריטי במצרים",
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.INTELLIGENCE_ORGANIZATION,
    ),
    _metadata(
        "EC0214",
        final_canonical="Banque Belge et Internationale en Égypte",
        before_canonical="Banque Belge et Internationale en Égypte",
        before_display="",
        before_type=NonPersonEntity.EntityType.ORGANIZATION,
        before_subtype=NonPersonEntity.EntitySubtype.OTHER,
        canonical_name="Banque Belge et Internationale en Égypte",
        display_name="הבנק הבלגי והבין־לאומי במצרים",
        entity_subtype=NonPersonEntity.EntitySubtype.OTHER,
        aliases=(("B. Belge", NonPersonEntityAlias.Kind.ABBREVIATION),),
    ),
    _metadata(
        "EC0232",
        final_canonical="שורה (קהיר)",
        before_canonical="שורה (קהיר)",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype=NonPersonEntity.EntitySubtype.NEIGHBORHOOD,
        canonical_name="שורה (קהיר)",
        display_name="",
        entity_subtype="",
        aliases=(("Chourah", NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT),),
    ),
    _metadata(
        "EC0532",
        final_canonical="À l'Américaine – סולימאן פאשה",
        before_canonical="À l'Américaine – סולימאן פאשה",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype=NonPersonEntity.EntitySubtype.OTHER,
        canonical_name="“Américaine” — סולימאן פאשה, קהיר",
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.OTHER,
    ),
    _metadata(
        "EC0537",
        final_canonical="רחוב אל־ח'ליג אל־מצרי",
        before_canonical="רחוב אל־ח'ליג אל־מצרי",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype=NonPersonEntity.EntitySubtype.OTHER,
        canonical_name="רחוב ח׳ליק (קהיר)",
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.OTHER,
        aliases=(("Rue Khaliq", NonPersonEntityAlias.Kind.OCR_VARIANT),),
    ),
    _metadata(
        "EC0538",
        final_canonical="רחוב אל־מנסי בקהיר",
        before_canonical="רחוב אל־מנסי בקהיר",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype=NonPersonEntity.EntitySubtype.OTHER,
        canonical_name="רחוב מנסי (קהיר)",
        display_name="",
        entity_subtype=NonPersonEntity.EntitySubtype.OTHER,
        aliases=(("Rue Mansi", NonPersonEntityAlias.Kind.LANGUAGE_VARIANT),),
    ),
    _metadata(
        "EC0550",
        final_canonical="קסרקטיני בולוק ניזאם בעבאסיה",
        before_canonical="קסרקטיני בולוק ניזאם בעבאסיה",
        before_display="",
        before_type=NonPersonEntity.EntityType.PLACE,
        before_subtype=NonPersonEntity.EntitySubtype.MILITARY_OR_SECURITY_SITE,
        canonical_name="בולוק ניזאם (עבאסיה, קהיר)",
        display_name="",
        entity_subtype="",
    ),
    {
        "kind": "create_effective",
        "candidate_id": "EC0251",
        "source": V6_DECISION_SOURCE,
        "expected_decision": ReviewedNonPersonEntityDecision.Decision.SKIP,
        "expected_final_canonical": "",
        "expected_merge_target_candidate_id": "",
        "expected_result_entity": None,
        "canonical_name": _SCHOOL,
        "display_name": "",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.SCHOOL,
    },
    {
        "kind": "create_effective",
        "candidate_id": "EC0521",
        "source": V6_DECISION_SOURCE,
        "expected_decision": ReviewedNonPersonEntityDecision.Decision.MERGE,
        "expected_final_canonical": _DAHER,
        "expected_merge_target_candidate_id": "EC0520",
        "expected_result_canonical_name": _DAHER,
        "expected_result_display_name": "",
        "expected_result_entity_type": NonPersonEntity.EntityType.PLACE,
        "expected_result_entity_subtype": NonPersonEntity.EntitySubtype.NEIGHBORHOOD,
        "expected_aliases": [
            ["Daker", NonPersonEntityAlias.Kind.OCR_VARIANT],
            ["Daber", NonPersonEntityAlias.Kind.OCR_VARIANT],
        ],
        "owner_candidate_id": "EC0520",
        "owner_source": V6_DECISION_SOURCE,
        "canonical_name": _DAKER,
        "display_name": "",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": "",
    },
    {
        "kind": "retire_duplicate",
        "candidate_id": "EC0455",
        "source": V6_DECISION_SOURCE,
        "expected_decision": ReviewedNonPersonEntityDecision.Decision.APPROVE,
        "expected_final_canonical": _FUAD,
        "expected_merge_target_candidate_id": "",
        "expected_result_canonical_name": _FUAD,
        "expected_result_display_name": "",
        "expected_result_entity_type": NonPersonEntity.EntityType.PLACE,
        "expected_result_entity_subtype": NonPersonEntity.EntitySubtype.SCHOOL,
        "expected_aliases": [],
        "survivor_candidate_id": "EC0225",
        "survivor_source": THIRD_DECISION_SOURCE,
        "survivor_expected_canonical_name": _FACULTY,
        "survivor_expected_display_name": "",
        "survivor_expected_entity_type": NonPersonEntity.EntityType.PLACE,
        "survivor_expected_entity_subtype": NonPersonEntity.EntitySubtype.SCHOOL,
        "alias_name": _FUAD,
        "alias_kind": NonPersonEntityAlias.Kind.SPELLING_VARIANT,
        "also_pointed_at_survivor": ("EC0252", "EC0253"),
    },
)


def manifest_document() -> dict[str, object]:
    """Stable description of the only approved correction payload."""

    return {
        "approval_reference": APPROVAL_REFERENCE,
        "excluded_candidates": list(EXCLUDED_CANDIDATES),
        "covered_without_a_separate_row": list(COVERED_BY_EC0200),
        "operations": [dict(operation) for operation in OPERATIONS],
    }


def manifest_sha256() -> str:
    payload = json.dumps(
        manifest_document(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CorrectionApplyError(Exception):
    """The manifest does not match the database. No partial correction remains."""

    def __init__(self, reasons: tuple[str, ...] | list[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclass(frozen=True)
class CorrectionAssessment:
    status: str
    reasons: tuple[str, ...]
    entity_delta: int
    alias_delta: int
    correction_delta: int
    decision_delta: int = 0
    occurrence_delta: int = 0


@dataclass(frozen=True)
class CorrectionApplyResult:
    applied: bool
    assessment: CorrectionAssessment


@dataclass
class _Loaded:
    decisions: dict[tuple[str, str], ReviewedNonPersonEntityDecision]
    entities: dict[int, NonPersonEntity]
    aliases: dict[int, tuple[tuple[str, str], ...]]
    corrections: dict[int, NonPersonEntityCorrection]
    occurrence_counts: dict[int, int]
    hospital_id: int | None


def retired_entity_id_subquery():
    """Entity ids hidden from active name indexes."""

    return NonPersonEntityCorrection.objects.exclude(
        retired_entity_id=None
    ).values("retired_entity_id")


def exclude_retired_entities(queryset):
    return queryset.exclude(pk__in=retired_entity_id_subquery())


def effective_entity_for_decision(
    decision: ReviewedNonPersonEntityDecision,
) -> NonPersonEntity | None:
    """Current entity for a decision. Historical ``result_entity`` is the fallback."""

    try:
        correction = decision.correction
    except NonPersonEntityCorrection.DoesNotExist:
        return decision.result_entity
    return correction.effective_entity


def redirect_target_for_retired_entity(entity_id: int) -> int | None:
    """Effective entity id when this row was retired. Ambiguous rows do not redirect."""

    targets = list(
        NonPersonEntityCorrection.objects.filter(retired_entity_id=entity_id)
        .values_list("effective_entity_id", flat=True)[:2]
    )
    if len(targets) != 1 or targets[0] is None or targets[0] == entity_id:
        return None
    return targets[0]


def dry_run_non_person_entity_corrections() -> CorrectionAssessment:
    """Read the manifest against current rows. Does not write."""

    return _assess(_load(for_update=False))


def apply_non_person_entity_corrections(*, confirm: str) -> CorrectionApplyResult:
    """Apply the manifest in one transaction, or leave the database unchanged."""

    if confirm != CONFIRM_TOKEN:
        raise CorrectionApplyError(("confirmation token rejected",))
    with transaction.atomic():
        state = _load(for_update=True)
        assessment = _assess(state)
        if assessment.status == "blocked":
            raise CorrectionApplyError(assessment.reasons)
        if assessment.status == "already_applied":
            return CorrectionApplyResult(applied=False, assessment=assessment)
        decision_digest = _decision_digest()
        occurrence_digest = _occurrence_digest()
        alias_rows = _alias_rows()
        hospital = _hospital_snapshot(state.hospital_id)
        _write(state)
        if _decision_digest() != decision_digest:
            raise CorrectionApplyError(("historical decision rows changed",))
        if _occurrence_digest() != occurrence_digest:
            raise CorrectionApplyError(("occurrence pins changed",))
        if _hospital_snapshot(state.hospital_id) != hospital:
            raise CorrectionApplyError(("hospital entity changed",))
        _require_only_new_alias(alias_rows, state)
        return CorrectionApplyResult(applied=True, assessment=assessment)


def _wanted_keys() -> set[tuple[str, str]]:
    keys = {(op["source"], op["candidate_id"]) for op in OPERATIONS}
    keys.add((V6_DECISION_SOURCE, "EC0520"))
    keys.add((THIRD_DECISION_SOURCE, "EC0225"))
    keys.add((THIRD_DECISION_SOURCE, "EC0252"))
    keys.add((THIRD_DECISION_SOURCE, "EC0253"))
    return keys


def _load(*, for_update: bool) -> _Loaded:
    identity = Q()
    for source, candidate_id in _wanted_keys():
        identity |= Q(source=source, candidate_id=candidate_id)
    queryset = ReviewedNonPersonEntityDecision.objects.filter(identity)
    if for_update:
        queryset = queryset.select_for_update(of=("self",))
    decisions = {
        (row.source, row.candidate_id): row
        for row in queryset.order_by("pk")
    }
    entity_ids = {
        row.result_entity_id
        for row in decisions.values()
        if row.result_entity_id is not None
    }
    hospital = (
        NonPersonEntity.objects.filter(canonical_name=HOSPITAL_CANONICAL)
        .order_by("pk")
        .first()
    )
    if hospital is not None:
        entity_ids.add(hospital.pk)
    entity_queryset = NonPersonEntity.objects.filter(pk__in=entity_ids)
    if for_update and entity_ids:
        entity_queryset = entity_queryset.select_for_update(of=("self",))
    entities = {row.pk: row for row in entity_queryset.order_by("pk")}
    aliases: dict[int, list[tuple[str, str]]] = {entity_id: [] for entity_id in entity_ids}
    for alias in NonPersonEntityAlias.objects.filter(entity_id__in=entity_ids).order_by(
        "name", "id"
    ):
        aliases[alias.entity_id].append((alias.name, alias.kind))
    decision_ids = [row.pk for row in decisions.values()]
    corrections = {
        row.decision_id: row
        for row in NonPersonEntityCorrection.objects.filter(decision_id__in=decision_ids)
    }
    occurrence_counts = {entity_id: 0 for entity_id in entity_ids}
    for entity_id in ArchiveItemEntityOccurrence.objects.filter(
        entity_id__in=entity_ids
    ).values_list("entity_id", flat=True):
        occurrence_counts[entity_id] = occurrence_counts.get(entity_id, 0) + 1
    return _Loaded(
        decisions=decisions,
        entities=entities,
        aliases={key: tuple(value) for key, value in aliases.items()},
        corrections=corrections,
        occurrence_counts=occurrence_counts,
        hospital_id=None if hospital is None else hospital.pk,
    )


def _assess(state: _Loaded) -> CorrectionAssessment:
    reasons: list[str] = []
    for source, candidate_id in sorted(_wanted_keys()):
        if (source, candidate_id) not in state.decisions:
            reasons.append(f"missing decision {candidate_id}")
    if reasons:
        return _blocked(reasons)
    applied = _applied_reasons(state)
    historical = _historical_reasons(state)
    present = [
        operation["candidate_id"]
        for operation in OPERATIONS
        if state.decisions[(operation["source"], operation["candidate_id"])].pk
        in state.corrections
    ]
    if not applied and len(present) == len(OPERATIONS):
        decision_reasons = _replay_decision_reasons(state)
        if decision_reasons:
            return _blocked(decision_reasons)
        return CorrectionAssessment(
            status="already_applied",
            reasons=(),
            entity_delta=0,
            alias_delta=0,
            correction_delta=0,
        )
    if present:
        reasons.append(
            "partial corrections: " + ", ".join(str(candidate) for candidate in present)
        )
    reasons.extend(historical)
    if reasons:
        return _blocked(reasons)
    return CorrectionAssessment(
        status="ready",
        reasons=(),
        entity_delta=2,
        alias_delta=1,
        correction_delta=len(OPERATIONS),
    )


def _blocked(reasons: list[str]) -> CorrectionAssessment:
    return CorrectionAssessment(
        status="blocked",
        reasons=tuple(reasons),
        entity_delta=0,
        alias_delta=0,
        correction_delta=0,
    )


def _applied_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    sha = manifest_sha256()
    for operation in OPERATIONS:
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        correction = state.corrections.get(decision.pk)
        if correction is None:
            reasons.append(f"correction missing {operation['candidate_id']}")
            continue
        if (
            correction.manifest_sha256 != sha
            or correction.approval_reference != APPROVAL_REFERENCE
        ):
            reasons.append(f"correction provenance mismatch {operation['candidate_id']}")
        reasons.extend(_applied_operation(state, operation, decision, correction))
    return reasons


def _applied_operation(state, operation, decision, correction) -> list[str]:
    reasons: list[str] = []
    candidate_id = operation["candidate_id"]
    entity = correction.effective_entity
    if operation["kind"] == "retire_duplicate":
        canonical = operation["survivor_expected_canonical_name"]
        display = operation["survivor_expected_display_name"]
        subtype = operation["survivor_expected_entity_subtype"]
        entity_type = operation["survivor_expected_entity_type"]
    else:
        canonical = operation["canonical_name"]
        display = operation["display_name"]
        subtype = operation["entity_subtype"]
        entity_type = operation.get(
            "entity_type",
            operation.get("expected_result_entity_type"),
        )
    if not _entity_is(
        entity,
        canonical=canonical,
        display=display,
        subtype=subtype,
        entity_type=entity_type,
    ):
        reasons.append(f"effective entity mismatch {candidate_id}")
    if operation["kind"] == "metadata":
        if correction.retired_entity_id is not None:
            reasons.append(f"unexpected retirement {candidate_id}")
        if correction.effective_entity_id != decision.result_entity_id:
            reasons.append(f"metadata entity moved {candidate_id}")
    elif operation["kind"] == "create_effective":
        if correction.retired_entity_id is not None:
            reasons.append(f"unexpected retirement {candidate_id}")
        if operation["candidate_id"] == "EC0251" and decision.result_entity_id is not None:
            reasons.append("EC0251 historical entity is no longer null")
        if operation["candidate_id"] == "EC0521":
            owner = state.decisions[(operation["owner_source"], operation["owner_candidate_id"])]
            if decision.result_entity_id != owner.result_entity_id:
                reasons.append("EC0521 no longer points at al-Daher")
            if correction.effective_entity_id == decision.result_entity_id:
                reasons.append("EC0521 effective entity is al-Daher")
    elif operation["kind"] == "retire_duplicate":
        survivor = state.decisions[
            (operation["survivor_source"], operation["survivor_candidate_id"])
        ]
        if correction.effective_entity_id != survivor.result_entity_id:
            reasons.append("EC0455 does not resolve to the faculty survivor")
        if correction.retired_entity_id != decision.result_entity_id:
            reasons.append("EC0455 did not retire its historical entity")
        if not _has_alias(
            correction.effective_entity_id,
            operation["alias_name"],
            operation["alias_kind"],
        ):
            reasons.append("Fuad I alias missing")
    return reasons


def _historical_reasons(state: _Loaded) -> list[str]:
    reasons: list[str] = []
    for operation in OPERATIONS:
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        reasons.extend(_decision_shape(decision, operation))
        if operation["kind"] == "metadata":
            entity = _require_entity(state, decision, operation["candidate_id"])
            if entity is None:
                reasons.append(f"missing entity {operation['candidate_id']}")
                continue
            reasons.extend(_historical_entity(state, entity, operation))
            reasons.extend(
                _name_collision(
                    name=operation["canonical_name"],
                    allowed_entity_ids={entity.pk},
                    label=operation["candidate_id"],
                )
            )
            if operation["display_name"]:
                reasons.extend(
                    _name_collision(
                        name=operation["display_name"],
                        allowed_entity_ids={entity.pk},
                        label=f"{operation['candidate_id']} display",
                    )
                )
        elif operation["kind"] == "create_effective":
            if operation["candidate_id"] == "EC0251":
                if decision.result_entity_id is not None:
                    reasons.append("EC0251 already has a result entity")
            else:
                entity = _require_entity(state, decision, "EC0521")
                owner = state.decisions[
                    (operation["owner_source"], operation["owner_candidate_id"])
                ]
                if (
                    owner.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE
                    or owner.final_canonical != _DAHER
                    or entity is None
                    or owner.result_entity_id != decision.result_entity_id
                ):
                    reasons.append("EC0521 is not merged into EC0520's entity")
                elif entity is not None:
                    reasons.extend(_historical_entity(state, entity, operation))
            reasons.extend(
                _name_collision(
                    name=operation["canonical_name"],
                    allowed_entity_ids=set(),
                    label=operation["candidate_id"],
                )
            )
        elif operation["kind"] == "retire_duplicate":
            entity = _require_entity(state, decision, "EC0455")
            survivor_decision = state.decisions[
                (operation["survivor_source"], operation["survivor_candidate_id"])
            ]
            survivor = state.entities.get(survivor_decision.result_entity_id)
            if survivor_decision.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE:
                reasons.append("faculty survivor decision is not APPROVE")
            if entity is None or survivor is None:
                reasons.append("faculty entities missing")
                continue
            if survivor.pk == entity.pk:
                reasons.append("faculty survivor is the duplicate")
            reasons.extend(_historical_entity(state, entity, operation))
            if not _entity_is(
                survivor,
                canonical=operation["survivor_expected_canonical_name"],
                display=operation["survivor_expected_display_name"],
                subtype=operation["survivor_expected_entity_subtype"],
                entity_type=operation["survivor_expected_entity_type"],
            ):
                reasons.append("faculty survivor does not match")
            if state.aliases.get(survivor.pk, ()):
                reasons.append("faculty survivor already has aliases")
            for candidate_id in operation["also_pointed_at_survivor"]:
                other = state.decisions[(THIRD_DECISION_SOURCE, candidate_id)]
                if (
                    other.decision != ReviewedNonPersonEntityDecision.Decision.MERGE
                    or other.merge_target_candidate_id != "EC0225"
                    or other.result_entity_id != survivor.pk
                ):
                    reasons.append(f"{candidate_id} does not point at the faculty survivor")
            reasons.extend(
                _name_collision(
                    name=operation["alias_name"],
                    allowed_entity_ids={entity.pk},
                    label="Fuad I alias",
                )
            )
    for operation in OPERATIONS:
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        entity_id = decision.result_entity_id
        if entity_id is not None and state.occurrence_counts.get(entity_id, 0):
            reasons.append(f"occurrence pins present {operation['candidate_id']}")
    survivor = state.decisions[(THIRD_DECISION_SOURCE, "EC0225")].result_entity_id
    if survivor is not None and state.occurrence_counts.get(survivor, 0):
        reasons.append("occurrence pins present on the faculty survivor")
    return reasons


def _replay_decision_reasons(state: _Loaded) -> list[str]:
    """Historical decision columns that must still match after a correction.

    Approved entity names are not compared here. Those live on the entity and
    the correction row. This checks opcode, ``final_canonical``, merge target,
    and the result-entity pointer the contract originally stored.
    """

    reasons: list[str] = []
    for operation in OPERATIONS:
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        reasons.extend(_decision_shape(decision, operation))
        reasons.extend(_replay_result_entity(state, operation, decision))
    owner = state.decisions[(V6_DECISION_SOURCE, "EC0520")]
    merged = state.decisions[(V6_DECISION_SOURCE, "EC0521")]
    if (
        owner.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE
        or owner.final_canonical != _DAHER
        or owner.result_entity_id != merged.result_entity_id
    ):
        reasons.append("EC0520 historical decision changed")
    survivor = state.decisions[(THIRD_DECISION_SOURCE, "EC0225")]
    if (
        survivor.decision != ReviewedNonPersonEntityDecision.Decision.APPROVE
        or survivor.final_canonical != _FACULTY
    ):
        reasons.append("EC0225 historical decision changed")
    for candidate_id in ("EC0252", "EC0253"):
        other = state.decisions[(THIRD_DECISION_SOURCE, candidate_id)]
        if (
            other.decision != ReviewedNonPersonEntityDecision.Decision.MERGE
            or other.merge_target_candidate_id != "EC0225"
            or other.result_entity_id != survivor.result_entity_id
        ):
            reasons.append(f"{candidate_id} historical decision changed")
    return reasons


def _replay_result_entity(state, operation, decision) -> list[str]:
    candidate_id = operation["candidate_id"]
    if operation["kind"] == "metadata":
        if decision.result_entity_id is None:
            return [f"target entity mismatch {candidate_id}"]
        return []
    if candidate_id == "EC0251":
        if decision.result_entity_id is not None:
            return ["target entity mismatch EC0251"]
        return []
    if candidate_id == "EC0521":
        owner = state.decisions[(operation["owner_source"], operation["owner_candidate_id"])]
        if decision.result_entity_id != owner.result_entity_id:
            return ["target entity mismatch EC0521"]
        return []
    survivor = state.decisions[
        (operation["survivor_source"], operation["survivor_candidate_id"])
    ]
    if decision.result_entity_id is None or decision.result_entity_id == survivor.result_entity_id:
        return ["target entity mismatch EC0455"]
    return []


def _decision_shape(decision, operation) -> list[str]:
    reasons = []
    if decision.decision != operation["expected_decision"]:
        reasons.append(f"opcode mismatch {operation['candidate_id']}")
    if decision.final_canonical != operation["expected_final_canonical"]:
        reasons.append(f"final_canonical mismatch {operation['candidate_id']}")
    if (
        decision.merge_target_candidate_id
        != operation["expected_merge_target_candidate_id"]
    ):
        reasons.append(f"merge target mismatch {operation['candidate_id']}")
    return reasons


def _historical_entity(state, entity, operation) -> list[str]:
    reasons = []
    if not _entity_is(
        entity,
        canonical=operation["expected_result_canonical_name"],
        display=operation["expected_result_display_name"],
        subtype=operation["expected_result_entity_subtype"],
        entity_type=operation["expected_result_entity_type"],
    ):
        reasons.append(f"current entity mismatch {operation['candidate_id']}")
    expected = tuple(tuple(pair) for pair in operation.get("expected_aliases", []))
    if tuple(sorted(state.aliases.get(entity.pk, ()))) != tuple(sorted(expected)):
        reasons.append(f"alias mismatch {operation['candidate_id']}")
    return reasons


def _name_collision(*, name: str, allowed_entity_ids: set[int], label: str) -> list[str]:
    if not name:
        return []
    excluded = list(allowed_entity_ids) or [-1]
    entity_hit = (
        NonPersonEntity.objects.filter(canonical_name=name)
        .exclude(pk__in=excluded)
        .exists()
    )
    display_hit = (
        NonPersonEntity.objects.filter(display_name=name)
        .exclude(pk__in=excluded)
        .exists()
    )
    alias_hit = (
        NonPersonEntityAlias.objects.filter(name=name)
        .exclude(entity_id__in=excluded)
        .exists()
    )
    if entity_hit or display_hit or alias_hit:
        return [f"name collision {label}"]
    return []


def _require_entity(state, decision, candidate_id):
    if decision.result_entity_id is None:
        return None
    return state.entities.get(decision.result_entity_id)


def _entity_is(entity, *, canonical, display, subtype, entity_type) -> bool:
    return (
        entity.canonical_name == canonical
        and entity.display_name == display
        and entity.entity_subtype == subtype
        and entity.entity_type == entity_type
    )


def _has_alias(entity_id: int, name: str, kind: str) -> bool:
    return NonPersonEntityAlias.objects.filter(
        entity_id=entity_id,
        name=name,
        kind=kind,
    ).exists()


def _write(state: _Loaded) -> None:
    from documents.services.non_person_entity_staff import (
        create_non_person_alias,
        update_non_person_entity,
    )

    sha = manifest_sha256()
    for operation in OPERATIONS:
        if operation["kind"] != "metadata":
            continue
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        entity = state.entities[decision.result_entity_id]
        update_non_person_entity(
            entity,
            canonical_name=operation["canonical_name"],
            display_name=operation["display_name"],
            entity_type=entity.entity_type,
            entity_subtype=operation["entity_subtype"],
        )
        _insert_correction(
            decision=decision,
            effective_entity=entity,
            retired_entity=None,
            canonical_name=operation["canonical_name"],
            display_name=operation["display_name"],
            entity_subtype=operation["entity_subtype"],
            sha=sha,
        )
    for operation in OPERATIONS:
        if operation["kind"] != "create_effective":
            continue
        decision = state.decisions[(operation["source"], operation["candidate_id"])]
        created = NonPersonEntity.objects.create(
            canonical_name=operation["canonical_name"],
            display_name=operation["display_name"],
            entity_type=operation["entity_type"],
            entity_subtype=operation["entity_subtype"],
        )
        _insert_correction(
            decision=decision,
            effective_entity=created,
            retired_entity=None,
            canonical_name=operation["canonical_name"],
            display_name=operation["display_name"],
            entity_subtype=operation["entity_subtype"],
            sha=sha,
        )
    retire = next(operation for operation in OPERATIONS if operation["kind"] == "retire_duplicate")
    decision = state.decisions[(retire["source"], retire["candidate_id"])]
    survivor_decision = state.decisions[
        (retire["survivor_source"], retire["survivor_candidate_id"])
    ]
    survivor = state.entities[survivor_decision.result_entity_id]
    retired = state.entities[decision.result_entity_id]
    create_non_person_alias(
        survivor,
        name=retire["alias_name"],
        kind=retire["alias_kind"],
    )
    _insert_correction(
        decision=decision,
        effective_entity=survivor,
        retired_entity=retired,
        canonical_name=retire["survivor_expected_canonical_name"],
        display_name=retire["survivor_expected_display_name"],
        entity_subtype=retire["survivor_expected_entity_subtype"],
        sha=sha,
    )


def _insert_correction(
    *,
    decision,
    effective_entity,
    retired_entity,
    canonical_name,
    display_name,
    entity_subtype,
    sha,
) -> None:
    NonPersonEntityCorrection.objects.create(
        decision=decision,
        effective_entity=effective_entity,
        retired_entity=retired_entity,
        canonical_name=canonical_name,
        display_name=display_name,
        entity_subtype=entity_subtype,
        approval_reference=APPROVAL_REFERENCE,
        manifest_sha256=sha,
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


def _alias_rows() -> tuple[tuple[int, int, str, str], ...]:
    return tuple(
        NonPersonEntityAlias.objects.order_by("pk").values_list(
            "id", "entity_id", "name", "kind"
        )
    )


def _require_only_new_alias(before: tuple[tuple[int, int, str, str], ...], state: _Loaded) -> None:
    after = _alias_rows()
    before_ids = {row[0] for row in before}
    created = [row for row in after if row[0] not in before_ids]
    kept = [row for row in after if row[0] in before_ids]
    if kept != list(before) or len(created) != 1:
        raise CorrectionApplyError(("existing aliases changed",))
    retire = next(operation for operation in OPERATIONS if operation["kind"] == "retire_duplicate")
    survivor_id = state.decisions[
        (retire["survivor_source"], retire["survivor_candidate_id"])
    ].result_entity_id
    if created[0][1:] != (survivor_id, retire["alias_name"], retire["alias_kind"]):
        raise CorrectionApplyError(("alias insert was not the Fuad I name",))


def _hospital_snapshot(hospital_id: int | None) -> tuple | None:
    if hospital_id is None:
        return None
    row = NonPersonEntity.objects.filter(pk=hospital_id).values_list(
        "id",
        "canonical_name",
        "display_name",
        "entity_type",
        "entity_subtype",
    ).first()
    return row


def _digest(rows) -> str:
    payload = json.dumps(rows, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
