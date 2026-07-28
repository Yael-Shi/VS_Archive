from __future__ import annotations

from django.test import SimpleTestCase

from documents.management.commands.run_worker import (
    _outcome_for_final_processing_state,
)
from documents.models import Document
from documents.services.process_document_outcome import (
    ProcessDocumentDisposition,
    ProcessDocumentOutcome,
)


class ProcessDocumentOutcomeTests(SimpleTestCase):
    def test_terminal_and_noop_dispositions_ack(self) -> None:
        for disposition in (
            ProcessDocumentDisposition.COMPLETED,
            ProcessDocumentDisposition.PARTIAL,
            ProcessDocumentDisposition.FAILED,
            ProcessDocumentDisposition.NOOP,
        ):
            with self.subTest(disposition=disposition):
                outcome = ProcessDocumentOutcome(disposition)
                self.assertTrue(outcome.should_ack)

    def test_final_document_state_mapping_preserves_partial(self) -> None:
        ready = _outcome_for_final_processing_state(Document.ProcessingState.READY)
        partial = _outcome_for_final_processing_state(Document.ProcessingState.PARTIAL)
        failed = _outcome_for_final_processing_state(Document.ProcessingState.FAILED)

        self.assertEqual(
            ready.disposition,
            ProcessDocumentDisposition.COMPLETED,
        )
        self.assertEqual(
            partial.disposition,
            ProcessDocumentDisposition.PARTIAL,
        )
        self.assertEqual(
            partial.failure_code,
            "PROCESS_DOCUMENT_PARTIAL",
        )
        self.assertTrue(partial.should_ack)
        self.assertEqual(
            failed.disposition,
            ProcessDocumentDisposition.FAILED,
        )
        self.assertEqual(
            failed.failure_code,
            "PROCESS_DOCUMENT_INCOMPLETE",
        )

    def test_deferred_and_retryable_dispositions_do_not_ack(self) -> None:
        for disposition in (
            ProcessDocumentDisposition.DEFERRED,
            ProcessDocumentDisposition.RETRYABLE,
        ):
            with self.subTest(disposition=disposition):
                outcome = ProcessDocumentOutcome(disposition)
                self.assertFalse(outcome.should_ack)

    def test_failure_metadata_is_preserved(self) -> None:
        outcome = ProcessDocumentOutcome(
            ProcessDocumentDisposition.FAILED,
            failure_code="OCR_FAILED",
            failure_message="provider failed",
        )

        self.assertEqual(outcome.failure_code, "OCR_FAILED")
        self.assertEqual(outcome.failure_message, "provider failed")
