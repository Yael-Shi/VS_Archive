"""Staff edits to DocumentTextResult rows (pending review and verified)."""

from __future__ import annotations

from django.db import transaction

from documents.models import Document, DocumentTextResult, DocumentTextResultEdit
from documents.services.review_backlog import is_review_editable_text_result
from documents.services.text_presentation import get_displayed_transcription_text
from documents.services.transcription_edit_suggestions import (
    normalize_transcription_text,
    texts_are_equivalent,
)


class VerifiedTextResultEditError(Exception):
    """Validation or eligibility failure for verified text edits."""


class PendingTextResultEditError(Exception):
    """Validation or eligibility failure for pending review text edits."""


def _is_hebrew_document(doc: Document) -> bool:
    return doc.language == Document.Language.HEBREW


def is_verified_editable_text_result(row: DocumentTextResult) -> bool:
    """Whether staff may edit an already-verified OCR/HTR text result."""
    if row.verification_status != DocumentTextResult.VerificationStatus.VERIFIED:
        return False
    if row.result_type not in (
        DocumentTextResult.ResultType.SOURCE_TEXT,
        DocumentTextResult.ResultType.HEBREW_TEXT,
    ):
        return False
    if row.status not in (
        DocumentTextResult.Status.NEEDS_REVIEW,
        DocumentTextResult.Status.SUCCEEDED,
    ):
        return False
    return bool(normalize_transcription_text(row.text or ""))


def find_paired_source_row(
    doc: Document,
    *,
    engine: str,
) -> DocumentTextResult | None:
    return DocumentTextResult.objects.filter(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=engine,
    ).first()


def find_paired_hebrew_row(
    doc: Document,
    *,
    engine: str,
) -> DocumentTextResult | None:
    return DocumentTextResult.objects.filter(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=engine,
    ).first()


def is_hebrew_translation_stale(
    hebrew_row: DocumentTextResult,
    source_row: DocumentTextResult | None,
) -> bool:
    """Non-Hebrew docs: HEBREW_TEXT is stale when revision linkage mismatches."""
    if _is_hebrew_document(hebrew_row.document):
        return False
    if hebrew_row.result_type != DocumentTextResult.ResultType.HEBREW_TEXT:
        return False
    if source_row is None:
        return False
    if hebrew_row.based_on_source_revision is None:
        return True
    return hebrew_row.based_on_source_revision != source_row.source_revision


def _lock_rows(row_ids: list[int]) -> dict[int, DocumentTextResult]:
    rows = DocumentTextResult.objects.select_for_update().filter(pk__in=row_ids)
    return {row.pk: row for row in rows}


def _save_text_result_row(
    row: DocumentTextResult,
    *,
    update_fields: list[str],
    force_verified: bool,
) -> None:
    if force_verified:
        row.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
        if "verification_status" not in update_fields:
            update_fields = [*update_fields, "verification_status"]
    row.save(update_fields=[*update_fields, "updated_at"])


def _save_verified_row(
    row: DocumentTextResult,
    *,
    update_fields: list[str],
) -> None:
    _save_text_result_row(row, update_fields=update_fields, force_verified=True)


def _apply_hebrew_document_mirror_edit(
    *,
    source_row: DocumentTextResult | None,
    hebrew_row: DocumentTextResult | None,
    normalized: str,
    force_verified: bool,
) -> None:
    new_revision = (source_row.source_revision + 1) if source_row is not None else 1

    if source_row is not None:
        source_row.text = normalized
        source_row.source_revision = new_revision
        _save_text_result_row(
            source_row,
            update_fields=["text", "source_revision"],
            force_verified=force_verified,
        )

    if hebrew_row is not None:
        hebrew_row.text = normalized
        if source_row is not None:
            hebrew_row.based_on_source_revision = new_revision
        _save_text_result_row(
            hebrew_row,
            update_fields=(
                ["text", "based_on_source_revision"]
                if source_row is not None
                else ["text"]
            ),
            force_verified=force_verified,
        )


