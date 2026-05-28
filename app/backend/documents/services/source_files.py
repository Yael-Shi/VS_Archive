from __future__ import annotations

from typing import Optional

from documents.models import Document, DocumentSourceFile

MULTI_IMAGE_MIN_FILES = 2
MULTI_IMAGE_MAX_FILES = 20


def is_multi_image_document(document: Document) -> bool:
    count = document.expected_source_file_count
    return count is not None and count >= MULTI_IMAGE_MIN_FILES


def sync_primary_document_source_file(document: Document) -> DocumentSourceFile:
    """
    Upsert the primary (order_index=0) source file row from Document file metadata.

    Raises ``ValueError`` when ``document.file_s3_key`` is missing. ``upload_complete``
    rejects that case before calling this helper on the success path.
    """
    if not document.file_s3_key:
        raise ValueError("document.file_s3_key is required to sync DocumentSourceFile")

    source, _created = DocumentSourceFile.objects.update_or_create(
        document=document,
        order_index=0,
        defaults={
            "file_s3_key": document.file_s3_key,
            "file_original_name": document.file_original_name,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
            "upload_status": DocumentSourceFile.UploadStatus.UPLOADED,
            "upload_error": None,
        },
    )
    return source


def mirror_primary_document_from_source_file(
    document: Document,
    source_file: DocumentSourceFile,
) -> None:
    document.file_s3_key = source_file.file_s3_key
    document.file_original_name = source_file.file_original_name
    document.mime_type = source_file.mime_type
    document.size_bytes = source_file.size_bytes


def get_source_file_for_order(
    document: Document,
    order_index: int,
) -> Optional[DocumentSourceFile]:
    return DocumentSourceFile.objects.filter(
        document=document,
        order_index=order_index,
    ).first()


def all_expected_source_files_uploaded(document: Document) -> tuple[bool, str]:
    """
    Return (ok, error_message). ``error_message`` is empty when ``ok`` is True.
    """
    expected = document.expected_source_file_count
    if expected is None or expected < MULTI_IMAGE_MIN_FILES:
        return False, "not a multi-image document"

    sources = {
        row.order_index: row
        for row in DocumentSourceFile.objects.filter(document=document)
    }

    for order_index in range(expected):
        source = sources.get(order_index)
        if source is None:
            return False, f"source file missing for order_index={order_index}"
        if source.upload_status == DocumentSourceFile.UploadStatus.PENDING:
            return False, f"source file still pending for order_index={order_index}"
        if source.upload_status == DocumentSourceFile.UploadStatus.FAILED:
            return False, f"source file failed for order_index={order_index}"
        if source.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            return False, f"source file not uploaded for order_index={order_index}"

    return True, ""
