"""Shared S3 orphan-audit and cleanup primitives."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from django.conf import settings
from django.utils import timezone

from documents.s3 import S3DeleteObjectResult, get_s3_client


@dataclass(frozen=True)
class S3ListedObject:
    key: str
    last_modified: datetime
    size: int


@dataclass(frozen=True)
class S3OrphanCandidate:
    key: str
    last_modified: str
    size: int


@dataclass
class S3OrphanCleanupReport:
    older_than_hours: int
    prefix: str
    limit: int | None
    bucket: str
    candidates: list[S3OrphanCandidate] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "older_than_hours": self.older_than_hours,
            "prefix": self.prefix,
            "limit": self.limit,
            "bucket": self.bucket,
            "candidate_count": self.candidate_count,
            "candidates": [
                {
                    "key": row.key,
                    "last_modified": row.last_modified,
                    "size": row.size,
                }
                for row in self.candidates
            ],
        }


@dataclass(frozen=True)
class S3DeleteFailure:
    s3_key: str
    error: str


@dataclass
class S3OrphanCleanupApplyResult:
    s3_keys_deleted: int
    s3_keys_not_found: int
    s3_delete_failures: list[S3DeleteFailure] = field(default_factory=list)

    @property
    def has_delete_failures(self) -> bool:
        return bool(self.s3_delete_failures)


def uploads_bucket_name() -> str:
    return (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()


def normalize_and_validate_s3_prefix(
    prefix: str,
    *,
    root_prefix: str,
) -> str:
    raw = (prefix or "").strip()
    if not raw:
        raise ValueError("prefix must not be empty.")
    if ".." in raw:
        raise ValueError("prefix must not contain '..'.")

    normalized_root = root_prefix.strip().lstrip("/")
    if not normalized_root.endswith("/"):
        normalized_root = f"{normalized_root}/"

    normalized = raw.lstrip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")

    if not normalized.startswith(normalized_root):
        raise ValueError(f"prefix must be within {normalized_root}.")

    if not normalized.endswith("/"):
        normalized = f"{normalized}/"

    return normalized


def list_s3_objects_under_prefix(
    bucket: str,
    prefix: str,
) -> Iterator[S3ListedObject]:
    s3 = get_s3_client()
    paginator = s3.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            last_modified = item.get("LastModified")
            size = item.get("Size")

            if not key or last_modified is None or size is None:
                continue

            yield S3ListedObject(
                key=key,
                last_modified=last_modified,
                size=int(size),
            )


def normalize_last_modified(value: datetime) -> datetime:
    if timezone.is_naive(value):
        return timezone.make_aware(
            value,
            timezone=timezone.get_current_timezone(),
        )
    return value


def candidate_from_s3_object(obj: S3ListedObject) -> S3OrphanCandidate:
    last_modified = normalize_last_modified(obj.last_modified)
    return S3OrphanCandidate(
        key=obj.key,
        last_modified=last_modified.isoformat(),
        size=obj.size,
    )


def build_s3_orphan_cleanup_report(
    *,
    older_than_hours: int,
    prefix: str,
    root_prefix: str,
    referenced_keys: Iterable[str],
    list_objects: Callable[[str, str], Iterable[S3ListedObject]],
    limit: int | None = None,
) -> S3OrphanCleanupReport:
    if older_than_hours < 1:
        raise ValueError("older_than_hours must be >= 1.")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be > 0.")

    normalized_prefix = normalize_and_validate_s3_prefix(
        prefix,
        root_prefix=root_prefix,
    )

    bucket = uploads_bucket_name()
    if not bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot audit S3 objects."
        )

    normalized_referenced_keys = {
        normalized for key in referenced_keys if (normalized := (key or "").strip())
    }

    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    orphan_objects: list[S3ListedObject] = []

    for obj in list_objects(bucket, normalized_prefix):
        if obj.key in normalized_referenced_keys:
            continue

        last_modified = normalize_last_modified(obj.last_modified)
        if last_modified >= cutoff:
            continue

        orphan_objects.append(
            S3ListedObject(
                key=obj.key,
                last_modified=last_modified,
                size=obj.size,
            )
        )

    orphan_objects.sort(key=lambda row: (row.last_modified, row.key))
    if limit is not None:
        orphan_objects = orphan_objects[:limit]

    return S3OrphanCleanupReport(
        older_than_hours=older_than_hours,
        prefix=normalized_prefix,
        limit=limit,
        bucket=bucket,
        candidates=[candidate_from_s3_object(obj) for obj in orphan_objects],
    )


def format_s3_delete_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = error.get("Code", "ClientError")
        message = error.get("Message", str(exc))
        return f"{code}: {message}"

    return f"{type(exc).__name__}: {exc}"


def apply_s3_orphan_cleanup(
    report: S3OrphanCleanupReport,
    *,
    delete_object: Callable[[str, str], S3DeleteObjectResult],
) -> S3OrphanCleanupApplyResult:
    if not report.bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot delete S3 objects."
        )

    result = S3OrphanCleanupApplyResult(
        s3_keys_deleted=0,
        s3_keys_not_found=0,
    )

    for candidate in report.candidates:
        try:
            delete_result = delete_object(report.bucket, candidate.key)
        except Exception as exc:
            result.s3_delete_failures.append(
                S3DeleteFailure(
                    s3_key=candidate.key,
                    error=format_s3_delete_error(exc),
                )
            )
            continue

        if delete_result.deleted:
            result.s3_keys_deleted += 1
        elif delete_result.not_found:
            result.s3_keys_not_found += 1
        else:
            result.s3_delete_failures.append(
                S3DeleteFailure(
                    s3_key=candidate.key,
                    error="unexpected delete result: deleted=False not_found=False",
                )
            )

    return result
