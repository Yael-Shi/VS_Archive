"""Staff registry editing for NonPersonEntity and its aliases.

Ordinary CRUD. This does not approve, reassign, or remove occurrences,
and it does not write ArchiveItemSearchIndex.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef, Prefetch, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Lower, NullIf, Trim
from django.urls import reverse

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
)
from documents.services.non_person_entity_occurrences import (
    AuthoritativeTextContext,
    authoritative_text_context_for_item,
    occurrence_is_currently_valid,
)
from documents.services.non_person_entity_presentation import (
    non_person_entity_subtype_label,
    non_person_entity_type_label,
    non_person_public_name,
    non_person_public_type_label,
)
from documents.services.text_presentation import (
    PREFETCHED_DISPLAYABLE_TEXT_RESULTS_ATTR,
    displayable_document_text_results_queryset,
)

ENTITY_CANONICAL_NAME_REQUIRED_ERROR = "השם הקנוני נדרש."
ENTITY_CANONICAL_NAME_TOO_LONG_ERROR = "השם הקנוני חייב להיות עד 255 תווים."
ENTITY_DISPLAY_NAME_TOO_LONG_ERROR = "שם התצוגה חייב להיות עד 255 תווים."
ENTITY_TYPE_INVALID_ERROR = "סוג הרשומה אינו תקין."
ENTITY_SUBTYPE_INVALID_ERROR = "תת-הסוג אינו תקין."
ALIAS_NAME_REQUIRED_ERROR = "שם חלופי נדרש."
ALIAS_NAME_TOO_LONG_ERROR = "השם החלופי חייב להיות עד 255 תווים."
ALIAS_KIND_INVALID_ERROR = "סוג השם החלופי אינו תקין."
ALIAS_DUPLICATE_ERROR = "שם חלופי זה כבר קיים עבור רשומה זו."
ALIAS_SHARED_WARNING = "השם הזה קיים גם ברשומה אחרת. זו אינה שגיאה."

VALID_OCCURRENCE_STATUS = "תואם לטקסט המוצג"
STALE_OCCURRENCE_STATUS = "אינו תואם לטקסט המוצג"

_NAME_MAX_LENGTH = 255

_ALIAS_KIND_LABELS = {
    NonPersonEntityAlias.Kind.LANGUAGE_VARIANT: "שם בשפה אחרת",
    NonPersonEntityAlias.Kind.OCR_VARIANT: "וריאנט זיהוי טקסט",
    NonPersonEntityAlias.Kind.SPELLING_VARIANT: "וריאנט כתיב",
    NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT: "תעתיק",
    NonPersonEntityAlias.Kind.ABBREVIATION: "קיצור",
    NonPersonEntityAlias.Kind.CURRENT_NAME: "שם נוכחי",
}

_TEXT_KIND_LABELS = {
    ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT: "טקסט ידני",
    ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION: "תעתיק",
}


class NonPersonEntityStaffError(Exception):
    """Staff-facing registry edit error. Does not write."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class StaffEntityIndexRow:
    entity_id: int
    public_name: str
    type_label: str
    alias_count: int
    stored_occurrence_count: int


@dataclass(frozen=True)
class StaffAliasRow:
    alias_id: int
    name: str
    kind: str
    kind_label: str
    shared_with_other_entity: bool


@dataclass(frozen=True)
class StaffLinkedOccurrence:
    """Read-only pin. ``is_valid`` is computed, not stored."""

    occurrence_id: int
    item_id: int
    item_title: str
    item_url: str
    text_kind_label: str
    matched_text: str
    is_valid: bool
    status_label: str


def staff_entity_type_choices() -> list[tuple[str, str]]:
    return [
        (value, non_person_entity_type_label(value))
        for value, _label in NonPersonEntity.EntityType.choices
    ]


def staff_entity_subtype_choices() -> list[tuple[str, str]]:
    choices = [("", "ללא")]
    choices.extend(
        (value, non_person_entity_subtype_label(value))
        for value, _label in NonPersonEntity.EntitySubtype.choices
    )
    return choices


def staff_alias_kind_choices() -> list[tuple[str, str]]:
    return [
        (value, non_person_alias_kind_label(value))
        for value, _label in NonPersonEntityAlias.Kind.choices
    ]


def non_person_alias_kind_label(kind: str) -> str:
    return _ALIAS_KIND_LABELS.get(kind, "")


