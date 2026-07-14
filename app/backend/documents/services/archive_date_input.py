"""Precision-aware archive date parsing, normalization, validation, and form helpers."""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any, Iterable

from documents.models import ArchiveItem

DATE_COMPONENT_FIELD_NAMES = (
    "date_start_year",
    "date_start_month",
    "date_start_day",
    "date_end_year",
    "date_end_month",
    "date_end_day",
)

_MIN_YEAR = 1
_MAX_YEAR = 9999

_SINGLE_POINT_PRECISIONS = frozenset(
    {
        ArchiveItem.DatePrecision.EXACT_DAY,
        ArchiveItem.DatePrecision.MONTH,
        ArchiveItem.DatePrecision.YEAR,
    }
)

_RANGE_PRECISIONS = frozenset(
    {
        ArchiveItem.DatePrecision.RANGE,
        ArchiveItem.DatePrecision.RANGE_MONTH,
        ArchiveItem.DatePrecision.RANGE_YEAR,
    }
)

_DATE_PRECISIONS_REQUIRING_BOUNDS = _SINGLE_POINT_PRECISIONS | _RANGE_PRECISIONS


_DATE_SCALAR_FIELD_NAMES = frozenset(
    (*DATE_COMPONENT_FIELD_NAMES, "date_start", "date_end")
)


def empty_date_component_form_data() -> dict[str, str]:
    return {name: "" for name in DATE_COMPONENT_FIELD_NAMES}


def _post_field_raw_values(post_data: Any, field_name: str) -> list[str]:
    if hasattr(post_data, "getlist"):
        raw_values = post_data.getlist(field_name)
    else:
        raw = post_data.get(field_name)
        if raw is None:
            return []
        raw_values = raw if isinstance(raw, (list, tuple)) else [raw]
    return [_strip_component_value(value) for value in raw_values]


def scalar_post_field(post_data: Any, field_name: str) -> str:
    """Return one scalar string for a POST field.

    For date component and legacy ISO fields, rejects ambiguous payloads where
    multiple distinct non-empty values were submitted under the same name.
    """
    values = _post_field_raw_values(post_data, field_name)
    non_empty = [value for value in values if value]
    if field_name in _DATE_SCALAR_FIELD_NAMES:
        distinct = list(dict.fromkeys(non_empty))
        if len(distinct) > 1:
            raise ValueError(f"ambiguous {field_name}, conflicting values submitted")
        if distinct:
            return distinct[0]
        return values[-1] if values else ""
    for value in values:
        if value:
            return value
    return values[-1] if values else ""


def _post_data_as_scalar_map(post_data: dict[str, Any]) -> dict[str, str]:
    """Flatten QueryDict/multi-value POST payloads to scalar strings."""
    keys: Iterable[str]
    if hasattr(post_data, "keys"):
        keys = post_data.keys()
    else:
        keys = post_data
    normalized: dict[str, str] = {}
    for key in keys:
        if key in _DATE_SCALAR_FIELD_NAMES or key == "date_precision":
            normalized[key] = scalar_post_field(post_data, key)
        else:
            raw = post_data.get(key)
            if isinstance(raw, (list, tuple)):
                normalized[key] = _strip_component_value(raw[-1]) if raw else ""
            else:
                normalized[key] = _strip_component_value(raw)
    return normalized


def _strip_component_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_optional_positive_int(value: Any, field_name: str) -> int | None:
    text = _strip_component_value(value)
    if not text:
        return None
    if not text.isdigit():
        raise ValueError(f"invalid {field_name}, expected a number")
    return int(text)


def _validate_year(year: int, field_name: str) -> None:
    if year < _MIN_YEAR or year > _MAX_YEAR:
        raise ValueError(
            f"invalid {field_name}, year must be between {_MIN_YEAR} and {_MAX_YEAR}"
        )


def _last_day_of_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _build_calendar_date(year: int, month: int, day: int, field_prefix: str) -> date:
    _validate_year(year, f"{field_prefix}_year")
    if month < 1 or month > 12:
        raise ValueError(f"invalid {field_prefix}_month, expected 1-12")
    last_day = _last_day_of_month(year, month)
    if day < 1 or day > last_day:
        raise ValueError(f"invalid {field_prefix}_day, expected 1-{last_day}")
    return date(year, month, day)


