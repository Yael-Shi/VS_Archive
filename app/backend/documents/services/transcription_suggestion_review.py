"""Staff approve/reject for public transcription edit suggestions."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from documents.models import DocumentTextResult, TranscriptionEditSuggestion
from documents.services.text_presentation import (
    _is_hebrew_language,
    resolve_displayed_transcription_result,
)
from documents.services.transcription_edit_suggestions import normalize_transcription_text


class TranscriptionSuggestionReviewError(Exception):
    """Validation or eligibility failure for suggestion review actions."""


def _paired_hebrew_sync_rows(
    doc: Document,
    target: DocumentTextResult,
) -> list[DocumentTextResult]:
    """Hebrew docs: also update the other result_type row when it shares the engine."""
    if not _is_hebrew_language(doc):
        return [target]

    other_type = (
        DocumentTextResult.ResultType.SOURCE_TEXT
        if target.result_type == DocumentTextResult.ResultType.HEBREW_TEXT
        else DocumentTextResult.ResultType.HEBREW_TEXT
    )
    paired = DocumentTextResult.objects.filter(
        document=doc,
        result_type=other_type,
        engine=target.engine,
    ).first()
    if paired is None:
        return [target]
    return [target, paired]


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
        row_ids = [row.pk for row in _paired_hebrew_sync_rows(doc, target)]
        rows = list(
            DocumentTextResult.objects.select_for_update().filter(pk__in=row_ids)
        )

        reviewed_at = timezone.now()
        for row in rows:
            row.text = approved_text
            row.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
            row.save(update_fields=["text", "verification_status", "updated_at"])

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
