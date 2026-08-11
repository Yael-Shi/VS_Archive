"""Map trusted archive-search overlay targets onto rendered source-image pages."""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import Document
from documents.services.archive_search_overlay_payload import (
    ArchiveSearchOverlayTarget,
)
from documents.services.source_image_renderability import (
    renderable_source_page_indexes,
)


@dataclass(frozen=True)
class ArchiveSearchOverlayPage:
    """One rendered source-image page and its trusted search overlay targets."""

    page_index: int
    targets: tuple[ArchiveSearchOverlayTarget, ...]


def build_archive_search_overlay_pages(
    document: Document,
    *,
    source_preview_items: list[dict],
    content_url: str | None,
    overlay_targets: tuple[ArchiveSearchOverlayTarget, ...],
) -> tuple[ArchiveSearchOverlayPage, ...]:
    """Return overlay payload only for source pages that are actually renderable.

    Multi-image documents use SourcePreview.display_number, whose contract is
    ``order_index + 1`` and therefore matches the 1-based Transkribus page index.

    Single IMAGE documents render through ``content_url`` and map only to page 1.

    PDF and other document types deliberately expose no overlay pages here.
    """

    renderable_page_indexes = renderable_source_page_indexes(
        document,
        source_preview_items=source_preview_items,
        content_url=content_url,
    )

    if not renderable_page_indexes:
        return ()

    targets_by_page: dict[int, list[ArchiveSearchOverlayTarget]] = {
        page_index: [] for page_index in renderable_page_indexes
    }

    for target in overlay_targets:
        page_targets = targets_by_page.get(target.page_index)
        if page_targets is not None:
            page_targets.append(target)

    return tuple(
        ArchiveSearchOverlayPage(
            page_index=page_index,
            targets=tuple(targets_by_page[page_index]),
        )
        for page_index in renderable_page_indexes
    )


__all__ = [
    "ArchiveSearchOverlayPage",
    "build_archive_search_overlay_pages",
]
