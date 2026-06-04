"""Precision-aware document date labels for UI display."""

from __future__ import annotations

from datetime import date

from documents.models import Document

NO_DATE_LABEL = "ללא תאריך"

_VALID_PRECISIONS = {choice.value for choice in Document.DatePrecision}


def _format_day_label(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def format_document_date(document: Document) -> str:
    """Return a human-facing date label according to ``document.date_precision``.

    Normalized ``date_start`` / ``date_end`` are used only when precision is not
    ``UNKNOWN``. For ``UNKNOWN``, bounds are ignored (Option B).
    """
    precision = document.date_precision or Document.DatePrecision.UNKNOWN
    if precision not in _VALID_PRECISIONS:
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.UNKNOWN:
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.EXACT_DAY:
        start = document.date_start
        end = document.date_end
        if start and end and start == end:
            return _format_day_label(start)
        if start:
            return _format_day_label(start)
        if end:
            return _format_day_label(end)
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.MONTH:
        if document.date_start:
            return f"{document.date_start.month:02d}/{document.date_start.year}"
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.YEAR:
        if document.date_start:
            return str(document.date_start.year)
        return NO_DATE_LABEL

    if precision == Document.DatePrecision.RANGE:
        start = document.date_start
        end = document.date_end
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
