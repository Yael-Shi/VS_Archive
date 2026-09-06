from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

import requests

from documents.services.antigravity_defaults import (
    ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS,
    ANTIGRAVITY_CANCEL_HTTP_TIMEOUT_SECONDS,
    ANTIGRAVITY_REMOTE_ENVIRONMENT,
    ANTIGRAVITY_REQUESTED_MODEL,
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    DEFAULT_POLL_GET_TIMEOUT_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INTERACTIONS_API_REVISION,
    INTERACTIONS_BASE_URL,
)
from documents.services.antigravity_interaction_id import (
    ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN,
    is_antigravity_interaction_id,
)
from documents.services.arabic_printed_ocr_contract import (
    COMPLETION_MARKER,
    evaluate_arabic_printed_band_output,
)
from documents.services.antigravity_ocr_contract import (
    OcrContractError,
    REASON_INVALID_JSON,
    ValidatedOcrOutput,
    ValidatedOcrPage,
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
class _OcrOutputShape:
    present: bool
    char_length: int
    stripped_char_length: int
    starts_with_open_brace: bool
    ends_with_close_brace: bool

    @property
    def structurally_truncated(self) -> bool:
        return (
            self.present
            and self.starts_with_open_brace
            and not self.ends_with_close_brace
        )


@dataclass
class _ProviderCallBudget:
    limit: int
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def consume(self) -> None:
        if self.used >= self.limit:
            raise AntigravityError(
                "Antigravity truncated-JSON recovery exceeded provider call budget"
            )
        self.used += 1


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


def build_antigravity_create_payload(
    *,
    agent_id: str,
    multimodal_input: list[dict[str, Any]],
    background: bool,
) -> dict[str, Any]:
    """Return the live-validated Antigravity Interactions create payload.

    Exact top-level keys: agent, input, environment, background, tools,
    agent_config. Does not send tool_choice, system_instruction,
    response_format, generation_config, or store.
    """
    return {
        "agent": agent_id,
        "input": multimodal_input,
        "environment": {
            "type": ANTIGRAVITY_REMOTE_ENVIRONMENT["type"],
            "network": ANTIGRAVITY_REMOTE_ENVIRONMENT["network"],
        },
        "background": background,
        "tools": [],
        "agent_config": {
            "type": "antigravity",
            "model": ANTIGRAVITY_REQUESTED_MODEL,
        },
    }


def _elapsed_seconds(started_at: float, now: float) -> float:
    return round(max(0.0, now - started_at), 3)


def _remaining_seconds(deadline: float, now: float) -> float:
    return deadline - now


def _raise_deadline_expired() -> NoReturn:
    raise AntigravityError("Timed out waiting for Antigravity OCR")


def _desired_create_timeout_seconds(page_count: int) -> float:
    return max(120.0, 30.0 * page_count)


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
    if is_antigravity_interaction_id(
        value, max_length=ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN
    ):
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
    deadline: float,
    document_id: int | None = None,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
    random_fn=random.random,
) -> tuple[dict[str, Any], int]:
    interaction_id = interaction.get("id")
    if not interaction_id:
        raise AntigravityError("Interaction response missing id")

    poll_started = monotonic_fn()
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


def _max_provider_calls_for_page_count(page_count: int) -> int:
    """Worst-case binary splits down to single pages: n successes + n-1 failures."""
    return max(1, 2 * page_count - 1)


def _ocr_output_shape(raw_text: str | None) -> _OcrOutputShape:
    if raw_text is None:
        return _OcrOutputShape(
            present=False,
            char_length=0,
            stripped_char_length=0,
            starts_with_open_brace=False,
            ends_with_close_brace=False,
        )
    stripped = raw_text.strip()
    return _OcrOutputShape(
        present=bool(stripped),
        char_length=len(raw_text),
        stripped_char_length=len(stripped),
        starts_with_open_brace=stripped.startswith("{"),
        ends_with_close_brace=stripped.endswith("}"),
    )


def _log_ocr_output_shape(
    *,
    document_id: int | None,
    page_count: int,
    global_page_start: int,
    global_page_end: int,
    provider_calls_used: int,
    provider_call_budget: int,
    shape: _OcrOutputShape,
) -> None:
    logger.info(
        "Antigravity OCR output shape document_id=%s page_count=%s "
        "global_page_start=%s global_page_end=%s provider_calls_used=%s "
        "provider_call_budget=%s output_present=%s output_char_length=%s "
        "output_stripped_char_length=%s starts_with_open_brace=%s "
        "ends_with_close_brace=%s",
        document_id,
        page_count,
        global_page_start,
        global_page_end,
        provider_calls_used,
        provider_call_budget,
        shape.present,
        shape.char_length,
        shape.stripped_char_length,
        shape.starts_with_open_brace,
        shape.ends_with_close_brace,
    )


def _log_truncated_json_split(
    *,
    document_id: int | None,
    page_count: int,
    global_page_start: int,
    global_page_end: int,
    left_page_count: int,
    right_page_count: int,
    provider_calls_used: int,
    provider_call_budget: int,
    shape: _OcrOutputShape,
) -> None:
    logger.warning(
        "Antigravity OCR truncated JSON split document_id=%s reason=%s "
        "page_count=%s global_page_start=%s global_page_end=%s "
        "left_page_count=%s right_page_count=%s provider_calls_used=%s "
        "provider_call_budget=%s output_char_length=%s "
        "starts_with_open_brace=%s ends_with_close_brace=%s",
        document_id,
        REASON_INVALID_JSON,
        page_count,
        global_page_start,
        global_page_end,
        left_page_count,
        right_page_count,
        provider_calls_used,
        provider_call_budget,
        shape.char_length,
        shape.starts_with_open_brace,
        shape.ends_with_close_brace,
    )


