"""Inline ראו עוד / הצג פחות on public archive item detail name lists."""

from __future__ import annotations

import re
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    NonPersonEntity,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_presentation import (
    PUBLIC_DETAIL_NAME_LIST_PREVIEW_LIMIT,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrences import source_text_sha256

PREVIEW = PUBLIC_DETAIL_NAME_LIST_PREVIEW_LIMIT
MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
PRESIGNED_URL = "https://s3.example/presigned/photo"


def _manual(title: str, *, visibility: str | None = None) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="גוף ציבורי",
        visibility=visibility or ArchiveItem.Visibility.PUBLIC,
    )


def _person(name: str) -> Person:
    return Person.objects.create(name=name)


def _link_person(item: ArchiveItem, person: Person) -> None:
    ArchiveItemPerson.objects.create(archive_item=item, person=person)


def _entity(name: str) -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.CITY,
    )


def _pin(
    item: ArchiveItem,
    entity: NonPersonEntity,
    body: str,
    *,
    ordinal: int,
    sha: str | None = None,
) -> None:
    ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=MANUAL,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version="surface-v1",
        normalized_surface=entity.canonical_name,
        occurrence_ordinal=ordinal,
        resolution_status=RESOLVED,
        entity=entity,
        matched_text=entity.canonical_name,
    )


def _photo(title: str) -> tuple[ArchiveItem, PhotoContent]:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=1,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        upload_error="",
    )
    photo.original_file_key = f"photos/{photo.id}/original.jpg"
    photo.save(update_fields=["original_file_key", "updated_at"])
    return item, photo


def _detail_html(client, item: ArchiveItem) -> str:
    response = client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
    assert response.status_code == 200
    return re.sub(r"\s+", " ", response.content.decode())


def _list_markup(html: str, list_key: str, item_id: int) -> str:
    marker = f'id="detail-name-list-{list_key}-{item_id}"'
    start = html.index(marker)
    next_list = html.find('class="expandable-name-list"', start)
    end = next_list if next_list != -1 else len(html)
    return html[start:end]


