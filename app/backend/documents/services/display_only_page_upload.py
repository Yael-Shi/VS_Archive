"""Staff post-finalize display-only source page upload (no OCR enqueue)."""

from __future__ import annotations

from documents.models import (
    ArchiveItem,
    Document,
    DocumentSourceFile,
    ProcessDocumentRequest,
)
from documents.s3 import build_document_source_file_s3_key
from documents.services.source_files import (
    MULTI_IMAGE_MAX_FILES,
    sync_primary_document_source_file,
)

ACTIVE_PROCESS_DOCUMENT_REQUEST_STATUSES = frozenset(
    {
        ProcessDocumentRequest.Status.QUEUED,
        ProcessDocumentRequest.Status.RUNNING,
        ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        ProcessDocumentRequest.Status.ENQUEUE_FAILED,
    }
)


class DisplayOnlyPageUploadErrorCode:
    NOT_FOUND = "NOT_FOUND"
    NOT_OCR_IMAGE = "NOT_OCR_IMAGE"
    NOT_UPLOADED = "NOT_UPLOADED"
    DOCUMENT_BUSY = "DOCUMENT_BUSY"
    ACTIVE_REQUEST = "ACTIVE_REQUEST"
    SOURCE_FILE_LIMIT = "SOURCE_FILE_LIMIT"
    INVALID_SOURCE = "INVALID_SOURCE"


class DisplayOnlyPageUploadError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        public_message: str,
        http_status: int,
    ) -> None:
        self.code = code
        self.public_message = public_message
        self.http_status = http_status
        super().__init__(public_message)


def committed_physical_source_count(document: Document) -> int:
    if document.expected_source_file_count is not None:
        return document.expected_source_file_count
    if (document.file_s3_key or "").strip():
        return 1
    return 0


def validate_document_for_display_only_page_add(doc: Document) -> None:
    if doc.doc_type != Document.DocType.IMAGE:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.NOT_OCR_IMAGE,
            public_message="Display-only pages can only be added to IMAGE documents.",
            http_status=400,
        )
    if not doc.archive_item_id:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.NOT_OCR_IMAGE,
            public_message="Display-only pages require an OCR document archive item.",
            http_status=400,
        )
    try:
        item_type = doc.archive_item.item_type
    except ArchiveItem.DoesNotExist as exc:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.NOT_OCR_IMAGE,
            public_message="Display-only pages require an OCR document archive item.",
            http_status=400,
        ) from exc
    if item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.NOT_OCR_IMAGE,
            public_message="Display-only pages can only be added to OCR documents.",
            http_status=400,
        )
    if doc.upload_status != Document.UploadStatus.UPLOADED:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.NOT_UPLOADED,
            public_message="Display-only pages can only be added after upload is complete.",
            http_status=400,
        )
    if doc.processing_state_user in (
        Document.ProcessingState.PROCESSING,
        Document.ProcessingState.RECOVERY_REQUIRED,
    ):
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.DOCUMENT_BUSY,
            public_message=(
                "Display-only pages cannot be added while document processing "
                "is active or requires recovery."
            ),
            http_status=409,
        )
    if ProcessDocumentRequest.objects.filter(
        document_id=doc.id,
        status__in=ACTIVE_PROCESS_DOCUMENT_REQUEST_STATUSES,
    ).exists():
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.ACTIVE_REQUEST,
            public_message=(
                "Display-only pages cannot be added while a processing request "
                "is active for this document."
            ),
            http_status=409,
        )
    if not (doc.file_s3_key or "").strip() and not doc.source_files.exists():
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.INVALID_SOURCE,
            public_message="Document has no original source file to append to.",
            http_status=400,
        )
    committed = committed_physical_source_count(doc)
    if committed >= MULTI_IMAGE_MAX_FILES:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.SOURCE_FILE_LIMIT,
            public_message=(
                f"documents may contain at most {MULTI_IMAGE_MAX_FILES} image parts"
            ),
            http_status=400,
        )


def is_display_only_page_add_eligible(doc: Document) -> bool:
    try:
        validate_document_for_display_only_page_add(doc)
    except DisplayOnlyPageUploadError:
        return False
    return True


