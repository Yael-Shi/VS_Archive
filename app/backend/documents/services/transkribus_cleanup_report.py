from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.utils import timezone

from documents.models import DocumentTextResult, TranskribusRun

RETAIN_EXISTING_SERVER = "retain_existing_server"
RETAIN_VERIFIED_DOCUMENT = "retain_verified_document"
RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC = "retain_latest_or_reusable_remote_doc"
RETAIN_AMBIGUOUS_LOCAL_STATE = "retain_ambiguous_local_state"
REVIEW_SUPERSEDED_FORCE_REPROCESS_REMOTE_DOC = (
    "review_superseded_force_reprocess_remote_doc"
)
REVIEW_FAILED_AFTER_UPLOAD_REMOTE_DOC = "review_failed_after_upload_remote_doc"
REVIEW_STALE_IN_PROGRESS_RUN = "review_stale_in_progress_run"
LOCAL_ONLY_FAILED_WITHOUT_REMOTE_DOC = "local_only_failed_without_remote_doc"
RETAIN_RECENT_IN_PROGRESS_RUN = "retain_recent_in_progress_run"

_IN_PROGRESS_STATUSES = frozenset(
    {
        TranskribusRun.Status.STARTED,
        TranskribusRun.Status.UPLOADED,
        TranskribusRun.Status.RECOGNITION_STARTED,
    }
)
_REUSABLE_STATUSES = frozenset(
    {
        TranskribusRun.Status.FAILED,
        TranskribusRun.Status.UPLOADED,
        TranskribusRun.Status.RECOGNITION_STARTED,
    }
)


def build_transkribus_cleanup_report(
    *,
    document_id: int | None = None,
    collection_id: str | None = None,
    model_id: str | None = None,
    stale_hours: int = 24,
) -> dict[str, Any]:
    """
    Build a dry-run cleanup/retention report from local DB state only.

    This function never makes network calls and never mutates local rows.
    """
    stale_hours_int = int(stale_hours)
    if stale_hours_int < 1:
        raise ValueError("stale_hours must be >= 1")

    normalized_collection_id = _normalize_text(collection_id)
    normalized_model_id = _normalize_text(model_id)

    qs = TranskribusRun.objects.all().order_by(
        "document_id",
        "collection_id",
        "model_id",
        "created_at",
        "id",
    )
    if document_id is not None:
        qs = qs.filter(document_id=int(document_id))
    if normalized_collection_id is not None:
        qs = qs.filter(collection_id=normalized_collection_id)
    if normalized_model_id is not None:
        qs = qs.filter(model_id=normalized_model_id)

    runs = list(qs)
    doc_ids = {run.document_id for run in runs}
    verified_doc_ids = set()
    if doc_ids:
        verified_doc_ids = set(
            DocumentTextResult.objects.filter(
                document_id__in=doc_ids,
                verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            )
            .values_list("document_id", flat=True)
            .distinct()
        )

    stale_cutoff = timezone.now() - timedelta(hours=stale_hours_int)
    lineages: dict[tuple[int, str, str], list[TranskribusRun]] = defaultdict(list)
    remote_doc_usage: dict[tuple[str, str], set[tuple[int, str, str]]] = defaultdict(set)
    remote_doc_modes: dict[tuple[str, str], set[str]] = defaultdict(set)

    for run in runs:
        lineage_key = _lineage_key(run)
        lineages[lineage_key].append(run)
        remote_doc_id = _normalized_remote_doc_id(run)
        if remote_doc_id is not None:
            remote_key = (_normalize_text(run.collection_id) or "", remote_doc_id)
            remote_doc_usage[remote_key].add(lineage_key)
            remote_doc_modes[remote_key].add(run.mode)

    remote_docs: list[dict[str, Any]] = []
    remote_lookup: dict[tuple[int, str, str, str], dict[str, Any]] = {}

    for lineage_key, lineage_runs in lineages.items():
        groups: dict[str, list[TranskribusRun]] = defaultdict(list)
        for run in lineage_runs:
            remote_doc_id = _normalized_remote_doc_id(run)
            if remote_doc_id is not None:
                groups[remote_doc_id].append(run)

        newest_useful_remote_doc_id = _newest_useful_remote_doc_id(groups)
        for remote_doc_id, grouped_runs in sorted(groups.items()):
            remote_doc = _classify_remote_doc_group(
                lineage_key=lineage_key,
                remote_doc_id=remote_doc_id,
                runs=grouped_runs,
                newest_useful_remote_doc_id=newest_useful_remote_doc_id,
                verified_doc_ids=verified_doc_ids,
                shared_lineages=remote_doc_usage[(_normalize_text(grouped_runs[0].collection_id) or "", remote_doc_id)],
                shared_modes=remote_doc_modes[(_normalize_text(grouped_runs[0].collection_id) or "", remote_doc_id)],
                stale_cutoff=stale_cutoff,
            )
            remote_docs.append(remote_doc)
            remote_lookup[(lineage_key[0], lineage_key[1], lineage_key[2], remote_doc_id)] = (
                remote_doc
            )

    run_rows: list[dict[str, Any]] = []
    for run in runs:
        run_rows.append(
            _classify_run(
                run,
                stale_cutoff=stale_cutoff,
                remote_lookup=remote_lookup,
            )
        )

    remote_bucket_counts = Counter(item["bucket"] for item in remote_docs)
    run_bucket_counts = Counter(item["bucket"] for item in run_rows)

    return {
        "generated_at": timezone.now().isoformat(),
        "filters": {
            "document_id": int(document_id) if document_id is not None else None,
            "collection_id": normalized_collection_id,
            "model_id": normalized_model_id,
            "stale_hours": stale_hours_int,
        },
        "summary": {
            "lineage_count": len(lineages),
            "run_count": len(run_rows),
            "remote_doc_count": len(remote_docs),
            "remote_doc_bucket_counts": dict(sorted(remote_bucket_counts.items())),
            "run_bucket_counts": dict(sorted(run_bucket_counts.items())),
        },
        "remote_docs": sorted(
            remote_docs,
            key=lambda item: (
                item["document_id"],
                item["collection_id"],
                item["model_id"],
                item["remote_doc_id"],
                item["latest_run_id"],
            ),
        ),
        "runs": sorted(
            run_rows,
            key=lambda item: (
                item["document_id"],
                item["run_id"],
            ),
        ),
    }


