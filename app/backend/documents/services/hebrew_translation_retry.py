"""Staff retry of failed or missing Hebrew translation for non-Hebrew OCR documents."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from documents.models import ArchiveItem, Document, DocumentTextResult
from documents.services.env_validation import WorkerEnvConfig
from documents.services.gemini_engine import translate_text_to_hebrew_with_gemini
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL
from documents.services.non_hebrew_hebrew_translation import (
    persist_hebrew_translation_result,
)
from documents.services.processing_state import (
    update_document_processing_state_for_engine,
)
from documents.services.sqs import send_process_document_message
from documents.services.text_presentation import resolve_displayed_transcription_result

logger = logging.getLogger(__name__)

PROCESS_DOCUMENT_OPERATION_KEY = "operation"
RETRY_HEBREW_TRANSLATION_OPERATION = "retry_hebrew_translation"
STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD = timedelta(minutes=30)


class HebrewTranslationRetryError(RuntimeError):
    """Raised when Hebrew translation retry cannot run."""


def _is_hebrew_language(language: str | None) -> bool:
    lang = (language or "").strip().lower()
    return lang in ("he", "heb", "hebrew")


def _hebrew_would_be_overwritten(row: DocumentTextResult | None) -> bool:
    if row is None:
        return False
    if row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED:
        return True
    if row.status not in (
        DocumentTextResult.Status.SUCCEEDED,
        DocumentTextResult.Status.NEEDS_REVIEW,
    ):
        return False
    return bool((row.text or "").strip())


def _protected_hebrew_text_exists_for_document(doc: Document) -> bool:
    rows = DocumentTextResult.objects.filter(
        document_id=doc.id,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
    )
    return any(_hebrew_would_be_overwritten(row) for row in rows)


def _processing_lease_is_fresh(doc: Document, *, now) -> bool:
    if doc.processing_state_user != Document.ProcessingState.PROCESSING:
        return False
    return (now - doc.updated_at) <= STALE_TRANSLATION_RETRY_PROCESSING_THRESHOLD


def _validate_retry_document_metadata(doc: Document) -> None:
    if _is_hebrew_language(doc.language):
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} language={doc.language!r} is Hebrew; "
            "translation-only retry applies to non-Hebrew documents only."
        )

    if doc.upload_status != Document.UploadStatus.UPLOADED:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} upload_status={doc.upload_status!r} "
            "must be UPLOADED."
        )

    if not doc.archive_item_id:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} has no linked ArchiveItem; "
            "only OCR_DOCUMENT archive items are supported."
        )

    try:
        item_type = doc.archive_item.item_type
    except ArchiveItem.DoesNotExist as exc:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} is linked to missing ArchiveItem "
            f"id={doc.archive_item_id}; only OCR_DOCUMENT is supported."
        ) from exc

    if item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} is linked to ArchiveItem item_type="
            f"{item_type!r}; only OCR_DOCUMENT is supported."
        )


def _validate_usable_source_row(doc: Document) -> DocumentTextResult:
    source_row = resolve_displayed_transcription_result(doc)
    if (
        source_row is None
        or source_row.result_type != DocumentTextResult.ResultType.SOURCE_TEXT
    ):
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} has no usable SOURCE_TEXT to translate."
        )

    if not (source_row.text or "").strip():
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} has no usable SOURCE_TEXT to translate."
        )

    return source_row


def validate_document_for_hebrew_translation_retry(doc: Document) -> DocumentTextResult:
    _validate_retry_document_metadata(doc)

    if doc.processing_state_user == Document.ProcessingState.PROCESSING:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} is currently processing; retry is blocked."
        )

    source_row = _validate_usable_source_row(doc)

    if _protected_hebrew_text_exists_for_document(doc):
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} already has successful or verified Hebrew translation; "
            "retry is blocked."
        )

    return source_row


def validate_document_for_hebrew_translation_retry_persistence(
    doc: Document,
    *,
    expected_engine: str,
    expected_source_text: str,
) -> DocumentTextResult:
    _validate_retry_document_metadata(doc)

    if doc.processing_state_user != Document.ProcessingState.PROCESSING:
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} is not in expected PROCESSING state "
            "for translation retry persistence."
        )

    source_row = _validate_usable_source_row(doc)

    if (
        source_row.engine != expected_engine
        or (source_row.text or "") != expected_source_text
    ):
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} SOURCE_TEXT changed during translation retry; "
            "aborting without overwrite."
        )

    if _protected_hebrew_text_exists_for_document(doc):
        raise HebrewTranslationRetryError(
            f"Document id={doc.id} gained successful or verified Hebrew translation "
            "during retry; aborting without overwrite."
        )

    return source_row


def _restore_processing_state_after_retry_abort(document_id: int, engine: str) -> None:
    """Best-effort rollback of PROCESSING after an aborted retry persistence attempt.

    Called when persistence validation or writes fail. Any restoration error is logged
    but not raised, so the original retry/persistence exception remains the one propagated.
    Restoration cannot be guaranteed during a database outage or similar failure.
    """
    try:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=document_id)
            if doc.processing_state_user != Document.ProcessingState.PROCESSING:
                return
            update_document_processing_state_for_engine(doc, engine)
            doc.save(update_fields=["processing_state_user", "updated_at"])
    except Exception:
        logger.exception(
            "Failed to restore processing_state_user after Hebrew translation retry abort "
            "document_id=%s engine=%s",
            document_id,
            engine,
        )


def _recompute_processing_state_from_source_engine(doc: Document) -> str:
    source_row = _validate_usable_source_row(doc)
    update_document_processing_state_for_engine(doc, source_row.engine)
    doc.save(update_fields=["processing_state_user", "updated_at"])
    return source_row.engine


def _claim_translation_retry(
    doc: Document,
    *,
    document_id: int,
    now,
) -> tuple[str, str] | bool:
    """Return (engine, source_text), False to defer, or True for terminal no-op."""
    if doc.processing_state_user == Document.ProcessingState.PROCESSING:
        if _processing_lease_is_fresh(doc, now=now):
            logger.info(
                "Hebrew translation retry deferred for fresh PROCESSING lease document_id=%s",
                document_id,
            )
            return False

        logger.warning(
            "Hebrew translation retry reclaiming stale PROCESSING lease document_id=%s",
            document_id,
        )
        try:
            _recompute_processing_state_from_source_engine(doc)
        except HebrewTranslationRetryError as exc:
            doc.processing_state_user = Document.ProcessingState.PARTIAL
            doc.save(update_fields=["processing_state_user", "updated_at"])
            logger.warning(
                "Hebrew translation retry stale PROCESSING restore rejected document_id=%s: %s",
                document_id,
                exc,
            )
            return True

        try:
            source_row = validate_document_for_hebrew_translation_retry(doc)
        except HebrewTranslationRetryError as exc:
            logger.warning(
                "Hebrew translation retry stale PROCESSING no longer eligible document_id=%s: %s",
                document_id,
                exc,
            )
            return True
    else:
        try:
            source_row = validate_document_for_hebrew_translation_retry(doc)
        except HebrewTranslationRetryError as exc:
            logger.warning(
                "Hebrew translation retry claim rejected document_id=%s: %s",
                document_id,
                exc,
            )
            return True

    engine = source_row.engine
    source_text = source_row.text or ""
    doc.processing_state_user = Document.ProcessingState.PROCESSING
    doc.save(update_fields=["processing_state_user", "updated_at"])
    return engine, source_text


def is_hebrew_translation_retry_ui_eligible(doc: Document) -> bool:
    try:
        validate_document_for_hebrew_translation_retry(doc)
    except HebrewTranslationRetryError:
        return False
    return True


def enqueue_hebrew_translation_retry(document_id: int) -> None:
    """Validate enqueue eligibility and send a translation-only worker message."""
    doc = Document.objects.select_related("archive_item").get(pk=document_id)
    validate_document_for_hebrew_translation_retry(doc)
    send_process_document_message(
        document_id,
        operation=RETRY_HEBREW_TRANSLATION_OPERATION,
    )


def run_hebrew_translation_retry(
    document_id: int,
    *,
    worker_env: WorkerEnvConfig,
) -> bool:
    """Worker entrypoint for translation-only retry. Returns True when the SQS message may be acked."""
    now = timezone.now()
    try:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=document_id)
            claim = _claim_translation_retry(doc, document_id=document_id, now=now)
            if claim is False:
                return False
            if claim is True:
                return True
            engine, source_text = claim
    except Document.DoesNotExist:
        return True

    try:
        translation = translate_text_to_hebrew_with_gemini(
            source_text,
            doc.language,
            model_name=DEFAULT_GEMINI_MODEL,
            min_text_length=worker_env.min_text_length,
            temperature=worker_env.gemini_temperature,
            top_k=worker_env.gemini_top_k,
            top_p=worker_env.gemini_top_p,
            max_output_tokens=worker_env.gemini_max_output_tokens,
        )
        translation_error: Exception | None = None
    except Exception as exc:
        translation = None
        translation_error = exc
        logger.exception(
            "Hebrew translation retry Gemini call failed document_id=%s",
            document_id,
        )

    try:
        with transaction.atomic():
            doc = Document.objects.select_for_update().get(pk=document_id)
            validate_document_for_hebrew_translation_retry_persistence(
                doc,
                expected_engine=engine,
                expected_source_text=source_text,
            )

            if translation_error is not None:
                persist_hebrew_translation_result(
                    doc,
                    engine,
                    error=translation_error,
                    min_text_length=worker_env.min_text_length,
                )
            else:
                persist_hebrew_translation_result(
                    doc,
                    engine,
                    translation=translation,
                    min_text_length=worker_env.min_text_length,
                )

            update_document_processing_state_for_engine(doc, engine)
            doc.save(update_fields=["processing_state_user", "updated_at"])
    except HebrewTranslationRetryError as exc:
        _restore_processing_state_after_retry_abort(document_id, engine)
        logger.warning(
            "Hebrew translation retry persistence rejected document_id=%s: %s",
            document_id,
            exc,
        )
        return True
    except Exception:
        _restore_processing_state_after_retry_abort(document_id, engine)
        logger.exception(
            "Hebrew translation retry persistence failed document_id=%s",
            document_id,
        )
        return False

    return True
