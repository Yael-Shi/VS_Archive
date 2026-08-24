from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, List, Optional

from documents.services.antigravity_engine import (
    AntigravityError,
    transcribe_pages_with_antigravity,
)
from documents.services.htr_adapters.base import EnginePermanentError, HtrResult
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.env_validation import WorkerEnvConfig

logger = logging.getLogger(__name__)

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
        document_id = kwargs.pop("document_id", None)

        if worker_env is None:
            raise EnginePermanentError(
                "AntigravityAdapter requires worker_env (supplied by run_worker)."
            )

        if not worker_env.enable_antigravity_arabic_printed:
            raise EnginePermanentError(_DISABLED_MESSAGE)

        for page in pages:
            logger.info(
                "Antigravity input page document_id=%s page_index=%s "
                "mime_type=%s bytes=%s sha256=%s",
                document_id,
                page.page_index,
                page.mime_type,
                len(page.image_bytes),
                hashlib.sha256(page.image_bytes).hexdigest()[:16],
            )

        logger.info(
            "Antigravity input summary document_id=%s pages=%s",
            document_id,
            len(pages),
        )

        try:
            result = transcribe_pages_with_antigravity(
                pages,
                api_key=worker_env.gemini_api_key,
                agent_id=worker_env.antigravity_agent_id,
                document_id=document_id,
                **kwargs,
            )
        except AntigravityError as exc:
            raise EnginePermanentError(str(exc)) from exc

        return HtrResult(
            text=result.text,
            needs_review=result.needs_review,
            engine_name=result.engine_name,
        )
