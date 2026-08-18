"""Staff management of multiple PhotoContent rows under one PHOTO ArchiveItem."""

from __future__ import annotations

from dataclasses import dataclass

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Prefetch

from documents.models import ArchiveItem, Person, PersonAlias, PhotoContent
from documents.s3 import create_presigned_get
from documents.services.photo_s3_cleanup import schedule_photo_s3_cleanup_after_commit

LAST_PHOTO_DELETE_ERROR = (
    "לא ניתן למחוק את התמונה האחרונה בפריט. מחיקת פריט הארכיון כולו תמחק את כל התמונות."
)
PHOTO_REORDER_INVALID_ERROR = "סדר התמונות שנשלח אינו תקין."
PHOTO_NOT_IN_ITEM_ERROR = "התמונה אינה שייכת לפריט זה."
PHOTO_POSITION_CONFLICT_ERROR = "מיקום התמונה כבר תפוס. נסו שוב."
PERSON_NOT_FOUND_ERROR = "אדם מזוהה לא נמצא."
PERSON_NAME_REQUIRED_ERROR = "שם האדם המזוהה נדרש."
PERSON_NAME_TOO_LONG_ERROR = "שם האדם המזוהה חייב להיות עד 255 תווים."
PERSON_ALIAS_REQUIRED_ERROR = "שם חלופי נדרש."
PERSON_ALIAS_TOO_LONG_ERROR = "השם החלופי חייב להיות עד 255 תווים."
PERSON_ALIAS_MATCHES_CANONICAL_ERROR = "השם החלופי אינו יכול להיות זהה לשם התצוגה."
PERSON_ALIAS_DUPLICATE_ERROR = "שם חלופי זה כבר קיים עבור אדם זה."
ARCHIVE_ITEM_NOT_PHOTO_ERROR = "archive item is not PHOTO"


class PhotoContentManagementError(Exception):
    """Staff-facing PHOTO component management error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class StaffPhotoManageRow:
    photo: PhotoContent
    thumbnail_url: str | None
    move_up_ids: list[int] | None
    move_down_ids: list[int] | None
    identified_people_summary: str


@dataclass(frozen=True)
class StaffPersonChoice:
    id: int
    name: str
    label: str
    selected: bool = False


def staff_person_aliases_prefetch() -> Prefetch:
    """Prefetch aliases in the same deterministic ``(name, id)`` order as the model."""
    return Prefetch(
        "aliases",
        queryset=PersonAlias.objects.order_by("name", "id"),
    )


def staff_person_picker_queryset():
    return Person.objects.order_by("name", "id").prefetch_related(
        staff_person_aliases_prefetch()
    )


def person_staff_picker_label(person: Person) -> str:
    """Canonical name, plus aliases in ``(name, id)`` order when present.

    Does not include Person ids. Empty/whitespace-only alias names are omitted.
    Callers must prefetch ``aliases`` when rendering many people.
    """
    alias_names = [
        alias.name for alias in person.aliases.all() if (alias.name or "").strip()
    ]
    if not alias_names:
        return person.name
    return f"{person.name} ({', '.join(alias_names)})"


def build_staff_person_choices(
    *,
    selected_person_ids: list[int] | tuple[int, ...] | set[int],
) -> tuple[list[StaffPersonChoice], list[StaffPersonChoice]]:
    """Return picker choices and the selected subset, both ordered by ``(name, id)``."""
    selected_set = {int(person_id) for person_id in selected_person_ids}
    choices: list[StaffPersonChoice] = []
    selected: list[StaffPersonChoice] = []
    for person in staff_person_picker_queryset():
        choice = StaffPersonChoice(
            id=person.pk,
            name=person.name,
            label=person_staff_picker_label(person),
            selected=person.pk in selected_set,
        )
        choices.append(choice)
        if choice.selected:
            selected.append(choice)
    return choices, selected


def lock_photo_archive_item(archive_item_id: int) -> ArchiveItem:
    """Lock the PHOTO ArchiveItem row; must run inside ``transaction.atomic``."""
    item = ArchiveItem.objects.select_for_update().filter(pk=archive_item_id).first()
    if item is None:
        raise ArchiveItem.DoesNotExist
    if item.item_type != ArchiveItem.ItemType.PHOTO:
        raise PhotoContentManagementError(ARCHIVE_ITEM_NOT_PHOTO_ERROR)
    return item


def lock_photo_contents_for_item(archive_item: ArchiveItem) -> list[PhotoContent]:
    """Lock this item's PhotoContent rows in stable id order after the item lock."""
    return list(archive_item.photo_contents.select_for_update().order_by("id"))


