"""Public non-person directory layout, type filter, and page-scoped counts.

Eligibility stays the non-retired registry, including rows with no valid pin.
Blank-query order is the displayed public name via ``hebrew_public_sort_key``,
then entity id. A search query keeps the existing match rank. A repeated
letter in that rank gets its own section id. Counts are distinct authorized
archive items for the current page only, using the same pin validity as the
public entity detail page. A pin that still points at a retired id is not
added to the successor. Archive cards follow that pin to the successor.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from urllib.parse import urlencode

from documents.models import ArchiveItem, ArchiveItemEntityOccurrence, NonPersonEntity
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    normalize_archive_public_list_page,
)
from documents.services.hebrew_display_order import hebrew_public_sort_key
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    authoritative_text_context_for_item,
    occurrence_is_currently_valid,
)
from documents.services.non_person_entity_presentation import (
    non_person_entity_type_label,
    non_person_reader_display_name,
)
from documents.services.non_person_entity_search import (
    PublicRegistryRow,
    public_registry_rows,
    registry_index_queryset,
)
from documents.services.public_holdings import (
    EMPTY_PUBLIC_HOLDINGS,
    PublicHoldingsCounts,
    holdings_from_item_type_pairs,
)
from documents.services.public_people_directory import (
    OTHER_INDEX_HEADING,
    OTHER_INDEX_LETTER,
    directory_index_letter,
    first_directory_letter_pages,
)
from documents.services.text_presentation import (
    archive_item_displayable_text_results_prefetch,
)

ENTITY_DIRECTORY_ALL_TYPES_LABEL = "כל הסוגים"
ENTITY_DIRECTORY_ZERO_COUNT = "0"


@dataclass(frozen=True, slots=True)
class PublicEntityLetterGroup:
    """One letter section of the current entity-directory page."""

    letter: str
    heading: str
    heading_id: str
    rows: tuple[PublicRegistryRow, ...]


def normalize_public_entity_type_filter(raw: str | None) -> str:
    """Return a known entity type, or blank when the value is absent or invalid."""

    value = (raw or "").strip()
    if value in NonPersonEntity.EntityType.values:
        return value
    return ""


def public_entity_type_filter_choices() -> tuple[tuple[str, str], ...]:
    """Existing entity types with their public Hebrew labels. No subtypes."""

    choices: list[tuple[str, str]] = []
    for entity_type in NonPersonEntity.EntityType.values:
        label = non_person_entity_type_label(entity_type)
        if label:
            choices.append((entity_type, label))
    return tuple(choices)


def entity_letter_anchor_id(letter: str) -> str:
    if letter == OTHER_INDEX_LETTER:
        return "entity-letter-other"
    return f"entity-letter-{letter}"


def group_entity_directory_rows(
    rows: list[PublicRegistryRow],
) -> list[PublicEntityLetterGroup]:
    """Group an already-ordered page by index letter. Does not re-sort.

    Consecutive rows with the same letter form one section. A ranked search
    can repeat a letter later on the same page. The first section keeps
    ``entity-letter-<letter>`` so the alphabet jump still lands there. Each
    later repeat gets a distinct id, ``entity-letter-<letter>-2`` and so on.
    A blank query is alphabetical, so each letter has one section.
    """

    groups: list[PublicEntityLetterGroup] = []
    seen: dict[str, int] = {}
    current_letter = ""
    current_rows: list[PublicRegistryRow] = []
    for row in rows:
        letter = directory_index_letter(row.sort_name or row.name)
        if letter != current_letter and current_rows:
            groups.append(
                _letter_group(
                    letter=current_letter,
                    rows=tuple(current_rows),
                    seen=seen,
                )
            )
            current_rows = []
        current_letter = letter
        current_rows.append(row)
    if current_rows:
        groups.append(
            _letter_group(
                letter=current_letter,
                rows=tuple(current_rows),
                seen=seen,
            )
        )
    return groups


def entity_directory_query(
    *,
    q: str = "",
    entity_type: str = "",
    page: int = 1,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
) -> str:
    """Query string for directory links. Invalid types are already blank."""

    params: list[tuple[str, str]] = []
    if q:
        params.append(("q", q))
    if entity_type:
        params.append(("entity_type", entity_type))
    if page > 1:
        params.append(("page", str(page)))
    if per_page != ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE:
        params.append(("per_page", str(per_page)))
    return urlencode(params)


def entity_directory_href_suffix(
    *,
    q: str = "",
    entity_type: str = "",
    page: int = 1,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
) -> str:
    query = entity_directory_query(
        q=q, entity_type=entity_type, page=page, per_page=per_page
    )
    return f"?{query}" if query else ""


def public_entity_association_counts(
    user,
    entity_ids: list[int],
) -> dict[int, PublicHoldingsCounts]:
    """Distinct authorized items for these entity ids. Missing ids count as zero.

    One occurrence query, one item load, and one authorization query. Text is
    hashed once per item. Stale, unresolved, and unsupported pins are ignored.
    Pins on other entities, including a retired predecessor, are not included.
    """

    if not entity_ids:
        return {}
    occurrences = list(
        ArchiveItemEntityOccurrence.objects.filter(
            entity_id__in=entity_ids,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            normalization_version=SURFACE_V1,
        ).only(
            "id",
            "entity_id",
            "archive_item_id",
            "text_kind",
            "source_text_sha256",
            "normalization_version",
            "resolution_status",
        )
    )
    item_ids = {
        occurrence.archive_item_id
        for occurrence in occurrences
        if occurrence.archive_item_id is not None
    }
    if not item_ids:
        return {}
    items = {
        item.pk: item
        for item in ArchiveItem.objects.filter(pk__in=item_ids)
        .select_related("manual_text_content", "ocr_document")
        .prefetch_related(archive_item_displayable_text_results_prefetch())
    }
    authorized = set(
        archive_browse_queryset_for_user(user)
        .filter(pk__in=item_ids)
        .values_list("pk", flat=True)
    )
    contexts = {}
    pairs: list[tuple[int, int, str]] = []
    for occurrence in occurrences:
        item = items.get(occurrence.archive_item_id)
        if item is None or item.pk not in authorized:
            continue
        occurrence.archive_item = item
        context = contexts.get(item.pk)
        if context is None:
            context = authoritative_text_context_for_item(item)
            contexts[item.pk] = context
        if not occurrence_is_currently_valid(occurrence, context=context):
            continue
        pairs.append((occurrence.entity_id, item.pk, item.item_type))
    return holdings_from_item_type_pairs(pairs)


def build_paginated_public_entity_directory(
    user,
    *,
    search_query: str = "",
    entity_type: str = "",
    page_raw=None,
    per_page: int = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
) -> tuple[list[PublicRegistryRow], int, int, dict[str, int]]:
    """Current page, total, page number, and first page of each index letter."""

    cleaned = (search_query or "").strip()
    selected_type = normalize_public_entity_type_filter(entity_type)
    queryset = registry_index_queryset(cleaned)
    if selected_type:
        queryset = queryset.filter(entity_type=selected_type)
    ordered = list(queryset)
    if not cleaned:
        ordered.sort(
            key=lambda entity: (
                hebrew_public_sort_key(non_person_reader_display_name(entity)),
                entity.pk,
            )
        )
    sort_names = [non_person_reader_display_name(entity) for entity in ordered]
    letter_first_pages = first_directory_letter_pages(sort_names, per_page=per_page)
    total_count = len(ordered)
    page = normalize_archive_public_list_page(
        page_raw,
        total_count=total_count,
        per_page=per_page,
    )
    offset = (page - 1) * per_page
    page_entities = ordered[offset : offset + per_page]
    page_names = sort_names[offset : offset + per_page]
    counts = public_entity_association_counts(
        user, [entity.pk for entity in page_entities]
    )
    rows: list[PublicRegistryRow] = []
    for base, sort_name in zip(
        public_registry_rows(page_entities, cleaned), page_names, strict=True
    ):
        holdings = counts.get(base.entity_id, EMPTY_PUBLIC_HOLDINGS)
        summary = (
            ENTITY_DIRECTORY_ZERO_COUNT
            if holdings.total <= 0
            else holdings.summary
        )
        rows.append(
            replace(
                base,
                sort_name=sort_name,
                association_count=holdings.total,
                association_summary=summary,
            )
        )
    return rows, total_count, page, letter_first_pages


def _letter_group(
    *,
    letter: str,
    rows: tuple[PublicRegistryRow, ...],
    seen: dict[str, int],
) -> PublicEntityLetterGroup:
    heading = OTHER_INDEX_HEADING if letter == OTHER_INDEX_LETTER else letter
    seen[letter] = seen.get(letter, 0) + 1
    heading_id = entity_letter_anchor_id(letter)
    if seen[letter] > 1:
        heading_id = f"{heading_id}-{seen[letter]}"
    return PublicEntityLetterGroup(
        letter=letter,
        heading=heading,
        heading_id=heading_id,
        rows=rows,
    )
