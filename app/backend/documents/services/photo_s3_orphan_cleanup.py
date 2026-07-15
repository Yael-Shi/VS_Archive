"""PHOTO-specific wrapper around the shared S3 orphan cleanup service."""

from __future__ import annotations

from documents.models import PhotoContent
from documents.s3 import delete_s3_object
from documents.services.s3_orphan_cleanup import (
    S3DeleteFailure as S3DeleteFailure,
    S3ListedObject as S3ListedObject,
    S3OrphanCandidate,
    S3OrphanCleanupApplyResult,
    S3OrphanCleanupReport,
    apply_s3_orphan_cleanup,
    build_s3_orphan_cleanup_report,
    list_s3_objects_under_prefix,
    normalize_and_validate_s3_prefix as normalize_shared_s3_prefix,
)

PHOTO_S3_REFERENCE_FIELDS: tuple[str, ...] = (
    "original_file_key",
    "thumbnail_file_key",
)

_PHOTOS_PREFIX_ROOT = "photos/"

PhotoS3OrphanCandidate = S3OrphanCandidate
PhotoS3OrphanCleanupReport = S3OrphanCleanupReport
PhotoS3OrphanCleanupApplyResult = S3OrphanCleanupApplyResult


def normalize_and_validate_s3_prefix(prefix: str) -> str:
    return normalize_shared_s3_prefix(
        prefix,
        root_prefix=_PHOTOS_PREFIX_ROOT,
    )


def collect_referenced_photo_s3_keys() -> set[str]:
    keys: set[str] = set()

    for field_name in PHOTO_S3_REFERENCE_FIELDS:
        for key in PhotoContent.objects.values_list(field_name, flat=True):
            normalized = (key or "").strip()
            if normalized:
                keys.add(normalized)

    return keys


def build_photo_s3_orphan_cleanup_report(
    *,
    older_than_hours: int,
    prefix: str = _PHOTOS_PREFIX_ROOT,
    limit: int | None = None,
) -> PhotoS3OrphanCleanupReport:
    return build_s3_orphan_cleanup_report(
        older_than_hours=older_than_hours,
        prefix=prefix,
        root_prefix=_PHOTOS_PREFIX_ROOT,
        referenced_keys=collect_referenced_photo_s3_keys(),
        list_objects=list_s3_objects_under_prefix,
        limit=limit,
    )


def apply_photo_s3_orphan_cleanup(
    report: PhotoS3OrphanCleanupReport,
) -> PhotoS3OrphanCleanupApplyResult:
    return apply_s3_orphan_cleanup(
        report,
        delete_object=delete_s3_object,
    )
