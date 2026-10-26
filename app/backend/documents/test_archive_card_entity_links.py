"""Compact public archive cards: people limit and non-person links."""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    NonPersonEntity,
    NonPersonEntityCorrection,
    Person,
    PhotoContent,
    PhotoPerson,
    ReviewedNonPersonEntityDecision,
    Tag,
)
from documents.services.archive_item_presentation import (
    PUBLIC_CARD_NAME_PREVIEW_LIMIT,
    build_archive_browse_card,
    build_archive_browse_cards,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_correction import (
    public_entity_id_after_retirement,
)
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.non_person_entity_presentation import (
    non_person_public_page_url,
    non_person_public_type_label,
)
from documents.services.photo_gallery import build_photo_person_appearance_cards
from documents.views import _archive_browse_select_related

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED


def _manual(title: str, *, visibility: str | None = None) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="גוף ציבורי",
        visibility=visibility or ArchiveItem.Visibility.PUBLIC,
    )


def _entity(name: str, *, display_name: str = "") -> NonPersonEntity:
    return NonPersonEntity.objects.create(
        canonical_name=name,
        display_name=display_name,
        entity_type=NonPersonEntity.EntityType.PLACE,
        entity_subtype=NonPersonEntity.EntitySubtype.CITY,
    )


def _pin(
    item: ArchiveItem,
    entity: NonPersonEntity,
    *,
    ordinal: int,
    sha: str | None = None,
) -> None:
    body = item.manual_text_content.body
    ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=MANUAL,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version="surface-v1",
        normalized_surface=f"{entity.canonical_name}-{ordinal}",
        occurrence_ordinal=ordinal,
        resolution_status=RESOLVED,
        entity=entity,
        matched_text=entity.canonical_name,
    )


def _person(name: str, *, honorific: str = "") -> Person:
    return Person.objects.create(name=name, honorific=honorific)


def _link_person(item: ArchiveItem, person: Person) -> None:
    ArchiveItemPerson.objects.create(archive_item=item, person=person)


def _loaded(item: ArchiveItem) -> ArchiveItem:
    return _archive_browse_select_related(ArchiveItem.objects.filter(pk=item.pk)).get()


