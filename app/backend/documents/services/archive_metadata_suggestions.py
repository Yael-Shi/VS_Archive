"""Public archive metadata suggestion form helpers."""

from __future__ import annotations

from documents.services.archive_discovery_metadata_validation import (
    historical_person_tag_reuse_errors,
    selected_tag_ids_from_post,
)
from public.services.registration import HONEYPOT_FIELD_NAME

NAME_REQUIRED_ERROR = "יש למלא שם."
SUGGESTION_CONTENT_REQUIRED_ERROR = "יש להזין מידע, קטגוריה, אירוע ו/או תגית."

SUGGESTION_STATUS_LABELS = {
    "PENDING": "ממתין לבדיקה",
    "APPROVED": "אושר",
    "REJECTED": "נדחה",
}


def normalize_suggestion_text(text: str) -> str:
    return (text or "").strip()


def is_honeypot_triggered(post_data) -> bool:
    return bool((post_data.get(HONEYPOT_FIELD_NAME) or "").strip())


def has_suggestion_content(
    *,
    suggested_categories: str,
    suggested_events: str,
    suggested_tags: str,
    submitter_note: str = "",
) -> bool:
    return any(
        normalize_suggestion_text(value)
        for value in (
            suggested_categories,
            suggested_events,
            suggested_tags,
            submitter_note,
        )
    )


def suggestion_status_label(status: str) -> str:
    return SUGGESTION_STATUS_LABELS.get(status, status)


def blocked_historical_tag_id_submission_errors(post_data) -> list[str]:
    """Reject tampered POSTs that include frozen historical person Tag ids."""
    return historical_person_tag_reuse_errors(selected_tag_ids_from_post(post_data))


def format_current_metadata_labels(archive_item) -> dict[str, str]:
    return {
        "categories": ", ".join(
            category.name for category in archive_item.categories.all()
        ),
        "events": ", ".join(event.name for event in archive_item.events.all()),
        "tags": ", ".join(tag.name for tag in archive_item.tags.all()),
    }