class ExpandableNameListTests(TestCase):
    def test_preview_limit_is_twelve(self):
        self.assertEqual(PREVIEW, 12)

    def test_twelve_names_have_no_expansion_control(self):
        item = _manual("Twelve names")
        people = [_person(f"אדם {index:02d}") for index in range(1, PREVIEW + 1)]
        entities = [_entity(f"מקום {index:02d}") for index in range(1, PREVIEW + 1)]
        for person in people:
            _link_person(item, person)
        body = item.manual_text_content.body
        for ordinal, entity in enumerate(entities, start=1):
            _pin(item, entity, body, ordinal=ordinal)

        html = _detail_html(self.client, item)

        self.assertNotIn("ראו עוד", html)
        self.assertNotIn("הצג פחות", html)
        self.assertNotIn("expandable-name-list__toggle", html)
        self.assertNotIn("expandable_name_list.js", html)
        self.assertNotIn("is-collapsed", html)
        self.assertEqual(html.count('class="expandable-name-list"'), 2)
        for person in people:
            self.assertIn(person.name, html)
        for entity in entities:
            self.assertIn(entity.canonical_name, html)
        people_markup = _list_markup(html, "people", item.id)
        self.assertLess(people_markup.index("אדם 01"), people_markup.index("אדם 12"))
        self.assertNotIn("expandable-name-list__extra", people_markup)

    def test_thirteen_names_share_one_component_for_people_and_entities(self):
        item = _manual("Thirteen names")
        people = [_person(f"אדם {index:02d}") for index in range(1, PREVIEW + 2)]
        entities = [_entity(f"מקום {index:02d}") for index in range(1, PREVIEW + 2)]
        for person in people:
            _link_person(item, person)
        body = item.manual_text_content.body
        for ordinal, entity in enumerate(entities, start=1):
            _pin(item, entity, body, ordinal=ordinal)

        html = _detail_html(self.client, item)

        self.assertEqual(html.count('class="expandable-name-list"'), 2)
        self.assertEqual(html.count("expandable_name_list.js"), 1)
        self.assertNotIn("is-collapsed", html)
        for list_key, last_name in (("people", "אדם 13"), ("entities", "מקום 13")):
            markup = _list_markup(html, list_key, item.id)
            list_id = f"detail-name-list-{list_key}-{item.id}"
            self.assertIn(f'id="{list_id}"', markup)
            self.assertIn(
                (
                    f'<button type="button" class="expandable-name-list__toggle" '
                    f'hidden aria-expanded="false" aria-controls="{list_id}" '
                    'data-name-list-toggle data-hidden-count="1">ראו עוד (1)</button>'
                ),
                markup,
            )
            self.assertNotIn(">הצג פחות<", markup)
            prefix = "אדם" if list_key == "people" else "מקום"
            self.assertLess(markup.index(f"{prefix} 01"), markup.index(f"{prefix} 13"))
            self.assertIn(
                (
                    '<span role="listitem" class="expandable-name-list__extra">'
                    '<span class="expandable-name-list__sep">, </span>'
                    "<a href="
                ),
                markup,
            )
            self.assertNotIn('aria-hidden="true">, </span>', markup)
            self.assertIn(f">{last_name}</a>", markup)
            visible_name = last_name.replace("13", "12")
            self.assertIn(f">{visible_name}</a></span>", markup)
            self.assertIsNone(
                re.search(
                    rf'class="expandable-name-list__extra">'
                    rf'<span class="expandable-name-list__sep">, </span>'
                    rf'<a href="[^"]+">{re.escape(visible_name)}</a>',
                    markup,
                )
            )

        for person in people:
            self.assertIn(f">{person.name}</a>", html)
        for entity in entities:
            self.assertIn(f">{entity.canonical_name}</a>", html)

    def test_hidden_count_ignores_stale_and_duplicate_entities(self):
        item = _manual("Validity and dedup")
        body = item.manual_text_content.body
        entities = [_entity(f"ארגון {index:02d}") for index in range(1, PREVIEW + 2)]
        for ordinal, entity in enumerate(entities, start=1):
            _pin(item, entity, body, ordinal=ordinal)
        _pin(item, entities[0], body, ordinal=99)
        stale = _entity("שם מיושן")
        _pin(item, stale, body, ordinal=1, sha="0" * 64)

        html = _detail_html(self.client, item)
        markup = _list_markup(html, "entities", item.id)

        self.assertIn('data-hidden-count="1"', markup)
        self.assertIn(">ראו עוד (1)<", markup)
        self.assertEqual(markup.count("ארגון 01"), 1)
        self.assertNotIn("שם מיושן", html)
        self.assertEqual(html.count('class="expandable-name-list"'), 1)

    def test_private_item_stays_hidden(self):
        item = _manual("Private names", visibility=ArchiveItem.Visibility.PRIVATE)
        for index in range(1, PREVIEW + 2):
            _link_person(item, _person(f"נסתר {index:02d}"))

        response = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        )

        self.assertEqual(response.status_code, 404)

    def test_archive_cards_do_not_expand(self):
        item = _manual("Card names stay plain")
        for index in range(1, PREVIEW + 2):
            _link_person(item, _person(f"כרטיס {index:02d}"))

        response = self.client.get(reverse("archive-list"))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("כרטיס 01", html)
        self.assertIn("כרטיס 13", html)
        self.assertNotIn("ראו עוד", html)
        self.assertNotIn("expandable-name-list", html)

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_photo_item_people_expand_and_identified_people_do_not(self, _presign):
        item, photo = _photo("Photo item people")
        for index in range(1, PREVIEW + 2):
            _link_person(item, _person(f"פריט {index:02d}"))
        identified = [_person(f"בתמונה {index}") for index in range(1, 4)]
        for person in identified:
            PhotoPerson.objects.create(photo_content=photo, person=person)

        html = _detail_html(self.client, item)
        people = _list_markup(html, "people", item.id)
        identified_at = html.index("מופיעים:")
        identified_ends = [
            html.index(marker, identified_at)
            for marker in ('class="photo-detail', "התמונה אינה זמינה")
            if marker in html[identified_at:]
        ]
        identified_block = html[identified_at : min(identified_ends)]

        self.assertIn("אנשים קשורים לפריט:", html)
        self.assertNotIn("אנשים קשורים:", html)
        self.assertIn(">ראו עוד (1)<", people)
        self.assertIn(">פריט 13</a>", people)
        self.assertLess(people.index("פריט 01"), people.index("פריט 13"))
        self.assertIn("בתמונה 1", identified_block)
        self.assertIn("בתמונה 3", identified_block)
        self.assertNotIn("ראו עוד", identified_block)
        self.assertNotIn("expandable-name-list", identified_block)
        self.assertNotIn("הצג פחות", identified_block)
