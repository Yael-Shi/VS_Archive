"""Build trusted text-line hover mapping for the public document detail page.

Displayed transcription offsets come from the selected DocumentTextResult and
its stored Transkribus line char ranges. Binding/snapshot trust is gated by
``resolve_trusted_hover_binding``. Per-line geometry is gated by
``resolve_text_range_geometry`` for that line's exact ``[char_start, char_end)``
range. This layer never bypasses those authorities and never fabricates
geometry.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
)
from documents.services.overlay_bbox_percent import page_bbox_to_percent
from documents.services.text_presentation import (
    ResultTypeStr,
    resolve_displayed_transcription_result,
)
from documents.services.transkribus_text_range_geometry import (
    TextRangeLineGeometry,
    resolve_text_range_geometry,
    resolve_trusted_hover_binding,
)


@dataclass(frozen=True)
class TextLineHoverSegment:
    """One contiguous slice of the displayed transcription text."""

    text: str
    hover_line_id: str | None


@dataclass(frozen=True)
class TextLineHoverOverlayTarget:
    """One browser-safe source-image overlay rectangle for a hoverable line."""

    hover_line_id: str
    page_index: int
    left_pct: float
    top_pct: float
    width_pct: float
    height_pct: float


@dataclass(frozen=True)
class TextLineHoverOverlayPage:
    """Hover overlay targets for one renderable source-image page."""

    page_index: int
    targets: tuple[TextLineHoverOverlayTarget, ...]


@dataclass(frozen=True)
class TextLineHoverSingleImageOverlay:
    """Hover overlays for the normal single-image document viewer."""

    page_index: int
    targets: tuple[TextLineHoverOverlayTarget, ...]


@dataclass(frozen=True)
class TextLineHoverPresentation:
    """Fail-closed hover payload for the currently displayed transcription."""

    enabled: bool
    result_type: ResultTypeStr | None
    text_result_id: int | None
    segments: tuple[TextLineHoverSegment, ...]
    overlay_targets: tuple[TextLineHoverOverlayTarget, ...]


_DISABLED = TextLineHoverPresentation(
    enabled=False,
    result_type=None,
    text_result_id=None,
    segments=(),
    overlay_targets=(),
)


def _hover_line_id(geometry: TextRangeLineGeometry) -> str:
    return f"p{geometry.page_index}-o{geometry.order_index}"


def _renderable_page_indexes(
    document: Document,
    *,
    source_preview_items: list[dict],
    content_url: str | None,
) -> tuple[int, ...]:
    """Match the archive-search overlay-page renderability contract."""
    if source_preview_items:
        return tuple(
            int(item["display_number"])
            for item in source_preview_items
            if item.get("url")
        )
    if document.doc_type == Document.DocType.IMAGE and content_url:
        return (1,)
    return ()


def _page_dimensions_by_index(
    text_result: DocumentTextResult,
) -> dict[int, dict]:
    pages = (
        TranskribusSnapshotPage.objects.filter(
            snapshot__text_result_bindings__text_result_id=text_result.pk,
        )
        .values(
            "page_index",
            "image_width",
            "image_height",
        )
        .distinct()
    )
    return {page["page_index"]: page for page in pages}


def _contributing_lines_for_binding(
    binding: TranskribusTextResultBinding,
) -> list[TranskribusSnapshotLine]:
    """Canonical contributing lines from the already-trusted binding snapshot."""
    return list(
        TranskribusSnapshotLine.objects.filter(
            page__snapshot_id=binding.snapshot_id,
            contributes_to_canonical=True,
        )
        .select_related("page")
        .order_by("page__page_index", "order_index")
    )


def _overlay_targets_for_geometry(
    geometry: TextRangeLineGeometry,
    *,
    hover_line_id: str,
    pages_by_index: dict[int, dict],
    renderable_page_indexes: set[int],
) -> tuple[TextLineHoverOverlayTarget, ...] | None:
    """Convert one trusted line to overlay rects, or fail closed for the line."""
    if geometry.page_index not in renderable_page_indexes:
        return None

    page = pages_by_index.get(geometry.page_index)
    if page is None:
        return None

    percents = page_bbox_to_percent(
        min_x=geometry.bbox_min_x,
        min_y=geometry.bbox_min_y,
        max_x=geometry.bbox_max_x,
        max_y=geometry.bbox_max_y,
        image_width=page["image_width"],
        image_height=page["image_height"],
    )
    if percents is None:
        return None

    left_pct, top_pct, width_pct, height_pct = percents
    return (
        TextLineHoverOverlayTarget(
            hover_line_id=hover_line_id,
            page_index=geometry.page_index,
            left_pct=left_pct,
            top_pct=top_pct,
            width_pct=width_pct,
            height_pct=height_pct,
        ),
    )


def _geometry_for_line(
    text_result: DocumentTextResult,
    *,
    binding: TranskribusTextResultBinding,
    line: TranskribusSnapshotLine,
) -> TextRangeLineGeometry | None:
    """Resolve one stored line through the trusted geometry authority."""
    if line.char_end <= line.char_start:
        return None

    geometries = resolve_text_range_geometry(
        text_result,
        start=line.char_start,
        end=line.char_end,
        binding=binding,
    )
    if len(geometries) != 1:
        return None

    geometry = geometries[0]
    if (
        geometry.char_start != line.char_start
        or geometry.char_end != line.char_end
        or geometry.page_index != line.page.page_index
        or geometry.order_index != line.order_index
    ):
        return None
    return geometry


def _build_segments_and_targets(
    text: str,
    text_result: DocumentTextResult,
    *,
    binding: TranskribusTextResultBinding,
    lines: list[TranskribusSnapshotLine],
    pages_by_index: dict[int, dict],
    renderable_page_indexes: set[int],
) -> tuple[tuple[TextLineHoverSegment, ...], tuple[TextLineHoverOverlayTarget, ...]]:
    segments: list[TextLineHoverSegment] = []
    overlay_targets: list[TextLineHoverOverlayTarget] = []
    cursor = 0

    for line in lines:
        if line.char_end <= line.char_start:
            continue
        if line.char_start < cursor or line.char_end > len(text):
            return (), ()

        if line.char_start > cursor:
            segments.append(
                TextLineHoverSegment(
                    text=text[cursor : line.char_start],
                    hover_line_id=None,
                )
            )

        line_text = text[line.char_start : line.char_end]
        geometry = _geometry_for_line(
            text_result,
            binding=binding,
            line=line,
        )
        if geometry is None:
            segments.append(
                TextLineHoverSegment(
                    text=line_text,
                    hover_line_id=None,
                )
            )
        else:
            hover_line_id = _hover_line_id(geometry)
            line_targets = _overlay_targets_for_geometry(
                geometry,
                hover_line_id=hover_line_id,
                pages_by_index=pages_by_index,
                renderable_page_indexes=renderable_page_indexes,
            )
            if line_targets is None:
                segments.append(
                    TextLineHoverSegment(
                        text=line_text,
                        hover_line_id=None,
                    )
                )
            else:
                segments.append(
                    TextLineHoverSegment(
                        text=line_text,
                        hover_line_id=hover_line_id,
                    )
                )
                overlay_targets.extend(line_targets)

        cursor = line.char_end

    if cursor < len(text):
        segments.append(
            TextLineHoverSegment(
                text=text[cursor:],
                hover_line_id=None,
            )
        )

    joined = "".join(segment.text for segment in segments)
    if joined != text:
        return (), ()

    return tuple(segments), tuple(overlay_targets)


def build_text_line_hover_presentation(
    document: Document,
    *,
    source_preview_items: list[dict],
    content_url: str | None,
) -> TextLineHoverPresentation:
    """Derive hoverable displayed-text segments for the selected transcription.

    Binding-level trust failures disable all hover. Invalid/unusable geometry on
    one stored line leaves that slice plain while other independently valid lines
    may remain hoverable. Never fabricates geometry or bypasses the trusted
    binding / ``resolve_text_range_geometry`` authorities.
    """
    text_result = resolve_displayed_transcription_result(document)
    if text_result is None:
        return _DISABLED

    text = text_result.text or ""
    if not text:
        return _DISABLED

    renderable = _renderable_page_indexes(
        document,
        source_preview_items=source_preview_items,
        content_url=content_url,
    )
    if not renderable:
        return _DISABLED

    binding = resolve_trusted_hover_binding(text_result)
    if binding is None:
        return _DISABLED

    lines = _contributing_lines_for_binding(binding)
    if not lines:
        return _DISABLED

    pages_by_index = _page_dimensions_by_index(text_result)
    segments, overlay_targets = _build_segments_and_targets(
        text,
        text_result,
        binding=binding,
        lines=lines,
        pages_by_index=pages_by_index,
        renderable_page_indexes=set(renderable),
    )
    if not segments or not overlay_targets:
        return _DISABLED
    if not any(segment.hover_line_id for segment in segments):
        return _DISABLED

    return TextLineHoverPresentation(
        enabled=True,
        result_type=text_result.result_type,  # type: ignore[arg-type]
        text_result_id=text_result.pk,
        segments=segments,
        overlay_targets=overlay_targets,
    )


def build_text_line_hover_overlay_pages(
    document: Document,
    *,
    source_preview_items: list[dict],
    content_url: str | None,
    overlay_targets: tuple[TextLineHoverOverlayTarget, ...],
) -> tuple[TextLineHoverOverlayPage, ...]:
    """Expose hover overlays only on source pages that are actually renderable."""
    renderable_page_indexes = _renderable_page_indexes(
        document,
        source_preview_items=source_preview_items,
        content_url=content_url,
    )
    if not renderable_page_indexes:
        return ()

    targets_by_page: dict[int, list[TextLineHoverOverlayTarget]] = {
        page_index: [] for page_index in renderable_page_indexes
    }
    for target in overlay_targets:
        page_targets = targets_by_page.get(target.page_index)
        if page_targets is not None:
            page_targets.append(target)

    return tuple(
        TextLineHoverOverlayPage(
            page_index=page_index,
            targets=tuple(targets_by_page[page_index]),
        )
        for page_index in renderable_page_indexes
    )


def apply_text_line_hover_overlay_to_source_previews(
    source_preview_items: list[dict],
    overlay_pages: tuple[TextLineHoverOverlayPage, ...],
) -> list[dict]:
    """Copy source-preview items and attach page-specific hover overlay targets."""
    targets_by_page = {page.page_index: page.targets for page in overlay_pages}
    return [
        {
            **item,
            "text_line_hover_overlay_targets": targets_by_page.get(
                int(item["display_number"]),
                (),
            ),
        }
        for item in source_preview_items
    ]


def build_text_line_hover_single_image_overlay(
    document: Document,
    *,
    content_url: str | None,
    overlay_pages: tuple[TextLineHoverOverlayPage, ...],
) -> TextLineHoverSingleImageOverlay | None:
    """Return page-1 hover overlay data for a renderable single IMAGE document."""
    if document.doc_type != Document.DocType.IMAGE or not content_url:
        return None

    for page in overlay_pages:
        if page.page_index == 1:
            return TextLineHoverSingleImageOverlay(
                page_index=1,
                targets=page.targets,
            )

    return TextLineHoverSingleImageOverlay(
        page_index=1,
        targets=(),
    )


__all__ = [
    "TextLineHoverOverlayPage",
    "TextLineHoverOverlayTarget",
    "TextLineHoverPresentation",
    "TextLineHoverSegment",
    "TextLineHoverSingleImageOverlay",
    "apply_text_line_hover_overlay_to_source_previews",
    "build_text_line_hover_overlay_pages",
    "build_text_line_hover_presentation",
    "build_text_line_hover_single_image_overlay",
]
