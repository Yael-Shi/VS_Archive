"""ArchiveItem foundation helpers for OCR-backed documents."""

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
