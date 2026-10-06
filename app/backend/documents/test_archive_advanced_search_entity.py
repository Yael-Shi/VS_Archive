"""Advanced archive search filter for one NonPersonEntity."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    NonPersonEntity,
    NonPersonEntityAlias,
    Person,
)
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.test_archive_item import create_viewable_ocr_document

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
RESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
UNRESOLVED = ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED
LIST = "archive-list"


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "קהיר",
        "display_name": "קהיר",
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
    entity: NonPersonEntity | None,
    body: str,
    *,
    text_kind: str = MANUAL,
    ordinal: int = 1,
    surface: str = "קהיר",
    status: str = RESOLVED,
    sha: str | None = None,
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version="surface-v1",
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        resolution_status=status,
        entity=entity,
        matched_text=surface,
    )


def _ids(entity_id: int | None) -> set[int]:
    filters = normalize_archive_advanced_filters(
        {} if entity_id is None else {"entity": str(entity_id)}
    )
    return set(
        filter_archive_items_by_advanced_filters(
            ArchiveItem.objects.all(),
            filters,
        ).values_list("pk", flat=True)
    )


class EntityFilterSemanticsTests(TestCase):
    def test_valid_manual_and_ocr_pins_match_once_and_other_states_do_not(self):
        entity = _entity()
        other = _entity(canonical_name="חיפה", display_name="חיפה", entity_subtype="")
        body = "ביקור בקהיר"
        valid = _manual("פריט תקין", body)
        _pin(valid, entity, body, ordinal=1)
        _pin(valid, entity, body, ordinal=2)
        stale = _manual("פריט מיושן", body)
        _pin(stale, entity, body, sha="a" * 64)
        unresolved_item = _manual("פריט פתוח", body)
        _pin(unresolved_item, None, body, status=UNRESOLVED)
        other_item = _manual("פריט אחר", "ביקור בחיפה")
        _pin(other_item, other, "ביקור בחיפה", surface="חיפה")
        named_only = _manual("רק שם בטקסט", "השם קהיר בלי סיכה")

        doc = create_viewable_ocr_document(
            title="מסמך תקין",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        ocr_body = "printed cairo"
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            text=ocr_body,
        )
        _pin(
            doc.archive_item,
            entity,
            ocr_body,
            text_kind=OCR,
            surface="cairo",
        )

        ids = _ids(entity.id)

        self.assertEqual(ids, {valid.pk, doc.archive_item_id})
        self.assertNotIn(stale.pk, ids)
        self.assertNotIn(unresolved_item.pk, ids)
        self.assertNotIn(other_item.pk, ids)
        self.assertNotIn(named_only.pk, ids)
        self.assertEqual(list(ids).count(valid.pk), 1)

    def test_entity_with_no_valid_pins_matches_nothing(self):
        entity = _entity(canonical_name="ללא סיכה", display_name="ללא סיכה")
        _manual("פריט גלוי", "טקסט")

        self.assertEqual(_ids(entity.id), set())

    def test_text_change_drops_and_restore_returns_the_item(self):
        body = "ביקור בקהיר"
        item = _manual("משתנה", body)
        entity = _entity()
        pin = _pin(item, entity, body)
        stored_sha = pin.source_text_sha256

        item.manual_text_content.body = "טקסט אחר"
        item.manual_text_content.save(update_fields=["body"])
        self.assertEqual(_ids(entity.id), set())
        pin.refresh_from_db()
        self.assertEqual(pin.source_text_sha256, stored_sha)

        item.manual_text_content.body = body
        item.manual_text_content.save(update_fields=["body"])
        self.assertEqual(_ids(entity.id), {item.pk})

    def test_ands_with_q_category_and_leaves_person_filter_alone(self):
        entity = _entity()
        pinned = _manual("UniqueAlphaToken", "ביקור בקהיר")
        also_pinned = _manual("UniqueBetaToken", "עוד קהיר")
        unpinned = _manual("UniqueAlphaTokenExtra", "בלי סיכה")
        _pin(pinned, entity, "ביקור בקהיר")
        _pin(also_pinned, entity, "עוד קהיר")
        category = ArchiveCategory.objects.create(name="משפחה", slug="entity-family")
        pinned.categories.add(category)
        person = Person.objects.create(name="אדם לסינון")
        ArchiveItemPerson.objects.create(archive_item=unpinned, person=person)

        entity_ids = _ids(entity.id)
        self.assertEqual(entity_ids, {pinned.pk, also_pinned.pk})
        self.assertNotIn(unpinned.pk, entity_ids)

        from documents.services.archive_item_presentation import (
            filter_archive_items_by_search_query,
        )

        q_and_entity = filter_archive_items_by_search_query(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"entity": str(entity.id)}),
            ),
            "UniqueAlphaToken",
        )
        self.assertEqual(set(q_and_entity.values_list("pk", flat=True)), {pinned.pk})

        category_and_entity = filter_archive_items_by_advanced_filters(
            ArchiveItem.objects.all(),
            normalize_archive_advanced_filters(
                {"entity": str(entity.id), "category": str(category.id)}
            ),
        )
        self.assertEqual(
            set(category_and_entity.values_list("pk", flat=True)),
            {pinned.pk},
        )

        person_only = filter_archive_items_by_advanced_filters(
            ArchiveItem.objects.all(),
            normalize_archive_advanced_filters({"person": str(person.id)}),
        )
        self.assertEqual(set(person_only.values_list("pk", flat=True)), {unpinned.pk})
        self.assertEqual(
            set(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({}),
                ).values_list("pk", flat=True)
            ),
            {pinned.pk, also_pinned.pk, unpinned.pk},
        )

    def test_first_entity_param_wins_and_invalid_ids_follow_filter_rules(self):
        first = _entity(canonical_name="ראשון", display_name="ראשון", entity_subtype="")
        second = _entity(canonical_name="שני", display_name="שני", entity_subtype="")
        first_item = _manual("פריט ראשון", "ראשון")
        second_item = _manual("פריט שני", "שני")
        _pin(first_item, first, "ראשון", surface="ראשון")
        _pin(second_item, second, "שני", surface="שני")

        filters = normalize_archive_advanced_filters(
            [("entity", str(first.id)), ("entity", str(second.id))]
        )
        self.assertEqual(filters.entity_id, first.id)
        ids = set(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                filters,
            ).values_list("pk", flat=True)
        )
        self.assertEqual(ids, {first_item.pk})

        malformed = normalize_archive_advanced_filters({"entity": "abc"})
        self.assertIsNone(malformed.entity_id)
        unknown = normalize_archive_advanced_filters({"entity": "999999"})
        self.assertEqual(unknown.entity_id, 999999)
        self.assertEqual(
            set(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    unknown,
                ).values_list("pk", flat=True)
            ),
            set(),
        )


class EntityFilterUiTests(TestCase):
    def test_selector_chip_clear_and_pagination_keep_the_entity_id(self):
        entity = _entity()
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="OCRSECRET",
            kind=NonPersonEntityAlias.Kind.OCR_VARIANT,
        )
        other = _entity(canonical_name="אלכסנדריה", display_name="", entity_subtype="")
        category = ArchiveCategory.objects.create(name="עיר", slug="entity-city")
        shown = _manual("פריט מוצג", "ביקור בקהיר")
        _pin(shown, entity, "ביקור בקהיר")
        shown.categories.add(category)
        for index in range(24):
            body = f"קהיר {index}"
            item = _manual(f"עמוד {index:02d}", body)
            _pin(item, entity, body)

        resp = self.client.get(
            reverse(LIST),
            {
                "advanced": "1",
                "entity": str(entity.id),
                "q": "פריט מוצג",
                "category": str(category.id),
            },
        )
        choices = list(resp.context["advanced_filter_entity_choices"])
        labels = [choice.label for choice in choices]
        selected = next(choice for choice in choices if choice.id == entity.id)
        chip = next(
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "entity"
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["advanced_filter_entity"], entity.id)
        self.assertIn("קהיר — מקום · עיר", labels)
        self.assertIn("אלכסנדריה — מקום", labels)
        self.assertLess(
            labels.index("אלכסנדריה — מקום"), labels.index("קהיר — מקום · עיר")
        )
        self.assertNotIn("PLACE", selected.label)
        self.assertNotIn("CITY", selected.label)
        self.assertNotIn("OCRSECRET", selected.label)
        self.assertIn("OCRSECRET", selected.search_text)
        self.assertContains(resp, "מקום, ארגון או גוף אחר")
        self.assertContains(resp, "קהיר — מקום · עיר")
        self.assertNotContains(resp, "ישות")
        self.assertNotContains(resp, "Entity")
        self.assertEqual(chip["value"], "קהיר — מקום · עיר")
        self.assertIn("q=", chip["remove_href_suffix"])
        self.assertIn(f"category={category.id}", chip["remove_href_suffix"])
        self.assertNotIn("entity=", chip["remove_href_suffix"])
        self.assertEqual(other.display_name, "")

        page2 = self.client.get(
            reverse(LIST),
            {"entity": str(entity.id), "per_page": "24", "page": "2"},
        )
        self.assertEqual(page2.status_code, 200)
        self.assertIn("entity=", page2.context["prev_href_suffix"])
        self.assertEqual(page2.context["advanced_filter_entity"], entity.id)
        self.assertEqual(len(page2.context["browse_cards"]), 1)

    def test_unknown_entity_is_empty_and_private_items_stay_hidden(self):
        entity = _entity(canonical_name="שםשאינובגוף", display_name="שםשאינובגוף")
        public = _manual("פריט ציבורי", "ביקור בקהיר")
        private = _manual(
            "SECRET-PRIVATE-TITLE",
            "ביקור בקהיר",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _pin(public, entity, "ביקור בקהיר", ordinal=1)
        _pin(private, entity, "ביקור בקהיר", ordinal=1)

        listed = self.client.get(reverse(LIST), {"entity": str(entity.id)})
        unknown = self.client.get(reverse(LIST), {"entity": "999999"})
        index = public.search_index

        self.assertContains(listed, "פריט ציבורי")
        self.assertNotContains(listed, "SECRET-PRIVATE-TITLE")
        self.assertEqual(unknown.status_code, 200)
        self.assertContains(unknown, "לא נמצאו פריטים התואמים את החיפוש.")
        self.assertEqual(unknown.context["browse_cards"], [])
        self.assertEqual(unknown.context["advanced_filter_entity"], 999999)
        self.assertTrue(unknown.context["advanced_filters_active"])
        chip = next(
            item
            for item in unknown.context["active_filter_chips"]
            if item["kind"] == "entity"
        )
        self.assertEqual(chip["value"], "לא נמצא")
        self.assertNotIn("999999", chip["value"])
        self.assertNotIn("entity=", chip["remove_href_suffix"])
        self.assertContains(unknown, "לא נמצא")
        self.assertNotIn("שםשאינובגוף", index.title_text)
        self.assertNotIn("שםשאינובגוף", index.metadata_text)
        self.assertNotIn("שםשאינובגוף", index.body_text)
        self.assertNotIn("שםשאינובגוף", index.hebrew_translation_text)

    def test_archive_q_does_not_match_registry_names(self):
        token = "UniqueRegistryFilterToken"
        entity = _entity(canonical_name=token, display_name=token, entity_subtype="")
        NonPersonEntityAlias.objects.create(
            entity=entity,
            name="UniqueRegistryFilterAlias",
            kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
        )
        item = _manual("כותרת רגילה", "גוף רגיל")
        before = ArchiveItemSearchIndex.objects.count()
        _pin(item, entity, "גוף רגיל", surface="גוף")

        self.assertEqual(ArchiveItemSearchIndex.objects.count(), before)
        resp = self.client.get(reverse(LIST), {"q": token})
        self.assertContains(resp, "נמצאו 0 תוצאות")
        self.assertNotContains(resp, "כותרת רגילה")
