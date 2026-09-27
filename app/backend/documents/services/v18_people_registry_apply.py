"""V18 people-registry apply: guarded, idempotent, no name-based merges."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from django.db import IntegrityError, transaction

from documents.services.v18_people_registry_preflight import (
    BINDING_SOURCE,
    PreflightError,
    database_preflight,
    planned_aliases,
    planned_binding_keys,
    planned_family_names,
    sha256_path,
)

APPLY_CONFIRM_TOKEN = "APPLY_V18_PEOPLE_REGISTRY"

STATE_READY_TO_APPLY = "READY_TO_APPLY"
STATE_ALREADY_APPLIED = "ALREADY_APPLIED"
STATE_DRIFT = "STATE_DRIFT"


class ApplyError(PreflightError):
    """Fail-closed apply/guard error."""


class ApplySearchRefreshError(ApplyError):
    """Registry committed, but search-index refresh failed afterward."""

    def __init__(self, message: str, *, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


def load_approved_live_preflight(
    path: str | Path,
    *,
    expected_preflight_sha256: str,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    path = Path(path)
    expected_preflight_sha256 = expected_preflight_sha256.lower()
    expected_candidate_sha256 = expected_candidate_sha256.lower()
    actual = sha256_path(path)

    if actual != expected_preflight_sha256:
        raise ApplyError(
            "approved live-preflight SHA-256 mismatch: "
            f"expected={expected_preflight_sha256} actual={actual}"
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ApplyError("approved live-preflight is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise ApplyError("approved live-preflight root must be an object")

    candidate_sha = payload.get("candidate_sha256")
    if not isinstance(candidate_sha, str) or not candidate_sha:
        raise ApplyError("approved live-preflight missing candidate_sha256")

    if candidate_sha.lower() != expected_candidate_sha256:
        raise ApplyError(
            "approved live-preflight candidate_sha256 mismatch: "
            f"expected={expected_candidate_sha256} actual={candidate_sha}"
        )

    database = payload.get("database")
    if not isinstance(database, dict):
        raise ApplyError("approved live-preflight missing database object")

    if database.get("status") != "LIVE_PREFLIGHT_PASS":
        raise ApplyError(
            "approved live-preflight database.status must be LIVE_PREFLIGHT_PASS"
        )

    summary = database.get("summary")
    if not isinstance(summary, dict):
        raise ApplyError("approved live-preflight database.summary missing")

    blockers = summary.get("blockers")
    if blockers != 0:
        raise ApplyError(
            "approved live-preflight database.summary.blockers must be 0, "
            f"got {blockers!r}"
        )

    return payload


def is_fully_applied_state(
    database: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    if database.get("status") != "LIVE_PREFLIGHT_PASS":
        return False

    summary = database.get("summary")
    if not isinstance(summary, dict):
        return False

    people = candidate["candidate_people"]
    planned_alias_total = sum(
        len(planned_aliases(person, candidate)) for person in people
    )
    planned_family_total = sum(
        len(planned_family_names(person, candidate)) for person in people
    )
    planned_binding_total = len(planned_binding_keys(candidate))

    return (
        summary.get("blockers") == 0
        and summary.get("ignored_live_drift") == 0
        and summary.get("person_creates") == 0
        and summary.get("person_updates") == 0
        and summary.get("alias_creates") == 0
        and summary.get("alias_updates") == 0
        and summary.get("family_name_creates") == 0
        and summary.get("binding_creates") == 0
        and summary.get("person_noops") == len(people)
        and summary.get("alias_noops") == planned_alias_total
        and summary.get("family_name_noops") == planned_family_total
        and summary.get("binding_noops") == planned_binding_total
    )


def classify_apply_state(
    database: dict[str, Any],
    *,
    candidate: dict[str, Any],
    approved_database: dict[str, Any],
) -> str:
    if is_fully_applied_state(database, candidate):
        return STATE_ALREADY_APPLIED

    if database == approved_database:
        return STATE_READY_TO_APPLY

    return STATE_DRIFT


def _relevant_person_ids(candidate: dict[str, Any]) -> list[int]:
    from documents.models import PersonRegistryImportBinding

    people = candidate["candidate_people"]
    ids: set[int] = set()

    for person in people:
        existing = person.get("existing_person_id")
        if existing is not None:
            ids.add(int(existing))

    for binding in PersonRegistryImportBinding.objects.filter(
        source=BINDING_SOURCE
    ).order_by("stable_key"):
        ids.add(binding.person_id)

    return sorted(ids)


def _lock_registry_rows(candidate: dict[str, Any]) -> None:
    from documents.models import (
        Person,
        PersonAlias,
        PersonFamilyName,
        PersonRegistryImportBinding,
    )

    person_ids = _relevant_person_ids(candidate)

    list(Person.objects.filter(pk__in=person_ids).order_by("pk").select_for_update())
    list(
        PersonAlias.objects.filter(person_id__in=person_ids)
        .order_by("pk")
        .select_for_update()
    )
    list(
        PersonFamilyName.objects.filter(person_id__in=person_ids)
        .order_by("pk")
        .select_for_update()
    )
    list(
        PersonRegistryImportBinding.objects.filter(source=BINDING_SOURCE)
        .order_by("stable_key", "pk")
        .select_for_update()
    )


def _resolve_person(
    *,
    item: dict[str, Any],
    created_by_key: dict[str, Any],
):
    person_id = item.get("person_id")
    if person_id is not None:
        from documents.models import Person

        return Person.objects.get(pk=int(person_id))

    ref = item.get("person_ref")
    if isinstance(ref, str) and ref.startswith("CREATE:"):
        key = ref[len("CREATE:") :]
        person = created_by_key.get(key)
        if person is None:
            raise ApplyError(f"missing created person for {ref}")
        return person

    raise ApplyError(f"cannot resolve person for planned row: {item!r}")


def _apply_mutations(database: dict[str, Any]) -> dict[str, int]:
    from documents.models import (
        Person,
        PersonAlias,
        PersonFamilyName,
        PersonRegistryImportBinding,
    )

    counts = {
        "people_created": 0,
        "people_updated": 0,
        "aliases_created": 0,
        "aliases_updated": 0,
        "family_names_created": 0,
        "bindings_created": 0,
    }
    created_by_key: dict[str, Person] = {}

    for item in database["people"]["creates"]:
        person = Person.objects.create(
            name=item["name"],
            honorific=item["honorific"],
        )
        created_by_key[item["stable_key"]] = person
        counts["people_created"] += 1

    for item in database["people"]["updates"]:
        person = Person.objects.get(pk=int(item["person_id"]))
        changes = item["changes"]
        update_fields: list[str] = []

        if "name" in changes:
            person.name = changes["name"]["planned"]
            update_fields.append("name")

        if "honorific" in changes:
            person.honorific = changes["honorific"]["planned"]
            update_fields.append("honorific")

        if update_fields:
            update_fields.append("updated_at")
            person.save(update_fields=update_fields)
            counts["people_updated"] += 1

    for item in database["aliases"]["creates"]:
        person = _resolve_person(item=item, created_by_key=created_by_key)
        try:
            PersonAlias.objects.create(
                person=person,
                name=item["name"],
                kind=item["kind"],
                display_publicly=bool(item["display_publicly"]),
            )
        except IntegrityError as exc:
            raise ApplyError(
                f"alias create integrity conflict for person_id={person.pk} "
                f"name={item['name']!r}"
            ) from exc
        counts["aliases_created"] += 1

    for item in database["aliases"]["updates"]:
        alias = PersonAlias.objects.get(pk=int(item["alias_id"]))
        changes = item["changes"]
        update_fields: list[str] = []

        if "kind" in changes:
            alias.kind = changes["kind"]["planned"]
            update_fields.append("kind")

        if "display_publicly" in changes:
            alias.display_publicly = bool(changes["display_publicly"]["planned"])
            update_fields.append("display_publicly")

        if update_fields:
            update_fields.append("updated_at")
            alias.save(update_fields=update_fields)
            counts["aliases_updated"] += 1

    for item in database["family_names"]["creates"]:
        person = _resolve_person(item=item, created_by_key=created_by_key)
        try:
            PersonFamilyName.objects.create(
                person=person,
                name=item["name"],
                role=item["role"],
            )
        except IntegrityError as exc:
            raise ApplyError(
                f"family name create integrity conflict for person_id={person.pk} "
                f"name={item['name']!r} role={item['role']!r}"
            ) from exc
        counts["family_names_created"] += 1

    for item in database["bindings"]["creates"]:
        person = _resolve_person(item=item, created_by_key=created_by_key)
        try:
            PersonRegistryImportBinding.objects.create(
                source=BINDING_SOURCE,
                stable_key=item["stable_key"],
                person=person,
            )
        except IntegrityError as exc:
            raise ApplyError(
                f"binding create integrity conflict for stable_key="
                f"{item['stable_key']!r}"
            ) from exc
        counts["bindings_created"] += 1

    return counts


def _search_item_ids_for_people(person_ids: list[int]) -> list[int]:
    from documents.services.archive_search_index import (
        archive_item_ids_for_person_search_refresh,
    )

    item_ids: set[int] = set()
    for person_id in person_ids:
        item_ids.update(archive_item_ids_for_person_search_refresh(person_id))
    return sorted(item_ids)


def _resolved_existing_person_ids(candidate: dict[str, Any]) -> list[int]:
    from documents.models import PersonRegistryImportBinding

    people = candidate["candidate_people"]
    ids: set[int] = set()

    for person in people:
        existing = person.get("existing_person_id")
        if existing is not None:
            ids.add(int(existing))

    keys = {person["person_key"] for person in people}
    for binding in PersonRegistryImportBinding.objects.filter(
        source=BINDING_SOURCE,
        stable_key__in=keys,
    ):
        ids.add(binding.person_id)

    return sorted(ids)


def _empty_write_counts() -> dict[str, int]:
    return {
        "people_created": 0,
        "people_updated": 0,
        "aliases_created": 0,
        "aliases_updated": 0,
        "family_names_created": 0,
        "bindings_created": 0,
    }


def _result_payload(
    *,
    state_before: str,
    candidate_sha256: str,
    approved_preflight_sha256: str,
    write_counts: dict[str, int],
    postflight_database: dict[str, Any] | None,
    search_index_item_ids: list[int],
    search_indexes_refreshed: bool,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "state_before": state_before,
        "candidate_sha256": candidate_sha256,
        "approved_preflight_sha256": approved_preflight_sha256,
        "dry_run": dry_run,
        "people_created": write_counts["people_created"],
        "people_updated": write_counts["people_updated"],
        "aliases_created": write_counts["aliases_created"],
        "aliases_updated": write_counts["aliases_updated"],
        "family_names_created": write_counts["family_names_created"],
        "bindings_created": write_counts["bindings_created"],
        "postflight": {
            "database": postflight_database,
        },
        "search_index_item_ids": list(search_index_item_ids),
        "search_indexes_refreshed": search_indexes_refreshed,
    }


def run_v18_people_registry_apply(
    *,
    candidate: dict[str, Any],
    approved_database: dict[str, Any],
    candidate_sha256: str,
    approved_preflight_sha256: str,
    mode: Literal["dry-run", "apply"],
) -> dict[str, Any]:
    """Apply or dry-run the V18 people registry against an approved live preflight.

    Never merges Persons by name. Never deletes Persons, aliases, family names,
    or bindings. Existing people are resolved only by explicit existing_person_id
    or PersonRegistryImportBinding rows.
    """
    if mode == "dry-run":
        current = database_preflight(candidate)
        state = classify_apply_state(
            current,
            candidate=candidate,
            approved_database=approved_database,
        )
        if state == STATE_DRIFT:
            raise ApplyError(
                "STATE_DRIFT: current database_preflight does not match the "
                "approved live-preflight database section and is not a fully "
                "applied idempotent state"
            )

        return _result_payload(
            state_before=state,
            candidate_sha256=candidate_sha256,
            approved_preflight_sha256=approved_preflight_sha256,
            write_counts=_empty_write_counts(),
            postflight_database=deepcopy(current),
            search_index_item_ids=[],
            search_indexes_refreshed=False,
            dry_run=True,
        )

    if mode != "apply":
        raise ApplyError(f"unsupported mode: {mode!r}")

    write_counts = _empty_write_counts()
    state_before = STATE_DRIFT
    search_index_item_ids: list[int] = []
    postflight_database: dict[str, Any] | None = None

    with transaction.atomic():
        _lock_registry_rows(candidate)
        locked = database_preflight(candidate)
        state_before = classify_apply_state(
            locked,
            candidate=candidate,
            approved_database=approved_database,
        )

        if state_before == STATE_DRIFT:
            raise ApplyError(
                "STATE_DRIFT: locked database_preflight does not match the "
                "approved live-preflight database section and is not a fully "
                "applied idempotent state"
            )

        if state_before == STATE_ALREADY_APPLIED:
            write_counts = _empty_write_counts()
            postflight_database = deepcopy(locked)
            search_index_item_ids = _search_item_ids_for_people(
                _resolved_existing_person_ids(candidate)
            )
        else:
            # READY_TO_APPLY: mutate from the locked plan, then require
            # the fully-applied postflight before commit.
            try:
                write_counts = _apply_mutations(locked)
            except IntegrityError as exc:
                raise ApplyError(
                    "registry mutation aborted due to integrity conflict"
                ) from exc

            postflight = database_preflight(candidate)
            if not is_fully_applied_state(postflight, candidate):
                raise ApplyError(
                    "post-mutation database_preflight is not the fully-applied "
                    "idempotent state; rolling back"
                )

            postflight_database = deepcopy(postflight)
            search_index_item_ids = _search_item_ids_for_people(
                list(locked.get("affected_existing_person_ids") or [])
            )

    refreshed = False
    result = _result_payload(
        state_before=state_before,
        candidate_sha256=candidate_sha256,
        approved_preflight_sha256=approved_preflight_sha256,
        write_counts=write_counts,
        postflight_database=postflight_database,
        search_index_item_ids=search_index_item_ids,
        search_indexes_refreshed=False,
        dry_run=False,
    )

    try:
        if search_index_item_ids:
            from documents.services.archive_search_index import (
                sync_archive_item_search_indexes,
            )

            sync_archive_item_search_indexes(search_index_item_ids)
        refreshed = True
    except Exception as exc:
        raise ApplySearchRefreshError(
            "registry commit succeeded but search-index refresh failed; "
            "rerun apply to retry refresh via ALREADY_APPLIED",
            result=result,
        ) from exc

    result["search_indexes_refreshed"] = refreshed
    return result
