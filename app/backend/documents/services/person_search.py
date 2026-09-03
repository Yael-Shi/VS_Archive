"""Shared Person name-search predicates (staff and public)."""

from __future__ import annotations

from django.db.models import Exists, OuterRef, Q

from documents.models import PersonAlias


def person_canonical_or_alias_icontains_q(search_query: str) -> Q | None:
    """Case-insensitive canonical name or alias substring match.

    Alias matching uses ``Exists`` so joining ``PersonAlias`` cannot duplicate
    Person rows. Empty/whitespace ``search_query`` yields ``None`` (no filter).
    Shared by staff and public Person name search.
    """
    q = (search_query or "").strip()
    if not q:
        return None
    alias_match = PersonAlias.objects.filter(
        person_id=OuterRef("pk"),
        name__icontains=q,
    )
    return Q(name__icontains=q) | Exists(alias_match)
