"""Explicit staff Author -> Person link. Never inferred from names."""

from __future__ import annotations

from django.db import transaction

from documents.models import Author, Person
from documents.services.archive_item_authors import (
    AUTHOR_NOT_FOUND_ERROR,
    ArchiveItemAuthorError,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR

AUTHOR_PERSON_ID_INVALID_ERROR = "מזהה האדם חייב להיות מספר שלם חיובי."
AUTHOR_PERSON_LINK_UPDATED_MSG = "קישור המחבר/ת לאדם עודכן."


def parse_author_person_id(raw: object) -> int | None:
    """Parse an optional Person primary key. Empty unlinks. Names are not accepted."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None
    try:
        person_id = int(text)
    except (TypeError, ValueError) as exc:
        raise ArchiveItemAuthorError(AUTHOR_PERSON_ID_INVALID_ERROR) from exc
    if person_id < 1:
        raise ArchiveItemAuthorError(AUTHOR_PERSON_ID_INVALID_ERROR)
    return person_id


@transaction.atomic
def set_author_person(*, author: Author, person_id: int | None) -> Author:
    """Link Author to an existing Person, or unlink. Does not create Person rows.

    Identity is ``Person.id`` only. Exact-name equality is not used.
    When linking, Person is locked before Author so the order matches Person merge.
    """
    target_person: Person | None = None
    if person_id is not None:
        target_person = (
            Person.objects.select_for_update().filter(pk=person_id).first()
        )
        if target_person is None:
            raise ArchiveItemAuthorError(PERSON_NOT_FOUND_ERROR)

    locked = Author.objects.select_for_update().filter(pk=author.pk).first()
    if locked is None:
        raise ArchiveItemAuthorError(AUTHOR_NOT_FOUND_ERROR)

    new_person_id = target_person.pk if target_person is not None else None
    if locked.person_id == new_person_id:
        return locked
    locked.person_id = new_person_id
    locked.save(update_fields=["person_id", "updated_at"])
    return locked
