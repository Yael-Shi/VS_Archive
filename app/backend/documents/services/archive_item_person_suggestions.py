"""Create/validate ArchiveItemPerson relationship-delta suggestions.

C2a service boundary: callers supply the authorized Person universe. This
module does not query public request visibility and does not write
ArchiveItemPerson, PhotoPerson, Person, PersonAlias, Tag, Document.tags_m2m,
or the search index.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db import IntegrityError, transaction

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    Person,
)
from documents.services.archive_metadata_suggestions import NAME_REQUIRED_ERROR
from documents.services.photo_content_management import (
    PERSON_NOT_FOUND_ERROR,
    staff_person_aliases_prefetch,
)


PERSON_ALREADY_LINKED_ERROR = "אדם זה כבר מקושר לפריט הארכיון."
PERSON_NOT_LINKED_ERROR = "אדם זה אינו מקושר לפריט הארכיון."
DUPLICATE_PENDING_SUGGESTION_ERROR = "הצעה זהה כבר ממתינה לבדיקה."
INVALID_SUGGESTION_ACTION_ERROR = "פעולת ההצעה אינה תקינה."
CONTRADICTORY_PERSON_ACTIONS_ERROR = (
    "לא ניתן להציע הוספה והסרה של אותו אדם באותה שליחה."
)
ADD_PERSON_IDS_FIELD = "add_person_ids"
REMOVE_PERSON_IDS_FIELD = "remove_person_ids"


class ArchiveItemPersonSuggestionError(Exception):
    """Validation failure for ArchiveItemPerson suggestion submission."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def existing_person_universe_ids() -> frozenset[int]:
    """All existing Person primary keys.

    C2b should pass a request-authorized subset of this universe into
    ``submit_archive_item_person_suggestion``. This helper does not apply
    ArchiveItem visibility.
    """
    return frozenset(Person.objects.values_list("pk", flat=True))


def linked_person_ids_for_archive_item(archive_item: ArchiveItem) -> frozenset[int]:
    """Person ids currently linked via ArchiveItemPerson (item-level only)."""
    return frozenset(
        ArchiveItemPerson.objects.filter(archive_item=archive_item).values_list(
            "person_id", flat=True
        )
    )


def _normalize_submitter_name(submitter_name: str) -> str:
    return (submitter_name or "").strip()


def _normalize_action(action: str) -> str:
    normalized = (action or "").strip()
    valid = {choice for choice, _label in ArchiveItemPersonSuggestion.Action.choices}
    if normalized not in valid:
        raise ArchiveItemPersonSuggestionError(INVALID_SUGGESTION_ACTION_ERROR)
    return normalized


def _require_authorized_person(
    *,
    person_id: int,
    authorized_person_ids: Iterable[int],
) -> Person:
    authorized = {int(pk) for pk in authorized_person_ids}
    if person_id not in authorized:
        raise ArchiveItemPersonSuggestionError(PERSON_NOT_FOUND_ERROR)
    person = Person.objects.filter(pk=person_id).first()
    if person is None:
        raise ArchiveItemPersonSuggestionError(PERSON_NOT_FOUND_ERROR)
    return person


def _person_is_linked(*, archive_item: ArchiveItem, person: Person) -> bool:
    return ArchiveItemPerson.objects.filter(
        archive_item=archive_item,
        person=person,
    ).exists()


def _pending_duplicate_exists(
    *,
    archive_item: ArchiveItem,
    person: Person,
    action: str,
) -> bool:
    return ArchiveItemPersonSuggestion.objects.filter(
        archive_item=archive_item,
        person=person,
        action=action,
        status=ArchiveItemPersonSuggestion.Status.PENDING,
    ).exists()


