"""Local completion and resume for automatic Transkribus snapshot integration.

Lock order inside the local-success transaction:

1. ``Document`` (``select_for_update``)
2. ``TranskribusRun`` (``select_for_update``)
3. ``TranskribusRunAutomaticSnapshot`` association row (``select_for_update``)
4. ``TranskribusTranscriptSnapshot`` (``select_for_update``)
5. All ``DocumentTextResult`` rows for the document (ordered by ``id``), for
   the VERIFIED write fence; then existing rows for the runtime engine again
   when writes proceed

S3 and provider HTTP must stay outside this transaction.

``TranskribusTranscriptSnapshot.transkribus_run`` is origin/provenance only.
Consuming runs associate via ``TranskribusRunAutomaticSnapshot`` so multiple runs
may share one immutable READY snapshot (storage fingerprint reuse).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence

from django.db import IntegrityError, transaction

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusRunAutomaticSnapshot,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.htr_adapters.base import (
    HtrResult,
    TranskribusLocalPersistenceRetryableError,
)
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.ocr_verified_write_fence import (
    inspect_automated_ocr_verified_write_fence,
)
from documents.services.processing_state import (
    update_document_processing_state_for_engine,
)
from documents.services.review_reasons import (
    AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW,
    HAS_UNCLEAR,
    MIN_TEXT_LENGTH,
    NEEDS_REVIEW_FLAG,
)
from documents.services import transkribus_run_persistence as trp
from documents.services.transkribus_snapshot_binding import (
    TranskribusSnapshotBindingError,
    bind_text_result_to_snapshot,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.transkribus_snapshot_storage import (
    TranskribusSnapshotStorageUploadError,
    TranskribusSnapshotStorageValidationError,
    store_transkribus_transcript_snapshot,
)

logger = logging.getLogger(__name__)


class TranskribusLocalCompletionError(ValueError):
    """Permanent local-completion / resume eligibility failure."""


def _dedupe_strings_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_hebrew_language(language: Optional[str]) -> bool:
    lang = (language or "").strip().lower()
    return lang in ("he", "heb", "hebrew")


def derive_automatic_review_reasons(
    text: str,
    adapter_needs_review: bool,
    engine_reasons: Optional[Sequence[str]],
    *,
    min_text_length: int,
) -> List[str]:
    reasons: List[str] = [AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW]
    if adapter_needs_review:
        reasons.append(NEEDS_REVIEW_FLAG)
    stripped = (text or "").strip()
    if len(stripped) < min_text_length:
        reasons.append(MIN_TEXT_LENGTH)
    if "[UNCLEAR]" in stripped:
        reasons.append(HAS_UNCLEAR)
    if engine_reasons:
        for r in engine_reasons:
            if r:
                reasons.append(r)
    return _dedupe_strings_preserve_order(reasons)


def reconstruct_engine_review_reasons_from_snapshot(
    snapshot: TranskribusTranscriptSnapshot,
) -> list[str]:
    """Deterministic engine reasons from READY snapshot pages (no recognition)."""
    reasons: list[str] = []
    pages = list(snapshot.pages.order_by("page_index"))
    for page in pages:
        if int(page.lines_with_non_empty_text or 0) == 0:
            reasons.append("EMPTY_TRANSCRIPT_PAGE")
    return reasons


def _run_has_durable_recognition_ids(run: TranskribusRun) -> bool:
    return bool(
        (run.recognition_job_id or "").strip()
        and (run.remote_doc_id or "").strip()
        and (run.pages_query or "").strip()
        and (run.collection_id or "").strip()
        and (run.model_id or "").strip()
    )


def get_run_automatic_snapshot_association(
    run: TranskribusRun,
) -> TranskribusRunAutomaticSnapshot | None:
    try:
        return run.automatic_snapshot_association
    except TranskribusRunAutomaticSnapshot.DoesNotExist:
        return None


def find_ready_automatic_snapshot_for_run(
    run: TranskribusRun,
) -> TranskribusTranscriptSnapshot | None:
    """Resolve the READY snapshot associated with this run (not origin FK alone)."""
    assoc = get_run_automatic_snapshot_association(run)
    if assoc is None:
        return None
    snapshot = assoc.snapshot
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise TranskribusLocalCompletionError(
            f"Associated snapshot id={snapshot.pk} for run id={run.pk} is not READY."
        )
    if snapshot.source_kind != TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR:
        raise TranskribusLocalCompletionError(
            f"Associated snapshot id={snapshot.pk} is not AUTOMATIC_HTR."
        )
    if snapshot.document_id != run.document_id:
        raise TranskribusLocalCompletionError(
            f"Associated snapshot id={snapshot.pk} document mismatch for run id={run.pk}."
        )
    return snapshot


def _reconcile_same_snapshot_association(
    assoc: TranskribusRunAutomaticSnapshot,
    *,
    snapshot: TranskribusTranscriptSnapshot,
    mapping_trusted: bool,
    review_reasons: list[str],
) -> TranskribusRunAutomaticSnapshot:
    """Idempotent reuse of an existing same-snapshot association.

    Safe reconcile rules only:
    - ``mapping_trusted`` may upgrade False→True, never downgrade True→False.
    - ``review_reasons``: fill if existing empty; keep existing if incoming empty;
      require exact equality when both non-empty.
    """
    if assoc.snapshot_id != snapshot.pk:
        raise TranskribusLocalCompletionError(
            f"TranskribusRun id={assoc.run_id} is already associated with "
            f"snapshot id={assoc.snapshot_id}; refusing reassignment to "
            f"snapshot id={snapshot.pk}."
        )

    update_fields: list[str] = []
    if mapping_trusted and not assoc.mapping_trusted:
        assoc.mapping_trusted = True
        update_fields.append("mapping_trusted")

    existing_reasons = list(assoc.review_reasons or [])
    if review_reasons and not existing_reasons:
        assoc.review_reasons = list(review_reasons)
        update_fields.append("review_reasons")
    elif review_reasons and existing_reasons and review_reasons != existing_reasons:
        raise TranskribusLocalCompletionError(
            f"TranskribusRun id={assoc.run_id} association review_reasons conflict "
            "with an existing non-empty value; refusing overwrite."
        )

    if update_fields:
        update_fields.append("updated_at")
        assoc.save(update_fields=update_fields)
    return assoc


def associate_run_with_automatic_snapshot(
    *,
    run: TranskribusRun,
    snapshot: TranskribusTranscriptSnapshot,
    mapping_trusted: bool,
    review_reasons: Sequence[str] | None = None,
) -> TranskribusRunAutomaticSnapshot:
    """Create or idempotently reuse a durable run→snapshot association.

    The association is immutable with respect to ``snapshot``: a run may not be
    reassigned from snapshot A to snapshot B. Concurrent creates are safe via
    IntegrityError retry that re-reads the winner and applies the same rules.
    """
    if snapshot.document_id != run.document_id:
        raise TranskribusLocalCompletionError(
            "Cannot associate snapshot and run from different documents."
        )
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise TranskribusLocalCompletionError(
            f"Cannot associate non-READY snapshot id={snapshot.pk}."
        )
    if snapshot.source_kind != TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR:
        raise TranskribusLocalCompletionError(
            f"Cannot associate non-AUTOMATIC_HTR snapshot id={snapshot.pk}."
        )

    reasons = list(review_reasons or [])
    trusted = bool(mapping_trusted)

    existing = get_run_automatic_snapshot_association(run)
    if existing is not None:
        return _reconcile_same_snapshot_association(
            existing,
            snapshot=snapshot,
            mapping_trusted=trusted,
            review_reasons=reasons,
        )

    try:
        with transaction.atomic():
            assoc = TranskribusRunAutomaticSnapshot(
                run=run,
                snapshot=snapshot,
                mapping_trusted=trusted,
                review_reasons=reasons,
            )
            assoc.save()
            return assoc
    except IntegrityError:
        # Concurrent create won; reuse if same snapshot, else refuse reassignment.
        winner = get_run_automatic_snapshot_association(run)
        if winner is None:
            raise TranskribusLocalCompletionError(
                f"Failed to create association for run id={run.pk} "
                "(IntegrityError without surviving row)."
            )
        return _reconcile_same_snapshot_association(
            winner,
            snapshot=snapshot,
            mapping_trusted=trusted,
            review_reasons=reasons,
        )


def _expected_result_types(*, is_hebrew: bool) -> list[DocumentTextResult.ResultType]:
    types = [DocumentTextResult.ResultType.SOURCE_TEXT]
    if is_hebrew:
        types.append(DocumentTextResult.ResultType.HEBREW_TEXT)
    return types


@dataclass(frozen=True)
class LocalCompletionBindingStatus:
    """Distinguish never-completed vs completed-with-later-edits vs corrupt."""

    structurally_complete: bool
    human_edited_after_bind: bool
    corrupt: bool


def inspect_local_completion_bindings(
    *,
    document_id: int,
    engine: str,
    snapshot: TranskribusTranscriptSnapshot,
    is_hebrew: bool,
) -> LocalCompletionBindingStatus:
    """Inspect bindings; corrupt original metadata ≠ later human text edits.

    Valid original binding requires correct snapshot/role, ``bound_source_revision
    >= 1``, and ``bound_text_sha256 == snapshot.canonical_text_sha256``. For
    Hebrew, SOURCE and HEBREW bindings must share the same bound revision and
    both hashes must equal the snapshot canonical hash. Only drift between the
    current DTR text/revision and otherwise-valid binding metadata counts as
    ``human_edited_after_bind``.
    """
    # Lazy import: avoid module-level cycle through snapshot_parser / adapter.
    from documents.services.transkribus_binding_freshness import (
        binding_matches_current_baseline,
        expected_binding_role_for_result_type,
    )

    human_edited = False
    bound_revisions: list[int] = []
    canonical_sha = (snapshot.canonical_text_sha256 or "").strip()

    for result_type in _expected_result_types(is_hebrew=is_hebrew):
        row = DocumentTextResult.objects.filter(
            document_id=document_id,
            result_type=result_type,
            engine=engine,
        ).first()
        if row is None:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=False,
            )
        binding = (
            TranskribusTextResultBinding.objects.filter(text_result_id=row.pk)
            .select_related("snapshot")
            .first()
        )
        if binding is None:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=False,
            )
        # Role mapping is shared; do not flatten never-bound / human-edit /
        # corrupt distinctions into the hover freshness helper.
        expected_role = expected_binding_role_for_result_type(result_type)
        if expected_role is None:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=True,
            )
        if binding.snapshot_id != snapshot.pk:
            # Binding exists for a different snapshot: not complete for this one
            # (allows a later run to rebind). Not corrupt original metadata.
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=False,
            )
        if binding.binding_role != expected_role:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=True,
            )
        bound_rev = int(binding.bound_source_revision or 0)
        if bound_rev < 1:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=True,
            )
        bound_sha = (binding.bound_text_sha256 or "").strip()
        if not canonical_sha or bound_sha != canonical_sha:
            return LocalCompletionBindingStatus(
                structurally_complete=False,
                human_edited_after_bind=False,
                corrupt=True,
            )
        bound_revisions.append(bound_rev)

        # Shared baseline match preserves text/revision drift semantics only.
        if not binding_matches_current_baseline(row, binding):
            human_edited = True

    if (
        is_hebrew
        and len(bound_revisions) == 2
        and bound_revisions[0] != bound_revisions[1]
    ):
        return LocalCompletionBindingStatus(
            structurally_complete=False,
            human_edited_after_bind=False,
            corrupt=True,
        )

    return LocalCompletionBindingStatus(
        structurally_complete=True,
        human_edited_after_bind=human_edited,
        corrupt=False,
    )


def local_completion_is_structurally_done(
    *,
    run: TranskribusRun,
    engine: str,
    is_hebrew: bool,
) -> bool:
    """True when association + SUCCEEDED + structurally valid bindings exist."""
    if run.status != TranskribusRun.Status.SUCCEEDED:
        return False
    snapshot = find_ready_automatic_snapshot_for_run(run)
    if snapshot is None:
        return False
    status = inspect_local_completion_bindings(
        document_id=run.document_id,
        engine=engine,
        snapshot=snapshot,
        is_hebrew=is_hebrew,
    )
    if status.corrupt:
        raise TranskribusLocalCompletionError(
            f"Corrupt Transkribus snapshot bindings for run id={run.pk}; "
            "refusing to treat as completed or overwrite."
        )
    return status.structurally_complete


def _succeeded_run_is_incomplete_for_resume(
    run: TranskribusRun,
    *,
    engine_runtime: str,
    is_hebrew: bool,
) -> bool:
    """SUCCEEDED is resumable only when local completion is demonstrably incomplete."""
    assoc = get_run_automatic_snapshot_association(run)
    if assoc is None:
        # Historical SUCCEEDED without association: not interrupted new-pipeline work.
        return False
    snapshot = find_ready_automatic_snapshot_for_run(run)
    if snapshot is None:
        return False
    status = inspect_local_completion_bindings(
        document_id=run.document_id,
        engine=engine_runtime,
        snapshot=snapshot,
        is_hebrew=is_hebrew,
    )
    if status.corrupt:
        raise TranskribusLocalCompletionError(
            f"Corrupt bindings while resuming run id={run.pk}."
        )
    return not status.structurally_complete


def find_upload_local_completion_resume_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
    engine_runtime: str,
    is_hebrew: bool,
) -> TranskribusRun | None:
    """Resume candidate for UPLOAD_CREATED using blocking-run rules."""
    blocking = trp.find_blocking_upload_run(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    )
    if blocking is None:
        return None
    if blocking.mode != TranskribusRun.Mode.UPLOAD_CREATED:
        return None
    if not _run_has_durable_recognition_ids(blocking):
        return None

    if blocking.status == TranskribusRun.Status.RECOGNITION_STARTED:
        return blocking

    if blocking.status == TranskribusRun.Status.SUCCEEDED:
        # Incomplete local completion only. Fully complete → still returned for
        # idempotent SQS duplicate no-op via already_complete (upload blocking).
        assoc = get_run_automatic_snapshot_association(blocking)
        if assoc is None:
            return None
        return blocking

    return None


def find_existing_server_local_completion_resume_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
    engine_runtime: str,
    is_hebrew: bool,
) -> TranskribusRun | None:
    """EXISTING_SERVER resume: incomplete only; never no-op fully completed runs.

    SQS payloads only carry ``document_id`` (no attempt id), so a fully completed
    SUCCEEDED EXISTING_SERVER run cannot be distinguished from a new processing
    request. Fully complete runs are therefore not selected; duplicate delivery
    after success may start a new attempt (documented limitation).
    """
    col = str(collection_id).strip()
    mid = str(model_id).strip()

    recognition_started = list(
        TranskribusRun.objects.filter(
            document_id=document_id,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id=col,
            model_id=mid,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
        ).order_by("id")
    )
    eligible_started = [
        run for run in recognition_started if _run_has_durable_recognition_ids(run)
    ]
    if len(eligible_started) > 1:
        raise TranskribusLocalCompletionError(
            "Ambiguous EXISTING_SERVER RECOGNITION_STARTED runs for local completion "
            f"resume (document_id={document_id}, count={len(eligible_started)})."
        )
    if len(eligible_started) == 1:
        return eligible_started[0]

    succeeded = list(
        TranskribusRun.objects.filter(
            document_id=document_id,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id=col,
            model_id=mid,
            status=TranskribusRun.Status.SUCCEEDED,
        ).order_by("id")
    )
    incomplete: list[TranskribusRun] = []
    for run in succeeded:
        if not _run_has_durable_recognition_ids(run):
            continue
        if _succeeded_run_is_incomplete_for_resume(
            run, engine_runtime=engine_runtime, is_hebrew=is_hebrew
        ):
            incomplete.append(run)
    if len(incomplete) > 1:
        raise TranskribusLocalCompletionError(
            "Ambiguous incomplete EXISTING_SERVER SUCCEEDED runs for local "
            f"completion resume (document_id={document_id}, count={len(incomplete)})."
        )
    if len(incomplete) == 1:
        return incomplete[0]
    return None


@dataclass(frozen=True)
class LocalCompletionResumePlan:
    run: TranskribusRun
    snapshot: TranskribusTranscriptSnapshot | None
    association: TranskribusRunAutomaticSnapshot | None
    already_complete: bool


def plan_local_completion_resume(
    *,
    document: Document,
    run: TranskribusRun,
    engine_runtime: str,
) -> LocalCompletionResumePlan:
    is_hebrew = _is_hebrew_language(document.language)
    assoc = get_run_automatic_snapshot_association(run)
    snapshot = find_ready_automatic_snapshot_for_run(run) if assoc else None
    already = False
    if snapshot is not None and run.status == TranskribusRun.Status.SUCCEEDED:
        status = inspect_local_completion_bindings(
            document_id=document.pk,
            engine=engine_runtime,
            snapshot=snapshot,
            is_hebrew=is_hebrew,
        )
        if status.corrupt:
            raise TranskribusLocalCompletionError(
                f"Corrupt bindings for run id={run.pk} during resume planning."
            )
        # Structurally complete (including human-edited-after-bind) → idempotent.
        already = status.structurally_complete
    return LocalCompletionResumePlan(
        run=run,
        snapshot=snapshot,
        association=assoc,
        already_complete=already,
    )


def store_automatic_snapshot_from_selected_pages(
    *,
    document: Document,
    run: TranskribusRun,
    page_inputs,
    mapping_trusted: bool,
    review_reasons: Sequence[str] | None = None,
) -> TranskribusTranscriptSnapshot:
    """Persist AUTOMATIC_HTR snapshot and associate it with ``run``.

    On REUSED_EXISTING, snapshot origin FK may point at an earlier run; this run
    still gets its own ``TranskribusRunAutomaticSnapshot`` row. Never mutates
    READY snapshot fields (including hover_eligible).
    """
    try:
        result = store_transkribus_transcript_snapshot(
            document=document,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            pages=page_inputs,
            transkribus_run=run,
            remote_doc_id=str(run.remote_doc_id or ""),
            collection_id=str(run.collection_id or ""),
            model_id=str(run.model_id or ""),
            recognition_job_id=str(run.recognition_job_id or ""),
            # Only applies to newly created pending rows; reuse leaves READY immutable.
            hover_eligible=False if not mapping_trusted else None,
        )
    except TranskribusSnapshotStorageUploadError as exc:
        raise TranskribusLocalPersistenceRetryableError(str(exc)) from exc
    except TranskribusSnapshotStorageValidationError:
        raise

    snapshot = result.snapshot
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise TranskribusLocalPersistenceRetryableError(
            f"Snapshot id={snapshot.pk} is not READY after store "
            f"(status={snapshot.storage_status})."
        )

    reasons = list(review_reasons or [])
    if not reasons:
        reasons = reconstruct_engine_review_reasons_from_snapshot(snapshot)

    associate_run_with_automatic_snapshot(
        run=run,
        snapshot=snapshot,
        mapping_trusted=mapping_trusted,
        review_reasons=reasons,
    )
    return snapshot


def _next_source_revision(*, existing: DocumentTextResult | None, new_text: str) -> int:
    if existing is None:
        return 1
    old_text = existing.text if existing.text is not None else ""
    if old_text == new_text:
        return int(existing.source_revision)
    return int(existing.source_revision) + 1


def complete_transkribus_local_success(
    *,
    document_id: int,
    run_id: int,
    snapshot_id: int,
    text: str,
    engine: str,
    route: OcrRouteConfig,
    needs_review: bool,
    review_reasons: Optional[Sequence[str]],
    min_text_length: int,
    pre_run_processing_state: Optional[str] = None,
) -> HtrResult:
    """Atomically persist DTR + bindings + association check + mark SUCCEEDED.

    ``pre_run_processing_state`` is the worker's processing_state_user from
    before Phase 1 wrote PROCESSING. A VERIFIED write-fence restores it instead
    of rolling up from the unused runtime engine.
    """
    with transaction.atomic():
        doc = Document.objects.select_for_update().get(pk=document_id)
        run = TranskribusRun.objects.select_for_update().get(pk=run_id)
        if run.document_id != doc.pk:
            raise TranskribusLocalCompletionError(
                f"TranskribusRun id={run_id} belongs to document_id={run.document_id}, "
                f"not document_id={document_id}."
            )

        try:
            assoc = TranskribusRunAutomaticSnapshot.objects.select_for_update().get(
                run_id=run.pk
            )
        except TranskribusRunAutomaticSnapshot.DoesNotExist as exc:
            raise TranskribusLocalCompletionError(
                f"Missing TranskribusRunAutomaticSnapshot for run id={run.pk}."
            ) from exc

        snapshot = TranskribusTranscriptSnapshot.objects.select_for_update().get(
            pk=assoc.snapshot_id
        )
        if snapshot.pk != snapshot_id:
            raise TranskribusLocalCompletionError(
                f"Worker snapshot_id={snapshot_id} does not match association "
                f"snapshot_id={snapshot.pk} for run id={run.pk}."
            )
        if snapshot.document_id != doc.pk:
            raise TranskribusLocalCompletionError(
                "Snapshot and document mismatch for local completion."
            )
        if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
            raise TranskribusLocalCompletionError(
                f"Cannot complete local success with non-READY snapshot "
                f"id={snapshot.pk}."
            )

        is_hebrew = _is_hebrew_language(doc.language)
        fence = inspect_automated_ocr_verified_write_fence(doc.pk)
        if fence.blocked:
            logger.info(
                "skipping automated Transkribus OCR persistence; "
                "document has VERIFIED text result",
                extra={
                    "document_id": document_id,
                    "runtime_engine": engine,
                    "verified_engine": fence.verified_engine,
                    "run_id": run.pk,
                },
            )
            # Do not roll up from this unused runtime engine or from one
            # VERIFIED row's engine. Restore the worker's pre-Phase-1 state
            # when provided; otherwise leave processing_state_user unchanged.
            if pre_run_processing_state is not None:
                doc.processing_state_user = pre_run_processing_state
                doc.save(update_fields=["processing_state_user"])
            trp.mark_succeeded(run, engine_runtime=engine)
            return HtrResult(
                text=text,
                needs_review=needs_review,
                engine_name=engine,
                review_reasons=list(review_reasons or []),
                transkribus_run_id=run.pk,
                transkribus_snapshot_id=snapshot.pk,
            )

        bind_status = inspect_local_completion_bindings(
            document_id=doc.pk,
            engine=engine,
            snapshot=snapshot,
            is_hebrew=is_hebrew,
        )
        if bind_status.corrupt:
            raise TranskribusLocalCompletionError(
                f"Corrupt bindings for run id={run.pk}; refusing overwrite."
            )
        if (
            bind_status.structurally_complete
            and run.status == TranskribusRun.Status.SUCCEEDED
        ):
            # Duplicate delivery / human-edited-after-bind: do not overwrite text.
            update_document_processing_state_for_engine(doc, engine)
            doc.save(update_fields=["processing_state_user"])
            return HtrResult(
                text=text,
                needs_review=needs_review,
                engine_name=engine,
                review_reasons=list(review_reasons or []),
                transkribus_run_id=run.pk,
                transkribus_snapshot_id=snapshot.pk,
            )

        existing_rows = list(
            DocumentTextResult.objects.select_for_update()
            .filter(document_id=doc.pk, engine=engine)
            .order_by("id")
        )
        existing_by_type = {row.result_type: row for row in existing_rows}

        reasons = derive_automatic_review_reasons(
            text,
            needs_review,
            review_reasons,
            min_text_length=min_text_length,
        )
        status = DocumentTextResult.Status.NEEDS_REVIEW
        verification = DocumentTextResult.VerificationStatus.UNVERIFIED
        text_sha = compute_sha256_hex(text)
        if text_sha != (snapshot.canonical_text_sha256 or "").strip():
            raise TranskribusLocalCompletionError(
                "HTR text SHA-256 does not match snapshot canonical_text_sha256."
            )

        source_existing = existing_by_type.get(
            DocumentTextResult.ResultType.SOURCE_TEXT
        )
        source_revision = _next_source_revision(existing=source_existing, new_text=text)

        source_row, _ = DocumentTextResult.objects.update_or_create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            defaults={
                "status": status,
                "text": text,
                "engine_key": route.engine_key,
                "prompt_variant": route.prompt_variant,
                "verification_status": verification,
                "error_code": None,
                "error_details": None,
                "review_reasons": json.dumps(reasons),
                "source_revision": source_revision,
            },
        )

        bind_text_result_to_snapshot(
            text_result=source_row,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=source_revision,
        )

        if is_hebrew:
            hebrew_row, _ = DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=engine,
                defaults={
                    "status": status,
                    "text": text,
                    "engine_key": route.engine_key,
                    "prompt_variant": route.prompt_variant,
                    "verification_status": verification,
                    "error_code": None,
                    "error_details": None,
                    "review_reasons": json.dumps(reasons),
                    "based_on_source_revision": source_revision,
                },
            )
            bind_text_result_to_snapshot(
                text_result=hebrew_row,
                snapshot=snapshot,
                binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
                bound_source_revision=source_revision,
            )

        # Ensure association remains pointed at this snapshot inside the same TX.
        assoc.snapshot = snapshot
        assoc.save(update_fields=["snapshot", "updated_at"])

        update_document_processing_state_for_engine(doc, engine)
        doc.save(update_fields=["processing_state_user"])

        trp.mark_succeeded(run, engine_runtime=engine)

        # Sync after final DTR/bindings/run success. Skip the early no-overwrite
        # path above. Lock order: Document → Run → Assoc → Snapshot → DTRs →
        # ArchiveItem (inside sync).
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        sync_archive_item_search_index(doc.archive_item_id)

        return HtrResult(
            text=text,
            needs_review=needs_review,
            engine_name=engine,
            review_reasons=list(review_reasons or []),
            transkribus_run_id=run.pk,
            transkribus_snapshot_id=snapshot.pk,
        )


__all__ = [
    "TranskribusLocalCompletionError",
    "LocalCompletionResumePlan",
    "LocalCompletionBindingStatus",
    "associate_run_with_automatic_snapshot",
    "complete_transkribus_local_success",
    "derive_automatic_review_reasons",
    "find_existing_server_local_completion_resume_run",
    "find_ready_automatic_snapshot_for_run",
    "find_upload_local_completion_resume_run",
    "get_run_automatic_snapshot_association",
    "inspect_local_completion_bindings",
    "local_completion_is_structurally_done",
    "plan_local_completion_resume",
    "reconstruct_engine_review_reasons_from_snapshot",
    "store_automatic_snapshot_from_selected_pages",
    "TranskribusSnapshotBindingError",
]
