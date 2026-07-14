"""Precision-aware document date labels for UI display."""

from __future__ import annotations

from datetime import date
from typing import Any

from documents.models import ArchiveItem

NO_DATE_LABEL = "ללא תאריך"

_VALID_PRECISIONS = {choice.value for choice in ArchiveItem.DatePrecision}


def _format_day_label(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _format_month_year_label(value: date) -> str:
    return f"{value.month:02d}/{value.year}"


def format_document_date(obj: Any) -> str:
    """Return a human-facing date label according to ``obj.date_precision``.

    Accepts any object with ``date_start``, ``date_end``, and ``date_precision``
    (e.g. ``ArchiveItem``). Normalized bounds are used only when
    precision is not ``UNKNOWN``. For ``UNKNOWN``, bounds are ignored (Option B).
    """
    precision = obj.date_precision or ArchiveItem.DatePrecision.UNKNOWN
    if precision not in _VALID_PRECISIONS:
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.UNKNOWN:
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.EXACT_DAY:
        start = obj.date_start
        end = obj.date_end
        if start and end and start == end:
            return _format_day_label(start)
        if start:
            return _format_day_label(start)
        if end:
            return _format_day_label(end)
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.MONTH:
        if obj.date_start:
            return _format_month_year_label(obj.date_start)
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.YEAR:
        if obj.date_start:
            return str(obj.date_start.year)
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.RANGE:
        start = obj.date_start
        end = obj.date_end
        if start and end:
            if start == end:
                return _format_day_label(start)
            return f"{_format_day_label(start)} - {_format_day_label(end)}"
        if start:
            return _format_day_label(start)
        if end:
            return _format_day_label(end)
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.RANGE_MONTH:
        start = obj.date_start
        end = obj.date_end
        if start and end:
            if start.year == end.year and start.month == end.month:
                return _format_month_year_label(start)
            return f"{_format_month_year_label(start)} - {_format_month_year_label(end)}"
        if start:
            return _format_month_year_label(start)
        if end:
            return _format_month_year_label(end)
        return NO_DATE_LABEL

    if precision == ArchiveItem.DatePrecision.RANGE_YEAR:
        start = obj.date_start
        end = obj.date_end
        if start and end:
            if start.year == end.year:
                return str(start.year)
            return f"{start.year} - {end.year}"
        if start:
            return str(start.year)
        if end:
            return str(end.year)
        return NO_DATE_LABEL

    return NO_DATE_LABEL
