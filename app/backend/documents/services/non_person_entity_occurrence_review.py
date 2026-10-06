"""Staff review of non-person occurrence proposals.

Detection is not resolution. A candidate becomes an approved
``ArchiveItemEntityOccurrence`` only through an explicit action here.
Views call these functions and do not mutate the rows themselves.

Source revalidation runs inside the write transaction, immediately before
any create, update, or delete of an approved occurrence. It also runs
before ``REJECT`` and ``NEEDS_RESEARCH``. Those two actions do not create
an occurrence. If the proposal SHA no longer matches the current
authoritative displayed text, they return stale and leave the candidate
unchanged. They do not record a decision against the new text, and they
do not rewrite ``source_text_sha256``.

A proposal is stale when its stored SHA differs from the SHA of the
current authoritative text, or when that text is missing or the item no
longer supports the text kind. Stale is computed. It is not a database
column.

Unknown ``normalization_version`` fails closed even when the SHA still
matches. A missing ordinal, an unsafe original slice, or a located slice
that does not normalize back to the proposal surface also fails closed.
The proposal row stays stored history.

``REJECTED`` and ``REMOVED`` rows are kept. Review does not delete
proposals, candidates, matches, or events. ``REMOVED`` is a durable
suppression of that candidate slot. ``APPROVE`` and ``REASSIGN`` from
``REMOVED`` are refused so a review replay cannot quietly recreate the
pin. A mistaken removal stays removed until a later explicit product
decision says otherwise.

Plain ``APPROVE`` resolves to ``candidate_entity``, unless the candidate
is already ``APPROVED`` with a reviewed ``resolved_entity``. In that case
``APPROVE`` keeps that entity and does not undo a reassignment.
``REASSIGN`` is the action that may point a staff occurrence at a
different registry row. It does not create aliases. It does not apply
to a pin whose ``decision_id`` is set.

Same proposal identity plus the same already-applied outcome is
idempotent: no second occurrence and no second event. The same identity
pointing at a different entity is a conflict for ``APPROVE`` and for
``REMOVE``. ``REASSIGN`` is the explicit permission to change that entity
only when ``decision_id`` is null.

These actions do not write ``ReviewedNonPersonEntityDecision`` or
``ArchiveItemSearchIndex``. New staff occurrences leave ``decision`` null.
An occurrence with a non-null ``decision_id`` is a workbook pin.
``ReviewedNonPersonEntityDecision`` stays its provenance. Review never
updates or deletes that row. Approve of the same entity records the
candidate and an audit event without writing the pin. Approve of a
different entity conflicts. Reassign and remove are refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, Prefetch, Value, When
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    SurfaceOccurrence,
    authoritative_displayed_text,
    authoritative_text_context_for_item,
    item_supports_occurrence_text_kind,
    locate_surface_occurrences,
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_presentation import (
    non_person_public_name,
    non_person_public_type_label,
)
from documents.services.text_presentation import (
    displayable_document_text_results_queryset,
)

STALE_SOURCE_MESSAGE = "הטקסט המקורי השתנה, והבדיקה אינה עדכנית."
UNSUPPORTED_NORMALIZATION_MESSAGE = "גרסת הנרמול אינה נתמכת."
SOURCE_LOCATION_MESSAGE = "המופע לא נמצא בטקסט המוצג כעת."
OCCURRENCE_CONFLICT_MESSAGE = "כבר קיים קישור מאושר לאותו מופע, לרשומה אחרת."
PROTECTED_DECISION_OCCURRENCE_MESSAGE = (
    "האזכור הזה נקבע בבדיקת החוברת, ולא ניתן לשנות או להסיר אותו מכאן."
)
INVALID_TRANSITION_MESSAGE = "לא ניתן לבצע את הפעולה במצב הנוכחי של ההצעה."
APPROVED_REJECT_MESSAGE = "לא ניתן לדחות מופע מאושר לפני הסרת הקישור."
TARGET_NOT_FOUND_MESSAGE = "הרשומה שנבחרה לא נמצאה."
ALREADY_APPLIED_MESSAGE = "הפעולה כבר נשמרה."

APPROVE_APPLIED_MESSAGE = "האזכור אושר."
REJECT_APPLIED_MESSAGE = "ההצעה נדחתה."
NEEDS_RESEARCH_APPLIED_MESSAGE = "ההצעה סומנה לבדיקה נוספת."
REASSIGN_APPLIED_MESSAGE = "האזכור שויך לרשומה שנבחרה."
REMOVE_APPLIED_MESSAGE = "הקישור המאושר הוסר."

STATE_REVIEWABLE_LABEL = "ניתן לבדיקה"
STATE_STALE_LABEL = "הטקסט השתנה"
STATE_UNSUPPORTED_LABEL = "גרסת נרמול לא נתמכת"
STATE_NOT_LOCATED_LABEL = "המופע לא אומת בטקסט"

_CONTEXT_RADIUS = 48

_OPEN_STATUSES = (
    NonPersonEntityOccurrenceCandidate.Status.PENDING,
    NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH,
)

_STATUS_LABELS = {
    NonPersonEntityOccurrenceCandidate.Status.PENDING: "ממתין לבדיקה",
    NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH: "נדרש בירור",
    NonPersonEntityOccurrenceCandidate.Status.APPROVED: "מאושר",
    NonPersonEntityOccurrenceCandidate.Status.REJECTED: "נדחה",
    NonPersonEntityOccurrenceCandidate.Status.REMOVED: "הוסר",
}

_TEXT_KIND_LABELS = {
    ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT: "טקסט ידני",
    ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION: "תעתיק",
}

_MATCH_METHOD_LABELS = {
    NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME: "שם קנוני",
    NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME: "שם תצוגה",
    NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS: "שם חלופי",
    NonPersonEntityOccurrenceCandidateMatch.Method.MANUAL: "התאמה ידנית",
}

_NO_RESOLVED_ENTITY_STATUSES = {
    NonPersonEntityOccurrenceCandidate.Status.PENDING,
    NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH,
    NonPersonEntityOccurrenceCandidate.Status.REJECTED,
}


class NonPersonEntityOccurrenceReviewError(Exception):
    """Review action refused. No occurrence or candidate write is kept."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class StaleSourceReviewError(NonPersonEntityOccurrenceReviewError):
    """Proposal SHA does not match the current authoritative text."""


