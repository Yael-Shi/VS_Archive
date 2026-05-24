from __future__ import annotations

import re

from documents.models import TranskribusRun

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
