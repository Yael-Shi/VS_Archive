"""Generate OCR document thumbnails from the first uploaded source image."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from documents.models import Document
from documents.s3 import (
    build_document_thumbnail_s3_key,
    get_object_bytes,
    put_object_bytes,
)
from documents.services.image_thumbnail import (
    THUMBNAIL_JPEG_MIME,
    generate_image_thumbnail_bytes,
)
from documents.services.source_files import get_source_file_for_order

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentThumbnailResult:
    first_page_width: int
    first_page_height: int
    thumbnail_file_key: str
    thumbnail_mime_type: str
    thumbnail_size_bytes: int


@dataclass(frozen=True)
class _PreservedDocumentThumbnailFields:
    first_page_width: int | None
    first_page_height: int | None
    thumbnail_file_key: str
    thumbnail_mime_type: str
    thumbnail_size_bytes: int | None


def should_generate_document_thumbnail(
    document: Document,
    *,
    already_uploaded: bool,
) -> bool:
    if already_uploaded:
        return False
    if document.doc_type != Document.DocType.IMAGE:
        return False
    if (document.thumbnail_file_key or "").strip():
        return False
    return True


def schedule_document_thumbnail_after_upload(
    document: Document,
    *,
    bucket: str,
    already_uploaded: bool,
) -> None:
    """
    Schedule best-effort thumbnail generation after upload finalize/complete commits.

    Thumbnail work is deferred with ``transaction.on_commit`` so S3/Pillow work runs
    only after the upload finalize database transaction commits successfully.
    """
    if not should_generate_document_thumbnail(
        document, already_uploaded=already_uploaded
    ):
        return

    document_id = document.pk

    def _generate() -> None:
        try:
            doc = Document.objects.get(pk=document_id)
        except Document.DoesNotExist:
            logger.warning(
                "document thumbnail skipped: document missing after commit",
                extra={"document_id": document_id},
            )
            return
        generate_and_persist_document_thumbnail(doc, bucket=bucket)

    transaction.on_commit(_generate)


def generate_and_persist_document_thumbnail(
    document: Document,
    *,
    bucket: str,
) -> DocumentThumbnailResult | None:
    """
    Download the first source image, generate a JPEG thumbnail, upload to S3,
    and persist preview metadata on ``document``.

    Returns a result on success. On failure logs the exception and returns None
    without raising (callers must keep upload_status=UPLOADED).

    If S3 upload succeeds but DB metadata persistence fails, the deterministic
    thumbnail object may remain at ``thumbnail_file_key``. That orphan is safe
    for future retry/backfill because the key is idempotent per Document id.
    """
    primary = get_source_file_for_order(document, 0)
    if primary is None:
        logger.warning(
            "document thumbnail skipped: primary source file missing",
            extra={"document_id": document.pk},
        )
        return None

    source_key = (primary.file_s3_key or "").strip()
    if not source_key:
        logger.warning(
            "document thumbnail skipped: missing primary source file key",
            extra={"document_id": document.pk},
        )
        return None

    thumbnail_key = build_document_thumbnail_s3_key(document.pk)
    log_extra = {
        "document_id": document.pk,
        "source_file_key": source_key,
        "thumbnail_file_key": thumbnail_key,
    }

    # Capture restore state before any S3 upload or in-memory mutation.
    preserved = _PreservedDocumentThumbnailFields(
        first_page_width=document.first_page_width,
        first_page_height=document.first_page_height,
        thumbnail_file_key=document.thumbnail_file_key,
        thumbnail_mime_type=document.thumbnail_mime_type,
        thumbnail_size_bytes=document.thumbnail_size_bytes,
    )

    try:
        image_bytes, _content_type = get_object_bytes(bucket, source_key)
        jpeg_bytes, width, height = generate_image_thumbnail_bytes(image_bytes)
        thumbnail_size = put_object_bytes(
            bucket=bucket,
            key=thumbnail_key,
            body=jpeg_bytes,
            content_type=THUMBNAIL_JPEG_MIME,
        )
    except Exception:
        logger.exception("document thumbnail generation failed", extra=log_extra)
        return None

    try:
        document.first_page_width = width
        document.first_page_height = height
        document.thumbnail_file_key = thumbnail_key
        document.thumbnail_mime_type = THUMBNAIL_JPEG_MIME
        document.thumbnail_size_bytes = thumbnail_size
        document.save(
            update_fields=[
                "first_page_width",
                "first_page_height",
                "thumbnail_file_key",
                "thumbnail_mime_type",
                "thumbnail_size_bytes",
                "updated_at",
            ]
        )
    except Exception:
        document.first_page_width = preserved.first_page_width
        document.first_page_height = preserved.first_page_height
        document.thumbnail_file_key = preserved.thumbnail_file_key
        document.thumbnail_mime_type = preserved.thumbnail_mime_type
        document.thumbnail_size_bytes = preserved.thumbnail_size_bytes
        logger.exception(
            "document thumbnail metadata persistence failed",
            extra=log_extra,
        )
        return None

    return DocumentThumbnailResult(
        first_page_width=width,
        first_page_height=height,
        thumbnail_file_key=thumbnail_key,
        thumbnail_mime_type=THUMBNAIL_JPEG_MIME,
        thumbnail_size_bytes=thumbnail_size,
    )
