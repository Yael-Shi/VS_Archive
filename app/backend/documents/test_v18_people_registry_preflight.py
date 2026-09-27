from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from documents.models import (
    Person,
    PersonAlias,
    PersonFamilyName,
    PersonRegistryImportBinding,
)
from documents.services.v18_people_registry_preflight import (
    PreflightError,
    effective_canonical,
    effective_honorific,
    planned_aliases,
    database_preflight,
    planned_binding_keys,
    planned_family_names,
)


def candidate():
    return {
        "candidate_people": [],
        "v16_to_final_survivor_stable_key_mapping": {},
        "site_or_active_registry_apply_performed": False,
        "final_apply_overrides": {
            "canonical_name_overrides": {},
            "manual_alias_additions": {},
            "family_name_exclusions": {},
            "honorific_overrides": {},
            "alias_kind_mapping": {
                "NAME_VARIANT": "name_variant",
                "TRANSLITERATION": "transliteration",
                "HONORIFIC_OR_TITLE": "honorific_or_title",
                "BIRTH_NAME": "birth_name",
            },
        },
    }


class V18PeopleRegistryPreflightPureTests(SimpleTestCase):
    def test_canonical_override_and_manual_alias(self):
        data = candidate()
        person = {
            "person_key": "N:PIN",
            "canonical_name": "יהודה לייב (לאון) פינסקר",
            "honorifics_or_titles": [],
            "approved_aliases": [],
            "previous_family_names": [],
            "married_acquired_family_names": [],
            "additional_family_names": [],
        }

        data["final_apply_overrides"]["canonical_name_overrides"]["N:PIN"] = {
            "to": "יהודה לייב פינסקר"
        }
        data["final_apply_overrides"]["manual_alias_additions"]["N:PIN"] = [
            {
                "form": "לאון פינסקר",
                "kind": "name_variant",
                "display_as_other_name": True,
            }
        ]

        self.assertEqual(
            effective_canonical(person, data),
            "יהודה לייב פינסקר",
        )
        self.assertEqual(
            planned_aliases(person, data),
            [
                {
                    "name": "לאון פינסקר",
                    "kind": "name_variant",
                    "display_publicly": True,
                    "origin": "manual_final_override",
                }
            ],
        )

    def test_contextual_and_canonical_equal_aliases_do_not_persist(self):
        data = candidate()
        person = {
            "person_key": "C:1",
            "canonical_name": "שם קנוני",
            "honorifics_or_titles": [],
            "previous_family_names": [],
            "married_acquired_family_names": [],
            "additional_family_names": [],
            "approved_aliases": [
                {
                    "form": "שם קנוני",
                    "alias_type": "NAME_VARIANT",
                    "usage_scope": "PERSON_SCOPED",
                    "registration_disposition": "ELIGIBLE_IN_REVIEW_PROJECTION",
                },
                {
                    "form": "שם חלקי",
                    "alias_type": "NAME_VARIANT",
                    "usage_scope": "CONTEXTUAL_SEARCH_ONLY",
                    "registration_disposition": "ELIGIBLE_IN_REVIEW_PROJECTION",
                },
                {
                    "form": "Name",
                    "alias_type": "TRANSLITERATION",
                    "usage_scope": "PERSON_SCOPED",
                    "registration_disposition": "ELIGIBLE_IN_REVIEW_PROJECTION",
                    "displayable_on_person_page": True,
                },
            ],
        }

        self.assertEqual(
            planned_aliases(person, data),
            [
                {
                    "name": "Name",
                    "kind": "transliteration",
                    "display_publicly": True,
                    "origin": "reviewed_alias",
                }
            ],
        )

    def test_multiple_titles_require_explicit_override(self):
        data = candidate()
        person = {
            "person_key": "C:9",
            "canonical_name": "משה ונטורה",
            "honorifics_or_titles": ['ד"ר', "הרב"],
        }

        with self.assertRaises(PreflightError):
            effective_honorific(person, data)

        data["final_apply_overrides"]["honorific_overrides"]["C:9"] = 'הרב ד"ר'

        self.assertEqual(
            effective_honorific(person, data),
            'הרב ד"ר',
        )

    def test_additional_family_name_requires_explicit_exclusion(self):
        data = candidate()
        person = {
            "person_key": "N:LUTZ",
            "canonical_name": "ואלטרוד מרתה נוימן",
            "honorifics_or_titles": [],
            "approved_aliases": [],
            "previous_family_names": [],
            "married_acquired_family_names": [],
            "additional_family_names": ["לוץ"],
        }

        with self.assertRaises(PreflightError):
            planned_family_names(person, data)

        data["final_apply_overrides"]["family_name_exclusions"]["N:LUTZ"] = [
            {
                "form": "לוץ",
                "action": "DO_NOT_PERSIST_AS_PERSON_FAMILY_NAME",
            }
        ]

        self.assertEqual(
            planned_family_names(person, data),
            [],
        )

    def test_binding_set_includes_historical_keys(self):
        data = candidate()
        data["candidate_people"] = [
            {"person_key": "C:1"},
            {"person_key": "N:NEW"},
        ]
        data["v16_to_final_survivor_stable_key_mapping"] = {
            "C:1": "C:1",
            "N:OLD": "C:1",
            "N:NEW": "N:NEW",
        }

        self.assertEqual(
            planned_binding_keys(data),
            {
                "C:1": "C:1",
                "N:NEW": "N:NEW",
                "N:OLD": "C:1",
            },
        )


