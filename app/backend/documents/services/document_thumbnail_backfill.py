"""Backfill OCR document thumbnails for uploaded IMAGE documents missing metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from django.conf import settings
from django.db.models import CharField, Func, OuterRef, Subquery

from documents.models import Document, DocumentSourceFile
from documents.services.document_thumbnail import (
    generate_and_persist_document_thumbnail,
)
from documents.services.source_files import get_source_file_for_order

DocumentThumbnailTargetDisposition = Literal[
    "candidate",
    "not_found",
    "ineligible",
    "has_thumbnail",
]

DocumentThumbnailOutcome = Literal["generated", "failed", "skipped", "candidate"]


@dataclass(frozen=True)
class DocumentThumbnailBackfillCandidate:
    document_id: int
    source_file_key: str
    upload_status: str
    doc_type: str


@dataclass(frozen=True)
class DocumentThumbnailBackfillTarget:
    document_id: int
    disposition: DocumentThumbnailTargetDisposition
    reason: str | None = None
    source_file_key: str | None = None
    upload_status: str | None = None
    doc_type: str | None = None
    thumbnail_file_key: str | None = None


@dataclass(frozen=True)
class DocumentThumbnailBackfillDocumentResult:
    document_id: int
    status: DocumentThumbnailOutcome
    reason: str | None = None
    thumbnail_file_key: str | None = None


@dataclass
class DocumentThumbnailBackfillReport:
    document_id_filter: int | None
    limit: int | None
    bucket: str
    candidates: list[DocumentThumbnailBackfillCandidate] = field(default_factory=list)
    target: DocumentThumbnailBackfillTarget | None = None

    @property
    def candidate_count(self) -> int:
        """Number of candidates selected for this invocation (after ``--limit``)."""
        return len(self.candidates)

    def dry_run_results(self) -> list[DocumentThumbnailBackfillDocumentResult]:
        if self.target is not None:
            target = self.target
            if target.disposition == "candidate":
                return [
                    DocumentThumbnailBackfillDocumentResult(
                        document_id=target.document_id,
                        status="candidate",
                    )
                ]
            if target.disposition == "has_thumbnail":
                return [
                    DocumentThumbnailBackfillDocumentResult(
                        document_id=target.document_id,
                        status="skipped",
                        reason=target.reason,
                        thumbnail_file_key=target.thumbnail_file_key,
                    )
                ]
            if target.disposition == "ineligible":
                return [
                    DocumentThumbnailBackfillDocumentResult(
                        document_id=target.document_id,
                        status="skipped",
                        reason=target.reason,
                    )
                ]
            if target.disposition == "not_found":
                return [
                    DocumentThumbnailBackfillDocumentResult(
                        document_id=target.document_id,
                        status="skipped",
                        reason=target.reason,
                    )
                ]
            return []

        return [
            DocumentThumbnailBackfillDocumentResult(
                document_id=row.document_id,
                status="candidate",
            )
            for row in self.candidates
        ]

    def to_json_dict(
        self,
        *,
        mode: Literal["dry-run", "commit"],
        apply_result: DocumentThumbnailBackfillApplyResult | None = None,
    ) -> dict[str, Any]:
        if apply_result is None:
            results = self.dry_run_results()
            generated_count = 0
            skipped_count = sum(1 for row in results if row.status == "skipped")
            failed_count = 0
        else:
            results = apply_result.results
            generated_count = apply_result.generated_count
            skipped_count = apply_result.skipped_count
            failed_count = apply_result.failed_count

        payload: dict[str, Any] = {
            "mode": mode,
            "filters": {
                "document_id": self.document_id_filter,
                "limit": self.limit,
            },
            "bucket": self.bucket,
            "candidate_count": self.candidate_count,
            "generated_count": generated_count,
            "skipped_count": skipped_count,
            "failed_count": failed_count,
            "candidates": [
                {
                    "document_id": row.document_id,
                    "source_file_key": row.source_file_key,
                    "upload_status": row.upload_status,
                    "doc_type": row.doc_type,
                }
                for row in self.candidates
            ],
            "results": [
                {
                    "document_id": row.document_id,
                    "status": row.status,
                    "reason": row.reason,
                    "thumbnail_file_key": row.thumbnail_file_key,
                }
                for row in results
            ],
        }
        if self.target is not None:
            payload["target"] = {
                "document_id": self.target.document_id,
                "disposition": self.target.disposition,
                "reason": self.target.reason,
                "source_file_key": self.target.source_file_key,
                "upload_status": self.target.upload_status,
                "doc_type": self.target.doc_type,
                "thumbnail_file_key": self.target.thumbnail_file_key,
            }
        return payload


@dataclass
class DocumentThumbnailBackfillApplyResult:
    processed_count: int = 0
    generated_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    results: list[DocumentThumbnailBackfillDocumentResult] = field(default_factory=list)


def _uploads_bucket_name() -> str:
    return (getattr(settings, "UPLOADS_BUCKET_NAME", None) or "").strip()


class _StripWhitespace(Func):
    """Strip leading/trailing PostgreSQL POSIX ``[[:space:]]`` characters."""

    template = "REGEXP_REPLACE(%(expressions)s, '^[[:space:]]+|[[:space:]]+$', '', 'g')"
    output_field = CharField()


def _non_empty_stripped(value: str | None) -> str:
    return (value or "").strip()


def _primary_source_eligibility(
    document: Document,
) -> tuple[DocumentSourceFile | None, str | None]:
    primary = get_source_file_for_order(document, 0)
    if primary is None:
        return None, "missing primary source file at order_index=0"
    if primary.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
        return primary, f"primary source upload_status={primary.upload_status}"
    if not _non_empty_stripped(primary.file_s3_key):
        return primary, "missing primary source file_s3_key"
    return primary, None


def _eligibility_reason(document: Document) -> tuple[bool, str | None]:
    if document.doc_type != Document.DocType.IMAGE:
        return False, f"doc_type={document.doc_type}"
    if document.upload_status != Document.UploadStatus.UPLOADED:
        return False, f"upload_status={document.upload_status}"
    if _non_empty_stripped(document.thumbnail_file_key):
        return False, "thumbnail_file_key already set"
    _primary, source_reason = _primary_source_eligibility(document)
    if source_reason is not None:
        return False, source_reason
    return True, None


def _candidate_from_document(document: Document) -> DocumentThumbnailBackfillCandidate:
    annotated_source_key = getattr(document, "_primary_source_file_key", None)
    if annotated_source_key is not None:
        source_key = annotated_source_key
    else:
        primary = get_source_file_for_order(document, 0)
        source_key = _non_empty_stripped(primary.file_s3_key) if primary else ""
    return DocumentThumbnailBackfillCandidate(
        document_id=document.pk,
        source_file_key=source_key,
        upload_status=document.upload_status,
        doc_type=document.doc_type,
    )


def _resolve_document_target(document_id: int) -> DocumentThumbnailBackfillTarget:
    document = Document.objects.filter(pk=document_id).first()
    if document is None:
        return DocumentThumbnailBackfillTarget(
            document_id=document_id,
            disposition="not_found",
            reason="Document does not exist",
        )

    thumbnail_key = _non_empty_stripped(document.thumbnail_file_key)
    if thumbnail_key:
        return DocumentThumbnailBackfillTarget(
            document_id=document_id,
            disposition="has_thumbnail",
            reason="thumbnail_file_key already set",
            thumbnail_file_key=thumbnail_key,
            upload_status=document.upload_status,
            doc_type=document.doc_type,
        )

    eligible, reason = _eligibility_reason(document)
    primary, _source_reason = _primary_source_eligibility(document)
    source_key = _non_empty_stripped(primary.file_s3_key) if primary else None
    if not eligible:
        return DocumentThumbnailBackfillTarget(
            document_id=document_id,
            disposition="ineligible",
            reason=reason,
            upload_status=document.upload_status,
            doc_type=document.doc_type,
            source_file_key=source_key,
        )

    return DocumentThumbnailBackfillTarget(
        document_id=document_id,
        disposition="candidate",
        source_file_key=source_key,
        upload_status=document.upload_status,
        doc_type=document.doc_type,
    )


def _eligible_primary_source_key_subquery():
    return (
        DocumentSourceFile.objects.filter(
            document_id=OuterRef("pk"),
            order_index=0,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )
        .annotate(_key_trimmed=_StripWhitespace("file_s3_key"))
        .exclude(_key_trimmed="")
        .values("_key_trimmed")[:1]
    )


def _backfill_candidates_queryset():
    return (
        Document.objects.annotate(
            _thumbnail_key_trimmed=_StripWhitespace("thumbnail_file_key"),
            _primary_source_file_key=Subquery(_eligible_primary_source_key_subquery()),
        )
        .filter(
            doc_type=Document.DocType.IMAGE,
            upload_status=Document.UploadStatus.UPLOADED,
            _thumbnail_key_trimmed="",
        )
        .exclude(_primary_source_file_key__isnull=True)
        .exclude(_primary_source_file_key="")
        .order_by("pk")
    )


def build_document_thumbnail_backfill_report(
    *,
    document_id: int | None = None,
    limit: int | None = None,
) -> DocumentThumbnailBackfillReport:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer.")

    report = DocumentThumbnailBackfillReport(
        document_id_filter=document_id,
        limit=limit,
        bucket=_uploads_bucket_name(),
    )

    if document_id is not None:
        target = _resolve_document_target(document_id)
        report.target = target
        if target.disposition == "candidate":
            report.candidates.append(
                DocumentThumbnailBackfillCandidate(
                    document_id=target.document_id,
                    source_file_key=target.source_file_key or "",
                    upload_status=target.upload_status
                    or Document.UploadStatus.UPLOADED,
                    doc_type=target.doc_type or Document.DocType.IMAGE,
                )
            )
        return report

    queryset = _backfill_candidates_queryset()
    if limit is not None:
        queryset = queryset[:limit]
    report.candidates = [_candidate_from_document(document) for document in queryset]
    return report


def apply_document_thumbnail_backfill(
    report: DocumentThumbnailBackfillReport,
) -> DocumentThumbnailBackfillApplyResult:
    bucket = report.bucket
    if not bucket:
        raise ValueError(
            "UPLOADS_BUCKET_NAME is not configured; cannot generate document thumbnails."
        )

    apply_result = DocumentThumbnailBackfillApplyResult()

    for candidate in report.candidates:
        document = Document.objects.filter(pk=candidate.document_id).first()
        if document is None:
            apply_result.skipped_count += 1
            apply_result.results.append(
                DocumentThumbnailBackfillDocumentResult(
                    document_id=candidate.document_id,
                    status="skipped",
                    reason="Document no longer exists",
                )
            )
            continue

        thumbnail_key = _non_empty_stripped(document.thumbnail_file_key)
        if thumbnail_key:
            apply_result.skipped_count += 1
            apply_result.results.append(
                DocumentThumbnailBackfillDocumentResult(
                    document_id=document.pk,
                    status="skipped",
                    reason="thumbnail_file_key already set",
                    thumbnail_file_key=thumbnail_key,
                )
            )
            continue

        eligible, reason = _eligibility_reason(document)
        if not eligible:
            apply_result.skipped_count += 1
            apply_result.results.append(
                DocumentThumbnailBackfillDocumentResult(
                    document_id=document.pk,
                    status="skipped",
                    reason=reason,
                )
            )
            continue

        apply_result.processed_count += 1
        thumb_result = generate_and_persist_document_thumbnail(document, bucket=bucket)
        if thumb_result is not None:
            apply_result.generated_count += 1
            apply_result.results.append(
                DocumentThumbnailBackfillDocumentResult(
                    document_id=document.pk,
                    status="generated",
                    thumbnail_file_key=thumb_result.thumbnail_file_key,
                )
            )
        else:
            apply_result.failed_count += 1
            apply_result.results.append(
                DocumentThumbnailBackfillDocumentResult(
                    document_id=document.pk,
                    status="failed",
                    reason="thumbnail generation or persistence failed",
                )
            )

    return apply_result
