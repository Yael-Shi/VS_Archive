"""Safe import of historical Transkribus PAGE XML for existing Hebrew text.

This path exists for archival documents whose transcription was historically
copied from Transkribus into VS Archive, while PAGE XML geometry was not stored.

Important invariants:
- HEBREW_TEXT only.
- SOURCE_TEXT is never modified or bound here.
- dry-run performs no DB/S3 writes.
- CONTENT_MISMATCH always fails closed.
- VERIFIED may be preserved only for EXACT / strictly WHITESPACE_ONLY alignment.
- final binding still uses the normal exact-SHA trust invariant.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Sequence

import requests
from django.contrib.auth.models import User
from django.db import transaction

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services import transkribus_engine as tr
from documents.services.archive_search_index import sync_archive_item_search_index
from documents.services.document_access import is_document_admin
from documents.services.transkribus_binding_freshness import (
    is_binding_structurally_fresh,
    is_binding_trusted_for_hover,
)
from documents.services.transkribus_corrected_current_selection import (
    CorrectedCurrentPageInput,
    select_corrected_current_transcripts_for_document,
)
from documents.services.transkribus_page_xml_types import SelectedTranscriptPage
from documents.services.transkribus_snapshot_binding import (
    bind_text_result_to_snapshot,
)
from documents.services.transkribus_snapshot_pages import (
    normalize_page_index_to_page_nr,
    snapshot_pages_from_upload_mapping,
)
from documents.services.transkribus_snapshot_parser import (
    compute_sha256_hex,
    parse_document_pages_for_snapshot,
)
from documents.services.transkribus_snapshot_storage import (
    SnapshotStorageOutcome,
    store_transkribus_transcript_snapshot,
)


class LegacyImportError(ValueError):
    """Safe, operator-facing validation/refusal error."""


class LegacyTextEquivalence(StrEnum):
    EXACT = "EXACT"
    WHITESPACE_ONLY = "WHITESPACE_ONLY"
    CONTENT_MISMATCH = "CONTENT_MISMATCH"


@dataclass(frozen=True)
class LegacySelectedPage:
    page_index: int
    page_nr: int
    transcript_ts_id: str
    remote_transcript_status: str | None


@dataclass(frozen=True)
class LegacyImportPlan:
    document_id: int
    text_result_id: int
    collection_id: str
    remote_doc_id: str
    page_index_to_page_nr: dict[int, int]
    selected_pages: tuple[LegacySelectedPage, ...]
    current_text_sha256: str
    canonical_text_sha256: str
    current_text_chars: int
    canonical_text_chars: int
    based_on_source_revision: int
    verification_status: str
    equivalence: LegacyTextEquivalence
    bytes_would_change: bool
    canonical_text: str
    snapshot_inputs: tuple
    geometry_capability: str
    hover_eligible: bool

    @property
    def would_apply(self) -> bool:
        return self.equivalence != LegacyTextEquivalence.CONTENT_MISMATCH


@dataclass(frozen=True)
class LegacyImportApplyResult:
    snapshot_id: int
    storage_outcome: str
    text_result_id: int
    equivalence: LegacyTextEquivalence
    text_changed: bool
    verification_status: str
    bound_source_revision: int
    binding_id: int
    binding_structurally_fresh: bool
    hover_trusted: bool
    outcome: str


_HORIZONTAL_WS_RE = re.compile(r"[ \t]+")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_legacy_whitespace(text: str) -> str:
    """Normalize only representation-level whitespace.

    Deliberately does NOT:
    - case-fold
    - Unicode-normalize letters
    - remove punctuation
    - alter digits
    - delete blank lines
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\u00a0", " ")

    lines: list[str] = []
    for line in normalized.split("\n"):
        line = _HORIZONTAL_WS_RE.sub(" ", line)
        line = line.strip(" ")
        lines.append(line)

    return "\n".join(lines)


def classify_legacy_text(
    current_text: str,
    canonical_text: str,
) -> LegacyTextEquivalence:
    if current_text == canonical_text:
        return LegacyTextEquivalence.EXACT

    if normalize_legacy_whitespace(current_text) == normalize_legacy_whitespace(
        canonical_text
    ):
        return LegacyTextEquivalence.WHITESPACE_ONLY

    return LegacyTextEquivalence.CONTENT_MISMATCH


def _page_metadata_by_nr(
    pages_meta: Sequence[tr.TrpPageMetadata],
    page_nr: int,
) -> tr.TrpPageMetadata:
    matches = [page for page in pages_meta if page.page_nr == page_nr]
    if len(matches) != 1:
        raise LegacyImportError(
            f"Expected exactly one metadata row for pageNr={page_nr}, "
            f"found {len(matches)}."
        )
    return matches[0]


