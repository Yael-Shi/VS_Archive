"""ArchiveItem helpers for OCR-backed documents and manual text items."""

from __future__ import annotations

from typing import Any

from django.db import transaction

ARCHIVE_ITEM_SHARED_FIELD_NAMES = (
    "title",
    "visibility",
    "date_start",
    "date_end",
    "date_precision",
    "metadata_status",
)


def archive_item_field_values_from_document(document: Any) -> dict[str, Any]:
    """Build ArchiveItem field values copied from a Document (no inference)."""
    return {
        name: getattr(document, name) for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES
    }


@transaction.atomic
def create_ocr_document(**document_kwargs: Any):
    """
    Create an OCR-backed Document with a linked ArchiveItem (item_type=OCR_DOCUMENT).

    Shared archival fields are copied onto ArchiveItem at create time only.
    """
    from documents.models import ArchiveItem, Document

    pending = Document(**document_kwargs)
    archive_values = archive_item_field_values_from_document(pending)

    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        **archive_values,
    )
    return Document.objects.create(archive_item=archive_item, **document_kwargs)


@transaction.atomic
def create_manual_text_archive_item(
    *,
    title: str,
    body: str,
    visibility: str | None = None,
    date_start=None,
    date_end=None,
    date_precision: str | None = None,
    metadata_status: str | None = None,
):
    """
    Create a MANUAL_TEXT ArchiveItem with linked ManualTextContent.

    Does not create Document rows or enqueue processing.
    """
    from documents.models import ArchiveItem, ManualTextContent

    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=visibility or ArchiveItem.Visibility.PRIVATE,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision or ArchiveItem.DatePrecision.UNKNOWN,
        metadata_status=metadata_status or ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
    )
    ManualTextContent.objects.create(archive_item=archive_item, body=body)
    return archive_item


@transaction.atomic
def update_manual_text_archive_item(
    archive_item,
    *,
    title: str,
    body: str,
    visibility: str,
    date_start=None,
    date_end=None,
    date_precision: str,
    metadata_status: str,
):
    """
    Update a MANUAL_TEXT ArchiveItem and its ManualTextContent body.

    Does not create Document rows or enqueue processing.
    """
    from documents.models import ArchiveItem

    if archive_item.item_type != ArchiveItem.ItemType.MANUAL_TEXT:
        raise ValueError("archive item is not MANUAL_TEXT")

    archive_item.title = title
    archive_item.visibility = visibility
    archive_item.date_start = date_start
    archive_item.date_end = date_end
    archive_item.date_precision = date_precision
    archive_item.metadata_status = metadata_status
    archive_item.save()

    content = archive_item.manual_text_content
    content.body = body
    content.save(update_fields=["body", "updated_at"])
    return archive_item
