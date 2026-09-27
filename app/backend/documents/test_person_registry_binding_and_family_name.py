"""Person registry bindings and structured family names."""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.test import TestCase

from documents.historical_person_tag_map import HISTORICAL_PERSON_NAME_TAG_RECORDS
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    PersonFamilyName,
    PersonRegistryImportBinding,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    SEARCH_SEGMENT_SEPARATOR,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
)
from documents.services.person_merge import merge_persons
from documents.services.person_search import person_identity_icontains_q

FROZEN_PERSON_IDS = frozenset(
    person_id for _tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS
)


def _next_ordinary_person_id() -> int:
    max_frozen = max(FROZEN_PERSON_IDS)
    max_existing = (
        Person.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    )
    candidate = max(max_frozen, max_existing) + 1
    while (
        candidate in FROZEN_PERSON_IDS or Person.objects.filter(pk=candidate).exists()
    ):
        candidate += 1
    return candidate


def _person(*, name: str) -> Person:
    return Person.objects.create(id=_next_ordinary_person_id(), name=name)


class PersonRegistryImportBindingTests(TestCase):
    def test_same_source_and_stable_key_cannot_bind_two_people(self):
        first = _person(name="First")
        second = _person(name="Second")
        PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="person-1",
            person=first,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PersonRegistryImportBinding.objects.create(
                    source="v18",
                    stable_key="person-1",
                    person=second,
                )
        self.assertEqual(PersonRegistryImportBinding.objects.count(), 1)
        self.assertTrue(Person.objects.filter(pk=second.pk).exists())

    def test_same_stable_key_is_allowed_for_a_different_source(self):
        person = _person(name="Existing")
        other = _person(name="Other")
        PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="person-1",
            person=person,
        )
        PersonRegistryImportBinding.objects.create(
            source="other-registry",
            stable_key="person-1",
            person=other,
        )
        self.assertEqual(PersonRegistryImportBinding.objects.count(), 2)

    def test_binding_can_point_at_an_existing_person(self):
        person = _person(name="Already Here")
        binding = PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="existing-7",
            person=person,
        )
        binding.refresh_from_db()
        self.assertEqual(binding.person_id, person.pk)
        self.assertEqual(person.registry_import_bindings.get().stable_key, "existing-7")

    def test_person_delete_is_protected_while_a_binding_exists(self):
        person = _person(name="Bound")
        PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="keep",
            person=person,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                person.delete()
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())


class PersonFamilyNameTests(TestCase):
    def test_same_person_name_and_role_cannot_be_duplicated(self):
        person = _person(name="כהן")
        PersonFamilyName.objects.create(
            person=person,
            name="לוי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PersonFamilyName.objects.create(
                    person=person,
                    name="לוי",
                    role=PersonFamilyName.Role.PREVIOUS_FAMILY,
                )
        self.assertEqual(person.family_names.count(), 1)

    def test_previous_and_acquired_roles_stay_distinct_for_the_same_name(self):
        person = _person(name="שרה כהן")
        PersonFamilyName.objects.create(
            person=person,
            name="לוי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        PersonFamilyName.objects.create(
            person=person,
            name="לוי",
            role=PersonFamilyName.Role.ACQUIRED_FAMILY,
        )
        roles = set(person.family_names.values_list("role", flat=True))
        self.assertEqual(
            roles,
            {
                PersonFamilyName.Role.PREVIOUS_FAMILY,
                PersonFamilyName.Role.ACQUIRED_FAMILY,
            },
        )


