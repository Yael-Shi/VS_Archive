from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from documents.services.antigravity_engine import (
    AntigravityError,
    transcribe_pages_with_antigravity,
)
from documents.services.htr_adapters.base import EnginePermanentError, HtrResult
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.env_validation import WorkerEnvConfig

_DISABLED_MESSAGE = (
    "Antigravity OCR is disabled. Set ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true "
    "to enable the Arabic printed Antigravity adapter."
)


class AntigravityAdapter:
    """
    Antigravity managed-agent OCR via the Gemini Interactions API.

    Gated by ``ENABLE_ANTIGRAVITY_ARABIC_PRINTED``; not wired into production
    OCR routing until explicitly enabled in a follow-up change.
    """

    engine_key = "ANTIGRAVITY"

    def execute(
        self,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        **kwargs,
    ) -> HtrResult:
        worker_env: Optional["WorkerEnvConfig"] = kwargs.pop("worker_env", None)
        kwargs.pop("document_id", None)

        if worker_env is None:
            raise EnginePermanentError(
                "AntigravityAdapter requires worker_env (supplied by run_worker)."
            )

        if not worker_env.enable_antigravity_arabic_printed:
            raise EnginePermanentError(_DISABLED_MESSAGE)

        try:
            result = transcribe_pages_with_antigravity(
                pages,
                api_key=worker_env.gemini_api_key,
                agent_id=worker_env.antigravity_agent_id,
                **kwargs,
            )
        except AntigravityError as exc:
            raise EnginePermanentError(str(exc)) from exc

        return HtrResult(
            text=result.text,
            needs_review=result.needs_review,
            engine_name=result.engine_name,
        )