class UnsupportedNormalizationReviewError(NonPersonEntityOccurrenceReviewError):
    """Stored normalization version is not surface-v1."""


class SourceLocationReviewError(NonPersonEntityOccurrenceReviewError):
    """Current text does not still contain this occurrence identity."""


class OccurrenceConflictReviewError(NonPersonEntityOccurrenceReviewError):
    """The occurrence identity already points at a different entity."""


class InvalidTransitionReviewError(NonPersonEntityOccurrenceReviewError):
    """The candidate status does not allow this action."""


class ReviewTargetNotFoundError(NonPersonEntityOccurrenceReviewError):
    """Reassign target is not an existing registry row."""


class ProtectedDecisionOccurrenceReviewError(NonPersonEntityOccurrenceReviewError):
    """A workbook pin with a decision FK cannot be changed or deleted."""


@dataclass(frozen=True)
class OccurrenceReviewResult:
    """Outcome of one review action.

    ``applied`` is false when the current rows already recorded the same
    outcome. That replay does not append another event.
    """

    candidate: NonPersonEntityOccurrenceCandidate
    applied: bool


@dataclass(frozen=True)
class _SourceState:
    """Computed source check. Not stored."""

    code: str
    is_stale: bool
    is_reviewable: bool
    text: str | None
    located: SurfaceOccurrence | None

    @property
    def state_label(self) -> str:
        if self.code == "unsupported_normalization":
            return STATE_UNSUPPORTED_LABEL
        if self.is_stale:
            return STATE_STALE_LABEL
        if self.code == "not_located":
            return STATE_NOT_LOCATED_LABEL
        return STATE_REVIEWABLE_LABEL


@dataclass(frozen=True)
class OccurrenceReviewQueueRow:
    candidate_id: int
    item_id: int
    item_title: str
    item_url: str
    review_url: str
    text_kind_label: str
    matched_text: str
    context_before: str
    context_match: str
    context_after: str
    has_current_context: bool
    candidate_name: str
    type_label: str
    match_summary: str
    status: str
    status_label: str
    state_label: str
    is_stale: bool
    is_reviewable: bool


