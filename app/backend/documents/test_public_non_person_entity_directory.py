"""Public non-person directory order, filter, counts, and navigation."""

from __future__ import annotations

from pathlib import Path

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    ReviewedNonPersonEntityDecision,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    build_archive_browse_card,
)
from documents.services.non_person_entity_presentation import (
    non_person_public_page_url,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrences import source_text_sha256

INDEX = "archive-non-person-index"
DETAIL = "archive-non-person-detail"
PEOPLE = "archive-people-index"
MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
UNRESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "קהיר",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": "",
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _manual(title: str, body: str, *, visibility: str | None = None) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=visibility or ArchiveItem.Visibility.PUBLIC,
    )


def _pin(
    item: ArchiveItem,
    entity: NonPersonEntity | None,
    body: str,
    *,
    ordinal: int = 1,
    sha: str | None = None,
    text_kind: str = MANUAL,
    status: str = RESOLVED,
    normalization_version: str = "surface-v1",
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version=normalization_version,
        normalized_surface="שם",
        occurrence_ordinal=ordinal,
        resolution_status=status,
        entity=entity,
        matched_text="שם",
    )


def _retire(retired: NonPersonEntity, survivor: NonPersonEntity, candidate_id: str) -> None:
    decision = ReviewedNonPersonEntityDecision.objects.create(
        source="directory-test",
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
        approval_reference="directory-test",
        manifest_sha256="b" * 64,
    )


def _alias(entity: NonPersonEntity, name: str) -> NonPersonEntityAlias:
    return NonPersonEntityAlias.objects.create(
        entity=entity,
        name=name,
        kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
    )


def _index(client, **params):
    return client.get(reverse(INDEX), params)


def _names(response) -> list[str]:
    return [row.name for row in response.context["registry_rows"]]