def _classify_remote_doc_group(
    *,
    lineage_key: tuple[int, str, str],
    remote_doc_id: str,
    runs: list[TranskribusRun],
    newest_useful_remote_doc_id: str | None,
    verified_doc_ids: set[int],
    shared_lineages: set[tuple[int, str, str]],
    shared_modes: set[str],
    stale_cutoff,
) -> dict[str, Any]:
    latest_run = runs[-1]
    document_id, collection_id, model_id = lineage_key
    has_reusable_run = any(_run_is_reusable_for_retry(run) for run in runs)
    has_succeeded_run = any(run.status == TranskribusRun.Status.SUCCEEDED for run in runs)
    has_failed_run = any(run.status == TranskribusRun.Status.FAILED for run in runs)
    has_stale_in_progress_run = any(
        run.status in _IN_PROGRESS_STATUSES and run.updated_at <= stale_cutoff
        for run in runs
    )
    contains_existing_server = any(
        run.mode == TranskribusRun.Mode.EXISTING_SERVER for run in runs
    )
    shared_across_lineages = len(shared_lineages) > 1 or len(shared_modes) > 1
    has_verified_text = document_id in verified_doc_ids

    reasons: list[str] = []
    if contains_existing_server:
        bucket = RETAIN_EXISTING_SERVER
        reasons.append("existing_server_remote_doc_not_owned_by_vs_archive")
    elif shared_across_lineages:
        bucket = RETAIN_AMBIGUOUS_LOCAL_STATE
        reasons.append("same_remote_doc_id_referenced_by_multiple_local_lineages")
    elif has_verified_text:
        bucket = RETAIN_VERIFIED_DOCUMENT
        reasons.append("document_has_verified_text_result")
    elif has_reusable_run:
        bucket = RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC
        reasons.append("remote_doc_still_reusable_for_recognition_only_retry")
    elif newest_useful_remote_doc_id == remote_doc_id and has_succeeded_run:
        bucket = RETAIN_LATEST_OR_REUSABLE_REMOTE_DOC
        reasons.append("newest_successful_remote_doc_in_lineage")
    elif has_failed_run:
        bucket = REVIEW_FAILED_AFTER_UPLOAD_REMOTE_DOC
        reasons.append("remote_doc_failed_after_remote_identity_was_created")
    else:
        bucket = REVIEW_SUPERSEDED_FORCE_REPROCESS_REMOTE_DOC
        reasons.append("older_remote_doc_superseded_by_newer_successful_remote_doc")

    return {
        "bucket": bucket,
        "reasons": reasons,
        "document_id": document_id,
        "collection_id": collection_id,
        "model_id": model_id,
        "remote_doc_id": remote_doc_id,
        "run_ids": [run.id for run in runs],
        "run_statuses": [run.status for run in runs],
        "run_modes": [run.mode for run in runs],
        "latest_run_id": latest_run.id,
        "latest_run_status": latest_run.status,
        "latest_run_created_at": latest_run.created_at.isoformat(),
        "latest_run_updated_at": latest_run.updated_at.isoformat(),
        "has_verified_text": has_verified_text,
        "has_reusable_run": has_reusable_run,
        "has_succeeded_run": has_succeeded_run,
        "has_failed_run": has_failed_run,
        "has_stale_in_progress_run": has_stale_in_progress_run,
        "contains_existing_server_run": contains_existing_server,
        "shared_across_lineages": shared_across_lineages,
    }


