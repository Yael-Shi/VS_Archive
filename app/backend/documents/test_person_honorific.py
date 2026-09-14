"""Person honorific display helper: strip-only, no inference."""

from django.test import SimpleTestCase

from documents.services.person_display import (
    format_person_display_name,
    person_public_display_name,
)


class _PersonLike:
    def __init__(self, name, honorific=""):
        self.name = name
        self.honorific = honorific


class PersonDisplayNameTests(SimpleTestCase):
    def test_blank_honorific_returns_name(self):
        self.assertEqual(
            format_person_display_name(name="חיים סעדיה", honorific=""),
            "חיים סעדיה",
        )
        self.assertEqual(
            format_person_display_name(name="חיים סעדיה", honorific="   "),
            "חיים סעדיה",
        )

    def test_normal_honorific_appends_comma(self):
        self.assertEqual(
            format_person_display_name(name="חיים סעדיה", honorific='ד"ר'),
            'חיים סעדיה, ד"ר',
        )
        self.assertEqual(
            format_person_display_name(name="ונטורה", honorific="רב"),
            "ונטורה, רב",
        )

    def test_whitespace_is_stripped_for_presentation_only(self):
        self.assertEqual(
            format_person_display_name(name="  חיים סעדיה  ", honorific='  ד"ר  '),
            'חיים סעדיה, ד"ר',
        )

    def test_multiple_title_string_is_kept_verbatim(self):
        self.assertEqual(
            format_person_display_name(name="משה ונטורה", honorific="הרב דר'"),
            "משה ונטורה, הרב דר'",
        )

    def test_does_not_parse_or_infer_honorific_from_name(self):
        self.assertEqual(
            format_person_display_name(name='ד"ר חיים סעדיה', honorific=""),
            'ד"ר חיים סעדיה',
        )
        self.assertNotEqual(
            format_person_display_name(name='ד"ר חיים סעדיה', honorific=""),
            'חיים סעדיה, ד"ר',
        )

    def test_person_like_uses_attributes(self):
        person = _PersonLike(name="נחום אפנדי", honorific="רב")
        self.assertEqual(person_public_display_name(person), "נחום אפנדי, רב")
