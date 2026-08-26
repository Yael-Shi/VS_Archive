from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, NoReturn

import requests

from documents.services.antigravity_defaults import (
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    DEFAULT_POLL_GET_TIMEOUT_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INTERACTIONS_API_REVISION,
    INTERACTIONS_BASE_URL,
)
from documents.services.antigravity_ocr_contract import (
    OcrContractError,
    build_antigravity_ocr_prompt,
    extract_final_model_output_text,
    render_validated_ocr_text,
    validate_antigravity_ocr_output,
)
from documents.services.page_extraction import PageImage

logger = logging.getLogger(__name__)
COMPLETED_STATUS = "completed"
IN_PROGRESS_STATUS = "in_progress"
USER_INPUT_STEP_TYPE = "user_input"
IMAGE_CONTENT_TYPE = "image"
RESPONSE_SOURCE_CREATE = "create"
RESPONSE_SOURCE_POLL_GET = "poll_get"
UNEXPECTED_TOKEN = "other"
INVALID_LOG_VALUE = "invalid"
_DATA_URL_PREFIX = "data:"
_JPEG_SOURCE_MIMES = frozenset({"image/jpeg", "image/jpg"})
OUTBOUND_JPEG_MIME = "image/jpeg"
_OUTBOUND_SHA256_PREFIX_LEN = 16
_MAX_SANITIZED_STRING = 128
_MAX_LOG_LIST_ITEMS = 32
_USAGE_TOTAL_FIELDS = (
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_tool_use_tokens",
    "total_tokens",
)
_KNOWN_STATUSES = frozenset({"completed", "in_progress", "failed", "cancelled"})
_KNOWN_STEP_TYPES = frozenset(
    {
        "user_input",
        "model_output",
        "thought",
        "tool_call",
        "tool_result",
        "function_call",
        "function_result",
        "code_execution_call",
        "code_execution_result",
    }
)
_KNOWN_CONTENT_TYPES = frozenset({"text", "image", "audio", "video", "document"})
_KNOWN_MODALITIES = frozenset({"text", "image", "audio", "video", "document"})
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_INTERACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_POLL_RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_POLL_HTTP_RETRY_BASE_SECONDS = 1.0
_POLL_HTTP_RETRY_MAX_SECONDS = 30.0
_POLL_HTTP_RETRY_JITTER_RATIO = 0.1


class AntigravityError(RuntimeError):
    """Raised when Antigravity Interactions OCR fails."""


class AntigravityHttpError(AntigravityError):
    """HTTP error from the Antigravity Interactions API."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class AntigravityOutputValidationError(AntigravityError):
    """Provider completed, but OCR output failed structural validation."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AntigravityResult:
    text: str
    engine_name: str
    needs_review: bool = True


def antigravity_outbound_image(page: PageImage) -> tuple[bytes, str]:
    """
    Bytes and MIME actually sent to Antigravity.

    Original JPEG/JPG is forwarded unchanged with canonical ``image/jpeg``.
    PDF-rendered pages, PNG, and other non-JPEG originals use normalized PNG.
    """
    original_bytes = page.original_image_bytes
    original_mime = (page.original_mime_type or "").strip().lower()
    if original_bytes and original_mime in _JPEG_SOURCE_MIMES:
        return original_bytes, OUTBOUND_JPEG_MIME
    return page.image_bytes, page.mime_type or "image/png"


def _outbound_sha256_prefix(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()[:_OUTBOUND_SHA256_PREFIX_LEN]


def _sorted_pages(pages: list[PageImage]) -> list[PageImage]:
    return sorted(pages, key=lambda page: page.page_index)


def _request_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Api-Revision": INTERACTIONS_API_REVISION,
    }


def _raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    message = response.text
    try:
        payload = response.json()
        error = payload.get("error") or {}
        message = error.get("message") or message
    except ValueError:
        pass
    raise AntigravityHttpError(
        f"HTTP {response.status_code}: {message}",
        status_code=response.status_code,
    )


def output_text_from_steps(steps: list[dict[str, Any]] | None) -> str | None:
    """Extract text from the last model_output step only."""
    return extract_final_model_output_text(steps)


def _raise_output_validation_error(
    exc: OcrContractError,
    *,
    document_id: int | None,
) -> NoReturn:
    logger.warning(
        "Antigravity OCR output validation failed document_id=%s reason=%s details=%s",
        document_id,
        exc.reason,
        exc.details,
    )
    raise AntigravityOutputValidationError(str(exc), reason=exc.reason) from exc


