"""Explicit ArchiveItemPerson create/delete with same-transaction search refresh.

Item-level person links are broader than photo appearances. PhotoPerson
writes ensure a matching ArchiveItemPerson (add-only). Item-level REPLACE
keeps every Person still implied by PhotoPerson on that ArchiveItem.
Callers that write ``ArchiveItemPerson`` directly must use these services;
raw model writes are not hooked. These helpers never create PhotoPerson
from AIP.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from documents.models import ArchiveItem, ArchiveItemPerson, Person, PhotoPerson
from documents.services.photo_content_management import (
    PERSON_NOT_FOUND_ERROR,
    PhotoContentManagementError,
    create_identified_people_from_new_names,
    parse_new_person_names_input,
)
from documents.services.person_duplicate_check import (
    FORCE_CREATE_PERSON_FIELD,
    PersonNameDuplicateConflictError,
    check_new_person_names,
    parse_force_create_person_keys,
)

ARCHIVE_ITEM_PERSON_IDS_FIELD = "archive_item_person_ids"
NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD = "new_archive_item_person_name"

ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR = (
    "this person is already linked to the archive item"
)


class ArchiveItemPersonError(Exception):
    """Staff/service-facing ArchiveItemPerson write error."""

    def __init__(self, message: str, *, check=None):
        super().__init__(message)
        self.message = message
        self.check = check


def empty_archive_item_people_form_fields() -> dict[str, Any]:
    """Empty staff form values for item-level people on create."""
    return {
        ARCHIVE_ITEM_PERSON_IDS_FIELD: [],
        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "",
        FORCE_CREATE_PERSON_FIELD: [],
        "person_name_conflicts": [],
    }


def archive_item_people_form_data_from_item(
    archive_item: ArchiveItem,
) -> dict[str, Any]:
    """Seed staff form values from current ArchiveItemPerson links."""
    return {
        ARCHIVE_ITEM_PERSON_IDS_FIELD: list(
            archive_item.people.order_by("name", "id").values_list("id", flat=True)
        ),
        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "",
        FORCE_CREATE_PERSON_FIELD: [],
        "person_name_conflicts": [],
    }


def parse_archive_item_person_ids(post_data) -> tuple[list[int], list[str]]:
    """Parse Person primary keys from the item-level people multi-select.

    Values must be positive integers. Names and aliases are not accepted.
    """
    if hasattr(post_data, "getlist"):
        raw_values = post_data.getlist(ARCHIVE_ITEM_PERSON_IDS_FIELD)
    else:
        raw = (
            post_data.get(ARCHIVE_ITEM_PERSON_IDS_FIELD)
            if post_data is not None
            else None
        )
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


def parse_new_archive_item_person_name(post_data) -> tuple[str, list[str]]:
    raw = (
        post_data.get(NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD)
        if post_data is not None
        else None
    )
    display, _names, errors = parse_new_person_names_input(raw)
    return display, errors


def parse_archive_item_people_form(post_data) -> tuple[dict[str, Any], list[str]]:
    """Parse item-level people fields and reject unknown Person ids."""
    person_ids, id_errors = parse_archive_item_person_ids(post_data)
    raw = (
        post_data.get(NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD)
        if post_data is not None
        else None
    )
    force_keys = parse_force_create_person_keys(post_data)
    name_check = check_new_person_names(raw, force_create_person_keys=force_keys)
    errors = id_errors + name_check.errors
    if not id_errors and person_ids:
        found = set(
            Person.objects.filter(pk__in=person_ids).values_list("pk", flat=True)
        )
        if found != set(person_ids):
            errors.append(PERSON_NOT_FOUND_ERROR)
    return {
        ARCHIVE_ITEM_PERSON_IDS_FIELD: person_ids,
        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: name_check.display,
        FORCE_CREATE_PERSON_FIELD: name_check.force_create_person_keys,
        "person_name_conflicts": list(name_check.matches),
    }, errors


def person_ids_required_by_photo_people(
    archive_item: ArchiveItem,
    *,
    for_update: bool = False,
) -> list[int]:
    """Distinct Person ids that currently appear on photos of this ArchiveItem.

    Order is first-seen ``PhotoPerson.id``. Duplicate appearances collapse to
    one id. Does not create PhotoPerson or ArchiveItemPerson rows.
    ``for_update=True`` locks matching PhotoPerson rows; callers must already
    be in a transaction (``set_archive_item_people``).
    """
    seen: set[int] = set()
    person_ids: list[int] = []
    queryset = PhotoPerson.objects.filter(
        photo_content__archive_item_id=archive_item.pk
    ).order_by("id")
    if for_update:
        queryset = queryset.select_for_update()
    for person_id in queryset.values_list("person_id", flat=True):
        if person_id in seen:
            continue
        seen.add(person_id)
        person_ids.append(person_id)
    return person_ids


def photo_person_requires_archive_item_person(
    *,
    archive_item_id: int,
    person_id: int,
) -> bool:
    """True when at least one PhotoPerson on this item still requires AIP."""
    return PhotoPerson.objects.filter(
        photo_content__archive_item_id=archive_item_id,
        person_id=person_id,
    ).exists()


def _union_person_ids(explicit_ids: list[int], required_ids: list[int]) -> list[int]:
    merged = list(dict.fromkeys(explicit_ids))
    seen = set(merged)
    for person_id in required_ids:
        if person_id not in seen:
            seen.add(person_id)
            merged.append(person_id)
    return merged


def _replace_archive_item_person_rows(
    *,
    archive_item: ArchiveItem,
    person_ids: list[int],
) -> tuple[list[ArchiveItemPerson], bool]:
    unique_ids = list(dict.fromkeys(person_ids))
    persons = list(Person.objects.filter(pk__in=unique_ids))
    if len(persons) != len(unique_ids):
        raise ArchiveItemPersonError(PERSON_NOT_FOUND_ERROR)

    by_id = {person.pk: person for person in persons}
    desired_persons = [by_id[person_id] for person_id in unique_ids]
    existing_links = list(
        ArchiveItemPerson.objects.select_for_update().filter(archive_item=archive_item)
    )
    existing_by_person_id = {link.person_id: link for link in existing_links}
    desired_ids = set(unique_ids)
    changed = False

    for person_id, link in existing_by_person_id.items():
        if person_id not in desired_ids:
            link.delete()
            changed = True

    kept_or_created: list[ArchiveItemPerson] = []
    for person in desired_persons:
        existing_link = existing_by_person_id.get(person.pk)
        if existing_link is None:
            kept_or_created.append(
                ArchiveItemPerson.objects.create(
                    archive_item=archive_item,
                    person=person,
                )
            )
            changed = True
            continue
        kept_or_created.append(existing_link)
    return kept_or_created, changed


@transaction.atomic
def set_archive_item_people(
    *,
    archive_item: ArchiveItem,
    person_ids: list[int],
    new_person_name: str = "",
    refresh_search_index: bool = True,
    force_create_person_keys: list[str] | None = None,
) -> list[ArchiveItemPerson]:
    """Replace ArchiveItemPerson links in one transaction.

    The persisted set is the explicit ``person_ids`` (plus any newly created
    names) unioned with Person ids implied by current PhotoPerson rows on
    this ArchiveItem. Staff may omit a photo-appearance Person from the
    form; save still keeps or restores that AIP. Does not create
    PhotoPerson from AIP, aliases, or Tags. Does not merge by name.
    Unknown Person ids are rejected. One search-index refresh when links
    change and ``refresh_search_index`` is true.

    Callers that already refresh this item in the same transaction (staff
    metadata save) may pass ``refresh_search_index=False`` so the later sync
    includes the new links.
    """
    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist

    resolved_ids = list(dict.fromkeys(person_ids))
    created_person = False
    try:
        created_people = create_identified_people_from_new_names(
            new_person_name,
            force_create_person_keys=force_create_person_keys,
        )
    except PersonNameDuplicateConflictError as exc:
        raise ArchiveItemPersonError(exc.message, check=exc.check) from exc
    except PhotoContentManagementError as exc:
        raise ArchiveItemPersonError(exc.message) from exc
    for created in created_people:
        resolved_ids.append(created.pk)
        created_person = True

    required_ids = person_ids_required_by_photo_people(locked_item, for_update=True)
    resolved_ids = _union_person_ids(resolved_ids, required_ids)

    links, changed = _replace_archive_item_person_rows(
        archive_item=locked_item,
        person_ids=resolved_ids,
    )
    if refresh_search_index and (changed or created_person):
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(locked_item.pk)
    return links


def ensure_archive_item_person(
    *,
    archive_item: ArchiveItem,
    person: Person,
    refresh_search_index: bool = False,
) -> tuple[ArchiveItemPerson, bool]:
    """Ensure an item-level person link exists (add-only).

    Existing ``(archive_item, person)`` is a NOOP and preserves the unique
    constraint. Does not create PhotoPerson rows, Tags, or aliases. Does not
    delete anything. Callers that already refresh this item in the same
    transaction should pass ``refresh_search_index=False``.
    """
    existing = ArchiveItemPerson.objects.filter(
        archive_item_id=archive_item.pk,
        person_id=person.pk,
    ).first()
    if existing is not None:
        return existing, False
    try:
        # Nested savepoint: IntegrityError on the unique constraint must not
        # mark the caller's outer transaction atomic block rollback-only.
        with transaction.atomic():
            link = ArchiveItemPerson.objects.create(
                archive_item=archive_item,
                person=person,
            )
    except IntegrityError:
        link = ArchiveItemPerson.objects.get(
            archive_item_id=archive_item.pk,
            person_id=person.pk,
        )
        return link, False
    if refresh_search_index:
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(archive_item.pk)
    return link, True


def ensure_archive_item_people_for_photo_content(
    photo_content,
    *,
    persons: list[Person],
    refresh_search_index: bool = False,
) -> int:
    """Ensure ArchiveItemPerson for each Person appearing on this photo.

    PhotoPerson is sufficient evidence that the Person is related to the
    containing ArchiveItem. Returns the number of AIP rows created.
    """
    archive_item = photo_content.archive_item
    if archive_item is None:
        raise ArchiveItemPersonError("photo content is missing its archive item")
    created_count = 0
    for person in persons:
        _link, created = ensure_archive_item_person(
            archive_item=archive_item,
            person=person,
            refresh_search_index=False,
        )
        if created:
            created_count += 1
    if refresh_search_index and created_count:
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(archive_item.pk)
    return created_count


@transaction.atomic
def create_archive_item_person(
    *,
    archive_item: ArchiveItem,
    person: Person,
) -> ArchiveItemPerson:
    """Create an item-level person link and refresh that item's search index.

    Does not create PhotoPerson rows, Tags, or aliases. Duplicate
    ``(archive_item, person)`` is a uniqueness error.
    """
    try:
        link = ArchiveItemPerson.objects.create(
            archive_item=archive_item,
            person=person,
        )
    except IntegrityError as exc:
        raise ArchiveItemPersonError(ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR) from exc

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return link


@transaction.atomic
def delete_archive_item_person(link: ArchiveItemPerson) -> bool:
    """Delete an item-level person link unless PhotoPerson still requires it.

    Returns True when the AIP row was deleted. If a PhotoPerson on the same
    ArchiveItem still names this Person, this is a NOOP (AIP kept, no index
    refresh, no PhotoPerson change).
    """
    archive_item_id = link.archive_item_id
    person_id = link.person_id
    if photo_person_requires_archive_item_person(
        archive_item_id=archive_item_id,
        person_id=person_id,
    ):
        return False
    link.delete()

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item_id)
    return True