def next_photo_position(archive_item: ArchiveItem) -> int:
    """Return max(position)+1 for the locked item. Never uses the model default."""
    current_max = archive_item.photo_contents.aggregate(value=Max("position"))["value"]
    return (current_max or 0) + 1


def _ordered_photo_ids(photos: list[PhotoContent]) -> list[int]:
    return [
        photo.pk for photo in sorted(photos, key=lambda row: (row.position, row.pk))
    ]


def _renumber_photos(photos_in_final_order: list[PhotoContent]) -> None:
    """Assign contiguous positions 1..N without unique-constraint collisions.

    Temporary positions are ``current_max + 1..N`` among the rows being
    moved, so they cannot collide with any still-occupied position.
    """
    if not photos_in_final_order:
        return
    current_max = max(photo.position for photo in photos_in_final_order)
    for index, photo in enumerate(photos_in_final_order, start=1):
        PhotoContent.objects.filter(pk=photo.pk).update(position=current_max + index)
    for position, photo in enumerate(photos_in_final_order, start=1):
        PhotoContent.objects.filter(pk=photo.pk).update(position=position)
        photo.position = position


def parse_photo_person_ids(post_data) -> tuple[list[int], list[str]]:
    if hasattr(post_data, "getlist"):
        raw_values = post_data.getlist("person_ids")
    else:
        raw = post_data.get("person_ids")
        if raw is None:
            raw_values = []
        elif isinstance(raw, (list, tuple)):
            raw_values = list(raw)
        else:
            raw_values = [raw]

    person_ids: list[int] = []
    seen: set[int] = set()
    errors: list[str] = []
    for raw in raw_values:
        text = str(raw).strip()
        if not text:
            continue
        try:
            person_id = int(text)
        except (TypeError, ValueError):
            errors.append(PERSON_NOT_FOUND_ERROR)
            return [], errors
        if person_id < 1:
            errors.append(PERSON_NOT_FOUND_ERROR)
            return [], errors
        if person_id not in seen:
            seen.add(person_id)
            person_ids.append(person_id)
    return person_ids, errors


def parse_new_person_name(post_data) -> tuple[str, list[str]]:
    raw = post_data.get("new_person_name") if post_data is not None else None
    name = (raw or "").strip()
    if len(name) > 255:
        return name, [PERSON_NAME_TOO_LONG_ERROR]
    return name, []


def create_identified_person(*, name: str) -> Person:
    normalized = (name or "").strip()
    if not normalized:
        raise PhotoContentManagementError(PERSON_NAME_REQUIRED_ERROR)
    if len(normalized) > 255:
        raise PhotoContentManagementError(PERSON_NAME_TOO_LONG_ERROR)
    return Person.objects.create(name=normalized)


def _sync_person_search_indexes(person_id: int) -> None:
    from documents.services.archive_search_index import (
        archive_item_ids_for_person_search_refresh,
        sync_archive_item_search_indexes,
    )

    sync_archive_item_search_indexes(
        archive_item_ids_for_person_search_refresh(person_id)
    )


def _normalize_person_alias_name(name: str, *, person: Person) -> str:
    normalized = (name or "").strip()
    if not normalized:
        raise PhotoContentManagementError(PERSON_ALIAS_REQUIRED_ERROR)
    if len(normalized) > 255:
        raise PhotoContentManagementError(PERSON_ALIAS_TOO_LONG_ERROR)
    if normalized == (person.name or "").strip():
        raise PhotoContentManagementError(PERSON_ALIAS_MATCHES_CANONICAL_ERROR)
    return normalized


@transaction.atomic
def update_person_name(person: Person, *, name: str) -> Person:
    """Rename a Person and refresh search indexes for linked ArchiveItems.

    Fans out to items reached through ArchiveItemPerson and through
    PhotoPerson. One rebuild per ArchiveItem even when both relations
    exist. Does not rewrite, delete, or promote PersonAlias rows.
    Raw ``Person.save()`` / ``QuerySet.update()`` are not hooked; callers
    that change ``name`` must use this service.
    """
    normalized = (name or "").strip()
    if not normalized:
        raise PhotoContentManagementError(PERSON_NAME_REQUIRED_ERROR)
    if len(normalized) > 255:
        raise PhotoContentManagementError(PERSON_NAME_TOO_LONG_ERROR)
    if person.name == normalized:
        return person
    person.name = normalized
    person.save(update_fields=["name", "updated_at"])
    _sync_person_search_indexes(person.pk)
    return person


