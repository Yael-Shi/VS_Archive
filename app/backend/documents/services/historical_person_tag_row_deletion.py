"""D2b: delete the 29 frozen historical person-name Tag *rows* only.

Identity is the frozen (Tag.id, Person.id, exact Tag.name) triples.
Through relations, Person/AIP/PhotoPerson, search indexes, and sequences
are never written. Default callers plan only; writes require
``--apply-rows``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from django.db import transaction
from django.db.models import Q

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_RECORDS,
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
    historical_person_name_tag_ids,
    historical_person_tag_retired_names,
)
from documents.models import (
    ArchiveItem,
    ArchiveMetadataSuggestion,
    Document,
    Person,
    Tag,
)
from documents.services.archive_metadata_suggestion_review import (
    pending_archive_metadata_suggestions_with_retired_tag_names,
)

EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE = 29
STATE_ALL_PRESENT = "all_present"
STATE_ALL_ABSENT = "all_absent"
MappedTagRowState = Literal["all_present", "all_absent"]


class HistoricalPersonTagRowDeletionError(Exception):
    """Fail-closed precondition or delete-count failure; no Tag-row writes."""


@dataclass(frozen=True)
class HistoricalPersonTagRow:
    tag_id: int
    name: str

    def as_tuple(self) -> tuple[int, str]:
        return (self.tag_id, self.name)


@dataclass
class HistoricalPersonTagRowDeletionPlan:
    state: MappedTagRowState = STATE_ALL_ABSENT
    planned_tag_rows: list[HistoricalPersonTagRow] = field(default_factory=list)
    deleted_tag_rows: list[HistoricalPersonTagRow] = field(default_factory=list)
    django_delete_total: int = 0
    django_delete_per_model: dict[str, int] = field(default_factory=dict)
    remaining_mapped_tag_rows: int = 0
    mapped_archiveitem_tag_relations: int = 0
    mapped_document_tag_relations: int = 0
    pending_retired_name_suggestions: int = 0

    @property
    def planned_count(self) -> int:
        return len(self.planned_tag_rows)

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_tag_rows)


def _sorted_missing_ids(required: list[int], found: set[int]) -> list[int]:
    return [pk for pk in required if pk not in found]


def _mapped_tag_ids() -> list[int]:
    return [tag_id for tag_id, _person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS]


def _mapped_person_ids() -> list[int]:
    return [
        person_id for _tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS
    ]


def _expected_tag_pairs() -> list[tuple[int, str]]:
    return [
        (tag_id, name)
        for tag_id, _person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS
    ]


def _expected_delete_per_model() -> dict[str, int]:
    return {Tag._meta.label: EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE}


def _exact_frozen_tag_q() -> Q:
    query = Q()
    for tag_id, name in _expected_tag_pairs():
        query |= Q(pk=tag_id, name=name)
    return query


def _validate_map_artifact() -> None:
    if len(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID) != (
        EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE
    ):
        raise HistoricalPersonTagRowDeletionError(
            "map size is "
            f"{len(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID)}, expected "
            f"{EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE}"
        )
    required_tag_ids = _mapped_tag_ids()
    required_person_ids = _mapped_person_ids()
    if len(set(required_tag_ids)) != EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE:
        raise HistoricalPersonTagRowDeletionError("mapped Tag ids are not unique")
    if len(set(required_person_ids)) != EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE:
        raise HistoricalPersonTagRowDeletionError("mapped Person ids are not unique")


def _lock_apply_rows() -> None:
    mapped_ids = historical_person_name_tag_ids()
    list(
        ArchiveMetadataSuggestion.objects.filter(
            status=ArchiveMetadataSuggestion.Status.PENDING
        )
        .order_by("pk")
        .select_for_update()
    )
    list(
        ArchiveItem.tags.through.objects.filter(tag_id__in=mapped_ids)
        .order_by("id")
        .select_for_update()
    )
    list(
        Document.tags_m2m.through.objects.filter(tag_id__in=mapped_ids)
        .order_by("id")
        .select_for_update()
    )
    list(Tag.objects.filter(pk__in=mapped_ids).order_by("pk").select_for_update())


def _require_persons_present() -> None:
    required_person_ids = _mapped_person_ids()
    found_person_ids = set(
        Person.objects.filter(pk__in=required_person_ids).values_list("pk", flat=True)
    )
    missing_person_ids = _sorted_missing_ids(required_person_ids, found_person_ids)
    if missing_person_ids:
        raise HistoricalPersonTagRowDeletionError(
            f"missing Person ids: {missing_person_ids}"
        )


def _require_no_mapped_through_rows() -> tuple[int, int]:
    mapped_ids = historical_person_name_tag_ids()
    archiveitem_ids = list(
        ArchiveItem.tags.through.objects.filter(tag_id__in=mapped_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    document_ids = list(
        Document.tags_m2m.through.objects.filter(tag_id__in=mapped_ids)
        .order_by("id")
        .values_list("id", flat=True)
    )
    parts: list[str] = []
    if archiveitem_ids:
        parts.append(f"mapped ArchiveItem.tags through rows exist: {archiveitem_ids}")
    if document_ids:
        parts.append(f"mapped Document.tags_m2m through rows exist: {document_ids}")
    if parts:
        raise HistoricalPersonTagRowDeletionError("; ".join(parts))
    return (len(archiveitem_ids), len(document_ids))


def _require_empty_pending_retired_name_inventory() -> int:
    inventory = pending_archive_metadata_suggestions_with_retired_tag_names()
    if inventory:
        suggestion_ids = [row.suggestion_id for row in inventory]
        raise HistoricalPersonTagRowDeletionError(
            f"pending retired-name ArchiveMetadataSuggestion ids: {suggestion_ids}"
        )
    return 0


def _require_no_retired_names_on_other_pks() -> None:
    frozen_ids = historical_person_name_tag_ids()
    retired_names = historical_person_tag_retired_names()
    rogue = list(
        Tag.objects.filter(name__in=retired_names)
        .exclude(pk__in=frozen_ids)
        .order_by("pk")
        .values_list("pk", "name")
    )
    if rogue:
        raise HistoricalPersonTagRowDeletionError(
            "retired historical Tag names on non-mapped ids: "
            f"{[(pk, name) for pk, name in rogue]}"
        )


def _classify_mapped_tag_rows() -> tuple[
    MappedTagRowState, list[HistoricalPersonTagRow]
]:
    expected_pairs = _expected_tag_pairs()
    expected_ids = [tag_id for tag_id, _name in expected_pairs]
    found_rows = list(
        Tag.objects.filter(pk__in=expected_ids).order_by("pk").values_list("pk", "name")
    )
    found_ids = {pk for pk, _name in found_rows}
    expected_id_set = set(expected_ids)

    if not found_ids:
        state: MappedTagRowState = STATE_ALL_ABSENT
        planned: list[HistoricalPersonTagRow] = []
    elif found_ids == expected_id_set:
        found_pair_set = set(found_rows)
        expected_pair_set = set(expected_pairs)
        if found_pair_set != expected_pair_set:
            mismatches = []
            found_name_by_id = dict(found_rows)
            for tag_id, expected_name in expected_pairs:
                actual_name = found_name_by_id.get(tag_id)
                if actual_name != expected_name:
                    mismatches.append((tag_id, actual_name, expected_name))
            raise HistoricalPersonTagRowDeletionError(
                f"mapped Tag name mismatch: {mismatches}"
            )
        state = STATE_ALL_PRESENT
        planned = [
            HistoricalPersonTagRow(tag_id=tag_id, name=name)
            for tag_id, name in expected_pairs
        ]
    else:
        missing_tag_ids = _sorted_missing_ids(expected_ids, found_ids)
        raise HistoricalPersonTagRowDeletionError(f"missing Tag ids: {missing_tag_ids}")

    _require_no_retired_names_on_other_pks()
    return state, planned


def _remaining_mapped_tag_count() -> int:
    return Tag.objects.filter(pk__in=_mapped_tag_ids()).count()


def _assert_absent_postconditions() -> None:
    remaining_ids = list(
        Tag.objects.filter(pk__in=_mapped_tag_ids())
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if remaining_ids:
        raise HistoricalPersonTagRowDeletionError(
            f"mapped Tag rows remained after delete: {remaining_ids}"
        )
    _require_no_mapped_through_rows()
    _require_no_retired_names_on_other_pks()


def build_historical_person_tag_row_deletion_plan(
    *,
    for_update: bool = False,
) -> HistoricalPersonTagRowDeletionPlan:
    """Read-only plan of frozen Tag rows to delete. Does not write.

    ``for_update=True`` must be used inside ``transaction.atomic()`` so apply
    locks Tag, through, and PENDING suggestion rows before deletion.
    """
    _validate_map_artifact()
    if for_update:
        _lock_apply_rows()
    _require_persons_present()
    archiveitem_count, document_count = _require_no_mapped_through_rows()
    pending_count = _require_empty_pending_retired_name_inventory()
    state, planned = _classify_mapped_tag_rows()
    remaining = _remaining_mapped_tag_count()
    if state == STATE_ALL_PRESENT and remaining != (
        EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE
    ):
        raise HistoricalPersonTagRowDeletionError(
            "mapped Tag row count mismatch: "
            f"found={remaining} expected={EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE}"
        )
    if state == STATE_ALL_ABSENT and remaining != 0:
        raise HistoricalPersonTagRowDeletionError(
            f"mapped Tag rows remained in all-absent state: remaining={remaining}"
        )
    return HistoricalPersonTagRowDeletionPlan(
        state=state,
        planned_tag_rows=planned,
        remaining_mapped_tag_rows=remaining,
        mapped_archiveitem_tag_relations=archiveitem_count,
        mapped_document_tag_relations=document_count,
        pending_retired_name_suggestions=pending_count,
    )


def apply_historical_person_tag_row_deletion() -> HistoricalPersonTagRowDeletionPlan:
    """Delete the 29 exact frozen Tag rows, or no-op when already absent."""
    with transaction.atomic():
        plan = build_historical_person_tag_row_deletion_plan(for_update=True)
        if plan.state == STATE_ALL_ABSENT:
            _assert_absent_postconditions()
            plan.deleted_tag_rows = []
            plan.django_delete_total = 0
            plan.django_delete_per_model = {}
            plan.remaining_mapped_tag_rows = 0
            return plan

        total, per_model = Tag.objects.filter(_exact_frozen_tag_q()).delete()
        expected_per_model = _expected_delete_per_model()
        if total != EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE or (
            per_model != expected_per_model
        ):
            raise HistoricalPersonTagRowDeletionError(
                "Tag row delete count mismatch: "
                f"deleted={total} per_model={per_model} "
                f"expected_total={EXPECTED_HISTORICAL_PERSON_NAME_TAG_MAP_SIZE} "
                f"expected_per_model={expected_per_model}"
            )
        _assert_absent_postconditions()
        plan.deleted_tag_rows = list(plan.planned_tag_rows)
        plan.django_delete_total = total
        plan.django_delete_per_model = dict(per_model)
        plan.remaining_mapped_tag_rows = 0
        return plan
