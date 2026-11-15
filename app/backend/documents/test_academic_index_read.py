# mypy: disable-error-code="union-attr,index"
"""Stage 3A read-layer tests. Pytest database only."""

from __future__ import annotations

import dataclasses
import sys
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.contrib.auth.models import Group, Permission
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.academic_index_seed_v2 import (
    APPLY_CONFIRM_TOKEN,
    OWNER_AUTHORIZATION_TOKEN,
    PINNED_DISTINCT_ITEM_IDS,
)
from documents.models import ArchiveItem, Document, PhotoContent
from documents.services import academic_index, academic_index_read
from documents.services.academic_index_read import (
    AcademicIndexAudience,
    BroaderGraph,
    ItemAppearance,
    PublicAssociationPath,
    PublicTopicDetail,
    PublicTopicItemRecord,
    PublicTopicNavigation,
    StaffAssociationPath,
    StaffTopicDetail,
    StaffTopicNavigation,
    ancestor_closure,
    descendant_closure,
    direct_items_queryset,
    get_topic_detail,
    get_topic_navigation,
    inherited_items_queryset,
    item_provenance,
    list_combined_items,
    list_direct_items,
    list_inherited_items,
    list_public_topics,
    list_related_topics,
    load_approved_broader_graph,
    public_index_enabled,
)
from documents.services.academic_index_seed import apply_approved_seed
from documents.services.archive_item_access import can_view_restricted_archive_items
from documents.services.hebrew_display_order import hebrew_public_sort_key

_SHA = "ab" * 32
_PUBLIC = AcademicIndexAudience.PUBLIC
_STAFF = AcademicIndexAudience.STAFF
_FORBIDDEN_PUBLIC_NAMES = {
    "depth",
    "association_id",
    "decision_event_id",
    "cycle_detected",
    "graph_truncated",
    "unlabeled_hidden_paths",
    "hidden_path",
    "omitted_path",
    "path_length",
    "distance",
}

_NAVIGATION = {
    "AI-01": ((), ("AI-02", "AI-03"), (), ("AI-02", "AI-03"), (), ()),
    "AI-02": (("AI-01",), (), ("AI-01",), (), (("AI-01", ("AI-03",)),), ()),
    "AI-03": (("AI-01",), (), ("AI-01",), (), (("AI-01", ("AI-02",)),), ()),
    "AI-04": ((), (), (), (), (), ()),
    "AI-05": ((), ("AI-10",), (), ("AI-10",), (), ("AI-11",)),
    "AI-06": ((), ("AI-09", "AI-08", "AI-07"), (), ("AI-09", "AI-08", "AI-07"), (), ()),
    "AI-07": (("AI-06",), (), ("AI-06",), (), (("AI-06", ("AI-09", "AI-08")),), ()),
    "AI-08": (("AI-06",), (), ("AI-06",), (), (("AI-06", ("AI-09", "AI-07")),), ()),
    "AI-09": (("AI-06",), (), ("AI-06",), (), (("AI-06", ("AI-08", "AI-07")),), ()),
    "AI-10": (("AI-05",), (), ("AI-05",), (), (), ()),
    "AI-11": ((), (), (), (), (), ("AI-05",)),
}

_DIRECT: dict[str, dict[int, str]] = {
    "AI-01": {264: "CENTRAL", 389: "CENTRAL"},
    "AI-02": {378: "CENTRAL", 379: "CENTRAL"},
    "AI-03": {366: "SECONDARY", 430: "CENTRAL"},
    "AI-04": {378: "CENTRAL"},
    "AI-05": {
        144: "CENTRAL",
        147: "CENTRAL",
        164: "CENTRAL",
        173: "CENTRAL",
        324: "SECONDARY",
    },
    "AI-06": {},
    "AI-07": {284: "CENTRAL"},
    "AI-08": {13: "CENTRAL"},
    "AI-09": {13: "CENTRAL", 284: "SECONDARY"},
    "AI-10": {144: "SECONDARY", 164: "CENTRAL", 173: "CENTRAL", 324: "CENTRAL"},
    "AI-11": {163: "CENTRAL", 301: "CENTRAL"},
}

_INHERITED: dict[str, dict[int, tuple[tuple[str, str], ...]]] = {
    "AI-01": {
        366: (("AI-03", "SECONDARY"),),
        378: (("AI-02", "CENTRAL"),),
        379: (("AI-02", "CENTRAL"),),
        430: (("AI-03", "CENTRAL"),),
    },
    "AI-02": {},
    "AI-03": {},
    "AI-04": {},
    "AI-05": {},
    "AI-06": {
        13: (("AI-08", "CENTRAL"), ("AI-09", "CENTRAL")),
        284: (("AI-07", "CENTRAL"), ("AI-09", "SECONDARY")),
    },
    "AI-07": {},
    "AI-08": {},
    "AI-09": {},
    "AI-10": {},
    "AI-11": {},
}


def _make_topic(key: str, label: str, *, candidate: bool = True) -> AcademicTopic:
    if candidate:
        academic_index.create_topic(
            stable_key=key,
            preferred_label=label,
            is_public_candidate=True,
            action=AcademicIndexDecisionEvent.Action.IMPORT,
            specification_sha256=_SHA,
        )
    else:
        academic_index.create_topic(stable_key=key, preferred_label=label)
    return AcademicTopic.objects.get(stable_key=key)


def _public_item(
    title: str, *, visibility: str = ArchiveItem.Visibility.PUBLIC
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        title=title,
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=visibility,
    )


def _associate(item: ArchiveItem, topic: AcademicTopic, relevance: str) -> None:
    academic_index.approve_item_topic(item, topic, relevance)


def _broader(child: AcademicTopic, parent: AcademicTopic) -> None:
    academic_index.approve_broader_relation(child, parent)


def _related(left: AcademicTopic, right: AcademicTopic) -> None:
    academic_index.approve_related_relation(left, right)


def _user(name: str, **extra) -> User:
    return User.objects.create_user(name, password="x", **extra)


def _family_user() -> User:
    user = _user("family-reader")
    group, _created = Group.objects.get_or_create(name="archive_family")
    user.groups.add(group)
    return user


def _restricted_user() -> User:
    user = _user("restricted-reader")
    permission = Permission.objects.get(
        content_type__app_label="documents",
        codename="view_restricted_archiveitem",
    )
    user.user_permissions.add(permission)
    user = User.objects.get(pk=user.pk)
    return user


def _topic(key: str) -> AcademicTopic:
    return AcademicTopic.objects.get(stable_key=key)


def _keys(refs) -> tuple[str, ...]:
    return tuple(ref.stable_key for ref in refs)


def _ids(page) -> set[int]:
    return {record.archive_item_id for record in page.records}


def _field_names(value) -> set[str]:
    names: set[str] = set()

    def walk(node) -> None:
        if dataclasses.is_dataclass(node):
            for field in dataclasses.fields(node):
                names.add(field.name)
                walk(getattr(node, field.name))
        elif isinstance(node, tuple):
            for item in node:
                walk(item)

    walk(value)
    return names


def _scalars(value) -> set[object]:
    found: set[object] = set()

    def walk(node) -> None:
        if dataclasses.is_dataclass(node):
            for field in dataclasses.fields(node):
                walk(getattr(node, field.name))
        elif isinstance(node, tuple):
            for item in node:
                walk(item)
        else:
            found.add(node)

    walk(value)
    return found


def _readable_event(action: str) -> AcademicIndexDecisionEvent:
    return AcademicIndexDecisionEvent.objects.bulk_create(
        [
            AcademicIndexDecisionEvent(
                action=action,
                target_kind=AcademicIndexDecisionEvent.TargetKind.TOPIC_RELATION,
                evidence_kind=AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
                specification_sha256=_SHA,
                snapshot={"stage3": True},
            )
        ]
    )[0]


def _bulk_broader(pairs: list[tuple[AcademicTopic, AcademicTopic]], event=None) -> None:
    event = event or _readable_event(AcademicIndexDecisionEvent.Action.IMPORT)
    AcademicTopicRelation.objects.bulk_create(
        [
            AcademicTopicRelation(
                relation_kind=AcademicTopicRelation.Kind.BROADER,
                status=AcademicTopicRelation.Status.APPROVED,
                narrower=child,
                broader=parent,
                decision_event=event,
            )
            for child, parent in pairs
        ]
    )


def _graph_from_edges(edges: list[tuple[int, int]]) -> BroaderGraph:
    children: dict[int, list[int]] = {}
    parents: dict[int, list[int]] = {}
    for narrower, broader in edges:
        children.setdefault(broader, []).append(narrower)
        parents.setdefault(narrower, []).append(broader)
    return BroaderGraph(
        parents={key: tuple(values) for key, values in parents.items()},
        children={key: tuple(values) for key, values in children.items()},
    )


def _reversed_graph(graph: BroaderGraph) -> BroaderGraph:
    return BroaderGraph(
        parents={key: tuple(values)[::-1] for key, values in graph.parents.items()},
        children={key: tuple(values)[::-1] for key, values in graph.children.items()},
    )