def live_candidate(people):
    return {
        "candidate_people": people,
        "v16_to_final_survivor_stable_key_mapping": {
            person["person_key"]: person["person_key"] for person in people
        },
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


def live_person(person_key, *, existing_person_id=None, name="שם"):
    return {
        "person_key": person_key,
        "existing_person_id": existing_person_id,
        "canonical_name": name,
        "honorifics_or_titles": [],
        "approved_aliases": [],
        "previous_family_names": [],
        "married_acquired_family_names": [],
        "additional_family_names": [],
    }


class V18PeopleRegistryLivePreflightTests(TestCase):
    def test_two_survivor_keys_bound_to_same_person_are_blocked(self):
        person = Person.objects.create(name="אדם קיים")

        PersonRegistryImportBinding.objects.create(
            source="vs_archive_people_v18",
            stable_key="N:A",
            person=person,
        )
        PersonRegistryImportBinding.objects.create(
            source="vs_archive_people_v18",
            stable_key="N:B",
            person=person,
        )

        data = live_candidate(
            [
                live_person("N:A", name="אדם א"),
                live_person("N:B", name="אדם ב"),
            ]
        )

        result = database_preflight(data)

        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn(
            {
                "type": "MULTIPLE_SURVIVORS_BOUND_TO_SAME_PERSON",
                "person_id": person.pk,
                "survivor_keys": ["N:A", "N:B"],
            },
            result["blockers"],
        )

    def test_database_preflight_is_read_only(self):
        person = Person.objects.create(
            name="שם נוכחי",
            honorific="",
        )

        data = live_candidate(
            [
                live_person(
                    "C:1",
                    existing_person_id=person.pk,
                    name="שם מתוכנן",
                )
            ]
        )

        before_counts = {
            "people": Person.objects.count(),
            "aliases": PersonAlias.objects.count(),
            "family_names": PersonFamilyName.objects.count(),
            "bindings": PersonRegistryImportBinding.objects.count(),
        }

        result = database_preflight(data)

        after_counts = {
            "people": Person.objects.count(),
            "aliases": PersonAlias.objects.count(),
            "family_names": PersonFamilyName.objects.count(),
            "bindings": PersonRegistryImportBinding.objects.count(),
        }

        person.refresh_from_db()

        self.assertEqual(result["status"], "LIVE_PREFLIGHT_PASS")
        self.assertEqual(result["summary"]["person_updates"], 1)
        self.assertEqual(before_counts, after_counts)
        self.assertEqual(person.name, "שם נוכחי")
        self.assertEqual(person.honorific, "")
