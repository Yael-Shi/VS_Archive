"""Controlled staff Person identity merge: duplicate INTO keeper.

Keeper id and duplicate id are explicit. This is not name matching.
Person and Author remain separate identities. ArchiveItemPerson and
PhotoPerson are moved independently with no cross-inference.
ReviewedPersonImportBinding rows on the duplicate are repointed to the
keeper. Authors explicitly linked to the duplicate Person are repointed
to the keeper; this is relation repointing, not name inference.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import IntegrityError, transaction

from documents.historical_person_tag_map import HISTORICAL_PERSON_NAME_TAG_RECORDS
from documents.models import (
    ArchiveItemPerson,
    ArchiveItemPersonSuggestion,
    Author,
    Person,
    PersonAlias,
    PhotoPerson,
    ReviewedPersonImportBinding,
)
from documents.services.archive_search_index import (
    archive_item_ids_for_person_search_refresh,
    sync_archive_item_search_indexes,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR

PERSON_MERGE_ID_REQUIRED_ERROR = "יש להזין מזהה של רשומת האדם הכפולה."
PERSON_MERGE_ID_INVALID_ERROR = "מזהה האדם חייב להיות מספר שלם חיובי."
PERSON_MERGE_SAME_ID_ERROR = "לא ניתן למזג רשומת אדם עם עצמה."
PERSON_MERGE_FROZEN_DUPLICATE_ERROR = (
    "לא ניתן למחוק רשומת אדם היסטורית קפואה. בחרו רשומה אחרת ככפולה."
)
PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR = (
    "לשתי הרשומות יש תקצירים שונים. יש ליישב את התקציר ידנית לפני המיזוג."
)
PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR = (
    "לשתי הרשומות יש הצעת אדם ממתינה לאותו פריט ואותה פעולה. "
    "יש לטפל בהצעות לפני המיזוג."
)
PERSON_MERGE_CONCURRENCY_ERROR = "המיזוג נכשל בגלל שינוי מקביל. נסו שוב."

BIOGRAPHY_OUTCOME_KEEP_KEEPER = "keep_keeper"
BIOGRAPHY_OUTCOME_COPY_DUPLICATE = "copy_duplicate"
BIOGRAPHY_OUTCOME_UNCHANGED = "unchanged"
BIOGRAPHY_OUTCOME_CONFLICT = "conflict"

FROZEN_HISTORICAL_PERSON_IDS: frozenset[int] = frozenset(
    person_id for _tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS
)


class PersonMergeError(Exception):
    """Staff-facing Person merge error. No partial merge is applied."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PersonMergeAuthorIdentityPreview:
    author_id: int
    author_name: str


