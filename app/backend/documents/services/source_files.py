from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from documents.models import Document, DocumentSourceFile
from documents.s3 import create_presigned_get

logger = logging.getLogger(__name__)

MULTI_IMAGE_MIN_FILES = 2
MULTI_IMAGE_MAX_FILES = 20


class MultiImageSourceFilesError(ValueError):
    """Raised when a document's multi-image source files are not valid for processing."""


def _is_image_mime_type(mime_type: Optional[str]) -> bool:
    return (mime_type or "").strip().lower().startswith("image/")


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


def get_ordered_source_files_for_processing(
    document: Document,
) -> List[DocumentSourceFile]:
    """
    Validate and return this document's source files ordered by ``order_index`` (0..N-1).

    Used by the worker before building the multi-image ``PageImage`` list. Raises
    ``MultiImageSourceFilesError`` (no OCR/HTR dispatch) when any of the following fail:

    - ``expected_source_file_count`` is missing or ``< MULTI_IMAGE_MIN_FILES``
    - a ``DocumentSourceFile`` is missing for any ``order_index`` in ``0..N-1`` (contiguous)
    - any row is not ``upload_status=UPLOADED``
    - any row has an empty ``file_s3_key``
    - any row is not an ``image/*`` MIME type (images only in V1)
    """
    expected = document.expected_source_file_count
    if expected is None or expected < MULTI_IMAGE_MIN_FILES:
        raise MultiImageSourceFilesError(
            f"document_id={document.id} is not a multi-image document "
            f"(expected_source_file_count={expected!r})"
        )

    sources = {
        row.order_index: row
        for row in DocumentSourceFile.objects.filter(document=document)
    }

    extra_indexes = sorted(idx for idx in sources if idx < 0 or idx >= expected)
    if extra_indexes:
        raise MultiImageSourceFilesError(
            f"unexpected source file order_index values {extra_indexes} "
            f"(valid range is 0..{expected - 1})"
        )

    ordered: List[DocumentSourceFile] = []
    for order_index in range(expected):
        source = sources.get(order_index)
        if source is None:
            raise MultiImageSourceFilesError(
                f"source file missing for order_index={order_index}"
            )
        if source.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            raise MultiImageSourceFilesError(
                f"source file not uploaded for order_index={order_index} "
                f"(upload_status={source.upload_status})"
            )
        if not (source.file_s3_key or "").strip():
            raise MultiImageSourceFilesError(
                f"source file has empty file_s3_key for order_index={order_index}"
            )
        if not _is_image_mime_type(source.mime_type):
            raise MultiImageSourceFilesError(
                f"source file mime_type must be image/* for order_index={order_index} "
                f"(got {source.mime_type!r})"
            )
        ordered.append(source)

    return ordered


@dataclass
class SourcePreview:
    """Read-only source-preview context for the document/review detail UI."""

    items: List[dict] = field(default_factory=list)
    non_uploaded_count: int = 0


def build_source_preview(
    document: Document,
    bucket: str,
    expires_in: int = 3600,
) -> SourcePreview:
    """
    Build read-only, ordered source-image preview items for a multi-image document.

    Returns an empty ``SourcePreview`` for non-multi-image documents so callers keep
    their existing single-file ``content_url`` behavior unchanged. Only
    ``upload_status=UPLOADED`` source files get a preview entry; any other rows are
    counted in ``non_uploaded_count`` so the UI can show one muted note instead of
    rendering broken placeholders.

    Presigned GET generation is guarded per item: a failure for one file yields
    ``url=None`` (a muted placeholder in the template) rather than raising.
    """
    if not is_multi_image_document(document):
        return SourcePreview()

    items: List[dict] = []
    non_uploaded_count = 0

    for source in document.source_files.all():
        if source.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            non_uploaded_count += 1
            continue

        url: Optional[str] = None
        if bucket and (source.file_s3_key or "").strip():
            try:
                url = create_presigned_get(
                    bucket=bucket,
                    key=source.file_s3_key,
                    expires_in=expires_in,
                )
            except Exception:
                logger.exception(
                    "source preview presigned GET failed",
                    extra={
                        "document_id": document.id,
                        "order_index": source.order_index,
                    },
                )
                url = None

        items.append(
            {
                "display_number": source.order_index + 1,
                "order_index": source.order_index,
                "url": url,
                "mime_type": source.mime_type,
                "original_name": source.file_original_name,
                "upload_status": source.upload_status,
            }
        )

    return SourcePreview(items=items, non_uploaded_count=non_uploaded_count)