class ArchiveCardEntityLinkTests(TestCase):
    def test_preview_limit_is_three(self):
        self.assertEqual(PUBLIC_CARD_NAME_PREVIEW_LIMIT, 3)

    def test_card_keeps_sections_and_limits_people_and_entities(self):
        item = _manual("Card sections")
        category = ArchiveCategory.objects.create(name="מצרים", slug="card-egypt")
        event = ArchiveEvent.objects.create(name="חופשה", slug="card-holiday")
        tag = Tag.objects.create(name="תגית א")
        item.categories.add(category)
        item.events.add(event)
        item.tags.add(tag)
        people = [
            _person("תמר"),
            _person("אבא", honorific="הרב"),
            _person("גל"),
            _person("ברוך"),
        ]
        for person in people:
            _link_person(item, person)
        entities = [_entity(name) for name in ("חיפה", "אילת", "גדרה", "באר", "דלת")]
        for ordinal, entity in enumerate(entities, start=1):
            _pin(item, entity, ordinal=ordinal)

        card = build_archive_browse_card(_loaded(item))

        self.assertEqual([link.name for link in card.category_links], ["מצרים"])
        self.assertEqual(
            [link.name for link in card.person_links],
            ["אבא, הרב", "ברוך", "גל"],
        )
        self.assertEqual(card.person_more_count, 1)
        self.assertEqual(
            [link.name for link in card.related_links[:2]],
            ["חופשה", "תגית א"],
        )
        self.assertEqual(
            [link.href for link in card.related_links[:2]],
            [
                reverse("archive-event-browse", kwargs={"event_id": event.id}),
                reverse("archive-tag-browse", kwargs={"tag_id": tag.id}),
            ],
        )
        self.assertEqual(
            [link.name for link in card.related_links[2:]],
            ["אילת", "באר", "גדרה"],
        )
        self.assertEqual(card.entity_more_count, 2)
        self.assertNotIn(
            non_person_public_type_label(entities[0]),
            [link.name for link in card.related_links],
        )

        response = self.client.get(reverse("archive-list"))
        html = response.content.decode()
        self.assertContains(response, "קטגוריה:")
        self.assertContains(response, "קשור ל־")
        self.assertContains(response, "אנשים קשורים")
        self.assertNotContains(response, "אירועים:")
        self.assertNotContains(response, "תגיות:")
        self.assertNotContains(response, "ראו עוד")
        self.assertContains(response, "ועוד 1")
        self.assertContains(response, "ועוד 2")
        self.assertIn(non_person_public_page_url(entities[1].pk), html)
        self.assertNotIn(non_person_public_page_url(entities[0].pk), html)
        self.assertNotIn("מקום · עיר", html)
        self.assertNotIn(f"/archive/people/{people[0].id}/", html)

    def test_three_names_have_no_more_indicator(self):
        item = _manual("Exactly three")
        for name in ("אבא", "ברוך", "גל"):
            _link_person(item, _person(name))
        entity = _entity("אילת")
        _pin(item, entity, ordinal=1)

        card = build_archive_browse_card(item)

        self.assertEqual(card.person_more_count, 0)
        self.assertEqual(card.entity_more_count, 0)
        self.assertEqual(len(card.person_links), 3)
        self.assertEqual(card.related_links[0].name, "אילת")

    def test_duplicate_pins_and_same_display_name_stay_distinct_by_id(self):
        item = _manual("Dedup")
        shared = "שם זהה"
        first = _entity("קאנוני א", display_name=shared)
        second = _entity("קאנוני ב", display_name=shared)
        _pin(item, first, ordinal=1)
        _pin(item, first, ordinal=2)
        _pin(item, second, ordinal=1)

        card = build_archive_browse_card(item)
        hrefs = [link.href for link in card.related_links]

        self.assertEqual(
            hrefs,
            [
                non_person_public_page_url(first.pk),
                non_person_public_page_url(second.pk),
            ],
        )
        self.assertEqual([link.name for link in card.related_links], [shared, shared])

    def test_stale_and_private_entities_stay_off_the_public_list(self):
        public = _manual("Public pin")
        shown = _entity("חיפה")
        stale = _entity("שם מיושן")
        _pin(public, shown, ordinal=1)
        _pin(public, stale, ordinal=1, sha="0" * 64)
        private = _manual("Private pin", visibility=ArchiveItem.Visibility.PRIVATE)
        hidden = _entity("שם פרטי")
        _pin(private, hidden, ordinal=1)

        response = self.client.get(reverse("archive-list"))

        self.assertContains(response, "חיפה")
        self.assertContains(response, non_person_public_page_url(shown.pk))
        self.assertNotContains(response, "שם מיושן")
        self.assertNotContains(response, "שם פרטי")
        self.assertNotContains(response, non_person_public_page_url(hidden.pk))

    def test_shared_display_name_does_not_merge_tag_and_entity(self):
        item = _manual("Name collision")
        tag = Tag.objects.create(name="חיפה")
        item.tags.add(tag)
        entity = _entity("קאנוני", display_name="חיפה")
        _pin(item, entity, ordinal=1)

        card = build_archive_browse_card(item)

        self.assertEqual(
            [(link.name, link.href) for link in card.related_links],
            [
                ("חיפה", reverse("archive-tag-browse", kwargs={"tag_id": tag.id})),
                ("חיפה", non_person_public_page_url(entity.pk)),
            ],
        )

    def test_retired_pin_is_shown_as_its_successor(self):
        item = _manual("Retired pin")
        retired = _entity("שם ישן")
        survivor = _entity("שם נוכחי")
        decision = ReviewedNonPersonEntityDecision.objects.create(
            source="card-test",
            candidate_id="CARD-RETIRED-1",
            workbook_sha256="a" * 64,
            decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
            review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
            result_entity=retired,
        )
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=survivor,
            retired_entity=retired,
            canonical_name=survivor.canonical_name,
            display_name="",
            entity_subtype="",
            approval_reference="card-test",
            manifest_sha256="b" * 64,
        )
        _pin(item, retired, ordinal=1)
        _pin(item, survivor, ordinal=1)

        card = build_archive_browse_card(item)

        self.assertEqual(
            [link.href for link in card.related_links],
            [non_person_public_page_url(survivor.pk)],
        )
        self.assertEqual(card.related_links[0].name, "שם נוכחי")
        self.assertNotIn("שם ישן", [link.name for link in card.related_links])

    def test_detail_page_still_lists_every_valid_entity(self):
        item = _manual("Detail unchanged")
        entities = [_entity(name) for name in ("דלת", "גל", "באר", "אבא")]
        for ordinal, entity in enumerate(entities, start=1):
            _pin(item, entity, ordinal=ordinal)

        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        card = build_archive_browse_card(item)

        self.assertContains(detail, "מקומות, ארגונים ועוד:")
        self.assertNotContains(detail, "archive-browse-card__more")
        self.assertNotContains(detail, "ראו עוד (")
        for entity in entities:
            self.assertContains(detail, entity.canonical_name)
        self.assertEqual(card.entity_more_count, 1)
        self.assertEqual(len(card.related_links), 3)

    def test_homepage_uses_the_same_card_limit(self):
        item = _manual("Homepage card")
        for name in ("אבא", "ברוך", "גל", "דלת"):
            _link_person(item, _person(name))
        entity = _entity("אילת")
        _pin(item, entity, ordinal=1)

        response = self.client.get(reverse("public-home"))

        self.assertContains(response, "אנשים קשורים")
        self.assertContains(response, "ועוד 1")
        self.assertContains(response, non_person_public_page_url(entity.pk))
        self.assertContains(response, "אבא")
        self.assertNotContains(response, "ראו עוד (")
        self.assertNotContains(response, "expandable-name-list")

    def test_occurrence_queries_stay_flat_as_cards_increase(self):
        def make_items(count: int) -> list[ArchiveItem]:
            items = []
            for index in range(count):
                item = _manual(f"Batch {index}")
                for ordinal, name in enumerate(("אילת", "באר"), start=1):
                    _pin(item, _entity(f"{name}-{index}-{ordinal}"), ordinal=ordinal)
                items.append(item)
            return items

        one = list(
            _archive_browse_select_related(
                ArchiveItem.objects.filter(pk__in=[item.pk for item in make_items(1)])
            )
        )
        many = list(
            _archive_browse_select_related(
                ArchiveItem.objects.filter(pk__in=[item.pk for item in make_items(3)])
            )
        )
        with CaptureQueriesContext(connection) as small:
            build_archive_browse_cards(one)
        with CaptureQueriesContext(connection) as large:
            build_archive_browse_cards(many)

        self.assertEqual(len(small.captured_queries), len(large.captured_queries))
        occurrence_queries = [
            query
            for query in large.captured_queries
            if "archiveitementityoccurrence" in query["sql"].lower()
        ]
        self.assertEqual(len(occurrence_queries), 1)
        correction_queries = [
            query
            for query in large.captured_queries
            if "nonpersonentitycorrection" in query["sql"].lower()
        ]
        self.assertEqual(len(correction_queries), 1)

    def test_retirement_follow_uses_the_shared_redirect_rule(self):
        active = 4
        hops = {
            1: (2,),
            2: (3,),
            5: (6,),
            6: (5,),
            7: (8, 9),
        }

        self.assertEqual(public_entity_id_after_retirement(1, hops), 3)
        self.assertEqual(public_entity_id_after_retirement(active, hops), active)
        self.assertEqual(public_entity_id_after_retirement(7, hops), 7)
        self.assertIsNone(public_entity_id_after_retirement(5, hops))

    def test_retired_successor_chain_and_repeated_pins_collapse_to_one_link(self):
        item = _manual("Retirement chain")
        first = _entity("שלב א")
        second = _entity("שלב ב")
        current = _entity("שלב ג")
        _retire(first, second, "CARD-CHAIN-1")
        _retire(second, current, "CARD-CHAIN-2")
        _pin(item, first, ordinal=1)
        _pin(item, second, ordinal=1)
        _pin(item, current, ordinal=1)

        card = build_archive_browse_card(item)

        self.assertEqual(
            [link.href for link in card.related_links],
            [non_person_public_page_url(current.pk)],
        )
        self.assertEqual(card.related_links[0].name, "שלב ג")
        self.assertNotIn("שלב א", [link.name for link in card.related_links])
        self.assertNotIn("שלב ב", [link.name for link in card.related_links])

    def test_public_routes_do_not_repeat_entity_lookups_per_card(self):
        person = _person("אדם כרטיס")
        category = ArchiveCategory.objects.create(name="מסלול", slug="card-route")
        shared = _entity("יעד משותף")
        first = _manual("Route 1")
        _link_person(first, person)
        first.categories.add(category)
        _pin(first, shared, ordinal=1)
        urls = {
            "list": reverse("archive-list") + "?item_type=documents_and_texts",
            "category": reverse(
                "archive-category-browse",
                kwargs={"category_id": category.id},
            ),
            "person": reverse("archive-person-detail", kwargs={"person_id": person.id}),
            "entity": reverse(
                "archive-non-person-detail",
                kwargs={"entity_id": shared.id},
            ),
            "home": reverse("public-home"),
        }
        before = {
            name: _route_lookup_counts(self.client, url) for name, url in urls.items()
        }
        for index in (2, 3):
            item = _manual(f"Route {index}")
            _link_person(item, person)
            item.categories.add(category)
            _pin(item, shared, ordinal=1)
            _pin(item, _entity(f"נוסף {index}"), ordinal=2)
        for name, url in urls.items():
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200, name)
            self.assertEqual(
                before[name], _lookup_counts(captured.captured_queries), name
            )
            html = response.content.decode()
            self.assertIn("Route 1", html, name)
            self.assertIn("Route 3", html, name)
            self.assertNotIn("ראו עוד (", html, name)

    def test_photo_appearance_cards_batch_entity_lookups(self):
        person = _person("מופיע")
        appearances = []
        for index in range(3):
            item = ArchiveItem.objects.create(
                item_type=ArchiveItem.ItemType.PHOTO,
                title=f"Photo route {index}",
                visibility=ArchiveItem.Visibility.PUBLIC,
            )
            photo = PhotoContent.objects.create(
                archive_item=item,
                position=1,
                original_file_key=f"photos/{index}/original.jpg",
                original_filename="photo.jpg",
                original_mime_type="image/jpeg",
                original_size_bytes=1024,
                upload_status=PhotoContent.UploadStatus.UPLOADED,
                upload_error="",
            )
            appearances.append(
                PhotoPerson.objects.create(photo_content=photo, person=person)
            )
            _pin_without_manual(item)

        with CaptureQueriesContext(connection) as captured:
            cards = build_photo_person_appearance_cards(appearances, bucket="")

        self.assertEqual(len(cards), 3)
        self.assertEqual(_lookup_counts(captured.captured_queries), (1, 1))


