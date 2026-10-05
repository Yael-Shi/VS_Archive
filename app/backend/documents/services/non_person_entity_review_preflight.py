"""Read-only preflight for the v3 non-person entity review workbook.

Parses and validates the authoritative FINAL sheet. Does not write to the
database, does not create aliases, and does not read final_notes for routing
or alias kinds.
"""

from __future__ import annotations

import hashlib
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from xml.etree import ElementTree as ET

from documents.models import NonPersonEntity, ReviewedNonPersonEntityDecision

AUTHORITATIVE_WORKBOOK_SHA256 = (
    "fe59efd0e88f447ffdcdfd80e34c4f34686674480d2bb426154ee1f41d6ed03d"
)
FINAL_SHEET_NAME = "FINAL_RECON_2026-10-04"
REQUIRED_COLUMNS = (
    "candidate_id",
    "surface / candidate",
    "final_decision",
    "final_canonical",
    "merge_target",
    "final_notes",
    "status",
    "entity_type",
    "entity_subtype",
)
COLUMN_LETTERS = ("A", "B", "C", "D", "E", "F", "G", "H", "I")
EXPECTED_ROW_COUNT = 107
EXPECTED_DECISION_COUNTS = {
    ReviewedNonPersonEntityDecision.Decision.APPROVE: 68,
    ReviewedNonPersonEntityDecision.Decision.MERGE: 28,
    ReviewedNonPersonEntityDecision.Decision.SKIP: 8,
    ReviewedNonPersonEntityDecision.Decision.SPLIT: 2,
    ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH: 1,
}
GERMAN_REICH_MINISTRY_CANONICAL = "משרד הרייך לתעמולה ולהשכלת העם"
SPLIT_NOT_APPLYABLE_REASON = "SPLIT occurrence routing is not parsed from final_notes"

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class PreflightError(ValueError):
    def __init__(self, errors: tuple[str, ...]):
        self.errors = errors
        super().__init__("\n".join(errors))


@dataclass(frozen=True)
class ReviewedNonPersonEntityRow:
    """One FINAL-sheet row. Values are the raw authoritative cell strings."""

    candidate_id: str
    surface: str
    final_decision: str
    final_canonical: str
    merge_target: str
    final_notes: str
    status: str
    entity_type: str
    entity_subtype: str


@dataclass(frozen=True)
class ApplyBlocker:
    code: str
    candidate_id: str
    message: str


@dataclass(frozen=True)
class SplitApplyReadiness:
    """Separate from parser validity. SPLIT rows are never apply-ready here."""

    candidate_id: str
    apply_ready: bool
    reason: str


@dataclass(frozen=True)
class PreflightResult:
    workbook_sha256: str
    rows: tuple[ReviewedNonPersonEntityRow, ...]
    apply_blockers: tuple[ApplyBlocker, ...]
    split_apply_readiness: tuple[SplitApplyReadiness, ...]

    @property
    def parser_valid(self) -> bool:
        return True

    def decision_counts(self) -> dict[str, int]:
        counts = Counter(row.final_decision for row in self.rows)
        return {decision: counts[decision] for decision in EXPECTED_DECISION_COUNTS}

    @property
    def entity_creation_candidate_ids(self) -> tuple[str, ...]:
        approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
        return tuple(
            row.candidate_id for row in self.rows if row.final_decision == approve
        )


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight_authoritative_workbook(path: str | Path) -> PreflightResult:
    """Parse the v3 workbook and require its fixed SHA-256."""

    return preflight_workbook(
        path,
        expected_sha256=AUTHORITATIVE_WORKBOOK_SHA256,
    )


def preflight_workbook(path: str | Path, *, expected_sha256: str) -> PreflightResult:
    workbook_path = Path(path)
    if not workbook_path.is_file():
        raise PreflightError((f"WORKBOOK_NOT_FOUND: {workbook_path}",))

    actual = sha256_path(workbook_path)
    expected = expected_sha256.lower()
    if actual != expected:
        raise PreflightError(
            (f"SHA256_MISMATCH: expected={expected_sha256} actual={actual}",)
        )

    rows = read_final_sheet_rows(workbook_path)
    result = validate_reviewed_rows(rows)
    return replace(result, workbook_sha256=actual)