class DirectoryOrderAndAlphabetTests(TestCase):
    def test_blank_query_uses_hebrew_display_order_and_present_letter_jumps(self):
        first = _entity(canonical_name="חיפה", display_name="חיפה")
        second = _entity(canonical_name="חיפה ", display_name="חיפה")
        aleph = _entity(canonical_name="אלף", display_name="אלף")
        spaced = _entity(canonical_name="  אלף", display_name="  אלף")
        final_letter = _entity(canonical_name="ץד", display_name="ץד")
        punctuated = _entity(canonical_name="(קהיר", display_name="(קהיר")
        latin = _entity(canonical_name="Cairo", display_name="Cairo")
        later = _entity(canonical_name="תל אביב", display_name="תל אביב")

        resp = _index(self.client)
        html = resp.content.decode()
        nav = dict(resp.context["entity_alphabet_nav"])

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מקומות, ארגונים, קהילות ופרסומים")
        self.assertEqual(
            _names(resp),
            ["אלף", "אלף", "חיפה", "חיפה", "ץד", "(קהיר", "תל אביב", "Cairo"],
        )
        self.assertLess(first.pk, second.pk)
        self.assertEqual(
            [row.entity_id for row in resp.context["registry_rows"] if row.name == "חיפה"],
            [first.pk, second.pk],
        )
        self.assertLess(aleph.pk, spaced.pk)
        self.assertEqual(
            [row.entity_id for row in resp.context["registry_rows"] if row.name == "אלף"],
            [aleph.pk, spaced.pk],
        )
        aleph_groups = [
            group
            for group in resp.context["entity_letter_groups"]
            if group.letter == "א"
        ]
        self.assertEqual(len(aleph_groups), 1)
        self.assertEqual(aleph_groups[0].heading_id, "entity-letter-א")
        self.assertEqual(html.count('id="entity-letter-א"'), 1)
        self.assertEqual(nav["א"], "#entity-letter-א")
        self.assertEqual(nav["ק"], "#entity-letter-ק")
        self.assertEqual(nav["צ"], "#entity-letter-צ")
        self.assertEqual(nav["ת"], "#entity-letter-ת")
        self.assertEqual(nav["ב"], "")
        tsadi = html.index('id="entity-letter-צ"')
        qof = html.index('id="entity-letter-ק"')
        tav = html.index('id="entity-letter-ת"')
        self.assertLess(tsadi, qof)
        self.assertLess(qof, tav)
        self.assertIn("ץד", html[tsadi:qof])
        self.assertIn("(קהיר", html[qof:tav])
        self.assertIn("Cairo", html)
        self.assertEqual(final_letter.canonical_name, "ץד")
        self.assertEqual(punctuated.canonical_name, "(קהיר")
        self.assertEqual(latin.canonical_name, "Cairo")
        self.assertEqual(later.canonical_name, "תל אביב")

    def test_letter_jump_uses_the_filtered_result_and_keeps_the_type(self):
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE):
            _entity(
                canonical_name=f"אלף {index:02d}",
                display_name=f"אלף {index:02d}",
                entity_type=NonPersonEntity.EntityType.PLACE,
            )
        _entity(
            canonical_name="תל אביב",
            display_name="תל אביב",
            entity_type=NonPersonEntity.EntityType.PLACE,
        )
        _entity(
            canonical_name="ארגון באות אלף",
            display_name="ארגון באות אלף",
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
        )

        resp = _index(self.client, entity_type="PLACE")
        nav = dict(resp.context["entity_alphabet_nav"])

        self.assertEqual(resp.context["total_count"], ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1)
        self.assertEqual(nav["א"], "#entity-letter-א")
        self.assertIn("page=2", nav["ת"])
        self.assertIn("entity_type=PLACE", nav["ת"])
        self.assertTrue(nav["ת"].endswith("#entity-letter-ת"))
        self.assertNotIn("ארגון באות אלף", _names(resp))

    def test_search_keeps_match_rank_ahead_of_alphabetical_order(self):
        late = _entity(canonical_name="תצוגה", display_name="TokenExact")
        early = _entity(canonical_name="אלף מכיל TokenExact באמצע", display_name="")

        resp = _index(self.client, q="TokenExact")

        self.assertEqual(
            [row.entity_id for row in resp.context["registry_rows"]],
            [late.pk, early.pk],
        )
        self.assertEqual(resp.context["registry_rows"][0].matched_alias, "")
        self.assertEqual(resp.context["registry_rows"][1].matched_alias, "")

    def test_ranked_search_repeats_a_letter_with_unique_section_ids(self):
        exact = _entity(canonical_name="מוסתר", display_name="אלף")
        middle = _entity(canonical_name="בית", display_name="בית")
        _alias(middle, "אלף")
        later = _entity(
            canonical_name="אמצע אלף כאן",
            display_name="אמצע אלף כאן",
        )

        resp = _index(self.client, q="אלף")
        html = resp.content.decode()
        groups = resp.context["entity_letter_groups"]
        nav = dict(resp.context["entity_alphabet_nav"])
        section_ids = [
            match
            for match in html.split('id="')
            if match.startswith("entity-letter-")
        ]
        section_ids = [item.split('"', 1)[0] for item in section_ids]

        self.assertEqual(
            [row.entity_id for row in resp.context["registry_rows"]],
            [exact.pk, middle.pk, later.pk],
        )
        self.assertEqual([group.letter for group in groups], ["א", "ב", "א"])
        self.assertEqual([group.heading for group in groups], ["א", "ב", "א"])
        self.assertEqual(
            [group.heading_id for group in groups],
            ["entity-letter-א", "entity-letter-ב", "entity-letter-א-2"],
        )
        self.assertEqual(groups[0].rows[0].entity_id, exact.pk)
        self.assertEqual(groups[2].rows[0].entity_id, later.pk)
        self.assertEqual(len(section_ids), len(set(section_ids)))
        self.assertEqual(html.count('id="entity-letter-א"'), 1)
        self.assertEqual(nav["א"], "#entity-letter-א")
        self.assertEqual(nav["ב"], "#entity-letter-ב")
        self.assertIn('href="#entity-letter-א"', html)
        self.assertIn('id="entity-letter-א"', html)


