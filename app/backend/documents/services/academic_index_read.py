"""Read and query the controlled academic index.

This module does not publish the index. It does not write topics, edges,
associations, or decision events, and it does not register a route or a
feature-flag setting.

Readable rows are approved, point at a decision event, and that event's
action is IMPORT, APPROVE, or SUPERSEDE. BROADER is the only hierarchy.
RELATED is a cross-link and is not walked. Item visibility is
``archive_browse_queryset_for_user``. Audience ``STAFF`` is a projection,
not a permission: the caller must already be an archive administrator.
"""

from __future__ import annotations

import enum
from collections import deque
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q, QuerySet

from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicLabel,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.models import ArchiveItem
from documents.services.academic_index import MAX_TOPIC_GRAPH_DEPTH
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.archive_item_presentation import (
    normalize_archive_public_list_page,
)
from documents.services.document_access import is_document_admin
from documents.services.hebrew_display_order import hebrew_public_sort_key

READABLE_EVENT_ACTIONS = (
    AcademicIndexDecisionEvent.Action.IMPORT,
    AcademicIndexDecisionEvent.Action.APPROVE,
    AcademicIndexDecisionEvent.Action.SUPERSEDE,
)

_STAFF_DENIED = "Academic index staff reads require an archive administrator."
_DEFAULT_PER_PAGE = 48
_WHITE = 0
_GRAY = 1
_BLACK = 2


class AcademicIndexAudience(enum.Enum):
    PUBLIC = "public"
    STAFF = "staff"


class ItemAppearance(enum.Enum):
    DIRECT = "direct"
    INHERITED = "inherited"


@dataclass(frozen=True, slots=True)
class TopicRef:
    topic_id: int
    stable_key: str
    preferred_label: str


@dataclass(frozen=True, slots=True)
class SiblingGroup:
    shared_parent: TopicRef
    siblings: tuple[TopicRef, ...]


@dataclass(frozen=True, slots=True)
class PublicAssociationPath:
    """Eligible source topic only. No graph distance and no audit ids."""

    topic_id: int
    stable_key: str
    preferred_label: str
    relevance: str


@dataclass(frozen=True, slots=True)
class StaffAssociationPath:
    """Readable stored association, including a topic that is not publicly eligible."""

    topic_id: int
    stable_key: str
    preferred_label: str
    relevance: str
    depth: int
    association_id: int
    decision_event_id: int


@dataclass(frozen=True, slots=True)
class PublicTopicItemRecord:
    archive_item_id: int
    appearance: ItemAppearance
    direct_relevance: str | None
    paths: tuple[PublicAssociationPath, ...]


@dataclass(frozen=True, slots=True)
class StaffTopicItemRecord:
    archive_item_id: int
    appearance: ItemAppearance
    direct_relevance: str | None
    paths: tuple[StaffAssociationPath, ...]


@dataclass(frozen=True, slots=True)
class PublicTopicItemPage:
    total_count: int
    page: int
    per_page: int
    records: tuple[PublicTopicItemRecord, ...]


@dataclass(frozen=True, slots=True)
class StaffTopicItemPage:
    total_count: int
    page: int
    per_page: int
    records: tuple[StaffTopicItemRecord, ...]


@dataclass(frozen=True, slots=True)
class PublicTopicNavigation:
    parents: tuple[TopicRef, ...]
    children: tuple[TopicRef, ...]
    ancestors: tuple[TopicRef, ...]
    descendants: tuple[TopicRef, ...]
    sibling_groups: tuple[SiblingGroup, ...]
    related: tuple[TopicRef, ...]


@dataclass(frozen=True, slots=True)
class StaffTopicNavigation:
    parents: tuple[TopicRef, ...]
    children: tuple[TopicRef, ...]
    ancestors: tuple[TopicRef, ...]
    descendants: tuple[TopicRef, ...]
    sibling_groups: tuple[SiblingGroup, ...]
    related: tuple[TopicRef, ...]
    graph_truncated: bool
    cycle_detected: bool


@dataclass(frozen=True, slots=True)
class PublicTopicDetail:
    topic_id: int
    stable_key: str
    preferred_label: str
    scope_note: str
    alternate_labels: tuple[str, ...]
    is_public_candidate: bool
    navigation: PublicTopicNavigation
    visible_item_count: int


@dataclass(frozen=True, slots=True)
class StaffTopicDetail:
    topic_id: int
    stable_key: str
    preferred_label: str
    scope_note: str
    alternate_labels: tuple[str, ...]
    is_public_candidate: bool
    navigation: StaffTopicNavigation
    visible_item_count: int


