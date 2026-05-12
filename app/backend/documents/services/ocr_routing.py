from __future__ import annotations

import os
from dataclasses import dataclass

from documents.models import Document, DocumentTextResult


@dataclass(frozen=True)
class OcrRouteConfig:
    engine_key: str
    prompt_variant: str


OCR_ROUTES: dict[tuple[str, str], OcrRouteConfig] = {
    ("he", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    ),
    ("he", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    ),
    ("en", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    ),
    ("en", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    ),
    ("fr", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    ),
    ("fr", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    ),
    ("ar", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    ),
    ("ar", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    ),
}


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip()
    if not v:
        return default
    v_lower = v.lower()
    if v_lower in ("1", "true", "yes", "y", "on"):
        return True
    if v_lower in ("0", "false", "no", "n", "off"):
        return False
    raise ValueError(
        f"Env var {name} must be a boolean (true/false). Got: {raw!r}"
    )


def select_ocr_route(language: str | None, text_input_type: str | None) -> OcrRouteConfig:
    lang = (language or "").strip().lower()
    if lang not in {v for v, _label in Document.Language.choices}:
        raise ValueError(f"Invalid or missing language for OCR routing: {language!r}")

    text_type = (text_input_type or "").strip().upper()
    valid_text_types = {v for v, _label in Document.TextInputType.choices}
    if text_type not in valid_text_types:
        raise ValueError(f"Invalid or missing text_input_type for OCR routing: {text_input_type!r}")

    route = OCR_ROUTES.get((lang, text_type))
    if route is None:
        raise ValueError(f"No OCR route configured for language={lang!r}, text_input_type={text_type!r}")

    dev_ocr_route = _env_bool("TRANSKRIBUS_DEV_OCR_ROUTE", default=False)
    if not dev_ocr_route:
        return route

    if lang != Document.Language.HEBREW or text_type != Document.TextInputType.HANDWRITTEN:
        return route

    if _env_bool("TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT", default=False):
        raise ValueError(
            "TRANSKRIBUS_DEV_OCR_ROUTE is enabled but TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=true. "
            "Dev OCR routing to Transkribus is only supported with upload mode "
            "(TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT must be false)."
        )

    if not _env_bool("TRANSKRIBUS_DEV_UPLOAD_MODE", default=False):
        raise ValueError(
            "TRANSKRIBUS_DEV_OCR_ROUTE is enabled but TRANSKRIBUS_DEV_UPLOAD_MODE is not true. "
            "Set TRANSKRIBUS_DEV_UPLOAD_MODE=true so the Transkribus adapter upload path matches routing."
        )

    return OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=route.prompt_variant,
    )
