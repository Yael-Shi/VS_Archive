from __future__ import annotations

import re

from django.db.models import Q

from documents.models import TranskribusRun

_BLOCKING_UPLOAD_STATUSES = frozenset(
    {
        TranskribusRun.Status.STARTED,
        TranskribusRun.Status.UPLOADED,
        TranskribusRun.Status.RECOGNITION_STARTED,
        TranskribusRun.Status.SUCCEEDED,
    }
)

_REUSABLE_UPLOAD_STATUSES = frozenset(
    {
        TranskribusRun.Status.FAILED,
        TranskribusRun.Status.UPLOADED,
        TranskribusRun.Status.RECOGNITION_STARTED,
    }
)

_MAX_ERROR_DETAILS_LEN = 4000

_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(password|passwd|api[_-]?token|bearer|authorization|jsessionid|cookie)\b"),
    re.compile(r"(?i)\b(transkribus_username|transkribus_password|gemini_api_key)\b"),
)


def sanitize_error_details(message: str, *, max_len: int = _MAX_ERROR_DETAILS_LEN) -> str:
    """Return bounded error text safe for persistence (no secrets or tracebacks)."""
    text = (message or "").strip()
    if not text:
        return "Transkribus attempt failed."
    first_line = text.splitlines()[0].strip()
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.search(first_line):
            return "Transkribus attempt failed."
    if len(first_line) > max_len:
        return first_line[: max_len - 3] + "..."
    return first_line


def _upload_run_blocks_new_upload(run: TranskribusRun) -> bool:
    if run.status in _BLOCKING_UPLOAD_STATUSES:
        return True
    if run.status == TranskribusRun.Status.FAILED:
        return bool((run.remote_doc_id or "").strip())
    return False


def _run_is_reusable_for_recognition_retry(run: TranskribusRun) -> bool:
    if run.status not in _REUSABLE_UPLOAD_STATUSES:
        return False
    if not (run.remote_doc_id or "").strip():
        return False
    if not (run.pages_query or "").strip():
        return False
    return True


