"""Typed execution outcome for legacy PROCESS_DOCUMENT worker work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProcessDocumentDisposition(StrEnum):
    """Semantic execution result, separate from the SQS acknowledgement decision."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    NOOP = "noop"
    DEFERRED = "deferred"
    RETRYABLE = "retryable"


_ACKNOWLEDGED_DISPOSITIONS = frozenset(
    {
        ProcessDocumentDisposition.COMPLETED,
        ProcessDocumentDisposition.PARTIAL,
        ProcessDocumentDisposition.FAILED,
        ProcessDocumentDisposition.NOOP,
    }
)


@dataclass(frozen=True)
class ProcessDocumentOutcome:
    """Describe processing semantics while preserving the current bool ack contract."""

    disposition: ProcessDocumentDisposition
    failure_code: str = ""
    failure_message: str = ""

    @property
    def should_ack(self) -> bool:
        return self.disposition in _ACKNOWLEDGED_DISPOSITIONS
