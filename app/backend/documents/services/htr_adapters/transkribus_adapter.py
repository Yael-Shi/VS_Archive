from __future__ import annotations

from typing import List, Optional

from documents.services.htr_adapters.base import EnginePermanentError, HtrResult
from documents.services.page_extraction import PageImage


class TranskribusAdapter:
    """
    Transkribus OCR/HTR adapter. PR #1: registry skeleton only (no live API).
    """

    engine_key = "TRANSKRIBUS"

    def execute(
        self,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        raise EnginePermanentError("Transkribus OCR/HTR is not implemented yet")
