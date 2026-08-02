"""Durable identity, fencing, and assembly for Gemini OCR pages."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Iterable, Sequence

from django.db import transaction
from django.utils import timezone

from documents.models import (
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
)
from documents.services.gemini_engine import (
    GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
    GEMINI_OCR_PAGE_RETRY_POLICY_VERSION,
    GeminiTranscriptionContract,
)
from documents.services.page_extraction import PageImage

PAGE_CHECKPOINT_LEASE = timedelta(minutes=45)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@dataclass(frozen=True)
class GeminiAttemptIdentity:
    identity_fingerprint: str
    source_fingerprint: str
    route_fingerprint: str
    prompt_fingerprint: str
    config_fingerprint: str
    prompt_contract_version: str
    model_candidates: tuple[str, ...]
    expected_page_count: int
    page_fingerprints: dict[int, str]
    source_content_fingerprints: dict[int, str]


def build_gemini_attempt_identity(
    *,
    pages: Sequence[PageImage],
    language_hint: str | None,
    text_input_type: str | None,
    handwriting_type: str | None,
    engine_key: str,
    prompt_variant: str,
    model_candidates: Sequence[str],
    contract: GeminiTranscriptionContract,
    min_text_length: int,
    double_pass: bool,
    consistency_min_ratio: float,
    temperature: float,
    top_k: int,
    top_p: float,
    max_output_tokens: int | None,
    max_output_tokens_hard_cap: int,
) -> GeminiAttemptIdentity:
    ordered_pages = sorted(pages, key=lambda page: page.page_index)
    expected_indices = list(range(1, len(ordered_pages) + 1))
    actual_indices = [page.page_index for page in ordered_pages]
    if not ordered_pages or actual_indices != expected_indices:
        raise ValueError(
            "Gemini page checkpoint identity requires contiguous 1-based page indices"
        )

    normalized_candidates = tuple(str(model).strip() for model in model_candidates)
    if not normalized_candidates or any(not model for model in normalized_candidates):
        raise ValueError("Gemini page checkpoint identity requires model candidates")

    page_fingerprints: dict[int, str] = {}
    source_content_fingerprints: dict[int, str] = {}
    source_pages: list[dict[str, Any]] = []
    for page in ordered_pages:
        normalized_page_sha256 = _bytes_sha256(page.image_bytes)
        source_content_fingerprint = (
            page.source_content_fingerprint or normalized_page_sha256
        )
        page_payload = {
            "page_index": page.page_index,
            "mime_type": page.mime_type,
            "source_identity": page.source_identity,
            "source_content_fingerprint": source_content_fingerprint,
            "normalized_page_sha256": normalized_page_sha256,
        }
        page_fingerprints[page.page_index] = _canonical_sha256(page_payload)
        source_content_fingerprints[page.page_index] = source_content_fingerprint
        source_pages.append(page_payload)

    source_fingerprint = _canonical_sha256({"pages": source_pages})
    route_fingerprint = _canonical_sha256(
        {
            "engine_key": engine_key,
            "handwriting_type": handwriting_type or "",
            "language_hint": language_hint or "",
            "prompt_variant": prompt_variant,
            "text_input_type": text_input_type or "",
        }
    )
    config_fingerprint = _canonical_sha256(
        {
            "api_version": contract.api_version,
            "consistency_min_ratio": consistency_min_ratio,
            "double_pass": double_pass,
            "effective_temperature": contract.effective_temperature,
            "max_output_tokens": max_output_tokens,
            "max_output_tokens_hard_cap": max_output_tokens_hard_cap,
            "max_provider_calls_per_page": GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
            "min_text_length": min_text_length,
            "model_candidates": list(normalized_candidates),
            "output_mode": contract.output_mode,
            "retry_policy_version": GEMINI_OCR_PAGE_RETRY_POLICY_VERSION,
            "temperature": temperature,
            "top_k": top_k,
            "top_p": top_p,
        }
    )
    identity_fingerprint = _canonical_sha256(
        {
            "config_fingerprint": config_fingerprint,
            "expected_page_count": len(ordered_pages),
            "model_candidates": list(normalized_candidates),
            "prompt_contract_version": contract.prompt_contract_version,
            "prompt_fingerprint": contract.prompt_fingerprint,
            "route_fingerprint": route_fingerprint,
            "source_fingerprint": source_fingerprint,
        }
    )
    return GeminiAttemptIdentity(
        identity_fingerprint=identity_fingerprint,
        source_fingerprint=source_fingerprint,
        route_fingerprint=route_fingerprint,
        prompt_fingerprint=contract.prompt_fingerprint,
        config_fingerprint=config_fingerprint,
        prompt_contract_version=contract.prompt_contract_version,
        model_candidates=normalized_candidates,
        expected_page_count=len(ordered_pages),
        page_fingerprints=page_fingerprints,
        source_content_fingerprints=source_content_fingerprints,
    )


def get_or_create_gemini_attempt(
    *,
    document_id: int,
    identity: GeminiAttemptIdentity,
) -> GeminiOcrAttempt:
    attempt, _created = GeminiOcrAttempt.objects.get_or_create(
        document_id=document_id,
        identity_fingerprint=identity.identity_fingerprint,
        defaults={
            "source_fingerprint": identity.source_fingerprint,
            "route_fingerprint": identity.route_fingerprint,
            "prompt_fingerprint": identity.prompt_fingerprint,
            "config_fingerprint": identity.config_fingerprint,
            "prompt_contract_version": identity.prompt_contract_version,
            "model_candidates": list(identity.model_candidates),
            "expected_page_count": identity.expected_page_count,
            "status": GeminiOcrAttempt.Status.IN_PROGRESS,
            "missing_page_indices": list(range(1, identity.expected_page_count + 1)),
        },
    )
    expected = {
        "source_fingerprint": identity.source_fingerprint,
        "route_fingerprint": identity.route_fingerprint,
        "prompt_fingerprint": identity.prompt_fingerprint,
        "config_fingerprint": identity.config_fingerprint,
        "prompt_contract_version": identity.prompt_contract_version,
        "model_candidates": list(identity.model_candidates),
        "expected_page_count": identity.expected_page_count,
    }
    for field_name, expected_value in expected.items():
        if getattr(attempt, field_name) != expected_value:
            raise RuntimeError(
                "Gemini OCR attempt identity collision or inconsistent "
                "persisted identity"
            )
    return attempt


class GeminiPageClaimAction(StrEnum):
    EXECUTE = "execute"
    REUSE = "reuse"
    BUSY = "busy"


@dataclass(frozen=True)
class GeminiPageClaim:
    action: GeminiPageClaimAction
    checkpoint_id: int
    page_index: int
    lease_token: uuid.UUID | None = None


def claim_gemini_page(
    *,
    attempt_id: int,
    page_index: int,
    page_fingerprint: str,
    source_content_fingerprint: str,
) -> GeminiPageClaim:
    now = timezone.now()
    with transaction.atomic():
        attempt = GeminiOcrAttempt.objects.select_for_update().get(pk=attempt_id)
        if page_index < 1 or page_index > attempt.expected_page_count:
            raise ValueError(
                "Gemini page checkpoint index is outside the attempt page range"
            )
        checkpoint = (
            GeminiOcrPageCheckpoint.objects.select_for_update()
            .filter(attempt=attempt, page_index=page_index)
            .first()
        )
        if checkpoint is not None:
            if checkpoint.page_fingerprint != page_fingerprint:
                raise RuntimeError(
                    "Gemini OCR checkpoint page fingerprint does not match "
                    "attempt identity"
                )
            if checkpoint.source_content_fingerprint != source_content_fingerprint:
                raise RuntimeError(
                    "Gemini OCR checkpoint source fingerprint does not match "
                    "attempt identity"
                )
            if checkpoint.status == GeminiOcrPageCheckpoint.Status.SUCCEEDED:
                return GeminiPageClaim(
                    GeminiPageClaimAction.REUSE,
                    checkpoint.id,
                    page_index,
                )
            if (
                checkpoint.status == GeminiOcrPageCheckpoint.Status.RUNNING
                and checkpoint.lease_expires_at is not None
                and checkpoint.lease_expires_at > now
            ):
                return GeminiPageClaim(
                    GeminiPageClaimAction.BUSY,
                    checkpoint.id,
                    page_index,
                )

        token = uuid.uuid4()
        values = {
            "page_fingerprint": page_fingerprint,
            "source_content_fingerprint": source_content_fingerprint,
            "status": GeminiOcrPageCheckpoint.Status.RUNNING,
            "lease_token": token,
            "lease_expires_at": now + PAGE_CHECKPOINT_LEASE,
            "actual_model": "",
            "text": None,
            "needs_review": False,
            "review_reasons": [],
            "failure_code": "",
            "failure_message": "",
            "started_at": now,
            "completed_at": None,
        }
        if checkpoint is None:
            checkpoint = GeminiOcrPageCheckpoint.objects.create(
                attempt=attempt,
                page_index=page_index,
                **values,
            )
        else:
            for field_name, value in values.items():
                setattr(checkpoint, field_name, value)
            checkpoint.save(
                update_fields=[*values.keys(), "updated_at"],
            )

        if attempt.status != GeminiOcrAttempt.Status.IN_PROGRESS:
            attempt.status = GeminiOcrAttempt.Status.IN_PROGRESS
            attempt.completed_at = None
            attempt.save(
                update_fields=["status", "completed_at", "updated_at"],
            )
        return GeminiPageClaim(
            GeminiPageClaimAction.EXECUTE,
            checkpoint.id,
            page_index,
            lease_token=token,
        )


class StaleGeminiPageClaimError(RuntimeError):
    pass


def persist_gemini_page_success(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    actual_model: str,
    text: str,
    needs_review: bool,
    review_reasons: Sequence[str],
) -> None:
    normalized_text = text.strip()
    if not normalized_text:
        raise ValueError("Cannot persist an empty successful Gemini page checkpoint")
    with transaction.atomic():
        checkpoint = GeminiOcrPageCheckpoint.objects.select_for_update().get(
            pk=checkpoint_id
        )
        if (
            checkpoint.status != GeminiOcrPageCheckpoint.Status.RUNNING
            or checkpoint.lease_token != lease_token
        ):
            raise StaleGeminiPageClaimError(
                "Stale Gemini page success claim for "
                f"page_index={checkpoint.page_index}"
            )
        checkpoint.status = GeminiOcrPageCheckpoint.Status.SUCCEEDED
        checkpoint.lease_token = None
        checkpoint.lease_expires_at = None
        checkpoint.actual_model = actual_model
        checkpoint.text = normalized_text
        checkpoint.needs_review = needs_review
        checkpoint.review_reasons = list(review_reasons)
        checkpoint.failure_code = ""
        checkpoint.failure_message = ""
        checkpoint.completed_at = timezone.now()
        checkpoint.save(
            update_fields=[
                "status",
                "lease_token",
                "lease_expires_at",
                "actual_model",
                "text",
                "needs_review",
                "review_reasons",
                "failure_code",
                "failure_message",
                "completed_at",
                "updated_at",
            ]
        )


def _missing_page_indices_locked(attempt: GeminiOcrAttempt) -> list[int]:
    succeeded = set(
        attempt.page_checkpoints.filter(
            status=GeminiOcrPageCheckpoint.Status.SUCCEEDED
        ).values_list("page_index", flat=True)
    )
    return [
        page_index
        for page_index in range(1, attempt.expected_page_count + 1)
        if page_index not in succeeded
    ]


def persist_gemini_page_failure(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    failure_code: str,
    failure_message: str,
) -> list[int]:
    safe_code = failure_code.strip()[:64] or "GEMINI_PAGE_FAILED"
    safe_message = failure_message.strip()[:512]
    with transaction.atomic():
        checkpoint = (
            GeminiOcrPageCheckpoint.objects.select_for_update()
            .select_related("attempt")
            .get(pk=checkpoint_id)
        )
        if (
            checkpoint.status != GeminiOcrPageCheckpoint.Status.RUNNING
            or checkpoint.lease_token != lease_token
        ):
            raise StaleGeminiPageClaimError(
                "Stale Gemini page failure claim for "
                f"page_index={checkpoint.page_index}"
            )
        checkpoint.status = GeminiOcrPageCheckpoint.Status.FAILED
        checkpoint.lease_token = None
        checkpoint.lease_expires_at = None
        checkpoint.actual_model = ""
        checkpoint.text = None
        checkpoint.needs_review = False
        checkpoint.review_reasons = []
        checkpoint.failure_code = safe_code
        checkpoint.failure_message = safe_message
        checkpoint.completed_at = timezone.now()
        checkpoint.save(
            update_fields=[
                "status",
                "lease_token",
                "lease_expires_at",
                "actual_model",
                "text",
                "needs_review",
                "review_reasons",
                "failure_code",
                "failure_message",
                "completed_at",
                "updated_at",
            ]
        )

        attempt = GeminiOcrAttempt.objects.select_for_update().get(
            pk=checkpoint.attempt_id
        )
        missing = _missing_page_indices_locked(attempt)
        attempt.status = GeminiOcrAttempt.Status.PARTIAL
        attempt.missing_page_indices = missing
        attempt.completed_at = None
        attempt.save(
            update_fields=[
                "status",
                "missing_page_indices",
                "completed_at",
                "updated_at",
            ]
        )
        return missing


@dataclass(frozen=True)
class AssembledGeminiCheckpointResult:
    text: str
    needs_review: bool
    engine_name: str
    review_reasons: list[str]


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _assembled_engine(checkpoints: Sequence[GeminiOcrPageCheckpoint]) -> str:
    models = {checkpoint.actual_model for checkpoint in checkpoints}
    if len(models) == 1:
        return next(iter(models))
    mapping = [
        {"page_index": checkpoint.page_index, "model": checkpoint.actual_model}
        for checkpoint in checkpoints
    ]
    return f"gemini-mixed:{_canonical_sha256(mapping)[:48]}"


def assemble_gemini_attempt(
    *,
    attempt_id: int,
) -> AssembledGeminiCheckpointResult | None:
    with transaction.atomic():
        attempt = GeminiOcrAttempt.objects.select_for_update().get(pk=attempt_id)
        checkpoints = list(
            attempt.page_checkpoints.select_for_update().order_by("page_index")
        )
        succeeded_by_index = {
            checkpoint.page_index: checkpoint
            for checkpoint in checkpoints
            if checkpoint.status == GeminiOcrPageCheckpoint.Status.SUCCEEDED
        }
        missing = [
            page_index
            for page_index in range(1, attempt.expected_page_count + 1)
            if page_index not in succeeded_by_index
        ]
        if missing:
            attempt.status = GeminiOcrAttempt.Status.PARTIAL
            attempt.missing_page_indices = missing
            attempt.completed_at = None
            attempt.save(
                update_fields=[
                    "status",
                    "missing_page_indices",
                    "completed_at",
                    "updated_at",
                ]
            )
            return None

        ordered = [
            succeeded_by_index[page_index]
            for page_index in range(1, attempt.expected_page_count + 1)
        ]
        text = "\n\n".join((checkpoint.text or "").strip() for checkpoint in ordered)
        if not text.strip():
            raise RuntimeError("Completed Gemini OCR attempt assembled empty text")

        attempt.status = GeminiOcrAttempt.Status.COMPLETED
        attempt.missing_page_indices = []
        if attempt.completed_at is None:
            attempt.completed_at = timezone.now()
        attempt.save(
            update_fields=[
                "status",
                "missing_page_indices",
                "completed_at",
                "updated_at",
            ]
        )
        return AssembledGeminiCheckpointResult(
            text=text.strip(),
            needs_review=any(checkpoint.needs_review for checkpoint in ordered),
            engine_name=_assembled_engine(ordered),
            review_reasons=_dedupe_preserving_order(
                reason for checkpoint in ordered for reason in checkpoint.review_reasons
            ),
        )


def missing_pages_for_attempt(attempt_id: int) -> list[int]:
    attempt = GeminiOcrAttempt.objects.get(pk=attempt_id)
    return _missing_page_indices_locked(attempt)
