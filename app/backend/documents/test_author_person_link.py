"""Explicit Author -> Person link foundation (staff only, no name inference)."""

from __future__ import annotations

import re
from importlib import import_module

from django.contrib.auth.models import Group, User
from django.db.models.deletion import SET_NULL
from django.test import TestCase
from django.urls import reverse

from documents.models import ArchiveItem, Author, Person, PersonAlias
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_authors import AUTHOR_NOT_FOUND_ERROR
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.author_person_link import (
    AUTHOR_PERSON_ID_INVALID_ERROR,
    AUTHOR_PERSON_LINK_UPDATED_MSG,
    parse_author_person_id,
    set_author_person,
)
from documents.services.photo_content_management import PERSON_NOT_FOUND_ERROR
from documents.views import AUTHOR_NAME_UPDATED_MSG

SCHEMA_MIGRATION = "0063_author_person_link"


def _edit_url(author: Author) -> str:
    return reverse("archive-manage-author-edit", kwargs={"author_id": author.id})


def _staff() -> User:
    return User.objects.create_user(
        username="author_person_link_staff",
        password="test-pass",
        is_staff=True,
    )


class AuthorPersonLinkModelTests(TestCase):
    def test_author_person_is_nullable_and_blank(self):
        author = Author.objects.create(name="Unlinked bibliographic")
        self.assertIsNone(author.person_id)
        field = Author._meta.get_field("person")
        self.assertTrue(field.null)
        self.assertTrue(field.blank)
        self.assertIs(field.remote_field.on_delete, SET_NULL)
        self.assertEqual(field.remote_field.related_name, "author_identities")
        self.assertFalse(field.unique)

    def test_multiple_authors_may_link_to_one_person(self):
        person = Person.objects.create(name="Ada Lovelace")
        first = Author.objects.create(name="Ada Lovelace", person=person)
        second = Author.objects.create(name="A. Lovelace", person=person)
        self.assertEqual(first.person_id, person.pk)
        self.assertEqual(second.person_id, person.pk)
        self.assertCountEqual(
            person.author_identities.values_list("pk", flat=True),
            [first.pk, second.pk],
        )

    def test_creating_author_does_not_link_by_exact_name(self):
        person = Person.objects.create(name="Exact Name Token")
        author = Author.objects.create(name="Exact Name Token")
        self.assertIsNone(author.person_id)
        self.assertEqual(person.author_identities.count(), 0)

    def test_person_ids_not_names_determine_identity(self):
        first = Person.objects.create(name="Shared Display Name")
        second = Person.objects.create(name="Shared Display Name")
        author = Author.objects.create(name="Shared Display Name", person=second)
        self.assertEqual(author.person_id, second.pk)
        self.assertNotEqual(author.person_id, first.pk)
        self.assertEqual(first.author_identities.count(), 0)


class AuthorPersonLinkServiceTests(TestCase):
    def test_parse_empty_unlinks_and_rejects_names(self):
        self.assertIsNone(parse_author_person_id(""))
        self.assertIsNone(parse_author_person_id("   "))
        self.assertIsNone(parse_author_person_id(None))
        with self.assertRaises(Exception) as ctx:
            parse_author_person_id("Ada Lovelace")
        self.assertEqual(ctx.exception.message, AUTHOR_PERSON_ID_INVALID_ERROR)
        with self.assertRaises(Exception) as ctx:
            parse_author_person_id("0")
        self.assertEqual(ctx.exception.message, AUTHOR_PERSON_ID_INVALID_ERROR)

    def test_set_author_person_links_and_unlinks_by_id(self):
        person = Person.objects.create(name="Target Person")
        decoy = Person.objects.create(name="Target Person")
        author = Author.objects.create(name="Target Person")
        linked = set_author_person(author=author, person_id=decoy.pk)
        self.assertEqual(linked.person_id, decoy.pk)
        self.assertNotEqual(linked.person_id, person.pk)
        unlinked = set_author_person(author=author, person_id=None)
        self.assertIsNone(unlinked.person_id)

    def test_missing_person_and_author_fail_closed(self):
        author = Author.objects.create(name="Needs person")
        with self.assertRaises(Exception) as ctx:
            set_author_person(author=author, person_id=999999)
        self.assertEqual(ctx.exception.message, PERSON_NOT_FOUND_ERROR)
        author.refresh_from_db()
        self.assertIsNone(author.person_id)

        missing = Author(pk=999998, name="Missing")
        person = Person.objects.create(name="Existing")
        with self.assertRaises(Exception) as ctx:
            set_author_person(author=missing, person_id=person.pk)
        self.assertEqual(ctx.exception.message, AUTHOR_NOT_FOUND_ERROR)