def _split_contiguous_pages(
    pages: list[PageImage],
) -> tuple[list[PageImage], list[PageImage]]:
    if len(pages) < 2:
        raise AntigravityError(
            "Cannot split an Antigravity OCR batch with fewer than 2 pages"
        )
    mid = len(pages) // 2
    return pages[:mid], pages[mid:]


def _remap_validated_ocr_output(
    output: ValidatedOcrOutput,
    *,
    global_page_start: int,
) -> ValidatedOcrOutput:
    return ValidatedOcrOutput(
        pages=tuple(
            ValidatedOcrPage(
                page_index=global_page_start + page.page_index - 1,
                outcome=page.outcome,
                text=page.text,
            )
            for page in output.pages
        )
    )


def _combine_validated_ocr_outputs(
    left: ValidatedOcrOutput,
    right: ValidatedOcrOutput,
) -> ValidatedOcrOutput:
    combined = ValidatedOcrOutput(pages=left.pages + right.pages)
    indexes = [page.page_index for page in combined.pages]
    expected = list(range(indexes[0], indexes[0] + len(indexes))) if indexes else []
    if indexes != expected:
        raise AntigravityError(
            "Antigravity truncated-JSON recovery assembled pages out of order"
        )
    return combined


def _create_and_poll_interaction(
    pages: list[PageImage],
    *,
    api_key: str,
    agent_id: str,
    document_id: int | None,
    poll_seconds: float,
    deadline: float,
    background: bool,
    sleep_fn,
    monotonic_fn,
    random_fn,
    budget: _ProviderCallBudget,
) -> dict[str, Any]:
    prompt = build_antigravity_ocr_prompt(len(pages))
    multimodal_input = build_multimodal_input(prompt, pages)

    block_types = [
        block.get("type") for block in multimodal_input if isinstance(block, dict)
    ]
    image_block_count = block_types.count("image")

    create_payload = build_antigravity_create_payload(
        agent_id=agent_id,
        multimodal_input=multimodal_input,
        background=background,
    )
    agent_config = create_payload.get("agent_config")
    requested_model = None
    if isinstance(agent_config, dict):
        model = agent_config.get("model")
        if isinstance(model, str):
            requested_model = model
    tools_config_empty = create_payload.get("tools") == []
    environment = create_payload.get("environment")
    requested_network_disabled = (
        isinstance(environment, dict)
        and environment.get("type") == "remote"
        and environment.get("network") == "disabled"
    )

    create_started = monotonic_fn()
    remaining = _remaining_seconds(deadline, create_started)
    if remaining <= 0:
        logger.warning(
            "Antigravity interaction lifecycle document_id=%s phase=create_deadline "
            "pages=%s remaining_seconds=%.3f provider_calls_used=%s "
            "provider_call_budget=%s",
            document_id,
            len(pages),
            remaining,
            budget.used,
            budget.limit,
        )
        _raise_deadline_expired()
    create_timeout = min(_desired_create_timeout_seconds(len(pages)), remaining)

    budget.consume()

    logger.info(
        "Antigravity request payload document_id=%s agent=%s "
        "requested_model=%s tools_config_empty=%s "
        "requested_network_disabled=%s pages=%s "
        "input_blocks=%s image_blocks=%s block_types=%s phase=create "
        "create_timeout_seconds=%s remaining_seconds=%s provider_calls_used=%s "
        "provider_call_budget=%s",
        document_id,
        agent_id,
        requested_model,
        tools_config_empty,
        requested_network_disabled,
        len(pages),
        len(multimodal_input),
        image_block_count,
        block_types,
        create_timeout,
        remaining,
        budget.used,
        budget.limit,
    )
    _log_encoded_image_blocks(
        document_id=document_id,
        pages=pages,
        input_blocks=multimodal_input,
    )

    try:
        interaction = _create_interaction(
            api_key,
            payload=create_payload,
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
            len(pages),
        )
        raise

    now = monotonic_fn()
    logger.info(
        "Antigravity interaction lifecycle document_id=%s phase=create "
        "elapsed_seconds=%.3f interaction_id=%s status=%s environment_id=%s",
        document_id,
        _elapsed_seconds(create_started, now),
        _sanitize_interaction_id(interaction.get("id")),
        _known_or_other(interaction.get("status"), _KNOWN_STATUSES)
        if interaction.get("status") is not None
        else None,
        _sanitize_environment_id(interaction.get("environment_id")),
    )

    response_source = RESPONSE_SOURCE_CREATE
    if background or interaction.get("status") == IN_PROGRESS_STATUS:
        remaining = max(0.0, _remaining_seconds(deadline, now))
        interaction, poll_successes = _poll_until_done(
            api_key,
            interaction,
            poll_seconds=poll_seconds,
            timeout_seconds=remaining,
            deadline=deadline,
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
    return interaction


def _transcribe_pages_recovering_truncated_json(
    pages: list[PageImage],
    *,
    global_page_start: int,
    budget: _ProviderCallBudget,
    api_key: str,
    agent_id: str,
    document_id: int | None,
    poll_seconds: float,
    deadline: float,
    background: bool,
    sleep_fn,
    monotonic_fn,
    random_fn,
) -> ValidatedOcrOutput:
    interaction = _create_and_poll_interaction(
        pages,
        api_key=api_key,
        agent_id=agent_id,
        document_id=document_id,
        poll_seconds=poll_seconds,
        deadline=deadline,
        background=background,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
        random_fn=random_fn,
        budget=budget,
    )
    raw_text = extract_final_model_output_text(interaction.get("steps"))
    shape = _ocr_output_shape(raw_text)
    page_count = len(pages)
    global_page_end = global_page_start + page_count - 1
    _log_ocr_output_shape(
        document_id=document_id,
        page_count=page_count,
        global_page_start=global_page_start,
        global_page_end=global_page_end,
        provider_calls_used=budget.used,
        provider_call_budget=budget.limit,
        shape=shape,
    )
    try:
        validated = validate_antigravity_ocr_output(
            interaction.get("steps"),
            expected_page_count=page_count,
        )
    except OcrContractError as exc:
        can_split = (
            exc.reason == REASON_INVALID_JSON
            and shape.structurally_truncated
            and page_count > 1
            and budget.remaining >= 2
        )
        if not can_split:
            _raise_output_validation_error(exc, document_id=document_id)
        left_pages, right_pages = _split_contiguous_pages(pages)
        _log_truncated_json_split(
            document_id=document_id,
            page_count=page_count,
            global_page_start=global_page_start,
            global_page_end=global_page_end,
            left_page_count=len(left_pages),
            right_page_count=len(right_pages),
            provider_calls_used=budget.used,
            provider_call_budget=budget.limit,
            shape=shape,
        )
        left = _transcribe_pages_recovering_truncated_json(
            left_pages,
            global_page_start=global_page_start,
            budget=budget,
            api_key=api_key,
            agent_id=agent_id,
            document_id=document_id,
            poll_seconds=poll_seconds,
            deadline=deadline,
            background=background,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            random_fn=random_fn,
        )
        right = _transcribe_pages_recovering_truncated_json(
            right_pages,
            global_page_start=global_page_start + len(left_pages),
            budget=budget,
            api_key=api_key,
            agent_id=agent_id,
            document_id=document_id,
            poll_seconds=poll_seconds,
            deadline=deadline,
            background=background,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            random_fn=random_fn,
        )
        return _combine_validated_ocr_outputs(left, right)

    return _remap_validated_ocr_output(validated, global_page_start=global_page_start)


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

    budget = _ProviderCallBudget(
        limit=_max_provider_calls_for_page_count(len(ordered_pages))
    )
    deadline = monotonic_fn() + timeout_seconds
    validated = _transcribe_pages_recovering_truncated_json(
        ordered_pages,
        global_page_start=1,
        budget=budget,
        api_key=api_key,
        agent_id=agent_id,
        document_id=document_id,
        poll_seconds=poll_seconds,
        deadline=deadline,
        background=background,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
        random_fn=random_fn,
    )
    return AntigravityResult(
        text=render_validated_ocr_text(validated),
        engine_name=agent_id,
    )


BAND_ATTEMPT_UNASSISTED = "unassisted"
BAND_ATTEMPT_ASSISTED_FALLBACK = "assisted_fallback"
_BAND_ATTEMPT_KINDS = frozenset(
    {BAND_ATTEMPT_UNASSISTED, BAND_ATTEMPT_ASSISTED_FALLBACK}
)
_BAND_JPEG_MIME = "image/jpeg"
_BAND_PROVIDER_MESSAGE_MAX_CHARS = 400
_BAND_FAILURE_INVALID_REQUEST = "invalid_request"
_BAND_FAILURE_CREATE_TIMEOUT = "create_timeout"
_BAND_FAILURE_CREATE_HTTP = "create_http"
_BAND_FAILURE_CREATE_NETWORK = "create_network"
_BAND_FAILURE_CREATE_INVALID_RESPONSE = "create_invalid_response"
_BAND_FAILURE_POLL_TIMEOUT = "poll_timeout"
_BAND_FAILURE_POLL_ERROR = "poll_error"
_BAND_POLL_COMPLETED = "completed"
_BAND_POLL_TIMEOUT = "timeout"
_BAND_POLL_ERROR = "poll_error"
_BAND_POLL_CREATE_ERROR = "create_error"
_CANCEL_OUTCOME_CANCELLED = "cancelled"
_CANCEL_OUTCOME_COMPLETED = "completed"
_CANCEL_OUTCOME_FAILED = "failed"
_CANCEL_OUTCOME_OTHER = "other"
_CANCEL_OUTCOME_HTTP_ERROR = "http_error"
_CANCEL_OUTCOME_TIMEOUT = "timeout"
_CANCEL_OUTCOME_NETWORK_ERROR = "network_error"
_BAND_KEYED_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+[?&]key=[^\s\"'<>\\]*",
    re.IGNORECASE,
)
_BAND_KEY_QUERY_RE = re.compile(r"[?&]key=[^&\s]*", re.IGNORECASE)
_BAND_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")
_ARABIC_PRINTED_BAND_SHARED_RULES = (
    "TASK: Transcribe the attached page image only. Do not translate, "
    "summarize, explain, complete missing content, or converse.\n"
    "RULES:\n"
    "- Transcribe visible text only.\n"
    "- Preserve visible Arabic, names, numbers, dates, punctuation, and "
    "reading order.\n"
    "- Preserve Latin and Hebrew spans exactly where they appear. Do not "
    "convert them into Arabic.\n"
    "- Include cover/catalog page text and visible handwritten additions.\n"
    "- Prefer [UNCLEAR] over inventing or guessing text.\n"
    "- There is exactly one inline image attached.\n"
)
NO_SYNTHETIC_ELLIPSIS_INSTRUCTION = (
    "Transcribe only visible source characters. If the image/crop ends in the "
    "middle of a sentence, transcribe the last visible word or characters and "
    "then emit the exact technical completion marker. Never add `...` or `…` "
    "merely to represent that the page continues. `[UNCLEAR]` remains allowed "
    "only for genuinely illegible visible text.\n"
)
_MARKER_INSTRUCTION_PREFIX = (
    "- After the transcription, write this exact completion marker as the "
    "final non-empty line and nothing after it:\n"
)


class AntigravityBandCheckpointError(Exception):
    """Persistence hook failed after create returned a usable interaction id."""

    def __init__(self, *, interaction_id: str, exception_class: str) -> None:
        self.interaction_id = interaction_id
        self.exception_class = exception_class
        super().__init__(self._safe_text())

    def _safe_text(self) -> str:
        return (
            "AntigravityBandCheckpointError("
            f"interaction_id={self.interaction_id!r}, "
            f"exception_class={self.exception_class!r})"
        )

    def __repr__(self) -> str:
        return self._safe_text()

    def __str__(self) -> str:
        return self._safe_text()


def _band_result_repr(name: str, fields: list[tuple[str, Any]]) -> str:
    return name + "(" + ", ".join(f"{key}={value!r}" for key, value in fields) + ")"


@dataclass(frozen=True)
class AntigravityBandOcrResult:
    interaction_id: str | None
    last_status: str | None
    polling_outcome: str
    step_types: tuple[str, ...]
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_thought_tokens: int | None
    total_tokens: int | None
    latency_seconds: float | None
    marker_seen: bool
    coverage_ratio: float | None
    output_non_whitespace: int | None
    draft_non_whitespace: int | None
    accepted: bool
    transcription: str
    failure_kind: str | None
    create_returned_interaction: bool

    def __repr__(self) -> str:
        return _band_result_repr(
            "AntigravityBandOcrResult",
            [
                ("interaction_id", self.interaction_id),
                ("last_status", self.last_status),
                ("polling_outcome", self.polling_outcome),
                ("step_types", self.step_types),
                ("total_input_tokens", self.total_input_tokens),
                ("total_output_tokens", self.total_output_tokens),
                ("total_thought_tokens", self.total_thought_tokens),
                ("total_tokens", self.total_tokens),
                ("latency_seconds", self.latency_seconds),
                ("marker_seen", self.marker_seen),
                ("coverage_ratio", self.coverage_ratio),
                ("output_non_whitespace", self.output_non_whitespace),
                ("draft_non_whitespace", self.draft_non_whitespace),
                ("accepted", self.accepted),
                ("failure_kind", self.failure_kind),
                ("create_returned_interaction", self.create_returned_interaction),
            ],
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True)
class AntigravityBandCancelResult:
    cancel_outcome: str
    last_status: str | None
    http_status: int | None
    provider_error_code: str | None
    evaluation_accepted: bool | None
    transcription: str
    marker_seen: bool
    coverage_ratio: float | None
    failure_kind: str | None

    def __repr__(self) -> str:
        return _band_result_repr(
            "AntigravityBandCancelResult",
            [
                ("cancel_outcome", self.cancel_outcome),
                ("last_status", self.last_status),
                ("http_status", self.http_status),
                ("provider_error_code", self.provider_error_code),
                ("evaluation_accepted", self.evaluation_accepted),
                ("marker_seen", self.marker_seen),
                ("coverage_ratio", self.coverage_ratio),
                ("failure_kind", self.failure_kind),
            ],
        )

    def __str__(self) -> str:
        return repr(self)


@dataclass(frozen=True)
class AntigravityBandCreateResult:
    interaction_id: str | None
    last_status: str | None
    step_types: tuple[str, ...]
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_thought_tokens: int | None
    total_tokens: int | None
    failure_kind: str | None
    create_returned_interaction: bool
    polling_outcome: str

    def __repr__(self) -> str:
        return _band_result_repr(
            "AntigravityBandCreateResult",
            [
                ("interaction_id", self.interaction_id),
                ("last_status", self.last_status),
                ("step_types", self.step_types),
                ("failure_kind", self.failure_kind),
                ("create_returned_interaction", self.create_returned_interaction),
                ("polling_outcome", self.polling_outcome),
            ],
        )


@dataclass
class _BandSnapshot:
    interaction_id: str | None
    last_status: str | None
    step_types: tuple[str, ...]
    total_input_tokens: int | None
    total_output_tokens: int | None
    total_thought_tokens: int | None
    total_tokens: int | None
    raw_text: str | None
    failure_kind: str | None
    create_returned_interaction: bool
    polling_outcome: str


def _band_redact_text(value: object, *, extra_secrets: tuple[str, ...] = ()) -> str:
    text = "" if value is None else str(value)
    redacted = _BAND_KEYED_URL_RE.sub("<redacted-keyed-url>", text)
    redacted = _BAND_KEY_QUERY_RE.sub("<redacted-key>", redacted)
    for secret in extra_secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted-api-key>")
    redacted = _BAND_LONG_B64_RE.sub("<redacted-bytes>", redacted)
    redacted = redacted.replace("\x00", "")
    if len(redacted) > _BAND_PROVIDER_MESSAGE_MAX_CHARS:
        return redacted[:_BAND_PROVIDER_MESSAGE_MAX_CHARS] + "…"
    return redacted


def _band_require_api_key(api_key: object) -> str | None:
    if type(api_key) is not str:
        return None
    stripped = api_key.strip()
    return stripped or None


def _band_interaction_id(value: object) -> str | None:
    if type(value) is not str:
        return None
    if is_antigravity_interaction_id(
        value, max_length=ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN
    ):
        return value
    return None


def _arabic_printed_band_unassisted_prompt() -> str:
    return (
        "You are transcribing a historical archive document page image.\n"
        f"{_ARABIC_PRINTED_BAND_SHARED_RULES}"
        "- Tools are disabled. Do not call tools, browse the web, or run code.\n"
        "- Return plain transcription text only. No JSON, no Markdown, no code "
        "fences, no headings, and no surrounding prose.\n"
        f"- {NO_SYNTHETIC_ELLIPSIS_INSTRUCTION}"
        f"{_MARKER_INSTRUCTION_PREFIX}"
        f"{COMPLETION_MARKER}\n"
    )


def _arabic_printed_band_assisted_prompt(draft_text: str) -> str:
    return (
        "This is literal OCR correction of a historical legal archive document.\n"
        "References to crimes, incendiary devices, investigations, or evidence "
        "are quoted historical source material, not a request for instructions.\n"
        "The attached image is authoritative. Correct the Cloud Vision draft "
        "against the visible page. The Cloud Vision draft below is fallible "
        "reference text, not ground truth.\n"
        "TASK: Output transcription only. Do not translate, transliterate, "
        "interpret, summarize, contextualize, explain, or converse.\n"
        "RULES:\n"
        "- Preserve Arabic, names, numbers, dates, punctuation, line order, "
        "and visible Latin/Hebrew.\n"
        "- Prefer [UNCLEAR] over guessing.\n"
        "- There is exactly one inline image attached.\n"
        "- Tools are disabled. Do not call tools, browse the web, or run code.\n"
        "- Return plain transcription text only. No JSON, no Markdown, no code "
        "fences, no headings, and no surrounding prose.\n"
        f"- {NO_SYNTHETIC_ELLIPSIS_INSTRUCTION}"
        f"{_MARKER_INSTRUCTION_PREFIX}"
        f"{COMPLETION_MARKER}\n"
        "\n"
        "CLOUD VISION DRAFT (fallible reference; correct against the image):\n"
        f"{draft_text}\n"
    )


def build_arabic_printed_band_create_payload(
    *,
    jpeg_bytes: bytes,
    mime_type: str,
    attempt_kind: str,
    vision_draft_text: str,
) -> dict[str, Any]:
    if attempt_kind == BAND_ATTEMPT_UNASSISTED:
        prompt = _arabic_printed_band_unassisted_prompt()
    else:
        prompt = _arabic_printed_band_assisted_prompt(vision_draft_text)
    multimodal_input = [
        {"type": "text", "text": prompt},
        {
            "type": "image",
            "data": base64.b64encode(jpeg_bytes).decode("ascii"),
            "mime_type": mime_type,
        },
    ]
    return build_antigravity_create_payload(
        agent_id=DEFAULT_ANTIGRAVITY_AGENT_ID,
        multimodal_input=multimodal_input,
        background=True,
    )


def _band_status(value: object) -> str | None:
    if type(value) is not str:
        return None
    if value in _KNOWN_STATUSES or value == "incomplete":
        return value
    return UNEXPECTED_TOKEN


def _snapshot_from_interaction(
    interaction: object,
    *,
    failure_kind: str | None,
    polling_outcome: str,
    fallback_id: str | None = None,
    fallback_status: str | None = None,
) -> _BandSnapshot:
    summary = summarize_antigravity_interaction(interaction)
    raw_id = interaction.get("id") if isinstance(interaction, dict) else None
    interaction_id = _band_interaction_id(raw_id) or _band_interaction_id(fallback_id)
    last_status = None
    if isinstance(interaction, dict):
        last_status = _band_status(interaction.get("status"))
    if last_status is None:
        last_status = fallback_status
    raw_text = None
    if isinstance(interaction, dict):
        raw_text = extract_final_model_output_text(interaction.get("steps"))
    step_types = tuple(summary.get("step_types") or ())
    return _BandSnapshot(
        interaction_id=interaction_id,
        last_status=last_status,
        step_types=step_types,
        total_input_tokens=summary.get("total_input_tokens"),
        total_output_tokens=summary.get("total_output_tokens"),
        total_thought_tokens=summary.get("total_thought_tokens"),
        total_tokens=summary.get("total_tokens"),
        raw_text=raw_text,
        failure_kind=failure_kind,
        create_returned_interaction=interaction_id is not None,
        polling_outcome=polling_outcome,
    )


def _empty_snapshot(
    *,
    failure_kind: str | None,
    polling_outcome: str,
    interaction_id: str | None = None,
    last_status: str | None = None,
) -> _BandSnapshot:
    return _BandSnapshot(
        interaction_id=interaction_id,
        last_status=last_status,
        step_types=(),
        total_input_tokens=None,
        total_output_tokens=None,
        total_thought_tokens=None,
        total_tokens=None,
        raw_text=None,
        failure_kind=failure_kind,
        create_returned_interaction=interaction_id is not None,
        polling_outcome=polling_outcome,
    )


def _validate_band_inputs(
    *,
    api_key: object,
    jpeg_bytes: object,
    mime_type: object,
    vision_draft_text: object,
    attempt_kind: object,
    absolute_deadline: object,
    monotonic_now: float,
) -> tuple[str, bytes, str, str, str] | _BandSnapshot:
    key = _band_require_api_key(api_key)
    if key is None:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if type(jpeg_bytes) is not bytes or not jpeg_bytes:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if mime_type != _BAND_JPEG_MIME:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if type(vision_draft_text) is not str:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if attempt_kind not in _BAND_ATTEMPT_KINDS:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if type(absolute_deadline) is not float and type(absolute_deadline) is not int:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    remaining_absolute = float(absolute_deadline) - monotonic_now
    if remaining_absolute <= 0:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_POLL_TIMEOUT,
            polling_outcome=_BAND_POLL_TIMEOUT,
        )
    return key, jpeg_bytes, mime_type, vision_draft_text, attempt_kind