def staff_entity_index_queryset(
    *,
    search_query: str = "",
    entity_type: str = "",
    entity_subtype: str = "",
) -> QuerySet[NonPersonEntity]:
    """One row per entity, ordered by public name then pk.

    ``search_query`` is a case-insensitive contains match on canonical name,
    display name, or any alias, including ``OCR_VARIANT``. Alias matching uses
    ``Exists`` so one entity stays one row. Type and subtype filters apply
    only when the value is a known choice.
    """

    entities = NonPersonEntity.objects.annotate(
        public_sort_name=_public_sort_name(),
        alias_count=Count("aliases", distinct=True),
        stored_occurrence_count=Count("archive_item_occurrences", distinct=True),
    ).order_by("public_sort_name", "pk")
    cleaned = (search_query or "").strip()
    if cleaned:
        alias_match = Exists(
            NonPersonEntityAlias.objects.filter(
                entity_id=OuterRef("pk"),
                name__icontains=cleaned,
            )
        )
        entities = entities.filter(
            Q(canonical_name__icontains=cleaned)
            | Q(display_name__icontains=cleaned)
            | alias_match
        )
    selected_type = (entity_type or "").strip()
    if selected_type in NonPersonEntity.EntityType.values:
        entities = entities.filter(entity_type=selected_type)
    selected_subtype = (entity_subtype or "").strip()
    if selected_subtype in NonPersonEntity.EntitySubtype.values:
        entities = entities.filter(entity_subtype=selected_subtype)
    return entities


def staff_entity_index_rows(
    entities: QuerySet[NonPersonEntity],
) -> list[StaffEntityIndexRow]:
    return [
        StaffEntityIndexRow(
            entity_id=entity.pk,
            public_name=non_person_public_name(entity),
            type_label=non_person_public_type_label(entity),
            alias_count=entity.alias_count,
            stored_occurrence_count=entity.stored_occurrence_count,
        )
        for entity in entities
    ]


def update_non_person_entity(
    entity: NonPersonEntity,
    *,
    canonical_name: str,
    display_name: str,
    entity_type: str,
    entity_subtype: str,
) -> NonPersonEntity:
    """Update registry fields only. Occurrences and aliases stay as they are."""

    canonical = _required_name(
        canonical_name,
        required_error=ENTITY_CANONICAL_NAME_REQUIRED_ERROR,
        too_long_error=ENTITY_CANONICAL_NAME_TOO_LONG_ERROR,
    )
    display = _optional_name(
        display_name,
        too_long_error=ENTITY_DISPLAY_NAME_TOO_LONG_ERROR,
    )
    selected_type = (entity_type or "").strip()
    if selected_type not in NonPersonEntity.EntityType.values:
        raise NonPersonEntityStaffError(ENTITY_TYPE_INVALID_ERROR)
    selected_subtype = (entity_subtype or "").strip()
    if (
        selected_subtype
        and selected_subtype not in NonPersonEntity.EntitySubtype.values
    ):
        raise NonPersonEntityStaffError(ENTITY_SUBTYPE_INVALID_ERROR)

    changed: list[str] = []
    if entity.canonical_name != canonical:
        entity.canonical_name = canonical
        changed.append("canonical_name")
    if entity.display_name != display:
        entity.display_name = display
        changed.append("display_name")
    if entity.entity_type != selected_type:
        entity.entity_type = selected_type
        changed.append("entity_type")
    if entity.entity_subtype != selected_subtype:
        entity.entity_subtype = selected_subtype
        changed.append("entity_subtype")
    if changed:
        entity.save(update_fields=[*changed, "updated_at"])
    return entity


@transaction.atomic
def create_non_person_alias(
    entity: NonPersonEntity,
    *,
    name: str,
    kind: str,
) -> NonPersonEntityAlias:
    normalized_name = _required_name(
        name,
        required_error=ALIAS_NAME_REQUIRED_ERROR,
        too_long_error=ALIAS_NAME_TOO_LONG_ERROR,
    )
    normalized_kind = _alias_kind(kind)
    try:
        return NonPersonEntityAlias.objects.create(
            entity=entity,
            name=normalized_name,
            kind=normalized_kind,
        )
    except IntegrityError as exc:
        raise NonPersonEntityStaffError(ALIAS_DUPLICATE_ERROR) from exc


