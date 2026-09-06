"""Public Authors index is a compatibility redirect to the People directory."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    Author,
    Person,
)
from documents.services.archive_items import create_manual_text_archive_item


def _index_url() -> str:
    return reverse("archive-authors-index")


def _people_index_url() -> str:
    return reverse("archive-people-index")


def _public_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _private_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Private body",
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


class AuthorsPublicIndexRedirectTests(TestCase):
    def test_named_route_redirects_to_people_index(self):
        self.assertEqual(_index_url(), "/archive/authors/")
        resp = self.client.get(_index_url())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], _people_index_url())

        people = self.client.get(_people_index_url())
        self.assertEqual(people.status_code, 200)
        self.assertContains(people, "אנשים")
        self.assertNotContains(people, "אין מחברים להצגה.")

    def test_preserves_q_and_drops_unrelated_parameters(self):
        resp = self.client.get(_index_url(), {"q": "foo", "page": "2", "advanced": "1"})
        self.assertEqual(resp.status_code, 302)
        parsed = urlparse(resp["Location"])
        self.assertEqual(parsed.path, _people_index_url())
        self.assertEqual(parse_qs(parsed.query), {"q": ["foo"]})

    def test_blank_q_does_not_add_query_string(self):
        resp = self.client.get(_index_url(), {"q": "  "})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], _people_index_url())

    def test_author_only_row_is_discoverable_on_people_index(self):
        author = Author.objects.create(name="Redirected Author Only")
        ArchiveItemAuthor.objects.create(
            archive_item=_public_manual("Redirect letter"),
            author=author,
            position=0,
        )
        resp = self.client.get(_index_url(), {"q": "Redirected Author Only"})
        self.assertEqual(resp.status_code, 302)
        parsed = urlparse(resp["Location"])
        self.assertEqual(parsed.path, _people_index_url())
        self.assertEqual(parse_qs(parsed.query), {"q": ["Redirected Author Only"]})
        people = self.client.get(_people_index_url(), {"q": "Redirected Author Only"})
        self.assertEqual(people.status_code, 200)
        self.assertEqual(
            [row.href for row in people.context["people_rows"]],
            [reverse("archive-author-detail", kwargs={"author_id": author.id})],
        )

    def test_forwarded_q_does_not_leak_private_linked_author_name(self):
        person = Person.objects.create(name="Redirect Visible Person")
        ArchiveItemPerson.objects.create(
            archive_item=_public_manual("Public AIP letter"),
            person=person,
        )
        author = Author.objects.create(
            name="RedirectPrivateLinkedAuthorToken", person=person
        )
        ArchiveItemAuthor.objects.create(
            archive_item=_private_manual("Private authored letter"),
            author=author,
            position=0,
        )

        resp = self.client.get(_index_url(), {"q": "RedirectPrivateLinkedAuthorToken"})
        self.assertEqual(resp.status_code, 302)
        parsed = urlparse(resp["Location"])
        self.assertEqual(parsed.path, _people_index_url())
        self.assertEqual(
            parse_qs(parsed.query), {"q": ["RedirectPrivateLinkedAuthorToken"]}
        )
        people = self.client.get(
            _people_index_url(), {"q": "RedirectPrivateLinkedAuthorToken"}
        )
        self.assertEqual(people.status_code, 200)
        self.assertEqual([row.href for row in people.context["people_rows"]], [])
        self.assertNotIn(
            reverse("archive-person-detail", kwargs={"person_id": person.id}),
            people.content.decode("utf-8"),
        )
        self.assertNotIn("Redirect Visible Person", people.content.decode("utf-8"))


class AuthorsPublicIndexLayoutTests(TestCase):
    def test_css_still_defines_authors_grid(self):
        css = (
            Path(__file__).resolve().parents[1] / "public/static/public/app.css"
        ).read_text(encoding="utf-8")
        self.assertIn(".archive-authors-index-list", css)
        self.assertIn(
            ".archive-authors-index-list {\n    grid-template-columns: repeat(2, minmax(0, 1fr));",
            css,
        )
