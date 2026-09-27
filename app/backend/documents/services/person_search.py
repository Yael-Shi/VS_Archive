"""Shared Person name-search predicates (staff and public)."""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q
from django.db.models.expressions import Combinable

from documents.models import PersonAlias, PersonFamilyName


def person_identity_icontains_q(search_query: str) -> Combinable | None:
    """Case-insensitive canonical name, alias, or family-name substring match.

    Alias and family-name matching use ``Exists`` so joining those rows cannot
    duplicate Person rows. Empty/whitespace ``search_query`` yields ``None``
    (no filter). Shared by staff and public Person name search. Honorifics are
    not included.
    """
    q = (search_query or "").strip()
    if not q:
        return None
    alias_match = PersonAlias.objects.filter(
        person_id=OuterRef("pk"),
        name__icontains=q,
    )
    family_name_match = PersonFamilyName.objects.filter(
        person_id=OuterRef("pk"),
        name__icontains=q,
    )
    return Q(name__icontains=q) | Exists(alias_match) | Exists(family_name_match)
