"""PHOTO archive item create/upload (no Document/OCR/worker)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from botocore.exceptions import BotoCoreError, ClientError
from django.db import IntegrityError, transaction

from documents.models import ArchiveItem, PhotoContent
from documents.services.photo_content_management import (
    lock_photo_archive_item,
    lock_photo_contents_for_item,
    next_photo_position,
    wrap_integrity_position_conflict,
)
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
from documents.services.photo_metadata_validation import photo_metadata_from_mapping
from documents.services.photo_thumbnail import generate_and_persist_photo_thumbnail

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
    original_name: str,
    mime_type: str,
    discovery_metadata: dict[str, list[str]],
    description: str = "",
    location: str = "",
    context: str = "",
    people_present: str = "",
    notes: str = "",
    public_note: str = "",
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
        public_note=public_note,
    )
    photo_content = PhotoContent.objects.create(
        archive_item=archive_item,
        position=1,
        original_file_key="",
        original_filename=original_name,
        original_mime_type=normalized_mime,
        original_size_bytes=0,
        upload_status=PhotoContent.UploadStatus.PENDING,
        upload_error="",
        description=description,
        location=location,
        context=context,
        people_present=people_present,
        notes=notes,
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
def create_additional_photo_upload_plan(
    *,
    archive_item: ArchiveItem,
    bucket: str,
    original_name: str,
    mime_type: str,
    description: str = "",
    location: str = "",
    context: str = "",
    people_present: str = "",
    notes: str = "",
    date_start=None,
    date_end=None,
    date_precision: str = ArchiveItem.DatePrecision.UNKNOWN,
) -> tuple[ArchiveItem, PhotoContent, str]:
    """
    Add a pending PhotoContent to an existing PHOTO item and return a presigned PUT.

    Allocates the next position under an ArchiveItem row lock. Does not create a
    Document, enqueue SQS, or modify shared ArchiveItem metadata.

    The new row is ``PENDING`` and is not public-renderable, so its descriptive
    metadata is omitted from search even though this path still syncs the
    owning ArchiveItem index. Successful ``finalize_photo_upload`` later
    refreshes the index once the row is ``UPLOADED``.
    """
    normalized_mime = normalize_upload_mime_type(mime_type)
    locked_item = lock_photo_archive_item(archive_item.pk)
    lock_photo_contents_for_item(locked_item)
    position = next_photo_position(locked_item)
    try:
        photo_content = PhotoContent.objects.create(
            archive_item=locked_item,
            position=position,
            original_file_key="",
            original_filename=original_name,
            original_mime_type=normalized_mime,
            original_size_bytes=0,
            upload_status=PhotoContent.UploadStatus.PENDING,
            upload_error="",
            description=description,
            location=location,
            context=context,
            people_present=people_present,
            notes=notes,
            date_start=date_start,
            date_end=date_end,
            date_precision=date_precision,
        )
    except IntegrityError as exc:
        raise wrap_integrity_position_conflict(exc) from exc

    s3_key = build_photo_original_s3_key(photo_content.id, normalized_mime)
    photo_content.original_file_key = s3_key
    photo_content.save(update_fields=["original_file_key", "updated_at"])

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(locked_item.pk)

    upload_url = create_presigned_put(
        bucket=bucket,
        key=s3_key,
        content_type=normalized_mime,
    )
    return locked_item, photo_content, upload_url


@transaction.atomic
def _finalize_photo_upload_in_transaction(
    photo_content: PhotoContent,
    *,
    bucket: str,
    success: bool,
    file_mime: str | None,
    client_error: str | None = None,
) -> tuple[PhotoContent, Optional[PhotoS3VerificationError], bool]:
    """
    Lock, validate, verify, and persist original upload state.

    Returns ``(photo_content, verify_err, should_generate_thumbnail)``.
    Thumbnail generation is intentionally deferred until after this transaction
    commits successfully.
    """
    # Lock ArchiveItem first so later search-index sync matches other PHOTO
    # writers (item then PhotoContent) and cannot invert lock order.
    lock_photo_archive_item(photo_content.archive_item_id)
    photo_content = (
        PhotoContent.objects.select_for_update()
        .select_related("archive_item")
        .get(pk=photo_content.pk)
    )

    if photo_content.upload_status == PhotoContent.UploadStatus.UPLOADED:
        return photo_content, None, False

    if not success:
        err = _safe_upload_error(client_error or "upload failed")
        return _mark_photo_upload_failed(photo_content, err), None, False

    expected_mime = normalize_upload_mime_type(
        file_mime or photo_content.original_mime_type
    )
    metadata_err = validate_image_upload_metadata(
        mime_type=expected_mime,
        original_name=photo_content.original_filename,
    )
    if metadata_err:
        return (
            _mark_photo_upload_failed(photo_content, metadata_err),
            PhotoS3VerificationError(metadata_err, 400),
            False,
        )

    verify_err, head = verify_photo_s3_upload(
        bucket=bucket,
        key=photo_content.original_file_key,
        expected_mime=expected_mime,
    )
    if verify_err:
        if verify_err.retryable:
            return photo_content, verify_err, False
        return (
            _mark_photo_upload_failed(photo_content, verify_err.message),
            verify_err,
            False,
        )

    assert head is not None
    content_length = head.content_length
    assert content_length is not None
    photo_content.original_mime_type = expected_mime
    photo_content.original_size_bytes = content_length
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
    from documents.services.archive_search_index import sync_archive_item_search_index

    # Renderable metadata enters the index only after UPLOADED+key. Thumbnail
    # generation runs after this transaction and does not refresh search.
    sync_archive_item_search_index(photo_content.archive_item_id)
    return photo_content, None, True


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

    A successful ``PENDING`` → ``UPLOADED`` transition rebuilds the owning
    ``ArchiveItemSearchIndex`` in this same transaction so newly renderable
    photo metadata becomes searchable. Failed / retryable outcomes do not
    sync. Best-effort thumbnail generation runs only after this transaction
    commits successfully and does not refresh search.
    """
    photo_content, verify_err, should_generate_thumbnail = (
        _finalize_photo_upload_in_transaction(
            photo_content,
            bucket=bucket,
            success=success,
            file_mime=file_mime,
            client_error=client_error,
        )
    )
    if should_generate_thumbnail:
        generate_and_persist_photo_thumbnail(photo_content, bucket=bucket)
    return photo_content, verify_err