def _normalize_start_date(
    precision: str, year: int, month: int | None, day: int | None
) -> date:
    _validate_year(year, "date_start_year")
    if precision in (
        ArchiveItem.DatePrecision.YEAR,
        ArchiveItem.DatePrecision.RANGE_YEAR,
    ):
        return date(year, 1, 1)
    if precision in (
        ArchiveItem.DatePrecision.MONTH,
        ArchiveItem.DatePrecision.RANGE_MONTH,
    ):
        if month is None:
            raise ValueError("date_start_month is required")
        if month < 1 or month > 12:
            raise ValueError("invalid date_start_month, expected 1-12")
        return date(year, month, 1)
    if month is None or day is None:
        raise ValueError("date_start_month and date_start_day are required")
    return _build_calendar_date(year, month, day, "date_start")


def _normalize_single_point_end_date(
    precision: str, year: int, month: int | None, day: int | None
) -> date:
    if precision == ArchiveItem.DatePrecision.YEAR:
        return date(year, 12, 31)
    if precision == ArchiveItem.DatePrecision.MONTH:
        if month is None:
            raise ValueError("date_start_month is required")
        if month < 1 or month > 12:
            raise ValueError("invalid date_start_month, expected 1-12")
        return date(year, month, _last_day_of_month(year, month))
    if month is None or day is None:
        raise ValueError("date_start_month and date_start_day are required")
    return _build_calendar_date(year, month, day, "date_start")


def _normalize_end_date(
    precision: str, year: int, month: int | None, day: int | None
) -> date:
    _validate_year(year, "date_end_year")
    if precision == ArchiveItem.DatePrecision.RANGE_YEAR:
        return date(year, 12, 31)
    if precision == ArchiveItem.DatePrecision.RANGE_MONTH:
        if month is None:
            raise ValueError("date_end_month is required")
        if month < 1 or month > 12:
            raise ValueError("invalid date_end_month, expected 1-12")
        return date(year, month, _last_day_of_month(year, month))
    if month is None or day is None:
        raise ValueError("date_end_month and date_end_day are required")
    return _build_calendar_date(year, month, day, "date_end")


def merge_legacy_iso_date_fields(post_data: dict[str, Any]) -> dict[str, Any]:
    """Map legacy ``date_start`` / ``date_end`` ISO fields into component inputs."""
    merged = _post_data_as_scalar_map(post_data)
    has_segmented = any(merged.get(name) for name in DATE_COMPONENT_FIELD_NAMES)
    if has_segmented:
        return merged

    for iso_field, year_field, month_field, day_field in (
        ("date_start", "date_start_year", "date_start_month", "date_start_day"),
        ("date_end", "date_end_year", "date_end_month", "date_end_day"),
    ):
        raw = merged.get(iso_field, "")
        if not raw:
            continue
        try:
            parsed = date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                f"invalid {iso_field} format, expected YYYY-MM-DD"
            ) from exc
        merged[year_field] = str(parsed.year)
        merged[month_field] = str(parsed.month)
        merged[day_field] = str(parsed.day)
    return merged


def extract_date_components(post_data: dict[str, Any]) -> dict[str, str]:
    merged = merge_legacy_iso_date_fields(post_data)
    return {
        name: _strip_component_value(merged.get(name))
        for name in DATE_COMPONENT_FIELD_NAMES
    }