class DirectoryFilterTests(TestCase):
    def test_type_filter_uses_existing_labels_and_ignores_invalid_values(self):
        place = _entity(canonical_name="מקום לסינון", display_name="מקום לסינון")
        org = _entity(
            canonical_name="ארגון לסינון",
            display_name="ארגון לסינון",
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
            entity_subtype=NonPersonEntity.EntitySubtype.SCHOOL,
        )
        before = NonPersonEntity.objects.count()

        filtered = _index(self.client, entity_type="ORGANIZATION")
        invalid = _index(self.client, entity_type="NOT_A_TYPE")
        subtype = _index(self.client, entity_type="PLACE", entity_subtype="SCHOOL")

        self.assertEqual([row.entity_id for row in filtered.context["registry_rows"]], [org.pk])
        self.assertContains(filtered, "ארגון")
        self.assertContains(filtered, 'value="ORGANIZATION" selected')
        self.assertEqual(invalid.status_code, 200)
        self.assertCountEqual(
            [row.entity_id for row in invalid.context["registry_rows"]],
            [place.pk, org.pk],
        )
        self.assertEqual(invalid.context["entity_type"], "")
        self.assertCountEqual(
            [row.entity_id for row in subtype.context["registry_rows"]],
            [place.pk],
        )
        self.assertEqual(NonPersonEntity.objects.count(), before)

    def test_search_type_and_page_stay_together(self):
        total = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1
        for index in range(total):
            _entity(
                canonical_name=f"סינון {index:02d}",
                display_name=f"סינון {index:02d}",
                entity_type=NonPersonEntity.EntityType.ORGANIZATION,
            )
        _entity(
            canonical_name="סינון מקום",
            display_name="סינון מקום",
            entity_type=NonPersonEntity.EntityType.PLACE,
        )

        page2 = _index(
            self.client,
            q="סינון",
            entity_type="ORGANIZATION",
            page=2,
        )

        self.assertEqual(page2.context["total_count"], total)
        self.assertEqual(len(page2.context["registry_rows"]), 1)
        self.assertIn("q=", page2.context["prev_href_suffix"])
        self.assertIn("entity_type=ORGANIZATION", page2.context["prev_href_suffix"])
        self.assertNotIn("page=2", page2.context["prev_href_suffix"])
        self.assertNotContains(page2, "סינון מקום")
        self.assertEqual(page2.context["registry_rows"][0].type_label, "ארגון")
        self.assertIn("entity_type=ORGANIZATION", page2.context["clear_search_href"])
        self.assertNotIn("q=", page2.context["clear_search_href"])


