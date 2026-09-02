"""Durable identity, fencing, and assembly for printed-Arabic banded OCR.

Provider HTTP, Cloud Vision, and Antigravity calls belong in later phases.
This module only performs deterministic identity and database operations.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Sequence

from django.db import DatabaseError, transaction
from django.db.models import Sum
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
)

PAGE_CHECKPOINT_LEASE = timedelta(minutes=45)

ARABIC_PRINTED_BANDED_PROMPT_CONTRACT_VERSION = "arabic-printed-banded-prompt-v1"
ARABIC_PRINTED_COMPLETION_MARKER_VERSION = "vs-archive-transcription-complete-v1"
ARABIC_PRINTED_BANDING_STRATEGY = "structural-gap-v3-hybrid"
ARABIC_PRINTED_MAX_BANDS = 6
ARABIC_PRINTED_MAX_BAND_HEIGHT_RATIO = "0.35"
ARABIC_PRINTED_JOIN = "single-newline"
ARABIC_PRINTED_COVERAGE_MIN = "0.65"
ARABIC_PRINTED_COVERAGE_MAX = "1.60"
ARABIC_PRINTED_CROP_MIME = "image/jpeg"
ARABIC_PRINTED_JPEG_QUALITY = 95
ARABIC_PRINTED_VISION_FEATURE = "DOCUMENT_TEXT_DETECTION"
ARABIC_PRINTED_VISION_LANGUAGE_HINTS = ("ar",)
ARABIC_PRINTED_MAX_CREATES_PER_BAND = 2
ARABIC_PRINTED_MAX_CREATES_PER_PAGE = 12
ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN = 24
ARABIC_PRINTED_CANCEL_CONFIRMED_CANCELLED = "cancelled"
_PRIOR_ATTEMPTS_MAX_ENTRIES = 4
_PRIOR_ATTEMPT_MAX_STRING_CHARS = 128
_PRIOR_ATTEMPTS_MAX_JSON_BYTES = 2048
_PRIOR_ATTEMPT_ALLOWED_KEYS = frozenset(
    {
        "kind",
        "interaction_id",
        "provider_status",
        "failure_type",
        "latency_ms",
        "http_status",
        "create_call_count",
    }
)
_PRIOR_ATTEMPT_REJECTED_KEYS = frozenset(
    {
        "api_key",
        "body",
        "draft",
        "image",
        "key",
        "payload",
        "prompt",
        "response",
        "response_body",
        "text",
        "thought",
        "thoughts",
        "token",
        "transcription",
        "transcription_text",
        "vision_draft",
        "vision_draft_text",
    }
)

_BAND_SAFE_DIAGNOSTIC_FIELDS = frozenset(
    {
        "primary_interaction_id",
        "primary_provider_status",
        "primary_latency_ms",
        "primary_failure_type",
        "primary_safe_diagnostics",
        "fallback_interaction_id",
        "fallback_provider_status",
        "fallback_latency_ms",
        "fallback_failure_type",
        "fallback_safe_diagnostics",
        "cancel_http_status",
        "cancel_confirmed_status",
        "cancel_safe_diagnostics",
        "prior_attempts",
    }
)


class ArabicPrintedCheckpointBusyError(RuntimeError):
    """An unexpired page lease is held by another claim."""


class StaleArabicPrintedPageClaimError(RuntimeError):
    """A state-changing write used a lease token that is no longer current."""


class ArabicPrintedIdentityMismatchError(RuntimeError):
    """Persisted identity or banding contract does not match the caller."""


class ArabicPrintedCheckpointPersistenceRetryableError(RuntimeError):
    """Local checkpoint persistence failed and should be retried."""

    def __init__(self, *, stage: str, page_index: int | None = None) -> None:
        self.stage = stage
        self.page_index = page_index
        parts = [f"stage={stage}"]
        if page_index is not None:
            parts.append(f"page_index={page_index}")
        self.safe_message = " ".join(parts)[:512]
        super().__init__(self.safe_message)


def _canonical_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _utf8_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utf8_byte_length(value: str) -> int:
    return len(value.encode("utf-8"))


def _safe_failure_code(failure_code: str, *, default: str) -> str:
    return failure_code.strip()[:64] or default


def _safe_failure_message(failure_message: str) -> str:
    return failure_message.strip()[:512]


def _truncate_diagnostic(value: str) -> str:
    return value.strip()[:512]


def _raise_retryable(stage: str, page_index: int | None, exc: DatabaseError) -> None:
    raise ArabicPrintedCheckpointPersistenceRetryableError(
        stage=stage,
        page_index=page_index,
    ) from exc


def _ratio_matches(stored: Decimal | str, expected: str) -> bool:
    return Decimal(str(stored)) == Decimal(expected)


@dataclass(frozen=True)
class ArabicPrintedPageSource:
    page_index: int
    mime_type: str
    source_identity: str
    source_content_fingerprint: str
    oriented_image_sha256: str
    oriented_image_width: int
    oriented_image_height: int


@dataclass(frozen=True)
class ArabicPrintedAttemptIdentity:
    identity_fingerprint: str
    source_fingerprint: str
    route_fingerprint: str
    prompt_fingerprint: str
    config_fingerprint: str
    prompt_contract_version: str
    banding_contract_fingerprint: str
    banding_strategy: str
    max_band_height_ratio: str
    expected_page_count: int
    page_fingerprints: dict[int, str]
    source_content_fingerprints: dict[int, str]
    oriented_image_sha256s: dict[int, str]
    oriented_dimensions: dict[int, tuple[int, int]]


@dataclass(frozen=True)
class ArabicPrintedBandPlan:
    band_index: int
    rect_x: int
    rect_y: int
    rect_width: int
    rect_height: int
    crop_mime: str
    crop_byte_length: int
    crop_sha256: str
    vision_draft_text: str
    vision_draft_byte_length: int
    vision_draft_sha256: str


@dataclass(frozen=True)
class ArabicPrintedBandSafeDiagnostics:
    primary_interaction_id: str | None = None
    primary_provider_status: str | None = None
    primary_latency_ms: int | None = None
    primary_failure_type: str | None = None
    primary_safe_diagnostics: str | None = None
    fallback_interaction_id: str | None = None
    fallback_provider_status: str | None = None
    fallback_latency_ms: int | None = None
    fallback_failure_type: str | None = None
    fallback_safe_diagnostics: str | None = None
    cancel_http_status: int | None = None
    cancel_confirmed_status: str | None = None
    cancel_safe_diagnostics: str | None = None
    prior_attempts: list[dict[str, Any]] | None = None


def build_arabic_printed_attempt_identity(
    *,
    pages: Sequence[ArabicPrintedPageSource],
    language_hint: str,
    text_input_type: str,
    engine_key: str,
    prompt_variant: str,
    prompt_contract_version: str = ARABIC_PRINTED_BANDED_PROMPT_CONTRACT_VERSION,
    completion_marker_version: str = ARABIC_PRINTED_COMPLETION_MARKER_VERSION,
    banding_strategy: str = ARABIC_PRINTED_BANDING_STRATEGY,
    max_bands: int = ARABIC_PRINTED_MAX_BANDS,
    max_band_height_ratio: str = ARABIC_PRINTED_MAX_BAND_HEIGHT_RATIO,
    coverage_min: str = ARABIC_PRINTED_COVERAGE_MIN,
    coverage_max: str = ARABIC_PRINTED_COVERAGE_MAX,
    crop_mime: str = ARABIC_PRINTED_CROP_MIME,
    jpeg_quality: int = ARABIC_PRINTED_JPEG_QUALITY,
    vision_feature: str = ARABIC_PRINTED_VISION_FEATURE,
    vision_language_hints: Sequence[str] = ARABIC_PRINTED_VISION_LANGUAGE_HINTS,
) -> ArabicPrintedAttemptIdentity:
    ordered_pages = sorted(pages, key=lambda page: page.page_index)
    expected_indices = list(range(len(ordered_pages)))
    actual_indices = [page.page_index for page in ordered_pages]
    if not ordered_pages or actual_indices != expected_indices:
        raise ValueError(
            "Arabic printed page checkpoint identity requires contiguous 0-based page indices"
        )

    page_fingerprints: dict[int, str] = {}
    source_content_fingerprints: dict[int, str] = {}
    oriented_image_sha256s: dict[int, str] = {}
    oriented_dimensions: dict[int, tuple[int, int]] = {}
    source_pages: list[dict[str, Any]] = []
    for page in ordered_pages:
        if page.oriented_image_width < 1 or page.oriented_image_height < 1:
            raise ValueError("Arabic printed identity requires oriented image dimensions")
        if len(page.oriented_image_sha256) != 64 or len(page.source_content_fingerprint) != 64:
            raise ValueError("Arabic printed identity requires SHA-256 fingerprints")
        page_payload = {
            "mime_type": page.mime_type,
            "oriented_image_height": page.oriented_image_height,
            "oriented_image_sha256": page.oriented_image_sha256,
            "oriented_image_width": page.oriented_image_width,
            "page_index": page.page_index,
            "source_content_fingerprint": page.source_content_fingerprint,
            "source_identity": page.source_identity,
        }
        page_fingerprints[page.page_index] = _canonical_sha256(page_payload)
        source_content_fingerprints[page.page_index] = page.source_content_fingerprint
        oriented_image_sha256s[page.page_index] = page.oriented_image_sha256
        oriented_dimensions[page.page_index] = (
            page.oriented_image_width,
            page.oriented_image_height,
        )
        source_pages.append(page_payload)

    source_fingerprint = _canonical_sha256({"pages": source_pages})
    route_fingerprint = _canonical_sha256(
        {
            "engine_key": engine_key,
            "language_hint": language_hint,
            "prompt_variant": prompt_variant,
            "text_input_type": text_input_type,
        }
    )
    prompt_fingerprint = _canonical_sha256(
        {
            "completion_marker_version": completion_marker_version,
            "prompt_contract_version": prompt_contract_version,
        }
    )
    banding_contract_fingerprint = _canonical_sha256(
        {
            "banding_strategy": banding_strategy,
            "join": ARABIC_PRINTED_JOIN,
            "max_band_height_ratio": max_band_height_ratio,
            "max_bands": max_bands,
        }
    )
    config_fingerprint = _canonical_sha256(
        {
            "banding_contract_fingerprint": banding_contract_fingerprint,
            "coverage_max": coverage_max,
            "coverage_min": coverage_min,
            "crop_mime": crop_mime,
            "jpeg_quality": jpeg_quality,
            "max_creates_per_band": ARABIC_PRINTED_MAX_CREATES_PER_BAND,
            "max_creates_per_page": ARABIC_PRINTED_MAX_CREATES_PER_PAGE,
            "vision_feature": vision_feature,
            "vision_language_hints": list(vision_language_hints),
        }
    )
    identity_fingerprint = _canonical_sha256(
        {
            "config_fingerprint": config_fingerprint,
            "expected_page_count": len(ordered_pages),
            "prompt_contract_version": prompt_contract_version,
            "prompt_fingerprint": prompt_fingerprint,
            "route_fingerprint": route_fingerprint,
            "source_fingerprint": source_fingerprint,
        }
    )
    return ArabicPrintedAttemptIdentity(
        identity_fingerprint=identity_fingerprint,
        source_fingerprint=source_fingerprint,
        route_fingerprint=route_fingerprint,
        prompt_fingerprint=prompt_fingerprint,
        config_fingerprint=config_fingerprint,
        prompt_contract_version=prompt_contract_version,
        banding_contract_fingerprint=banding_contract_fingerprint,
        banding_strategy=banding_strategy,
        max_band_height_ratio=max_band_height_ratio,
        expected_page_count=len(ordered_pages),
        page_fingerprints=page_fingerprints,
        source_content_fingerprints=source_content_fingerprints,
        oriented_image_sha256s=oriented_image_sha256s,
        oriented_dimensions=oriented_dimensions,
    )


def get_or_create_arabic_printed_attempt(
    *,
    document_id: int,
    identity: ArabicPrintedAttemptIdentity,
) -> ArabicPrintedOcrAttempt:
    try:
        attempt, _created = ArabicPrintedOcrAttempt.objects.get_or_create(
            document_id=document_id,
            identity_fingerprint=identity.identity_fingerprint,
            defaults={
                "source_fingerprint": identity.source_fingerprint,
                "route_fingerprint": identity.route_fingerprint,
                "prompt_fingerprint": identity.prompt_fingerprint,
                "config_fingerprint": identity.config_fingerprint,
                "prompt_contract_version": identity.prompt_contract_version,
                "expected_page_count": identity.expected_page_count,
                "status": ArabicPrintedOcrAttempt.Status.IN_PROGRESS,
                "missing_page_indices": list(range(identity.expected_page_count)),
            },
        )
    except DatabaseError as exc:
        _raise_retryable("get_or_create_attempt", None, exc)
    expected = {
        "source_fingerprint": identity.source_fingerprint,
        "route_fingerprint": identity.route_fingerprint,
        "prompt_fingerprint": identity.prompt_fingerprint,
        "config_fingerprint": identity.config_fingerprint,
        "prompt_contract_version": identity.prompt_contract_version,
        "expected_page_count": identity.expected_page_count,
    }
    for field_name, expected_value in expected.items():
        if getattr(attempt, field_name) != expected_value:
            raise ArabicPrintedIdentityMismatchError(
                "Arabic printed OCR attempt identity collision or inconsistent "
                "persisted identity"
            )
    return attempt


def ensure_arabic_printed_page_checkpoints(
    *,
    attempt_id: int,
    identity: ArabicPrintedAttemptIdentity,
) -> list[ArabicPrintedOcrPageCheckpoint]:
    try:
        with transaction.atomic():
            attempt = ArabicPrintedOcrAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
            if attempt.expected_page_count != identity.expected_page_count:
                raise ArabicPrintedIdentityMismatchError(
                    "Arabic printed OCR attempt page count does not match identity"
                )
            checkpoints: list[ArabicPrintedOcrPageCheckpoint] = []
            for page_index in range(identity.expected_page_count):
                width, height = identity.oriented_dimensions[page_index]
                defaults = {
                    "page_fingerprint": identity.page_fingerprints[page_index],
                    "source_content_fingerprint": (
                        identity.source_content_fingerprints[page_index]
                    ),
                    "oriented_image_sha256": identity.oriented_image_sha256s[page_index],
                    "oriented_image_width": width,
                    "oriented_image_height": height,
                    "banding_contract_fingerprint": identity.banding_contract_fingerprint,
                    "banding_strategy": identity.banding_strategy,
                    "max_band_height_ratio": identity.max_band_height_ratio,
                    "status": ArabicPrintedOcrPageCheckpoint.Status.PLANNING,
                }
                checkpoint, created = (
                    ArabicPrintedOcrPageCheckpoint.objects.select_for_update()
                    .get_or_create(
                        attempt=attempt,
                        page_index=page_index,
                        defaults=defaults,
                    )
                )
                if not created:
                    mismatches = (
                        checkpoint.page_fingerprint != defaults["page_fingerprint"]
                        or checkpoint.source_content_fingerprint
                        != defaults["source_content_fingerprint"]
                        or checkpoint.oriented_image_sha256
                        != defaults["oriented_image_sha256"]
                        or checkpoint.oriented_image_width != width
                        or checkpoint.oriented_image_height != height
                        or checkpoint.banding_contract_fingerprint
                        != defaults["banding_contract_fingerprint"]
                        or checkpoint.banding_strategy != defaults["banding_strategy"]
                        or not _ratio_matches(
                            checkpoint.max_band_height_ratio,
                            identity.max_band_height_ratio,
                        )
                    )
                    if mismatches:
                        raise ArabicPrintedIdentityMismatchError(
                            "Arabic printed OCR page identity does not match "
                            f"attempt identity for page_index={page_index}"
                        )
                checkpoints.append(checkpoint)
            return checkpoints
    except (
        ArabicPrintedIdentityMismatchError,
        ArabicPrintedOcrAttempt.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("ensure_pages", None, exc)


class ArabicPrintedPageClaimAction(StrEnum):
    EXECUTE = "execute"
    REUSE = "reuse"


@dataclass(frozen=True)
class ArabicPrintedPageClaim:
    action: ArabicPrintedPageClaimAction
    checkpoint_id: int
    page_index: int
    lease_token: uuid.UUID | None = None


def _page_identity_matches(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    *,
    page_fingerprint: str,
    source_content_fingerprint: str,
    oriented_image_sha256: str,
) -> None:
    if checkpoint.page_fingerprint != page_fingerprint:
        raise ArabicPrintedIdentityMismatchError(
            "Arabic printed OCR checkpoint page fingerprint does not match "
            "attempt identity"
        )
    if checkpoint.source_content_fingerprint != source_content_fingerprint:
        raise ArabicPrintedIdentityMismatchError(
            "Arabic printed OCR checkpoint source fingerprint does not match "
            "attempt identity"
        )
    if checkpoint.oriented_image_sha256 != oriented_image_sha256:
        raise ArabicPrintedIdentityMismatchError(
            "Arabic printed OCR checkpoint oriented image fingerprint does not "
            "match attempt identity"
        )


def claim_arabic_printed_page(
    *,
    attempt_id: int,
    page_index: int,
    page_fingerprint: str,
    source_content_fingerprint: str,
    oriented_image_sha256: str,
) -> ArabicPrintedPageClaim:
    now = timezone.now()
    try:
        with transaction.atomic():
            attempt = ArabicPrintedOcrAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
            if page_index < 0 or page_index >= attempt.expected_page_count:
                raise ValueError(
                    "Arabic printed page checkpoint index is outside the attempt page range"
                )
            checkpoint = (
                ArabicPrintedOcrPageCheckpoint.objects.select_for_update()
                .filter(attempt=attempt, page_index=page_index)
                .first()
            )
            if checkpoint is None:
                raise ArabicPrintedIdentityMismatchError(
                    "Arabic printed OCR page checkpoint is missing; call "
                    "ensure_arabic_printed_page_checkpoints first"
                )
            _page_identity_matches(
                checkpoint,
                page_fingerprint=page_fingerprint,
                source_content_fingerprint=source_content_fingerprint,
                oriented_image_sha256=oriented_image_sha256,
            )
            if checkpoint.status == ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED:
                return ArabicPrintedPageClaim(
                    ArabicPrintedPageClaimAction.REUSE,
                    checkpoint.id,
                    page_index,
                )
            if (
                checkpoint.status == ArabicPrintedOcrPageCheckpoint.Status.RUNNING
                and checkpoint.lease_expires_at is not None
                and checkpoint.lease_expires_at > now
            ):
                raise ArabicPrintedCheckpointBusyError(
                    f"Arabic printed OCR page_index={page_index} is already claimed"
                )

            token = uuid.uuid4()
            checkpoint.status = ArabicPrintedOcrPageCheckpoint.Status.RUNNING
            checkpoint.lease_token = token
            checkpoint.lease_expires_at = now + PAGE_CHECKPOINT_LEASE
            checkpoint.assembled_text = None
            checkpoint.page_quality = ""
            checkpoint.runtime_engine_marker = ""
            checkpoint.failure_code = ""
            checkpoint.failure_message = ""
            checkpoint.started_at = checkpoint.started_at or now
            checkpoint.completed_at = None
            checkpoint.save(
                update_fields=[
                    "status",
                    "lease_token",
                    "lease_expires_at",
                    "assembled_text",
                    "page_quality",
                    "runtime_engine_marker",
                    "failure_code",
                    "failure_message",
                    "started_at",
                    "completed_at",
                    "updated_at",
                ]
            )
            if attempt.status != ArabicPrintedOcrAttempt.Status.IN_PROGRESS:
                attempt.status = ArabicPrintedOcrAttempt.Status.IN_PROGRESS
                attempt.completed_at = None
                attempt.save(
                    update_fields=["status", "completed_at", "updated_at"],
                )
            return ArabicPrintedPageClaim(
                ArabicPrintedPageClaimAction.EXECUTE,
                checkpoint.id,
                page_index,
                lease_token=token,
            )
    except (
        ArabicPrintedCheckpointBusyError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrAttempt.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("claim_page", page_index, exc)


def _require_fresh_page_claim(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    lease_token: uuid.UUID,
    *,
    operation: str,
) -> None:
    now = timezone.now()
    if (
        checkpoint.status != ArabicPrintedOcrPageCheckpoint.Status.RUNNING
        or checkpoint.lease_token != lease_token
        or checkpoint.lease_expires_at is None
        or checkpoint.lease_expires_at <= now
    ):
        raise StaleArabicPrintedPageClaimError(
            f"Stale Arabic printed page {operation} claim for "
            f"page_index={checkpoint.page_index}"
        )


def _missing_page_indices_locked(attempt: ArabicPrintedOcrAttempt) -> list[int]:
    succeeded = set(
        attempt.page_checkpoints.filter(
            status=ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED
        ).values_list("page_index", flat=True)
    )
    return [
        page_index
        for page_index in range(attempt.expected_page_count)
        if page_index not in succeeded
    ]


def _rollup_attempt_locked(attempt: ArabicPrintedOcrAttempt) -> list[int]:
    missing = _missing_page_indices_locked(attempt)
    if missing:
        attempt.status = ArabicPrintedOcrAttempt.Status.PARTIAL
        attempt.missing_page_indices = missing
        attempt.completed_at = None
    else:
        attempt.status = ArabicPrintedOcrAttempt.Status.COMPLETED
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
    return missing


def _locked_page(
    checkpoint_id: int,
    lease_token: uuid.UUID,
    *,
    operation: str,
) -> ArabicPrintedOcrPageCheckpoint:
    checkpoint = ArabicPrintedOcrPageCheckpoint.objects.select_for_update().get(
        pk=checkpoint_id
    )
    _require_fresh_page_claim(checkpoint, lease_token, operation=operation)
    return checkpoint


def _locked_band(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    band_index: int,
) -> ArabicPrintedOcrBandCheckpoint:
    band = (
        ArabicPrintedOcrBandCheckpoint.objects.select_for_update()
        .filter(page_checkpoint=checkpoint, band_index=band_index)
        .first()
    )
    if band is None:
        raise ValueError(f"Arabic printed band_index={band_index} is missing for the page")
    return band


def _durable_vision_plan_exists(checkpoint: ArabicPrintedOcrPageCheckpoint) -> bool:
    return (
        checkpoint.cloud_vision_call_count == 1
        and checkpoint.band_count >= 1
        and bool(checkpoint.cloud_vision_response_sha256)
        and checkpoint.band_checkpoints.exists()
    )


def _refresh_page_create_count(checkpoint: ArabicPrintedOcrPageCheckpoint) -> None:
    total = (
        ArabicPrintedOcrBandCheckpoint.objects.filter(
            page_checkpoint=checkpoint
        ).aggregate(total=Sum("create_call_count"))["total"]
        or 0
    )
    if total > ARABIC_PRINTED_MAX_CREATES_PER_PAGE:
        raise ValueError("Page antigravity create count cannot exceed 12")
    checkpoint.antigravity_create_count = total
    checkpoint.save(update_fields=["antigravity_create_count", "updated_at"])


def _normalize_diagnostics_payload(
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None,
) -> dict[str, Any]:
    if diagnostics is None:
        return {}
    if isinstance(diagnostics, ArabicPrintedBandSafeDiagnostics):
        payload = {
            field_name: getattr(diagnostics, field_name)
            for field_name in _BAND_SAFE_DIAGNOSTIC_FIELDS
            if getattr(diagnostics, field_name) is not None
        }
        return payload
    unknown = set(diagnostics) - _BAND_SAFE_DIAGNOSTIC_FIELDS
    if unknown:
        raise ValueError(
            "Unsupported diagnostic fields: " + ", ".join(sorted(unknown))
        )
    return dict(diagnostics)


def _normalized_cancel_confirmed_status(value: str) -> str:
    return value.strip().lower()


def _cancel_confirmed_cancelled(band: ArabicPrintedOcrBandCheckpoint) -> bool:
    return (
        _normalized_cancel_confirmed_status(band.cancel_confirmed_status)
        == ARABIC_PRINTED_CANCEL_CONFIRMED_CANCELLED
    )


def _validate_prior_attempts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("prior_attempts must be a list")
    if len(value) > _PRIOR_ATTEMPTS_MAX_ENTRIES:
        raise ValueError("prior_attempts cannot contain more than four entries")
    normalized: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("prior_attempts entries must be dictionaries")
        keys = {str(key) for key in entry}
        rejected = keys & _PRIOR_ATTEMPT_REJECTED_KEYS
        if rejected:
            raise ValueError(
                "prior_attempts contains sensitive keys: " + ", ".join(sorted(rejected))
            )
        unsupported = keys - _PRIOR_ATTEMPT_ALLOWED_KEYS
        if unsupported:
            raise ValueError(
                "prior_attempts contains unsupported keys: "
                + ", ".join(sorted(unsupported))
            )
        cleaned: dict[str, Any] = {}
        for key, item in entry.items():
            if item is not None and not isinstance(item, (str, int, float, bool)):
                raise ValueError("prior_attempts metadata must be JSON scalars")
            if isinstance(item, bool) or item is None:
                cleaned[str(key)] = item
            elif isinstance(item, int) and not isinstance(item, bool):
                cleaned[str(key)] = item
            elif isinstance(item, float):
                cleaned[str(key)] = item
            else:
                text = str(item)
                if len(text) > _PRIOR_ATTEMPT_MAX_STRING_CHARS:
                    raise ValueError("prior_attempts string values exceed the bound")
                cleaned[str(key)] = text
        normalized.append(cleaned)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > _PRIOR_ATTEMPTS_MAX_JSON_BYTES:
        raise ValueError("prior_attempts serialized size exceeds the bound")
    return normalized


def _apply_band_diagnostics(
    band: ArabicPrintedOcrBandCheckpoint,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None,
) -> list[str]:
    payload = _normalize_diagnostics_payload(diagnostics)
    update_fields: list[str] = []
    for field_name, value in payload.items():
        if field_name == "prior_attempts":
            value = _validate_prior_attempts(value)
        elif field_name in {
            "primary_safe_diagnostics",
            "fallback_safe_diagnostics",
            "cancel_safe_diagnostics",
        } and isinstance(value, str):
            value = _truncate_diagnostic(value)
        elif field_name == "cancel_confirmed_status" and isinstance(value, str):
            value = _normalized_cancel_confirmed_status(value)[:64]
        elif field_name in {
            "primary_interaction_id",
            "primary_provider_status",
            "primary_failure_type",
            "fallback_interaction_id",
            "fallback_provider_status",
            "fallback_failure_type",
        } and isinstance(value, str):
            value = (
                value.strip()[:128]
                if field_name.endswith("interaction_id")
                else value.strip()[:64]
            )
        setattr(band, field_name, value)
        update_fields.append(field_name)
    return update_fields


def _validate_vision_plan(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    bands: Sequence[ArabicPrintedBandPlan],
) -> None:
    if not 1 <= len(bands) <= ARABIC_PRINTED_MAX_BANDS:
        raise ValueError("Vision plan requires 1 to 6 contiguous bands")
    if [band.band_index for band in bands] != list(range(len(bands))):
        raise ValueError("Vision plan bands must be contiguous and zero-based")
    page_width = checkpoint.oriented_image_width
    page_height = checkpoint.oriented_image_height
    previous_bottom = 0
    for index, band in enumerate(bands):
        if band.rect_x != 0 or band.rect_width != page_width:
            raise ValueError("Vision plan bands must be full width")
        if band.rect_y < 0 or band.rect_height < 1:
            raise ValueError("Band rectangle coordinates must be nonnegative with positive size")
        if band.rect_y + band.rect_height > page_height:
            raise ValueError("Band rectangle is outside the oriented page dimensions")
        if index == 0 and band.rect_y < 0:
            raise ValueError("Band rectangle is outside the oriented page dimensions")
        if index > 0 and band.rect_y < previous_bottom:
            raise ValueError("Vision plan bands must be vertically ordered and non-overlapping")
        previous_bottom = band.rect_y + band.rect_height
        if band.crop_byte_length < 1 or len(band.crop_sha256) != 64:
            raise ValueError("Vision plan bands require crop hash and byte length")
        if band.crop_mime != ARABIC_PRINTED_CROP_MIME:
            raise ValueError("Vision plan crop MIME must match the banding contract")
        expected_draft_sha = _utf8_sha256(band.vision_draft_text)
        expected_draft_len = _utf8_byte_length(band.vision_draft_text)
        if (
            band.vision_draft_sha256 != expected_draft_sha
            or band.vision_draft_byte_length != expected_draft_len
        ):
            raise ValueError("Vision draft hash metadata does not match draft text")


def _stored_plan_matches(
    checkpoint: ArabicPrintedOcrPageCheckpoint,
    stored: Sequence[ArabicPrintedOcrBandCheckpoint],
    planned: Sequence[ArabicPrintedBandPlan],
    *,
    cloud_vision_response_sha256: str,
) -> None:
    if checkpoint.cloud_vision_response_sha256 != cloud_vision_response_sha256:
        raise ArabicPrintedIdentityMismatchError(
            "Persisted Vision response hash does not match the new plan"
        )
    if checkpoint.band_count != len(planned) or len(stored) != len(planned):
        raise ArabicPrintedIdentityMismatchError(
            "Persisted band count does not match the new plan"
        )
    if checkpoint.oriented_image_width < 1 or checkpoint.oriented_image_height < 1:
        raise ArabicPrintedIdentityMismatchError(
            "Persisted oriented dimensions do not match the new plan"
        )
    for existing, band in zip(stored, planned, strict=True):
        if (
            existing.band_index != band.band_index
            or existing.rect_x != band.rect_x
            or existing.rect_y != band.rect_y
            or existing.rect_width != band.rect_width
            or existing.rect_height != band.rect_height
            or existing.crop_mime != band.crop_mime
            or existing.crop_byte_length != band.crop_byte_length
            or existing.crop_sha256 != band.crop_sha256
            or existing.vision_draft_text != band.vision_draft_text
            or existing.vision_draft_byte_length != band.vision_draft_byte_length
            or existing.vision_draft_sha256 != band.vision_draft_sha256
        ):
            raise ArabicPrintedIdentityMismatchError(
                "Persisted band plan does not match the new plan"
            )


def reserve_arabic_printed_vision_call(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
) -> ArabicPrintedOcrPageCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="vision reservation",
            )
            if _durable_vision_plan_exists(checkpoint):
                raise ValueError("Vision call already reserved for this page")
            if checkpoint.cloud_vision_call_count != 0:
                raise ValueError(
                    "Ambiguous Vision reservation cannot be repeated"
                )
            if (
                checkpoint.band_count != 0
                or checkpoint.cloud_vision_response_sha256
                or checkpoint.band_checkpoints.exists()
            ):
                raise ValueError(
                    "Ambiguous Vision reservation cannot be repeated"
                )
            checkpoint.cloud_vision_call_count = 1
            checkpoint.save(
                update_fields=["cloud_vision_call_count", "updated_at"],
            )
            return checkpoint
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("reserve_vision", None, exc)


def persist_arabic_printed_vision_plan(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    cloud_vision_response_sha256: str,
    bands: Sequence[ArabicPrintedBandPlan],
) -> list[ArabicPrintedOcrBandCheckpoint]:
    if len(cloud_vision_response_sha256) != 64:
        raise ValueError("Vision response hash must be a SHA-256 hex digest")
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="vision plan",
            )
            if checkpoint.cloud_vision_call_count != 1:
                raise ValueError("Vision plan requires an existing Vision reservation")
            _validate_vision_plan(checkpoint, bands)
            existing = list(
                checkpoint.band_checkpoints.select_for_update().order_by("band_index")
            )
            if existing or checkpoint.band_count or checkpoint.cloud_vision_response_sha256:
                _stored_plan_matches(
                    checkpoint,
                    existing,
                    bands,
                    cloud_vision_response_sha256=cloud_vision_response_sha256,
                )
                return existing

            created = [
                ArabicPrintedOcrBandCheckpoint(
                    page_checkpoint=checkpoint,
                    band_index=band.band_index,
                    rect_x=band.rect_x,
                    rect_y=band.rect_y,
                    rect_width=band.rect_width,
                    rect_height=band.rect_height,
                    crop_mime=band.crop_mime,
                    crop_byte_length=band.crop_byte_length,
                    crop_sha256=band.crop_sha256,
                    vision_draft_text=band.vision_draft_text,
                    vision_draft_byte_length=band.vision_draft_byte_length,
                    vision_draft_sha256=band.vision_draft_sha256,
                    status=ArabicPrintedOcrBandCheckpoint.Status.PENDING,
                )
                for band in bands
            ]
            ArabicPrintedOcrBandCheckpoint.objects.bulk_create(created)
            checkpoint.cloud_vision_response_sha256 = cloud_vision_response_sha256
            checkpoint.band_count = len(bands)
            checkpoint.save(
                update_fields=[
                    "cloud_vision_response_sha256",
                    "band_count",
                    "updated_at",
                ]
            )
            return list(
                checkpoint.band_checkpoints.select_for_update().order_by("band_index")
            )
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("persist_vision_plan", None, exc)


def apply_arabic_printed_band_diagnostics(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="band diagnostics",
            )
            band = _locked_band(checkpoint, band_index)
            update_fields = _apply_band_diagnostics(band, diagnostics)
            if update_fields:
                band.save(update_fields=[*update_fields, "updated_at"])
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("band_diagnostics", None, exc)


def reserve_arabic_printed_primary_create(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="primary reservation",
            )
            band = _locked_band(checkpoint, band_index)
            if band.create_call_count != 0:
                raise ValueError("Primary create already reserved for this band")
            if band.status not in {
                ArabicPrintedOcrBandCheckpoint.Status.PENDING,
                ArabicPrintedOcrBandCheckpoint.Status.FAILED,
            }:
                raise ValueError("Primary reservation requires a pending or failed band")
            band.status = ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
            band.create_call_count = 1
            band.completed_at = None
            band.failure_code = ""
            band.failure_message = ""
            band.selected_result = ""
            band.transcription_text = None
            band.transcription_sha256 = ""
            band.transcription_byte_length = None
            band.save(
                update_fields=[
                    "status",
                    "create_call_count",
                    "completed_at",
                    "failure_code",
                    "failure_message",
                    "selected_result",
                    "transcription_text",
                    "transcription_sha256",
                    "transcription_byte_length",
                    "updated_at",
                ]
            )
            _refresh_page_create_count(checkpoint)
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("reserve_primary", None, exc)


def _in_flight_interaction_id(band: ArabicPrintedOcrBandCheckpoint) -> str:
    if (
        band.status == ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
        and band.create_call_count == 1
    ):
        return band.primary_interaction_id.strip()
    if (
        band.status == ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING
        and band.create_call_count == 2
    ):
        return band.fallback_interaction_id.strip()
    return ""


def mark_arabic_printed_band_cancel_pending(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
    cancel_attempted_at: datetime | None = None,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None = None,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="band cancel",
            )
            band = _locked_band(checkpoint, band_index)
            diagnostic_fields = _apply_band_diagnostics(band, diagnostics)
            if band.status not in {
                ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
                ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
            }:
                raise ValueError("Cancel pending requires a reserved primary or fallback")
            if (
                band.status == ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
                and band.create_call_count != 1
            ):
                raise ValueError("Cancel pending requires primary create count 1")
            if (
                band.status == ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING
                and band.create_call_count != 2
            ):
                raise ValueError("Cancel pending requires fallback create count 2")
            if not _in_flight_interaction_id(band):
                raise ValueError(
                    "Cannot mark cancel pending without the in-flight interaction id"
                )
            band.status = ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING
            band.cancel_attempted = True
            band.cancel_attempted_at = cancel_attempted_at or timezone.now()
            band.completed_at = None
            update_fields = [
                "status",
                "cancel_attempted",
                "cancel_attempted_at",
                "completed_at",
                "updated_at",
                *diagnostic_fields,
            ]
            band.save(update_fields=list(dict.fromkeys(update_fields)))
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("band_cancel", None, exc)


def _fallback_path_is_allowed(band: ArabicPrintedOcrBandCheckpoint) -> bool:
    if band.status == ArabicPrintedOcrBandCheckpoint.Status.FAILED:
        return bool(band.failure_code.strip())
    if band.status == ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING:
        return (
            band.cancel_attempted
            and band.cancel_attempted_at is not None
            and _cancel_confirmed_cancelled(band)
        )
    return False


def reserve_arabic_printed_fallback_create(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="fallback reservation",
            )
            band = _locked_band(checkpoint, band_index)
            if band.create_call_count != 1:
                raise ValueError("Fallback create already reserved or primary missing")
            if not _fallback_path_is_allowed(band):
                raise ValueError(
                    "Fallback reservation requires a terminal or cancel-confirmed primary"
                )
            band.status = ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING
            band.create_call_count = 2
            band.completed_at = None
            band.failure_code = ""
            band.failure_message = ""
            band.selected_result = ""
            band.transcription_text = None
            band.transcription_sha256 = ""
            band.transcription_byte_length = None
            band.save(
                update_fields=[
                    "status",
                    "create_call_count",
                    "completed_at",
                    "failure_code",
                    "failure_message",
                    "selected_result",
                    "transcription_sha256",
                    "transcription_text",
                    "transcription_byte_length",
                    "updated_at",
                ]
            )
            _refresh_page_create_count(checkpoint)
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("reserve_fallback", None, exc)


def persist_arabic_printed_band_success(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
    selected_result: str,
    transcription_text: str,
    transcription_sha256: str,
    transcription_byte_length: int,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None = None,
) -> ArabicPrintedOcrBandCheckpoint:
    normalized = transcription_text.strip()
    if not normalized:
        raise ValueError("Cannot persist empty successful Arabic printed band text")
    if selected_result not in {
        ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
        ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK,
    }:
        raise ValueError("Band success requires an Antigravity selected result")
    expected_sha = _utf8_sha256(normalized)
    expected_len = _utf8_byte_length(normalized)
    if transcription_sha256 != expected_sha or transcription_byte_length != expected_len:
        raise ValueError("Band transcription hash metadata does not match text")
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="band success",
            )
            band = _locked_band(checkpoint, band_index)
            if selected_result == ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED:
                if band.status not in {
                    ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
                    ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
                }:
                    raise ValueError("UNASSISTED requires a reserved primary")
                if band.create_call_count != 1:
                    raise ValueError("UNASSISTED requires create_call_count 1")
            else:
                if band.create_call_count != 2:
                    raise ValueError("ASSISTED_FALLBACK requires create_call_count 2")
                if band.status not in {
                    ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
                    ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
                }:
                    raise ValueError("ASSISTED_FALLBACK requires a reserved fallback")
            band.status = ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
            band.selected_result = selected_result
            band.transcription_text = normalized
            band.transcription_sha256 = expected_sha
            band.transcription_byte_length = expected_len
            band.failure_code = ""
            band.failure_message = ""
            band.completed_at = timezone.now()
            update_fields = [
                "status",
                "selected_result",
                "transcription_text",
                "transcription_sha256",
                "transcription_byte_length",
                "failure_code",
                "failure_message",
                "completed_at",
                "updated_at",
            ]
            update_fields.extend(_apply_band_diagnostics(band, diagnostics))
            band.save(update_fields=list(dict.fromkeys(update_fields)))
            _refresh_page_create_count(checkpoint)
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("band_success", None, exc)


def _low_quality_path_is_allowed(band: ArabicPrintedOcrBandCheckpoint) -> bool:
    if band.status == ArabicPrintedOcrBandCheckpoint.Status.FAILED:
        return True
    if band.status == ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING:
        return (
            band.cancel_attempted
            and band.cancel_attempted_at is not None
            and _cancel_confirmed_cancelled(band)
        )
    return False


def select_arabic_printed_band_cloud_vision_low_quality(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None = None,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="band low quality",
            )
            band = _locked_band(checkpoint, band_index)
            if band.create_call_count > ARABIC_PRINTED_MAX_CREATES_PER_BAND:
                raise ValueError("Low-quality selection requires create_call_count 0..2")
            if not _low_quality_path_is_allowed(band):
                raise ValueError(
                    "CLOUD_VISION_LOW_QUALITY requires a failed band or confirmed cancellation"
                )
            stored_draft = band.vision_draft_text
            expected_draft_sha = _utf8_sha256(stored_draft)
            expected_draft_len = _utf8_byte_length(stored_draft)
            if (
                band.vision_draft_sha256 != expected_draft_sha
                or band.vision_draft_byte_length != expected_draft_len
            ):
                raise ValueError("Stored Vision draft hash metadata does not match draft text")
            normalized = stored_draft.strip()
            if not normalized:
                raise ValueError("Stored Vision draft is empty")
            band.status = ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
            band.selected_result = (
                ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY
            )
            band.transcription_text = normalized
            band.transcription_sha256 = _utf8_sha256(normalized)
            band.transcription_byte_length = _utf8_byte_length(normalized)
            band.failure_code = ""
            band.failure_message = ""
            band.completed_at = timezone.now()
            update_fields = [
                "status",
                "selected_result",
                "transcription_text",
                "transcription_sha256",
                "transcription_byte_length",
                "failure_code",
                "failure_message",
                "completed_at",
                "updated_at",
            ]
            update_fields.extend(_apply_band_diagnostics(band, diagnostics))
            band.save(update_fields=list(dict.fromkeys(update_fields)))
            _refresh_page_create_count(checkpoint)
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("band_low_quality", None, exc)


def persist_arabic_printed_band_failure(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    band_index: int,
    failure_code: str,
    failure_message: str,
    diagnostics: Mapping[str, Any] | ArabicPrintedBandSafeDiagnostics | None = None,
) -> ArabicPrintedOcrBandCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = _locked_page(
                checkpoint_id,
                lease_token,
                operation="band failure",
            )
            band = _locked_band(checkpoint, band_index)
            if band.status not in {
                ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
                ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
                ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
            }:
                raise ValueError("Band failure requires an in-flight reserved attempt")
            band.status = ArabicPrintedOcrBandCheckpoint.Status.FAILED
            band.selected_result = ""
            band.transcription_text = None
            band.transcription_sha256 = ""
            band.transcription_byte_length = None
            band.failure_code = _safe_failure_code(
                failure_code,
                default="ARABIC_PRINTED_BAND_FAILED",
            )
            band.failure_message = _safe_failure_message(failure_message)
            band.completed_at = timezone.now()
            update_fields = [
                "status",
                "selected_result",
                "transcription_text",
                "transcription_sha256",
                "transcription_byte_length",
                "failure_code",
                "failure_message",
                "completed_at",
                "updated_at",
            ]
            update_fields.extend(_apply_band_diagnostics(band, diagnostics))
            band.save(update_fields=list(dict.fromkeys(update_fields)))
            _refresh_page_create_count(checkpoint)
            return band
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("band_failure", None, exc)


def _rollup_page_quality(selected: Sequence[str]) -> str:
    if any(
        value
        == ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY
        for value in selected
    ):
        return ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY
    unique = set(selected)
    if unique == {ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED}:
        return ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED
    if unique == {ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK}:
        return ArabicPrintedOcrPageCheckpoint.PageQuality.ASSISTED
    return ArabicPrintedOcrPageCheckpoint.PageQuality.MIXED


def _runtime_engine_marker(selected: Sequence[str]) -> str:
    quality = _rollup_page_quality(selected)
    if quality == ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED:
        marker = "antigravity-banded:unassisted"
    elif quality == ArabicPrintedOcrPageCheckpoint.PageQuality.ASSISTED:
        marker = "antigravity-banded:assisted"
    else:
        mapping = [
            {"band_index": index, "selected": value}
            for index, value in enumerate(selected)
        ]
        digest = _canonical_sha256(mapping)[:ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN]
        if quality == ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY:
            marker = f"antigravity-banded:cloud-vision-lq:{digest}"
        else:
            marker = f"antigravity-banded:mixed:{digest}"
    if len(marker) > 64:
        raise RuntimeError("Arabic printed runtime engine marker exceeds 64 characters")
    return marker


def assemble_arabic_printed_page(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
) -> ArabicPrintedOcrPageCheckpoint:
    try:
        with transaction.atomic():
            checkpoint = (
                ArabicPrintedOcrPageCheckpoint.objects.select_for_update()
                .select_related("attempt")
                .get(pk=checkpoint_id)
            )
            _require_fresh_page_claim(checkpoint, lease_token, operation="page success")
            bands = list(
                checkpoint.band_checkpoints.select_for_update().order_by("band_index")
            )
            if not bands or len(bands) != checkpoint.band_count:
                raise ValueError("Cannot assemble page with missing bands")
            expected_indices = list(range(checkpoint.band_count))
            actual_indices = [band.band_index for band in bands]
            if actual_indices != expected_indices:
                raise ValueError("Cannot assemble page with missing bands")
            if any(
                band.status != ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED
                for band in bands
            ):
                raise ValueError("Cannot assemble page with missing or failed bands")
            selected = [band.selected_result for band in bands]
            texts = [(band.transcription_text or "").strip() for band in bands]
            if any(not text for text in texts):
                raise ValueError("Cannot assemble page with empty band transcriptions")
            assembled = "\n".join(texts)
            checkpoint.status = ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED
            checkpoint.lease_token = None
            checkpoint.lease_expires_at = None
            checkpoint.assembled_text = assembled
            checkpoint.page_quality = _rollup_page_quality(selected)
            checkpoint.runtime_engine_marker = _runtime_engine_marker(selected)
            checkpoint.failure_code = ""
            checkpoint.failure_message = ""
            checkpoint.completed_at = timezone.now()
            checkpoint.save(
                update_fields=[
                    "status",
                    "lease_token",
                    "lease_expires_at",
                    "assembled_text",
                    "page_quality",
                    "runtime_engine_marker",
                    "failure_code",
                    "failure_message",
                    "completed_at",
                    "updated_at",
                ]
            )
            attempt = ArabicPrintedOcrAttempt.objects.select_for_update().get(
                pk=checkpoint.attempt_id
            )
            _rollup_attempt_locked(attempt)
            return checkpoint
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("page_success", None, exc)


def persist_arabic_printed_page_failure(
    *,
    checkpoint_id: int,
    lease_token: uuid.UUID,
    failure_code: str,
    failure_message: str,
) -> list[int]:
    try:
        with transaction.atomic():
            checkpoint = (
                ArabicPrintedOcrPageCheckpoint.objects.select_for_update()
                .select_related("attempt")
                .get(pk=checkpoint_id)
            )
            _require_fresh_page_claim(checkpoint, lease_token, operation="page failure")
            checkpoint.status = ArabicPrintedOcrPageCheckpoint.Status.FAILED
            checkpoint.lease_token = None
            checkpoint.lease_expires_at = None
            checkpoint.assembled_text = None
            checkpoint.page_quality = ""
            checkpoint.runtime_engine_marker = ""
            checkpoint.failure_code = _safe_failure_code(
                failure_code,
                default="ARABIC_PRINTED_PAGE_FAILED",
            )
            checkpoint.failure_message = _safe_failure_message(failure_message)
            checkpoint.completed_at = timezone.now()
            checkpoint.save(
                update_fields=[
                    "status",
                    "lease_token",
                    "lease_expires_at",
                    "assembled_text",
                    "page_quality",
                    "runtime_engine_marker",
                    "failure_code",
                    "failure_message",
                    "completed_at",
                    "updated_at",
                ]
            )
            attempt = ArabicPrintedOcrAttempt.objects.select_for_update().get(
                pk=checkpoint.attempt_id
            )
            return _rollup_attempt_locked(attempt)
    except (
        StaleArabicPrintedPageClaimError,
        ArabicPrintedIdentityMismatchError,
        ValueError,
        ArabicPrintedOcrPageCheckpoint.DoesNotExist,
    ):
        raise
    except DatabaseError as exc:
        _raise_retryable("page_failure", None, exc)


def rollup_arabic_printed_attempt(*, attempt_id: int) -> list[int]:
    try:
        with transaction.atomic():
            attempt = ArabicPrintedOcrAttempt.objects.select_for_update().get(
                pk=attempt_id
            )
            return _rollup_attempt_locked(attempt)
    except ArabicPrintedOcrAttempt.DoesNotExist:
        raise
    except DatabaseError as exc:
        _raise_retryable("attempt_rollup", None, exc)


def missing_pages_for_arabic_printed_attempt(attempt_id: int) -> list[int]:
    attempt = ArabicPrintedOcrAttempt.objects.get(pk=attempt_id)
    return _missing_page_indices_locked(attempt)
