from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from documents.services.gemini_engine import transcribe_pages_with_gemini
from documents.services.page_extraction import PageImage


class HtrNotImplementedError(RuntimeError):
    pass


@dataclass(frozen=True)
class HtrResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini_2_5_flash"


def transcribe_pages(
    pages: List[PageImage],
    language_hint: Optional[str],
) -> HtrResult:
    """
    V2: Gemini-only OCR/HTR for page images (PNG bytes).
    """
    r = transcribe_pages_with_gemini(
        pages=pages,
        language_hint=language_hint,
        model_name="gemini-1.5-flash",
    )
    return HtrResult(
        text=r.text,
        needs_review=r.needs_review,
        engine_name=r.engine_name,
    )