@dataclass(frozen=True, slots=True)
class PublicTopicListRow:
    topic_id: int
    stable_key: str
    preferred_label: str
    visible_item_count: int
    parent_topic_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BroaderGraph:
    """In-memory readable BROADER adjacency. Not a public response."""

    parents: Mapping[int, Collection[int]]
    children: Mapping[int, Collection[int]]


@dataclass(frozen=True, slots=True)
class GraphClosure:
    """Internal walk result. Not returned by an audience-facing function."""

    topic_ids: frozenset[int]
    min_depth: Mapping[int, int]
    cycle_detected: bool
    graph_truncated: bool


@dataclass(frozen=True, slots=True)
class _TopicIdentity:
    topic_id: int
    stable_key: str
    preferred_label: str
    is_public_candidate: bool


@dataclass(frozen=True, slots=True)
class _TopicView:
    topic: AcademicTopic
    graph: BroaderGraph
    descendants: GraphClosure
    ancestors: GraphClosure
    visible_count: int
    related_ids: tuple[int, ...]
    identities: Mapping[int, _TopicIdentity]
    eligible_ids: frozenset[int] | None


def public_index_enabled() -> bool:
    """True only when ``settings.ACADEMIC_INDEX_PUBLIC_ENABLED`` is true.

    A missing setting is false. Stage 3A does not add the setting.
    """

    return bool(getattr(settings, "ACADEMIC_INDEX_PUBLIC_ENABLED", False))


def load_approved_broader_graph() -> BroaderGraph:
    """One query of readable BROADER edges. Does not follow RELATED or raise."""

    parents: dict[int, set[int]] = {}
    children: dict[int, set[int]] = {}
    edges = AcademicTopicRelation.objects.filter(
        relation_kind=AcademicTopicRelation.Kind.BROADER,
        status=AcademicTopicRelation.Status.APPROVED,
        decision_event__isnull=False,
        decision_event__action__in=READABLE_EVENT_ACTIONS,
    ).values_list("narrower_id", "broader_id")
    for narrower_id, broader_id in edges:
        if narrower_id is None or broader_id is None:
            continue
        parents.setdefault(narrower_id, set()).add(broader_id)
        children.setdefault(broader_id, set()).add(narrower_id)
    return BroaderGraph(
        parents={topic_id: frozenset(values) for topic_id, values in parents.items()},
        children={topic_id: frozenset(values) for topic_id, values in children.items()},
    )


def descendant_closure(graph: BroaderGraph, topic_id: int) -> GraphClosure:
    """Topics reached by child edges, excluding ``topic_id``."""

    return _directed_closure(graph.children, topic_id)


def ancestor_closure(graph: BroaderGraph, topic_id: int) -> GraphClosure:
    """Topics reached by parent edges, excluding ``topic_id``."""

    return _directed_closure(graph.parents, topic_id)


def publicly_eligible_topic_ids(
    graph: BroaderGraph,
    *,
    user,
    candidate_ids: frozenset[int],
    visible_direct_items_by_topic: Mapping[int, Collection[int]],
) -> frozenset[int]:
    """Candidate topics whose rolled-up browse-visible set is non-empty.

    The flag gates the result. Roll-up uses the approved ancestor closure and
    does not consult whether a descendant is itself eligible.
    """

    if not public_index_enabled():
        return frozenset()
    rolled = _roll_up_visible_items(graph, visible_direct_items_by_topic)
    return frozenset(topic_id for topic_id in candidate_ids if rolled.get(topic_id))


def get_topic_detail(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
) -> PublicTopicDetail | StaffTopicDetail | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=True)
    if view is None:
        return None
    labels = _alternate_labels(view.topic.pk)
    navigation = _navigation(view, audience)
    if isinstance(navigation, PublicTopicNavigation):
        return PublicTopicDetail(
            topic_id=view.topic.pk,
            stable_key=view.topic.stable_key,
            preferred_label=view.topic.preferred_label,
            scope_note=view.topic.scope_note,
            alternate_labels=labels,
            is_public_candidate=True,
            navigation=navigation,
            visible_item_count=view.visible_count,
        )
    return StaffTopicDetail(
        topic_id=view.topic.pk,
        stable_key=view.topic.stable_key,
        preferred_label=view.topic.preferred_label,
        scope_note=view.topic.scope_note,
        alternate_labels=labels,
        is_public_candidate=view.topic.is_public_candidate,
        navigation=navigation,
        visible_item_count=view.visible_count,
    )


