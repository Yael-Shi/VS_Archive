from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from google import genai
from google.genai import types

from documents.services.page_extraction import PageImage


class GeminiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini_2_5_flash"


def _guess_needs_review(text: str, *, min_len: int) -> bool:
    stripped = (text or "").strip()
    return len(stripped) < min_len


def _get_api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise GeminiError("Missing GEMINI_API_KEY")
    return key


def _create_client(api_key: str) -> genai.Client:
    """
    Create Gemini client using stable v1 API (not beta).
    """
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1"),
    )


def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-2.5-flash",
    min_text_length: int = 20,
) -> GeminiResult:
    api_key = _get_api_key()
    client = _create_client(api_key)

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
            resp = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(
                        data=p.image_bytes,
                        mime_type="image/png",
                    ),
                ],
            )
        except Exception as e:
            raise GeminiError(
                f"Gemini request failed on page {p.page_index}: {e}"
            ) from e

        page_text = (getattr(resp, "text", None) or "").strip()

        if not page_text:
            raise GeminiError(
                f"Gemini returned empty text on page {p.page_index}"
            )

        texts.append(page_text)
        any_review = any_review or _guess_needs_review(
            page_text, min_len=min_text_length
        )

    full_text = "\n\n".join(texts).strip()

    if not full_text:
        raise GeminiError("Gemini returned empty text")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name="gemini_2_5_flash",
    )
