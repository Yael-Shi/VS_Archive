"""Prepare archive-search overlay data for document-detail rendering."""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import Document
from documents.services.archive_search_overlay_pages import (
    ArchiveSearchOverlayPage,
)


@dataclass(frozen=True)
class ArchiveSearchSingleImageOverlay:
    """Overlay presentation for the normal single-image document viewer."""

    page_index: int
    targets: tuple


def apply_archive_search_overlay_to_source_previews(
    source_preview_items: list[dict],
    overlay_pages: tuple[ArchiveSearchOverlayPage, ...],
) -> list[dict]:
    """Copy source-preview items and attach page-specific overlay targets.

    The original source-preview dictionaries are not mutated.
    """
    targets_by_page = {page.page_index: page.targets for page in overlay_pages}

    return [
        {
            **item,
            "archive_search_overlay_targets": targets_by_page.get(
                int(item["display_number"]),
                (),
            ),
        }
        for item in source_preview_items
    ]


def build_archive_search_single_image_overlay(
    document: Document,
    *,
    content_url: str | None,
    overlay_pages: tuple[ArchiveSearchOverlayPage, ...],
) -> ArchiveSearchSingleImageOverlay | None:
    """Return page-1 overlay data only for a renderable single IMAGE document."""

    if document.doc_type != Document.DocType.IMAGE or not content_url:
        return None

    for page in overlay_pages:
        if page.page_index == 1:
            return ArchiveSearchSingleImageOverlay(
                page_index=1,
                targets=page.targets,
            )

    return ArchiveSearchSingleImageOverlay(
        page_index=1,
        targets=(),
    )


__all__ = [
    "ArchiveSearchSingleImageOverlay",
    "apply_archive_search_overlay_to_source_previews",
    "build_archive_search_single_image_overlay",
]