class AuthorPersonLinkStaffEditTests(TestCase):
    def setUp(self):
        self.staff = _staff()
        self.client.force_login(self.staff)
        self.author = Author.objects.create(name="Bibliographic Ada")
        self.url = _edit_url(self.author)

    def test_staff_can_see_link_and_unlink_controls(self):
        person = Person.objects.create(name="Ada Lovelace")
        PersonAlias.objects.create(person=person, name="Ada L.")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קישור לרשומת אדם")
        self.assertContains(resp, "אין קישור לרשומת אדם.")
        self.assertContains(resp, 'id="author_person_id"')
        self.assertContains(resp, 'name="person_id"')
        self.assertContains(resp, 'value="update_person_link"')
        self.assertContains(resp, f'value="{person.id}"')
        self.assertContains(resp, f"מזהה {person.id}")
        self.assertContains(resp, "Ada Lovelace (Ada L.)")
        self.assertNotContains(resp, 'name="new_person_name"')

        link = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": str(person.id)},
            follow=True,
        )
        self.assertContains(link, AUTHOR_PERSON_LINK_UPDATED_MSG)
        self.author.refresh_from_db()
        self.assertEqual(self.author.person_id, person.pk)
        self.assertContains(link, "מקושר כרגע:")
        self.assertContains(
            link,
            reverse("archive-manage-person-edit", kwargs={"person_id": person.id}),
        )

        unlink = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": ""},
            follow=True,
        )
        self.assertContains(unlink, AUTHOR_PERSON_LINK_UPDATED_MSG)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.person_id)
        self.assertContains(unlink, "אין קישור לרשומת אדם.")

    def _link_existing_person(self) -> Person:
        person = Person.objects.create(name="Already Linked Person")
        Person.objects.create(name="Already Linked Person")
        self.author.person = person
        self.author.save(update_fields=["person", "updated_at"])
        return person

    def _option_attrs(self, html: str, value: str) -> str:
        match = re.search(rf'<option value="{re.escape(value)}"([^>]*)>', html)
        self.assertIsNotNone(match, f"missing option value={value!r}")
        return match.group(1)

    def test_get_linked_author_selects_person_option_by_id(self):
        person = self._link_existing_person()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(f'<option value="{person.id}" selected', html)
        self.assertIn("selected", self._option_attrs(html, str(person.id)))

    def test_post_currently_selected_person_id_is_noop(self):
        person = self._link_existing_person()
        linked_at = self.author.updated_at
        resp = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": str(person.id)},
        )
        self.assertEqual(resp.status_code, 302)
        self.author.refresh_from_db()
        self.assertEqual(self.author.person_id, person.pk)
        self.assertEqual(self.author.updated_at, linked_at)

    def test_post_blank_unlinks_linked_author(self):
        person = self._link_existing_person()
        resp = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": ""},
            follow=True,
        )
        self.assertContains(resp, AUTHOR_PERSON_LINK_UPDATED_MSG)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.person_id)
        self.assertIsNone(person.author_identities.filter(pk=self.author.pk).first())
        html = resp.content.decode()
        self.assertIn("selected", self._option_attrs(html, ""))
        self.assertNotIn("selected", self._option_attrs(html, str(person.id)))

    def test_blank_option_is_not_selected_while_person_is_linked(self):
        person = self._link_existing_person()
        resp = self.client.get(self.url)
        html = resp.content.decode()
        self.assertNotIn("selected", self._option_attrs(html, ""))
        self.assertIn("selected", self._option_attrs(html, str(person.id)))
        unlinked_html = self.client.get(
            _edit_url(Author.objects.create(name="Still unlinked"))
        ).content.decode()
        self.assertIn("selected", self._option_attrs(unlinked_html, ""))

    def test_person_id_not_name_selects_among_duplicate_names(self):
        first = Person.objects.create(name="Duplicate Person Name")
        second = Person.objects.create(name="Duplicate Person Name")
        resp = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": str(second.id)},
        )
        self.assertEqual(resp.status_code, 302)
        self.author.refresh_from_db()
        self.assertEqual(self.author.person_id, second.pk)
        self.assertNotEqual(self.author.person_id, first.pk)

        named = self.client.post(
            self.url,
            data={
                "action": "update_person_link",
                "person_id": "Duplicate Person Name",
            },
        )
        self.assertEqual(named.status_code, 200)
        self.assertContains(named, AUTHOR_PERSON_ID_INVALID_ERROR)
        self.author.refresh_from_db()
        self.assertEqual(self.author.person_id, second.pk)

    def test_rename_does_not_create_or_infer_person_link(self):
        person = Person.objects.create(name="Renamed Bibliographic")
        before_people = Person.objects.count()
        resp = self.client.post(self.url, data={"name": "Renamed Bibliographic"})
        self.assertEqual(resp.status_code, 302)
        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "Renamed Bibliographic")
        self.assertIsNone(self.author.person_id)
        self.assertEqual(Person.objects.count(), before_people)
        self.assertEqual(person.author_identities.count(), 0)

        follow = self.client.get(self.url)
        self.assertContains(follow, AUTHOR_NAME_UPDATED_MSG)

    def test_link_does_not_create_person_rows(self):
        before = Person.objects.count()
        missing = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": "999999"},
        )
        self.assertEqual(missing.status_code, 200)
        self.assertContains(missing, PERSON_NOT_FOUND_ERROR)
        self.assertEqual(Person.objects.count(), before)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.person_id)

    def test_staff_index_does_not_gain_person_link_controls(self):
        index = self.client.get(reverse("archive-manage-authors"))
        self.assertEqual(index.status_code, 200)
        self.assertNotContains(index, "קישור לרשומת אדם")
        self.assertNotContains(index, 'id="author_person_id"')

    def test_anonymous_and_non_staff_cannot_link(self):
        person = Person.objects.create(name="Protected Person")
        self.client.logout()
        anon = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": str(person.id)},
        )
        self.assertEqual(anon.status_code, 302)
        self.assertTrue(anon["Location"].startswith("/accounts/login/"))

        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(
            username="author_person_family",
            password="test-pass",
        )
        family.groups.add(family_group)
        self.client.force_login(family)
        forbidden = self.client.post(
            self.url,
            data={"action": "update_person_link", "person_id": str(person.id)},
        )
        self.assertEqual(forbidden.status_code, 403)
        self.author.refresh_from_db()
        self.assertIsNone(self.author.person_id)


