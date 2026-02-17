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
    *,
    model_name: str = "gemini-2.5-flash",
    min_text_length: int = 20,
    double_pass: bool = True,
    consistency_min_ratio: float = 0.92,
    temperature: float = 0.0,
    top_k: int = 1,
    top_p: float = 0.2,
    max_output_tokens: Optional[int] = None,
) -> HtrResult:
    """
    V2: Gemini-only OCR/HTR for page images (PNG bytes).

    Hardening params are passed through to gemini_engine so they can be configured
    by the worker (env-driven) without editing code each time.
    """
    r = transcribe_pages_with_gemini(
        pages=pages,
        language_hint=language_hint,
        model_name=model_name,
        min_text_length=min_text_length,
        double_pass=double_pass,
        consistency_min_ratio=consistency_min_ratio,
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
    )
    return HtrResult(
        text=r.text,
        needs_review=r.needs_review,
        engine_name=r.engine_name,
    )
