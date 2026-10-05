"""Parser tests for the v3 non-person entity review preflight."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)
from documents.services import non_person_entity_review_preflight as preflight_module
from documents.services.non_person_entity_review_preflight import (
    AUTHORITATIVE_WORKBOOK_SHA256,
    FINAL_SHEET_NAME,
    GERMAN_REICH_MINISTRY_CANONICAL,
    REQUIRED_COLUMNS,
    ApplyBlocker,
    PreflightError,
    ReviewedNonPersonEntityRow,
    format_preflight_report,
    preflight_authoritative_workbook,
    preflight_workbook,
    read_final_sheet_rows,
    sha256_path,
    validate_reviewed_rows,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def _row(
    candidate_id: str,
    final_decision: str,
    *,
    surface: str = "",
    final_canonical: str = "",
    merge_target: str = "",
    final_notes: str = "",
    status: str = ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
    entity_type: str = "",
    entity_subtype: str = "",
) -> ReviewedNonPersonEntityRow:
    return ReviewedNonPersonEntityRow(
        candidate_id=candidate_id,
        surface=surface,
        final_decision=final_decision,
        final_canonical=final_canonical,
        merge_target=merge_target,
        final_notes=final_notes,
        status=status,
        entity_type=entity_type,
        entity_subtype=entity_subtype,
    )


def valid_rows() -> list[ReviewedNonPersonEntityRow]:
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    specials = {
        "EC0006": ("ארץ ישראל", "PLACE", "REGION_OR_HISTORICAL_AREA"),
        "EC0219": ("פלסטינה", "PLACE", "REGION_OR_HISTORICAL_AREA"),
    }
    approve_ids = [
        "EC0006",
        "EC0219",
        *[f"EC{number:04d}" for number in range(100, 166)],
    ]
    rows: list[ReviewedNonPersonEntityRow] = []
    for index, candidate_id in enumerate(approve_ids):
        if candidate_id in specials:
            canonical, entity_type, entity_subtype = specials[candidate_id]
        else:
            canonical = f"canonical-{candidate_id}"
            entity_type = NonPersonEntity.EntityType.PLACE
            entity_subtype = (
                "" if index % 5 == 0 else NonPersonEntity.EntitySubtype.CITY
            )
        rows.append(
            _row(
                candidate_id,
                approve,
                surface=candidate_id,
                final_canonical=canonical,
                entity_type=entity_type,
                entity_subtype=entity_subtype,
            )
        )

    for index in range(28):
        target = rows[index]
        rows.append(
            _row(
                f"EC2{index:03d}",
                ReviewedNonPersonEntityDecision.Decision.MERGE,
                surface=f"surface-{index}",
                final_canonical=target.final_canonical,
                merge_target=target.candidate_id,
            )
        )
    for index in range(8):
        rows.append(
            _row(
                f"EC3{index:03d}",
                ReviewedNonPersonEntityDecision.Decision.SKIP,
            )
        )
    rows.append(
        _row(
            "EC0009",
            ReviewedNonPersonEntityDecision.Decision.SPLIT,
            surface="Palestine",
        )
    )
    rows.append(
        _row(
            "EC0045",
            ReviewedNonPersonEntityDecision.Decision.SPLIT,
            surface="ministère de la propagande",
        )
    )
    rows.append(
        _row(
            "EC0025",
            ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH,
            status=ReviewedNonPersonEntityDecision.ReviewStatus.UNRESOLVED,
        )
    )
    return rows


def _replace_candidate(
    rows: list[ReviewedNonPersonEntityRow],
    candidate_id: str,
    **changes,
) -> list[ReviewedNonPersonEntityRow]:
    replaced = []
    for row in rows:
        if row.candidate_id == candidate_id:
            replaced.append(replace(row, **changes))
        else:
            replaced.append(row)
    return replaced


def _errors(rows: list[ReviewedNonPersonEntityRow]) -> str:
    try:
        validate_reviewed_rows(rows)
    except PreflightError as exc:
        return str(exc)
    raise AssertionError("expected PreflightError")


def _worksheet_xml(
    rows: list[list[tuple[str, str]]],
    *,
    shared_rows: list[list[tuple[str, str]]] | None = None,
) -> str:
    body = []
    for row_number, cells in enumerate(rows, start=1):
        rendered = []
        for letter, value in cells:
            text = escape(value)
            rendered.append(
                f'<c r="{letter}{row_number}" t="inlineStr"><is><t>{text}</t></is></c>'
            )
        joined = "".join(rendered)
        body.append(f'<row r="{row_number}">{joined}</row>')
    offset = len(rows)
    for extra_index, cells in enumerate(shared_rows or [], start=1):
        row_number = offset + extra_index
        rendered = []
        for letter, value in cells:
            rendered.append(
                f'<c r="{letter}{row_number}" t="s"><v>{escape(value)}</v></c>'
            )
        body.append(f'<row r="{row_number}">{"".join(rendered)}</row>')
    sheet_rows = "".join(body)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}"><sheetData>{sheet_rows}</sheetData></worksheet>'
    )


def _shared_strings_xml(values: list[str]) -> str:
    items = "".join(f"<si><t>{escape(value)}</t></si>" for value in values)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_MAIN_NS}">{items}</sst>'
    )


def _write_workbook(
    path: Path,
    final_rows: list[list[tuple[str, str]]],
    *,
    include_decoy: bool = False,
    shared_string_rows: list[list[tuple[str, str]]] | None = None,
    shared_strings: list[str] | None = None,
) -> None:
    sheets = [
        (
            FINAL_SHEET_NAME,
            "worksheets/sheet1.xml",
            _worksheet_xml(final_rows, shared_rows=shared_string_rows),
        )
    ]
    if include_decoy:
        decoy = [
            [
                (letter, column)
                for letter, column in zip("ABCDEFGHI", REQUIRED_COLUMNS, strict=True)
            ],
            [
                ("A", "EC9999"),
                ("B", "decoy"),
                ("C", "APPROVE"),
                ("D", "decoy canonical"),
                ("G", "CLOSED"),
                ("H", "PLACE"),
                ("I", "CITY"),
            ],
        ]
        sheets.append(
            ("approved_decisions_log", "worksheets/sheet2.xml", _worksheet_xml(decoy))
        )

    content_types = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    ]
    for index in range(len(sheets)):
        content_types.append(
            "<Override "
            f'PartName="/xl/worksheets/sheet{index + 1}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
    content_types.append("</Types>")

    workbook = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_OFFICE_REL_NS}"><sheets>',
    ]
    rels = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{_PKG_REL_NS}">',
    ]
    for index, (name, _target, _xml) in enumerate(sheets, start=1):
        workbook.append(
            f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        )
        rels.append(
            f'<Relationship Id="rId{index}" Type="{_OFFICE_REL_NS}/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
    workbook.append("</sheets></workbook>")
    rels.append("</Relationships>")
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_OFFICE_REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "".join(content_types))
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", "".join(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
        for _name, target, xml in sheets:
            archive.writestr(f"xl/{target}", xml)
        if shared_strings is not None:
            archive.writestr(
                "xl/sharedStrings.xml", _shared_strings_xml(shared_strings)
            )


def _cells_for(row: ReviewedNonPersonEntityRow) -> list[tuple[str, str]]:
    values = (
        row.candidate_id,
        row.surface,
        row.final_decision,
        row.final_canonical,
        row.merge_target,
        row.final_notes,
        row.status,
        row.entity_type,
        row.entity_subtype,
    )
    return [
        (letter, value)
        for letter, value in zip("ABCDEFGHI", values, strict=True)
        if value != ""
    ]


def _write_valid_workbook(path: Path, rows: list[ReviewedNonPersonEntityRow]) -> None:
    header = list(zip("ABCDEFGHI", REQUIRED_COLUMNS, strict=True))
    preamble = [("A", "not a reviewed row"), ("B", "EC9999 should not be read")]
    table = [preamble, header, *[_cells_for(row) for row in rows]]
    _write_workbook(path, table, include_decoy=True)


class NonPersonEntityReviewPreflightTests(SimpleTestCase):
    def test_authoritative_sha_constant(self):
        self.assertEqual(
            AUTHORITATIVE_WORKBOOK_SHA256,
            "fe59efd0e88f447ffdcdfd80e34c4f34686674480d2bb426154ee1f41d6ed03d",
        )

    def test_valid_fixture_counts_and_preserves_raw_values(self):
        rows = valid_rows()
        result = validate_reviewed_rows(rows)

        self.assertTrue(result.parser_valid)
        self.assertEqual(len(result.rows), 107)
        self.assertEqual(
            result.decision_counts(),
            {
                "APPROVE": 68,
                "MERGE": 28,
                "SKIP": 8,
                "SPLIT": 2,
                "NEEDS_RESEARCH": 1,
            },
        )
        palestine = result.rows[-3]
        self.assertEqual(palestine.candidate_id, "EC0009")
        self.assertEqual(palestine.surface, "Palestine")
        self.assertEqual(palestine.final_notes, "")
        self.assertEqual(len(result.entity_creation_candidate_ids), 68)
        self.assertIn("EC0006", result.entity_creation_candidate_ids)
        self.assertIn("EC0219", result.entity_creation_candidate_ids)
        for candidate_id in ("EC0009", "EC0045", "EC0025", "EC3000", "EC2000"):
            self.assertNotIn(candidate_id, result.entity_creation_candidate_ids)
        self.assertEqual(
            [item.candidate_id for item in result.split_apply_readiness],
            ["EC0009", "EC0045"],
        )
        self.assertTrue(
            all(item.apply_ready is False for item in result.split_apply_readiness)
        )

    def test_workbook_parse_uses_final_sheet_only(self):
        rows = valid_rows()
        path = self._workbook_path("valid.xlsx")
        _write_valid_workbook(path, rows)

        result = preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertEqual(result.workbook_sha256, sha256_path(path))
        self.assertEqual(result.rows, tuple(rows))
        self.assertNotIn("EC9999", {row.candidate_id for row in result.rows})
        self.assertIn("parser_valid: yes", format_preflight_report(result))
        self.assertIn("split_apply_ready: 0", format_preflight_report(result))

    def test_sha_mismatch_rejected(self):
        path = self._workbook_path("mismatch.xlsx")
        _write_valid_workbook(path, valid_rows())

        with self.assertRaises(PreflightError) as caught:
            preflight_authoritative_workbook(path)

        self.assertIn("SHA256_MISMATCH", str(caught.exception))

    def test_command_rejects_sha_mismatch(self):
        path = self._workbook_path("command.xlsx")
        _write_valid_workbook(path, valid_rows())

        with self.assertRaises(CommandError) as caught:
            call_command("non_person_entity_review_preflight", workbook=str(path))

        self.assertIn("SHA256_MISMATCH", str(caught.exception))

    def test_inline_final_does_not_read_shared_strings(self):
        path = self._workbook_path("inline.xlsx")
        header = list(zip("ABCDEFGHI", REQUIRED_COLUMNS, strict=True))
        _write_workbook(path, [header])
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("xl/sharedStrings.xml", "not-xml")
            archive.writestr(
                "xl/worksheets/sheet9.xml",
                "<worksheet>do not read</worksheet>",
            )

        read_names: list[str] = []

        class TrackingZipFile(zipfile.ZipFile):
            def read(self, name, pwd=None):
                read_names.append(name if isinstance(name, str) else name.decode())
                return super().read(name, pwd)

        with patch.object(preflight_module.zipfile, "ZipFile", TrackingZipFile):
            rows = read_final_sheet_rows(path)

        self.assertEqual(rows, ())
        self.assertIn("xl/worksheets/sheet1.xml", read_names)
        self.assertNotIn("xl/sharedStrings.xml", read_names)
        self.assertNotIn("xl/worksheets/sheet9.xml", read_names)

    def test_final_shared_string_cells_are_decoded(self):
        path = self._workbook_path("shared.xlsx")
        strings = [
            "EC0001",
            "Palestine",
            "APPROVE",
            "פלסטינה",
            "CLOSED",
            "PLACE",
            "REGION_OR_HISTORICAL_AREA",
        ]
        header = list(zip("ABCDEFGHI", REQUIRED_COLUMNS, strict=True))
        data = [
            ("A", "0"),
            ("B", "1"),
            ("C", "2"),
            ("D", "3"),
            ("G", "4"),
            ("H", "5"),
            ("I", "6"),
        ]
        _write_workbook(
            path, [header], shared_string_rows=[data], shared_strings=strings
        )

        rows = read_final_sheet_rows(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].candidate_id, "EC0001")
        self.assertEqual(rows[0].surface, "Palestine")
        self.assertEqual(rows[0].final_decision, "APPROVE")
        self.assertEqual(rows[0].final_canonical, "פלסטינה")
        self.assertEqual(rows[0].merge_target, "")
        self.assertEqual(rows[0].final_notes, "")
        self.assertEqual(rows[0].status, "CLOSED")
        self.assertEqual(rows[0].entity_type, "PLACE")
        self.assertEqual(rows[0].entity_subtype, "REGION_OR_HISTORICAL_AREA")

    def test_missing_required_column_rejected(self):
        path = self._workbook_path("missing-column.xlsx")
        header = [
            (letter, column)
            for letter, column in zip("ABCDEFGH", REQUIRED_COLUMNS[:-1], strict=True)
        ]
        _write_workbook(path, [header])

        with self.assertRaises(PreflightError) as caught:
            preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertIn("MISSING_COLUMN: entity_subtype", str(caught.exception))

    def test_duplicate_candidate_id_rejected(self):
        rows = valid_rows()
        rows[1] = replace(rows[1], candidate_id=rows[0].candidate_id)

        self.assertIn("DUPLICATE_CANDIDATE_ID: EC0006", _errors(rows))

    def test_wrong_counts_rejected(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC0165",
            final_decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
            final_canonical="",
            entity_type="",
            entity_subtype="",
        )

        message = _errors(rows)
        self.assertIn("DECISION_COUNT: APPROVE expected 68 got 67", message)
        self.assertIn("DECISION_COUNT: SKIP expected 8 got 9", message)

    def test_invalid_decision_and_status_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", final_decision="MAYBE")
        self.assertIn("INVALID_DECISION: EC0165 MAYBE", _errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", status="OPEN")
        self.assertIn("INVALID_STATUS: EC0165 OPEN", _errors(rows))

    def test_invalid_entity_type_and_subtype_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", entity_type="LATIN")
        self.assertIn("INVALID_ENTITY_TYPE: EC0165 LATIN", _errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", entity_subtype="NICKNAME")
        self.assertIn("INVALID_ENTITY_SUBTYPE: EC0165 NICKNAME", _errors(rows))

    def test_non_approve_entity_metadata_rejected(self):
        cases = (
            (
                "EC2000",
                {"entity_type": "PLACE"},
                "UNEXPECTED_ENTITY_TYPE: EC2000 PLACE",
            ),
            (
                "EC2000",
                {"entity_subtype": "CITY"},
                "UNEXPECTED_ENTITY_SUBTYPE: EC2000 CITY",
            ),
            (
                "EC2000",
                {"entity_type": "LATIN"},
                "INVALID_ENTITY_TYPE: EC2000 LATIN",
            ),
            (
                "EC0009",
                {"entity_type": "PLACE", "entity_subtype": "CITY"},
                "UNEXPECTED_ENTITY_TYPE: EC0009 PLACE",
            ),
            (
                "EC3000",
                {"entity_subtype": "NICKNAME"},
                "UNEXPECTED_ENTITY_SUBTYPE: EC3000 NICKNAME",
            ),
            (
                "EC0025",
                {"entity_type": "EVENT"},
                "UNEXPECTED_ENTITY_TYPE: EC0025 EVENT",
            ),
        )
        for candidate_id, changes, expected in cases:
            with self.subTest(candidate_id=candidate_id, changes=changes):
                message = _errors(
                    _replace_candidate(valid_rows(), candidate_id, **changes)
                )
                self.assertIn(expected, message)
        split_message = _errors(
            _replace_candidate(
                valid_rows(),
                "EC0009",
                entity_type="PLACE",
                entity_subtype="CITY",
            )
        )
        self.assertIn("UNEXPECTED_ENTITY_SUBTYPE: EC0009 CITY", split_message)
        skip_message = _errors(
            _replace_candidate(valid_rows(), "EC3000", entity_subtype="NICKNAME")
        )
        self.assertIn("INVALID_ENTITY_SUBTYPE: EC3000 NICKNAME", skip_message)

    def test_approve_missing_canonical_or_type_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", final_canonical="")
        self.assertIn("APPROVE_MISSING_CANONICAL: EC0165", _errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", entity_type="")
        self.assertIn("APPROVE_MISSING_ENTITY_TYPE: EC0165", _errors(rows))

    def test_approve_with_merge_target_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", merge_target="EC0006")

        self.assertIn("APPROVE_HAS_MERGE_TARGET: EC0165", _errors(rows))

    def test_merge_missing_target_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="")

        self.assertIn("MERGE_MISSING_TARGET: EC2000", _errors(rows))

    def test_merge_target_absent_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC9999")

        self.assertIn("MERGE_TARGET_ABSENT: EC2000 -> EC9999", _errors(rows))

    def test_merge_target_not_approve_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC3000")

        self.assertIn("MERGE_TARGET_NOT_APPROVE: EC2000 -> EC3000", _errors(rows))

    def test_merge_canonical_mismatch_rejected(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC2000",
            final_canonical="different canonical",
        )

        self.assertIn("MERGE_CANONICAL_MISMATCH: EC2000", _errors(rows))

    def test_self_merge_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC2000")

        self.assertIn("MERGE_TO_SELF: EC2000", _errors(rows))
        self.assertNotIn("MERGE_CYCLE", _errors(rows))

    def test_merge_cycle_rejected(self):
        rows = valid_rows()
        rows = _replace_candidate(rows, "EC2000", merge_target="EC2001")
        rows = _replace_candidate(
            rows,
            "EC2001",
            merge_target="EC2000",
            final_canonical=rows[0].final_canonical,
        )

        self.assertIn("MERGE_CYCLE: EC2000 -> EC2001 -> EC2000", _errors(rows))

    def test_split_does_not_infer_occurrence_routing_from_notes(self):
        note = (
            "item 285 Palestine → EC0219 פלסטינה; "
            "item 340 Palestine → EC0219; "
            "LANGUAGE_VARIANT"
        )
        rows = _replace_candidate(valid_rows(), "EC0009", final_notes=note)
        plain = validate_reviewed_rows(valid_rows())
        noted = validate_reviewed_rows(rows)

        self.assertEqual(plain.apply_blockers, noted.apply_blockers)
        self.assertEqual(noted.rows[-3].final_notes, note)
        rendered = " ".join(blocker.message for blocker in noted.apply_blockers)
        self.assertNotIn("285", rendered)
        self.assertNotIn("340", rendered)
        self.assertNotIn("LANGUAGE_VARIANT", rendered)
        self.assertFalse(any(item.apply_ready for item in noted.split_apply_readiness))

    def test_ec0009_blocker_classification(self):
        result = validate_reviewed_rows(valid_rows())
        codes = {blocker.code: blocker for blocker in result.apply_blockers}

        self.assertIn("EC0009_TARGETS_EXIST", codes)
        self.assertIn("EC0219", codes["EC0009_TARGETS_EXIST"].message)
        self.assertIn("EC0006", codes["EC0009_TARGETS_EXIST"].message)
        self.assertEqual(
            codes["EC0009_NO_GLOBAL_PALESTINE_ALIAS"].message,
            "no global Palestine alias may be inferred",
        )
        self.assertEqual(
            codes["EC0009_ROUTING_PROSE_ONLY"].message,
            "routing is prose-only",
        )
        self.assertEqual(
            codes["EC0009_OCCURRENCE_KEYS_UNPINNED"].message,
            "occurrence keys are not pinned",
        )
        self.assertEqual(
            codes["EC0009_ITEM_353_ORDINALS_UNAPPROVED"].message,
            "item 353 says both mentions, not approved ordinal numbers",
        )
        ec0009_blockers = [
            blocker for code, blocker in codes.items() if code.startswith("EC0009_")
        ]
        self.assertTrue(
            all(blocker.candidate_id == "EC0009" for blocker in ec0009_blockers)
        )

    def test_ec0045_blocker_classification(self):
        result = validate_reviewed_rows(valid_rows())
        messages = {
            blocker.code: blocker.message
            for blocker in result.apply_blockers
            if blocker.candidate_id == "EC0045"
        }

        self.assertIn(
            GERMAN_REICH_MINISTRY_CANONICAL,
            messages["EC0045_GERMAN_CANONICAL_NOT_APPROVE"],
        )
        self.assertIn(
            "not represented as an APPROVE candidate",
            messages["EC0045_GERMAN_CANONICAL_NOT_APPROVE"],
        )
        self.assertEqual(
            messages["EC0045_EGYPTIAN_UNRESOLVED"],
            "Egyptian referent remains unresolved",
        )
        self.assertEqual(
            messages["EC0045_NO_OCCURRENCE_MAPPING"],
            "no authoritative item/ordinal occurrence mapping exists",
        )

        noted = _replace_candidate(
            valid_rows(),
            "EC0100",
            final_notes=GERMAN_REICH_MINISTRY_CANONICAL,
        )
        noted_codes = {
            blocker.code for blocker in validate_reviewed_rows(noted).apply_blockers
        }
        self.assertIn("EC0045_GERMAN_CANONICAL_NOT_APPROVE", noted_codes)

    def test_aliases_are_machine_unreadable(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC2001",
            final_notes="BAHAD = LANGUAGE_VARIANT",
        )
        result = validate_reviewed_rows(rows)
        alias_blockers = [
            blocker
            for blocker in result.apply_blockers
            if blocker.code == "ALIASES_NOT_MACHINE_READABLE"
        ]

        self.assertEqual(len(alias_blockers), 1)
        self.assertIsInstance(alias_blockers[0], ApplyBlocker)
        self.assertIn(
            "no machine-readable authoritative alias contract",
            alias_blockers[0].message,
        )
        self.assertIn("not parsed from final_notes", alias_blockers[0].message)
        self.assertNotIn("LANGUAGE_VARIANT", alias_blockers[0].message)
        self.assertNotIn("BAHAD", alias_blockers[0].message)
        report = format_preflight_report(result)
        self.assertIn("ALIASES_NOT_MACHINE_READABLE", report)

    def _workbook_path(self, name: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory / name


class NonPersonEntityReviewPreflightDatabaseTests(TestCase):
    def test_validation_does_not_write_database_rows(self):
        before = _identity_counts()

        result = validate_reviewed_rows(valid_rows())

        self.assertEqual(before, _identity_counts())
        self.assertEqual(NonPersonEntity.objects.count(), 0)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(len(result.rows), 107)
        self.assertGreater(len(result.apply_blockers), 0)


def _identity_counts() -> dict[str, int]:
    return {
        "entity": NonPersonEntity.objects.count(),
        "alias": NonPersonEntityAlias.objects.count(),
        "decision": ReviewedNonPersonEntityDecision.objects.count(),
        "occurrence": ArchiveItemEntityOccurrence.objects.count(),
    }