def _attempt_remaining(absolute_deadline: float, now: float, started: float) -> float:
    window_end = started + ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS
    return min(absolute_deadline, window_end) - now


def _create_band_snapshot(
    *,
    api_key: str,
    jpeg_bytes: bytes,
    mime_type: str,
    vision_draft_text: str,
    attempt_kind: str,
    timeout_seconds: float,
) -> _BandSnapshot:
    """One create POST. Does not poll or invoke JSON repair."""
    payload = build_arabic_printed_band_create_payload(
        jpeg_bytes=jpeg_bytes,
        mime_type=mime_type,
        attempt_kind=attempt_kind,
        vision_draft_text=vision_draft_text,
    )
    try:
        response = requests.post(
            INTERACTIONS_BASE_URL,
            headers=_request_headers(api_key),
            json=payload,
            timeout=timeout_seconds,
        )
    except requests.Timeout:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_CREATE_TIMEOUT,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    except requests.RequestException:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_CREATE_NETWORK,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if not response.ok:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_CREATE_HTTP,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    try:
        body = response.json()
    except ValueError:
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_CREATE_INVALID_RESPONSE,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    if not isinstance(body, dict):
        return _empty_snapshot(
            failure_kind=_BAND_FAILURE_CREATE_INVALID_RESPONSE,
            polling_outcome=_BAND_POLL_CREATE_ERROR,
        )
    snapshot = _snapshot_from_interaction(
        body,
        failure_kind=None,
        polling_outcome=_BAND_POLL_COMPLETED,
    )
    if snapshot.interaction_id is None:
        snapshot.failure_kind = _BAND_FAILURE_CREATE_INVALID_RESPONSE
        snapshot.create_returned_interaction = False
        snapshot.polling_outcome = _BAND_POLL_CREATE_ERROR
        snapshot.raw_text = None
        return snapshot
    return snapshot


