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
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.non_person_entity_presentation import (
    public_mention_sort_key,
    public_mentioned_object_links,
)
from documents.test_archive_item import create_viewable_ocr_document

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
HEADING = "מקומות, ארגונים ועוד המופיעים ברשומה"


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
        self.assertContains(resp, "מקום · עיר")
        self.assertContains(resp, f"/archive/entities/{entity.id}/")
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

    def test_two_entities_sort_by_character_position_not_ordinal(self):
        body = "aaaa aaaa bbbb"
        item = _manual("positions", body)
        later = _entity(canonical_name="אחר-כך", entity_subtype="")
        earlier = _entity(canonical_name="קודם", entity_subtype="")
        _pin(item, later, body, ordinal=2, surface="aaaa")
        _pin(item, earlier, body, ordinal=1, surface="bbbb")

        resp = _detail(self.client, item)
        html = resp.content.decode()

        self.assertLess(html.index("אחר-כך"), html.index("קודם"))

    def test_manual_source_sorts_before_ocr_source(self):
        self.assertLess(
            public_mention_sort_key(MANUAL, 500),
            public_mention_sort_key(OCR, 0),
        )
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
        manual_entity = _entity(canonical_name="ידני", entity_subtype="")
        ocr_entity = _entity(canonical_name="מסמך", entity_subtype="")
        _pin(item, manual_entity, manual_body, text_kind=MANUAL, surface="manualplace")
        _pin(item, ocr_entity, ocr_body, text_kind=OCR, surface="ocrplace")

        with patch(
            "documents.services.non_person_entity_occurrences.item_supports_occurrence_text_kind",
            return_value=True,
        ):
            names = [link.name for link in public_mentioned_object_links(item)]

        self.assertEqual(names, ["ידני", "מסמך"])

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

        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, "אדם קשור")
        self.assertContains(resp, "קטגוריות:")
        self.assertContains(resp, "משפחה")
        self.assertContains(resp, HEADING)
        self.assertContains(resp, "קהיר")

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
