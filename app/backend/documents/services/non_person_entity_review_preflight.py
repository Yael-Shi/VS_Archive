"""Read-only preflight for the v6 non-person entity review workbook.

Parses and validates the authoritative FINAL, alias, and split-routing sheets.
Does not write to the database, does not create aliases or occurrences, and
does not read final_notes or route notes for alias kinds or occurrence routing.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from xml.etree import ElementTree as ET

from documents.models import (
    ArchiveItemEntityOccurrence,
    NonPersonEntity,
    NonPersonEntityAlias,
    ReviewedNonPersonEntityDecision,
)

AUTHORITATIVE_WORKBOOK_SHA256 = (
    "c17a5d52ad531ca144ce113cab5abbdd252f752d62c765451769705ee536f948"
)
FINAL_SHEET_NAME = "FINAL_RECON_2026-10-04"
FINAL_ALIAS_SHEET_NAME = "FINAL_ALIASES"
ALIAS_REVIEW_SHEET_NAME = "ALIAS_REVIEW_REQUIRED"
FINAL_SPLIT_ROUTING_SHEET_NAME = "FINAL_SPLIT_ROUTING"
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
ALIAS_COLUMNS = (
    "target_candidate_id",
    "target_canonical",
    "alias_name",
    "alias_kind",
    "source_candidate_id",
    "review_status",
    "basis",
)
ALIAS_REVIEW_COLUMNS = (
    "target_candidate_id",
    "target_canonical",
    "alias_name",
    "kind_issue_or_suggestion",
    "source_candidate_id",
    "reason_for_review",
)
SPLIT_ROUTE_COLUMNS = (
    "split_candidate_id",
    "archive_item_id",
    "surface",
    "occurrence_ordinal",
    "target_candidate_id",
    "target_canonical",
    "resolution_status",
    "normalization_version",
    "text_kind",
    "source_text_sha256",
    "pin_status",
    "review_status",
    "notes",
)
EXPECTED_ROW_COUNT = 109
EXPECTED_ALIAS_COUNT = 63
EXPECTED_SPLIT_ROUTE_COUNTS = MappingProxyType(
    {
        "EC0009": 4,
        "EC0045": 2,
    }
)
EXPECTED_SPLIT_ROUTE_COUNT = sum(EXPECTED_SPLIT_ROUTE_COUNTS.values())
ALIAS_REVIEW_CLOSED_STATUS = (
    "No unresolved alias decisions remain. All alias-review items are "
    "user-approved or explicitly excluded as of 2026-10-05."
)
EXPECTED_DECISION_COUNTS = {
    ReviewedNonPersonEntityDecision.Decision.APPROVE: 70,
    ReviewedNonPersonEntityDecision.Decision.MERGE: 28,
    ReviewedNonPersonEntityDecision.Decision.SKIP: 8,
    ReviewedNonPersonEntityDecision.Decision.SPLIT: 2,
    ReviewedNonPersonEntityDecision.Decision.NEEDS_RESEARCH: 1,
}
SURFACE_V1 = "surface-v1"
ALIAS_REVIEW_STATUS_APPROVED = "APPROVED"
SPLIT_ROUTE_PIN_STATUS = "PINNED"
SPLIT_ROUTE_REVIEW_STATUS = "APPROVED"
SPLIT_APPLY_READY_REASON = "structured FINAL_SPLIT_ROUTING is complete"

_POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BIDI_MARKS = dict.fromkeys(
    map(
        ord,
        "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069",
    ),
    None,
)
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
class ReviewedAliasRow:
    """One FINAL_ALIASES row. source_candidate_id is audit provenance only."""

    target_candidate_id: str
    target_canonical: str
    alias_name: str
    alias_kind: str
    source_candidate_id: str
    review_status: str
    basis: str


@dataclass(frozen=True)
class ReviewedSplitRoute:
    """One FINAL_SPLIT_ROUTING row. notes are not a routing source."""

    split_candidate_id: str
    archive_item_id: str
    surface: str
    occurrence_ordinal: str
    target_candidate_id: str
    target_canonical: str
    resolution_status: str
    normalization_version: str
    text_kind: str
    source_text_sha256: str
    pin_status: str
    review_status: str
    notes: str


@dataclass(frozen=True)
class ApplyBlocker:
    code: str
    candidate_id: str
    message: str


@dataclass(frozen=True)
class SplitApplyReadiness:
    """Set only after structured routing validates. Notes are not a plan."""

    candidate_id: str
    apply_ready: bool
    reason: str


@dataclass(frozen=True)
class PreflightResult:
    workbook_sha256: str
    rows: tuple[ReviewedNonPersonEntityRow, ...]
    aliases: tuple[ReviewedAliasRow, ...]
    split_routes: tuple[ReviewedSplitRoute, ...]
    unresolved_alias_reviews: int
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


def normalize_surface_v1(surface: str) -> str:
    """Normalize one surface with the occurrence surface-v1 contract.

    NFC, strip bidi marks, casefold, trim, and collapse internal whitespace.
    Punctuation and Hebrew prefixes are kept. There is no final-letter folding.
    """

    text = unicodedata.normalize("NFC", surface)
    text = text.translate(_BIDI_MARKS)
    text = text.casefold().strip()
    return re.sub(r"\s+", " ", text)


def sha256_path(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preflight_authoritative_workbook(path: str | Path) -> PreflightResult:
    """Parse the v6 workbook and require its fixed SHA-256."""

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

    unresolved_ids = read_unresolved_alias_review_ids(workbook_path)
    result = validate_review_contract(
        read_final_sheet_rows(workbook_path),
        read_alias_rows(workbook_path),
        read_split_route_rows(workbook_path),
        unresolved_alias_review_ids=unresolved_ids,
    )
    return replace(result, workbook_sha256=actual)


def validate_review_contract(
    rows: Sequence[ReviewedNonPersonEntityRow],
    aliases: Sequence[ReviewedAliasRow],
    split_routes: Sequence[ReviewedSplitRoute],
    *,
    unresolved_alias_review_ids: Sequence[str] = (),
) -> PreflightResult:
    errors = [
        *_validation_errors(rows),
        *_alias_errors(rows, aliases),
        *_split_route_errors(rows, split_routes),
    ]
    if unresolved_alias_review_ids:
        joined = ", ".join(unresolved_alias_review_ids)
        errors.append(
            f"UNRESOLVED_ALIAS_REVIEW: {len(unresolved_alias_review_ids)} {joined}"
        )
    if errors:
        raise PreflightError(tuple(sorted(set(errors))))

    stored_rows = tuple(rows)
    return PreflightResult(
        workbook_sha256="",
        rows=stored_rows,
        aliases=tuple(aliases),
        split_routes=tuple(split_routes),
        unresolved_alias_reviews=0,
        apply_blockers=(),
        split_apply_readiness=_split_apply_readiness(stored_rows, split_routes),
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
    lines.extend(
        [
            f"aliases: {len(result.aliases)}",
            f"split_routes: {len(result.split_routes)}",
            f"unresolved_alias_reviews: {result.unresolved_alias_reviews}",
            f"entity_creation_rows: {len(result.entity_creation_candidate_ids)}",
            (
                "split_apply_ready: "
                f"{sum(1 for item in result.split_apply_readiness if item.apply_ready)}"
            ),
            f"apply_blocker_count: {len(result.apply_blockers)}",
        ]
    )
    for blocker in result.apply_blockers:
        candidate = blocker.candidate_id or "-"
        lines.append(f"apply_blocker: {blocker.code} {candidate} {blocker.message}")
    return "\n".join(lines) + "\n"


def read_final_sheet_rows(path: str | Path) -> tuple[ReviewedNonPersonEntityRow, ...]:
    rows: list[ReviewedNonPersonEntityRow] = []
    for row_number, values in _rows_after_header(
        _read_sheet_table(Path(path), FINAL_SHEET_NAME),
        REQUIRED_COLUMNS,
    ):
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


def read_alias_rows(path: str | Path) -> tuple[ReviewedAliasRow, ...]:
    rows: list[ReviewedAliasRow] = []
    for _row_number, values in _rows_after_header(
        _read_sheet_table(Path(path), FINAL_ALIAS_SHEET_NAME),
        ALIAS_COLUMNS,
    ):
        rows.append(
            ReviewedAliasRow(
                target_candidate_id=values[0],
                target_canonical=values[1],
                alias_name=values[2],
                alias_kind=values[3],
                source_candidate_id=values[4],
                review_status=values[5],
                basis=values[6],
            )
        )
    return tuple(rows)


def read_split_route_rows(path: str | Path) -> tuple[ReviewedSplitRoute, ...]:
    rows: list[ReviewedSplitRoute] = []
    for _row_number, values in _rows_after_header(
        _read_sheet_table(Path(path), FINAL_SPLIT_ROUTING_SHEET_NAME),
        SPLIT_ROUTE_COLUMNS,
    ):
        rows.append(
            ReviewedSplitRoute(
                split_candidate_id=values[0],
                archive_item_id=values[1],
                surface=values[2],
                occurrence_ordinal=values[3],
                target_candidate_id=values[4],
                target_canonical=values[5],
                resolution_status=values[6],
                normalization_version=values[7],
                text_kind=values[8],
                source_text_sha256=values[9],
                pin_status=values[10],
                review_status=values[11],
                notes=values[12],
            )
        )
    return tuple(rows)


def read_unresolved_alias_review_ids(path: str | Path) -> tuple[str, ...]:
    """Return unresolved ALIAS_REVIEW_REQUIRED rows.

    Blank rows are ignored. The one known closed status sentence is ignored
    only when it is the entire row. Every other non-empty row is unresolved.
    """

    table = _read_sheet_table(Path(path), ALIAS_REVIEW_SHEET_NAME)
    header_index = _header_row_index(table, ALIAS_REVIEW_COLUMNS)
    letters = _column_letters(len(ALIAS_REVIEW_COLUMNS))
    candidate_ids: list[str] = []
    for row_number, cells in table[header_index + 1 :]:
        values = [cells.get(letter, "") for letter in letters]
        if not any(values) or _is_closed_alias_review_status(values):
            continue
        candidate_ids.append(values[0] or f"row {row_number}")
    return tuple(candidate_ids)


def _is_closed_alias_review_status(values: Sequence[str]) -> bool:
    return values[0] == ALIAS_REVIEW_CLOSED_STATUS and not any(values[1:])


def _validation_errors(rows: Sequence[ReviewedNonPersonEntityRow]) -> list[str]:
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


def _alias_errors(
    rows: Sequence[ReviewedNonPersonEntityRow],
    aliases: Sequence[ReviewedAliasRow],
) -> list[str]:
    errors: list[str] = []
    if len(aliases) != EXPECTED_ALIAS_COUNT:
        errors.append(
            f"ALIAS_COUNT: expected {EXPECTED_ALIAS_COUNT} got {len(aliases)}"
        )

    decisions = ReviewedNonPersonEntityDecision.Decision
    kinds = set(NonPersonEntityAlias.Kind.values)
    by_id = {row.candidate_id: row for row in rows}
    seen: set[tuple[str, str]] = set()
    duplicated: set[tuple[str, str]] = set()
    for alias in aliases:
        if alias.target_candidate_id == "":
            errors.append("ALIAS_TARGET_MISSING")
            continue
        if alias.alias_name == "":
            errors.append(f"ALIAS_NAME_BLANK: {alias.target_candidate_id}")
        if alias.alias_kind not in kinds:
            errors.append(
                f"INVALID_ALIAS_KIND: {alias.target_candidate_id} {alias.alias_kind}"
            )
        if alias.review_status != ALIAS_REVIEW_STATUS_APPROVED:
            errors.append(
                "ALIAS_REVIEW_STATUS_NOT_APPROVED: "
                f"{alias.target_candidate_id} {alias.review_status}"
            )
        pair = (alias.target_candidate_id, alias.alias_name)
        if alias.alias_name != "" and pair in seen:
            duplicated.add(pair)
        if alias.alias_name != "":
            seen.add(pair)
        target = by_id.get(alias.target_candidate_id)
        if target is None:
            errors.append(f"ALIAS_TARGET_ABSENT: {alias.target_candidate_id}")
            continue
        if target.final_decision != decisions.APPROVE:
            errors.append(f"ALIAS_TARGET_NOT_APPROVE: {alias.target_candidate_id}")
            continue
        if alias.target_canonical != target.final_canonical:
            errors.append(f"ALIAS_CANONICAL_MISMATCH: {alias.target_candidate_id}")
    errors.extend(
        f"DUPLICATE_ALIAS: {target_id} {alias_name}"
        for target_id, alias_name in sorted(duplicated)
    )
    return errors


def _split_route_errors(
    rows: Sequence[ReviewedNonPersonEntityRow],
    split_routes: Sequence[ReviewedSplitRoute],
) -> list[str]:
    errors: list[str] = []
    if len(split_routes) != EXPECTED_SPLIT_ROUTE_COUNT:
        errors.append(
            "SPLIT_ROUTE_COUNT: "
            f"expected {EXPECTED_SPLIT_ROUTE_COUNT} got {len(split_routes)}"
        )

    decisions = ReviewedNonPersonEntityDecision.Decision
    text_kinds = set(ArchiveItemEntityOccurrence.TextKind.values)
    resolved = ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
    by_id = {row.candidate_id: row for row in rows}
    identities: set[tuple[object, ...]] = set()
    duplicated: set[tuple[object, ...]] = set()
    routed_split_ids: set[str] = set()
    route_counts: Counter[str] = Counter()

    for route in split_routes:
        route_counts[route.split_candidate_id] += 1
        split_row = by_id.get(route.split_candidate_id)
        if route.split_candidate_id == "" or split_row is None:
            errors.append(f"SPLIT_CANDIDATE_ABSENT: {route.split_candidate_id}")
        elif split_row.final_decision != decisions.SPLIT:
            errors.append(f"SPLIT_CANDIDATE_NOT_SPLIT: {route.split_candidate_id}")
        else:
            routed_split_ids.add(route.split_candidate_id)

        target = by_id.get(route.target_candidate_id)
        if route.target_candidate_id == "" or target is None:
            errors.append(
                "ROUTE_TARGET_ABSENT: "
                f"{route.split_candidate_id} -> {route.target_candidate_id}"
            )
        elif target.final_decision != decisions.APPROVE:
            errors.append(
                "ROUTE_TARGET_NOT_APPROVE: "
                f"{route.split_candidate_id} -> {route.target_candidate_id}"
            )
        elif route.target_canonical != target.final_canonical:
            errors.append(f"ROUTE_CANONICAL_MISMATCH: {route.split_candidate_id}")

        if not _POSITIVE_INT_RE.fullmatch(route.archive_item_id):
            errors.append(
                f"INVALID_ARCHIVE_ITEM_ID: {route.split_candidate_id} "
                f"{route.archive_item_id}"
            )
        if not _POSITIVE_INT_RE.fullmatch(route.occurrence_ordinal):
            errors.append(
                f"INVALID_OCCURRENCE_ORDINAL: {route.split_candidate_id} "
                f"{route.occurrence_ordinal}"
            )
        if route.surface == "" or normalize_surface_v1(route.surface) == "":
            errors.append(f"ROUTE_SURFACE_BLANK: {route.split_candidate_id}")
        if route.resolution_status != resolved:
            errors.append(
                f"INVALID_RESOLUTION_STATUS: {route.split_candidate_id} "
                f"{route.resolution_status}"
            )
        if route.normalization_version != SURFACE_V1:
            errors.append(
                f"INVALID_NORMALIZATION_VERSION: {route.split_candidate_id} "
                f"{route.normalization_version}"
            )
        if route.text_kind not in text_kinds:
            errors.append(
                f"INVALID_TEXT_KIND: {route.split_candidate_id} {route.text_kind}"
            )
        if not _SHA256_RE.fullmatch(route.source_text_sha256):
            errors.append(f"MALFORMED_SOURCE_TEXT_SHA256: {route.split_candidate_id}")
        if route.pin_status != SPLIT_ROUTE_PIN_STATUS:
            errors.append(f"ROUTE_NOT_PINNED: {route.split_candidate_id}")
        if route.review_status != SPLIT_ROUTE_REVIEW_STATUS:
            errors.append(f"ROUTE_NOT_APPROVED: {route.split_candidate_id}")

        identity = _occurrence_identity(route, text_kinds)
        if identity is None:
            continue
        if identity in identities:
            duplicated.add(identity)
        identities.add(identity)

    for candidate_id, expected in EXPECTED_SPLIT_ROUTE_COUNTS.items():
        got = route_counts[candidate_id]
        if got != expected:
            errors.append(
                "SPLIT_CANDIDATE_ROUTE_COUNT: "
                f"{candidate_id} expected {expected} got {got}"
            )
    for row in rows:
        if row.final_decision != decisions.SPLIT:
            continue
        if row.candidate_id not in routed_split_ids:
            errors.append(f"SPLIT_ROUTES_MISSING: {row.candidate_id}")
        if row.candidate_id not in EXPECTED_SPLIT_ROUTE_COUNTS:
            errors.append(f"UNEXPECTED_SPLIT_CANDIDATE: {row.candidate_id}")
    errors.extend(
        "DUPLICATE_OCCURRENCE_IDENTITY: " + " ".join(str(part) for part in identity)
        for identity in sorted(duplicated)
    )
    return errors


def _occurrence_identity(
    route: ReviewedSplitRoute,
    text_kinds: set[str],
) -> tuple[object, ...] | None:
    if not _POSITIVE_INT_RE.fullmatch(route.archive_item_id):
        return None
    if not _POSITIVE_INT_RE.fullmatch(route.occurrence_ordinal):
        return None
    if route.text_kind not in text_kinds:
        return None
    if not _SHA256_RE.fullmatch(route.source_text_sha256):
        return None
    if route.normalization_version != SURFACE_V1:
        return None
    normalized = normalize_surface_v1(route.surface)
    if normalized == "":
        return None
    return (
        int(route.archive_item_id),
        route.text_kind,
        route.source_text_sha256,
        route.normalization_version,
        normalized,
        int(route.occurrence_ordinal),
    )


def _split_apply_readiness(
    rows: Sequence[ReviewedNonPersonEntityRow],
    split_routes: Sequence[ReviewedSplitRoute],
) -> tuple[SplitApplyReadiness, ...]:
    split = ReviewedNonPersonEntityDecision.Decision.SPLIT
    route_counts = Counter(route.split_candidate_id for route in split_routes)
    readiness: list[SplitApplyReadiness] = []
    for row in rows:
        if row.final_decision != split:
            continue
        expected = EXPECTED_SPLIT_ROUTE_COUNTS.get(row.candidate_id)
        actual = route_counts[row.candidate_id]
        apply_ready = expected is not None and actual == expected
        if apply_ready:
            reason = SPLIT_APPLY_READY_REASON
        else:
            reason = (
                f"structured FINAL_SPLIT_ROUTING count expected {expected} got {actual}"
            )
        readiness.append(
            SplitApplyReadiness(
                candidate_id=row.candidate_id,
                apply_ready=apply_ready,
                reason=reason,
            )
        )
    return tuple(readiness)


def _column_letters(count: int) -> tuple[str, ...]:
    return tuple(chr(ord("A") + index) for index in range(count))


def _rows_after_header(
    table: list[tuple[int, dict[str, str]]],
    columns: Sequence[str],
) -> list[tuple[int, list[str]]]:
    header_index = _header_row_index(table, columns)
    letters = _column_letters(len(columns))
    rows: list[tuple[int, list[str]]] = []
    for row_number, cells in table[header_index + 1 :]:
        values = [cells.get(letter, "") for letter in letters]
        if any(values):
            rows.append((row_number, values))
    return rows


def _header_row_index(
    table: list[tuple[int, dict[str, str]]],
    columns: Sequence[str],
) -> int:
    letters = _column_letters(len(columns))
    matches = [
        index
        for index, (_, cells) in enumerate(table)
        if cells.get(letters[0], "") == columns[0]
    ]
    if not matches:
        raise PreflightError((f"MISSING_HEADER: {columns[0]}",))
    if len(matches) > 1:
        raise PreflightError((f"AMBIGUOUS_HEADER: {columns[0]}",))
    cells = table[matches[0]][1]
    missing = [
        column
        for column, letter in zip(columns, letters, strict=True)
        if cells.get(letter, "") != column
    ]
    if missing:
        joined = ", ".join(missing)
        raise PreflightError((f"MISSING_COLUMN: {joined}",))
    return matches[0]


def _read_sheet_table(path: Path, sheet_name: str) -> list[tuple[int, dict[str, str]]]:
    try:
        with zipfile.ZipFile(path) as workbook:
            sheet_xml_name = _sheet_xml_name(workbook, sheet_name)
            root = ET.fromstring(workbook.read(sheet_xml_name))
            shared_strings = (
                _shared_strings(workbook)
                if _worksheet_uses_shared_strings(root)
                else []
            )
    except PreflightError:
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        raise PreflightError((f"WORKBOOK_UNREADABLE: {exc}",)) from exc
    return _table_from_root(root, shared_strings)


def _table_from_root(
    root: ET.Element,
    shared_strings: list[str],
) -> list[tuple[int, dict[str, str]]]:
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


def _sheet_xml_name(workbook: zipfile.ZipFile, sheet_name: str) -> str:
    workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels_root.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    relation_id = ""
    for sheet in workbook_root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        if sheet.attrib.get("name") == sheet_name:
            relation_id = sheet.attrib[f"{{{_OFFICE_REL_NS}}}id"]
            break
    if not relation_id:
        raise PreflightError((f"MISSING_SHEET: {sheet_name}",))
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
