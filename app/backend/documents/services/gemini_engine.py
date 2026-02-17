from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import google.generativeai as genai

from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.page_extraction import PageImage


class GeminiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini_1_5_flash"


def _guess_needs_review(text: str, *, min_len: int) -> bool:
    stripped = (text or "").strip()
    return len(stripped) < min_len


def _get_api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise GeminiError("Missing GEMINI_API_KEY")
    return key


def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-1.5-flash",
) -> GeminiResult:
    """
    OCR/HTR via Gemini Vision. Input: list of PNG bytes (one per page).
    Output: a single concatenated text string.
    """
    try:
        cfg = validate_required_env()
    except EnvConfigError as e:
        raise GeminiError(f"Env config error: {e}") from e

    min_text_length = cfg.min_text_length

    api_key = _get_api_key()
    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(model_name)

    texts: list[str] = []
    any_review = False

    lang = (language_hint or "").strip()
    lang_line = f"Language hint: {lang}." if lang else "Language hint: unknown."

    for p in pages:
        prompt = (
            "You are an OCR engine.\n"
            "Extract the text EXACTLY as it appears in the image.\n"
            "Preserve line breaks and punctuation.\n"
            "Do NOT add explanations.\n"
            f"{lang_line}\n"
        )

        try:
            resp = model.generate_content(
                [
                    prompt,
                    {
                        "mime_type": "image/png",
                        "data": p.image_bytes,
                    },
                ]
            )
        except Exception as e:
            raise GeminiError(f"Gemini request failed on page {p.page_index}: {e}") from e

        page_text = (getattr(resp, "text", None) or "").strip()
        texts.append(page_text)
        any_review = any_review or _guess_needs_review(
            page_text,
            min_len=min_text_length,
        )

    full_text = "\n\n".join([t for t in texts if t]).strip()
    if not full_text:
        raise GeminiError("Gemini returned empty text")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name="gemini_1_5_flash",
    )