@dataclass(frozen=True)
class ReassignTargetOption:
    entity_id: int
    label: str


@dataclass(frozen=True)
class OccurrenceReviewDetail:
    candidate_id: int
    item_id: int
    item_title: str
    item_url: str
    text_kind_label: str
    occurrence_ordinal: int
    historical_matched_text: str
    context_before: str
    context_match: str
    context_after: str
    has_current_context: bool
    candidate_name: str
    type_label: str
    match_summary: str
    status: str
    status_label: str
    state_label: str
    is_stale: bool
    is_reviewable: bool
    resolved_name: str
    show_approve: bool
    show_reject: bool
    show_needs_research: bool
    show_reassign: bool
    show_remove: bool
    reassign_targets: tuple[ReassignTargetOption, ...]


def approve_candidate(
    candidate_id: int,
    *,
    actor,
    note: str = "",
) -> OccurrenceReviewResult:
    """Approve one candidate onto its candidate entity, or no-op if applied.

    An already ``APPROVED`` candidate keeps its reviewed ``resolved_entity``
    instead of being reset to ``candidate_entity``. ``REMOVED`` is refused
    and is not reopened. A workbook pin with the same entity is not
    rewritten; the candidate confirmation is recorded beside it. A workbook
    pin for a different entity is a conflict.
    """

    with transaction.atomic():
        proposal, candidate, occurrence = _lock_review_rows(candidate_id)
        if candidate.status == NonPersonEntityOccurrenceCandidate.Status.REMOVED:
            raise InvalidTransitionReviewError(INVALID_TRANSITION_MESSAGE)
        state = _require_current_source(proposal)
        intended_id = _approve_entity_id(candidate)
        if occurrence is not None and occurrence.entity_id != intended_id:
            raise OccurrenceConflictReviewError(OCCURRENCE_CONFLICT_MESSAGE)
        if _approve_already_applied(candidate, occurrence, intended_id):
            return OccurrenceReviewResult(candidate=candidate, applied=False)
        if occurrence is None:
            _create_resolved_occurrence(
                proposal,
                entity_id=intended_id,
                matched_text=state.located.matched_text if state.located else "",
            )
        _mark_candidate(
            candidate,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity_id=intended_id,
            actor=actor,
        )
        _append_event(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.APPROVE,
            to_entity_id=intended_id,
            actor=actor,
            note=note,
        )
        return OccurrenceReviewResult(candidate=candidate, applied=True)


def reject_candidate(
    candidate_id: int,
    *,
    actor,
    note: str = "",
) -> OccurrenceReviewResult:
    """Reject one candidate. Does not create an occurrence.

    ``APPROVED`` must be removed first. ``REMOVED`` stays removed.
    A stale source is refused without changing the stored status.
    """

    with transaction.atomic():
        proposal, candidate, _occurrence = _lock_review_rows(candidate_id)
        _require_current_source(proposal)
        status = candidate.status
        if status == NonPersonEntityOccurrenceCandidate.Status.APPROVED:
            raise InvalidTransitionReviewError(APPROVED_REJECT_MESSAGE)
        if status == NonPersonEntityOccurrenceCandidate.Status.REMOVED:
            raise InvalidTransitionReviewError(INVALID_TRANSITION_MESSAGE)
        if (
            status == NonPersonEntityOccurrenceCandidate.Status.REJECTED
            and candidate.resolved_entity_id is None
        ):
            return OccurrenceReviewResult(candidate=candidate, applied=False)
        _mark_candidate(
            candidate,
            status=NonPersonEntityOccurrenceCandidate.Status.REJECTED,
            resolved_entity_id=None,
            actor=actor,
        )
        _append_event(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.REJECT,
            actor=actor,
            note=note,
        )
        return OccurrenceReviewResult(candidate=candidate, applied=True)