@transaction.atomic
def create_person_alias(person: Person, *, name: str) -> PersonAlias:
    """Create an alias and refresh search indexes for linked ArchiveItems.

    Strips surrounding whitespace only. Does not rewrite ``Person.name`` or
    touch PhotoPerson / ArchiveItemPerson / Tag rows. Duplicate
    ``(person, name)`` is a uniqueness error, not a merge. Fan-out covers
    ArchiveItemPerson and PhotoPerson; one rebuild per ArchiveItem.
    """
    normalized = _normalize_person_alias_name(name, person=person)
    try:
        alias = PersonAlias.objects.create(person=person, name=normalized)
    except IntegrityError as exc:
        raise PhotoContentManagementError(PERSON_ALIAS_DUPLICATE_ERROR) from exc
    _sync_person_search_indexes(person.pk)
    return alias


@transaction.atomic
def update_person_alias(alias: PersonAlias, *, name: str) -> PersonAlias:
    """Rename an alias and refresh search indexes for linked ArchiveItems."""
    person = alias.person
    normalized = _normalize_person_alias_name(name, person=person)
    if alias.name == normalized:
        return alias
    alias.name = normalized
    try:
        alias.save(update_fields=["name", "updated_at"])
    except IntegrityError as exc:
        raise PhotoContentManagementError(PERSON_ALIAS_DUPLICATE_ERROR) from exc
    _sync_person_search_indexes(person.pk)
    return alias


@transaction.atomic
def delete_person_alias(alias: PersonAlias) -> None:
    """Delete an alias and refresh search indexes for linked ArchiveItems."""
    person_id = alias.person_id
    alias.delete()
    _sync_person_search_indexes(person_id)


def set_photo_people(photo_content: PhotoContent, person_ids: list[int]) -> None:
    """Replace PhotoPerson links only. Does not create ArchiveItemPerson rows."""
    unique_ids = list(dict.fromkeys(person_ids))
    persons = list(Person.objects.filter(pk__in=unique_ids))
    if len(persons) != len(unique_ids):
        raise PhotoContentManagementError(PERSON_NOT_FOUND_ERROR)
    by_id = {person.pk: person for person in persons}
    photo_content.people.set([by_id[person_id] for person_id in unique_ids])


@transaction.atomic
def update_photo_content_metadata(
    photo_content: PhotoContent,
    *,
    description: str,
    location: str,
    context: str,
    people_present: str,
    notes: str,
    date_start,
    date_end,
    date_precision: str,
    person_ids: list[int],
    new_person_name: str = "",
) -> PhotoContent:
    """Update one PhotoContent row. Does not write ArchiveItem shared metadata."""
    locked_item = lock_photo_archive_item(photo_content.archive_item_id)
    locked_photos = lock_photo_contents_for_item(locked_item)
    locked = next(
        (row for row in locked_photos if row.pk == photo_content.pk),
        None,
    )
    if locked is None:
        raise PhotoContent.DoesNotExist

    locked.description = description
    locked.location = location
    locked.context = context
    locked.people_present = people_present
    locked.notes = notes
    locked.date_start = date_start
    locked.date_end = date_end
    locked.date_precision = date_precision
    locked.full_clean()
    locked.save(
        update_fields=[
            "description",
            "location",
            "context",
            "people_present",
            "notes",
            "date_start",
            "date_end",
            "date_precision",
            "updated_at",
        ]
    )

    resolved_person_ids = list(person_ids)
    if new_person_name:
        created = create_identified_person(name=new_person_name)
        resolved_person_ids.append(created.pk)
    set_photo_people(locked, resolved_person_ids)

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(locked_item.pk)
    return locked


