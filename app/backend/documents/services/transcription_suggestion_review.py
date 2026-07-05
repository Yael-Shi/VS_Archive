"""Staff approve/reject for public transcription edit suggestions."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from documents.models import Document, DocumentTextResult, TranscriptionEditSuggestion
from documents.services.text_presentation import (
    is_hebrew_language,
    resolve_displayed_transcription_result,
)
from documents.services.transcription_edit_suggestions import (
    normalize_transcription_text,
)
from documents.services.verified_text_result_edit import (
    find_paired_hebrew_row,
    find_paired_source_row,
)


class TranscriptionSuggestionReviewError(Exception):
    """Validation or eligibility failure for suggestion review actions."""


def _apply_approved_suggestion_text(
    doc: Document,
    target: DocumentTextResult,
    approved_text: str,
) -> None:
    verified = DocumentTextResult.VerificationStatus.VERIFIED

    if is_hebrew_language(doc):
        source = find_paired_source_row(doc, engine=target.engine)
        hebrew = find_paired_hebrew_row(doc, engine=target.engine)
        if source is None or hebrew is None:
            raise TranscriptionSuggestionReviewError(
                "חסרה תוצאת טקסט מקור או עברי מקושרת; לא ניתן לאשר את ההצעה."
            )

        rows = DocumentTextResult.objects.select_for_update().filter(
            pk__in=[source.pk, hebrew.pk]
        )
        locked = {row.pk: row for row in rows}
        source = locked[source.pk]
        hebrew = locked[hebrew.pk]
        new_revision = source.source_revision + 1

        source.text = approved_text
        source.source_revision = new_revision
        source.verification_status = verified
        source.save(
            update_fields=[
                "text",
                "source_revision",
                "verification_status",
                "updated_at",
            ]
        )

        hebrew.text = approved_text
        hebrew.based_on_source_revision = new_revision
        hebrew.verification_status = verified
        hebrew.save(
            update_fields=[
                "text",
                "based_on_source_revision",
                "verification_status",
                "updated_at",
            ]
        )
        return

    if target.result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        target.text = approved_text
        target.source_revision += 1
        target.verification_status = verified
        target.save(
            update_fields=[
                "text",
                "source_revision",
                "verification_status",
                "updated_at",
            ]
        )
        return

    paired_source = find_paired_source_row(doc, engine=target.engine)
    if paired_source is None:
        raise TranscriptionSuggestionReviewError("אין תעתוק מקור לקישור גרסת תרגום.")

    paired_source = DocumentTextResult.objects.select_for_update().get(
        pk=paired_source.pk
    )
    target.text = approved_text
    target.based_on_source_revision = paired_source.source_revision
    target.verification_status = verified
    target.save(
        update_fields=[
            "text",
            "based_on_source_revision",
            "verification_status",
            "updated_at",
        ]
    )


def approve_suggestion(
    suggestion_id: int,
    *,
    approved_text: str,
    reviewer,
) -> TranscriptionEditSuggestion:
    if not normalize_transcription_text(approved_text):
        raise TranscriptionSuggestionReviewError("יש להזין טקסט מאושר.")

    with transaction.atomic():
        suggestion = (
            TranscriptionEditSuggestion.objects.select_for_update()
            .select_related("document")
            .get(pk=suggestion_id)
        )
        if suggestion.status != TranscriptionEditSuggestion.Status.PENDING:
            raise TranscriptionSuggestionReviewError("ההצעה כבר נבדקה.")

        doc = suggestion.document
        target = resolve_displayed_transcription_result(doc)
        if target is None:
            raise TranscriptionSuggestionReviewError("אין תעתוק להצגה לעדכון.")

        target = DocumentTextResult.objects.select_for_update().get(pk=target.pk)
        _apply_approved_suggestion_text(doc, target, approved_text)

        reviewed_at = timezone.now()
        suggestion.status = TranscriptionEditSuggestion.Status.APPROVED
        suggestion.approved_text = approved_text
        suggestion.applied_text_result = target
        suggestion.reviewed_at = reviewed_at
        suggestion.reviewed_by = reviewer
        suggestion.save(
            update_fields=[
                "status",
                "approved_text",
                "applied_text_result",
                "reviewed_at",
                "reviewed_by",
            ]
        )

    return suggestion


def reject_suggestion(
    suggestion_id: int,
    *,
    reviewer,
) -> TranscriptionEditSuggestion:
    with transaction.atomic():
        suggestion = TranscriptionEditSuggestion.objects.select_for_update().get(
            pk=suggestion_id
        )
        if suggestion.status != TranscriptionEditSuggestion.Status.PENDING:
            raise TranscriptionSuggestionReviewError("ההצעה כבר נבדקה.")

        suggestion.status = TranscriptionEditSuggestion.Status.REJECTED
        suggestion.reviewed_at = timezone.now()
        suggestion.reviewed_by = reviewer
        suggestion.save(update_fields=["status", "reviewed_at", "reviewed_by"])

    return suggestion