def validate_reviewed_rows(
    rows: Sequence[ReviewedNonPersonEntityRow],
) -> PreflightResult:
    errors = _validation_errors(rows)
    if errors:
        raise PreflightError(tuple(sorted(errors)))

    stored = tuple(rows)
    return PreflightResult(
        workbook_sha256="",
        rows=stored,
        apply_blockers=_apply_blockers(stored),
        split_apply_readiness=_split_apply_readiness(stored),
    )


def format_preflight_report(result: PreflightResult) -> str:
    counts = result.decision_counts()
    lines = [
        "non_person_entity_review_preflight",
        "parser_valid: yes",
        f"workbook_sha256: {result.workbook_sha256}",
        f"sheet: {FINAL_SHEET_NAME}",
        f"rows: {len(result.rows)}",
    ]
    for decision in EXPECTED_DECISION_COUNTS:
        lines.append(f"{decision}: {counts[decision]}")
    lines.append(f"entity_creation_rows: {len(result.entity_creation_candidate_ids)}")
    ready = sum(1 for item in result.split_apply_readiness if item.apply_ready)
    lines.append(f"split_apply_ready: {ready}")
    lines.append(f"apply_blocker_count: {len(result.apply_blockers)}")
    for blocker in result.apply_blockers:
        candidate = blocker.candidate_id or "-"
        lines.append(f"apply_blocker: {blocker.code} {candidate} {blocker.message}")
    return "\n".join(lines) + "\n"


def read_final_sheet_rows(path: str | Path) -> tuple[ReviewedNonPersonEntityRow, ...]:
    table = _read_final_sheet_table(Path(path))
    header_index = _header_row_index(table)
    rows: list[ReviewedNonPersonEntityRow] = []
    for row_number, cells in table[header_index + 1 :]:
        values = [cells.get(letter, "") for letter in COLUMN_LETTERS]
        if not any(values):
            continue
        if values[0] == "":
            raise PreflightError((f"ROW_WITHOUT_CANDIDATE_ID: row {row_number}",))
        rows.append(
            ReviewedNonPersonEntityRow(
                candidate_id=values[0],
                surface=values[1],
                final_decision=values[2],
                final_canonical=values[3],
                merge_target=values[4],
                final_notes=values[5],
                status=values[6],
                entity_type=values[7],
                entity_subtype=values[8],
            )
        )
    return tuple(rows)


def _validation_errors(
    rows: Sequence[ReviewedNonPersonEntityRow],
) -> list[str]:
    errors: list[str] = []
    if len(rows) != EXPECTED_ROW_COUNT:
        errors.append(f"ROW_COUNT: expected {EXPECTED_ROW_COUNT} got {len(rows)}")

    decisions = ReviewedNonPersonEntityDecision.Decision
    statuses = ReviewedNonPersonEntityDecision.ReviewStatus
    entity_types = set(NonPersonEntity.EntityType.values)
    entity_subtypes = set(NonPersonEntity.EntitySubtype.values)
    counts: Counter[str] = Counter(row.final_decision for row in rows)
    for decision, expected in EXPECTED_DECISION_COUNTS.items():
        got = counts[decision]
        if got != expected:
            errors.append(f"DECISION_COUNT: {decision} expected {expected} got {got}")

    seen: set[str] = set()
    duplicated: set[str] = set()
    for row in rows:
        if row.candidate_id in seen:
            duplicated.add(row.candidate_id)
        seen.add(row.candidate_id)
        if row.final_decision not in decisions.values:
            errors.append(f"INVALID_DECISION: {row.candidate_id} {row.final_decision}")
        if row.status not in statuses.values:
            errors.append(f"INVALID_STATUS: {row.candidate_id} {row.status}")
        errors.extend(_entity_metadata_errors(row, entity_types, entity_subtypes))
    errors.extend(
        f"DUPLICATE_CANDIDATE_ID: {candidate_id}" for candidate_id in sorted(duplicated)
    )
    errors.extend(_merge_errors(rows))
    return errors


