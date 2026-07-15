"""Document-specific wrapper around the shared S3 orphan cleanup service."""

from __future__ import annotations

from documents.models import Document, DocumentSourceFile
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

# Database fields that may legitimately reference S3 objects under documents/.
DOCUMENT_S3_REFERENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("Document", "file_s3_key"),
    ("Document", "thumbnail_file_key"),
    ("DocumentSourceFile", "file_s3_key"),
)

_DOCUMENT_S3_REFERENCE_MODELS: dict[str, type] = {
    "Document": Document,
    "DocumentSourceFile": DocumentSourceFile,
}

_DOCUMENTS_PREFIX_ROOT = "documents/"

# Preserve the existing public names used by the command and tests.
DocumentS3OrphanCandidate = S3OrphanCandidate
DocumentS3OrphanCleanupReport = S3OrphanCleanupReport
DocumentS3OrphanCleanupApplyResult = S3OrphanCleanupApplyResult


def normalize_and_validate_s3_prefix(prefix: str) -> str:
    return normalize_shared_s3_prefix(
        prefix,
        root_prefix=_DOCUMENTS_PREFIX_ROOT,
    )


def collect_referenced_document_s3_keys() -> set[str]:
    keys: set[str] = set()

    for model_name, field_name in DOCUMENT_S3_REFERENCE_FIELDS:
        model = _DOCUMENT_S3_REFERENCE_MODELS[model_name]

        for key in model.objects.values_list(field_name, flat=True):
            normalized = (key or "").strip()
            if normalized:
                keys.add(normalized)

    return keys


def build_document_s3_orphan_cleanup_report(
    *,
    older_than_hours: int,
    prefix: str = _DOCUMENTS_PREFIX_ROOT,
    limit: int | None = None,
) -> DocumentS3OrphanCleanupReport:
    return build_s3_orphan_cleanup_report(
        older_than_hours=older_than_hours,
        prefix=prefix,
        root_prefix=_DOCUMENTS_PREFIX_ROOT,
        referenced_keys=collect_referenced_document_s3_keys(),
        list_objects=list_s3_objects_under_prefix,
        limit=limit,
    )


def apply_document_s3_orphan_cleanup(
    report: DocumentS3OrphanCleanupReport,
) -> DocumentS3OrphanCleanupApplyResult:
    return apply_s3_orphan_cleanup(
        report,
        delete_object=delete_s3_object,
    )
