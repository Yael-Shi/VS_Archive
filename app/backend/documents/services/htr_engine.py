from __future__ import annotations

from typing import Optional

from documents.services.htr_adapters.base import HtrResult
from documents.services.htr_adapters.registry import get_htr_adapter
from documents.services.ocr_routing import OcrRouteConfig, select_ocr_route
from documents.services.page_extraction import PageImage


def transcribe_pages(
    pages: list[PageImage],
    language_hint: Optional[str],
    text_input_type: Optional[str],
    *,
    route: Optional[OcrRouteConfig] = None,
    source_transkribus_run_id: int | None = None,
    **kwargs,
) -> HtrResult:
    """
    HTR entry point. kwargs allows flexible parameter passing from worker/env.

    When ``route`` is provided (e.g. from the worker), it must be the same route
    used for persistence; ``select_ocr_route`` is not called again.
    """
    selected = route if route is not None else select_ocr_route(
        language_hint, text_input_type
    )
    adapter = get_htr_adapter(selected.engine_key)
    if source_transkribus_run_id is not None:
        kwargs["source_transkribus_run_id"] = source_transkribus_run_id
    return adapter.execute(
        pages=pages,
        language_hint=language_hint,
        prompt_variant=selected.prompt_variant,
        **kwargs,
    )