def _find_explicit_transcript(
    raw_transcripts: Sequence[Mapping],
    *,
    page_nr: int,
    transcript_ts_id: str,
) -> Mapping:
    target = str(transcript_ts_id).strip()
    matches = [
        raw for raw in raw_transcripts if str(raw.get("tsId") or "").strip() == target
    ]
    if len(matches) != 1:
        raise LegacyImportError(
            f"pageNr={page_nr}: explicit tsId={target!r} matched "
            f"{len(matches)} transcripts."
        )
    return matches[0]


def _selected_pages_from_metadata(
    *,
    pages_meta: Sequence[tr.TrpPageMetadata],
    page_index_to_page_nr: Mapping[int, int],
    transcript_ts_id_by_page_nr: Mapping[int, str] | None,
    bearer_token: str,
) -> tuple[list[SelectedTranscriptPage], tuple[LegacySelectedPage, ...]]:
    explicit = {
        int(page_nr): str(ts_id).strip()
        for page_nr, ts_id in dict(transcript_ts_id_by_page_nr or {}).items()
    }

    selected_pages: list[SelectedTranscriptPage] = []
    selected_summary: list[LegacySelectedPage] = []

    for page_index, page_nr in sorted(page_index_to_page_nr.items()):
        pm = _page_metadata_by_nr(pages_meta, page_nr)

        if page_nr in explicit:
            raw = _find_explicit_transcript(
                pm.transcripts,
                page_nr=page_nr,
                transcript_ts_id=explicit[page_nr],
            )
            ts_id = str(raw.get("tsId") or "").strip()
            status_raw = raw.get("status")
            remote_status = str(status_raw).strip() if status_raw is not None else None
        else:
            selection = select_corrected_current_transcripts_for_document(
                [
                    CorrectedCurrentPageInput(
                        page_index=page_index,
                        page_nr=page_nr,
                        raw_transcripts=tuple(pm.transcripts),
                    )
                ]
            )
            if selection.is_refused:
                assert selection.page_errors is not None
                err = selection.page_errors[0]
                raise LegacyImportError(
                    f"page_index={page_index} pageNr={page_nr}: "
                    f"{err.code}: {err.message}"
                )

            assert selection.selections is not None
            chosen = selection.selections[0]
            ts_id = chosen.transcript_ts_id
            remote_status = chosen.remote_transcript_status
            raw = _find_explicit_transcript(
                pm.transcripts,
                page_nr=page_nr,
                transcript_ts_id=ts_id,
            )

        url = raw.get("url")
        if not isinstance(url, str) or not url.strip():
            raise LegacyImportError(
                f"pageNr={page_nr}: selected transcript has no URL."
            )

        page_xml = tr.fetch_transcript_xml(
            url.strip(),
            bearer_token=bearer_token,
        )

        selected_pages.append(
            SelectedTranscriptPage(
                page_nr=page_nr,
                transcript_ts_id=ts_id,
                page_xml=page_xml,
                url=url.strip(),
                provider_page_id=pm.page_id,
                remote_transcript_status=remote_status,
            )
        )
        selected_summary.append(
            LegacySelectedPage(
                page_index=page_index,
                page_nr=page_nr,
                transcript_ts_id=ts_id,
                remote_transcript_status=remote_status,
            )
        )

    return selected_pages, tuple(selected_summary)


def _pages_query(page_nrs: Sequence[int]) -> str:
    """Compact sorted page numbers into Transkribus-style ranges."""
    nums = sorted(set(int(n) for n in page_nrs))
    if not nums:
        raise LegacyImportError("At least one pageNr is required.")

    chunks: list[str] = []
    start = previous = nums[0]

    for number in nums[1:]:
        if number == previous + 1:
            previous = number
            continue

        chunks.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number

    chunks.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(chunks)


