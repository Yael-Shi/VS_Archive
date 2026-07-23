"""ArchiveItem search-index builder and persistence (PR1 foundation).

Pure builder returns a value object only. Persistence materializes
``ArchiveItemSearchIndex`` including the weighted PostgreSQL ``search_vector``.
No write-path hooks live here yet (PR2).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import QuerySet
from django.db.models.expressions import CombinedExpression

from documents.models import ArchiveItem, ArchiveItemSearchIndex
from documents.services.text_presentation import (
    archive_item_displayable_text_results_prefetch,
    get_displayed_transcription_text,
)

# Separates unrelated field segments so adjacent values do not concatenate.
SEARCH_SEGMENT_SEPARATOR = "\n"

SEARCH_VECTOR_CONFIG = "simple"


@dataclass(frozen=True)
class ArchiveItemSearchContent:
    """Derived searchable plain text for one ArchiveItem (no DB / no tsvector)."""

    archive_item_id: int
    title_text: str
    metadata_text: str
    body_text: str


def archive_items_for_search_index_build(
    *,
    archive_item_ids: Iterable[int] | None = None,
) -> QuerySet[ArchiveItem]:
    """
    Queryset with relations the pure builder expects (avoids N+1).

    Callers must use this (or equivalent select/prefetch) before
    ``build_archive_item_search_content``. Displayable OCR rows use the same
    prefetch contract as browse cards / ``get_displayed_transcription_text``.
    """
    qs = ArchiveItem.objects.select_related(
        "manual_text_content",
        "ocr_document",
    ).prefetch_related(
        "categories",
        "events",
        "tags",
        archive_item_displayable_text_results_prefetch(),
    )
    if archive_item_ids is not None:
        qs = qs.filter(pk__in=list(archive_item_ids))
    return qs.order_by("pk")


def _normalize_segment(value: str | None) -> str:
    return " ".join((value or "").split())


def _normalize_body(value: str | None) -> str:
    return (value or "").strip()


def _join_segments(segments: Iterable[str]) -> str:
    return SEARCH_SEGMENT_SEPARATOR.join(segment for segment in segments if segment)


def _sorted_relation_names(archive_item: ArchiveItem, relation: str) -> list[str]:
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and relation in cache:
        rows = list(cache[relation])
    else:
        rows = list(getattr(archive_item, relation).all())
    names = [_normalize_segment(row.name) for row in rows]
    return sorted(name for name in names if name)


def _body_text_for_archive_item(archive_item: ArchiveItem) -> str:
    if archive_item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        content = getattr(archive_item, "manual_text_content", None)
        if content is None:
            return ""
        return _normalize_body(content.body)

    if archive_item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        document = getattr(archive_item, "ocr_document", None)
        if document is None:
            return ""
        return _normalize_body(get_displayed_transcription_text(document))

    return ""


def build_archive_item_search_content(
    archive_item: ArchiveItem,
) -> ArchiveItemSearchContent:
    """
    Pure builder: select and normalize searchable text for ``archive_item``.

    Does not write to the database or materialize ``search_vector``.
    Expects relations from ``archive_items_for_search_index_build`` (or equivalent).
    """
    if archive_item.pk is None:
        raise ValueError("archive_item must be saved before building search content")

    title_text = _normalize_segment(archive_item.title)
    metadata_text = _join_segments(
        [
            _normalize_segment(archive_item.author_name),
            _normalize_segment(archive_item.source_title),
            *_sorted_relation_names(archive_item, "categories"),
            *_sorted_relation_names(archive_item, "events"),
            *_sorted_relation_names(archive_item, "tags"),
            _normalize_segment(archive_item.public_note),
        ]
    )
    body_text = _body_text_for_archive_item(archive_item)

    return ArchiveItemSearchContent(
        archive_item_id=archive_item.pk,
        title_text=title_text,
        metadata_text=metadata_text,
        body_text=body_text,
    )


def _weighted_search_vector() -> CombinedExpression:
    return (
        SearchVector("title_text", weight="A", config=SEARCH_VECTOR_CONFIG)
        + SearchVector("metadata_text", weight="B", config=SEARCH_VECTOR_CONFIG)
        + SearchVector("body_text", weight="C", config=SEARCH_VECTOR_CONFIG)
    )


def persist_archive_item_search_content(
    content: ArchiveItemSearchContent,
) -> ArchiveItemSearchIndex:
    """
    Atomically upsert ``ArchiveItemSearchIndex`` and materialize ``search_vector``.

    Idempotent for unchanged source-derived content.
    """
    with transaction.atomic():
        index, _created = ArchiveItemSearchIndex.objects.update_or_create(
            archive_item_id=content.archive_item_id,
            defaults={
                "title_text": content.title_text,
                "metadata_text": content.metadata_text,
                "body_text": content.body_text,
            },
        )
        ArchiveItemSearchIndex.objects.filter(pk=index.pk).update(
            search_vector=_weighted_search_vector(),
        )
        index.refresh_from_db()
        return index


def rebuild_archive_item_search_index(
    archive_item: ArchiveItem,
) -> ArchiveItemSearchIndex:
    """Build from ``archive_item`` then persist (backfill / future sync helper)."""
    content = build_archive_item_search_content(archive_item)
    return persist_archive_item_search_content(content)
