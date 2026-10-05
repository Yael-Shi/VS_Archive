"""Parser tests for the v6 non-person entity review preflight."""

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
    ALIAS_COLUMNS,
    ALIAS_REVIEW_CLOSED_STATUS,
    ALIAS_REVIEW_COLUMNS,
    AUTHORITATIVE_WORKBOOK_SHA256,
    FINAL_ALIAS_SHEET_NAME,
    FINAL_SHEET_NAME,
    FINAL_SPLIT_ROUTING_SHEET_NAME,
    REQUIRED_COLUMNS,
    SPLIT_ROUTE_COLUMNS,
    PreflightError,
    ReviewedAliasRow,
    ReviewedNonPersonEntityRow,
    ReviewedSplitRoute,
    format_preflight_report,
    normalize_surface_v1,
    preflight_authoritative_workbook,
    preflight_workbook,
    read_final_sheet_rows,
    sha256_path,
    validate_review_contract,
)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_ITEM_SHA = {
    108: "dc7d5c0f621d26d08e4f30d910864e7c00f70444b282981e656de16bd3fdbff2",
    285: "617b4442af431ebb2d7ae1146b351f0f11b09b3e33ba656a8c1f80993e774a52",
    340: "fbcdf789f8f3a08045f123f6bbac2cc242cab8e5e827d98381017ddc9999c0c5",
    353: "04b1e0d31706225b3b8e2116fe7127cc34970696e35256c8569fcef695ca9a47",
}
_ALIAS_REVIEW_STATUS = ALIAS_REVIEW_CLOSED_STATUS


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
        "EC0802": (
            "משרד הרייך לתעמולה ולהשכלת העם",
            "ORGANIZATION",
            "GOVERNMENT_BODY",
        ),
        "EC0803": ("משרד ההכוונה הלאומית המצרי", "ORGANIZATION", "GOVERNMENT_BODY"),
    }
    approve_ids = [
        "EC0006",
        "EC0219",
        "EC0802",
        "EC0803",
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
            final_notes="item 285 Palestine ordinal 1 → EC0219; LANGUAGE_VARIANT",
        )
    )
    rows.append(
        _row(
            "EC0045",
            ReviewedNonPersonEntityDecision.Decision.SPLIT,
            surface="ministère de la propagande",
            final_notes="item 108 ordinal 1 → EC0802; ordinal 2 → EC0803",
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


def valid_aliases(
    rows: list[ReviewedNonPersonEntityRow] | None = None,
) -> list[ReviewedAliasRow]:
    source_rows = valid_rows() if rows is None else rows
    target = next(row for row in source_rows if row.candidate_id == "EC0006")
    return [
        ReviewedAliasRow(
            target_candidate_id=target.candidate_id,
            target_canonical=target.final_canonical,
            alias_name=f"alias-{index:02d}",
            alias_kind=NonPersonEntityAlias.Kind.LANGUAGE_VARIANT,
            source_candidate_id=f"EC9{index:03d}",
            review_status="APPROVED",
            basis="synthetic",
        )
        for index in range(63)
    ]


def _route(
    split_candidate_id: str,
    archive_item_id: int,
    surface: str,
    occurrence_ordinal: int,
    target_candidate_id: str,
    target_canonical: str,
    *,
    notes: str = "",
    **changes: str,
) -> ReviewedSplitRoute:
    values = {
        "split_candidate_id": split_candidate_id,
        "archive_item_id": str(archive_item_id),
        "surface": surface,
        "occurrence_ordinal": str(occurrence_ordinal),
        "target_candidate_id": target_candidate_id,
        "target_canonical": target_canonical,
        "resolution_status": ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        "normalization_version": "surface-v1",
        "text_kind": ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        "source_text_sha256": _ITEM_SHA[archive_item_id],
        "pin_status": "PINNED",
        "review_status": "APPROVED",
        "notes": notes,
    }
    values.update(changes)
    return ReviewedSplitRoute(**values)


def valid_routes() -> list[ReviewedSplitRoute]:
    return [
        _route(
            "EC0009",
            285,
            "Palestine",
            1,
            "EC0219",
            "פלסטינה",
            notes="ordinal prose must not add a route",
        ),
        _route("EC0009", 340, "Palestine", 1, "EC0219", "פלסטינה"),
        _route("EC0009", 353, "Palestine", 1, "EC0006", "ארץ ישראל"),
        _route("EC0009", 353, "Palestine", 2, "EC0006", "ארץ ישראל"),
        _route(
            "EC0045",
            108,
            "ministère de la propagande",
            1,
            "EC0802",
            "משרד הרייך לתעמולה ולהשכלת העם",
        ),
        _route(
            "EC0045",
            108,
            "ministère de la propagande",
            2,
            "EC0803",
            "משרד ההכוונה הלאומית המצרי",
            notes="shared French surface must not become a global alias",
        ),
    ]


def _replace_candidate(
    rows: list[ReviewedNonPersonEntityRow],
    candidate_id: str,
    **changes,
) -> list[ReviewedNonPersonEntityRow]:
    return [
        replace(row, **changes) if row.candidate_id == candidate_id else row
        for row in rows
    ]


def _contract_errors(
    rows: list[ReviewedNonPersonEntityRow] | None = None,
    aliases: list[ReviewedAliasRow] | None = None,
    routes: list[ReviewedSplitRoute] | None = None,
    unresolved: tuple[str, ...] = (),
) -> str:
    try:
        validate_review_contract(
            valid_rows() if rows is None else rows,
            valid_aliases() if aliases is None else aliases,
            valid_routes() if routes is None else routes,
            unresolved_alias_review_ids=unresolved,
        )
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
    extra_sheets: list[tuple[str, list[list[tuple[str, str]]]]] | None = None,
    include_decoy: bool = False,
    shared_string_rows: list[list[tuple[str, str]]] | None = None,
    shared_strings: list[str] | None = None,
) -> None:
    sheets = [
        (
            FINAL_SHEET_NAME,
            _worksheet_xml(final_rows, shared_rows=shared_string_rows),
        )
    ]
    for name, rows in extra_sheets or []:
        sheets.append((name, _worksheet_xml(rows)))
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
        sheets.append(("approved_decisions_log", _worksheet_xml(decoy)))
        sheets.append(
            (
                "ALIAS_EXCLUSIONS",
                _worksheet_xml(
                    [
                        [
                            ("A", "candidate_id"),
                            ("B", "surface"),
                            ("C", "reason_excluded"),
                        ],
                        [
                            ("A", "EC0009"),
                            ("B", "Palestine"),
                            ("C", "SPLIT; no global alias"),
                        ],
                    ]
                ),
            )
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
    for index, (name, _xml) in enumerate(sheets, start=1):
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
        for index, (_name, xml) in enumerate(sheets, start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", xml)
        if shared_strings is not None:
            archive.writestr(
                "xl/sharedStrings.xml", _shared_strings_xml(shared_strings)
            )


def _cells(letters: str, values: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (letter, value)
        for letter, value in zip(letters, values, strict=True)
        if value != ""
    ]


def _cells_for(row: ReviewedNonPersonEntityRow) -> list[tuple[str, str]]:
    return _cells(
        "ABCDEFGHI",
        (
            row.candidate_id,
            row.surface,
            row.final_decision,
            row.final_canonical,
            row.merge_target,
            row.final_notes,
            row.status,
            row.entity_type,
            row.entity_subtype,
        ),
    )


def _alias_cells(alias: ReviewedAliasRow) -> list[tuple[str, str]]:
    return _cells(
        "ABCDEFG",
        (
            alias.target_candidate_id,
            alias.target_canonical,
            alias.alias_name,
            alias.alias_kind,
            alias.source_candidate_id,
            alias.review_status,
            alias.basis,
        ),
    )


def _route_cells(route: ReviewedSplitRoute) -> list[tuple[str, str]]:
    return _cells(
        "ABCDEFGHIJKLM",
        (
            route.split_candidate_id,
            route.archive_item_id,
            route.surface,
            route.occurrence_ordinal,
            route.target_candidate_id,
            route.target_canonical,
            route.resolution_status,
            route.normalization_version,
            route.text_kind,
            route.source_text_sha256,
            route.pin_status,
            route.review_status,
            route.notes,
        ),
    )


def _header(columns: tuple[str, ...]) -> list[tuple[str, str]]:
    return list(zip(_column_span(len(columns)), columns, strict=True))


def _column_span(count: int) -> str:
    return "".join(chr(ord("A") + index) for index in range(count))


def _alias_review_sheet(
    extra_rows: list[list[tuple[str, str]]] | None = None,
) -> list[list[tuple[str, str]]]:
    return [
        [("A", "Alias candidates requiring explicit human choice")],
        _header(ALIAS_REVIEW_COLUMNS),
        [("A", _ALIAS_REVIEW_STATUS)],
        *(extra_rows or []),
    ]


def _contract_sheets(
    rows: list[ReviewedNonPersonEntityRow],
    aliases: list[ReviewedAliasRow],
    routes: list[ReviewedSplitRoute],
    *,
    alias_review_rows: list[list[tuple[str, str]]] | None = None,
) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    return [
        (FINAL_ALIAS_SHEET_NAME, [_header(ALIAS_COLUMNS), *map(_alias_cells, aliases)]),
        (
            preflight_module.ALIAS_REVIEW_SHEET_NAME,
            alias_review_rows or _alias_review_sheet(),
        ),
        (
            FINAL_SPLIT_ROUTING_SHEET_NAME,
            [_header(SPLIT_ROUTE_COLUMNS), *map(_route_cells, routes)],
        ),
    ]


def _write_valid_workbook(
    path: Path,
    rows: list[ReviewedNonPersonEntityRow],
    aliases: list[ReviewedAliasRow] | None = None,
    routes: list[ReviewedSplitRoute] | None = None,
    *,
    alias_review_rows: list[list[tuple[str, str]]] | None = None,
) -> None:
    header = _header(REQUIRED_COLUMNS)
    preamble = [("A", "not a reviewed row"), ("B", "EC9999 should not be read")]
    stored_aliases = valid_aliases(rows) if aliases is None else aliases
    stored_routes = valid_routes() if routes is None else routes
    table = [preamble, header, *[_cells_for(row) for row in rows]]
    _write_workbook(
        path,
        table,
        extra_sheets=_contract_sheets(
            rows,
            stored_aliases,
            stored_routes,
            alias_review_rows=alias_review_rows,
        ),
        include_decoy=True,
    )


class NonPersonEntityReviewPreflightTests(SimpleTestCase):
    def test_authoritative_sha_constant(self):
        self.assertEqual(
            AUTHORITATIVE_WORKBOOK_SHA256,
            "c17a5d52ad531ca144ce113cab5abbdd252f752d62c765451769705ee536f948",
        )

    def test_surface_v1_normalization(self):
        self.assertEqual(normalize_surface_v1("  Palestine\u200f "), "palestine")
        self.assertEqual(
            normalize_surface_v1("ministère   de la propagande"),
            "ministère de la propagande",
        )
        self.assertEqual(normalize_surface_v1("א-רץ"), "א-רץ")

    def test_valid_v6_contract_counts_and_exact_split_routes(self):
        rows = valid_rows()
        aliases = valid_aliases(rows)
        routes = valid_routes()
        result = validate_review_contract(rows, aliases, routes)

        self.assertTrue(result.parser_valid)
        self.assertEqual(len(result.rows), 109)
        self.assertEqual(
            result.decision_counts(),
            {
                "APPROVE": 70,
                "MERGE": 28,
                "SKIP": 8,
                "SPLIT": 2,
                "NEEDS_RESEARCH": 1,
            },
        )
        self.assertEqual(len(result.aliases), 63)
        self.assertEqual(len(result.split_routes), 6)
        self.assertEqual(result.unresolved_alias_reviews, 0)
        self.assertEqual(result.apply_blockers, ())
        self.assertEqual(len(result.entity_creation_candidate_ids), 70)
        self.assertEqual(
            [
                (item.candidate_id, item.apply_ready, item.reason)
                for item in result.split_apply_readiness
            ],
            [
                ("EC0009", True, preflight_module.SPLIT_APPLY_READY_REASON),
                ("EC0045", True, preflight_module.SPLIT_APPLY_READY_REASON),
            ],
        )
        self.assertEqual(
            [
                (
                    route.archive_item_id,
                    route.surface,
                    route.occurrence_ordinal,
                    route.target_candidate_id,
                    route.target_canonical,
                )
                for route in result.split_routes
                if route.split_candidate_id == "EC0009"
            ],
            [
                ("285", "Palestine", "1", "EC0219", "פלסטינה"),
                ("340", "Palestine", "1", "EC0219", "פלסטינה"),
                ("353", "Palestine", "1", "EC0006", "ארץ ישראל"),
                ("353", "Palestine", "2", "EC0006", "ארץ ישראל"),
            ],
        )
        self.assertEqual(
            [
                (
                    route.archive_item_id,
                    route.surface,
                    route.occurrence_ordinal,
                    route.target_candidate_id,
                    route.target_canonical,
                )
                for route in result.split_routes
                if route.split_candidate_id == "EC0045"
            ],
            [
                (
                    "108",
                    "ministère de la propagande",
                    "1",
                    "EC0802",
                    "משרד הרייך לתעמולה ולהשכלת העם",
                ),
                (
                    "108",
                    "ministère de la propagande",
                    "2",
                    "EC0803",
                    "משרד ההכוונה הלאומית המצרי",
                ),
            ],
        )
        needs_research = next(
            row for row in result.rows if row.candidate_id == "EC0025"
        )
        self.assertEqual(
            needs_research.final_decision,
            ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH,
        )
        self.assertNotIn("EC0025", result.entity_creation_candidate_ids)
        alias_names = {alias.alias_name for alias in result.aliases}
        self.assertNotIn("Palestine", alias_names)
        self.assertNotIn("ministère de la propagande", alias_names)
        report = format_preflight_report(result)
        self.assertIn("parser_valid: yes", report)
        self.assertIn("rows: 109", report)
        self.assertIn("APPROVE: 70", report)
        self.assertIn("aliases: 63", report)
        self.assertIn("split_routes: 6", report)
        self.assertIn("unresolved_alias_reviews: 0", report)
        self.assertIn("apply_blocker_count: 0", report)
        self.assertNotIn("ALIASES_NOT_MACHINE_READABLE", report)
        self.assertNotIn("EC0009_NO_GLOBAL_PALESTINE_ALIAS", report)
        self.assertNotIn("EC0045_EGYPTIAN_UNRESOLVED", report)

    def test_workbook_parse_reads_structured_sheets_only(self):
        rows = valid_rows()
        aliases = valid_aliases(rows)
        routes = valid_routes()
        path = self._workbook_path("valid.xlsx")
        _write_valid_workbook(path, rows, aliases, routes)

        result = preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertEqual(result.rows, tuple(rows))
        self.assertEqual(result.aliases, tuple(aliases))
        self.assertEqual(result.split_routes, tuple(routes))
        self.assertEqual(result.unresolved_alias_reviews, 0)
        self.assertEqual(result.apply_blockers, ())
        self.assertNotIn("EC9999", {row.candidate_id for row in result.rows})
        self.assertNotIn("Palestine", {alias.alias_name for alias in result.aliases})
        self.assertEqual(result.aliases[0].source_candidate_id, "EC9000")

    def test_closed_alias_review_status_row_is_not_a_candidate(self):
        path = self._workbook_path("alias-review.xlsx")
        rows = valid_rows()
        _write_valid_workbook(path, rows)

        result = preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertEqual(result.unresolved_alias_reviews, 0)

    def test_alias_review_one_cell_row_is_not_ignored(self):
        path = self._workbook_path("alias-review-prose.xlsx")
        _write_valid_workbook(
            path,
            valid_rows(),
            alias_review_rows=_alias_review_sheet(
                [[("A", "please look at this later")]]
            ),
        )

        with self.assertRaises(PreflightError) as caught:
            preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertIn(
            "UNRESOLVED_ALIAS_REVIEW: 1 please look at this later",
            str(caught.exception),
        )

    def test_unresolved_alias_review_candidate_rejected(self):
        path = self._workbook_path("alias-review-open.xlsx")
        rows = valid_rows()
        _write_valid_workbook(
            path,
            rows,
            alias_review_rows=_alias_review_sheet(
                [
                    [
                        ("A", "EC0999"),
                        ("B", "somewhere"),
                        ("C", "Somewhere"),
                        ("D", "LANGUAGE_VARIANT?"),
                        ("E", "EC0999"),
                        ("F", "needs a choice"),
                    ]
                ]
            ),
        )

        with self.assertRaises(PreflightError) as caught:
            preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertIn("UNRESOLVED_ALIAS_REVIEW: 1 EC0999", str(caught.exception))

    def test_notes_do_not_infer_aliases_or_routes(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC0009",
            final_notes=(
                "item 999 Palestine ordinal 9 → EC0219; "
                "Palestine LANGUAGE_VARIANT; "
                "ministère de la propagande ABBREVIATION"
            ),
        )
        routes = valid_routes()
        routes[0] = replace(
            routes[0],
            notes="item 999 should not be parsed; LANGUAGE_VARIANT",
        )
        plain = validate_review_contract(valid_rows(), valid_aliases(), valid_routes())
        noted = validate_review_contract(rows, valid_aliases(), routes)

        self.assertEqual(
            [alias.alias_name for alias in plain.aliases],
            [alias.alias_name for alias in noted.aliases],
        )
        self.assertEqual(len(noted.split_routes), len(plain.split_routes))
        self.assertEqual(
            [
                (
                    route.split_candidate_id,
                    route.archive_item_id,
                    route.occurrence_ordinal,
                    route.target_candidate_id,
                )
                for route in noted.split_routes
            ],
            [
                (
                    route.split_candidate_id,
                    route.archive_item_id,
                    route.occurrence_ordinal,
                    route.target_candidate_id,
                )
                for route in plain.split_routes
            ],
        )
        self.assertNotIn("999", {route.archive_item_id for route in noted.split_routes})
        self.assertTrue(
            all(alias.alias_kind == "LANGUAGE_VARIANT" for alias in noted.aliases)
        )

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
        _write_workbook(path, [_header(REQUIRED_COLUMNS)])
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
            path,
            [_header(REQUIRED_COLUMNS)],
            shared_string_rows=[data],
            shared_strings=strings,
        )

        rows = read_final_sheet_rows(path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].candidate_id, "EC0001")
        self.assertEqual(rows[0].surface, "Palestine")
        self.assertEqual(rows[0].final_canonical, "פלסטינה")
        self.assertEqual(rows[0].entity_subtype, "REGION_OR_HISTORICAL_AREA")

    def test_missing_required_column_rejected(self):
        path = self._workbook_path("missing-column.xlsx")
        header = [
            (letter, column)
            for letter, column in zip("ABCDEFGH", REQUIRED_COLUMNS[:-1], strict=True)
        ]
        _write_workbook(path, [header])

        with self.assertRaises(PreflightError) as caught:
            read_final_sheet_rows(path)

        self.assertIn("MISSING_COLUMN: entity_subtype", str(caught.exception))

    def test_missing_alias_sheet_rejected(self):
        path = self._workbook_path("missing-alias-sheet.xlsx")
        rows = valid_rows()
        routes = valid_routes()
        _write_workbook(
            path,
            [_header(REQUIRED_COLUMNS), *[_cells_for(row) for row in rows]],
            extra_sheets=[
                (preflight_module.ALIAS_REVIEW_SHEET_NAME, _alias_review_sheet()),
                (
                    FINAL_SPLIT_ROUTING_SHEET_NAME,
                    [
                        _header(SPLIT_ROUTE_COLUMNS),
                        *[_route_cells(route) for route in routes],
                    ],
                ),
            ],
        )

        with self.assertRaises(PreflightError) as caught:
            preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertIn(f"MISSING_SHEET: {FINAL_ALIAS_SHEET_NAME}", str(caught.exception))

    def test_duplicate_candidate_id_rejected(self):
        rows = valid_rows()
        rows[1] = replace(rows[1], candidate_id=rows[0].candidate_id)

        self.assertIn("DUPLICATE_CANDIDATE_ID: EC0006", _contract_errors(rows))

    def test_wrong_counts_rejected(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC0165",
            final_decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
            final_canonical="",
            entity_type="",
            entity_subtype="",
        )

        message = _contract_errors(rows)
        self.assertIn("DECISION_COUNT: APPROVE expected 70 got 69", message)
        self.assertIn("DECISION_COUNT: SKIP expected 8 got 9", message)

    def test_invalid_decision_and_status_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", final_decision="MAYBE")
        self.assertIn("INVALID_DECISION: EC0165 MAYBE", _contract_errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", status="OPEN")
        self.assertIn("INVALID_STATUS: EC0165 OPEN", _contract_errors(rows))

    def test_invalid_entity_type_and_subtype_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", entity_type="LATIN")
        self.assertIn("INVALID_ENTITY_TYPE: EC0165 LATIN", _contract_errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", entity_subtype="NICKNAME")
        self.assertIn("INVALID_ENTITY_SUBTYPE: EC0165 NICKNAME", _contract_errors(rows))

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
                "EC0009",
                {"entity_type": "PLACE", "entity_subtype": "CITY"},
                "UNEXPECTED_ENTITY_TYPE: EC0009 PLACE",
            ),
            (
                "EC0025",
                {"entity_type": "EVENT"},
                "UNEXPECTED_ENTITY_TYPE: EC0025 EVENT",
            ),
        )
        for candidate_id, changes, expected in cases:
            with self.subTest(candidate_id=candidate_id, changes=changes):
                self.assertIn(
                    expected,
                    _contract_errors(
                        _replace_candidate(valid_rows(), candidate_id, **changes)
                    ),
                )

    def test_approve_missing_canonical_or_type_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", final_canonical="")
        self.assertIn("APPROVE_MISSING_CANONICAL: EC0165", _contract_errors(rows))

        rows = _replace_candidate(valid_rows(), "EC0165", entity_type="")
        self.assertIn("APPROVE_MISSING_ENTITY_TYPE: EC0165", _contract_errors(rows))

    def test_approve_with_merge_target_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC0165", merge_target="EC0006")

        self.assertIn("APPROVE_HAS_MERGE_TARGET: EC0165", _contract_errors(rows))

    def test_merge_missing_target_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="")

        self.assertIn("MERGE_MISSING_TARGET: EC2000", _contract_errors(rows))

    def test_merge_target_absent_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC9999")

        self.assertIn("MERGE_TARGET_ABSENT: EC2000 -> EC9999", _contract_errors(rows))

    def test_merge_target_not_approve_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC3000")

        self.assertIn(
            "MERGE_TARGET_NOT_APPROVE: EC2000 -> EC3000",
            _contract_errors(rows),
        )

    def test_merge_canonical_mismatch_rejected(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC2000",
            final_canonical="different canonical",
        )

        self.assertIn("MERGE_CANONICAL_MISMATCH: EC2000", _contract_errors(rows))

    def test_self_merge_rejected(self):
        rows = _replace_candidate(valid_rows(), "EC2000", merge_target="EC2000")

        message = _contract_errors(rows)
        self.assertIn("MERGE_TO_SELF: EC2000", message)
        self.assertNotIn("MERGE_CYCLE", message)

    def test_merge_cycle_rejected(self):
        rows = valid_rows()
        rows = _replace_candidate(rows, "EC2000", merge_target="EC2001")
        rows = _replace_candidate(
            rows,
            "EC2001",
            merge_target="EC2000",
            final_canonical=next(
                row.final_canonical for row in rows if row.candidate_id == "EC2000"
            ),
        )

        self.assertIn("MERGE_CYCLE: EC2000 -> EC2001 -> EC2000", _contract_errors(rows))

    def test_alias_target_missing_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], target_candidate_id="")

        self.assertIn("ALIAS_TARGET_MISSING", _contract_errors(aliases=aliases))

    def test_alias_target_absent_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], target_candidate_id="EC9999")

        self.assertIn("ALIAS_TARGET_ABSENT: EC9999", _contract_errors(aliases=aliases))

    def test_alias_target_not_approve_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(
            aliases[0], target_candidate_id="EC3000", target_canonical=""
        )

        self.assertIn(
            "ALIAS_TARGET_NOT_APPROVE: EC3000",
            _contract_errors(aliases=aliases),
        )

    def test_alias_canonical_mismatch_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], target_canonical="different canonical")

        self.assertIn(
            "ALIAS_CANONICAL_MISMATCH: EC0006",
            _contract_errors(aliases=aliases),
        )

    def test_invalid_alias_kind_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], alias_kind="FORMAL_NAME")

        self.assertIn(
            "INVALID_ALIAS_KIND: EC0006 FORMAL_NAME",
            _contract_errors(aliases=aliases),
        )

    def test_blank_alias_name_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], alias_name="")

        self.assertIn("ALIAS_NAME_BLANK: EC0006", _contract_errors(aliases=aliases))

    def test_duplicate_alias_rejected(self):
        aliases = valid_aliases()
        aliases[1] = replace(aliases[1], alias_name=aliases[0].alias_name)

        self.assertIn(
            "DUPLICATE_ALIAS: EC0006 alias-00",
            _contract_errors(aliases=aliases),
        )

    def test_unapproved_alias_review_status_rejected(self):
        aliases = valid_aliases()
        aliases[0] = replace(aliases[0], review_status="CLOSED")

        self.assertIn(
            "ALIAS_REVIEW_STATUS_NOT_APPROVED: EC0006 CLOSED",
            _contract_errors(aliases=aliases),
        )

    def test_same_canonical_name_does_not_resolve_alias_target(self):
        rows = _replace_candidate(
            valid_rows(),
            "EC0165",
            final_canonical="ארץ ישראל",
        )
        aliases = valid_aliases()
        aliases[0] = replace(
            aliases[0],
            target_candidate_id="EC0165",
            target_canonical="ארץ ישראל",
        )

        result = validate_review_contract(rows, aliases, valid_routes())

        self.assertEqual(result.aliases[0].target_candidate_id, "EC0165")
        self.assertEqual(result.aliases[0].target_canonical, "ארץ ישראל")

    def test_malformed_source_text_sha_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], source_text_sha256="ABC")

        self.assertIn(
            "MALFORMED_SOURCE_TEXT_SHA256: EC0009",
            _contract_errors(routes=routes),
        )

    def test_invalid_text_kind_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], text_kind="TRANSLATION")

        self.assertIn(
            "INVALID_TEXT_KIND: EC0009 TRANSLATION", _contract_errors(routes=routes)
        )

    def test_non_split_route_source_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], split_candidate_id="EC0006")

        self.assertIn(
            "SPLIT_CANDIDATE_NOT_SPLIT: EC0006",
            _contract_errors(routes=routes),
        )

    def test_route_target_absent_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], target_candidate_id="EC9999")

        self.assertIn(
            "ROUTE_TARGET_ABSENT: EC0009 -> EC9999",
            _contract_errors(routes=routes),
        )

    def test_route_target_not_approve_rejected(self):
        routes = valid_routes()
        routes[0] = replace(
            routes[0], target_candidate_id="EC3000", target_canonical=""
        )

        self.assertIn(
            "ROUTE_TARGET_NOT_APPROVE: EC0009 -> EC3000",
            _contract_errors(routes=routes),
        )

    def test_route_canonical_mismatch_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], target_canonical="ישראל")

        self.assertIn(
            "ROUTE_CANONICAL_MISMATCH: EC0009",
            _contract_errors(routes=routes),
        )

    def test_invalid_archive_item_id_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], archive_item_id="0")

        self.assertIn(
            "INVALID_ARCHIVE_ITEM_ID: EC0009 0", _contract_errors(routes=routes)
        )

    def test_invalid_occurrence_ordinal_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], occurrence_ordinal="0")

        self.assertIn(
            "INVALID_OCCURRENCE_ORDINAL: EC0009 0",
            _contract_errors(routes=routes),
        )

    def test_invalid_normalization_version_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], normalization_version="surface-v2")

        self.assertIn(
            "INVALID_NORMALIZATION_VERSION: EC0009 surface-v2",
            _contract_errors(routes=routes),
        )

    def test_invalid_resolution_status_rejected(self):
        routes = valid_routes()
        routes[0] = replace(
            routes[0],
            resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.UNRESOLVED,
        )

        self.assertIn(
            "INVALID_RESOLUTION_STATUS: EC0009 UNRESOLVED",
            _contract_errors(routes=routes),
        )

    def test_route_not_pinned_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], pin_status="UNPINNED")

        self.assertIn("ROUTE_NOT_PINNED: EC0009", _contract_errors(routes=routes))

    def test_route_not_approved_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], review_status="CLOSED")

        self.assertIn("ROUTE_NOT_APPROVED: EC0009", _contract_errors(routes=routes))

    def test_duplicate_occurrence_identity_rejected(self):
        routes = valid_routes()
        routes[1] = replace(
            routes[1],
            surface="  PALESTINE\u200f",
            archive_item_id="285",
            source_text_sha256=_ITEM_SHA[285],
        )

        message = _contract_errors(routes=routes)
        self.assertIn("DUPLICATE_OCCURRENCE_IDENTITY:", message)
        self.assertIn("285", message)
        self.assertIn("palestine", message)

    def test_blank_route_surface_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], surface="   ")

        self.assertIn("ROUTE_SURFACE_BLANK: EC0009", _contract_errors(routes=routes))

    def test_incomplete_split_routes_rejected(self):
        routes = [
            route for route in valid_routes() if route.split_candidate_id != "EC0045"
        ]

        message = _contract_errors(routes=routes)
        self.assertIn("SPLIT_ROUTES_MISSING: EC0045", message)
        self.assertIn("SPLIT_ROUTE_COUNT: expected 6 got 4", message)
        self.assertIn(
            "SPLIT_CANDIDATE_ROUTE_COUNT: EC0045 expected 2 got 0",
            message,
        )

    def test_split_route_distribution_must_match_candidate_counts(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], split_candidate_id="EC0045")

        self.assertEqual(len(routes), 6)
        self.assertEqual(
            [route.split_candidate_id for route in routes].count("EC0009"),
            3,
        )
        self.assertEqual(
            [route.split_candidate_id for route in routes].count("EC0045"),
            3,
        )
        message = _contract_errors(routes=routes)
        self.assertIn(
            "SPLIT_CANDIDATE_ROUTE_COUNT: EC0009 expected 4 got 3",
            message,
        )
        self.assertIn(
            "SPLIT_CANDIDATE_ROUTE_COUNT: EC0045 expected 2 got 3",
            message,
        )
        self.assertNotIn("SPLIT_ROUTES_MISSING", message)
        self.assertNotIn("SPLIT_ROUTE_COUNT: expected 6 got", message)

    def test_split_candidate_absent_rejected(self):
        routes = valid_routes()
        routes[0] = replace(routes[0], split_candidate_id="EC9999")

        self.assertIn(
            "SPLIT_CANDIDATE_ABSENT: EC9999",
            _contract_errors(routes=routes),
        )

    def _workbook_path(self, name: str) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        return directory / name


class NonPersonEntityReviewPreflightDatabaseTests(TestCase):
    def test_validation_does_not_write_database_rows(self):
        before = _identity_counts()
        rows = valid_rows()
        path = Path(tempfile.mkdtemp()) / "valid.xlsx"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        _write_valid_workbook(path, rows)

        result = preflight_workbook(path, expected_sha256=sha256_path(path))

        self.assertEqual(before, _identity_counts())
        self.assertEqual(NonPersonEntity.objects.count(), 0)
        self.assertEqual(NonPersonEntityAlias.objects.count(), 0)
        self.assertEqual(ReviewedNonPersonEntityDecision.objects.count(), 0)
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 0)
        self.assertEqual(len(result.rows), 109)
        self.assertEqual(len(result.aliases), 63)
        self.assertEqual(len(result.split_routes), 6)
        self.assertEqual(result.apply_blockers, ())


def _identity_counts() -> dict[str, int]:
    return {
        "entity": NonPersonEntity.objects.count(),
        "alias": NonPersonEntityAlias.objects.count(),
        "decision": ReviewedNonPersonEntityDecision.objects.count(),
        "occurrence": ArchiveItemEntityOccurrence.objects.count(),
    }