def _entity_metadata_errors(
    row: ReviewedNonPersonEntityRow,
    entity_types: set[str],
    entity_subtypes: set[str],
) -> list[str]:
    decisions = ReviewedNonPersonEntityDecision.Decision
    errors: list[str] = []
    if row.entity_type != "" and row.entity_type not in entity_types:
        errors.append(f"INVALID_ENTITY_TYPE: {row.candidate_id} {row.entity_type}")
    if row.entity_subtype != "" and row.entity_subtype not in entity_subtypes:
        errors.append(
            f"INVALID_ENTITY_SUBTYPE: {row.candidate_id} {row.entity_subtype}"
        )
    if row.final_decision == decisions.APPROVE:
        if row.final_canonical == "":
            errors.append(f"APPROVE_MISSING_CANONICAL: {row.candidate_id}")
        if row.entity_type == "":
            errors.append(f"APPROVE_MISSING_ENTITY_TYPE: {row.candidate_id}")
        if row.merge_target != "":
            errors.append(f"APPROVE_HAS_MERGE_TARGET: {row.candidate_id}")
        return errors
    if row.final_decision not in {
        decisions.MERGE,
        decisions.SPLIT,
        decisions.SKIP,
        decisions.NEEDS_RESEARCH,
    }:
        return errors
    if row.entity_type != "":
        errors.append(f"UNEXPECTED_ENTITY_TYPE: {row.candidate_id} {row.entity_type}")
    if row.entity_subtype != "":
        errors.append(
            f"UNEXPECTED_ENTITY_SUBTYPE: {row.candidate_id} {row.entity_subtype}"
        )
    return errors


def _merge_errors(rows: Sequence[ReviewedNonPersonEntityRow]) -> list[str]:
    decisions = ReviewedNonPersonEntityDecision.Decision
    by_id: dict[str, ReviewedNonPersonEntityRow] = {}
    for row in rows:
        by_id.setdefault(row.candidate_id, row)

    errors: list[str] = []
    for row in rows:
        if row.final_decision != decisions.MERGE:
            continue
        if row.merge_target == "":
            errors.append(f"MERGE_MISSING_TARGET: {row.candidate_id}")
            continue
        if row.merge_target == row.candidate_id:
            errors.append(f"MERGE_TO_SELF: {row.candidate_id}")
            continue
        target = by_id.get(row.merge_target)
        if target is None:
            errors.append(
                f"MERGE_TARGET_ABSENT: {row.candidate_id} -> {row.merge_target}"
            )
            continue
        if target.final_decision != decisions.APPROVE:
            errors.append(
                f"MERGE_TARGET_NOT_APPROVE: {row.candidate_id} -> {row.merge_target}"
            )
            continue
        if row.final_canonical != target.final_canonical:
            errors.append(f"MERGE_CANONICAL_MISMATCH: {row.candidate_id}")
    errors.extend(_merge_cycle_errors(rows))
    return errors


def _merge_cycle_errors(rows: Sequence[ReviewedNonPersonEntityRow]) -> list[str]:
    decisions = ReviewedNonPersonEntityDecision.Decision
    edges: dict[str, str] = {}
    for row in rows:
        if row.final_decision != decisions.MERGE:
            continue
        if row.merge_target == "" or row.merge_target == row.candidate_id:
            continue
        edges[row.candidate_id] = row.merge_target

    cycles: set[tuple[str, ...]] = set()
    for start in sorted(edges):
        trail: list[str] = []
        index: dict[str, int] = {}
        node = start
        while node in edges and node not in index:
            index[node] = len(trail)
            trail.append(node)
            node = edges[node]
        if node not in index:
            continue
        cycle = trail[index[node] :]
        rotations = [tuple(cycle[i:] + cycle[:i]) for i in range(len(cycle))]
        cycles.add(min(rotations))

    return [
        "MERGE_CYCLE: " + " -> ".join((*cycle, cycle[0])) for cycle in sorted(cycles)
    ]


