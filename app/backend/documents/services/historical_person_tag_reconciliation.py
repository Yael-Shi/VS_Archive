"""Create-only reconciliation of missing ArchiveItemPerson links from the frozen map.

Identity is Tag.id → Person.id. Names, aliases, and PhotoPerson are never used.
Default callers plan only; writes require an explicit apply step.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
)
from documents.models import ArchiveItem, ArchiveItemPerson, Person, Tag
from documents.services.archive_item_people import create_archive_item_person


class HistoricalPersonTagReconciliationError(Exception):
    """Missing required Tag or Person ids, or apply failure."""


@dataclass(frozen=True)
class ReconciliationRow:
    tag_id: int
    person_id: int
    archive_item_id: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.tag_id, self.person_id, self.archive_item_id)


@dataclass
class ReconciliationPlan:
    planned: list[ReconciliationRow] = field(default_factory=list)
    already_present: list[ReconciliationRow] = field(default_factory=list)
    created: list[ReconciliationRow] = field(default_factory=list)

    @property
    def planned_count(self) -> int:
        return len(self.planned)

    @property
    def already_present_count(self) -> int:
        return len(self.already_present)

    @property
    def created_count(self) -> int:
        return len(self.created)


def _sorted_missing_ids(required: list[int], found: set[int]) -> list[int]:
    return [pk for pk in required if pk not in found]


def validate_required_historical_person_tag_ids() -> None:
    """Fail closed unless every frozen Tag.id and Person.id exists."""
    required_tag_ids = [
        tag_id for tag_id, _person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
    ]
    required_person_ids = [
        person_id for _tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
    ]
    found_tag_ids = set(
        Tag.objects.filter(pk__in=required_tag_ids).values_list("pk", flat=True)
    )
    found_person_ids = set(
        Person.objects.filter(pk__in=required_person_ids).values_list("pk", flat=True)
    )
    missing_tag_ids = _sorted_missing_ids(required_tag_ids, found_tag_ids)
    missing_person_ids = _sorted_missing_ids(required_person_ids, found_person_ids)
    parts: list[str] = []
    if missing_tag_ids:
        parts.append(f"missing Tag ids: {missing_tag_ids}")
    if missing_person_ids:
        parts.append(f"missing Person ids: {missing_person_ids}")
    if parts:
        raise HistoricalPersonTagReconciliationError("; ".join(parts))


def build_historical_person_tag_reconciliation_plan() -> ReconciliationPlan:
    """Read-only plan of missing and already-present ArchiveItemPerson links."""
    validate_required_historical_person_tag_ids()

    planned: list[ReconciliationRow] = []
    already_present: list[ReconciliationRow] = []
    for tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID:
        item_ids_with_tag = list(
            ArchiveItem.objects.filter(tags__pk=tag_id)
            .order_by("pk")
            .values_list("pk", flat=True)
            .distinct()
        )
        linked_item_ids = set(
            ArchiveItemPerson.objects.filter(person_id=person_id).values_list(
                "archive_item_id", flat=True
            )
        )
        for archive_item_id in item_ids_with_tag:
            row = ReconciliationRow(
                tag_id=tag_id,
                person_id=person_id,
                archive_item_id=archive_item_id,
            )
            if archive_item_id in linked_item_ids:
                already_present.append(row)
            else:
                planned.append(row)
    return ReconciliationPlan(planned=planned, already_present=already_present)


def apply_historical_person_tag_reconciliation() -> ReconciliationPlan:
    """Create missing ArchiveItemPerson links only. Never deletes or replaces."""
    created: list[ReconciliationRow] = []
    with transaction.atomic():
        plan = build_historical_person_tag_reconciliation_plan()
        for row in plan.planned:
            archive_item = ArchiveItem.objects.get(pk=row.archive_item_id)
            person = Person.objects.get(pk=row.person_id)
            create_archive_item_person(archive_item=archive_item, person=person)
            created.append(row)
        plan.created = created
        return plan
