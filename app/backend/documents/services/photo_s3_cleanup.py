"""Best-effort S3 cleanup for deleted PHOTO archive items."""

from __future__ import annotations

import logging
from functools import partial

from botocore.exceptions import BotoCoreError, ClientError
from django.db import transaction

from documents.s3 import delete_s3_object

logger = logging.getLogger(__name__)


def delete_photo_s3_objects_best_effort(
    *,
    bucket: str,
    original_file_key: str,
    thumbnail_file_key: str,
    photo_content_id: int | None,
) -> None:
    """
    Delete a PHOTO original and thumbnail after their DB rows are gone.

    Cleanup is best-effort because the database transaction has already committed.
    AWS failures are logged and do not propagate to the completed delete request.
    """
    normalized_bucket = (bucket or "").strip()
    if not normalized_bucket:
        logger.warning(
            "photo S3 cleanup skipped: uploads bucket is missing",
            extra={"photo_content_id": photo_content_id},
        )
        return

    unique_keys: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    for object_kind, raw_key in (
        ("original", original_file_key),
        ("thumbnail", thumbnail_file_key),
    ):
        normalized_key = (raw_key or "").strip()
        if not normalized_key or normalized_key in seen_keys:
            continue
        seen_keys.add(normalized_key)
        unique_keys.append((object_kind, normalized_key))

    for object_kind, key in unique_keys:
        try:
            delete_s3_object(normalized_bucket, key)
        except (BotoCoreError, ClientError):
            logger.exception(
                "photo S3 cleanup failed",
                extra={
                    "photo_content_id": photo_content_id,
                    "object_kind": object_kind,
                    "s3_key": key,
                },
            )


def schedule_photo_s3_cleanup_after_commit(
    *,
    bucket: str,
    original_file_key: str,
    thumbnail_file_key: str,
    photo_content_id: int | None,
) -> None:
    """Schedule PHOTO object cleanup only after the DB transaction commits."""
    transaction.on_commit(
        partial(
            delete_photo_s3_objects_best_effort,
            bucket=bucket,
            original_file_key=original_file_key,
            thumbnail_file_key=thumbnail_file_key,
            photo_content_id=photo_content_id,
        )
    )