def _apply_blockers(
    rows: Sequence[ReviewedNonPersonEntityRow],
) -> tuple[ApplyBlocker, ...]:
    by_id = {row.candidate_id: row for row in rows}
    blockers = [
        *_ec0009_blockers(by_id),
        *_ec0045_blockers(by_id, rows),
        ApplyBlocker(
            code="ALIASES_NOT_MACHINE_READABLE",
            candidate_id="",
            message=(
                "v3 contains no machine-readable authoritative alias contract; "
                "alias names and kinds are not parsed from final_notes; "
                "alias application remains blocked until a separate "
                "authoritative alias table or sheet exists"
            ),
        ),
    ]
    return tuple(sorted(blockers, key=lambda blocker: blocker.code))


def _is_approve(
    by_id: dict[str, ReviewedNonPersonEntityRow],
    candidate_id: str,
) -> bool:
    target = by_id.get(candidate_id)
    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    return target is not None and target.final_decision == approve


def _ec0009_blockers(
    by_id: dict[str, ReviewedNonPersonEntityRow],
) -> list[ApplyBlocker]:
    row = by_id.get("EC0009")
    split = ReviewedNonPersonEntityDecision.Decision.SPLIT
    if row is None or row.final_decision != split:
        return []

    targets_exist = _is_approve(by_id, "EC0219") and _is_approve(by_id, "EC0006")
    if targets_exist:
        target_blocker = ApplyBlocker(
            code="EC0009_TARGETS_EXIST",
            candidate_id="EC0009",
            message=(
                "targets EC0219 and EC0006 exist as APPROVE rows; "
                "split routing is not applyable"
            ),
        )
    else:
        target_blocker = ApplyBlocker(
            code="EC0009_TARGETS_MISSING",
            candidate_id="EC0009",
            message="APPROVE targets EC0219 and EC0006 are not both present",
        )
    return [
        target_blocker,
        ApplyBlocker(
            code="EC0009_NO_GLOBAL_PALESTINE_ALIAS",
            candidate_id="EC0009",
            message="no global Palestine alias may be inferred",
        ),
        ApplyBlocker(
            code="EC0009_ROUTING_PROSE_ONLY",
            candidate_id="EC0009",
            message="routing is prose-only",
        ),
        ApplyBlocker(
            code="EC0009_OCCURRENCE_KEYS_UNPINNED",
            candidate_id="EC0009",
            message="occurrence keys are not pinned",
        ),
        ApplyBlocker(
            code="EC0009_ITEM_353_ORDINALS_UNAPPROVED",
            candidate_id="EC0009",
            message="item 353 says both mentions, not approved ordinal numbers",
        ),
    ]


def _ec0045_blockers(
    by_id: dict[str, ReviewedNonPersonEntityRow],
    rows: Sequence[ReviewedNonPersonEntityRow],
) -> list[ApplyBlocker]:
    row = by_id.get("EC0045")
    split = ReviewedNonPersonEntityDecision.Decision.SPLIT
    if row is None or row.final_decision != split:
        return []

    approve = ReviewedNonPersonEntityDecision.Decision.APPROVE
    blockers = [
        ApplyBlocker(
            code="EC0045_EGYPTIAN_UNRESOLVED",
            candidate_id="EC0045",
            message="Egyptian referent remains unresolved",
        ),
        ApplyBlocker(
            code="EC0045_NO_OCCURRENCE_MAPPING",
            candidate_id="EC0045",
            message="no authoritative item/ordinal occurrence mapping exists",
        ),
    ]
    german_is_approve = any(
        item.final_decision == approve
        and item.final_canonical == GERMAN_REICH_MINISTRY_CANONICAL
        for item in rows
    )
    if not german_is_approve:
        blockers.append(
            ApplyBlocker(
                code="EC0045_GERMAN_CANONICAL_NOT_APPROVE",
                candidate_id="EC0045",
                message=(
                    "German referent canonical "
                    f"{GERMAN_REICH_MINISTRY_CANONICAL} "
                    "is not represented as an APPROVE candidate in FINAL"
                ),
            )
        )
    return blockers


