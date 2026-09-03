"""Staff new-Person name duplicate candidates (exact, case-insensitive).

A match is a confirmation warning, never automatic reuse or merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import reduce
import hashlib
import operator
from typing import Any, Iterable

from django.db.models import Q

from documents.models import Person
from documents.services.photo_content_management import (
    PhotoContentManagementError,
    parse_new_person_names_input,
    person_staff_picker_label,
    staff_person_aliases_prefetch,
)

FORCE_CREATE_PERSON_FIELD = "force_create_person"
PERSON_NAME_CANDIDATES_ERROR_CODE = "PERSON_NAME_CANDIDATES"
PERSON_NAME_CANDIDATES_ERROR = (
    "נמצאו רשומות אדם קיימות עם אותו שם. בחרו אדם קיים בבורר והסירו את השם "
    "משדה ההוספה, או אשרו יצירת אדם חדש במכוון עבור כל שם מסומן."
)


@dataclass(frozen=True)
class PersonDuplicateCandidate:
    id: int
    name: str
    aliases: tuple[str, ...]
    label: str


@dataclass(frozen=True)
class PersonNameConflict:
    submitted_name: str
    token_key: str
    candidates: tuple[PersonDuplicateCandidate, ...]
    needs_confirmation: bool = True


@dataclass(frozen=True)
class NewPersonNamesCheck:
    display: str
    names: list[str]
    errors: list[str]
    matches: tuple[PersonNameConflict, ...]
    conflicts: tuple[PersonNameConflict, ...]
    force_create_person_keys: list[str]


class PersonNameDuplicateConflictError(PhotoContentManagementError):
    """Raised when new-Person tokens match existing people without force-create."""

    def __init__(self, check: NewPersonNamesCheck):
        message = (
            check.errors[0] if check.errors else PERSON_NAME_CANDIDATES_ERROR
        )
        super().__init__(message)
        self.check = check


def person_new_name_token_key(submitted_name: str) -> str:
    """Deterministic key bound to the exact trimmed submitted token."""
    normalized = (submitted_name or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_force_create_person_keys(post_data) -> list[str]:
    if post_data is None:
        raw_values: list[Any] = []
    elif hasattr(post_data, "getlist"):
        raw_values = list(post_data.getlist(FORCE_CREATE_PERSON_FIELD))
    else:
        raw = post_data.get(FORCE_CREATE_PERSON_FIELD)
        if raw is None:
            raw_values = []
        elif isinstance(raw, (list, tuple)):
            raw_values = list(raw)
        else:
            raw_values = [raw]

    keys: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        key = str(raw).strip().lower()
        if len(key) != 64:
            continue
        if any(char not in "0123456789abcdef" for char in key):
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _alias_names(person: Person) -> tuple[str, ...]:
    names: list[str] = []
    for alias in person.aliases.all():
        text = (alias.name or "").strip()
        if text:
            names.append(text)
    return tuple(names)


def _candidate_from_person(person: Person) -> PersonDuplicateCandidate:
    return PersonDuplicateCandidate(
        id=person.pk,
        name=person.name,
        aliases=_alias_names(person),
        label=person_staff_picker_label(person),
    )


def _names_match(stored: str, submitted: str) -> bool:
    return (stored or "").strip().casefold() == submitted.casefold()


def _person_matches_submitted_name(person: Person, submitted: str) -> bool:
    if _names_match(person.name, submitted):
        return True
    return any(_names_match(alias, submitted) for alias in _alias_names(person))


def find_existing_person_candidates(name: str) -> tuple[PersonDuplicateCandidate, ...]:
    """Return distinct existing people matching one name (canonical or alias)."""
    normalized = (name or "").strip()
    if not normalized:
        return ()
    by_name = find_existing_person_candidates_for_names([normalized])
    return by_name.get(normalized, ())


def find_existing_person_candidates_for_names(
    names: Iterable[str],
) -> dict[str, tuple[PersonDuplicateCandidate, ...]]:
    """Batch exact case-insensitive canonical/alias lookup for submitted tokens."""
    submitted = [(name or "").strip() for name in names]
    submitted = [name for name in submitted if name]
    unique_names = list(dict.fromkeys(submitted))
    result: dict[str, tuple[PersonDuplicateCandidate, ...]] = {
        name: () for name in unique_names
    }
    if not unique_names:
        return result

    name_q = reduce(operator.or_, (Q(name__iexact=name) for name in unique_names))
    alias_q = reduce(
        operator.or_, (Q(aliases__name__iexact=name) for name in unique_names)
    )
    people = list(
        Person.objects.filter(name_q | alias_q)
        .distinct()
        .prefetch_related(staff_person_aliases_prefetch())
        .order_by("name", "id")
    )

    for name in unique_names:
        matched = [
            _candidate_from_person(person)
            for person in people
            if _person_matches_submitted_name(person, name)
        ]
        matched.sort(key=lambda candidate: (candidate.name, candidate.id))
        result[name] = tuple(matched)
    return result


def check_new_person_names(
    raw: str | None,
    *,
    force_create_person_keys: Iterable[str] | None = None,
) -> NewPersonNamesCheck:
    """Parse tokens, then require per-token force-create when candidates exist."""
    display, names, errors = parse_new_person_names_input(raw)
    keys = list(force_create_person_keys or [])
    if errors or not names:
        return NewPersonNamesCheck(
            display=display,
            names=names,
            errors=errors,
            matches=(),
            conflicts=(),
            force_create_person_keys=keys,
        )

    candidates_by_name = find_existing_person_candidates_for_names(names)
    approved = set(keys)
    matches: list[PersonNameConflict] = []
    conflicts: list[PersonNameConflict] = []
    for name in names:
        candidates = candidates_by_name.get(name) or ()
        if not candidates:
            continue
        token_key = person_new_name_token_key(name)
        needs_confirmation = token_key not in approved
        match = PersonNameConflict(
            submitted_name=name,
            token_key=token_key,
            candidates=candidates,
            needs_confirmation=needs_confirmation,
        )
        matches.append(match)
        if needs_confirmation:
            conflicts.append(match)

    if conflicts:
        errors = [PERSON_NAME_CANDIDATES_ERROR]
    return NewPersonNamesCheck(
        display=display,
        names=names,
        errors=errors,
        matches=tuple(matches),
        conflicts=tuple(conflicts),
        force_create_person_keys=keys,
    )


def person_name_conflicts_as_dicts(
    conflicts: Iterable[PersonNameConflict],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for conflict in conflicts:
        payload.append(
            {
                "submitted_name": conflict.submitted_name,
                "token_key": conflict.token_key,
                "needs_confirmation": conflict.needs_confirmation,
                "candidates": [
                    {
                        "id": candidate.id,
                        "name": candidate.name,
                        "aliases": list(candidate.aliases),
                        "label": candidate.label,
                    }
                    for candidate in conflict.candidates
                ],
            }
        )
    return payload


def person_name_candidates_error_payload(
    conflicts: Iterable[PersonNameConflict],
) -> dict[str, Any]:
    return {
        "error": PERSON_NAME_CANDIDATES_ERROR,
        "error_code": PERSON_NAME_CANDIDATES_ERROR_CODE,
        "person_name_conflicts": person_name_conflicts_as_dicts(conflicts),
    }
