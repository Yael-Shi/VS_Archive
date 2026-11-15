"""Isolated schema and transition tests for the academic index."""

from __future__ import annotations

import threading
from pathlib import Path

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicLabel,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    Author,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
    PhotoPerson,
    Tag,
)
from documents.services import academic_index

_SHA = "ab" * 32


def _topic(key: str, label: str | None = None) -> AcademicTopic:
    result = academic_index.create_topic(
        stable_key=key,
        preferred_label=label or key,
    )
    topic = AcademicTopic.objects.get(stable_key=key)
    assert result.created is True or topic.pk
    return topic


def _item() -> ArchiveItem:
    return ArchiveItem.objects.create(
        title="Synthetic academic-index item",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


class AcademicIndexTopicTests(TestCase):
    def test_stable_key_is_unique_and_does_not_merge_a_different_label(self):
        _topic("AI-01", "נושא")
        with self.assertRaises(ValidationError):
            academic_index.create_topic(stable_key="AI-01", preferred_label="אחר")
        self.assertEqual(AcademicTopic.objects.count(), 1)

    def test_preferred_label_is_required(self):
        with self.assertRaises(ValidationError):
            academic_index.create_topic(stable_key="AI-REQ", preferred_label="  ")

    def test_publication_candidate_defaults_to_false_without_an_approval_event(self):
        topic = _topic("AI-DEF", "טיוטה")
        self.assertFalse(topic.is_public_candidate)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 0)

    def test_alternate_label_unique_per_topic_but_shared_across_topics(self):
        left = _topic("AI-L", "שמאל")
        right = _topic("AI-R", "ימין")
        academic_index.add_alternate_label(left, "משותף")
        academic_index.add_alternate_label(right, "משותף")
        self.assertEqual(AcademicTopicLabel.objects.filter(label="משותף").count(), 2)
        again = academic_index.add_alternate_label(left, "משותף")
        self.assertFalse(again.created)
        self.assertEqual(AcademicTopicLabel.objects.filter(topic=left, label="משותף").count(), 1)
        self.assertEqual(AcademicTopic.objects.count(), 2)

    def test_direct_state_save_is_rejected(self):
        with self.assertRaises(ValidationError):
            AcademicTopic.objects.create(stable_key="AI-X", preferred_label="חסום")


class AcademicIndexHierarchyTests(TestCase):
    def test_single_and_multiple_parents(self):
        parent_a = _topic("P-A")
        parent_b = _topic("P-B")
        child = _topic("P-C")
        academic_index.approve_broader_relation(child, parent_a)
        academic_index.approve_broader_relation(child, parent_b)
        self.assertEqual(academic_index.approved_ancestor_ids(child.pk), {parent_a.pk, parent_b.pk})
        self.assertEqual(academic_index.approved_descendant_ids(parent_a.pk), {child.pk})

    def test_self_parent_is_rejected(self):
        topic = _topic("SELF")
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(topic, topic)
        self.assertEqual(AcademicTopicRelation.objects.count(), 0)

    def test_two_node_cycle_is_rejected(self):
        left = _topic("CY2-A")
        right = _topic("CY2-B")
        academic_index.approve_broader_relation(left, right)
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(right, left)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)

    def test_three_node_cycle_is_rejected(self):
        first = _topic("CY3-A")
        second = _topic("CY3-B")
        third = _topic("CY3-C")
        academic_index.approve_broader_relation(second, first)
        academic_index.approve_broader_relation(third, second)
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(first, third)
        self.assertEqual(
            academic_index.approved_ancestor_ids(third.pk),
            {first.pk, second.pk},
        )
        self.assertEqual(
            academic_index.approved_descendant_ids(first.pk),
            {second.pk, third.pk},
        )

    def test_retracted_edge_leaves_traversal_and_can_be_reapproved_once(self):
        parent = _topic("RET-P")
        child = _topic("RET-C")
        academic_index.approve_broader_relation(child, parent)
        relation = AcademicTopicRelation.objects.get()
        academic_index.retract_relation(relation)
        relation.refresh_from_db()
        self.assertEqual(relation.status, AcademicTopicRelation.Status.RETRACTED)
        self.assertEqual(academic_index.approved_ancestor_ids(child.pk), set())
        academic_index.approve_broader_relation(child, parent)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)
        relation.refresh_from_db()
        self.assertEqual(relation.status, AcademicTopicRelation.Status.APPROVED)
        self.assertEqual(academic_index.approved_ancestor_ids(child.pk), {parent.pk})
        self.assertEqual(
            AcademicIndexDecisionEvent.objects.filter(
                target_id=relation.pk,
                action=AcademicIndexDecisionEvent.Action.RETRACT,
            ).count(),
            1,
        )

    def test_queryset_update_cannot_reactivate_a_retracted_edge(self):
        parent = _topic("UPD-P")
        child = _topic("UPD-C")
        academic_index.approve_broader_relation(child, parent)
        relation = AcademicTopicRelation.objects.get()
        academic_index.retract_relation(relation)
        with self.assertRaises(ValidationError):
            AcademicTopicRelation.objects.update(status=AcademicTopicRelation.Status.APPROVED)
        relation.refresh_from_db()
        self.assertEqual(relation.status, AcademicTopicRelation.Status.RETRACTED)