class DirectoryCountTests(TestCase):
    def test_zero_count_active_rows_stay_and_retired_rows_stay_out(self):
        active = _entity(canonical_name="בלי אזכור", display_name="בלי אזכור")
        survivor = _entity(canonical_name="יורש", display_name="יורש")
        retired = _entity(canonical_name="פרישה", display_name="פרישה")
        _retire(retired, survivor, "DIR-RETIRED")
        item = _manual("פריט של הפרישה", "פרישה בטקסט")
        _pin(item, retired, "פרישה בטקסט")

        resp = _index(self.client)
        detail = self.client.get(reverse(DETAIL, kwargs={"entity_id": retired.pk}))
        survivor_row = next(
            row for row in resp.context["registry_rows"] if row.entity_id == survivor.pk
        )
        active_row = next(
            row for row in resp.context["registry_rows"] if row.entity_id == active.pk
        )

        self.assertNotIn(retired.pk, [row.entity_id for row in resp.context["registry_rows"]])
        self.assertEqual(active_row.association_count, 0)
        self.assertEqual(active_row.association_summary, "0")
        self.assertEqual(survivor_row.association_count, 0)
        self.assertRedirects(
            detail,
            reverse(DETAIL, kwargs={"entity_id": survivor.pk}),
        )

    def test_successor_count_ignores_retired_pin_that_cards_follow(self):
        survivor = _entity(canonical_name="יורש פעיל", display_name="יורש פעיל")
        retired = _entity(canonical_name="קודם שפרש", display_name="קודם שפרש")
        _retire(retired, survivor, "DIR-DIRECT-PIN")
        predecessor_item = _manual("פריט על הקודם", "קודם שפרש בטקסט")
        _pin(predecessor_item, retired, "קודם שפרש בטקסט")
        successor_item = _manual("פריט על היורש", "יורש פעיל בטקסט")
        _pin(successor_item, survivor, "יורש פעיל בטקסט")

        resp = _index(self.client)
        successor_row = next(
            row for row in resp.context["registry_rows"] if row.entity_id == survivor.pk
        )
        detail = self.client.get(reverse(DETAIL, kwargs={"entity_id": survivor.pk}))
        card = build_archive_browse_card(predecessor_item)
        card_hrefs = [link.href for link in card.related_links]

        self.assertNotIn(
            retired.pk, [row.entity_id for row in resp.context["registry_rows"]]
        )
        self.assertEqual(successor_row.association_count, 1)
        self.assertEqual(detail.context["total_count"], 1)
        self.assertContains(detail, "פריט על היורש")
        self.assertNotContains(detail, "פריט על הקודם")
        self.assertIn(non_person_public_page_url(survivor.pk), card_hrefs)
        self.assertNotIn(non_person_public_page_url(retired.pk), card_hrefs)
        self.assertEqual(predecessor_item.entity_occurrences.get().entity_id, retired.pk)

    def test_counts_are_distinct_authorized_valid_items(self):
        entity = _entity(canonical_name="ספירה", display_name="ספירה")
        body = "ספירה בטקסט"
        counted = _manual("פריט נספר", body)
        _pin(counted, entity, body, ordinal=1)
        _pin(counted, entity, body, ordinal=2)
        private = _manual(
            "פריט פרטי",
            body,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _pin(private, entity, body, ordinal=1)
        restricted = _manual(
            "פריט מוגבל",
            body,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        _pin(restricted, entity, body, ordinal=1)
        stale = _manual("פריט ישן", body)
        _pin(stale, entity, body, sha="a" * 64, ordinal=1)
        unsupported = _manual("פריט לא נתמך", body)
        _pin(
            unsupported,
            entity,
            body,
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            ordinal=1,
        )
        unresolved_item = _manual("פריט לא מיושב", body)
        _pin(
            unresolved_item,
            None,
            body,
            status=UNRESOLVED,
            ordinal=1,
        )
        other = _entity(canonical_name="אחרת", display_name="אחרת")
        other_item = _manual("פריט של אחרת", "אחרת בטקסט")
        _pin(other_item, other, "אחרת בטקסט")

        resp = _index(self.client)
        row = next(row for row in resp.context["registry_rows"] if row.entity_id == entity.pk)

        self.assertEqual(row.association_count, 1)
        self.assertEqual(row.association_summary, "1 טקסט")
        self.assertNotContains(resp, "פריט פרטי")
        self.assertNotContains(resp, "פריט מוגבל")

    def test_page_counts_do_not_add_a_query_per_entity(self):
        one = self._query_profile(1)
        many = self._query_profile(4)

        self.assertEqual(one[0], many[0])
        self.assertEqual(one[1], 1)
        self.assertEqual(many[1], 1)

    def _query_profile(self, count: int) -> tuple[int, int]:
        for index in range(count):
            entity = _entity(
                canonical_name=f"שאילתה {count}-{index}",
                display_name=f"שאילתה {count}-{index}",
            )
            body = f"שאילתה {count}-{index}"
            item = _manual(f"כותרת {count}-{index}", body)
            _pin(item, entity, body)
        with CaptureQueriesContext(connection) as captured:
            response = _index(self.client)
        self.assertEqual(response.status_code, 200)
        occurrence_queries = sum(
            1
            for query in captured.captured_queries
            if "archiveitementityoccurrence" in query["sql"].lower()
        )
        return len(captured.captured_queries), occurrence_queries


class DirectoryNavigationTests(TestCase):
    def test_public_nav_links_the_directory_beside_people(self):
        resp = self.client.get(reverse("public-home"))
        html = resp.content.decode()
        people = html.index(">אנשים</a>")
        entities = html.index(">מקומות וארגונים</a>")
        about = html.index(">אודות</a>")

        self.assertLess(people, entities)
        self.assertLess(entities, about)
        self.assertIn(reverse(INDEX), html)
        self.assertIn(reverse(PEOPLE), html)

    def test_people_directory_does_not_gain_the_entity_filter(self):
        resp = self.client.get(reverse(PEOPLE))

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים")
        self.assertNotContains(resp, "archive-registry-entity-type")
        self.assertNotContains(resp, "entity-letter-")

    def test_template_reuses_the_responsive_people_directory_structure(self):
        _entity(canonical_name="מבנה", display_name="מבנה")
        resp = _index(self.client)
        html = resp.content.decode()
        css = (
            Path(__file__).resolve().parents[1] / "public/static/public/app.css"
        ).read_text(encoding="utf-8")

        self.assertIn("archive-people-index-columns", html)
        self.assertIn("archive-people-index-alphabet", html)
        self.assertIn("archive-entity-index-name", html)
        self.assertIn("מקום", html)
        self.assertIn(".archive-people-index-columns {\n  column-count: 1;", css)
        self.assertIn(".archive-entity-index-name {\n  flex-wrap: wrap;", css)
        self.assertIn(
            ".archive-people-index-columns {\n    column-count: 2;",
            css,
        )