def _ensure_primary_source_file(document: Document) -> None:
    if not (document.file_s3_key or "").strip():
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.INVALID_SOURCE,
            public_message="Document has no original source file to append to.",
            http_status=400,
        )
    primary = sync_primary_document_source_file(document)
    if not primary.include_in_ocr:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.INVALID_SOURCE,
            public_message="The original source file must remain included in OCR.",
            http_status=400,
        )


def prepare_display_only_page_upload(
    *,
    document: Document,
    original_name: str,
    mime_type: str,
    size_bytes: int | None,
) -> DocumentSourceFile:
    """
    Lock the document, ensure a primary OCR source row, and create/reuse a
    PENDING display-only ``DocumentSourceFile`` at the next order_index.

    Does not bump ``expected_source_file_count`` until successful complete.
    Does not enqueue OCR.
    """
    validate_document_for_display_only_page_add(document)
    _ensure_primary_source_file(document)
    document.refresh_from_db()
    committed = committed_physical_source_count(document)
    next_index = committed
    if next_index >= MULTI_IMAGE_MAX_FILES:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.SOURCE_FILE_LIMIT,
            public_message=(
                f"documents may contain at most {MULTI_IMAGE_MAX_FILES} image parts"
            ),
            http_status=400,
        )

    key = build_document_source_file_s3_key(
        document_id=document.id,
        order_index=next_index,
        mime_type=mime_type,
    )
    existing = DocumentSourceFile.objects.filter(
        document=document,
        order_index=next_index,
    ).first()
    if existing is not None:
        if existing.include_in_ocr:
            raise DisplayOnlyPageUploadError(
                code=DisplayOnlyPageUploadErrorCode.INVALID_SOURCE,
                public_message="Next source-file slot is already an OCR page.",
                http_status=409,
            )
        existing.file_s3_key = key
        existing.file_original_name = original_name
        existing.mime_type = mime_type
        existing.size_bytes = size_bytes
        existing.upload_status = DocumentSourceFile.UploadStatus.PENDING
        existing.upload_error = None
        existing.include_in_ocr = False
        existing.save(
            update_fields=[
                "file_s3_key",
                "file_original_name",
                "mime_type",
                "size_bytes",
                "upload_status",
                "upload_error",
                "include_in_ocr",
                "updated_at",
            ]
        )
        return existing

    return DocumentSourceFile.objects.create(
        document=document,
        order_index=next_index,
        file_s3_key=key,
        file_original_name=original_name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        upload_status=DocumentSourceFile.UploadStatus.PENDING,
        include_in_ocr=False,
    )


def complete_display_only_page_upload(
    *,
    document: Document,
    source_file: DocumentSourceFile,
    success: bool,
    upload_error: str | None = None,
    mime_type: str | None = None,
    size_bytes: int | None = None,
) -> DocumentSourceFile:
    """
    Mark a display-only part complete. Never changes Document processing/upload
    status or enqueues OCR. On success, commits expected_source_file_count.
    """
    if source_file.include_in_ocr:
        raise DisplayOnlyPageUploadError(
            code=DisplayOnlyPageUploadErrorCode.INVALID_SOURCE,
            public_message="This source file is not a display-only page.",
            http_status=400,
        )

    if not success:
        err = (upload_error or "upload failed").strip() or "upload failed"
        source_file.upload_status = DocumentSourceFile.UploadStatus.FAILED
        source_file.upload_error = err
        source_file.save(update_fields=["upload_status", "upload_error", "updated_at"])
        return source_file

    validate_document_for_display_only_page_add(document)

    source_file.upload_status = DocumentSourceFile.UploadStatus.UPLOADED
    source_file.upload_error = None
    if mime_type:
        source_file.mime_type = mime_type
    if size_bytes is not None:
        source_file.size_bytes = size_bytes
    source_file.include_in_ocr = False
    source_file.save(
        update_fields=[
            "upload_status",
            "upload_error",
            "mime_type",
            "size_bytes",
            "include_in_ocr",
            "updated_at",
        ]
    )
    document.expected_source_file_count = source_file.order_index + 1
    document.save(update_fields=["expected_source_file_count", "updated_at"])
    return source_file