def create_arabic_printed_band_interaction(
    *,
    api_key: str,
    jpeg_bytes: bytes,
    mime_type: str,
    vision_draft_text: str,
    attempt_kind: str,
    timeout_seconds: float,
) -> AntigravityBandCreateResult:
    """One create POST. Does not poll, cancel, or evaluate OCR text."""
    snapshot = _create_band_snapshot(
        api_key=api_key,
        jpeg_bytes=jpeg_bytes,
        mime_type=mime_type,
        vision_draft_text=vision_draft_text,
        attempt_kind=attempt_kind,
        timeout_seconds=timeout_seconds,
    )
    return AntigravityBandCreateResult(
        interaction_id=snapshot.interaction_id,
        last_status=snapshot.last_status,
        step_types=snapshot.step_types,
        total_input_tokens=snapshot.total_input_tokens,
        total_output_tokens=snapshot.total_output_tokens,
        total_thought_tokens=snapshot.total_thought_tokens,
        total_tokens=snapshot.total_tokens,
        failure_kind=snapshot.failure_kind,
        create_returned_interaction=snapshot.create_returned_interaction,
        polling_outcome=snapshot.polling_outcome,
    )


def _poll_band_snapshot(
    *,
    api_key: str,
    interaction_id: str,
    snapshot: _BandSnapshot,
    attempt_deadline: float,
    poll_seconds: float,
    sleep_fn,
    monotonic_fn,
) -> _BandSnapshot:
    """GET-poll until terminal, timeout, or poll error. Never creates. Never cancels."""
    current = snapshot
    while True:
        now = monotonic_fn()
        remaining = attempt_deadline - now
        if remaining <= 0:
            current.failure_kind = _BAND_FAILURE_POLL_TIMEOUT
            current.polling_outcome = _BAND_POLL_TIMEOUT
            current.raw_text = None
            return current
        if (
            current.last_status != IN_PROGRESS_STATUS
            and current.last_status is not None
        ):
            return current
        sleep_for = min(poll_seconds, remaining)
        if sleep_for > 0:
            sleep_fn(sleep_for)
        now = monotonic_fn()
        remaining = attempt_deadline - now
        if remaining <= 0:
            current.failure_kind = _BAND_FAILURE_POLL_TIMEOUT
            current.polling_outcome = _BAND_POLL_TIMEOUT
            current.raw_text = None
            return current
        try:
            response = requests.get(
                f"{INTERACTIONS_BASE_URL}/{interaction_id}",
                headers=_request_headers(api_key),
                timeout=remaining,
            )
        except requests.Timeout:
            current.failure_kind = _BAND_FAILURE_POLL_TIMEOUT
            current.polling_outcome = _BAND_POLL_TIMEOUT
            current.raw_text = None
            return current
        except requests.RequestException:
            current.failure_kind = _BAND_FAILURE_POLL_ERROR
            current.polling_outcome = _BAND_POLL_ERROR
            current.raw_text = None
            return current
        if not response.ok:
            current.failure_kind = _BAND_FAILURE_POLL_ERROR
            current.polling_outcome = _BAND_POLL_ERROR
            current.raw_text = None
            return current
        try:
            body = response.json()
        except ValueError:
            current.failure_kind = _BAND_FAILURE_POLL_ERROR
            current.polling_outcome = _BAND_POLL_ERROR
            current.raw_text = None
            return current
        if not isinstance(body, dict):
            current.failure_kind = _BAND_FAILURE_POLL_ERROR
            current.polling_outcome = _BAND_POLL_ERROR
            current.raw_text = None
            return current
        current = _snapshot_from_interaction(
            body,
            failure_kind=None,
            polling_outcome=_BAND_POLL_COMPLETED,
            fallback_id=interaction_id,
            fallback_status=snapshot.last_status,
        )
        current.interaction_id = interaction_id
        current.create_returned_interaction = True


