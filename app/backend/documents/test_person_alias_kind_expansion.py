"""PersonAlias kind expansion: transliteration, honorific_or_title, birth_name."""

from __future__ import annotations

from django.test import TestCase

from documents.models import Person, PersonAlias, PersonFamilyName
from documents.services.person_search import person_identity_icontains_q
from documents.services.photo_content_management import create_person_alias


class PersonAliasKindExpansionTests(TestCase):
    def test_new_kind_values_are_stored_and_existing_kinds_remain(self):
        self.assertEqual(PersonAlias.Kind.TRANSLITERATION, "transliteration")
        self.assertEqual(PersonAlias.Kind.HONORIFIC_OR_TITLE, "honorific_or_title")
        self.assertEqual(PersonAlias.Kind.BIRTH_NAME, "birth_name")
        self.assertEqual(
            set(PersonAlias.Kind.values),
            {
                "unspecified",
                "other_language",
                "cover_identity",
                "code_name",
                "underground_name",
                "nickname",
                "name_variant",
                "spelling_variant",
                "ocr_variant",
                "partial_name",
                "transliteration",
                "honorific_or_title",
                "birth_name",
                "other",
            },
        )

    def test_existing_alias_creation_and_name_search_still_match(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(
            person,
            name="יענקל",
            kind=PersonAlias.Kind.NICKNAME,
        )

        self.assertEqual(alias.kind, PersonAlias.Kind.NICKNAME)
        self.assertEqual(alias.name, "יענקל")
        matched = person_identity_icontains_q("יענקל")
        self.assertIsNotNone(matched)
        self.assertEqual(
            set(Person.objects.filter(matched).values_list("pk", flat=True)),
            {person.pk},
        )
        self.assertEqual(PersonFamilyName.objects.count(), 0)

    def test_birth_name_alias_is_searchable_without_a_family_name_row(self):
        person = Person.objects.create(name="מרדכי אביצור", honorific="")
        alias = create_person_alias(
            person,
            name="מרסל וידסיס",
            kind=PersonAlias.Kind.BIRTH_NAME,
        )

        self.assertEqual(alias.kind, PersonAlias.Kind.BIRTH_NAME)
        self.assertEqual(person.honorific, "")
        self.assertEqual(PersonFamilyName.objects.filter(person=person).count(), 0)
        matched = person_identity_icontains_q("וידסיס")
        self.assertIsNotNone(matched)
        self.assertEqual(
            set(Person.objects.filter(matched).values_list("pk", flat=True)),
            {person.pk},
        )

    def test_transliteration_and_honorific_or_title_store_without_family_names(self):
        person = Person.objects.create(name="משה מרזוק")
        transliteration = create_person_alias(
            person,
            name="Moshe Marzouk",
            kind=PersonAlias.Kind.TRANSLITERATION,
        )
        titled = create_person_alias(
            person,
            name='ד"ר משה מרזוק',
            kind=PersonAlias.Kind.HONORIFIC_OR_TITLE,
        )

        self.assertEqual(transliteration.kind, "transliteration")
        self.assertEqual(titled.kind, "honorific_or_title")
        self.assertEqual(person.honorific, "")
        self.assertEqual(PersonFamilyName.objects.count(), 0)
        matched = person_identity_icontains_q("Marzouk")
        self.assertIsNotNone(matched)
        self.assertIn(
            person.pk,
            set(Person.objects.filter(matched).values_list("pk", flat=True)),
        )