def mark_needs_research(
    candidate_id: int,
    *,
    actor,
    note: str = "",
) -> OccurrenceReviewResult:
    """Mark one candidate for more research. Does not create an occurrence."""

    with transaction.atomic():
        proposal, candidate, _occurrence = _lock_review_rows(candidate_id)
        _require_current_source(proposal)
        status = candidate.status
        if status in {
            NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            NonPersonEntityOccurrenceCandidate.Status.REMOVED,
        }:
            raise InvalidTransitionReviewError(INVALID_TRANSITION_MESSAGE)
        if (
            status == NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
            and candidate.resolved_entity_id is None
        ):
            return OccurrenceReviewResult(candidate=candidate, applied=False)
        _mark_candidate(
            candidate,
            status=NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH,
            resolved_entity_id=None,
            actor=actor,
        )
        _append_event(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.NEEDS_RESEARCH,
            actor=actor,
            note=note,
        )
        return OccurrenceReviewResult(candidate=candidate, applied=True)


def reassign_candidate(
    candidate_id: int,
    *,
    target_entity_id: int,
    actor,
    note: str = "",
) -> OccurrenceReviewResult:
    """Resolve the occurrence to an existing registry row.

    This may create the occurrence or change the entity it already points
    at when that row has no decision FK. A workbook pin is refused with
    no candidate or event write. It does not create an alias. ``REMOVED``
    is not reopened.
    """

    with transaction.atomic():
        proposal, candidate, occurrence = _lock_review_rows(candidate_id)
        state = _require_current_source(proposal)
        _refuse_decision_backed_occurrence(occurrence)
        if candidate.status == NonPersonEntityOccurrenceCandidate.Status.REMOVED:
            raise InvalidTransitionReviewError(INVALID_TRANSITION_MESSAGE)
        target = _lock_target_entity(target_entity_id)
        if _reassign_already_applied(candidate, occurrence, target.pk):
            return OccurrenceReviewResult(candidate=candidate, applied=False)
        previous_id = occurrence.entity_id if occurrence is not None else None
        matched_text = state.located.matched_text if state.located else ""
        if occurrence is None:
            _create_resolved_occurrence(
                proposal,
                entity_id=target.pk,
                matched_text=matched_text,
            )
        else:
            occurrence.entity_id = target.pk
            occurrence.resolution_status = (
                ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
            )
            occurrence.matched_text = matched_text
            occurrence.save(
                update_fields=[
                    "entity",
                    "resolution_status",
                    "matched_text",
                    "updated_at",
                ]
            )
        _mark_candidate(
            candidate,
            status=NonPersonEntityOccurrenceCandidate.Status.APPROVED,
            resolved_entity_id=target.pk,
            actor=actor,
        )
        _append_event(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.REASSIGN,
            from_entity_id=previous_id,
            to_entity_id=target.pk,
            actor=actor,
            note=note,
        )
        return OccurrenceReviewResult(candidate=candidate, applied=True)


def remove_approved_occurrence(
    candidate_id: int,
    *,
    actor,
    note: str = "",
) -> OccurrenceReviewResult:
    """Delete the approved occurrence for this candidate and keep suppression.

    The occurrence is deleted only when it points at the candidate's
    current ``resolved_entity`` and ``decision_id`` is null. A workbook
    pin is refused. A different entity is a conflict. Proposal, candidate,
    match, and event rows stay.
    """

    with transaction.atomic():
        proposal, candidate, occurrence = _lock_review_rows(candidate_id)
        _require_current_source(proposal)
        _refuse_decision_backed_occurrence(occurrence)
        resolved_id = candidate.resolved_entity_id
        if occurrence is not None and occurrence.entity_id != resolved_id:
            raise OccurrenceConflictReviewError(OCCURRENCE_CONFLICT_MESSAGE)
        status = candidate.status
        if occurrence is None and status == (
            NonPersonEntityOccurrenceCandidate.Status.REMOVED
        ):
            return OccurrenceReviewResult(candidate=candidate, applied=False)
        if status != NonPersonEntityOccurrenceCandidate.Status.APPROVED:
            raise InvalidTransitionReviewError(INVALID_TRANSITION_MESSAGE)
        from_entity_id = resolved_id
        if occurrence is not None:
            from_entity_id = occurrence.entity_id
            occurrence.delete()
        _mark_candidate(
            candidate,
            status=NonPersonEntityOccurrenceCandidate.Status.REMOVED,
            resolved_entity_id=resolved_id,
            actor=actor,
        )
        _append_event(
            proposal=proposal,
            candidate=candidate,
            action=NonPersonEntityOccurrenceReviewEvent.Action.REMOVE,
            from_entity_id=from_entity_id,
            actor=actor,
            note=note,
        )
        return OccurrenceReviewResult(candidate=candidate, applied=True)


