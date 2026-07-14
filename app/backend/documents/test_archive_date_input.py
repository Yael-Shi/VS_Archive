"""Tests for precision-aware archive date input, storage, display, and form markup."""

from __future__ import annotations

from datetime import date
import json
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import Client, TestCase, override_settings

from documents.models import ArchiveItem, Document
from documents.services.archive_date_input import (
    date_component_form_data_from_stored,
    parse_archive_date_bounds,
    scalar_post_field,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.archive_metadata_validation import parse_archive_metadata_form
from documents.services.document_date import NO_DATE_LABEL, format_document_date


class ArchiveDateScalarPostFieldTests(TestCase):
    def test_empty_then_non_empty_duplicate_returns_active_value(self):
        qd = QueryDict(mutable=True)
        qd.setlist("date_start_year", ["", "2020"])
        self.assertEqual(scalar_post_field(qd, "date_start_year"), "2020")

    def test_non_empty_then_empty_duplicate_returns_active_value(self):
        qd = QueryDict(mutable=True)
        qd.setlist("date_start_year", ["2020", ""])
        self.assertEqual(scalar_post_field(qd, "date_start_year"), "2020")

    def test_distinct_non_empty_duplicates_raise_clear_error(self):
        qd = QueryDict(mutable=True)
        qd.setlist("date_start_year", ["2020", "2021"])
        with self.assertRaisesRegex(
            ValueError,
            "ambiguous date_start_year, conflicting values submitted",
        ):
            scalar_post_field(qd, "date_start_year")

    def test_ambiguous_duplicate_surfaces_through_bounds_parser(self):
        qd = QueryDict(mutable=True)
        qd.update(
            {
                "date_precision": ArchiveItem.DatePrecision.YEAR,
                "date_start_year": "2020",
            }
        )
        qd.appendlist("date_start_year", "2021")
        _, _, _, errors = parse_archive_date_bounds(
            date_precision=ArchiveItem.DatePrecision.YEAR,
            post_data=qd,
        )
        self.assertEqual(
            errors,
            ["ambiguous date_start_year, conflicting values submitted"],
        )

    def test_ambiguous_duplicate_surfaces_through_metadata_form_parser(self):
        qd = QueryDict(mutable=True)
        qd.update(
            {
                "title": "Ambiguous date",
                "visibility": ArchiveItem.Visibility.PUBLIC,
                "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
                "date_precision": ArchiveItem.DatePrecision.YEAR,
                "date_start_year": "2020",
            }
        )
        qd.appendlist("date_start_year", "2021")
        _, errors = parse_archive_metadata_form(qd)
        self.assertEqual(
            errors,
            ["ambiguous date_start_year, conflicting values submitted"],
        )


class ArchiveDateInputParsingTests(TestCase):
    def _bounds(self, precision: str, **fields):
        start, end, components, errors = parse_archive_date_bounds(
            date_precision=precision,
            post_data=fields,
        )
        return start, end, components, errors

    def test_exact_day_parses_and_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.EXACT_DAY,
            date_start_year="1952",
            date_start_month="3",
            date_start_day="12",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1952, 3, 12))
        self.assertEqual(end, date(1952, 3, 12))

    def test_month_parses_and_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.MONTH,
            date_start_year="2021",
            date_start_month="12",
            date_start_day="99",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(2021, 12, 1))
        self.assertEqual(end, date(2021, 12, 31))

    def test_year_parses_and_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.YEAR,
            date_start_year="1954",
            date_start_month="6",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1954, 1, 1))
        self.assertEqual(end, date(1954, 12, 31))

    def test_exact_day_range_parses_exact_boundaries(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE,
            date_start_year="1953",
            date_start_month="3",
            date_start_day="12",
            date_end_year="1954",
            date_end_month="4",
            date_end_day="19",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1953, 3, 12))
        self.assertEqual(end, date(1954, 4, 19))

    def test_month_range_within_one_year_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE_MONTH,
            date_start_year="1954",
            date_start_month="3",
            date_end_year="1954",
            date_end_month="6",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1954, 3, 1))
        self.assertEqual(end, date(1954, 6, 30))

    def test_month_range_crossing_years_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE_MONTH,
            date_start_year="2021",
            date_start_month="12",
            date_end_year="2022",
            date_end_month="2",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(2021, 12, 1))
        self.assertEqual(end, date(2022, 2, 28))

    def test_year_range_normalizes(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE_YEAR,
            date_start_year="1953",
            date_end_year="1954",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1953, 1, 1))
        self.assertEqual(end, date(1954, 12, 31))

    def test_invalid_month_rejected(self):
        _, _, _, errors = self._bounds(
            ArchiveItem.DatePrecision.MONTH,
            date_start_year="1952",
            date_start_month="13",
        )
        self.assertIn("invalid date_start_month, expected 1-12", errors)

    def test_invalid_day_rejected(self):
        _, _, _, errors = self._bounds(
            ArchiveItem.DatePrecision.EXACT_DAY,
            date_start_year="1952",
            date_start_month="2",
            date_start_day="30",
        )
        self.assertTrue(any("invalid date_start_day" in err for err in errors))

    def test_reversed_range_rejected(self):
        _, _, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE_YEAR,
            date_start_year="1960",
            date_end_year="1950",
        )
        self.assertIn("date_end must not be before date_start", errors)

    def test_missing_required_year_rejected(self):
        _, _, _, errors = self._bounds(
            ArchiveItem.DatePrecision.YEAR,
            date_start_month="5",
        )
        self.assertIn("date_start_year is required", errors)

    def test_unknown_clears_bounds_and_ignores_components(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.UNKNOWN,
            date_start_year="1952",
            date_start_month="1",
            date_start_day="1",
        )
        self.assertEqual(errors, [])
        self.assertIsNone(start)
        self.assertIsNone(end)

    def test_legacy_iso_fields_map_into_components(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.RANGE,
            date_start="1953-03-12",
            date_end="1954-04-19",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1953, 3, 12))
        self.assertEqual(end, date(1954, 4, 19))

    def test_legacy_iso_with_empty_segmented_keys_still_parses(self):
        start, end, _, errors = self._bounds(
            ArchiveItem.DatePrecision.YEAR,
            date_start="1930-01-01",
            date_end="1935-12-31",
            date_start_year="",
            date_start_month="",
            date_start_day="",
            date_end_year="",
            date_end_month="",
            date_end_day="",
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1930, 1, 1))
        self.assertEqual(end, date(1930, 12, 31))

    def test_edit_form_repopulation_from_stored_month_range(self):
        components = date_component_form_data_from_stored(
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        self.assertEqual(components["date_start_year"], "2021")
        self.assertEqual(components["date_start_month"], "12")
        self.assertEqual(components["date_start_day"], "")
        self.assertEqual(components["date_end_year"], "2022")
        self.assertEqual(components["date_end_month"], "2")
        self.assertEqual(components["date_end_day"], "")


class ArchiveDateDisplayTests(TestCase):
    def test_display_exact_day(self):
        item = ArchiveItem(
            date_start=date(1953, 3, 12),
            date_end=date(1953, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        self.assertEqual(format_document_date(item), "12/03/1953")

    def test_display_month(self):
        item = ArchiveItem(
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        self.assertEqual(format_document_date(item), "05/1948")

    def test_display_year(self):
        item = ArchiveItem(
            date_start=date(1948, 1, 1),
            date_end=date(1948, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        self.assertEqual(format_document_date(item), "1948")

    def test_display_exact_day_range_keeps_existing_semantics(self):
        item = ArchiveItem(
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        self.assertEqual(format_document_date(item), "12/03/1953 - 19/04/1954")

    def test_display_month_range_without_day_leakage(self):
        item = ArchiveItem(
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        label = format_document_date(item)
        self.assertEqual(label, "12/2021 - 02/2022")
        self.assertNotIn("01/12/2021", label)
        self.assertNotIn("28/02/2022", label)

    def test_display_year_range_without_day_leakage(self):
        item = ArchiveItem(
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        label = format_document_date(item)
        self.assertEqual(label, "1953 - 1954")
        self.assertNotIn("01/01/1953", label)
        self.assertNotIn("31/12/1954", label)

    def test_display_unknown(self):
        item = ArchiveItem(
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        )
        self.assertEqual(format_document_date(item), NO_DATE_LABEL)


class ArchiveDateFormIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="archive_date_staff",
            password="test-pass",
            is_staff=True,
        )

    def test_manual_text_create_persists_year_range(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            "/archive/manage/new/manual-text/",
            data={
                "title": "Year range manual text",
                "body": "content",
                "visibility": ArchiveItem.Visibility.PUBLIC,
                "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                "date_precision": ArchiveItem.DatePrecision.RANGE_YEAR,
                "date_start_year": "1953",
                "date_end_year": "1954",
            },
        )
        self.assertEqual(resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Year range manual text")
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.RANGE_YEAR)
        self.assertEqual(item.date_start, date(1953, 1, 1))
        self.assertEqual(item.date_end, date(1954, 12, 31))

    def test_manual_text_edit_form_repopulates_month_precision(self):
        item = create_manual_text_archive_item(
            title="Month edit repopulation",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/manage/{item.id}/edit/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="date_start_year"')
        self.assertContains(resp, 'value="1948"', html=False)
        self.assertContains(resp, 'name="date_start_month"')
        self.assertContains(resp, 'value="5"', html=False)
        self.assertNotContains(resp, 'type="date"')

    def test_public_detail_shows_month_range_label(self):
        item = create_manual_text_archive_item(
            title="Public month range",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "12/2021 - 02/2022")
        self.assertNotContains(resp, "01/12/2021")

    def test_staff_manage_list_shows_year_range_label(self):
        item = create_manual_text_archive_item(
            title="Manage year range",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "1953 - 1954")

    def test_upload_page_uses_typed_date_markup(self):
        self.client.force_login(self.staff)
        resp = self.client.get("/api/ui/upload/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'name="date_start_year"')
        self.assertContains(resp, 'inputmode="numeric"')
        self.assertContains(resp, "archive-date-entry")
        self.assertContains(resp, "archive_date_entry.js")
        self.assertNotContains(resp, 'type="date"')


class ArchiveDateMetadataFormTests(TestCase):
    def test_parse_archive_metadata_form_normalizes_month(self):
        parsed, errors = parse_archive_metadata_form(
            {
                "title": "Month metadata",
                "visibility": ArchiveItem.Visibility.PUBLIC,
                "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                "date_precision": ArchiveItem.DatePrecision.MONTH,
                "date_start_year": "1948",
                "date_start_month": "5",
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(parsed["date_start_value"], date(1948, 5, 1))
        self.assertEqual(parsed["date_end_value"], date(1948, 5, 31))

    def test_validation_error_preserves_entered_components(self):
        parsed, errors = parse_archive_metadata_form(
            {
                "title": "Bad month",
                "visibility": ArchiveItem.Visibility.PUBLIC,
                "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                "date_precision": ArchiveItem.DatePrecision.MONTH,
                "date_start_year": "1948",
                "date_start_month": "13",
            }
        )
        self.assertTrue(errors)
        self.assertEqual(parsed["date_start_year"], "1948")
        self.assertEqual(parsed["date_start_month"], "13")


class OcrDocumentDateDisplayTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="ocr_date_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_ocr_detail_uses_shared_formatter_for_year_range(self):
        doc = create_ocr_document(
            title="OCR year range",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        resp = self.client.get(f"/api/ui/documents/{doc.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "1953 - 1954")
        self.assertNotContains(resp, "01/01/1953")


def _count_substrings(content: bytes, needle: str) -> int:
    return content.decode().count(needle)


class ArchiveDateDuplicateControlTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_dup_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _assert_single_date_precision_control(self, resp):
        self.assertEqual(resp.status_code, 200)
        content = resp.content
        self.assertEqual(_count_substrings(content, 'id="date_precision"'), 1)
        self.assertEqual(_count_substrings(content, 'name="date_precision"'), 1)

    def test_upload_page_has_single_date_precision_control(self):
        self._assert_single_date_precision_control(self.client.get("/api/ui/upload/"))

    def test_manual_text_form_has_single_date_precision_control(self):
        self._assert_single_date_precision_control(
            self.client.get("/archive/manage/new/manual-text/")
        )

    def test_ocr_edit_form_has_single_date_precision_control(self):
        doc = create_ocr_document(
            title="OCR dup control",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self._assert_single_date_precision_control(
            self.client.get(f"/archive/manage/{doc.archive_item_id}/edit/")
        )

    def test_photo_edit_form_has_single_date_precision_control(self):
        from documents.models import PhotoContent

        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo dup control",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        PhotoContent.objects.create(
            archive_item=photo_item,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=100,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self._assert_single_date_precision_control(
            self.client.get(f"/archive/manage/{photo_item.id}/edit/")
        )


class ArchiveDateInitialMarkupTests(TestCase):
    @staticmethod
    def _date_group_is_hidden(content: str, group: str) -> bool:
        match = re.search(
            rf'(<div[^>]*class="archive-date-group archive-date-group--{group}"[^>]*>)',
            content,
            re.DOTALL,
        )
        if not match:
            return False
        return "hidden" in match.group(1)

    @staticmethod
    def _input_tag_for_id(content: str, input_id: str):
        return re.search(
            rf'<input\b[^>]*\bid="{re.escape(input_id)}"[^>]*>',
            content,
            re.DOTALL,
        )

    @staticmethod
    def _input_tag_has_attr(content: str, input_id: str, attr: str) -> bool:
        match = ArchiveDateInitialMarkupTests._input_tag_for_id(content, input_id)
        if not match:
            return False
        return re.search(rf"\b{re.escape(attr)}\b", match.group(0)) is not None

    @staticmethod
    def _input_tag_lacks_attr(content: str, input_id: str, attr: str) -> bool:
        match = ArchiveDateInitialMarkupTests._input_tag_for_id(content, input_id)
        if not match:
            return False
        return re.search(rf"\b{re.escape(attr)}\b", match.group(0)) is None

    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_markup_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _get_manual_text_edit(self, item):
        return self.client.get(f"/archive/manage/{item.id}/edit/")

    def _assert_group_state(self, resp, *, single_active: bool, range_active: bool):
        content = resp.content.decode()
        if single_active:
            self.assertFalse(self._date_group_is_hidden(content, "single"))
        else:
            self.assertTrue(self._date_group_is_hidden(content, "single"))

        if range_active:
            self.assertFalse(self._date_group_is_hidden(content, "range"))
        else:
            self.assertTrue(self._date_group_is_hidden(content, "range"))

    def test_unknown_initial_markup_hides_and_disables_all_groups(self):
        item = create_manual_text_archive_item(title="Unknown markup", body="x")
        resp = self._get_manual_text_edit(item)
        self.assertEqual(resp.status_code, 200)
        self._assert_group_state(resp, single_active=False, range_active=False)
        content = resp.content.decode()
        self.assertTrue(
            self._input_tag_has_attr(content, "date_start_year", "disabled")
        )

    def test_year_initial_markup_enables_single_year_only(self):
        item = create_manual_text_archive_item(
            title="Year markup",
            body="x",
            date_start=date(1954, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=True, range_active=False)
        content = resp.content.decode()
        self.assertIn('id="date_start_year"', content)
        self.assertTrue(
            self._input_tag_has_attr(content, "date_start_month", "disabled")
        )
        self.assertTrue(self._input_tag_has_attr(content, "date_start_day", "disabled"))

    def test_month_initial_markup_enables_single_year_and_month(self):
        item = create_manual_text_archive_item(
            title="Month markup",
            body="x",
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=True, range_active=False)
        content = resp.content.decode()
        self.assertIn('id="date_start_month"', content)
        self.assertTrue(self._input_tag_has_attr(content, "date_start_day", "disabled"))

    def test_exact_day_initial_markup_enables_all_single_parts(self):
        item = create_manual_text_archive_item(
            title="Exact markup",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=True, range_active=False)
        content = resp.content.decode()
        self.assertIn('id="date_start_day"', content)
        self.assertTrue(
            self._input_tag_lacks_attr(content, "date_start_day", "disabled")
        )

    def test_range_year_initial_markup_enables_range_year_only(self):
        item = create_manual_text_archive_item(
            title="Range year markup",
            body="x",
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=False, range_active=True)
        content = resp.content.decode()
        self.assertIn('id="date_end_year"', content)
        self.assertTrue(self._input_tag_has_attr(content, "date_end_month", "disabled"))
        self.assertTrue(
            self._input_tag_has_attr(content, "date_start_month_range", "disabled")
        )

    def test_range_month_initial_markup_enables_range_month_parts(self):
        item = create_manual_text_archive_item(
            title="Range month markup",
            body="x",
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=False, range_active=True)
        content = resp.content.decode()
        self.assertIn('id="date_end_month"', content)
        self.assertTrue(self._input_tag_has_attr(content, "date_end_day", "disabled"))

    def test_range_initial_markup_enables_all_range_parts(self):
        item = create_manual_text_archive_item(
            title="Range exact markup",
            body="x",
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_group_state(resp, single_active=False, range_active=True)
        content = resp.content.decode()
        self.assertIn('id="date_end_day"', content)
        self.assertTrue(self._input_tag_lacks_attr(content, "date_end_day", "disabled"))


class ArchiveDateUploadIntegrationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_upload_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    def test_ocr_upload_create_persists_range_year(self, _mock_put):
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(
                {
                    "title": "OCR range year upload",
                    "doc_type": "IMAGE",
                    "text_input_type": "HANDWRITTEN",
                    "original_name": "scan.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1000,
                    "date_precision": ArchiveItem.DatePrecision.RANGE_YEAR,
                    "date_start_year": "1953",
                    "date_end_year": "1954",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        item = doc.archive_item
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.RANGE_YEAR)
        self.assertEqual(item.date_start, date(1953, 1, 1))
        self.assertEqual(item.date_end, date(1954, 12, 31))

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    def test_ocr_upload_create_persists_range_month(self, _mock_put):
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(
                {
                    "title": "OCR range month upload",
                    "doc_type": "IMAGE",
                    "text_input_type": "HANDWRITTEN",
                    "original_name": "scan.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1000,
                    "date_precision": ArchiveItem.DatePrecision.RANGE_MONTH,
                    "date_start_year": "2021",
                    "date_start_month": "12",
                    "date_end_year": "2022",
                    "date_end_month": "2",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        item = Document.objects.get(id=resp.json()["document_id"]).archive_item
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.RANGE_MONTH)
        self.assertEqual(item.date_start, date(2021, 12, 1))
        self.assertEqual(item.date_end, date(2022, 2, 28))

    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    def test_ocr_upload_create_persists_month_bounds(self, _mock_put):
        resp = self.client.post(
            "/api/uploads/create/",
            data=json.dumps(
                {
                    "title": "OCR month upload",
                    "doc_type": "IMAGE",
                    "text_input_type": "HANDWRITTEN",
                    "original_name": "scan.jpg",
                    "mime_type": "image/jpeg",
                    "size_bytes": 1000,
                    "date_precision": ArchiveItem.DatePrecision.MONTH,
                    "date_start_year": "1948",
                    "date_start_month": "5",
                    "date_end_year": "1999",
                    "date_end_month": "12",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 201)
        item = Document.objects.get(id=resp.json()["document_id"]).archive_item
        self.assertEqual(item.date_precision, ArchiveItem.DatePrecision.MONTH)
        self.assertEqual(item.date_start, date(1948, 5, 1))
        self.assertEqual(item.date_end, date(1948, 5, 31))

    def test_year_precision_ignores_stale_range_component_fields(self):
        start, end, _, errors = parse_archive_date_bounds(
            date_precision=ArchiveItem.DatePrecision.YEAR,
            post_data={
                "date_start_year": "1930",
                "date_end_year": "1935",
                "date_end_month": "12",
            },
        )
        self.assertEqual(errors, [])
        self.assertEqual(start, date(1930, 1, 1))
        self.assertEqual(end, date(1930, 12, 31))


class ArchiveDateScriptIncludeTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_script_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _assert_single_script_include(self, resp):
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            _count_substrings(resp.content, "archive_date_entry.js"),
            1,
        )

    def test_upload_page_loads_date_script_once(self):
        self._assert_single_script_include(self.client.get("/api/ui/upload/"))

    def test_manual_text_edit_loads_date_script_once(self):
        item = create_manual_text_archive_item(title="Script once", body="x")
        self._assert_single_script_include(
            self.client.get(f"/archive/manage/{item.id}/edit/")
        )
