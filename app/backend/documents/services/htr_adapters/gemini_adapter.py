from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, List, Optional

from django.db import DatabaseError

from documents.models import Document
from documents.services.gemini_defaults import (
    DEFAULT_GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP,
    DEFAULT_GEMINI_TEMPERATURE,
    DEFAULT_GEMINI_TOP_K,
    DEFAULT_GEMINI_TOP_P,
)
from documents.services.gemini_engine import (
    GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
    GeminiApiError,
    GeminiError,
    GeminiQuotaError,
    GeminiResponseError,
    GeminiResponseFailureCode,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL_CANDIDATES
from documents.services.gemini_page_checkpoints import (
    GeminiPageClaimAction,
    StaleGeminiPageClaimError,
    assemble_gemini_attempt,
    build_gemini_attempt_identity,
    claim_gemini_page,
    get_or_create_gemini_attempt,
    missing_pages_for_attempt,
    persist_gemini_page_failure,
    persist_gemini_page_success,
)
from documents.services.htr_adapters.base import (
    EnginePageCheckpointBusyError,
    EnginePageCheckpointPersistenceRetryableError,
    EnginePageIncompleteError,
    EnginePermanentError,
    EngineRetryableError,
    HtrResult,
)
from documents.services.page_extraction import PageImage

if TYPE_CHECKING:
    from documents.services.env_validation import WorkerEnvConfig


logger = logging.getLogger(__name__)

_QUOTA_ERROR_MARKERS = (
    "429",
    "RESOURCE_EXHAUSTED",
    "QUOTA_EXHAUSTED",
    "QUOTA",
)


def _is_quota_error(exc: GeminiError) -> bool:
    error_text = str(exc).upper()
    return isinstance(exc, GeminiQuotaError) or any(
        marker in error_text for marker in _QUOTA_ERROR_MARKERS
    )


def _provider_calls_used(
    exc: GeminiError,
    *,
    provider_call_offset: int,
) -> int:
    if isinstance(exc, GeminiResponseError):
        return max(1, exc.metadata.attempt - provider_call_offset)
    return max(1, int(getattr(exc, "provider_calls_used", 1)))


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
        document_id = kwargs.pop("document_id", None)
        text_input_type = kwargs.pop("text_input_type", None)
        handwriting_type = kwargs.pop("handwriting_type", None)
        engine_key = kwargs.pop("engine_key", self.engine_key)
        kwargs.pop("absolute_deadline_monotonic", None)
        # English handwriting retains its RECITATION-only candidate switch.
        # Hebrew GENERAL handwriting gets a separate cost-aware policy: one
        # primary 2.5 Flash call, then 3.6 Flash only for MAX_TOKENS or
        # RECITATION. Hebrew VS handwriting never reaches this Gemini route.
        recitation_model_fallback_enabled = (
            language_hint == Document.Language.ENGLISH
            and text_input_type == Document.TextInputType.HANDWRITTEN
        )
        hebrew_general_model_fallback_enabled = (
            language_hint == Document.Language.HEBREW
            and text_input_type == Document.TextInputType.HANDWRITTEN
            and handwriting_type == Document.HandwritingType.GENERAL
        )

        model_candidates = kwargs.pop(
            "model_candidates",
            DEFAULT_GEMINI_MODEL_CANDIDATES,
        )
        model_candidates = [str(model).strip() for model in model_candidates]
        if not model_candidates or any(not model for model in model_candidates):
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
            kwargs.setdefault(
                "max_output_tokens",
                worker_env.gemini_ocr_max_output_tokens,
            )
            kwargs.setdefault(
                "max_output_tokens_hard_cap",
                worker_env.gemini_max_output_tokens_hard_cap,
            )

        if document_id is None:
            return self._execute_without_checkpoints(
                pages=pages,
                language_hint=language_hint,
                prompt_variant=prompt_variant,
                model_candidates=model_candidates,
                kwargs=kwargs,
            )

        temperature = kwargs.get("temperature", DEFAULT_GEMINI_TEMPERATURE)
        contract = gemini_transcription_contract(
            prompt_variant=prompt_variant,
            language_hint=language_hint,
            temperature=temperature,
        )
        identity = build_gemini_attempt_identity(
            pages=pages,
            language_hint=language_hint,
            text_input_type=text_input_type,
            handwriting_type=handwriting_type,
            engine_key=engine_key,
            prompt_variant=prompt_variant,
            model_candidates=model_candidates,
            contract=contract,
            min_text_length=kwargs.get("min_text_length", 20),
            double_pass=kwargs.get("double_pass", False),
            consistency_min_ratio=kwargs.get("consistency_min_ratio", 0.85),
            temperature=temperature,
            top_k=kwargs.get("top_k", DEFAULT_GEMINI_TOP_K),
            top_p=kwargs.get("top_p", DEFAULT_GEMINI_TOP_P),
            max_output_tokens=kwargs.get("max_output_tokens", 8192),
            max_output_tokens_hard_cap=kwargs.get(
                "max_output_tokens_hard_cap",
                DEFAULT_GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP,
            ),
        )
        try:
            attempt = get_or_create_gemini_attempt(
                document_id=document_id,
                identity=identity,
            )
        except DatabaseError as exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage="attempt",
            ) from exc

        pages_by_index = {page.page_index: page for page in pages}
        for page_index in range(1, identity.expected_page_count + 1):
            page = pages_by_index[page_index]
            try:
                claim = claim_gemini_page(
                    attempt_id=attempt.id,
                    page_index=page_index,
                    page_fingerprint=identity.page_fingerprints[page_index],
                    source_content_fingerprint=(
                        identity.source_content_fingerprints[page_index]
                    ),
                )
            except DatabaseError as exc:
                raise EnginePageCheckpointPersistenceRetryableError(
                    stage="claim",
                    page_index=page_index,
                ) from exc
            if claim.action == GeminiPageClaimAction.REUSE:
                continue
            if claim.action == GeminiPageClaimAction.BUSY:
                raise EnginePageCheckpointBusyError(page_index)
            assert claim.lease_token is not None

            self._execute_claimed_page(
                page=page,
                language_hint=language_hint,
                prompt_variant=prompt_variant,
                model_candidates=model_candidates,
                recitation_model_fallback_enabled=(recitation_model_fallback_enabled),
                hebrew_general_model_fallback_enabled=(
                    hebrew_general_model_fallback_enabled
                ),
                kwargs=kwargs,
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                attempt_id=attempt.id,
            )

        try:
            assembled = assemble_gemini_attempt(attempt_id=attempt.id)
        except DatabaseError as exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage="assembly",
            ) from exc
        if assembled is None:
            raise EnginePageIncompleteError(
                self._missing_pages_or_retryable(attempt.id),
                failure_code="GEMINI_PAGES_INCOMPLETE",
            )
        return HtrResult(
            text=assembled.text,
            needs_review=assembled.needs_review,
            engine_name=assembled.engine_name,
            review_reasons=assembled.review_reasons,
        )

    def _execute_claimed_page(
        self,
        *,
        page: PageImage,
        language_hint: Optional[str],
        prompt_variant: str,
        model_candidates: List[str],
        recitation_model_fallback_enabled: bool,
        hebrew_general_model_fallback_enabled: bool,
        kwargs: dict[str, Any],
        checkpoint_id: int,
        lease_token: uuid.UUID,
        attempt_id: int,
    ) -> None:
        last_error: Exception | None = None
        remaining_provider_calls = GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS
        next_max_output_tokens = kwargs.get("max_output_tokens", 8192)

        for model_index, model_name in enumerate(model_candidates):
            if remaining_provider_calls <= 0:
                break

            provider_call_offset = (
                GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS - remaining_provider_calls
            )
            model_kwargs = dict(kwargs)
            model_kwargs["max_output_tokens"] = next_max_output_tokens
            if hebrew_general_model_fallback_enabled and model_index == 0:
                # Do not spend the 2.5 Flash budget on the runaway
                # 4096 -> 8192 -> 16384 ladder. Preserve two calls for 3.6.
                model_kwargs["max_provider_calls"] = 1
            else:
                model_kwargs["max_provider_calls"] = remaining_provider_calls
            model_kwargs["provider_call_offset"] = provider_call_offset

            result = None
            try:
                result = transcribe_pages_with_gemini(
                    pages=[page],
                    language_hint=language_hint,
                    prompt_variant=prompt_variant,
                    model_name=model_name,
                    **model_kwargs,
                )
            except GeminiError as exc:
                last_error = exc
                calls_used = _provider_calls_used(
                    exc,
                    provider_call_offset=provider_call_offset,
                )
                remaining_provider_calls = max(
                    0,
                    remaining_provider_calls - calls_used,
                )
                has_next_model = model_index + 1 < len(model_candidates)

                if _is_quota_error(exc):
                    if (
                        has_next_model
                        and hebrew_general_model_fallback_enabled
                        and remaining_provider_calls > 0
                    ):
                        # Keep calls already spent by the primary 2.5 model.
                        # Gemini 3.6 receives only the remainder of the shared
                        # three-call page budget.
                        # Keep the current output cap when advancing.
                        continue

                    if (
                        has_next_model
                        and not recitation_model_fallback_enabled
                        and not hebrew_general_model_fallback_enabled
                    ):
                        remaining_provider_calls = GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS
                        next_max_output_tokens = kwargs.get(
                            "max_output_tokens",
                            8192,
                        )
                    if has_next_model and remaining_provider_calls > 0:
                        continue
                    break

                response_failure_code = (
                    exc.failure_code if isinstance(exc, GeminiResponseError) else None
                )
                use_recitation_fallback = (
                    recitation_model_fallback_enabled
                    and response_failure_code == GeminiResponseFailureCode.RECITATION
                )
                use_hebrew_general_fallback = (
                    hebrew_general_model_fallback_enabled
                    and response_failure_code
                    in (
                        GeminiResponseFailureCode.MAX_TOKENS,
                        GeminiResponseFailureCode.RECITATION,
                    )
                )

                if (
                    (use_recitation_fallback or use_hebrew_general_fallback)
                    and has_next_model
                    and remaining_provider_calls > 0
                ):
                    assert isinstance(exc, GeminiResponseError)
                    next_model = model_candidates[model_index + 1]
                    next_max_output_tokens = exc.metadata.max_output_tokens
                    logger.warning(
                        "Retrying Gemini transcription after %s with fallback "
                        "model: page=%s model=%s -> %s provider_calls_used=%s "
                        "remaining_provider_calls=%s max_output_tokens=%s",
                        exc.failure_code.value,
                        page.page_index,
                        model_name,
                        next_model,
                        calls_used,
                        remaining_provider_calls,
                        next_max_output_tokens,
                    )
                    continue

                self._persist_page_failure(
                    checkpoint_id=checkpoint_id,
                    lease_token=lease_token,
                    exc=exc,
                    page_index=page.page_index,
                )
                self._raise_incomplete(attempt_id)
            except Exception as exc:
                self._persist_page_failure(
                    checkpoint_id=checkpoint_id,
                    lease_token=lease_token,
                    exc=exc,
                    page_index=page.page_index,
                )
                self._raise_incomplete(attempt_id)

            assert result is not None
            try:
                persist_gemini_page_success(
                    checkpoint_id=checkpoint_id,
                    lease_token=lease_token,
                    actual_model=result.engine_name,
                    text=result.text,
                    needs_review=result.needs_review,
                    review_reasons=list(result.review_reasons or []),
                )
            except StaleGeminiPageClaimError as exc:
                raise EnginePageCheckpointBusyError(page.page_index) from exc
            except ValueError as exc:
                self._persist_page_failure(
                    checkpoint_id=checkpoint_id,
                    lease_token=lease_token,
                    exc=exc,
                    page_index=page.page_index,
                )
                self._raise_incomplete(attempt_id)
            except DatabaseError as exc:
                raise EnginePageCheckpointPersistenceRetryableError(
                    stage="success",
                    page_index=page.page_index,
                ) from exc
            return

        try:
            persist_gemini_page_failure(
                checkpoint_id=checkpoint_id,
                lease_token=lease_token,
                failure_code="GEMINI_MODELS_EXHAUSTED",
                failure_message=(
                    "model_candidates_exhausted="
                    f"{len(model_candidates)} exception_class="
                    f"{type(last_error).__name__ if last_error else 'None'}"
                ),
            )
        except StaleGeminiPageClaimError as exc:
            raise EnginePageCheckpointBusyError(page.page_index) from exc
        except DatabaseError as exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage="failure",
                page_index=page.page_index,
            ) from exc
        self._raise_incomplete(attempt_id)

    def _persist_page_failure(
        self,
        *,
        checkpoint_id: int,
        lease_token: uuid.UUID,
        exc: Exception,
        page_index: int,
    ) -> None:
        if isinstance(exc, GeminiResponseError):
            failure_code = exc.failure_code.value
            failure_message = str(exc)
        elif isinstance(exc, GeminiApiError):
            failure_code = exc.failure_code.value
            failure_message = str(exc)
        elif isinstance(exc, GeminiError):
            failure_code = "GEMINI_ERROR"
            failure_message = f"exception_class={type(exc).__name__}"
        else:
            failure_code = "API_ERROR"
            failure_message = f"exception_class={type(exc).__name__}"
        try:
            persist_gemini_page_failure(
                checkpoint_id=checkpoint_id,
                lease_token=lease_token,
                failure_code=failure_code,
                failure_message=failure_message,
            )
        except StaleGeminiPageClaimError as stale_exc:
            raise EnginePageCheckpointBusyError(page_index) from stale_exc
        except DatabaseError as db_exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage="failure",
                page_index=page_index,
            ) from db_exc

    def _raise_incomplete(self, attempt_id: int) -> None:
        raise EnginePageIncompleteError(
            self._missing_pages_or_retryable(attempt_id),
            failure_code="GEMINI_PAGES_INCOMPLETE",
        )

    def _missing_pages_or_retryable(self, attempt_id: int) -> list[int]:
        try:
            return missing_pages_for_attempt(attempt_id)
        except DatabaseError as exc:
            raise EnginePageCheckpointPersistenceRetryableError(
                stage="missing_pages",
            ) from exc

    def _execute_without_checkpoints(
        self,
        *,
        pages: List[PageImage],
        language_hint: Optional[str],
        prompt_variant: str,
        model_candidates: List[str],
        kwargs: dict[str, Any],
    ) -> HtrResult:
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
