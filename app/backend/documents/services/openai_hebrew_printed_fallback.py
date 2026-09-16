"""Isolated OpenAI Responses helper for Hebrew printed OCR fallback.

Worker-only. Web/Gunicorn must not import this module for request handling.
Env validation may import constants from this module; the OpenAI SDK is loaded
lazily. GeminiAdapter may call ``transcribe_hebrew_printed_page_with_openai``
for checkpointed Hebrew PRINTED pages when the worker flag is enabled.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from enum import Enum, StrEnum
from typing import Any

from documents.services.review_reasons import HEBREW_PRINTED_OPENAI_FALLBACK

logger = logging.getLogger(__name__)

DEFAULT_OPENAI_HEBREW_PRINTED_MODEL = "gpt-5.6-sol"
OPENAI_RUNTIME_ENGINE_PREFIX = "openai:"
OPENAI_IMAGE_DETAIL = "original"
CHECKPOINT_ACTUAL_MODEL_MAX_LEN = 64
_ALLOWED_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/webp"})


class HebrewPrintedOpenAIFallbackFailureCode(StrEnum):
    EMPTY_OUTPUT = "EMPTY_OUTPUT"
    INCOMPLETE = "INCOMPLETE"
    INVALID_REQUEST = "INVALID_REQUEST"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    REFUSAL = "REFUSAL"


class HebrewPrintedOpenAIFallbackError(Exception):
    """Typed OpenAI fallback failure with no provider body or exception text."""

    def __init__(
        self,
        failure_code: HebrewPrintedOpenAIFallbackFailureCode,
        *,
        http_status: int | None = None,
        response_status: str | None = None,
    ) -> None:
        self.failure_code = failure_code
        self.http_status = http_status
        self.response_status = response_status
        super().__init__(failure_code.value)


@dataclass(frozen=True)
class HebrewPrintedOpenAIFallbackResult:
    text: str
    engine_name: str
    review_reasons: tuple[str, ...]
    needs_review: bool = True


def openai_runtime_engine_name(model: str) -> str:
    normalized = str(model or "").strip()
    if not normalized:
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
    engine_name = f"{OPENAI_RUNTIME_ENGINE_PREFIX}{normalized}"
    if len(engine_name) > CHECKPOINT_ACTUAL_MODEL_MAX_LEN:
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
    return engine_name


def transcribe_hebrew_printed_page_with_openai(
    *,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    model: str,
    api_key: str,
    client: Any | None = None,
) -> HebrewPrintedOpenAIFallbackResult:
    """One full-page Responses OCR call. No retries, crops, tools, or fallbacks."""

    engine_name = openai_runtime_engine_name(model)
    prompt_text = str(prompt or "")
    if not prompt_text.strip():
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
    if not image_bytes:
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
    normalized_mime = str(mime_type or "").strip().lower()
    if normalized_mime not in _ALLOWED_IMAGE_MIMES:
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )
    key = str(api_key or "").strip()
    if not key:
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INVALID_REQUEST,
        )

    data_url = (
        f"data:{normalized_mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    )
    request_kwargs = {
        "model": str(model).strip(),
        "store": False,
        "stream": False,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    {
                        "type": "input_image",
                        "image_url": data_url,
                        "detail": OPENAI_IMAGE_DETAIL,
                    },
                ],
            }
        ],
    }

    try:
        sdk_client = client
        if sdk_client is None:
            sdk_client = _build_openai_client(key)
        response = sdk_client.responses.create(**request_kwargs)
    except HebrewPrintedOpenAIFallbackError:
        raise
    except Exception as exc:
        _log_provider_failure(
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
            http_status=_http_status_from_exc(exc),
        )
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
            http_status=_http_status_from_exc(exc),
        ) from None

    return _result_from_response(response, engine_name=engine_name)


def _build_openai_client(api_key: str) -> Any:
    from openai import OpenAI

    return OpenAI(api_key=api_key)


def _result_from_response(
    response: Any,
    *,
    engine_name: str,
) -> HebrewPrintedOpenAIFallbackResult:
    response_status = _status_value(_attr(response, "status"))
    if _response_has_refusal(response):
        _log_provider_failure(
            HebrewPrintedOpenAIFallbackFailureCode.REFUSAL,
            response_status=response_status,
        )
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.REFUSAL,
            response_status=response_status,
        )
    if response_status == "incomplete":
        _log_provider_failure(
            HebrewPrintedOpenAIFallbackFailureCode.INCOMPLETE,
            response_status=response_status,
        )
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.INCOMPLETE,
            response_status=response_status,
        )
    if response_status != "completed":
        _log_provider_failure(
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
            response_status=response_status,
        )
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.PROVIDER_ERROR,
            response_status=response_status,
        )

    output_text = _attr(response, "output_text")
    if not isinstance(output_text, str) or not output_text.strip():
        _log_provider_failure(
            HebrewPrintedOpenAIFallbackFailureCode.EMPTY_OUTPUT,
            response_status=response_status,
        )
        raise HebrewPrintedOpenAIFallbackError(
            HebrewPrintedOpenAIFallbackFailureCode.EMPTY_OUTPUT,
            response_status=response_status,
        )
    return HebrewPrintedOpenAIFallbackResult(
        text=output_text.strip(),
        engine_name=engine_name,
        review_reasons=(HEBREW_PRINTED_OPENAI_FALLBACK,),
        needs_review=True,
    )


def _log_provider_failure(
    failure_code: HebrewPrintedOpenAIFallbackFailureCode,
    *,
    http_status: int | None = None,
    response_status: str | None = None,
) -> None:
    logger.warning(
        "hebrew printed openai fallback failed: failure_code=%s "
        "http_status=%s response_status=%s",
        failure_code.value,
        http_status,
        response_status,
    )


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _status_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        raw = value.value
        return raw if isinstance(raw, str) else None
    if isinstance(value, str):
        return value
    return None


def _http_status_from_exc(exc: BaseException) -> int | None:
    for name in ("status_code", "http_status", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None) if response is not None else None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _part_type(value: Any) -> str | None:
    if isinstance(value, Enum):
        raw = value.value
        return raw if isinstance(raw, str) else None
    if isinstance(value, str):
        return value
    return None


def _response_has_refusal(response: Any) -> bool:
    output = _attr(response, "output")
    if not output:
        return False
    try:
        items = list(output)
    except TypeError:
        return False
    for item in items:
        item_type = _part_type(_attr(item, "type"))
        if item_type in {"refusal", "output_refusal"}:
            return True
        content = _attr(item, "content")
        if not isinstance(content, (list, tuple)):
            continue
        for part in content:
            part_type = _part_type(_attr(part, "type"))
            if part_type in {"refusal", "output_refusal"}:
                return True
    return False
