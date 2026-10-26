"""Public item section and non-person detail page."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    person_links_for_item,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.non_person_entity_presentation import (
    non_person_entity_subtype_label,
    non_person_public_type_label,
    public_mentioned_object_links,
    public_non_person_aliases,
)
from documents.test_archive_item import create_viewable_ocr_document

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
HEADING = "מקומות, ארגונים ועוד:"


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "קהיר",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
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
    entity: NonPersonEntity,
    body: str,
    *,
    text_kind: str = MANUAL,
    ordinal: int = 1,
    surface: str = "קהיר",
    matched_text: str = "קהיר",
    sha: str | None = None,
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version="surface-v1",
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        resolution_status=RESOLVED,
        entity=entity,
        matched_text=matched_text,
    )


def _detail(client, item: ArchiveItem):
    return client.get(reverse("archive-detail", kwargs={"item_id": item.id}))


class PublicSubtypeLabelTests(TestCase):
    def test_new_subtypes_have_distinct_public_labels(self):
        cases = (
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.MILITARY_ORGANIZATION,
                "ארגון · ארגון צבאי",
            ),
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.INTERNATIONAL_ORGANIZATION,
                "ארגון · ארגון בין-לאומי",
            ),
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.POLITICAL_PARTY_OR_MOVEMENT,
                "ארגון · מפלגה או תנועה פוליטית",
            ),
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.COMMITTEE_OR_INTERNAL_BODY,
                "ארגון · ועדה או גוף פנימי",
            ),
            (
                NonPersonEntity.EntityType.PLACE,
                NonPersonEntity.EntitySubtype.PORT,
                "מקום · נמל",
            ),
            (
                NonPersonEntity.EntityType.PLACE,
                NonPersonEntity.EntitySubtype.DIPLOMATIC_SITE,
                "מקום · אתר דיפלומטי",
            ),
            (
                NonPersonEntity.EntityType.EVENT,
                NonPersonEntity.EntitySubtype.TRIAL_OR_LEGAL_PROCEEDING,
                "אירוע · משפט או הליך משפטי",
            ),
            (
                NonPersonEntity.EntityType.EVENT,
                NonPersonEntity.EntitySubtype.POLITICAL_AFFAIR_OR_CRISIS,
                "אירוע · פרשה או משבר מדיני",
            ),
            (
                NonPersonEntity.EntityType.NAMED_GROUP,
                NonPersonEntity.EntitySubtype.HISTORICAL_COHORT,
                "קבוצה בשם · קבוצה היסטורית",
            ),
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.CULTURAL_ORGANIZATION,
                "ארגון · ארגון תרבות",
            ),
            (
                NonPersonEntity.EntityType.ORGANIZATION,
                NonPersonEntity.EntitySubtype.RELIGIOUS_INSTITUTION,
                "ארגון · מוסד דתי",
            ),
            (
                NonPersonEntity.EntityType.EVENT,
                NonPersonEntity.EntitySubtype.TREATY_OR_DIPLOMATIC_EVENT,
                "אירוע · אירוע דיפלומטי",
            ),
            (
                NonPersonEntity.EntityType.PLACE,
                NonPersonEntity.EntitySubtype.SETTLEMENT_OR_KIBBUTZ,
                "מקום · יישוב או קיבוץ",
            ),
        )
        for entity_type, subtype, label in cases:
            entity = NonPersonEntity(
                canonical_name=subtype,
                entity_type=entity_type,
                entity_subtype=subtype,
            )
            self.assertEqual(non_person_public_type_label(entity), label)

        city = NonPersonEntity(
            canonical_name="קהיר",
            entity_type=NonPersonEntity.EntityType.PLACE,
            entity_subtype=NonPersonEntity.EntitySubtype.CITY,
        )
        self.assertEqual(non_person_public_type_label(city), "מקום · עיר")

        blank_group = NonPersonEntity(
            canonical_name="הפורשים",
            entity_type=NonPersonEntity.EntityType.NAMED_GROUP,
            entity_subtype="",
        )
        self.assertEqual(non_person_public_type_label(blank_group), "קבוצה בשם")
        for subtype in NonPersonEntity.EntitySubtype.values:
            label = non_person_entity_subtype_label(subtype)
            self.assertTrue(label, subtype)
            self.assertNotIn("_", label)

    def test_port_subtype_is_shown_on_the_public_detail_page(self):
        entity = _entity(
            canonical_name="נמל חיפה",
            entity_subtype=NonPersonEntity.EntitySubtype.PORT,
        )

        resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )

        self.assertContains(resp, "נמל חיפה")
        self.assertContains(resp, "מקום · נמל")
        self.assertNotContains(resp, "PORT")


class MentionedObjectSectionTests(TestCase):
    def test_valid_occurrence_appears_as_one_natural_link(self):
        body = "ביקור בקהיר"
        item = _manual("valid pin", body)
        entity = _entity()
        _pin(item, entity, body, matched_text="SECRET_SLICE")

        resp = _detail(self.client, item)
        html = resp.content.decode()

        self.assertContains(resp, HEADING)
        self.assertContains(resp, "קהיר")
        self.assertNotContains(resp, "מקום · עיר")
        self.assertNotContains(resp, "archive-mentioned-objects")
        self.assertContains(resp, f'href="/archive/entities/{entity.id}/"')
        entity_page = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )
        self.assertContains(entity_page, "מקום · עיר")
        self.assertNotIn("SECRET_SLICE", html)
        self.assertNotIn(source_text_sha256(body), html)
        self.assertNotIn("ישות", html)
        self.assertNotIn("Entities", html)
        self.assertNotIn("PLACE", html)
        self.assertNotIn("CITY", html)

    def test_stale_occurrence_hides_the_section(self):
        body = "ביקור בקהיר"
        item = _manual("stale pin", body)
        entity = _entity(canonical_name="קהיר הישנה")
        _pin(item, entity, body, sha="a" * 64)

        resp = _detail(self.client, item)

        self.assertNotContains(resp, HEADING)
        self.assertNotContains(resp, "קהיר הישנה")
        self.assertEqual(item.entity_occurrences.count(), 1)

    def test_two_occurrences_of_one_entity_are_one_link(self):
        body = "קהיר ואחר כך קהיר"
        item = _manual("repeat", body)
        entity = _entity()
        _pin(item, entity, body, ordinal=1, surface="קהיר")
        _pin(item, entity, body, ordinal=2, surface="קהיר")

        resp = _detail(self.client, item)
        html = resp.content.decode()

        self.assertEqual(item.entity_occurrences.count(), 2)
        self.assertEqual(html.count(f"/archive/entities/{entity.id}/"), 1)

    def test_same_display_name_stays_two_links_in_id_order(self):
        body = "אלפא ואז ביתא"
        item = _manual("same label", body)
        first = _entity(canonical_name="קאנוני א", display_name="שם זהה", entity_subtype="")
        second = _entity(
            canonical_name="קאנוני ב", display_name="שם זהה", entity_subtype=""
        )
        _pin(item, first, body, ordinal=1, surface="אלפא", matched_text="אלפא")
        _pin(item, second, body, ordinal=1, surface="ביתא", matched_text="ביתא")

        resp = _detail(self.client, item)
        html = resp.content.decode()
        first_href = f"/archive/entities/{first.id}/"
        second_href = f"/archive/entities/{second.id}/"

        self.assertEqual(html.count(first_href), 1)
        self.assertEqual(html.count(second_href), 1)
        self.assertLess(html.index(first_href), html.index(second_href))
        self.assertIn(
            f'<a href="{first_href}">שם זהה</a>, <a href="{second_href}">שם זהה</a>',
            html,
        )

    def test_names_sort_alphabetically_not_by_occurrence(self):
        body = "aaaa aaaa bbbb"
        item = _manual("positions", body)
        later_in_text = _entity(canonical_name="אבן", entity_subtype="")
        earlier_in_text = _entity(canonical_name="תמר", entity_subtype="")
        _pin(
            item,
            later_in_text,
            body,
            ordinal=2,
            surface="aaaa",
            matched_text="aaaa",
        )
        _pin(
            item,
            earlier_in_text,
            body,
            ordinal=1,
            surface="bbbb",
            matched_text="bbbb",
        )

        names = [link.name for link in public_mentioned_object_links(item)]

        self.assertEqual(names, ["אבן", "תמר"])

    def test_manual_and_ocr_sources_sort_by_displayed_name(self):
        doc = create_viewable_ocr_document(
            title="both sources",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        item = doc.archive_item
        ocr_body = "ocrplace"
        manual_body = "zzzz manualplace"
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            text=ocr_body,
        )
        ManualTextContent.objects.create(archive_item=item, body=manual_body)
        manual_entity = _entity(canonical_name="תמר", entity_subtype="")
        ocr_entity = _entity(canonical_name="אבן", entity_subtype="")
        _pin(item, manual_entity, manual_body, text_kind=MANUAL, surface="manualplace")
        _pin(item, ocr_entity, ocr_body, text_kind=OCR, surface="ocrplace")

        with patch(
            "documents.services.non_person_entity_occurrences.item_supports_occurrence_text_kind",
            return_value=True,
        ):
            names = [link.name for link in public_mentioned_object_links(item)]

        self.assertEqual(names, ["אבן", "תמר"])

    def test_people_and_categories_still_render(self):
        body = "ביקור בקהיר"
        item = _manual("with people", body)
        entity = _entity()
        _pin(item, entity, body)
        person = Person.objects.create(name="אדם קשור")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        category = ArchiveCategory.objects.create(name="משפחה", slug="family")
        item.categories.add(category)

        resp = _detail(self.client, item)

        self.assertContains(resp, "אנשים קשורים:")
        self.assertNotContains(resp, "אנשים קשורים לפריט:")
        self.assertContains(resp, "אדם קשור")
        self.assertContains(resp, "קטגוריות:")
        self.assertContains(resp, "משפחה")
        self.assertContains(resp, HEADING)
        self.assertContains(resp, "קהיר")

    def test_people_sort_by_name_with_hebrew_before_latin(self):
        item = _manual("people order", "טקסט")
        Person.objects.create(name="Zebra")
        Person.objects.create(name="תמר", honorific="ד״ר")
        final_letter = Person.objects.create(name="ץפת")
        regular = Person.objects.create(name="צפת")
        Person.objects.create(name="(אבא)")
        for person in Person.objects.all():
            ArchiveItemPerson.objects.create(archive_item=item, person=person)

        self.assertEqual(
            [link.name for link in person_links_for_item(item, hebrew_display_order=True)],
            ["(אבא)", final_letter.name, regular.name, "תמר, ד״ר", "Zebra"],
        )
        self.assertLess(final_letter.id, regular.id)

    def test_long_dashes_change_display_only(self):
        stored = "הסוכנות\u2014היהודית"
        body = "אזכור"
        item = _manual("dash", body)
        entity = _entity(canonical_name=stored, display_name="", entity_subtype="")
        _pin(item, entity, body, surface="אזכור", matched_text="אזכור")
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="שם\u2013נוסף",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="בית־ספר",
            kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
        )

        resp = _detail(self.client, item)
        detail = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )
        entity.refresh_from_db()

        self.assertContains(resp, "הסוכנות-היהודית")
        self.assertNotContains(resp, stored)
        self.assertContains(detail, "הסוכנות-היהודית")
        self.assertContains(detail, "שם-נוסף")
        self.assertContains(detail, "בית־ספר")
        self.assertNotContains(detail, "שם\u2013נוסף")
        self.assertEqual(entity.canonical_name, stored)
        self.assertEqual(entity.aliases.get(kind="LANGUAGE_VARIANT").name, "שם\u2013נוסף")
        self.assertEqual(public_non_person_aliases(entity), ("בית־ספר", "שם-נוסף"))

    def test_aliases_that_differ_only_by_dash_show_once(self):
        entity = _entity(canonical_name="גוף", display_name="גוף", entity_subtype="")
        hyphen = NonPersonEntityAlias.objects.create(
            entity=entity,
            name="ועד-הצירים",
            kind=NonPersonEntityAlias.Kind.SPELLING_VARIANT,
        )
        en_dash = NonPersonEntityAlias.objects.create(
            entity=entity,
            name="ועד\u2013הצירים",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        kept = NonPersonEntityAlias.objects.create(
            entity=entity,
            name="שם אחר",
            kind=NonPersonEntityAlias.Kind.CURRENT_NAME,
        )
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="ועד\u2014הצירים",
            kind=NonPersonEntityAlias.Kind.OCR_VARIANT,
        )

        resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )
        html = resp.content.decode()

        self.assertEqual(public_non_person_aliases(entity), ("ועד-הצירים", "שם אחר"))
        self.assertEqual(html.count("ועד-הצירים"), 1)
        self.assertContains(resp, "שם אחר")
        self.assertNotContains(resp, "ועד\u2013הצירים")
        self.assertNotContains(resp, "ועד\u2014הצירים")
        self.assertEqual(entity.aliases.count(), 4)
        self.assertEqual(hyphen.name, "ועד-הצירים")
        self.assertEqual(en_dash.name, "ועד\u2013הצירים")
        self.assertEqual(kept.name, "שם אחר")

    def test_ocr_document_detail_shows_the_section(self):
        doc = create_viewable_ocr_document(
            title="ocr mention",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        body = "printed Cairo"
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            text=body,
        )
        entity = _entity(canonical_name="Cairo", display_name="קהיר")
        _pin(
            doc.archive_item,
            entity,
            body,
            text_kind=OCR,
            surface="cairo",
            matched_text="Cairo",
        )

        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, HEADING)
        self.assertContains(resp, "קהיר")
        self.assertContains(resp, f"/archive/entities/{entity.id}/")


class NonPersonDetailPageTests(TestCase):
    def test_existing_entity_renders_and_missing_id_is_404(self):
        entity = _entity()
        url = reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})

        resp = self.client.get(url)

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "קהיר")
        self.assertContains(resp, "מקום · עיר")
        missing = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id + 99})
        )
        self.assertEqual(missing.status_code, 404)

    def test_display_name_wins_and_blank_display_uses_canonical(self):
        named = _entity(canonical_name="القاهرة", display_name="קהיר")
        plain = _entity(
            canonical_name="אלכסנדריה",
            display_name="   ",
            entity_subtype="",
        )

        named_resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": named.id})
        )
        plain_resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": plain.id})
        )

        self.assertContains(named_resp, "קהיר")
        self.assertNotContains(named_resp, "القاهرة")
        self.assertContains(plain_resp, "אלכסנדריה")
        self.assertContains(plain_resp, "מקום")
        self.assertNotContains(plain_resp, "אחר")

    def test_public_aliases_omit_ocr_variant_and_empty_section(self):
        entity = _entity()
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="القاهرة",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="OCR_CAIRO",
            kind=NonPersonEntityAlias.Kind.OCR_VARIANT,
        )
        bare = _entity(canonical_name="חיפה", entity_subtype="")

        resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )
        bare_resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": bare.id})
        )

        self.assertContains(resp, "שמות נוספים")
        self.assertContains(resp, "القاهرة")
        self.assertNotContains(resp, "OCR_CAIRO")
        self.assertNotContains(bare_resp, "שמות נוספים")

    def test_valid_item_is_listed_once_and_stale_or_private_items_are_absent(self):
        body = "ביקור בקהיר"
        shown = _manual("פריט גלוי", body)
        repeated = _manual("פריט כפול בטקסט", "קהיר קהיר")
        stale = _manual("פריט מיושן", body)
        private = _manual(
            "SECRET-PRIVATE-TITLE",
            body,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        entity = _entity()
        _pin(shown, entity, body)
        _pin(repeated, entity, "קהיר קהיר", ordinal=1)
        _pin(repeated, entity, "קהיר קהיר", ordinal=2)
        _pin(stale, entity, body, sha="b" * 64)
        _pin(private, entity, body)

        resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )
        html = resp.content.decode()

        self.assertContains(resp, "פריט גלוי")
        self.assertContains(resp, "פריט כפול בטקסט")
        self.assertEqual(html.count("פריט כפול בטקסט"), 1)
        self.assertNotContains(resp, "פריט מיושן")
        self.assertNotContains(resp, "SECRET-PRIVATE-TITLE")
        self.assertNotIn("b" * 64, html)

    def test_entity_without_public_items_is_200_with_empty_state(self):
        entity = _entity(canonical_name="ללא פריטים", entity_subtype="")

        resp = self.client.get(
            reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        )

        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ללא פריטים")
        self.assertContains(resp, "אין כרגע פריטים שבהם זה מופיע בטקסט המוצג.")
        self.assertNotContains(resp, "נמצאו")

    def test_linked_items_paginate_at_the_public_page_size(self):
        entity = _entity(canonical_name="עמודים", entity_subtype="")
        total = ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1
        for index in range(total):
            body = f"עמודים {index}"
            item = _manual(f"פריט {index}", body)
            _pin(item, entity, body, surface="עמודים")

        url = reverse("archive-non-person-detail", kwargs={"entity_id": entity.id})
        page1 = self.client.get(url)
        page2 = self.client.get(f"{url}?page=2")

        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.context["per_page"], 48)
        self.assertEqual(len(page1.context["browse_cards"]), 48)
        self.assertEqual(len(page2.context["browse_cards"]), 1)
        self.assertContains(page1, "הבא")
