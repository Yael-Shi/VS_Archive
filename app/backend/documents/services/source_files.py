from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from documents.models import Document, DocumentSourceFile
from documents.s3 import create_presigned_get
from documents.services.page_extraction import PageImage, source_file_bytes_to_page
from documents.services.upload_validation import (
    validate_allowed_image_mime,
    validate_image_upload_metadata,
)

logger = logging.getLogger(__name__)

MULTI_IMAGE_MIN_FILES = 2
MULTI_IMAGE_MAX_FILES = 35
INCREMENTAL_MIN_PARTS = 1


class MultiImageSourceFilesError(ValueError):
    """Raised when a document's multi-image source files are not valid for processing."""


def _validate_source_file_image_metadata(
    source: DocumentSourceFile,
    order_index: int,
) -> None:
    original_name = (source.file_original_name or "").strip()
    if original_name:
        metadata_err = validate_image_upload_metadata(
            mime_type=source.mime_type or "",
            original_name=original_name,
        )
    else:
        metadata_err = validate_allowed_image_mime(source.mime_type or "")

    if metadata_err:
        raise MultiImageSourceFilesError(
            f"source file metadata invalid for order_index={order_index}: "
            f"{metadata_err}"
        )


def is_multi_image_document(document: Document) -> bool:
    count = document.expected_source_file_count
    return count is not None and count >= MULTI_IMAGE_MIN_FILES


def is_incremental_multi_image_draft(document: Document) -> bool:
    """
    In-progress incremental multi-image upload: IMAGE doc with no fixed part count yet.

    Distinguished from single-file uploads in progress by an empty ``file_s3_key``.
    """
    if document.doc_type != Document.DocType.IMAGE:
        return False
    if document.expected_source_file_count is not None:
        return False
    if document.upload_status == Document.UploadStatus.UPLOADED:
        return False
    if (document.file_s3_key or "").strip():
        return False
    return True


def uses_multi_image_part_endpoints(document: Document) -> bool:
    """Whether this document uses part-complete / finalize (batch or incremental)."""
    return is_multi_image_document(document) or is_incremental_multi_image_draft(
        document
    )


def ordered_source_files_for_document(document: Document) -> List[DocumentSourceFile]:
    return list(document.source_files.order_by("order_index"))


def validate_incremental_finalize_ready(
    document: Document,
) -> tuple[bool, str, int]:
    """
    Validate an incremental draft is ready to finalize.

    Returns (ok, error_message, uploaded_count). On success, ``uploaded_count`` is
    the contiguous part count (>= ``INCREMENTAL_MIN_PARTS``).
    """
    if not is_incremental_multi_image_draft(document):
        return False, "not an incremental multi-image draft", 0

    sources = ordered_source_files_for_document(document)
    if len(sources) < INCREMENTAL_MIN_PARTS:
        return (
            False,
            f"incremental finalize requires at least {INCREMENTAL_MIN_PARTS} "
            "uploaded image part",
            len(sources),
        )

    for order_index, source in enumerate(sources):
        if source.order_index != order_index:
            return False, "source file order_index values are not contiguous", 0
        if source.upload_status == DocumentSourceFile.UploadStatus.PENDING:
            return False, f"source file still pending for order_index={order_index}", 0
        if source.upload_status == DocumentSourceFile.UploadStatus.FAILED:
            return False, f"source file failed for order_index={order_index}", 0
        if source.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            return False, f"source file not uploaded for order_index={order_index}", 0
        if not (source.file_s3_key or "").strip():
            return (
                False,
                f"source file has empty file_s3_key for order_index={order_index}",
                0,
            )
        _validate_source_file_image_metadata(source, order_index)

    return True, "", len(sources)


def next_incremental_part_order_index(document: Document) -> int:
    """Return the next zero-based order_index for an incremental draft."""
    last = document.source_files.order_by("-order_index").first()
    if last is None:
        return 0
    return last.order_index + 1


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


