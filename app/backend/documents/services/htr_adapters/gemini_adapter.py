from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from documents.services.gemini_engine import GeminiError, transcribe_pages_with_gemini
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL_CANDIDATES
from documents.services.htr_adapters.base import (
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.env_validation import WorkerEnvConfig


class GeminiAdapter:
    engine_key = "GEMINI"

    def execute(
        self,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        worker_env: Optional["WorkerEnvConfig"] = kwargs.pop("worker_env", None)
        kwargs.pop("document_id", None)

        model_candidates = kwargs.pop(
            "model_candidates",
            DEFAULT_GEMINI_MODEL_CANDIDATES,
        )
        if not model_candidates:
            raise EnginePermanentError("No Gemini model candidates configured.")

        if worker_env is not None:
            kwargs.setdefault("min_text_length", worker_env.min_text_length)
            kwargs.setdefault("double_pass", worker_env.gemini_double_pass)
            kwargs.setdefault(
                "consistency_min_ratio", worker_env.gemini_consistency_min_ratio
            )
            kwargs.setdefault("temperature", worker_env.gemini_temperature)
            kwargs.setdefault("top_k", worker_env.gemini_top_k)
            kwargs.setdefault("top_p", worker_env.gemini_top_p)
            max_tok = worker_env.gemini_max_output_tokens
            kwargs.setdefault(
                "max_output_tokens", max_tok if max_tok is not None else 8192
            )

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
                    for marker in [
                        "429",
                        "RESOURCE_EXHAUSTED",
                        "QUOTA_EXHAUSTED",
                        "QUOTA",
                    ]
                ):
                    continue
                raise EnginePermanentError(str(exc)) from exc
            except Exception as exc:
                raise EnginePermanentError(str(exc)) from exc

        raise EngineRetryableError(
            f"Gemini models exhausted: {[str(m) for m in model_candidates]}"
        ) from last_error
