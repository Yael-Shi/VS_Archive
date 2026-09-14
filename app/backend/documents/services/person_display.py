"""Shared public/staff Person display-name formatting.

Canonical ``Person.name`` is the actual name. Optional ``Person.honorific`` is
a separate title. This helper only strips edges and appends ``, {honorific}``
when the honorific is nonblank. It does not parse, infer, or mutate rows.
"""

from __future__ import annotations

from typing import Any


def format_person_display_name(*, name: str, honorific: str = "") -> str:
    """Return ``name, honorific`` when honorific is nonblank, else ``name``."""
    display_name = (name or "").strip()
    display_honorific = (honorific or "").strip()
    if display_honorific:
        return f"{display_name}, {display_honorific}"
    return display_name


def person_public_display_name(person: Any) -> str:
    """Format a Person-like object with ``name`` and optional ``honorific``."""
    return format_person_display_name(
        name=getattr(person, "name", "") or "",
        honorific=getattr(person, "honorific", "") or "",
    )
