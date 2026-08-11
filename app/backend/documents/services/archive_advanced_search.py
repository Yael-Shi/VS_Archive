"""Public ``/archive/`` advanced filter contract (normalize, apply, serialize, choices).

Structured filters compose with the existing authorized browse queryset, item-type
filter, and full-text ``q`` search. They operate on ``ArchiveItem`` fields/relations
and must not bypass visibility helpers or replace FTS ranking.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Sequence

from django.db.models import QuerySet

from documents.models import ArchiveCategory, ArchiveEvent, ArchiveItem, Tag

# Match archive date-component year bounds (``archive_date_input``).
_MIN_YEAR = 1
_MAX_YEAR = 9999

ARCHIVE_ADVANCED_FILTER_PARAM_AUTHOR = "author"
ARCHIVE_ADVANCED_FILTER_PARAM_CATEGORY = "category"
ARCHIVE_ADVANCED_FILTER_PARAM_EVENT = "event"
ARCHIVE_ADVANCED_FILTER_PARAM_TAG = "tag"
ARCHIVE_ADVANCED_FILTER_PARAM_YEAR = "year"
ARCHIVE_ADVANCED_FILTER_PARAM_YEAR_TO = "year_to"


@dataclass(frozen=True)
class ArchiveAdvancedFilters:
    """Normalized advanced filters for the public archive list."""

    author: str = ""
    category_ids: tuple[int, ...] = ()
    event_ids: tuple[int, ...] = ()
    tag_ids: tuple[int, ...] = ()
    year: int | None = None
    year_to: int | None = None

    def is_active(self) -> bool:
        return bool(
            self.author
            or self.category_ids
            or self.event_ids
            or self.tag_ids
            or self.year is not None
        )

    def query_param_pairs(self) -> list[tuple[str, str]]:
        """Stable GET pairs for URL construction (repeatable M2M params)."""
        params: list[tuple[str, str]] = []
        if self.author:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_AUTHOR, self.author))
        for category_id in self.category_ids:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_CATEGORY, str(category_id)))
        for event_id in self.event_ids:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_EVENT, str(event_id)))
        for tag_id in self.tag_ids:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_TAG, str(tag_id)))
        if self.year is not None:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_YEAR, str(self.year)))
            if self.year_to is not None and self.year_to != self.year:
                params.append(
                    (ARCHIVE_ADVANCED_FILTER_PARAM_YEAR_TO, str(self.year_to))
                )
        return params


EMPTY_ARCHIVE_ADVANCED_FILTERS = ArchiveAdvancedFilters()


def _raw_values(params: Any, field_name: str) -> list[Any]:
    if params is None:
        return []
    if hasattr(params, "getlist"):
        return list(params.getlist(field_name))
    if isinstance(params, Mapping):
        raw = params.get(field_name)
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return list(raw)
        return [raw]
    if isinstance(params, (list, tuple)):
        values: list[Any] = []
        for pair in params:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            key, value = pair
            if key == field_name:
                values.append(value)
        return values
    return []


def _normalize_single_string(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def _parse_positive_int_ids(raw_values: Sequence[Any]) -> tuple[int, ...]:
    ids: list[int] = []
    seen: set[int] = set()
    for raw in raw_values:
        text = _normalize_single_string(raw)
        if not text:
            continue
        try:
            value = int(text)
        except (TypeError, ValueError):
            continue
        if value < 1 or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return tuple(ids)


def _parse_year_value(raw: Any) -> int | None:
    """Return a calendar year in ``[_MIN_YEAR, _MAX_YEAR]``, else ``None``."""
    text = _normalize_single_string(raw)
    if not text:
        return None
    # Reject signed / decimal / non-canonical forms (e.g. "+1950", "1950.0").
    if not text.isdigit():
        return None
    year = int(text)
    if year < _MIN_YEAR or year > _MAX_YEAR:
        return None
    return year


def normalize_archive_advanced_filters(
    params: Mapping[str, Any] | Any | None,
) -> ArchiveAdvancedFilters:
    """
    Parse public archive advanced-filter GET parameters.

    - ``author``: single trimmed exact value (empty → inactive).
    - ``category`` / ``event`` / ``tag``: repeatable positive integer ids;
      malformed values skipped; order of first occurrence preserved.
    - ``year`` / ``year_to``: optional inclusive year range. ``year_to`` without
      ``year`` is ignored. Malformed ``year`` drops the date filter. Malformed
      ``year_to`` with a valid ``year`` falls back to a single-year window.
      Reverse ranges (``year_to < year``) also fall back to a single-year
      window on ``year`` (no silent swap). Explicit reverse-range UI validation
      is deferred to PR2.
    """
    if params is None:
        return EMPTY_ARCHIVE_ADVANCED_FILTERS

    author_values = _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_AUTHOR)
    author = ""
    for raw in author_values:
        author = _normalize_single_string(raw)
        if author:
            break

    category_ids = _parse_positive_int_ids(
        _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_CATEGORY)
    )
    event_ids = _parse_positive_int_ids(
        _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_EVENT)
    )
    tag_ids = _parse_positive_int_ids(
        _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_TAG)
    )

    year_values = _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_YEAR)
    year_to_values = _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_YEAR_TO)
    year_raw = year_values[-1] if year_values else None
    year_to_raw = year_to_values[-1] if year_to_values else None

    year = _parse_year_value(year_raw)
    year_to: int | None = None
    if year is not None:
        parsed_year_to = _parse_year_value(year_to_raw)
        if parsed_year_to is None or parsed_year_to < year:
            # Malformed or reverse year_to: keep year as a single-year window.
            year_to = year
        else:
            year_to = parsed_year_to
    # year_to alone (no valid year) is ignored.

    return ArchiveAdvancedFilters(
        author=author,
        category_ids=category_ids,
        event_ids=event_ids,
        tag_ids=tag_ids,
        year=year,
        year_to=year_to,
    )


def _search_window_for_years(year: int, year_to: int) -> tuple[date, date]:
    return date(year, 1, 1), date(year_to, 12, 31)


def filter_archive_items_by_advanced_filters(
    queryset: QuerySet[ArchiveItem],
    filters: ArchiveAdvancedFilters | None,
) -> QuerySet[ArchiveItem]:
    """
    Apply advanced filters to an already-authorized archive queryset.

    Within each multi-value M2M group values are OR'd; groups AND together.
    M2M filtering uses ``pk__in`` subqueries so join fan-out cannot duplicate
    ``ArchiveItem`` rows. Date filtering requires a known archival date
    (non-``UNKNOWN`` precision with both bounds) and uses interval overlap.
    """
    if filters is None or not filters.is_active():
        return queryset

    filtered = queryset

    if filters.author:
        filtered = filtered.filter(author_name=filters.author)

    if filters.category_ids:
        filtered = filtered.filter(
            pk__in=filtered.filter(categories__id__in=filters.category_ids).values("pk")
        )

    if filters.event_ids:
        filtered = filtered.filter(
            pk__in=filtered.filter(events__id__in=filters.event_ids).values("pk")
        )

    if filters.tag_ids:
        filtered = filtered.filter(
            pk__in=filtered.filter(tags__id__in=filters.tag_ids).values("pk")
        )

    if filters.year is not None:
        year_to = filters.year_to if filters.year_to is not None else filters.year
        search_start, search_end = _search_window_for_years(filters.year, year_to)
        filtered = filtered.exclude(
            date_precision=ArchiveItem.DatePrecision.UNKNOWN
        ).filter(
            date_start__isnull=False,
            date_end__isnull=False,
            date_start__lte=search_end,
            date_end__gte=search_start,
        )

    return filtered


def archive_advanced_filter_choice_context(
    authorized_queryset: QuerySet[ArchiveItem],
) -> dict[str, object]:
    """
    Discovery/author choices for advanced filters.

    Derived only from ``authorized_queryset`` so private/restricted metadata
    cannot leak to unauthorized viewers.
    """
    item_pks = authorized_queryset.order_by().values("pk")
    author_names = tuple(
        authorized_queryset.exclude(author_name="")
        .order_by("author_name")
        .values_list("author_name", flat=True)
        .distinct()
    )
    categories = tuple(
        ArchiveCategory.objects.filter(archive_items__pk__in=item_pks)
        .distinct()
        .order_by("name")
    )
    events = tuple(
        ArchiveEvent.objects.filter(archive_items__pk__in=item_pks)
        .distinct()
        .order_by("name")
    )
    tags = tuple(
        Tag.objects.filter(archive_items__pk__in=item_pks).distinct().order_by("name")
    )
    return {
        "advanced_filter_author_choices": author_names,
        "advanced_filter_category_choices": categories,
        "advanced_filter_event_choices": events,
        "advanced_filter_tag_choices": tags,
    }


def archive_advanced_filter_template_context(
    filters: ArchiveAdvancedFilters,
) -> dict[str, object]:
    """Template/query-preservation context for active advanced filters."""
    return {
        "advanced_filters": filters,
        "advanced_filter_author": filters.author,
        "advanced_filter_category_ids": filters.category_ids,
        "advanced_filter_event_ids": filters.event_ids,
        "advanced_filter_tag_ids": filters.tag_ids,
        "advanced_filter_year": filters.year,
        "advanced_filter_year_to": (
            filters.year_to
            if filters.year is not None
            and filters.year_to is not None
            and filters.year_to != filters.year
            else ""
        ),
        "advanced_filters_active": filters.is_active(),
    }
