"""Reviewed PhotoPerson import: parse/validate/plan/apply with binding idempotency.

ADD-only. Does not write ArchiveItemPerson, Author, or people_present.
create_person identity on re-apply is ReviewedPersonImportBinding.operation_id
only — never Person.name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db import IntegrityError, transaction

from documents.models import (
    ArchiveItem,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    ReviewedPersonImportBinding,
)
from documents.services.archive_search_index import (
    archive_item_ids_for_person_search_refresh,
    sync_archive_item_search_indexes,
)
from documents.services.person_duplicate_check import find_existing_person_candidates
from documents.services.photo_content_management import (
    PERSON_ALIAS_MATCHES_CANONICAL_ERROR,
    PERSON_ALIAS_REQUIRED_ERROR,
    PERSON_ALIAS_TOO_LONG_ERROR,
    PERSON_NAME_MAX_LENGTH,
    PERSON_NAME_REQUIRED_ERROR,
    PERSON_NAME_TOO_LONG_ERROR,
    PERSON_NOT_FOUND_ERROR,
    create_identified_person,
)
from documents.services.photo_presentation import photo_is_archive_renderable

SCHEMA = "photo-person-reviewed-import-v1"
OP_CREATE_PERSON = "create_person"
OP_ADD_ALIAS = "add_alias"
OP_ADD_PHOTO_PERSON = "add_photo_person"
SUPPORTED_OPS = frozenset({OP_CREATE_PERSON, OP_ADD_ALIAS, OP_ADD_PHOTO_PERSON})

STATUS_CREATE = "CREATE"
STATUS_ADD = "ADD"
STATUS_NOOP = "NOOP"
STATUS_ERROR = "ERROR"

SCHEMA_ERROR = "artifact schema must be photo-person-reviewed-import-v1"
OPERATIONS_REQUIRED_ERROR = "artifact operations must be a non-empty list"
OPERATION_ID_REQUIRED_ERROR = "operation id is required"
OPERATION_ID_DUPLICATE_ERROR = "duplicate operation id"
UNKNOWN_OPERATION_ERROR = "unsupported operation type"
UNKNOWN_FIELD_ERROR = "operation contains an unsupported field"
DOCUMENT_ID_FORBIDDEN_ERROR = "Document ids are not valid PHOTO identifiers"
LOCAL_REF_REQUIRED_ERROR = "create_person requires local_person_ref"
LOCAL_REF_DUPLICATE_ERROR = "duplicate local_person_ref"
LOCAL_REF_UNKNOWN_ERROR = "unknown local_person_ref"
PERSON_TARGET_ERROR = "operation requires exactly one of person_id or local_person_ref"
CANONICAL_NAME_STALE_ERROR = "expected canonical name does not match the bound Person"
BINDING_BROKEN_ERROR = "reviewed import binding is missing its Person"
BINDING_REINTERPRET_ERROR = "operation_id is already bound and cannot be reinterpreted"
CREATE_PERSON_CANDIDATES_ERROR = (
    "create_person is blocked because an existing Person canonical name or "
    "alias matches; v1 does not force-create or reuse by name"
)
PHOTO_BINDING_ERROR = (
    "photo must belong to the given PHOTO ArchiveItem with a matching "
    "original_file_key"
)
PHOTO_NOT_RENDERABLE_ERROR = "photo is not public-renderable"
NON_PHOTO_PARENT_ERROR = "archive item is not PHOTO"
FILE_KEY_REQUIRED_ERROR = "expected_original_file_key is required"
PHOTO_IDS_REQUIRED_ERROR = "archive_item_id and photo_content_id are required"


class ReviewedPhotoPersonImportError(Exception):
    """Fail-closed import validation or apply error. No partial apply."""

    def __init__(
        self,
        message: str,
        *,
        operation_id: str | None = None,
        status: str = STATUS_ERROR,
    ):
        super().__init__(message)
        self.message = message
        self.operation_id = operation_id
        self.status = status


@dataclass(frozen=True)
class PlannedOperation:
    operation_id: str
    op: str
    status: str
    reason: str
    person_id: int | None = None
    local_person_ref: str | None = None
    archive_item_id: int | None = None
    photo_content_id: int | None = None
    alias_name: str | None = None


@dataclass
class ImportPlan:
    operations: list[PlannedOperation] = field(default_factory=list)
    search_index_item_ids: tuple[int, ...] = ()
    applied: bool = False

    @property
    def create_count(self) -> int:
        return sum(1 for row in self.operations if row.status == STATUS_CREATE)

    @property
    def add_count(self) -> int:
        return sum(1 for row in self.operations if row.status == STATUS_ADD)

    @property
    def noop_count(self) -> int:
        return sum(1 for row in self.operations if row.status == STATUS_NOOP)

    @property
    def error_count(self) -> int:
        return sum(1 for row in self.operations if row.status == STATUS_ERROR)


def _strip(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require_operation_id(raw: object) -> str:
    operation_id = _strip(raw)
    if not operation_id:
        raise ReviewedPhotoPersonImportError(OPERATION_ID_REQUIRED_ERROR)
    if len(operation_id) > 255:
        raise ReviewedPhotoPersonImportError(OPERATION_ID_REQUIRED_ERROR)
    return operation_id


def _require_positive_int(raw: object, *, field_name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ReviewedPhotoPersonImportError(f"{field_name} must be a positive integer")
    if raw < 1:
        raise ReviewedPhotoPersonImportError(f"{field_name} must be a positive integer")
    return raw


def _canonical_name(raw: object) -> str:
    name = _strip(raw)
    if not name:
        raise ReviewedPhotoPersonImportError(PERSON_NAME_REQUIRED_ERROR)
    if len(name) > PERSON_NAME_MAX_LENGTH:
        raise ReviewedPhotoPersonImportError(PERSON_NAME_TOO_LONG_ERROR)
    return name


def _alias_name_for_canonical(raw: object, *, canonical_name: str) -> str:
    name = _strip(raw)
    if not name:
        raise ReviewedPhotoPersonImportError(PERSON_ALIAS_REQUIRED_ERROR)
    if len(name) > 255:
        raise ReviewedPhotoPersonImportError(PERSON_ALIAS_TOO_LONG_ERROR)
    if name == canonical_name:
        raise ReviewedPhotoPersonImportError(PERSON_ALIAS_MATCHES_CANONICAL_ERROR)
    return name


def _reject_unknown_fields(op: dict[str, Any], allowed: set[str], *, operation_id: str) -> None:
    extra = sorted(key for key in op if key not in allowed)
    if "document_id" in extra:
        raise ReviewedPhotoPersonImportError(
            DOCUMENT_ID_FORBIDDEN_ERROR, operation_id=operation_id
        )
    if extra:
        raise ReviewedPhotoPersonImportError(
            f"{UNKNOWN_FIELD_ERROR}: {', '.join(extra)}",
            operation_id=operation_id,
        )


def _person_target(op: dict[str, Any], *, operation_id: str) -> tuple[int | None, str | None]:
    has_id = "person_id" in op and op.get("person_id") is not None
    has_ref = bool(_strip(op.get("local_person_ref")))
    if has_id == has_ref:
        raise ReviewedPhotoPersonImportError(
            PERSON_TARGET_ERROR, operation_id=operation_id
        )
    if has_ref:
        return None, _strip(op.get("local_person_ref"))
    return _require_positive_int(op.get("person_id"), field_name="person_id"), None


def _load_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ReviewedPhotoPersonImportError(SCHEMA_ERROR)
    if _strip(payload.get("schema")) != SCHEMA:
        raise ReviewedPhotoPersonImportError(SCHEMA_ERROR)
    operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ReviewedPhotoPersonImportError(OPERATIONS_REQUIRED_ERROR)
    return operations


def _parse_operations(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_ops = _load_payload(payload)
    parsed: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_refs: set[str] = set()
    create_refs: set[str] = set()

    for index, raw in enumerate(raw_ops):
        if not isinstance(raw, dict):
            raise ReviewedPhotoPersonImportError("each operation must be an object")
        operation_id = _require_operation_id(raw.get("id"))
        if operation_id in seen_ids:
            raise ReviewedPhotoPersonImportError(
                OPERATION_ID_DUPLICATE_ERROR, operation_id=operation_id
            )
        seen_ids.add(operation_id)
        op_type = _strip(raw.get("op"))
        if op_type not in SUPPORTED_OPS:
            raise ReviewedPhotoPersonImportError(
                UNKNOWN_OPERATION_ERROR, operation_id=operation_id
            )
        if "document_id" in raw:
            raise ReviewedPhotoPersonImportError(
                DOCUMENT_ID_FORBIDDEN_ERROR, operation_id=operation_id
            )
        row = dict(raw)
        row["_index"] = index
        row["id"] = operation_id
        row["op"] = op_type
        if op_type == OP_CREATE_PERSON:
            _reject_unknown_fields(
                raw,
                {"id", "op", "local_person_ref", "canonical_name"},
                operation_id=operation_id,
            )
            local_ref = _strip(raw.get("local_person_ref"))
            if not local_ref:
                raise ReviewedPhotoPersonImportError(
                    LOCAL_REF_REQUIRED_ERROR, operation_id=operation_id
                )
            if local_ref in seen_refs:
                raise ReviewedPhotoPersonImportError(
                    LOCAL_REF_DUPLICATE_ERROR, operation_id=operation_id
                )
            seen_refs.add(local_ref)
            create_refs.add(local_ref)
            row["local_person_ref"] = local_ref
            row["canonical_name"] = _canonical_name(raw.get("canonical_name"))
        elif op_type == OP_ADD_ALIAS:
            _reject_unknown_fields(
                raw,
                {
                    "id",
                    "op",
                    "person_id",
                    "expected_canonical_name",
                    "local_person_ref",
                    "alias_name",
                },
                operation_id=operation_id,
            )
            person_id, local_ref = _person_target(raw, operation_id=operation_id)
            row["person_id"] = person_id
            row["local_person_ref"] = local_ref
            if person_id is not None:
                row["expected_canonical_name"] = _canonical_name(
                    raw.get("expected_canonical_name")
                )
            row["alias_name"] = _strip(raw.get("alias_name"))
        else:
            _reject_unknown_fields(
                raw,
                {
                    "id",
                    "op",
                    "archive_item_id",
                    "photo_content_id",
                    "expected_original_file_key",
                    "person_id",
                    "expected_canonical_name",
                    "local_person_ref",
                    "note",
                    "observed_people_present",
                },
                operation_id=operation_id,
            )
            person_id, local_ref = _person_target(raw, operation_id=operation_id)
            row["person_id"] = person_id
            row["local_person_ref"] = local_ref
            if person_id is not None:
                row["expected_canonical_name"] = _canonical_name(
                    raw.get("expected_canonical_name")
                )
            try:
                row["archive_item_id"] = _require_positive_int(
                    raw.get("archive_item_id"), field_name="archive_item_id"
                )
                row["photo_content_id"] = _require_positive_int(
                    raw.get("photo_content_id"), field_name="photo_content_id"
                )
            except ReviewedPhotoPersonImportError as exc:
                raise ReviewedPhotoPersonImportError(
                    PHOTO_IDS_REQUIRED_ERROR, operation_id=operation_id
                ) from exc
            key = _strip(raw.get("expected_original_file_key"))
            if not key:
                raise ReviewedPhotoPersonImportError(
                    FILE_KEY_REQUIRED_ERROR, operation_id=operation_id
                )
            row["expected_original_file_key"] = key
        parsed.append(row)

    for row in parsed:
        local_ref = row.get("local_person_ref")
        if row["op"] != OP_CREATE_PERSON and local_ref and local_ref not in create_refs:
            raise ReviewedPhotoPersonImportError(
                LOCAL_REF_UNKNOWN_ERROR, operation_id=row["id"]
            )
    return parsed


def _require_person(*, person_id: int, expected_canonical_name: str, operation_id: str) -> Person:
    person = Person.objects.filter(pk=person_id).first()
    if person is None:
        raise ReviewedPhotoPersonImportError(
            PERSON_NOT_FOUND_ERROR, operation_id=operation_id
        )
    if (person.name or "").strip() != expected_canonical_name:
        raise ReviewedPhotoPersonImportError(
            CANONICAL_NAME_STALE_ERROR, operation_id=operation_id
        )
    return person


def _resolve_create_person(row: dict[str, Any], *, for_write: bool) -> tuple[Person | None, str, str]:
    operation_id = row["id"]
    canonical_name = row["canonical_name"]
    queryset = ReviewedPersonImportBinding.objects.select_related("person")
    if for_write:
        queryset = queryset.select_for_update()
    binding = queryset.filter(operation_id=operation_id).first()
    if binding is None:
        if find_existing_person_candidates(canonical_name):
            raise ReviewedPhotoPersonImportError(
                CREATE_PERSON_CANDIDATES_ERROR, operation_id=operation_id
            )
        return None, STATUS_CREATE, "create Person and import binding"
    person = binding.person
    if person is None:
        raise ReviewedPhotoPersonImportError(
            BINDING_BROKEN_ERROR, operation_id=operation_id
        )
    if (person.name or "").strip() != canonical_name:
        raise ReviewedPhotoPersonImportError(
            CANONICAL_NAME_STALE_ERROR, operation_id=operation_id
        )
    return person, STATUS_NOOP, "binding already exists"


def _load_renderable_photo(row: dict[str, Any]) -> PhotoContent:
    operation_id = row["id"]
    archive_item_id = row["archive_item_id"]
    photo_content_id = row["photo_content_id"]
    expected_key = row["expected_original_file_key"]
    item = ArchiveItem.objects.filter(pk=archive_item_id).first()
    photo = PhotoContent.objects.filter(pk=photo_content_id).first()
    if item is None or photo is None:
        raise ReviewedPhotoPersonImportError(
            PHOTO_BINDING_ERROR, operation_id=operation_id
        )
    if item.item_type != ArchiveItem.ItemType.PHOTO:
        raise ReviewedPhotoPersonImportError(
            NON_PHOTO_PARENT_ERROR, operation_id=operation_id
        )
    if photo.archive_item_id != archive_item_id:
        raise ReviewedPhotoPersonImportError(
            PHOTO_BINDING_ERROR, operation_id=operation_id
        )
    stored_key = (photo.original_file_key or "").strip()
    if stored_key != expected_key:
        raise ReviewedPhotoPersonImportError(
            PHOTO_BINDING_ERROR, operation_id=operation_id
        )
    if not photo_is_archive_renderable(photo):
        raise ReviewedPhotoPersonImportError(
            PHOTO_NOT_RENDERABLE_ERROR, operation_id=operation_id
        )
    return photo


def _alias_exists(person_id: int, alias_name: str) -> bool:
    return PersonAlias.objects.filter(person_id=person_id, name=alias_name).exists()


def _photo_person_exists(photo_id: int, person_id: int) -> bool:
    return PhotoPerson.objects.filter(
        photo_content_id=photo_id, person_id=person_id
    ).exists()


def _plan_later_operation(
    row: dict[str, Any],
    *,
    person: Person | None,
    canonical_name: str,
) -> PlannedOperation:
    if row["op"] == OP_ADD_ALIAS:
        alias_name = _alias_name_for_canonical(
            row.get("alias_name"), canonical_name=canonical_name
        )
        if person is not None and _alias_exists(person.pk, alias_name):
            return PlannedOperation(
                operation_id=row["id"],
                op=row["op"],
                status=STATUS_NOOP,
                reason="alias already exists",
                person_id=person.pk,
                local_person_ref=row.get("local_person_ref"),
                alias_name=alias_name,
            )
        return PlannedOperation(
            operation_id=row["id"],
            op=row["op"],
            status=STATUS_ADD,
            reason="add PersonAlias",
            person_id=None if person is None else person.pk,
            local_person_ref=row.get("local_person_ref"),
            alias_name=alias_name,
        )

    photo = _load_renderable_photo(row)
    if person is not None and _photo_person_exists(photo.pk, person.pk):
        return PlannedOperation(
            operation_id=row["id"],
            op=row["op"],
            status=STATUS_NOOP,
            reason="PhotoPerson already exists",
            person_id=person.pk,
            local_person_ref=row.get("local_person_ref"),
            archive_item_id=photo.archive_item_id,
            photo_content_id=photo.pk,
        )
    return PlannedOperation(
        operation_id=row["id"],
        op=row["op"],
        status=STATUS_ADD,
        reason="add PhotoPerson",
        person_id=None if person is None else person.pk,
        local_person_ref=row.get("local_person_ref"),
        archive_item_id=photo.archive_item_id,
        photo_content_id=photo.pk,
    )


def _person_and_canonical_for_later(
    row: dict[str, Any],
    *,
    resolved_refs: dict[str, Person],
    create_by_ref: dict[str, dict[str, Any]],
) -> tuple[Person | None, str]:
    local_ref = row.get("local_person_ref")
    if local_ref:
        person = resolved_refs.get(local_ref)
        if person is not None:
            return person, (person.name or "").strip()
        create_row = create_by_ref[local_ref]
        return None, create_row["canonical_name"]
    person = _require_person(
        person_id=row["person_id"],
        expected_canonical_name=row["expected_canonical_name"],
        operation_id=row["id"],
    )
    return person, (person.name or "").strip()


def plan_reviewed_photo_person_import(payload: dict[str, Any]) -> ImportPlan:
    """Read-only validation and plan. Performs no writes."""
    rows = _parse_operations(payload)
    create_rows = [row for row in rows if row["op"] == OP_CREATE_PERSON]
    later_rows = [row for row in rows if row["op"] != OP_CREATE_PERSON]
    create_by_ref = {row["local_person_ref"]: row for row in create_rows}
    resolved_refs: dict[str, Person] = {}
    planned: list[PlannedOperation] = []

    for row in create_rows:
        person, status, reason = _resolve_create_person(row, for_write=False)
        if person is not None:
            resolved_refs[row["local_person_ref"]] = person
        planned.append(
            PlannedOperation(
                operation_id=row["id"],
                op=row["op"],
                status=status,
                reason=reason,
                person_id=None if person is None else person.pk,
                local_person_ref=row["local_person_ref"],
            )
        )

    for row in later_rows:
        person, canonical_name = _person_and_canonical_for_later(
            row, resolved_refs=resolved_refs, create_by_ref=create_by_ref
        )
        planned.append(
            _plan_later_operation(row, person=person, canonical_name=canonical_name)
        )
    return ImportPlan(operations=planned)


def apply_reviewed_photo_person_import(payload: dict[str, Any]) -> ImportPlan:
    """Validate the full artifact, then apply ADD-only writes in one transaction."""
    rows = _parse_operations(payload)
    create_rows = [row for row in rows if row["op"] == OP_CREATE_PERSON]
    later_rows = [row for row in rows if row["op"] != OP_CREATE_PERSON]
    create_by_ref = {row["local_person_ref"]: row for row in create_rows}

    with transaction.atomic():
        plan_reviewed_photo_person_import(payload)

        resolved_refs: dict[str, Person] = {}
        applied: list[PlannedOperation] = []
        refresh_ids: set[int] = set()

        for row in create_rows:
            person, status, reason = _resolve_create_person(row, for_write=True)
            if person is None:
                person = create_identified_person(name=row["canonical_name"])
                try:
                    ReviewedPersonImportBinding.objects.create(
                        operation_id=row["id"],
                        person=person,
                    )
                except IntegrityError as exc:
                    raise ReviewedPhotoPersonImportError(
                        BINDING_REINTERPRET_ERROR, operation_id=row["id"]
                    ) from exc
                status = STATUS_CREATE
                reason = "create Person and import binding"
            resolved_refs[row["local_person_ref"]] = person
            applied.append(
                PlannedOperation(
                    operation_id=row["id"],
                    op=row["op"],
                    status=status,
                    reason=reason,
                    person_id=person.pk,
                    local_person_ref=row["local_person_ref"],
                )
            )

        for row in later_rows:
            person, canonical_name = _person_and_canonical_for_later(
                row, resolved_refs=resolved_refs, create_by_ref=create_by_ref
            )
            if person is None:
                raise ReviewedPhotoPersonImportError(
                    LOCAL_REF_UNKNOWN_ERROR, operation_id=row["id"]
                )
            planned_op = _plan_later_operation(
                row, person=person, canonical_name=canonical_name
            )
            if planned_op.status == STATUS_NOOP:
                applied.append(planned_op)
                continue
            if planned_op.op == OP_ADD_ALIAS:
                PersonAlias.objects.create(
                    person=person, name=planned_op.alias_name or ""
                )
                refresh_ids.update(
                    archive_item_ids_for_person_search_refresh(person.pk)
                )
                applied.append(
                    PlannedOperation(
                        operation_id=planned_op.operation_id,
                        op=planned_op.op,
                        status=STATUS_ADD,
                        reason=planned_op.reason,
                        person_id=person.pk,
                        local_person_ref=planned_op.local_person_ref,
                        alias_name=planned_op.alias_name,
                    )
                )
                continue
            photo = _load_renderable_photo(row)
            PhotoPerson.objects.create(photo_content=photo, person=person)
            refresh_ids.add(photo.archive_item_id)
            applied.append(
                PlannedOperation(
                    operation_id=planned_op.operation_id,
                    op=planned_op.op,
                    status=STATUS_ADD,
                    reason=planned_op.reason,
                    person_id=person.pk,
                    local_person_ref=planned_op.local_person_ref,
                    archive_item_id=photo.archive_item_id,
                    photo_content_id=photo.pk,
                )
            )

        if refresh_ids:
            sync_archive_item_search_indexes(sorted(refresh_ids))
        return ImportPlan(
            operations=applied,
            search_index_item_ids=tuple(sorted(refresh_ids)),
            applied=True,
        )