def _apply_seed() -> None:
    for item_id in PINNED_DISTINCT_ITEM_IDS:
        ArchiveItem.objects.create(
            id=item_id,
            title=f"Seed item {item_id}",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
    apply_approved_seed(
        confirm=APPLY_CONFIRM_TOKEN,
        owner_authorization=OWNER_AUTHORIZATION_TOKEN,
    )


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexSeedReadTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("seed-staff", is_staff=True)

    def test_an_explicit_false_flag_disables_public_reads(self) -> None:
        with override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=False):
            self.assertFalse(public_index_enabled())

    def test_seed_navigation_for_staff_and_public(self) -> None:
        graph = load_approved_broader_graph()
        for key, expected in _NAVIGATION.items():
            topic = _topic(key)
            parents, children, ancestors, descendants, siblings, related = expected
            with self.subTest(topic=key):
                for audience, user in ((_STAFF, self.staff), (_PUBLIC, None)):
                    navigation = get_topic_navigation(
                        topic.pk, user=user, audience=audience
                    )
                    self.assertIsNotNone(navigation)
                    assert navigation is not None
                    self.assertEqual(_keys(navigation.parents), parents)
                    self.assertEqual(_keys(navigation.children), children)
                    self.assertEqual(_keys(navigation.ancestors), ancestors)
                    self.assertEqual(_keys(navigation.descendants), descendants)
                    self.assertEqual(_keys(navigation.related), related)
                    self.assertEqual(
                        tuple(
                            (group.shared_parent.stable_key, _keys(group.siblings))
                            for group in navigation.sibling_groups
                        ),
                        siblings,
                    )
                    self.assertNotIn(key, _keys(navigation.ancestors))
                    self.assertNotIn(key, _keys(navigation.descendants))
                staff_navigation = get_topic_navigation(
                    topic.pk, user=self.staff, audience=_STAFF
                )
                self.assertIsInstance(staff_navigation, StaffTopicNavigation)
                assert isinstance(staff_navigation, StaffTopicNavigation)
                self.assertFalse(staff_navigation.cycle_detected)
                self.assertFalse(staff_navigation.graph_truncated)
                public_navigation = get_topic_navigation(
                    topic.pk, user=None, audience=_PUBLIC
                )
                self.assertIsInstance(public_navigation, PublicTopicNavigation)
                assert isinstance(public_navigation, PublicTopicNavigation)
                self.assertFalse(hasattr(public_navigation, "cycle_detected"))
                self.assertFalse(hasattr(public_navigation, "graph_truncated"))
                for audience, user, detail_type in (
                    (_STAFF, self.staff, StaffTopicDetail),
                    (_PUBLIC, None, PublicTopicDetail),
                ):
                    detail = get_topic_detail(topic.pk, user=user, audience=audience)
                    self.assertIsInstance(detail, detail_type)
                    assert isinstance(detail, detail_type)
                    self.assertEqual(detail.alternate_labels, ())
                    self.assertEqual(detail.scope_note, "")
                    self.assertTrue(detail.is_public_candidate)
                closure = descendant_closure(graph, topic.pk)
                self.assertFalse(closure.cycle_detected)
                self.assertFalse(closure.graph_truncated)
                self.assertNotIn(topic.pk, closure.topic_ids)
                self.assertEqual(closure.min_depth[topic.pk], 0)
                self.assertEqual(
                    closure.topic_ids,
                    academic_index.approved_descendant_ids(topic.pk),
                )
                self.assertNotIn(topic.pk, ancestor_closure(graph, topic.pk).topic_ids)
        self.assertEqual(
            _keys(
                list_related_topics(
                    _topic("AI-05").pk, user=self.staff, audience=_STAFF
                )
            ),
            ("AI-11",),
        )
        self.assertEqual(
            _keys(list_related_topics(_topic("AI-11").pk, user=None, audience=_PUBLIC)),
            ("AI-05",),
        )
        self.assertNotIn(
            "AI-11",
            _keys(
                get_topic_navigation(
                    _topic("AI-05").pk, user=self.staff, audience=_STAFF
                ).children
            ),
        )
        self.assertEqual(
            get_topic_navigation(
                _topic("AI-04").pk, user=None, audience=_PUBLIC
            ).parents,
            (),
        )

    def test_seed_item_sets_are_exact_for_a_viewer_who_can_browse_every_pin(
        self,
    ) -> None:
        events_before = AcademicIndexDecisionEvent.objects.count()
        associations_before = ArchiveItemTopicAssociation.objects.count()
        self.assertEqual(associations_before, 22)
        self.assertEqual(events_before, 40)
        self.assertFalse(
            ArchiveItem.objects.filter(pk__in=(84, 360, 363, 386, 422)).exists()
        )
        for key, direct in _DIRECT.items():
            inherited = _INHERITED[key]
            topic = _topic(key)
            with self.subTest(topic=key):
                public_direct = list_direct_items(
                    topic.pk, user=None, audience=_PUBLIC, per_page=48
                )
                public_inherited = list_inherited_items(
                    topic.pk, user=None, audience=_PUBLIC, per_page=48
                )
                public_combined = list_combined_items(
                    topic.pk, user=None, audience=_PUBLIC, per_page=48
                )
                self.assertEqual(_ids(public_direct), set(direct))
                self.assertEqual(_ids(public_inherited), set(inherited))
                self.assertEqual(_ids(public_combined), set(direct) | set(inherited))
                self.assertEqual(
                    public_direct.total_count + public_inherited.total_count,
                    public_combined.total_count,
                )
                detail = get_topic_detail(topic.pk, user=None, audience=_PUBLIC)
                self.assertEqual(detail.visible_item_count, public_combined.total_count)
                for record in public_direct.records:
                    self.assertEqual(record.paths, ())
                    self.assertEqual(record.appearance, ItemAppearance.DIRECT)
                    self.assertEqual(
                        record.direct_relevance, direct[record.archive_item_id]
                    )
                    self.assertIsInstance(record, PublicTopicItemRecord)
                    assert isinstance(record, PublicTopicItemRecord)
                for record in public_inherited.records:
                    self.assertIsNone(record.direct_relevance)
                    self.assertEqual(record.appearance, ItemAppearance.INHERITED)
                    expected = tuple(
                        sorted(
                            inherited[record.archive_item_id],
                            key=lambda path: (
                                hebrew_public_sort_key(_topic(path[0]).preferred_label),
                                _topic(path[0]).pk,
                            ),
                        )
                    )
                    self.assertEqual(
                        tuple(
                            (path.stable_key, path.relevance) for path in record.paths
                        ),
                        expected,
                    )
                    for path in record.paths:
                        self.assertIsInstance(path, PublicAssociationPath)
                        assert isinstance(path, PublicAssociationPath)
                        self.assertFalse(hasattr(path, "depth"))
                staff_inherited = list_inherited_items(
                    topic.pk, user=self.staff, audience=_STAFF, per_page=48
                )
                for record in staff_inherited.records:
                    for path in record.paths:
                        self.assertIsInstance(path, StaffAssociationPath)
                        assert isinstance(path, StaffAssociationPath)
                        self.assertEqual(path.depth, 1)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), events_before)
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.count(), associations_before
        )

    def test_d2_staff_provenance_keeps_child_relevance_off_the_direct_record(
        self,
    ) -> None:
        topic = _topic("AI-05")
        child = _topic("AI-10")
        provenance = item_provenance(
            topic.pk, [144, 164, 173, 324], user=self.staff, audience=_STAFF
        )
        expected = {144: "SECONDARY", 164: "CENTRAL", 173: "CENTRAL", 324: "CENTRAL"}
        for item_id, child_relevance in expected.items():
            record = provenance[item_id]
            self.assertEqual(record.appearance, ItemAppearance.DIRECT)
            self.assertEqual(record.direct_relevance, _DIRECT["AI-05"][item_id])
            self.assertEqual(len(record.paths), 1)
            path = record.paths[0]
            self.assertEqual(path.stable_key, "AI-10")
            self.assertEqual(path.relevance, child_relevance)
            self.assertEqual(path.depth, 1)
            self.assertEqual(path.topic_id, child.pk)
            self.assertEqual(
                path.association_id,
                ArchiveItemTopicAssociation.objects.get(
                    archive_item_id=item_id, topic=child
                ).pk,
            )
        inherited = list_inherited_items(
            topic.pk, user=self.staff, audience=_STAFF, per_page=48
        )
        self.assertEqual(_ids(inherited), set())
        self.assertNotIn(
            163, _ids(list_combined_items(topic.pk, user=None, audience=_PUBLIC))
        )
        self.assertNotIn(
            301, _ids(list_combined_items(topic.pk, user=None, audience=_PUBLIC))
        )
        ai01 = _ids(
            list_combined_items(_topic("AI-01").pk, user=None, audience=_PUBLIC)
        )
        self.assertTrue({163, 301, 147}.isdisjoint(ai01))

    def test_staff_direct_page_keeps_descendant_paths_for_shared_seed_items(
        self,
    ) -> None:
        expected_paths = {
            144: ("AI-10", "SECONDARY"),
            164: ("AI-10", "CENTRAL"),
            173: ("AI-10", "CENTRAL"),
            324: ("AI-10", "CENTRAL"),
        }
        topic = _topic("AI-05")
        staff_direct = list_direct_items(
            topic.pk, user=self.staff, audience=_STAFF, per_page=48
        )
        public_direct = list_direct_items(
            topic.pk, user=None, audience=_PUBLIC, per_page=48
        )
        staff_by_id = {
            record.archive_item_id: record for record in staff_direct.records
        }
        public_by_id = {
            record.archive_item_id: record for record in public_direct.records
        }
        self.assertEqual(set(staff_by_id), set(public_by_id))
        for record in staff_direct.records:
            if record.archive_item_id not in expected_paths:
                self.assertEqual(record.paths, ())
                self.assertEqual(public_by_id[record.archive_item_id].paths, ())
        for item_id, (stable_key, relevance) in expected_paths.items():
            staff_record = staff_by_id[item_id]
            self.assertEqual(staff_record.appearance, ItemAppearance.DIRECT)
            self.assertEqual(staff_record.direct_relevance, _DIRECT["AI-05"][item_id])
            self.assertEqual(len(staff_record.paths), 1)
            path = staff_record.paths[0]
            assert isinstance(path, StaffAssociationPath)
            self.assertEqual(path.stable_key, stable_key)
            self.assertEqual(path.relevance, relevance)
            self.assertEqual(path.depth, 1)
            self.assertEqual(
                path.association_id,
                ArchiveItemTopicAssociation.objects.get(
                    archive_item_id=item_id, topic=_topic(stable_key)
                ).pk,
            )
            self.assertIsInstance(path.decision_event_id, int)
            self.assertEqual(public_by_id[item_id].paths, ())
            self.assertEqual(
                public_by_id[item_id].direct_relevance, _DIRECT["AI-05"][item_id]
            )
        for audience, user in ((_PUBLIC, None), (_STAFF, self.staff)):
            self.assertTrue(
                set(expected_paths).isdisjoint(
                    _ids(
                        list_inherited_items(
                            topic.pk, user=user, audience=audience, per_page=48
                        )
                    )
                )
            )
        for key, direct in _DIRECT.items():
            if key == "AI-05":
                continue
            page = list_direct_items(
                _topic(key).pk, user=self.staff, audience=_STAFF, per_page=48
            )
            for record in page.records:
                self.assertIn(record.archive_item_id, direct)
                self.assertEqual(record.paths, ())
                self.assertEqual(
                    record.direct_relevance, direct[record.archive_item_id]
                )

    def test_d3_item_378_is_inherited_only_through_ai_02(self) -> None:
        topic = _topic("AI-01")
        provenance = item_provenance(topic.pk, [378], user=self.staff, audience=_STAFF)
        paths = provenance[378].paths
        self.assertEqual(tuple(path.stable_key for path in paths), ("AI-02",))
        self.assertEqual(paths[0].relevance, "CENTRAL")
        self.assertEqual(paths[0].depth, 1)
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.filter(archive_item_id=378).count(),
            2,
        )
        self.assertFalse(
            ArchiveItemTopicAssociation.objects.filter(
                archive_item_id__in=(366, 378, 379, 430),
                topic=_topic("AI-01"),
            ).exists()
        )

    def test_d4_ai_06_items_are_unique_and_keep_both_public_paths(self) -> None:
        topic = _topic("AI-06")
        page = list_inherited_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
        self.assertEqual(
            [record.archive_item_id for record in page.records].count(13), 1
        )
        self.assertEqual(
            [record.archive_item_id for record in page.records].count(284), 1
        )
        for record in page.records:
            self.assertEqual(len(record.paths), 2)
            self.assertTrue(all(not hasattr(path, "depth") for path in record.paths))
        staff = item_provenance(topic.pk, [13, 284], user=self.staff, audience=_STAFF)
        self.assertTrue(
            all(path.depth == 1 for record in staff.values() for path in record.paths)
        )

    def test_d6_an_unassociated_archive_item_386_is_in_no_topic_set(self) -> None:
        ArchiveItem.objects.create(
            id=386,
            title="Not an association key",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for topic in AcademicTopic.objects.all():
            combined = list_combined_items(
                topic.pk, user=None, audience=_PUBLIC, per_page=48
            )
            self.assertNotIn(386, _ids(combined))


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexGraphMembershipTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("graph-staff", is_staff=True)

    def test_m1_two_parents_list_the_child_item_once_each(self) -> None:
        parent_a = _make_topic("S3-M1-P1", "אב ראשון")
        parent_b = _make_topic("S3-M1-P2", "אב שני")
        child = _make_topic("S3-M1-C", "ילד")
        _broader(child, parent_a)
        _broader(child, parent_b)
        item = _public_item("shared child")
        _associate(item, child, "SECONDARY")
        for parent in (parent_a, parent_b):
            inherited = list_inherited_items(parent.pk, user=None, audience=_PUBLIC)
            self.assertEqual(_ids(inherited), {item.pk})
            path = inherited.records[0].paths[0]
            self.assertEqual(path.stable_key, "S3-M1-C")
            self.assertEqual(path.relevance, "SECONDARY")
            self.assertFalse(hasattr(path, "depth"))
            staff = item_provenance(
                parent.pk, [item.pk], user=self.staff, audience=_STAFF
            )
            self.assertEqual(staff[item.pk].paths[0].depth, 1)
            self.assertFalse(
                ArchiveItemTopicAssociation.objects.filter(
                    topic=parent, archive_item=item
                ).exists()
            )
        self.assertEqual(
            _ids(list_direct_items(child.pk, user=None, audience=_PUBLIC)), {item.pk}
        )
        navigation = get_topic_navigation(child.pk, user=self.staff, audience=_STAFF)
        self.assertFalse(navigation.cycle_detected)
        self.assertEqual(set(_keys(navigation.parents)), {"S3-M1-P1", "S3-M1-P2"})

    def test_m2_through_m6_grandparent_dedupes_and_direct_wins(self) -> None:
        grandparent = _make_topic("S3-M2-G", "סב")
        parent_a = _make_topic("S3-M2-P1", "אב")
        parent_b = _make_topic("S3-M2-P2", "אם")
        child = _make_topic("S3-M2-C", "ילד")
        other = _make_topic("S3-M2-D", "דוד")
        _broader(parent_a, grandparent)
        _broader(parent_b, grandparent)
        _broader(child, parent_a)
        _broader(child, parent_b)
        item = _public_item("rolled")
        _associate(item, child, "SECONDARY")
        inherited = list_inherited_items(grandparent.pk, user=None, audience=_PUBLIC)
        self.assertEqual(_ids(inherited), {item.pk})
        self.assertEqual(inherited.records[0].paths[0].stable_key, "S3-M2-C")
        self.assertFalse(hasattr(inherited.records[0].paths[0], "depth"))
        staff = item_provenance(
            grandparent.pk, [item.pk], user=self.staff, audience=_STAFF
        )
        self.assertEqual(staff[item.pk].paths[0].depth, 2)
        detail = get_topic_detail(grandparent.pk, user=self.staff, audience=_STAFF)
        self.assertEqual(detail.visible_item_count, 1)
        self.assertFalse(detail.navigation.cycle_detected)
        _associate(item, other, "CENTRAL")
        _broader(other, grandparent)
        combined = list_combined_items(grandparent.pk, user=None, audience=_PUBLIC)
        self.assertEqual(combined.total_count, 1)
        provenance = item_provenance(
            grandparent.pk, [item.pk], user=self.staff, audience=_STAFF
        )
        self.assertEqual(
            tuple(
                (path.stable_key, path.relevance, path.depth)
                for path in provenance[item.pk].paths
            ),
            (("S3-M2-D", "CENTRAL", 1), ("S3-M2-C", "SECONDARY", 2)),
        )
        public_paths = (
            list_inherited_items(grandparent.pk, user=None, audience=_PUBLIC)
            .records[0]
            .paths
        )
        self.assertEqual(
            tuple((path.stable_key, path.relevance) for path in public_paths),
            (("S3-M2-D", "CENTRAL"), ("S3-M2-C", "SECONDARY")),
        )
        self.assertTrue(all(not hasattr(path, "depth") for path in public_paths))
        _associate(item, grandparent, "CENTRAL")
        direct = list_direct_items(grandparent.pk, user=None, audience=_PUBLIC)
        inherited = list_inherited_items(grandparent.pk, user=None, audience=_PUBLIC)
        self.assertEqual(_ids(direct), {item.pk})
        self.assertEqual(direct.records[0].direct_relevance, "CENTRAL")
        self.assertEqual(direct.records[0].paths, ())
        self.assertEqual(_ids(inherited), set())
        staff_direct = item_provenance(
            grandparent.pk, [item.pk], user=self.staff, audience=_STAFF
        )
        self.assertEqual(
            {path.stable_key for path in staff_direct[item.pk].paths},
            {"S3-M2-C", "S3-M2-D"},
        )
        lone_a = _make_topic("S3-M5-A", "ענף א")
        lone_b = _make_topic("S3-M5-B", "ענף ב")
        leaf = _make_topic("S3-M5-C", "עלה")
        _broader(leaf, lone_a)
        _broader(leaf, lone_b)
        other_item = _public_item("two roots")
        _associate(other_item, leaf, "CENTRAL")
        self.assertEqual(
            _ids(list_inherited_items(lone_a.pk, user=None, audience=_PUBLIC)),
            {other_item.pk},
        )
        self.assertEqual(
            _ids(list_inherited_items(lone_b.pk, user=None, audience=_PUBLIC)),
            {other_item.pk},
        )
        closure_ids = descendant_closure(
            load_approved_broader_graph(), grandparent.pk
        ).topic_ids
        self.assertEqual(
            closure_ids, academic_index.approved_descendant_ids(grandparent.pk)
        )
        self.assertIn(child.pk, closure_ids)
        self.assertIn(other.pk, closure_ids)
        self.assertIn(parent_a.pk, closure_ids)
        self.assertIn(parent_b.pk, closure_ids)
        self.assertNotIn(grandparent.pk, closure_ids)

    def test_related_edges_do_not_inherit_items(self) -> None:
        _apply_seed()
        ai05 = _ids(
            list_combined_items(
                _topic("AI-05").pk, user=None, audience=_PUBLIC, per_page=48
            )
        )
        ai11 = _ids(
            list_combined_items(
                _topic("AI-11").pk, user=None, audience=_PUBLIC, per_page=48
            )
        )
        self.assertEqual(ai05, {144, 147, 164, 173, 324})
        self.assertEqual(ai11, {163, 301})
        self.assertEqual(ai05 & ai11, set())
        topic_a = _make_topic("S3-R-A", "קשור א")
        topic_b = _make_topic("S3-R-B", "קשור ב")
        topic_c = _make_topic("S3-R-C", "ילד קשור")
        _related(topic_a, topic_b)
        _broader(topic_c, topic_b)
        own = _public_item("related anchor")
        item = _public_item("related only")
        _associate(own, topic_a, "CENTRAL")
        _associate(item, topic_c, "CENTRAL")
        self.assertEqual(
            _ids(list_inherited_items(topic_a.pk, user=None, audience=_PUBLIC)), set()
        )
        self.assertEqual(
            _ids(list_inherited_items(topic_b.pk, user=None, audience=_PUBLIC)),
            {item.pk},
        )
        self.assertEqual(
            _keys(list_related_topics(topic_a.pk, user=None, audience=_PUBLIC)),
            ("S3-R-B",),
        )
        self.assertEqual(
            _keys(list_related_topics(topic_b.pk, user=None, audience=_PUBLIC)),
            ("S3-R-A",),
        )
        relation = AcademicTopicRelation.objects.get(
            relation_kind=AcademicTopicRelation.Kind.RELATED,
            topic_low__in=(topic_a, topic_b),
            topic_high__in=(topic_a, topic_b),
        )
        before_items = (
            _ids(list_combined_items(topic_a.pk, user=None, audience=_PUBLIC)),
            _ids(list_combined_items(topic_b.pk, user=None, audience=_PUBLIC)),
            _ids(list_combined_items(topic_c.pk, user=None, audience=_PUBLIC)),
        )
        academic_index.retract_relation(relation)
        self.assertEqual(
            list_related_topics(topic_a.pk, user=None, audience=_PUBLIC), ()
        )
        self.assertEqual(
            list_related_topics(topic_b.pk, user=self.staff, audience=_STAFF), ()
        )
        self.assertEqual(
            (
                _ids(list_combined_items(topic_a.pk, user=None, audience=_PUBLIC)),
                _ids(list_combined_items(topic_b.pk, user=None, audience=_PUBLIC)),
                _ids(list_combined_items(topic_c.pk, user=None, audience=_PUBLIC)),
            ),
            before_items,
        )
        academic_index.approve_related_relation(topic_b, topic_a)
        self.assertEqual(
            AcademicTopicRelation.objects.filter(
                relation_kind=AcademicTopicRelation.Kind.RELATED,
                topic_low__in=(topic_a, topic_b),
                topic_high__in=(topic_a, topic_b),
            ).count(),
            1,
        )
        self.assertEqual(
            _keys(list_related_topics(topic_a.pk, user=None, audience=_PUBLIC)),
            ("S3-R-B",),
        )
        self.assertEqual(
            _ids(list_inherited_items(topic_a.pk, user=None, audience=_PUBLIC)), set()
        )

    def test_provenance_uses_the_approved_closure_not_an_unrelated_topic(self) -> None:
        topic_a = _make_topic("S3-R01-A", "נושא א")
        topic_b = _make_topic("S3-R01-B", "נושא ב")
        own = _public_item("anchor")
        item = _public_item("only on b")
        _associate(own, topic_a, "CENTRAL")
        _associate(item, topic_b, "SECONDARY")
        for audience, user in ((_PUBLIC, None), (_STAFF, self.staff)):
            provenance = item_provenance(
                topic_a.pk, [item.pk, own.pk], user=user, audience=audience
            )
            self.assertNotIn(item.pk, provenance)
            self.assertEqual(provenance[own.pk].appearance, ItemAppearance.DIRECT)
            self.assertEqual(
                _ids(list_inherited_items(topic_a.pk, user=user, audience=audience)),
                set(),
            )
        child = _make_topic("S3-R01-C", "צאצא")
        _broader(child, topic_a)
        child_item = _public_item("real descendant")
        _associate(child_item, child, "CENTRAL")
        for audience, user in ((_PUBLIC, None), (_STAFF, self.staff)):
            record = item_provenance(
                topic_a.pk, [child_item.pk], user=user, audience=audience
            )[child_item.pk]
            self.assertEqual(record.appearance, ItemAppearance.INHERITED)
            self.assertIsNone(record.direct_relevance)
            self.assertEqual(record.paths[0].stable_key, "S3-R01-C")
            self.assertEqual(record.paths[0].relevance, "CENTRAL")
            if audience is _STAFF:
                assert isinstance(record.paths[0], StaffAssociationPath)
                self.assertEqual(record.paths[0].depth, 1)
            else:
                self.assertFalse(hasattr(record.paths[0], "depth"))
        _related(topic_a, topic_b)
        for audience, user in ((_PUBLIC, None), (_STAFF, self.staff)):
            provenance = item_provenance(
                topic_a.pk, [item.pk], user=user, audience=audience
            )
            self.assertNotIn(item.pk, provenance)
            self.assertNotIn(
                item.pk,
                _ids(list_inherited_items(topic_a.pk, user=user, audience=audience)),
            )
            self.assertNotIn(
                item.pk,
                _ids(list_combined_items(topic_a.pk, user=user, audience=audience)),
            )


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexRetractionTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("retract-staff", is_staff=True)

    def test_t1_and_t2_retracting_ai_02_removes_only_its_inherited_items(self) -> None:
        relation = AcademicTopicRelation.objects.get(
            narrower=_topic("AI-02"),
            broader=_topic("AI-01"),
        )
        academic_index.retract_relation(relation)
        inherited = _ids(
            list_inherited_items(
                _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
            )
        )
        self.assertEqual(inherited, {366, 430})
        self.assertEqual(
            _ids(list_direct_items(_topic("AI-02").pk, user=None, audience=_PUBLIC)),
            {378, 379},
        )
        self.assertNotIn(
            "AI-02",
            _keys(
                get_topic_navigation(
                    _topic("AI-01").pk, user=self.staff, audience=_STAFF
                ).children
            ),
        )
        academic_index.approve_broader_relation(_topic("AI-02"), _topic("AI-01"))
        self.assertEqual(
            _ids(
                list_inherited_items(
                    _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
                )
            ),
            {366, 378, 379, 430},
        )
        self.assertEqual(
            AcademicTopicRelation.objects.filter(
                narrower=_topic("AI-02"), broader=_topic("AI-01")
            ).count(),
            1,
        )

    def test_t3_through_t5_item_13_follows_the_remaining_readable_path(self) -> None:
        ai08 = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=13, topic=_topic("AI-08")
        )
        academic_index.retract_item_topic(ai08)
        paths = item_provenance(
            _topic("AI-06").pk, [13], user=self.staff, audience=_STAFF
        )[13].paths
        self.assertEqual(
            tuple((path.stable_key, path.relevance) for path in paths),
            (("AI-09", "CENTRAL"),),
        )
        self.assertIsNone(
            list_direct_items(_topic("AI-08").pk, user=None, audience=_PUBLIC)
        )
        self.assertEqual(
            _ids(
                list_direct_items(_topic("AI-08").pk, user=self.staff, audience=_STAFF)
            ),
            set(),
        )
        self.assertIn(
            13, _ids(list_direct_items(_topic("AI-09").pk, user=None, audience=_PUBLIC))
        )
        academic_index.approve_item_topic(
            ArchiveItem.objects.get(pk=13), _topic("AI-08"), "SECONDARY"
        )
        self.assertEqual(
            list_direct_items(_topic("AI-08").pk, user=None, audience=_PUBLIC)
            .records[0]
            .direct_relevance,
            "SECONDARY",
        )
        restored = item_provenance(
            _topic("AI-06").pk, [13], user=self.staff, audience=_STAFF
        )[13]
        restored_paths = tuple(
            (path.stable_key, path.relevance) for path in restored.paths
        )
        self.assertIn(("AI-08", "SECONDARY"), restored_paths)
        self.assertIn(("AI-09", "CENTRAL"), restored_paths)
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.filter(
                archive_item_id=13, topic=_topic("AI-08")
            ).count(),
            1,
        )
        ai08 = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=13, topic=_topic("AI-08")
        )
        ai09 = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=13, topic=_topic("AI-09")
        )
        academic_index.retract_item_topic(ai08)
        academic_index.retract_item_topic(ai09)
        self.assertNotIn(
            13,
            _ids(list_combined_items(_topic("AI-06").pk, user=None, audience=_PUBLIC)),
        )
        self.assertNotIn(
            13,
            _ids(
                list_combined_items(
                    _topic("AI-08").pk, user=self.staff, audience=_STAFF
                )
            ),
        )
        ai09_public = list_combined_items(
            _topic("AI-09").pk, user=None, audience=_PUBLIC
        )
        self.assertNotIn(13, _ids(ai09_public))
        self.assertIn(284, _ids(ai09_public))
        self.assertIn(
            284,
            _ids(list_combined_items(_topic("AI-06").pk, user=None, audience=_PUBLIC)),
        )

    def test_t6_supersede_stays_readable(self) -> None:
        academic_index.approve_item_topic(
            ArchiveItem.objects.get(pk=284), _topic("AI-07"), "SECONDARY"
        )
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=284, topic=_topic("AI-07")
        )
        self.assertEqual(
            association.decision_event.action,
            AcademicIndexDecisionEvent.Action.SUPERSEDE,
        )
        self.assertIn(
            284,
            _ids(list_direct_items(_topic("AI-07").pk, user=None, audience=_PUBLIC)),
        )
        provenance = item_provenance(
            _topic("AI-06").pk, [284], user=self.staff, audience=_STAFF
        )[284]
        ai07 = next(path for path in provenance.paths if path.stable_key == "AI-07")
        self.assertEqual(ai07.relevance, "SECONDARY")
        self.assertIn(284, academic_index.direct_approved_item_ids(_topic("AI-07").pk))

    def test_t7_retracting_ai_10_removes_the_path_not_the_direct_items(self) -> None:
        academic_index.retract_relation(
            AcademicTopicRelation.objects.get(
                narrower=_topic("AI-10"), broader=_topic("AI-05")
            )
        )
        self.assertEqual(
            _ids(
                list_direct_items(
                    _topic("AI-05").pk, user=None, audience=_PUBLIC, per_page=48
                )
            ),
            {144, 147, 164, 173, 324},
        )
        provenance = item_provenance(
            _topic("AI-05").pk, [144, 164, 173, 324], user=self.staff, audience=_STAFF
        )
        for item_id in (144, 164, 173, 324):
            self.assertEqual(provenance[item_id].appearance, ItemAppearance.DIRECT)
            self.assertEqual(provenance[item_id].paths, ())
            self.assertNotIn(
                item_id,
                _ids(
                    list_inherited_items(
                        _topic("AI-05").pk, user=None, audience=_PUBLIC, per_page=48
                    )
                ),
            )


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexCorruptRowTests(TestCase):
    staff: User
    topic: AcademicTopic
    item: ArchiveItem

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("corrupt-staff", is_staff=True)
        cls.topic = _make_topic("S3-C", "שורה פגומה")
        cls.item = _public_item("corrupt item")

    def test_c1_through_c5_stage3_is_stricter_than_stage1_and_does_not_repair_rows(
        self,
    ) -> None:
        before_associations = ArchiveItemTopicAssociation.objects.count()
        ArchiveItemTopicAssociation.objects.bulk_create(
            [
                ArchiveItemTopicAssociation(
                    archive_item=self.item,
                    topic=self.topic,
                    relevance="CENTRAL",
                    status=ArchiveItemTopicAssociation.Status.APPROVED,
                    decision_event=None,
                )
            ]
        )
        self.assertEqual(
            _ids(list_direct_items(self.topic.pk, user=self.staff, audience=_STAFF)),
            set(),
        )
        self.assertIn(
            self.item.pk, academic_index.direct_approved_item_ids(self.topic.pk)
        )
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.count(), before_associations + 1
        )

        retract_event = _readable_event(AcademicIndexDecisionEvent.Action.RETRACT)
        other = _public_item("retract event")
        ArchiveItemTopicAssociation.objects.bulk_create(
            [
                ArchiveItemTopicAssociation(
                    archive_item=other,
                    topic=self.topic,
                    relevance="CENTRAL",
                    status=ArchiveItemTopicAssociation.Status.APPROVED,
                    decision_event=retract_event,
                )
            ]
        )
        self.assertIsNone(list_direct_items(self.topic.pk, user=None, audience=_PUBLIC))
        self.assertNotIn(
            other.pk,
            _ids(list_direct_items(self.topic.pk, user=self.staff, audience=_STAFF)),
        )
        self.assertIn(other.pk, academic_index.direct_approved_item_ids(self.topic.pk))

        imported = _readable_event(AcademicIndexDecisionEvent.Action.IMPORT)
        retracted_item = _public_item("retracted row")
        ArchiveItemTopicAssociation.objects.bulk_create(
            [
                ArchiveItemTopicAssociation(
                    archive_item=retracted_item,
                    topic=self.topic,
                    relevance="CENTRAL",
                    status=ArchiveItemTopicAssociation.Status.RETRACTED,
                    decision_event=imported,
                )
            ]
        )
        self.assertIsNone(list_direct_items(self.topic.pk, user=None, audience=_PUBLIC))
        self.assertNotIn(
            retracted_item.pk,
            _ids(list_direct_items(self.topic.pk, user=self.staff, audience=_STAFF)),
        )
        self.assertNotIn(
            retracted_item.pk, academic_index.direct_approved_item_ids(self.topic.pk)
        )

        parent = _make_topic("S3-C-P", "הורה פגום")
        reject_event = _readable_event(AcademicIndexDecisionEvent.Action.REJECT)
        AcademicTopicRelation.objects.bulk_create(
            [
                AcademicTopicRelation(
                    relation_kind=AcademicTopicRelation.Kind.BROADER,
                    status=AcademicTopicRelation.Status.APPROVED,
                    narrower=self.topic,
                    broader=parent,
                    decision_event=reject_event,
                )
            ]
        )
        self.assertNotIn(
            "S3-C",
            _keys(
                get_topic_navigation(
                    parent.pk, user=self.staff, audience=_STAFF
                ).children
            ),
        )
        self.assertNotIn(
            self.item.pk,
            _ids(list_inherited_items(parent.pk, user=self.staff, audience=_STAFF)),
        )
        self.assertTrue(
            academic_index.approved_broader_relations()
            .filter(narrower=self.topic, broader=parent)
            .exists()
        )
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.count(), before_associations + 3
        )

    def test_c5_readable_supersede_is_included(self) -> None:
        _associate(self.item, self.topic, "CENTRAL")
        academic_index.approve_item_topic(self.item, self.topic, "SECONDARY")
        self.assertEqual(
            list_direct_items(self.topic.pk, user=None, audience=_PUBLIC)
            .records[0]
            .direct_relevance,
            "SECONDARY",
        )
        self.assertIn(
            self.item.pk, academic_index.direct_approved_item_ids(self.topic.pk)
        )


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexVisibilityTests(TestCase):
    staff: User
    superuser: User

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("visibility-staff", is_staff=True)
        cls.superuser = _user("visibility-super", is_superuser=True, is_staff=False)

    def _hide(self, item_id: int, visibility: str) -> None:
        ArchiveItem.objects.filter(pk=item_id).update(visibility=visibility)

    def test_v1_anonymous_sees_the_public_seed_sets(self) -> None:
        self.assertEqual(
            _ids(
                list_combined_items(
                    _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
                )
            ),
            {264, 366, 378, 379, 389, 430},
        )

    def test_v2_v3_and_v4_private_item_follows_browse_visibility(self) -> None:
        self._hide(378, ArchiveItem.Visibility.PRIVATE)
        self.assertNotIn(
            378,
            _ids(
                list_combined_items(
                    _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
                )
            ),
        )
        self.assertNotIn(
            378,
            _ids(list_direct_items(_topic("AI-02").pk, user=None, audience=_PUBLIC)),
        )
        self.assertIsNone(
            get_topic_detail(_topic("AI-04").pk, user=None, audience=_PUBLIC)
        )
        self.assertEqual(
            _ids(list_direct_items(_topic("AI-02").pk, user=None, audience=_PUBLIC)),
            {379},
        )
        self.assertEqual(
            _ids(
                list_inherited_items(
                    _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
                )
            ),
            {366, 379, 430},
        )
        family = _family_user()
        self.assertIn(
            378,
            _ids(list_direct_items(_topic("AI-04").pk, user=family, audience=_PUBLIC)),
        )
        self.assertIn(
            378,
            _ids(
                list_combined_items(
                    _topic("AI-01").pk, user=family, audience=_PUBLIC, per_page=48
                )
            ),
        )
        for audience in (_PUBLIC, _STAFF):
            self.assertIn(
                378,
                _ids(
                    list_direct_items(
                        _topic("AI-02").pk, user=self.staff, audience=audience
                    )
                ),
            )

    def test_v5_through_v8_restricted_items_stay_on_the_browse_rule(self) -> None:
        self._hide(163, ArchiveItem.Visibility.RESTRICTED)
        anonymous = _ids(
            list_direct_items(_topic("AI-11").pk, user=None, audience=_PUBLIC)
        )
        self.assertNotIn(163, anonymous)
        self.assertIn(301, anonymous)
        self.assertIsNotNone(
            get_topic_detail(_topic("AI-11").pk, user=None, audience=_PUBLIC)
        )
        staff_page = list_direct_items(
            _topic("AI-11").pk, user=self.staff, audience=_STAFF
        )
        self.assertIsNotNone(
            get_topic_detail(_topic("AI-11").pk, user=self.staff, audience=_STAFF)
        )
        self.assertNotIn(163, _ids(staff_page))
        self.assertIn(301, _ids(staff_page))
        restricted = _restricted_user()
        self.assertIn(
            163,
            _ids(
                list_direct_items(_topic("AI-11").pk, user=restricted, audience=_PUBLIC)
            ),
        )
        with self.assertRaises(PermissionDenied):
            get_topic_detail(_topic("AI-11").pk, user=restricted, audience=_STAFF)
        self.assertTrue(can_view_restricted_archive_items(self.superuser))
        self.assertIn(
            163,
            _ids(
                list_direct_items(
                    _topic("AI-11").pk, user=self.superuser, audience=_STAFF
                )
            ),
        )

    def test_v9_unknown_visibility_is_absent_for_every_viewer(self) -> None:
        ArchiveItem.objects.filter(pk=389).update(visibility="secret")
        viewers = (None, _family_user(), self.staff, _restricted_user())
        for user in viewers:
            audience = _STAFF if user is self.staff else _PUBLIC
            caller = (
                user if user is None or audience is _PUBLIC or user.is_staff else user
            )
            page = list_combined_items(
                _topic("AI-01").pk, user=caller, audience=audience, per_page=48
            )
            self.assertNotIn(389, _ids(page))

    def test_v10_and_v11_ocr_renderability(self) -> None:
        item = ArchiveItem.objects.get(pk=264)
        item.item_type = ArchiveItem.ItemType.OCR_DOCUMENT
        item.save(update_fields=["item_type"])
        document = Document.objects.create(
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            archive_item=item,
            upload_status=Document.UploadStatus.UPLOADING,
        )
        self.assertNotIn(
            264,
            _ids(list_direct_items(_topic("AI-01").pk, user=None, audience=_PUBLIC)),
        )
        self.assertIsNotNone(
            get_topic_detail(_topic("AI-01").pk, user=None, audience=_PUBLIC)
        )
        document.upload_status = Document.UploadStatus.UPLOADED
        document.save(update_fields=["upload_status"])
        self.assertIn(
            264,
            _ids(list_direct_items(_topic("AI-01").pk, user=None, audience=_PUBLIC)),
        )

    def test_v12_and_v13_photo_renderability(self) -> None:
        topic = _make_topic("S3-V-PHOTO", "תצלום")
        item = ArchiveItem.objects.create(
            title="Photo gate",
            item_type=ArchiveItem.ItemType.PHOTO,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = PhotoContent.objects.create(
            archive_item=item,
            position=1,
            original_file_key="",
            original_filename="scan.jpg",
            original_mime_type="image/jpeg",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        _associate(item, topic, "CENTRAL")
        self.assertIsNone(get_topic_detail(topic.pk, user=None, audience=_PUBLIC))
        photo.upload_status = PhotoContent.UploadStatus.UPLOADED
        photo.original_file_key = "photos/stage3/original.jpg"
        photo.save(update_fields=["upload_status", "original_file_key"])
        self.assertIn(
            item.pk, _ids(list_direct_items(topic.pk, user=None, audience=_PUBLIC))
        )

    def test_v14_and_v15_hidden_items_change_public_eligibility_and_directory_counts(
        self,
    ) -> None:
        ArchiveItem.objects.filter(pk__in=(13, 284)).update(
            visibility=ArchiveItem.Visibility.PRIVATE
        )
        self.assertIsNone(
            get_topic_detail(_topic("AI-06").pk, user=None, audience=_PUBLIC)
        )
        staff_detail = get_topic_detail(
            _topic("AI-06").pk, user=self.staff, audience=_STAFF
        )
        self.assertIsNotNone(staff_detail)
        assert staff_detail is not None
        self.assertEqual(staff_detail.visible_item_count, 2)
        ArchiveItem.objects.filter(pk=378).update(
            visibility=ArchiveItem.Visibility.PRIVATE
        )
        rows, total = list_public_topics(None, per_page=48)
        keys = {row.stable_key for row in rows}
        self.assertNotIn("AI-04", keys)
        self.assertTrue(all(row.visible_item_count >= 1 for row in rows))
        ai01 = next(row for row in rows if row.stable_key == "AI-01")
        self.assertEqual(ai01.visible_item_count, 5)
        self.assertNotIn(
            _topic("AI-04").preferred_label, {row.preferred_label for row in rows}
        )
        found, found_total = list_public_topics(
            None, search_query=_topic("AI-04").preferred_label
        )
        self.assertEqual(found, ())
        self.assertEqual(found_total, 0)
        self.assertEqual(total, len(keys))


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexEligibilityTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("eligible-staff", is_staff=True)

    def test_e1_and_e2_empty_and_unbrowsable_topics(self) -> None:
        topic = _make_topic("S3-E1", "ריק")
        self.assertIsNone(get_topic_detail(topic.pk, user=None, audience=_PUBLIC))
        staff = get_topic_detail(topic.pk, user=self.staff, audience=_STAFF)
        self.assertIsNotNone(staff)
        assert staff is not None
        self.assertEqual(staff.visible_item_count, 0)
        self.assertEqual(
            list_direct_items(topic.pk, user=self.staff, audience=_STAFF).records, ()
        )
        self.assertEqual(
            list_inherited_items(topic.pk, user=self.staff, audience=_STAFF).records, ()
        )
        self.assertNotIn(
            "S3-E1", {row.stable_key for row in list_public_topics(None)[0]}
        )
        hidden = _make_topic("S3-E2", "מוסתר")
        private_item = _public_item(
            "private only", visibility=ArchiveItem.Visibility.PRIVATE
        )
        _associate(private_item, hidden, "CENTRAL")
        self.assertIsNone(get_topic_detail(hidden.pk, user=None, audience=_PUBLIC))
        staff_detail = get_topic_detail(hidden.pk, user=self.staff, audience=_STAFF)
        self.assertEqual(
            _ids(list_direct_items(hidden.pk, user=self.staff, audience=_STAFF)),
            {private_item.pk},
        )
        self.assertEqual(staff_detail.visible_item_count, 1)

    def test_e2_staff_who_cannot_browse_the_item_gets_an_empty_page(self) -> None:
        topic = _make_topic("S3-E2B", "מוגבל")
        item = _public_item(
            "restricted only", visibility=ArchiveItem.Visibility.RESTRICTED
        )
        _associate(item, topic, "CENTRAL")
        page = list_direct_items(topic.pk, user=self.staff, audience=_STAFF)
        self.assertEqual(page.records, ())
        self.assertEqual(
            get_topic_detail(
                topic.pk, user=self.staff, audience=_STAFF
            ).visible_item_count,
            0,
        )

    def test_e3_and_e4_a_candidate_flag_alone_is_not_eligibility(self) -> None:
        parent = _make_topic("S3-E3-P", "הורה גלוי")
        child = _make_topic("S3-E3-C", "ילד חסוי", candidate=False)
        own = _public_item("parent item")
        rolled = _public_item("child item")
        _associate(own, parent, "CENTRAL")
        _associate(rolled, child, "CENTRAL")
        _broader(child, parent)
        self.assertIsNone(get_topic_detail(child.pk, user=None, audience=_PUBLIC))
        self.assertIn(
            rolled.pk,
            _ids(list_direct_items(child.pk, user=self.staff, audience=_STAFF)),
        )
        self.assertNotIn(
            "S3-E3-C",
            {row.stable_key for row in list_public_topics(None, per_page=48)[0]},
        )
        navigation = get_topic_navigation(parent.pk, user=None, audience=_PUBLIC)
        self.assertNotIn("S3-E3-C", _keys(navigation.children))
        self.assertNotIn("S3-E3-C", _keys(navigation.descendants))
        published = _make_topic("S3-E4", "יוסר")
        item = _public_item("e4")
        _associate(item, published, "CENTRAL")
        events = AcademicIndexDecisionEvent.objects.count()
        academic_index.set_topic_public_candidate(
            published,
            is_public_candidate=False,
            action=AcademicIndexDecisionEvent.Action.RETRACT,
        )
        self.assertGreater(AcademicIndexDecisionEvent.objects.count(), events)
        events_after_write = AcademicIndexDecisionEvent.objects.count()
        self.assertIsNone(get_topic_detail(published.pk, user=None, audience=_PUBLIC))
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), events_after_write)
        self.assertNotIn(
            "S3-E4", {row.stable_key for row in list_public_topics(None)[0]}
        )

    def test_e5_ai_06_is_public_through_inherited_items(self) -> None:
        _apply_seed()
        detail = get_topic_detail(_topic("AI-06").pk, user=None, audience=_PUBLIC)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(
            _ids(list_direct_items(_topic("AI-06").pk, user=None, audience=_PUBLIC)),
            set(),
        )
        self.assertGreater(detail.visible_item_count, 0)


class AcademicIndexFlagOffTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("flag-staff", is_staff=True)

    def test_e6_and_f1_public_calls_do_not_query_when_the_flag_is_absent_or_false(
        self,
    ) -> None:
        topic_id = _topic("AI-01").pk
        family = _family_user()
        callers = (None, family, self.staff)
        for enabled in (None, False):
            settings_override = (
                {} if enabled is None else {"ACADEMIC_INDEX_PUBLIC_ENABLED": False}
            )
            with override_settings(**settings_override):
                self.assertFalse(public_index_enabled())
                for user in callers:
                    with CaptureQueriesContext(connection) as captured:
                        self.assertIsNone(
                            get_topic_detail(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertIsNone(
                            get_topic_navigation(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertIsNone(
                            list_direct_items(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertIsNone(
                            list_inherited_items(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertIsNone(
                            list_combined_items(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertIsNone(
                            list_related_topics(topic_id, user=user, audience=_PUBLIC)
                        )
                        self.assertEqual(
                            item_provenance(
                                topic_id, [264], user=user, audience=_PUBLIC
                            ),
                            {},
                        )
                        self.assertEqual(list_public_topics(user), ((), 0))
                    academic_sql = [
                        query["sql"]
                        for query in captured.captured_queries
                        if "academic" in query["sql"].lower()
                    ]
                    self.assertEqual(academic_sql, [])

    def test_e7_authorized_staff_ignore_the_missing_flag(self) -> None:
        self.assertFalse(public_index_enabled())
        detail = get_topic_detail(_topic("AI-01").pk, user=self.staff, audience=_STAFF)
        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertIsNone(
            get_topic_detail(_topic("AI-01").pk, user=self.staff, audience=_PUBLIC)
        )


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexHiddenDescendantTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("hidden-staff", is_staff=True)

    def test_n1_public_path_names_an_eligible_child_without_depth(self) -> None:
        parent = _make_topic("S3-N1-P", "הורה")
        child = _make_topic("S3-N1-C", "ילד גלוי")
        _broader(child, parent)
        item = _public_item("n1")
        _associate(item, child, "CENTRAL")
        record = list_inherited_items(parent.pk, user=None, audience=_PUBLIC).records[0]
        self.assertEqual(record.paths[0].stable_key, "S3-N1-C")
        self.assertEqual(record.paths[0].relevance, "CENTRAL")
        self.assertFalse(hasattr(record.paths[0], "depth"))
        self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(record)))
        staff = item_provenance(parent.pk, [item.pk], user=self.staff, audience=_STAFF)[
            item.pk
        ]
        path = staff.paths[0]
        self.assertEqual(path.depth, 1)
        self.assertEqual(path.topic_id, child.pk)
        self.assertEqual(
            path.association_id,
            ArchiveItemTopicAssociation.objects.get(archive_item=item, topic=child).pk,
        )
        self.assertEqual(
            path.decision_event_id,
            ArchiveItemTopicAssociation.objects.get(
                archive_item=item, topic=child
            ).decision_event_id,
        )
        self.assertFalse(hasattr(path, "evidence_note"))

    def test_n2_n4_and_n8_a_hidden_child_is_not_named(self) -> None:
        parent = _make_topic("S3-N2-P", "הורה גלוי")
        child = _make_topic("S3-N2-C", "נושא חסוי", candidate=False)
        academic_index.add_alternate_label(child, "כינוי חסוי")
        _broader(child, parent)
        item = _public_item("n2")
        _associate(item, child, "SECONDARY")
        page = list_inherited_items(parent.pk, user=None, audience=_PUBLIC)
        self.assertEqual(_ids(page), {item.pk})
        self.assertEqual(page.records[0].appearance, ItemAppearance.INHERITED)
        self.assertEqual(page.records[0].paths, ())
        detail = get_topic_detail(parent.pk, user=None, audience=_PUBLIC)
        navigation = get_topic_navigation(parent.pk, user=None, audience=_PUBLIC)
        rows, _total = list_public_topics(None, per_page=48)
        search_label, _label_total = list_public_topics(
            None, search_query=child.preferred_label
        )
        search_key, _key_total = list_public_topics(None, search_query=child.stable_key)
        search_alt, _alt_total = list_public_topics(None, search_query="כינוי חסוי")
        payloads = (
            page,
            detail,
            navigation,
            rows,
            search_label,
            search_key,
            search_alt,
        )
        for payload in payloads:
            scalars = _scalars(payload)
            names = _field_names(payload)
            self.assertNotIn(child.pk, scalars)
            self.assertNotIn(child.stable_key, scalars)
            self.assertNotIn(child.preferred_label, scalars)
            self.assertNotIn("כינוי חסוי", scalars)
            self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(names))
        self.assertEqual(detail.visible_item_count, 1)
        directory_parent = next(row for row in rows if row.stable_key == "S3-N2-P")
        self.assertEqual(directory_parent.visible_item_count, 1)
        self.assertEqual(search_label, ())
        self.assertEqual(search_key, ())
        self.assertEqual(search_alt, ())
        parent_hit, _parent_total = list_public_topics(None, search_query="הורה גלוי")
        self.assertEqual(tuple(row.stable_key for row in parent_hit), ("S3-N2-P",))
        self.assertNotIn(child.preferred_label, _scalars(parent_hit))
        self.assertEqual(navigation.children, ())
        self.assertEqual(navigation.descendants, ())
        self.assertEqual(navigation.sibling_groups, ())
        staff_navigation = get_topic_navigation(
            parent.pk, user=self.staff, audience=_STAFF
        )
        self.assertIn("S3-N2-C", _keys(staff_navigation.children))
        staff_path = item_provenance(
            parent.pk, [item.pk], user=self.staff, audience=_STAFF
        )[item.pk].paths[0]
        self.assertEqual(staff_path.stable_key, "S3-N2-C")
        self.assertIsInstance(staff_path.association_id, int)
        self.assertIsInstance(staff_path.decision_event_id, int)

    def test_n3_only_the_eligible_source_is_named(self) -> None:
        parent = _make_topic("S3-N3-P", "הורה")
        visible = _make_topic("S3-N3-A", "מקור גלוי")
        hidden = _make_topic("S3-N3-B", "מקור חסוי", candidate=False)
        _broader(visible, parent)
        _broader(hidden, parent)
        item = _public_item("n3")
        _associate(item, visible, "CENTRAL")
        _associate(item, hidden, "SECONDARY")
        page = list_inherited_items(parent.pk, user=None, audience=_PUBLIC)
        self.assertEqual(len(page.records), 1)
        self.assertEqual(
            tuple(path.stable_key for path in page.records[0].paths), ("S3-N3-A",)
        )
        self.assertNotIn(hidden.pk, _scalars(page))
        self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(page)))
        staff = item_provenance(parent.pk, [item.pk], user=self.staff, audience=_STAFF)
        self.assertEqual(
            {path.stable_key for path in staff[item.pk].paths}, {"S3-N3-A", "S3-N3-B"}
        )

    def test_n5_a_candidate_without_items_is_not_a_public_related_topic(self) -> None:
        topic = _make_topic("S3-N5-Q", "נושא")
        related = _make_topic("S3-N5-R", "קשר ריק")
        _related(topic, related)
        _associate(_public_item("n5"), topic, "CENTRAL")
        self.assertEqual(list_related_topics(topic.pk, user=None, audience=_PUBLIC), ())
        self.assertEqual(
            _keys(list_related_topics(topic.pk, user=self.staff, audience=_STAFF)),
            ("S3-N5-R",),
        )

    def test_n6_and_n7_sibling_groups_omit_ineligible_parents_and_siblings(
        self,
    ) -> None:
        parent = _make_topic("S3-N6-P", "הורה משותף")
        topic = _make_topic("S3-N6-Q", "נשאל")
        sibling = _make_topic("S3-N6-S", "אח חסוי")
        _broader(topic, parent)
        _broader(sibling, parent)
        _associate(_public_item("n6-parent"), parent, "CENTRAL")
        _associate(_public_item("n6-topic"), topic, "CENTRAL")
        _associate(
            _public_item("n6-private", visibility=ArchiveItem.Visibility.PRIVATE),
            sibling,
            "CENTRAL",
        )
        group = get_topic_navigation(
            topic.pk, user=None, audience=_PUBLIC
        ).sibling_groups
        self.assertEqual(len(group), 1)
        self.assertEqual(group[0].siblings, ())
        self.assertNotIn("S3-N6-S", _scalars(group))
        staff_navigation = get_topic_navigation(
            topic.pk, user=self.staff, audience=_STAFF
        )
        self.assertIn("S3-N6-S", _keys(staff_navigation.sibling_groups[0].siblings))
        self.assertIn(
            ArchiveItem.objects.get(title="n6-private").pk,
            _ids(list_direct_items(sibling.pk, user=self.staff, audience=_STAFF)),
        )
        hidden_parent = _make_topic("S3-N7-P", "הורה חסוי", candidate=False)
        queried = _make_topic("S3-N7-Q", "נשאל גלוי")
        visible_sibling = _make_topic("S3-N7-S", "אח גלוי")
        _broader(queried, hidden_parent)
        _broader(visible_sibling, hidden_parent)
        _associate(_public_item("n7-q"), queried, "CENTRAL")
        _associate(_public_item("n7-s"), visible_sibling, "CENTRAL")
        public_navigation = get_topic_navigation(
            queried.pk, user=None, audience=_PUBLIC
        )
        self.assertEqual(public_navigation.sibling_groups, ())
        self.assertNotIn("S3-N7-P", _scalars(public_navigation))
        self.assertNotIn("S3-N7-S", _scalars(public_navigation))
        staff_group = get_topic_navigation(
            queried.pk, user=self.staff, audience=_STAFF
        ).sibling_groups
        self.assertEqual(staff_group[0].shared_parent.stable_key, "S3-N7-P")
        self.assertEqual(_keys(staff_group[0].siblings), ("S3-N7-S",))

    def test_n9_a_hidden_intermediate_does_not_add_depth_to_the_public_path(
        self,
    ) -> None:
        topic_a = _make_topic("S3-N9-A", "אלף")
        topic_b = _make_topic("S3-N9-B", "בית חסוי", candidate=False)
        topic_c = _make_topic("S3-N9-C", "גימל")
        _broader(topic_b, topic_a)
        _broader(topic_c, topic_b)
        item = _public_item("n9")
        _associate(item, topic_c, "SECONDARY")
        page = list_inherited_items(topic_a.pk, user=None, audience=_PUBLIC)
        self.assertEqual(_ids(page), {item.pk})
        self.assertEqual(
            tuple((path.stable_key, path.relevance) for path in page.records[0].paths),
            (("S3-N9-C", "SECONDARY"),),
        )
        detail = get_topic_detail(topic_a.pk, user=None, audience=_PUBLIC)
        navigation = get_topic_navigation(topic_a.pk, user=None, audience=_PUBLIC)
        directory, _total = list_public_topics(None, search_query="בית")
        self.assertIn("S3-N9-C", _keys(navigation.descendants))
        self.assertNotIn("S3-N9-B", _keys(navigation.descendants))
        self.assertNotIn("S3-N9-B", _keys(navigation.children))
        for payload in (page, detail, navigation, directory):
            self.assertNotIn(topic_b.pk, _scalars(payload))
            self.assertNotIn(topic_b.stable_key, _scalars(payload))
            self.assertNotIn(topic_b.preferred_label, _scalars(payload))
            self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(payload)))
        staff_path = item_provenance(
            topic_a.pk, [item.pk], user=self.staff, audience=_STAFF
        )[item.pk].paths[0]
        self.assertEqual(staff_path.stable_key, "S3-N9-C")
        self.assertEqual(staff_path.depth, 2)
        self.assertEqual(
            staff_path.association_id,
            ArchiveItemTopicAssociation.objects.get(
                topic=topic_c, archive_item=item
            ).pk,
        )
        staff_navigation = get_topic_navigation(
            topic_a.pk, user=self.staff, audience=_STAFF
        )
        self.assertIn("S3-N9-B", _keys(staff_navigation.children))
        self.assertIn("S3-N9-C", _keys(staff_navigation.descendants))


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexPaginationTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("page-staff", is_staff=True)

    def test_p1_through_p9_item_pages_use_sql_order_and_clamps(self) -> None:
        topic = _make_topic("S3-P", "עמוד")
        child = _make_topic("S3-P-C", "בן")
        _broader(child, topic)
        base = timezone.now()
        items = []
        for index in range(1, 6):
            item = _public_item(f"I{index}")
            ArchiveItem.objects.filter(pk=item.pk).update(
                created_at=base - timedelta(days=index)
            )
            items.append(item)
            _associate(item, topic, "CENTRAL")
        ordered = list(
            ArchiveItem.objects.filter(pk__in=[item.pk for item in items]).order_by(
                "-created_at", "pk"
            )
        )
        self.assertEqual(
            [item.title for item in ordered], ["I1", "I2", "I3", "I4", "I5"]
        )
        # I1 is newest because index 1 has the smallest day delta?
        # created_at = base - days(index): index 1 is newest (I1), index 5 is oldest (I5).
        # The matrix wants I5 newest. Recreate timestamps so title I5 is newest.
        for index, item in enumerate(items, start=1):
            ArchiveItem.objects.filter(pk=item.pk).update(
                created_at=base - timedelta(days=6 - index)
            )
        page1 = list_direct_items(
            topic.pk, user=None, audience=_PUBLIC, page=1, per_page=2
        )
        page2 = list_direct_items(
            topic.pk, user=None, audience=_PUBLIC, page=2, per_page=2
        )
        page3 = list_direct_items(
            topic.pk, user=None, audience=_PUBLIC, page=3, per_page=2
        )
        self.assertEqual(
            tuple(record.archive_item_id for record in page1.records),
            (items[4].pk, items[3].pk),
        )
        self.assertEqual(page1.total_count, 5)
        self.assertEqual(page1.page, 1)
        self.assertEqual(
            tuple(record.archive_item_id for record in page2.records),
            (items[2].pk, items[1].pk),
        )
        self.assertEqual(
            tuple(record.archive_item_id for record in page3.records), (items[0].pk,)
        )
        for raw in (0, -1, "abc"):
            self.assertEqual(
                list_direct_items(
                    topic.pk, user=None, audience=_PUBLIC, page=raw, per_page=2
                ).page,
                1,
            )
        self.assertEqual(
            list_direct_items(
                topic.pk, user=None, audience=_PUBLIC, page=99, per_page=2
            ).page,
            3,
        )
        _associate(items[4], child, "SECONDARY")
        combined = list_combined_items(
            topic.pk, user=None, audience=_PUBLIC, per_page=48
        )
        self.assertEqual(combined.total_count, 5)
        self.assertEqual(
            [record.archive_item_id for record in combined.records].count(items[4].pk),
            1,
        )
        private = items[0]
        ArchiveItem.objects.filter(pk=private.pk).update(
            visibility=ArchiveItem.Visibility.PRIVATE
        )
        anonymous = list_direct_items(
            topic.pk, user=None, audience=_PUBLIC, per_page=2, page=99
        )
        self.assertEqual(anonymous.total_count, 4)
        self.assertNotIn(
            private.pk,
            _ids(list_direct_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)),
        )
        self.assertEqual(anonymous.page, 2)
        tie_topic = _make_topic("S3-P-TIE", "שוויון")
        first = _public_item("older-pk")
        second = _public_item("newer-pk")
        self.assertLess(first.pk, second.pk)
        stamp = timezone.now()
        ArchiveItem.objects.filter(pk__in=(first.pk, second.pk)).update(
            created_at=stamp
        )
        _associate(first, tie_topic, "CENTRAL")
        _associate(second, tie_topic, "CENTRAL")
        tied = list_direct_items(tie_topic.pk, user=None, audience=_PUBLIC)
        self.assertEqual(
            tuple(record.archive_item_id for record in tied.records),
            (first.pk, second.pk),
        )
        both = _make_topic("S3-P-BOTH", "גם וגם")
        descendant = _make_topic("S3-P-D", "צאצא")
        _broader(descendant, both)
        older = _public_item("direct-old")
        newer = _public_item("inherited-new")
        ArchiveItem.objects.filter(pk=older.pk).update(
            created_at=stamp - timedelta(days=2)
        )
        ArchiveItem.objects.filter(pk=newer.pk).update(
            created_at=stamp - timedelta(days=1)
        )
        _associate(older, both, "CENTRAL")
        _associate(newer, descendant, "SECONDARY")
        direct_ids = tuple(
            record.archive_item_id
            for record in list_direct_items(
                both.pk, user=None, audience=_PUBLIC
            ).records
        )
        inherited_ids = tuple(
            record.archive_item_id
            for record in list_inherited_items(
                both.pk, user=None, audience=_PUBLIC
            ).records
        )
        self.assertEqual(direct_ids, (older.pk,))
        self.assertEqual(inherited_ids, (newer.pk,))
        sliced = direct_items_queryset(topic.pk, user=None)[:2]
        self.assertIn("LIMIT", str(sliced.query).upper())

    def test_p10_through_p15_directory_pagination_search_and_order(self) -> None:
        topics = []
        for index in range(49):
            topic = _make_topic(f"S3-D{index:02d}", f"נושא {index:02d}")
            _associate(_public_item(f"dir {index}"), topic, "CENTRAL")
            topics.append(topic)
        page1, total = list_public_topics(None, per_page=48, page=1)
        page2, _same_total = list_public_topics(None, per_page=48, page=2)
        self.assertEqual(total, 49)
        self.assertEqual(len(page1), 48)
        self.assertEqual(len(page2), 1)
        private_topic = _make_topic("S3-D-PRIVATE", "נושא פרטי")
        _associate(
            _public_item("private dir", visibility=ArchiveItem.Visibility.PRIVATE),
            private_topic,
            "CENTRAL",
        )
        hidden = _make_topic("S3-D-HIDDEN", "נושא לא מועמד", candidate=False)
        _associate(_public_item("hidden dir"), hidden, "CENTRAL")
        _rows, total_after = list_public_topics(None, per_page=48)
        self.assertEqual(total_after, 49)
        self.assertNotIn("S3-D-PRIVATE", {row.stable_key for row in _rows})
        self.assertNotIn("S3-D-HIDDEN", {row.stable_key for row in _rows})
        alpha = _make_topic("S3-S-A", "אלפא מיוחד")
        beta = _make_topic("S3-S-B", "ביתא מיוחד")
        _associate(_public_item("alpha"), alpha, "CENTRAL")
        _associate(_public_item("beta"), beta, "CENTRAL")
        decoy = _make_topic("S3-S-NO", "אלפא בלי פריט")
        non_candidate = _make_topic("S3-S-NC", "אלפא חסוי", candidate=False)
        _associate(_public_item("nc"), non_candidate, "CENTRAL")
        found, found_total = list_public_topics(None, search_query="מיוחד")
        self.assertEqual(found_total, 2)
        self.assertEqual({row.stable_key for row in found}, {"S3-S-A", "S3-S-B"})
        self.assertEqual(list_public_topics(None, search_query="אלפא בלי")[0], ())
        self.assertEqual(
            list_public_topics(None, search_query=decoy.preferred_label)[1], 0
        )
        full, full_total = list_public_topics(None, search_query="אלפא מיוחד")
        prefix, prefix_total = list_public_topics(None, search_query="אלפא מי")
        interior, interior_total = list_public_topics(None, search_query="פא מי")
        typo, typo_total = list_public_topics(None, search_query="אלפב מיוחד")
        self.assertEqual(full_total, 1)
        self.assertEqual(prefix_total, 1)
        self.assertEqual(interior_total, 1)
        self.assertEqual(tuple(row.stable_key for row in full), ("S3-S-A",))
        self.assertEqual(tuple(row.stable_key for row in prefix), ("S3-S-A",))
        self.assertEqual(tuple(row.stable_key for row in interior), ("S3-S-A",))
        self.assertEqual(typo, ())
        self.assertEqual(typo_total, 0)
        labeled = _make_topic("S3-S-ALT", "שם ראשי")
        academic_index.add_alternate_label(labeled, "שם חלופי")
        _associate(_public_item("alt"), labeled, "CENTRAL")
        alternate_hits, alternate_total = list_public_topics(None, search_query="חלופי")
        both_hits, both_total = list_public_topics(None, search_query="שם")
        self.assertEqual(tuple(row.stable_key for row in alternate_hits), ("S3-S-ALT",))
        self.assertEqual(alternate_total, 1)
        self.assertEqual([row.stable_key for row in both_hits].count("S3-S-ALT"), 1)
        self.assertEqual(both_total, len(both_hits))
        sample, _sample_total = list_public_topics(None, per_page=100)
        self.assertEqual(
            tuple((row.preferred_label, row.topic_id) for row in sample),
            tuple(
                sorted(
                    ((row.preferred_label, row.topic_id) for row in sample),
                    key=lambda row: (hebrew_public_sort_key(row[0]), row[1]),
                )
            ),
        )


class AcademicIndexWalkerTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        cls.staff = _user("walker-staff", is_staff=True)

    def test_g1_and_g2_cycles_return_without_including_the_start(self) -> None:
        topic_a = _make_topic("S3-G1-A", "אלף")
        topic_b = _make_topic("S3-G1-B", "בית")
        _bulk_broader([(topic_b, topic_a), (topic_a, topic_b)])
        graph = load_approved_broader_graph()
        closure = descendant_closure(graph, topic_a.pk)
        self.assertEqual(closure.topic_ids, frozenset({topic_b.pk}))
        self.assertTrue(closure.cycle_detected)
        self.assertFalse(closure.graph_truncated)
        self.assertNotIn(topic_a.pk, ancestor_closure(graph, topic_a.pk).topic_ids)
        navigation = get_topic_navigation(topic_a.pk, user=self.staff, audience=_STAFF)
        self.assertTrue(navigation.cycle_detected)
        self.assertFalse(navigation.graph_truncated)
        cycle = [
            _make_topic(f"S3-G2-{name}", label)
            for name, label in (("A", "א"), ("B", "ב"), ("C", "ג"))
        ]
        _bulk_broader(
            [(cycle[1], cycle[0]), (cycle[2], cycle[1]), (cycle[0], cycle[2])]
        )
        with self.assertNumQueries(1):
            loaded = load_approved_broader_graph()
        with self.assertNumQueries(0):
            cycled = descendant_closure(loaded, cycle[0].pk)
        self.assertEqual(cycled.topic_ids, frozenset({cycle[1].pk, cycle[2].pk}))
        self.assertNotIn(cycle[0].pk, cycled.topic_ids)
        self.assertTrue(cycled.cycle_detected)

    def test_g3_depth_boundary_does_not_raise(self) -> None:
        included = [
            _make_topic(f"S3-G32-{index}", f"עומק {index:02d}") for index in range(33)
        ]
        _bulk_broader([(included[index + 1], included[index]) for index in range(32)])
        near = _public_item("depth 32")
        _associate(near, included[32], "CENTRAL")
        closure = descendant_closure(load_approved_broader_graph(), included[0].pk)
        self.assertIn(included[32].pk, closure.topic_ids)
        self.assertEqual(closure.min_depth[included[32].pk], 32)
        self.assertFalse(closure.graph_truncated)
        self.assertFalse(closure.cycle_detected)
        self.assertTrue(
            inherited_items_queryset(
                included[0].pk,
                user=None,
                descendant_topic_ids=closure.topic_ids,
            )
            .filter(pk=near.pk)
            .exists()
        )
        far_topics = [
            _make_topic(f"S3-G33-{index}", f"ארוך {index:02d}") for index in range(34)
        ]
        _bulk_broader(
            [(far_topics[index + 1], far_topics[index]) for index in range(33)]
        )
        far = _public_item("depth 33")
        _associate(far, far_topics[33], "CENTRAL")
        far_closure = descendant_closure(
            load_approved_broader_graph(), far_topics[0].pk
        )
        self.assertNotIn(far_topics[33].pk, far_closure.topic_ids)
        self.assertIn(far_topics[32].pk, far_closure.topic_ids)
        self.assertTrue(far_closure.graph_truncated)
        self.assertFalse(far_closure.cycle_detected)
        self.assertFalse(
            inherited_items_queryset(
                far_topics[0].pk,
                user=None,
                descendant_topic_ids=far_closure.topic_ids,
            )
            .filter(pk=far.pk)
            .exists()
        )

    def test_g4_and_g5_stage1_walker_is_not_used_or_weakened(self) -> None:
        source = Path(academic_index_read.__file__).read_text(encoding="utf-8")
        self.assertNotIn("approved_descendant_ids", source)
        self.assertNotIn("_walk_children", source)
        self.assertNotIn("_walk_parents", source)
        service_source = Path(academic_index.__file__).read_text(encoding="utf-8")
        self.assertIn("A topic cannot be its own broader topic.", service_source)
        before = AcademicTopicRelation.objects.count()
        descendant_closure(BroaderGraph(parents={}, children={}), 1)
        self.assertEqual(AcademicTopicRelation.objects.count(), before)

    def test_g6_diamond_is_not_a_cycle_in_either_neighbor_order(self) -> None:
        topic_a = _make_topic("S3-G6-A", "אלף")
        topic_b = _make_topic("S3-G6-B", "בית")
        topic_c = _make_topic("S3-G6-C", "גימל")
        topic_d = _make_topic("S3-G6-D", "דלת")
        _broader(topic_b, topic_a)
        _broader(topic_c, topic_a)
        _broader(topic_d, topic_b)
        _broader(topic_d, topic_c)
        closure = descendant_closure(load_approved_broader_graph(), topic_a.pk)
        self.assertEqual(
            closure.topic_ids, frozenset({topic_b.pk, topic_c.pk, topic_d.pk})
        )
        self.assertEqual(closure.min_depth[topic_d.pk], 2)
        self.assertFalse(closure.cycle_detected)
        self.assertFalse(closure.graph_truncated)
        navigation = get_topic_navigation(topic_a.pk, user=self.staff, audience=_STAFF)
        self.assertFalse(navigation.cycle_detected)
        edges = [(2, 1), (3, 1), (4, 2), (4, 3)]
        forward = descendant_closure(_graph_from_edges(edges), 1)
        backward = descendant_closure(_reversed_graph(_graph_from_edges(edges)), 1)
        self.assertEqual(forward.topic_ids, frozenset({2, 3, 4}))
        self.assertEqual(forward.min_depth[4], 2)
        self.assertEqual(backward.topic_ids, forward.topic_ids)
        self.assertEqual(dict(backward.min_depth), dict(forward.min_depth))
        self.assertFalse(forward.cycle_detected)
        self.assertFalse(backward.cycle_detected)
        self.assertEqual(forward.graph_truncated, backward.graph_truncated)

    def test_g7_and_g8_a_long_path_does_not_hide_a_short_path(self) -> None:
        chain = [(index + 1, index) for index in range(32)]
        long_to_c = [(33, 32)]
        direct = [(33, 0)]
        for edges in (chain + long_to_c + direct, direct + chain + long_to_c):
            closure = descendant_closure(_graph_from_edges(edges), 0)
            self.assertEqual(closure.min_depth[33], 1)
            for index in range(1, 33):
                self.assertEqual(closure.min_depth[index], index)
            self.assertFalse(closure.graph_truncated)
            self.assertFalse(closure.cycle_detected)
            self.assertNotIn(0, closure.topic_ids)
            reversed_closure = descendant_closure(
                _reversed_graph(_graph_from_edges(edges)), 0
            )
            self.assertEqual(dict(reversed_closure.min_depth), dict(closure.min_depth))
            self.assertEqual(reversed_closure.cycle_detected, closure.cycle_detected)
            self.assertEqual(reversed_closure.graph_truncated, closure.graph_truncated)
        with_z = chain + long_to_c + direct + [(34, 32)]
        z_closure = descendant_closure(_graph_from_edges(with_z), 0)
        self.assertNotIn(34, z_closure.topic_ids)
        self.assertTrue(z_closure.graph_truncated)
        self.assertEqual(z_closure.min_depth[33], 1)
        self.assertFalse(z_closure.cycle_detected)

    def test_g9_a_return_edge_is_a_cycle_without_truncation(self) -> None:
        edges = [(index + 1, index) for index in range(32)]
        edges.append((1, 32))
        closure = descendant_closure(_graph_from_edges(edges), 0)
        self.assertEqual(closure.min_depth[1], 1)
        self.assertFalse(closure.graph_truncated)
        self.assertTrue(closure.cycle_detected)
        self.assertNotIn(0, closure.topic_ids)

    def test_g10_and_g11_deep_chains_do_not_recurse(self) -> None:
        edge_count = sys.getrecursionlimit() + 50
        edges = [(index + 1, index) for index in range(edge_count)]
        graph = _graph_from_edges(edges)
        with self.assertNumQueries(0):
            closure = descendant_closure(graph, 0)
            again = descendant_closure(graph, 0)
        self.assertIn(32, closure.topic_ids)
        self.assertNotIn(33, closure.topic_ids)
        self.assertTrue(closure.graph_truncated)
        self.assertFalse(closure.cycle_detected)
        self.assertEqual(dict(again.min_depth), dict(closure.min_depth))
        self.assertEqual(again.cycle_detected, False)
        back = _graph_from_edges(edges + [(0, edge_count)])
        with self.assertNumQueries(0):
            cycled = descendant_closure(back, 0)
        self.assertTrue(cycled.cycle_detected)
        self.assertEqual(cycled.topic_ids, closure.topic_ids)
        self.assertTrue(cycled.graph_truncated)
        self.assertNotIn(0, cycled.topic_ids)

    @override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
    def test_g12_public_results_hide_diagnostics_and_private_items(self) -> None:
        topic_a = _make_topic("S3-G12-A", "גלוי")
        topic_b = _make_topic("S3-G12-B", "חסוי", candidate=False)
        public_item = _public_item("g12 public")
        private_item = _public_item(
            "g12 private", visibility=ArchiveItem.Visibility.PRIVATE
        )
        _associate(public_item, topic_a, "CENTRAL")
        _associate(private_item, topic_a, "SECONDARY")
        event = _readable_event(AcademicIndexDecisionEvent.Action.IMPORT)
        chain = [
            _make_topic(f"S3-G12-H{index}", f"שרשרת {index}", candidate=False)
            for index in range(33)
        ]
        _bulk_broader([(topic_b, topic_a), (topic_a, topic_b)], event)
        _bulk_broader(
            [(chain[0], topic_a)]
            + [(chain[index + 1], chain[index]) for index in range(32)],
            event,
        )
        detail = get_topic_detail(topic_a.pk, user=None, audience=_PUBLIC)
        navigation = get_topic_navigation(topic_a.pk, user=None, audience=_PUBLIC)
        self.assertIsInstance(detail, PublicTopicDetail)
        assert isinstance(detail, PublicTopicDetail)
        self.assertIsInstance(navigation, PublicTopicNavigation)
        assert isinstance(navigation, PublicTopicNavigation)
        self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(detail)))
        self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(navigation)))
        self.assertNotIn(topic_b.pk, _scalars(detail))
        self.assertNotIn(
            private_item.pk,
            _ids(list_combined_items(topic_a.pk, user=None, audience=_PUBLIC)),
        )
        staff = get_topic_navigation(topic_a.pk, user=self.staff, audience=_STAFF)
        self.assertTrue(staff.cycle_detected)
        self.assertTrue(staff.graph_truncated)
        self.assertIn(
            private_item.pk,
            _ids(list_direct_items(topic_a.pk, user=self.staff, audience=_STAFF)),
        )


