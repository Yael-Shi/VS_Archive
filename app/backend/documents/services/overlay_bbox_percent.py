"""Fail-closed conversion of stored PAGE bounding boxes to page-relative percents."""

from __future__ import annotations


def page_bbox_to_percent(
    *,
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    image_width: object,
    image_height: object,
) -> tuple[float, float, float, float] | None:
    """Return ``(left_pct, top_pct, width_pct, height_pct)`` or ``None``.

    Coordinates must be fully inside a page with positive dimensions.
    Degenerate or out-of-bounds boxes fail closed rather than being clamped.
    """
    if (
        image_width is None
        or image_height is None
        or isinstance(image_width, bool)
        or isinstance(image_height, bool)
        or not isinstance(image_width, (int, float))
        or not isinstance(image_height, (int, float))
        or image_width <= 0
        or image_height <= 0
    ):
        return None

    width = float(image_width)
    height = float(image_height)

    if (
        min_x < 0
        or min_y < 0
        or max_x <= min_x
        or max_y <= min_y
        or max_x > width
        or max_y > height
    ):
        return None

    return (
        (min_x / width) * 100,
        (min_y / height) * 100,
        ((max_x - min_x) / width) * 100,
        ((max_y - min_y) / height) * 100,
    )


__all__ = [
    "page_bbox_to_percent",
]
