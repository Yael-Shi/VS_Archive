"""Dry-run and apply for the approved Academic Index pilot seed.

Writes only through ``documents.services.academic_index``. The allow-list is
``AcademicTopic``, ``AcademicTopicLabel`` (none in this seed),
``AcademicTopicRelation``, ``ArchiveItemTopicAssociation``, and
``AcademicIndexDecisionEvent``. Archive items are read to check that the
seeded ids exist. This module does not write authors, people, entities,
occurrences, categories, events, tags, documents, or private metadata.

Default inspection writes nothing. Apply requires the confirmation token
and a separate owner-authorization token. A provisional or placeholder
evidence note is rejected before any database read or write, including when
those tokens are present. A repeated apply of the same seed returns without
calling the state service, so it appends no decision event.

Existing academic-index rows that are not the exact seed abort the apply.
Rows are not skipped and are not overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicLabel,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.academic_index_seed_v2 import (
    ALTERNATE_LABELS,
    APPLY_CONFIRM_TOKEN,
    APPROVED_EVIDENCE_NOTE,
    ASSOCIATIONS,
    BROADER_EDGES,
    EXPECTED_ASSOCIATION_COUNT,
    EXPECTED_BROADER_COUNT,
    EXPECTED_DISTINCT_ITEM_COUNT,
    EXPECTED_ISSUER_ROW_COUNT,
    EXPECTED_RELATED_COUNT,
    EXPECTED_TOPIC_COUNT,
    OWNER_AUTHORIZATION_TOKEN,
    PINNED_DISTINCT_ITEM_IDS,
    RELATED_EDGES,
    SEED_REASON,
    SOURCE_SPECIFICATION_SHA256,
    SPECIFICATION_V2_DOCUMENT_SHA256,
    TOPIC_BATCH_ID,
    TOPICS,
    BroaderEdge,
    RelatedEdge,
    SeedAssociation,
    SeedTopic,
    all_operation_keys,
    association_operation_key,
    broader_operation_key,
    evidence_note_rejection,
    related_operation_key,
    seed_definition_errors,
    topic_operation_key,
)
from documents.models import ArchiveItem
from documents.services import academic_index

_EVENT_COUNT = len(all_operation_keys())


class AcademicIndexSeedError(Exception):
    """The seed was not applied. No academic-index write is committed."""


# Two-integer transaction advisory lock. It serializes seed applies only and
# does not add a schema object. Dry-run never acquires it.
_SEED_APPLY_LOCK_KEY = (20261115, 2)


@dataclass(frozen=True)
class SeedInspection:
    mode: str
    outcome: str
    missing_archive_item_ids: tuple[int, ...]
    conflicts: tuple[str, ...]
    decision_events_written: int = 0

    @property
    def summary(self) -> str:
        lines = [
            f"academic-index-seed-v2 mode={self.mode} outcome={self.outcome}",
            (
                f"topics={EXPECTED_TOPIC_COUNT} broader={EXPECTED_BROADER_COUNT} "
                f"related={EXPECTED_RELATED_COUNT} associations={EXPECTED_ASSOCIATION_COUNT} "
                f"distinct_items={EXPECTED_DISTINCT_ITEM_COUNT} "
                f"issuer_rows={EXPECTED_ISSUER_ROW_COUNT}"
            ),
            f"decision_events_written={self.decision_events_written}",
            f"source_specification_sha256={SOURCE_SPECIFICATION_SHA256}",
            f"specification_v2_document_sha256={SPECIFICATION_V2_DOCUMENT_SHA256}",
            f"evidence_note={APPROVED_EVIDENCE_NOTE}",
            "evidence_note_status=OWNER_APPROVED",
        ]
        if self.missing_archive_item_ids:
            missing = ",".join(
                str(item_id) for item_id in self.missing_archive_item_ids
            )
            lines.append(f"missing_archive_item_ids={missing}")
        for conflict in self.conflicts:
            lines.append(f"conflict: {conflict}")
        return "\n".join(lines)


@dataclass(frozen=True)
class SeedApplyResult:
    outcome: str
    decision_events_written: int
    summary: str


def inspect_approved_seed() -> SeedInspection:
    """Read the fixture and the database. Does not insert, update, or delete."""

    _require_consistent_fixture()
    missing = _missing_archive_item_ids()
    conflicts = _issuer_conflicts()
    if not _academic_tables_empty():
        conflicts.extend(_state_conflicts())
    if missing or conflicts:
        outcome = "aborted"
    elif _academic_tables_empty():
        outcome = "would_create"
    else:
        outcome = "already_applied"
    return SeedInspection(
        mode="dry-run",
        outcome=outcome,
        missing_archive_item_ids=missing,
        conflicts=tuple(conflicts),
        decision_events_written=0,
    )


def apply_approved_seed(
    *,
    confirm: str,
    owner_authorization: str,
) -> SeedApplyResult:
    """Materialize the seed in one locked transaction, or return when it is already exact.

    Authorization and the evidence-note check stay outside the transaction
    because they do not read the database. The authoritative inspection and
    the writes share one transaction and one PostgreSQL advisory lock, so a
    second identical apply waits and then observes the committed seed.
    """

    authorization_errors = _authorization_errors(
        confirm=confirm,
        owner_authorization=owner_authorization,
    )
    if authorization_errors:
        raise AcademicIndexSeedError(" ".join(authorization_errors))
    note_error = evidence_note_rejection(APPROVED_EVIDENCE_NOTE)
    if note_error:
        raise AcademicIndexSeedError(note_error)
    _require_consistent_fixture()
    outcome = "already_applied"
    written = 0
    try:
        with transaction.atomic():
            _lock_seed_apply()
            inspection = inspect_approved_seed()
            if inspection.outcome == "aborted":
                raise AcademicIndexSeedError(inspection.summary)
            if inspection.outcome == "would_create":
                before_events = AcademicIndexDecisionEvent.objects.count()
                _materialize()
                confirmed = inspect_approved_seed()
                if confirmed.outcome != "already_applied":
                    raise AcademicIndexSeedError(confirmed.summary)
                written = AcademicIndexDecisionEvent.objects.count() - before_events
                if written != _EVENT_COUNT:
                    raise AcademicIndexSeedError(
                        f"Seed apply wrote {written} decision events; expected {_EVENT_COUNT}."
                    )
                outcome = "applied"
    except AcademicIndexSeedError:
        raise
    except (ValidationError, IntegrityError) as exc:
        raise AcademicIndexSeedError(
            "Seed apply failed and was rolled back with no committed academic-index rows."
        ) from exc
    result = SeedInspection(
        mode="apply",
        outcome=outcome,
        missing_archive_item_ids=(),
        conflicts=(),
        decision_events_written=written,
    )
    return SeedApplyResult(
        outcome=outcome,
        decision_events_written=written,
        summary=result.summary,
    )


def _lock_seed_apply() -> None:
    """Hold the seed-apply advisory lock until the surrounding transaction ends."""

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)",
            [_SEED_APPLY_LOCK_KEY[0], _SEED_APPLY_LOCK_KEY[1]],
        )


def _authorization_errors(
    *,
    confirm: str,
    owner_authorization: str,
) -> list[str]:
    errors: list[str] = []
    if confirm != APPLY_CONFIRM_TOKEN:
        errors.append("Apply confirmation was rejected.")
    if owner_authorization != OWNER_AUTHORIZATION_TOKEN:
        errors.append("Owner authorization was rejected.")
    return errors


def _require_consistent_fixture() -> None:
    errors = seed_definition_errors()
    if errors:
        raise AcademicIndexSeedError(
            "Seed fixture is inconsistent: " + "; ".join(errors)
        )


def _missing_archive_item_ids() -> tuple[int, ...]:
    found = set(
        ArchiveItem.objects.filter(pk__in=PINNED_DISTINCT_ITEM_IDS).values_list(
            "pk", flat=True
        )
    )
    return tuple(
        item_id for item_id in PINNED_DISTINCT_ITEM_IDS if item_id not in found
    )


def _issuer_conflicts() -> list[str]:
    names = sorted(
        model.__name__
        for model in apps.get_app_config("documents").get_models()
        if "issuer" in model.__name__.lower()
    )
    if names:
        return ["Issuer models are present: " + ", ".join(names)]
    return []


def _academic_tables_empty() -> bool:
    return (
        not AcademicTopic.objects.exists()
        and not AcademicTopicLabel.objects.exists()
        and not AcademicTopicRelation.objects.exists()
        and not ArchiveItemTopicAssociation.objects.exists()
        and not AcademicIndexDecisionEvent.objects.exists()
    )


def _state_conflicts() -> list[str]:
    conflicts: list[str] = []
    topics = {topic.stable_key: topic for topic in AcademicTopic.objects.all()}
    expected_topic_keys = {topic.stable_key for topic in TOPICS}
    extra_topics = sorted(set(topics) - expected_topic_keys)
    missing_topics = sorted(expected_topic_keys - set(topics))
    if extra_topics:
        conflicts.append("Unexpected topics: " + ", ".join(extra_topics))
    if missing_topics:
        conflicts.append("Missing topics: " + ", ".join(missing_topics))
    for seed_topic in TOPICS:
        row = topics.get(seed_topic.stable_key)
        if row is None:
            continue
        if row.preferred_label != seed_topic.preferred_label:
            conflicts.append(
                f"{seed_topic.stable_key} preferred label does not match the seed."
            )
        if row.scope_note != "":
            conflicts.append(f"{seed_topic.stable_key} scope note is not empty.")
        if row.is_public_candidate is not True:
            conflicts.append(
                f"{seed_topic.stable_key} is not an approved public candidate."
            )
    label_count = AcademicTopicLabel.objects.count()
    if label_count or ALTERNATE_LABELS:
        conflicts.append(f"Alternate label count is {label_count}; the seed has none.")

    relations = list(
        AcademicTopicRelation.objects.select_related(
            "narrower",
            "broader",
            "topic_low",
            "topic_high",
            "decision_event",
        )
    )
    actual_broader: set[tuple[str, str]] = set()
    actual_related: list[tuple[str, str]] = []
    relations_by_broader: dict[tuple[str, str], AcademicTopicRelation] = {}
    related_row: AcademicTopicRelation | None = None
    for relation in relations:
        if relation.status != AcademicTopicRelation.Status.APPROVED:
            conflicts.append(f"Relation {relation.pk} is not APPROVED.")
            continue
        if relation.relation_kind == AcademicTopicRelation.Kind.BROADER:
            pair = (relation.narrower.stable_key, relation.broader.stable_key)
            actual_broader.add(pair)
            relations_by_broader[pair] = relation
        elif relation.relation_kind == AcademicTopicRelation.Kind.RELATED:
            actual_related.append(
                (relation.topic_low.stable_key, relation.topic_high.stable_key)
            )
            related_row = relation
        else:
            conflicts.append(f"Relation {relation.pk} has an unknown kind.")
    expected_broader = {(edge.narrower_key, edge.broader_key) for edge in BROADER_EDGES}
    if actual_broader != expected_broader or len(relations_by_broader) != len(
        actual_broader
    ):
        conflicts.append("Broader edges do not match the seed.")
    if actual_related != [("AI-05", "AI-11")]:
        conflicts.append(
            "The stored related edge is not the single AI-05 / AI-11 edge."
        )

    associations = list(
        ArchiveItemTopicAssociation.objects.select_related("topic", "decision_event")
    )
    actual_associations: dict[tuple[int, str], ArchiveItemTopicAssociation] = {}
    for association in associations:
        if association.status != ArchiveItemTopicAssociation.Status.APPROVED:
            conflicts.append(
                f"Association item {association.archive_item_id} "
                f"topic {association.topic.stable_key} is not APPROVED."
            )
            continue
        key = (association.archive_item_id, association.topic.stable_key)
        actual_associations[key] = association
    expected_associations = {
        (row.archive_item_id, row.topic_key): row.relevance for row in ASSOCIATIONS
    }
    if set(actual_associations) != set(expected_associations):
        conflicts.append("Item association pairs do not match the seed.")
    for key, relevance in expected_associations.items():
        association = actual_associations.get(key)
        if association is not None and association.relevance != relevance:
            conflicts.append(
                f"Archive item {key[0]} topic {key[1]} relevance is "
                f"{association.relevance}; the seed requires {relevance}."
            )

    events = list(AcademicIndexDecisionEvent.objects.all())
    expected_requests = _expected_requests(topics)
    events_by_key: dict[str, AcademicIndexDecisionEvent] = {}
    duplicate_operation_key = False
    for event in events:
        if not event.operation_key:
            continue
        if event.operation_key in events_by_key:
            duplicate_operation_key = True
            continue
        events_by_key[event.operation_key] = event
    if duplicate_operation_key or set(expected_requests) - set(events_by_key):
        conflicts.append("Decision events do not match the seed operation keys.")
    else:
        for operation_key, request in expected_requests.items():
            conflicts.extend(
                _event_conflicts(
                    events_by_key[operation_key],
                    request=request,
                    relations_by_broader=relations_by_broader,
                    related_row=related_row,
                    associations=actual_associations,
                    topics=topics,
                )
            )
    seed_targets = _seed_audit_targets(
        topics,
        relations_by_broader=relations_by_broader,
        related_row=related_row,
        associations=actual_associations,
    )
    for event in events:
        if event.operation_key in expected_requests:
            continue
        if (event.target_kind, event.target_id) not in seed_targets:
            conflicts.append(
                f"Unexpected decision event {event.pk} is not an audit of a seed row."
            )
    for broader_edge in BROADER_EDGES:
        relation = relations_by_broader.get(
            (broader_edge.narrower_key, broader_edge.broader_key)
        )
        if relation is None:
            continue
        conflicts.extend(
            _decision_pointer_conflicts(
                relation,
                AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                events_by_key.get(
                    broader_operation_key(
                        broader_edge.narrower_key, broader_edge.broader_key
                    )
                ),
            )
        )
    if related_row is not None and RELATED_EDGES:
        related_edge = RELATED_EDGES[0]
        conflicts.extend(
            _decision_pointer_conflicts(
                related_row,
                AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                events_by_key.get(
                    related_operation_key(related_edge.left_key, related_edge.right_key)
                ),
            )
        )
    for association_row in ASSOCIATIONS:
        association = actual_associations.get(
            (association_row.archive_item_id, association_row.topic_key)
        )
        if association is None:
            continue
        conflicts.extend(
            _decision_pointer_conflicts(
                association,
                AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
                events_by_key.get(
                    association_operation_key(
                        association_row.archive_item_id, association_row.topic_key
                    )
                ),
            )
        )
    return conflicts


def _event_conflicts(
    event: AcademicIndexDecisionEvent,
    *,
    request: dict,
    relations_by_broader: dict[tuple[str, str], AcademicTopicRelation],
    related_row: AcademicTopicRelation | None,
    associations: dict[tuple[int, str], ArchiveItemTopicAssociation],
    topics: dict[str, AcademicTopic],
) -> list[str]:
    conflicts: list[str] = []
    operation_key = event.operation_key
    if event.action != AcademicIndexDecisionEvent.Action.IMPORT:
        conflicts.append(f"{operation_key} action is not IMPORT.")
    if event.evidence_kind != AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL:
        conflicts.append(f"{operation_key} evidence kind is not OWNER_APPROVAL.")
    if event.evidence_note != APPROVED_EVIDENCE_NOTE:
        conflicts.append(
            f"{operation_key} evidence note does not match the owner-approved text."
        )
    if evidence_note_rejection(event.evidence_note or ""):
        conflicts.append(
            f"{operation_key} evidence note is provisional or a placeholder."
        )
    if event.specification_sha256 != SOURCE_SPECIFICATION_SHA256:
        conflicts.append(
            f"{operation_key} specification SHA does not match the v1 source SHA."
        )
    if event.reason != SEED_REASON:
        conflicts.append(f"{operation_key} reason does not match the seed.")
    if event.batch_id != request["batch_id"]:
        conflicts.append(f"{operation_key} batch does not match the seed.")
    snapshot = event.snapshot if isinstance(event.snapshot, dict) else None
    stored_request = snapshot.get("request") if snapshot is not None else None
    if stored_request != request:
        conflicts.append(f"{operation_key} stored request does not match the seed.")
    target = _expected_target(
        operation_key,
        relations_by_broader=relations_by_broader,
        related_row=related_row,
        associations=associations,
        topics=topics,
    )
    if target is None:
        conflicts.append(f"{operation_key} does not point at a seed row.")
        return conflicts
    target_kind, target_id = target
    if event.target_kind != target_kind or event.target_id != target_id:
        conflicts.append(f"{operation_key} target does not match the seed row.")
    return conflicts


def _seed_audit_targets(
    topics: dict[str, AcademicTopic],
    *,
    relations_by_broader: dict[tuple[str, str], AcademicTopicRelation],
    related_row: AcademicTopicRelation | None,
    associations: dict[tuple[int, str], ArchiveItemTopicAssociation],
) -> set[tuple[str, int]]:
    """Rows a later Stage 1 decision may audit without becoming a new import."""

    targets: set[tuple[str, int]] = set()
    for topic in TOPICS:
        topic_row = topics.get(topic.stable_key)
        if topic_row is not None:
            targets.add((AcademicIndexDecisionEvent.TargetKind.TOPIC, topic_row.pk))
    for relation in relations_by_broader.values():
        targets.add((AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION, relation.pk))
    if related_row is not None:
        targets.add(
            (AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION, related_row.pk)
        )
    expected_pairs = {(row.archive_item_id, row.topic_key) for row in ASSOCIATIONS}
    for key, association in associations.items():
        if key in expected_pairs:
            targets.add(
                (AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION, association.pk)
            )
    return targets


def _decision_pointer_conflicts(
    row: AcademicTopicRelation | ArchiveItemTopicAssociation,
    target_kind: str,
    import_event: AcademicIndexDecisionEvent | None,
) -> list[str]:
    """Require a pointer at the import or at a later audit of the same approved row."""

    label = f"{target_kind} {row.pk}"
    if row.decision_event_id is None:
        return [f"{label} is missing its decision event pointer."]
    pointed = row.decision_event
    if pointed is None:
        return [f"{label} decision event pointer does not exist."]
    if pointed.target_kind != target_kind or pointed.target_id != row.pk:
        return [f"{label} decision event pointer does not audit this row."]
    if import_event is not None and pointed.pk == import_event.pk:
        return []
    if pointed.action not in {
        AcademicIndexDecisionEvent.Action.IMPORT,
        AcademicIndexDecisionEvent.Action.APPROVE,
        AcademicIndexDecisionEvent.Action.SUPERSEDE,
    }:
        return [
            f"{label} decision event pointer is not an approval of the current row."
        ]
    if not _snapshot_matches_current_row(pointed, row):
        return [
            f"{label} decision event pointer does not record the current approved state."
        ]
    if import_event is not None and pointed.pk < import_event.pk:
        return [f"{label} decision event pointer is older than the import."]
    return []


def _snapshot_matches_current_row(
    event: AcademicIndexDecisionEvent,
    row: AcademicTopicRelation | ArchiveItemTopicAssociation,
) -> bool:
    snapshot = event.snapshot if isinstance(event.snapshot, dict) else None
    after = snapshot.get("after") if snapshot is not None else None
    if not isinstance(after, dict):
        return False
    if isinstance(row, ArchiveItemTopicAssociation):
        return (
            after.get("id") == row.pk
            and after.get("archive_item_id") == row.archive_item_id
            and after.get("topic_id") == row.topic_id
            and after.get("relevance") == row.relevance
            and after.get("status") == row.status
        )
    return (
        after.get("id") == row.pk
        and after.get("relation_kind") == row.relation_kind
        and after.get("status") == row.status
        and after.get("narrower_id") == row.narrower_id
        and after.get("broader_id") == row.broader_id
        and after.get("topic_low_id") == row.topic_low_id
        and after.get("topic_high_id") == row.topic_high_id
    )


def _expected_target(
    operation_key: str,
    *,
    relations_by_broader: dict[tuple[str, str], AcademicTopicRelation],
    related_row: AcademicTopicRelation | None,
    associations: dict[tuple[int, str], ArchiveItemTopicAssociation],
    topics: dict[str, AcademicTopic],
) -> tuple[str, int] | None:
    for topic in TOPICS:
        if operation_key == topic_operation_key(topic.stable_key):
            topic_row = topics.get(topic.stable_key)
            if topic_row is None:
                return None
            return (AcademicIndexDecisionEvent.TargetKind.TOPIC, topic_row.pk)
    for broader_edge in BROADER_EDGES:
        if operation_key == broader_operation_key(
            broader_edge.narrower_key, broader_edge.broader_key
        ):
            broader_row = relations_by_broader.get(
                (broader_edge.narrower_key, broader_edge.broader_key)
            )
            if broader_row is None:
                return None
            return (
                AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                broader_row.pk,
            )
    for related_edge in RELATED_EDGES:
        if operation_key == related_operation_key(
            related_edge.left_key, related_edge.right_key
        ):
            if related_row is None:
                return None
            return (
                AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                related_row.pk,
            )
    for association in ASSOCIATIONS:
        if operation_key == association_operation_key(
            association.archive_item_id, association.topic_key
        ):
            association_row = associations.get(
                (association.archive_item_id, association.topic_key)
            )
            if association_row is None:
                return None
            return (
                AcademicIndexDecisionEvent.TargetKind.ITEM_ASSOCIATION,
                association_row.pk,
            )
    return None


def _expected_requests(topics: dict[str, AcademicTopic]) -> dict[str, dict]:
    requests: dict[str, dict] = {}
    for topic in TOPICS:
        requests[topic_operation_key(topic.stable_key)] = _request(
            "create_topic",
            **_common_fields(TOPIC_BATCH_ID),
            stable_key=topic.stable_key,
            preferred_label=topic.preferred_label,
            scope_note="",
            is_public_candidate=True,
        )
    for broader_edge in BROADER_EDGES:
        requests[
            broader_operation_key(broader_edge.narrower_key, broader_edge.broader_key)
        ] = _request(
            "approve_broader_relation",
            **_common_fields(TOPIC_BATCH_ID),
            narrower_stable_key=broader_edge.narrower_key,
            broader_stable_key=broader_edge.broader_key,
        )
    low_key, high_key = _related_stable_keys(topics)
    for related_edge in RELATED_EDGES:
        requests[
            related_operation_key(related_edge.left_key, related_edge.right_key)
        ] = _request(
            "approve_related_relation",
            **_common_fields(TOPIC_BATCH_ID),
            topic_low_stable_key=low_key,
            topic_high_stable_key=high_key,
        )
    for association in ASSOCIATIONS:
        requests[
            association_operation_key(
                association.archive_item_id, association.topic_key
            )
        ] = _request(
            "approve_item_topic",
            **_common_fields(association.batch_id),
            archive_item_id=association.archive_item_id,
            topic_stable_key=association.topic_key,
            relevance=association.relevance,
        )
    return requests


def _related_stable_keys(topics: dict[str, AcademicTopic]) -> tuple[str, str]:
    edge = RELATED_EDGES[0]
    left = topics.get(edge.left_key)
    right = topics.get(edge.right_key)
    if left is None or right is None:
        return edge.left_key, edge.right_key
    if left.pk < right.pk:
        return edge.left_key, edge.right_key
    return edge.right_key, edge.left_key


def _common_fields(batch_id: str) -> dict:
    return {
        "action": AcademicIndexDecisionEvent.Action.IMPORT,
        "actor_id": None,
        "batch_id": batch_id,
        "evidence_kind": AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
        "evidence_note": APPROVED_EVIDENCE_NOTE,
        "reason": SEED_REASON,
        "specification_sha256": SOURCE_SPECIFICATION_SHA256,
    }


def _request(operation: str, **fields) -> dict:
    """Match ``academic_index._decision_request``. Divergence fails the post-check."""

    request = {"operation": operation}
    for key in sorted(fields):
        value = fields[key]
        if isinstance(value, str):
            value = value.strip()
            if key == "specification_sha256":
                value = value.lower()
        request[key] = value
    return request


def _materialize() -> None:
    item_ids = list(PINNED_DISTINCT_ITEM_IDS)
    locked_items = {
        item.pk: item
        for item in ArchiveItem.objects.select_for_update()
        .filter(pk__in=item_ids)
        .order_by("pk")
    }
    missing = [item_id for item_id in item_ids if item_id not in locked_items]
    if missing:
        raise AcademicIndexSeedError(
            "Missing ArchiveItem ids: " + ", ".join(str(item_id) for item_id in missing)
        )
    topics: dict[str, AcademicTopic] = {}
    for topic in TOPICS:
        _create_topic(topic)
        topics[topic.stable_key] = AcademicTopic.objects.get(
            stable_key=topic.stable_key
        )
    for broader_edge in BROADER_EDGES:
        _create_broader(broader_edge, topics)
    for related_edge in RELATED_EDGES:
        _create_related(related_edge, topics)
    for association in ASSOCIATIONS:
        _create_association(association, topics, locked_items)


def _audit_kwargs(*, operation_key: str, batch_id: str) -> dict:
    return {
        "action": AcademicIndexDecisionEvent.Action.IMPORT,
        "specification_sha256": SOURCE_SPECIFICATION_SHA256,
        "operation_key": operation_key,
        "batch_id": batch_id,
        "evidence_kind": AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
        "evidence_note": APPROVED_EVIDENCE_NOTE,
        "reason": SEED_REASON,
        "actor": None,
    }


def _create_topic(topic: SeedTopic) -> None:
    academic_index.create_topic(
        stable_key=topic.stable_key,
        preferred_label=topic.preferred_label,
        scope_note="",
        is_public_candidate=True,
        **_audit_kwargs(
            operation_key=topic_operation_key(topic.stable_key),
            batch_id=TOPIC_BATCH_ID,
        ),
    )


def _create_broader(edge: BroaderEdge, topics: dict[str, AcademicTopic]) -> None:
    academic_index.approve_broader_relation(
        topics[edge.narrower_key],
        topics[edge.broader_key],
        **_audit_kwargs(
            operation_key=broader_operation_key(edge.narrower_key, edge.broader_key),
            batch_id=TOPIC_BATCH_ID,
        ),
    )


def _create_related(edge: RelatedEdge, topics: dict[str, AcademicTopic]) -> None:
    academic_index.approve_related_relation(
        topics[edge.left_key],
        topics[edge.right_key],
        **_audit_kwargs(
            operation_key=related_operation_key(edge.left_key, edge.right_key),
            batch_id=TOPIC_BATCH_ID,
        ),
    )


def _create_association(
    association: SeedAssociation,
    topics: dict[str, AcademicTopic],
    items: dict[int, ArchiveItem],
) -> None:
    academic_index.approve_item_topic(
        items[association.archive_item_id],
        topics[association.topic_key],
        association.relevance,
        **_audit_kwargs(
            operation_key=association_operation_key(
                association.archive_item_id,
                association.topic_key,
            ),
            batch_id=association.batch_id,
        ),
    )
