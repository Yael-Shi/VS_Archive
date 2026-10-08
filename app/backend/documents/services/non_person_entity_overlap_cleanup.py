"""Reject old pending hits that current overlap suppression would not emit.

This is not the detector. It does not scan the corpus, does not insert
proposals, and does not delete proposal, candidate, match, or event rows.
Dry-run is the default. Apply rejects each still-eligible candidate through
``reject_candidate`` and leaves proposal identity unchanged.

A proposal is eligible only when every condition below holds. The first
failure skips the whole proposal:

1. The stored SHA matches the current authoritative text.
2. ``normalization_version`` is ``surface-v1``.
3. The stored surface and ordinal are still a current accepted hit.
4. That hit is strictly contained by a longer accepted hit.
5. The proposal has at least one candidate, and every candidate is
   ``PENDING``.
6. No ``ArchiveItemEntityOccurrence`` uses the same identity.
7. Every candidate has a ``DETECT`` event on this same proposal.

Apply locks one proposal at a time, re-reads the authoritative text inside
that transaction, and revalidates. Drift skips that proposal. Other
proposals are separate transactions. ``rejected_candidates`` counts only
rejects that committed. Skip counters, ``eligible_proposals``, and
``errors`` together equal ``proposals_examined``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from django.db import transaction

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_detector import (
    _load_surface_index,
    _overlap_containment_index,
)
from documents.services.non_person_entity_occurrence_review import (
    InvalidTransitionReviewError,
    NonPersonEntityOccurrenceReviewError,
    SourceLocationReviewError,
    StaleSourceReviewError,
    UnsupportedNormalizationReviewError,
    _read_authoritative_text,
    reject_candidate,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    item_supports_occurrence_text_kind,
    source_text_sha256,
)

OVERLAP_CLEANUP_NOTE = (
    "Rejected by overlap-cleanup: occurrence is strictly contained "
    "by a longer accepted registry surface."
)

_SKIP_REASONS = (
    "stale_source",
    "wrong_normalization_version",
    "not_currently_accepted",
    "not_contained",
    "mixed_or_nonpending_status",
    "has_authoritative_occurrence",
    "missing_detect_event",
)
_REVIEW_SKIP = {
    StaleSourceReviewError: "stale_source",
    UnsupportedNormalizationReviewError: "wrong_normalization_version",
    SourceLocationReviewError: "not_currently_accepted",
    InvalidTransitionReviewError: "mixed_or_nonpending_status",
}
_PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
_DETECT = NonPersonEntityOccurrenceReviewEvent.Action.DETECT


class _Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class OverlapCleanupLine:
    proposal_id: int
    archive_item_id: int
    text_kind: str
    normalized_surface: str
    occurrence_ordinal: int
    source_text_sha256: str
    covering_surface: str
    covering_ordinal: int
    candidate_ids: tuple[int, ...]
    candidate_entity_ids: tuple[int, ...]


@dataclass
class OverlapCleanupReport:
    apply: bool
    proposals_examined: int = 0
    stale_source: int = 0
    wrong_normalization_version: int = 0
    not_currently_accepted: int = 0
    not_contained: int = 0
    mixed_or_nonpending_status: int = 0
    has_authoritative_occurrence: int = 0
    missing_detect_event: int = 0
    eligible_proposals: int = 0
    eligible_candidates: int = 0
    rejected_candidates: int = 0
    errors: int = 0
    eligible: list[OverlapCleanupLine] = field(default_factory=list)
    error_lines: list[str] = field(default_factory=list)


def cleanup_non_person_overlap_candidates(
    item_ids: Sequence[int],
    *,
    apply: bool = False,
    actor=None,
) -> OverlapCleanupReport:
    """Classify proposals on the named items. Apply rejects eligible ones."""

    if apply and actor is None:
        raise ValueError("apply requires an actor.")
    report = OverlapCleanupReport(apply=apply)
    proposal_ids = list(
        NonPersonEntityOccurrenceProposal.objects.filter(
            archive_item_id__in=list(item_ids)
        )
        .order_by("archive_item_id", "pk")
        .values_list("pk", flat=True)
    )
    report.proposals_examined = len(proposal_ids)
    for proposal_id in proposal_ids:
        if apply:
            _apply_one(proposal_id, actor=actor, report=report)
        else:
            _dry_run_one(proposal_id, report=report)
    return report


def format_overlap_cleanup_report(report: OverlapCleanupReport) -> str:
    lines: list[str] = []
    for row in report.eligible:
        lines.extend(
            [
                f"proposal_id: {row.proposal_id}",
                f"archive_item_id: {row.archive_item_id}",
                f"text_kind: {row.text_kind}",
                f"normalized_surface: {row.normalized_surface}",
                f"occurrence_ordinal: {row.occurrence_ordinal}",
                f"source_text_sha256: {row.source_text_sha256}",
                f"covering_surface: {row.covering_surface}",
                f"covering_ordinal: {row.covering_ordinal}",
                f"candidate_ids: {','.join(str(pk) for pk in row.candidate_ids)}",
                (
                    "candidate_entity_ids: "
                    + ",".join(str(pk) for pk in row.candidate_entity_ids)
                ),
                "",
            ]
        )
    lines.extend(
        [
            f"mode: {'apply' if report.apply else 'dry-run'}",
            f"proposals_examined: {report.proposals_examined}",
            f"stale_source: {report.stale_source}",
            f"wrong_normalization_version: {report.wrong_normalization_version}",
            f"not_currently_accepted: {report.not_currently_accepted}",
            f"not_contained: {report.not_contained}",
            f"mixed_or_nonpending_status: {report.mixed_or_nonpending_status}",
            f"has_authoritative_occurrence: {report.has_authoritative_occurrence}",
            f"missing_detect_event: {report.missing_detect_event}",
            f"eligible_proposals: {report.eligible_proposals}",
            f"eligible_candidates: {report.eligible_candidates}",
            f"rejected_candidates: {report.rejected_candidates}",
            f"errors: {report.errors}",
        ]
    )
    if report.error_lines:
        lines.append("error_lines:")
        lines.extend(report.error_lines)
    return "\n".join(lines) + "\n"


def _dry_run_one(proposal_id: int, *, report: OverlapCleanupReport) -> None:
    try:
        proposal = NonPersonEntityOccurrenceProposal.objects.get(pk=proposal_id)
        candidates = list(proposal.candidates.order_by("pk"))
        verdict = _classify(proposal, candidates)
    except Exception as exc:
        _record_error(report, proposal_id, exc)
        return
    _record_verdict(report, verdict, rejected=0)


def _apply_one(proposal_id: int, *, actor, report: OverlapCleanupReport) -> None:
    try:
        line = _reject_proposal(proposal_id, actor=actor)
    except _Skip as skip:
        _count_skip(report, skip.reason)
        return
    except NonPersonEntityOccurrenceReviewError as exc:
        reason = _REVIEW_SKIP.get(type(exc))
        if reason is None:
            _record_error(report, proposal_id, exc)
            return
        _count_skip(report, reason)
        return
    except Exception as exc:
        _record_error(report, proposal_id, exc)
        return
    _record_verdict(report, line, rejected=len(line.candidate_ids))


def _reject_proposal(proposal_id: int, *, actor) -> OverlapCleanupLine:
    with transaction.atomic():
        proposal = NonPersonEntityOccurrenceProposal.objects.select_for_update(
            of=("self",)
        ).get(pk=proposal_id)
        candidates = list(
            NonPersonEntityOccurrenceCandidate.objects.select_for_update(of=("self",))
            .filter(proposal_id=proposal.pk)
            .order_by("pk")
        )
        _lock_identity_occurrences(proposal)
        verdict = _classify(proposal, candidates)
        if isinstance(verdict, str):
            raise _Skip(verdict)
        for candidate in candidates:
            result = reject_candidate(
                candidate.pk,
                actor=actor,
                note=OVERLAP_CLEANUP_NOTE,
            )
            if not result.applied:
                raise _Skip("mixed_or_nonpending_status")
        return verdict


def _classify(
    proposal: NonPersonEntityOccurrenceProposal,
    candidates: Sequence[NonPersonEntityOccurrenceCandidate],
) -> OverlapCleanupLine | str:
    """Return an eligible line, or the first failed condition.

    The overlap index is read once for this classification. A skip reason
    is one of ``_SKIP_REASONS``.
    """

    text = _current_text(proposal)
    if text is None or source_text_sha256(text) != proposal.source_text_sha256:
        return "stale_source"
    if proposal.normalization_version != SURFACE_V1:
        return "wrong_normalization_version"
    accepted, covering = _overlap_containment_index(text, _load_surface_index())
    identity = (proposal.normalized_surface, proposal.occurrence_ordinal)
    if identity not in accepted:
        return "not_currently_accepted"
    cover = covering.get(identity)
    if cover is None:
        return "not_contained"
    if not candidates or any(row.status != _PENDING for row in candidates):
        return "mixed_or_nonpending_status"
    if _identity_occurrence_exists(proposal):
        return "has_authoritative_occurrence"
    if not _every_candidate_has_detect(proposal, candidates):
        return "missing_detect_event"
    ordered = tuple(sorted(candidates, key=lambda row: row.pk))
    return OverlapCleanupLine(
        proposal_id=proposal.pk,
        archive_item_id=proposal.archive_item_id,
        text_kind=proposal.text_kind,
        normalized_surface=proposal.normalized_surface,
        occurrence_ordinal=proposal.occurrence_ordinal,
        source_text_sha256=proposal.source_text_sha256,
        covering_surface=cover[0],
        covering_ordinal=cover[1],
        candidate_ids=tuple(row.pk for row in ordered),
        candidate_entity_ids=tuple(row.candidate_entity_id for row in ordered),
    )


def _current_text(proposal: NonPersonEntityOccurrenceProposal) -> str | None:
    item = ArchiveItem.objects.get(pk=proposal.archive_item_id)
    if not item_supports_occurrence_text_kind(item, proposal.text_kind):
        return None
    return _read_authoritative_text(item, proposal.text_kind)


def _identity_occurrence_exists(
    proposal: NonPersonEntityOccurrenceProposal,
) -> bool:
    return ArchiveItemEntityOccurrence.objects.filter(
        archive_item_id=proposal.archive_item_id,
        text_kind=proposal.text_kind,
        source_text_sha256=proposal.source_text_sha256,
        normalization_version=proposal.normalization_version,
        normalized_surface=proposal.normalized_surface,
        occurrence_ordinal=proposal.occurrence_ordinal,
    ).exists()


def _lock_identity_occurrences(
    proposal: NonPersonEntityOccurrenceProposal,
) -> None:
    list(
        ArchiveItemEntityOccurrence.objects.select_for_update(of=("self",))
        .filter(
            archive_item_id=proposal.archive_item_id,
            text_kind=proposal.text_kind,
            source_text_sha256=proposal.source_text_sha256,
            normalization_version=proposal.normalization_version,
            normalized_surface=proposal.normalized_surface,
            occurrence_ordinal=proposal.occurrence_ordinal,
        )
        .order_by("pk")
    )


def _every_candidate_has_detect(
    proposal: NonPersonEntityOccurrenceProposal,
    candidates: Sequence[NonPersonEntityOccurrenceCandidate],
) -> bool:
    found = set(
        NonPersonEntityOccurrenceReviewEvent.objects.filter(
            proposal_id=proposal.pk,
            candidate_id__in=[row.pk for row in candidates],
            action=_DETECT,
        ).values_list("candidate_id", flat=True)
    )
    return all(row.pk in found for row in candidates)


def _record_verdict(
    report: OverlapCleanupReport,
    verdict: OverlapCleanupLine | str | None,
    *,
    rejected: int,
) -> None:
    if isinstance(verdict, str):
        _count_skip(report, verdict)
        return
    if verdict is None:
        return
    report.eligible_proposals += 1
    report.eligible_candidates += len(verdict.candidate_ids)
    report.rejected_candidates += rejected
    report.eligible.append(verdict)


def _count_skip(report: OverlapCleanupReport, reason: str) -> None:
    if reason not in _SKIP_REASONS:
        raise ValueError(f"Unknown overlap-cleanup skip reason: {reason}")
    setattr(report, reason, getattr(report, reason) + 1)


def _record_error(
    report: OverlapCleanupReport,
    proposal_id: int,
    exc: Exception,
) -> None:
    report.errors += 1
    report.error_lines.append(f"proposal_id={proposal_id}: {exc}")