def get_topic_navigation(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
) -> PublicTopicNavigation | StaffTopicNavigation | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=True)
    if view is None:
        return None
    navigation = _navigation(view, audience)
    if audience is AcademicIndexAudience.PUBLIC:
        assert isinstance(navigation, PublicTopicNavigation)
        return navigation
    assert isinstance(navigation, StaffTopicNavigation)
    return navigation


def direct_items_queryset(topic_id: int, *, user) -> QuerySet[ArchiveItem]:
    """Browse-visible items with a readable association on ``topic_id``."""

    return (
        archive_browse_queryset_for_user(user)
        .filter(_readable_association_exists((topic_id,)))
        .order_by("-created_at", "pk")
    )


def inherited_items_queryset(
    topic_id: int,
    *,
    user,
    descendant_topic_ids: frozenset[int],
) -> QuerySet[ArchiveItem]:
    """Browse-visible descendant items, excluding the topic's own direct items."""

    if not descendant_topic_ids:
        return ArchiveItem.objects.none()
    return (
        archive_browse_queryset_for_user(user)
        .filter(_readable_association_exists(descendant_topic_ids))
        .filter(~_readable_association_exists((topic_id,)))
        .order_by("-created_at", "pk")
    )


def combined_items_queryset(
    topic_id: int,
    *,
    user,
    descendant_topic_ids: frozenset[int],
) -> QuerySet[ArchiveItem]:
    """Distinct browse-visible items that are direct or inherited."""

    topic_ids = set(descendant_topic_ids)
    topic_ids.add(topic_id)
    return (
        archive_browse_queryset_for_user(user)
        .filter(_readable_association_exists(topic_ids))
        .order_by("-created_at", "pk")
    )


def list_direct_items(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
    page: int | str = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> PublicTopicItemPage | StaffTopicItemPage | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=False)
    if view is None:
        return None
    return _item_page(
        view,
        user=user,
        audience=audience,
        queryset=direct_items_queryset(topic_id, user=user),
        page=page,
        per_page=per_page,
    )


def list_inherited_items(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
    page: int | str = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> PublicTopicItemPage | StaffTopicItemPage | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=False)
    if view is None:
        return None
    return _item_page(
        view,
        user=user,
        audience=audience,
        queryset=inherited_items_queryset(
            topic_id,
            user=user,
            descendant_topic_ids=view.descendants.topic_ids,
        ),
        page=page,
        per_page=per_page,
    )


def list_combined_items(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
    page: int | str = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> PublicTopicItemPage | StaffTopicItemPage | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=False)
    if view is None:
        return None
    return _item_page(
        view,
        user=user,
        audience=audience,
        queryset=combined_items_queryset(
            topic_id,
            user=user,
            descendant_topic_ids=view.descendants.topic_ids,
        ),
        page=page,
        per_page=per_page,
    )


def item_provenance(
    topic_id: int,
    item_ids: Sequence[int],
    *,
    user,
    audience: AcademicIndexAudience,
) -> dict[int, PublicTopicItemRecord] | dict[int, StaffTopicItemRecord]:
    """Paths for browse-visible items that belong to this topic.

    Descendant membership is the approved readable BROADER closure of
    ``topic_id``, capped at ``MAX_TOPIC_GRAPH_DEPTH``. A caller cannot
    supply that set.
    """

    _reject_unauthorized_staff(user, audience)
    if _public_disabled(audience):
        return {}
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=False)
    if view is None or not item_ids:
        return {}
    visible_ids = list(
        archive_browse_queryset_for_user(user)
        .filter(pk__in=list(item_ids))
        .values_list("pk", flat=True)
    )
    if not visible_ids:
        return {}
    return _records_for_item_ids(
        view,
        user=user,
        audience=audience,
        item_ids=visible_ids,
        direct_paths_empty=audience is AcademicIndexAudience.PUBLIC,
        preserve_order=True,
    )


def list_related_topics(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
) -> tuple[TopicRef, ...] | None:
    view = _topic_view(topic_id, user=user, audience=audience, neighbors=True)
    if view is None:
        return None
    allowed = _allowed_ids(view, audience)
    return _refs(view.related_ids, view.identities, allowed)


