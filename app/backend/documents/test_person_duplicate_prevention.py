"""Staff new-Person duplicate candidate matching and force-create acknowledgements."""

from __future__ import annotations

from django.test import SimpleTestCase, TestCase

from documents.models import Person, PersonAlias
from documents.services.person_duplicate_check import (
    PERSON_NAME_CANDIDATES_ERROR,
    PersonNameDuplicateConflictError,
    check_new_person_names,
    find_existing_person_candidates,
    person_new_name_token_key,
)
from documents.services.photo_content_management import (
    create_identified_people_from_new_names,
)


class PersonNewNameTokenKeyTests(SimpleTestCase):
    def test_token_key_uses_stripped_utf8_without_casefold(self):
        self.assertEqual(
            person_new_name_token_key("Ada"),
            person_new_name_token_key("  Ada  "),
        )
        self.assertNotEqual(
            person_new_name_token_key("Ada"),
            person_new_name_token_key("ada"),
        )


class PersonDuplicateCandidateMatchingTests(TestCase):
    def test_no_candidate_creates_normally(self):
        created = create_identified_people_from_new_names("Ada Lovelace")
        self.assertEqual([person.name for person in created], ["Ada Lovelace"])
        self.assertEqual(Person.objects.filter(name="Ada Lovelace").count(), 1)

    def test_exact_canonical_match_blocks_creation(self):
        existing = Person.objects.create(name="יעקב כהן")
        with self.assertRaises(PersonNameDuplicateConflictError) as raised:
            create_identified_people_from_new_names("יעקב כהן")
        conflict = raised.exception.check.conflicts[0]
        self.assertEqual(conflict.submitted_name, "יעקב כהן")
        self.assertEqual([candidate.id for candidate in conflict.candidates], [existing.id])
        self.assertEqual(Person.objects.filter(name="יעקב כהן").count(), 1)

    def test_case_insensitive_canonical_match_blocks_creation(self):
        existing = Person.objects.create(name="Ada")
        with self.assertRaises(PersonNameDuplicateConflictError) as raised:
            create_identified_people_from_new_names("ADA")
        self.assertEqual(
            [candidate.id for candidate in raised.exception.check.conflicts[0].candidates],
            [existing.id],
        )
        self.assertFalse(Person.objects.filter(name="ADA").exists())

    def test_exact_alias_match_blocks_creation(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        with self.assertRaises(PersonNameDuplicateConflictError) as raised:
            create_identified_people_from_new_names("Jacob Cohen")
        self.assertEqual(
            [candidate.id for candidate in raised.exception.check.conflicts[0].candidates],
            [person.id],
        )
        self.assertFalse(Person.objects.filter(name="Jacob Cohen").exists())

    def test_case_insensitive_alias_match_blocks_creation(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        with self.assertRaises(PersonNameDuplicateConflictError):
            create_identified_people_from_new_names("jacob cohen")
        self.assertFalse(Person.objects.filter(name="jacob cohen").exists())

    def test_no_substring_or_fuzzy_match(self):
        Person.objects.create(name="Ada Lovelace")
        Person.objects.create(name="Ada")
        other = Person.objects.create(name="Charles")
        PersonAlias.objects.create(person=other, name="Ada Byron")
        self.assertEqual(find_existing_person_candidates("Ada Love"), ())
        created = create_identified_people_from_new_names("Ada Love")
        self.assertEqual(created[0].name, "Ada Love")

    def test_same_alias_on_two_people_returns_both(self):
        first = Person.objects.create(name="First")
        second = Person.objects.create(name="Second")
        PersonAlias.objects.create(person=first, name="Shared Alias")
        PersonAlias.objects.create(person=second, name="Shared Alias")
        candidates = find_existing_person_candidates("Shared Alias")
        self.assertEqual(
            [candidate.id for candidate in candidates],
            [first.id, second.id],
        )

    def test_same_canonical_name_on_two_people_returns_both(self):
        first = Person.objects.create(name="יעקב כהן")
        second = Person.objects.create(name="יעקב כהן")
        candidates = find_existing_person_candidates("יעקב כהן")
        self.assertEqual(
            [candidate.id for candidate in candidates],
            [first.id, second.id],
        )

    def test_canonical_and_alias_same_person_are_deduped(self):
        person = Person.objects.create(name="Ada")
        PersonAlias.objects.create(person=person, name="ada")
        candidates = find_existing_person_candidates("ADA")
        self.assertEqual([candidate.id for candidate in candidates], [person.id])

    def test_multi_name_input_reports_all_conflicting_tokens(self):
        Person.objects.create(name="Alpha")
        other = Person.objects.create(name="Other")
        PersonAlias.objects.create(person=other, name="Beta")
        check = check_new_person_names("Alpha, Unique New, Beta")
        self.assertEqual(check.errors, [PERSON_NAME_CANDIDATES_ERROR])
        self.assertEqual(
            [conflict.submitted_name for conflict in check.conflicts],
            ["Alpha", "Beta"],
        )
        self.assertEqual(check.names, ["Alpha", "Unique New", "Beta"])

    def test_one_conflicting_token_creates_zero_people(self):
        Person.objects.create(name="Alpha")
        with self.assertRaises(PersonNameDuplicateConflictError):
            create_identified_people_from_new_names("Alpha, Unique New")
        self.assertFalse(Person.objects.filter(name="Unique New").exists())
        self.assertEqual(Person.objects.filter(name="Alpha").count(), 1)

    def test_force_create_for_exact_token_permits_new_person(self):
        existing = Person.objects.create(name="Ada")
        created = create_identified_people_from_new_names(
            "Ada",
            force_create_person_keys=[person_new_name_token_key("Ada")],
        )
        self.assertEqual(len(created), 1)
        self.assertNotEqual(created[0].pk, existing.pk)
        self.assertEqual(Person.objects.filter(name="Ada").count(), 2)

    def test_force_approval_for_one_token_does_not_approve_another(self):
        Person.objects.create(name="Ada")
        Person.objects.create(name="Charles")
        with self.assertRaises(PersonNameDuplicateConflictError) as raised:
            create_identified_people_from_new_names(
                "Ada, Charles",
                force_create_person_keys=[person_new_name_token_key("Ada")],
            )
        self.assertEqual(
            [conflict.submitted_name for conflict in raised.exception.check.conflicts],
            ["Charles"],
        )
        self.assertEqual(Person.objects.filter(name="Ada").count(), 1)
        self.assertEqual(Person.objects.filter(name="Charles").count(), 1)

    def test_stale_force_approval_does_not_bypass_detection(self):
        Person.objects.create(name="Ada")
        with self.assertRaises(PersonNameDuplicateConflictError):
            create_identified_people_from_new_names(
                "Ada",
                force_create_person_keys=[person_new_name_token_key("Charles")],
            )
        with self.assertRaises(PersonNameDuplicateConflictError):
            create_identified_people_from_new_names(
                "ada",
                force_create_person_keys=[person_new_name_token_key("Ada")],
            )
        self.assertEqual(Person.objects.filter(name__iexact="ada").count(), 1)
