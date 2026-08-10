"""Tests for precision-aware archive date input, storage, display, and form markup."""

from __future__ import annotations

from datetime import date
import json
import re
import shutil
import subprocess
from pathlib import Path
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
        self.assertContains(resp, 'placeholder="MM"')
        self.assertContains(resp, 'id="date_start_month_desktop_month"')

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
        self.assertContains(resp, 'data-date-ui="desktop"')
        self.assertContains(resp, 'data-date-ui="mobile"')
        self.assertContains(resp, 'class="archive-date-control"')
        self.assertContains(resp, "archive_date_entry.js")
        self.assertNotContains(resp, 'type="number"')


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


def _ui_area_html(content: str, ui: str) -> str:
    marker = f'data-date-ui="{ui}"'
    marker_pos = content.find(marker)
    if marker_pos == -1:
        return ""
    div_start = content.rfind("<div", 0, marker_pos)
    if div_start == -1:
        return ""
    nested = 0
    index = div_start
    while index < len(content):
        if content.startswith("<div", index):
            nested += 1
        elif content.startswith("</div>", index):
            nested -= 1
            if nested == 0:
                return content[div_start : index + len("</div>")]
        index += 1
    return content[div_start:]


def _desktop_ui_html(content: str) -> str:
    return _ui_area_html(content, "desktop")


def _mobile_ui_html(content: str) -> str:
    return _ui_area_html(content, "mobile")


def _archive_date_entry_html(content: str) -> str:
    marker = 'id="archiveDateEntry"'
    marker_pos = content.find(marker)
    if marker_pos == -1:
        return ""
    div_start = content.rfind("<div", 0, marker_pos)
    if div_start == -1:
        return ""
    nested = 0
    index = div_start
    while index < len(content):
        if content.startswith("<div", index):
            nested += 1
        elif content.startswith("</div>", index):
            nested -= 1
            if nested == 0:
                return content[div_start : index + len("</div>")]
        index += 1
    return content[div_start:]


def _html_id_values(html: str) -> list[str]:
    return re.findall(r'\bid="([^"]+)"', html)


def _enabled_named_input_count(html: str, field_name: str) -> int:
    count = 0
    for match in re.finditer(
        rf'<input\b[^>]*\bname="{re.escape(field_name)}"[^>]*>',
        html,
        re.IGNORECASE,
    ):
        if re.search(r"\bdisabled\b", match.group(0)):
            continue
        count += 1
    return count