def prepare_legacy_transkribus_import(
    *,
    document_id: int,
    text_result_id: int,
    collection_id: str,
    remote_doc_id: str,
    page_index_to_page_nr: Mapping[int, int],
    username: str,
    password: str,
    bearer_token: str,
    transcript_ts_id_by_page_nr: Mapping[int, str] | None = None,
) -> LegacyImportPlan:
    """Read-only HTTP/DB planning step. No DB/S3 writes."""
    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise LegacyImportError(f"Document id={document_id} does not exist.") from exc

    if document.language != Document.Language.HEBREW:
        raise LegacyImportError(
            "Legacy hover import currently requires a Hebrew document."
        )

    try:
        text_result = DocumentTextResult.objects.get(pk=text_result_id)
    except DocumentTextResult.DoesNotExist as exc:
        raise LegacyImportError(
            f"DocumentTextResult id={text_result_id} does not exist."
        ) from exc

    if text_result.document_id != document.pk:
        raise LegacyImportError("Target text result belongs to a different document.")

    if text_result.result_type != DocumentTextResult.ResultType.HEBREW_TEXT:
        raise LegacyImportError("Target text result must be HEBREW_TEXT.")

    bound_revision = int(text_result.based_on_source_revision or 0)
    if bound_revision < 1:
        raise LegacyImportError("HEBREW_TEXT based_on_source_revision must be >= 1.")

    normalized_map = normalize_page_index_to_page_nr(page_index_to_page_nr)
    pages_query = _pages_query(list(normalized_map.values()))

    with requests.Session() as session:
        tr.login_trp_server(
            session,
            username=username,
            password=password,
        )
        pages_meta = tr.fetch_pages_metadata(
            session,
            collection_id=str(collection_id).strip(),
            document_id=str(remote_doc_id).strip(),
            pages_query=pages_query,
        )

    expected_nrs = set(normalized_map.values())
    actual_nrs = {page.page_nr for page in pages_meta if page.page_nr is not None}
    if expected_nrs != actual_nrs:
        raise LegacyImportError(
            "Returned pageNr set does not match trusted mapping "
            f"(expected={sorted(expected_nrs)}, actual={sorted(actual_nrs)})."
        )

    selected_pages, selected_summary = _selected_pages_from_metadata(
        pages_meta=pages_meta,
        page_index_to_page_nr=normalized_map,
        transcript_ts_id_by_page_nr=transcript_ts_id_by_page_nr,
        bearer_token=bearer_token,
    )

    snapshot_inputs = snapshot_pages_from_upload_mapping(
        selected_pages,
        normalized_map,
    )
    parsed = parse_document_pages_for_snapshot(snapshot_inputs)

    current_text = text_result.text or ""
    canonical_text = parsed.canonical_text
    equivalence = classify_legacy_text(current_text, canonical_text)

    return LegacyImportPlan(
        document_id=document.pk,
        text_result_id=text_result.pk,
        collection_id=str(collection_id).strip(),
        remote_doc_id=str(remote_doc_id).strip(),
        page_index_to_page_nr=normalized_map,
        selected_pages=selected_summary,
        current_text_sha256=_sha(current_text),
        canonical_text_sha256=parsed.canonical_text_sha256,
        current_text_chars=len(current_text),
        canonical_text_chars=len(canonical_text),
        based_on_source_revision=bound_revision,
        verification_status=text_result.verification_status,
        equivalence=equivalence,
        bytes_would_change=current_text != canonical_text,
        canonical_text=canonical_text,
        snapshot_inputs=tuple(snapshot_inputs),
        geometry_capability=parsed.geometry_capability,
        hover_eligible=bool(parsed.hover_eligible),
    )


