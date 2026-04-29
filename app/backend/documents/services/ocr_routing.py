from __future__ import annotations

from dataclasses import dataclass

from documents.models import Document


@dataclass(frozen=True)
class OcrRouteConfig:
    engine_key: str
    prompt_variant: str


OCR_ROUTES: dict[tuple[str, str], OcrRouteConfig] = {
    ("he", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="handwritten"
    ),
    ("he", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="printed"
    ),
    ("en", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="handwritten"
    ),
    ("en", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="printed"
    ),
    ("fr", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="handwritten"
    ),
    ("fr", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="printed"
    ),
    ("ar", Document.TextInputType.HANDWRITTEN): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="handwritten"
    ),
    ("ar", Document.TextInputType.PRINTED): OcrRouteConfig(
        engine_key="GEMINI", prompt_variant="printed"
    ),
}


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
    return route