class PersonRegistryMergeTests(TestCase):
    def test_merge_repoints_registry_bindings(self):
        keeper = _person(name="Keeper")
        duplicate = _person(name="Duplicate")
        PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="keeper-key",
            person=keeper,
        )
        PersonRegistryImportBinding.objects.create(
            source="v18",
            stable_key="duplicate-key",
            person=duplicate,
        )

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.registry_bindings_repointed, 1)
        keys = set(
            PersonRegistryImportBinding.objects.filter(person=keeper).values_list(
                "stable_key", flat=True
            )
        )
        self.assertEqual(keys, {"keeper-key", "duplicate-key"})
        self.assertFalse(Person.objects.filter(pk=duplicate.pk).exists())

    def test_merge_repoints_family_names_and_collapses_identical_rows(self):
        keeper = _person(name="Keeper")
        duplicate = _person(name="Duplicate")
        PersonFamilyName.objects.create(
            person=keeper,
            name="לוי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        PersonFamilyName.objects.create(
            person=duplicate,
            name="לוי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        PersonFamilyName.objects.create(
            person=duplicate,
            name="לוי",
            role=PersonFamilyName.Role.ACQUIRED_FAMILY,
        )
        PersonFamilyName.objects.create(
            person=duplicate,
            name="אזולאי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )

        result = merge_persons(keeper_id=keeper.pk, duplicate_id=duplicate.pk)

        self.assertEqual(result.family_names_moved, 2)
        self.assertEqual(result.family_names_deduped, 1)
        rows = set(
            PersonFamilyName.objects.filter(person=keeper).values_list("name", "role")
        )
        self.assertEqual(
            rows,
            {
                ("לוי", PersonFamilyName.Role.PREVIOUS_FAMILY),
                ("לוי", PersonFamilyName.Role.ACQUIRED_FAMILY),
                ("אזולאי", PersonFamilyName.Role.PREVIOUS_FAMILY),
            },
        )
        self.assertFalse(Person.objects.filter(pk=duplicate.pk).exists())


class PersonFamilyNameSearchTests(TestCase):
    def test_person_search_matches_family_names_and_still_matches_alias(self):
        family_person = _person(name="שרה כהן")
        alias_person = _person(name="יעקב לוי")
        other = _person(name="אחר")
        PersonFamilyName.objects.create(
            person=family_person,
            name="אזולאי",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        PersonAlias.objects.create(person=alias_person, name="יענקל")
        Person.objects.create(name="Unrelated", honorific="אזולאי")

        family_q = person_identity_icontains_q("אזולאי")
        alias_q = person_identity_icontains_q("יענקל")
        self.assertIsNotNone(family_q)
        self.assertIsNotNone(alias_q)
        family_ids = set(Person.objects.filter(family_q).values_list("pk", flat=True))
        alias_ids = set(Person.objects.filter(alias_q).values_list("pk", flat=True))
        self.assertEqual(family_ids, {family_person.pk})
        self.assertEqual(alias_ids, {alias_person.pk})
        self.assertNotIn(other.pk, family_ids)
        canonical_q = person_identity_icontains_q("שרה")
        self.assertIn(
            family_person.pk,
            set(Person.objects.filter(canonical_q).values_list("pk", flat=True)),
        )

    def test_archive_metadata_includes_family_names_after_aliases(self):
        item = create_manual_text_archive_item(
            title="Family name item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = _person(name="Amy Canonical")
        PersonAlias.objects.create(person=person, name="AmyAlias")
        PersonFamilyName.objects.create(
            person=person,
            name="AcquiredName",
            role=PersonFamilyName.Role.ACQUIRED_FAMILY,
        )
        PersonFamilyName.objects.create(
            person=person,
            name="PreviousName",
            role=PersonFamilyName.Role.PREVIOUS_FAMILY,
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        loaded = archive_items_for_search_index_build(archive_item_ids=[item.pk]).get()
        content = build_archive_item_search_content(loaded)
        self.assertEqual(
            content.metadata_text.split(SEARCH_SEGMENT_SEPARATOR),
            ["Amy Canonical", "AmyAlias", "PreviousName", "AcquiredName"],
        )

    def test_archive_metadata_keeps_canonical_then_alias_order_without_family_names(
        self,
    ):
        item = create_manual_text_archive_item(
            title="Alias order",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="Zed Canonical")
        PersonAlias.objects.create(person=person, name="ZedAlias")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        loaded = archive_items_for_search_index_build(archive_item_ids=[item.pk]).get()
        content = build_archive_item_search_content(loaded)
        self.assertEqual(
            content.metadata_text.split(SEARCH_SEGMENT_SEPARATOR),
            ["Zed Canonical", "ZedAlias"],
        )
