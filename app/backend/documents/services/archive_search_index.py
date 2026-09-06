"""ArchiveItem search-index builder, persistence, and explicit write-path sync.

Pure builder returns a value object only. Persistence materializes
``ArchiveItemSearchIndex`` including the weighted PostgreSQL ``search_vector``.

PR2a adds id-based synchronization for discovery/manual/taxonomy writers.
Displayed OCR mutation hooks are deferred to PR2b.
PHOTO search aggregation indexes public-renderable PhotoContent descriptive
text, PhotoPerson canonical names, and PersonAlias names onto the owning
ArchiveItem (one result per item).
``ArchiveItemPerson`` canonical names and aliases are item-level metadata for
every item type; they are not photo-appearance search.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.contrib.postgres.search import SearchVector
from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.db.models.expressions import CombinedExpression

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_authors import searchable_author_names_for_item
from documents.services.photo_metadata_validation import PHOTO_METADATA_FIELD_NAMES
from documents.services.photo_presentation import photo_is_archive_renderable
from documents.services.text_presentation import (
    archive_item_displayable_text_results_prefetch,
    get_displayed_hebrew_translation_text,
    get_displayed_transcription_text,
)

# Separates unrelated field segments so adjacent values do not concatenate.
SEARCH_SEGMENT_SEPARATOR = "\n"

SEARCH_VECTOR_CONFIG = "simple"


def _search_person_aliases_prefetch() -> Prefetch:
    return Prefetch(
        "aliases",
        queryset=PersonAlias.objects.order_by("name", "id"),
    )


def _search_people_prefetch() -> Prefetch:
    """Person rows plus aliases in deterministic ``(name, id)`` order.

    Used for both item-level ``ArchiveItem.people`` and PHOTO
    ``PhotoContent.people``. Search-index only.
    """
    return Prefetch(
        "people",
        queryset=Person.objects.order_by("name", "id").prefetch_related(
            _search_person_aliases_prefetch()
        ),
    )


@dataclass(frozen=True)
class ArchiveItemSearchContent:
    """Derived searchable plain text for one ArchiveItem (no DB / no tsvector)."""

    archive_item_id: int
    title_text: str
    metadata_text: str
    body_text: str
    hebrew_translation_text: str


def archive_items_for_search_index_build(
    *,
    archive_item_ids: Iterable[int] | None = None,
) -> QuerySet[ArchiveItem]:
    """
    Queryset with relations the pure builder expects (avoids N+1).

    Callers must use this (or equivalent select/prefetch) before
    ``build_archive_item_search_content``. Displayable OCR rows use the same
    prefetch contract as browse cards / ``get_displayed_transcription_text``.
    PHOTO items prefetch every ``PhotoContent`` plus identified ``people``
    and each person's ``aliases``, ordered by ``(position, id)`` then
    Person ``(name, id)``. Item-level ``ArchiveItem.people`` (ArchiveItemPerson)
    is prefetched the same way for every item type. Ordered ``author_links``
    (with ``Author``) feed public author discovery text. This alias prefetch is
    search-index only; public gallery/access querysets must not load aliases.
    The builder then keeps only PhotoContent rows that pass
    ``photo_is_archive_renderable`` (same public-gallery contract).
    ``ArchiveItemPerson`` does not use that renderability gate.
    """
    qs = ArchiveItem.objects.select_related(
        "manual_text_content",
        "ocr_document",
    ).prefetch_related(
        "categories",
        "events",
        "tags",
        Prefetch(
            "author_links",
            queryset=ArchiveItemAuthor.objects.select_related("author").order_by(
                "position", "id"
            ),
        ),
        _search_people_prefetch(),
        archive_item_displayable_text_results_prefetch(),
        Prefetch(
            "photo_contents",
            queryset=PhotoContent.objects.order_by("position", "id").prefetch_related(
                _search_people_prefetch(),
            ),
        ),
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


def _unique_normalized_segments(values: Iterable[str]) -> list[str]:
    """First-occurrence segment list after empty values are dropped."""
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _related_rows(archive_item: ArchiveItem, relation: str) -> list:
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and relation in cache:
        return list(cache[relation])
    return list(getattr(archive_item, relation).all())


def _sorted_relation_names(archive_item: ArchiveItem, relation: str) -> list[str]:
    names = [
        _normalize_segment(row.name) for row in _related_rows(archive_item, relation)
    ]
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

    # PHOTO / VIDEO / unknown: metadata-only indexing (no body/transcript).
    return ""


def _hebrew_translation_text_for_archive_item(archive_item: ArchiveItem) -> str:
    """Displayed Hebrew translation only; empty for Hebrew docs / non-OCR."""
    if archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        return ""
    document = getattr(archive_item, "ocr_document", None)
    if document is None:
        return ""
    return _normalize_body(get_displayed_hebrew_translation_text(document))


def _photo_contents_for_search(archive_item: ArchiveItem) -> list[PhotoContent]:
    """Public-renderable PhotoContent rows in ``(position, id)`` order.

    Reuses ``photo_is_archive_renderable`` (the public gallery contract):
    ``UPLOADED`` and a non-empty ``original_file_key``. Thumbnail presence
    is irrelevant. PENDING / FAILED / empty-key rows are omitted.
    """
    cache = getattr(archive_item, "_prefetched_objects_cache", None)
    if cache is not None and "photo_contents" in cache:
        rows = list(cache["photo_contents"])
    else:
        rows = list(archive_item.photo_contents.all())
    rows.sort(key=lambda photo: (photo.position, photo.pk))
    return [photo for photo in rows if photo_is_archive_renderable(photo)]


def _person_identity_name_segments(persons: Iterable[Person]) -> list[str]:
    """Canonical Person.name values then aliases, deterministic.

    Distinct Persons ordered by ``(name, id)``. Canonical names come first
    in that order, then ``PersonAlias.name`` values for those same persons
    (same person order, aliases by ``(name, id)``). Outer segment dedupe
    is applied by the caller.
    """
    by_id: dict[int, Person] = {}
    for person in persons:
        by_id[person.pk] = person
    ordered = sorted(by_id.values(), key=lambda person: (person.name, person.pk))
    segments: list[str] = []
    for person in ordered:
        name = _normalize_segment(person.name)
        if name:
            segments.append(name)
    for person in ordered:
        aliases = list(person.aliases.all())
        aliases.sort(key=lambda alias: (alias.name, alias.pk))
        for alias in aliases:
            alias_name = _normalize_segment(alias.name)
            if alias_name:
                segments.append(alias_name)
    return segments


def _archive_item_person_name_segments(archive_item: ArchiveItem) -> list[str]:
    """Item-level ArchiveItemPerson identities for every item type.

    Does not depend on PhotoContent renderability and is not a photo
    appearance. Distinct Persons from ``archive_item.people``.
    """
    return _person_identity_name_segments(_related_rows(archive_item, "people"))


def _photo_person_name_segments(photos: list[PhotoContent]) -> list[str]:
    """Canonical PhotoPerson names then aliases, deterministic.

    Distinct Persons attached to the given (already renderable) photos.
    Does not read ArchiveItemPerson.
    """
    persons: list[Person] = []
    for photo in photos:
        persons.extend(photo.people.all())
    return _person_identity_name_segments(persons)


def _photo_search_metadata_segments(archive_item: ArchiveItem) -> list[str]:
    """PHOTO component text for ``metadata_text``; empty for other item types.

    Only public-renderable PhotoContent rows contribute (same helper as
    the public gallery). Canonical Person names and PersonAlias names are
    taken only from those rows.
    Per-photo dates, S3 keys, filenames, MIME, upload status, and other
    technical fields are omitted (ArchiveItem dates are also not in FTS).
    Repeated normalized fragments across photos are kept once (first
    occurrence in ``(position, id)`` then Person ``(name, id)`` order).
    Item-level ``ArchiveItemPerson`` names are assembled separately and are
    not photo-appearance text. Item visibility remains query-time on the
    browse queryset.
    """
    if archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        return []
    photos = _photo_contents_for_search(archive_item)
    segments: list[str] = []
    seen: set[str] = set()
    for photo in photos:
        for field_name in PHOTO_METADATA_FIELD_NAMES:
            segment = _normalize_segment(getattr(photo, field_name, ""))
            if segment and segment not in seen:
                seen.add(segment)
                segments.append(segment)
    for name in _photo_person_name_segments(photos):
        if name not in seen:
            seen.add(name)
            segments.append(name)
    return segments


def build_archive_item_search_content(
    archive_item: ArchiveItem,
) -> ArchiveItemSearchContent:
    """
    Pure builder: select and normalize searchable text for ``archive_item``.

    Does not write to the database or materialize ``search_vector``.
    Expects relations from ``archive_items_for_search_index_build`` (or equivalent).

    ``body_text`` is ManualText body or displayed OCR transcription (source /
    original contract via ``get_displayed_transcription_text``).
    ``hebrew_translation_text`` is the current displayed Hebrew translation for
    non-Hebrew OCR only (never concatenated into ``body_text``; empty for
    Hebrew-language documents so mirrored HEBREW/SOURCE is not duplicated).
    PHOTO descriptive fields, PhotoPerson canonical names, and PersonAlias
    names from public-renderable photos are appended to ``metadata_text``
    (weight B, substring) after ArchiveItem discovery fields and
    ``ArchiveItemPerson`` identities. PHOTO ``body_text`` stays empty.
    Author discovery uses ordered ``ArchiveItemAuthor`` names when any links
    exist, else trimmed ``author_name``. ``ArchiveItemPerson`` canonical names
    and aliases apply to every item type and do not depend on photo
    renderability.
    """
    if archive_item.pk is None:
        raise ValueError("archive_item must be saved before building search content")

    title_text = _normalize_segment(archive_item.title)
    metadata_text = _join_segments(
        _unique_normalized_segments(
            [
                *[
                    _normalize_segment(name)
                    for name in searchable_author_names_for_item(archive_item)
                ],
                _normalize_segment(archive_item.source_title),
                *_sorted_relation_names(archive_item, "categories"),
                *_sorted_relation_names(archive_item, "events"),
                *_sorted_relation_names(archive_item, "tags"),
                _normalize_segment(archive_item.public_note),
                *_archive_item_person_name_segments(archive_item),
                *_photo_search_metadata_segments(archive_item),
            ]
        )
    )
    body_text = _body_text_for_archive_item(archive_item)
    hebrew_translation_text = _hebrew_translation_text_for_archive_item(archive_item)

    return ArchiveItemSearchContent(
        archive_item_id=archive_item.pk,
        title_text=title_text,
        metadata_text=metadata_text,
        body_text=body_text,
        hebrew_translation_text=hebrew_translation_text,
    )


def _weighted_search_vector() -> CombinedExpression:
    return (
        SearchVector("title_text", weight="A", config=SEARCH_VECTOR_CONFIG)
        + SearchVector("metadata_text", weight="B", config=SEARCH_VECTOR_CONFIG)
        + SearchVector("body_text", weight="C", config=SEARCH_VECTOR_CONFIG)
        + SearchVector(
            "hebrew_translation_text", weight="C", config=SEARCH_VECTOR_CONFIG
        )
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
                "hebrew_translation_text": content.hebrew_translation_text,
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
    """Build from ``archive_item`` then persist (backfill / sync helper)."""
    content = build_archive_item_search_content(archive_item)
    return persist_archive_item_search_content(content)


def sync_archive_item_search_index(
    archive_item_id: int,
) -> ArchiveItemSearchIndex | None:
    """
    Explicit write-path sync: reload by id, rebuild, persist.

    Accepts only ``archive_item_id`` so callers never pass a stale prefetched
    ``ArchiveItem``. Reloads through ``archive_items_for_search_index_build``.

    Missing-item behavior: if the ArchiveItem row is gone (delete race), return
    ``None`` without writing. All other errors propagate so a surrounding source
    transaction rolls back when sync fails inside it.

    Locks only the ``ArchiveItem`` row (no ``select_for_update`` on nullable
    ``select_related`` joins). Idempotent when source-derived content is unchanged.
    """
    with transaction.atomic():
        locked = (
            ArchiveItem.objects.select_for_update().filter(pk=archive_item_id).first()
        )
        if locked is None:
            return None

        item = archive_items_for_search_index_build(
            archive_item_ids=[archive_item_id]
        ).get()
        return rebuild_archive_item_search_index(item)


def sync_archive_item_search_indexes(
    archive_item_ids: Iterable[int],
) -> list[ArchiveItemSearchIndex]:
    """
    Fan-out sync for taxonomy renames and similar multi-item updates.

    Materializes and deduplicates ids, then syncs in deterministic ascending
    primary-key order inside one ``transaction.atomic()`` (all-or-nothing even
    without a caller outer transaction). Each id still uses
    ``sync_archive_item_search_index`` (nested atomic/savepoint). Missing items
    are skipped (delete races). Other errors propagate and roll back every
    index update from this fan-out.
    """
    ordered_ids = sorted(set(archive_item_ids))

    synced: list[ArchiveItemSearchIndex] = []
    with transaction.atomic():
        for archive_item_id in ordered_ids:
            index = sync_archive_item_search_index(archive_item_id)
            if index is not None:
                synced.append(index)
    return synced


def archive_item_ids_for_person_photo_appearances(person_id: int) -> list[int]:
    """Distinct ArchiveItem ids reached through this Person's PhotoPerson links.

    Does not include ArchiveItemPerson-only relations. One query; no per-photo
    loop. Order is deterministic ascending id (``sync_archive_item_search_indexes``
    also sorts).
    """
    return list(
        PhotoPerson.objects.filter(person_id=person_id)
        .values_list("photo_content__archive_item_id", flat=True)
        .distinct()
        .order_by("photo_content__archive_item_id")
    )


def archive_item_ids_for_person_item_links(person_id: int) -> list[int]:
    """Distinct ArchiveItem ids reached through this Person's ArchiveItemPerson links.

    Does not include PhotoPerson-only relations. One query. Order is
    deterministic ascending id.
    """
    return list(
        ArchiveItemPerson.objects.filter(person_id=person_id)
        .values_list("archive_item_id", flat=True)
        .distinct()
        .order_by("archive_item_id")
    )


def archive_item_ids_for_person_search_refresh(person_id: int) -> list[int]:
    """ArchiveItem ids whose derived search text can include this Person.

    Union of ``ArchiveItemPerson`` item links and ``PhotoPerson`` appearances.
    An item linked both ways is returned once. Order is ascending id.
    """
    return sorted(
        set(archive_item_ids_for_person_item_links(person_id))
        | set(archive_item_ids_for_person_photo_appearances(person_id))
    )
