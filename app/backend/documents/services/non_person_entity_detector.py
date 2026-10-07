"""Explicit batch detector for non-person occurrence proposals.

Detection is not resolution. This module reads registry names and the
current authoritative displayed text, then records proposal, candidate,
and match rows. It does not write ``ArchiveItemEntityOccurrence``,
aliases, entities, review decisions, or the search index.

Default mode is dry-run. ``apply=True`` inserts missing rows and does
not reset an existing candidate. ``REJECTED`` and ``REMOVED`` stay
suppression slots. A changed source SHA is a new proposal identity; older
proposals are left in place.

One ``DETECT`` event is appended only when apply mode inserts a candidate.
Replay of that candidate appends nothing, including when a later registry
name adds another match row.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Prefetch

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    authoritative_displayed_text,
    item_supports_occurrence_text_kind,
    locate_surface_occurrences,
    normalize_surface_v1,
    source_text_sha256,
)

_MAX_SURFACE_LENGTH = NonPersonEntityOccurrenceProposal._meta.get_field(
    "normalized_surface"
).max_length
_SUPPRESSED = frozenset(
    {
        NonPersonEntityOccurrenceCandidate.Status.REJECTED,
        NonPersonEntityOccurrenceCandidate.Status.REMOVED,
    }
)
_TEXT_KINDS = (
    ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
    ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
)


@dataclass(frozen=True)
class DetectionReason:
    """One registry reason that produced a normalized surface."""

    entity_id: int
    method: str
    matched_value: str
    alias_kind: str = ""


@dataclass(frozen=True)
class NonPersonEntityDetectionReport:
    """Counts for one detector call. Dry-run and apply use the same fields."""

    apply: bool
    items_scanned: int
    text_sources_scanned: int
    text_sources_skipped: int
    detected_textual_occurrences: int
    new_proposals: int
    existing_proposals: int
    new_candidates: int
    existing_candidates: int
    suppressed_candidates: int
    new_match_rows: int
    existing_match_rows: int
    ambiguous_occurrences: int
    new_detect_events: int
    errors: tuple[str, ...] = ()

    @property
    def mode(self) -> str:
        return "apply" if self.apply else "dry-run"


@dataclass
class _Counts:
    items_scanned: int = 0
    text_sources_scanned: int = 0
    text_sources_skipped: int = 0
    detected_textual_occurrences: int = 0
    new_proposals: int = 0
    existing_proposals: int = 0
    new_candidates: int = 0
    existing_candidates: int = 0
    suppressed_candidates: int = 0
    new_match_rows: int = 0
    existing_match_rows: int = 0
    ambiguous_occurrences: int = 0
    new_detect_events: int = 0

    def freeze(self, *, apply: bool) -> NonPersonEntityDetectionReport:
        return NonPersonEntityDetectionReport(
            apply=apply,
            items_scanned=self.items_scanned,
            text_sources_scanned=self.text_sources_scanned,
            text_sources_skipped=self.text_sources_skipped,
            detected_textual_occurrences=self.detected_textual_occurrences,
            new_proposals=self.new_proposals,
            existing_proposals=self.existing_proposals,
            new_candidates=self.new_candidates,
            existing_candidates=self.existing_candidates,
            suppressed_candidates=self.suppressed_candidates,
            new_match_rows=self.new_match_rows,
            existing_match_rows=self.existing_match_rows,
            ambiguous_occurrences=self.ambiguous_occurrences,
            new_detect_events=self.new_detect_events,
        )


@dataclass
class _Ledger:
    """Rows already stored for the scanned items, keyed for this run."""

    proposals: dict[tuple[object, ...], NonPersonEntityOccurrenceProposal]
    candidates: dict[tuple[int, int], NonPersonEntityOccurrenceCandidate]
    matches: set[tuple[int, str, str]]


def format_detection_report(report: NonPersonEntityDetectionReport) -> str:
    """Stable plain-text summary for the management command."""

    lines = [
        f"mode: {report.mode}",
        f"items_scanned: {report.items_scanned}",
        f"text_sources_scanned: {report.text_sources_scanned}",
        f"text_sources_skipped: {report.text_sources_skipped}",
        f"detected_textual_occurrences: {report.detected_textual_occurrences}",
        f"new_proposals: {report.new_proposals}",
        f"existing_proposals: {report.existing_proposals}",
        f"new_candidates: {report.new_candidates}",
        f"existing_candidates: {report.existing_candidates}",
        f"suppressed_candidates: {report.suppressed_candidates}",
        f"new_match_rows: {report.new_match_rows}",
        f"existing_match_rows: {report.existing_match_rows}",
        f"ambiguous_occurrences: {report.ambiguous_occurrences}",
        f"new_detect_events: {report.new_detect_events}",
        f"errors: {len(report.errors)}",
    ]
    return "\n".join(lines) + "\n"


def detect_non_person_entities_for_item(
    item: ArchiveItem,
    *,
    apply: bool = False,
) -> NonPersonEntityDetectionReport:
    """Detect against one archive item. ``apply`` defaults to dry-run."""

    return detect_non_person_entities_for_items([item], apply=apply)


def detect_non_person_entities_for_items(
    items: Iterable[ArchiveItem],
    *,
    apply: bool = False,
) -> NonPersonEntityDetectionReport:
    """Detect against an explicit item iterable. Does not scan the corpus.

    Registry rows are loaded once. Each supported text body is scanned in
    memory with ``locate_surface_occurrences``, then kept only when the
    normalized span is a whole token. Dry-run classifies the same identities
    apply would write and inserts nothing.
    """

    item_list = list(items)
    surfaces = _load_surface_index()
    if apply:
        with transaction.atomic():
            return _detect(item_list, surfaces, apply=True)
    return _detect(item_list, surfaces, apply=False)


def _load_surface_index() -> dict[str, dict[int, tuple[DetectionReason, ...]]]:
    """Map each normalized surface to entity id, then to match reasons.

    Blank normalized names are ignored. The same entity keeps one entry per
    distinct method and matched-value snapshot. Several entities may share
    one surface. That stays ambiguous; this map does not choose one.
    """

    grouped: dict[str, dict[int, list[DetectionReason]]] = {}
    entities = NonPersonEntity.objects.prefetch_related(
        Prefetch(
            "aliases",
            queryset=NonPersonEntityAlias.objects.order_by("id"),
        )
    ).order_by("id")
    for entity in entities:
        _add_reason(
            grouped,
            entity_id=entity.pk,
            raw=entity.canonical_name,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME,
        )
        _add_reason(
            grouped,
            entity_id=entity.pk,
            raw=entity.display_name,
            method=NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME,
        )
        for alias in entity.aliases.all():
            _add_reason(
                grouped,
                entity_id=entity.pk,
                raw=alias.name,
                method=NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS,
                alias_kind=alias.kind,
            )
    return {
        surface: {
            entity_id: tuple(reasons)
            for entity_id, reasons in entities_for_surface.items()
        }
        for surface, entities_for_surface in grouped.items()
    }


def _add_reason(
    grouped: dict[str, dict[int, list[DetectionReason]]],
    *,
    entity_id: int,
    raw: str,
    method: str,
    alias_kind: str = "",
) -> None:
    surface = normalize_surface_v1(raw or "")
    if surface == "" or len(surface) > _MAX_SURFACE_LENGTH:
        return
    reason = DetectionReason(
        entity_id=entity_id,
        method=method,
        matched_value=raw,
        alias_kind=alias_kind,
    )
    bucket = grouped.setdefault(surface, {}).setdefault(entity_id, [])
    if reason not in bucket:
        bucket.append(reason)


def _detect(
    items: list[ArchiveItem],
    surfaces: Mapping[str, Mapping[int, tuple[DetectionReason, ...]]],
    *,
    apply: bool,
) -> NonPersonEntityDetectionReport:
    counts = _Counts()
    ledger = _load_ledger([item.pk for item in items if item.pk is not None])
    for item in items:
        if item.pk is None:
            continue
        counts.items_scanned += 1
        _detect_item(item, surfaces, ledger, counts, apply=apply)
    return counts.freeze(apply=apply)


def _load_ledger(item_ids: list[int]) -> _Ledger:
    if not item_ids:
        return _Ledger(proposals={}, candidates={}, matches=set())
    proposals = NonPersonEntityOccurrenceProposal.objects.filter(
        archive_item_id__in=item_ids
    )
    proposal_map = {_proposal_key(row): row for row in proposals}
    candidates = NonPersonEntityOccurrenceCandidate.objects.filter(
        proposal__archive_item_id__in=item_ids
    )
    candidate_map = {
        (row.proposal_id, row.candidate_entity_id): row for row in candidates
    }
    match_rows = NonPersonEntityOccurrenceCandidateMatch.objects.filter(
        candidate__proposal__archive_item_id__in=item_ids
    ).values_list("candidate_id", "method", "matched_value")
    return _Ledger(
        proposals=proposal_map,
        candidates=candidate_map,
        matches=set(match_rows),
    )


def _proposal_key(
    row: NonPersonEntityOccurrenceProposal,
) -> tuple[object, ...]:
    return (
        row.archive_item_id,
        row.text_kind,
        row.source_text_sha256,
        row.normalization_version,
        row.normalized_surface,
        row.occurrence_ordinal,
    )


def _detect_item(
    item: ArchiveItem,
    surfaces: Mapping[str, Mapping[int, tuple[DetectionReason, ...]]],
    ledger: _Ledger,
    counts: _Counts,
    *,
    apply: bool,
) -> None:
    for text_kind in _TEXT_KINDS:
        if not item_supports_occurrence_text_kind(item, text_kind):
            counts.text_sources_skipped += 1
            continue
        text = authoritative_displayed_text(item, text_kind)
        if text is None:
            counts.text_sources_skipped += 1
            continue
        counts.text_sources_scanned += 1
        digest = source_text_sha256(text)
        for surface, reasons_by_entity in surfaces.items():
            for ordinal, matched_text in _bounded_surface_slices(text, surface):
                _record_occurrence(
                    item=item,
                    text_kind=text_kind,
                    digest=digest,
                    surface=surface,
                    ordinal=ordinal,
                    matched_text=matched_text,
                    reasons_by_entity=reasons_by_entity,
                    ledger=ledger,
                    counts=counts,
                    apply=apply,
                )


def _bounded_surface_slices(text: str, surface: str) -> tuple[tuple[int, str], ...]:
    """Accepted detector hits, still addressed by the shared locator ordinal.

    ``locate_surface_occurrences`` stays a non-overlapping substring locator.
    Review and source revalidation look a proposal up by that same ordinal, so
    this filter does not renumber accepted hits. A rejected substring can leave
    a gap. For example, ``לבון ואז בון`` keeps locator ordinal 2 for the
    standalone ``בון``.

    A hit is kept only when the character immediately before and after its
    span in ``normalize_surface_v1`` text is outside a token. A token
    character is a Unicode letter, number, or combining mark. Punctuation and
    whitespace are boundaries. Hebrew prefixes are letters, so they are not
    stripped. A normalized count without recoverable original slices is not a
    hit: ``occurrences is None`` emits nothing, so no empty ``matched_text``
    is stored and no other ordinal is renumbered. A count that does not match
    the normalized finds is dropped.
    """

    located = locate_surface_occurrences(text, surface)
    if located.count == 0 or located.occurrences is None:
        return ()
    normalized_surface = normalize_surface_v1(surface)
    if normalized_surface == "":
        return ()
    normalized_source = normalize_surface_v1(text)
    starts = _normalized_find_starts(normalized_source, normalized_surface)
    if len(starts) != located.count:
        return ()
    accepted = [
        index
        for index, start in enumerate(starts)
        if _span_is_token_bounded(
            normalized_source,
            start,
            start + len(normalized_surface),
        )
    ]
    if len(located.occurrences) != located.count:
        return ()
    return tuple(
        (located.occurrences[index].ordinal, located.occurrences[index].matched_text)
        for index in accepted
    )


def _normalized_find_starts(text: str, surface: str) -> list[int]:
    starts: list[int] = []
    cursor = 0
    while True:
        found = text.find(surface, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + len(surface)


def _span_is_token_bounded(text: str, start: int, end: int) -> bool:
    if start > 0 and _is_token_continuation(text[start - 1]):
        return False
    if end < len(text) and _is_token_continuation(text[end]):
        return False
    return True


def _is_token_continuation(character: str) -> bool:
    return unicodedata.category(character)[:1] in {"L", "N", "M"}


def _record_occurrence(
    *,
    item: ArchiveItem,
    text_kind: str,
    digest: str,
    surface: str,
    ordinal: int,
    matched_text: str,
    reasons_by_entity: Mapping[int, tuple[DetectionReason, ...]],
    ledger: _Ledger,
    counts: _Counts,
    apply: bool,
) -> None:
    counts.detected_textual_occurrences += 1
    if len(reasons_by_entity) > 1:
        counts.ambiguous_occurrences += 1
    identity = (
        item.pk,
        text_kind,
        digest,
        SURFACE_V1,
        surface,
        ordinal,
    )
    proposal = ledger.proposals.get(identity)
    if proposal is None:
        counts.new_proposals += 1
        if apply:
            proposal = NonPersonEntityOccurrenceProposal.objects.create(
                archive_item=item,
                text_kind=text_kind,
                source_text_sha256=digest,
                normalization_version=SURFACE_V1,
                normalized_surface=surface,
                occurrence_ordinal=ordinal,
                matched_text=matched_text,
            )
            ledger.proposals[identity] = proposal
    else:
        counts.existing_proposals += 1
        if apply and proposal.matched_text == "" and matched_text:
            proposal.matched_text = matched_text
            proposal.save(update_fields=["matched_text", "updated_at"])
    for entity_id in sorted(reasons_by_entity):
        _record_candidate(
            proposal=proposal,
            entity_id=entity_id,
            reasons=reasons_by_entity[entity_id],
            ledger=ledger,
            counts=counts,
            apply=apply,
        )


def _record_candidate(
    *,
    proposal: NonPersonEntityOccurrenceProposal | None,
    entity_id: int,
    reasons: tuple[DetectionReason, ...],
    ledger: _Ledger,
    counts: _Counts,
    apply: bool,
) -> None:
    candidate = None
    if proposal is not None:
        candidate = ledger.candidates.get((proposal.pk, entity_id))
    if candidate is None:
        counts.new_candidates += 1
        counts.new_detect_events += 1
        counts.new_match_rows += len(reasons)
        if not apply or proposal is None:
            return
        candidate = NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=proposal,
            candidate_entity_id=entity_id,
            status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
        )
        ledger.candidates[(proposal.pk, entity_id)] = candidate
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.DETECT,
            to_entity_id=entity_id,
        )
        for reason in reasons:
            _insert_match(candidate, reason, ledger)
        return
    if candidate.status in _SUPPRESSED:
        counts.suppressed_candidates += 1
    else:
        counts.existing_candidates += 1
    for reason in reasons:
        match_key = (candidate.pk, reason.method, reason.matched_value)
        if match_key in ledger.matches:
            counts.existing_match_rows += 1
            continue
        counts.new_match_rows += 1
        if apply:
            _insert_match(candidate, reason, ledger)


def _insert_match(
    candidate: NonPersonEntityOccurrenceCandidate,
    reason: DetectionReason,
    ledger: _Ledger,
) -> None:
    NonPersonEntityOccurrenceCandidateMatch.objects.create(
        candidate=candidate,
        method=reason.method,
        matched_value=reason.matched_value,
        alias_kind=reason.alias_kind,
    )
    ledger.matches.add((candidate.pk, reason.method, reason.matched_value))