@transaction.atomic
def update_non_person_alias(
    alias: NonPersonEntityAlias,
    *,
    name: str,
    kind: str,
) -> NonPersonEntityAlias:
    normalized_name = _required_name(
        name,
        required_error=ALIAS_NAME_REQUIRED_ERROR,
        too_long_error=ALIAS_NAME_TOO_LONG_ERROR,
    )
    normalized_kind = _alias_kind(kind)
    changed: list[str] = []
    if alias.name != normalized_name:
        alias.name = normalized_name
        changed.append("name")
    if alias.kind != normalized_kind:
        alias.kind = normalized_kind
        changed.append("kind")
    if not changed:
        return alias
    try:
        alias.save(update_fields=[*changed, "updated_at"])
    except IntegrityError as exc:
        raise NonPersonEntityStaffError(ALIAS_DUPLICATE_ERROR) from exc
    return alias


@transaction.atomic
def delete_non_person_alias(alias: NonPersonEntityAlias) -> None:
    """Delete one alias. The registry row stays."""

    alias.delete()


def alias_name_shared_with_other_entity(*, entity_id: int, name: str) -> bool:
    """True when another registry row already stores this exact alias string."""

    cleaned = (name or "").strip()
    if not cleaned:
        return False
    return (
        NonPersonEntityAlias.objects.filter(name=cleaned)
        .exclude(entity_id=entity_id)
        .exists()
    )


def staff_alias_rows(entity: NonPersonEntity) -> list[StaffAliasRow]:
    aliases = list(entity.aliases.order_by("name", "id"))
    names = [alias.name for alias in aliases]
    shared = set(
        NonPersonEntityAlias.objects.filter(name__in=names)
        .exclude(entity_id=entity.pk)
        .values_list("name", flat=True)
    )
    return [
        StaffAliasRow(
            alias_id=alias.pk,
            name=alias.name,
            kind=alias.kind,
            kind_label=non_person_alias_kind_label(alias.kind),
            shared_with_other_entity=alias.name in shared,
        )
        for alias in aliases
    ]


def staff_linked_occurrences(entity: NonPersonEntity) -> list[StaffLinkedOccurrence]:
    """Read-only pins for one registry row, with computed validity.

    ``stored_occurrence_count`` on the index is not this list. Validity uses
    ``occurrence_is_currently_valid`` and hashes each item's displayed text
    once. This does not write.
    """

    rows = list(
        ArchiveItemEntityOccurrence.objects.filter(entity_id=entity.pk)
        .select_related("archive_item", "archive_item__manual_text_content")
        .prefetch_related(_ocr_text_prefetch())
        .order_by("archive_item_id", "id")
    )
    contexts: dict[int, AuthoritativeTextContext] = {}
    linked: list[StaffLinkedOccurrence] = []
    for occurrence in rows:
        item = occurrence.archive_item
        context = contexts.get(item.pk)
        if context is None:
            context = authoritative_text_context_for_item(item)
            contexts[item.pk] = context
        is_valid = occurrence_is_currently_valid(occurrence, context=context)
        title = (item.title or "").strip() or f"פריט {item.pk}"
        linked.append(
            StaffLinkedOccurrence(
                occurrence_id=occurrence.pk,
                item_id=item.pk,
                item_title=title,
                item_url=reverse("archive-detail", kwargs={"item_id": item.pk}),
                text_kind_label=_TEXT_KIND_LABELS.get(occurrence.text_kind, ""),
                matched_text=occurrence.matched_text,
                is_valid=is_valid,
                status_label=(
                    VALID_OCCURRENCE_STATUS if is_valid else STALE_OCCURRENCE_STATUS
                ),
            )
        )
    return linked


def _public_sort_name():
    """Lowercased public name: nonblank display_name, otherwise canonical_name."""

    return Lower(Coalesce(NullIf(Trim("display_name"), Value("")), F("canonical_name")))


def _ocr_text_prefetch() -> Prefetch:
    return Prefetch(
        "archive_item__ocr_document__text_results",
        queryset=displayable_document_text_results_queryset(),
        to_attr=PREFETCHED_DISPLAYABLE_TEXT_RESULTS_ATTR,
    )


def _required_name(value: str, *, required_error: str, too_long_error: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        raise NonPersonEntityStaffError(required_error)
    if len(normalized) > _NAME_MAX_LENGTH:
        raise NonPersonEntityStaffError(too_long_error)
    return normalized


def _optional_name(value: str, *, too_long_error: str) -> str:
    normalized = (value or "").strip()
    if len(normalized) > _NAME_MAX_LENGTH:
        raise NonPersonEntityStaffError(too_long_error)
    return normalized


def _alias_kind(kind: str) -> str:
    normalized = (kind or "").strip()
    if normalized not in NonPersonEntityAlias.Kind.values:
        raise NonPersonEntityStaffError(ALIAS_KIND_INVALID_ERROR)
    return normalized
