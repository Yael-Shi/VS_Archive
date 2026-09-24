"""Shared public/staff Person display-name formatting.

Canonical ``Person.name`` is the actual name. Optional ``Person.honorific`` is
a separate title. Display-name formatting only strips edges and appends
``, {honorific}`` when the honorific is nonblank. It does not parse, infer,
or mutate rows.

Public alternate names come only from ``PersonAlias`` rows explicitly marked
``display_publicly=True``. ``kind`` controls their public grouping and, where
useful, the type label shown inside the chip. ``language`` is stored metadata;
it does not change search behavior and is not currently repeated in public
chips.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from django.db.models import Prefetch

from documents.models import PersonAlias


PUBLIC_PERSON_ADDITIONAL_NAMES_LABEL = "שמות נוספים"


def public_person_aliases_prefetch() -> Prefetch:
    """Prefetch only aliases eligible for the public Person profile."""
    return Prefetch(
        "aliases",
        queryset=PersonAlias.objects.filter(display_publicly=True).order_by(
            "name",
            "id",
        ),
    )


class PublicPersonAlternateName(NamedTuple):
    """One public alternate name rendered inside a chip.

    ``label`` is shown only when the semantic type matters to the reader
    (cover identity, code name, or underground name). Otherwise it is empty.
    """

    label: str
    value: str


class PublicPersonNameGroup(NamedTuple):
    """One visual group of public alternate names."""

    label: str
    entries: tuple[PublicPersonAlternateName, ...]


_PUBLIC_NAME_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "שמות בשפות אחרות",
        (PersonAlias.Kind.OTHER_LANGUAGE,),
    ),
    (
        "שמות כיסוי וקוד",
        (
            PersonAlias.Kind.COVER_IDENTITY,
            PersonAlias.Kind.CODE_NAME,
            PersonAlias.Kind.UNDERGROUND_NAME,
        ),
    ),
    (
        "כינויים ושמות מוכרים",
        (PersonAlias.Kind.NICKNAME,),
    ),
    (
        "וריאנטים וכתיבים נוספים",
        (
            PersonAlias.Kind.NAME_VARIANT,
            PersonAlias.Kind.SPELLING_VARIANT,
        ),
    ),
)

_KIND_TO_GROUP = {
    kind: group_label
    for group_label, kinds in _PUBLIC_NAME_GROUPS
    for kind in kinds
}

_KIND_ORDER = {
    kind: index
    for _group_label, kinds in _PUBLIC_NAME_GROUPS
    for index, kind in enumerate(kinds)
}

_PUBLIC_CHIP_LABELS = {
    PersonAlias.Kind.COVER_IDENTITY: "שם כיסוי",
    PersonAlias.Kind.CODE_NAME: "שם קוד",
    PersonAlias.Kind.UNDERGROUND_NAME: "שם מחתרתי",
}


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


def public_person_additional_name_groups(
    person: Any,
) -> tuple[PublicPersonNameGroup, ...]:
    """Return explicitly public alternate names grouped for the Person page.

    Only ``PersonAlias`` rows with ``display_publicly=True`` are eligible.
    Blank names and the canonical ``Person.name`` are omitted. Duplicate names
    collapse case-insensitively to the first eligible row.

    Known semantic kinds are placed in their public group. Any other kind that
    staff explicitly marked public is retained under ``שמות נוספים`` rather
    than silently dropped.
    """
    aliases = getattr(person, "aliases", None)
    if aliases is None or not hasattr(aliases, "all"):
        return ()

    canonical = (getattr(person, "name", "") or "").strip().casefold()
    seen: set[str] = set()
    grouped: dict[str, list[tuple[str, PublicPersonAlternateName]]] = {}

    for alias in aliases.all():
        if not getattr(alias, "display_publicly", False):
            continue

        name = (getattr(alias, "name", "") or "").strip()
        if not name:
            continue

        key = name.casefold()
        if canonical and key == canonical:
            continue
        if key in seen:
            continue
        seen.add(key)

        kind = getattr(alias, "kind", PersonAlias.Kind.UNSPECIFIED)
        group_label = _KIND_TO_GROUP.get(
            kind,
            PUBLIC_PERSON_ADDITIONAL_NAMES_LABEL,
        )
        entry = PublicPersonAlternateName(
            label=_PUBLIC_CHIP_LABELS.get(kind, ""),
            value=name,
        )
        grouped.setdefault(group_label, []).append((kind, entry))

    groups: list[PublicPersonNameGroup] = []

    for group_label, _kinds in _PUBLIC_NAME_GROUPS:
        rows = grouped.get(group_label)
        if not rows:
            continue

        rows.sort(
            key=lambda row: (
                _KIND_ORDER.get(row[0], 999),
                row[1].value.casefold(),
            )
        )
        groups.append(
            PublicPersonNameGroup(
                label=group_label,
                entries=tuple(entry for _kind, entry in rows),
            )
        )

    fallback = grouped.get(PUBLIC_PERSON_ADDITIONAL_NAMES_LABEL)
    if fallback:
        groups.append(
            PublicPersonNameGroup(
                label=PUBLIC_PERSON_ADDITIONAL_NAMES_LABEL,
                entries=tuple(entry for _kind, entry in fallback),
            )
        )

    return tuple(groups)