def apply_legacy_transkribus_import(
    *,
    plan: LegacyImportPlan,
    actor: User,
) -> LegacyImportApplyResult:
    """Persist snapshot, canonicalize HEBREW bytes if safe, then bind.

    Snapshot storage necessarily happens before the enclosing DTR transaction
    because it performs S3 I/O. A failure after snapshot creation leaves a
    harmless reusable READY snapshot and is safe to retry.
    """
    if plan.equivalence == LegacyTextEquivalence.CONTENT_MISMATCH:
        raise LegacyImportError("CONTENT_MISMATCH cannot be applied.")

    if (
        not isinstance(actor, User)
        or not actor.is_active
        or not is_document_admin(actor)
    ):
        raise LegacyImportError(
            "An active document administrator is required for --apply."
        )

    document = Document.objects.get(pk=plan.document_id)

    storage_result = store_transkribus_transcript_snapshot(
        document=document,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.LEGACY_IMPORT,
        pages=plan.snapshot_inputs,
        transkribus_run=None,
        remote_doc_id=plan.remote_doc_id,
        collection_id=plan.collection_id,
        model_id="",
        recognition_job_id="",
        created_by=actor,
        hover_eligible=None,
    )
    snapshot = storage_result.snapshot

    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise LegacyImportError(
            f"Snapshot id={snapshot.pk} is not READY after storage."
        )

    if compute_sha256_hex(snapshot.canonical_text or "") != (
        snapshot.canonical_text_sha256 or ""
    ):
        raise LegacyImportError("Stored snapshot canonical SHA is invalid.")

    if snapshot.canonical_text_sha256 != plan.canonical_text_sha256:
        raise LegacyImportError(
            "Stored snapshot canonical SHA does not match the prepared import plan."
        )

    text_changed = False

    with transaction.atomic():
        try:
            row = DocumentTextResult.objects.select_for_update().get(
                pk=plan.text_result_id
            )
        except DocumentTextResult.DoesNotExist as exc:
            raise LegacyImportError(
                "Target text result disappeared before apply."
            ) from exc

        if row.document_id != plan.document_id:
            raise LegacyImportError("Target text result document changed.")
        if row.result_type != DocumentTextResult.ResultType.HEBREW_TEXT:
            raise LegacyImportError("Target text result is no longer HEBREW_TEXT.")

        current_sha = compute_sha256_hex(row.text or "")
        if current_sha != plan.current_text_sha256:
            raise LegacyImportError(
                "Target text changed after dry-run/preparation; refusing stale apply."
            )

        current_revision = int(row.based_on_source_revision or 0)
        if current_revision != plan.based_on_source_revision:
            raise LegacyImportError(
                "based_on_source_revision changed after preparation."
            )

        if row.verification_status != plan.verification_status:
            raise LegacyImportError("verification_status changed after preparation.")

        current_equivalence = classify_legacy_text(
            row.text or "",
            snapshot.canonical_text or "",
        )
        if current_equivalence == LegacyTextEquivalence.CONTENT_MISMATCH:
            raise LegacyImportError(
                "Target text no longer matches snapshot under legacy whitespace rules."
            )

        existing_binding = (
            TranskribusTextResultBinding.objects.select_for_update()
            .filter(text_result_id=row.pk)
            .select_related("snapshot", "text_result")
            .first()
        )
        existing_binding_fresh = (
            existing_binding is not None
            and is_binding_structurally_fresh(row, binding=existing_binding)
        )
        already_bound_to_snapshot = (
            existing_binding_fresh
            and existing_binding.snapshot_id == snapshot.pk
            and existing_binding.binding_role
            == TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
        )

        if (
            existing_binding is not None
            and existing_binding.snapshot_id != snapshot.pk
            and existing_binding_fresh
        ):
            raise LegacyImportError(
                "Target already has a different structurally-fresh Transkribus binding."
            )

        canonical_text = snapshot.canonical_text or ""
        text_changed = (row.text or "") != canonical_text

        if text_changed:
            old_text = row.text or ""
            original_verification = row.verification_status

            row.text = canonical_text
            row.save(update_fields=["text", "updated_at"])

            DocumentTextResultEdit.objects.create(
                text_result=row,
                editor=actor,
                old_text=old_text,
                new_text=canonical_text,
                edit_type=DocumentTextResultEdit.EditType.HEBREW_TEXT,
            )

            row.refresh_from_db()
            if row.verification_status != original_verification:
                raise LegacyImportError(
                    "Legacy whitespace alignment unexpectedly changed verification status."
                )

        binding = bind_text_result_to_snapshot(
            text_result=row,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_source_revision=plan.based_on_source_revision,
            bound_by=actor,
        )

        structurally_fresh = is_binding_structurally_fresh(
            row,
            binding=binding,
        )
        hover_trusted = is_binding_trusted_for_hover(
            row,
            binding=binding,
        )
        if not structurally_fresh:
            raise LegacyImportError(
                "Binding was created but is not structurally fresh; transaction rolled back."
            )
        if not hover_trusted:
            raise LegacyImportError(
                "Binding was created but is not trusted for hover; transaction rolled back."
            )

    if text_changed:
        sync_archive_item_search_index(document.archive_item_id)

    already_imported = (
        not text_changed
        and already_bound_to_snapshot
        and storage_result.outcome != SnapshotStorageOutcome.CREATED
    )

    return LegacyImportApplyResult(
        snapshot_id=snapshot.pk,
        storage_outcome=storage_result.outcome.value,
        text_result_id=plan.text_result_id,
        equivalence=plan.equivalence,
        text_changed=text_changed,
        verification_status=row.verification_status,
        bound_source_revision=plan.based_on_source_revision,
        binding_id=binding.pk,
        binding_structurally_fresh=structurally_fresh,
        hover_trusted=hover_trusted,
        outcome="ALREADY_IMPORTED" if already_imported else "APPLIED",
    )


__all__ = [
    "LegacyImportApplyResult",
    "LegacyImportError",
    "LegacyImportPlan",
    "LegacySelectedPage",
    "LegacyTextEquivalence",
    "apply_legacy_transkribus_import",
    "classify_legacy_text",
    "normalize_legacy_whitespace",
    "prepare_legacy_transkribus_import",
]
