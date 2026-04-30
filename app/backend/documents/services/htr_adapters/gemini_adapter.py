from __future__ import annotations

from typing import List, Optional

from documents.services.gemini_engine import GeminiError, transcribe_pages_with_gemini
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage


class GeminiAdapter:
    engine_key = "GEMINI"

    def execute(
        self,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        model_candidates = kwargs.pop(
            "model_candidates",
            ["gemini-2.0-flash", "gemini-1.5-flash"],
        )
        if not model_candidates:
            raise EnginePermanentError("No Gemini model candidates configured.")

        last_error: Exception | None = None
        for model_name in model_candidates:
            try:
                result = transcribe_pages_with_gemini(
                    pages=pages,
                    language_hint=language_hint,
                    prompt_variant=prompt_variant,
                    model_name=model_name,
                    **kwargs,
                )
                return HtrResult(
                    text=result.text,
                    needs_review=result.needs_review,
                    engine_name=result.engine_name,
                    review_reasons=list(result.review_reasons or []),
                )
            except GeminiError as exc:
                last_error = exc
                error_text = str(exc).upper()
                if any(
                    marker in error_text
                    for marker in ["429", "RESOURCE_EXHAUSTED", "QUOTA_EXHAUSTED", "QUOTA"]
                ):
                    continue
                raise EnginePermanentError(str(exc)) from exc
            except Exception as exc:
                raise EnginePermanentError(str(exc)) from exc

        raise EngineRetryableError(
            f"Gemini models exhausted: {[str(m) for m in model_candidates]}"
        ) from last_error
