"""Staff approve/reject for ArchiveItemPerson relationship-delta suggestions.

Authorize the suggestion/item in the view first (C2b). This module applies
one explicit ADD or REMOVE delta; it never reconstructs a Person set and
never calls set_archive_item_people.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from documents.models import ArchiveItemPerson, ArchiveItemPersonSuggestion
from documents.services.archive_item_people import (
    create_archive_item_person,
    delete_archive_item_person,
)

ALREADY_REVIEWED_ERROR = "ההצעה כבר נבדקה."


class ArchiveItemPersonSuggestionReviewError(Exception):
    """Validation or eligibility failure for person-suggestion review actions."""


@dataclass(frozen=True)
class ArchiveItemPersonSuggestionReviewResult:
    suggestion: ArchiveItemPersonSuggestion
    relationship_changed: bool


def archive_item_person_suggestions_queryset_for_user(user):
    """Staff backlog queryset scoped to archive items the user may view.

    C2b should use this (or equivalent) before calling approve/reject.
    The apply functions below do not re-check ArchiveItem visibility.
    """
    from documents.services.archive_item_access import archive_item_queryset_for_user

    return ArchiveItemPersonSuggestion.objects.filter(
        archive_item__in=archive_item_queryset_for_user(user)
    ).select_related(
        "archive_item",
        "person",
        "submitter_user",
        "reviewed_by",
    )


def _lock_pending_suggestion(
    suggestion_id: int,
) -> ArchiveItemPersonSuggestion:
    suggestion = (
        ArchiveItemPersonSuggestion.objects.select_for_update()
        .select_related("archive_item", "person")
        .get(pk=suggestion_id)
    )
    if suggestion.status != ArchiveItemPersonSuggestion.Status.PENDING:
        raise ArchiveItemPersonSuggestionReviewError(ALREADY_REVIEWED_ERROR)
    return suggestion


def _mark_reviewed(
    suggestion: ArchiveItemPersonSuggestion,
    *,
    status: str,
    reviewer,
) -> None:
    suggestion.status = status
    suggestion.reviewed_at = timezone.now()
    suggestion.reviewed_by = reviewer
    suggestion.save(update_fields=["status", "reviewed_at", "reviewed_by"])


def _apply_add_delta(suggestion: ArchiveItemPersonSuggestion) -> bool:
    already_linked = ArchiveItemPerson.objects.filter(
        archive_item_id=suggestion.archive_item_id,
        person_id=suggestion.person_id,
    ).exists()
    if already_linked:
        return False
    create_archive_item_person(
        archive_item=suggestion.archive_item,
        person=suggestion.person,
    )
    return True


def _apply_remove_delta(suggestion: ArchiveItemPersonSuggestion) -> bool:
    link = ArchiveItemPerson.objects.filter(
        archive_item_id=suggestion.archive_item_id,
        person_id=suggestion.person_id,
    ).first()
    if link is None:
        return False
    delete_archive_item_person(link)
    return True


def approve_suggestion(
    suggestion_id: int,
    *,
    reviewer,
) -> ArchiveItemPersonSuggestionReviewResult:
    with transaction.atomic():
        suggestion = _lock_pending_suggestion(suggestion_id)
        if suggestion.action == ArchiveItemPersonSuggestion.Action.ADD:
            relationship_changed = _apply_add_delta(suggestion)
        else:
            relationship_changed = _apply_remove_delta(suggestion)
        _mark_reviewed(
            suggestion,
            status=ArchiveItemPersonSuggestion.Status.APPROVED,
            reviewer=reviewer,
        )
    return ArchiveItemPersonSuggestionReviewResult(
        suggestion=suggestion,
        relationship_changed=relationship_changed,
    )


def reject_suggestion(
    suggestion_id: int,
    *,
    reviewer,
) -> ArchiveItemPersonSuggestionReviewResult:
    with transaction.atomic():
        suggestion = _lock_pending_suggestion(suggestion_id)
        _mark_reviewed(
            suggestion,
            status=ArchiveItemPersonSuggestion.Status.REJECTED,
            reviewer=reviewer,
        )
    return ArchiveItemPersonSuggestionReviewResult(
        suggestion=suggestion,
        relationship_changed=False,
    )
