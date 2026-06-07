"""Compare and reconcile shared archival fields between Document and ArchiveItem."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from django.db import transaction

from documents.services.archive_items import (
    ARCHIVE_ITEM_SHARED_FIELD_NAMES,
    sync_archive_item_shared_fields_from_document,
)


@dataclass(frozen=True)
class FieldMismatch:
    document_value: Any
    archive_item_value: Any


@dataclass
class ReconciliationRow:
    document_id: int
    archive_item_id: int
    title: str
    mismatches: dict[str, FieldMismatch] = field(default_factory=dict)

    @property
    def visibility_only(self) -> bool:
        return set(self.mismatches) == {"visibility"}


@dataclass
class ReconciliationReport:
    documents_checked: int = 0
    in_sync: int = 0
    with_mismatches: int = 0
    visibility_mismatches: int = 0
    mismatch_counts_by_field: dict[str, int] = field(default_factory=dict)
    mismatched_rows: list[ReconciliationRow] = field(default_factory=list)
    document_id_filter: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "document_id_filter": self.document_id_filter,
            "summary": {
                "documents_checked": self.documents_checked,
                "in_sync": self.in_sync,
                "with_mismatches": self.with_mismatches,
                "visibility_mismatches": self.visibility_mismatches,
                "mismatch_counts_by_field": dict(self.mismatch_counts_by_field),
            },
            "mismatched_rows": [
                {
                    "document_id": row.document_id,
                    "archive_item_id": row.archive_item_id,
                    "title": row.title,
                    "visibility_only": row.visibility_only,
                    "fields": {
                        name: {
                            "document": serialize_reconciliation_value(m.document_value),
                            "archive_item": serialize_reconciliation_value(m.archive_item_value),
                        }
                        for name, m in row.mismatches.items()
                    },
                }
                for row in self.mismatched_rows
            ],
        }


@dataclass
class ApplyResult:
    documents_updated: int = 0
    fields_updated: int = 0
    visibility_skipped_count: int = 0
    include_visibility: bool = False


def serialize_reconciliation_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return value


def compare_shared_fields(document, archive_item) -> dict[str, FieldMismatch]:
    """Return mismatches for ARCHIVE_ITEM_SHARED_FIELD_NAMES (empty if in sync)."""
    mismatches: dict[str, FieldMismatch] = {}
    for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES:
        document_value = getattr(document, name)
        archive_item_value = getattr(archive_item, name)
        if document_value != archive_item_value:
            mismatches[name] = FieldMismatch(
                document_value=document_value,
                archive_item_value=archive_item_value,
            )
    return mismatches


def _ocr_document_queryset(*, document_id: int | None = None):
    from documents.models import ArchiveItem, Document

    qs = (
        Document.objects.filter(
            archive_item__item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        )
        .select_related("archive_item")
        .order_by("id")
    )
    if document_id is not None:
        qs = qs.filter(pk=int(document_id))
    return qs


def build_ocr_shared_field_reconciliation_report(
    *,
    document_id: int | None = None,
) -> ReconciliationReport:
    """Scan OCR_DOCUMENT rows and aggregate shared-field drift."""
    report = ReconciliationReport(document_id_filter=document_id)
    field_counter: Counter[str] = Counter()

    for document in _ocr_document_queryset(document_id=document_id):
        report.documents_checked += 1
        mismatches = compare_shared_fields(document, document.archive_item)
        if not mismatches:
            report.in_sync += 1
            continue

        report.with_mismatches += 1
        if "visibility" in mismatches:
            report.visibility_mismatches += 1
        for name in mismatches:
            field_counter[name] += 1

        report.mismatched_rows.append(
            ReconciliationRow(
                document_id=document.pk,
                archive_item_id=document.archive_item_id,
                title=document.title,
                mismatches=mismatches,
            )
        )

    report.mismatch_counts_by_field = {
        name: field_counter.get(name, 0) for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES
    }
    return report


def _apply_field_names(*, include_visibility: bool) -> tuple[str, ...]:
    if include_visibility:
        return ARCHIVE_ITEM_SHARED_FIELD_NAMES
    return tuple(
        name for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES if name != "visibility"
    )


def apply_ocr_shared_field_reconciliation(
    report: ReconciliationReport,
    *,
    include_visibility: bool = False,
) -> ApplyResult:
    """
    Copy shared fields from Document onto linked ArchiveItem for mismatched rows.

    Never mutates Document or non-shared fields.
    """
    from documents.models import Document

    apply_fields = _apply_field_names(include_visibility=include_visibility)
    result = ApplyResult(include_visibility=include_visibility)

    for row in report.mismatched_rows:
        fields_to_sync = [
            name for name in apply_fields if name in row.mismatches
        ]
        if "visibility" in row.mismatches and not include_visibility:
            result.visibility_skipped_count += 1
        if not fields_to_sync:
            continue

        with transaction.atomic():
            document = Document.objects.select_related("archive_item").get(
                pk=row.document_id
            )
            sync_archive_item_shared_fields_from_document(
                document,
                field_names=fields_to_sync,
            )
        result.documents_updated += 1
        result.fields_updated += len(fields_to_sync)

    return result
