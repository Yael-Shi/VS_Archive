"""Public archive metadata suggestion form helpers."""

from __future__ import annotations

from public.services.registration import HONEYPOT_FIELD_NAME

NAME_REQUIRED_ERROR = "יש למלא שם."
SUGGESTION_CONTENT_REQUIRED_ERROR = "יש להזין מידע, קטגוריה, אירוע או תגית מוצעים."

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


def format_current_metadata_labels(archive_item) -> dict[str, str]:
    return {
        "categories": ", ".join(
            category.name for category in archive_item.categories.all()
        ),
        "events": ", ".join(event.name for event in archive_item.events.all()),
        "tags": ", ".join(tag.name for tag in archive_item.tags.all()),
    }