def _split_apply_readiness(
    rows: Sequence[ReviewedNonPersonEntityRow],
) -> tuple[SplitApplyReadiness, ...]:
    split = ReviewedNonPersonEntityDecision.Decision.SPLIT
    return tuple(
        SplitApplyReadiness(
            candidate_id=row.candidate_id,
            apply_ready=False,
            reason=SPLIT_NOT_APPLYABLE_REASON,
        )
        for row in rows
        if row.final_decision == split
    )


def _header_row_index(table: list[tuple[int, dict[str, str]]]) -> int:
    matches = [
        index
        for index, (_, cells) in enumerate(table)
        if cells.get("A", "") == REQUIRED_COLUMNS[0]
    ]
    if not matches:
        raise PreflightError(("MISSING_HEADER: candidate_id",))
    if len(matches) > 1:
        raise PreflightError(("AMBIGUOUS_HEADER: candidate_id",))
    cells = table[matches[0]][1]
    missing = [
        column
        for column, letter in zip(REQUIRED_COLUMNS, COLUMN_LETTERS, strict=True)
        if cells.get(letter, "") != column
    ]
    if missing:
        joined = ", ".join(missing)
        raise PreflightError((f"MISSING_COLUMN: {joined}",))
    return matches[0]


def _read_final_sheet_table(path: Path) -> list[tuple[int, dict[str, str]]]:
    try:
        with zipfile.ZipFile(path) as workbook:
            sheet_name = _final_sheet_xml_name(workbook)
            root = ET.fromstring(workbook.read(sheet_name))
            shared_strings = (
                _shared_strings(workbook)
                if _worksheet_uses_shared_strings(root)
                else []
            )
    except PreflightError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise PreflightError((f"WORKBOOK_UNREADABLE: {exc}",)) from exc

    table: list[tuple[int, dict[str, str]]] = []
    for row in root.findall(f"{{{_MAIN_NS}}}sheetData/{{{_MAIN_NS}}}row"):
        row_number = int(row.attrib.get("r", "0"))
        cells: dict[str, str] = {}
        for cell in row.findall(f"{{{_MAIN_NS}}}c"):
            ref = cell.attrib.get("r", "")
            letter = "".join(character for character in ref if character.isalpha())
            if letter:
                cells[letter] = _cell_text(cell, shared_strings)
        table.append((row_number, cells))
    return table


def _final_sheet_xml_name(workbook: zipfile.ZipFile) -> str:
    workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    relation_id = ""
    for sheet in workbook_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        if sheet.attrib.get("name") == FINAL_SHEET_NAME:
            relation_id = sheet.attrib[f"{{{_OFFICE_REL_NS}}}id"]
            break
    if not relation_id:
        raise PreflightError((f"MISSING_SHEET: {FINAL_SHEET_NAME}",))
    target = targets[relation_id].lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _worksheet_uses_shared_strings(root: ET.Element) -> bool:
    return any(
        cell.attrib.get("t") == "s" for cell in root.findall(f".//{{{_MAIN_NS}}}c")
    )


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.findall(f"{{{_MAIN_NS}}}si"):
        strings.append(
            "".join(text.text or "" for text in item.findall(f".//{{{_MAIN_NS}}}t"))
        )
    return strings


def _cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(f".//{{{_MAIN_NS}}}t"))
    value = cell.find(f"{{{_MAIN_NS}}}v")
    if value is None or value.text is None:
        return ""
    if cell_type == "s":
        return shared_strings[int(value.text)]
    return value.text