def _is_in_flight_display_only_extra(
    source: DocumentSourceFile,
    *,
    expected: int,
) -> bool:
    """PENDING/FAILED display-only rows beyond the committed physical count."""
    if source.order_index < expected:
        return False
    if source.include_in_ocr:
        return False
    return source.upload_status in (
        DocumentSourceFile.UploadStatus.PENDING,
        DocumentSourceFile.UploadStatus.FAILED,
    )


def ocr_page_source_identity(
    document: Document,
    source: DocumentSourceFile,
    *,
    ocr_included_count: int,
) -> str:
    """
    PageImage.source_identity for one OCR-included source file.

    Legacy single-image worker identity is ``Document.file_s3_key``. After a
    display-only append, that document becomes multi-image with one OCR page;
    keep the same identity so Gemini/Arabic attempt fingerprints do not change.
    Multi-image documents with two or more OCR-included files keep
    ``{source.id}:{source.file_s3_key}``.
    """
    if (
        ocr_included_count == 1
        and source.order_index == 0
        and (source.file_s3_key or "").strip()
        and source.file_s3_key == (document.file_s3_key or "")
    ):
        return document.file_s3_key
    return f"{source.id}:{source.file_s3_key}"


def page_images_from_ocr_source_bytes(
    document: Document,
    loaded_sources: Sequence[tuple[DocumentSourceFile, bytes]],
) -> List[PageImage]:
    """
    Convert OCR-included source files (already downloaded) to contiguous PageImages.

    ``loaded_sources`` must already be the ``include_in_ocr=True`` set in
    physical ``order_index`` order. Display-only files must not be included.
    ``page_index`` is 1..K in that filtered order.
    """
    ocr_count = len(loaded_sources)
    pages: List[PageImage] = []
    for ocr_order_index, (source, file_bytes) in enumerate(loaded_sources):
        source_content_fingerprint = hashlib.sha256(file_bytes).hexdigest()
        pages.append(
            source_file_bytes_to_page(
                order_index=ocr_order_index,
                file_bytes=file_bytes,
                mime_type=source.mime_type,
                source_identity=ocr_page_source_identity(
                    document,
                    source,
                    ocr_included_count=ocr_count,
                ),
                source_content_fingerprint=source_content_fingerprint,
            )
        )
    return pages


def get_ordered_source_files_for_processing(
    document: Document,
) -> List[DocumentSourceFile]:
    """
    Validate the physical source set and return OCR-included files only.

    Physical/display set: contiguous ``order_index`` 0..N-1 matching
    ``expected_source_file_count``. OCR set: those rows with
    ``include_in_ocr=True``, still in physical order. Worker/adapters must use
    this list (not the full physical set).

    In-flight display-only extras (PENDING/FAILED, ``include_in_ocr=False``,
    ``order_index >= N``) are ignored so an unfinished add cannot fail OCR.

    Raises ``MultiImageSourceFilesError`` (no OCR/HTR dispatch) when:

    - ``expected_source_file_count`` is missing or ``< MULTI_IMAGE_MIN_FILES``
    - a committed physical row is missing / not UPLOADED / empty key / invalid MIME
    - an unexpected extra row is not an in-flight display-only add
    - the OCR-included set is empty
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
    unexpected_extras = [
        idx
        for idx in extra_indexes
        if not _is_in_flight_display_only_extra(sources[idx], expected=expected)
    ]
    if unexpected_extras:
        raise MultiImageSourceFilesError(
            f"unexpected source file order_index values {unexpected_extras} "
            f"(valid range is 0..{expected - 1})"
        )

    ordered_physical: List[DocumentSourceFile] = []
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
        _validate_source_file_image_metadata(source, order_index)
        ordered_physical.append(source)

    ocr_sources = [source for source in ordered_physical if source.include_in_ocr]
    if not ocr_sources:
        raise MultiImageSourceFilesError(
            f"document_id={document.id} has no OCR-included source files"
        )
    return ocr_sources


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
                "include_in_ocr": source.include_in_ocr,
            }
        )

    return SourcePreview(items=items, non_uploaded_count=non_uploaded_count)
