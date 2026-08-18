"""Explicit ArchiveItemPerson create/delete with same-transaction search refresh.

Item-level person links are not photo appearances. Callers that write
``ArchiveItemPerson`` must use these services; raw model writes are not hooked.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction

from documents.models import ArchiveItem, ArchiveItemPerson, Person

ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR = (
    "this person is already linked to the archive item"
)


class ArchiveItemPersonError(Exception):
    """Staff/service-facing ArchiveItemPerson write error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


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
