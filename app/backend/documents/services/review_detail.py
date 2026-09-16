"""Staff transcription-review page projection (GET and post-mutation AJAX)."""

from __future__ import annotations

from django.template.loader import render_to_string

from documents.models import Document, DocumentTextResult
from documents.services.review_backlog import (
    is_review_editable_text_result,
    is_review_pending_text_result,
    parse_review_reasons,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.verified_text_result_edit import (
    find_paired_hebrew_row,
    find_paired_source_row,
    is_hebrew_translation_stale,
    is_verified_editable_text_result,
    review_form_revision_for_row,
)

REVIEW_TEXT_RESULT_CARD_TEMPLATE = "documents/partials/review_text_result_card.html"


def review_result_type_label(doc: Document, result_type: str) -> str:
    if doc.language == Document.Language.HEBREW:
        if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
            return "תעתוק מקור (עברית כפי שחולצה)"
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            return "טקסט עברי לבדיקה"

    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return "תעתוק מקור"
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return "טקסט עברי"
    return result_type


def review_result_type_description(doc: Document, result_type: str) -> str:
    """One-line reviewer-facing explanation of what a text result represents."""
    if doc.language == Document.Language.HEBREW:
        if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
            return "טקסט המקור כפי שחולץ אוטומטית מן המסמך."
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            return "הטקסט העברי שמיועד לבדיקה ולאישור."

    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return "טקסט בשפת המקור כפי שחולץ אוטומטית."
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return "תרגום לעברית (אם קיים)."
    return ""


def review_non_actionable_reason(row: DocumentTextResult) -> str | None:
    """
    Human-readable reason when edit/approve/reject controls are unavailable.

    Display-only; uses the same eligibility rules as ``is_review_pending_text_result``.
    """
    if is_review_pending_text_result(row):
        return None

    if row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED:
        if is_verified_editable_text_result(row):
            return None
        return "התעתוק כבר אושר אנושית — אין פעולות בקרה זמינות במסך זה."

    if row.status == DocumentTextResult.Status.FAILED:
        return "תעתוק זה נכשל בעיבוד — לא ניתן לבדוק או לאשר."

    if not (row.text or "").strip():
        return "אין טקסט זמין לבדיקה."

    if row.status != DocumentTextResult.Status.NEEDS_REVIEW:
        return "תוצאה זו אינה ממתינה לבקרה."

    return "פעולות בקרה אינן זמינות לתוצאה זו."


def build_review_text_result_cards(doc: Document) -> list[dict]:
    """Build the review-card dicts rendered by GET review detail.

    Uses in-memory ``doc.text_results`` (prefetch on the document when possible).
    """
    text_results = sorted(
        doc.text_results.all(),
        key=lambda r: (r.result_type, r.engine, -r.updated_at.timestamp()),
    )
    source_by_engine = {
        r.engine: r
        for r in text_results
        if r.result_type == DocumentTextResult.ResultType.SOURCE_TEXT
    }
    text_result_cards = []
    for row in text_results:
        paired_source = (
            source_by_engine.get(row.engine)
            if row.result_type == DocumentTextResult.ResultType.HEBREW_TEXT
            else None
        )
        text_result_cards.append(
            {
                "row": row,
                "result_type_label": review_result_type_label(doc, row.result_type),
                "result_type_description": review_result_type_description(
                    doc, row.result_type
                ),
                "review_reasons": parse_review_reasons(row.review_reasons),
                "text_length": len((row.text or "").strip()),
                "is_pending_review": is_review_pending_text_result(row),
                "is_editable": is_review_editable_text_result(row),
                "is_verified_editable": is_verified_editable_text_result(row),
                "hebrew_translation_stale": is_hebrew_translation_stale(
                    row, paired_source
                ),
                "non_actionable_reason": review_non_actionable_reason(row),
                "expected_text_sha256": compute_sha256_hex(row.text or ""),
                "expected_source_revision": review_form_revision_for_row(
                    row, doc, paired_source=paired_source
                ),
            }
        )
    return text_result_cards


def render_review_text_result_card_html(request, card: dict) -> str:
    """Server-rendered HTML for one review card (same template as GET)."""
    return render_to_string(
        REVIEW_TEXT_RESULT_CARD_TEMPLATE,
        {"card": card},
        request=request,
    )


def affected_review_card_result_ids(
    *,
    target: DocumentTextResult,
    text_saved: bool,
) -> tuple[int, ...]:
    """Result ids whose GET card projection may have changed for this mutation.

    Always includes ``target``. Additional same-engine rows are included only
    when this mutation writes or changes their stored/rendered card state:

    * reject / verify-only / save no-op (``text_saved=False``): target only
    * Hebrew-language SOURCE or HEBREW text write: same-engine SOURCE + HEBREW
      (established mirror)
    * non-Hebrew SOURCE text write: same-engine HEBREW (stale-translation
      presentation)
    * non-Hebrew HEBREW text write: target only (SOURCE is not rewritten)

    Unrelated engines and unrelated result rows are never included.
    """
    ids = {int(target.pk)}
    if not text_saved:
        return tuple(sorted(ids))

    doc = target.document
    if doc.language == Document.Language.HEBREW:
        source = find_paired_source_row(doc, engine=target.engine)
        hebrew = find_paired_hebrew_row(doc, engine=target.engine)
        if source is not None:
            ids.add(int(source.pk))
        if hebrew is not None:
            ids.add(int(hebrew.pk))
        return tuple(sorted(ids))

    if target.result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        hebrew = find_paired_hebrew_row(doc, engine=target.engine)
        if hebrew is not None:
            ids.add(int(hebrew.pk))
    return tuple(sorted(ids))


def review_mutation_card_payload(
    request,
    *,
    document_id: int,
    result_ids: tuple[int, ...] | list[int],
) -> list[dict]:
    """Authoritative post-mutation cards for the affected result ids only."""
    wanted = {int(result_id) for result_id in result_ids}
    if not wanted:
        return []
    doc = Document.objects.prefetch_related("text_results").get(pk=document_id)
    return [
        {
            "result_id": card["row"].id,
            "html": render_review_text_result_card_html(request, card),
        }
        for card in build_review_text_result_cards(doc)
        if int(card["row"].id) in wanted
    ]


__all__ = [
    "REVIEW_TEXT_RESULT_CARD_TEMPLATE",
    "affected_review_card_result_ids",
    "build_review_text_result_cards",
    "render_review_text_result_card_html",
    "review_mutation_card_payload",
    "review_non_actionable_reason",
    "review_result_type_description",
    "review_result_type_label",
]
