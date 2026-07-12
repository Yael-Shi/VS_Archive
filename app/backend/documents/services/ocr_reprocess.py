"""Staff/admin OCR reprocess planning and enqueue for failed OCR-backed documents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.db import transaction

from documents.models import ArchiveItem, Document, DocumentTextResult, TranskribusRun
from documents.services import transkribus_run_persistence as trp
from documents.services.ocr_routing import select_ocr_route
from documents.services.sqs import send_process_document_message


class OcrReprocessError(RuntimeError):
    """Raised when a document cannot be reprocessed."""


class OcrRetryMode(str, Enum):
    NORMAL_REENQUEUE = "normal_reenqueue"
    TRANSKRIBUS_RECOGNITION_ONLY = "transkribus_recognition_only"


OCR_RETRY_MODE_PAYLOAD_KEY = "ocr_retry_mode"
SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY = "source_transkribus_run_id"


@dataclass(frozen=True)
class OcrReprocessAssessment:
    document_id: int
    retry_mode: OcrRetryMode
    source_transkribus_run_id: int | None = None


def _get_document(document_id: int) -> Document:
    try:
        return Document.objects.select_related("archive_item").get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise OcrReprocessError(f"Document id={document_id} does not exist.") from exc


def _document_routes_to_gemini(doc: Document) -> bool:
    try:
        route = select_ocr_route(doc.language, doc.text_input_type)
    except ValueError:
        return False
    return route.engine_key == DocumentTextResult.OcrEngineKey.GEMINI


def _source_text_row_is_usable(row: DocumentTextResult) -> bool:
    if row.status not in (
        DocumentTextResult.Status.SUCCEEDED,
        DocumentTextResult.Status.NEEDS_REVIEW,
    ):
        return False
    return bool((row.text or "").strip())


def _has_usable_source_text(doc: Document) -> bool:
    rows = DocumentTextResult.objects.filter(
        document_id=doc.id,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
    )
    return any(_source_text_row_is_usable(row) for row in rows)


def _has_failed_source_ocr(doc: Document) -> bool:
    return DocumentTextResult.objects.filter(
        document_id=doc.id,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        status=DocumentTextResult.Status.FAILED,
        error_code="OCR_FAILED",
    ).exists()


def _is_gemini_recoverable_partial_ocr_failure(doc: Document) -> bool:
    """Gemini OCR failed with no usable source text; missing translation keeps PARTIAL."""
    if doc.processing_state_user != Document.ProcessingState.PARTIAL:
        return False
    if not _document_routes_to_gemini(doc):
        return False
    if _has_usable_source_text(doc):
        return False
    if not _has_failed_source_ocr(doc):
        return False
    return True


def _processing_state_allows_ocr_reprocess(doc: Document) -> bool:
    if doc.processing_state_user == Document.ProcessingState.FAILED:
        return True
    return _is_gemini_recoverable_partial_ocr_failure(doc)


def validate_document_for_ocr_reprocess(doc: Document) -> None:
    if doc.upload_status != Document.UploadStatus.UPLOADED:
        raise OcrReprocessError(
            f"Document id={doc.id} upload_status={doc.upload_status!r} "
            "must be UPLOADED."
        )

    if not _processing_state_allows_ocr_reprocess(doc):
        raise OcrReprocessError(
            f"Document id={doc.id} processing_state_user="
            f"{doc.processing_state_user!r} is not eligible for OCR reprocess."
        )

    if not doc.archive_item_id:
        raise OcrReprocessError(
            f"Document id={doc.id} has no linked ArchiveItem; "
            "only OCR_DOCUMENT archive items are supported."
        )

    try:
        item_type = doc.archive_item.item_type
    except ArchiveItem.DoesNotExist as exc:
        raise OcrReprocessError(
            f"Document id={doc.id} is linked to missing ArchiveItem "
            f"id={doc.archive_item_id}; only OCR_DOCUMENT is supported."
        ) from exc

    if item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise OcrReprocessError(
            f"Document id={doc.id} is linked to ArchiveItem item_type="
            f"{item_type!r}; only OCR_DOCUMENT is supported."
        )

    if DocumentTextResult.objects.filter(
        document_id=doc.id,
        verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
    ).exists():
        raise OcrReprocessError(
            f"Document id={doc.id} has VERIFIED DocumentTextResult row(s); "
            "reprocess is blocked."
        )


def is_ocr_reprocess_ui_eligible(doc: Document) -> bool:
    try:
        validate_document_for_ocr_reprocess(doc)
    except OcrReprocessError:
        return False
    return True


def _is_hebrew_handwritten(doc: Document) -> bool:
    return (
        doc.language == Document.Language.HEBREW
        and doc.text_input_type == Document.TextInputType.HANDWRITTEN
    )


def _transkribus_reprocess_config_present(collection_id: str, model_id: str) -> bool:
    return bool(str(collection_id).strip()) and bool(str(model_id).strip())


def _validate_transkribus_reprocess_config(
    doc: Document,
    *,
    collection_id: str,
    model_id: str,
) -> None:
    if not _is_hebrew_handwritten(doc):
        return
    if _transkribus_reprocess_config_present(collection_id, model_id):
        return
    raise OcrReprocessError(
        f"Document id={doc.id} is Hebrew handwritten and requires "
        "TRANSKRIBUS_COLLECTION_ID and TRANSKRIBUS_MODEL_ID for OCR reprocess "
        "assessment, but one or both are missing in this environment. "
        "Cannot safely classify retry mode without them."
    )


def _has_succeeded_transkribus_upload_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> bool:
    col = str(collection_id).strip()
    mid = str(model_id).strip()
    return TranskribusRun.objects.filter(
        document_id=document_id,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id=col,
        model_id=mid,
        status=TranskribusRun.Status.SUCCEEDED,
    ).exists()


def classify_ocr_retry_mode(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> OcrReprocessAssessment:
    reusable = trp.find_reusable_upload_run(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    )
    if reusable is not None and not _has_succeeded_transkribus_upload_run(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    ):
        return OcrReprocessAssessment(
            document_id=document_id,
            retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
            source_transkribus_run_id=reusable.id,
        )
    return OcrReprocessAssessment(
        document_id=document_id,
        retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
        source_transkribus_run_id=None,
    )


def assess_ocr_reprocess(
    document_id: int,
    *,
    collection_id: str,
    model_id: str,
) -> OcrReprocessAssessment:
    doc = _get_document(document_id)
    validate_document_for_ocr_reprocess(doc)
    _validate_transkribus_reprocess_config(
        doc,
        collection_id=collection_id,
        model_id=model_id,
    )
    return classify_ocr_retry_mode(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    )


def apply_ocr_reprocess(
    document_id: int,
    *,
    collection_id: str,
    model_id: str,
) -> OcrReprocessAssessment:
    assessment = assess_ocr_reprocess(
        document_id,
        collection_id=collection_id,
        model_id=model_id,
    )

    with transaction.atomic():
        doc = Document.objects.select_for_update().get(pk=document_id)
        validate_document_for_ocr_reprocess(doc)
        doc.processing_state_user = Document.ProcessingState.PROCESSING
        if doc.upload_error:
            doc.upload_error = None
        doc.save(update_fields=["processing_state_user", "upload_error", "updated_at"])

        try:
            if assessment.retry_mode == OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY:
                if assessment.source_transkribus_run_id is None:
                    raise OcrReprocessError(
                        f"Document id={document_id} recognition-only reprocess requires "
                        "source_transkribus_run_id but none was classified."
                    )
                send_process_document_message(
                    document_id,
                    ocr_retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY.value,
                    source_transkribus_run_id=assessment.source_transkribus_run_id,
                )
            else:
                send_process_document_message(document_id)
        except OcrReprocessError:
            raise
        except Exception as exc:
            raise OcrReprocessError(
                f"Failed to enqueue document id={document_id} for OCR reprocess: {exc}"
            ) from exc
    return assessment
