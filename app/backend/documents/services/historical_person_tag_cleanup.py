"""D1 cleanup: delete mapped historical person-Tag *relations* only.

Identity is Tag.id → Person.id. Names, aliases, PhotoPerson, Person rows,
ArchiveItemPerson rows, Tag rows, and ordinary Tags are never written.
Mapped Tag rows may all be absent after D2b; that is a no-op success.
Partial mapped Tag presence stays fail-closed. Default callers plan only;
writes require ``--apply-relations``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
    historical_person_name_tag_ids,
    is_historical_person_name_tag,
    person_id_for_historical_person_name_tag,
)
from documents.models import ArchiveItem, ArchiveItemPerson, Document, Person, Tag
from documents.services.archive_search_index import sync_archive_item_search_indexes

EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE = 29


class HistoricalPersonTagCleanupError(Exception):
    """Fail-closed precondition failure; no relation or index writes."""


@dataclass(frozen=True)
class ArchiveItemTagCleanupRow:
    through_id: int
    archive_item_id: int
    tag_id: int
    person_id: int

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.through_id, self.archive_item_id, self.tag_id, self.person_id)


@dataclass(frozen=True)
class DocumentTagCleanupRow:
    through_id: int
    document_id: int
    tag_id: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.through_id, self.document_id, self.tag_id)


@dataclass
class HistoricalPersonTagCleanupPlan:
    archive_item_tag_rows: list[ArchiveItemTagCleanupRow] = field(default_factory=list)
    document_tag_rows: list[DocumentTagCleanupRow] = field(default_factory=list)
    deleted_archive_item_tag_rows: list[ArchiveItemTagCleanupRow] = field(
        default_factory=list
    )
    deleted_document_tag_rows: list[DocumentTagCleanupRow] = field(default_factory=list)

    @property
    def planned_archive_item_tag_count(self) -> int:
        return len(self.archive_item_tag_rows)

    @property
    def planned_document_tag_count(self) -> int:
        return len(self.document_tag_rows)

    @property
    def planned_count(self) -> int:
        return self.planned_archive_item_tag_count + self.planned_document_tag_count

    @property
    def deleted_archive_item_tag_count(self) -> int:
        return len(self.deleted_archive_item_tag_rows)

    @property
    def deleted_document_tag_count(self) -> int:
        return len(self.deleted_document_tag_rows)

    @property
    def deleted_count(self) -> int:
        return self.deleted_archive_item_tag_count + self.deleted_document_tag_count

    @property
    def affected_archive_item_ids(self) -> list[int]:
        return sorted({row.archive_item_id for row in self.archive_item_tag_rows})


def _sorted_missing_ids(required: list[int], found: set[int]) -> list[int]:
    return [pk for pk in required if pk not in found]


def _mapped_tag_ids() -> list[int]:
    return [tag_id for tag_id, _person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID]


def _mapped_person_ids() -> list[int]:
    return [person_id for _tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID]


def validate_historical_person_tag_cleanup_preconditions() -> None:
    """Fail closed unless the frozen map and required Person rows are intact.

    Mapped Tag ids must be all present or all absent. Partial Tag presence
    fails closed. All-absent Tags is a D2b success state and is not an error.
    """
    if len(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID) != (
        EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE
    ):
        raise HistoricalPersonTagCleanupError(
            "map size is "
            f"{len(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID)}, expected "
            f"{EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE}"
        )

    required_tag_ids = _mapped_tag_ids()
    required_person_ids = _mapped_person_ids()
    if len(set(required_tag_ids)) != EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE:
        raise HistoricalPersonTagCleanupError("mapped Tag ids are not unique")
    if len(set(required_person_ids)) != EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE:
        raise HistoricalPersonTagCleanupError("mapped Person ids are not unique")

    found_tag_ids = set(
        Tag.objects.filter(pk__in=required_tag_ids).values_list("pk", flat=True)
    )
    found_person_ids = set(
        Person.objects.filter(pk__in=required_person_ids).values_list("pk", flat=True)
    )
    parts: list[str] = []
    missing_person_ids = _sorted_missing_ids(required_person_ids, found_person_ids)
    if missing_person_ids:
        parts.append(f"missing Person ids: {missing_person_ids}")
    if found_tag_ids and found_tag_ids != set(required_tag_ids):
        missing_tag_ids = _sorted_missing_ids(required_tag_ids, found_tag_ids)
        parts.append(f"missing Tag ids: {missing_tag_ids}")
    if parts:
        raise HistoricalPersonTagCleanupError("; ".join(parts))


def _require_mapped_tag_id(tag_id: int) -> int:
    if not is_historical_person_name_tag(tag_id):
        raise HistoricalPersonTagCleanupError(
            f"planned rows include unmapped Tag id: {tag_id}"
        )
    person_id = person_id_for_historical_person_name_tag(tag_id)
    if person_id is None:
        raise HistoricalPersonTagCleanupError(
            f"planned rows include unmapped Tag id: {tag_id}"
        )
    return person_id


def _mapped_through_queryset(
    through_model, *, mapped_ids: frozenset[int], for_update: bool
):
    qs = through_model.objects.filter(tag_id__in=mapped_ids).order_by("id")
    if for_update:
        qs = qs.select_for_update()
    return qs


def _delete_planned_through_rows(
    *,
    through_model,
    planned_ids: list[int],
    mapped_ids: frozenset[int],
    label: str,
) -> None:
    """Delete only the planned mapped through PKs. Fail if the count drifts."""
    if planned_ids:
        found_ids = list(
            through_model.objects.filter(pk__in=planned_ids, tag_id__in=mapped_ids)
            .order_by("id")
            .values_list("pk", flat=True)
        )
        if found_ids != planned_ids:
            raise HistoricalPersonTagCleanupError(
                f"{label} through plan drift: planned={planned_ids} found={found_ids}"
            )
        deleted_count, _deleted_by_model = through_model.objects.filter(
            pk__in=planned_ids, tag_id__in=mapped_ids
        ).delete()
        if deleted_count != len(planned_ids):
            raise HistoricalPersonTagCleanupError(
                f"{label} through delete count mismatch: "
                f"deleted={deleted_count} planned={len(planned_ids)}"
            )
        if through_model.objects.filter(pk__in=planned_ids).exists():
            raise HistoricalPersonTagCleanupError(
                f"{label} through rows remained after delete: {planned_ids}"
            )
    remaining_ids = list(
        through_model.objects.filter(tag_id__in=mapped_ids)
        .order_by("id")
        .values_list("pk", flat=True)
    )
    if remaining_ids:
        raise HistoricalPersonTagCleanupError(
            f"{label} mapped through rows remained after delete: {remaining_ids}"
        )


def build_historical_person_tag_cleanup_plan(
    *,
    for_update: bool = False,
) -> HistoricalPersonTagCleanupPlan:
    """Read-only plan of mapped through rows to delete. Does not write.

    ``for_update=True`` must be used inside ``transaction.atomic()`` so apply
    locks the planned through rows before deletion.
    """
    validate_historical_person_tag_cleanup_preconditions()
    mapped_ids = historical_person_name_tag_ids()

    archive_item_tag_rows: list[ArchiveItemTagCleanupRow] = []
    for rel in _mapped_through_queryset(
        ArchiveItem.tags.through,
        mapped_ids=mapped_ids,
        for_update=for_update,
    ).values("id", "archiveitem_id", "tag_id"):
        person_id = _require_mapped_tag_id(rel["tag_id"])
        archive_item_tag_rows.append(
            ArchiveItemTagCleanupRow(
                through_id=rel["id"],
                archive_item_id=rel["archiveitem_id"],
                tag_id=rel["tag_id"],
                person_id=person_id,
            )
        )

    document_tag_rows: list[DocumentTagCleanupRow] = []
    for rel in _mapped_through_queryset(
        Document.tags_m2m.through,
        mapped_ids=mapped_ids,
        for_update=for_update,
    ).values("id", "document_id", "tag_id"):
        _require_mapped_tag_id(rel["tag_id"])
        document_tag_rows.append(
            DocumentTagCleanupRow(
                through_id=rel["id"],
                document_id=rel["document_id"],
                tag_id=rel["tag_id"],
            )
        )

    missing_aip: list[tuple[int, int, int, int]] = []
    aip_pairs = set(
        ArchiveItemPerson.objects.filter(
            person_id__in=_mapped_person_ids()
        ).values_list("archive_item_id", "person_id")
    )
    for row in archive_item_tag_rows:
        if (row.archive_item_id, row.person_id) not in aip_pairs:
            missing_aip.append(row.as_tuple())
    if missing_aip:
        raise HistoricalPersonTagCleanupError(
            "mapped ArchiveItem.tags relations lack ArchiveItemPerson: "
            + ", ".join(str(row) for row in missing_aip)
        )

    return HistoricalPersonTagCleanupPlan(
        archive_item_tag_rows=archive_item_tag_rows,
        document_tag_rows=document_tag_rows,
    )


def apply_historical_person_tag_relation_cleanup() -> HistoricalPersonTagCleanupPlan:
    """Delete mapped through rows only. Keeps Tag/Person/AIP/PhotoPerson rows."""
    with transaction.atomic():
        plan = build_historical_person_tag_cleanup_plan(for_update=True)
        mapped_ids = historical_person_name_tag_ids()
        _delete_planned_through_rows(
            through_model=ArchiveItem.tags.through,
            planned_ids=[row.through_id for row in plan.archive_item_tag_rows],
            mapped_ids=mapped_ids,
            label="ArchiveItem.tags",
        )
        _delete_planned_through_rows(
            through_model=Document.tags_m2m.through,
            planned_ids=[row.through_id for row in plan.document_tag_rows],
            mapped_ids=mapped_ids,
            label="Document.tags_m2m",
        )
        sync_archive_item_search_indexes(plan.affected_archive_item_ids)
        plan.deleted_archive_item_tag_rows = list(plan.archive_item_tag_rows)
        plan.deleted_document_tag_rows = list(plan.document_tag_rows)
        return plan