class AcademicIndexAuthorizationTests(TestCase):
    staff: User
    superuser: User
    topic_id: int

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("auth-staff", is_staff=True, is_superuser=False)
        cls.superuser = _user("auth-super", is_superuser=True, is_staff=False)
        cls.topic_id = AcademicTopic.objects.get(stable_key="AI-01").pk

    def _assert_denied(self, user, topic_id: int) -> None:
        calls = (
            lambda: get_topic_detail(topic_id, user=user, audience=_STAFF),
            lambda: get_topic_navigation(topic_id, user=user, audience=_STAFF),
            lambda: list_direct_items(topic_id, user=user, audience=_STAFF),
            lambda: list_inherited_items(topic_id, user=user, audience=_STAFF),
            lambda: list_combined_items(topic_id, user=user, audience=_STAFF),
            lambda: list_related_topics(topic_id, user=user, audience=_STAFF),
            lambda: item_provenance(topic_id, [264], user=user, audience=_STAFF),
        )
        for call in calls:
            with CaptureQueriesContext(connection) as captured:
                with self.assertRaises(PermissionDenied):
                    call()
            self.assertEqual(captured.captured_queries, [])

    def test_a1_a2_and_a6_unauthorized_staff_queries_nothing(self) -> None:
        family = _family_user()
        restricted = _restricted_user()
        plain = _user("plain")
        for user in (None, plain, family, restricted):
            self._assert_denied(user, self.topic_id)
            self._assert_denied(user, 999999)

    @override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
    def test_a3_and_a4_administrators_do_not_bypass_restricted_items(self) -> None:
        hidden = _make_topic("S3-A3", "לא מועמד", candidate=False)
        self.assertIsNotNone(
            get_topic_detail(hidden.pk, user=self.staff, audience=_STAFF)
        )
        self.assertIsNotNone(
            get_topic_detail(hidden.pk, user=self.superuser, audience=_STAFF)
        )
        item = _public_item(
            "restricted auth", visibility=ArchiveItem.Visibility.RESTRICTED
        )
        _associate(item, hidden, "CENTRAL")
        self.assertIsNotNone(
            get_topic_detail(hidden.pk, user=self.staff, audience=_STAFF)
        )
        self.assertNotIn(
            item.pk,
            _ids(list_direct_items(hidden.pk, user=self.staff, audience=_STAFF)),
        )
        provenance = item_provenance(
            hidden.pk, [item.pk], user=self.staff, audience=_STAFF
        )
        self.assertNotIn(item.pk, provenance)

    def test_a5_and_f2_missing_topics_are_not_permission_errors(self) -> None:
        self.assertFalse(public_index_enabled())
        self.assertIsNotNone(
            get_topic_detail(self.topic_id, user=self.staff, audience=_STAFF)
        )
        with CaptureQueriesContext(connection) as captured:
            self.assertIsNone(
                get_topic_detail(self.topic_id, user=self.staff, audience=_PUBLIC)
            )
            self.assertEqual(list_public_topics(self.staff), ((), 0))
        self.assertEqual(captured.captured_queries, [])
        with override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True):
            for audience, user in ((_PUBLIC, None), (_STAFF, self.staff)):
                with self.assertNumQueries(1):
                    self.assertIsNone(
                        get_topic_detail(999999, user=user, audience=audience)
                    )

    @override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
    def test_f3_f4_f6_and_f7_projection_boundaries(self) -> None:
        hidden = _make_topic("S3-F3", "חסוי", candidate=False)
        _associate(_public_item("f3"), hidden, "CENTRAL")
        with self.assertNumQueries(1):
            self.assertIsNone(get_topic_detail(hidden.pk, user=None, audience=_PUBLIC))
        private = _public_item("f4", visibility=ArchiveItem.Visibility.PRIVATE)
        topic = _make_topic("S3-F4", "פער")
        _associate(private, topic, "CENTRAL")
        family = _family_user()
        self.assertEqual(
            _ids(list_direct_items(topic.pk, user=family, audience=_PUBLIC)),
            {private.pk},
        )
        self.assertIsNone(get_topic_detail(topic.pk, user=None, audience=_PUBLIC))
        seed = _topic("AI-06")
        public_page = list_inherited_items(seed.pk, user=None, audience=_PUBLIC)
        self.assertTrue(_FORBIDDEN_PUBLIC_NAMES.isdisjoint(_field_names(public_page)))
        self.assertTrue(
            _FORBIDDEN_PUBLIC_NAMES.isdisjoint(
                _field_names(get_topic_navigation(seed.pk, user=None, audience=_PUBLIC))
            )
        )
        staff_record = item_provenance(seed.pk, [13], user=self.staff, audience=_STAFF)[
            13
        ]
        self.assertTrue(
            all(isinstance(path.association_id, int) for path in staff_record.paths)
        )
        self.assertTrue(
            all(isinstance(path.decision_event_id, int) for path in staff_record.paths)
        )
        self.assertTrue(all(path.depth == 1 for path in staff_record.paths))
        self.assertFalse(hasattr(staff_record.paths[0], "evidence_note"))
        staff_navigation = get_topic_navigation(
            seed.pk, user=self.staff, audience=_STAFF
        )
        self.assertFalse(staff_navigation.cycle_detected)
        self.assertFalse(staff_navigation.graph_truncated)

    @override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
    def test_f5_public_sql_does_not_select_evidence_or_metadata(self) -> None:
        with CaptureQueriesContext(connection) as captured:
            list_public_topics(None, search_query="מצרים")
            list_combined_items(
                _topic("AI-01").pk, user=None, audience=_PUBLIC, per_page=48
            )
            get_topic_navigation(_topic("AI-06").pk, user=None, audience=_PUBLIC)
        forbidden = (
            "insert",
            "update",
            "delete",
            "evidence_note",
            "snapshot",
            "donor",
            "original_location",
            "documents_documentmetadata",
        )
        for query in captured.captured_queries:
            sql = " ".join(query["sql"].lower().split())
            verb = sql.split(None, 1)[0]
            self.assertIn(verb, {"select", "savepoint", "release"})
            for token in forbidden:
                if token in {"insert", "update", "delete"}:
                    self.assertFalse(sql.startswith(token))
                else:
                    self.assertNotIn(token, sql)


