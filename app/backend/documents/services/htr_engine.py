from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from documents.models import DocumentTextResult
from documents.services.gemini_engine import transcribe_pages_with_gemini
from documents.services.ocr_routing import select_ocr_route
from documents.services.page_extraction import PageImage


@dataclass(frozen=True)
class HtrResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini_2_0_flash"
    engine_key: str = DocumentTextResult.OcrEngineKey.GEMINI
    prompt_variant: str = DocumentTextResult.OcrPromptVariant.HANDWRITTEN
    review_reasons: List[str] = field(default_factory=list)


def transcribe_pages(
    pages: List[PageImage],
    language_hint: Optional[str],
    text_input_type: Optional[str],
    **kwargs
) -> HtrResult:
    """
    HTR entry point. kwargs allows flexible parameter passing from worker/env.
    """
    route = select_ocr_route(language_hint, text_input_type)
    if route.engine_key != DocumentTextResult.OcrEngineKey.GEMINI:
        raise RuntimeError(f"OCR engine is not implemented yet: {route.engine_key}")

    r = transcribe_pages_with_gemini(
        pages=pages,
        language_hint=language_hint,
        prompt_variant=route.prompt_variant,
        **kwargs
    )
    return HtrResult(
        text=r.text,
        needs_review=r.needs_review,
        engine_name=r.engine_name,
        engine_key=route.engine_key,
        prompt_variant=route.prompt_variant,
        review_reasons=list(r.review_reasons or []),
    )
