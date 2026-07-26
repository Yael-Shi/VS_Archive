"""Backfill ArchiveItem discovery metadata from legacy OCR-side Document fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from documents.services.archive_items import get_or_create_archive_category_by_name


@dataclass
class DocumentBackfillRow:
    document_id: int
    archive_item_id: int
    tag_names_to_add: list[str] = field(default_factory=list)
    tag_names_skipped: list[str] = field(default_factory=list)
    category_name: str | None = None
    category_would_be_created: bool = False
    category_link_to_add: bool = False
    category_link_skipped: bool = False


@dataclass
class BackfillReport:
    scanned_ocr_documents: int = 0
    documents_missing_archive_item: int = 0
    documents_with_legacy_tags: int = 0
    tag_links_to_add: int = 0
    tag_links_skipped: int = 0
    documents_with_category_event: int = 0
    categories_to_create: int = 0
    category_links_to_add: int = 0
    category_links_skipped: int = 0
    document_id_filter: int | None = None
    rows: list[DocumentBackfillRow] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "document_id_filter": self.document_id_filter,
            "summary": {
                "scanned_ocr_documents": self.scanned_ocr_documents,
                "documents_missing_archive_item": self.documents_missing_archive_item,
                "documents_with_legacy_tags": self.documents_with_legacy_tags,
                "tag_links_to_add": self.tag_links_to_add,
                "tag_links_skipped": self.tag_links_skipped,
                "documents_with_category_event": self.documents_with_category_event,
                "categories_to_create": self.categories_to_create,
                "category_links_to_add": self.category_links_to_add,
                "category_links_skipped": self.category_links_skipped,
            },
            "rows": [
                {
                    "document_id": row.document_id,
                    "archive_item_id": row.archive_item_id,
                    "tag_names_to_add": row.tag_names_to_add,
                    "tag_names_skipped": row.tag_names_skipped,
                    "category_name": row.category_name,
                    "category_would_be_created": row.category_would_be_created,
                    "category_link_to_add": row.category_link_to_add,
                    "category_link_skipped": row.category_link_skipped,
                }
                for row in self.rows
            ],
        }


@dataclass
class ApplyResult:
    tag_links_added: int = 0
    categories_created: int = 0
    category_links_added: int = 0
    documents_updated: int = 0


def _is_non_blank_category_event(value: str | None) -> bool:
    return bool(value and value.strip())


def _normalized_category_name(value: str) -> str:
    return value.strip()


def _ocr_document_queryset(*, document_id: int | None = None):
    from documents.models import ArchiveItem, Document

    qs = (
        Document.objects.filter(
            archive_item__item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        )
        .select_related("archive_item")
        .prefetch_related(
            "tags_m2m",
            "archive_item__tags",
            "archive_item__categories",
        )
        .order_by("id")
    )
    if document_id is not None:
        qs = qs.filter(pk=int(document_id))
    return qs


def _plan_document_backfill(document) -> DocumentBackfillRow | None:
    archive_item = document.archive_item
    if archive_item is None:
        return None

    existing_tag_ids = {tag.pk for tag in archive_item.tags.all()}
    tag_names_to_add: list[str] = []
    tag_names_skipped: list[str] = []
    for tag in document.tags_m2m.all():
        if tag.pk in existing_tag_ids:
            tag_names_skipped.append(tag.name)
        else:
            tag_names_to_add.append(tag.name)

    category_name: str | None = None
    category_would_be_created = False
    category_link_to_add = False
    category_link_skipped = False

    if _is_non_blank_category_event(document.category_event):
        category_name = _normalized_category_name(document.category_event)
        existing_category_names = {cat.name for cat in archive_item.categories.all()}
        if category_name in existing_category_names:
            category_link_skipped = True
        else:
            category_link_to_add = True
            from documents.models import ArchiveCategory

            category_would_be_created = not ArchiveCategory.objects.filter(
                name=category_name
            ).exists()

    if not tag_names_to_add and not category_link_to_add:
        if not tag_names_skipped and not category_link_skipped:
            return None

    return DocumentBackfillRow(
        document_id=document.pk,
        archive_item_id=archive_item.pk,
        tag_names_to_add=tag_names_to_add,
        tag_names_skipped=tag_names_skipped,
        category_name=category_name,
        category_would_be_created=category_would_be_created,
        category_link_to_add=category_link_to_add,
        category_link_skipped=category_link_skipped,
    )


def build_archive_discovery_metadata_backfill_report(
    *,
    document_id: int | None = None,
) -> BackfillReport:
    """Scan OCR documents and plan legacy tag/category_event backfill onto ArchiveItem."""
    report = BackfillReport(document_id_filter=document_id)
    category_names_to_create: set[str] = set()

    for document in _ocr_document_queryset(document_id=document_id):
        report.scanned_ocr_documents += 1
        if document.archive_item_id is None:
            report.documents_missing_archive_item += 1
            continue

        if document.tags_m2m.exists():
            report.documents_with_legacy_tags += 1

        if _is_non_blank_category_event(document.category_event):
            report.documents_with_category_event += 1

        row = _plan_document_backfill(document)
        if row is None:
            continue

        report.tag_links_to_add += len(row.tag_names_to_add)
        report.tag_links_skipped += len(row.tag_names_skipped)
        if row.category_link_to_add:
            report.category_links_to_add += 1
            if row.category_would_be_created and row.category_name:
                if row.category_name in category_names_to_create:
                    row.category_would_be_created = False
                else:
                    category_names_to_create.add(row.category_name)
        if row.category_link_skipped:
            report.category_links_skipped += 1
        report.rows.append(row)

    report.categories_to_create = len(category_names_to_create)
    return report


def apply_archive_discovery_metadata_backfill(
    report: BackfillReport,
) -> ApplyResult:
    """Copy legacy Document tags and category_event onto linked ArchiveItem discovery fields."""
    from documents.models import Document

    result = ApplyResult()

    for row in report.rows:
        if not row.tag_names_to_add and not row.category_link_to_add:
            continue

        with transaction.atomic():
            document = (
                Document.objects.select_related("archive_item")
                .prefetch_related(
                    "tags_m2m",
                    "archive_item__tags",
                    "archive_item__categories",
                )
                .get(pk=row.document_id)
            )
            archive_item = document.archive_item
            document_updated = False

            existing_tag_ids = {tag.pk for tag in archive_item.tags.all()}
            tags_to_add = [
                tag for tag in document.tags_m2m.all() if tag.pk not in existing_tag_ids
            ]
            if tags_to_add:
                archive_item.tags.add(*tags_to_add)
                result.tag_links_added += len(tags_to_add)
                document_updated = True

            if row.category_link_to_add and _is_non_blank_category_event(
                document.category_event
            ):
                category_name = _normalized_category_name(document.category_event)
                existing_category_names = {
                    cat.name for cat in archive_item.categories.all()
                }
                if category_name not in existing_category_names:
                    category, created = get_or_create_archive_category_by_name(
                        category_name
                    )
                    archive_item.categories.add(category)
                    result.category_links_added += 1
                    if created:
                        result.categories_created += 1
                    document_updated = True

            if document_updated:
                from documents.services.archive_search_index import (
                    sync_archive_item_search_index,
                )

                sync_archive_item_search_index(archive_item.pk)
                result.documents_updated += 1

    return result
