from __future__ import annotations

import os
from dataclasses import dataclass

from documents.models import Document, DocumentTextResult
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL_CANDIDATES


@dataclass(frozen=True)
class OcrRouteConfig:
    engine_key: str
    prompt_variant: str


OCR_ROUTES: dict[tuple[str, str], OcrRouteConfig] = {
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

HEBREW_HANDWRITTEN_TRANSKRIBUS_ROUTE = OcrRouteConfig(
    engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
    prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
)

def gemini_model_candidates(
    route: OcrRouteConfig,
    *,
    language: str | None,
    text_input_type: str | None,
    gemini_hebrew_printed_model: str,
) -> tuple[str, ...]:
    """
    Resolve Gemini model candidates for a selected OCR route.

    Route-specific overrides live here so a future explicit
    (language, text_input_type) matrix can extend this helper without
    embedding language logic in GeminiAdapter.
    """
    if route.engine_key != DocumentTextResult.OcrEngineKey.GEMINI:
        return DEFAULT_GEMINI_MODEL_CANDIDATES

    lang = (language or "").strip().lower()
    text_type = (text_input_type or "").strip().upper()
    if (
        lang == Document.Language.HEBREW
        and text_type == Document.TextInputType.PRINTED
    ):
        return (gemini_hebrew_printed_model,)

    return DEFAULT_GEMINI_MODEL_CANDIDATES


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

    if (
        lang == Document.Language.HEBREW
        and text_type == Document.TextInputType.HANDWRITTEN
    ):
        if not _env_bool("ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN", default=False):
            raise ValueError(
                "Hebrew handwritten documents require Transkribus, but "
                "ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN is not enabled. "
                "Gemini fallback is not allowed for language='he' and "
                "text_input_type='HANDWRITTEN'."
            )
        return HEBREW_HANDWRITTEN_TRANSKRIBUS_ROUTE

    route = OCR_ROUTES.get((lang, text_type))
    if route is None:
        raise ValueError(f"No OCR route configured for language={lang!r}, text_input_type={text_type!r}")
    return route
