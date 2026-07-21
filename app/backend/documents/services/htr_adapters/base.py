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