def build_multimodal_input(prompt: str, pages: list[PageImage]) -> list[dict[str, Any]]:
    input_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for page in pages:
        image_bytes, mime_type = antigravity_outbound_image(page)
        input_blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(image_bytes).decode("ascii"),
                "mime_type": mime_type,
            }
        )
    return input_blocks


def _elapsed_seconds(started_at: float, now: float) -> float:
    return round(max(0.0, now - started_at), 3)


def _poll_http_retry_delay_seconds(
    retry_count: int,
    *,
    random_fn=random.random,
) -> float:
    exponent = max(0, retry_count - 1)
    base_delay = min(
        _POLL_HTTP_RETRY_BASE_SECONDS * (2**exponent),
        _POLL_HTTP_RETRY_MAX_SECONDS,
    )
    jitter_span = _POLL_HTTP_RETRY_JITTER_RATIO * base_delay
    jitter = (float(random_fn()) * 2.0 - 1.0) * jitter_span
    return max(0.0, base_delay + jitter)


def _sanitize_interaction_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if _INTERACTION_ID_RE.fullmatch(value):
        return value
    return INVALID_LOG_VALUE


def _sanitize_environment_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if _CONTROL_CHARS_RE.search(value):
        return INVALID_LOG_VALUE
    if len(value) > _MAX_SANITIZED_STRING:
        return INVALID_LOG_VALUE
    return value


def _known_or_other(value: Any, known: frozenset[str]) -> str:
    if isinstance(value, str) and value in known:
        return value
    return UNEXPECTED_TOKEN


def _bounded_token_list(
    values: list[Any],
    *,
    known: frozenset[str],
) -> tuple[list[str], int, bool]:
    total = len(values)
    bounded = [_known_or_other(value, known) for value in values[:_MAX_LOG_LIST_ITEMS]]
    return bounded, total, total > _MAX_LOG_LIST_ITEMS


def _coerce_token_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _normalize_input_tokens_by_modality(value: Any) -> list[dict[str, int | str]]:
    normalized: list[dict[str, int | str]] = []
    if not isinstance(value, list):
        return normalized
    for item in value:
        if not isinstance(item, dict):
            continue
        tokens = _coerce_token_count(item.get("tokens"))
        if tokens is None:
            continue
        normalized.append(
            {
                "modality": _known_or_other(item.get("modality"), _KNOWN_MODALITIES),
                "tokens": tokens,
            }
        )
    return normalized


def _empty_interaction_summary() -> dict[str, Any]:
    return {
        "interaction_id": None,
        "status": None,
        "environment_id": None,
        "step_types": [],
        "step_types_total": 0,
        "step_types_truncated": False,
        "user_input_content_types": [],
        "user_input_content_types_total": 0,
        "user_input_content_types_truncated": False,
        "stored_image_content_count": 0,
        "input_tokens_by_modality": [],
        "input_tokens_by_modality_total": 0,
        "input_tokens_by_modality_truncated": False,
        "total_input_tokens": None,
        "total_output_tokens": None,
        "total_thought_tokens": None,
        "total_tool_use_tokens": None,
        "total_tokens": None,
    }


