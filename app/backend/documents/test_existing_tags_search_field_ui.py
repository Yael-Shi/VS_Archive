"""Existing-tags search field presentation on discovery metadata forms."""

from pathlib import Path

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from documents.services.archive_discovery_metadata_validation import (
    empty_discovery_metadata_form_fields,
)


def _render_discovery_metadata_fields() -> str:
    return render_to_string(
        "documents/archive/discovery_metadata_form_fields.html",
        {
            "form_data": empty_discovery_metadata_form_fields(),
            "discovery_all_categories": [],
            "discovery_all_events": [],
            "discovery_all_tags": [],
            "discovery_tags_input_id": "tags",
            "discovery_tags_input_name": "tags",
        },
    )


class ExistingTagsSearchFieldRenderTests(SimpleTestCase):
    def test_existing_tags_search_renders_with_filter_hook_and_tag_style_class(self):
        html = _render_discovery_metadata_fields()

        self.assertIn("data-existing-tags-search", html)
        self.assertIn('placeholder="חיפוש בתגיות קיימות…"', html)
        self.assertIn('aria-label="חיפוש בתגיות קיימות"', html)
        self.assertEqual(html.count('class="existing-tags-search"'), 1)
        self.assertIn("data-existing-tags-select", html)
        self.assertIn("data-existing-tags-field", html)

        self.assertIn('id="selected_categories"', html)
        self.assertIn('id="selected_events"', html)
        self.assertIn('id="categories"', html)
        self.assertIn('id="events"', html)
        self.assertIn('id="tags"', html)
        self.assertNotIn(
            'id="selected_categories" class="existing-tags-search"',
            html,
        )
        self.assertNotIn('id="selected_events" class="existing-tags-search"', html)
        self.assertNotIn('id="categories" class="existing-tags-search"', html)
        self.assertNotIn('id="events" class="existing-tags-search"', html)
        self.assertNotIn('id="tags" class="existing-tags-search"', html)
        self.assertNotIn("archive-advanced-search__choice-filter", html)

    def test_existing_tags_filter_script_hooks_are_unchanged(self):
        html = _render_discovery_metadata_fields()

        self.assertIn('querySelectorAll("[data-existing-tags-field]")', html)
        self.assertIn('querySelector("[data-existing-tags-search]")', html)
        self.assertIn('querySelector("[data-existing-tags-select]")', html)
        self.assertIn('querySelector("[data-existing-tags-empty]")', html)
        self.assertIn('searchInput.addEventListener("input", applyFilter)', html)
        self.assertIn('select.addEventListener("change", applyFilter)', html)
        self.assertIn("option.selected", html)
        self.assertIn('name="selected_tags"', html)


class ExistingTagsSearchFieldCssTests(SimpleTestCase):
    def _css(self) -> str:
        return (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        ).read_text(encoding="utf-8")

    def test_existing_tags_search_rule_is_tag_scoped_and_compact(self):
        css = self._css()
        marker = "[data-existing-tags-field] > .existing-tags-search {"
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
        self.assertNotIn("existing-tags-search", shared_selector)

        choice_filter_rule = css[
            css.index(".archive-advanced-search__choice-filter {") : css.index(
                ".archive-advanced-search__field--years {"
            )
        ]
        self.assertNotIn("existing-tags-search", choice_filter_rule)
        self.assertNotIn("[data-existing-tags-field]", choice_filter_rule)
        self.assertEqual(
            css.count("[data-existing-tags-field] > .existing-tags-search"),
            2,
        )
