"""Typed PersonAlias metadata, staff editing, and public Person profile."""

from __future__ import annotations

import importlib
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    PhotoContent,
)
from documents.services.photo_content_management import (
    PERSON_ALIAS_KIND_INVALID_ERROR,
    PERSON_ALIAS_LANGUAGE_INVALID_ERROR,
    PhotoContentManagementError,
    create_person_alias,
    update_person_alias,
)
from documents.services.person_display import (
    public_person_additional_name_groups,
    public_person_aliases_prefetch,
)
from documents.views import _person_alias_staff_sort_key


class PersonAliasTypedModelTests(TestCase):
    def test_existing_style_alias_defaults_to_private_unspecified_metadata(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = PersonAlias.objects.create(person=person, name="Jacob Cohen")

        self.assertEqual(alias.kind, PersonAlias.Kind.UNSPECIFIED)
        self.assertEqual(alias.language, "")
        self.assertFalse(alias.display_publicly)

    def test_kind_and_language_choices_cover_supported_values(self):
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
                "other",
            },
        )
        self.assertEqual(
            set(PersonAlias.Language.values),
            {"he", "en", "fr", "ar"},
        )


class PersonAliasTypedMigrationTests(TestCase):
    def test_0069_is_additive_and_defaults_existing_aliases_to_private(self):
        migration_module = importlib.import_module(
            "documents.migrations.0069_personalias_metadata"
        )
        Migration = migration_module.Migration

        self.assertEqual(
            Migration.dependencies,
            [("documents", "0068_alter_transkribustranscriptsnapshot_source_kind")],
        )
        self.assertEqual(len(Migration.operations), 3)

        fields = {
            operation.name: operation.field
            for operation in Migration.operations
        }

        self.assertEqual(set(fields), {"kind", "language", "display_publicly"})
        self.assertEqual(fields["kind"].default, "unspecified")
        self.assertEqual(fields["language"].default, "")
        self.assertFalse(fields["display_publicly"].default)


class PersonAliasTypedServiceTests(TestCase):
    def test_create_persists_typed_metadata(self):
        person = Person.objects.create(name="אלי כהן")

        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ):
            alias = create_person_alias(
                person,
                name="  Eli Cohen  ",
                kind=PersonAlias.Kind.OTHER_LANGUAGE,
                language=PersonAlias.Language.ENGLISH,
                display_publicly=True,
            )

        self.assertEqual(alias.name, "Eli Cohen")
        self.assertEqual(alias.kind, PersonAlias.Kind.OTHER_LANGUAGE)
        self.assertEqual(alias.language, PersonAlias.Language.ENGLISH)
        self.assertTrue(alias.display_publicly)

    def test_old_create_call_remains_private_unspecified(self):
        person = Person.objects.create(name="אלי כהן")

        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ):
            alias = create_person_alias(person, name="Eli Cohen")

        self.assertEqual(alias.kind, PersonAlias.Kind.UNSPECIFIED)
        self.assertEqual(alias.language, "")
        self.assertFalse(alias.display_publicly)

    def test_invalid_kind_and_language_are_rejected(self):
        person = Person.objects.create(name="אלי כהן")

        with self.assertRaises(PhotoContentManagementError) as raised_kind:
            create_person_alias(
                person,
                name="Invalid kind alias",
                kind="not-a-kind",
            )
        self.assertEqual(
            raised_kind.exception.message,
            PERSON_ALIAS_KIND_INVALID_ERROR,
        )

        with self.assertRaises(PhotoContentManagementError) as raised_language:
            create_person_alias(
                person,
                name="Invalid language alias",
                language="xx",
            )
        self.assertEqual(
            raised_language.exception.message,
            PERSON_ALIAS_LANGUAGE_INVALID_ERROR,
        )

        self.assertEqual(person.aliases.count(), 0)

    def test_name_only_update_preserves_existing_metadata(self):
        person = Person.objects.create(name="אלי כהן")
        alias = PersonAlias.objects.create(
            person=person,
            name="Eli Cohen",
            kind=PersonAlias.Kind.OTHER_LANGUAGE,
            language=PersonAlias.Language.ENGLISH,
            display_publicly=True,
        )

        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ):
            update_person_alias(alias, name="Eli Kohn")

        alias.refresh_from_db()
        self.assertEqual(alias.name, "Eli Kohn")
        self.assertEqual(alias.kind, PersonAlias.Kind.OTHER_LANGUAGE)
        self.assertEqual(alias.language, PersonAlias.Language.ENGLISH)
        self.assertTrue(alias.display_publicly)

    def test_metadata_only_update_does_not_refresh_search_index(self):
        person = Person.objects.create(name="אלי כהן")
        alias = PersonAlias.objects.create(
            person=person,
            name="Eli Cohen",
        )

        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ) as sync:
            update_person_alias(
                alias,
                name="Eli Cohen",
                kind=PersonAlias.Kind.OTHER_LANGUAGE,
                language=PersonAlias.Language.ENGLISH,
                display_publicly=True,
            )

        sync.assert_not_called()
        alias.refresh_from_db()
        self.assertEqual(alias.kind, PersonAlias.Kind.OTHER_LANGUAGE)
        self.assertEqual(alias.language, PersonAlias.Language.ENGLISH)
        self.assertTrue(alias.display_publicly)