def poll_arabic_printed_band_interaction(
    *,
    api_key: str,
    interaction_id: str,
    vision_draft_text: str,
    last_status: str | None,
    absolute_deadline_monotonic: float,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> AntigravityBandOcrResult:
    """Poll an already-created band interaction. Never creates or cancels."""
    started = monotonic_fn()
    key = _band_require_api_key(api_key)
    sanitized_id = _band_interaction_id(interaction_id)
    if key is None or sanitized_id is None:
        return _result_from_snapshot(
            _empty_snapshot(
                failure_kind=_BAND_FAILURE_INVALID_REQUEST,
                polling_outcome=_BAND_POLL_ERROR,
            ),
            vision_draft_text=vision_draft_text
            if type(vision_draft_text) is str
            else "",
            latency_seconds=monotonic_fn() - started,
            evaluate=False,
        )
    snapshot = _empty_snapshot(
        failure_kind=None,
        polling_outcome=_BAND_POLL_COMPLETED,
        interaction_id=sanitized_id,
        last_status=last_status,
    )
    snapshot = _poll_band_snapshot(
        api_key=key,
        interaction_id=sanitized_id,
        snapshot=snapshot,
        attempt_deadline=float(absolute_deadline_monotonic),
        poll_seconds=poll_seconds,
        sleep_fn=sleep_fn,
        monotonic_fn=monotonic_fn,
    )
    evaluate = snapshot.failure_kind is None
    if not evaluate:
        snapshot.raw_text = None
    return _result_from_snapshot(
        snapshot,
        vision_draft_text=vision_draft_text if type(vision_draft_text) is str else "",
        latency_seconds=monotonic_fn() - started,
        evaluate=evaluate,
    )


def _result_from_snapshot(
    snapshot: _BandSnapshot,
    *,
    vision_draft_text: str,
    latency_seconds: float,
    evaluate: bool,
) -> AntigravityBandOcrResult:
    marker_seen = False
    coverage_ratio = None
    output_nw = None
    draft_nw = None
    accepted = False
    transcription = ""
    failure_kind = snapshot.failure_kind
    if evaluate and snapshot.failure_kind is None:
        evaluation = evaluate_arabic_printed_band_output(
            snapshot.raw_text,
            vision_draft_text,
            status=snapshot.last_status,
            step_types=snapshot.step_types,
        )
        marker_seen = evaluation.marker_seen
        coverage_ratio = evaluation.coverage_ratio
        output_nw = evaluation.output_non_whitespace
        draft_nw = evaluation.draft_non_whitespace
        accepted = evaluation.accepted
        transcription = evaluation.transcription if evaluation.accepted else ""
        failure_kind = evaluation.failure_kind
    return AntigravityBandOcrResult(
        interaction_id=snapshot.interaction_id,
        last_status=snapshot.last_status,
        polling_outcome=snapshot.polling_outcome,
        step_types=snapshot.step_types,
        total_input_tokens=snapshot.total_input_tokens,
        total_output_tokens=snapshot.total_output_tokens,
        total_thought_tokens=snapshot.total_thought_tokens,
        total_tokens=snapshot.total_tokens,
        latency_seconds=round(max(0.0, latency_seconds), 3),
        marker_seen=marker_seen,
        coverage_ratio=coverage_ratio,
        output_non_whitespace=output_nw,
        draft_non_whitespace=draft_nw,
        accepted=accepted,
        transcription=transcription,
        failure_kind=failure_kind,
        create_returned_interaction=snapshot.create_returned_interaction,
    )


def transcribe_band_with_antigravity(
    *,
    api_key: str,
    jpeg_bytes: bytes,
    mime_type: str,
    vision_draft_text: str,
    attempt_kind: str,
    absolute_deadline_monotonic: float,
    on_interaction_created: Callable[[str], None],
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> AntigravityBandOcrResult:
    """One-band plain-text Antigravity attempt. One create. No cancel. No JSON repair."""
    started = monotonic_fn()
    if not callable(on_interaction_created):
        return _result_from_snapshot(
            _empty_snapshot(
                failure_kind=_BAND_FAILURE_INVALID_REQUEST,
                polling_outcome=_BAND_POLL_CREATE_ERROR,
            ),
            vision_draft_text=""
            if type(vision_draft_text) is not str
            else vision_draft_text,
            latency_seconds=monotonic_fn() - started,
            evaluate=False,
        )
    validated = _validate_band_inputs(
        api_key=api_key,
        jpeg_bytes=jpeg_bytes,
        mime_type=mime_type,
        vision_draft_text=vision_draft_text,
        attempt_kind=attempt_kind,
        absolute_deadline=absolute_deadline_monotonic,
        monotonic_now=started,
    )
    if isinstance(validated, _BandSnapshot):
        return _result_from_snapshot(
            validated,
            vision_draft_text=""
            if type(vision_draft_text) is not str
            else vision_draft_text,
            latency_seconds=monotonic_fn() - started,
            evaluate=False,
        )
    key, jpeg, mime, draft, kind = validated
    remaining = _attempt_remaining(float(absolute_deadline_monotonic), started, started)
    if remaining <= 0:
        return _result_from_snapshot(
            _empty_snapshot(
                failure_kind=_BAND_FAILURE_POLL_TIMEOUT,
                polling_outcome=_BAND_POLL_TIMEOUT,
            ),
            vision_draft_text=draft,
            latency_seconds=monotonic_fn() - started,
            evaluate=False,
        )
    snapshot = _create_band_snapshot(
        api_key=key,
        jpeg_bytes=jpeg,
        mime_type=mime,
        vision_draft_text=draft,
        attempt_kind=kind,
        timeout_seconds=remaining,
    )
    if snapshot.interaction_id is None:
        snapshot.raw_text = None
        return _result_from_snapshot(
            snapshot,
            vision_draft_text=draft,
            latency_seconds=monotonic_fn() - started,
            evaluate=False,
        )
    try:
        on_interaction_created(snapshot.interaction_id)
    except Exception as exc:
        raise AntigravityBandCheckpointError(
            interaction_id=snapshot.interaction_id,
            exception_class=type(exc).__name__,
        ) from exc
    attempt_deadline = started + ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS
    attempt_deadline = min(attempt_deadline, float(absolute_deadline_monotonic))
    if snapshot.last_status == IN_PROGRESS_STATUS or snapshot.last_status is None:
        snapshot = _poll_band_snapshot(
            api_key=key,
            interaction_id=snapshot.interaction_id,
            snapshot=snapshot,
            attempt_deadline=attempt_deadline,
            poll_seconds=poll_seconds,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )
    evaluate = snapshot.failure_kind is None
    if not evaluate:
        snapshot.raw_text = None
    return _result_from_snapshot(
        snapshot,
        vision_draft_text=draft,
        latency_seconds=monotonic_fn() - started,
        evaluate=evaluate,
    )


def _cancel_outcome_for_status(status: str | None) -> str:
    if status == "cancelled":
        return _CANCEL_OUTCOME_CANCELLED
    if status == "completed":
        return _CANCEL_OUTCOME_COMPLETED
    if status == "failed":
        return _CANCEL_OUTCOME_FAILED
    return _CANCEL_OUTCOME_OTHER


def cancel_antigravity_interaction(
    *,
    api_key: str,
    interaction_id: str,
    vision_draft_text: str = "",
) -> AntigravityBandCancelResult:
    """One cancel POST. Never retried. Never counted as a create. Does not OCR-succeed."""
    extra_keys: tuple[str, ...] = ()
    key = _band_require_api_key(api_key)
    if key is not None and type(api_key) is str:
        extra_keys = (api_key, key)
    sanitized_id = _band_interaction_id(interaction_id)
    if key is None or sanitized_id is None:
        return AntigravityBandCancelResult(
            cancel_outcome=_CANCEL_OUTCOME_OTHER,
            last_status=None,
            http_status=None,
            provider_error_code=None,
            evaluation_accepted=None,
            transcription="",
            marker_seen=False,
            coverage_ratio=None,
            failure_kind=_BAND_FAILURE_INVALID_REQUEST,
        )
    try:
        response = requests.post(
            f"{INTERACTIONS_BASE_URL}/{sanitized_id}/cancel",
            headers=_request_headers(key),
            json={},
            timeout=ANTIGRAVITY_CANCEL_HTTP_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return AntigravityBandCancelResult(
            cancel_outcome=_CANCEL_OUTCOME_TIMEOUT,
            last_status=None,
            http_status=None,
            provider_error_code=None,
            evaluation_accepted=None,
            transcription="",
            marker_seen=False,
            coverage_ratio=None,
            failure_kind=_CANCEL_OUTCOME_TIMEOUT,
        )
    except requests.RequestException:
        return AntigravityBandCancelResult(
            cancel_outcome=_CANCEL_OUTCOME_NETWORK_ERROR,
            last_status=None,
            http_status=None,
            provider_error_code=None,
            evaluation_accepted=None,
            transcription="",
            marker_seen=False,
            coverage_ratio=None,
            failure_kind=_CANCEL_OUTCOME_NETWORK_ERROR,
        )
    if not response.ok:
        code = None
        try:
            payload = response.json()
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                raw_code = error.get("status") or error.get("code")
                if raw_code is not None:
                    code = _band_redact_text(raw_code, extra_secrets=extra_keys)
        except ValueError:
            code = None
        return AntigravityBandCancelResult(
            cancel_outcome=_CANCEL_OUTCOME_HTTP_ERROR,
            last_status=None,
            http_status=response.status_code,
            provider_error_code=code,
            evaluation_accepted=None,
            transcription="",
            marker_seen=False,
            coverage_ratio=None,
            failure_kind=_CANCEL_OUTCOME_HTTP_ERROR,
        )
    body: dict[str, Any] | None
    if not (response.text or "").strip():
        body = None
    else:
        try:
            parsed = response.json()
        except ValueError:
            return AntigravityBandCancelResult(
                cancel_outcome=_CANCEL_OUTCOME_OTHER,
                last_status=None,
                http_status=response.status_code,
                provider_error_code=None,
                evaluation_accepted=None,
                transcription="",
                marker_seen=False,
                coverage_ratio=None,
                failure_kind=_CANCEL_OUTCOME_OTHER,
            )
        if parsed is not None and not isinstance(parsed, dict):
            return AntigravityBandCancelResult(
                cancel_outcome=_CANCEL_OUTCOME_OTHER,
                last_status=None,
                http_status=response.status_code,
                provider_error_code=None,
                evaluation_accepted=None,
                transcription="",
                marker_seen=False,
                coverage_ratio=None,
                failure_kind=_CANCEL_OUTCOME_OTHER,
            )
        body = parsed if isinstance(parsed, dict) else None
    status = None
    if isinstance(body, dict) and body.get("status") is not None:
        raw_status = body.get("status")
        status = raw_status if isinstance(raw_status, str) else None
        if status is not None and status not in _KNOWN_STATUSES | {"incomplete"}:
            status = UNEXPECTED_TOKEN
    outcome = _cancel_outcome_for_status(status)
    evaluation_accepted = None
    transcription = ""
    marker_seen = False
    coverage_ratio = None
    failure_kind = None
    if outcome == _CANCEL_OUTCOME_COMPLETED and isinstance(body, dict):
        raw_text = extract_final_model_output_text(body.get("steps"))
        summary = summarize_antigravity_interaction(body)
        evaluation = evaluate_arabic_printed_band_output(
            raw_text,
            vision_draft_text,
            status="completed",
            step_types=tuple(summary.get("step_types") or ()),
        )
        evaluation_accepted = evaluation.accepted
        transcription = evaluation.transcription if evaluation.accepted else ""
        marker_seen = evaluation.marker_seen
        coverage_ratio = evaluation.coverage_ratio
        failure_kind = evaluation.failure_kind
    return AntigravityBandCancelResult(
        cancel_outcome=outcome,
        last_status=status,
        http_status=response.status_code,
        provider_error_code=None,
        evaluation_accepted=evaluation_accepted,
        transcription=transcription,
        marker_seen=marker_seen,
        coverage_ratio=coverage_ratio,
        failure_kind=failure_kind,
    )
