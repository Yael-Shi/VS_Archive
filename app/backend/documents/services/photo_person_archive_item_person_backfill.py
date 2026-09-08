"""Backfill ArchiveItemPerson from existing PhotoPerson rows.

Add-only and idempotent. Does not write PhotoPerson, Person, Author,
people_present, or aliases. Default callers plan only; writes require apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import transaction

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    PhotoPerson,
)
from documents.services.archive_item_people import ensure_archive_item_person
from documents.services.archive_search_index import sync_archive_item_search_indexes

STATUS_CREATE = "CREATE"
STATUS_NOOP = "NOOP"
STATUS_ERROR = "ERROR"


class PhotoPersonArchiveItemPersonBackfillError(Exception):
    """Unexpected parent or data-integrity failure. Fail closed."""


@dataclass(frozen=True)
class BackfillRow:
    photo_person_id: int
    photo_content_id: int
    archive_item_id: int | None
    person_id: int
    status: str
    reason: str

    def as_tuple(self) -> tuple[int, int, int | None, int, str, str]:
        return (
            self.photo_person_id,
            self.photo_content_id,
            self.archive_item_id,
            self.person_id,
            self.status,
            self.reason,
        )


@dataclass
class BackfillPlan:
    rows: list[BackfillRow] = field(default_factory=list)
    created_archive_item_ids: tuple[int, ...] = ()
    applied: bool = False

    @property
    def create_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_CREATE)

    @property
    def noop_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_NOOP)

    @property
    def error_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_ERROR)


def _classify_photo_person(link: PhotoPerson) -> BackfillRow:
    photo = link.photo_content
    archive_item = photo.archive_item
    if archive_item is None:
        return BackfillRow(
            photo_person_id=link.pk,
            photo_content_id=photo.pk,
            archive_item_id=None,
            person_id=link.person_id,
            status=STATUS_ERROR,
            reason="photo content is missing its archive item",
        )
    if archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        return BackfillRow(
            photo_person_id=link.pk,
            photo_content_id=photo.pk,
            archive_item_id=archive_item.pk,
            person_id=link.person_id,
            status=STATUS_ERROR,
            reason="parent archive item is not PHOTO",
        )
    already = ArchiveItemPerson.objects.filter(
        archive_item_id=archive_item.pk,
        person_id=link.person_id,
    ).exists()
    if already:
        return BackfillRow(
            photo_person_id=link.pk,
            photo_content_id=photo.pk,
            archive_item_id=archive_item.pk,
            person_id=link.person_id,
            status=STATUS_NOOP,
            reason="ArchiveItemPerson already exists",
        )
    return BackfillRow(
        photo_person_id=link.pk,
        photo_content_id=photo.pk,
        archive_item_id=archive_item.pk,
        person_id=link.person_id,
        status=STATUS_CREATE,
        reason="create ArchiveItemPerson from PhotoPerson",
    )


def build_photo_person_archive_item_person_backfill_plan() -> BackfillPlan:
    """Read-only plan. No writes."""
    rows: list[BackfillRow] = []
    queryset = PhotoPerson.objects.select_related(
        "photo_content",
        "photo_content__archive_item",
        "person",
    ).order_by("id")
    for link in queryset:
        rows.append(_classify_photo_person(link))
    return BackfillPlan(rows=rows)


def apply_photo_person_archive_item_person_backfill() -> BackfillPlan:
    """Create missing ArchiveItemPerson rows in one transaction, then refresh indexes.

    Returned row statuses are actual apply outcomes, not the pre-apply plan.
    Several PhotoPerson rows for the same item/person may all plan as CREATE;
    only the first successful ensure is CREATE, later rows are NOOP.
    """
    with transaction.atomic():
        plan = build_photo_person_archive_item_person_backfill_plan()
        if plan.error_count:
            raise PhotoPersonArchiveItemPersonBackfillError(
                "photo-person AIP backfill found integrity errors; refusing to continue"
            )
        created_item_ids: set[int] = set()
        applied_rows: list[BackfillRow] = []
        for row in plan.rows:
            if row.status != STATUS_CREATE:
                applied_rows.append(row)
                continue
            link = PhotoPerson.objects.select_related(
                "photo_content",
                "photo_content__archive_item",
                "person",
            ).get(pk=row.photo_person_id)
            classified = _classify_photo_person(link)
            if classified.status == STATUS_ERROR:
                raise PhotoPersonArchiveItemPersonBackfillError(classified.reason)
            if classified.status == STATUS_NOOP:
                applied_rows.append(classified)
                continue
            _link, created = ensure_archive_item_person(
                archive_item=link.photo_content.archive_item,
                person=link.person,
                refresh_search_index=False,
            )
            if created:
                created_item_ids.add(link.photo_content.archive_item_id)
                applied_rows.append(classified)
            else:
                applied_rows.append(
                    BackfillRow(
                        photo_person_id=classified.photo_person_id,
                        photo_content_id=classified.photo_content_id,
                        archive_item_id=classified.archive_item_id,
                        person_id=classified.person_id,
                        status=STATUS_NOOP,
                        reason="ArchiveItemPerson already exists",
                    )
                )
        if created_item_ids:
            sync_archive_item_search_indexes(sorted(created_item_ids))
        return BackfillPlan(
            rows=applied_rows,
            created_archive_item_ids=tuple(sorted(created_item_ids)),
            applied=True,
        )