@override_settings(ACADEMIC_INDEX_PUBLIC_ENABLED=True)
class AcademicIndexQueryAndWriteTests(TestCase):
    staff: User

    @classmethod
    def setUpTestData(cls) -> None:
        _apply_seed()
        cls.staff = _user("query-staff", is_staff=True)

    def test_q1_and_q2_direct_page_query_count_does_not_grow_with_items(self) -> None:
        topic = _topic("AI-01")
        with CaptureQueriesContext(connection) as before:
            list_direct_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
        self.assertLessEqual(len(before.captured_queries), 8)
        for index in range(20):
            _associate(_public_item(f"extra {index}"), topic, "SECONDARY")
        with CaptureQueriesContext(connection) as after:
            page = list_direct_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
        self.assertEqual(len(after.captured_queries), len(before.captured_queries))
        self.assertGreaterEqual(page.total_count, 22)

    def test_q3_directory_query_count_does_not_grow_per_topic(self) -> None:
        with CaptureQueriesContext(connection) as before:
            list_public_topics(None, per_page=48)
        extra = _make_topic("S3-Q3", "נוסף")
        _associate(_public_item("q3"), extra, "CENTRAL")
        with CaptureQueriesContext(connection) as after:
            list_public_topics(None, per_page=48)
        self.assertEqual(len(before.captured_queries), len(after.captured_queries))
        self.assertLessEqual(len(after.captured_queries), 4)

    def test_q4_and_q6_counts_follow_items_not_paths_or_hops(self) -> None:
        descendants = descendant_closure(
            load_approved_broader_graph(), _topic("AI-06").pk
        ).topic_ids
        self.assertEqual(
            inherited_items_queryset(
                _topic("AI-06").pk,
                user=None,
                descendant_topic_ids=descendants,
            ).count(),
            2,
        )
        short = [_make_topic(f"S3-Q6S-{index}", f"קצר {index}") for index in range(3)]
        _bulk_broader([(short[1], short[0]), (short[2], short[1])])
        long = [
            _make_topic(f"S3-Q6L-{index}", f"ארוך {index:02d}") for index in range(34)
        ]
        _bulk_broader([(long[index + 1], long[index]) for index in range(33)])
        get_topic_navigation(short[0].pk, user=self.staff, audience=_STAFF)
        with CaptureQueriesContext(connection) as short_queries:
            get_topic_navigation(short[0].pk, user=self.staff, audience=_STAFF)
        with CaptureQueriesContext(connection) as long_queries:
            get_topic_navigation(long[0].pk, user=self.staff, audience=_STAFF)
        self.assertEqual(
            len(short_queries.captured_queries), len(long_queries.captured_queries)
        )
        self.assertLess(len(long_queries.captured_queries), 33)
        edge_count = sys.getrecursionlimit() + 50
        with self.assertNumQueries(0):
            descendant_closure(
                _graph_from_edges([(index + 1, index) for index in range(edge_count)]),
                0,
            )

    def test_w1_through_w7_reads_do_not_write(self) -> None:
        counts = {
            "events": AcademicIndexDecisionEvent.objects.count(),
            "associations": ArchiveItemTopicAssociation.objects.count(),
            "topics": AcademicTopic.objects.count(),
            "items": ArchiveItem.objects.count(),
        }
        topics = list(AcademicTopic.objects.filter(stable_key__startswith="AI-"))
        with CaptureQueriesContext(connection) as captured:
            for topic in topics:
                get_topic_detail(topic.pk, user=self.staff, audience=_STAFF)
                get_topic_navigation(topic.pk, user=self.staff, audience=_STAFF)
                list_direct_items(
                    topic.pk, user=self.staff, audience=_STAFF, per_page=48
                )
                list_inherited_items(
                    topic.pk, user=self.staff, audience=_STAFF, per_page=48
                )
                list_combined_items(
                    topic.pk, user=self.staff, audience=_STAFF, per_page=48
                )
                list_related_topics(topic.pk, user=self.staff, audience=_STAFF)
                get_topic_detail(topic.pk, user=None, audience=_PUBLIC)
                get_topic_navigation(topic.pk, user=None, audience=_PUBLIC)
                list_direct_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
                list_inherited_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
                list_combined_items(topic.pk, user=None, audience=_PUBLIC, per_page=48)
                list_related_topics(topic.pk, user=None, audience=_PUBLIC)
                item_provenance(topic.pk, [13], user=self.staff, audience=_STAFF)
            list_public_topics(None)
        for query in captured.captured_queries:
            verb = query["sql"].lstrip().split(None, 1)[0].upper()
            self.assertNotIn(verb, {"INSERT", "UPDATE", "DELETE"})
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), counts["events"])
        self.assertEqual(
            ArchiveItemTopicAssociation.objects.count(), counts["associations"]
        )
        self.assertEqual(AcademicTopic.objects.count(), counts["topics"])
        self.assertEqual(ArchiveItem.objects.count(), counts["items"])
        get_topic_detail(_topic("AI-01").pk, user=None, audience=_PUBLIC)
        with self.assertRaises(PermissionDenied):
            get_topic_detail(_topic("AI-01").pk, user=None, audience=_STAFF)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), counts["events"])