def list_public_topics(
    user,
    *,
    search_query: str = "",
    page: int | str = 1,
    per_page: int = _DEFAULT_PER_PAGE,
) -> tuple[tuple[PublicTopicListRow, ...], int]:
    """Publicly eligible topics and the total eligible count."""

    if not public_index_enabled():
        return (), 0
    graph = load_approved_broader_graph()
    visible_direct = _visible_direct_items_by_topic(user)
    rolled = _roll_up_visible_items(graph, visible_direct)
    possible_ids = [topic_id for topic_id, item_ids in rolled.items() if item_ids]
    if not possible_ids:
        return (), 0
    loaded = list(
        AcademicTopic.objects.filter(
            id__in=possible_ids,
            is_public_candidate=True,
        ).values("id", "stable_key", "preferred_label")
    )
    identities = {
        row["id"]: _TopicIdentity(
            topic_id=row["id"],
            stable_key=row["stable_key"],
            preferred_label=row["preferred_label"],
            is_public_candidate=True,
        )
        for row in loaded
    }
    eligible_ids = frozenset(identities)
    query = search_query.strip()
    alternate_hits: set[int] = set()
    if query and eligible_ids:
        alternate_hits = set(
            AcademicTopicLabel.objects.filter(
                topic_id__in=eligible_ids,
                label__icontains=query,
            ).values_list("topic_id", flat=True)
        )
    rows = []
    for identity in identities.values():
        item_ids = rolled.get(identity.topic_id)
        if not item_ids:
            continue
        if query and not _label_contains(identity.preferred_label, query):
            if identity.topic_id not in alternate_hits:
                continue
        rows.append(
            PublicTopicListRow(
                topic_id=identity.topic_id,
                stable_key=identity.stable_key,
                preferred_label=identity.preferred_label,
                visible_item_count=len(item_ids),
                parent_topic_ids=_parent_ids(
                    identity.topic_id,
                    graph,
                    identities,
                    eligible_ids,
                ),
            )
        )
    rows.sort(key=_list_row_sort_key)
    per_page_value = _normalize_per_page(per_page)
    total_count = len(rows)
    page_number = _normalize_page(
        page, total_count=total_count, per_page=per_page_value
    )
    offset = (page_number - 1) * per_page_value
    return tuple(rows[offset : offset + per_page_value]), total_count


def _reject_unauthorized_staff(user, audience: AcademicIndexAudience) -> None:
    if audience is AcademicIndexAudience.STAFF and not is_document_admin(user):
        raise PermissionDenied(_STAFF_DENIED)


def _public_disabled(audience: AcademicIndexAudience) -> bool:
    return audience is AcademicIndexAudience.PUBLIC and not public_index_enabled()


def _topic_view(
    topic_id: int,
    *,
    user,
    audience: AcademicIndexAudience,
    neighbors: bool,
) -> _TopicView | None:
    _reject_unauthorized_staff(user, audience)
    if _public_disabled(audience):
        return None
    topic = AcademicTopic.objects.filter(pk=topic_id).first()
    if topic is None:
        return None
    if audience is AcademicIndexAudience.PUBLIC and not topic.is_public_candidate:
        return None
    graph = load_approved_broader_graph()
    descendants = descendant_closure(graph, topic.pk)
    ancestors = ancestor_closure(graph, topic.pk)
    visible_count = combined_items_queryset(
        topic.pk,
        user=user,
        descendant_topic_ids=descendants.topic_ids,
    ).count()
    if audience is AcademicIndexAudience.PUBLIC and visible_count == 0:
        return None
    if not neighbors:
        return _TopicView(
            topic=topic,
            graph=graph,
            descendants=descendants,
            ancestors=ancestors,
            visible_count=visible_count,
            related_ids=(),
            identities={},
            eligible_ids=None,
        )
    related_ids = _related_topic_ids(topic.pk)
    identity_ids = _structural_neighbor_ids(
        topic.pk,
        graph,
        descendants,
        ancestors,
        related_ids,
    )
    identities = _load_identities(identity_ids)
    eligible_ids = None
    if audience is AcademicIndexAudience.PUBLIC:
        eligible_ids = _eligible_for_loaded(graph, user, identities, topic)
    return _TopicView(
        topic=topic,
        graph=graph,
        descendants=descendants,
        ancestors=ancestors,
        visible_count=visible_count,
        related_ids=related_ids,
        identities=identities,
        eligible_ids=eligible_ids,
    )


