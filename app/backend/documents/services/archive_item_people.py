"""Explicit ArchiveItemPerson create/delete with same-transaction search refresh.

Item-level person links are not photo appearances. Callers that write
``ArchiveItemPerson`` must use these services; raw model writes are not hooked.
"""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from documents.models import ArchiveItem, ArchiveItemPerson, Person
from documents.services.photo_content_management import (
    PERSON_NOT_FOUND_ERROR,
    PhotoContentManagementError,
    create_identified_people_from_new_names,
    parse_new_person_names_input,
)

ARCHIVE_ITEM_PERSON_IDS_FIELD = "archive_item_person_ids"
NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD = "new_archive_item_person_name"

ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR = (
    "this person is already linked to the archive item"
)


class ArchiveItemPersonError(Exception):
    """Staff/service-facing ArchiveItemPerson write error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def empty_archive_item_people_form_fields() -> dict[str, Any]:
    """Empty staff form values for item-level people on create."""
    return {
        ARCHIVE_ITEM_PERSON_IDS_FIELD: [],
        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: "",
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
    new_person_name, name_errors = parse_new_archive_item_person_name(post_data)
    errors = id_errors + name_errors
    if not errors and person_ids:
        found = set(
            Person.objects.filter(pk__in=person_ids).values_list("pk", flat=True)
        )
        if found != set(person_ids):
            errors.append(PERSON_NOT_FOUND_ERROR)
    return {
        ARCHIVE_ITEM_PERSON_IDS_FIELD: person_ids,
        NEW_ARCHIVE_ITEM_PERSON_NAME_FIELD: new_person_name,
    }, errors


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
        link = existing_by_person_id.get(person.pk)
        if link is None:
            link = ArchiveItemPerson.objects.create(
                archive_item=archive_item,
                person=person,
            )
            changed = True
        kept_or_created.append(link)
    return kept_or_created, changed


@transaction.atomic
def set_archive_item_people(
    *,
    archive_item: ArchiveItem,
    person_ids: list[int],
    new_person_name: str = "",
    refresh_search_index: bool = True,
) -> list[ArchiveItemPerson]:
    """Replace ArchiveItemPerson links to match ``person_ids`` in one transaction.

    Optional ``new_person_name`` may be comma-separated. Each token always
    creates a new canonical Person and appends it. Does not create aliases,
    PhotoPerson rows, or Tags. Does not look up or merge by name. Unknown
    Person ids are rejected. One search-index refresh when links change and
    ``refresh_search_index`` is true.

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
        created_people = create_identified_people_from_new_names(new_person_name)
    except PhotoContentManagementError as exc:
        raise ArchiveItemPersonError(exc.message) from exc
    for created in created_people:
        resolved_ids.append(created.pk)
        created_person = True

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
def delete_archive_item_person(link: ArchiveItemPerson) -> None:
    """Delete an item-level person link and refresh that item's search index."""
    archive_item_id = link.archive_item_id
    link.delete()

    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item_id)
