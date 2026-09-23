"""Shared searchable multi-select on archive-item metadata forms.

The control generalizes the previous existing-tags filter. Categories, events,
tags, authors, and people share one client-side filter. Underlying select
names and option values stay the submitted fields.
"""

import re
from pathlib import Path
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from documents.services.archive_discovery_metadata_validation import (
    empty_discovery_metadata_form_fields,
)
from documents.services.archive_item_authors import (
    empty_archive_item_authors_form_fields,
)
from documents.services.archive_item_people import empty_archive_item_people_form_fields


def _render_discovery_metadata_fields() -> str:
    return render_to_string(
        "documents/archive/discovery_metadata_form_fields.html",
        {
            "form_data": {
                **empty_discovery_metadata_form_fields(),
                "selected_tag_ids": [3],
            },
            "discovery_all_categories": [
                SimpleNamespace(id=1, name="קטגוריה"),
            ],
            "discovery_all_events": [
                SimpleNamespace(id=2, name="אירוע"),
            ],
            "discovery_all_tags": [
                SimpleNamespace(id=3, name="תגית שמורה"),
                SimpleNamespace(id=4, name="אחר"),
            ],
            "discovery_tags_input_id": "tags",
            "discovery_tags_input_name": "tags",
        },
    )


def _select_inner_html(html: str, field_name: str) -> str:
    match = re.search(
        rf'<select[^>]*name="{field_name}"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing select name={field_name}"
    return match.group(1)


def _searchable_multi_select_js() -> str:
    return (
        Path(__file__).resolve().parents[1]
        / "public"
        / "static"
        / "public"
        / "searchable_multi_select.js"
    ).read_text(encoding="utf-8")


class SearchableMultiSelectRenderTests(SimpleTestCase):
    def test_discovery_fields_share_one_filter_and_keep_submitted_names(self):
        html = _render_discovery_metadata_fields()

        self.assertEqual(html.count("data-searchable-multi-select-search"), 3)
        self.assertEqual(html.count('class="searchable-multi-select__search"'), 3)
        self.assertIn("סיווג וקישור", html)
        self.assertIn('placeholder="חיפוש בתגיות קיימות…"', html)
        self.assertIn('aria-label="חיפוש בתגיות קיימות"', html)
        self.assertIn('aria-label="חיפוש בקטגוריות קיימות"', html)
        self.assertIn('aria-label="חיפוש באירועים קיימים"', html)
        self.assertIn("לא נמצאו תגיות תואמות.", html)
        self.assertIn('name="selected_categories"', html)
        self.assertIn('name="selected_events"', html)
        self.assertIn('name="selected_tags"', html)
        self.assertIn('id="categories"', html)
        self.assertIn('id="events"', html)
        self.assertIn('id="tags"', html)
        self.assertIn('name="categories"', html)
        self.assertIn('name="events"', html)
        self.assertIn('name="tags"', html)
        self.assertNotIn(
            'id="selected_categories" class="searchable-multi-select__search"',
            html,
        )
        self.assertNotIn(
            'id="selected_events" class="searchable-multi-select__search"',
            html,
        )
        self.assertNotIn(
            'id="categories" class="searchable-multi-select__search"', html
        )
        self.assertNotIn('id="events" class="searchable-multi-select__search"', html)
        self.assertNotIn('id="tags" class="searchable-multi-select__search"', html)
        self.assertNotIn("archive-advanced-search__choice-filter", html)
        self.assertNotIn("data-existing-tags-field", html)
        self.assertNotIn("existing-tags-search", html)

        tags_html = _select_inner_html(html, "selected_tags")
        self.assertIn('value="3"', tags_html)
        self.assertIn("selected", tags_html)
        self.assertIn("תגית שמורה", tags_html)
        self.assertIn('value="4"', tags_html)

    def test_filter_script_is_shared_and_keeps_selected_options_visible(self):
        js = _searchable_multi_select_js()
        script_html = render_to_string(
            "documents/archive/searchable_multi_select_script.html"
        )

        self.assertIn("public/searchable_multi_select.js", script_html)
        self.assertIn('querySelectorAll("[data-searchable-multi-select]")', js)
        self.assertIn('querySelector("[data-searchable-multi-select-search]")', js)
        self.assertIn('querySelector("[data-searchable-multi-select-control]")', js)
        self.assertIn('querySelector("[data-searchable-multi-select-empty]")', js)
        self.assertIn('searchInput.addEventListener("input", onFilter)', js)
        self.assertIn('select.addEventListener("change", onFilter)', js)
        self.assertIn("toLocaleLowerCase()", js)
        self.assertIn("option.selected", js)
        self.assertIn("const shouldHide = !matches && !option.selected;", js)
        self.assertEqual(js.count("function applyFilter(field)"), 1)
        self.assertIn("ArrowDown", js)

    def test_authors_keep_checkbox_removal_and_filter_unlinked_choices(self):
        linked = SimpleNamespace(id=7, name="Ada Lovelace", selected=True)
        available = SimpleNamespace(id=8, name="Grace Hopper", selected=False)
        html = render_to_string(
            "documents/archive/archive_item_authors_form_fields.html",
            {
                "form_data": {
                    **empty_archive_item_authors_form_fields(),
                    "author_ids": [7],
                },
                "archive_item_selected_authors": [linked],
                "archive_item_author_choices": [linked, available],
            },
        )

        self.assertIn("<legend>מחברים משויכים לפריט זה</legend>", html)
        self.assertIn("אין צורך ב-Ctrl", html)
        self.assertIn('name="new_author_name"', html)
        self.assertRegex(
            html,
            r'<input[^>]*type="checkbox"[^>]*name="author_ids"[^>]*value="7"[^>]*checked',
        )
        picker = _select_inner_html(html, "author_ids")
        self.assertNotIn('value="7"', picker)
        self.assertIn('value="8"', picker)
        self.assertIn("data-searchable-multi-select-search", html)
        self.assertIn('aria-label="חיפוש במחברים קיימים"', html)

    def test_people_keep_selected_options_aliases_and_new_name_field(self):
        linked = SimpleNamespace(id=5, name="Ada", label="Ada (Alias)", selected=True)
        available = SimpleNamespace(id=6, name="Grace", label="Grace", selected=False)
        html = render_to_string(
            "documents/archive/archive_item_people_form_fields.html",
            {
                "show_archive_item_people": True,
                "archive_item_people_heading": "אנשים קשורים",
                "archive_item_people_current_heading": "אנשים קשורים לפריט זה",
                "archive_item_people_hint": "רמז לאנשים",
                "archive_item_selected_people": [linked],
                "archive_item_person_choices": [linked, available],
                "form_data": {
                    **empty_archive_item_people_form_fields(),
                    "archive_item_person_ids": [5],
                },
            },
        )

        self.assertIn("אנשים קשורים", html)
        self.assertIn("אנשים קשורים לפריט זה", html)
        self.assertIn("רמז לאנשים", html)
        self.assertIn("Ada (Alias)", html)
        self.assertIn("בחירת אנשים קיימים", html)
        self.assertIn("אנשים מסומנים מקושרים לפריט", html)
        self.assertIn("ביטול סימון מנתק מהפריט בלבד", html)
        self.assertIn("אינו מוחק את רשומת האדם מהארכיון", html)
        self.assertNotIn("הוספת אדם קיים", html)
        self.assertIn('name="new_archive_item_person_name"', html)
        self.assertIn("data-searchable-multi-select-search", html)
        self.assertIn('aria-label="חיפוש באנשים קיימים"', html)
        picker = _select_inner_html(html, "archive_item_person_ids")
        self.assertIn('value="5"', picker)
        self.assertIn("selected", picker)
        self.assertIn("Ada (Alias)", picker)
        self.assertIn('value="6"', picker)


class SearchableMultiSelectCssTests(SimpleTestCase):
    def _css(self) -> str:
        return (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        ).read_text(encoding="utf-8")

    def test_search_rule_is_widget_scoped_and_compact(self):
        css = self._css()
        marker = "[data-searchable-multi-select] > .searchable-multi-select__search {"
        start = css.index(marker)
        rule = css[start : css.index("}", start)]

        self.assertIn("align-self: start;", rule)
        self.assertIn("width: min(20rem, 100%);", rule)
        self.assertIn("max-width: 100%;", rule)
        self.assertIn("border: 1px solid var(--border-strong);", rule)
        self.assertIn("border-radius: var(--radius-sm);", rule)
        self.assertIn("padding: 10px 12px;", rule)
        self.assertIn("font: inherit;", rule)
        self.assertIn("font-size: var(--font-sm);", rule)
        self.assertIn("background: var(--card-input);", rule)

        shared_selector = css[
            css.index('input[type="text"],') : css.index("textarea {")
        ]
        self.assertNotIn('input[type="search"]', shared_selector)
        self.assertNotIn("searchable-multi-select__search", shared_selector)
        self.assertNotIn("existing-tags-search", shared_selector)

        choice_filter_rule = css[
            css.index(".archive-advanced-search__choice-filter {") : css.index(
                ".archive-advanced-search__field--years {"
            )
        ]
        self.assertNotIn("searchable-multi-select", choice_filter_rule)
        self.assertNotIn("[data-searchable-multi-select]", choice_filter_rule)
        self.assertNotIn("existing-tags-search", choice_filter_rule)
        self.assertEqual(
            css.count(
                "[data-searchable-multi-select] > .searchable-multi-select__search"
            ),
            2,
        )

        mobile = css[
            css.index(
                "  .archive-metadata-grid,\n  .archive-metadata-discovery {\n    grid-template-columns: 1fr;"
            ) : css.index("@media (max-width: 980px)")
        ]
        self.assertIn(".archive-metadata-discovery", mobile)
        self.assertIn(".archive-metadata-grid", mobile)
        self.assertIn("grid-template-columns: 1fr;", mobile)
        self.assertIn(".archive-metadata-edit", css)
        self.assertIn(".archive-metadata-section", css)
        self.assertIn(
            "@media (max-width: 1100px) {\n  .archive-metadata-discovery {",
            css,
        )
        edit_rule = css[
            css.index(".archive-metadata-edit {") : css.index(
                ".archive-metadata-edit > .btn"
            )
        ]
        self.assertIn("width: 100%;", edit_rule)
        self.assertIn("max-width: 100%;", edit_rule)
        self.assertNotIn("52rem", edit_rule)
        self.assertIn(".archive-item-edit.page-header", css)
        self.assertIn(".archive-item-edit__help", css)