def _json_value_as_discovery_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def parse_create_photo_upload_metadata(
    payload: dict[str, Any],
    *,
    user=None,
) -> tuple[dict | None, str | None]:
    """Parse JSON body for photo upload create; return (parsed, error_message)."""
    title = (payload.get("title") or "").strip()
    if not title:
        return None, "title required"

    from documents.services.archive_discovery_metadata_validation import (
        empty_discovery_metadata_form_fields,
        parse_archive_item_discovery_metadata_form,
    )
    from documents.services.archive_item_validation import parse_date_precision
    from documents.services.archive_date_input import parse_archive_date_bounds
    from documents.services.archive_metadata_validation import (
        parse_metadata_status,
        parse_public_note,
        parse_visibility,
        validate_archive_metadata_fields,
    )

    try:
        visibility = parse_visibility(payload.get("visibility"), user=user)
        date_precision = parse_date_precision(payload.get("date_precision"))
        metadata_status = parse_metadata_status(payload.get("metadata_status"))
        date_start, date_end, _, date_errors = parse_archive_date_bounds(
            date_precision=date_precision,
            post_data=payload,
        )
    except ValueError as exc:
        return None, str(exc)

    if date_errors:
        return None, date_errors[0]

    field_errors = validate_archive_metadata_fields(
        title=title,
        visibility=visibility,
        metadata_status=metadata_status,
        date_precision=date_precision,
        date_start=date_start,
        date_end=date_end,
        user=user,
    )
    if field_errors:
        return None, field_errors[0]

    form_data = {
        **empty_discovery_metadata_form_fields(),
        "categories": _json_value_as_discovery_string(payload.get("categories")),
        "events": _json_value_as_discovery_string(payload.get("events")),
        "discovery_tags": _json_value_as_discovery_string(
            payload.get("discovery_tags")
        ),
        "selected_categories": payload.get("selected_categories"),
        "selected_events": payload.get("selected_events"),
        "selected_tags": payload.get("selected_tags"),
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

    photo_metadata = photo_metadata_from_mapping(payload)
    public_note = parse_public_note(payload.get("public_note"))

    return {
        "title": title,
        "visibility": visibility,
        "date_start": date_start,
        "date_end": date_end,
        "date_precision": date_precision,
        "metadata_status": metadata_status,
        "original_name": original_name,
        "mime_type": normalize_upload_mime_type(mime_type),
        "discovery_metadata": parsed_discovery,
        "public_note": public_note,
        **photo_metadata,
    }, None


def parse_add_photo_upload_metadata(
    payload: dict[str, Any],
) -> tuple[dict | None, str | None]:
    """Parse JSON body for adding a PhotoContent to an existing PHOTO item."""
    from documents.services.archive_item_validation import parse_date_precision
    from documents.services.archive_date_input import parse_archive_date_bounds
    from documents.services.photo_metadata_validation import (
        parse_photo_content_date_fields,
        photo_metadata_from_mapping,
    )

    raw_item_id = payload.get("archive_item_id")
    try:
        archive_item_id = int(raw_item_id)
    except (TypeError, ValueError):
        return None, "archive_item_id required"
    if archive_item_id < 1:
        return None, "archive_item_id required"

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

    try:
        date_precision = parse_date_precision(payload.get("date_precision"))
    except ValueError as exc:
        return None, str(exc)

    date_start, date_end, _, date_errors = parse_archive_date_bounds(
        date_precision=date_precision,
        post_data=payload,
    )
    if date_errors:
        return None, date_errors[0]

    date_error = parse_photo_content_date_fields(
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )
    if date_error:
        return None, date_error

    photo_metadata = photo_metadata_from_mapping(payload)
    return {
        "archive_item_id": archive_item_id,
        "original_name": original_name,
        "mime_type": normalize_upload_mime_type(mime_type),
        "date_start": date_start,
        "date_end": date_end,
        "date_precision": date_precision,
        **photo_metadata,
    }, None
