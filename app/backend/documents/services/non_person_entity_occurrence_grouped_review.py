"""Read-only groups of pending non-person occurrence candidates.

Detection and approval stay occurrence-level. This module does not approve,
reject, reassign, or remove. It does not write proposals, candidates,
matches, events, occurrences, registry rows, or the search index.

A group is a presentation of ``PENDING`` candidates that share one entity,
one normalized surface, one text kind, and one complete match-provenance
set. The provenance set is every ``(method, alias_kind)`` pair on the
candidate, sorted. Match-row order and ``matched_value`` do not change the
group. Different entities never share a group. Proposal rows are not merged.
Each candidate id stays independently addressable.

The group id is a SHA-256 of that key. It is not stored.

Work is split in two. The summary inspects every pending member for exact
counts and risk flags, and builds a context window only for the
representative and outlier rows of groups that the current filter returns.
The detail page resolves one group id, then builds a context window only
for the members on the requested page. Authoritative text is loaded once
per archive item. A surface is located once per item, text kind, and
normalized surface.

Context uses the same source rules as individual review. A stale source,
an unsupported normalization version, or a slice that cannot be relocated
keeps the candidate in the group, records a risk flag, and leaves the
context window empty. Title and metadata are not detection evidence.
Risk flags are not dropped for members that were not sampled.

Ordinal gaps from token-boundary filtering are not a risk flag.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Prefetch
from django.urls import reverse

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_occurrence_review import (
    _MATCH_METHOD_LABELS,
    _TEXT_KIND_LABELS,
    _SourceState,
    _context_parts,
    _item_title,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    SurfaceOccurrence,
    authoritative_text_context_for_item,
    item_supports_occurrence_text_kind,
    locate_surface_occurrences,
    normalize_surface_v1,
)
from documents.services.non_person_entity_presentation import (
    non_person_public_name,
    non_person_public_type_label,
)
from documents.services.non_person_entity_staff import non_person_alias_kind_label

SHORT_SURFACE_MAX_LENGTH = 3
MANY_ARCHIVE_ITEMS_THRESHOLD = 5
REPRESENTATIVE_EXAMPLE_LIMIT = 4
OUTLIER_EXAMPLE_LIMIT = 3
GROUP_DETAIL_PAGE_SIZE = 50

OCR_VARIANT = "OCR_VARIANT"
SHORT_SURFACE = "SHORT_SURFACE"
MULTIPLE_MATCH_METHODS = "MULTIPLE_MATCH_METHODS"
MULTIPLE_ALIAS_KINDS = "MULTIPLE_ALIAS_KINDS"
SOURCE_STALE = "SOURCE_STALE"
CONTEXT_UNAVAILABLE = "CONTEXT_UNAVAILABLE"
MATCHED_TEXT_UNAVAILABLE = "MATCHED_TEXT_UNAVAILABLE"
PRIOR_REVIEW_HISTORY = "PRIOR_REVIEW_HISTORY"
MANY_ARCHIVE_ITEMS = "MANY_ARCHIVE_ITEMS"

RISK_FLAG_ORDER = (
    OCR_VARIANT,
    SHORT_SURFACE,
    MULTIPLE_MATCH_METHODS,
    MULTIPLE_ALIAS_KINDS,
    SOURCE_STALE,
    CONTEXT_UNAVAILABLE,
    MATCHED_TEXT_UNAVAILABLE,
    PRIOR_REVIEW_HISTORY,
    MANY_ARCHIVE_ITEMS,
)

RISK_FLAG_LABELS = {
    OCR_VARIANT: "וריאנט זיהוי טקסט",
    SHORT_SURFACE: "שם קצר",
    MULTIPLE_MATCH_METHODS: "כמה שיטות התאמה",
    MULTIPLE_ALIAS_KINDS: "כמה סוגי שמות",
    SOURCE_STALE: "הטקסט השתנה",
    CONTEXT_UNAVAILABLE: "אין הקשר בטוח",
    MATCHED_TEXT_UNAVAILABLE: "אין טקסט שזוהה",
    PRIOR_REVIEW_HISTORY: "יש היסטוריית בדיקה",
    MANY_ARCHIVE_ITEMS: "מופיע בפריטים רבים",
}

_MEMBER_RISK_FLAGS = (
    SOURCE_STALE,
    CONTEXT_UNAVAILABLE,
    MATCHED_TEXT_UNAVAILABLE,
    PRIOR_REVIEW_HISTORY,
)

_RISK_FILTER_ANY = "any"
_RISK_FILTER_NONE = "none"
_KNOWN_TEXT_KINDS = frozenset(ArchiveItemEntityOccurrence.TextKind.values)
_REVIEW_HISTORY_IGNORED = frozenset(
    {NonPersonEntityOccurrenceReviewEvent.Action.DETECT}
)


@dataclass(frozen=True)
class OccurrenceGroupKey:
    """Fields that decide whether two pending candidates are one group."""

    candidate_entity_id: int
    normalized_surface: str
    text_kind: str
    provenance: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class OccurrenceContextExample:
    """One candidate, with a context window only when the source still matches."""

    candidate_id: int
    item_id: int
    item_title: str
    item_url: str
    review_url: str
    text_kind: str
    text_kind_label: str
    matched_text: str
    occurrence_ordinal: int
    context_before: str
    context_match: str
    context_after: str
    has_context: bool
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class OccurrenceReviewGroup:
    """One pending group prepared for the staff list. Read-only."""

    group_id: str
    candidate_entity_id: int
    entity_label: str
    type_label: str
    normalized_surface: str
    text_kind: str
    text_kind_label: str
    match_methods: tuple[str, ...]
    match_method_labels: tuple[str, ...]
    alias_kinds: tuple[str, ...]
    alias_kind_labels: tuple[str, ...]
    provenance_summary: str
    candidate_count: int
    archive_item_count: int
    candidate_ids: tuple[int, ...]
    archive_item_ids: tuple[int, ...]
    risk_flags: tuple[str, ...]
    risk_flag_labels: tuple[str, ...]
    representative_examples: tuple[OccurrenceContextExample, ...]
    outlier_examples: tuple[OccurrenceContextExample, ...]
    detail_url: str


@dataclass(frozen=True)
class OccurrenceReviewGroupDetail:
    """One page of one group, still without an approval action.

    ``examples`` are the members on ``page`` only. ``group.candidate_ids``
    still lists every member, in the same order as the pages.
    """

    group: OccurrenceReviewGroup
    examples: tuple[OccurrenceContextExample, ...]
    page: int
    page_count: int
    page_size: int


@dataclass(frozen=True)
class GroupedOccurrenceReviewPage:
    groups: tuple[OccurrenceReviewGroup, ...]
    entity_options: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class _MemberSource:
    """Source verdict for one candidate. No context window."""

    code: str
    is_stale: bool
    has_context: bool
    located: SurfaceOccurrence | None


@dataclass(frozen=True)
class _MemberRef:
    candidate: NonPersonEntityOccurrenceCandidate
    source: _MemberSource
    risk_flags: tuple[str, ...]


@dataclass(frozen=True)
class _BuiltGroup:
    """Pending members that share a key. Source text is not loaded yet."""

    key: OccurrenceGroupKey
    candidates: tuple[NonPersonEntityOccurrenceCandidate, ...]
    search_text: str
    entity_label: str
    type_label: str


@dataclass(frozen=True)
class _PreparedGroup:
    indexed: _BuiltGroup
    refs: tuple[_MemberRef, ...]
    risk_flags: tuple[str, ...]


def occurrence_group_id(key: OccurrenceGroupKey) -> str:
    """Deterministic presentation id. Not a database key."""

    payload = json.dumps(
        {
            "alias_kinds": [pair[1] for pair in key.provenance],
            "candidate_entity_id": key.candidate_entity_id,
            "match_methods": [pair[0] for pair in key.provenance],
            "normalized_surface": key.normalized_surface,
            "provenance": [list(pair) for pair in key.provenance],
            "text_kind": key.text_kind,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def staff_grouped_occurrence_review_page(
    *,
    query: str = "",
    entity_id: int | None = None,
    text_kind: str = "",
    risk: str = "",
) -> GroupedOccurrenceReviewPage:
    """Pending groups for the staff list. Reads only.

    Risk flags and counts cover every member. Context windows are built
    only for the representative and outlier examples of groups that pass
    the filter.
    """

    cache = _AuthoritativeSourceCache()
    prepared = _prepare_groups(_index_groups(), cache)
    entity_options = tuple(
        (row.indexed.key.candidate_entity_id, row.indexed.entity_label)
        for row in _unique_entities(prepared)
    )
    selected = _filter_groups(
        prepared,
        query=query,
        entity_id=entity_id,
        text_kind=text_kind,
        risk=risk,
    )
    return GroupedOccurrenceReviewPage(
        groups=tuple(_group_for_list(row, cache) for row in selected),
        entity_options=entity_options,
    )


def staff_occurrence_review_group_detail(
    group_id: str,
    *,
    page: int = 1,
) -> OccurrenceReviewGroupDetail | None:
    """One page of one pending group, or None.

    Other groups are identified only by their key. Their text is not loaded
    and their context windows are not built. Context windows on this page
    are limited to ``examples``.
    """

    cleaned = (group_id or "").strip()
    if len(cleaned) != 64 or any(char not in "0123456789abcdef" for char in cleaned):
        return None
    matched = next(
        (row for row in _index_groups() if occurrence_group_id(row.key) == cleaned),
        None,
    )
    if matched is None:
        return None
    cache = _AuthoritativeSourceCache()
    refs = _assess_members(matched.candidates, cache)
    page_count = _page_count(len(refs))
    current = _clamp_page(page, page_count)
    start = (current - 1) * GROUP_DETAIL_PAGE_SIZE
    page_refs = refs[start : start + GROUP_DETAIL_PAGE_SIZE]
    examples = tuple(_example(ref, cache) for ref in page_refs)
    prepared = _PreparedGroup(
        indexed=matched,
        refs=refs,
        risk_flags=_group_flags(matched.key, refs),
    )
    return OccurrenceReviewGroupDetail(
        group=_make_group(prepared, representative=(), outliers=()),
        examples=examples,
        page=current,
        page_count=page_count,
        page_size=GROUP_DETAIL_PAGE_SIZE,
    )


def risk_filter_choices() -> tuple[tuple[str, str], ...]:
    choices = [
        ("", "הכול"),
        (_RISK_FILTER_ANY, "יש סימון"),
        (_RISK_FILTER_NONE, "בלי סימון"),
    ]
    choices.extend((code, RISK_FLAG_LABELS[code]) for code in RISK_FLAG_ORDER)
    return tuple(choices)


def text_kind_filter_choices() -> tuple[tuple[str, str], ...]:
    choices = [("", "הכול")]
    choices.extend(
        (kind, _TEXT_KIND_LABELS.get(kind, kind))
        for kind in ArchiveItemEntityOccurrence.TextKind.values
    )
    return tuple(choices)


def _index_groups() -> list[_BuiltGroup]:
    grouped: dict[OccurrenceGroupKey, list[NonPersonEntityOccurrenceCandidate]] = (
        defaultdict(list)
    )
    for candidate in _pending_queryset():
        grouped[_group_key(candidate)].append(candidate)
    indexed: list[_BuiltGroup] = []
    for key, members in grouped.items():
        ordered = tuple(sorted(members, key=_member_sort_key))
        entity = ordered[0].candidate_entity
        entity_label = non_person_public_name(entity)
        item_ids = tuple(
            sorted({member.proposal.archive_item_id for member in ordered})
        )
        titles = [_item_title(member.proposal.archive_item) for member in ordered]
        search_text = "\n".join(
            [
                entity_label,
                entity.canonical_name,
                entity.display_name or "",
                key.normalized_surface,
                *titles,
                *(str(item_id) for item_id in item_ids),
            ]
        ).casefold()
        indexed.append(
            _BuiltGroup(
                key=key,
                candidates=ordered,
                search_text=search_text,
                entity_label=entity_label,
                type_label=non_person_public_type_label(entity),
            )
        )
    indexed.sort(key=_index_sort_key)
    return indexed


def _prepare_groups(
    indexed: list[_BuiltGroup],
    cache: _AuthoritativeSourceCache,
) -> list[_PreparedGroup]:
    prepared: list[_PreparedGroup] = []
    for row in indexed:
        refs = _assess_members(row.candidates, cache)
        prepared.append(
            _PreparedGroup(
                indexed=row,
                refs=refs,
                risk_flags=_group_flags(row.key, refs),
            )
        )
    return prepared


def _pending_queryset():
    return (
        NonPersonEntityOccurrenceCandidate.objects.filter(
            status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
        )
        .select_related(
            "proposal",
            "proposal__archive_item",
            "candidate_entity",
        )
        .prefetch_related(
            Prefetch(
                "matches",
                queryset=NonPersonEntityOccurrenceCandidateMatch.objects.order_by("id"),
            ),
            Prefetch(
                "review_events",
                queryset=NonPersonEntityOccurrenceReviewEvent.objects.order_by("id"),
            ),
        )
    )


def _group_key(candidate: NonPersonEntityOccurrenceCandidate) -> OccurrenceGroupKey:
    proposal = candidate.proposal
    return OccurrenceGroupKey(
        candidate_entity_id=candidate.candidate_entity_id,
        normalized_surface=proposal.normalized_surface,
        text_kind=proposal.text_kind,
        provenance=_provenance(candidate),
    )


def _provenance(
    candidate: NonPersonEntityOccurrenceCandidate,
) -> tuple[tuple[str, str], ...]:
    pairs = {
        (match.method, match.alias_kind or "") for match in candidate.matches.all()
    }
    return tuple(sorted(pairs))


def _member_sort_key(candidate: NonPersonEntityOccurrenceCandidate) -> tuple:
    proposal = candidate.proposal
    return (proposal.archive_item_id, proposal.occurrence_ordinal, candidate.pk)


def _assess_members(
    candidates: tuple[NonPersonEntityOccurrenceCandidate, ...],
    cache: _AuthoritativeSourceCache,
) -> tuple[_MemberRef, ...]:
    refs: list[_MemberRef] = []
    for candidate in candidates:
        source = cache.assess(candidate.proposal)
        stored_matched = (candidate.proposal.matched_text or "").strip()
        refs.append(
            _MemberRef(
                candidate=candidate,
                source=source,
                risk_flags=_member_flags(
                    candidate,
                    state_code=source.code,
                    is_stale=source.is_stale,
                    has_context=source.has_context,
                    stored_matched=stored_matched,
                ),
            )
        )
    return tuple(refs)


class _AuthoritativeSourceCache:
    """One authoritative text per item, one surface location per item and kind.

    Member checks after that are ordinal lookups. This cache does not build
    context windows.
    """

    def __init__(self) -> None:
        self._contexts: dict[int, object] = {}
        self._by_ordinal: dict[
            tuple[int, str, str],
            dict[int, SurfaceOccurrence] | None,
        ] = {}
        self._surface_ok: dict[str, bool] = {}
        self._matched_ok: dict[str, str] = {}

    def assess(
        self,
        proposal: NonPersonEntityOccurrenceProposal,
    ) -> _MemberSource:
        if proposal.normalization_version != SURFACE_V1:
            return _MemberSource(
                code="unsupported_normalization",
                is_stale=False,
                has_context=False,
                located=None,
            )
        item = proposal.archive_item
        if not item_supports_occurrence_text_kind(item, proposal.text_kind):
            return _MemberSource(
                code="stale",
                is_stale=True,
                has_context=False,
                located=None,
            )
        context = self._context(item)
        if context.archive_item_id != proposal.archive_item_id:
            return _MemberSource(
                code="stale",
                is_stale=True,
                has_context=False,
                located=None,
            )
        text = context.texts.get(proposal.text_kind)
        if text is None:
            return _MemberSource(
                code="stale",
                is_stale=True,
                has_context=False,
                located=None,
            )
        digest = context.digests.get(proposal.text_kind)
        if digest != proposal.source_text_sha256:
            return _MemberSource(
                code="stale",
                is_stale=True,
                has_context=False,
                located=None,
            )
        if not self._normalized_surface(proposal.normalized_surface):
            return _MemberSource(
                code="not_located",
                is_stale=False,
                has_context=False,
                located=None,
            )
        located = self._located(
            item.pk,
            proposal.text_kind,
            proposal.normalized_surface,
            text,
            proposal.occurrence_ordinal,
        )
        if located is None:
            return _MemberSource(
                code="not_located",
                is_stale=False,
                has_context=False,
                located=None,
            )
        return _MemberSource(
            code="current",
            is_stale=False,
            has_context=True,
            located=located,
        )

    def text_for(self, proposal: NonPersonEntityOccurrenceProposal) -> str | None:
        context = self._context(proposal.archive_item)
        if context.archive_item_id != proposal.archive_item_id:
            return None
        return context.texts.get(proposal.text_kind)

    def _context(self, item):
        cached = self._contexts.get(item.pk)
        if cached is None:
            cached = authoritative_text_context_for_item(item)
            self._contexts[item.pk] = cached
        return cached

    def _normalized_surface(self, surface: str) -> bool:
        cached = self._surface_ok.get(surface)
        if cached is None:
            cached = normalize_surface_v1(surface) == surface
            self._surface_ok[surface] = cached
        return cached

    def _located(
        self,
        item_id: int,
        text_kind: str,
        surface: str,
        text: str,
        ordinal: int,
    ) -> SurfaceOccurrence | None:
        key = (item_id, text_kind, surface)
        mapping = self._by_ordinal.get(key, ...)
        if mapping is ...:
            location = locate_surface_occurrences(text, surface)
            if location.occurrences is None:
                mapping = None
            else:
                mapping = {
                    occurrence.ordinal: occurrence
                    for occurrence in location.occurrences
                }
            self._by_ordinal[key] = mapping
        if mapping is None:
            return None
        located = mapping.get(ordinal)
        if located is None:
            return None
        normalized = self._matched_ok.get(located.matched_text)
        if normalized is None:
            normalized = normalize_surface_v1(located.matched_text)
            self._matched_ok[located.matched_text] = normalized
        if normalized != surface:
            return None
        return located


def _example(
    ref: _MemberRef, cache: _AuthoritativeSourceCache
) -> OccurrenceContextExample:
    candidate = ref.candidate
    proposal = candidate.proposal
    item = proposal.archive_item
    source = ref.source
    before = ""
    match = ""
    after = ""
    if source.has_context and source.located is not None:
        before, match, after = _context_parts(
            _SourceState(
                code=source.code,
                is_stale=False,
                is_reviewable=True,
                text=cache.text_for(proposal),
                located=source.located,
            )
        )
    stored_matched = (proposal.matched_text or "").strip()
    if source.has_context:
        matched_text = match or stored_matched
    else:
        matched_text = stored_matched
        before, match, after = "", "", ""
    return OccurrenceContextExample(
        candidate_id=candidate.pk,
        item_id=item.pk,
        item_title=_item_title(item),
        item_url=reverse("archive-detail", kwargs={"item_id": item.pk}),
        review_url=reverse(
            "archive-manage-entity-occurrence-proposal",
            kwargs={"candidate_id": candidate.pk},
        ),
        text_kind=proposal.text_kind,
        text_kind_label=_TEXT_KIND_LABELS.get(proposal.text_kind, ""),
        matched_text=matched_text,
        occurrence_ordinal=proposal.occurrence_ordinal,
        context_before=before,
        context_match=match if source.has_context else "",
        context_after=after,
        has_context=source.has_context and source.located is not None,
        risk_flags=ref.risk_flags,
    )


def _member_flags(
    candidate: NonPersonEntityOccurrenceCandidate,
    *,
    state_code: str,
    is_stale: bool,
    has_context: bool,
    stored_matched: str,
) -> tuple[str, ...]:
    flags: list[str] = []
    if is_stale or state_code == "stale":
        flags.append(SOURCE_STALE)
    if not has_context:
        flags.append(CONTEXT_UNAVAILABLE)
    if not stored_matched:
        flags.append(MATCHED_TEXT_UNAVAILABLE)
    if _has_prior_review_history(candidate):
        flags.append(PRIOR_REVIEW_HISTORY)
    return _ordered(flags)


def _has_prior_review_history(candidate: NonPersonEntityOccurrenceCandidate) -> bool:
    return any(
        event.action not in _REVIEW_HISTORY_IGNORED
        for event in candidate.review_events.all()
    )


def _group_for_list(
    row: _PreparedGroup,
    cache: _AuthoritativeSourceCache,
) -> OccurrenceReviewGroup:
    representative = _representative_refs(row.refs)
    outliers = _outlier_refs(row.refs)
    examples: dict[int, OccurrenceContextExample] = {}
    for ref in (*representative, *outliers):
        if ref.candidate.pk not in examples:
            examples[ref.candidate.pk] = _example(ref, cache)
    return _make_group(
        row,
        representative=tuple(examples[ref.candidate.pk] for ref in representative),
        outliers=tuple(examples[ref.candidate.pk] for ref in outliers),
    )


def _make_group(
    row: _PreparedGroup,
    *,
    representative: tuple[OccurrenceContextExample, ...],
    outliers: tuple[OccurrenceContextExample, ...],
) -> OccurrenceReviewGroup:
    key = row.indexed.key
    indexed = row.indexed
    methods = tuple(sorted({pair[0] for pair in key.provenance}))
    alias_kinds = tuple(sorted({pair[1] for pair in key.provenance if pair[1]}))
    item_ids = tuple(
        sorted({ref.candidate.proposal.archive_item_id for ref in row.refs})
    )
    method_labels = tuple(
        label for method in methods if (label := _MATCH_METHOD_LABELS.get(method, ""))
    )
    alias_labels = tuple(
        label for kind in alias_kinds if (label := non_person_alias_kind_label(kind))
    )
    group_id = occurrence_group_id(key)
    return OccurrenceReviewGroup(
        group_id=group_id,
        candidate_entity_id=key.candidate_entity_id,
        entity_label=indexed.entity_label,
        type_label=indexed.type_label,
        normalized_surface=key.normalized_surface,
        text_kind=key.text_kind,
        text_kind_label=_TEXT_KIND_LABELS.get(key.text_kind, ""),
        match_methods=methods,
        match_method_labels=method_labels,
        alias_kinds=alias_kinds,
        alias_kind_labels=alias_labels,
        provenance_summary=_provenance_summary(method_labels, alias_labels),
        candidate_count=len(row.refs),
        archive_item_count=len(item_ids),
        candidate_ids=tuple(ref.candidate.pk for ref in row.refs),
        archive_item_ids=item_ids,
        risk_flags=row.risk_flags,
        risk_flag_labels=tuple(RISK_FLAG_LABELS[flag] for flag in row.risk_flags),
        representative_examples=representative,
        outlier_examples=outliers,
        detail_url=reverse(
            "archive-manage-entity-occurrence-group",
            kwargs={"group_id": group_id},
        ),
    )


def _group_flags(
    key: OccurrenceGroupKey, refs: tuple[_MemberRef, ...]
) -> tuple[str, ...]:
    alias_kinds = {pair[1] for pair in key.provenance if pair[1]}
    item_ids = {ref.candidate.proposal.archive_item_id for ref in refs}
    flags: list[str] = []
    if NonPersonEntityAlias.Kind.OCR_VARIANT in alias_kinds:
        flags.append(OCR_VARIANT)
    if len(key.normalized_surface) <= SHORT_SURFACE_MAX_LENGTH:
        flags.append(SHORT_SURFACE)
    if len({pair[0] for pair in key.provenance}) > 1:
        flags.append(MULTIPLE_MATCH_METHODS)
    if len(alias_kinds) > 1:
        flags.append(MULTIPLE_ALIAS_KINDS)
    member_flags = {flag for ref in refs for flag in ref.risk_flags}
    for flag in _MEMBER_RISK_FLAGS:
        if flag in member_flags:
            flags.append(flag)
    if len(item_ids) >= MANY_ARCHIVE_ITEMS_THRESHOLD:
        flags.append(MANY_ARCHIVE_ITEMS)
    return _ordered(flags)


def _provenance_summary(
    method_labels: tuple[str, ...],
    alias_labels: tuple[str, ...],
) -> str:
    parts = list(method_labels)
    if alias_labels:
        parts.append("סוגי שמות: " + " · ".join(alias_labels))
    return " · ".join(parts)


def _representative_refs(refs: tuple[_MemberRef, ...]) -> tuple[_MemberRef, ...]:
    """First, last, then unusual members, then other items. Capped. No randomness."""

    if not refs:
        return ()
    chosen_ids = [refs[0].candidate.pk]
    if len(refs) > 1:
        chosen_ids.append(refs[-1].candidate.pk)
    unusual_ids = [
        ref.candidate.pk
        for ref in refs
        if any(flag in ref.risk_flags for flag in _MEMBER_RISK_FLAGS)
    ]
    seen_items = {
        ref.candidate.proposal.archive_item_id
        for ref in refs
        if ref.candidate.pk in chosen_ids
    }
    by_id = {ref.candidate.pk: ref for ref in refs}
    for candidate_id in unusual_ids:
        if len(chosen_ids) >= REPRESENTATIVE_EXAMPLE_LIMIT:
            break
        if candidate_id not in chosen_ids:
            chosen_ids.append(candidate_id)
            seen_items.add(by_id[candidate_id].candidate.proposal.archive_item_id)
    for ref in refs:
        if len(chosen_ids) >= REPRESENTATIVE_EXAMPLE_LIMIT:
            break
        if ref.candidate.pk in chosen_ids:
            continue
        item_id = ref.candidate.proposal.archive_item_id
        if item_id not in seen_items:
            chosen_ids.append(ref.candidate.pk)
            seen_items.add(item_id)
    chosen = set(chosen_ids)
    return tuple(ref for ref in refs if ref.candidate.pk in chosen)


def _outlier_refs(refs: tuple[_MemberRef, ...]) -> tuple[_MemberRef, ...]:
    """Members whose risk is not shared by the whole group."""

    if len(refs) < 2:
        return ()
    selected: list[_MemberRef] = []
    selected_ids: set[int] = set()
    for flag in _MEMBER_RISK_FLAGS:
        holders = [ref for ref in refs if flag in ref.risk_flags]
        if not holders or len(holders) == len(refs):
            continue
        for ref in holders:
            if ref.candidate.pk in selected_ids:
                continue
            selected.append(ref)
            selected_ids.add(ref.candidate.pk)
            if len(selected) >= OUTLIER_EXAMPLE_LIMIT:
                return tuple(selected)
    return tuple(selected)


def _ordered(flags: list[str]) -> tuple[str, ...]:
    present = set(flags)
    return tuple(flag for flag in RISK_FLAG_ORDER if flag in present)


def _index_sort_key(row: _BuiltGroup) -> tuple:
    return (
        row.entity_label.casefold(),
        row.key.candidate_entity_id,
        row.key.normalized_surface.casefold(),
        row.key.text_kind,
        row.key.provenance,
    )


def _unique_entities(prepared: list[_PreparedGroup]) -> list[_PreparedGroup]:
    seen: set[int] = set()
    rows: list[_PreparedGroup] = []
    ordered = sorted(
        prepared,
        key=lambda row: (
            row.indexed.entity_label.casefold(),
            row.indexed.key.candidate_entity_id,
        ),
    )
    for row in ordered:
        entity_id = row.indexed.key.candidate_entity_id
        if entity_id in seen:
            continue
        seen.add(entity_id)
        rows.append(row)
    return rows


def _filter_groups(
    prepared: list[_PreparedGroup],
    *,
    query: str,
    entity_id: int | None,
    text_kind: str,
    risk: str,
) -> list[_PreparedGroup]:
    cleaned_kind = (text_kind or "").strip()
    if cleaned_kind and cleaned_kind not in _KNOWN_TEXT_KINDS:
        return []
    cleaned_risk = (risk or "").strip()
    known_risk = cleaned_risk in {"", _RISK_FILTER_ANY, _RISK_FILTER_NONE} or (
        cleaned_risk in RISK_FLAG_LABELS
    )
    if not known_risk:
        return []
    cleaned_query = (query or "").strip().casefold()
    selected: list[_PreparedGroup] = []
    for row in prepared:
        key = row.indexed.key
        if entity_id is not None and key.candidate_entity_id != entity_id:
            continue
        if cleaned_kind and key.text_kind != cleaned_kind:
            continue
        if cleaned_query and cleaned_query not in row.indexed.search_text:
            continue
        if cleaned_risk == _RISK_FILTER_ANY and not row.risk_flags:
            continue
        if cleaned_risk == _RISK_FILTER_NONE and row.risk_flags:
            continue
        if cleaned_risk in RISK_FLAG_LABELS and cleaned_risk not in row.risk_flags:
            continue
        selected.append(row)
    return selected


def _page_count(member_count: int) -> int:
    if member_count <= 0:
        return 1
    return (member_count + GROUP_DETAIL_PAGE_SIZE - 1) // GROUP_DETAIL_PAGE_SIZE


def _clamp_page(page: int, page_count: int) -> int:
    if page < 1:
        return 1
    if page > page_count:
        return page_count
    return page