class AuthorPersonLinkPublicIsolationTests(TestCase):
    def test_public_catalog_and_detail_do_not_use_the_link(self):
        person = Person.objects.create(
            name="UniquePersonTokenForAuthorLink",
            biography="Person biography must stay off author pages.",
        )
        author = Author.objects.create(name="UniqueAuthorTokenForPersonLink")
        author.person = person
        author.save(update_fields=["person"])
        item = create_manual_text_archive_item(
            title="Public authored item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[author.id],
        )

        authors_index = self.client.get(reverse("archive-authors-index"))
        author_detail = self.client.get(
            reverse("archive-author-detail", kwargs={"author_id": author.id})
        )
        people_index = self.client.get(reverse("archive-people-index"))
        person_detail = self.client.get(
            reverse("archive-person-detail", kwargs={"person_id": person.id})
        )
        archive_list = self.client.get(reverse("archive-list"))
        item_detail = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        )

        self.assertEqual(authors_index.status_code, 200)
        self.assertEqual(author_detail.status_code, 200)
        self.assertEqual(people_index.status_code, 200)
        self.assertEqual(person_detail.status_code, 404)
        self.assertEqual(archive_list.status_code, 200)
        self.assertEqual(item_detail.status_code, 200)

        for resp in (
            authors_index,
            author_detail,
            people_index,
            archive_list,
            item_detail,
        ):
            html = resp.content.decode()
            self.assertNotIn("קישור לרשומת אדם", html)
            self.assertNotIn("עדכון קישור לאדם", html)
            self.assertNotIn('id="author_person_id"', html)
            self.assertNotIn(
                reverse(
                    "archive-manage-person-edit", kwargs={"person_id": person.id}
                ),
                html,
            )
            self.assertNotIn("UniquePersonTokenForAuthorLink", html)
            self.assertNotIn("Person biography must stay off author pages.", html)

        self.assertContains(authors_index, "UniqueAuthorTokenForPersonLink")
        self.assertContains(author_detail, "UniqueAuthorTokenForPersonLink")
        self.assertNotContains(author_detail, reverse("archive-people-index"))
        self.assertNotContains(people_index, reverse("archive-authors-index"))


class AuthorPersonLinkMigrationContractTests(TestCase):
    def test_schema_migration_adds_nullable_fk_without_data_backfill(self):
        migration_module = import_module(f"documents.migrations.{SCHEMA_MIGRATION}")
        Migration = migration_module.Migration
        self.assertEqual(
            Migration.dependencies, [("documents", "0062_reviewed_person_import_binding")]
        )
        self.assertEqual(len(Migration.operations), 1)
        operation = Migration.operations[0]
        self.assertEqual(operation.__class__.__name__, "AddField")
        self.assertEqual(operation.model_name, "author")
        self.assertEqual(operation.name, "person")
        self.assertTrue(operation.field.null)
        self.assertTrue(operation.field.blank)
        self.assertEqual(operation.field.remote_field.related_name, "author_identities")
        self.assertFalse(operation.field.unique)
        for op in Migration.operations:
            self.assertNotEqual(op.__class__.__name__, "RunPython")
