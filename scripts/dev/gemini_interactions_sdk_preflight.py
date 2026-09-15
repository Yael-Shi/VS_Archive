#!/usr/bin/env python3
"""Isolated Gemini Interactions SDK diagnostic spike (preflight).

Closes empirical gaps for a future generateContent → Interactions migration.
Does NOT import production OCR/translation modules, routing, or checkpoints.
Does NOT embed or import production Gemini prompts or production model-candidate
lists; caller-supplied diagnostic inputs may use them explicitly.

Requires google-genai >= 2.3.0 (client.interactions.create). The repo Poetry
pin is currently google-genai 1.63.0; run this script in a temporary
environment instead of changing pyproject.toml / poetry.lock.

Stdout is diagnostic JSON: types, status, documented code/reason fields,
usage numbers, lengths, field names, and nested object keys. No output-text
preview, prompts, source, image bytes, or API keys directly. For Python API
exceptions, provider body.error.code and body.error.message may be emitted as
free-form provider diagnostics.

Official docs:
  https://ai.google.dev/gemini-api/docs/api-versions
  https://ai.google.dev/gemini-api/docs/interactions-overview
  https://ai.google.dev/gemini-api/docs/api-errors
  https://ai.google.dev/gemini-api/docs/image-understanding
  https://ai.google.dev/gemini-api/docs/thinking
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

# Diagnostic-only. Not production OCR models or prompts.
DEFAULT_DIAGNOSTIC_MODEL = "gemini-2.5-flash-lite"
MIN_SDK = (2, 3, 0)
SPIKE_TEXT_MARKER = "interactions-v1-preflight-ok"
# Spike-only transcription instruction. Intentionally not the production prompt.
SPIKE_OCR_INSTRUCTION = (
    "Diagnostic spike only. Transcribe visible printed text in the image. "
    "Output plain text. Do not translate."
)
TINY_MAX_OUTPUT_TOKENS = 1
_STRUCTURAL_SCALAR_KEYS = frozenset(
    {
        "code",
        "finish_reason",
        "http_status",
        "reason",
        "status",
        "status_code",
        "stop_reason",
        "type",
    }
)
_REDACT_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "content",
        "data",
        "delta",
        "input",
        "key",
        "message",
        "output_text",
        "prompt",
        "secret",
        "signature",
        "summary",
        "text",
        "token",
        "x-goog-api-key",
    }
)
_USAGE_ATTRS = (
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_tokens",
    "total_cached_tokens",
    "total_tool_use_tokens",
    "prompt_token_count",
    "candidates_token_count",
    "thoughts_token_count",
    "total_token_count",
    "prompt_tokens",
    "completion_tokens",
)
_ERROR_ATTRS = (
    "code",
    "status",
    "status_code",
    "http_status",
    "reason",
    "type",
    "error",
    "errors",
)
_INCOMPLETE_ATTRS = (
    "status",
    "incomplete",
    "incomplete_reason",
    "incomplete_details",
    "finish_reason",
    "stop_reason",
    "errors",
)
_BUILTIN_CASES = (
    "success",
    "incomplete",
    "ocr-shape",
    "negative-control",
)
_DEEP_MAX_DEPTH = 5
_DEEP_MAX_ITEMS = 20
_DEEP_MAX_KEYS = 40


class SpikeError(RuntimeError):
    def __init__(self, kind: str) -> None:
        self.kind = kind
        super().__init__(kind)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_fixture() -> Path:
    return _repo_root() / "scripts" / "dev" / "fixtures" / "printed_arabic_smoke.png"


def _load_env_file(env_file: Path | None) -> None:
    if env_file is None:
        return
    if not env_file.is_file():
        raise SpikeError("env_file_not_found")
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise SpikeError("python_dotenv_missing") from None
    load_dotenv(env_file, override=False)


def _api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise SpikeError("missing_gemini_api_key")
    return key


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    parts: list[int] = []
    for token in raw.split("."):
        digits = ""
        for char in token:
            if char.isdigit():
                digits += char
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
        if len(parts) == 3:
            break
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _installed_sdk_version() -> str:
    try:
        return version("google-genai")
    except PackageNotFoundError:
        return "not-installed"


def _require_interactions_sdk() -> Any:
    installed = _installed_sdk_version()
    parsed = _parse_version(installed) if installed != "not-installed" else None
    if parsed is None or parsed < MIN_SDK:
        raise SpikeError("google_genai_below_2_3")
    try:
        from google import genai
    except ImportError:
        raise SpikeError("google_genai_not_importable") from None
    if not hasattr(genai, "Client"):
        raise SpikeError("google_genai_client_missing")
    return genai


def _redact_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in _REDACT_KEYS or "api_key" in lowered


def _public_attr_names(obj: Any, *, limit: int = 80) -> list[str]:
    return [name for name in dir(obj) if not name.startswith("_")][:limit]


def _output_text_presence(text: Any) -> dict[str, Any]:
    if not isinstance(text, str):
        return {"present": False, "length": 0}
    return {"present": bool(text), "length": len(text)}


def _model_field_names(obj: Any) -> list[str] | None:
    fields = getattr(type(obj), "model_fields", None)
    if fields is None:
        fields = getattr(obj, "model_fields", None)
    if not isinstance(fields, dict):
        return None
    return sorted(str(name) for name in fields)


def _model_dump_mapping(obj: Any) -> dict[str, Any] | None:
    dump = getattr(obj, "model_dump", None)
    if not callable(dump):
        return None
    try:
        dumped = dump()
    except TypeError:
        try:
            dumped = dump(mode="python")
        except Exception:
            return None
    except Exception:
        return None
    if isinstance(dumped, dict):
        return dumped
    return None


def _is_usage_numeric_key(name: str) -> bool:
    lowered = name.lower()
    if lowered in {item.lower() for item in _USAGE_ATTRS}:
        return True
    return "token" in lowered


def _enum_or_allowed_scalar(value: Any, *, key: str) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        raw = value.value
        if isinstance(raw, (bool, int, float)):
            return {
                "python_type": type(value).__name__,
                "name": value.name,
                "value": raw,
            }
        if isinstance(raw, str) and key.lower() in _STRUCTURAL_SCALAR_KEYS:
            return {
                "python_type": type(value).__name__,
                "name": value.name,
                "value": raw,
            }
        return {"python_type": type(value).__name__, "name": value.name}
    if isinstance(value, str) and key.lower() in _STRUCTURAL_SCALAR_KEYS:
        return value
    return None


def _structural_value(value: Any, *, depth: int, key: str | None = None) -> Any:
    if depth < 0:
        return {"python_type": type(value).__name__}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return _enum_or_allowed_scalar(value, key=key or "type")
    if isinstance(value, str):
        if key is not None and key.lower() in _STRUCTURAL_SCALAR_KEYS:
            return value
        return {"python_type": "str", "length": len(value)}
    if isinstance(value, dict):
        return {
            str(item_key): (
                "[redacted]"
                if _redact_key(str(item_key))
                else _structural_value(
                    item_value,
                    depth=depth - 1,
                    key=str(item_key),
                )
            )
            for item_key, item_value in list(value.items())[:_DEEP_MAX_KEYS]
        }
    if isinstance(value, (list, tuple)):
        return [
            _structural_value(item, depth=depth - 1)
            for item in list(value)[:_DEEP_MAX_ITEMS]
        ]
    snapshot: dict[str, Any] = {
        "python_type": type(value).__name__,
        "python_module": type(value).__module__,
        "public_attr_names": _public_attr_names(value, limit=40),
    }
    for attr in _STRUCTURAL_SCALAR_KEYS | set(_ERROR_ATTRS) | set(_USAGE_ATTRS):
        if not hasattr(value, attr):
            continue
        snapshot[attr] = _structural_value(
            getattr(value, attr),
            depth=depth - 1,
            key=attr,
        )
    return snapshot


def _candidate_field_names(obj: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    dump = _model_dump_mapping(obj)
    if dump is not None:
        names.extend(str(key) for key in dump)
    fields = _model_field_names(obj)
    if fields is not None:
        names.extend(fields)
    if isinstance(obj, dict):
        names.extend(str(key) for key in obj)
    for name in names:
        seen.add(name)
    ordered = list(dict.fromkeys(names))
    extra = [
        name
        for name in (
            "code",
            "content",
            "error",
            "errors",
            "finish_reason",
            "incomplete",
            "incomplete_details",
            "incomplete_reason",
            "status",
            "steps",
            "stop_reason",
            "type",
            "usage",
        )
        if name not in seen
        and (isinstance(obj, dict) and name in obj or hasattr(obj, name))
    ]
    return (ordered + extra)[:_DEEP_MAX_KEYS]


def _deep_inspect(obj: Any, *, depth: int = _DEEP_MAX_DEPTH) -> dict[str, Any]:
    node: dict[str, Any] = {
        "python_type": type(obj).__name__,
        "python_module": type(obj).__module__,
        "public_field_names": _public_attr_names(obj),
        "model_fields": _model_field_names(obj),
        "model_dump_keys": None,
        "structural_scalars": {},
        "numeric_fields": {},
        "nested": {},
    }
    if obj is None or isinstance(obj, (bool, int, float, str, Enum)):
        node["structural_scalars"] = {
            "value": _enum_or_allowed_scalar(obj, key="type")
            if not isinstance(obj, str)
            else {"python_type": "str", "length": len(obj)}
        }
        return node

    dump = _model_dump_mapping(obj)
    if dump is not None:
        node["model_dump_keys"] = sorted(str(key) for key in dump)

    if depth < 0:
        return node

    for name in _candidate_field_names(obj):
        if _redact_key(name) and name.lower() not in {"content", "summary"}:
            node["nested"][name] = {"redacted": True, "python_type": "omitted"}
            continue
        value = _attr(obj, name)
        if callable(value):
            continue
        scalar = _enum_or_allowed_scalar(value, key=name)
        if scalar is not None and (
            name.lower() in _STRUCTURAL_SCALAR_KEYS
            or isinstance(value, (bool, int, float))
        ):
            if isinstance(value, (int, float)) and _is_usage_numeric_key(name):
                node["numeric_fields"][name] = value
            elif name.lower() in _STRUCTURAL_SCALAR_KEYS or isinstance(value, Enum):
                node["structural_scalars"][name] = scalar
            elif isinstance(value, (bool, int, float)):
                if _is_usage_numeric_key(name):
                    node["numeric_fields"][name] = value
            continue
        if isinstance(value, (int, float)) and _is_usage_numeric_key(name):
            node["numeric_fields"][name] = value
            continue
        if isinstance(value, dict):
            node["nested"][name] = {
                "python_type": "dict",
                "keys": sorted(str(key) for key in list(value)[:_DEEP_MAX_KEYS]),
                "items": {
                    str(item_key): _deep_inspect(item_value, depth=depth - 1)
                    if not _redact_key(str(item_key))
                    else {"redacted": True}
                    for item_key, item_value in list(value.items())[:_DEEP_MAX_KEYS]
                },
            }
            continue
        if isinstance(value, (list, tuple)):
            node["nested"][name] = {
                "python_type": type(value).__name__,
                "length": len(value),
                "item_python_types": [
                    type(item).__name__ for item in list(value)[:_DEEP_MAX_ITEMS]
                ],
                "items": [
                    _deep_inspect(item, depth=depth - 1)
                    for item in list(value)[:_DEEP_MAX_ITEMS]
                ],
            }
            continue
        if value is None:
            continue
        if isinstance(value, str):
            continue
        node["nested"][name] = _deep_inspect(value, depth=depth - 1)
    return node


def _usage_snapshot(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    snapshot: dict[str, Any] = {
        "python_type": type(usage).__name__,
        "public_attr_names": _public_attr_names(usage, limit=40),
    }
    source_items: list[tuple[str, Any]]
    if isinstance(usage, dict):
        source_items = list(usage.items())
    else:
        source_items = [
            (attr, getattr(usage, attr))
            for attr in _USAGE_ATTRS
            if hasattr(usage, attr)
        ]
        dump = _model_dump_mapping(usage)
        if dump is not None:
            source_items.extend(dump.items())
    seen: set[str] = set()
    for key, value in source_items:
        name = str(key)
        if name in seen or _redact_key(name):
            continue
        seen.add(name)
        if isinstance(value, (int, float, bool)) or value is None:
            snapshot[name] = value
    return snapshot


def _step_types(steps: Any) -> list[Any]:
    if not steps:
        return []
    types: list[Any] = []
    for step in steps:
        if isinstance(step, dict):
            types.append(step.get("type"))
            continue
        types.append(getattr(step, "type", None))
    return types


def _attr(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _error_snapshot(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    snapshot: dict[str, Any] = {
        "python_type": type(obj).__name__,
        "python_module": type(obj).__module__,
        "public_attr_names": _public_attr_names(obj, limit=40),
    }
    for attr in _ERROR_ATTRS:
        if isinstance(obj, dict) and attr in obj:
            value = obj[attr]
        elif hasattr(obj, attr):
            value = getattr(obj, attr)
        else:
            continue
        if _redact_key(attr):
            continue
        snapshot[attr] = _structural_value(value, depth=2, key=attr)
    return snapshot


def _incomplete_discriminator(interaction: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for attr in _INCOMPLETE_ATTRS:
        value = _attr(interaction, attr)
        if value is not None:
            found[attr] = _structural_value(value, depth=2, key=attr)
    return found


def _safe_provider_error(exc: BaseException) -> dict[str, Any] | None:
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None

    error = body.get("error")
    if not isinstance(error, dict):
        return None

    payload: dict[str, Any] = {}

    code = error.get("code")
    if isinstance(code, (str, int, float, bool)) or code is None:
        payload["code"] = code

    message = error.get("message")
    if isinstance(message, str):
        payload["message"] = message

    return payload or None


def _exception_diagnostics(exc: BaseException) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "python_type": type(exc).__name__,
        "module": type(exc).__module__,
        "mro": [cls.__name__ for cls in type(exc).mro()[:8]],
        "public_attr_names": _public_attr_names(exc, limit=40),
    }
    for attr in _ERROR_ATTRS:
        if not hasattr(exc, attr):
            continue
        payload[attr] = _structural_value(
            getattr(exc, attr),
            depth=2,
            key=attr,
        )
    provider_error = _safe_provider_error(exc)
    if provider_error is not None:
        payload["provider_error"] = provider_error

    response = getattr(exc, "response", None)
    if response is not None:
        payload["response"] = {
            "python_type": type(response).__name__,
            "status_code": getattr(response, "status_code", None),
            "public_attr_names": _public_attr_names(response, limit=20),
        }
    return payload


def _interaction_diagnostics(interaction: Any) -> dict[str, Any]:
    output_text = _attr(interaction, "output_text")
    steps = _attr(interaction, "steps")
    step_nodes: list[dict[str, Any]] = []
    if steps:
        for step in list(steps)[:_DEEP_MAX_ITEMS]:
            step_nodes.append(_deep_inspect(step))
    return {
        "returned_as": "interaction",
        "python_type": type(interaction).__name__,
        "python_module": type(interaction).__module__,
        "status": _attr(interaction, "status"),
        "id_present": bool(_attr(interaction, "id")),
        "error": _error_snapshot(_attr(interaction, "error")),
        "errors": _structural_value(
            _attr(interaction, "errors"), depth=2, key="errors"
        ),
        "step_types": _step_types(steps),
        "step_count": len(steps) if steps is not None else None,
        "output_text": _output_text_presence(
            output_text if isinstance(output_text, str) else None
        ),
        "usage": _usage_snapshot(_attr(interaction, "usage")),
        "incomplete_related_attrs": _incomplete_discriminator(interaction),
        "public_attr_names": _public_attr_names(interaction),
        "deep_structure": {
            "purpose": (
                "Discover typed incomplete/reason fields on Interaction and steps"
            ),
            "interaction": _deep_inspect(interaction),
            "steps": step_nodes,
        },
    }


def _call_create(client: Any, **kwargs: Any) -> dict[str, Any]:
    request_shape = {
        "api_version": "v1",
        "store": False,
        "stream": False,
        "model": kwargs.get("model"),
        "has_input": "input" in kwargs,
        "generation_config_keys": sorted(
            (kwargs.get("generation_config") or {}).keys()
        ),
        "input_kinds": _input_kinds(kwargs.get("input")),
    }
    try:
        interaction = client.interactions.create(**kwargs, store=False, stream=False)
    except Exception as exc:  # noqa: BLE001 — spike must capture any SDK/API error shape
        return {
            "ok": False,
            "delivery": "python_exception",
            "request_shape": request_shape,
            "exception": _exception_diagnostics(exc),
        }
    return {
        "ok": True,
        "delivery": "returned_interaction",
        "request_shape": request_shape,
        "interaction": _interaction_diagnostics(interaction),
    }


def _input_kinds(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return ["string"]
    if not isinstance(value, list):
        return [type(value).__name__]
    kinds: list[str] = []
    for item in value:
        if isinstance(item, dict):
            kinds.append(str(item.get("type") or "dict"))
        else:
            kinds.append(type(item).__name__)
    return kinds


def _make_client(genai: Any, api_key: str) -> Any:
    return genai.Client(
        api_key=api_key,
        http_options={"api_version": "v1"},
    )


def _case_success(client: Any, model: str) -> dict[str, Any]:
    return _call_create(
        client,
        model=model,
        input=f"Reply with exactly: {SPIKE_TEXT_MARKER}",
    )


def _case_incomplete(client: Any, model: str) -> dict[str, Any]:
    return _call_create(
        client,
        model=model,
        input=(
            "Write a long explanation of archival paper conservation, "
            "at least twenty sentences."
        ),
        generation_config={"max_output_tokens": TINY_MAX_OUTPUT_TOKENS},
    )


def _inline_image_part(image_path: Path) -> dict[str, str]:
    suffix = image_path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(suffix)
    if mime is None:
        raise SpikeError("unsupported_image_suffix")
    raw = image_path.read_bytes()
    return {
        "type": "image",
        "data": base64.b64encode(raw).decode("ascii"),
        "mime_type": mime,
    }


def _case_ocr_shape(client: Any, model: str, image_path: Path) -> dict[str, Any]:
    image_part = _inline_image_part(image_path)
    result = _call_create(
        client,
        model=model,
        input=[
            {"type": "text", "text": SPIKE_OCR_INSTRUCTION},
            image_part,
        ],
    )
    result["inline_image_request"] = {
        "python_form": (
            'input=[{"type": "text", "text": "..."}, '
            '{"type": "image", "data": "<base64>", "mime_type": "<mime>"}]'
        ),
        "image_bytes": len(image_path.read_bytes()),
        "mime_type": image_part["mime_type"],
        "resolution_field": "unset",
        "note": (
            "Official inline image shape from "
            "https://ai.google.dev/gemini-api/docs/image-understanding"
        ),
    }
    return result


def _read_prompt_file(prompt_file: Path) -> str:
    if not prompt_file.is_file():
        raise SpikeError("prompt_file_not_found")
    text = prompt_file.read_text(encoding="utf-8")
    if not text.strip():
        raise SpikeError("prompt_file_empty")
    return text


def _explicit_probe_generation_config(args: argparse.Namespace) -> dict[str, Any]:
    """Include only generation_config keys the caller passed. No invented equivalents."""
    config: dict[str, Any] = {}
    if args.temperature is not None:
        config["temperature"] = args.temperature
    if args.top_p is not None:
        config["top_p"] = args.top_p
    if args.top_k is not None:
        config["top_k"] = args.top_k
    if args.max_output_tokens is not None:
        config["max_output_tokens"] = args.max_output_tokens
    if args.thinking_level is not None:
        config["thinking_level"] = args.thinking_level
    return config


def _generation_config_flags_present(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (
            args.temperature,
            args.top_p,
            args.top_k,
            args.max_output_tokens,
            args.thinking_level,
        )
    )


def _case_recitation_probe(
    client: Any,
    *,
    model: str,
    image_path: Path,
    prompt_file: Path,
    generation_config: dict[str, Any],
) -> dict[str, Any]:
    """Generic image+prompt probe. No document-specific logic."""
    prompt = _read_prompt_file(prompt_file)
    image_part = _inline_image_part(image_path)
    create_kwargs: dict[str, Any] = {
        "model": model,
        "input": [
            {"type": "text", "text": prompt},
            image_part,
        ],
    }
    if generation_config:
        create_kwargs["generation_config"] = generation_config
    result = _call_create(client, **create_kwargs)
    result["recitation_probe"] = {
        "diagnostic_only": True,
        "document_specific_logic": False,
        "prompt_file_chars": len(prompt),
        "image_bytes": len(image_path.read_bytes()),
        "mime_type": image_part["mime_type"],
        "resolution_field": "unset",
        "model": model,
        "api_version": "v1",
        "store": False,
        "stream": False,
        "generation_config_sent": bool(generation_config),
        "generation_config_keys": sorted(generation_config.keys()),
        "generation_config_values": dict(generation_config),
    }
    return result


def _case_negative_control(client: Any, model: str) -> dict[str, Any]:
    """Benign negative control. Does not send policy-violating content.

    Success or a missing generation-blocked code is NOT evidence of how
    recitation/safety (or other documented generation-blocked codes) are
    delivered. Those codes are documented at:
    https://ai.google.dev/gemini-api/docs/api-errors
    This case never attempts to force a block.
    """
    result = _call_create(
        client,
        model=model,
        input="Reply with exactly: preflight-negative-control-ok",
    )
    result["negative_control"] = {
        "role": "negative_control",
        "attempted_force": False,
        "benign": True,
        "evidence": (
            "success_or_not_observed_is_not_evidence_of_generation_blocked_delivery"
        ),
        "documented_generation_blocked_codes": [
            "safety",
            "recitation",
            "language",
            "prohibited_content",
            "spii",
            "blocklist",
            "image_safety",
            "content_blocked",
        ],
    }
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnostic spike for Gemini Interactions v1 "
            "(store=False, google-genai >= 2.3). "
            "Does not change production OCR."
        )
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Optional dotenv file (does not override existing env vars)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model id. Defaults to gemini-2.5-flash-lite for built-in cases. "
            "Required for recitation-probe."
        ),
    )
    parser.add_argument(
        "--image",
        type=Path,
        default=None,
        help=(
            "Image path. Optional for ocr-shape (repo fixture default). "
            "Required for recitation-probe."
        ),
    )
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=None,
        help="UTF-8 prompt file. Required for recitation-probe. Never printed.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="recitation-probe only. Sent as generation_config.temperature if set.",
    )
    parser.add_argument(
        "--top-p",
        type=float,
        default=None,
        dest="top_p",
        help="recitation-probe only. Sent as generation_config.top_p if set.",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        dest="top_k",
        help=(
            "recitation-probe only. Sent as generation_config.top_k if set. "
            "Not translated or dropped if the API rejects it."
        ),
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        dest="max_output_tokens",
        help="recitation-probe only. Sent as generation_config.max_output_tokens if set.",
    )
    parser.add_argument(
        "--thinking-level",
        choices=("minimal", "low", "medium", "high"),
        default=None,
        dest="thinking_level",
        help=(
            "recitation-probe only. Sent as generation_config.thinking_level if set. "
            "Not a substitute for production thinking_budget."
        ),
    )
    parser.add_argument(
        "--case",
        action="append",
        choices=[
            "success",
            "incomplete",
            "ocr-shape",
            "negative-control",
            "blocked-probe",
            "recitation-probe",
            "all",
        ],
        help=(
            "Repeatable. Default: built-in cases (not recitation-probe). "
            "blocked-probe is an alias of negative-control."
        ),
    )
    return parser


def _selected_cases(raw: list[str] | None) -> list[str]:
    if not raw:
        return list(_BUILTIN_CASES)
    selected: list[str] = []
    if "all" in raw:
        selected.extend(_BUILTIN_CASES)
    for name in raw:
        if name in {"all"}:
            continue
        mapped = "negative-control" if name == "blocked-probe" else name
        if mapped not in selected:
            selected.append(mapped)
    return selected


def _json_default(value: Any) -> dict[str, str]:
    return {"python_type": type(value).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        _load_env_file(args.env_file)
        genai = _require_interactions_sdk()
        api_key = _api_key()
        client = _make_client(genai, api_key)
        cases = _selected_cases(args.case)
        diagnostic_model = args.model or DEFAULT_DIAGNOSTIC_MODEL
        ocr_image = (
            args.image.expanduser().resolve()
            if args.image is not None
            else _default_fixture()
        )
        if "ocr-shape" in cases and not ocr_image.is_file():
            raise SpikeError("image_not_found")
        probe_generation_config = _explicit_probe_generation_config(args)
        if _generation_config_flags_present(args) and "recitation-probe" not in cases:
            raise SpikeError("generation_config_flags_require_recitation_probe")
        recitation_probe_paths: tuple[Path, Path] | None = None
        if "recitation-probe" in cases:
            if args.model is None:
                raise SpikeError("recitation_probe_model_required")
            if args.image is None:
                raise SpikeError("recitation_probe_image_required")
            if args.prompt_file is None:
                raise SpikeError("recitation_probe_prompt_file_required")
            probe_image = args.image.expanduser().resolve()
            if not probe_image.is_file():
                raise SpikeError("image_not_found")
            prompt_file = args.prompt_file.expanduser().resolve()
            recitation_probe_paths = (probe_image, prompt_file)

        report: dict[str, Any] = {
            "spike": "gemini_interactions_sdk_preflight",
            "sdk_version": _installed_sdk_version(),
            "sdk_requirement": ">=2.3.0",
            "api_version": "v1",
            "store": False,
            "stream": False,
            "diagnostic_model": diagnostic_model,
            "cases": {},
        }
        runners = {
            "success": lambda: _case_success(client, diagnostic_model),
            "incomplete": lambda: _case_incomplete(client, diagnostic_model),
            "ocr-shape": lambda: _case_ocr_shape(client, diagnostic_model, ocr_image),
            "negative-control": lambda: _case_negative_control(
                client, diagnostic_model
            ),
        }
        if "recitation-probe" in cases:
            if recitation_probe_paths is None or args.model is None:
                raise SpikeError("recitation_probe_internal_state")
            probe_image, prompt_file = recitation_probe_paths
            probe_model = args.model
            runners["recitation-probe"] = lambda: _case_recitation_probe(
                client,
                model=probe_model,
                image_path=probe_image,
                prompt_file=prompt_file,
                generation_config=probe_generation_config,
            )
        for name in cases:
            report["cases"][name] = runners[name]()
        print(json.dumps(report, indent=2, default=_json_default, ensure_ascii=False))
        return 0
    except SpikeError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "spike_error_kind": exc.kind,
                    "sdk_version": _installed_sdk_version(),
                    "sdk_requirement": ">=2.3.0",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
