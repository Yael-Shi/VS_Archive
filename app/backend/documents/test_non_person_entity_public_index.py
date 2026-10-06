"""Public non-person registry index and name lookup."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemSearchIndex,
    NonPersonEntity,
    NonPersonEntityAlias,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
)
from documents.services.archive_items import create_manual_text_archive_item

INDEX = "archive-non-person-index"
DETAIL = "archive-non-person-detail"


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "קהיר",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _alias(entity: NonPersonEntity, name: str, kind: str) -> NonPersonEntityAlias:
    return NonPersonEntityAlias.objects.create(entity=entity, name=name, kind=kind)


def _index(client, **params):
    return client.get(reverse(INDEX), params)


class RegistryIndexTests(TestCase):
    def test_index_lists_rows_without_occurrences_and_links_to_detail(self):
        shown = _entity(canonical_name="אלכסנדריה", display_name="אלכסנדריה")
        preferred = _entity(canonical_name="القاهرة", display_name="קהיר")
        plain = _entity(canonical_name="חיפה", display_name="   ", entity_subtype="")

        resp = _index(self.client)
        html = resp.content.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מקומות, ארגונים, קהילות ופרסומים")
        self.assertContains(resp, f"/archive/entities/{shown.id}/")
        self.assertContains(resp, f"/archive/entities/{plain.id}/")
        self.assertLess(html.index("אלכסנדריה"), html.index("חיפה"))
        self.assertContains(resp, "קהיר")
        self.assertNotContains(resp, "القاهرة")
        self.assertContains(resp, "חיפה")
        self.assertContains(resp, "מקום · עיר")
        self.assertContains(resp, "מקום")
        self.assertNotContains(resp, "PLACE")
        self.assertNotContains(resp, "CITY")
        self.assertNotContains(resp, "אחר")
        self.assertNotContains(resp, "Entity")
        self.assertNotContains(resp, "Entities")
        self.assertNotContains(resp, "ישות")
        self.assertNotContains(resp, "ישויות")
        self.assertEqual(preferred.archive_item_occurrences.count(), 0)

    def test_detail_page_still_renders(self):
        entity = _entity()
        resp = self.client.get(reverse(DETAIL, kwargs={"entity_id": entity.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קהיר")


class RegistrySearchTests(TestCase):
    def test_rank_order_and_single_row_per_entity(self):
        canonical = _entity(
            canonical_name="QueryToken",
            display_name="",
            entity_subtype="",
        )
        display = _entity(
            canonical_name="HiddenCanonical",
            display_name="QueryToken",
            entity_subtype="",
        )
        alias_entity = _entity(
            canonical_name="AliasOnlyPublic",
            display_name="AliasOnlyPublic",
            entity_subtype="",
        )
        _alias(
            alias_entity,
            "QueryToken",
            NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        _alias(
            alias_entity,
            "QueryToken-extra",
            NonPersonEntityAlias.Kind.SPELLING_VARIANT,
        )
        prefix_name = _entity(
            canonical_name="ZZZ-prefix",
            display_name="ZZZ-prefix",
            entity_subtype="",
        )
        prefix_name.display_name = "QueryToken-prefix"
        prefix_name.canonical_name = "QueryToken-prefix"
        prefix_name.save(update_fields=["display_name", "canonical_name"])
        prefix_alias = _entity(
            canonical_name="AAA-prefix-alias",
            display_name="AAA-prefix-alias",
            entity_subtype="",
        )
        _alias(
            prefix_alias,
            "QueryToken-alias",
            NonPersonEntityAlias.Kind.ABBREVIATION,
        )
        contains_name = _entity(
            canonical_name="AAA-contains",
            display_name="mid QueryToken end",
            entity_subtype="",
        )
        contains_alias = _entity(
            canonical_name="MMM-contains-alias",
            display_name="MMM-contains-alias",
            entity_subtype="",
        )
        _alias(
            contains_alias,
            "has QueryToken",
            NonPersonEntityAlias.Kind.CURRENT_NAME,
        )

        resp = _index(self.client, q="QueryToken")
        html = resp.content.decode()
        names = [row.name for row in resp.context["registry_rows"]]

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            names,
            [
                "QueryToken",
                "QueryToken",
                "AliasOnlyPublic",
                "QueryToken-prefix",
                "AAA-prefix-alias",
                "mid QueryToken end",
                "MMM-contains-alias",
            ],
        )
        self.assertLess(
            html.index(f"/archive/entities/{display.id}/"),
            html.index(f"/archive/entities/{canonical.id}/"),
        )
        self.assertEqual(html.count(f"/archive/entities/{alias_entity.id}/"), 1)
        alias_row = next(
            row
            for row in resp.context["registry_rows"]
            if row.entity_id == alias_entity.id
        )
        self.assertEqual(alias_row.matched_alias, "QueryToken")
        self.assertNotContains(resp, "QueryToken-extra")
        self.assertEqual(resp.context["registry_rows"][5].entity_id, contains_name.id)

    def test_shared_alias_returns_both_entities(self):
        first = _entity(canonical_name="ראשון", display_name="ראשון", entity_subtype="")
        second = _entity(canonical_name="שני", display_name="שני", entity_subtype="")
        _alias(first, "שם משותף", NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)
        _alias(second, "שם משותף", NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)

        resp = _index(self.client, q="שם משותף")
        names = [row.name for row in resp.context["registry_rows"]]

        self.assertEqual(names, ["ראשון", "שני"])
        self.assertContains(resp, "נמצא גם בשם שם משותף")

    def test_ocr_variant_is_searchable_and_not_shown(self):
        entity = _entity(canonical_name="קהיר המוכרת", display_name="קהיר המוכרת")
        _alias(entity, "OCRSECRET", NonPersonEntityAlias.Kind.OCR_VARIANT)
        _alias(entity, "שם גלוי", NonPersonEntityAlias.Kind.LANGUAGE_VARIANT)

        resp = _index(self.client, q="OCRSECRET")
        html = resp.content.decode().replace('value="OCRSECRET"', "")

        self.assertContains(resp, "קהיר המוכרת")
        self.assertNotIn("OCRSECRET", html)
        self.assertNotContains(resp, "נמצא גם בשם")
        self.assertNotContains(resp, "שם גלוי")

    def test_visible_alias_prefix_and_contains_and_case(self):
        prefix = _entity(
            canonical_name="קידומת", display_name="קידומת", entity_subtype=""
        )
        _alias(prefix, "PrefixAliasRest", NonPersonEntityAlias.Kind.SPELLING_VARIANT)
        contains = _entity(
            canonical_name="מכיל",
            display_name="מכיל",
            entity_subtype="",
        )
        _alias(contains, "zz ContainsAlias zz", NonPersonEntityAlias.Kind.ABBREVIATION)
        latin = _entity(canonical_name="Cairo", display_name="", entity_subtype="")

        prefix_resp = _index(self.client, q="PrefixAlias")
        contains_resp = _index(self.client, q="ContainsAlias")
        case_resp = _index(self.client, q="cAiRo")

        self.assertEqual(
            [row.name for row in prefix_resp.context["registry_rows"]],
            ["קידומת"],
        )
        self.assertContains(prefix_resp, "נמצא גם בשם PrefixAliasRest")
        self.assertEqual(
            [row.name for row in contains_resp.context["registry_rows"]],
            ["מכיל"],
        )
        self.assertContains(contains_resp, "נמצא גם בשם zz ContainsAlias zz")
        self.assertEqual(
            [row.name for row in case_resp.context["registry_rows"]],
            ["Cairo"],
        )
        self.assertEqual(case_resp.context["registry_rows"][0].matched_alias, "")
        self.assertEqual(latin.display_name, "")

    def test_name_prefix_and_contains_without_alias_note(self):
        prefix = _entity(
            canonical_name="אור-עיר",
            display_name="אור-עיר",
            entity_subtype="",
        )
        contains = _entity(
            canonical_name="עיר אור אחרת",
            display_name="עיר אור אחרת",
            entity_subtype="",
        )

        resp = _index(self.client, q="אור")
        names = [row.name for row in resp.context["registry_rows"]]

        self.assertEqual(names, ["אור-עיר", "עיר אור אחרת"])
        self.assertEqual(resp.context["registry_rows"][0].matched_alias, "")
        self.assertEqual(resp.context["registry_rows"][1].matched_alias, "")
        self.assertEqual(prefix.pk, resp.context["registry_rows"][0].entity_id)
        self.assertEqual(contains.pk, resp.context["registry_rows"][1].entity_id)

    def test_near_miss_has_empty_state_and_keeps_query(self):
        _entity(canonical_name="קהיר", display_name="קהיר")

        resp = _index(self.client, q="קהר")

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "לא נמצאו תוצאות תואמות.")
        self.assertContains(resp, 'value="קהר"')
        self.assertNotContains(resp, "קהיר")

    def test_archive_free_text_search_ignores_registry_names(self):
        token = "UniqueRegistryOnlyToken"
        title = "UniqueManualTitleToken"
        item = create_manual_text_archive_item(
            title=title,
            body="גוף רגיל",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        before = ArchiveItemSearchIndex.objects.count()
        _entity(canonical_name=token, display_name=token, entity_subtype="")
        _alias(
            NonPersonEntity.objects.get(canonical_name=token),
            "UniqueRegistryAliasToken",
            NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), before)

        archive_hit = self.client.get(reverse("archive-list"), {"q": title})
        archive_miss = self.client.get(reverse("archive-list"), {"q": token})
        index = item.search_index

        self.assertContains(archive_hit, title)
        self.assertContains(archive_miss, "נמצאו 0 תוצאות")
        self.assertNotContains(archive_miss, title)
        self.assertNotIn("UniqueRegistryAliasToken", index.metadata_text)
        self.assertNotIn(token, index.title_text)
        self.assertNotIn(token, index.metadata_text)
        self.assertNotIn(token, index.body_text)
        self.assertNotIn(token, index.hebrew_translation_text)


class RegistryPaginationTests(TestCase):
    def test_pages_use_public_page_size_and_preserve_query(self):
        total = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1
        for index in range(total):
            _entity(
                canonical_name=f"דפדוף {index:02d}",
                display_name=f"דפדוף {index:02d}",
                entity_subtype="",
            )

        page1 = _index(self.client, q="דפדוף")
        page2 = _index(self.client, q="דפדוף", page=2)

        self.assertEqual(page1.context["per_page"], 48)
        self.assertEqual(page1.context["total_count"], total)
        self.assertEqual(len(page1.context["registry_rows"]), 48)
        self.assertEqual(len(page2.context["registry_rows"]), 1)
        self.assertEqual(page2.context["q"], "דפדוף")
        self.assertIn("q=", page2.context["prev_href_suffix"])
        self.assertContains(page2, 'value="דפדוף"')
        self.assertGreater(page1.context["total_count"], 5)