def staff_occurrence_review_rows(
    *,
    status: str = "",
    query: str = "",
    item_id: int | None = None,
) -> list[OccurrenceReviewQueueRow]:
    """Staff queue. Default is pending and needs-research, oldest id first.

    ``status="all"`` includes every candidate status. A known status value
    filters to that status. ``query`` is an item id when it is all digits,
    otherwise a case-insensitive title contains match.
    """

    candidates = list(_queue_queryset(status=status, query=query, item_id=item_id))
    states = _source_states_for_candidates(candidates)
    rows: list[OccurrenceReviewQueueRow] = []
    for candidate in candidates:
        proposal = candidate.proposal
        state = states[candidate.pk]
        before, match, after = _context_parts(state)
        item = proposal.archive_item
        rows.append(
            OccurrenceReviewQueueRow(
                candidate_id=candidate.pk,
                item_id=item.pk,
                item_title=_item_title(item),
                item_url=reverse("archive-detail", kwargs={"item_id": item.pk}),
                review_url=reverse(
                    "archive-manage-entity-occurrence-proposal",
                    kwargs={"candidate_id": candidate.pk},
                ),
                text_kind_label=_TEXT_KIND_LABELS.get(proposal.text_kind, ""),
                matched_text=proposal.matched_text,
                context_before=before,
                context_match=match,
                context_after=after,
                has_current_context=state.is_reviewable and state.located is not None,
                candidate_name=non_person_public_name(candidate.candidate_entity),
                type_label=non_person_public_type_label(candidate.candidate_entity),
                match_summary=_match_summary(candidate),
                status=candidate.status,
                status_label=_STATUS_LABELS.get(candidate.status, ""),
                state_label=state.state_label,
                is_stale=state.is_stale,
                is_reviewable=state.is_reviewable,
            )
        )
    return rows


def staff_occurrence_review_detail(candidate_id: int) -> OccurrenceReviewDetail:
    """One candidate prepared for the staff review page. Read-only."""

    candidate = _candidate_for_display(candidate_id)
    proposal = candidate.proposal
    state = _assess_proposal_source(proposal)
    before, match, after = _context_parts(state)
    item = proposal.archive_item
    status = candidate.status
    reviewable = state.is_reviewable
    open_review = status in _NO_RESOLVED_ENTITY_STATUSES
    resolved = candidate.resolved_entity
    decision_backed = _proposal_occurrence_is_decision_backed(proposal)
    return OccurrenceReviewDetail(
        candidate_id=candidate.pk,
        item_id=item.pk,
        item_title=_item_title(item),
        item_url=reverse("archive-detail", kwargs={"item_id": item.pk}),
        text_kind_label=_TEXT_KIND_LABELS.get(proposal.text_kind, ""),
        occurrence_ordinal=proposal.occurrence_ordinal,
        historical_matched_text=proposal.matched_text,
        context_before=before,
        context_match=match,
        context_after=after,
        has_current_context=reviewable and state.located is not None,
        candidate_name=non_person_public_name(candidate.candidate_entity),
        type_label=non_person_public_type_label(candidate.candidate_entity),
        match_summary=_match_summary(candidate),
        status=status,
        status_label=_STATUS_LABELS.get(status, ""),
        state_label=state.state_label,
        is_stale=state.is_stale,
        is_reviewable=reviewable,
        resolved_name=non_person_public_name(resolved) if resolved is not None else "",
        show_approve=reviewable and open_review,
        show_reject=reviewable and open_review,
        show_needs_research=reviewable
        and status
        in {
            NonPersonEntityOccurrenceCandidate.Status.PENDING,
            NonPersonEntityOccurrenceCandidate.Status.REJECTED,
        },
        show_reassign=reviewable
        and not decision_backed
        and status != NonPersonEntityOccurrenceCandidate.Status.REMOVED,
        show_remove=reviewable
        and not decision_backed
        and status == NonPersonEntityOccurrenceCandidate.Status.APPROVED,
        reassign_targets=tuple(_reassign_target_options()),
    )