class PersonAliasStaffSortTests(TestCase):
    def test_staff_alias_order_is_hebrew_then_latin_then_arabic(self):
        person = Person.objects.create(name="אדם")
        names = [
            "كمال أمين ثابت",
            "כמאל אמין",
            "Élie Cohen",
            "אלי",
            "Eli Cohen",
            "إيلي كوهين",
        ]
        aliases = [
            PersonAlias.objects.create(person=person, name=name)
            for name in names
        ]

        ordered = sorted(aliases, key=_person_alias_staff_sort_key)

        self.assertEqual(
            [alias.name for alias in ordered],
            [
                "אלי",
                "כמאל אמין",
                "Eli Cohen",
                "Élie Cohen",
                "إيلي كوهين",
                "كمال أمين ثابت",
            ],
        )


class PersonAliasStaffTypedUITests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="typed_alias_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.person = Person.objects.create(name="אלי כהן")
        self.person_url = reverse(
            "archive-manage-person-edit",
            kwargs={"person_id": self.person.id},
        )

    def test_add_alias_saves_type_language_and_public_flag(self):
        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ):
            response = self.client.post(
                self.person_url,
                data={
                    "action": "add_alias",
                    "alias_name": "Eli Cohen",
                    "alias_kind": PersonAlias.Kind.OTHER_LANGUAGE,
                    "alias_language": PersonAlias.Language.ENGLISH,
                    "alias_display_publicly": "on",
                },
            )

        self.assertEqual(response.status_code, 302)

        alias = self.person.aliases.get(name="Eli Cohen")
        self.assertEqual(alias.kind, PersonAlias.Kind.OTHER_LANGUAGE)
        self.assertEqual(alias.language, PersonAlias.Language.ENGLISH)
        self.assertTrue(alias.display_publicly)

    def test_edit_alias_can_change_metadata_without_renaming(self):
        alias = PersonAlias.objects.create(
            person=self.person,
            name="כמאל אמין ת'אבת",
        )
        url = reverse(
            "archive-manage-person-alias-edit",
            kwargs={"person_id": self.person.id, "alias_id": alias.id},
        )

        with patch(
            "documents.services.photo_content_management."
            "_sync_person_search_indexes"
        ) as sync:
            response = self.client.post(
                url,
                data={
                    "name": alias.name,
                    "kind": PersonAlias.Kind.COVER_IDENTITY,
                    "language": PersonAlias.Language.HEBREW,
                    "display_publicly": "on",
                },
            )

        self.assertEqual(response.status_code, 302)
        sync.assert_not_called()

        alias.refresh_from_db()
        self.assertEqual(alias.kind, PersonAlias.Kind.COVER_IDENTITY)
        self.assertEqual(alias.language, PersonAlias.Language.HEBREW)
        self.assertTrue(alias.display_publicly)

    def test_person_edit_context_uses_human_friendly_alias_order(self):
        for name in (
            "كمال أمين ثابت",
            "Élie Cohen",
            "כמאל אמין",
            "Eli Cohen",
            "אלי",
            "إيلي كوهين",
        ):
            PersonAlias.objects.create(person=self.person, name=name)

        response = self.client.get(self.person_url)
        self.assertEqual(response.status_code, 200)

        self.assertEqual(
            [alias.name for alias in response.context["aliases"]],
            [
                "אלי",
                "כמאל אמין",
                "Eli Cohen",
                "Élie Cohen",
                "إيلي كوهين",
                "كمال أمين ثابت",
            ],
        )


