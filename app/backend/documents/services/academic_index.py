"""Academic-index state transitions and append-only decision events.

Callers outside this module cannot ``save`` topic, label, relation, or
association rows. Each approved or retracted transition inserts one
``AcademicIndexDecisionEvent`` in the same transaction as the state change.

Graph cycles and hierarchy-versus-related conflicts are enforced here.
A SQL check constraint cannot see a transitive cycle. Direct SQL can still
write rows and is not intercepted.

Approved hierarchy walks ignore ``RETRACTED`` edges. Depth is capped.

A non-blank ``operation_key`` is an idempotency token. The event snapshot
stores the canonical request. The same key with the same request returns the
original event. The same key with a different request raises ``ValidationError``.
The unique constraint ``uniq_acad_decision_operation_key`` is the concurrent
authority. Direct SQL can still write rows and is not intercepted.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicLabel,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.services.academic_index_guard import academic_index_state_write

MAX_TOPIC_GRAPH_DEPTH = 32
_APPROVAL_ACTIONS = {
    AcademicIndexDecisionEvent.Action.IMPORT,
    AcademicIndexDecisionEvent.Action.APPROVE,
}
_OPERATION_KEY_CONSTRAINT = "uniq_acad_decision_operation_key"
_OPERATION_KEY_CONFLICT = {
    "operation_key": "This operation key was already used for a different decision."
}


@dataclass(frozen=True)
class AcademicIndexWrite:
    event: AcademicIndexDecisionEvent | None
    created: bool


def approved_broader_relations():
    return AcademicTopicRelation.objects.filter(
        relation_kind=AcademicTopicRelation.Kind.BROADER,
        status=AcademicTopicRelation.Status.APPROVED,
    )


def approved_related_relations():
    return AcademicTopicRelation.objects.filter(
        relation_kind=AcademicTopicRelation.Kind.RELATED,
        status=AcademicTopicRelation.Status.APPROVED,
    )


def approved_ancestor_ids(topic_id: int) -> set[int]:
    """Topics reached by walking approved broader edges, not including ``topic_id``."""

    return _walk_parents(topic_id, _approved_parent_map())


def approved_descendant_ids(topic_id: int) -> set[int]:
    """Topics that list ``topic_id`` as an approved ancestor. Not including itself."""

    return _walk_children(topic_id, _approved_child_map())


def direct_approved_item_ids(topic_id: int) -> set[int]:
    return set(
        ArchiveItemTopicAssociation.objects.filter(
            topic_id=topic_id,
            status=ArchiveItemTopicAssociation.Status.APPROVED,
        ).values_list("archive_item_id", flat=True)
    )


def inherited_approved_item_ids(topic_id: int) -> set[int]:
    """Approved direct items of descendants, excluding items stored on ``topic_id``."""

    descendants = approved_descendant_ids(topic_id)
    if not descendants:
        return set()
    inherited = set(
        ArchiveItemTopicAssociation.objects.filter(
            topic_id__in=descendants,
            status=ArchiveItemTopicAssociation.Status.APPROVED,
        ).values_list("archive_item_id", flat=True)
    )
    return inherited - direct_approved_item_ids(topic_id)


def create_topic(
    *,
    stable_key: str,
    preferred_label: str,
    scope_note: str = "",
    is_public_candidate: bool = False,
    action: str = AcademicIndexDecisionEvent.Action.IMPORT,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    """Insert a topic. An approved publication candidate requires IMPORT or APPROVE."""

    key = stable_key.strip()
    label = preferred_label.strip()
    note = scope_note.strip()
    request = _decision_request(
        "create_topic",
        action=action,
        stable_key=key,
        preferred_label=label,
        scope_note=note,
        is_public_candidate=is_public_candidate,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        existing = AcademicTopic.objects.select_for_update().filter(stable_key=key).first()
        if existing is not None:
            if existing.preferred_label != label or existing.scope_note != note:
                raise ValidationError(
                    {"stable_key": "This stable key already identifies a different topic."}
                )
            if existing.is_public_candidate != is_public_candidate:
                raise ValidationError(
                    {
                        "stable_key": (
                            "This stable key already exists in a different approval state."
                        )
                    }
                )
            if not operation_key:
                return AcademicIndexWrite(
                    event=_latest_event(
                        AcademicIndexDecisionEvent.TargetKind.TOPIC,
                        existing.pk,
                    ),
                    created=False,
                )
        else:
            existing = AcademicTopic(
                stable_key=key,
                preferred_label=label,
                scope_note=note,
                is_public_candidate=False,
            )
            existing.save()
            if not is_public_candidate and not operation_key:
                return AcademicIndexWrite(event=None, created=True)
        if is_public_candidate and not existing.is_public_candidate:
            _require_approval_action(action)
            before = {"is_public_candidate": False}
            existing.is_public_candidate = True
            existing.save()
        elif is_public_candidate:
            _require_approval_action(action)
            before = _topic_state(existing)
        else:
            if action in _APPROVAL_ACTIONS:
                raise ValidationError(
                    {
                        "action": (
                            "A keyed unpublished topic uses DEFER or REJECT. "
                            "IMPORT and APPROVE record an approved topic."
                        )
                    }
                )
            before = None if existing.pk and not operation_key else _topic_state(existing)
        event = _append_event(
            action=action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC,
            target_id=existing.pk,
            before=before,
            after=_topic_state(existing),
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def set_topic_public_candidate(
    topic: AcademicTopic,
    *,
    is_public_candidate: bool,
    action: str,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    request = _decision_request(
        "set_topic_public_candidate",
        action=action,
        topic_stable_key=topic.stable_key,
        is_public_candidate=is_public_candidate,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        locked = AcademicTopic.objects.select_for_update().get(pk=topic.pk)
        already = locked.is_public_candidate == is_public_candidate
        if is_public_candidate:
            _require_approval_action(action)
        elif action != AcademicIndexDecisionEvent.Action.RETRACT:
            raise ValidationError({"action": "Clearing a topic candidate requires RETRACT."})
        before = _topic_state(locked)
        if not already:
            locked.is_public_candidate = is_public_candidate
            locked.save()
        elif not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(AcademicIndexDecisionEvent.TargetKind.TOPIC, locked.pk),
                created=False,
            )
        event = _append_event(
            action=action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC,
            target_id=locked.pk,
            before=before,
            after=_topic_state(locked),
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def add_alternate_label(
    topic: AcademicTopic,
    label: str,
    *,
    language: str = "he",
    action: str = AcademicIndexDecisionEvent.Action.APPROVE,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    text = label.strip()
    language_code = language.strip()
    request = _decision_request(
        "add_alternate_label",
        action=action,
        topic_stable_key=topic.stable_key,
        label=text,
        language=language_code,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        _require_approval_action(action)
        existing = AcademicTopicLabel.objects.filter(topic=topic, label=text).first()
        if existing is not None and existing.language != language_code:
            raise ValidationError(
                {"language": "This alternate label is already stored in another language."}
            )
        if existing is not None and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.TOPIC_LABEL,
                    existing.pk,
                ),
                created=False,
            )
        if existing is None:
            existing = AcademicTopicLabel(topic=topic, label=text, language=language_code)
            existing.save()
        event = _append_event(
            action=action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC_LABEL,
            target_id=existing.pk,
            before=None,
            after={
                "topic_stable_key": topic.stable_key,
                "label": existing.label,
                "language": existing.language,
            },
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def approve_broader_relation(
    narrower: AcademicTopic,
    broader: AcademicTopic,
    *,
    action: str = AcademicIndexDecisionEvent.Action.APPROVE,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    if narrower.pk == broader.pk:
        raise ValidationError({"broader": "A topic cannot be its own broader topic."})
    request = _decision_request(
        "approve_broader_relation",
        action=action,
        narrower_stable_key=narrower.stable_key,
        broader_stable_key=broader.stable_key,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        _lock_all_topics()
        replay = _matching_operation(operation_key, request)
        if replay is not None:
            return AcademicIndexWrite(event=replay, created=False)
        _require_approval_action(action)
        existing = AcademicTopicRelation.objects.filter(
            relation_kind=AcademicTopicRelation.Kind.BROADER,
            narrower=narrower,
            broader=broader,
        ).first()
        already = (
            existing is not None and existing.status == AcademicTopicRelation.Status.APPROVED
        )
        if already and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                    existing.pk,
                ),
                created=False,
            )
        if not already:
            _assert_broader_allowed(
                narrower_id=narrower.pk,
                broader_id=broader.pk,
                ignore_relation_id=existing.pk if existing is not None else None,
            )
            before = _relation_state(existing) if existing is not None else None
            if existing is None:
                row = AcademicTopicRelation(
                    relation_kind=AcademicTopicRelation.Kind.BROADER,
                    status=AcademicTopicRelation.Status.APPROVED,
                    narrower=narrower,
                    broader=broader,
                )
                row.save()
            else:
                row = existing
                row.status = AcademicTopicRelation.Status.APPROVED
                row.save()
        else:
            row = existing
            before = _relation_state(row)
        event = _append_event(
            action=action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
            target_id=row.pk,
            before=before,
            after=_relation_state(row),
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        if not already:
            row.decision_event = event
            row.save()
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def approve_related_relation(
    left: AcademicTopic,
    right: AcademicTopic,
    *,
    action: str = AcademicIndexDecisionEvent.Action.APPROVE,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    """Store one canonical related edge. Endpoint order does not matter."""

    low, high = _canonical_related_pair(left, right)
    request = _decision_request(
        "approve_related_relation",
        action=action,
        topic_low_stable_key=low.stable_key,
        topic_high_stable_key=high.stable_key,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        _lock_all_topics()
        replay = _matching_operation(operation_key, request)
        if replay is not None:
            return AcademicIndexWrite(event=replay, created=False)
        _require_approval_action(action)
        existing = AcademicTopicRelation.objects.filter(
            relation_kind=AcademicTopicRelation.Kind.RELATED,
            topic_low=low,
            topic_high=high,
        ).first()
        already = (
            existing is not None and existing.status == AcademicTopicRelation.Status.APPROVED
        )
        if already and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                    existing.pk,
                ),
                created=False,
            )
        if not already:
            _assert_related_allowed(low.pk, high.pk)
            before = _relation_state(existing) if existing is not None else None
            if existing is None:
                row = AcademicTopicRelation(
                    relation_kind=AcademicTopicRelation.Kind.RELATED,
                    status=AcademicTopicRelation.Status.APPROVED,
                    topic_low=low,
                    topic_high=high,
                )
                row.save()
            else:
                row = existing
                row.status = AcademicTopicRelation.Status.APPROVED
                row.save()
        else:
            row = existing
            before = _relation_state(row)
        event = _append_event(
            action=action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
            target_id=row.pk,
            before=before,
            after=_relation_state(row),
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        if not already:
            row.decision_event = event
            row.save()
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def retract_relation(
    relation: AcademicTopicRelation,
    *,
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    request = _relation_endpoint_request(
        "retract_relation",
        relation,
        action=AcademicIndexDecisionEvent.Action.RETRACT,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        _lock_all_topics()
        replay = _matching_operation(operation_key, request)
        if replay is not None:
            return AcademicIndexWrite(event=replay, created=False)
        row = AcademicTopicRelation.objects.select_for_update().get(pk=relation.pk)
        already = row.status == AcademicTopicRelation.Status.RETRACTED
        if already and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                    row.pk,
                ),
                created=False,
            )
        before = _relation_state(row)
        if not already:
            row.status = AcademicTopicRelation.Status.RETRACTED
            row.save()
        event = _append_event(
            action=AcademicIndexDecisionEvent.Action.RETRACT,
            target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
            target_id=row.pk,
            before=before,
            after=_relation_state(row),
            specification_sha256="",
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        if not already:
            row.decision_event = event
            row.save()
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def approve_item_topic(
    archive_item,
    topic: AcademicTopic,
    relevance: str,
    *,
    action: str = AcademicIndexDecisionEvent.Action.APPROVE,
    specification_sha256: str = "",
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    """Approve one direct association. Does not copy the row onto ancestors."""

    if relevance not in {
        ArchiveItemTopicAssociation.Relevance.CENTRAL,
        ArchiveItemTopicAssociation.Relevance.SECONDARY,
    }:
        raise ValidationError({"relevance": "Relevance must be CENTRAL or SECONDARY."})
    request = _decision_request(
        "approve_item_topic",
        action=action,
        archive_item_id=archive_item.pk,
        topic_stable_key=topic.stable_key,
        relevance=relevance,
        specification_sha256=specification_sha256,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        _require_approval_action(action)
        existing = (
            ArchiveItemTopicAssociation.objects.select_for_update()
            .filter(archive_item=archive_item, topic=topic)
            .first()
        )
        already = (
            existing is not None
            and existing.status == ArchiveItemTopicAssociation.Status.APPROVED
            and existing.relevance == relevance
        )
        if already and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
                    existing.pk,
                ),
                created=False,
            )
        before = _association_state(existing) if existing is not None else None
        event_action = action
        if (
            existing is not None
            and existing.status == ArchiveItemTopicAssociation.Status.APPROVED
            and existing.relevance != relevance
        ):
            event_action = AcademicIndexDecisionEvent.Action.SUPERSEDE
        if not already:
            if existing is None:
                row = ArchiveItemTopicAssociation(
                    archive_item=archive_item,
                    topic=topic,
                    relevance=relevance,
                    status=ArchiveItemTopicAssociation.Status.APPROVED,
                )
                row.save()
            else:
                row = existing
                row.relevance = relevance
                row.status = ArchiveItemTopicAssociation.Status.APPROVED
                row.save()
        else:
            row = existing
        event = _append_event(
            action=event_action,
            target_kind=AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
            target_id=row.pk,
            before=before,
            after=_association_state(row),
            specification_sha256=specification_sha256,
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        if not already:
            row.decision_event = event
            row.save()
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def retract_item_topic(
    association: ArchiveItemTopicAssociation,
    *,
    operation_key: str = "",
    batch_id: str = "",
    evidence_kind: str = AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
    evidence_note: str = "",
    reason: str = "",
    actor=None,
) -> AcademicIndexWrite:
    request = _decision_request(
        "retract_item_topic",
        action=AcademicIndexDecisionEvent.Action.RETRACT,
        archive_item_id=association.archive_item_id,
        topic_stable_key=association.topic.stable_key,
        relevance=association.relevance,
        batch_id=batch_id,
        evidence_kind=evidence_kind,
        evidence_note=evidence_note,
        reason=reason,
        actor_id=_actor_id(actor),
    )

    def mutate() -> AcademicIndexWrite:
        row = ArchiveItemTopicAssociation.objects.select_for_update().get(pk=association.pk)
        already = row.status == ArchiveItemTopicAssociation.Status.RETRACTED
        if already and not operation_key:
            return AcademicIndexWrite(
                event=_latest_event(
                    AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
                    row.pk,
                ),
                created=False,
            )
        before = _association_state(row)
        if not already:
            row.status = ArchiveItemTopicAssociation.Status.RETRACTED
            row.save()
        event = _append_event(
            action=AcademicIndexDecisionEvent.Action.RETRACT,
            target_kind=AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
            target_id=row.pk,
            before=before,
            after=_association_state(row),
            specification_sha256="",
            operation_key=operation_key,
            batch_id=batch_id,
            evidence_kind=evidence_kind,
            evidence_note=evidence_note,
            reason=reason,
            actor=actor,
            request=request,
        )
        if not already:
            row.decision_event = event
            row.save()
        return AcademicIndexWrite(event=event, created=True)

    return _run_idempotent(operation_key, request, mutate)


def _require_approval_action(action: str) -> None:
    if action not in _APPROVAL_ACTIONS:
        raise ValidationError(
            {"action": "Entering an approved state requires IMPORT or APPROVE."}
        )


def _lock_all_topics() -> None:
    list(AcademicTopic.objects.select_for_update().order_by("pk").values_list("pk", flat=True))


def _approved_parent_map() -> dict[int, set[int]]:
    parent_map: dict[int, set[int]] = {}
    rows = approved_broader_relations().values_list("narrower_id", "broader_id")
    for narrower_id, broader_id in rows:
        parent_map.setdefault(narrower_id, set()).add(broader_id)
    return parent_map


def _approved_child_map() -> dict[int, set[int]]:
    child_map: dict[int, set[int]] = {}
    rows = approved_broader_relations().values_list("broader_id", "narrower_id")
    for broader_id, narrower_id in rows:
        child_map.setdefault(broader_id, set()).add(narrower_id)
    return child_map


def _walk_parents(topic_id: int, parent_map: dict[int, set[int]]) -> set[int]:
    found: set[int] = set()
    stack = [(topic_id, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_TOPIC_GRAPH_DEPTH:
            raise ValidationError("Academic topic graph exceeded the depth cap.")
        for parent_id in parent_map.get(current, ()):
            if parent_id in found:
                continue
            found.add(parent_id)
            stack.append((parent_id, depth + 1))
    return found


def _walk_children(topic_id: int, child_map: dict[int, set[int]]) -> set[int]:
    found: set[int] = set()
    stack = [(topic_id, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > MAX_TOPIC_GRAPH_DEPTH:
            raise ValidationError("Academic topic graph exceeded the depth cap.")
        for child_id in child_map.get(current, ()):
            if child_id in found:
                continue
            found.add(child_id)
            stack.append((child_id, depth + 1))
    return found


def _assert_broader_allowed(
    *,
    narrower_id: int,
    broader_id: int,
    ignore_relation_id: int | None,
) -> None:
    parent_map = _approved_parent_map()
    if ignore_relation_id is not None:
        ignored = (
            AcademicTopicRelation.objects.filter(pk=ignore_relation_id)
            .values_list("narrower_id", "broader_id")
            .first()
        )
        if ignored is not None:
            parents = parent_map.get(ignored[0])
            if parents is not None:
                parents.discard(ignored[1])
    if narrower_id in _walk_parents(broader_id, parent_map):
        raise ValidationError({"broader": "That broader edge would create a cycle."})
    low, high = sorted((narrower_id, broader_id))
    if AcademicTopicRelation.objects.filter(
        relation_kind=AcademicTopicRelation.Kind.RELATED,
        status=AcademicTopicRelation.Status.APPROVED,
        topic_low_id=low,
        topic_high_id=high,
    ).exists():
        raise ValidationError(
            {"broader": "An approved related edge already connects these topics."}
        )


def _assert_related_allowed(low_id: int, high_id: int) -> None:
    ancestors_low = approved_ancestor_ids(low_id)
    ancestors_high = approved_ancestor_ids(high_id)
    if (
        high_id in ancestors_low
        or low_id in ancestors_high
        or low_id in approved_descendant_ids(high_id)
    ):
        raise ValidationError(
            {
                "relation_kind": (
                    "Related edges cannot connect a topic to its ancestor or descendant."
                )
            }
        )


def _canonical_related_pair(
    left: AcademicTopic,
    right: AcademicTopic,
) -> tuple[AcademicTopic, AcademicTopic]:
    if left.pk == right.pk:
        raise ValidationError({"topic_low": "A topic cannot be related to itself."})
    if left.pk < right.pk:
        return left, right
    return right, left


def _run_idempotent(operation_key: str, request: dict, mutate):
    """Run one keyed decision. Integrity conflicts are resolved after rollback."""

    try:
        with academic_index_state_write(), transaction.atomic():
            existing = _matching_operation(operation_key, request)
            if existing is not None:
                return AcademicIndexWrite(event=existing, created=False)
            return mutate()
    except IntegrityError as exc:
        return _resolve_operation_key_integrity(exc, operation_key, request)


def _decision_request(operation: str, **fields) -> dict:
    """Stable request identity. Strings are stripped. SHA text is lowercase."""

    request: dict = {"operation": operation}
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, str):
            value = value.strip()
            if key == "specification_sha256":
                value = value.lower()
        request[key] = value
    return request


def _relation_endpoint_request(operation: str, relation: AcademicTopicRelation, **fields) -> dict:
    """Natural key of one edge. Related order is the stored low/high pair."""

    return _decision_request(
        operation,
        relation_kind=relation.relation_kind,
        narrower_stable_key=_stable_key(relation.narrower),
        broader_stable_key=_stable_key(relation.broader),
        topic_low_stable_key=_stable_key(relation.topic_low),
        topic_high_stable_key=_stable_key(relation.topic_high),
        **fields,
    )


def _stable_key(topic: AcademicTopic | None) -> str:
    if topic is None:
        return ""
    return topic.stable_key


def _actor_id(actor) -> int | None:
    if actor is None:
        return None
    return getattr(actor, "pk", None)


def _append_event(
    *,
    action: str,
    target_kind: str,
    target_id: int,
    before: dict | None,
    after: dict,
    specification_sha256: str,
    operation_key: str,
    batch_id: str,
    evidence_kind: str,
    evidence_note: str,
    reason: str,
    actor,
    request: dict,
) -> AcademicIndexDecisionEvent:
    event = AcademicIndexDecisionEvent(
        action=action,
        target_kind=target_kind,
        target_id=target_id,
        batch_id=_decision_request("batch", batch_id=batch_id)["batch_id"],
        actor=actor,
        evidence_kind=evidence_kind,
        evidence_note=_decision_request("note", evidence_note=evidence_note)["evidence_note"],
        reason=_decision_request("reason", reason=reason)["reason"],
        specification_sha256=_decision_request("sha", specification_sha256=specification_sha256)[
            "specification_sha256"
        ],
        operation_key=operation_key,
        snapshot={
            "target_kind": target_kind,
            "target_id": target_id,
            "before": before,
            "after": after,
            "request": request,
        },
    )
    event.save()
    return event


def _matching_operation(
    operation_key: str,
    request: dict,
) -> AcademicIndexDecisionEvent | None:
    """Return the stored event when the key and request match.

    A blank key is not an idempotency token. A stored key with a different
    request raises. The row is locked when it exists so a concurrent replay
    waits for the transaction that inserted it.
    """

    if not operation_key:
        return None
    existing = (
        AcademicIndexDecisionEvent.objects.select_for_update()
        .filter(operation_key=operation_key)
        .first()
    )
    if existing is None:
        return None
    stored = existing.snapshot.get("request") if isinstance(existing.snapshot, dict) else None
    if stored != request:
        raise ValidationError(_OPERATION_KEY_CONFLICT)
    return existing


def _resolve_operation_key_integrity(
    exc: IntegrityError,
    operation_key: str,
    request: dict,
) -> AcademicIndexWrite:
    """Apply the unique operation key after a concurrent insert wins."""

    if not operation_key:
        raise exc
    existing = AcademicIndexDecisionEvent.objects.filter(operation_key=operation_key).first()
    if existing is None:
        if _OPERATION_KEY_CONSTRAINT in _integrity_text(exc):
            raise ValidationError(_OPERATION_KEY_CONFLICT) from exc
        raise exc
    stored = existing.snapshot.get("request") if isinstance(existing.snapshot, dict) else None
    if stored == request:
        return AcademicIndexWrite(event=existing, created=False)
    raise ValidationError(_OPERATION_KEY_CONFLICT) from exc


def _integrity_text(exc: IntegrityError) -> str:
    parts = [str(exc)]
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        parts.append(str(cause))
    return " ".join(parts)


def _latest_event(target_kind: str, target_id: int) -> AcademicIndexDecisionEvent | None:
    return (
        AcademicIndexDecisionEvent.objects.filter(
            target_kind=target_kind,
            target_id=target_id,
        )
        .order_by("-id")
        .first()
    )


def _topic_state(topic: AcademicTopic) -> dict:
    return {
        "id": topic.pk,
        "stable_key": topic.stable_key,
        "preferred_label": topic.preferred_label,
        "scope_note": topic.scope_note,
        "is_public_candidate": topic.is_public_candidate,
    }


def _relation_state(relation: AcademicTopicRelation | None) -> dict | None:
    if relation is None:
        return None
    return {
        "id": relation.pk,
        "relation_kind": relation.relation_kind,
        "status": relation.status,
        "narrower_id": relation.narrower_id,
        "broader_id": relation.broader_id,
        "topic_low_id": relation.topic_low_id,
        "topic_high_id": relation.topic_high_id,
    }


def _association_state(association: ArchiveItemTopicAssociation | None) -> dict | None:
    if association is None:
        return None
    return {
        "id": association.pk,
        "archive_item_id": association.archive_item_id,
        "topic_id": association.topic_id,
        "relevance": association.relevance,
        "status": association.status,
    }
