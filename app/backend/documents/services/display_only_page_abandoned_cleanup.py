"""Stale uncommitted display-only source-file extras (not whole-document drafts)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from botocore.exceptions import ClientError
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.models import Document, DocumentSourceFile, ProcessDocumentRequest
from documents.s3 import delete_s3_object
from documents.services.display_only_page_upload import (
    ACTIVE_PROCESS_DOCUMENT_REQUEST_STATUSES,
    committed_physical_source_count,
)


@dataclass(frozen=True)
class AbandonedDisplayOnlyExtraCandidate:
    document_id: int
    source_file_id: int
    order_index: int
    upload_status: str
    updated_at: str
    s3_key: str


@dataclass
class AbandonedDisplayOnlyExtraCleanupReport:
    stale_hours: int
    document_id_filter: int | None
    bucket: str
    candidates: list[AbandonedDisplayOnlyExtraCandidate] = field(default_factory=list)

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
                    "source_file_id": row.source_file_id,
                    "order_index": row.order_index,
                    "upload_status": row.upload_status,
                    "updated_at": row.updated_at,
                    "s3_key": row.s3_key,
                }
                for row in self.candidates
            ],
        }


@dataclass(frozen=True)
class DisplayOnlyExtraS3DeleteFailure:
    document_id: int
    source_file_id: int
    s3_key: str
    error: str


@dataclass
class AbandonedDisplayOnlyExtraCleanupApplyResult:
    source_files_deleted: int
    s3_keys_deleted: int
    s3_keys_not_found: int
    s3_delete_failures: list[DisplayOnlyExtraS3DeleteFailure] = field(
        default_factory=list
    )


def _format_s3_delete_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "ClientError")
        message = err.get("Message", str(exc))
        return f"{code}: {message}"
    return f"{type(exc).__name__}: {exc}"


def _uploads_bucket_name() -> str:
    return (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()


def _document_is_busy(document: Document) -> bool:
    if document.processing_state_user in (
        Document.ProcessingState.PROCESSING,
        Document.ProcessingState.RECOVERY_REQUIRED,
    ):
        return True
    return ProcessDocumentRequest.objects.filter(
        document_id=document.id,
        status__in=ACTIVE_PROCESS_DOCUMENT_REQUEST_STATUSES,
    ).exists()


def is_abandoned_display_only_extra(
    source: DocumentSourceFile,
    document: Document,
    *,
    cutoff,
) -> bool:
    """Uncommitted display-only extra that is stale and not mid-upload/OCR."""
    if source.include_in_ocr:
        return False
    if source.upload_status not in (
        DocumentSourceFile.UploadStatus.PENDING,
        DocumentSourceFile.UploadStatus.FAILED,
    ):
        return False
    if source.updated_at >= cutoff:
        return False
    if source.order_index < committed_physical_source_count(document):
        return False
    if document.upload_status != Document.UploadStatus.UPLOADED:
        return False
    if _document_is_busy(document):
        return False
    return True


def abandoned_display_only_extras_queryset(
    *,
    stale_hours: int,
    document_id: int | None = None,
):
    cutoff = timezone.now() - timedelta(hours=stale_hours)
    qs = (
        DocumentSourceFile.objects.filter(
            include_in_ocr=False,
            upload_status__in=(
                DocumentSourceFile.UploadStatus.PENDING,
                DocumentSourceFile.UploadStatus.FAILED,
            ),
            updated_at__lt=cutoff,
        )
        .select_related("document", "document__archive_item")
        .order_by("updated_at", "pk")
    )
    if document_id is not None:
        qs = qs.filter(document_id=document_id)
    return qs


def build_abandoned_display_only_extra_cleanup_report(
    *,
    stale_hours: int,
    document_id: int | None = None,
) -> AbandonedDisplayOnlyExtraCleanupReport:
    if stale_hours < 1:
        raise ValueError("stale_hours must be >= 1.")

    cutoff = timezone.now() - timedelta(hours=stale_hours)
    report = AbandonedDisplayOnlyExtraCleanupReport(
        stale_hours=stale_hours,
        document_id_filter=document_id,
        bucket=_uploads_bucket_name(),
    )
    for source in abandoned_display_only_extras_queryset(
        stale_hours=stale_hours,
        document_id=document_id,
    ):
        if not is_abandoned_display_only_extra(
            source, source.document, cutoff=cutoff
        ):
            continue
        report.candidates.append(
            AbandonedDisplayOnlyExtraCandidate(
                document_id=source.document_id,
                source_file_id=source.pk,
                order_index=source.order_index,
                upload_status=source.upload_status,
                updated_at=source.updated_at.isoformat(),
                s3_key=(source.file_s3_key or "").strip(),
            )
        )
    return report


def apply_abandoned_display_only_extra_cleanup(
    report: AbandonedDisplayOnlyExtraCleanupReport,
) -> AbandonedDisplayOnlyExtraCleanupApplyResult:
    bucket = report.bucket
    if not bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot delete S3 objects."
        )

    result = AbandonedDisplayOnlyExtraCleanupApplyResult(
        source_files_deleted=0,
        s3_keys_deleted=0,
        s3_keys_not_found=0,
    )
    cutoff = timezone.now() - timedelta(hours=report.stale_hours)

    for candidate in report.candidates:
        with transaction.atomic():
            document = (
                Document.objects.select_for_update()
                .filter(pk=candidate.document_id)
                .first()
            )
            if document is None:
                continue
            source = (
                DocumentSourceFile.objects.select_for_update()
                .filter(pk=candidate.source_file_id, document_id=document.id)
                .first()
            )
            if source is None:
                continue
            if not is_abandoned_display_only_extra(
                source, document, cutoff=cutoff
            ):
                continue

            s3_key = (source.file_s3_key or "").strip()
            if s3_key:
                try:
                    delete_result = delete_s3_object(bucket, s3_key)
                except Exception as exc:
                    result.s3_delete_failures.append(
                        DisplayOnlyExtraS3DeleteFailure(
                            document_id=document.pk,
                            source_file_id=source.pk,
                            s3_key=s3_key,
                            error=_format_s3_delete_error(exc),
                        )
                    )
                    continue
                if delete_result.deleted:
                    result.s3_keys_deleted += 1
                elif delete_result.not_found:
                    result.s3_keys_not_found += 1

            source.delete()
            result.source_files_deleted += 1

    return result