def _enabled_native_input_count(html: str, input_type: str) -> int:
    count = 0
    for match in re.finditer(
        rf"<input\b[^>]*\btype=\"{re.escape(input_type)}\"[^>]*>",
        html,
        re.IGNORECASE,
    ):
        tag = match.group(0)
        if re.search(r"\bdisabled\b", tag):
            continue
        count += 1
    return count


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
    def _precision_group_is_hidden(content: str, precision: str, *, ui: str) -> bool:
        area = (
            _desktop_ui_html(content) if ui == "desktop" else _mobile_ui_html(content)
        )
        match = re.search(
            rf'(<div[^>]*data-date-precision-group="{re.escape(precision)}"[^>]*>)',
            area,
            re.DOTALL,
        )
        if not match:
            return True
        return "hidden" in match.group(1)

    @staticmethod
    def _precision_group_block(content: str, precision: str, *, ui: str) -> str:
        area = (
            _desktop_ui_html(content) if ui == "desktop" else _mobile_ui_html(content)
        )
        match = re.search(
            rf'<div[^>]*data-date-precision-group="{re.escape(precision)}"[^>]*>',
            area,
            re.DOTALL,
        )
        if not match:
            return ""
        start = match.start()
        next_group = re.search(
            r'<div[^>]*data-date-precision-group="',
            area[match.end() :],
        )
        end = match.end() + next_group.start() if next_group else len(area)
        return area[start:end]

    @staticmethod
    def _logical_field_count_in_block(block: str) -> int:
        return block.count('class="archive-date-logical-field"')

    @staticmethod
    def _active_logical_control_count(content: str) -> int:
        mobile = _mobile_ui_html(content)
        if not mobile:
            return 0
        count = 0
        for match in re.finditer(
            r'(<div[^>]*data-date-precision-group="[^"]+"[^>]*>)',
            mobile,
            re.DOTALL,
        ):
            if "hidden" in match.group(1):
                continue
            block_start = match.start()
            next_group = re.search(
                r'<div[^>]*data-date-precision-group="',
                mobile[match.end() :],
            )
            block_end = match.end() + next_group.start() if next_group else len(mobile)
            count += ArchiveDateInitialMarkupTests._logical_field_count_in_block(
                mobile[block_start:block_end]
            )
        return count

    @staticmethod
    def _visible_desktop_precision_fields(content: str, precision: str) -> int:
        if ArchiveDateInitialMarkupTests._precision_group_is_hidden(
            content, precision, ui="desktop"
        ):
            return 0
        block = ArchiveDateInitialMarkupTests._precision_group_block(
            content, precision, ui="desktop"
        )
        return ArchiveDateInitialMarkupTests._logical_field_count_in_block(block)

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

    def _assert_precision_group_state(self, resp, *, active_precision: str | None):
        content = resp.content.decode()
        for precision in (
            "YEAR",
            "MONTH",
            "EXACT_DAY",
            "RANGE_YEAR",
            "RANGE_MONTH",
            "RANGE",
        ):
            should_hide = precision != active_precision
            self.assertEqual(
                self._precision_group_is_hidden(content, precision, ui="desktop"),
                should_hide,
            )
            self.assertEqual(
                self._precision_group_is_hidden(content, precision, ui="mobile"),
                should_hide,
            )

    def test_unknown_initial_markup_hides_and_disables_all_groups(self):
        item = create_manual_text_archive_item(title="Unknown markup", body="x")
        resp = self._get_manual_text_edit(item)
        self.assertEqual(resp.status_code, 200)
        self._assert_precision_group_state(resp, active_precision=None)
        content = resp.content.decode()
        self.assertTrue(
            self._input_tag_has_attr(content, "date_start_year_mobile_year", "disabled")
        )
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_month_mobile_month", "disabled"
            )
        )
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_day_mobile_exact_day", "disabled"
            )
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
        self._assert_precision_group_state(resp, active_precision="YEAR")
        content = resp.content.decode()
        self.assertIn('id="date_start_year_mobile_year"', content)
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_month_mobile_month", "disabled"
            )
        )
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_day_mobile_exact_day", "disabled"
            )
        )

    def test_month_initial_markup_enables_single_year_and_month(self):
        item = create_manual_text_archive_item(
            title="Month markup",
            body="x",
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_precision_group_state(resp, active_precision="MONTH")
        content = resp.content.decode()
        self.assertIn('id="date_start_month_mobile_month"', content)
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_day_mobile_exact_day", "disabled"
            )
        )

    def test_exact_day_initial_markup_enables_all_single_parts(self):
        item = create_manual_text_archive_item(
            title="Exact markup",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_precision_group_state(resp, active_precision="EXACT_DAY")
        content = resp.content.decode()
        self.assertIn('id="date_start_desktop_exact_day"', content)
        self.assertTrue(
            self._input_tag_lacks_attr(
                content, "date_start_desktop_exact_day", "disabled"
            )
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
        self._assert_precision_group_state(resp, active_precision="RANGE_YEAR")
        content = resp.content.decode()
        self.assertIn('id="date_end_year_mobile_range_year"', content)
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_end_month_mobile_range_month", "disabled"
            )
        )
        self.assertTrue(
            self._input_tag_has_attr(
                content, "date_start_month_mobile_range_month", "disabled"
            )
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
        self._assert_precision_group_state(resp, active_precision="RANGE_MONTH")
        content = resp.content.decode()
        self.assertIn('id="date_end_month_mobile_range_month"', content)
        self.assertTrue(
            self._input_tag_has_attr(content, "date_end_day_mobile_range", "disabled")
        )

    def test_range_initial_markup_enables_all_range_parts(self):
        item = create_manual_text_archive_item(
            title="Range exact markup",
            body="x",
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        resp = self._get_manual_text_edit(item)
        self._assert_precision_group_state(resp, active_precision="RANGE")
        content = resp.content.decode()
        self.assertIn('id="date_end_desktop_range"', content)
        self.assertTrue(
            self._input_tag_lacks_attr(content, "date_end_desktop_range", "disabled")
        )
        self.assertEqual(self._active_logical_control_count(content), 2)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "RANGE"),
            2,
        )

    def test_unknown_initial_markup_has_no_active_logical_controls(self):
        item = create_manual_text_archive_item(title="Unknown compact", body="x")
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 0)

    def test_exact_day_renders_one_active_logical_control(self):
        item = create_manual_text_archive_item(
            title="Exact compact",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 1)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "EXACT_DAY"),
            1,
        )
        mobile = _mobile_ui_html(content)
        self.assertIn('dir="ltr"', mobile)
        self.assertIn('aria-label="יום"', mobile)
        self.assertIn('aria-label="חודש"', mobile)
        self.assertIn('aria-label="שנה"', mobile)

    def test_year_renders_one_active_logical_control(self):
        item = create_manual_text_archive_item(
            title="Year compact",
            body="x",
            date_start=date(1954, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 1)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "YEAR"),
            1,
        )

    def test_range_year_renders_two_active_logical_controls(self):
        item = create_manual_text_archive_item(
            title="Range year compact",
            body="x",
            date_start=date(1953, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 2)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "RANGE_YEAR"),
            2,
        )
        self.assertIn("מתאריך", content)
        self.assertIn("עד תאריך", content)

    def test_month_renders_one_active_logical_control(self):
        item = create_manual_text_archive_item(
            title="Month compact",
            body="x",
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 1)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "MONTH"),
            1,
        )

    def test_range_month_renders_two_active_logical_controls(self):
        item = create_manual_text_archive_item(
            title="Range month compact",
            body="x",
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        self.assertEqual(self._active_logical_control_count(content), 2)
        self.assertEqual(
            self._visible_desktop_precision_fields(content, "RANGE_MONTH"),
            2,
        )

    def test_markup_preserves_component_field_names(self):
        item = create_manual_text_archive_item(
            title="Field names",
            body="x",
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        for field_name in (
            "date_start_year",
            "date_start_month",
            "date_start_day",
            "date_end_year",
            "date_end_month",
            "date_end_day",
        ):
            self.assertIn(f'name="{field_name}"', content)

    def test_compact_css_does_not_stack_date_parts_vertically(self):
        css_path = (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        )
        css = css_path.read_text(encoding="utf-8")
        self.assertIn(".archive-date-control", css)
        self.assertNotIn(".archive-date-components", css)
        self.assertNotRegex(
            css,
            r"\.archive-date-(?:components|control)[^{]*\{[^}]*flex-direction:\s*column",
        )

    def test_compact_css_uses_intrinsic_width_and_flex_range(self):
        css_path = (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        )
        css = css_path.read_text(encoding="utf-8")
        archive_date_css = re.search(
            r"/\* Grids \*/.*?\.meta-item\s*\{",
            css,
            re.DOTALL,
        )
        self.assertIsNotNone(archive_date_css)
        block = archive_date_css.group(0)
        self.assertIn("width: fit-content", block)
        self.assertIn("unicode-bidi: isolate", block)
        self.assertNotIn("max-content", block)
        self.assertRegex(
            block,
            r"\.archive-date-group--range\s*\{[^}]*display:\s*flex",
        )
        self.assertRegex(
            block,
            r"\.archive-date-group--range\s*\{[^}]*flex-direction:\s*row",
        )
        base_range_rule = re.search(
            r"\.archive-date-group--range\s*\{[^}]+\}",
            block,
        )
        self.assertIsNotNone(base_range_rule)
        self.assertNotIn("flex-direction: column", base_range_rule.group(0))
        mobile_range_rule = re.search(
            r"@media \(max-width: 640px\)\s*\{[^}]*\.archive-date-group--range\s*\{[^}]+\}",
            block,
            re.DOTALL,
        )
        self.assertIsNotNone(mobile_range_rule)
        self.assertIn("flex-direction: column", mobile_range_rule.group(0))
        self.assertRegex(
            block,
            r"\.archive-date-control \.archive-date-input-year\s*\{[^}]*width:\s*4ch",
        )

    def test_logical_field_does_not_force_full_row_width(self):
        css_path = (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        )
        css = css_path.read_text(encoding="utf-8")
        logical_field_rule = re.search(
            r"\.archive-date-logical-field\s*\{[^}]+\}",
            css,
            re.DOTALL,
        )
        self.assertIsNotNone(logical_field_rule)
        rule = logical_field_rule.group(0)
        self.assertRegex(rule, r"flex:\s*0\s+0\s+auto")
        self.assertNotRegex(rule, r"(?<![a-z-])width:\s*100%")
        self.assertNotRegex(rule, r"flex:\s*1")

    def test_exact_day_control_uses_ltr_isolated_context(self):
        item = create_manual_text_archive_item(
            title="LTR order",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        resp = self._get_manual_text_edit(item)
        content = resp.content.decode()
        exact_day_mobile = ArchiveDateInitialMarkupTests._precision_group_block(
            content,
            "EXACT_DAY",
            ui="mobile",
        )
        self.assertIn('dir="ltr"', content)
        self.assertIn('lang="en"', content)
        day_pos = exact_day_mobile.index('id="date_start_day_mobile_exact_day"')
        month_pos = exact_day_mobile.index('id="date_start_month_mobile_exact_day"')
        year_pos = exact_day_mobile.index('id="date_start_year_mobile_exact_day"')
        self.assertLess(day_pos, month_pos)
        self.assertLess(month_pos, year_pos)

    def _get_range_markup(self):
        item = create_manual_text_archive_item(
            title="Range polish markup",
            body="x",
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        resp = self._get_manual_text_edit(item)
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_compact_placeholders_use_dd_mm_yyyy(self):
        content = self._get_range_markup()
        mobile = _mobile_ui_html(content)
        range_block = ArchiveDateInitialMarkupTests._precision_group_block(
            content,
            "RANGE",
            ui="mobile",
        )
        self.assertNotIn('placeholder="1-31"', content)
        self.assertNotIn('placeholder="1-12"', content)
        self.assertNotIn('placeholder="למשל 1952"', content)
        self.assertEqual(range_block.count('placeholder="DD"'), 2)
        self.assertEqual(range_block.count('placeholder="MM"'), 2)
        self.assertEqual(range_block.count('placeholder="YYYY"'), 2)
        self.assertEqual(mobile.count('placeholder="DD"'), 3)

    def test_logical_controls_use_group_labelling(self):
        content = self._get_range_markup()
        desktop = _desktop_ui_html(content)
        range_mobile = ArchiveDateInitialMarkupTests._precision_group_block(
            content,
            "RANGE",
            ui="mobile",
        )
        self.assertNotIn('<label class="archive-date-group-label"', range_mobile)
        self.assertIn('<label class="archive-date-group-label"', desktop)
        self.assertEqual(range_mobile.count('role="group"'), 2)
        labelled_controls = re.findall(
            r'aria-labelledby="(archive_date_[^"]+)"',
            range_mobile,
        )
        self.assertEqual(len(labelled_controls), 0)
        self.assertIn('aria-label="מתאריך"', range_mobile)
        self.assertIn('aria-label="עד תאריך"', range_mobile)
        self.assertEqual(content.count('id="archive_date_range_start_mobile_label"'), 1)
        self.assertEqual(content.count('id="archive_date_range_end_mobile_label"'), 1)


class ArchiveDateSharedFormLocationTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="date_location_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _assert_compact_date_markup(self, resp):
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        self.assertIn('id="archiveDateEntry"', content)
        self.assertIn('class="archive-date-control"', content)
        self.assertIn('class="archive-date-logical-field"', content)
        self.assertNotContains(resp, 'type="number"')
        self.assertEqual(content.count('id="date_precision"'), 1)

    def test_upload_page_renders_compact_date_control(self):
        self._assert_compact_date_markup(self.client.get("/api/ui/upload/"))

    def test_manual_text_create_renders_compact_date_control(self):
        self._assert_compact_date_markup(
            self.client.get("/archive/manage/new/manual-text/")
        )

    def test_shared_date_component_html_ids_are_unique(self):
        resp = self.client.get("/archive/manage/new/manual-text/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        entry_html = _archive_date_entry_html(content)
        self.assertTrue(entry_html)
        ids = _html_id_values(entry_html)
        self.assertTrue(ids)
        duplicates = sorted({id_value for id_value in ids if ids.count(id_value) > 1})
        self.assertEqual(
            duplicates,
            [],
            msg=f"duplicate archive date ids: {duplicates}",
        )

    def test_photo_create_renders_compact_date_control(self):
        self._assert_compact_date_markup(
            self.client.get("/archive/manage/new/?item_type=photo")
        )

    def test_manual_text_edit_renders_compact_date_control(self):
        item = create_manual_text_archive_item(title="Location edit", body="x")
        self._assert_compact_date_markup(
            self.client.get(f"/archive/manage/{item.id}/edit/")
        )

    def test_ocr_edit_renders_compact_date_control(self):
        doc = create_ocr_document(
            title="Location OCR edit",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
        )
        self._assert_compact_date_markup(
            self.client.get(f"/archive/manage/{doc.archive_item_id}/edit/")
        )

    def test_photo_edit_renders_compact_date_control(self):
        from documents.models import PhotoContent

        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Location photo edit",
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
        self._assert_compact_date_markup(
            self.client.get(f"/archive/manage/{photo_item.id}/edit/")
        )


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

    def test_manual_text_create_loads_date_script_once(self):
        self._assert_single_script_include(
            self.client.get("/archive/manage/new/?item_type=manual_text")
        )

    def test_manual_text_edit_loads_date_script_once(self):
        item = create_manual_text_archive_item(title="Script once", body="x")
        self._assert_single_script_include(
            self.client.get(f"/archive/manage/{item.id}/edit/")
        )


def _archive_date_js_path() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "public"
        / "static"
        / "public"
        / "archive_date_entry.js"
    )


def _sanitize_archive_date_digits_python(raw_value, max_length=None):
    """Mirror of archive_date_entry.js::sanitizeArchiveDateDigits for spec tests."""
    digits = re.sub(r"\D", "", str(raw_value if raw_value is not None else ""))
    if max_length is not None and max_length > 0:
        return digits[:max_length]
    return digits


class ArchiveDateDigitInputScriptTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.js_source = _archive_date_js_path().read_text(encoding="utf-8")

    def test_script_declares_digit_sanitizer_and_input_binding(self):
        self.assertIn("function sanitizeArchiveDateDigits", self.js_source)
        self.assertIn('addEventListener("input"', self.js_source)
        self.assertIn("sanitizeDigits: sanitizeArchiveDateDigits", self.js_source)
        for field_name in (
            "date_start_day",
            "date_start_month",
            "date_start_year",
            "date_end_day",
            "date_end_month",
            "date_end_year",
        ):
            self.assertIn(f'"{field_name}"', self.js_source)

    def test_script_does_not_use_keydown_only_filtering(self):
        self.assertNotIn('addEventListener("keydown"', self.js_source)

    def test_upload_page_includes_dual_representations(self):
        client = Client()
        staff = User.objects.create_user(
            username="date_digit_staff",
            password="test-pass",
            is_staff=True,
        )
        client.force_login(staff)
        resp = client.get("/api/ui/upload/")
        self.assertEqual(resp.status_code, 200)
        content = resp.content.decode()
        desktop = _desktop_ui_html(content)
        mobile = _mobile_ui_html(content)
        self.assertIn("archive_date_entry.js", content)
        self.assertIn('type="date"', desktop)
        self.assertNotIn('type="month"', content)
        self.assertNotIn('type="date"', mobile)
        self.assertNotIn('type="number"', content)
        for field_name in (
            "date_start_day",
            "date_start_month",
            "date_start_year",
            "date_end_day",
            "date_end_month",
            "date_end_year",
        ):
            self.assertIn(f'name="{field_name}"', mobile)

    def test_digit_sanitizer_removes_non_digits(self):
        self.assertEqual(_sanitize_archive_date_digits_python("ab12-3"), "123")
        self.assertEqual(_sanitize_archive_date_digits_python("19x52"), "1952")
        self.assertEqual(_sanitize_archive_date_digits_python("1"), "1")

    def test_digit_sanitizer_respects_maxlength(self):
        self.assertEqual(_sanitize_archive_date_digits_python("123456", 2), "12")
        self.assertEqual(_sanitize_archive_date_digits_python("19x52789", 4), "1952")

    def test_js_sanitize_digits_matches_python_spec(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not available for JavaScript execution checks")
        match = re.search(
            r"function sanitizeArchiveDateDigits\(rawValue, maxLength\)\s*\{.*?\n  \}",
            self.js_source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        script = (
            match.group(0)
            + """
const cases = [
  ["ab12-3", 2, "12"],
  ["19x52", 4, "1952"],
  ["1", 2, "1"],
  ["", 4, ""],
];
for (const [raw, maxLen, expected] of cases) {
  const got = sanitizeArchiveDateDigits(raw, maxLen);
  if (got !== expected) {
    throw new Error(
      `sanitizeDigits(${JSON.stringify(raw)}, ${maxLen}) => ${JSON.stringify(got)}, expected ${JSON.stringify(expected)}`
    );
  }
}
"""
        )
        subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


class ArchiveDateDualUiMarkupTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="dual_ui_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _edit_content(self, item):
        resp = self.client.get(f"/archive/manage/{item.id}/edit/")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode()

    def test_exact_day_desktop_has_native_date_mobile_has_grouped_control(self):
        item = create_manual_text_archive_item(
            title="Dual exact",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        content = self._edit_content(item)
        desktop = _desktop_ui_html(content)
        mobile = _mobile_ui_html(content)
        self.assertEqual(_enabled_native_input_count(desktop, "date"), 1)
        self.assertEqual(_enabled_native_input_count(desktop, "month"), 0)
        self.assertNotIn('type="date"', mobile)
        self.assertEqual(
            ArchiveDateInitialMarkupTests._active_logical_control_count(content),
            1,
        )
        self.assertIn('role="group"', mobile)

    def test_range_desktop_has_two_native_dates(self):
        item = create_manual_text_archive_item(
            title="Dual range",
            body="x",
            date_start=date(1953, 3, 12),
            date_end=date(1954, 4, 19),
            date_precision=ArchiveItem.DatePrecision.RANGE,
        )
        content = self._edit_content(item)
        desktop = _desktop_ui_html(content)
        self.assertEqual(_enabled_native_input_count(desktop, "date"), 2)
        self.assertEqual(
            ArchiveDateInitialMarkupTests._active_logical_control_count(content),
            2,
        )

    def test_month_desktop_has_segmented_month_year_control(self):
        item = create_manual_text_archive_item(
            title="Dual month",
            body="x",
            date_start=date(1948, 5, 1),
            date_end=date(1948, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        content = self._edit_content(item)
        desktop = _desktop_ui_html(content)
        month_block = ArchiveDateInitialMarkupTests._precision_group_block(
            content,
            "MONTH",
            ui="desktop",
        )
        self.assertNotIn('type="month"', desktop)
        self.assertEqual(_enabled_native_input_count(desktop, "month"), 0)
        self.assertEqual(
            ArchiveDateInitialMarkupTests._logical_field_count_in_block(month_block),
            1,
        )
        self.assertIn('placeholder="MM"', month_block)
        self.assertIn('placeholder="YYYY"', month_block)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_month"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_year"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_day"), 0)
        self.assertTrue(
            ArchiveDateInitialMarkupTests._input_tag_lacks_attr(
                month_block, "date_start_month_desktop_month", "disabled"
            )
        )
        self.assertTrue(
            ArchiveDateInitialMarkupTests._input_tag_lacks_attr(
                month_block, "date_start_year_desktop_month", "disabled"
            )
        )

    def test_range_month_desktop_has_two_segmented_month_year_controls(self):
        item = create_manual_text_archive_item(
            title="Dual range month",
            body="x",
            date_start=date(2021, 12, 1),
            date_end=date(2022, 2, 28),
            date_precision=ArchiveItem.DatePrecision.RANGE_MONTH,
        )
        content = self._edit_content(item)
        desktop = _desktop_ui_html(content)
        range_month_block = ArchiveDateInitialMarkupTests._precision_group_block(
            content,
            "RANGE_MONTH",
            ui="desktop",
        )
        self.assertNotIn('type="month"', desktop)
        self.assertEqual(_enabled_native_input_count(desktop, "month"), 0)
        self.assertEqual(
            ArchiveDateInitialMarkupTests._logical_field_count_in_block(
                range_month_block
            ),
            2,
        )
        self.assertEqual(range_month_block.count('placeholder="MM"'), 2)
        self.assertEqual(range_month_block.count('placeholder="YYYY"'), 2)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_month"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_year"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_end_month"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_end_year"), 1)
        self.assertEqual(_enabled_named_input_count(desktop, "date_start_day"), 0)
        self.assertEqual(_enabled_named_input_count(desktop, "date_end_day"), 0)

    def test_year_desktop_uses_numeric_year_not_native_date(self):
        item = create_manual_text_archive_item(
            title="Dual year",
            body="x",
            date_start=date(1954, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        content = self._edit_content(item)
        desktop = _desktop_ui_html(content)
        self.assertEqual(_enabled_native_input_count(desktop, "date"), 0)
        self.assertEqual(_enabled_native_input_count(desktop, "month"), 0)
        self.assertIn('name="date_start_year"', desktop)

    def test_mobile_area_initially_disabled(self):
        item = create_manual_text_archive_item(
            title="Dual disabled mobile",
            body="x",
            date_start=date(1952, 3, 12),
            date_end=date(1952, 3, 12),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        content = self._edit_content(item)
        self.assertRegex(content, r'data-date-ui="mobile"[^>]*\bhidden\b')
        self.assertTrue(
            ArchiveDateInitialMarkupTests._input_tag_has_attr(
                content, "date_start_day_mobile_exact_day", "disabled"
            )
        )

    def test_script_uses_matchmedia_for_responsive_mode(self):
        js_source = _archive_date_js_path().read_text(encoding="utf-8")
        self.assertIn("matchMedia", js_source)
        self.assertIn("(max-width: 640px)", js_source)
        self.assertIn("prepareSubmission", js_source)