@transaction.atomic
def reorder_photo_contents(
    archive_item: ArchiveItem,
    ordered_photo_ids: list[int],
) -> list[PhotoContent]:
    locked_item = lock_photo_archive_item(archive_item.pk)
    photos = lock_photo_contents_for_item(locked_item)
    existing_ids = set(_ordered_photo_ids(photos))
    requested_ids = list(ordered_photo_ids)

    if not requested_ids or len(requested_ids) != len(set(requested_ids)):
        raise PhotoContentManagementError(PHOTO_REORDER_INVALID_ERROR)
    if set(requested_ids) != existing_ids:
        raise PhotoContentManagementError(PHOTO_REORDER_INVALID_ERROR)
    if len(requested_ids) != len(photos):
        raise PhotoContentManagementError(PHOTO_REORDER_INVALID_ERROR)

    by_id = {photo.pk: photo for photo in photos}
    ordered = [by_id[photo_id] for photo_id in requested_ids]
    _renumber_photos(ordered)

    from documents.services.archive_search_index import sync_archive_item_search_index

    # Aggregation order is ``(position, id)``; keep the derived index aligned.
    sync_archive_item_search_index(locked_item.pk)
    return ordered


@transaction.atomic
def delete_one_photo_content(
    photo_content: PhotoContent,
    *,
    bucket: str,
) -> ArchiveItem:
    locked_item = lock_photo_archive_item(photo_content.archive_item_id)
    photos = lock_photo_contents_for_item(locked_item)
    locked = next((row for row in photos if row.pk == photo_content.pk), None)
    if locked is None:
        raise PhotoContent.DoesNotExist
    if len(photos) <= 1:
        raise PhotoContentManagementError(LAST_PHOTO_DELETE_ERROR)

    original_file_key = locked.original_file_key
    thumbnail_file_key = locked.thumbnail_file_key
    photo_content_id = locked.pk
    remaining = [row for row in photos if row.pk != locked.pk]
    remaining_ordered = sorted(remaining, key=lambda row: (row.position, row.pk))

    locked.delete()
    _renumber_photos(remaining_ordered)
    schedule_photo_s3_cleanup_after_commit(
        bucket=bucket,
        original_file_key=original_file_key,
        thumbnail_file_key=thumbnail_file_key,
        photo_content_id=photo_content_id,
    )

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(locked_item.pk)
    return locked_item


def staff_photo_thumbnail_url(
    photo_content: PhotoContent,
    *,
    bucket: str,
    expires_in: int = 3600,
) -> str | None:
    normalized_bucket = (bucket or "").strip()
    thumbnail_key = (photo_content.thumbnail_file_key or "").strip()
    if not normalized_bucket or not thumbnail_key:
        return None
    try:
        return create_presigned_get(
            bucket=normalized_bucket,
            key=thumbnail_key,
            expires_in=expires_in,
        )
    except (BotoCoreError, ClientError):
        return None


def staff_photo_contents_queryset(archive_item: ArchiveItem):
    return (
        PhotoContent.objects.filter(archive_item_id=archive_item.pk)
        .order_by("position", "id")
        .prefetch_related("people")
    )


def build_staff_photo_manage_rows(
    archive_item: ArchiveItem,
    *,
    photos: list[PhotoContent] | None = None,
    bucket: str | None = None,
    expires_in: int = 3600,
) -> list[StaffPhotoManageRow]:
    if photos is None:
        photos = list(staff_photo_contents_queryset(archive_item))
    else:
        photos = list(photos)
    photos.sort(key=lambda photo: (photo.position, photo.pk))
    ordered_ids = [photo.pk for photo in photos]
    resolved_bucket = (
        bucket if bucket is not None else getattr(settings, "UPLOADS_BUCKET_NAME", "")
    )
    rows: list[StaffPhotoManageRow] = []
    for index, photo in enumerate(photos):
        move_up_ids = None
        move_down_ids = None
        if index > 0:
            swapped = list(ordered_ids)
            swapped[index - 1], swapped[index] = swapped[index], swapped[index - 1]
            move_up_ids = swapped
        if index < len(ordered_ids) - 1:
            swapped = list(ordered_ids)
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            move_down_ids = swapped
        people = list(photo.people.all())
        people.sort(key=lambda person: (person.name, person.pk))
        rows.append(
            StaffPhotoManageRow(
                photo=photo,
                thumbnail_url=staff_photo_thumbnail_url(
                    photo,
                    bucket=resolved_bucket,
                    expires_in=expires_in,
                ),
                move_up_ids=move_up_ids,
                move_down_ids=move_down_ids,
                identified_people_summary=", ".join(person.name for person in people),
            )
        )
    return rows


def wrap_integrity_position_conflict(
    exc: IntegrityError,
) -> PhotoContentManagementError:
    _ = exc
    return PhotoContentManagementError(PHOTO_POSITION_CONFLICT_ERROR)
