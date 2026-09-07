"""Roll up Document.processing_state_user from persisted text results.

This helper writes READY / PARTIAL / FAILED only. It does not produce
RECOVERY_REQUIRED; that overlay is written from ProcessDocumentRequest fencing.
"""

from __future__ import annotations

from documents.models import Document, DocumentTextResult
from documents.services.expected_outputs import expected_result_types_for_document

ORDINARY_RESULT_PROCESSING_STATES = frozenset(
    {
        Document.ProcessingState.READY,
        Document.ProcessingState.PARTIAL,
        Document.ProcessingState.FAILED,
    }
)


def apply_verified_fence_processing_state_restore(
    doc: Document,
    prior_processing_state: str | None,
) -> bool:
    """Restore the pre-run Document state without inventing a DTR rollup.

    When the Document currently shows the request-lifecycle overlay
    ``RECOVERY_REQUIRED``, restore only an ordinary result state
    (``READY`` / ``PARTIAL`` / ``FAILED``). Restoring ``PROCESSING`` would
    re-stick a false in-progress signal; restoring ``RECOVERY_REQUIRED`` is
    a no-op.

    Returns True when ``processing_state_user`` was assigned.
    """
    if prior_processing_state is None:
        return False
    if (
        doc.processing_state_user == Document.ProcessingState.RECOVERY_REQUIRED
        and prior_processing_state not in ORDINARY_RESULT_PROCESSING_STATES
    ):
        return False
    if doc.processing_state_user == prior_processing_state:
        return False
    doc.processing_state_user = prior_processing_state
    return True


def update_document_processing_state_for_engine(doc: Document, engine: str) -> None:
    expected_types = expected_result_types_for_document(doc)

    # Fetch all expected rows in a single query. The (document, result_type, engine)
    # unique constraint guarantees at most one row per result_type, so keying by
    # result_type returns exactly the same rows as the previous per-type .first()
    # lookups while avoiding one query per expected result type.
    rows_by_type: dict[str, DocumentTextResult] = {
        row.result_type: row
        for row in doc.text_results.filter(
            engine=engine, result_type__in=expected_types
        )
    }

    all_rows: list[DocumentTextResult] = []
    for result_type in expected_types:
        row = rows_by_type.get(result_type)
        if row is None:
            doc.processing_state_user = Document.ProcessingState.PARTIAL
            return
        all_rows.append(row)

    def _row_usable(row: DocumentTextResult) -> bool:
        if row.status not in (
            DocumentTextResult.Status.SUCCEEDED,
            DocumentTextResult.Status.NEEDS_REVIEW,
        ):
            return False
        return bool((row.text or "").strip())

    all_failed = all(r.status == DocumentTextResult.Status.FAILED for r in all_rows)
    if all_failed:
        doc.processing_state_user = Document.ProcessingState.FAILED
        return

    if all(_row_usable(r) for r in all_rows):
        doc.processing_state_user = Document.ProcessingState.READY
        return

    doc.processing_state_user = Document.ProcessingState.PARTIAL