def _apply_text_result_edit(
    *,
    target: DocumentTextResult,
    doc: Document,
    persist_text: str,
    audit_new_text: str,
    editor,
    force_verified: bool,
) -> DocumentTextResult:
    """Apply a text change with revision/audit semantics (caller holds transaction)."""
    is_hebrew_doc = _is_hebrew_document(doc)
    old_text = target.text or ""
    lock_ids = [target.pk]

    if is_hebrew_doc:
        paired_source = find_paired_source_row(doc, engine=target.engine)
        paired_hebrew = find_paired_hebrew_row(doc, engine=target.engine)
        if paired_source is None or paired_hebrew is None:
            raise VerifiedTextResultEditError(
                "חסרה תוצאת טקסט מקור או עברי מקושרת; לא ניתן לשמור את העריכה."
            )
        lock_ids.append(paired_source.pk)
        lock_ids.append(paired_hebrew.pk)

        locked = _lock_rows(lock_ids)
        target = locked[target.pk]
        source_row = locked[paired_source.pk]
        hebrew_row = locked[paired_hebrew.pk]

        _apply_hebrew_document_mirror_edit(
            source_row=source_row,
            hebrew_row=hebrew_row,
            normalized=persist_text,
            force_verified=force_verified,
        )
        edit_type = (
            DocumentTextResultEdit.EditType.SOURCE_TEXT
            if target.result_type == DocumentTextResult.ResultType.SOURCE_TEXT
            else DocumentTextResultEdit.EditType.HEBREW_TEXT
        )
    elif target.result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        paired_hebrew = find_paired_hebrew_row(doc, engine=target.engine)
        if paired_hebrew is not None:
            lock_ids.append(paired_hebrew.pk)

        locked = _lock_rows(lock_ids)
        target = locked[target.pk]

        target.text = persist_text
        target.source_revision += 1
        _save_text_result_row(
            target,
            update_fields=["text", "source_revision"],
            force_verified=force_verified,
        )
        edit_type = DocumentTextResultEdit.EditType.SOURCE_TEXT
    else:
        paired_source = find_paired_source_row(doc, engine=target.engine)
        if paired_source is None:
            raise VerifiedTextResultEditError("אין תעתוק מקור לקישור גרסת תרגום.")
        lock_ids.append(paired_source.pk)

        locked = _lock_rows(lock_ids)
        target = locked[target.pk]
        source_row = locked[paired_source.pk]

        target.text = persist_text
        target.based_on_source_revision = source_row.source_revision
        _save_text_result_row(
            target,
            update_fields=["text", "based_on_source_revision"],
            force_verified=force_verified,
        )
        edit_type = DocumentTextResultEdit.EditType.HEBREW_TEXT

    DocumentTextResultEdit.objects.create(
        text_result=target,
        editor=editor,
        old_text=old_text,
        new_text=audit_new_text,
        edit_type=edit_type,
    )
    return target


def _submitted_text_differs_from_current(
    target: DocumentTextResult,
    doc: Document,
    normalized: str,
) -> bool:
    if _is_hebrew_document(doc):
        return not texts_are_equivalent(
            get_displayed_transcription_text(doc),
            normalized,
        )
    return not texts_are_equivalent(target.text or "", normalized)


def edit_verified_text_result(
    *,
    result_id: int,
    new_text: str,
    editor,
) -> DocumentTextResult:
    normalized = normalize_transcription_text(new_text)
    if not normalized:
        raise VerifiedTextResultEditError("יש להזין טקסט.")

    with transaction.atomic():
        target = DocumentTextResult.objects.select_for_update().get(pk=result_id)
        if not is_verified_editable_text_result(target):
            raise VerifiedTextResultEditError("תוצאה זו אינה זמינה לעריכה מאושרת.")

        doc = Document.objects.select_for_update().get(pk=target.document_id)

        if not _submitted_text_differs_from_current(target, doc, normalized):
            raise VerifiedTextResultEditError("לא בוצעו שינויים בטקסט.")

        target = _apply_text_result_edit(
            target=target,
            doc=doc,
            persist_text=normalized,
            audit_new_text=normalized,
            editor=editor,
            force_verified=True,
        )
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(doc.archive_item_id)

    return target


def edit_pending_text_result(
    *,
    result_id: int,
    new_text: str,
    editor,
) -> DocumentTextResult:
    normalized = normalize_transcription_text(new_text)
    if not normalized:
        raise PendingTextResultEditError("text is required and must be non-empty")

    with transaction.atomic():
        target = DocumentTextResult.objects.select_for_update().get(pk=result_id)
        if not is_review_editable_text_result(target):
            raise PendingTextResultEditError(
                "transcription result is not eligible for review action"
            )

        doc = Document.objects.select_for_update().get(pk=target.document_id)

        if not _submitted_text_differs_from_current(target, doc, normalized):
            return target

        try:
            target = _apply_text_result_edit(
                target=target,
                doc=doc,
                persist_text=new_text,
                audit_new_text=new_text,
                editor=editor,
                force_verified=False,
            )
        except VerifiedTextResultEditError as exc:
            raise PendingTextResultEditError(str(exc)) from exc

        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(doc.archive_item_id)

    return target