class AcademicIndexRelatedTests(TestCase):
    def test_related_is_stored_once_in_canonical_order(self):
        left = _topic("REL-L")
        right = _topic("REL-R")
        high, low = (left, right) if left.pk > right.pk else (right, left)
        first = academic_index.approve_related_relation(high, low)
        second = academic_index.approve_related_relation(low, high)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        row = AcademicTopicRelation.objects.get()
        self.assertLess(row.topic_low_id, row.topic_high_id)
        self.assertIsNone(row.narrower_id)
        self.assertIsNone(row.broader_id)

    def test_self_related_is_rejected(self):
        topic = _topic("REL-SELF")
        with self.assertRaises(ValidationError):
            academic_index.approve_related_relation(topic, topic)

    def test_related_rejects_direct_and_indirect_hierarchy(self):
        root = _topic("H-ROOT")
        middle = _topic("H-MID")
        leaf = _topic("H-LEAF")
        academic_index.approve_broader_relation(middle, root)
        academic_index.approve_broader_relation(leaf, middle)
        with self.assertRaises(ValidationError):
            academic_index.approve_related_relation(middle, root)
        with self.assertRaises(ValidationError):
            academic_index.approve_related_relation(leaf, root)
        self.assertEqual(
            AcademicTopicRelation.objects.filter(
                relation_kind=AcademicTopicRelation.Kind.RELATED
            ).count(),
            0,
        )

    def test_retracted_related_is_inactive_and_reapproval_does_not_duplicate(self):
        left = _topic("RR-L")
        right = _topic("RR-R")
        academic_index.approve_related_relation(left, right)
        relation = AcademicTopicRelation.objects.get()
        academic_index.retract_relation(relation)
        self.assertEqual(academic_index.approved_related_relations().count(), 0)
        academic_index.approve_related_relation(right, left)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)
        relation.refresh_from_db()
        self.assertEqual(relation.status, AcademicTopicRelation.Status.APPROVED)

    def test_retracted_related_does_not_block_a_later_broader_edge(self):
        left = _topic("RB-L")
        right = _topic("RB-R")
        academic_index.approve_related_relation(left, right)
        academic_index.retract_relation(AcademicTopicRelation.objects.get())
        academic_index.approve_broader_relation(left, right)
        with self.assertRaises(ValidationError):
            academic_index.approve_related_relation(left, right)
        self.assertEqual(
            AcademicTopicRelation.objects.filter(
                status=AcademicTopicRelation.Status.APPROVED
            ).count(),
            1,
        )

    def test_database_rejects_a_reversed_related_pair(self):
        left = _topic("DB-L")
        right = _topic("DB-R")
        low, high = (left, right) if left.pk < right.pk else (right, left)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AcademicTopicRelation.objects.bulk_create(
                    [
                        AcademicTopicRelation(
                            relation_kind=AcademicTopicRelation.Kind.RELATED,
                            status=AcademicTopicRelation.Status.APPROVED,
                            topic_low=high,
                            topic_high=low,
                        )
                    ]
                )


