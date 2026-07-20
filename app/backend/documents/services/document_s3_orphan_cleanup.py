"""Document-specific wrapper around the shared S3 orphan cleanup service."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from documents.models import (
    Document,
    DocumentSourceFile,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.s3 import (
    build_transkribus_snapshot_page_xml_s3_key,
    delete_s3_object,
)
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
# Snapshot PAGE XML keys are collected separately with status + exact-key-identity
# rules (READY always; recent PENDING_UPLOAD only; FAILED residuals orphan-eligible).
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

# PENDING_UPLOAD snapshot PAGE XML keys are protected only while the snapshot row
# is younger than this window. Stale PENDING keys become orphan-eligible under the
# command's existing object-age safeguards. This does not change DB status.
TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS = 24

# Preserve the existing public names used by the command and tests.
DocumentS3OrphanCandidate = S3OrphanCandidate
DocumentS3OrphanCleanupReport = S3OrphanCleanupReport
DocumentS3OrphanCleanupApplyResult = S3OrphanCleanupApplyResult


def normalize_and_validate_s3_prefix(prefix: str) -> str:
    return normalize_shared_s3_prefix(
        prefix,
        root_prefix=_DOCUMENTS_PREFIX_ROOT,
    )


def collect_referenced_transkribus_snapshot_page_xml_s3_keys(
    *,
    now=None,
) -> set[str]:
    """Return protected snapshot PAGE XML keys under documents/.

    Protection rules:

    * ``READY``: protect when stored key exactly equals the deterministic key built
      from ``(snapshot.document_id, snapshot_id, page_index)``.
    * ``PENDING_UPLOAD``: same exact-key rule, but only while
      ``created_at`` is newer than
      ``TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS``.
    * ``FAILED``: never protected (age-eligible orphan candidates).

    Syntactically valid keys that belong to another document/snapshot/page are
    not protected. Stale PENDING DB status is left unchanged.
    """
    reference_now = now if now is not None else timezone.now()
    pending_cutoff = reference_now - timedelta(
        hours=TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS
    )

    rows = TranskribusSnapshotPage.objects.filter(
        Q(
            snapshot__storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        | Q(
            snapshot__storage_status=(
                TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD
            ),
            snapshot__created_at__gt=pending_cutoff,
        )
    ).values_list(
        "page_xml_s3_key",
        "snapshot__document_id",
        "snapshot_id",
        "page_index",
    )

    keys: set[str] = set()
    for raw_key, document_id, snapshot_id, page_index in rows:
        normalized = (raw_key or "").strip()
        if not normalized:
            continue
        try:
            expected = build_transkribus_snapshot_page_xml_s3_key(
                document_id=int(document_id),
                snapshot_id=int(snapshot_id),
                page_index=int(page_index),
            )
        except (TypeError, ValueError):
            continue
        if normalized == expected:
            keys.add(normalized)
    return keys


def collect_referenced_document_s3_keys(*, now=None) -> set[str]:
    keys: set[str] = set()

    for model_name, field_name in DOCUMENT_S3_REFERENCE_FIELDS:
        model = _DOCUMENT_S3_REFERENCE_MODELS[model_name]

        for key in model.objects.values_list(field_name, flat=True):
            normalized = (key or "").strip()
            if normalized:
                keys.add(normalized)

    keys.update(collect_referenced_transkribus_snapshot_page_xml_s3_keys(now=now))
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