def _lookup_counts(queries) -> tuple[int, int]:
    occurrence = 0
    correction = 0
    for query in queries:
        sql = query["sql"].lower()
        if "archiveitementityoccurrence" in sql:
            occurrence += 1
        if "nonpersonentitycorrection" in sql:
            correction += 1
    return occurrence, correction


def _route_lookup_counts(client, url: str) -> tuple[int, int]:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200
    return _lookup_counts(captured.captured_queries)


def _retire(
    retired: NonPersonEntity, survivor: NonPersonEntity, candidate_id: str
) -> None:
    decision = ReviewedNonPersonEntityDecision.objects.create(
        source="card-test",
        candidate_id=candidate_id,
        workbook_sha256="a" * 64,
        decision=ReviewedNonPersonEntityDecision.Decision.APPROVE,
        review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
        result_entity=retired,
    )
    NonPersonEntityCorrection.objects.create(
        decision=decision,
        effective_entity=survivor,
        retired_entity=retired,
        canonical_name=survivor.canonical_name,
        display_name="",
        entity_subtype="",
        approval_reference="card-test",
        manifest_sha256="b" * 64,
    )


def _pin_without_manual(item: ArchiveItem) -> None:
    entity = _entity(f"צילום {item.pk}")
    ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=MANUAL,
        source_text_sha256="0" * 64,
        normalization_version="surface-v1",
        normalized_surface=entity.canonical_name,
        occurrence_ordinal=1,
        resolution_status=RESOLVED,
        entity=entity,
        matched_text=entity.canonical_name,
    )