def parse_archive_date_bounds(
    *,
    date_precision: str,
    post_data: dict[str, Any],
) -> tuple[date | None, date | None, dict[str, str], list[str]]:
    """Parse component fields, normalize bounds, and return user-facing errors."""
    components = empty_date_component_form_data()
    errors: list[str] = []

    if date_precision == ArchiveItem.DatePrecision.UNKNOWN:
        return None, None, components, errors

    try:
        components = extract_date_components(post_data)
    except ValueError as exc:
        return None, None, components, [str(exc)]

    try:
        start_year = _parse_optional_positive_int(
            components["date_start_year"], "date_start_year"
        )
        start_month = _parse_optional_positive_int(
            components["date_start_month"], "date_start_month"
        )
        start_day = _parse_optional_positive_int(
            components["date_start_day"], "date_start_day"
        )
        end_year = _parse_optional_positive_int(
            components["date_end_year"], "date_end_year"
        )
        end_month = _parse_optional_positive_int(
            components["date_end_month"], "date_end_month"
        )
        end_day = _parse_optional_positive_int(
            components["date_end_day"], "date_end_day"
        )
    except ValueError as exc:
        return None, None, components, [str(exc)]

    if date_precision in _SINGLE_POINT_PRECISIONS:
        if start_year is None:
            errors.append("date_start_year is required")
            return None, None, components, errors
        try:
            normalized_start = _normalize_start_date(
                date_precision,
                start_year,
                start_month,
                start_day,
            )
            normalized_end = _normalize_single_point_end_date(
                date_precision,
                start_year,
                start_month,
                start_day,
            )
        except ValueError as exc:
            errors.append(str(exc))
            return None, None, components, errors
        return normalized_start, normalized_end, components, errors

    if date_precision in _RANGE_PRECISIONS:
        if start_year is None:
            errors.append("date_start_year is required")
        if end_year is None:
            errors.append("date_end_year is required")
        if errors:
            return None, None, components, errors
        assert start_year is not None
        assert end_year is not None
        try:
            normalized_start = _normalize_start_date(
                date_precision,
                start_year,
                start_month,
                start_day,
            )
            normalized_end = _normalize_end_date(
                date_precision,
                end_year,
                end_month,
                end_day,
            )
        except ValueError as exc:
            errors.append(str(exc))
            return None, None, components, errors
        if normalized_end < normalized_start:
            errors.append("date_end must not be before date_start")
            return None, None, components, errors
        return normalized_start, normalized_end, components, errors

    errors.append("date_precision is invalid")
    return None, None, components, errors


def date_component_form_data_from_stored(
    *,
    date_start: date | None,
    date_end: date | None,
    date_precision: str,
) -> dict[str, str]:
    """Populate segmented form fields from stored normalized bounds."""
    data = empty_date_component_form_data()
    if date_precision == ArchiveItem.DatePrecision.UNKNOWN:
        return data

    if date_precision in _SINGLE_POINT_PRECISIONS and date_start:
        data["date_start_year"] = str(date_start.year)
        if date_precision in (
            ArchiveItem.DatePrecision.MONTH,
            ArchiveItem.DatePrecision.EXACT_DAY,
        ):
            data["date_start_month"] = str(date_start.month)
        if date_precision == ArchiveItem.DatePrecision.EXACT_DAY:
            data["date_start_day"] = str(date_start.day)
        return data

    if date_precision in _RANGE_PRECISIONS:
        if date_start:
            data["date_start_year"] = str(date_start.year)
            if date_precision in (
                ArchiveItem.DatePrecision.RANGE,
                ArchiveItem.DatePrecision.RANGE_MONTH,
            ):
                data["date_start_month"] = str(date_start.month)
            if date_precision == ArchiveItem.DatePrecision.RANGE:
                data["date_start_day"] = str(date_start.day)
        if date_end:
            data["date_end_year"] = str(date_end.year)
            if date_precision in (
                ArchiveItem.DatePrecision.RANGE,
                ArchiveItem.DatePrecision.RANGE_MONTH,
            ):
                data["date_end_month"] = str(date_end.month)
            if date_precision == ArchiveItem.DatePrecision.RANGE:
                data["date_end_day"] = str(date_end.day)
    return data


def archive_date_form_data(
    *,
    date_start: date | None,
    date_end: date | None,
    date_precision: str,
) -> dict[str, str]:
    """Build template form_data date fields (components + legacy ISO keys)."""
    components = date_component_form_data_from_stored(
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )
    return {
        **components,
        "date_start": date_start.isoformat() if date_start else "",
        "date_end": date_end.isoformat() if date_end else "",
        "date_precision": date_precision,
    }
