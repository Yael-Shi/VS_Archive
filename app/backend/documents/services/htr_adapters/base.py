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


class UnsupportedEngineError(RuntimeError):
    def __init__(self, engine_key: str):
        super().__init__(f"OCR engine is not implemented yet: {engine_key}")
        self.engine_key = engine_key


class EngineRetryableError(RuntimeError):
    pass


class EnginePermanentError(RuntimeError):
    pass


class HtrEngineAdapter(Protocol):
    engine_key: str

    def execute(
        self,
        pages: List[PageImage],
        language_hint: str | None,
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        ...