class AcademicIndexAssociationTests(TestCase):
    def test_central_and_secondary_and_invalid_relevance(self):
        topic = _topic("AS-T")
        item = _item()
        academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.CENTRAL
        )
        other = _item()
        academic_index.approve_item_topic(
            other, topic, ArchiveItemTopicAssociation.Relevance.SECONDARY
        )
        with self.assertRaises(ValidationError):
            academic_index.approve_item_topic(item, topic, "ABOUT")
        self.assertFalse(hasattr(ArchiveItemTopicAssociation, "source_text_sha256"))
        self.assertNotIn("source_text_sha256", {
            field.name for field in ArchiveItemTopicAssociation._meta.fields
        })

    def test_one_approved_pair_retracts_and_reapproves_without_a_second_row(self):
        topic = _topic("AS-ONE")
        item = _item()
        first = academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.CENTRAL
        )
        second = academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.CENTRAL
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        row = ArchiveItemTopicAssociation.objects.get()
        academic_index.retract_item_topic(row)
        row.refresh_from_db()
        self.assertEqual(row.status, ArchiveItemTopicAssociation.Status.RETRACTED)
        academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.SECONDARY
        )
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), 1)
        row.refresh_from_db()
        self.assertEqual(row.status, ArchiveItemTopicAssociation.Status.APPROVED)
        self.assertEqual(row.relevance, ArchiveItemTopicAssociation.Relevance.SECONDARY)

    def test_child_association_is_not_copied_onto_the_ancestor(self):
        parent = _topic("INH-P")
        child = _topic("INH-C")
        academic_index.approve_broader_relation(child, parent)
        item = _item()
        academic_index.approve_item_topic(
            item, child, ArchiveItemTopicAssociation.Relevance.SECONDARY
        )
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), 1)
        self.assertEqual(academic_index.direct_approved_item_ids(parent.pk), set())
        self.assertEqual(academic_index.inherited_approved_item_ids(parent.pk), {item.pk})
        self.assertEqual(academic_index.direct_approved_item_ids(child.pk), {item.pk})


class AcademicIndexDecisionTests(TestCase):
    def test_event_cannot_be_edited_or_deleted(self):
        topic = _topic("EV-T")
        written = academic_index.set_topic_public_candidate(
            topic,
            is_public_candidate=True,
            action=AcademicIndexDecisionEvent.Action.APPROVE,
        )
        event = written.event
        assert event is not None
        event.reason = "changed"
        with self.assertRaises(ValidationError):
            event.save()
        with self.assertRaises(ValidationError):
            event.delete()
        with self.assertRaises(ValidationError):
            AcademicIndexDecisionEvent.objects.filter(pk=event.pk).delete()
        with self.assertRaises(ValidationError):
            AcademicIndexDecisionEvent.objects.filter(pk=event.pk).update(reason="changed")

    def test_approval_snapshot_identifies_before_and_after(self):
        topic = _topic("EV-SNAP")
        written = academic_index.set_topic_public_candidate(
            topic,
            is_public_candidate=True,
            action=AcademicIndexDecisionEvent.Action.IMPORT,
            specification_sha256=_SHA,
            operation_key="snap-1",
        )
        event = written.event
        assert event is not None
        self.assertEqual(event.target_kind, AcademicIndexDecisionEvent.TargetKind.TOPIC)
        self.assertEqual(event.target_id, topic.pk)
        self.assertEqual(event.snapshot["before"]["is_public_candidate"], False)
        self.assertEqual(event.snapshot["after"]["is_public_candidate"], True)
        self.assertEqual(event.snapshot["target_id"], topic.pk)

    def test_failed_event_rolls_back_the_state_change(self):
        narrower = _topic("RB-N")
        broader = _topic("RB-B")
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(
                narrower,
                broader,
                action=AcademicIndexDecisionEvent.Action.IMPORT,
                specification_sha256="",
            )
        self.assertEqual(AcademicTopicRelation.objects.count(), 0)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 0)

    def test_repeated_operation_key_does_not_append_a_second_event(self):
        topic = _topic("IDEM")
        parent = _topic("IDEM-P")
        first = academic_index.approve_broader_relation(
            topic,
            parent,
            operation_key="edge-1",
        )
        second = academic_index.approve_broader_relation(
            topic,
            parent,
            operation_key="edge-1",
        )
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.event.pk, second.event.pk)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 1)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)

    def test_supersede_keeps_the_earlier_event(self):
        topic = _topic("SUP")
        item = _item()
        academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.CENTRAL
        )
        academic_index.approve_item_topic(
            item, topic, ArchiveItemTopicAssociation.Relevance.SECONDARY
        )
        actions = list(
            AcademicIndexDecisionEvent.objects.order_by("id").values_list("action", flat=True)
        )
        self.assertEqual(
            actions,
            [
                AcademicIndexDecisionEvent.Action.APPROVE,
                AcademicIndexDecisionEvent.Action.SUPERSEDE,
            ],
        )


