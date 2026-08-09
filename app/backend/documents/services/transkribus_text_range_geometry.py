"""Resolve trusted text ranges to stored Transkribus line geometry.

Ranges use Python's half-open convention: ``[start, end)``.

This service is deliberately fail-closed. Geometry is returned only through
the current trustworthy TranskribusTextResultBinding for the displayed
DocumentTextResult. It never falls back to another snapshot owned by the same
document.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import (
    DocumentTextResult,
    TranskribusSnapshotLine,
    TranskribusTextResultBinding,
)
from documents.services.transkribus_binding_freshness import (
    is_binding_trusted_for_hover,
)


@dataclass(frozen=True)
class TextRangeLineGeometry:
    """One stored Transkribus line intersecting a trusted text range."""

    page_index: int
    page_nr: int
    order_index: int
    provider_region_id: str
    provider_line_id: str
    char_start: int
    char_end: int
    polygon_points: tuple[tuple[float, float], ...]
    bbox_min_x: float
    bbox_min_y: float
    bbox_max_x: float
    bbox_max_y: float


def _validated_polygon_points(
    value: object,
) -> tuple[tuple[float, float], ...] | None:
    """Return a numeric non-degenerate polygon shape, otherwise None."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None

    points: list[tuple[float, float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            return None
        x, y = point
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
        ):
            return None
        points.append((float(x), float(y)))

    return tuple(points)


def _line_geometry(
    line: TranskribusSnapshotLine,
) -> TextRangeLineGeometry | None:
    """Convert one stored line to safe consumer geometry, or fail closed."""
    if not line.contributes_to_canonical:
        return None
    if line.char_end <= line.char_start:
        return None
    if not line.coords_valid or not line.has_meaningful_geometry:
        return None

    polygon = _validated_polygon_points(line.polygon_points)
    if polygon is None:
        return None

    bbox_values = (
        line.bbox_min_x,
        line.bbox_min_y,
        line.bbox_max_x,
        line.bbox_max_y,
    )
    if any(value is None for value in bbox_values):
        return None

    min_x, min_y, max_x, max_y = (
        float(value) for value in bbox_values if value is not None
    )
    if max_x <= min_x or max_y <= min_y:
        return None

    return TextRangeLineGeometry(
        page_index=line.page.page_index,
        page_nr=line.page.page_nr,
        order_index=line.order_index,
        provider_region_id=line.provider_region_id,
        provider_line_id=line.provider_line_id,
        char_start=line.char_start,
        char_end=line.char_end,
        polygon_points=polygon,
        bbox_min_x=min_x,
        bbox_min_y=min_y,
        bbox_max_x=max_x,
        bbox_max_y=max_y,
    )


def resolve_text_range_geometry(
    text_result: DocumentTextResult,
    *,
    start: int,
    end: int,
    binding: TranskribusTextResultBinding | None = None,
) -> tuple[TextRangeLineGeometry, ...]:
    """Resolve ``[start, end)`` to trusted intersecting Transkribus lines.

    Invalid ranges, stale/untrusted bindings, separator-only ranges, or any
    intersecting contributing line without safe polygon/bbox geometry return
    an empty tuple.

    Multi-line and multi-page ranges are supported. Returned targets are
    ordered deterministically by page index and line order.
    """
    if isinstance(start, bool) or isinstance(end, bool):
        return ()
    if not isinstance(start, int) or not isinstance(end, int):
        return ()

    text = text_result.text or ""
    if start < 0 or start >= end or end > len(text):
        return ()

    resolved_binding = binding
    if resolved_binding is None:
        resolved_binding = (
            TranskribusTextResultBinding.objects.filter(text_result_id=text_result.pk)
            .select_related("snapshot", "text_result")
            .first()
        )

    if resolved_binding is None:
        return ()
    if not is_binding_trusted_for_hover(
        text_result,
        binding=resolved_binding,
    ):
        return ()

    lines = list(
        TranskribusSnapshotLine.objects.filter(
            page__snapshot_id=resolved_binding.snapshot_id,
            contributes_to_canonical=True,
            char_start__lt=end,
            char_end__gt=start,
        )
        .select_related("page")
        .order_by("page__page_index", "order_index")
    )

    if not lines:
        return ()

    resolved: list[TextRangeLineGeometry] = []
    for line in lines:
        geometry = _line_geometry(line)
        if geometry is None:
            return ()
        resolved.append(geometry)

    return tuple(resolved)


__all__ = [
    "TextRangeLineGeometry",
    "resolve_text_range_geometry",
]