@dataclass(frozen=True)
class PersonMergePreview:
    keeper_id: int
    keeper_name: str
    duplicate_id: int
    duplicate_name: str
    keeper_aliases: tuple[str, ...]
    duplicate_aliases: tuple[str, ...]
    duplicate_canonical_becomes_alias: str | None
    archive_item_person_count_keeper: int
    archive_item_person_count_duplicate: int
    photo_person_count_keeper: int
    photo_person_count_duplicate: int
    author_identity_count_keeper: int
    author_identity_count_duplicate: int
    duplicate_author_identities: tuple[PersonMergeAuthorIdentityPreview, ...]
    suggestion_count: int
    pending_suggestion_count: int
    biography_outcome: str
    duplicate_is_frozen: bool
    pending_suggestion_conflict: bool
    affected_archive_item_count: int
    blockers: tuple[str, ...]

    @property
    def can_execute(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class PersonMergeResult:
    keeper_id: int
    deleted_duplicate_id: int
    aliases_moved: int
    aliases_deduped: int
    aliases_skipped_canonical: int
    canonical_name_alias_created: bool
    archive_item_person_moved: int
    archive_item_person_deduped: int
    photo_person_moved: int
    photo_person_deduped: int
    author_identities_repointed: int
    suggestions_repointed: int
    import_bindings_repointed: int
    biography_copied: bool
    search_indexes_refreshed: int


def parse_person_merge_id(raw: object) -> int:
    """Parse an explicit Person primary key. Names are not accepted."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        raise PersonMergeError(PERSON_MERGE_ID_REQUIRED_ERROR)
    try:
        person_id = int(text)
    except (TypeError, ValueError) as exc:
        raise PersonMergeError(PERSON_MERGE_ID_INVALID_ERROR) from exc
    if person_id < 1:
        raise PersonMergeError(PERSON_MERGE_ID_INVALID_ERROR)
    return person_id


def is_frozen_historical_person_id(person_id: int) -> bool:
    """True when Person.id is frozen historical identity. ID membership only."""
    return person_id in FROZEN_HISTORICAL_PERSON_IDS


def _normalize_biography(value: str | None) -> str:
    return "" if value is None else str(value).strip()


def _biography_outcome(keeper: Person, duplicate: Person) -> str:
    keeper_bio = _normalize_biography(keeper.biography)
    duplicate_bio = _normalize_biography(duplicate.biography)
    if keeper_bio and duplicate_bio and keeper_bio != duplicate_bio:
        return BIOGRAPHY_OUTCOME_CONFLICT
    if not keeper_bio and duplicate_bio:
        return BIOGRAPHY_OUTCOME_COPY_DUPLICATE
    if keeper_bio and not duplicate_bio:
        return BIOGRAPHY_OUTCOME_KEEP_KEEPER
    return BIOGRAPHY_OUTCOME_UNCHANGED


def _alias_names(person: Person) -> tuple[str, ...]:
    return tuple(
        person.aliases.order_by("name", "id").values_list("name", flat=True)
    )


def _duplicate_canonical_becomes_alias(keeper: Person, duplicate: Person) -> str | None:
    duplicate_name = (duplicate.name or "").strip()
    keeper_name = (keeper.name or "").strip()
    if not duplicate_name or duplicate_name == keeper_name:
        return None
    if duplicate_name in set(_alias_names(keeper)):
        return None
    return duplicate_name


def _pending_suggestion_conflict_keys(
    *,
    keeper_id: int,
    duplicate_id: int,
) -> list[tuple[int, str]]:
    keeper_pending = set(
        ArchiveItemPersonSuggestion.objects.filter(
            person_id=keeper_id,
            status=ArchiveItemPersonSuggestion.Status.PENDING,
        ).values_list("archive_item_id", "action")
    )
    duplicate_pending = list(
        ArchiveItemPersonSuggestion.objects.filter(
            person_id=duplicate_id,
            status=ArchiveItemPersonSuggestion.Status.PENDING,
        ).values_list("archive_item_id", "action")
    )
    return [
        key for key in duplicate_pending if key in keeper_pending
    ]


def _author_identities(person_id: int) -> tuple[PersonMergeAuthorIdentityPreview, ...]:
    return tuple(
        PersonMergeAuthorIdentityPreview(author_id=author.pk, author_name=author.name)
        for author in Author.objects.filter(person_id=person_id).order_by("name", "id")
    )


def _load_merge_pair(keeper_id: int, duplicate_id: int) -> tuple[Person, Person]:
    if keeper_id == duplicate_id:
        raise PersonMergeError(PERSON_MERGE_SAME_ID_ERROR)
    found = {
        person.pk: person
        for person in Person.objects.filter(pk__in=[keeper_id, duplicate_id])
    }
    if keeper_id not in found or duplicate_id not in found:
        raise PersonMergeError(PERSON_NOT_FOUND_ERROR)
    return found[keeper_id], found[duplicate_id]


def _affected_archive_item_ids(keeper_id: int, duplicate_id: int) -> list[int]:
    return sorted(
        set(archive_item_ids_for_person_search_refresh(keeper_id))
        | set(archive_item_ids_for_person_search_refresh(duplicate_id))
    )


def _preview_blockers(
    *,
    duplicate_is_frozen: bool,
    biography_outcome: str,
    pending_suggestion_conflict: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if duplicate_is_frozen:
        blockers.append(PERSON_MERGE_FROZEN_DUPLICATE_ERROR)
    if biography_outcome == BIOGRAPHY_OUTCOME_CONFLICT:
        blockers.append(PERSON_MERGE_BIOGRAPHY_CONFLICT_ERROR)
    if pending_suggestion_conflict:
        blockers.append(PERSON_MERGE_PENDING_SUGGESTION_CONFLICT_ERROR)
    return tuple(blockers)


def preview_person_merge(*, keeper_id: int, duplicate_id: int) -> PersonMergePreview:
    """Read-only merge preview. Does not lock or mutate rows."""
    keeper, duplicate = _load_merge_pair(keeper_id, duplicate_id)
    duplicate_is_frozen = is_frozen_historical_person_id(duplicate.pk)
    biography_outcome = _biography_outcome(keeper, duplicate)
    pending_conflicts = _pending_suggestion_conflict_keys(
        keeper_id=keeper.pk,
        duplicate_id=duplicate.pk,
    )
    suggestions = ArchiveItemPersonSuggestion.objects.filter(person_id=duplicate.pk)
    suggestion_count = suggestions.count()
    pending_suggestion_count = suggestions.filter(
        status=ArchiveItemPersonSuggestion.Status.PENDING
    ).count()
    return PersonMergePreview(
        keeper_id=keeper.pk,
        keeper_name=keeper.name,
        duplicate_id=duplicate.pk,
        duplicate_name=duplicate.name,
        keeper_aliases=_alias_names(keeper),
        duplicate_aliases=_alias_names(duplicate),
        duplicate_canonical_becomes_alias=_duplicate_canonical_becomes_alias(
            keeper, duplicate
        ),
        archive_item_person_count_keeper=ArchiveItemPerson.objects.filter(
            person_id=keeper.pk
        ).count(),
        archive_item_person_count_duplicate=ArchiveItemPerson.objects.filter(
            person_id=duplicate.pk
        ).count(),
        photo_person_count_keeper=PhotoPerson.objects.filter(
            person_id=keeper.pk
        ).count(),
        photo_person_count_duplicate=PhotoPerson.objects.filter(
            person_id=duplicate.pk
        ).count(),
        author_identity_count_keeper=Author.objects.filter(
            person_id=keeper.pk
        ).count(),
        author_identity_count_duplicate=Author.objects.filter(
            person_id=duplicate.pk
        ).count(),
        duplicate_author_identities=_author_identities(duplicate.pk),
        suggestion_count=suggestion_count,
        pending_suggestion_count=pending_suggestion_count,
        biography_outcome=biography_outcome,
        duplicate_is_frozen=duplicate_is_frozen,
        pending_suggestion_conflict=bool(pending_conflicts),
        affected_archive_item_count=len(
            _affected_archive_item_ids(keeper.pk, duplicate.pk)
        ),
        blockers=_preview_blockers(
            duplicate_is_frozen=duplicate_is_frozen,
            biography_outcome=biography_outcome,
            pending_suggestion_conflict=bool(pending_conflicts),
        ),
    )


def _lock_merge_pair(keeper_id: int, duplicate_id: int) -> tuple[Person, Person]:
    if keeper_id == duplicate_id:
        raise PersonMergeError(PERSON_MERGE_SAME_ID_ERROR)
    locked = list(
        Person.objects.select_for_update()
        .filter(pk__in=[keeper_id, duplicate_id])
        .order_by("pk")
    )
    found = {person.pk: person for person in locked}
    if keeper_id not in found or duplicate_id not in found:
        raise PersonMergeError(PERSON_NOT_FOUND_ERROR)
    return found[keeper_id], found[duplicate_id]


def _lock_person_dependents(keeper_id: int, duplicate_id: int) -> None:
    person_ids = [keeper_id, duplicate_id]
    list(
        PersonAlias.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )
    list(
        ArchiveItemPerson.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )
    list(
        PhotoPerson.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )
    list(
        ArchiveItemPersonSuggestion.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )
    list(
        ReviewedPersonImportBinding.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )
    list(
        Author.objects.select_for_update()
        .filter(person_id__in=person_ids)
        .order_by("id")
    )


def _raise_if_not_executable(preview_like: PersonMergePreview) -> None:
    if preview_like.blockers:
        raise PersonMergeError(preview_like.blockers[0])


def _merge_aliases(keeper: Person, duplicate: Person) -> tuple[int, int, int, bool]:
    keeper_name = (keeper.name or "").strip()
    keeper_alias_names = set(
        PersonAlias.objects.filter(person_id=keeper.pk).values_list("name", flat=True)
    )
    moved = 0
    deduped = 0
    skipped_canonical = 0
    for alias in PersonAlias.objects.filter(person_id=duplicate.pk).order_by("id"):
        if alias.name == keeper_name:
            alias.delete()
            skipped_canonical += 1
            continue
        if alias.name in keeper_alias_names:
            alias.delete()
            deduped += 1
            continue
        PersonAlias.objects.filter(pk=alias.pk).update(person_id=keeper.pk)
        keeper_alias_names.add(alias.name)
        moved += 1

    canonical_created = False
    duplicate_name = (duplicate.name or "").strip()
    if (
        duplicate_name
        and duplicate_name != keeper_name
        and duplicate_name not in keeper_alias_names
    ):
        PersonAlias.objects.create(person=keeper, name=duplicate_name)
        canonical_created = True
    return moved, deduped, skipped_canonical, canonical_created


def _merge_archive_item_people(keeper: Person, duplicate: Person) -> tuple[int, int]:
    keeper_item_ids = set(
        ArchiveItemPerson.objects.filter(person_id=keeper.pk).values_list(
            "archive_item_id", flat=True
        )
    )
    moved = 0
    deduped = 0
    for link in ArchiveItemPerson.objects.filter(person_id=duplicate.pk).order_by("id"):
        if link.archive_item_id in keeper_item_ids:
            link.delete()
            deduped += 1
            continue
        ArchiveItemPerson.objects.filter(pk=link.pk).update(person_id=keeper.pk)
        keeper_item_ids.add(link.archive_item_id)
        moved += 1
    return moved, deduped


def _merge_photo_people(keeper: Person, duplicate: Person) -> tuple[int, int]:
    keeper_photo_ids = set(
        PhotoPerson.objects.filter(person_id=keeper.pk).values_list(
            "photo_content_id", flat=True
        )
    )
    moved = 0
    deduped = 0
    for link in PhotoPerson.objects.filter(person_id=duplicate.pk).order_by("id"):
        if link.photo_content_id in keeper_photo_ids:
            link.delete()
            deduped += 1
            continue
        PhotoPerson.objects.filter(pk=link.pk).update(person_id=keeper.pk)
        keeper_photo_ids.add(link.photo_content_id)
        moved += 1
    return moved, deduped


def _repoint_suggestions(keeper: Person, duplicate: Person) -> int:
    count = ArchiveItemPersonSuggestion.objects.filter(person_id=duplicate.pk).count()
    ArchiveItemPersonSuggestion.objects.filter(person_id=duplicate.pk).update(
        person_id=keeper.pk
    )
    return count


def _repoint_author_identities(keeper: Person, duplicate: Person) -> int:
    count = Author.objects.filter(person_id=duplicate.pk).count()
    Author.objects.filter(person_id=duplicate.pk).update(person_id=keeper.pk)
    return count


def _repoint_import_bindings(keeper: Person, duplicate: Person) -> int:
    count = ReviewedPersonImportBinding.objects.filter(person_id=duplicate.pk).count()
    ReviewedPersonImportBinding.objects.filter(person_id=duplicate.pk).update(
        person_id=keeper.pk
    )
    return count


def _apply_biography(keeper: Person, duplicate: Person, outcome: str) -> bool:
    if outcome != BIOGRAPHY_OUTCOME_COPY_DUPLICATE:
        return False
    keeper.biography = _normalize_biography(duplicate.biography)
    keeper.save(update_fields=["biography", "updated_at"])
    return True


@transaction.atomic
def merge_persons(*, keeper_id: int, duplicate_id: int) -> PersonMergeResult:
    """Merge duplicate INTO keeper. Fail closed. Duplicate delete is last."""
    keeper, duplicate = _lock_merge_pair(keeper_id, duplicate_id)
    _lock_person_dependents(keeper.pk, duplicate.pk)

    preview = preview_person_merge(keeper_id=keeper.pk, duplicate_id=duplicate.pk)
    _raise_if_not_executable(preview)

    affected_ids = _affected_archive_item_ids(keeper.pk, duplicate.pk)

    try:
        biography_copied = _apply_biography(
            keeper, duplicate, preview.biography_outcome
        )
        aliases_moved, aliases_deduped, aliases_skipped, canonical_created = (
            _merge_aliases(keeper, duplicate)
        )
        aip_moved, aip_deduped = _merge_archive_item_people(keeper, duplicate)
        photo_moved, photo_deduped = _merge_photo_people(keeper, duplicate)
        suggestions_repointed = _repoint_suggestions(keeper, duplicate)
        import_bindings_repointed = _repoint_import_bindings(keeper, duplicate)
        author_identities_repointed = _repoint_author_identities(keeper, duplicate)
        duplicate.delete()
    except IntegrityError as exc:
        raise PersonMergeError(PERSON_MERGE_CONCURRENCY_ERROR) from exc

    synced = sync_archive_item_search_indexes(affected_ids)
    return PersonMergeResult(
        keeper_id=keeper.pk,
        deleted_duplicate_id=duplicate_id,
        aliases_moved=aliases_moved,
        aliases_deduped=aliases_deduped,
        aliases_skipped_canonical=aliases_skipped,
        canonical_name_alias_created=canonical_created,
        archive_item_person_moved=aip_moved,
        archive_item_person_deduped=aip_deduped,
        photo_person_moved=photo_moved,
        photo_person_deduped=photo_deduped,
        author_identities_repointed=author_identities_repointed,
        suggestions_repointed=suggestions_repointed,
        import_bindings_repointed=import_bindings_repointed,
        biography_copied=biography_copied,
        search_indexes_refreshed=len(synced),
    )
