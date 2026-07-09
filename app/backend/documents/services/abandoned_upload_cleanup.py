from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from botocore.exceptions import ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.models import ArchiveItem, Document
from documents.s3 import delete_s3_object
from documents.services.source_files import is_incremental_multi_image_draft


@dataclass(frozen=True)
class AbandonedUploadCandidate:
    document_id: int
    title: str
    created_at: str
    updated_at: str
    upload_status: str
    processing_state_user: str
    source_file_count: int
    s3_keys: list[str] = field(default_factory=list)


@dataclass
class AbandonedUploadCleanupReport:
    stale_hours: int
    document_id_filter: int | None
    bucket: str
    candidates: list[AbandonedUploadCandidate] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "stale_hours": self.stale_hours,
            "document_id_filter": self.document_id_filter,
            "bucket": self.bucket,
            "candidate_count": self.candidate_count,
            "candidates": [
                {
                    "document_id": row.document_id,
                    "title": row.title,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "upload_status": row.upload_status,
                    "processing_state_user": row.processing_state_user,
                    "source_file_count": row.source_file_count,
                    "s3_keys": list(row.s3_keys),
                }
                for row in self.candidates
            ],
        }


@dataclass(frozen=True)
class S3DeleteFailure:
    document_id: int
    s3_key: str
    error: str


@dataclass
class AbandonedUploadCleanupApplyResult:
    documents_deleted: int
    s3_keys_deleted: int
    s3_keys_not_found: int
    s3_delete_failures: list[S3DeleteFailure] = field(default_factory=list)


def _format_s3_delete_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "ClientError")
        message = err.get("Message", str(exc))
        return f"{code}: {message}"
    return f"{type(exc).__name__}: {exc}"


def _uploads_bucket_name() -> str:
    return (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()


def abandoned_incremental_ocr_drafts_queryset(
    *,
    stale_hours: int,
    document_id: int | None = None,
):
    cutoff = timezone.now() - timedelta(hours=stale_hours)
    qs = (
        Document.objects.filter(
            doc_type=Document.DocType.IMAGE,
            expected_source_file_count__isnull=True,
            file_s3_key="",
            archive_item__item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
            updated_at__lt=cutoff,
        )
        .exclude(upload_status=Document.UploadStatus.UPLOADED)
        .select_related("archive_item")
        .prefetch_related("source_files")
        .order_by("updated_at", "pk")
    )
    if document_id is not None:
        qs = qs.filter(pk=document_id)
    return qs


def _collect_s3_keys(document: Document) -> list[str]:
    keys: list[str] = []
    doc_key = (document.file_s3_key or "").strip()
    if doc_key:
        keys.append(doc_key)
    for source in document.source_files.all():
        source_key = (source.file_s3_key or "").strip()
        if source_key and source_key not in keys:
            keys.append(source_key)
    return keys


def _candidate_from_document(document: Document) -> AbandonedUploadCandidate:
    title = ""
    if document.archive_item_id:
        title = (document.archive_item.title or "").strip()
    return AbandonedUploadCandidate(
        document_id=document.pk,
        title=title,
        created_at=document.created_at.isoformat(),
        updated_at=document.updated_at.isoformat(),
        upload_status=document.upload_status,
        processing_state_user=document.processing_state_user,
        source_file_count=document.source_files.count(),
        s3_keys=_collect_s3_keys(document),
    )


def build_abandoned_upload_cleanup_report(
    *,
    stale_hours: int,
    document_id: int | None = None,
) -> AbandonedUploadCleanupReport:
    if stale_hours < 1:
        raise ValueError("stale_hours must be >= 1.")

    report = AbandonedUploadCleanupReport(
        stale_hours=stale_hours,
        document_id_filter=document_id,
        bucket=_uploads_bucket_name(),
    )
    for document in abandoned_incremental_ocr_drafts_queryset(
        stale_hours=stale_hours,
        document_id=document_id,
    ):
        if not is_incremental_multi_image_draft(document):
            continue
        report.candidates.append(_candidate_from_document(document))
    return report


def apply_abandoned_upload_cleanup(
    report: AbandonedUploadCleanupReport,
) -> AbandonedUploadCleanupApplyResult:
    bucket = report.bucket
    if not bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot delete S3 objects."
        )

    result = AbandonedUploadCleanupApplyResult(
        documents_deleted=0,
        s3_keys_deleted=0,
        s3_keys_not_found=0,
    )

    for candidate in report.candidates:
        document = Document.objects.filter(pk=candidate.document_id).first()
        if document is None:
            continue
        if not is_incremental_multi_image_draft(document):
            continue

        s3_keys = _collect_s3_keys(document)
        for key in s3_keys:
            try:
                delete_result = delete_s3_object(bucket, key)
            except Exception as exc:
                result.s3_delete_failures.append(
                    S3DeleteFailure(
                        document_id=document.pk,
                        s3_key=key,
                        error=_format_s3_delete_error(exc),
                    )
                )
                continue
            if delete_result.deleted:
                result.s3_keys_deleted += 1
            elif delete_result.not_found:
                result.s3_keys_not_found += 1

        with transaction.atomic():
            document.delete()
        result.documents_deleted += 1

    return result
