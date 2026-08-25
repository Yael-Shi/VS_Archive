from __future__ import annotations

from django.conf import settings

from documents.models import ArchiveItem
from documents.services.archive_item_access import (
    filter_browse_renderable_archive_items,
)
from documents.services.archive_item_presentation import (
    archive_browse_displayable_text_results_prefetch,
    build_archive_browse_cards,
)
from documents.services.document_archive_urls import (
    apply_document_thumbnail_urls_to_browse_cards,
)
from documents.services.photo_archive_urls import (
    apply_photo_thumbnail_urls_to_browse_cards,
)


HOMEPAGE_ARCHIVE_ITEM_LIMIT = 3


def homepage_archive_cards():
    """Return a small random PUBLIC-only archive showcase for the homepage."""
    queryset = (
        filter_browse_renderable_archive_items(
            ArchiveItem.objects.filter(visibility=ArchiveItem.Visibility.PUBLIC)
        )
        .select_related(
            "manual_text_content",
            "ocr_document",
            "video_content",
        )
        .prefetch_related(
            "photo_contents",
            "categories",
            "events",
            "tags",
            "people",
            archive_browse_displayable_text_results_prefetch(),
        )
    )

    items = list(queryset.order_by("?")[:HOMEPAGE_ARCHIVE_ITEM_LIMIT])

    cards = build_archive_browse_cards(items)
    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    cards = apply_photo_thumbnail_urls_to_browse_cards(
        cards,
        bucket=bucket,
    )
    return apply_document_thumbnail_urls_to_browse_cards(
        cards,
        bucket=bucket,
    )
