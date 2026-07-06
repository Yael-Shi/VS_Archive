"""Precision-aware document date labels for UI display."""

from __future__ import annotations

from datetime import date
from typing import Any

from documents.models import Document

NO_DATE_LABEL = "ללא תאריך"

_VALID_PRECISIONS = {choice.value for choice in Document.DatePrecision}


def _format_day_label(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def format_document_date(obj: Any) -> str:
    """Return a human-facing date label according to ``obj.date_precision``.

    Accepts any object with ``date_start``, ``date_end``, and ``date_precision``
    (e.g. ``ArchiveItem``). Normalized bounds are used only when
    precision is not ``UNKNOWN``. For ``UNKNOWN``, bounds are ignored (Option B).
    """
    precision = obj.date_precision or Document.DatePrecision.UNKNOWN
    if precision not in _VALID_PRECISIONS:
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.UNKNOWN:
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.EXACT_DAY:
        start = obj.date_start
        end = obj.date_end
        if start and end and start == end:
            return _format_day_label(start)
        if start:
            return _format_day_label(start)
        if end:
            return _format_day_label(end)
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.MONTH:
        if obj.date_start:
            return f"{obj.date_start.month:02d}/{obj.date_start.year}"
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.YEAR:
        if obj.date_start:
            return str(obj.date_start.year)
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.RANGE:
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

    return NO_DATE_LABEL
