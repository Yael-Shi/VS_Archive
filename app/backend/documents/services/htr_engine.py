from __future__ import annotations

from typing import Optional

from documents.services.htr_adapters.base import HtrResult
from documents.services.htr_adapters.registry import get_htr_adapter
from documents.services.ocr_routing import select_ocr_route
from documents.services.page_extraction import PageImage


def transcribe_pages(
    pages: list[PageImage],
    language_hint: Optional[str],
    text_input_type: Optional[str],
    **kwargs
) -> HtrResult:
    """
    HTR entry point. kwargs allows flexible parameter passing from worker/env.
    """
    route = select_ocr_route(language_hint, text_input_type)
    adapter = get_htr_adapter(route.engine_key)
    return adapter.execute(
        pages=pages,
        language_hint=language_hint,
        prompt_variant=route.prompt_variant,
        **kwargs,
    )
