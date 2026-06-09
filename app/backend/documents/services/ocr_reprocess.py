"""Staff/admin OCR reprocess planning and enqueue for failed OCR-backed documents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.db import transaction

from documents.models import ArchiveItem, Document, DocumentTextResult, TranskribusRun
from documents.services import transkribus_run_persistence as trp
from documents.services.sqs import send_process_document_message


class OcrReprocessError(RuntimeError):
    """Raised when a document cannot be reprocessed."""


class OcrRetryMode(str, Enum):
    NORMAL_REENQUEUE = "normal_reenqueue"
    TRANSKRIBUS_RECOGNITION_ONLY = "transkribus_recognition_only"


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


def validate_document_for_ocr_reprocess(doc: Document) -> None:
    if doc.upload_status != Document.UploadStatus.UPLOADED:
        raise OcrReprocessError(
            f"Document id={doc.id} upload_status={doc.upload_status!r} "
            "must be UPLOADED."
        )

    if doc.processing_state_user != Document.ProcessingState.FAILED:
        raise OcrReprocessError(
            f"Document id={doc.id} processing_state_user="
            f"{doc.processing_state_user!r} must be FAILED."
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
    return assessment