def _directed_closure(
    adjacency: Mapping[int, Collection[int]],
    start: int,
) -> GraphClosure:
    min_depth = {start: 0}
    queue: deque[int] = deque((start,))
    graph_truncated = False
    while queue:
        current = queue.popleft()
        base_depth = min_depth[current]
        for neighbor in adjacency.get(current, ()):
            next_depth = base_depth + 1
            if next_depth > MAX_TOPIC_GRAPH_DEPTH:
                if neighbor not in min_depth:
                    graph_truncated = True
                continue
            if neighbor not in min_depth:
                min_depth[neighbor] = next_depth
                queue.append(neighbor)
    topic_ids = frozenset(topic_id for topic_id in min_depth if topic_id != start)
    return GraphClosure(
        topic_ids=topic_ids,
        min_depth=MappingProxyType(min_depth),
        cycle_detected=_cycle_detected(adjacency, start),
        graph_truncated=graph_truncated,
    )


def _cycle_detected(adjacency: Mapping[int, Collection[int]], start: int) -> bool:
    """Iterative white/gray/black walk. A gray target is a back edge."""

    color = {start: _GRAY}
    stack: list[tuple[int, Iterator[int]]] = [(start, iter(adjacency.get(start, ())))]
    cycle_detected = False
    while stack:
        node, neighbors = stack[-1]
        try:
            nxt = next(neighbors)
        except StopIteration:
            color[node] = _BLACK
            stack.pop()
            continue
        state = color.get(nxt, _WHITE)
        if state == _GRAY:
            cycle_detected = True
        elif state == _WHITE:
            color[nxt] = _GRAY
            stack.append((nxt, iter(adjacency.get(nxt, ()))))
    return cycle_detected


def _readable_association_exists(topic_ids: Collection[int]) -> Exists:
    return Exists(
        ArchiveItemTopicAssociation.objects.filter(
            archive_item_id=OuterRef("pk"),
            topic_id__in=topic_ids,
            status=ArchiveItemTopicAssociation.Status.APPROVED,
            decision_event__isnull=False,
            decision_event__action__in=READABLE_EVENT_ACTIONS,
        )
    )


def _roll_up_visible_items(
    graph: BroaderGraph,
    visible_direct_items_by_topic: Mapping[int, Collection[int]],
) -> dict[int, set[int]]:
    rolled: dict[int, set[int]] = {}
    for topic_id, item_ids in visible_direct_items_by_topic.items():
        if not item_ids:
            continue
        rolled.setdefault(topic_id, set()).update(item_ids)
        for ancestor_id in ancestor_closure(graph, topic_id).topic_ids:
            rolled.setdefault(ancestor_id, set()).update(item_ids)
    return rolled


def _visible_direct_items_by_topic(user) -> dict[int, set[int]]:
    grouped: dict[int, set[int]] = {}
    rows = ArchiveItemTopicAssociation.objects.filter(
        status=ArchiveItemTopicAssociation.Status.APPROVED,
        decision_event__isnull=False,
        decision_event__action__in=READABLE_EVENT_ACTIONS,
        archive_item_id__in=archive_browse_queryset_for_user(user).values("pk"),
    ).values_list("topic_id", "archive_item_id")
    for topic_id, archive_item_id in rows:
        grouped.setdefault(topic_id, set()).add(archive_item_id)
    return grouped


def _related_topic_ids(topic_id: int) -> tuple[int, ...]:
    rows = AcademicTopicRelation.objects.filter(
        relation_kind=AcademicTopicRelation.Kind.RELATED,
        status=AcademicTopicRelation.Status.APPROVED,
        decision_event__isnull=False,
        decision_event__action__in=READABLE_EVENT_ACTIONS,
    ).filter(Q(topic_low_id=topic_id) | Q(topic_high_id=topic_id))
    neighbor_ids: list[int] = []
    for low_id, high_id in rows.values_list("topic_low_id", "topic_high_id"):
        neighbor_id = high_id if low_id == topic_id else low_id
        if neighbor_id is not None and neighbor_id != topic_id:
            neighbor_ids.append(neighbor_id)
    return tuple(dict.fromkeys(neighbor_ids))


def _structural_neighbor_ids(
    topic_id: int,
    graph: BroaderGraph,
    descendants: GraphClosure,
    ancestors: GraphClosure,
    related_ids: Sequence[int],
) -> set[int]:
    neighbor_ids = set(descendants.topic_ids)
    neighbor_ids.update(ancestors.topic_ids)
    neighbor_ids.update(related_ids)
    for parent_id in graph.parents.get(topic_id, ()):
        neighbor_ids.update(graph.children.get(parent_id, ()))
    neighbor_ids.discard(topic_id)
    return neighbor_ids


