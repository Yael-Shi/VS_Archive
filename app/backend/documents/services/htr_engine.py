from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from documents.services.gemini_engine import transcribe_pages_with_gemini
from documents.services.page_extraction import PageImage


@dataclass(frozen=True)
class HtrResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini_2_0_flash"
    review_reasons: List[str] = field(default_factory=list)


def transcribe_pages(
    pages: List[PageImage],
    language_hint: Optional[str],
    **kwargs
) -> HtrResult:
    """
    HTR entry point. kwargs allows flexible parameter passing from worker/env.
    """
    r = transcribe_pages_with_gemini(
        pages=pages,
        language_hint=language_hint,
        **kwargs
    )
    return HtrResult(
        text=r.text,
        needs_review=r.needs_review,
        engine_name=r.engine_name,
        review_reasons=list(r.review_reasons or []),
    )