class AcademicIndexOperationKeyTests(TestCase):
    def test_new_key_then_identical_retry_keeps_one_event_and_one_row(self):
        topic = _topic("KEY-T")
        item = _item()
        kwargs = {
            "operation_key": "item-1",
            "evidence_kind": AcademicIndexDecisionEvent.EvidenceKind.METADATA,
            "evidence_note": "page 1",
            "reason": "owner",
            "batch_id": "01",
        }
        before_events = AcademicIndexDecisionEvent.objects.count()
        before_rows = ArchiveItemTopicAssociation.objects.count()
        first = academic_index.approve_item_topic(
            item,
            topic,
            ArchiveItemTopicAssociation.Relevance.CENTRAL,
            **kwargs,
        )
        self.assertTrue(first.created)
        self.assertEqual(first.event.snapshot["request"]["operation"], "approve_item_topic")
        self.assertEqual(first.event.snapshot["request"]["archive_item_id"], item.pk)
        second = academic_index.approve_item_topic(
            item,
            topic,
            ArchiveItemTopicAssociation.Relevance.CENTRAL,
            **kwargs,
        )
        self.assertFalse(second.created)
        self.assertEqual(first.event.pk, second.event.pk)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), before_events + 1)
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), before_rows + 1)

    def test_reused_key_rejects_a_different_target_action_relevance_or_evidence(self):
        topic = _topic("KEY-DIFF")
        other_topic = _topic("KEY-OTHER")
        item = _item()
        other_item = _item()
        academic_index.approve_item_topic(
            item,
            topic,
            ArchiveItemTopicAssociation.Relevance.CENTRAL,
            action=AcademicIndexDecisionEvent.Action.APPROVE,
            operation_key="shared",
            evidence_note="original",
            batch_id="01",
        )
        events = AcademicIndexDecisionEvent.objects.count()
        rows = ArchiveItemTopicAssociation.objects.count()
        conflicts = [
            {
                "archive_item": other_item,
                "topic": topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.CENTRAL,
                "action": AcademicIndexDecisionEvent.Action.APPROVE,
                "evidence_note": "original",
            },
            {
                "archive_item": item,
                "topic": other_topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.CENTRAL,
                "action": AcademicIndexDecisionEvent.Action.APPROVE,
                "evidence_note": "original",
            },
            {
                "archive_item": item,
                "topic": topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.CENTRAL,
                "action": AcademicIndexDecisionEvent.Action.IMPORT,
                "evidence_note": "original",
                "specification_sha256": _SHA,
            },
            {
                "archive_item": item,
                "topic": topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.SECONDARY,
                "action": AcademicIndexDecisionEvent.Action.APPROVE,
                "evidence_note": "original",
            },
            {
                "archive_item": item,
                "topic": topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.CENTRAL,
                "action": AcademicIndexDecisionEvent.Action.APPROVE,
                "evidence_note": "changed",
            },
            {
                "archive_item": item,
                "topic": topic,
                "relevance": ArchiveItemTopicAssociation.Relevance.CENTRAL,
                "action": AcademicIndexDecisionEvent.Action.APPROVE,
                "evidence_note": "original",
                "evidence_kind": AcademicIndexDecisionEvent.EvidenceKind.ORIGINAL_IMAGE,
            },
        ]
        for conflict in conflicts:
            with self.assertRaises(ValidationError):
                academic_index.approve_item_topic(
                    conflict.pop("archive_item"),
                    conflict.pop("topic"),
                    conflict.pop("relevance"),
                    operation_key="shared",
                    batch_id="01",
                    **conflict,
                )
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), events)
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), rows)
        row = ArchiveItemTopicAssociation.objects.get()
        self.assertEqual(row.archive_item_id, item.pk)
        self.assertEqual(row.topic_id, topic.pk)
        self.assertEqual(row.relevance, ArchiveItemTopicAssociation.Relevance.CENTRAL)
        self.assertEqual(row.status, ArchiveItemTopicAssociation.Status.APPROVED)

    def test_reused_key_rejects_different_relation_endpoints(self):
        parent = _topic("EDGE-P")
        child = _topic("EDGE-C")
        other = _topic("EDGE-O")
        academic_index.approve_broader_relation(child, parent, operation_key="edge")
        events = AcademicIndexDecisionEvent.objects.count()
        rows = AcademicTopicRelation.objects.count()
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(other, parent, operation_key="edge")
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), events)
        self.assertEqual(AcademicTopicRelation.objects.count(), rows)
        self.assertEqual(AcademicTopicRelation.objects.get().narrower_id, child.pk)

    def test_reversed_related_retry_is_the_same_operation(self):
        left = _topic("SYM-L")
        right = _topic("SYM-R")
        first = academic_index.approve_related_relation(
            left,
            right,
            operation_key="sym",
            evidence_note="pair",
        )
        second = academic_index.approve_related_relation(
            right,
            left,
            operation_key="sym",
            evidence_note="pair",
        )
        self.assertEqual(first.event.pk, second.event.pk)
        self.assertFalse(second.created)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 1)
        stored = first.event.snapshot["request"]
        low = AcademicTopic.objects.get(stable_key=stored["topic_low_stable_key"])
        high = AcademicTopic.objects.get(stable_key=stored["topic_high_stable_key"])
        self.assertLess(low.pk, high.pk)
        self.assertEqual(
            {stored["topic_low_stable_key"], stored["topic_high_stable_key"]},
            {left.stable_key, right.stable_key},
        )

    def test_failed_transaction_does_not_consume_the_operation_key(self):
        narrower = _topic("FAIL-N")
        broader = _topic("FAIL-B")
        with self.assertRaises(ValidationError):
            academic_index.approve_broader_relation(
                narrower,
                broader,
                action=AcademicIndexDecisionEvent.Action.IMPORT,
                specification_sha256="",
                operation_key="retry-after-failure",
            )
        self.assertEqual(AcademicTopicRelation.objects.count(), 0)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 0)
        written = academic_index.approve_broader_relation(
            narrower,
            broader,
            operation_key="retry-after-failure",
        )
        self.assertTrue(written.created)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 1)
        repeated = academic_index.approve_broader_relation(
            narrower,
            broader,
            operation_key="retry-after-failure",
        )
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.event.pk, written.event.pk)


