"""Build safe page-relative overlay targets for archive search matches.

The input geometry has already passed the trusted Transkribus binding checks.
This layer adds the page image dimensions needed by the browser and converts
stored PAGE coordinates to percentages.

Invalid or out-of-bounds geometry fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from documents.models import TranskribusSnapshotPage
from documents.services.archive_search_match_ranges import (
    ArchiveSearchGeometryMatch,
)
from documents.services.overlay_bbox_percent import page_bbox_to_percent


class _SnapshotPageDimensionRow(TypedDict):
    snapshot__text_result_bindings__text_result_id: int
    page_index: int
    image_width: int | None
    image_height: int | None


@dataclass(frozen=True)
class ArchiveSearchOverlayTarget:
    """One browser-safe overlay rectangle for one matched Transkribus line."""

    match_index: int
    term: str
    page_index: int
    left_pct: float
    top_pct: float
    width_pct: float
    height_pct: float


def build_archive_search_overlay_targets(
    matches: tuple[ArchiveSearchGeometryMatch, ...],
) -> tuple[ArchiveSearchOverlayTarget, ...]:
    """Convert trusted search geometry to page-relative percentages.

    Every geometry row must resolve to exactly one stored snapshot page with
    positive dimensions. Coordinates must be fully inside that page.

    A malformed match is omitted rather than approximated.
    """
    targets: list[ArchiveSearchOverlayTarget] = []

    text_result_ids = {match.text_result.pk for match in matches if match.geometry}

    pages_by_text_result_and_index: dict[
        tuple[int, int], _SnapshotPageDimensionRow
    ] = {}

    if text_result_ids:
        page_rows = (
            TranskribusSnapshotPage.objects.filter(
                snapshot__text_result_bindings__text_result_id__in=text_result_ids,
            )
            .values(
                "snapshot__text_result_bindings__text_result_id",
                "page_index",
                "image_width",
                "image_height",
            )
            .distinct()
        )

        for values_row in page_rows:
            text_result_id = values_row[
                "snapshot__text_result_bindings__text_result_id"
            ]
            page_index = values_row["page_index"]
            image_width = values_row["image_width"]
            image_height = values_row["image_height"]
            if not isinstance(text_result_id, int) or isinstance(text_result_id, bool):
                continue
            if not isinstance(page_index, int) or isinstance(page_index, bool):
                continue
            if image_width is not None and (
                not isinstance(image_width, int) or isinstance(image_width, bool)
            ):
                continue
            if image_height is not None and (
                not isinstance(image_height, int) or isinstance(image_height, bool)
            ):
                continue
            pages_by_text_result_and_index[(text_result_id, page_index)] = {
                "snapshot__text_result_bindings__text_result_id": text_result_id,
                "page_index": page_index,
                "image_width": image_width,
                "image_height": image_height,
            }

    for match_index, match in enumerate(matches):
        match_targets: list[ArchiveSearchOverlayTarget] = []

        for geometry in match.geometry:
            dimension_row = pages_by_text_result_and_index.get(
                (match.text_result.pk, geometry.page_index)
            )

            if dimension_row is None:
                match_targets = []
                break

            percents = page_bbox_to_percent(
                min_x=geometry.bbox_min_x,
                min_y=geometry.bbox_min_y,
                max_x=geometry.bbox_max_x,
                max_y=geometry.bbox_max_y,
                image_width=dimension_row["image_width"],
                image_height=dimension_row["image_height"],
            )
            if percents is None:
                match_targets = []
                break

            left_pct, top_pct, width_pct, height_pct = percents
            match_targets.append(
                ArchiveSearchOverlayTarget(
                    match_index=match_index,
                    term=match.term,
                    page_index=geometry.page_index,
                    left_pct=left_pct,
                    top_pct=top_pct,
                    width_pct=width_pct,
                    height_pct=height_pct,
                )
            )

        targets.extend(match_targets)

    return tuple(targets)


__all__ = [
    "ArchiveSearchOverlayTarget",
    "build_archive_search_overlay_targets",
]
