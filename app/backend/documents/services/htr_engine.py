from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from documents.models import DocumentTextResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.htr_adapters.registry import get_htr_adapter
from documents.services.ocr_routing import (
    OcrRouteConfig,
    gemini_model_candidates,
    select_ocr_route,
)
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.env_validation import WorkerEnvConfig


def transcribe_pages(
    pages: list[PageImage],
    language_hint: Optional[str],
    text_input_type: Optional[str],
    *,
    handwriting_type: Optional[str] = None,
    route: Optional[OcrRouteConfig] = None,
    source_transkribus_run_id: int | None = None,
    **kwargs,
) -> HtrResult:
    """
    HTR entry point. kwargs allows flexible parameter passing from worker/env.

    When ``route`` is provided (e.g. from the worker), it must be the same route
    used for persistence; ``select_ocr_route`` is not called again.

    Optional ``absolute_deadline_monotonic`` is a generic remaining
    request/lease deadline in ``time.monotonic()`` units. Adapters that do
    not use it ignore it.
    """
    selected = (
        route
        if route is not None
        else select_ocr_route(
            language_hint,
            text_input_type,
            handwriting_type=handwriting_type,
        )
    )
    adapter = get_htr_adapter(selected.engine_key)
    if source_transkribus_run_id is not None:
        kwargs["source_transkribus_run_id"] = source_transkribus_run_id
    worker_env: Optional["WorkerEnvConfig"] = kwargs.get("worker_env")
    if (
        selected.engine_key == DocumentTextResult.OcrEngineKey.GEMINI
        and worker_env is not None
    ):
        if "model_candidates" not in kwargs:
            kwargs["model_candidates"] = list(
                gemini_model_candidates(
                    selected,
                    language=language_hint,
                    text_input_type=text_input_type,
                    gemini_hebrew_printed_model=worker_env.gemini_hebrew_printed_model,
                )
            )
        kwargs.setdefault("text_input_type", text_input_type)
        kwargs.setdefault("handwriting_type", handwriting_type)
        kwargs.setdefault("engine_key", selected.engine_key)
    return adapter.execute(
        pages=pages,
        language_hint=language_hint,
        prompt_variant=selected.prompt_variant,
        **kwargs,
    )