class AcademicIndexOperationKeyConcurrencyTests(TransactionTestCase):
    def test_concurrent_identical_key_resolves_to_one_committed_event(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        left = _topic("CONC-L")
        right = _topic("CONC-R")
        barrier = threading.Barrier(2)
        event_ids = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                result = academic_index.approve_related_relation(
                    left,
                    right,
                    operation_key="conc-same",
                    evidence_note="same",
                )
                event_ids.append(result.event.pk)
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(event_ids), 2)
        self.assertEqual(set(event_ids), {event_ids[0]})
        self.assertEqual(AcademicIndexDecisionEvent.objects.filter(operation_key="conc-same").count(), 1)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)

    def test_concurrent_conflicting_key_keeps_only_the_winning_request(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        left = _topic("CONC2-L")
        right = _topic("CONC2-R")
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(note: str):
            try:
                barrier.wait(timeout=10)
                academic_index.approve_related_relation(
                    left,
                    right,
                    operation_key="conc-diff",
                    evidence_note=note,
                )
                outcomes.append(("ok", note))
            except ValidationError:
                outcomes.append(("conflict", note))
            except Exception as exc:  # pragma: no cover
                outcomes.append(("error", repr(exc)))
            finally:
                connection.close()

        threads = [
            threading.Thread(target=worker, args=("alpha",)),
            threading.Thread(target=worker, args=("beta",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(item[0] for item in outcomes), ["conflict", "ok"])
        self.assertEqual(AcademicIndexDecisionEvent.objects.filter(operation_key="conc-diff").count(), 1)
        self.assertEqual(AcademicTopicRelation.objects.count(), 1)
        event = AcademicIndexDecisionEvent.objects.get(operation_key="conc-diff")
        self.assertIn(event.evidence_note, {"alpha", "beta"})
        self.assertEqual(event.snapshot["request"]["evidence_note"], event.evidence_note)


class AcademicIndexRegressionTests(TestCase):
    def test_existing_catalog_models_are_unchanged(self):
        expected = {
            Person: {
                "id",
                "name",
                "honorific",
                "biography",
                "created_at",
                "updated_at",
            },
            Author: {"id", "name", "person", "created_at", "updated_at"},
            ArchiveItemAuthor: {
                "id",
                "archive_item",
                "author",
                "position",
                "created_at",
            },
            ArchiveItemPerson: {"id", "archive_item", "person", "created_at"},
            PhotoPerson: {"id", "photo_content", "person", "created_at"},
            NonPersonEntity: {
                "id",
                "canonical_name",
                "display_name",
                "entity_type",
                "entity_subtype",
                "created_at",
                "updated_at",
            },
            NonPersonEntityAlias: {
                "id",
                "entity",
                "name",
                "kind",
                "created_at",
                "updated_at",
            },
            ArchiveCategory: {
                "id",
                "name",
                "slug",
                "description",
                "created_at",
                "updated_at",
            },
            ArchiveEvent: {
                "id",
                "name",
                "slug",
                "description",
                "date_start",
                "date_end",
                "date_precision",
                "created_at",
                "updated_at",
            },
            Tag: {"id", "name", "created_at", "updated_at"},
        }
        for model, names in expected.items():
            self.assertEqual({field.name for field in model._meta.fields}, names)
        occurrence_names = {field.name for field in ArchiveItemEntityOccurrence._meta.fields}
        self.assertIn("source_text_sha256", occurrence_names)
        self.assertIn("entity", occurrence_names)
        model_names = {model.__name__ for model in apps.get_app_config("documents").get_models()}
        self.assertNotIn("AcademicIndexIssuer", model_names)
        self.assertNotIn("ArchiveItemIssuer", model_names)
        self.assertIn("AcademicTopic", model_names)

    def test_migration_is_schema_only_and_adds_no_public_route(self):
        from django.db.migrations.loader import MigrationLoader

        migration = MigrationLoader(connection, ignore_no_migrations=True).get_migration(
            "documents",
            "0079_academic_index_foundation",
        )
        self.assertEqual(
            [operation.__class__.__name__ for operation in migration.operations],
            ["CreateModel"] * 5,
        )
        from documents.migrations import __path__ as migration_paths

        migration_file = Path(migration_paths[0]) / "0079_academic_index_foundation.py"
        text = migration_file.read_text(encoding="utf-8")
        self.assertNotIn("RunPython", text)
        self.assertNotIn("RunSQL", text)
        from documents import archive_urls

        routes = " ".join(str(pattern.pattern) for pattern in archive_urls.urlpatterns)
        self.assertNotIn("topics", routes)

    def test_database_name_is_the_disposable_test_database(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))


class AcademicIndexMigrationReverseTests(TransactionTestCase):
    def test_foundation_migration_reverses_on_the_test_database(self):
        name = str(connection.settings_dict["NAME"])
        self.assertTrue(name.startswith("test_"), name)
        executor = MigrationExecutor(connection)
        executor.migrate([("documents", "0078_non_person_entity_correction")])
        tables = set(connection.introspection.table_names())
        self.assertNotIn(AcademicTopic._meta.db_table, tables)
        executor = MigrationExecutor(connection)
        executor.migrate([("documents", "0079_academic_index_foundation")])
        tables = set(connection.introspection.table_names())
        self.assertIn(AcademicTopic._meta.db_table, tables)
        self.assertIn(AcademicIndexDecisionEvent._meta.db_table, tables)
        self.assertIn(ArchiveItemTopicAssociation._meta.db_table, tables)
