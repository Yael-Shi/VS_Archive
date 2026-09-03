"""Document-level base quality for Arabic printed banded OCR SOURCE_TEXT.

Pure scorer over completed banded ``page_quality`` values plus assembled text.
Does not query checkpoints, status, engine names, or other routes.
"""

from __future__ import annotations

from collections.abc import Sequence

from documents.models import ArabicPrintedOcrPageCheckpoint, DocumentTextResult

_UNCLEAR_TOKEN = "[UNCLEAR]"
_VALID_PAGE_QUALITIES = frozenset(ArabicPrintedOcrPageCheckpoint.PageQuality.values)
_LQ = ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY
_UNASSISTED = ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED


def quality_from_banded_page_qualities(
    page_qualities: Sequence[str | None],
    *,
    assembled_text: str | None,
) -> str:
    """Return a persisted ``DocumentTextResult.Quality`` value.

    UNKNOWN: empty page evidence, any missing/blank/unknown page_quality,
    or missing/blank/whitespace-only assembled source text.
    LOW: at least half of pages are CLOUD_VISION_LOW_QUALITY (``2 * lq >= n``).
    GOOD: every page is UNASSISTED and assembled text has no ``[UNCLEAR]``.
    MEDIUM: every other valid completed case (ASSISTED, MIXED, minority LQ,
    all-UNASSISTED text containing ``[UNCLEAR]``).
    """
    if not page_qualities:
        return DocumentTextResult.Quality.UNKNOWN
    if assembled_text is None or not str(assembled_text).strip():
        return DocumentTextResult.Quality.UNKNOWN

    normalized: list[str] = []
    for value in page_qualities:
        if value is None:
            return DocumentTextResult.Quality.UNKNOWN
        token = str(value).strip()
        if not token or token not in _VALID_PAGE_QUALITIES:
            return DocumentTextResult.Quality.UNKNOWN
        normalized.append(token)

    total_pages = len(normalized)
    lq_count = sum(1 for token in normalized if token == _LQ)
    if 2 * lq_count >= total_pages:
        return DocumentTextResult.Quality.LOW

    all_unassisted = all(token == _UNASSISTED for token in normalized)
    if all_unassisted and _UNCLEAR_TOKEN not in assembled_text:
        return DocumentTextResult.Quality.GOOD
    return DocumentTextResult.Quality.MEDIUM