def _load_identities(topic_ids: Collection[int]) -> dict[int, _TopicIdentity]:
    if not topic_ids:
        return {}
    rows = AcademicTopic.objects.filter(pk__in=topic_ids).values(
        "id",
        "stable_key",
        "preferred_label",
        "is_public_candidate",
    )
    return {
        row["id"]: _TopicIdentity(
            topic_id=row["id"],
            stable_key=row["stable_key"],
            preferred_label=row["preferred_label"],
            is_public_candidate=row["is_public_candidate"],
        )
        for row in rows
    }


def _eligible_for_loaded(
    graph: BroaderGraph,
    user,
    identities: Mapping[int, _TopicIdentity],
    topic: AcademicTopic,
) -> frozenset[int]:
    candidate_ids = {
        identity.topic_id
        for identity in identities.values()
        if identity.is_public_candidate
    }
    if topic.is_public_candidate:
        candidate_ids.add(topic.pk)
    return publicly_eligible_topic_ids(
        graph,
        user=user,
        candidate_ids=frozenset(candidate_ids),
        visible_direct_items_by_topic=_visible_direct_items_by_topic(user),
    )


def _alternate_labels(topic_id: int) -> tuple[str, ...]:
    labels = list(
        AcademicTopicLabel.objects.filter(topic_id=topic_id).values_list("label", "id")
    )
    labels.sort(key=lambda row: (hebrew_public_sort_key(row[0]), row[1]))
    return tuple(label for label, _label_id in labels)


def _navigation(
    view: _TopicView,
    audience: AcademicIndexAudience,
) -> PublicTopicNavigation | StaffTopicNavigation:
    allowed = _allowed_ids(view, audience)
    parents = _depth_one_ids(view.graph.parents, view.topic.pk, view.ancestors)
    children = _depth_one_ids(view.graph.children, view.topic.pk, view.descendants)
    parents_refs = _refs(parents, view.identities, allowed)
    children_refs = _refs(children, view.identities, allowed)
    ancestor_refs = _refs(view.ancestors.topic_ids, view.identities, allowed)
    descendant_refs = _refs(view.descendants.topic_ids, view.identities, allowed)
    sibling_groups = _sibling_groups(view, allowed)
    related_refs = _refs(view.related_ids, view.identities, allowed)
    if audience is AcademicIndexAudience.PUBLIC:
        return PublicTopicNavigation(
            parents=parents_refs,
            children=children_refs,
            ancestors=ancestor_refs,
            descendants=descendant_refs,
            sibling_groups=sibling_groups,
            related=related_refs,
        )
    return StaffTopicNavigation(
        parents=parents_refs,
        children=children_refs,
        ancestors=ancestor_refs,
        descendants=descendant_refs,
        sibling_groups=sibling_groups,
        related=related_refs,
        graph_truncated=view.descendants.graph_truncated
        or view.ancestors.graph_truncated,
        cycle_detected=view.descendants.cycle_detected or view.ancestors.cycle_detected,
    )


def _allowed_ids(
    view: _TopicView, audience: AcademicIndexAudience
) -> frozenset[int] | None:
    if audience is AcademicIndexAudience.PUBLIC:
        return view.eligible_ids or frozenset()
    return None


def _depth_one_ids(
    adjacency: Mapping[int, Collection[int]],
    topic_id: int,
    closure: GraphClosure,
) -> tuple[int, ...]:
    return tuple(
        neighbor_id
        for neighbor_id in adjacency.get(topic_id, ())
        if closure.min_depth.get(neighbor_id) == 1
    )


def _refs(
    topic_ids: Collection[int],
    identities: Mapping[int, _TopicIdentity],
    allowed: frozenset[int] | None,
) -> tuple[TopicRef, ...]:
    refs: list[TopicRef] = []
    for topic_id in topic_ids:
        if allowed is not None and topic_id not in allowed:
            continue
        identity = identities.get(topic_id)
        if identity is None:
            continue
        refs.append(
            TopicRef(
                topic_id=identity.topic_id,
                stable_key=identity.stable_key,
                preferred_label=identity.preferred_label,
            )
        )
    refs.sort(key=_topic_ref_sort_key)
    return tuple(refs)


