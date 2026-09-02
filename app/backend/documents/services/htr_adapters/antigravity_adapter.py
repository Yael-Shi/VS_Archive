from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import TYPE_CHECKING, List, Optional, Sequence

from documents.models import Document
from documents.services.antigravity_engine import (
    AntigravityError,
    antigravity_outbound_image,
    transcribe_pages_with_antigravity,
)
from documents.services.arabic_printed_banded_document_ocr import (
    OUTCOME_COMPLETED,
    OUTCOME_PARTIAL,
    ArabicPrintedBandedDocumentResult,
    ArabicPrintedDocumentPageInput,
    process_arabic_printed_banded_document,
)
from documents.services.arabic_printed_banded_ocr import (
    OUTCOME_SUCCEEDED,
    ArabicPrintedBandedPageResult,
)
from documents.services.arabic_printed_page_checkpoints import (
    ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN,
    ArabicPrintedCheckpointBusyError,
    ArabicPrintedCheckpointPersistenceRetryableError,
    ArabicPrintedIdentityMismatchError,
    StaleArabicPrintedPageClaimError,
)
from documents.services.cloud_vision_document_text import (
    CloudVisionDocumentTextError,
    prepare_arabic_printed_working_image,
)
from documents.services.htr_adapters.base import (
    EnginePageCheckpointBusyError,
    EnginePageCheckpointPersistenceRetryableError,
    EnginePageIncompleteError,
    EnginePermanentError,
    HtrResult,
)
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.cloud_vision_document_text import ArabicPrintedWorkingImage
    from documents.services.env_validation import WorkerEnvConfig

logger = logging.getLogger(__name__)

_DISABLED_MESSAGE = (
    "Antigravity OCR is disabled. Set ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true "
    "to enable the Arabic printed Antigravity adapter."
)
_BANDED_VISION_KEY_MESSAGE = (
    "Arabic printed banded OCR requires GOOGLE_CLOUD_VISION_API_KEY when "
    "ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED=true."
)
_BANDED_DEADLINE_MESSAGE = (
    "Arabic printed banded OCR requires absolute_deadline_monotonic from the "
    "ProcessDocumentRequest execution lease."
)
_PAGE_INDEX_RE = re.compile(r"page_index=(\d+)")