def summarize_antigravity_interaction(interaction: Any) -> dict[str, Any]:
    """Return an allowlisted, privacy-safe summary of an interaction response."""
    empty = _empty_interaction_summary()
    try:
        if not isinstance(interaction, dict):
            return empty

        raw_step_types: list[Any] = []
        raw_user_input_content_types: list[Any] = []
        stored_image_content_count = 0
        steps = interaction.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    raw_step_types.append(None)
                    continue
                raw_step_types.append(step.get("type"))
                if step.get("type") != USER_INPUT_STEP_TYPE:
                    continue
                content_items = step.get("content")
                if not isinstance(content_items, list):
                    continue
                for content in content_items:
                    if not isinstance(content, dict):
                        continue
                    content_type = content.get("type")
                    raw_user_input_content_types.append(content_type)
                    if content_type == IMAGE_CONTENT_TYPE:
                        stored_image_content_count += 1

        step_types, step_types_total, step_types_truncated = _bounded_token_list(
            raw_step_types,
            known=_KNOWN_STEP_TYPES,
        )
        content_types, content_types_total, content_types_truncated = (
            _bounded_token_list(
                raw_user_input_content_types,
                known=_KNOWN_CONTENT_TYPES,
            )
        )

        usage_totals: dict[str, int | None] = {
            field: None for field in _USAGE_TOTAL_FIELDS
        }
        input_tokens_by_modality: list[dict[str, int | str]] = []
        usage = interaction.get("usage")
        if isinstance(usage, dict):
            input_tokens_by_modality = _normalize_input_tokens_by_modality(
                usage.get("input_tokens_by_modality")
            )
            for field in _USAGE_TOTAL_FIELDS:
                usage_totals[field] = _coerce_token_count(usage.get(field))

        modality_total = len(input_tokens_by_modality)
        modality_truncated = modality_total > _MAX_LOG_LIST_ITEMS
        if modality_truncated:
            input_tokens_by_modality = input_tokens_by_modality[:_MAX_LOG_LIST_ITEMS]

        return {
            "interaction_id": _sanitize_interaction_id(interaction.get("id")),
            "status": _known_or_other(interaction.get("status"), _KNOWN_STATUSES)
            if interaction.get("status") is not None
            else None,
            "environment_id": _sanitize_environment_id(
                interaction.get("environment_id")
            ),
            "step_types": step_types,
            "step_types_total": step_types_total,
            "step_types_truncated": step_types_truncated,
            "user_input_content_types": content_types,
            "user_input_content_types_total": content_types_total,
            "user_input_content_types_truncated": content_types_truncated,
            "stored_image_content_count": stored_image_content_count,
            "input_tokens_by_modality": input_tokens_by_modality,
            "input_tokens_by_modality_total": modality_total,
            "input_tokens_by_modality_truncated": modality_truncated,
            **usage_totals,
        }
    except Exception:
        return empty


