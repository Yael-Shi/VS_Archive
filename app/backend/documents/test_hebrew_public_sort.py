"""Public Hebrew display order. No database."""

from django.test import SimpleTestCase

from documents.services.hebrew_display_order import (
    fold_long_dashes,
    hebrew_public_sort_key,
)


class HebrewPublicSortKeyTests(SimpleTestCase):
    def test_hebrew_precedes_latin_and_finals_share_a_key(self):
        ordered = sorted(
            ["Zebra", "תמר", "אבא", "ץפת", "(בני)", "  צפת  ", "école"],
            key=hebrew_public_sort_key,
        )

        self.assertEqual(
            ordered,
            ["אבא", "(בני)", "ץפת", "  צפת  ", "תמר", "Zebra", "école"],
        )
        self.assertEqual(hebrew_public_sort_key("ץפת"), hebrew_public_sort_key("צפת"))
        self.assertEqual(hebrew_public_sort_key("צפת"), hebrew_public_sort_key("  צפת  "))

    def test_long_dashes_fold_and_maqaf_stays(self):
        self.assertEqual(fold_long_dashes("א\u2013ב\u2014ג־ד"), "א-ב-ג־ד")
        self.assertEqual(
            hebrew_public_sort_key("א\u2014ב"),
            hebrew_public_sort_key("א-ב"),
        )

    def test_equal_names_need_a_caller_tie_break(self):
        key = hebrew_public_sort_key("שם זהה")
        ordered = sorted(
            [(key, 20, "later"), (key, 7, "earlier")],
            key=lambda row: (row[0], row[1]),
        )

        self.assertEqual([row[2] for row in ordered], ["earlier", "later"])
