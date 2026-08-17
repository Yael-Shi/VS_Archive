"""PersonAlias schema, write services, and PHOTO search integration (PR6a)."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_search_index import (
    archive_item_ids_for_person_photo_appearances,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.photo_content_management import (
    PERSON_ALIAS_DUPLICATE_ERROR,
    PERSON_ALIAS_MATCHES_CANONICAL_ERROR,
    PERSON_ALIAS_REQUIRED_ERROR,
    PERSON_ALIAS_TOO_LONG_ERROR,
    PhotoContentManagementError,
    create_person_alias,
    delete_person_alias,
    update_person_alias,
    update_person_name,
    update_photo_content_metadata,
)


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    return rebuild_archive_item_search_index(_load_item(archive_item_id))


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


def _create_photo_item(
    *,
    title: str,
    visibility: str = ArchiveItem.Visibility.PUBLIC,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
) -> PhotoContent:
    if original_file_key is None:
        original_file_key = f"photos/{item.pk}-{position}/original.jpg"
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=original_file_key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
    )


def _metadata_update_kwargs(**overrides) -> dict:
    values = {
        "description": "",
        "location": "",
        "context": "",
        "people_present": "",
        "notes": "",
        "date_start": None,
        "date_end": None,
        "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        "person_ids": [],
        "new_person_name": "",
    }
    values.update(overrides)
    return values


def _alias_sql(captured_queries) -> list[str]:
    return [
        query["sql"]
        for query in captured_queries
        if "documents_personalias" in query["sql"].lower()
    ]


class PersonAliasModelTests(TestCase):
    def test_person_alias_can_be_created(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = PersonAlias.objects.create(person=person, name="Jacob Cohen")
        self.assertEqual(alias.person_id, person.pk)
        self.assertEqual(alias.name, "Jacob Cohen")
        self.assertIsNotNone(alias.created_at)
        self.assertIsNotNone(alias.updated_at)
        self.assertEqual(str(alias), "Jacob Cohen")
        self.assertEqual(list(person.aliases.all()), [alias])

    def test_person_can_have_multiple_aliases(self):
        person = Person.objects.create(name="יעקב כהן")
        first = PersonAlias.objects.create(person=person, name="Jacob Cohen")
        second = PersonAlias.objects.create(person=person, name="Yaakov Cohen")
        self.assertEqual(person.aliases.count(), 2)
        self.assertCountEqual(
            person.aliases.values_list("pk", flat=True),
            [first.pk, second.pk],
        )

    def test_two_persons_may_share_the_same_alias_string(self):
        first = Person.objects.create(name="Jacob A")
        second = Person.objects.create(name="Jacob B")
        PersonAlias.objects.create(person=first, name="Yaakov")
        PersonAlias.objects.create(person=second, name="Yaakov")
        self.assertEqual(PersonAlias.objects.filter(name="Yaakov").count(), 2)

    def test_duplicate_alias_on_the_same_person_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                PersonAlias.objects.create(person=person, name="Jacob Cohen")
        self.assertEqual(person.aliases.count(), 1)

    def test_deleting_person_cascades_aliases(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        PersonAlias.objects.create(person=person, name="Yaakov Cohen")
        person.delete()
        self.assertEqual(PersonAlias.objects.count(), 0)

    def test_existing_person_with_zero_aliases_remains_valid(self):
        person = Person.objects.create(name="Ada Lovelace")
        self.assertEqual(person.aliases.count(), 0)
        self.assertEqual(person.name, "Ada Lovelace")
        self.assertEqual(Person.objects.filter(pk=person.pk).count(), 1)

    def test_alias_name_is_not_globally_unique(self):
        constraint_names = {
            constraint.name for constraint in PersonAlias._meta.constraints
        }
        self.assertEqual(constraint_names, {"uniq_person_alias_person_name"})
        fields = PersonAlias._meta.constraints[0].fields
        self.assertEqual(list(fields), ["person", "name"])


class PersonAliasServiceTests(TestCase):
    def test_create_strips_surrounding_whitespace_and_preserves_interior(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(person, name="  Jacob  Cohen  ")
        self.assertEqual(alias.name, "Jacob  Cohen")
        person.refresh_from_db()
        self.assertEqual(person.name, "יעקב כהן")

    def test_create_preserves_case_and_unicode(self):
        person = Person.objects.create(name="יעקב כהן")
        hebrew = create_person_alias(person, name="יעקב")
        latin = create_person_alias(person, name="Yaakov Cohen")
        self.assertEqual(hebrew.name, "יעקב")
        self.assertEqual(latin.name, "Yaakov Cohen")

    def test_alias_may_equal_another_persons_canonical_name(self):
        first = Person.objects.create(name="Jacob Cohen")
        second = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(second, name="Jacob Cohen")
        self.assertEqual(alias.name, "Jacob Cohen")
        self.assertEqual(first.aliases.count(), 0)

    def test_empty_alias_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                with self.assertRaises(PhotoContentManagementError) as raised:
                    create_person_alias(person, name=raw)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.message, PERSON_ALIAS_REQUIRED_ERROR)
        self.assertEqual(person.aliases.count(), 0)

    def test_alias_longer_than_255_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        with self.assertRaises(PhotoContentManagementError) as raised:
            create_person_alias(person, name="x" * 256)
        self.assertEqual(raised.exception.message, PERSON_ALIAS_TOO_LONG_ERROR)
        self.assertEqual(person.aliases.count(), 0)

    def test_exact_canonical_name_duplicate_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        with self.assertRaises(PhotoContentManagementError) as raised:
            create_person_alias(person, name="  יעקב כהן  ")
        self.assertEqual(raised.exception.message, PERSON_ALIAS_MATCHES_CANONICAL_ERROR)
        self.assertEqual(person.aliases.count(), 0)

    def test_duplicate_alias_becomes_service_error(self):
        person = Person.objects.create(name="יעקב כהן")
        create_person_alias(person, name="Jacob Cohen")
        with self.assertRaises(PhotoContentManagementError) as raised:
            create_person_alias(person, name="Jacob Cohen")
        self.assertEqual(raised.exception.message, PERSON_ALIAS_DUPLICATE_ERROR)
        self.assertEqual(person.aliases.count(), 1)

    def test_update_renames_alias_without_changing_canonical_name(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(person, name="Jacob Cohen")
        updated = update_person_alias(alias, name="  Yaakov Cohen  ")
        self.assertEqual(updated.pk, alias.pk)
        self.assertEqual(updated.name, "Yaakov Cohen")
        person.refresh_from_db()
        self.assertEqual(person.name, "יעקב כהן")

    def test_update_to_canonical_name_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(person, name="Jacob Cohen")
        with self.assertRaises(PhotoContentManagementError) as raised:
            update_person_alias(alias, name="יעקב כהן")
        self.assertEqual(raised.exception.message, PERSON_ALIAS_MATCHES_CANONICAL_ERROR)
        alias.refresh_from_db()
        self.assertEqual(alias.name, "Jacob Cohen")

    def test_update_to_existing_alias_on_same_person_is_rejected(self):
        person = Person.objects.create(name="יעקב כהן")
        create_person_alias(person, name="Jacob Cohen")
        other = create_person_alias(person, name="Yaakov Cohen")
        with self.assertRaises(PhotoContentManagementError) as raised:
            update_person_alias(other, name="Jacob Cohen")
        self.assertEqual(raised.exception.message, PERSON_ALIAS_DUPLICATE_ERROR)
        other.refresh_from_db()
        self.assertEqual(other.name, "Yaakov Cohen")

    def test_delete_removes_alias_only(self):
        person = Person.objects.create(name="יעקב כהן")
        keep = create_person_alias(person, name="Jacob Cohen")
        gone = create_person_alias(person, name="Yaakov Cohen")
        delete_person_alias(gone)
        self.assertFalse(PersonAlias.objects.filter(pk=gone.pk).exists())
        self.assertTrue(PersonAlias.objects.filter(pk=keep.pk).exists())
        self.assertTrue(Person.objects.filter(pk=person.pk).exists())

    def test_write_services_do_not_touch_photo_item_or_tag_relations(self):
        person = Person.objects.create(name="יעקב כהן")
        other = Person.objects.create(name="Ada")
        item = _create_photo_item(title="Relations")
        photo = _add_photo(item, position=1)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        tag = Tag.objects.create(name="family")
        item.tags.add(tag)

        alias = create_person_alias(person, name="Jacob Cohen")
        update_person_alias(alias, name="Yaakov Cohen")
        delete_person_alias(alias)
        create_person_alias(other, name="Ada Lovelace")

        person.refresh_from_db()
        self.assertEqual(person.name, "יעקב כהן")
        self.assertEqual(PhotoPerson.objects.filter(person=person).count(), 1)
        self.assertEqual(ArchiveItemPerson.objects.filter(person=person).count(), 1)
        self.assertEqual(list(item.tags.all()), [tag])
        self.assertEqual(photo.people.count(), 1)
        self.assertEqual(item.people.count(), 1)

    def test_canonical_rename_does_not_rewrite_or_delete_matching_alias(self):
        person = Person.objects.create(name="יעקב כהן")
        alias = create_person_alias(person, name="Jacob Cohen")
        update_person_name(person, name="Jacob Cohen")
        alias.refresh_from_db()
        person.refresh_from_db()
        self.assertEqual(person.name, "Jacob Cohen")
        self.assertEqual(alias.name, "Jacob Cohen")
        self.assertEqual(person.aliases.count(), 1)


class PersonAliasSearchTests(TestCase):
    def test_alias_on_renderable_photo_enters_metadata_text_after_canonical(self):
        item = _create_photo_item(title="Named photo")
        photo = _add_photo(item, position=1)
        zeta = Person.objects.create(name="Zeta")
        ada = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=photo, person=zeta)
        PhotoPerson.objects.create(photo_content=photo, person=ada)
        PersonAlias.objects.create(person=zeta, name="Z-alias-b")
        PersonAlias.objects.create(person=zeta, name="Z-alias-a")
        PersonAlias.objects.create(person=ada, name="A-alias")

        content = build_archive_item_search_content(_load_item(item.pk))
        ada_at = content.metadata_text.find("Ada")
        zeta_at = content.metadata_text.find("Zeta")
        a_alias_at = content.metadata_text.find("A-alias")
        z_a_at = content.metadata_text.find("Z-alias-a")
        z_b_at = content.metadata_text.find("Z-alias-b")
        self.assertLess(ada_at, zeta_at)
        self.assertLess(zeta_at, a_alias_at)
        self.assertLess(a_alias_at, z_a_at)
        self.assertLess(z_a_at, z_b_at)

    def test_search_by_alias_and_canonical_finds_the_same_archive_item(self):
        item = _create_photo_item(title="Cohen photo")
        photo = _add_photo(item, position=1)
        person = Person.objects.create(name="יעקב כהן")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        PersonAlias.objects.create(person=person, name="Yaakov Cohen")
        _rebuild(item.pk)

        qs = ArchiveItem.objects.all()
        for token in ("יעקב כהן", "Jacob Cohen", "Yaakov Cohen"):
            self.assertEqual(
                _ids(filter_archive_items_by_search_query(qs, token)), [item.pk], token
            )

    def test_same_alias_through_multiple_photos_contributes_once(self):
        item = _create_photo_item(title="Repeated person")
        first = _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        person = Person.objects.create(name="יעקב כהן")
        PhotoPerson.objects.create(photo_content=first, person=person)
        PhotoPerson.objects.create(photo_content=second, person=person)
        PersonAlias.objects.create(person=person, name="Jacob Cohen")
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("Jacob Cohen"), 1)
        self.assertEqual(content.metadata_text.count("יעקב כהן"), 1)

    def test_alias_on_non_renderable_photo_only_is_excluded(self):
        item = _create_photo_item(title="Pending alias")
        renderable = _add_photo(item, position=1)
        pending = _add_photo(
            item,
            position=2,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        visible = Person.objects.create(name="VisiblePerson")
        hidden = Person.objects.create(name="HiddenPerson")
        PhotoPerson.objects.create(photo_content=renderable, person=visible)
        PhotoPerson.objects.create(photo_content=pending, person=hidden)
        PersonAlias.objects.create(person=visible, name="VisibleAliasToken")
        PersonAlias.objects.create(person=hidden, name="HiddenAliasToken")
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("VisibleAliasToken", content.metadata_text)
        self.assertNotIn("HiddenAliasToken", content.metadata_text)
        self.assertNotIn("HiddenPerson", content.metadata_text)

    def test_archive_item_person_only_alias_is_excluded(self):
        item = _create_photo_item(title="Item person only")
        _add_photo(item, position=1)
        related = Person.objects.create(name="ItemOnlyPerson")
        ArchiveItemPerson.objects.create(archive_item=item, person=related)
        PersonAlias.objects.create(person=related, name="ItemOnlyAliasToken")
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertNotIn("ItemOnlyAliasToken", content.metadata_text)
        self.assertNotIn("ItemOnlyPerson", content.metadata_text)

    def test_removing_photo_person_removes_alias_search_text(self):
        item = _create_photo_item(title="Unlink person")
        photo = _add_photo(item, position=1)
        person = Person.objects.create(name="LinkedPerson")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        PersonAlias.objects.create(person=person, name="LinkedAliasToken")
        _rebuild(item.pk)
        self.assertIn("LinkedAliasToken", _index_for(item.pk).metadata_text)
        update_photo_content_metadata(photo, **_metadata_update_kwargs(person_ids=[]))
        self.assertNotIn("LinkedAliasToken", _index_for(item.pk).metadata_text)
        self.assertNotIn("LinkedPerson", _index_for(item.pk).metadata_text)
        self.assertTrue(PersonAlias.objects.filter(name="LinkedAliasToken").exists())

    def test_alias_create_edit_delete_refreshes_all_affected_archive_items(self):
        first = _create_photo_item(title="First linked")
        second = _create_photo_item(title="Second linked")
        unrelated = _create_photo_item(title="Unrelated")
        person = Person.objects.create(name="יעקב כהן")
        other = Person.objects.create(name="OtherPerson")
        PhotoPerson.objects.create(
            photo_content=_add_photo(first, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(first, position=2), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(second, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(unrelated, position=1), person=other
        )
        ArchiveItemPerson.objects.create(archive_item=unrelated, person=person)
        for archive_item in (first, second, unrelated):
            _rebuild(archive_item.pk)

        self.assertEqual(
            archive_item_ids_for_person_photo_appearances(person.pk),
            [first.pk, second.pk],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            alias = create_person_alias(person, name="Jacob Cohen")
        self.assertEqual(wrapped.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in wrapped.call_args_list],
            [first.pk, second.pk],
        )
        self.assertIn("Jacob Cohen", _index_for(first.pk).metadata_text)
        self.assertIn("Jacob Cohen", _index_for(second.pk).metadata_text)
        self.assertNotIn("Jacob Cohen", _index_for(unrelated.pk).metadata_text)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            update_person_alias(alias, name="Yaakov Cohen")
        self.assertEqual(wrapped.call_count, 2)
        self.assertIn("Yaakov Cohen", _index_for(first.pk).metadata_text)
        self.assertNotIn("Jacob Cohen", _index_for(first.pk).metadata_text)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            delete_person_alias(alias)
        self.assertEqual(wrapped.call_count, 2)
        self.assertNotIn("Yaakov Cohen", _index_for(first.pk).metadata_text)
        self.assertIn("יעקב כהן", _index_for(first.pk).metadata_text)

    def test_multiple_photo_person_links_on_one_item_rebuild_once(self):
        item = _create_photo_item(title="One album")
        person = Person.objects.create(name="יעקב כהן")
        PhotoPerson.objects.create(
            photo_content=_add_photo(item, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(item, position=2), person=person
        )
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            create_person_alias(person, name="Jacob Cohen")
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args, (item.pk,))

    def test_private_visibility_filtering_unchanged_for_alias_search(self):
        family = User.objects.create_user(
            username="alias_family",
            password="test-pass",
        )
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family.groups.add(family_group)
        private = _create_photo_item(
            title="Private album",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        photo = _add_photo(private, position=1)
        person = Person.objects.create(name="SecretPerson")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        PersonAlias.objects.create(person=person, name="SecretAliasToken")
        _rebuild(private.pk)

        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    archive_browse_queryset_for_user(None),
                    "SecretAliasToken",
                )
            ),
            [],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    archive_browse_queryset_for_user(family),
                    "SecretAliasToken",
                )
            ),
            [private.pk],
        )

    def test_result_is_one_archive_item_without_photo_deeplink(self):
        item = _create_photo_item(title="Public gallery album")
        _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        person = Person.objects.create(name="יעקב כהן")
        PhotoPerson.objects.create(photo_content=second, person=person)
        PersonAlias.objects.create(person=person, name="JacobCohenSearchToken")
        _rebuild(item.pk)

        resp = self.client.get(
            reverse("archive-list"),
            {"q": "JacobCohenSearchToken"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        card = resp.context["browse_cards"][0]
        expected = reverse("archive-detail", kwargs={"item_id": item.pk})
        self.assertEqual(card.detail_url, expected)
        self.assertNotIn("photo=", card.detail_url)
        self.assertNotIn(str(second.pk), card.detail_url)
        self.assertNotIn("person=", card.detail_url)


class PersonAliasPerformanceTests(TestCase):
    def test_rebuild_query_count_does_not_scale_with_alias_count(self):
        few = _create_photo_item(title="Few aliases")
        many = _create_photo_item(title="Many aliases")
        few_people = [
            Person.objects.create(name=f"Few person {index}") for index in range(3)
        ]
        many_people = [
            Person.objects.create(name=f"Many person {index}") for index in range(3)
        ]
        for position in range(1, 4):
            few_photo = _add_photo(few, position=position)
            few_photo.people.set(few_people)
            many_photo = _add_photo(many, position=position)
            many_photo.people.set(many_people)
        for person in few_people:
            PersonAlias.objects.create(person=person, name=f"{person.name} alias 1")
        for person in many_people:
            for alias_index in range(1, 8):
                PersonAlias.objects.create(
                    person=person,
                    name=f"{person.name} alias {alias_index}",
                )

        with CaptureQueriesContext(connection) as few_ctx:
            rebuild_archive_item_search_index(_load_item(few.pk))
        with CaptureQueriesContext(connection) as many_ctx:
            rebuild_archive_item_search_index(_load_item(many.pk))
        self.assertEqual(len(few_ctx), len(many_ctx))
        self.assertEqual(len(_alias_sql(few_ctx.captured_queries)), 1)
        self.assertEqual(len(_alias_sql(many_ctx.captured_queries)), 1)


class PersonAliasAdminTests(TestCase):
    def test_person_and_person_alias_remain_unregistered(self):
        self.assertFalse(django_admin.site.is_registered(Person))
        self.assertFalse(django_admin.site.is_registered(PersonAlias))
        self.assertFalse(django_admin.site.is_registered(ArchiveItemPerson))
        self.assertFalse(django_admin.site.is_registered(PhotoPerson))


class PersonAliasMigrationTests(TestCase):
    def test_migration_is_additive_create_model_with_uniqueness(self):
        import importlib

        migration_module = importlib.import_module(
            "documents.migrations.0054_personalias"
        )
        Migration = migration_module.Migration
        self.assertEqual(
            Migration.dependencies,
            [("documents", "0053_photocontent_multi_photo_foundation")],
        )
        self.assertEqual(len(Migration.operations), 1)
        create_op = Migration.operations[0]
        self.assertEqual(create_op.name, "PersonAlias")
        field_names = {name for name, _field in create_op.fields}
        self.assertEqual(
            field_names,
            {"id", "name", "created_at", "updated_at", "person"},
        )
        constraints = create_op.options["constraints"]
        self.assertEqual(len(constraints), 1)
        self.assertEqual(constraints[0].name, "uniq_person_alias_person_name")
        self.assertEqual(list(constraints[0].fields), ["person", "name"])
        self.assertEqual(create_op.options["ordering"], ["name", "id"])
