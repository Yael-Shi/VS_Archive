"""Public wording and read-only links for non-person registry rows.

The URL may contain ``entities``. Visible copy does not say Entity or ישות.
Validity stays in ``non_person_entity_occurrences``. This module does not write.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Prefetch, QuerySet
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
)
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.non_person_entity_occurrences import (
    AuthoritativeTextContext,
    authoritative_text_context_for_item,
    locate_surface_occurrences,
    occurrence_is_currently_valid,
)
from documents.services.text_presentation import (
    archive_item_displayable_text_results_prefetch,
)

MENTIONED_OBJECTS_PUBLIC_HEADING = "מקומות, ארגונים ועוד המופיעים ברשומה"
PUBLIC_ADDITIONAL_NAMES_LABEL = "שמות נוספים"
PUBLIC_EMPTY_ITEMS_MESSAGE = "אין כרגע פריטים שבהם זה מופיע בטקסט המוצג."
PUBLIC_ITEMS_HEADING = "פריטים שבהם זה מופיע"
REGISTRY_INDEX_TITLE = "מקומות, ארגונים, קהילות ופרסומים"
REGISTRY_SEARCH_PLACEHOLDER = "חיפוש לפי שם או שם נוסף"
REGISTRY_EMPTY_SEARCH_MESSAGE = "לא נמצאו תוצאות תואמות."
REGISTRY_EMPTY_INDEX_MESSAGE = "אין כרגע רשומות להצגה."
REGISTRY_MATCHED_ALIAS_LABEL = "נמצא גם בשם"

_TYPE_LABELS = {
    NonPersonEntity.EntityType.PLACE: "מקום",
    NonPersonEntity.EntityType.ORGANIZATION: "ארגון",
    NonPersonEntity.EntityType.COMMUNITY: "קהילה",
    NonPersonEntity.EntityType.PUBLICATION_WORK: "פרסום",
    NonPersonEntity.EntityType.LEGAL_INSTRUMENT: "מסמך משפטי",
    NonPersonEntity.EntityType.EVENT: "אירוע",
}

_SUBTYPE_LABELS = {
    NonPersonEntity.EntitySubtype.ASSOCIATION_OR_NGO: "עמותה או ארגון",
    NonPersonEntity.EntitySubtype.BOOK: "ספר",
    NonPersonEntity.EntitySubtype.BUILDING: "מבנה",
    NonPersonEntity.EntitySubtype.CAMP: "מחנה",
    NonPersonEntity.EntitySubtype.CITY: "עיר",
    NonPersonEntity.EntitySubtype.COUNTRY: "מדינה",
    NonPersonEntity.EntitySubtype.COURT_OR_JUDICIAL_BODY: "בית משפט או גוף שיפוטי",
    NonPersonEntity.EntitySubtype.COVERT_OPERATION: "מבצע חשאי",
    NonPersonEntity.EntitySubtype.EDUCATIONAL_INSTITUTION: "מוסד חינוך",
    NonPersonEntity.EntitySubtype.ETHNO_RELIGIOUS_GROUP: "קבוצה אתנית או דתית",
    NonPersonEntity.EntitySubtype.GOVERNMENT_BODY: "גוף ממשלתי",
    NonPersonEntity.EntitySubtype.HOSPITAL: "בית חולים",
    NonPersonEntity.EntitySubtype.IMMIGRATION_OR_RESCUE_BODY: "גוף עלייה או הצלה",
    NonPersonEntity.EntitySubtype.INTELLIGENCE_ORGANIZATION: "ארגון מודיעין",
    NonPersonEntity.EntitySubtype.JEWISH_COMMUNITY: "קהילה יהודית",
    NonPersonEntity.EntitySubtype.LAW_OR_STATUTE: "חוק",
    NonPersonEntity.EntitySubtype.MILITARY_OR_SECURITY_SITE: "אתר צבאי או ביטחוני",
    NonPersonEntity.EntitySubtype.NEIGHBORHOOD: "שכונה",
    NonPersonEntity.EntitySubtype.NEWSPAPER: "עיתון",
    NonPersonEntity.EntitySubtype.ORDER_OR_DECREE: "צו",
    NonPersonEntity.EntitySubtype.PERIODICAL_OR_JOURNAL: "כתב עת",
    NonPersonEntity.EntitySubtype.PRISON: "בית סוהר",
    NonPersonEntity.EntitySubtype.REGION_OR_HISTORICAL_AREA: "אזור",
    NonPersonEntity.EntitySubtype.RELIGIOUS_SITE: "אתר דתי",
    NonPersonEntity.EntitySubtype.REPORT_OR_PROTOCOL_NON_LEGAL: "דוח או פרוטוקול",
    NonPersonEntity.EntitySubtype.SCHOOL: "בית ספר",
    NonPersonEntity.EntitySubtype.VERDICT_OR_SENTENCE: "פסק דין או גזר דין",
    NonPersonEntity.EntitySubtype.WAR_OR_CAMPAIGN: "מלחמה או מערכה",
    NonPersonEntity.EntitySubtype.YOUTH_MOVEMENT: "תנועת נוער",
    NonPersonEntity.EntitySubtype.ZIONIST_FEDERATION_OR_BODY: "פדרציה או גוף ציוני",
}

_MANUAL_SOURCE_RANK = 0
_OCR_SOURCE_RANK = 1
_UNKNOWN_POSITION = 10**12


@dataclass(frozen=True)
class PublicMentionedObjectLink:
    """One deduped public link. No occurrence internals."""

    entity_id: int
    name: str
    href: str
    type_label: str


def non_person_public_name(entity: NonPersonEntity) -> str:
    """``display_name`` when it has non-blank text, otherwise ``canonical_name``."""

    display_name = (entity.display_name or "").strip()
    if display_name:
        return display_name
    return entity.canonical_name


def non_person_public_type_label(entity: NonPersonEntity) -> str:
    """Natural Hebrew type, plus subtype when it adds a real distinction.

    Blank and ``OTHER`` subtypes are omitted. Unknown tokens are omitted
    rather than shown as enum codes.
    """

    type_label = _TYPE_LABELS.get(entity.entity_type, "")
    subtype = (entity.entity_subtype or "").strip()
    if not type_label or not subtype or subtype == NonPersonEntity.EntitySubtype.OTHER:
        return type_label
    subtype_label = _SUBTYPE_LABELS.get(subtype, "")
    if not subtype_label:
        return type_label
    return f"{type_label} · {subtype_label}"


def non_person_public_page_url(entity_id: int) -> str:
    return reverse("archive-non-person-detail", kwargs={"entity_id": entity_id})


def public_mention_sort_key(text_kind: str, start: int | None) -> tuple[int, int]:
    """MANUAL_TEXT before OCR. Within a source, earlier character offset wins.

    ``occurrence_ordinal`` is not part of this key. A missing offset sorts
    after known positions in the same source and does not drop the link.
    """

    if text_kind == ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT:
        source_rank = _MANUAL_SOURCE_RANK
    else:
        source_rank = _OCR_SOURCE_RANK
    position = _UNKNOWN_POSITION if start is None else start
    return (source_rank, position)


def public_non_person_aliases(entity: NonPersonEntity) -> tuple[str, ...]:
    """Alias names safe to show. ``OCR_VARIANT`` is omitted."""

    cached = getattr(entity, "public_aliases", None)
    if cached is not None:
        names = [alias.name for alias in cached if alias.name]
    else:
        names = list(
            entity.aliases.exclude(kind=NonPersonEntityAlias.Kind.OCR_VARIANT)
            .order_by("name", "id")
            .values_list("name", flat=True)
        )
    return tuple(name for name in names if name)


def public_mentioned_object_links(
    item: ArchiveItem,
) -> tuple[PublicMentionedObjectLink, ...]:
    """Deduped valid links for one item, ordered by first textual occurrence.

    Authoritative text is hashed once per text kind. Each normalized surface
    is located once per kind. Separate occurrence rows stay in the database.
    """

    rows = list(item.entity_occurrences.select_related("entity").order_by("id"))
    if not rows:
        return ()
    context = authoritative_text_context_for_item(item)
    located: dict[tuple[str, str], object] = {}
    best: dict[int, tuple[tuple[int, int], NonPersonEntity]] = {}
    for occurrence in rows:
        if not occurrence_is_currently_valid(occurrence, context=context):
            continue
        entity = occurrence.entity
        if entity is None:
            continue
        start = _character_start(occurrence, context, located)
        key = public_mention_sort_key(occurrence.text_kind, start)
        current = best.get(entity.pk)
        if current is None or key < current[0]:
            best[entity.pk] = (key, entity)
    ordered = sorted(best.values(), key=lambda pair: (pair[0], pair[1].pk))
    return tuple(_public_link(entity) for _key, entity in ordered)


def valid_archive_item_ids_for_entity(entity_id: int) -> list[int]:
    """Item ids with a currently valid pin for this registry row.

    Starts from that entity's resolved ``surface-v1`` occurrences. Authoritative
    text is hashed once per item. Does not apply archive authorization and does
    not write.
    """

    occurrences = list(_candidate_occurrences(entity_id))
    if not occurrences:
        return []
    by_item: dict[int, list[ArchiveItemEntityOccurrence]] = {}
    items: dict[int, ArchiveItem] = {}
    for occurrence in occurrences:
        by_item.setdefault(occurrence.archive_item_id, []).append(occurrence)
        items[occurrence.archive_item_id] = occurrence.archive_item
    return [
        item_id
        for item_id, rows in by_item.items()
        if _item_has_valid_occurrence(items[item_id], rows)
    ]


def authorized_valid_archive_item_ids(user, entity_id: int) -> list[int]:
    """Authorized browse item ids with a currently valid pin for this registry row.

    Validity and authorization stay separate. Private items are absent.
    """

    valid_ids = valid_archive_item_ids_for_entity(entity_id)
    if not valid_ids:
        return []
    return list(
        archive_browse_queryset_for_user(user)
        .filter(pk__in=valid_ids)
        .order_by("-created_at", "pk")
        .values_list("pk", flat=True)
    )


def _candidate_occurrences(entity_id: int) -> QuerySet[ArchiveItemEntityOccurrence]:
    return (
        ArchiveItemEntityOccurrence.objects.filter(
            entity_id=entity_id,
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            normalization_version="surface-v1",
        )
        .prefetch_related(
            Prefetch(
                "archive_item",
                queryset=ArchiveItem.objects.select_related(
                    "manual_text_content",
                    "ocr_document",
                ).prefetch_related(archive_item_displayable_text_results_prefetch()),
            )
        )
        .order_by("archive_item_id", "id")
    )


def _item_has_valid_occurrence(
    item: ArchiveItem,
    rows: list[ArchiveItemEntityOccurrence],
) -> bool:
    context = authoritative_text_context_for_item(item)
    return any(occurrence_is_currently_valid(row, context=context) for row in rows)


def _character_start(
    occurrence: ArchiveItemEntityOccurrence,
    context: AuthoritativeTextContext,
    located: dict[tuple[str, str], object],
) -> int | None:
    text = context.texts.get(occurrence.text_kind)
    if text is None:
        return None
    cache_key = (occurrence.text_kind, occurrence.normalized_surface)
    found = located.get(cache_key)
    if found is None:
        found = locate_surface_occurrences(text, occurrence.normalized_surface)
        located[cache_key] = found
    matches = getattr(found, "occurrences", None)
    if not matches:
        return None
    for match in matches:
        if match.ordinal == occurrence.occurrence_ordinal:
            return match.start
    return None


def _public_link(entity: NonPersonEntity) -> PublicMentionedObjectLink:
    return PublicMentionedObjectLink(
        entity_id=entity.pk,
        name=non_person_public_name(entity),
        href=non_person_public_page_url(entity.pk),
        type_label=non_person_public_type_label(entity),
    )