def _decode_outbound_image_bytes(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        try:
            return base64.b64decode(data)
        except (ValueError, binascii.Error):
            return None


def _log_encoded_image_blocks(
    *,
    document_id: int | None,
    pages: list[PageImage],
    input_blocks: list[dict[str, Any]],
) -> None:
    try:
        page_offset = 0
        for block in input_blocks:
            if not isinstance(block, dict) or block.get("type") != IMAGE_CONTENT_TYPE:
                continue
            page = pages[page_offset] if page_offset < len(pages) else None
            page_offset += 1
            data = block.get("data")
            data_is_str = isinstance(data, str)
            outbound_bytes = None
            if data_is_str and data and not data.startswith(_DATA_URL_PREFIX):
                outbound_bytes = _decode_outbound_image_bytes(data)
            logger.info(
                "Antigravity image block document_id=%s page_index=%s "
                "outbound_mime_type=%s outbound_byte_length=%s outbound_sha256=%s "
                "base64_character_length=%s data_is_str=%s data_nonempty=%s "
                "starts_with_data_url_prefix=%s",
                document_id,
                page.page_index if page is not None else None,
                block.get("mime_type"),
                len(outbound_bytes) if outbound_bytes is not None else None,
                (
                    _outbound_sha256_prefix(outbound_bytes)
                    if outbound_bytes is not None
                    else None
                ),
                len(data) if data_is_str else None,
                data_is_str,
                data_is_str and bool(data),
                data_is_str and data.startswith(_DATA_URL_PREFIX),
            )
    except Exception:
        logger.warning(
            "Antigravity image block metadata unavailable document_id=%s",
            document_id,
        )


def _log_interaction_summary(
    interaction: Any,
    *,
    document_id: int | None,
    response_source: str,
) -> None:
    summary = summarize_antigravity_interaction(interaction)
    logger.info(
        "Antigravity interaction summary document_id=%s response_source=%s "
        "interaction_id=%s status=%s environment_id=%s step_types=%s "
        "step_types_total=%s step_types_truncated=%s "
        "user_input_content_types=%s user_input_content_types_total=%s "
        "user_input_content_types_truncated=%s stored_image_content_count=%s "
        "input_tokens_by_modality=%s input_tokens_by_modality_total=%s "
        "input_tokens_by_modality_truncated=%s total_input_tokens=%s "
        "total_output_tokens=%s total_thought_tokens=%s "
        "total_tool_use_tokens=%s total_tokens=%s",
        document_id,
        response_source,
        summary.get("interaction_id"),
        summary.get("status"),
        summary.get("environment_id"),
        summary.get("step_types"),
        summary.get("step_types_total"),
        summary.get("step_types_truncated"),
        summary.get("user_input_content_types"),
        summary.get("user_input_content_types_total"),
        summary.get("user_input_content_types_truncated"),
        summary.get("stored_image_content_count"),
        summary.get("input_tokens_by_modality"),
        summary.get("input_tokens_by_modality_total"),
        summary.get("input_tokens_by_modality_truncated"),
        summary.get("total_input_tokens"),
        summary.get("total_output_tokens"),
        summary.get("total_thought_tokens"),
        summary.get("total_tool_use_tokens"),
        summary.get("total_tokens"),
    )


def _create_interaction(
    api_key: str,
    *,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    response = requests.post(
        INTERACTIONS_BASE_URL,
        headers=_request_headers(api_key),
        json=payload,
        timeout=timeout_seconds,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise AntigravityError("Unexpected non-object Interactions API response")
    return body


def _get_interaction(api_key: str, interaction_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{INTERACTIONS_BASE_URL}/{interaction_id}",
        headers=_request_headers(api_key),
        timeout=DEFAULT_POLL_GET_TIMEOUT_SECONDS,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise AntigravityError("Unexpected non-object Interactions API response")
    return body


def _poll_until_done(
    api_key: str,
    interaction: dict[str, Any],
    *,
    poll_seconds: float,
    timeout_seconds: float,
    document_id: int | None = None,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    random_fn=random.random,
) -> tuple[dict[str, Any], int]:
    interaction_id = interaction.get("id")
    if not interaction_id:
        raise AntigravityError("Interaction response missing id")

    poll_started = monotonic_fn()
    deadline = poll_started + timeout_seconds
    current = interaction
    poll_attempts = 0
    poll_successes = 0
    poll_transport_timeouts = 0
    poll_http_retries = 0
    skip_poll_interval = False
    last_status = current.get("status")
    while current.get("status") == IN_PROGRESS_STATUS:
        now = monotonic_fn()
        if now >= deadline:
            logger.warning(
                "Antigravity interaction lifecycle document_id=%s phase=poll_deadline "
                "interaction_id=%s poll_attempts=%s poll_successes=%s "
                "poll_transport_timeouts=%s poll_http_retries=%s "
                "elapsed_seconds=%.3f last_status=%s",
                document_id,
                _sanitize_interaction_id(interaction_id),
                poll_attempts,
                poll_successes,
                poll_transport_timeouts,
                poll_http_retries,
                _elapsed_seconds(poll_started, now),
                _known_or_other(last_status, _KNOWN_STATUSES)
                if last_status is not None
                else None,
            )
            raise AntigravityError(
                f"Timed out after {timeout_seconds}s waiting for interaction {interaction_id}"
            )
        if skip_poll_interval:
            skip_poll_interval = False
        else:
            sleep_fn(poll_seconds)
        poll_attempts += 1
        try:
            current = _get_interaction(api_key, interaction_id)
        except requests.Timeout as exc:
            poll_transport_timeouts += 1
            logger.warning(
                "Antigravity interaction lifecycle document_id=%s phase=poll "
                "interaction_id=%s attempt=%s elapsed_seconds=%.3f "
                "exception_class=%s last_status=%s",
                document_id,
                _sanitize_interaction_id(interaction_id),
                poll_attempts,
                _elapsed_seconds(poll_started, monotonic_fn()),
                type(exc).__name__,
                _known_or_other(last_status, _KNOWN_STATUSES)
                if last_status is not None
                else None,
            )
            continue
        except AntigravityHttpError as exc:
            if exc.status_code not in _POLL_RETRYABLE_HTTP_STATUSES:
                raise
            poll_http_retries += 1
            now = monotonic_fn()
            remaining = deadline - now
            delay = 0.0
            if remaining > 0:
                delay = min(
                    _poll_http_retry_delay_seconds(
                        poll_http_retries,
                        random_fn=random_fn,
                    ),
                    remaining,
                )
            logger.warning(
                "Antigravity interaction lifecycle document_id=%s "
                "phase=poll_http_retry interaction_id=%s http_status=%s "
                "retry_count=%s delay_seconds=%.3f elapsed_seconds=%.3f",
                document_id,
                _sanitize_interaction_id(interaction_id),
                exc.status_code,
                poll_http_retries,
                delay,
                _elapsed_seconds(poll_started, now),
            )
            if remaining <= 0:
                continue
            sleep_fn(delay)
            skip_poll_interval = True
            continue
        poll_successes += 1
        last_status = current.get("status")
    if poll_attempts:
        logger.info(
            "Antigravity interaction lifecycle document_id=%s phase=poll "
            "interaction_id=%s poll_attempts=%s poll_successes=%s "
            "poll_transport_timeouts=%s poll_http_retries=%s "
            "elapsed_seconds=%.3f status=%s",
            document_id,
            _sanitize_interaction_id(interaction_id),
            poll_attempts,
            poll_successes,
            poll_transport_timeouts,
            poll_http_retries,
            _elapsed_seconds(poll_started, monotonic_fn()),
            _known_or_other(current.get("status"), _KNOWN_STATUSES)
            if current.get("status") is not None
            else None,
        )
    return current, poll_successes


def transcribe_pages_with_antigravity(
    pages: list[PageImage],
    *,
    api_key: str,
    agent_id: str = DEFAULT_ANTIGRAVITY_AGENT_ID,
    document_id: int | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    background: bool = True,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    random_fn=random.random,
) -> AntigravityResult:
    if not (api_key or "").strip():
        raise AntigravityError("Missing GEMINI_API_KEY")

    ordered_pages = _sorted_pages(pages)
    if not ordered_pages:
        raise AntigravityError("No page images supplied for Antigravity OCR")

    prompt = build_antigravity_ocr_prompt(len(ordered_pages))
    create_timeout = max(120.0, 30.0 * len(ordered_pages))
    multimodal_input = build_multimodal_input(prompt, ordered_pages)

    block_types = [
        block.get("type") for block in multimodal_input if isinstance(block, dict)
    ]
    image_block_count = block_types.count("image")

    logger.info(
        "Antigravity request payload document_id=%s agent=%s pages=%s "
        "input_blocks=%s image_blocks=%s block_types=%s phase=create "
        "create_timeout_seconds=%s",
        document_id,
        agent_id,
        len(ordered_pages),
        len(multimodal_input),
        image_block_count,
        block_types,
        create_timeout,
    )
    _log_encoded_image_blocks(
        document_id=document_id,
        pages=ordered_pages,
        input_blocks=multimodal_input,
    )

    create_started = monotonic_fn()
    try:
        interaction = _create_interaction(
            api_key,
            payload={
                "agent": agent_id,
                "input": multimodal_input,
                "environment": "remote",
                "background": background,
                "tool_choice": "none",
            },
            timeout_seconds=create_timeout,
        )
    except requests.Timeout as exc:
        logger.warning(
            "Antigravity interaction lifecycle document_id=%s phase=create "
            "elapsed_seconds=%.3f exception_class=%s pages=%s "
            "interaction_id_available=false",
            document_id,
            _elapsed_seconds(create_started, monotonic_fn()),
            type(exc).__name__,
            len(ordered_pages),
        )
        raise

    logger.info(
        "Antigravity interaction lifecycle document_id=%s phase=create "
        "elapsed_seconds=%.3f interaction_id=%s status=%s environment_id=%s",
        document_id,
        _elapsed_seconds(create_started, monotonic_fn()),
        _sanitize_interaction_id(interaction.get("id")),
        _known_or_other(interaction.get("status"), _KNOWN_STATUSES)
        if interaction.get("status") is not None
        else None,
        _sanitize_environment_id(interaction.get("environment_id")),
    )

    response_source = RESPONSE_SOURCE_CREATE
    if background or interaction.get("status") == IN_PROGRESS_STATUS:
        interaction, poll_successes = _poll_until_done(
            api_key,
            interaction,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
            document_id=document_id,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            random_fn=random_fn,
        )
        if poll_successes:
            response_source = RESPONSE_SOURCE_POLL_GET

    _log_interaction_summary(
        interaction,
        document_id=document_id,
        response_source=response_source,
    )

    status = interaction.get("status")
    if status != COMPLETED_STATUS:
        raise AntigravityError(
            f"Antigravity interaction finished with status={status!r}"
        )

    try:
        validated = validate_antigravity_ocr_output(
            interaction.get("steps"),
            expected_page_count=len(ordered_pages),
        )
    except OcrContractError as exc:
        _raise_output_validation_error(exc, document_id=document_id)

    return AntigravityResult(
        text=render_validated_ocr_text(validated),
        engine_name=agent_id,
    )
