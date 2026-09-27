from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


BINDING_SOURCE = "vs_archive_people_v18"

UNCHANGED_EXISTING_KEYS = {
    "C:4",
    "C:19",
    "C:24",
    "C:32",
    "C:34",
    "C:36",
    "C:37",
    "C:39",
}


class PreflightError(ValueError):
    pass


def sha256_path(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_candidate(path: str | Path, expected_sha256: str) -> dict[str, Any]:
    path = Path(path)
    actual = sha256_path(path)

    if actual != expected_sha256:
        raise PreflightError(
            f"candidate SHA-256 mismatch: expected={expected_sha256} actual={actual}"
        )

    data = json.loads(path.read_text(encoding="utf-8"))

    if len(data.get("candidate_people", [])) != 942:
        raise PreflightError(
            f"expected 942 candidate_people, got "
            f"{len(data.get('candidate_people', []))}"
        )

    if data.get("site_or_active_registry_apply_performed") is not False:
        raise PreflightError("candidate apply flag is not false")

    return data


def overrides(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("final_apply_overrides")
    if not isinstance(value, dict):
        raise PreflightError("final_apply_overrides missing")
    return value


def effective_canonical(
    person: dict[str, Any],
    data: dict[str, Any],
) -> str:
    item = overrides(data).get("canonical_name_overrides", {}).get(person["person_key"])

    if item:
        return str(item["to"]).strip()

    return str(person["canonical_name"]).strip()


def effective_honorific(
    person: dict[str, Any],
    data: dict[str, Any],
) -> str:
    key = person["person_key"]
    explicit = overrides(data).get("honorific_overrides", {})

    if key in explicit:
        return str(explicit[key]).strip()

    terms = [
        str(value).strip()
        for value in person.get("honorifics_or_titles", [])
        if str(value).strip()
    ]

    if len(terms) > 1:
        raise PreflightError(
            f"{key} has multiple title terms but no final honorific override: {terms!r}"
        )

    return terms[0] if terms else ""


def planned_aliases(
    person: dict[str, Any],
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    final = overrides(data)
    mapping = final.get("alias_kind_mapping", {})
    canonical = effective_canonical(person, data)

    result: dict[str, dict[str, Any]] = {}

    for alias in person.get("approved_aliases", []):
        if alias.get("usage_scope") != "PERSON_SCOPED":
            continue

        if alias.get("registration_disposition") != "ELIGIBLE_IN_REVIEW_PROJECTION":
            continue

        name = str(alias.get("form", "")).strip()
        if not name:
            continue

        if name == canonical:
            continue

        reviewed_kind = alias.get("alias_type")
        model_kind = mapping.get(reviewed_kind)

        if not model_kind:
            raise PreflightError(
                f"{person['person_key']} alias {name!r} has unmapped kind "
                f"{reviewed_kind!r}"
            )

        desired = {
            "name": name,
            "kind": model_kind,
            "display_publicly": bool(alias.get("displayable_on_person_page", True)),
            "origin": "reviewed_alias",
        }

        previous = result.get(name)
        if previous and (
            previous["kind"] != desired["kind"]
            or previous["display_publicly"] != desired["display_publicly"]
        ):
            raise PreflightError(
                f"{person['person_key']} has conflicting planned alias "
                f"definitions for {name!r}"
            )

        result[name] = desired

    manual = final.get("manual_alias_additions", {}).get(person["person_key"], [])

    for alias in manual:
        name = str(alias["form"]).strip()

        desired = {
            "name": name,
            "kind": str(alias["kind"]),
            "display_publicly": bool(alias.get("display_as_other_name", True)),
            "origin": "manual_final_override",
        }

        previous = result.get(name)
        if previous and (
            previous["kind"] != desired["kind"]
            or previous["display_publicly"] != desired["display_publicly"]
        ):
            raise PreflightError(
                f"{person['person_key']} has conflicting manual alias {name!r}"
            )

        result[name] = desired

    return [result[name] for name in sorted(result)]


def planned_family_names(
    person: dict[str, Any],
    data: dict[str, Any],
) -> list[dict[str, str]]:
    key = person["person_key"]
    final = overrides(data)

    exclusions = {
        str(item["form"]).strip()
        for item in final.get("family_name_exclusions", {}).get(key, [])
        if item.get("action") == "DO_NOT_PERSIST_AS_PERSON_FAMILY_NAME"
    }

    additional = {
        str(value).strip()
        for value in person.get("additional_family_names", [])
        if str(value).strip()
    }

    unexplained = sorted(additional - exclusions)

    if unexplained:
        raise PreflightError(
            f"{key} has additional_family_names with no supported model role "
            f"and no explicit exclusion: {unexplained!r}"
        )

    rows = set()

    for name in person.get("previous_family_names", []):
        name = str(name).strip()
        if name:
            rows.add((name, "previous_family"))

    for name in person.get("married_acquired_family_names", []):
        name = str(name).strip()
        if name:
            rows.add((name, "acquired_family"))

    return [{"name": name, "role": role} for name, role in sorted(rows)]


def planned_binding_keys(data: dict[str, Any]) -> dict[str, str]:
    people = data["candidate_people"]
    survivor_keys = {person["person_key"] for person in people}
    mapping = data["v16_to_final_survivor_stable_key_mapping"]

    result = {key: key for key in survivor_keys}

    for old_key, survivor in mapping.items():
        if old_key != survivor and survivor in survivor_keys:
            result[old_key] = survivor

    return dict(sorted(result.items()))


def static_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    people = data["candidate_people"]

    existing = [
        person for person in people if person.get("existing_person_id") is not None
    ]

    new = [person for person in people if person.get("existing_person_id") is None]

    alias_count = 0
    family_count = 0
    honorific_count = 0

    for person in people:
        effective_canonical(person, data)

        if effective_honorific(person, data):
            honorific_count += 1

        alias_count += len(planned_aliases(person, data))
        family_count += len(planned_family_names(person, data))

    bindings = planned_binding_keys(data)

    if len(bindings) != 976:
        raise PreflightError(f"expected 976 planned bindings, got {len(bindings)}")

    return {
        "status": "STATIC_PREFLIGHT_PASS",
        "candidate_people": len(people),
        "existing_candidate_people": len(existing),
        "new_candidate_people": len(new),
        "planned_person_alias_rows": alias_count,
        "planned_person_family_name_rows": family_count,
        "people_with_nonempty_effective_honorific": honorific_count,
        "planned_binding_rows": len(bindings),
        "binding_source": BINDING_SOURCE,
        "unchanged_existing_keys": sorted(UNCHANGED_EXISTING_KEYS),
    }


def _binding_dict(binding: Any) -> dict[str, Any]:
    return {
        "stable_key": binding.stable_key,
        "person_id": binding.person_id,
    }


def database_preflight(data: dict[str, Any]) -> dict[str, Any]:
    from documents.models import (
        Person,
        PersonAlias,
        PersonFamilyName,
        PersonRegistryImportBinding,
    )

    blockers: list[dict[str, Any]] = []

    person_creates: list[dict[str, Any]] = []
    person_updates: list[dict[str, Any]] = []
    person_noops: list[dict[str, Any]] = []
    ignored_live_drift: list[dict[str, Any]] = []

    alias_creates: list[dict[str, Any]] = []
    alias_updates: list[dict[str, Any]] = []
    alias_noops: list[dict[str, Any]] = []

    family_creates: list[dict[str, Any]] = []
    family_noops: list[dict[str, Any]] = []

    binding_creates: list[dict[str, Any]] = []
    binding_noops: list[dict[str, Any]] = []

    people = sorted(
        data["candidate_people"],
        key=lambda person: person["person_key"],
    )

    expected_existing_ids = sorted(
        {
            int(person["existing_person_id"])
            for person in people
            if person.get("existing_person_id") is not None
        }
    )

    db_existing = Person.objects.in_bulk(expected_existing_ids)

    for person_id in expected_existing_ids:
        if person_id not in db_existing:
            blockers.append(
                {
                    "type": "MISSING_EXISTING_PERSON",
                    "person_id": person_id,
                }
            )

    bindings = list(
        PersonRegistryImportBinding.objects.filter(source=BINDING_SOURCE).order_by(
            "stable_key"
        )
    )

    binding_by_key = {row.stable_key: row for row in bindings}

    planned_bindings = planned_binding_keys(data)

    unexpected_binding_keys = sorted(set(binding_by_key) - set(planned_bindings))

    for key in unexpected_binding_keys:
        blockers.append(
            {
                "type": "UNEXPECTED_EXISTING_BINDING_IN_SOURCE_NAMESPACE",
                **_binding_dict(binding_by_key[key]),
            }
        )

    resolved_person_id: dict[str, int | None] = {}

    for person in people:
        key = person["person_key"]
        expected_id = person.get("existing_person_id")
        existing_binding = binding_by_key.get(key)

        if expected_id is not None:
            expected_id = int(expected_id)
            resolved_person_id[key] = expected_id

            if existing_binding and existing_binding.person_id != expected_id:
                blockers.append(
                    {
                        "type": "SURVIVOR_BINDING_CONFLICT",
                        "stable_key": key,
                        "expected_person_id": expected_id,
                        "bound_person_id": existing_binding.person_id,
                    }
                )

        elif existing_binding:
            resolved_person_id[key] = existing_binding.person_id

        else:
            resolved_person_id[key] = None

    # Distinct final survivor keys must never resolve to the same Person.
    # Historical/remapped keys may intentionally converge later via
    # planned_bindings, but two members of candidate_people may not.
    survivor_keys_by_person_id: dict[int, list[str]] = {}

    for survivor_key, person_id in sorted(resolved_person_id.items()):
        if person_id is None:
            continue

        survivor_keys_by_person_id.setdefault(person_id, []).append(survivor_key)

    for person_id, survivor_keys in sorted(survivor_keys_by_person_id.items()):
        if len(survivor_keys) > 1:
            blockers.append(
                {
                    "type": "MULTIPLE_SURVIVORS_BOUND_TO_SAME_PERSON",
                    "person_id": person_id,
                    "survivor_keys": survivor_keys,
                }
            )

    for stable_key, survivor_key in planned_bindings.items():
        existing_binding = binding_by_key.get(stable_key)
        target_id = resolved_person_id[survivor_key]

        if existing_binding:
            if target_id is None:
                blockers.append(
                    {
                        "type": (
                            "HISTORICAL_BINDING_EXISTS_BEFORE_SURVIVOR_IS_RESOLVED"
                        ),
                        "stable_key": stable_key,
                        "survivor_key": survivor_key,
                        "bound_person_id": existing_binding.person_id,
                    }
                )

            elif existing_binding.person_id != target_id:
                blockers.append(
                    {
                        "type": "BINDING_TARGET_CONFLICT",
                        "stable_key": stable_key,
                        "survivor_key": survivor_key,
                        "expected_person_id": target_id,
                        "bound_person_id": existing_binding.person_id,
                    }
                )

            else:
                binding_noops.append(
                    {
                        "stable_key": stable_key,
                        "survivor_key": survivor_key,
                        "person_id": target_id,
                    }
                )

        else:
            binding_creates.append(
                {
                    "stable_key": stable_key,
                    "survivor_key": survivor_key,
                    "person_id": target_id,
                    "person_ref": (
                        f"CREATE:{survivor_key}" if target_id is None else None
                    ),
                }
            )

    resolved_ids = sorted(
        {
            person_id
            for person_id in resolved_person_id.values()
            if person_id is not None
        }
    )

    current_people = Person.objects.in_bulk(resolved_ids)

    for key, person_id in sorted(resolved_person_id.items()):
        if person_id is not None and person_id not in current_people:
            blockers.append(
                {
                    "type": "BOUND_PERSON_MISSING",
                    "stable_key": key,
                    "person_id": person_id,
                }
            )

    aliases_by_person: dict[int, dict[str, Any]] = {}

    for row in PersonAlias.objects.filter(person_id__in=resolved_ids).order_by(
        "person_id", "name", "pk"
    ):
        aliases_by_person.setdefault(row.person_id, {})

        if row.name in aliases_by_person[row.person_id]:
            blockers.append(
                {
                    "type": "DUPLICATE_EXISTING_ALIAS_NAME",
                    "person_id": row.person_id,
                    "name": row.name,
                }
            )

        aliases_by_person[row.person_id][row.name] = row

    family_by_person: dict[int, set[tuple[str, str]]] = {}

    for row in PersonFamilyName.objects.filter(person_id__in=resolved_ids).order_by(
        "person_id", "name", "role", "pk"
    ):
        family_by_person.setdefault(
            row.person_id,
            set(),
        ).add((row.name, row.role))

    for person in people:
        key = person["person_key"]
        target_name = effective_canonical(person, data)
        target_honorific = effective_honorific(person, data)
        person_id = resolved_person_id[key]

        if person_id is None:
            person_creates.append(
                {
                    "stable_key": key,
                    "name": target_name,
                    "honorific": target_honorific,
                }
            )

        else:
            current = current_people.get(person_id)

            if current is None:
                continue

            changed = {}

            if current.name != target_name:
                changed["name"] = {
                    "current": current.name,
                    "planned": target_name,
                }

            if current.honorific != target_honorific:
                changed["honorific"] = {
                    "current": current.honorific,
                    "planned": target_honorific,
                }

            if key in UNCHANGED_EXISTING_KEYS and changed:
                ignored_live_drift.append(
                    {
                        "stable_key": key,
                        "person_id": person_id,
                        "differences": changed,
                        "reason": "UNCHANGED_EXISTING_V16_NOT_APPLIED",
                    }
                )

            elif changed:
                person_updates.append(
                    {
                        "stable_key": key,
                        "person_id": person_id,
                        "changes": changed,
                    }
                )

            else:
                person_noops.append(
                    {
                        "stable_key": key,
                        "person_id": person_id,
                    }
                )

        planned_alias_rows = planned_aliases(person, data)

        if person_id is None:
            for desired in planned_alias_rows:
                alias_creates.append(
                    {
                        "stable_key": key,
                        "person_ref": f"CREATE:{key}",
                        **desired,
                    }
                )

        else:
            current_aliases = aliases_by_person.get(
                person_id,
                {},
            )

            for desired in planned_alias_rows:
                current = current_aliases.get(desired["name"])

                if current is None:
                    alias_creates.append(
                        {
                            "stable_key": key,
                            "person_id": person_id,
                            **desired,
                        }
                    )
                    continue

                changes = {}

                if current.kind != desired["kind"]:
                    changes["kind"] = {
                        "current": current.kind,
                        "planned": desired["kind"],
                    }

                if current.display_publicly != desired["display_publicly"]:
                    changes["display_publicly"] = {
                        "current": current.display_publicly,
                        "planned": desired["display_publicly"],
                    }

                if changes:
                    alias_updates.append(
                        {
                            "stable_key": key,
                            "person_id": person_id,
                            "alias_id": current.pk,
                            "name": desired["name"],
                            "changes": changes,
                        }
                    )

                else:
                    alias_noops.append(
                        {
                            "stable_key": key,
                            "person_id": person_id,
                            "alias_id": current.pk,
                            "name": desired["name"],
                        }
                    )

        planned_family_rows = planned_family_names(
            person,
            data,
        )

        if person_id is None:
            for desired in planned_family_rows:
                family_creates.append(
                    {
                        "stable_key": key,
                        "person_ref": f"CREATE:{key}",
                        **desired,
                    }
                )

        else:
            current_family = family_by_person.get(
                person_id,
                set(),
            )

            for desired in planned_family_rows:
                pair = (
                    desired["name"],
                    desired["role"],
                )

                if pair in current_family:
                    family_noops.append(
                        {
                            "stable_key": key,
                            "person_id": person_id,
                            **desired,
                        }
                    )

                else:
                    family_creates.append(
                        {
                            "stable_key": key,
                            "person_id": person_id,
                            **desired,
                        }
                    )

    affected_existing_ids = sorted(
        {
            item["person_id"]
            for group in (
                person_updates,
                alias_creates,
                alias_updates,
                family_creates,
            )
            for item in group
            if item.get("person_id") is not None
        }
    )

    result = {
        "status": ("BLOCKED" if blockers else "LIVE_PREFLIGHT_PASS"),
        "binding_source": BINDING_SOURCE,
        "summary": {
            "candidate_people": len(people),
            "person_creates": len(person_creates),
            "person_updates": len(person_updates),
            "person_noops": len(person_noops),
            "ignored_live_drift": len(ignored_live_drift),
            "alias_creates": len(alias_creates),
            "alias_updates": len(alias_updates),
            "alias_noops": len(alias_noops),
            "family_name_creates": len(family_creates),
            "family_name_noops": len(family_noops),
            "binding_creates": len(binding_creates),
            "binding_noops": len(binding_noops),
            "blockers": len(blockers),
            "affected_existing_person_ids": len(affected_existing_ids),
        },
        "blockers": sorted(
            blockers,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
        "people": {
            "creates": person_creates,
            "updates": person_updates,
            "noops": person_noops,
            "ignored_live_drift": ignored_live_drift,
        },
        "aliases": {
            "creates": alias_creates,
            "updates": alias_updates,
            "noops": alias_noops,
        },
        "family_names": {
            "creates": family_creates,
            "noops": family_noops,
        },
        "bindings": {
            "creates": binding_creates,
            "noops": binding_noops,
        },
        "affected_existing_person_ids": affected_existing_ids,
    }

    return result