def _sibling_groups(
    view: _TopicView,
    allowed: frozenset[int] | None,
) -> tuple[SiblingGroup, ...]:
    groups: list[SiblingGroup] = []
    parent_ids = _depth_one_ids(view.graph.parents, view.topic.pk, view.ancestors)
    for parent_id in parent_ids:
        if allowed is not None and parent_id not in allowed:
            continue
        parent_identity = view.identities.get(parent_id)
        if parent_identity is None:
            continue
        sibling_ids = [
            child_id
            for child_id in view.graph.children.get(parent_id, ())
            if child_id != view.topic.pk
        ]
        if not sibling_ids:
            continue
        groups.append(
            SiblingGroup(
                shared_parent=TopicRef(
                    topic_id=parent_identity.topic_id,
                    stable_key=parent_identity.stable_key,
                    preferred_label=parent_identity.preferred_label,
                ),
                siblings=_refs(sibling_ids, view.identities, allowed),
            )
        )
    groups.sort(key=lambda group: _topic_ref_sort_key(group.shared_parent))
    return tuple(groups)


def _parent_ids(
    topic_id: int,
    graph: BroaderGraph,
    identities: Mapping[int, _TopicIdentity],
    allowed: frozenset[int],
) -> tuple[int, ...]:
    parents = [
        identities[parent_id]
        for parent_id in graph.parents.get(topic_id, ())
        if parent_id in allowed and parent_id in identities
    ]
    parents.sort(
        key=lambda identity: (
            hebrew_public_sort_key(identity.preferred_label),
            identity.topic_id,
        )
    )
    return tuple(identity.topic_id for identity in parents)


def _topic_ref_sort_key(ref: TopicRef) -> tuple[tuple[int, str], int]:
    return (hebrew_public_sort_key(ref.preferred_label), ref.topic_id)


def _list_row_sort_key(row: PublicTopicListRow) -> tuple[tuple[int, str], int]:
    return (hebrew_public_sort_key(row.preferred_label), row.topic_id)


def _item_page(
    view: _TopicView,
    *,
    user,
    audience: AcademicIndexAudience,
    queryset: QuerySet[ArchiveItem],
    page: int | str,
    per_page: int,
) -> PublicTopicItemPage | StaffTopicItemPage:
    per_page_value = _normalize_per_page(per_page)
    total_count = queryset.count()
    page_number = _normalize_page(
        page, total_count=total_count, per_page=per_page_value
    )
    offset = (page_number - 1) * per_page_value
    item_ids = list(
        queryset.values_list("pk", flat=True)[offset : offset + per_page_value]
    )
    records = _records_for_item_ids(
        view,
        user=user,
        audience=audience,
        item_ids=item_ids,
        direct_paths_empty=audience is AcademicIndexAudience.PUBLIC,
        preserve_order=True,
    )
    ordered = tuple(records[item_id] for item_id in item_ids if item_id in records)
    if audience is AcademicIndexAudience.PUBLIC:
        public_records = tuple(
            record for record in ordered if isinstance(record, PublicTopicItemRecord)
        )
        return PublicTopicItemPage(
            total_count=total_count,
            page=page_number,
            per_page=per_page_value,
            records=public_records,
        )
    staff_records = tuple(
        record for record in ordered if isinstance(record, StaffTopicItemRecord)
    )
    return StaffTopicItemPage(
        total_count=total_count,
        page=page_number,
        per_page=per_page_value,
        records=staff_records,
    )


