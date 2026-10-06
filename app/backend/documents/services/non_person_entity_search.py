"""Public registry lookup for NonPersonEntity.

Name lookup only. This does not read occurrences, authorize archive items,
or write ``ArchiveItemSearchIndex``. Matching is case-insensitive exact,
prefix, or contains. There is no fuzzy match and no result cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import (
    Case,
    Exists,
    F,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf, Trim

from documents.models import NonPersonEntity, NonPersonEntityAlias
from documents.services.non_person_entity_presentation import (
    non_person_public_name,
    non_person_public_page_url,
    non_person_public_type_label,
)

_RANK_EXACT_DISPLAY = 1
_RANK_EXACT_CANONICAL = 2
_RANK_EXACT_ALIAS = 3
_RANK_PREFIX_NAME = 4
_RANK_PREFIX_ALIAS = 5
_RANK_CONTAINS_NAME = 6
_RANK_CONTAINS_ALIAS = 7
_ALIAS_MATCH_RANKS = frozenset(
    {_RANK_EXACT_ALIAS, _RANK_PREFIX_ALIAS, _RANK_CONTAINS_ALIAS}
)


@dataclass(frozen=True)
class PublicRegistryRow:
    """One registry index result. No occurrence or review fields."""

    entity_id: int
    name: str
    href: str
    type_label: str
    matched_alias: str


def registry_index_queryset(query: str) -> QuerySet[NonPersonEntity]:
    """Registry rows for ``query``.

    A blank query is every registry row, ordered by public name then pk.
    A nonempty query keeps one row per entity at its best rank:
    exact display, exact canonical, exact alias, prefix name, prefix alias,
    contains name, contains alias. Ties use public name, then pk.
    """

    cleaned = (query or "").strip()
    queryset = NonPersonEntity.objects.annotate(public_sort_name=_public_sort_name())
    if not cleaned:
        return queryset.order_by("public_sort_name", "pk")
    return (
        queryset.annotate(match_rank=_match_rank(cleaned))
        .filter(match_rank__gt=0)
        .order_by("match_rank", "public_sort_name", "pk")
    )


def public_registry_rows(
    entities: list[NonPersonEntity],
    query: str,
) -> tuple[PublicRegistryRow, ...]:
    """Presentation rows for one already-ordered page of registry matches."""

    cleaned = (query or "").strip()
    alias_ids = [
        entity.pk
        for entity in entities
        if getattr(entity, "match_rank", 0) in _ALIAS_MATCH_RANKS
    ]
    matched_aliases = (
        _best_visible_matched_aliases(alias_ids, cleaned) if cleaned else {}
    )
    return tuple(
        PublicRegistryRow(
            entity_id=entity.pk,
            name=non_person_public_name(entity),
            href=non_person_public_page_url(entity.pk),
            type_label=non_person_public_type_label(entity),
            matched_alias=matched_aliases.get(entity.pk, ""),
        )
        for entity in entities
    )


def _public_sort_name():
    """Lowercased public name: nonblank display_name, otherwise canonical_name."""

    return Lower(Coalesce(NullIf(Trim("display_name"), Value("")), F("canonical_name")))


def _alias_exists(query: str, lookup: str) -> Exists:
    return Exists(
        NonPersonEntityAlias.objects.filter(
            entity_id=OuterRef("pk"),
            **{f"name__{lookup}": query},
        )
    )


def _match_rank(query: str) -> Case:
    return Case(
        When(display_name__iexact=query, then=Value(_RANK_EXACT_DISPLAY)),
        When(canonical_name__iexact=query, then=Value(_RANK_EXACT_CANONICAL)),
        When(
            condition=_alias_exists(query, "iexact"),
            then=Value(_RANK_EXACT_ALIAS),
        ),
        When(
            Q(display_name__istartswith=query) | Q(canonical_name__istartswith=query),
            then=Value(_RANK_PREFIX_NAME),
        ),
        When(
            condition=_alias_exists(query, "istartswith"),
            then=Value(_RANK_PREFIX_ALIAS),
        ),
        When(
            Q(display_name__icontains=query) | Q(canonical_name__icontains=query),
            then=Value(_RANK_CONTAINS_NAME),
        ),
        When(
            condition=_alias_exists(query, "icontains"),
            then=Value(_RANK_CONTAINS_ALIAS),
        ),
        default=Value(0),
        output_field=IntegerField(),
    )


def _best_visible_matched_aliases(
    entity_ids: list[int],
    query: str,
) -> dict[int, str]:
    """Best non-OCR alias that matched, for alias-ranked results only."""

    if not entity_ids:
        return {}
    folded_query = query.casefold()
    best: dict[int, tuple[tuple[int, str, int], str]] = {}
    aliases = (
        NonPersonEntityAlias.objects.filter(entity_id__in=entity_ids)
        .exclude(kind=NonPersonEntityAlias.Kind.OCR_VARIANT)
        .filter(
            Q(name__iexact=query)
            | Q(name__istartswith=query)
            | Q(name__icontains=query)
        )
    )
    for alias in aliases:
        folded_name = alias.name.casefold()
        if folded_name == folded_query:
            rank = _RANK_EXACT_ALIAS
        elif folded_name.startswith(folded_query):
            rank = _RANK_PREFIX_ALIAS
        else:
            rank = _RANK_CONTAINS_ALIAS
        key = (rank, folded_name, alias.pk)
        current = best.get(alias.entity_id)
        if current is None or key < current[0]:
            best[alias.entity_id] = (key, alias.name)
    return {entity_id: name for entity_id, (_key, name) in best.items()}
