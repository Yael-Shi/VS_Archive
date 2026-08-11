"""Public /archive/ advanced-search UI (PR2)."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    Tag,
)
from documents.services.archive_advanced_search import (
    ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR,
    ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR,
    ARCHIVE_ADVANCED_YEAR_TO_WITHOUT_YEAR_ERROR,
    EMPTY_ARCHIVE_ADVANCED_FILTER_CHOICE_CONTEXT,
    archive_advanced_filter_choice_context,
    filters_for_archive_list_search,
    should_load_archive_advanced_filter_choices,
    validate_archive_advanced_year_fields,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    update_archive_item_discovery_metadata,
)

User = get_user_model()


def _public_item(
    *,
    title: str,
    body: str = "body",
    author_name: str = "",
    date_start=None,
    date_end=None,
    date_precision: str | None = None,
    category_names: list[str] | None = None,
    event_names: list[str] | None = None,
    tag_names: list[str] | None = None,
) -> ArchiveItem:
    item = create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )
    if category_names is not None or event_names is not None or tag_names is not None:
        update_archive_item_discovery_metadata(
            item,
            category_names=category_names or [],
            event_names=event_names or [],
            tag_names=tag_names or [],
        )
    return item


def _choice_context_query_count(captured_queries) -> int:
    """Count queries issued by ``archive_advanced_filter_choice_context`` itself."""
    count = 0
    for query in captured_queries:
        sql = query["sql"].lower().replace('"', "")
        # Author distinct list from the authorized item table.
        if (
            "documents_archiveitem" in sql
            and "author_name" in sql
            and "distinct" in sql
            and "documents_archivecategory" not in sql
        ):
            count += 1
            continue
        # Taxonomy choice queries are rooted at category/event/tag tables with
        # an archive_items__pk__in subquery (not browse-card prefetches).
        if sql.lstrip().startswith("select") and "documents_archivecategory" in sql:
            if "archiveitem_categories" in sql or "archive_items" in sql:
                count += 1
                continue
        if sql.lstrip().startswith("select") and "documents_archiveevent" in sql:
            if "archiveitem_events" in sql or "archive_items" in sql:
                count += 1
                continue
        if sql.lstrip().startswith("select") and " from documents_tag" in sql:
            if "archiveitem_tags" in sql or "archive_items" in sql:
                count += 1
    return count


def _measure_choice_context_queries(authorized_queryset) -> int:
    with CaptureQueriesContext(connection) as ctx:
        archive_advanced_filter_choice_context(authorized_queryset)
    return _choice_context_query_count(ctx)


class ArchiveAdvancedYearValidationTests(SimpleTestCase):
    def test_reverse_year_range_is_invalid(self):
        result = validate_archive_advanced_year_fields(
            {"year": "1960", "year_to": "1950"}
        )
        self.assertFalse(result.is_valid)
        self.assertEqual(result.year_raw, "1960")
        self.assertEqual(result.year_to_raw, "1950")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR, result.errors)

    def test_malformed_year_is_invalid(self):
        result = validate_archive_advanced_year_fields({"year": "abc"})
        self.assertFalse(result.is_valid)
        self.assertEqual(result.year_raw, "abc")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR, result.errors)

    def test_year_to_without_year_is_invalid(self):
        result = validate_archive_advanced_year_fields({"year_to": "1955"})
        self.assertFalse(result.is_valid)
        self.assertIn(ARCHIVE_ADVANCED_YEAR_TO_WITHOUT_YEAR_ERROR, result.errors)

    def test_valid_single_and_range(self):
        single = validate_archive_advanced_year_fields({"year": "1953"})
        self.assertTrue(single.is_valid)
        ranged = validate_archive_advanced_year_fields(
            {"year": "1950", "year_to": "1955"}
        )
        self.assertTrue(ranged.is_valid)

    def test_filters_for_search_drop_year_when_invalid(self):
        filters = filters_for_archive_list_search(
            {
                "author": "Alice",
                "year": "1960",
                "year_to": "1950",
            }
        )
        self.assertEqual(filters.author, "Alice")
        self.assertIsNone(filters.year)
        self.assertIsNone(filters.year_to)


class ArchiveAdvancedSearchUiTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family = User.objects.create_user(username="adv-ui-family", password="x")
        self.family.groups.add(self.family_group)
        self.url = reverse("archive-list")

    def test_advanced_panel_closed_by_default(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["advanced_panel_open"])
        html = resp.content.decode("utf-8")
        self.assertIn("חיפוש מתקדם", html)
        self.assertNotIn('id="archive-filter-author"', html)
        self.assertNotIn("חפשו בארכיון", html)

    def test_opening_advanced_panel_renders_authorized_choices(self):
        public_cat = ArchiveCategory.objects.create(
            name="UI Public Cat", slug="ui-public-cat"
        )
        private_cat = ArchiveCategory.objects.create(
            name="UI Private Cat", slug="ui-private-cat"
        )
        public_tag = Tag.objects.create(name="UI Public Tag")
        private_tag = Tag.objects.create(name="UI Private Tag")
        public_event = ArchiveEvent.objects.create(
            name="UI Public Event", slug="ui-public-event"
        )
        private_event = ArchiveEvent.objects.create(
            name="UI Private Event", slug="ui-private-event"
        )
        _public_item(
            title="UI public item",
            author_name="UI Public Author",
            category_names=["UI Public Cat"],
            event_names=["UI Public Event"],
            tag_names=["UI Public Tag"],
        )
        private_item = create_manual_text_archive_item(
            title="UI private item",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="UI Private Author",
        )
        update_archive_item_discovery_metadata(
            private_item,
            category_names=["UI Private Cat"],
            event_names=["UI Private Event"],
            tag_names=["UI Private Tag"],
        )

        anon = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(anon.status_code, 200)
        self.assertTrue(anon.context["advanced_panel_open"])
        self.assertTrue(anon.context["load_advanced_choices"])
        self.assertEqual(
            anon.context["advanced_filter_author_choices"], ("UI Public Author",)
        )
        self.assertEqual(
            [c.pk for c in anon.context["advanced_filter_category_choices"]],
            [public_cat.pk],
        )
        self.assertEqual(
            [e.pk for e in anon.context["advanced_filter_event_choices"]],
            [public_event.pk],
        )
        self.assertEqual(
            [t.pk for t in anon.context["advanced_filter_tag_choices"]],
            [public_tag.pk],
        )
        html = anon.content.decode("utf-8")
        self.assertIn("UI Public Author", html)
        self.assertIn("UI Public Cat", html)
        self.assertNotIn("UI Private Author", html)
        self.assertNotIn("UI Private Cat", html)
        self.assertNotIn("UI Private Tag", html)
        self.assertNotIn("UI Private Event", html)

        self.client.force_login(self.family)
        family = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(
            set(family.context["advanced_filter_author_choices"]),
            {"UI Public Author", "UI Private Author"},
        )
        self.assertEqual(
            {c.pk for c in family.context["advanced_filter_category_choices"]},
            {public_cat.pk, private_cat.pk},
        )
        self.assertEqual(
            {e.pk for e in family.context["advanced_filter_event_choices"]},
            {public_event.pk, private_event.pk},
        )
        self.assertEqual(
            {t.pk for t in family.context["advanced_filter_tag_choices"]},
            {public_tag.pk, private_tag.pk},
        )

    def test_ordinary_and_q_only_skip_choice_context_queries(self):
        _public_item(
            title="ChoiceCost Item",
            author_name="ChoiceCost Author",
            category_names=["ChoiceCost Cat"],
            event_names=["ChoiceCost Event"],
            tag_names=["ChoiceCost Tag"],
        )
        from documents.services.archive_item_access import (
            archive_browse_queryset_for_user,
        )

        authorized = archive_browse_queryset_for_user(None)
        # Baseline cost of the choice-context helper itself (PR1-era every-request cost).
        baseline_choice_queries = _measure_choice_context_queries(authorized)
        self.assertGreaterEqual(baseline_choice_queries, 4)

        # Warm caches / auth queries.
        self.client.get(self.url)

        with patch(
            "documents.views.archive_advanced_filter_choice_context",
            wraps=archive_advanced_filter_choice_context,
        ) as mock_choices:
            plain = self.client.get(self.url)
            self.assertEqual(plain.status_code, 200)
            self.assertFalse(plain.context["load_advanced_choices"])
            self.assertEqual(plain.context["advanced_filter_author_choices"], ())
            self.assertEqual(plain.context["advanced_filter_category_choices"], ())
            mock_choices.assert_not_called()

            q_only = self.client.get(self.url, {"q": "ChoiceCost"})
            self.assertEqual(q_only.status_code, 200)
            self.assertFalse(q_only.context["load_advanced_choices"])
            mock_choices.assert_not_called()

            with CaptureQueriesContext(connection) as advanced_ctx:
                advanced = self.client.get(self.url, {"advanced": "1"})
            self.assertEqual(advanced.status_code, 200)
            self.assertTrue(advanced.context["load_advanced_choices"])
            mock_choices.assert_called_once()
            self.assertGreaterEqual(
                _choice_context_query_count(advanced_ctx),
                baseline_choice_queries,
            )
            self.assertIn(
                "ChoiceCost Author",
                advanced.context["advanced_filter_author_choices"],
            )

        # Keep empty-context constant import used (guards against accidental drift).
        self.assertEqual(
            EMPTY_ARCHIVE_ADVANCED_FILTER_CHOICE_CONTEXT[
                "advanced_filter_author_choices"
            ],
            (),
        )

    def test_should_load_choices_helper(self):
        self.assertFalse(
            should_load_archive_advanced_filter_choices(
                panel_open=False, advanced_filters_active=False
            )
        )
        self.assertTrue(
            should_load_archive_advanced_filter_choices(
                panel_open=True, advanced_filters_active=False
            )
        )
        self.assertTrue(
            should_load_archive_advanced_filter_choices(
                panel_open=False, advanced_filters_active=True
            )
        )

    def test_controls_preserve_selected_values_including_multi(self):
        cat_a = ArchiveCategory.objects.create(name="UI Cat A", slug="ui-cat-a")
        cat_b = ArchiveCategory.objects.create(name="UI Cat B", slug="ui-cat-b")
        event = ArchiveEvent.objects.create(name="UI Event A", slug="ui-event-a")
        tag_a = Tag.objects.create(name="UI Tag A")
        tag_b = Tag.objects.create(name="UI Tag B")
        _public_item(
            title="Preserve multi",
            author_name="Preserve Author",
            category_names=["UI Cat A", "UI Cat B"],
            event_names=["UI Event A"],
            tag_names=["UI Tag A", "UI Tag B"],
        )
        resp = self.client.get(
            self.url,
            [
                ("advanced", "1"),
                ("author", "Preserve Author"),
                ("category", str(cat_a.id)),
                ("category", str(cat_b.id)),
                ("event", str(event.id)),
                ("tag", str(tag_a.id)),
                ("tag", str(tag_b.id)),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["advanced_filter_author"], "Preserve Author")
        self.assertEqual(
            resp.context["advanced_filter_category_ids"], (cat_a.id, cat_b.id)
        )
        self.assertEqual(resp.context["advanced_filter_event_ids"], (event.id,))
        self.assertEqual(resp.context["advanced_filter_tag_ids"], (tag_a.id, tag_b.id))
        html = resp.content.decode("utf-8")
        self.assertIn('id="archive-filter-author"', html)
        self.assertIn(f'value="{cat_a.id}"', html)
        self.assertIn(f'value="{cat_b.id}"', html)
        self.assertIn(f'value="{tag_a.id}"', html)
        self.assertIn(f'value="{tag_b.id}"', html)

    def test_single_year_and_year_range_through_ui(self):
        _public_item(
            title="UIYEAR-1953",
            date_start=date(1953, 6, 1),
            date_end=date(1953, 6, 30),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        _public_item(
            title="UIYEAR-1955",
            date_start=date(1955, 1, 1),
            date_end=date(1955, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        outside = _public_item(
            title="UIYEAR-1960",
            date_start=date(1960, 1, 1),
            date_end=date(1960, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )

        single = self.client.get(self.url, {"year": "1953"})
        self.assertEqual(single.status_code, 200)
        self.assertFalse(single.context["advanced_year_validation_failed"])
        self.assertEqual(single.context["total_count"], 1)
        html = single.content.decode("utf-8")
        self.assertIn("UIYEAR-1953", html)
        self.assertNotIn("UIYEAR-1955", html)
        self.assertNotIn("UIYEAR-1960", html)
        self.assertIn("נמצאו 1 תוצאות", html)

        ranged = self.client.get(self.url, {"year": "1953", "year_to": "1955"})
        self.assertEqual(ranged.status_code, 200)
        self.assertEqual(ranged.context["total_count"], 2)
        ranged_html = ranged.content.decode("utf-8")
        self.assertIn("UIYEAR-1953", ranged_html)
        self.assertIn("UIYEAR-1955", ranged_html)
        self.assertNotIn(outside.title, ranged_html)
        self.assertIn("1953–1955", ranged_html)

    def test_invalid_year_suppresses_all_result_execution(self):
        """Any year validation failure blocks results (not only year-only forms)."""
        only_1960 = _public_item(
            title="REVERSE-1960-ONLY",
            date_start=date(1960, 1, 1),
            date_end=date(1960, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        _public_item(
            title="REVERSE-1950",
            date_start=date(1950, 1, 1),
            date_end=date(1950, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        resp = self.client.get(self.url, {"year": "1960", "year_to": "1950"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["advanced_year_validation_failed"])
        self.assertTrue(resp.context["advanced_panel_open"])
        self.assertIsNone(resp.context["advanced_filters"].year)
        self.assertEqual(resp.context["advanced_filter_year_input"], "1960")
        self.assertEqual(resp.context["advanced_filter_year_to_input"], "1950")
        html = resp.content.decode("utf-8")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR, html)
        self.assertIn('value="1960"', html)
        self.assertIn('value="1950"', html)
        # Must not silently execute the defensive single-year fallback search.
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])
        self.assertEqual(list(resp.context["browse_cards"]), [])
        self.assertNotIn(only_1960.title, html)
        self.assertNotIn("archive-search-results-count", html)
        self.assertNotIn("נמצאו 0 תוצאות", html)
        self.assertNotIn("נמצאו 1 תוצאות", html)

    def test_reverse_year_with_valid_author_suppresses_results_and_preserves_form(
        self,
    ):
        match = _public_item(
            title="AUTHOR-YEAR-MATCH",
            author_name="Alice",
            date_start=date(1960, 1, 1),
            date_end=date(1960, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        resp = self.client.get(
            self.url,
            {
                "author": "Alice",
                "year": "1960",
                "year_to": "1950",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["advanced_year_validation_failed"])
        self.assertTrue(resp.context["advanced_panel_open"])
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])
        self.assertEqual(list(resp.context["browse_cards"]), [])
        self.assertEqual(resp.context["advanced_filter_author"], "Alice")
        self.assertEqual(resp.context["advanced_filter_year_input"], "1960")
        self.assertEqual(resp.context["advanced_filter_year_to_input"], "1950")
        html = resp.content.decode("utf-8")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_REVERSE_RANGE_ERROR, html)
        self.assertIn('value="1960"', html)
        self.assertIn('value="1950"', html)
        self.assertIn("Alice", html)
        self.assertIn(
            'value="Alice"',
            html,
        )
        self.assertNotIn(match.title, html)
        self.assertNotIn("archive-search-results-count", html)

    def test_malformed_year_with_valid_q_suppresses_results_and_preserves_q(self):
        match = _public_item(title="QYEAR Unique Token Title", body="body")
        resp = self.client.get(
            self.url,
            {
                "q": "QYEAR Unique Token",
                "year": "abc",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["advanced_year_validation_failed"])
        self.assertTrue(resp.context["advanced_panel_open"])
        self.assertEqual(resp.context["q"], "QYEAR Unique Token")
        self.assertEqual(resp.context["advanced_filter_year_input"], "abc")
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])
        self.assertEqual(list(resp.context["browse_cards"]), [])
        html = resp.content.decode("utf-8")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR, html)
        self.assertIn('value="QYEAR Unique Token"', html)
        self.assertIn('value="abc"', html)
        self.assertNotIn(match.title, html)
        self.assertNotIn("archive-search-results-count", html)

    def test_malformed_year_shows_validation_error_and_preserves_value(self):
        resp = self.client.get(self.url, {"year": "19xx"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["advanced_year_validation_failed"])
        self.assertEqual(resp.context["advanced_filter_year_input"], "19xx")
        html = resp.content.decode("utf-8")
        self.assertIn(ARCHIVE_ADVANCED_YEAR_MALFORMED_ERROR, html)
        self.assertIn('value="19xx"', html)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])

    def test_active_filter_summary_chips_and_clear_all(self):
        cat = ArchiveCategory.objects.create(name="Chip Cat", slug="chip-cat")
        tag = Tag.objects.create(name="Chip Tag")
        _public_item(
            title="Chip Item",
            author_name="Chip Author",
            category_names=["Chip Cat"],
            tag_names=["Chip Tag"],
            date_start=date(1950, 1, 1),
            date_end=date(1955, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        resp = self.client.get(
            self.url,
            [
                ("q", "Chip"),
                ("author", "Chip Author"),
                ("category", str(cat.id)),
                ("tag", str(tag.id)),
                ("year", "1950"),
                ("year_to", "1955"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.context["active_filter_summary_visible"])
        html = resp.content.decode("utf-8")
        self.assertIn("שינוי החיפוש המתקדם", html)
        self.assertIn("ניקוי הכול", html)
        self.assertIn("מחבר/ת", html)
        self.assertIn("Chip Author", html)
        self.assertIn("Chip Cat", html)
        self.assertIn("Chip Tag", html)
        self.assertIn("1950–1955", html)
        self.assertIn("נמצאו 1 תוצאות", html)
        self.assertIn('עבור "Chip"', html)

        clear_all = resp.context["clear_all_query_suffix"]
        clear_resp = self.client.get(f"{self.url}{clear_all}")
        self.assertEqual(clear_resp.status_code, 200)
        self.assertEqual(clear_resp.context["q"], "")
        self.assertFalse(clear_resp.context["advanced_filters_active"])
        self.assertFalse(clear_resp.context["active_filter_summary_visible"])
        clear_html = clear_resp.content.decode("utf-8")
        self.assertNotIn('name="author"', clear_html)
        self.assertNotIn(f"category={cat.id}", clear_html)

    def test_clear_q_only_preserves_advanced_filters(self):
        cat = ArchiveCategory.objects.create(name="ClearQ Cat", slug="clearq-cat")
        _public_item(
            title="ClearQ Item",
            author_name="ClearQ Author",
            category_names=["ClearQ Cat"],
        )
        resp = self.client.get(
            self.url,
            {
                "q": "ClearQ",
                "author": "ClearQ Author",
                "category": str(cat.id),
            },
        )
        clear_q = resp.context["clear_search_query_suffix"]
        parsed = parse_qs(clear_q.lstrip("?"))
        self.assertNotIn("q", parsed)
        self.assertEqual(parsed["author"], ["ClearQ Author"])
        self.assertEqual(parsed["category"], [str(cat.id)])

        cleared = self.client.get(f"{self.url}{clear_q}")
        self.assertEqual(cleared.context["q"], "")
        self.assertEqual(cleared.context["advanced_filter_author"], "ClearQ Author")
        self.assertEqual(cleared.context["advanced_filter_category_ids"], (cat.id,))

    def test_item_type_switch_preserves_advanced_filters(self):
        cat = ArchiveCategory.objects.create(name="TypeKeep Cat", slug="typekeep-cat")
        _public_item(
            title="TypeKeep Doc",
            author_name="TypeKeep Author",
            category_names=["TypeKeep Cat"],
        )
        resp = self.client.get(
            self.url,
            {
                "author": "TypeKeep Author",
                "category": str(cat.id),
                "year": "1950",
            },
        )
        photo_link = next(
            link
            for link in resp.context["item_type_filter_links"]
            if link["label"] == "תמונות"
        )
        parsed = parse_qs(str(photo_link["href_suffix"]).lstrip("?"))
        self.assertEqual(parsed["author"], ["TypeKeep Author"])
        self.assertEqual(parsed["category"], [str(cat.id)])
        self.assertEqual(parsed["year"], ["1950"])
        self.assertEqual(parsed["item_type"], ["photo"])

    def test_pagination_preserves_advanced_filters(self):
        cat = ArchiveCategory.objects.create(name="PageUI Cat", slug="pageui-cat")
        for index in range(50):
            _public_item(
                title=f"PAGEUI-{index:02d}",
                author_name="PageUI Author",
                category_names=["PageUI Cat"],
            )
        resp = self.client.get(
            self.url,
            {
                "author": "PageUI Author",
                "category": str(cat.id),
                "per_page": "24",
                "page": "2",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["page"], 2)
        html = resp.content.decode("utf-8")
        self.assertIn(f"category={cat.id}", html)
        self.assertIn("author=PageUI+Author", html)
        next_parsed = parse_qs(str(resp.context["prev_href_suffix"]).lstrip("?"))
        self.assertEqual(next_parsed["author"], ["PageUI Author"])
        self.assertEqual(next_parsed["category"], [str(cat.id)])

    def test_zero_results_still_exposes_advanced_search(self):
        resp = self.client.get(self.url, {"q": "zzznomatchtoken"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        html = resp.content.decode("utf-8")
        self.assertIn("לא נמצאו פריטים התואמים את החיפוש", html)
        self.assertIn("חיפוש מתקדם", html)
        self.assertIn("advanced=1", html)

    def test_q_only_search_ui_regression(self):
        _public_item(title="QOnly Unique Title", body="hello")
        resp = self.client.get(self.url, {"q": "QOnly Unique"})
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("QOnly Unique Title", html)
        self.assertIn("נמצאו 1 תוצאות", html)
        self.assertIn('עבור "QOnly Unique"', html)
        self.assertIn("ניקוי החיפוש", html)
        self.assertNotIn(">ניקוי<", html)
        self.assertFalse(resp.context["advanced_panel_open"])
        self.assertIn("archive-search-form__row", html)
        self.assertIn("archive-type-filter", html)

    def test_advanced_param_does_not_affect_filtering(self):
        match = _public_item(title="AdvParam Item", author_name="AdvParam Author")
        with_flag = self.client.get(
            self.url,
            {"author": "AdvParam Author", "advanced": "1"},
        )
        without_flag = self.client.get(
            self.url,
            {"author": "AdvParam Author"},
        )
        self.assertEqual(with_flag.context["total_count"], 1)
        self.assertEqual(without_flag.context["total_count"], 1)
        self.assertEqual(
            [item.pk for item in with_flag.context["items"]],
            [match.pk],
        )
        self.assertTrue(with_flag.context["advanced_panel_open"])
        self.assertFalse(without_flag.context["advanced_panel_open"])

    def test_mobile_responsive_classes_present(self):
        resp = self.client.get(self.url, {"advanced": "1"})
        html = resp.content.decode("utf-8")
        self.assertIn("archive-advanced-search", html)
        self.assertIn("archive-advanced-search__year-row", html)
        self.assertIn("archive-search-form__row", html)
