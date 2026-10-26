"""Public registry lookup for NonPersonEntity.

Name lookup only. This does not read occurrences, authorize archive items,
or write ``ArchiveItemSearchIndex``. Matching is case-insensitive exact,
prefix, or contains, after long dashes are folded to ASCII hyphens. There
is no fuzzy match and no result cap. Blank-query order is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import (
    Case,
    Exists,
    F,
    Func,
    IntegerField,
    OuterRef,
    Q,
    QuerySet,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Lower, NullIf, Trim

from documents.models import NonPersonEntity, NonPersonEntityAlias
from documents.services.hebrew_display_order import (
    LONG_DASH_CHARACTERS,
    fold_long_dashes,
)
from documents.services.non_person_entity_correction import exclude_retired_entities
from documents.services.non_person_entity_presentation import (
    non_person_public_page_url,
    non_person_public_type_label,
    non_person_reader_display_name,
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
    """One registry index result. No occurrence or review fields.

    Association fields are filled by the public directory for the current
    page. Search lookup leaves them empty.
    """

    entity_id: int
    name: str
    href: str
    type_label: str
    matched_alias: str
    sort_name: str = ""
    association_count: int = 0
    association_summary: str = ""


def registry_index_queryset(query: str) -> QuerySet[NonPersonEntity]:
    """Registry rows for ``query``.

    A blank query is every registry row, ordered by public name then pk.
    A nonempty query keeps one row per entity at its best rank:
    exact display, exact canonical, exact alias, prefix name, prefix alias,
    contains name, contains alias. Ties use public name, then pk.
    """

    cleaned = (query or "").strip()
    queryset = exclude_retired_entities(
        NonPersonEntity.objects.annotate(public_sort_name=_public_sort_name())
    )
    if not cleaned:
        return queryset.order_by("public_sort_name", "pk")
    folded_query = fold_long_dashes(cleaned)
    return (
        queryset.annotate(
            folded_display=_long_dash_folded(F("display_name")),
            folded_canonical=_long_dash_folded(F("canonical_name")),
        )
        .annotate(match_rank=_match_rank(folded_query))
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
            name=non_person_reader_display_name(entity),
            href=non_person_public_page_url(entity.pk),
            type_label=non_person_public_type_label(entity),
            matched_alias=matched_aliases.get(entity.pk, ""),
        )
        for entity in entities
    )


def _public_sort_name():
    """Lowercased public name: nonblank display_name, otherwise canonical_name."""

    return Lower(Coalesce(NullIf(Trim("display_name"), Value("")), F("canonical_name")))


class _Translate(Func):
    """PostgreSQL ``translate``: one pass, not a nested ``replace`` chain."""

    function = "TRANSLATE"
    arity = 3


_LONG_DASH_FROM = "".join(LONG_DASH_CHARACTERS)
_LONG_DASH_TO = "-" * len(LONG_DASH_CHARACTERS)


def _long_dash_folded(expression):
    """SQL fold of the same long dashes ``fold_long_dashes`` replaces.

    One ``translate`` call maps every long dash to ``-``. The Hebrew maqaf
    is not in the map. This does not change stored text.
    """

    return _Translate(expression, Value(_LONG_DASH_FROM), Value(_LONG_DASH_TO))


def _alias_exists(query: str, lookup: str) -> Exists:
    return Exists(
        NonPersonEntityAlias.objects.annotate(
            folded_name=_long_dash_folded(F("name"))
        ).filter(
            entity_id=OuterRef("pk"),
            **{f"folded_name__{lookup}": query},
        )
    )


def _match_rank(query: str) -> Case:
    return Case(
        When(folded_display__iexact=query, then=Value(_RANK_EXACT_DISPLAY)),
        When(folded_canonical__iexact=query, then=Value(_RANK_EXACT_CANONICAL)),
        When(
            condition=_alias_exists(query, "iexact"),
            then=Value(_RANK_EXACT_ALIAS),
        ),
        When(
            Q(folded_display__istartswith=query)
            | Q(folded_canonical__istartswith=query),
            then=Value(_RANK_PREFIX_NAME),
        ),
        When(
            condition=_alias_exists(query, "istartswith"),
            then=Value(_RANK_PREFIX_ALIAS),
        ),
        When(
            Q(folded_display__icontains=query) | Q(folded_canonical__icontains=query),
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
    """Best non-OCR alias that matched, for alias-ranked results only.

    The database returns only aliases of those entities whose dash-folded
    name is an exact, prefix, or contains match. Ranking among that set
    stays exact, then prefix, then contains, then folded name, then pk.
    ``OCR_VARIANT`` stays searchable on the entity and is not a visible label.
    """

    if not entity_ids:
        return {}
    folded_query = fold_long_dashes(query).casefold()
    best: dict[int, tuple[tuple[int, str, int], str]] = {}
    aliases = (
        NonPersonEntityAlias.objects.filter(entity_id__in=entity_ids)
        .exclude(kind=NonPersonEntityAlias.Kind.OCR_VARIANT)
        .annotate(folded_name=_long_dash_folded(F("name")))
        .filter(
            Q(folded_name__iexact=folded_query)
            | Q(folded_name__istartswith=folded_query)
            | Q(folded_name__icontains=folded_query)
        )
    )
    for alias in aliases:
        folded_name = fold_long_dashes(alias.name).casefold()
        if folded_name == folded_query:
            rank = _RANK_EXACT_ALIAS
        elif folded_name.startswith(folded_query):
            rank = _RANK_PREFIX_ALIAS
        elif folded_query in folded_name:
            rank = _RANK_CONTAINS_ALIAS
        else:
            continue
        key = (rank, folded_name, alias.pk)
        current = best.get(alias.entity_id)
        if current is None or key < current[0]:
            best[alias.entity_id] = (key, fold_long_dashes(alias.name))
    return {entity_id: name for entity_id, (_key, name) in best.items()}
