"""Staff approve/reject for public archive metadata suggestions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from documents.historical_person_tag_map import is_retired_historical_person_tag_name
from documents.models import ArchiveMetadataSuggestion
from documents.services.archive_discovery_metadata_validation import (
    historical_person_tag_name_write_errors,
    retired_historical_person_tag_name_errors,
)
from documents.services.archive_items import (
    _get_or_create_archive_event_by_name,
    _get_or_create_tag_by_name,
    get_or_create_archive_category_by_name,
)
from documents.services.archive_metadata_suggestions import normalize_suggestion_text


class ArchiveMetadataSuggestionReviewError(Exception):
    """Validation or eligibility failure for metadata suggestion review actions."""


@dataclass(frozen=True)
class PendingRetiredHistoricalPersonTagSuggestion:
    """Read-only inventory row for D2b preflight. Does not mutate suggestions."""

    suggestion_id: int
    archive_item_id: int
    retired_names: tuple[str, ...]


def parse_suggested_metadata_values(text: str) -> list[str]:
    """Split free-text suggestions by newlines and commas; trim, dedupe, drop blanks."""
    if not text:
        return []

    seen: set[str] = set()
    values: list[str] = []
    for part in re.split(r"[\n,]+", text):
        value = normalize_suggestion_text(part)
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def retired_historical_person_tag_suggested_tags_errors(
    suggested_tags: str,
) -> list[str]:
    """Reject retired historical Tag names in suggestion free text after parse/trim."""
    return retired_historical_person_tag_name_errors(
        parse_suggested_metadata_values(suggested_tags)
    )


def pending_archive_metadata_suggestions_with_retired_tag_names() -> list[
    PendingRetiredHistoricalPersonTagSuggestion
]:
    """List PENDING metadata suggestions whose parsed tags include a retired name.

    Read-only. Does not reject, mutate, or delete rows. D2b Tag-row deletion
    must refuse while this inventory is non-empty.
    """
    inventory: list[PendingRetiredHistoricalPersonTagSuggestion] = []
    suggestions = (
        ArchiveMetadataSuggestion.objects.filter(
            status=ArchiveMetadataSuggestion.Status.PENDING
        )
        .order_by("pk")
        .only("id", "archive_item_id", "suggested_tags")
    )
    for suggestion in suggestions:
        retired_names = tuple(
            name
            for name in parse_suggested_metadata_values(suggestion.suggested_tags)
            if is_retired_historical_person_tag_name(name)
        )
        if retired_names:
            inventory.append(
                PendingRetiredHistoricalPersonTagSuggestion(
                    suggestion_id=suggestion.id,
                    archive_item_id=suggestion.archive_item_id,
                    retired_names=retired_names,
                )
            )
    return inventory


def approve_suggestion(
    suggestion_id: int,
    *,
    reviewer,
) -> ArchiveMetadataSuggestion:
    with transaction.atomic():
        suggestion = (
            ArchiveMetadataSuggestion.objects.select_for_update()
            .select_related("archive_item")
            .get(pk=suggestion_id)
        )
        if suggestion.status != ArchiveMetadataSuggestion.Status.PENDING:
            raise ArchiveMetadataSuggestionReviewError("ההצעה כבר נבדקה.")

        archive_item = suggestion.archive_item

        category_names = parse_suggested_metadata_values(
            suggestion.suggested_categories
        )
        event_names = parse_suggested_metadata_values(suggestion.suggested_events)
        tag_names = parse_suggested_metadata_values(suggestion.suggested_tags)
        reuse_errors = historical_person_tag_name_write_errors(tag_names)
        if reuse_errors:
            raise ArchiveMetadataSuggestionReviewError(reuse_errors[0])

        if category_names:
            categories = [
                get_or_create_archive_category_by_name(name)[0]
                for name in category_names
            ]
            archive_item.categories.add(*categories)

        if event_names:
            events = [
                _get_or_create_archive_event_by_name(name)[0] for name in event_names
            ]
            archive_item.events.add(*events)

        if tag_names:
            tags = [_get_or_create_tag_by_name(name)[0] for name in tag_names]
            archive_item.tags.add(*tags)

        if category_names or event_names or tag_names:
            from documents.services.archive_search_index import (
                sync_archive_item_search_index,
            )

            sync_archive_item_search_index(archive_item.pk)

        reviewed_at = timezone.now()
        suggestion.status = ArchiveMetadataSuggestion.Status.APPROVED
        suggestion.reviewed_at = reviewed_at
        suggestion.reviewed_by = reviewer
        suggestion.save(update_fields=["status", "reviewed_at", "reviewed_by"])

    return suggestion


def reject_suggestion(
    suggestion_id: int,
    *,
    reviewer,
) -> ArchiveMetadataSuggestion:
    with transaction.atomic():
        suggestion = ArchiveMetadataSuggestion.objects.select_for_update().get(
            pk=suggestion_id
        )
        if suggestion.status != ArchiveMetadataSuggestion.Status.PENDING:
            raise ArchiveMetadataSuggestionReviewError("ההצעה כבר נבדקה.")

        suggestion.status = ArchiveMetadataSuggestion.Status.REJECTED
        suggestion.reviewed_at = timezone.now()
        suggestion.reviewed_by = reviewer
        suggestion.save(update_fields=["status", "reviewed_at", "reviewed_by"])

    return suggestion