class PersonAliasPublicProfileTests(TestCase):
    def setUp(self):
        self.person = Person.objects.create(
            name="אלי כהן",
            biography="ביוגרפיה לצורך בדיקה.",
        )
        self.item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Public Person profile test item",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=self.item,
            position=1,
            original_file_key="photos/person-profile-test/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        ArchiveItemPerson.objects.create(
            archive_item=self.item,
            person=self.person,
        )
        self.url = reverse(
            "archive-person-detail",
            kwargs={"person_id": self.person.id},
        )

    def test_only_explicitly_public_aliases_are_prefetched(self):
        PersonAlias.objects.create(
            person=self.person,
            name="Visible alias",
            kind=PersonAlias.Kind.NICKNAME,
            display_publicly=True,
        )
        PersonAlias.objects.create(
            person=self.person,
            name="Hidden alias",
            kind=PersonAlias.Kind.NICKNAME,
            display_publicly=False,
        )

        person = (
            Person.objects.prefetch_related(public_person_aliases_prefetch())
            .get(pk=self.person.pk)
        )

        self.assertEqual(
            [alias.name for alias in person.aliases.all()],
            ["Visible alias"],
        )

    def test_public_page_hides_private_alias_and_groups_public_aliases(self):
        rows = [
            (
                "Eli Cohen",
                PersonAlias.Kind.OTHER_LANGUAGE,
                PersonAlias.Language.ENGLISH,
                True,
            ),
            (
                "כמאל אמין ת'אבת",
                PersonAlias.Kind.COVER_IDENTITY,
                PersonAlias.Language.HEBREW,
                True,
            ),
            (
                "QA Falcon",
                PersonAlias.Kind.CODE_NAME,
                PersonAlias.Language.ENGLISH,
                True,
            ),
            (
                "QA Aleph",
                PersonAlias.Kind.UNDERGROUND_NAME,
                PersonAlias.Language.ENGLISH,
                True,
            ),
            (
                "האיש שלנו בדמשק",
                PersonAlias.Kind.NICKNAME,
                PersonAlias.Language.HEBREW,
                True,
            ),
            (
                "Eli Kohn",
                PersonAlias.Kind.SPELLING_VARIANT,
                PersonAlias.Language.ENGLISH,
                True,
            ),
            (
                "SearchOnlySecret",
                PersonAlias.Kind.OCR_VARIANT,
                "",
                False,
            ),
        ]

        for name, kind, language, display_publicly in rows:
            PersonAlias.objects.create(
                person=self.person,
                name=name,
                kind=kind,
                language=language,
                display_publicly=display_publicly,
            )

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, "Eli Cohen")
        self.assertContains(response, "כמאל אמין")
        self.assertContains(response, "QA Falcon")
        self.assertContains(response, "QA Aleph")
        self.assertContains(response, "האיש שלנו בדמשק")
        self.assertContains(response, "Eli Kohn")
        self.assertNotContains(response, "SearchOnlySecret")

        groups = response.context["person_additional_name_groups"]
        self.assertEqual(
            [group.label for group in groups],
            [
                "שמות בשפות אחרות",
                "שמות כיסוי וקוד",
                "כינויים ושמות מוכרים",
                "וריאנטים וכתיבים נוספים",
            ],
        )

        cover_group = groups[1]
        self.assertEqual(
            [(entry.label, entry.value) for entry in cover_group.entries],
            [
                ("שם כיסוי", "כמאל אמין ת'אבת"),
                ("שם קוד", "QA Falcon"),
                ("שם מחתרתי", "QA Aleph"),
            ],
        )

    def test_public_unspecified_alias_uses_fallback_group(self):
        PersonAlias.objects.create(
            person=self.person,
            name="Public miscellaneous name",
            kind=PersonAlias.Kind.UNSPECIFIED,
            display_publicly=True,
        )

        groups = public_person_additional_name_groups(
            Person.objects.prefetch_related("aliases").get(pk=self.person.pk)
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].label, "שמות נוספים")
        self.assertEqual(groups[0].entries[0].value, "Public miscellaneous name")
        self.assertEqual(groups[0].entries[0].label, "")