def _classify_run(
    run: TranskribusRun,
    *,
    stale_cutoff,
    remote_lookup: dict[tuple[int, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    remote_doc_id = _normalized_remote_doc_id(run)
    reasons: list[str] = []

    if run.status in _IN_PROGRESS_STATUSES and run.updated_at <= stale_cutoff:
        bucket = REVIEW_STALE_IN_PROGRESS_RUN
        reasons.append("in_progress_run_older_than_stale_threshold")
    elif remote_doc_id is None and run.status == TranskribusRun.Status.FAILED:
        bucket = LOCAL_ONLY_FAILED_WITHOUT_REMOTE_DOC
        reasons.append("failed_before_remote_doc_identity_was_persisted")
    elif remote_doc_id is None and run.status == TranskribusRun.Status.STARTED:
        bucket = RETAIN_RECENT_IN_PROGRESS_RUN
        reasons.append("recent_started_run_without_remote_doc_id")
    elif remote_doc_id is None:
        bucket = RETAIN_AMBIGUOUS_LOCAL_STATE
        reasons.append("run_has_no_remote_doc_id_and_requires_manual_review")
    else:
        lineage_key = _lineage_key(run)
        remote_summary = remote_lookup[(lineage_key[0], lineage_key[1], lineage_key[2], remote_doc_id)]
        bucket = remote_summary["bucket"]
        reasons.extend(remote_summary["reasons"])

    return {
        "bucket": bucket,
        "reasons": reasons,
        "run_id": run.id,
        "document_id": run.document_id,
        "collection_id": _normalize_text(run.collection_id) or "",
        "model_id": _normalize_text(run.model_id) or "",
        "status": run.status,
        "mode": run.mode,
        "remote_doc_id": remote_doc_id,
        "upload_id": run.upload_id,
        "ingest_job_id": run.ingest_job_id,
        "recognition_job_id": run.recognition_job_id,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
    }


def _newest_useful_remote_doc_id(groups: dict[str, list[TranskribusRun]]) -> str | None:
    newest_remote_doc_id: str | None = None
    newest_sort_key: tuple[Any, Any] | None = None

    for remote_doc_id, runs in groups.items():
        if not any(
            _run_is_reusable_for_retry(run) or run.status == TranskribusRun.Status.SUCCEEDED
            for run in runs
        ):
            continue
        latest_run = runs[-1]
        sort_key = (latest_run.created_at, latest_run.id)
        if newest_sort_key is None or sort_key > newest_sort_key:
            newest_remote_doc_id = remote_doc_id
            newest_sort_key = sort_key

    return newest_remote_doc_id


def _run_is_reusable_for_retry(run: TranskribusRun) -> bool:
    return (
        run.mode == TranskribusRun.Mode.UPLOAD_CREATED
        and run.status in _REUSABLE_STATUSES
        and _normalized_remote_doc_id(run) is not None
        and _normalize_text(run.pages_query) is not None
    )


def _normalized_remote_doc_id(run: TranskribusRun) -> str | None:
    return _normalize_text(run.remote_doc_id)


def _lineage_key(run: TranskribusRun) -> tuple[int, str, str]:
    return (
        run.document_id,
        _normalize_text(run.collection_id) or "",
        _normalize_text(run.model_id) or "",
    )


def _normalize_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
