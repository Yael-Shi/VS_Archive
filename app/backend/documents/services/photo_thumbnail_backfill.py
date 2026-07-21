"""Backfill PHOTO thumbnails for uploaded PhotoContent rows missing metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from django.conf import settings
from django.db.models import CharField, Func

from documents.models import PhotoContent
from documents.services.photo_thumbnail import generate_and_persist_photo_thumbnail

PhotoThumbnailTargetDisposition = Literal[
    "candidate",
    "not_found",
    "ineligible",
    "has_thumbnail",
]

PhotoThumbnailOutcome = Literal["succeeded", "failed", "skipped"]


@dataclass(frozen=True)
class PhotoThumbnailBackfillCandidate:
    photo_content_id: int
    original_file_key: str
    upload_status: str


@dataclass(frozen=True)
class PhotoThumbnailBackfillTarget:
    photo_content_id: int
    disposition: PhotoThumbnailTargetDisposition
    reason: str | None = None
    original_file_key: str | None = None
    upload_status: str | None = None
    thumbnail_file_key: str | None = None


@dataclass(frozen=True)
class PhotoThumbnailBackfillPhotoResult:
    photo_content_id: int
    outcome: PhotoThumbnailOutcome
    reason: str | None = None
    thumbnail_file_key: str | None = None


@dataclass
class PhotoThumbnailBackfillReport:
    photo_id_filter: int | None
    limit: int | None
    bucket: str
    candidates: list[PhotoThumbnailBackfillCandidate] = field(default_factory=list)
    target: PhotoThumbnailBackfillTarget | None = None

    @property
    def candidate_count(self) -> int:
        """Number of candidates selected for this invocation (after ``--limit``)."""
        return len(self.candidates)

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "photo_id_filter": self.photo_id_filter,
            "limit": self.limit,
            "bucket": self.bucket,
            "candidate_count": self.candidate_count,
            "candidates": [
                {
                    "photo_content_id": row.photo_content_id,
                    "original_file_key": row.original_file_key,
                    "upload_status": row.upload_status,
                }
                for row in self.candidates
            ],
        }
        if self.target is not None:
            payload["target"] = {
                "photo_content_id": self.target.photo_content_id,
                "disposition": self.target.disposition,
                "reason": self.target.reason,
                "original_file_key": self.target.original_file_key,
                "upload_status": self.target.upload_status,
                "thumbnail_file_key": self.target.thumbnail_file_key,
            }
        return payload


@dataclass
class PhotoThumbnailBackfillApplyResult:
    processed_count: int = 0
    succeeded_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    results: list[PhotoThumbnailBackfillPhotoResult] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "processed_count": self.processed_count,
            "succeeded_count": self.succeeded_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "results": [
                {
                    "photo_content_id": row.photo_content_id,
                    "outcome": row.outcome,
                    "reason": row.reason,
                    "thumbnail_file_key": row.thumbnail_file_key,
                }
                for row in self.results
            ],
        }


def _uploads_bucket_name() -> str:
    return (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()


class _StripWhitespace(Func):
    """Strip leading/trailing PostgreSQL POSIX ``[[:space:]]`` characters.

    Covers expected stored S3 key whitespace, including spaces and tabs. This is
    not a claim of exact equivalence with Python ``str.strip()`` for every
    Unicode whitespace code point.
    """

    template = "REGEXP_REPLACE(%(expressions)s, '^[[:space:]]+|[[:space:]]+$', '', 'g')"
    output_field = CharField()


def _non_empty_stripped(value: str | None) -> str:
    return (value or "").strip()


def _eligibility_reason(photo: PhotoContent) -> tuple[bool, str | None]:
    if photo.upload_status != PhotoContent.UploadStatus.UPLOADED:
        return False, f"upload_status={photo.upload_status}"
    if not _non_empty_stripped(photo.original_file_key):
        return False, "missing original_file_key"
    if _non_empty_stripped(photo.thumbnail_file_key):
        return False, "thumbnail_file_key already set"
    return True, None


def _candidate_from_photo(photo: PhotoContent) -> PhotoThumbnailBackfillCandidate:
    return PhotoThumbnailBackfillCandidate(
        photo_content_id=photo.pk,
        original_file_key=_non_empty_stripped(photo.original_file_key),
        upload_status=photo.upload_status,
    )


def _resolve_photo_target(photo_id: int) -> PhotoThumbnailBackfillTarget:
    photo = PhotoContent.objects.filter(pk=photo_id).first()
    if photo is None:
        return PhotoThumbnailBackfillTarget(
            photo_content_id=photo_id,
            disposition="not_found",
            reason="PhotoContent does not exist",
        )

    thumbnail_key = _non_empty_stripped(photo.thumbnail_file_key)
    if thumbnail_key:
        return PhotoThumbnailBackfillTarget(
            photo_content_id=photo_id,
            disposition="has_thumbnail",
            reason="thumbnail_file_key already set",
            thumbnail_file_key=thumbnail_key,
            upload_status=photo.upload_status,
            original_file_key=photo.original_file_key,
        )

    eligible, reason = _eligibility_reason(photo)
    if not eligible:
        return PhotoThumbnailBackfillTarget(
            photo_content_id=photo_id,
            disposition="ineligible",
            reason=reason,
            upload_status=photo.upload_status,
            original_file_key=photo.original_file_key,
        )

    return PhotoThumbnailBackfillTarget(
        photo_content_id=photo_id,
        disposition="candidate",
        original_file_key=_non_empty_stripped(photo.original_file_key),
        upload_status=photo.upload_status,
    )


def _backfill_candidates_queryset():
    return (
        PhotoContent.objects.annotate(
            _original_key_trimmed=_StripWhitespace("original_file_key"),
            _thumbnail_key_trimmed=_StripWhitespace("thumbnail_file_key"),
        )
        .filter(
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        .exclude(_original_key_trimmed="")
        .filter(_thumbnail_key_trimmed="")
        .order_by("pk")
    )


def build_photo_thumbnail_backfill_report(
    *,
    photo_id: int | None = None,
    limit: int | None = None,
) -> PhotoThumbnailBackfillReport:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer.")

    report = PhotoThumbnailBackfillReport(
        photo_id_filter=photo_id,
        limit=limit,
        bucket=_uploads_bucket_name(),
    )

    if photo_id is not None:
        target = _resolve_photo_target(photo_id)
        report.target = target
        if target.disposition == "candidate":
            report.candidates.append(
                PhotoThumbnailBackfillCandidate(
                    photo_content_id=target.photo_content_id,
                    original_file_key=target.original_file_key or "",
                    upload_status=target.upload_status
                    or PhotoContent.UploadStatus.UPLOADED,
                )
            )
        return report

    queryset = _backfill_candidates_queryset()
    if limit is not None:
        queryset = queryset[:limit]
    report.candidates = [_candidate_from_photo(photo) for photo in queryset]
    return report


def apply_photo_thumbnail_backfill(
    report: PhotoThumbnailBackfillReport,
) -> PhotoThumbnailBackfillApplyResult:
    bucket = report.bucket
    if not bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot generate photo thumbnails."
        )

    apply_result = PhotoThumbnailBackfillApplyResult()

    for candidate in report.candidates:
        photo = PhotoContent.objects.filter(pk=candidate.photo_content_id).first()
        if photo is None:
            apply_result.skipped_count += 1
            apply_result.results.append(
                PhotoThumbnailBackfillPhotoResult(
                    photo_content_id=candidate.photo_content_id,
                    outcome="skipped",
                    reason="PhotoContent no longer exists",
                )
            )
            continue

        thumbnail_key = _non_empty_stripped(photo.thumbnail_file_key)
        if thumbnail_key:
            apply_result.skipped_count += 1
            apply_result.results.append(
                PhotoThumbnailBackfillPhotoResult(
                    photo_content_id=photo.pk,
                    outcome="skipped",
                    reason="thumbnail_file_key already set",
                    thumbnail_file_key=thumbnail_key,
                )
            )
            continue

        eligible, reason = _eligibility_reason(photo)
        if not eligible:
            apply_result.skipped_count += 1
            apply_result.results.append(
                PhotoThumbnailBackfillPhotoResult(
                    photo_content_id=photo.pk,
                    outcome="skipped",
                    reason=reason,
                )
            )
            continue

        apply_result.processed_count += 1
        thumb_result = generate_and_persist_photo_thumbnail(photo, bucket=bucket)
        if thumb_result is not None:
            apply_result.succeeded_count += 1
            apply_result.results.append(
                PhotoThumbnailBackfillPhotoResult(
                    photo_content_id=photo.pk,
                    outcome="succeeded",
                    thumbnail_file_key=thumb_result.thumbnail_file_key,
                )
            )
        else:
            apply_result.failed_count += 1
            apply_result.results.append(
                PhotoThumbnailBackfillPhotoResult(
                    photo_content_id=photo.pk,
                    outcome="failed",
                    reason="thumbnail generation or persistence failed",
                )
            )

    return apply_result
