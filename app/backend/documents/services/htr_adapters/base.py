from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Protocol

from documents.services.page_extraction import PageImage


@dataclass(frozen=True)
class HtrResult:
    text: str
    needs_review: bool = False
    engine_name: str = ""
    review_reasons: List[str] = field(default_factory=list)
    # Optional persisted DocumentTextResult.Quality. None means the worker
    # must not write quality (model default UNKNOWN on insert; existing
    # rows keep their stored value on rerun).
    quality: str | None = None
    # Transkribus automatic snapshot local-completion (optional; not routing metadata).
    transkribus_run_id: int | None = None
    transkribus_snapshot_id: int | None = None


class UnsupportedEngineError(RuntimeError):
    def __init__(self, engine_key: str):
        super().__init__(f"OCR engine is not implemented yet: {engine_key}")
        self.engine_key = engine_key


class EngineRetryableError(RuntimeError):
    pass


class EnginePermanentError(RuntimeError):
    pass


class EnginePageIncompleteError(RuntimeError):
    """Provider-neutral page-checkpoint outcome with explicit missing pages."""

    def __init__(
        self,
        missing_page_indices: list[int],
        *,
        failure_code: str = "OCR_PAGES_INCOMPLETE",
    ) -> None:
        self.missing_page_indices = tuple(sorted(set(missing_page_indices)))
        self.failure_code = failure_code
        joined = ",".join(str(page) for page in self.missing_page_indices)
        self.safe_message = f"missing_pages={joined}"[:512]
        super().__init__(self.safe_message)


class EnginePageCheckpointBusyError(RuntimeError):
    """A current fenced page claim prevents duplicate provider execution."""

    failure_code = "OCR_PAGE_CHECKPOINT_BUSY"

    def __init__(self, page_index: int) -> None:
        self.page_index = page_index
        self.safe_message = f"page_index={page_index}"
        super().__init__(self.safe_message)


class EnginePageCheckpointPersistenceRetryableError(RuntimeError):
    """Local checkpoint persistence failed and the SQS message must remain."""

    failure_code = "OCR_PAGE_CHECKPOINT_PERSISTENCE_RETRYABLE"

    def __init__(self, *, stage: str, page_index: int | None = None) -> None:
        self.stage = stage
        self.page_index = page_index
        parts = [f"stage={stage}"]
        if page_index is not None:
            parts.append(f"page_index={page_index}")
        self.safe_message = " ".join(parts)[:512]
        super().__init__(self.safe_message)


class TranskribusLocalPersistenceRetryableError(RuntimeError):
    """Transient local snapshot/S3 persistence failure after durable recognition.

    Must not be persisted as an OCR failure or mark TranskribusRun FAILED.
    Worker must leave the SQS message unacknowledged (return False).
    """


class HtrEngineAdapter(Protocol):
    engine_key: str

    def execute(
        self,
        pages: List[PageImage],
        language_hint: str | None,
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult: ...