def _queue_queryset(*, status: str, query: str, item_id: int | None):
    queryset = NonPersonEntityOccurrenceCandidate.objects.select_related(
        "proposal",
        "proposal__archive_item",
        "proposal__archive_item__manual_text_content",
        "candidate_entity",
        "resolved_entity",
    ).prefetch_related(
        Prefetch(
            "matches",
            queryset=NonPersonEntityOccurrenceCandidateMatch.objects.order_by("id"),
        ),
        Prefetch(
            "proposal__archive_item__ocr_document__text_results",
            queryset=displayable_document_text_results_queryset(),
        ),
    )
    if item_id is not None:
        queryset = queryset.filter(proposal__archive_item_id=item_id)
    cleaned_status = (status or "").strip()
    if cleaned_status == "all":
        pass
    elif cleaned_status in NonPersonEntityOccurrenceCandidate.Status.values:
        queryset = queryset.filter(status=cleaned_status)
    else:
        queryset = queryset.filter(status__in=_OPEN_STATUSES)
    cleaned_query = (query or "").strip()
    if cleaned_query.isdigit():
        queryset = queryset.filter(proposal__archive_item_id=int(cleaned_query))
    elif cleaned_query:
        queryset = queryset.filter(
            proposal__archive_item__title__icontains=cleaned_query
        )
    return queryset.annotate(
        queue_rank=Case(
            When(
                status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
                then=Value(0),
            ),
            When(
                status=NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH,
                then=Value(1),
            ),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by("queue_rank", "id")


def _candidate_for_display(candidate_id: int) -> NonPersonEntityOccurrenceCandidate:
    return _queue_queryset(status="all", query="", item_id=None).get(pk=candidate_id)


def _source_states_for_candidates(
    candidates: list[NonPersonEntityOccurrenceCandidate],
) -> dict[int, _SourceState]:
    contexts = {}
    states: dict[int, _SourceState] = {}
    for candidate in candidates:
        item = candidate.proposal.archive_item
        context = contexts.get(item.pk)
        if context is None:
            context = authoritative_text_context_for_item(item)
            contexts[item.pk] = context
        states[candidate.pk] = _assess_proposal_source(
            candidate.proposal,
            text=_text_from_context(candidate.proposal, context),
        )
    return states


def _text_from_context(proposal, context) -> str | None:
    if context.archive_item_id != proposal.archive_item_id:
        return None
    return context.texts.get(proposal.text_kind)


def _assess_proposal_source(
    proposal: NonPersonEntityOccurrenceProposal,
    *,
    text: str | None | object = ...,
) -> _SourceState:
    """Compare one proposal with the current authoritative text.

    Pass ``text`` only when the caller already loaded an
    ``AuthoritativeTextContext`` for this item. Omit it to read the text
    again from the database.
    """

    if proposal.normalization_version != SURFACE_V1:
        return _SourceState(
            code="unsupported_normalization",
            is_stale=False,
            is_reviewable=False,
            text=None,
            located=None,
        )
    item = proposal.archive_item
    if not item_supports_occurrence_text_kind(item, proposal.text_kind):
        return _stale_state()
    if text is ...:
        text = _read_authoritative_text(item, proposal.text_kind)
    if text is None:
        return _stale_state()
    if source_text_sha256(text) != proposal.source_text_sha256:
        return _stale_state(text=text)
    if normalize_surface_v1(proposal.normalized_surface) != proposal.normalized_surface:
        return _SourceState(
            code="not_located",
            is_stale=False,
            is_reviewable=False,
            text=text,
            located=None,
        )
    location = locate_surface_occurrences(text, proposal.normalized_surface)
    if location.occurrences is None:
        return _SourceState(
            code="not_located",
            is_stale=False,
            is_reviewable=False,
            text=text,
            located=None,
        )
    located = next(
        (
            occurrence
            for occurrence in location.occurrences
            if occurrence.ordinal == proposal.occurrence_ordinal
        ),
        None,
    )
    if located is None:
        return _SourceState(
            code="not_located",
            is_stale=False,
            is_reviewable=False,
            text=text,
            located=None,
        )
    if normalize_surface_v1(located.matched_text) != proposal.normalized_surface:
        return _SourceState(
            code="not_located",
            is_stale=False,
            is_reviewable=False,
            text=text,
            located=None,
        )
    return _SourceState(
        code="current",
        is_stale=False,
        is_reviewable=True,
        text=text,
        located=located,
    )


def _stale_state(*, text: str | None = None) -> _SourceState:
    return _SourceState(
        code="stale",
        is_stale=True,
        is_reviewable=False,
        text=text,
        located=None,
    )


def _require_current_source(
    proposal: NonPersonEntityOccurrenceProposal,
) -> _SourceState:
    item = ArchiveItem.objects.get(pk=proposal.archive_item_id)
    proposal.archive_item = item
    state = _assess_proposal_source(proposal)
    if state.code == "unsupported_normalization":
        raise UnsupportedNormalizationReviewError(UNSUPPORTED_NORMALIZATION_MESSAGE)
    if state.is_stale:
        raise StaleSourceReviewError(STALE_SOURCE_MESSAGE)
    if not state.is_reviewable or state.located is None:
        raise SourceLocationReviewError(SOURCE_LOCATION_MESSAGE)
    return state


def _read_authoritative_text(item: ArchiveItem, text_kind: str) -> str | None:
    """Read displayed text without using a cached manual-text relation."""

    kinds = ArchiveItemEntityOccurrence.TextKind
    if text_kind == kinds.MANUAL_TEXT:
        return (
            ManualTextContent.objects.filter(archive_item_id=item.pk)
            .values_list("body", flat=True)
            .first()
        )
    return authoritative_displayed_text(item, text_kind)


def _lock_review_rows(
    candidate_id: int,
) -> tuple[
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceCandidate,
    ArchiveItemEntityOccurrence | None,
]:
    proposal_id = (
        NonPersonEntityOccurrenceCandidate.objects.filter(pk=candidate_id)
        .values_list("proposal_id", flat=True)
        .first()
    )
    if proposal_id is None:
        raise NonPersonEntityOccurrenceCandidate.DoesNotExist
    proposal = NonPersonEntityOccurrenceProposal.objects.select_for_update(
        of=("self",)
    ).get(pk=proposal_id)
    candidate = NonPersonEntityOccurrenceCandidate.objects.select_for_update(
        of=("self",)
    ).get(pk=candidate_id, proposal_id=proposal.pk)
    occurrence = (
        ArchiveItemEntityOccurrence.objects.select_for_update(of=("self",))
        .filter(
            archive_item_id=proposal.archive_item_id,
            text_kind=proposal.text_kind,
            source_text_sha256=proposal.source_text_sha256,
            normalization_version=proposal.normalization_version,
            normalized_surface=proposal.normalized_surface,
            occurrence_ordinal=proposal.occurrence_ordinal,
        )
        .first()
    )
    return proposal, candidate, occurrence


def _lock_target_entity(target_entity_id: int) -> NonPersonEntity:
    try:
        return NonPersonEntity.objects.select_for_update(of=("self",)).get(
            pk=target_entity_id
        )
    except NonPersonEntity.DoesNotExist as exc:
        raise ReviewTargetNotFoundError(TARGET_NOT_FOUND_MESSAGE) from exc


def _approve_entity_id(candidate: NonPersonEntityOccurrenceCandidate) -> int:
    if (
        candidate.status == NonPersonEntityOccurrenceCandidate.Status.APPROVED
        and candidate.resolved_entity_id is not None
    ):
        return candidate.resolved_entity_id
    return candidate.candidate_entity_id


def _approve_already_applied(
    candidate: NonPersonEntityOccurrenceCandidate,
    occurrence: ArchiveItemEntityOccurrence | None,
    intended_id: int,
) -> bool:
    return (
        occurrence is not None
        and occurrence.entity_id == intended_id
        and candidate.status == NonPersonEntityOccurrenceCandidate.Status.APPROVED
        and candidate.resolved_entity_id == intended_id
    )


def _reassign_already_applied(
    candidate: NonPersonEntityOccurrenceCandidate,
    occurrence: ArchiveItemEntityOccurrence | None,
    target_id: int,
) -> bool:
    return (
        occurrence is not None
        and occurrence.entity_id == target_id
        and candidate.status == NonPersonEntityOccurrenceCandidate.Status.APPROVED
        and candidate.resolved_entity_id == target_id
    )


def _refuse_decision_backed_occurrence(
    occurrence: ArchiveItemEntityOccurrence | None,
) -> None:
    """Workbook provenance stays on the occurrence. Do not rewrite it."""

    if occurrence is not None and occurrence.decision_id is not None:
        raise ProtectedDecisionOccurrenceReviewError(
            PROTECTED_DECISION_OCCURRENCE_MESSAGE
        )


def _proposal_occurrence_is_decision_backed(
    proposal: NonPersonEntityOccurrenceProposal,
) -> bool:
    return ArchiveItemEntityOccurrence.objects.filter(
        archive_item_id=proposal.archive_item_id,
        text_kind=proposal.text_kind,
        source_text_sha256=proposal.source_text_sha256,
        normalization_version=proposal.normalization_version,
        normalized_surface=proposal.normalized_surface,
        occurrence_ordinal=proposal.occurrence_ordinal,
        decision_id__isnull=False,
    ).exists()


def _create_resolved_occurrence(
    proposal: NonPersonEntityOccurrenceProposal,
    *,
    entity_id: int,
    matched_text: str,
) -> ArchiveItemEntityOccurrence:
    try:
        with transaction.atomic():
            return ArchiveItemEntityOccurrence.objects.create(
                archive_item_id=proposal.archive_item_id,
                text_kind=proposal.text_kind,
                source_text_sha256=proposal.source_text_sha256,
                normalization_version=proposal.normalization_version,
                normalized_surface=proposal.normalized_surface,
                occurrence_ordinal=proposal.occurrence_ordinal,
                resolution_status=(
                    ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
                ),
                entity_id=entity_id,
                decision_id=None,
                matched_text=matched_text,
            )
    except IntegrityError as exc:
        raise OccurrenceConflictReviewError(OCCURRENCE_CONFLICT_MESSAGE) from exc


def _mark_candidate(
    candidate: NonPersonEntityOccurrenceCandidate,
    *,
    status: str,
    resolved_entity_id: int | None,
    actor,
) -> None:
    candidate.status = status
    candidate.resolved_entity_id = resolved_entity_id
    candidate.reviewed_by = actor
    candidate.reviewed_at = timezone.now()
    candidate.save(
        update_fields=[
            "status",
            "resolved_entity",
            "reviewed_by",
            "reviewed_at",
            "updated_at",
        ]
    )


def _append_event(
    *,
    proposal: NonPersonEntityOccurrenceProposal,
    candidate: NonPersonEntityOccurrenceCandidate,
    action: str,
    actor,
    note: str,
    from_entity_id: int | None = None,
    to_entity_id: int | None = None,
) -> None:
    NonPersonEntityOccurrenceReviewEvent.objects.create(
        proposal=proposal,
        candidate=candidate,
        action=action,
        from_entity_id=from_entity_id,
        to_entity_id=to_entity_id,
        actor=actor,
        note=(note or "").strip(),
    )


def _context_parts(state: _SourceState) -> tuple[str, str, str]:
    if not state.is_reviewable or state.text is None or state.located is None:
        return "", "", ""
    text = state.text
    start = state.located.start
    end = state.located.end
    before = text[max(0, start - _CONTEXT_RADIUS) : start]
    match = text[start:end]
    after = text[end : end + _CONTEXT_RADIUS]
    return before, match, after


def _match_summary(candidate: NonPersonEntityOccurrenceCandidate) -> str:
    parts: list[str] = []
    for match in candidate.matches.all():
        label = _MATCH_METHOD_LABELS.get(match.method, "")
        if label:
            parts.append(f"{label}: {match.matched_value}")
    return " · ".join(parts)


def _item_title(item: ArchiveItem) -> str:
    title = (item.title or "").strip()
    if title:
        return title
    return f"פריט {item.pk}"


def _reassign_target_options() -> list[ReassignTargetOption]:
    options: list[ReassignTargetOption] = []
    for entity in NonPersonEntity.objects.order_by("canonical_name", "id"):
        type_label = non_person_public_type_label(entity)
        name = non_person_public_name(entity)
        label = f"{name} — {type_label}" if type_label else name
        options.append(ReassignTargetOption(entity_id=entity.pk, label=label))
    return options
