"""Presigned URL helpers for public PHOTO archive browse surfaces."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from botocore.exceptions import BotoCoreError, ClientError

from documents.models import ArchiveItem
from documents.services.archive_item_presentation import ArchiveBrowseCard
from documents.s3 import create_presigned_get

logger = logging.getLogger(__name__)


def apply_photo_thumbnail_urls_to_browse_cards(
    cards: Sequence[ArchiveBrowseCard],
    *,
    bucket: str,
    expires_in: int = 3600,
) -> list[ArchiveBrowseCard]:
    """Attach optional presigned thumbnail URLs to PHOTO browse cards.

    Presigns ``PhotoContent.thumbnail_file_key`` only. Never uses
    ``original_file_key``. When the bucket is unset, the key is empty, or
    presigning fails, ``thumbnail_url`` remains ``None`` and callers should
    keep the CSS type marker fallback.
    """
    normalized_bucket = (bucket or "").strip()
    if not normalized_bucket:
        return list(cards)

    enriched: list[ArchiveBrowseCard] = []
    for card in cards:
        if card.item.item_type != ArchiveItem.ItemType.PHOTO:
            enriched.append(card)
            continue

        photo_content = getattr(card.item, "photo_content", None)
        thumbnail_key = (
            (photo_content.thumbnail_file_key or "").strip()
            if photo_content is not None
            else ""
        )
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
                "photo browse thumbnail presign failed",
                exc_info=True,
                extra={
                    "archive_item_id": card.item.pk,
                    "thumbnail_file_key": thumbnail_key,
                },
            )
            enriched.append(card)
            continue

        enriched.append(replace(card, thumbnail_url=thumbnail_url))
    return enriched
