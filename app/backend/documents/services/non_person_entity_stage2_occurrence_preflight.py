"""Read-only preflight for one reconstructed stage-2 occurrence import.

Compares the candidate CSV with current authoritative text, reviewed
decisions, and effective corrections. It does not insert occurrences, approve
candidates, or update ``approved_for_apply``. Pending proposals, including the
Israel occurrence proposals, are only reported when a six-field identity
overlaps. They are not resolved.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from django.db.models import Prefetch, Q

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Document,
    NonPersonEntityCorrection,
    NonPersonEntityOccurrenceProposal,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_bnei_akiva_shadow_correction import (
    OCCURRENCE_IDENTITIES,
    SURVIVOR_ENTITY_ID,
)
from documents.services.non_person_entity_correction import (
    effective_entity_for_decision,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    authoritative_displayed_text,
    item_supports_occurrence_text_kind,
    locate_surface_occurrences,
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_review_preflight import (
    FIFTH_DECISION_SOURCE,
    FOURTH_DECISION_SOURCE,
    SECOND_DECISION_SOURCE,
    SIXTH_DECISION_SOURCE,
    THIRD_DECISION_SOURCE,
    V6_DECISION_SOURCE,
)
from documents.services.text_presentation import text_presentation_results_prefetch

RECORDED_CANDIDATE_SHA256 = (
    "c7d7a4770487db457ca915c9286692451bfb4628f8f1235bb199e1ce4545fb4f"
)
TECHNICAL_CANDIDATE = "TECHNICAL_CANDIDATE"
OCCURRENCE_98_ID = 98

_CONTRACT_SOURCES = {
    "v6": V6_DECISION_SOURCE,
    "second": SECOND_DECISION_SOURCE,
    "third": THIRD_DECISION_SOURCE,
    "fourth": FOURTH_DECISION_SOURCE,
    "fifth": FIFTH_DECISION_SOURCE,
    "sixth": SIXTH_DECISION_SOURCE,
}
_REQUIRED_COLUMNS = (
    "mention_index",
    "archive_item_id",
    "proposed_candidate_id",
    "ledger_latest_contract",
    "dev_decision_ids",
    "dev_decision_action",
    "dev_effective_entity_id",
    "current_text_kind",
    "reconstructed_start",
    "reconstructed_end",
    "reconstructed_text",
    "computed_normalized_surface",
    "computed_occurrence_ordinal",
    "computed_source_sha256",
    "post_merge_technical_status",
    "approved_for_apply",
)
_REPORT_FIELDS = (
    "mention_index",
    "archive_item_id",
    "proposed_candidate_id",
    "status",
    "explanation",
    "live_effective_entity_id",
    "csv_effective_entity_id",
    "live_decision_id",
    "existing_occurrence_id",
    "proposal_id",
    "approved_for_apply",
    "write_authorized",
)
_STATUS_PRIORITY = (
    "invalid_row",
    "out_of_scope",
    "duplicate_mention",
    "duplicate_identity",
    "excluded_occurrence_98",
    "source_item_missing",
    "unsupported_text_kind",
    "source_missing",
    "source_sha_drift",
    "surface_drift",
    "source_ambiguous",
    "boundary_drift",
    "source_not_located",
    "decision_contract_unknown",
    "decision_missing",
    "decision_identity_drift",
    "decision_action_drift",
    "entity_unresolved",
    "retired_entity",
    "entity_drift",
    "identity_collision",
    "already_linked",
    "proposal_overlap",
    "eligible",
)
_OCCURRENCE_98 = next(
    identity
    for identity in OCCURRENCE_IDENTITIES
    if identity.occurrence_id == OCCURRENCE_98_ID
)
_OCCURRENCE_98_IDENTITY = (
    _OCCURRENCE_98.archive_item_id,
    _OCCURRENCE_98.text_kind,
    _OCCURRENCE_98.source_text_sha256,
    _OCCURRENCE_98.normalization_version,
    _OCCURRENCE_98.normalized_surface,
    _OCCURRENCE_98.occurrence_ordinal,
)


class Stage2OccurrencePreflightError(Exception):
    """The candidate file could not be read."""


@dataclass(frozen=True)
class Stage2OccurrencePreflight:
    clean: bool
    file_sha256: str
    expected_sha256: str
    row_count: int
    eligible_count: int
    status_counts: Mapping[str, int]
    occurrence_98_ok: bool
    file_errors: tuple[str, ...]
    summary: str
    output_dir: Path


@dataclass
class _Finding:
    status: str
    message: str


@dataclass
class _Row:
    mention_index: str
    archive_item_id: int | None
    proposed_candidate_id: str
    contract: str
    decision_id: int | None
    decision_action: str
    csv_entity_id: int | None
    text_kind: str
    start: int | None
    end: int | None
    reconstructed_text: str
    normalized_surface: str
    ordinal: int | None
    source_sha256: str
    technical_status: str
    approved_for_apply: str
    identity: tuple | None
    findings: list[_Finding] = field(default_factory=list)
    live_entity_id: int | None = None
    live_decision_id: int | None = None
    existing_occurrence_id: int | None = None
    proposal_id: int | None = None

    @property
    def status(self) -> str:
        found = {finding.status for finding in self.findings}
        for status in _STATUS_PRIORITY:
            if status in found:
                return status
        return "invalid_row"

    @property
    def explanation(self) -> str:
        ordered = sorted(
            self.findings,
            key=lambda finding: _STATUS_PRIORITY.index(finding.status),
        )
        return "; ".join(finding.message for finding in ordered)


def preflight_stage2_occurrence_links(
    csv_path: str | Path,
    *,
    expected_sha256: str,
    output_dir: str | Path,
) -> Stage2OccurrencePreflight:
    """Validate one candidate CSV. Writes report files and no database rows."""

    path = Path(csv_path)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise Stage2OccurrencePreflightError(
            f"could not read candidate: {exc}"
        ) from exc
    file_sha = hashlib.sha256(payload).hexdigest()
    expected = expected_sha256.strip().lower()
    if file_sha != expected:
        result = _finish(
            destination,
            file_sha256=file_sha,
            expected_sha256=expected,
            rows=[],
            file_errors=(
                f"candidate sha256 {file_sha} does not match expected {expected}",
            ),
            occurrence_98_ok=False,
        )
        return result
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeError as exc:
        raise Stage2OccurrencePreflightError("candidate is not utf-8") from exc
    reader = csv.DictReader(text.splitlines())
    fieldnames = tuple(reader.fieldnames or ())
    missing = [name for name in _REQUIRED_COLUMNS if name not in fieldnames]
    parsed = [_parse_row(raw) for raw in reader]
    file_errors: list[str] = []
    if missing:
        file_errors.append("missing columns: " + ", ".join(missing))
    _mark_duplicates(parsed)
    if not file_errors:
        _classify_against_database(parsed)
    occurrence_98 = _occurrence_98_report(parsed)
    if not occurrence_98["ok"]:
        file_errors.extend(occurrence_98["errors"])
    return _finish(
        destination,
        file_sha256=file_sha,
        expected_sha256=expected,
        rows=parsed,
        file_errors=tuple(file_errors),
        occurrence_98_ok=bool(occurrence_98["ok"]),
        occurrence_98=occurrence_98,
    )


def _parse_row(raw: dict[str, str]) -> _Row:
    findings: list[_Finding] = []

    def text(name: str) -> str:
        return (raw.get(name) or "").strip()

    def integer(name: str) -> int | None:
        value = text(name)
        if not value.isdigit() or int(value) < 1:
            findings.append(
                _Finding("invalid_row", f"{name} must be a positive integer")
            )
            return None
        return int(value)

    start = integer("reconstructed_start") if text("reconstructed_start") else None
    if text("reconstructed_start") and start is None:
        pass
    elif text("reconstructed_start") == "":
        findings.append(_Finding("invalid_row", "reconstructed_start is required"))
    end = integer("reconstructed_end") if text("reconstructed_end") else None
    if text("reconstructed_end") == "":
        findings.append(_Finding("invalid_row", "reconstructed_end is required"))
    ordinal = (
        integer("computed_occurrence_ordinal")
        if text("computed_occurrence_ordinal")
        else None
    )
    if text("computed_occurrence_ordinal") == "":
        findings.append(
            _Finding("invalid_row", "computed_occurrence_ordinal is required")
        )
    mention = text("mention_index")
    if not mention:
        findings.append(_Finding("invalid_row", "mention_index is required"))
    item_id = integer("archive_item_id") if text("archive_item_id") else None
    if text("archive_item_id") == "":
        findings.append(_Finding("invalid_row", "archive_item_id is required"))
    decision_id = integer("dev_decision_ids") if text("dev_decision_ids") else None
    if text("dev_decision_ids") == "":
        findings.append(_Finding("invalid_row", "dev_decision_ids is required"))
    csv_entity = (
        integer("dev_effective_entity_id") if text("dev_effective_entity_id") else None
    )
    if text("dev_effective_entity_id") == "":
        findings.append(_Finding("invalid_row", "dev_effective_entity_id is required"))
    candidate_id = text("proposed_candidate_id")
    if not candidate_id:
        findings.append(_Finding("invalid_row", "proposed_candidate_id is required"))
    contract = text("ledger_latest_contract")
    if not contract:
        findings.append(_Finding("invalid_row", "ledger_latest_contract is required"))
    action = text("dev_decision_action")
    if not action:
        findings.append(_Finding("invalid_row", "dev_decision_action is required"))
    kind = text("current_text_kind")
    if kind not in ArchiveItemEntityOccurrence.TextKind.values:
        findings.append(_Finding("invalid_row", "current_text_kind is not supported"))
    reconstructed = raw.get("reconstructed_text") or ""
    if reconstructed == "":
        findings.append(_Finding("invalid_row", "reconstructed_text is required"))
    surface = text("computed_normalized_surface")
    if surface == "":
        findings.append(
            _Finding("invalid_row", "computed_normalized_surface is required")
        )
    elif len(surface) > 255:
        findings.append(
            _Finding("invalid_row", "computed_normalized_surface exceeds 255")
        )
    digest = text("computed_source_sha256").lower()
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        findings.append(_Finding("invalid_row", "computed_source_sha256 is invalid"))
    technical = text("post_merge_technical_status")
    if technical != TECHNICAL_CANDIDATE:
        findings.append(
            _Finding(
                "out_of_scope",
                "only TECHNICAL_CANDIDATE rows are in this preflight",
            )
        )
    approved = text("approved_for_apply")
    if approved.lower() != "false":
        findings.append(
            _Finding(
                "invalid_row",
                "approved_for_apply must stay false; this preflight does not grant a write",
            )
        )
    if (
        start is not None
        and end is not None
        and (end <= start or end - start != len(reconstructed))
    ):
        findings.append(
            _Finding(
                "boundary_drift",
                "reconstructed span does not match reconstructed_text",
            )
        )
    identity = None
    if (
        item_id is not None
        and kind in ArchiveItemEntityOccurrence.TextKind.values
        and len(digest) == 64
        and surface
        and ordinal is not None
        and not any(finding.status == "invalid_row" for finding in findings)
    ):
        identity = (item_id, kind, digest, SURFACE_V1, surface, ordinal)
        if identity == _OCCURRENCE_98_IDENTITY:
            findings.append(
                _Finding(
                    "excluded_occurrence_98",
                    "occurrence 98 is already linked and is not an insert candidate",
                )
            )
    return _Row(
        mention_index=mention,
        archive_item_id=item_id,
        proposed_candidate_id=candidate_id,
        contract=contract,
        decision_id=decision_id,
        decision_action=action,
        csv_entity_id=csv_entity,
        text_kind=kind,
        start=start,
        end=end,
        reconstructed_text=reconstructed,
        normalized_surface=surface,
        ordinal=ordinal,
        source_sha256=digest,
        technical_status=technical,
        approved_for_apply=approved,
        identity=identity,
        findings=findings,
    )


def _mark_duplicates(rows: list[_Row]) -> None:
    mentions: dict[str, list[_Row]] = {}
    identities: dict[tuple, list[_Row]] = {}
    for row in rows:
        if row.mention_index:
            mentions.setdefault(row.mention_index, []).append(row)
        if row.identity is not None:
            identities.setdefault(row.identity, []).append(row)
    for group in mentions.values():
        if len(group) > 1:
            for row in group:
                row.findings.append(
                    _Finding("duplicate_mention", "mention_index is repeated")
                )
    for group in identities.values():
        if len(group) > 1:
            for row in group:
                row.findings.append(
                    _Finding(
                        "duplicate_identity",
                        "six-field occurrence identity is repeated",
                    )
                )


def _classify_against_database(rows: list[_Row]) -> None:
    item_ids = {row.archive_item_id for row in rows if row.archive_item_id}
    items = _load_items(item_ids)
    decisions = _load_decisions(rows)
    retired = set(
        NonPersonEntityCorrection.objects.exclude(retired_entity_id=None).values_list(
            "retired_entity_id", flat=True
        )
    )
    occurrences = _load_occurrences(item_ids)
    proposals = _load_proposals(item_ids)
    texts: dict[int, str | None] = {}
    for row in rows:
        if row.status != "eligible" and _blocked_before_database(row):
            continue
        _classify_row(
            row,
            items=items,
            texts=texts,
            decisions=decisions,
            retired=retired,
            occurrences=occurrences,
            proposals=proposals,
        )


def _blocked_before_database(row: _Row) -> bool:
    return any(
        finding.status
        in {
            "invalid_row",
            "out_of_scope",
            "duplicate_mention",
            "duplicate_identity",
            "excluded_occurrence_98",
        }
        for finding in row.findings
    )


def _classify_row(
    row: _Row,
    *,
    items: dict[int, ArchiveItem],
    texts: dict[int, str | None],
    decisions: dict[tuple[str, str], ReviewedNonPersonEntityDecision],
    retired: set[int],
    occurrences: dict[tuple, ArchiveItemEntityOccurrence],
    proposals: dict[tuple, NonPersonEntityOccurrenceProposal],
) -> None:
    if row.archive_item_id is None or row.identity is None:
        return
    _classify_decision(row, decisions=decisions, retired=retired)
    item = items.get(row.archive_item_id)
    if item is None:
        row.findings.append(
            _Finding("source_item_missing", "archive item was not found")
        )
    elif not item_supports_occurrence_text_kind(item, row.text_kind):
        row.findings.append(
            _Finding(
                "unsupported_text_kind",
                "item type does not supply this text kind",
            )
        )
    else:
        if row.archive_item_id not in texts:
            texts[row.archive_item_id] = authoritative_displayed_text(
                item, row.text_kind
            )
        text = texts[row.archive_item_id]
        if text is None:
            row.findings.append(
                _Finding("source_missing", "authoritative displayed text is missing")
            )
        else:
            _classify_text(row, text)
    if row.identity in occurrences:
        existing = occurrences[row.identity]
        row.existing_occurrence_id = existing.pk
        if (
            row.live_entity_id is not None
            and existing.entity_id == row.live_entity_id
            and existing.resolution_status
            == ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
        ):
            row.findings.append(
                _Finding(
                    "already_linked",
                    f"occurrence {existing.pk} already has this identity and entity",
                )
            )
        else:
            row.findings.append(
                _Finding(
                    "identity_collision",
                    f"occurrence {existing.pk} already uses this identity",
                )
            )
    proposal = proposals.get(row.identity)
    if proposal is not None:
        row.proposal_id = proposal.pk
        pending = [
            candidate.pk
            for candidate in proposal.candidates.all()
            if candidate.status == "PENDING"
        ]
        detail = f"proposal {proposal.pk} already uses this identity"
        if pending:
            detail += " and its pending candidates were left unresolved"
        row.findings.append(_Finding("proposal_overlap", detail))
    if not any(finding.status != "eligible" for finding in row.findings):
        row.findings.append(
            _Finding(
                "eligible",
                "text and effective entity match; approved_for_apply stays false; no row was written",
            )
        )


def _classify_text(row: _Row, text: str) -> None:
    digest = source_text_sha256(text)
    if digest != row.source_sha256:
        row.findings.append(
            _Finding(
                "source_sha_drift",
                "authoritative text sha256 differs from the candidate",
            )
        )
        return
    if normalize_surface_v1(row.reconstructed_text) != row.normalized_surface:
        row.findings.append(
            _Finding(
                "surface_drift",
                "reconstructed text does not normalize to the candidate surface",
            )
        )
        return
    if normalize_surface_v1(row.normalized_surface) != row.normalized_surface:
        row.findings.append(
            _Finding("surface_drift", "candidate surface is not surface-v1")
        )
        return
    located = locate_surface_occurrences(text, row.normalized_surface)
    if located.occurrences is None:
        row.findings.append(
            _Finding(
                "source_ambiguous",
                "surface count is known but a safe original slice was not recovered",
            )
        )
        return
    match = next(
        (
            occurrence
            for occurrence in located.occurrences
            if occurrence.ordinal == row.ordinal
        ),
        None,
    )
    if match is None or row.start is None or row.end is None:
        row.findings.append(
            _Finding(
                "source_not_located", "ordinal was not found in authoritative text"
            )
        )
        return
    if match.start != row.start or match.end != row.end:
        row.findings.append(
            _Finding(
                "boundary_drift",
                f"live span {match.start}:{match.end} differs from the candidate",
            )
        )
        return
    if normalize_surface_v1(match.matched_text) != row.normalized_surface:
        row.findings.append(
            _Finding("surface_drift", "live slice does not normalize to the surface")
        )


def _classify_decision(
    row: _Row,
    *,
    decisions: dict[tuple[str, str], ReviewedNonPersonEntityDecision],
    retired: set[int],
) -> None:
    source = _CONTRACT_SOURCES.get(row.contract)
    if source is None:
        row.findings.append(
            _Finding("decision_contract_unknown", "ledger contract is not recognized")
        )
        return
    decision = decisions.get((source, row.proposed_candidate_id))
    if decision is None:
        row.findings.append(
            _Finding(
                "decision_missing",
                "no reviewed decision matches this contract and candidate",
            )
        )
        return
    row.live_decision_id = decision.pk
    if row.decision_id != decision.pk:
        row.findings.append(
            _Finding(
                "decision_identity_drift",
                f"live decision {decision.pk} differs from the candidate decision id",
            )
        )
    if decision.decision != row.decision_action:
        row.findings.append(
            _Finding(
                "decision_action_drift",
                f"live decision action is {decision.decision}",
            )
        )
    entity = effective_entity_for_decision(decision)
    if entity is None:
        row.findings.append(
            _Finding("entity_unresolved", "the decision has no effective entity")
        )
        return
    row.live_entity_id = entity.pk
    if entity.pk in retired:
        row.findings.append(
            _Finding(
                "retired_entity",
                f"effective entity {entity.pk} is retired",
            )
        )
        return
    if row.csv_entity_id != entity.pk:
        row.findings.append(
            _Finding(
                "entity_drift",
                f"effective entity is {entity.pk}; the csv entity id was not used",
            )
        )


def _load_items(item_ids: set[int]) -> dict[int, ArchiveItem]:
    if not item_ids:
        return {}
    queryset = (
        ArchiveItem.objects.filter(pk__in=item_ids)
        .select_related("manual_text_content")
        .prefetch_related(
            Prefetch(
                "ocr_document",
                queryset=Document.objects.prefetch_related(
                    text_presentation_results_prefetch()
                ),
            )
        )
    )
    return {item.pk: item for item in queryset}


def _load_decisions(
    rows: list[_Row],
) -> dict[tuple[str, str], ReviewedNonPersonEntityDecision]:
    candidate_ids = {
        row.proposed_candidate_id for row in rows if row.proposed_candidate_id
    }
    if not candidate_ids:
        return {}
    queryset = ReviewedNonPersonEntityDecision.objects.filter(
        source__in=_CONTRACT_SOURCES.values(),
        candidate_id__in=candidate_ids,
    ).select_related("result_entity", "correction", "correction__effective_entity")
    return {(row.source, row.candidate_id): row for row in queryset}


def _load_occurrences(
    item_ids: set[int],
) -> dict[tuple, ArchiveItemEntityOccurrence]:
    queryset = ArchiveItemEntityOccurrence.objects.filter(
        Q(archive_item_id__in=item_ids) | Q(pk=OCCURRENCE_98_ID)
    )
    found: dict[tuple, ArchiveItemEntityOccurrence] = {}
    for row in queryset:
        identity = (
            row.archive_item_id,
            row.text_kind,
            row.source_text_sha256,
            row.normalization_version,
            row.normalized_surface,
            row.occurrence_ordinal,
        )
        found[identity] = row
    return found


def _load_proposals(
    item_ids: set[int],
) -> dict[tuple, NonPersonEntityOccurrenceProposal]:
    if not item_ids:
        return {}
    queryset = NonPersonEntityOccurrenceProposal.objects.filter(
        archive_item_id__in=item_ids
    ).prefetch_related("candidates")
    found: dict[tuple, NonPersonEntityOccurrenceProposal] = {}
    for row in queryset:
        identity = (
            row.archive_item_id,
            row.text_kind,
            row.source_text_sha256,
            row.normalization_version,
            row.normalized_surface,
            row.occurrence_ordinal,
        )
        found[identity] = row
    return found


def _occurrence_98_report(rows: list[_Row]) -> dict[str, object]:
    row = ArchiveItemEntityOccurrence.objects.filter(pk=OCCURRENCE_98_ID).first()
    errors: list[str] = []
    if row is None:
        errors.append("occurrence 98 is missing")
    else:
        actual = (
            row.archive_item_id,
            row.text_kind,
            row.source_text_sha256,
            row.normalization_version,
            row.normalized_surface,
            row.occurrence_ordinal,
        )
        if actual != _OCCURRENCE_98_IDENTITY:
            errors.append("occurrence 98 identity drifted")
        if (
            row.entity_id != SURVIVOR_ENTITY_ID
            or row.resolution_status
            != ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
        ):
            errors.append("occurrence 98 is not a resolved pin of entity 655")
    proposed = [
        item.mention_index for item in rows if item.identity == _OCCURRENCE_98_IDENTITY
    ]
    if proposed:
        errors.append(
            "candidate rows propose occurrence 98 again: " + ", ".join(proposed)
        )
    return {
        "ok": not errors,
        "errors": errors,
        "present": row is not None,
        "proposed_again": bool(proposed),
        "mention_indexes": proposed,
    }


def _finish(
    destination: Path,
    *,
    file_sha256: str,
    expected_sha256: str,
    rows: list[_Row],
    file_errors: tuple[str, ...],
    occurrence_98_ok: bool,
    occurrence_98: dict[str, object] | None = None,
) -> Stage2OccurrencePreflight:
    counts = Counter(row.status for row in rows)
    eligible = counts.get("eligible", 0)
    clean = (
        not file_errors and occurrence_98_ok and bool(rows) and eligible == len(rows)
    )
    recorded = occurrence_98 or {
        "ok": False,
        "errors": ["occurrence 98 was not checked"],
        "present": False,
        "proposed_again": False,
        "mention_indexes": [],
    }
    summary = {
        "mode": "dry-run",
        "database_writes": 0,
        "write_authorized": False,
        "approved_for_apply_changed": False,
        "file_sha256": file_sha256,
        "expected_sha256": expected_sha256,
        "recorded_candidate_sha256": RECORDED_CANDIDATE_SHA256,
        "row_count": len(rows),
        "eligible_count": eligible,
        "status_counts": dict(sorted(counts.items())),
        "clean": clean,
        "file_errors": list(file_errors),
        "occurrence_98": recorded,
        "pending_proposals_modified": False,
        "note": (
            "eligible means a later confirmed import could insert a pin. "
            "This preflight does not insert one and does not set approved_for_apply."
        ),
    }
    _write_reports(destination, rows, summary)
    lines = [
        "Stage 2 occurrence import preflight",
        "mode: dry-run",
        "database_writes: 0",
        f"clean: {str(clean).lower()}",
        f"rows: {len(rows)}",
        f"eligible: {eligible}",
        f"occurrence_98_ok: {str(occurrence_98_ok).lower()}",
    ]
    if file_errors:
        lines.append("file_errors:")
        lines.extend(f"- {error}" for error in file_errors)
    return Stage2OccurrencePreflight(
        clean=clean,
        file_sha256=file_sha256,
        expected_sha256=expected_sha256,
        row_count=len(rows),
        eligible_count=eligible,
        status_counts=dict(counts),
        occurrence_98_ok=occurrence_98_ok,
        file_errors=file_errors,
        summary="\n".join(lines),
        output_dir=destination,
    )


def _write_reports(destination: Path, rows: list[_Row], summary: dict) -> None:
    csv_path = destination / "stage2_occurrence_import_preflight.csv"
    json_path = destination / "stage2_occurrence_import_preflight.json"
    markdown_path = destination / "stage2_occurrence_import_preflight.md"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_REPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "mention_index": row.mention_index,
                    "archive_item_id": row.archive_item_id or "",
                    "proposed_candidate_id": row.proposed_candidate_id,
                    "status": row.status,
                    "explanation": row.explanation,
                    "live_effective_entity_id": row.live_entity_id or "",
                    "csv_effective_entity_id": row.csv_entity_id or "",
                    "live_decision_id": row.live_decision_id or "",
                    "existing_occurrence_id": row.existing_occurrence_id or "",
                    "proposal_id": row.proposal_id or "",
                    "approved_for_apply": row.approved_for_apply,
                    "write_authorized": "false",
                }
            )
    json_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown(rows, summary), encoding="utf-8")


def _markdown(rows: list[_Row], summary: dict) -> str:
    counts = summary["status_counts"]
    count_lines = "\n".join(f"- {status}: {count}" for status, count in counts.items())
    problems = [row for row in rows if row.status != "eligible"]
    problem_lines = "\n".join(
        f"- mention {row.mention_index}: {row.status}. {row.explanation}"
        for row in problems
    )
    occurrence = summary["occurrence_98"]
    occurrence_errors = "\n".join(f"- {error}" for error in occurrence["errors"])
    file_errors = "\n".join(f"- {error}" for error in summary["file_errors"])
    return (
        "# Stage 2 occurrence import preflight\n\n"
        "Read-only dry run. No occurrence, proposal, candidate, decision, "
        "correction, alias, or entity row was written. `approved_for_apply` "
        "was not changed. Pending proposals were not resolved.\n\n"
        f"- file sha256: `{summary['file_sha256']}`\n"
        f"- expected sha256: `{summary['expected_sha256']}`\n"
        f"- rows: {summary['row_count']}\n"
        f"- eligible: {summary['eligible_count']}\n"
        f"- clean: {str(summary['clean']).lower()}\n"
        "- database writes: 0\n\n"
        "Eligible is not permission to insert. A later import still needs a "
        "separate confirmation.\n\n"
        "## Status counts\n\n"
        f"{count_lines or '- none'}\n\n"
        "## Occurrence 98\n\n"
        "The stored pin must keep its recorded six-field identity, entity 655, "
        "and resolved status. This file must not propose inserting it again.\n\n"
        f"{occurrence_errors or '- matches the recorded pin'}\n\n"
        "## File errors\n\n"
        f"{file_errors or '- none'}\n\n"
        "## Rows that are not eligible\n\n"
        f"{problem_lines or '- none'}\n"
    )