@transaction.atomic
def submit_archive_item_person_suggestion(
    *,
    archive_item: ArchiveItem,
    person_id: int,
    action: str,
    submitter_name: str,
    authorized_person_ids: Iterable[int],
    submitter_email: str = "",
    submitter_note: str = "",
    submitter_user=None,
) -> ArchiveItemPersonSuggestion:
    """Create one PENDING ADD or REMOVE suggestion. Does not mutate relationships."""
    name = _normalize_submitter_name(submitter_name)
    if not name:
        raise ArchiveItemPersonSuggestionError(NAME_REQUIRED_ERROR)

    resolved_action = _normalize_action(action)
    person = _require_authorized_person(
        person_id=person_id,
        authorized_person_ids=authorized_person_ids,
    )
    linked = _person_is_linked(archive_item=archive_item, person=person)

    if resolved_action == ArchiveItemPersonSuggestion.Action.ADD and linked:
        raise ArchiveItemPersonSuggestionError(PERSON_ALREADY_LINKED_ERROR)
    if resolved_action == ArchiveItemPersonSuggestion.Action.REMOVE and not linked:
        raise ArchiveItemPersonSuggestionError(PERSON_NOT_LINKED_ERROR)
    if _pending_duplicate_exists(
        archive_item=archive_item,
        person=person,
        action=resolved_action,
    ):
        raise ArchiveItemPersonSuggestionError(DUPLICATE_PENDING_SUGGESTION_ERROR)

    try:
        with transaction.atomic():
            return ArchiveItemPersonSuggestion.objects.create(
                archive_item=archive_item,
                person=person,
                action=resolved_action,
                submitter_name=name,
                submitter_email=(submitter_email or "").strip(),
                submitter_note=(submitter_note or "").strip(),
                submitter_user=submitter_user,
            )
    except IntegrityError as exc:
        raise ArchiveItemPersonSuggestionError(
            DUPLICATE_PENDING_SUGGESTION_ERROR
        ) from exc


def authorized_person_universe_ids_for_user(user) -> frozenset[int]:
    """Person ids linked via ArchiveItemPerson to items the user may view.

    PhotoPerson-only identities are excluded. Private/restricted names are
    included only when the user may view the linked ArchiveItem.
    """
    from documents.services.archive_item_access import archive_item_queryset_for_user

    return frozenset(
        Person.objects.filter(
            archive_items__in=archive_item_queryset_for_user(user)
        ).values_list("pk", flat=True)
    )


def authorized_person_queryset_for_user(user):
    """Authorized Person rows for the public ADD picker, with aliases prefetched."""
    from documents.services.archive_item_access import archive_item_queryset_for_user

    return (
        Person.objects.filter(archive_items__in=archive_item_queryset_for_user(user))
        .distinct()
        .order_by("name", "id")
        .prefetch_related(staff_person_aliases_prefetch())
    )


def current_archive_item_people_queryset(archive_item: ArchiveItem):
    """Item-level ArchiveItemPerson people for REMOVE context (not PhotoPerson)."""
    return archive_item.people.order_by("name", "id").prefetch_related(
        staff_person_aliases_prefetch()
    )


def parse_posted_person_ids(post_data, field_name: str) -> tuple[list[int], list[str]]:
    """Parse unique positive Person ids from a POST list field.

    Invalid tokens yield the generic not-found error and do not reveal whether
    a Person row exists.
    """
    if hasattr(post_data, "getlist"):
        raw_values = post_data.getlist(field_name)
    else:
        raw = post_data.get(field_name) if post_data is not None else None
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
        token = str(raw).strip()
        if not token:
            continue
        try:
            person_id = int(token)
        except (TypeError, ValueError):
            if PERSON_NOT_FOUND_ERROR not in errors:
                errors.append(PERSON_NOT_FOUND_ERROR)
            continue
        if person_id <= 0:
            if PERSON_NOT_FOUND_ERROR not in errors:
                errors.append(PERSON_NOT_FOUND_ERROR)
            continue
        if person_id in seen:
            continue
        seen.add(person_id)
        person_ids.append(person_id)
    return person_ids, errors
