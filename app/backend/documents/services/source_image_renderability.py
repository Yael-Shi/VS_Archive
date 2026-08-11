"""Shared renderability contract for source-image presentation."""

from __future__ import annotations

from documents.models import Document


def renderable_source_page_indexes(
    document: Document,
    *,
    source_preview_items: list[dict],
    content_url: str | None,
) -> tuple[int, ...]:
    """Return 1-based source page indexes that are actually renderable.

    Multi-image documents use SourcePreview.display_number, whose contract is
    ``order_index + 1`` and therefore matches the 1-based Transkribus page index.

    Single IMAGE documents render through ``content_url`` and map only to page 1.

    PDF and other document types deliberately expose no source-image pages.
    """
    if source_preview_items:
        return tuple(
            int(item["display_number"])
            for item in source_preview_items
            if item.get("url")
        )

    if document.doc_type == Document.DocType.IMAGE and content_url:
        return (1,)

    return ()


__all__ = ["renderable_source_page_indexes"]