class AntigravityAdapter:
    """
    Antigravity managed-agent OCR via the Gemini Interactions API.

    Gated by ``ENABLE_ANTIGRAVITY_ARABIC_PRINTED``. Banded Cloud Vision
    execution is a second worker-only gate,
    ``ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED`` (default off). When that
    flag is false, the existing whole-document JSON Interactions path is
    unchanged.
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
        absolute_deadline_monotonic = kwargs.pop(
            "absolute_deadline_monotonic", None
        )

        if worker_env is None:
            raise EnginePermanentError(
                "AntigravityAdapter requires worker_env (supplied by run_worker)."
            )

        if not worker_env.enable_antigravity_arabic_printed:
            raise EnginePermanentError(_DISABLED_MESSAGE)

        if worker_env.enable_antigravity_arabic_printed_banded:
            return self._execute_banded(
                pages=pages,
                language_hint=language_hint,
                prompt_variant=prompt_variant,
                worker_env=worker_env,
                document_id=document_id,
                absolute_deadline_monotonic=absolute_deadline_monotonic,
                kwargs=kwargs,
            )

        for page in pages:
            outbound_bytes, outbound_mime = antigravity_outbound_image(page)
            logger.info(
                "Antigravity input page document_id=%s page_index=%s "
                "outbound_mime_type=%s outbound_byte_length=%s outbound_sha256=%s",
                document_id,
                page.page_index,
                outbound_mime,
                len(outbound_bytes),
                hashlib.sha256(outbound_bytes).hexdigest()[:16],
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

    def _execute_banded(
        self,
        *,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        worker_env: "WorkerEnvConfig",
        document_id: object,
        absolute_deadline_monotonic: object,
        kwargs: dict,
    ) -> HtrResult:
        vision_key = (worker_env.google_cloud_vision_api_key or "").strip()
        if not vision_key:
            raise EnginePermanentError(_BANDED_VISION_KEY_MESSAGE)
        if document_id is None:
            raise EnginePermanentError(
                "Arabic printed banded OCR requires document_id."
            )
        if not language_hint:
            raise EnginePermanentError(
                "Arabic printed banded OCR requires a language hint."
            )
        if type(absolute_deadline_monotonic) is not int and type(
            absolute_deadline_monotonic
        ) is not float:
            raise EnginePermanentError(_BANDED_DEADLINE_MESSAGE)

        text_input_type = kwargs.get("text_input_type") or (
            Document.TextInputType.PRINTED
        )
        engine_key = kwargs.get("engine_key") or self.engine_key
        ordered = _contiguous_one_based_pages(pages)
        working_by_index = _prepare_banded_working_images(ordered)
        page_inputs = [
            _banded_page_input(page, working_by_index[page.page_index - 1])
            for page in ordered
        ]

        def load_working_image(page_index: int) -> "ArabicPrintedWorkingImage":
            return working_by_index[page_index]

        try:
            result = process_arabic_printed_banded_document(
                document_id=int(document_id),
                pages=page_inputs,
                load_working_image=load_working_image,
                gemini_api_key=worker_env.gemini_api_key,
                cloud_vision_api_key=vision_key,
                absolute_deadline_monotonic=float(absolute_deadline_monotonic),
                language_hint=str(language_hint),
                text_input_type=str(text_input_type),
                engine_key=str(engine_key),
                prompt_variant=prompt_variant or "printed",
            )
        except ArabicPrintedCheckpointBusyError as exc:
            raise EnginePageCheckpointBusyError(
                _page_index_from_error(exc, default=0)
            ) from exc
        except ArabicPrintedCheckpointPersistenceRetryableError as exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage=exc.stage,
                page_index=exc.page_index,
            ) from exc
        except ArabicPrintedIdentityMismatchError as exc:
            raise EnginePermanentError(
                "Arabic printed banded OCR identity mismatch"
            ) from exc
        except StaleArabicPrintedPageClaimError as exc:
            raise EnginePermanentError(
                "Arabic printed banded OCR stale page lease"
            ) from exc

        return _htr_result_from_banded_document(result)


def _contiguous_one_based_pages(pages: Sequence[PageImage]) -> list[PageImage]:
    if not pages:
        raise EnginePermanentError("Arabic printed banded OCR requires at least one page.")
    ordered = sorted(pages, key=lambda page: page.page_index)
    expected = list(range(1, len(ordered) + 1))
    actual = [page.page_index for page in ordered]
    if actual != expected:
        raise EnginePermanentError(
            "Arabic printed banded OCR requires contiguous 1-based page indexes."
        )
    return ordered


def _banded_source_bytes(page: PageImage) -> bytes:
    if page.original_image_bytes:
        return page.original_image_bytes
    return page.image_bytes


def _prepare_banded_working_images(
    pages: Sequence[PageImage],
) -> dict[int, "ArabicPrintedWorkingImage"]:
    prepared: dict[int, "ArabicPrintedWorkingImage"] = {}
    for page in pages:
        zero_based = page.page_index - 1
        try:
            prepared[zero_based] = prepare_arabic_printed_working_image(
                _banded_source_bytes(page)
            )
        except CloudVisionDocumentTextError as exc:
            raise EnginePermanentError(
                "Arabic printed banded OCR working-image preparation failed"
            ) from exc
    return prepared


def _banded_page_input(
    page: PageImage,
    working_image: "ArabicPrintedWorkingImage",
) -> ArabicPrintedDocumentPageInput:
    return ArabicPrintedDocumentPageInput(
        page_index=page.page_index - 1,
        mime_type=working_image.mime_type,
        source_identity=page.source_identity,
        source_content_fingerprint=page.source_content_fingerprint,
        oriented_image_sha256=working_image.sha256,
        oriented_image_width=working_image.width,
        oriented_image_height=working_image.height,
    )


def _page_index_from_error(exc: BaseException, *, default: int) -> int:
    page_index = getattr(exc, "page_index", None)
    if isinstance(page_index, int):
        return page_index
    match = _PAGE_INDEX_RE.search(str(exc))
    return int(match.group(1)) if match else default


def _banded_runtime_engine_name(
    pages: Sequence[ArabicPrintedBandedPageResult],
) -> str:
    markers = tuple(
        page.runtime_engine_marker
        for page in sorted(pages, key=lambda page: page.page_index)
        if page.outcome == OUTCOME_SUCCEEDED and page.runtime_engine_marker
    )
    if not markers:
        raise EnginePermanentError(
            "Arabic printed banded OCR completed without a runtime engine marker"
        )
    if len(set(markers)) == 1:
        marker = markers[0]
    else:
        digest = hashlib.sha256(
            json.dumps(list(markers), ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()[:ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN]
        marker = f"antigravity-banded:mixed:{digest}"
    if len(marker) > 64:
        raise EnginePermanentError(
            "Arabic printed banded runtime engine marker exceeds 64 characters"
        )
    return marker


def _htr_result_from_banded_document(
    result: ArabicPrintedBandedDocumentResult,
) -> HtrResult:
    if result.outcome == OUTCOME_PARTIAL or result.missing_page_indices:
        raise EnginePageIncompleteError(
            list(result.missing_page_indices),
            failure_code=result.failure_code or "OCR_PAGES_INCOMPLETE",
        )
    if result.outcome != OUTCOME_COMPLETED:
        raise EnginePermanentError(
            f"Arabic printed banded OCR unexpected outcome={result.outcome}"
        )
    ordered = tuple(sorted(result.pages, key=lambda page: page.page_index))
    text = "\n\n".join(page.assembled_text.strip() for page in ordered).strip()
    if not text:
        raise EnginePermanentError(
            "Arabic printed banded OCR completed with empty assembled text"
        )
    return HtrResult(
        text=text,
        needs_review=True,
        engine_name=_banded_runtime_engine_name(ordered),
    )
