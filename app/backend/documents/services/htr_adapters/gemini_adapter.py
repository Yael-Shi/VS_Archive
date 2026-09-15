from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, List, Optional

from django.db import DatabaseError

from documents.models import Document, DocumentTextResult
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
    GeminiResult,
    gemini_transcription_contract,
    transcribe_pages_with_gemini,
)
from documents.services.gemini_models import (
    DEFAULT_GEMINI_MODEL_CANDIDATES,
    LATIN_PRINTED_GEMINI_MODEL,
)
from documents.services.gemini_hebrew_printed_crop_recovery import (
    REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY,
    crop_assembly_engine_name,
    hebrew_printed_recitation_crop_recovery_policy,
    merge_overlapping_crop_texts,
    plan_hebrew_printed_recitation_crops,
)
from documents.services.gemini_hebrew_printed_mixed_script import (
    MAX_LATIN_REGION_PROVIDER_CALLS,
    MixedScriptPlanDecision,
    MixedScriptRegionBox,
    MixedScriptRegionProvenance,
    REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
    ScriptDominance,
    evaluate_script_dominance,
    hebrew_printed_mixed_script_region_fallback_policy,
    hebrew_text_is_reusable_for_mixed_script,
    mixed_script_assembly_engine_name,
    plan_structural_candidate_region,
)
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
        execution_identity = kwargs.pop("execution_identity", None)
        kwargs.pop("absolute_deadline_monotonic", None)
        kwargs.pop("source_transkribus_run_id", None)
        # English handwriting and Hebrew printed share the RECITATION-only
        # candidate switch: the model that returned RECITATION is not called
        # again for that reason; remaining page budget goes to the next
        # candidate. Hebrew printed checkpoint-backed OCR may then split the
        # page into two overlapping horizontal crops after that full-page
        # chain is exhausted. Hebrew GENERAL handwriting gets a separate
        # cost-aware policy: one primary 2.5 Flash call, then 3.6 Flash only
        # for MAX_TOKENS or RECITATION. Hebrew VS handwriting never reaches
        # this Gemini route.
        recitation_model_fallback_enabled = (
            language_hint == Document.Language.ENGLISH
            and text_input_type == Document.TextInputType.HANDWRITTEN
        ) or (
            language_hint == Document.Language.HEBREW
            and text_input_type == Document.TextInputType.PRINTED
        )
        hebrew_printed_crop_recovery_enabled = bool(
            hebrew_printed_recitation_crop_recovery_policy(
                language_hint=language_hint,
                text_input_type=text_input_type,
            )
        )
        hebrew_printed_mixed_script_enabled = bool(
            hebrew_printed_mixed_script_region_fallback_policy(
                language_hint=language_hint,
                text_input_type=text_input_type,
            )
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
                    execution_identity=execution_identity,
                )
            except StaleGeminiPageClaimError as exc:
                raise EnginePermanentError(
                    "Gemini OCR stale process-document execution cannot claim a page"
                ) from exc
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
                hebrew_printed_crop_recovery_enabled=(
                    hebrew_printed_crop_recovery_enabled
                ),
                hebrew_printed_mixed_script_enabled=(
                    hebrew_printed_mixed_script_enabled
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
        hebrew_printed_crop_recovery_enabled: bool = False,
        hebrew_printed_mixed_script_enabled: bool = False,
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

                if (
                    hebrew_printed_crop_recovery_enabled
                    and response_failure_code == GeminiResponseFailureCode.RECITATION
                    and not has_next_model
                ):
                    recovered = self._recover_hebrew_printed_recitation_crops(
                        page=page,
                        language_hint=language_hint,
                        prompt_variant=prompt_variant,
                        model_candidates=model_candidates,
                        kwargs=kwargs,
                        checkpoint_id=checkpoint_id,
                        lease_token=lease_token,
                        attempt_id=attempt_id,
                        hebrew_printed_mixed_script_enabled=(
                            hebrew_printed_mixed_script_enabled
                        ),
                    )
                    if recovered is not None:
                        try:
                            persist_gemini_page_success(
                                checkpoint_id=checkpoint_id,
                                lease_token=lease_token,
                                actual_model=recovered.engine_name,
                                text=recovered.text,
                                needs_review=recovered.needs_review,
                                review_reasons=list(recovered.review_reasons or []),
                            )
                        except StaleGeminiPageClaimError as stale_exc:
                            raise EnginePageCheckpointBusyError(
                                page.page_index
                            ) from stale_exc
                        except ValueError as persist_exc:
                            self._persist_page_failure(
                                checkpoint_id=checkpoint_id,
                                lease_token=lease_token,
                                exc=persist_exc,
                                page_index=page.page_index,
                            )
                            self._raise_incomplete(attempt_id)
                        except DatabaseError as db_exc:
                            raise EnginePageCheckpointPersistenceRetryableError(
                                stage="success",
                                page_index=page.page_index,
                            ) from db_exc
                        return

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

    def _recover_hebrew_printed_recitation_crops(
        self,
        *,
        page: PageImage,
        language_hint: Optional[str],
        prompt_variant: str,
        model_candidates: List[str],
        kwargs: dict[str, Any],
        checkpoint_id: int,
        lease_token: uuid.UUID,
        attempt_id: int,
        hebrew_printed_mixed_script_enabled: bool = False,
    ) -> GeminiResult | None:
        plan = plan_hebrew_printed_recitation_crops(page)
        if plan is None:
            logger.warning(
                "Hebrew printed RECITATION crop recovery skipped; full-page "
                "RECITATION stands: page=%s",
                page.page_index,
            )
            return None

        logger.warning(
            "Starting Hebrew printed RECITATION crop recovery: page=%s crop_count=%s",
            page.page_index,
            len(plan.crops),
        )
        crop_kwargs = dict(kwargs)
        crop_kwargs.pop("max_provider_calls", None)
        crop_kwargs.pop("provider_call_offset", None)
        crop_kwargs["double_pass"] = False
        crop_texts: list[str] = []
        crop_models: list[str] = []
        mixed_script_provenances: list[MixedScriptRegionProvenance] = []
        used_mixed_script = False
        latin_region_calls_used = 0
        engine_reasons: list[str] = []

        for crop_index, crop in enumerate(plan.crops, start=1):
            crop_result: GeminiResult | None = None
            last_error: Exception | None = None
            recitation_exhausted = False
            for model_index, model_name in enumerate(model_candidates):
                try:
                    crop_result = transcribe_pages_with_gemini(
                        pages=[crop],
                        language_hint=language_hint,
                        prompt_variant=prompt_variant,
                        model_name=model_name,
                        max_provider_calls=1,
                        provider_call_offset=0,
                        **crop_kwargs,
                    )
                    break
                except GeminiError as exc:
                    last_error = exc
                    response_failure_code = (
                        exc.failure_code
                        if isinstance(exc, GeminiResponseError)
                        else None
                    )
                    has_next_model = model_index + 1 < len(model_candidates)
                    if (
                        response_failure_code == GeminiResponseFailureCode.RECITATION
                        and has_next_model
                    ):
                        logger.warning(
                            "Hebrew printed crop RECITATION advancing model: "
                            "page=%s crop_index=%s model=%s -> %s",
                            page.page_index,
                            crop_index,
                            model_name,
                            model_candidates[model_index + 1],
                        )
                        continue
                    if _is_quota_error(exc) and has_next_model:
                        logger.warning(
                            "Hebrew printed crop quota advancing model: "
                            "page=%s crop_index=%s model=%s -> %s",
                            page.page_index,
                            crop_index,
                            model_name,
                            model_candidates[model_index + 1],
                        )
                        continue
                    recitation_exhausted = (
                        response_failure_code == GeminiResponseFailureCode.RECITATION
                        and not has_next_model
                    )
                    if recitation_exhausted:
                        break
                    logger.warning(
                        "Hebrew printed crop recovery failed: page=%s "
                        "crop_index=%s model=%s failure=%s",
                        page.page_index,
                        crop_index,
                        model_name,
                        (
                            response_failure_code.value
                            if response_failure_code is not None
                            else type(exc).__name__
                        ),
                    )
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

            recovered_via_mixed_script = False
            recovered_region_box: MixedScriptRegionBox | None = None
            if crop_result is None and recitation_exhausted:
                mixed_result, latin_region_calls_used, mixed_failure, mixed_box = (
                    self._recover_hebrew_printed_mixed_script_region(
                        failed_crop=crop,
                        crop_index=crop_index,
                        successful_crop_texts=crop_texts,
                        crop_kwargs=crop_kwargs,
                        hebrew_printed_mixed_script_enabled=(
                            hebrew_printed_mixed_script_enabled
                        ),
                        latin_region_calls_used=latin_region_calls_used,
                    )
                )
                if mixed_result is not None:
                    crop_result = mixed_result
                    used_mixed_script = True
                    recovered_via_mixed_script = True
                    recovered_region_box = mixed_box
                elif mixed_failure is not None:
                    last_error = mixed_failure

            if crop_result is None:
                self._persist_page_failure(
                    checkpoint_id=checkpoint_id,
                    lease_token=lease_token,
                    exc=last_error or GeminiError("crop recovery produced no result"),
                    page_index=page.page_index,
                )
                self._raise_incomplete(attempt_id)
                raise AssertionError("unreachable")

            crop_texts.append(crop_result.text)
            crop_models.append(crop_result.engine_name)
            engine_reasons.extend(crop_result.review_reasons or [])
            if recovered_via_mixed_script:
                mixed_script_provenances.append(
                    MixedScriptRegionProvenance(
                        order=crop_index,
                        script="latn",
                        model=crop_result.engine_name,
                        source="region",
                        region_box=recovered_region_box,
                    )
                )
            elif hebrew_text_is_reusable_for_mixed_script(crop_result.text):
                mixed_script_provenances.append(
                    MixedScriptRegionProvenance(
                        order=crop_index,
                        script="he",
                        model=crop_result.engine_name,
                        source="crop",
                    )
                )

        assembled = merge_overlapping_crop_texts(crop_texts[0], crop_texts[1])
        if not assembled:
            self._persist_page_failure(
                checkpoint_id=checkpoint_id,
                lease_token=lease_token,
                exc=ValueError("crop recovery assembled empty page text"),
                page_index=page.page_index,
            )
            self._raise_incomplete(attempt_id)
            raise AssertionError("unreachable")

        reasons: list[str] = []
        reason_candidates = [REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY]
        if used_mixed_script:
            reason_candidates.append(
                REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK
            )
        reason_candidates.extend(engine_reasons)
        for reason in reason_candidates:
            if reason and reason not in reasons:
                reasons.append(reason)
        if used_mixed_script:
            engine_name = mixed_script_assembly_engine_name(
                tuple(sorted(mixed_script_provenances, key=lambda region: region.order))
            )
        else:
            engine_name = crop_assembly_engine_name(crop_models)
        return GeminiResult(
            text=assembled,
            needs_review=True,
            engine_name=engine_name,
            review_reasons=reasons,
        )

    def _recover_hebrew_printed_mixed_script_region(
        self,
        *,
        failed_crop: PageImage,
        crop_index: int,
        successful_crop_texts: list[str],
        crop_kwargs: dict[str, Any],
        hebrew_printed_mixed_script_enabled: bool,
        latin_region_calls_used: int,
    ) -> tuple[GeminiResult | None, int, Exception | None, MixedScriptRegionBox | None]:
        if not hebrew_printed_mixed_script_enabled:
            return None, latin_region_calls_used, None, None
        if latin_region_calls_used >= MAX_LATIN_REGION_PROVIDER_CALLS:
            return None, latin_region_calls_used, None, None
        if not any(
            hebrew_text_is_reusable_for_mixed_script(text)
            for text in successful_crop_texts
        ):
            return None, latin_region_calls_used, None, None

        plan = plan_structural_candidate_region(failed_crop)
        if plan.decision == MixedScriptPlanDecision.NO_REGION:
            logger.warning(
                "Hebrew printed mixed-script fallback skipped; no substantial "
                "structural candidate: crop_index=%s",
                crop_index,
            )
            return None, latin_region_calls_used, None, None
        if (
            plan.decision == MixedScriptPlanDecision.STRUCTURAL_AMBIGUOUS
            or plan.region is None
        ):
            logger.warning(
                "Hebrew printed mixed-script fallback skipped; structurally "
                "ambiguous candidate spans: crop_index=%s",
                crop_index,
            )
            return None, latin_region_calls_used, None, None

        logger.warning(
            "Starting Hebrew printed mixed-script Latin OCR on a structural "
            "candidate: crop_index=%s model=%s",
            crop_index,
            LATIN_PRINTED_GEMINI_MODEL,
        )
        region_kwargs = dict(crop_kwargs)
        try:
            latin_result = transcribe_pages_with_gemini(
                pages=[plan.region],
                language_hint=Document.Language.ENGLISH,
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                model_name=LATIN_PRINTED_GEMINI_MODEL,
                max_provider_calls=1,
                provider_call_offset=0,
                **region_kwargs,
            )
        except Exception as exc:
            return None, latin_region_calls_used + 1, exc, None

        latin_region_calls_used += 1
        evaluation = evaluate_script_dominance(latin_result.text)
        logger.warning(
            "Hebrew printed mixed-script Latin OCR dominance evaluated: "
            "crop_index=%s dominance=%s latin_letters=%s hebrew_letters=%s "
            "identified_letters=%s latin_ratio=%s hebrew_ratio=%s "
            "technical_token_letters_excluded=%s",
            crop_index,
            evaluation.dominance.value,
            evaluation.latin_letters,
            evaluation.hebrew_letters,
            evaluation.identified_letters,
            evaluation.latin_ratio,
            evaluation.hebrew_ratio,
            evaluation.technical_token_letters_excluded,
            extra={
                "crop_index": crop_index,
                "dominance": evaluation.dominance.value,
                "latin_letters": evaluation.latin_letters,
                "hebrew_letters": evaluation.hebrew_letters,
                "identified_letters": evaluation.identified_letters,
                "latin_ratio": evaluation.latin_ratio,
                "hebrew_ratio": evaluation.hebrew_ratio,
                "technical_token_letters_excluded": (
                    evaluation.technical_token_letters_excluded
                ),
            },
        )
        if evaluation.dominance == ScriptDominance.LATIN:
            return latin_result, latin_region_calls_used, None, plan.box
        logger.warning(
            "Hebrew printed mixed-script fallback fail-closed; Latin OCR was "
            "not Latin-dominant: crop_index=%s dominance=%s",
            crop_index,
            evaluation.dominance.value,
        )
        # Only after mixed-script preconditions and a Latin printed probe.
        return (
            None,
            latin_region_calls_used,
            ValueError("MIXED_SCRIPT_REGION_AMBIGUOUS"),
            None,
        )

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
            failure_message = f"exception_class={type(exc).__name__}"
            message = str(exc).strip()
            if message == "MIXED_SCRIPT_REGION_AMBIGUOUS":
                failure_code = "MIXED_SCRIPT_REGION_AMBIGUOUS"
                failure_message = "mixed_script_region_ambiguous"
            else:
                failure_code = "API_ERROR"
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
