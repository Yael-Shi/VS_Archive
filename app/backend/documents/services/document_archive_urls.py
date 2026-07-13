"""Presigned URL helpers for public OCR document archive browse surfaces."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from botocore.exceptions import BotoCoreError, ClientError

from documents.models import ArchiveItem, Document
from documents.services.archive_item_presentation import ArchiveBrowseCard
from documents.s3 import create_presigned_get

logger = logging.getLogger(__name__)


def apply_document_thumbnail_urls_to_browse_cards(
    cards: Sequence[ArchiveBrowseCard],
    *,
    bucket: str,
    expires_in: int = 3600,
) -> list[ArchiveBrowseCard]:
    """Attach optional presigned thumbnail URLs to image OCR document browse cards.

    Presigns ``Document.thumbnail_file_key`` only for ``OCR_DOCUMENT`` items whose
    related ``Document`` has ``doc_type=IMAGE``. Never uses ``file_s3_key``. When
    the bucket is unset, the key is empty, or presigning fails, ``thumbnail_url``
    remains ``None`` and callers should keep the CSS type marker fallback.
    """
    normalized_bucket = (bucket or "").strip()
    if not normalized_bucket:
        return list(cards)

    enriched: list[ArchiveBrowseCard] = []
    for card in cards:
        if card.item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
            enriched.append(card)
            continue

        document = getattr(card.item, "ocr_document", None)
        if document is None or document.doc_type != Document.DocType.IMAGE:
            enriched.append(card)
            continue

        thumbnail_key = (document.thumbnail_file_key or "").strip()
        if not thumbnail_key:
            enriched.append(card)
            continue

        try:
            thumbnail_url = create_presigned_get(
                bucket=normalized_bucket,
                key=thumbnail_key,
                expires_in=expires_in,
            )
        except (BotoCoreError, ClientError):
            logger.warning(
                "document browse thumbnail presign failed",
                exc_info=True,
                extra={
                    "archive_item_id": card.item.pk,
                    "document_id": document.pk,
                    "thumbnail_file_key": thumbnail_key,
                },
            )
            enriched.append(card)
            continue

        enriched.append(replace(card, thumbnail_url=thumbnail_url))
    return enriched