def get_upload_run_for_recognition_retry(
    *,
    run_id: int,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> TranskribusRun:
    """
    Load an explicit UPLOAD_CREATED TranskribusRun for recognition-only retry.

    Validates document/collection/model ownership and reusable-run rules.
    Raises ValueError when the run cannot be used (no rediscovery fallback).
    """
    try:
        run = TranskribusRun.objects.get(pk=run_id)
    except TranskribusRun.DoesNotExist as exc:
        raise ValueError(f"TranskribusRun id={run_id} does not exist.") from exc

    if run.document_id != document_id:
        raise ValueError(
            f"TranskribusRun id={run_id} belongs to document_id={run.document_id}, "
            f"not document_id={document_id}."
        )

    col = str(collection_id).strip()
    mid = str(model_id).strip()
    if run.collection_id != col or run.model_id != mid:
        raise ValueError(
            f"TranskribusRun id={run_id} collection_id/model_id="
            f"{run.collection_id!r}/{run.model_id!r} does not match active "
            f"collection_id/model_id={col!r}/{mid!r}."
        )

    if run.mode != TranskribusRun.Mode.UPLOAD_CREATED:
        raise ValueError(
            f"TranskribusRun id={run_id} mode={run.mode!r} is not UPLOAD_CREATED."
        )

    if not _run_is_reusable_for_recognition_retry(run):
        remote = (run.remote_doc_id or "").strip() or "none"
        pages = (run.pages_query or "").strip() or "none"
        raise ValueError(
            f"TranskribusRun id={run_id} is not reusable for recognition-only retry "
            f"(status={run.status!r}, remote_doc_id={remote}, pages_query={pages})."
        )

    return run


def find_reusable_upload_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> TranskribusRun | None:
    """
    Return the most recent UPLOAD_CREATED run that can seed recognition-only retry
    for the same (document_id, collection_id, model_id), or None.

    Qualifying statuses: FAILED, UPLOADED, RECOGNITION_STARTED (not SUCCEEDED).
    Requires non-empty remote_doc_id and pages_query after strip.
    """
    col = str(collection_id).strip()
    mid = str(model_id).strip()
    candidates = (
        TranskribusRun.objects.filter(
            document_id=document_id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id=col,
            model_id=mid,
            status__in=_REUSABLE_UPLOAD_STATUSES,
        )
        .order_by("-created_at", "-id")
    )
    for run in candidates:
        if _run_is_reusable_for_recognition_retry(run):
            return run
    return None


def find_blocking_upload_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> TranskribusRun | None:
    """
    Return the most recent UPLOAD_CREATED run that blocks a new Trp upload for the
    same (document_id, collection_id, model_id), or None if upload may proceed.

    Blocks: STARTED, UPLOADED, RECOGNITION_STARTED, SUCCEEDED, or FAILED with a
    non-empty remote_doc_id (after strip). Does not block FAILED with remote_doc_id
    null, empty, or whitespace-only.
    """
    col = str(collection_id).strip()
    mid = str(model_id).strip()
    candidates = (
        TranskribusRun.objects.filter(
            document_id=document_id,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id=col,
            model_id=mid,
        )
        .filter(
            Q(status__in=_BLOCKING_UPLOAD_STATUSES)
            | Q(status=TranskribusRun.Status.FAILED)
        )
        .order_by("-created_at", "-id")
    )
    for run in candidates:
        if _upload_run_blocks_new_upload(run):
            return run
    return None


def format_upload_blocked_error_message(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
    blocking_run: TranskribusRun,
) -> str:
    remote = (blocking_run.remote_doc_id or "").strip() or "none"
    return (
        f"Transkribus upload blocked: document_id={document_id} already has "
        f"UPLOAD_CREATED run id={blocking_run.id} status={blocking_run.status} "
        f"remote_doc_id={remote} (collection_id={str(collection_id).strip()}, "
        f"model_id={str(model_id).strip()}). "
        "Set TRANSKRIBUS_FORCE_REPROCESS=true to create another Transkribus document "
        "(may orphan prior Trp documents)."
    )


def start_run(
    *,
    document_id: int,
    mode: str,
    collection_id: str,
    model_id: str,
    remote_doc_id: str | None = None,
    pages_query: str | None = None,
) -> TranskribusRun:
    return TranskribusRun.objects.create(
        document_id=document_id,
        status=TranskribusRun.Status.STARTED,
        mode=mode,
        collection_id=str(collection_id).strip(),
        model_id=str(model_id).strip(),
        remote_doc_id=remote_doc_id,
        pages_query=pages_query,
    )


def apply_source_upload_metadata(
    run: TranskribusRun,
    *,
    source: TranskribusRun,
) -> TranskribusRun:
    """
    Copy upload-time metadata from a prior UPLOAD_CREATED run onto a new attempt row
    without calling TrpServer upload APIs (recognition-only retry).
    """
    raw_mapping = source.page_index_to_page_nr
    if raw_mapping is None:
        page_map: dict[int, int] | None = None
    elif isinstance(raw_mapping, dict):
        page_map = {int(k): int(v) for k, v in raw_mapping.items()}
    else:
        page_map = {int(k): int(v) for k, v in dict(raw_mapping).items()}

    run.status = TranskribusRun.Status.UPLOADED
    run.remote_doc_id = str(source.remote_doc_id).strip()
    run.pages_query = str(source.pages_query).strip()
    run.page_index_to_page_nr = page_map
    run.upload_id = source.upload_id
    ingest = (source.ingest_job_id or "").strip()
    run.ingest_job_id = ingest if ingest else None
    run.save(
        update_fields=[
            "status",
            "remote_doc_id",
            "upload_id",
            "ingest_job_id",
            "pages_query",
            "page_index_to_page_nr",
            "updated_at",
        ]
    )
    return run


def mark_uploaded(
    run: TranskribusRun,
    *,
    remote_doc_id: str,
    upload_id: int,
    ingest_job_id: str,
    pages_query: str,
    page_index_to_page_nr: dict[int, int],
) -> TranskribusRun:
    run.status = TranskribusRun.Status.UPLOADED
    run.remote_doc_id = str(remote_doc_id).strip()
    run.upload_id = int(upload_id)
    run.ingest_job_id = str(ingest_job_id).strip()
    run.pages_query = str(pages_query).strip()
    run.page_index_to_page_nr = dict(page_index_to_page_nr)
    run.save(
        update_fields=[
            "status",
            "remote_doc_id",
            "upload_id",
            "ingest_job_id",
            "pages_query",
            "page_index_to_page_nr",
            "updated_at",
        ]
    )
    return run


def mark_recognition_started(
    run: TranskribusRun,
    *,
    recognition_job_id: str,
) -> TranskribusRun:
    run.status = TranskribusRun.Status.RECOGNITION_STARTED
    run.recognition_job_id = str(recognition_job_id).strip()
    run.save(update_fields=["status", "recognition_job_id", "updated_at"])
    return run


def mark_succeeded(
    run: TranskribusRun,
    *,
    engine_runtime: str,
) -> TranskribusRun:
    run.status = TranskribusRun.Status.SUCCEEDED
    run.engine_runtime = str(engine_runtime).strip()
    run.error_code = None
    run.error_details = None
    run.save(
        update_fields=[
            "status",
            "engine_runtime",
            "error_code",
            "error_details",
            "updated_at",
        ]
    )
    return run


def mark_failed(
    run: TranskribusRun,
    *,
    error_code: str,
    error_details: str,
) -> TranskribusRun:
    run.status = TranskribusRun.Status.FAILED
    run.error_code = str(error_code).strip()[:64]
    run.error_details = sanitize_error_details(error_details)
    run.save(update_fields=["status", "error_code", "error_details", "updated_at"])
    return run
