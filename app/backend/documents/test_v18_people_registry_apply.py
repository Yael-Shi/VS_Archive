"""V18 people-registry apply: guards, mutations, idempotency, search refresh."""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    PersonFamilyName,
    PersonRegistryImportBinding,
)
from documents.services.v18_people_registry_apply import (
    APPLY_CONFIRM_TOKEN,
    ApplyError,
    ApplySearchRefreshError,
    STATE_ALREADY_APPLIED,
    STATE_READY_TO_APPLY,
    is_fully_applied_state,
    load_approved_live_preflight,
    run_v18_people_registry_apply,
)
from documents.services.v18_people_registry_preflight import (
    BINDING_SOURCE,
    database_preflight,
    planned_binding_keys,
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def live_candidate(people, *, remaps=None):
    mapping = {person["person_key"]: person["person_key"] for person in people}
    if remaps:
        mapping.update(remaps)
    return {
        "candidate_people": people,
        "v16_to_final_survivor_stable_key_mapping": mapping,
        "site_or_active_registry_apply_performed": False,
        "final_apply_overrides": {
            "canonical_name_overrides": {},
            "manual_alias_additions": {},
            "family_name_exclusions": {},
            "honorific_overrides": {},
            "alias_kind_mapping": {
                "OCR_VARIANT": "ocr_variant",
                "NAME_VARIANT": "name_variant",
                "PARTIAL_NAME": "partial_name",
                "SPELLING_VARIANT": "spelling_variant",
                "CODE_NAME": "code_name",
                "NICKNAME": "nickname",
                "TRANSLITERATION": "transliteration",
                "HONORIFIC_OR_TITLE": "honorific_or_title",
                "BIRTH_NAME": "birth_name",
            },
        },
    }


def live_person(
    person_key,
    *,
    existing_person_id=None,
    name="שם",
    honorifics=None,
    aliases=None,
    previous_family_names=None,
    married_acquired_family_names=None,
    additional_family_names=None,
):
    return {
        "person_key": person_key,
        "existing_person_id": existing_person_id,
        "canonical_name": name,
        "honorifics_or_titles": list(honorifics or []),
        "approved_aliases": list(aliases or []),
        "previous_family_names": list(previous_family_names or []),
        "married_acquired_family_names": list(married_acquired_family_names or []),
        "additional_family_names": list(additional_family_names or []),
    }


def reviewed_alias(
    form,
    *,
    alias_type="NAME_VARIANT",
    usage_scope="PERSON_SCOPED",
    disposition="ELIGIBLE_IN_REVIEW_PROJECTION",
    displayable=True,
):
    return {
        "form": form,
        "alias_type": alias_type,
        "usage_scope": usage_scope,
        "registration_disposition": disposition,
        "displayable_on_person_page": displayable,
    }


def approved_preflight_payload(candidate, *, candidate_sha256="a" * 64):
    database = database_preflight(candidate)
    return {
        "candidate_path": "/tmp/candidate.json",
        "candidate_sha256": candidate_sha256,
        "static": {"status": "STATIC_PREFLIGHT_PASS"},
        "database": database,
    }


def counts():
    return {
        "people": Person.objects.count(),
        "aliases": PersonAlias.objects.count(),
        "family_names": PersonFamilyName.objects.count(),
        "bindings": PersonRegistryImportBinding.objects.count(),
    }


def transaction_atomic_tracker(call_order: list):
    real_atomic = transaction.atomic

    class _Atomic:
        def __init__(self, *args, **kwargs):
            self._cm = real_atomic(*args, **kwargs)

        def __enter__(self):
            call_order.append("atomic_enter")
            return self._cm.__enter__()

        def __exit__(self, exc_type, exc, tb):
            result = self._cm.__exit__(exc_type, exc, tb)
            call_order.append("atomic_exit")
            return result

    def factory(*args, **kwargs):
        return _Atomic(*args, **kwargs)

    return factory


class V18PeopleRegistryApplyTests(TestCase):
    def test_dry_run_is_read_only_and_reports_ready_to_apply(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                    honorifics=['ד"ר'],
                    aliases=[reviewed_alias("כינוי")],
                    previous_family_names=["לוי"],
                ),
                live_person("N:NEW", name="אדם חדש"),
            ]
        )
        approved = approved_preflight_payload(candidate)
        before = counts()

        result = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="dry-run",
        )

        self.assertEqual(result["state_before"], STATE_READY_TO_APPLY)
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["people_created"], 0)
        self.assertEqual(result["people_updated"], 0)
        self.assertEqual(result["aliases_created"], 0)
        self.assertFalse(result["search_indexes_refreshed"])
        self.assertEqual(result["search_index_item_ids"], [])
        self.assertEqual(counts(), before)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "שם ישן")
        self.assertEqual(existing.honorific, "")

    def test_apply_creates_and_updates_as_planned(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                    honorifics=['ד"ר'],
                    aliases=[
                        reviewed_alias(
                            "כינוי",
                            alias_type="NICKNAME",
                            displayable=True,
                        )
                    ],
                    previous_family_names=["לוי"],
                ),
                live_person(
                    "N:NEW",
                    name="אדם חדש",
                    aliases=[reviewed_alias("New Alias", alias_type="TRANSLITERATION")],
                    married_acquired_family_names=["גולדשטיין"],
                ),
            ]
        )
        approved = approved_preflight_payload(candidate)

        result = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )

        self.assertEqual(result["state_before"], STATE_READY_TO_APPLY)
        self.assertEqual(result["people_created"], 1)
        self.assertEqual(result["people_updated"], 1)
        self.assertEqual(result["aliases_created"], 2)
        self.assertEqual(result["family_names_created"], 2)
        self.assertEqual(result["bindings_created"], 2)
        self.assertTrue(
            is_fully_applied_state(result["postflight"]["database"], candidate)
        )

        existing.refresh_from_db()
        self.assertEqual(existing.name, "שם מתוכנן")
        self.assertEqual(existing.honorific, 'ד"ר')
        self.assertTrue(
            PersonAlias.objects.filter(
                person=existing,
                name="כינוי",
                kind=PersonAlias.Kind.NICKNAME,
                display_publicly=True,
            ).exists()
        )
        self.assertTrue(
            PersonFamilyName.objects.filter(
                person=existing,
                name="לוי",
                role=PersonFamilyName.Role.PREVIOUS_FAMILY,
            ).exists()
        )

        created = Person.objects.get(name="אדם חדש")
        self.assertNotEqual(created.pk, existing.pk)
        self.assertTrue(
            PersonAlias.objects.filter(
                person=created,
                name="New Alias",
                kind=PersonAlias.Kind.TRANSLITERATION,
            ).exists()
        )
        self.assertTrue(
            PersonFamilyName.objects.filter(
                person=created,
                name="גולדשטיין",
                role=PersonFamilyName.Role.ACQUIRED_FAMILY,
            ).exists()
        )
        self.assertEqual(
            PersonRegistryImportBinding.objects.get(
                source=BINDING_SOURCE,
                stable_key="C:1",
            ).person_id,
            existing.pk,
        )
        self.assertEqual(
            PersonRegistryImportBinding.objects.get(
                source=BINDING_SOURCE,
                stable_key="N:NEW",
            ).person_id,
            created.pk,
        )

    def test_historical_binding_points_at_survivor_from_same_apply(self):
        candidate = live_candidate(
            [live_person("N:SURV", name="שורד")],
            remaps={"N:OLD": "N:SURV"},
        )
        approved = approved_preflight_payload(candidate)

        result = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )

        self.assertEqual(result["bindings_created"], 2)
        survivor = Person.objects.get(name="שורד")
        self.assertEqual(
            PersonRegistryImportBinding.objects.get(
                source=BINDING_SOURCE,
                stable_key="N:SURV",
            ).person_id,
            survivor.pk,
        )
        self.assertEqual(
            PersonRegistryImportBinding.objects.get(
                source=BINDING_SOURCE,
                stable_key="N:OLD",
            ).person_id,
            survivor.pk,
        )
        self.assertEqual(
            planned_binding_keys(candidate),
            {"N:OLD": "N:SURV", "N:SURV": "N:SURV"},
        )

    def test_db_drift_before_apply_blocks_with_zero_mutations(self):
        existing = Person.objects.create(name="שם ראשון", honorific="")
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                )
            ]
        )
        approved = approved_preflight_payload(candidate)
        existing.name = "שם שנסחף"
        existing.save(update_fields=["name", "updated_at"])
        before = counts()

        with self.assertRaises(ApplyError) as ctx:
            run_v18_people_registry_apply(
                candidate=candidate,
                approved_database=approved["database"],
                candidate_sha256="c" * 64,
                approved_preflight_sha256="p" * 64,
                mode="apply",
            )

        self.assertIn("STATE_DRIFT", str(ctx.exception))
        self.assertEqual(counts(), before)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "שם שנסחף")
        self.assertFalse(
            PersonRegistryImportBinding.objects.filter(source=BINDING_SOURCE).exists()
        )

    def test_post_mutation_validation_failure_rolls_back_all_writes(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                ),
                live_person("N:NEW", name="אדם חדש"),
            ]
        )
        approved = approved_preflight_payload(candidate)
        before = counts()
        calls = {"n": 0}
        real_preflight = database_preflight

        def flaky_preflight(data):
            calls["n"] += 1
            result = real_preflight(data)
            # First call: locked pre-apply plan. Second call: post-mutation check.
            if calls["n"] >= 2:
                result = json.loads(json.dumps(result))
                result["summary"]["person_creates"] = 1
                result["status"] = "LIVE_PREFLIGHT_PASS"
            return result

        with patch(
            "documents.services.v18_people_registry_apply.database_preflight",
            side_effect=flaky_preflight,
        ):
            with self.assertRaises(ApplyError) as ctx:
                run_v18_people_registry_apply(
                    candidate=candidate,
                    approved_database=approved["database"],
                    candidate_sha256="c" * 64,
                    approved_preflight_sha256="p" * 64,
                    mode="apply",
                )

        self.assertIn("post-mutation", str(ctx.exception))
        self.assertEqual(counts(), before)
        existing.refresh_from_db()
        self.assertEqual(existing.name, "שם ישן")
        self.assertFalse(Person.objects.filter(name="אדם חדש").exists())

    def test_second_apply_is_idempotent_already_applied(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                    aliases=[reviewed_alias("כינוי")],
                    previous_family_names=["לוי"],
                ),
                live_person("N:NEW", name="אדם חדש"),
            ]
        )
        approved = approved_preflight_payload(candidate)

        first = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )
        after_first = counts()
        self.assertEqual(first["state_before"], STATE_READY_TO_APPLY)

        second = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )

        self.assertEqual(second["state_before"], STATE_ALREADY_APPLIED)
        self.assertEqual(second["people_created"], 0)
        self.assertEqual(second["people_updated"], 0)
        self.assertEqual(second["aliases_created"], 0)
        self.assertEqual(second["aliases_updated"], 0)
        self.assertEqual(second["family_names_created"], 0)
        self.assertEqual(second["bindings_created"], 0)
        self.assertEqual(counts(), after_first)
        self.assertEqual(Person.objects.filter(name="אדם חדש").count(), 1)
        self.assertEqual(PersonAlias.objects.filter(name="כינוי").count(), 1)
        self.assertEqual(PersonFamilyName.objects.filter(name="לוי").count(), 1)
        self.assertEqual(
            PersonRegistryImportBinding.objects.filter(source=BINDING_SOURCE).count(),
            2,
        )

    def test_equal_names_are_not_collapsed_across_survivors(self):
        candidate = live_candidate(
            [
                live_person("N:A", name="שם זהה"),
                live_person("N:B", name="שם זהה"),
            ]
        )
        approved = approved_preflight_payload(candidate)

        result = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )

        self.assertEqual(result["people_created"], 2)
        people = list(Person.objects.filter(name="שם זהה").order_by("pk"))
        self.assertEqual(len(people), 2)
        self.assertNotEqual(people[0].pk, people[1].pk)
        binding_a = PersonRegistryImportBinding.objects.get(
            source=BINDING_SOURCE,
            stable_key="N:A",
        )
        binding_b = PersonRegistryImportBinding.objects.get(
            source=BINDING_SOURCE,
            stable_key="N:B",
        )
        self.assertNotEqual(binding_a.person_id, binding_b.person_id)

    def test_search_index_refresh_runs_after_commit_for_linked_existing_people(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        item = ArchiveItem.objects.create(
            title="פריט",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=existing)
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                    aliases=[reviewed_alias("כינוי")],
                ),
                live_person("N:NEW", name="אדם חדש"),
            ]
        )
        approved = approved_preflight_payload(candidate)
        call_order: list = []

        def sync_side_effect(ids):
            call_order.append(("sync", list(ids)))
            return []

        with (
            patch(
                "documents.services.v18_people_registry_apply.transaction.atomic",
                side_effect=transaction_atomic_tracker(call_order),
            ),
            patch(
                "documents.services.archive_search_index.sync_archive_item_search_indexes",
                side_effect=sync_side_effect,
            ),
        ):
            result = run_v18_people_registry_apply(
                candidate=candidate,
                approved_database=approved["database"],
                candidate_sha256="c" * 64,
                approved_preflight_sha256="p" * 64,
                mode="apply",
            )

        self.assertIn(item.pk, result["search_index_item_ids"])
        self.assertTrue(result["search_indexes_refreshed"])
        self.assertEqual(result["people_created"], 1)
        created = Person.objects.get(name="אדם חדש")
        self.assertFalse(ArchiveItemPerson.objects.filter(person=created).exists())
        self.assertIn("atomic_enter", call_order)
        self.assertIn("atomic_exit", call_order)
        sync_events = [event for event in call_order if isinstance(event, tuple)]
        self.assertEqual(len(sync_events), 1)
        self.assertEqual(sync_events[0][1], [item.pk])
        self.assertLess(
            call_order.index("atomic_exit"),
            call_order.index(sync_events[0]),
        )

    def test_search_refresh_failure_after_commit_retries_via_already_applied(self):
        existing = Person.objects.create(name="שם ישן", honorific="")
        item = ArchiveItem.objects.create(
            title="פריט לריענון",
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=existing)
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם מתוכנן",
                    honorifics=['ד"ר'],
                    aliases=[
                        reviewed_alias(
                            "כינוי",
                            alias_type="NICKNAME",
                            displayable=True,
                        )
                    ],
                )
            ]
        )
        approved = approved_preflight_payload(candidate)
        sync_calls: list[list[int]] = []

        def failing_then_ok(ids):
            sync_calls.append(list(ids))
            if len(sync_calls) == 1:
                raise RuntimeError("search refresh boom")
            return []

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes",
            side_effect=failing_then_ok,
        ):
            with self.assertRaises(ApplySearchRefreshError) as ctx:
                run_v18_people_registry_apply(
                    candidate=candidate,
                    approved_database=approved["database"],
                    candidate_sha256="c" * 64,
                    approved_preflight_sha256="p" * 64,
                    mode="apply",
                )

        self.assertIn("registry commit succeeded", str(ctx.exception))
        self.assertFalse(ctx.exception.result["search_indexes_refreshed"])
        self.assertIn(item.pk, ctx.exception.result["search_index_item_ids"])

        existing.refresh_from_db()
        self.assertEqual(existing.name, "שם מתוכנן")
        self.assertEqual(existing.honorific, 'ד"ר')
        alias = PersonAlias.objects.get(person=existing, name="כינוי")
        self.assertEqual(alias.kind, PersonAlias.Kind.NICKNAME)
        binding = PersonRegistryImportBinding.objects.get(
            source=BINDING_SOURCE,
            stable_key="C:1",
        )
        self.assertEqual(binding.person_id, existing.pk)

        after_failed_refresh = counts()
        snapshot = {
            "person_pk": existing.pk,
            "alias_pk": alias.pk,
            "binding_pk": binding.pk,
            "counts": after_failed_refresh,
        }

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes",
            side_effect=failing_then_ok,
        ):
            retry = run_v18_people_registry_apply(
                candidate=candidate,
                approved_database=approved["database"],
                candidate_sha256="c" * 64,
                approved_preflight_sha256="p" * 64,
                mode="apply",
            )

        self.assertEqual(retry["state_before"], STATE_ALREADY_APPLIED)
        self.assertEqual(retry["people_created"], 0)
        self.assertEqual(retry["people_updated"], 0)
        self.assertEqual(retry["aliases_created"], 0)
        self.assertEqual(retry["aliases_updated"], 0)
        self.assertEqual(retry["family_names_created"], 0)
        self.assertEqual(retry["bindings_created"], 0)
        self.assertEqual(counts(), snapshot["counts"])
        self.assertEqual(Person.objects.get(pk=snapshot["person_pk"]).name, "שם מתוכנן")
        self.assertEqual(
            PersonAlias.objects.get(pk=snapshot["alias_pk"]).name,
            "כינוי",
        )
        self.assertEqual(
            PersonRegistryImportBinding.objects.get(
                pk=snapshot["binding_pk"]
            ).stable_key,
            "C:1",
        )
        self.assertEqual(Person.objects.filter(pk=existing.pk).count(), 1)
        self.assertEqual(PersonAlias.objects.filter(person=existing).count(), 1)
        self.assertEqual(
            PersonRegistryImportBinding.objects.filter(source=BINDING_SOURCE).count(),
            1,
        )
        self.assertEqual(len(sync_calls), 2)
        self.assertEqual(sync_calls[1], [item.pk])
        self.assertIn(item.pk, retry["search_index_item_ids"])
        self.assertTrue(retry["search_indexes_refreshed"])

    def test_existing_alias_metadata_update_does_not_recreate_row(self):
        existing = Person.objects.create(name="שם קנוני", honorific="")
        alias = PersonAlias.objects.create(
            person=existing,
            name="כינוי",
            kind=PersonAlias.Kind.UNSPECIFIED,
            display_publicly=False,
        )
        alias_id = alias.pk
        candidate = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=existing.pk,
                    name="שם קנוני",
                    aliases=[
                        reviewed_alias(
                            "כינוי",
                            alias_type="NICKNAME",
                            displayable=True,
                        )
                    ],
                )
            ]
        )
        approved = approved_preflight_payload(candidate)
        self.assertEqual(approved["database"]["summary"]["alias_updates"], 1)

        result = run_v18_people_registry_apply(
            candidate=candidate,
            approved_database=approved["database"],
            candidate_sha256="c" * 64,
            approved_preflight_sha256="p" * 64,
            mode="apply",
        )

        self.assertEqual(result["aliases_updated"], 1)
        self.assertEqual(result["aliases_created"], 0)
        self.assertEqual(PersonAlias.objects.filter(person=existing).count(), 1)
        alias.refresh_from_db()
        self.assertEqual(alias.pk, alias_id)
        self.assertEqual(alias.kind, PersonAlias.Kind.NICKNAME)
        self.assertTrue(alias.display_publicly)

    def test_apply_command_requires_confirmation_token(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "v18_people_registry_apply",
                "--candidate",
                "/tmp/missing-candidate.json",
                "--expected-sha256",
                "a" * 64,
                "--approved-live-preflight",
                "/tmp/missing-preflight.json",
                "--expected-preflight-sha256",
                "b" * 64,
                "--apply",
                stdout=StringIO(),
            )
        self.assertIn("--confirm", str(ctx.exception))
        self.assertIn(APPLY_CONFIRM_TOKEN, str(ctx.exception))

    def test_load_approved_live_preflight_fail_closed(self):
        candidate = live_candidate([live_person("N:1", name="א")])
        payload = approved_preflight_payload(candidate, candidate_sha256="c" * 64)
        raw = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        path = Path("/tmp/v18_apply_approved_preflight_test.json")
        path.write_bytes(raw)
        digest = _sha256_bytes(raw)

        loaded = load_approved_live_preflight(
            path,
            expected_preflight_sha256=digest,
            expected_candidate_sha256="c" * 64,
        )
        self.assertEqual(loaded["database"]["status"], "LIVE_PREFLIGHT_PASS")

        with self.assertRaises(ApplyError):
            load_approved_live_preflight(
                path,
                expected_preflight_sha256="0" * 64,
                expected_candidate_sha256="c" * 64,
            )

        with self.assertRaises(ApplyError):
            load_approved_live_preflight(
                path,
                expected_preflight_sha256=digest,
                expected_candidate_sha256="d" * 64,
            )