def _records_for_item_ids(
    view: _TopicView,
    *,
    user,
    audience: AcademicIndexAudience,
    item_ids: Sequence[int],
    direct_paths_empty: bool,
    preserve_order: bool = False,
) -> dict[int, PublicTopicItemRecord] | dict[int, StaffTopicItemRecord]:
    if not item_ids:
        return {}
    descendant_topic_ids = view.descendants.topic_ids
    path_topic_ids = set(descendant_topic_ids)
    path_topic_ids.add(view.topic.pk)
    association_rows = list(
        ArchiveItemTopicAssociation.objects.filter(
            archive_item_id__in=list(item_ids),
            topic_id__in=path_topic_ids,
            status=ArchiveItemTopicAssociation.Status.APPROVED,
            decision_event__isnull=False,
            decision_event__action__in=READABLE_EVENT_ACTIONS,
        ).values("id", "archive_item_id", "topic_id", "relevance", "decision_event_id")
    )
    direct_by_item: dict[int, str] = {}
    paths_by_item: dict[int, list[dict]] = {}
    for row in association_rows:
        if row["topic_id"] == view.topic.pk:
            direct_by_item[row["archive_item_id"]] = row["relevance"]
            continue
        if row["topic_id"] in descendant_topic_ids:
            paths_by_item.setdefault(row["archive_item_id"], []).append(row)
    kept_ids = [
        item_id
        for item_id in item_ids
        if item_id in direct_by_item or item_id in paths_by_item
    ]
    if not preserve_order:
        kept_ids = list(
            archive_browse_queryset_for_user(user)
            .filter(pk__in=kept_ids)
            .order_by("-created_at", "pk")
            .values_list("pk", flat=True)
        )
    rendered_paths: dict[int, list[dict]] = {}
    label_ids: set[int] = set()
    for item_id in kept_ids:
        if item_id in direct_by_item and direct_paths_empty:
            rendered_paths[item_id] = []
            continue
        rows = paths_by_item.get(item_id, [])
        rendered_paths[item_id] = rows
        label_ids.update(row["topic_id"] for row in rows)
    identities = _load_identities(label_ids) if label_ids else {}
    eligible_ids = view.eligible_ids
    if audience is AcademicIndexAudience.PUBLIC and label_ids and eligible_ids is None:
        eligible_ids = _eligible_for_loaded(view.graph, user, identities, view.topic)
    if audience is AcademicIndexAudience.PUBLIC:
        public_records: dict[int, PublicTopicItemRecord] = {}
        for item_id in kept_ids:
            appearance = (
                ItemAppearance.DIRECT
                if item_id in direct_by_item
                else ItemAppearance.INHERITED
            )
            public_records[item_id] = PublicTopicItemRecord(
                archive_item_id=item_id,
                appearance=appearance,
                direct_relevance=(
                    direct_by_item.get(item_id)
                    if appearance is ItemAppearance.DIRECT
                    else None
                ),
                paths=_public_paths(
                    rendered_paths.get(item_id, []),
                    identities,
                    eligible_ids or frozenset(),
                ),
            )
        return public_records
    staff_records: dict[int, StaffTopicItemRecord] = {}
    for item_id in kept_ids:
        appearance = (
            ItemAppearance.DIRECT
            if item_id in direct_by_item
            else ItemAppearance.INHERITED
        )
        staff_records[item_id] = StaffTopicItemRecord(
            archive_item_id=item_id,
            appearance=appearance,
            direct_relevance=(
                direct_by_item.get(item_id)
                if appearance is ItemAppearance.DIRECT
                else None
            ),
            paths=_staff_paths(
                rendered_paths.get(item_id, []), identities, view.descendants
            ),
        )
    return staff_records


def _public_paths(
    rows: Sequence[dict],
    identities: Mapping[int, _TopicIdentity],
    eligible_ids: frozenset[int],
) -> tuple[PublicAssociationPath, ...]:
    paths: list[PublicAssociationPath] = []
    for row in rows:
        topic_id = row["topic_id"]
        if topic_id not in eligible_ids:
            continue
        identity = identities.get(topic_id)
        if identity is None:
            continue
        paths.append(
            PublicAssociationPath(
                topic_id=identity.topic_id,
                stable_key=identity.stable_key,
                preferred_label=identity.preferred_label,
                relevance=row["relevance"],
            )
        )
    paths.sort(
        key=lambda path: (hebrew_public_sort_key(path.preferred_label), path.topic_id)
    )
    return tuple(paths)


def _staff_paths(
    rows: Sequence[dict],
    identities: Mapping[int, _TopicIdentity],
    descendants: GraphClosure,
) -> tuple[StaffAssociationPath, ...]:
    paths: list[StaffAssociationPath] = []
    for row in rows:
        topic_id = row["topic_id"]
        depth = descendants.min_depth.get(topic_id)
        identity = identities.get(topic_id)
        if depth is None or identity is None or row["decision_event_id"] is None:
            continue
        paths.append(
            StaffAssociationPath(
                topic_id=identity.topic_id,
                stable_key=identity.stable_key,
                preferred_label=identity.preferred_label,
                relevance=row["relevance"],
                depth=depth,
                association_id=row["id"],
                decision_event_id=row["decision_event_id"],
            )
        )
    paths.sort(
        key=lambda path: (hebrew_public_sort_key(path.preferred_label), path.topic_id)
    )
    return tuple(paths)


def _label_contains(label: str, query: str) -> bool:
    return query.casefold() in label.casefold()


def _normalize_per_page(per_page: int) -> int:
    if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1:
        return _DEFAULT_PER_PAGE
    return per_page


def _normalize_page(page: int | str, *, total_count: int, per_page: int) -> int:
    raw = page if isinstance(page, str) else str(page)
    return normalize_archive_public_list_page(
        raw,
        total_count=total_count,
        per_page=per_page,
    )
