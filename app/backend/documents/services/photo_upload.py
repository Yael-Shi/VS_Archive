"""PHOTO archive item create/upload (no Document/OCR/worker)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from django.db import transaction

from documents.models import ArchiveItem, PhotoContent
from documents.s3 import (
    S3HeadObjectResult,
    build_photo_original_s3_key,
    create_presigned_put,
    head_s3_object,
)
from documents.services.archive_items import update_archive_item_discovery_metadata
from documents.services.upload_validation import (
    normalize_upload_mime_type,
    upload_mime_types_match,
    validate_image_upload_metadata,
)

_UPLOAD_ERROR_MAX_LENGTH = 512


@dataclass(frozen=True)
class PhotoS3VerificationError:
    message: str
    status: int
    retryable: bool = False


def _safe_upload_error(message: str) -> str:
    return (message or "").strip()[:_UPLOAD_ERROR_MAX_LENGTH]


def _mark_photo_upload_failed(photo_content: PhotoContent, error: str) -> PhotoContent:
    photo_content.upload_status = PhotoContent.UploadStatus.FAILED
    photo_content.upload_error = _safe_upload_error(error)
    photo_content.save(update_fields=["upload_status", "upload_error", "updated_at"])
    return photo_content


def verify_photo_s3_upload(
    *,
    bucket: str,
    key: str,
    expected_mime: str,
) -> tuple[Optional[PhotoS3VerificationError], Optional[S3HeadObjectResult]]:
    """
    Verify uploaded S3 object exists and ContentType matches expected MIME.

    Returns (error, head). On retryable AWS/client failure (502), error is set and
    head is None — caller should leave upload_status as PENDING.
    """
    if not normalize_upload_mime_type(expected_mime):
        return PhotoS3VerificationError("expected mime type missing", 400), None

    try:
        head = head_s3_object(bucket, key)
    except (BotoCoreError, ClientError):
        return (
            PhotoS3VerificationError("s3 verification failed", 502, retryable=True),
            None,
        )

    if not head.exists:
        return PhotoS3VerificationError("s3 object not found", 400), head

    if not head.content_type:
        return PhotoS3VerificationError("s3 content type missing", 400), head

    if not upload_mime_types_match(expected_mime, head.content_type):
        return PhotoS3VerificationError("s3 content type mismatch", 400), head

    if head.content_length is None or head.content_length <= 0:
        return PhotoS3VerificationError("s3 content length missing", 400), head

    return None, head


@transaction.atomic
def create_photo_upload_plan(
    *,
    bucket: str,
    title: str,
    visibility: str,
    date_start,
    date_end,
    date_precision: str,
    metadata_status: str,
    author_name: str,
    source_title: str,
    original_name: str,
    mime_type: str,
    discovery_metadata: dict[str, list[str]],
) -> tuple[ArchiveItem, PhotoContent, str]:
    """
    Create PHOTO ArchiveItem + pending PhotoContent and return a presigned PUT URL.

    PhotoContent is created before client upload with ``upload_status=PENDING``.
    """
    normalized_mime = normalize_upload_mime_type(mime_type)

    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
        metadata_status=metadata_status,
        author_name=author_name,
        source_title=source_title,
    )
    photo_content = PhotoContent.objects.create(
        archive_item=archive_item,
        original_file_key="",
        original_filename=original_name,
        original_mime_type=normalized_mime,
        original_size_bytes=0,
        upload_status=PhotoContent.UploadStatus.PENDING,
        upload_error="",
    )
    s3_key = build_photo_original_s3_key(photo_content.id, normalized_mime)
    photo_content.original_file_key = s3_key
    photo_content.save(update_fields=["original_file_key", "updated_at"])

    update_archive_item_discovery_metadata(
        archive_item,
        category_names=discovery_metadata["category_names"],
        event_names=discovery_metadata["event_names"],
        tag_names=discovery_metadata["tag_names"],
    )

    upload_url = create_presigned_put(
        bucket=bucket,
        key=s3_key,
        content_type=normalized_mime,
    )
    return archive_item, photo_content, upload_url


@transaction.atomic
def finalize_photo_upload(
    photo_content: PhotoContent,
    *,
    bucket: str,
    success: bool,
    file_mime: str | None,
    client_error: str | None = None,
) -> tuple[PhotoContent, Optional[PhotoS3VerificationError]]:
    """
    Finalize client S3 upload for a pending PhotoContent row.

    Persists ``original_size_bytes`` from S3 HeadObject ``ContentLength`` only.
    Client-reported size is never used as source of truth.

    Already-``UPLOADED`` rows are returned unchanged (idempotent complete).

    Retryable S3 verification failures (502) leave ``upload_status=PENDING``.
    Validation/verification/client failures set ``upload_status=FAILED``.
    Re-upload/retry after ``FAILED`` is deferred (not implemented in PR3).
    """
    photo_content = PhotoContent.objects.select_for_update().select_related(
        "archive_item"
    ).get(pk=photo_content.pk)

    if photo_content.upload_status == PhotoContent.UploadStatus.UPLOADED:
        return photo_content, None

    if not success:
        err = _safe_upload_error(client_error or "upload failed")
        return _mark_photo_upload_failed(photo_content, err), None

    expected_mime = normalize_upload_mime_type(
        file_mime or photo_content.original_mime_type
    )
    metadata_err = validate_image_upload_metadata(
        mime_type=expected_mime,
        original_name=photo_content.original_filename,
    )
    if metadata_err:
        return _mark_photo_upload_failed(photo_content, metadata_err), PhotoS3VerificationError(
            metadata_err,
            400,
        )

    verify_err, head = verify_photo_s3_upload(
        bucket=bucket,
        key=photo_content.original_file_key,
        expected_mime=expected_mime,
    )
    if verify_err:
        if verify_err.retryable:
            return photo_content, verify_err
        return _mark_photo_upload_failed(photo_content, verify_err.message), verify_err

    assert head is not None
    photo_content.original_mime_type = expected_mime
    photo_content.original_size_bytes = head.content_length
    photo_content.upload_status = PhotoContent.UploadStatus.UPLOADED
    photo_content.upload_error = ""
    photo_content.save(
        update_fields=[
            "original_mime_type",
            "original_size_bytes",
            "upload_status",
            "upload_error",
            "updated_at",
        ]
    )
    return photo_content, None


def _json_value_as_discovery_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def parse_create_photo_upload_metadata(payload: dict[str, Any]) -> tuple[dict | None, str | None]:
    """Parse JSON body for photo upload create; return (parsed, error_message)."""
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "title required"

    visibility = (payload.get("visibility") or ArchiveItem.Visibility.PRIVATE).strip()
    if visibility not in {choice.value for choice in ArchiveItem.Visibility}:
        return None, "visibility must be private or public"

    from documents.services.archive_discovery_metadata_validation import (
        parse_archive_item_discovery_metadata_form,
    )
    from documents.services.archive_item_validation import parse_date_precision
    from documents.services.archive_metadata_validation import (
        parse_metadata_status,
        parse_optional_date,
        validate_archive_metadata_fields,
        validate_source_metadata_fields,
    )

    try:
        date_start = parse_optional_date(payload.get("date_start"), "date_start")
        date_end = parse_optional_date(payload.get("date_end"), "date_end")
        date_precision = parse_date_precision(payload.get("date_precision"))
        metadata_status = parse_metadata_status(payload.get("metadata_status"))
    except ValueError as exc:
        return None, str(exc)

    author_name = (payload.get("author_name") or "").strip()
    source_title = (payload.get("source_title") or "").strip()
    source_errors = validate_source_metadata_fields(
        author_name=author_name,
        source_title=source_title,
    )
    if source_errors:
        return None, source_errors[0]

    field_errors = validate_archive_metadata_fields(
        title=title,
        visibility=visibility,
        metadata_status=metadata_status,
        date_precision=date_precision,
        date_start=date_start,
        date_end=date_end,
        author_name=author_name,
        source_title=source_title,
    )
    if field_errors:
        return None, field_errors[0]

    form_data = {
        "categories": _json_value_as_discovery_string(payload.get("categories")),
        "events": _json_value_as_discovery_string(payload.get("events")),
        "discovery_tags": _json_value_as_discovery_string(payload.get("discovery_tags")),
    }
    parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
        form_data,
        tags_field="discovery_tags",
    )
    if discovery_errors:
        return None, discovery_errors[0]

    original_name = (payload.get("original_name") or "").strip()
    mime_type = (payload.get("mime_type") or "").strip()
    if not original_name:
        return None, "original_name required"
    if not mime_type:
        return None, "mime_type required"

    metadata_err = validate_image_upload_metadata(
        mime_type=mime_type,
        original_name=original_name,
    )
    if metadata_err:
        return None, metadata_err

    return {
        "title": title,
        "visibility": visibility,
        "date_start": date_start,
        "date_end": date_end,
        "date_precision": date_precision,
        "metadata_status": metadata_status,
        "author_name": author_name,
        "source_title": source_title,
        "original_name": original_name,
        "mime_type": normalize_upload_mime_type(mime_type),
        "discovery_metadata": parsed_discovery,
    }, None
