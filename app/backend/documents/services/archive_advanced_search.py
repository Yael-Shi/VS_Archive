"""Public ``/archive/`` advanced filter contract (normalize, apply, serialize, choices).

Structured filters compose with the existing authorized browse queryset, item-type
filter, and full-text ``q`` search. They operate on ``ArchiveItem`` fields/relations
and must not bypass visibility helpers or replace FTS ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Mapping, Sequence

from django.db.models import QuerySet

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    Tag,
)

# Match archive date-component year bounds (``archive_date_input``).
_MIN_YEAR = 1
_MAX_YEAR = 9999

ARCHIVE_ADVANCED_FILTER_PARAM_AUTHOR = "author"
ARCHIVE_ADVANCED_FILTER_PARAM_CATEGORY = "category"
ARCHIVE_ADVANCED_FILTER_PARAM_EVENT = "event"
ARCHIVE_ADVANCED_FILTER_PARAM_TAG = "tag"
ARCHIVE_ADVANCED_FILTER_PARAM_PERSON = "person"
ARCHIVE_ADVANCED_FILTER_PARAM_YEAR = "year"
ARCHIVE_ADVANCED_FILTER_PARAM_YEAR_TO = "year_to"

# UI-only panel open state. Must not affect filter semantics.
ARCHIVE_ADVANCED_PANEL_PARAM = "advanced"
ARCHIVE_ADVANCED_PANEL_VALUE = "1"

ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR = (
    "עד שנה חייבת להיות שווה לשנה הראשונה או מאוחרת ממנה."
)
ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR = "יש להזין שנה תקינה (למשל 1950)."
ARCHIVE_ADVANCED_YEAR_TO_MALFORMED_ERROR = (
    'יש להזין שנה תקינה בשדה "עד שנה" (למשל 1955).'
)
ARCHIVE_ADVANCED_YEAR_TO_WITHOUT_YEAR_ERROR = (
    "כדי לחפש לפי טווח שנים יש להזין גם את השנה הראשונה."
)

EMPTY_ARCHIVE_ADVANCED_FILTER_CHOICE_CONTEXT: dict[str, object] = {
    "advanced_filter_author_choices": (),
    "advanced_filter_category_choices": (),
    "advanced_filter_event_choices": (),
    "advanced_filter_tag_choices": (),
    "advanced_filter_person_choices": (),
}


@dataclass(frozen=True)
class ArchiveAdvancedFilters:
    """Normalized advanced filters for the public archive list."""

    author: str = ""
    category_ids: tuple[int, ...] = ()
    event_ids: tuple[int, ...] = ()
    tag_ids: tuple[int, ...] = ()
    person_ids: tuple[int, ...] = ()
    year: int | None = None
    year_to: int | None = None

    def is_active(self) -> bool:
        return bool(
            self.author
            or self.category_ids
            or self.event_ids
            or self.tag_ids
            or self.person_ids
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
        for person_id in self.person_ids:
            params.append((ARCHIVE_ADVANCED_FILTER_PARAM_PERSON, str(person_id)))
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
    - ``category`` / ``event`` / ``tag`` / ``person``: repeatable positive
      integer ids; malformed values skipped; order of first occurrence
      preserved. ``person`` is a Person primary key, never a name or alias.
    - ``year`` / ``year_to``: optional inclusive year range. ``year_to`` without
      ``year`` is ignored. Malformed ``year`` drops the date filter. Malformed
      ``year_to`` with a valid ``year`` falls back to a single-year window.
      Reverse ranges (``year_to < year``) also fall back to a single-year
      window on ``year`` (no silent swap). Public UI validation rejects reverse
      and malformed years before this defensive fallback is relied on for search.
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
    person_ids = _parse_positive_int_ids(
        _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_PERSON)
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
        person_ids=person_ids,
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

    if filters.person_ids:
        # ArchiveItemPerson only. Query the through table directly so the
        # authorized browse queryset (EXISTS/JOINs) is not copied into the
        # inner subquery. PhotoPerson is a PhotoContent relation and must not
        # match this filter.
        filtered = filtered.filter(
            pk__in=ArchiveItemPerson.objects.filter(
                person_id__in=filters.person_ids
            ).values("archive_item_id")
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
    persons = tuple(
        Person.objects.filter(archive_items__pk__in=item_pks)
        .distinct()
        .order_by("name", "id")
    )
    return {
        "advanced_filter_author_choices": author_names,
        "advanced_filter_category_choices": categories,
        "advanced_filter_event_choices": events,
        "advanced_filter_tag_choices": tags,
        "advanced_filter_person_choices": persons,
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
        "advanced_filter_person_ids": filters.person_ids,
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


@dataclass(frozen=True)
class ArchiveAdvancedYearFieldValidation:
    """Authoritative public-UI validation for ``year`` / ``year_to`` inputs."""

    year_raw: str = ""
    year_to_raw: str = ""
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_archive_advanced_year_fields(
    params: Mapping[str, Any] | Any | None,
) -> ArchiveAdvancedYearFieldValidation:
    """
    Validate public advanced year fields for user-facing search.

    Unlike ``normalize_archive_advanced_filters``, malformed and reverse ranges
    are explicit errors. Submitted raw strings are preserved for form redisplay.
    """
    if params is None:
        return ArchiveAdvancedYearFieldValidation()

    year_values = _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_YEAR)
    year_to_values = _raw_values(params, ARCHIVE_ADVANCED_FILTER_PARAM_YEAR_TO)
    year_raw = _normalize_single_string(year_values[-1] if year_values else "")
    year_to_raw = _normalize_single_string(year_to_values[-1] if year_to_values else "")

    errors: list[str] = []
    year: int | None = None
    if year_raw:
        year = _parse_year_value(year_raw)
        if year is None:
            errors.append(ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR)

    if year_to_raw:
        year_to = _parse_year_value(year_to_raw)
        if year_to is None:
            errors.append(ARCHIVE_ADVANCED_YEAR_TO_MALFORMED_ERROR)
        elif not year_raw:
            errors.append(ARCHIVE_ADVANCED_YEAR_TO_WITHOUT_YEAR_ERROR)
        elif year is not None and year_to < year:
            errors.append(ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR)

    return ArchiveAdvancedYearFieldValidation(
        year_raw=year_raw,
        year_to_raw=year_to_raw,
        errors=tuple(errors),
    )


def filters_for_archive_list_search(
    params: Mapping[str, Any] | Any | None,
    *,
    year_validation: ArchiveAdvancedYearFieldValidation | None = None,
) -> ArchiveAdvancedFilters:
    """
    Normalize filters for queryset application.

    When UI year validation fails, date filters are dropped so the defensive
    normalize fallback is never treated as a successful reverse-range search.
    """
    filters = normalize_archive_advanced_filters(params)
    validation = year_validation or validate_archive_advanced_year_fields(params)
    if validation.is_valid:
        return filters
    return replace(filters, year=None, year_to=None)


def archive_advanced_panel_is_requested(
    params: Mapping[str, Any] | Any | None,
) -> bool:
    """Return True when the advanced panel open flag is present (``advanced=1``)."""
    if params is None:
        return False
    values = _raw_values(params, ARCHIVE_ADVANCED_PANEL_PARAM)
    if not values:
        return False
    return _normalize_single_string(values[-1]) == ARCHIVE_ADVANCED_PANEL_VALUE


def should_load_archive_advanced_filter_choices(
    *,
    panel_open: bool,
    advanced_filters_active: bool,
) -> bool:
    """Load authorized choice context only when the advanced UI needs it."""
    return bool(panel_open or advanced_filters_active)


def archive_advanced_year_form_values(
    filters: ArchiveAdvancedFilters,
    year_validation: ArchiveAdvancedYearFieldValidation,
) -> dict[str, str]:
    """String year inputs for the advanced form (preserve raw on validation error)."""
    if not year_validation.is_valid:
        return {
            "advanced_filter_year_input": year_validation.year_raw,
            "advanced_filter_year_to_input": year_validation.year_to_raw,
        }
    year_input = str(filters.year) if filters.year is not None else ""
    year_to_input = ""
    if (
        filters.year is not None
        and filters.year_to is not None
        and filters.year_to != filters.year
    ):
        year_to_input = str(filters.year_to)
    return {
        "advanced_filter_year_input": year_input,
        "advanced_filter_year_to_input": year_to_input,
    }


def archive_advanced_filters_without_author(
    filters: ArchiveAdvancedFilters,
) -> ArchiveAdvancedFilters:
    return replace(filters, author="")


def archive_advanced_filters_without_category(
    filters: ArchiveAdvancedFilters,
    category_id: int,
) -> ArchiveAdvancedFilters:
    return replace(
        filters,
        category_ids=tuple(
            value for value in filters.category_ids if value != category_id
        ),
    )


def archive_advanced_filters_without_event(
    filters: ArchiveAdvancedFilters,
    event_id: int,
) -> ArchiveAdvancedFilters:
    return replace(
        filters,
        event_ids=tuple(value for value in filters.event_ids if value != event_id),
    )


def archive_advanced_filters_without_tag(
    filters: ArchiveAdvancedFilters,
    tag_id: int,
) -> ArchiveAdvancedFilters:
    return replace(
        filters,
        tag_ids=tuple(value for value in filters.tag_ids if value != tag_id),
    )


def archive_advanced_filters_without_person(
    filters: ArchiveAdvancedFilters,
    person_id: int,
) -> ArchiveAdvancedFilters:
    return replace(
        filters,
        person_ids=tuple(value for value in filters.person_ids if value != person_id),
    )


def archive_advanced_filters_without_year(
    filters: ArchiveAdvancedFilters,
) -> ArchiveAdvancedFilters:
    return replace(filters, year=None, year_to=None)


def _choice_name_by_id(choices: Sequence[Any], choice_id: int) -> str:
    for choice in choices:
        if getattr(choice, "pk", None) == choice_id:
            return str(getattr(choice, "name", choice_id))
    return str(choice_id)


def build_archive_advanced_filter_summary_items(
    *,
    q: str,
    filters: ArchiveAdvancedFilters,
    category_choices: Sequence[Any] = (),
    event_choices: Sequence[Any] = (),
    tag_choices: Sequence[Any] = (),
    person_choices: Sequence[Any] = (),
) -> list[dict[str, object]]:
    """
    Compact active-filter summary descriptors (labels/values only).

    Remove hrefs are attached by the presentation/query-builder layer.
    """
    items: list[dict[str, object]] = []
    if q:
        items.append(
            {
                "kind": "q",
                "label": "חיפוש",
                "value": q,
            }
        )
    if filters.author:
        items.append(
            {
                "kind": "author",
                "label": "מחבר/ת",
                "value": filters.author,
            }
        )
    if filters.category_ids:
        names = [
            _choice_name_by_id(category_choices, category_id)
            for category_id in filters.category_ids
        ]
        items.append(
            {
                "kind": "category",
                "label": "קטגוריה" if len(names) == 1 else "קטגוריות",
                "value": ", ".join(names),
                "ids": filters.category_ids,
            }
        )
    if filters.event_ids:
        names = [
            _choice_name_by_id(event_choices, event_id)
            for event_id in filters.event_ids
        ]
        items.append(
            {
                "kind": "event",
                "label": "אירוע" if len(names) == 1 else "אירועים",
                "value": ", ".join(names),
                "ids": filters.event_ids,
            }
        )
    if filters.tag_ids:
        names = [_choice_name_by_id(tag_choices, tag_id) for tag_id in filters.tag_ids]
        items.append(
            {
                "kind": "tag",
                "label": "תגית" if len(names) == 1 else "תגיות",
                "value": ", ".join(names),
                "ids": filters.tag_ids,
            }
        )
    if filters.person_ids:
        names = [
            _choice_name_by_id(person_choices, person_id)
            for person_id in filters.person_ids
        ]
        items.append(
            {
                "kind": "person",
                "label": "אדם" if len(names) == 1 else "אנשים",
                "value": ", ".join(names),
                "ids": filters.person_ids,
            }
        )
    if filters.year is not None:
        if filters.year_to is not None and filters.year_to != filters.year:
            year_value = f"{filters.year}–{filters.year_to}"
        else:
            year_value = str(filters.year)
        items.append(
            {
                "kind": "year",
                "label": "שנים",
                "value": year_value,
            }
        )
    return items
