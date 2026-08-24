"""Staff approve/reject for public archive metadata suggestions."""

from __future__ import annotations

import re

from django.db import transaction
from django.utils import timezone

from documents.models import ArchiveMetadataSuggestion
from documents.services.archive_discovery_metadata_validation import (
    existing_tag_name_reuse_errors,
)
from documents.services.archive_items import (
    _get_or_create_archive_event_by_name,
    _get_or_create_tag_by_name,
    get_or_create_archive_category_by_name,
)
from documents.services.archive_metadata_suggestions import normalize_suggestion_text


class ArchiveMetadataSuggestionReviewError(Exception):
    """Validation or eligibility failure for metadata suggestion review actions."""


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
        reuse_errors = existing_tag_name_reuse_errors(tag_names)
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
