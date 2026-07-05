"""Roll up Document.processing_state_user from persisted text results."""

from __future__ import annotations

from documents.models import Document, DocumentTextResult
from documents.services.expected_outputs import expected_result_types_for_document


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
