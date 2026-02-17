from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

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


# ---------------------------
# Strict OCR prompt + schema
# ---------------------------

_OCR_SYSTEM_PROMPT = (
    "You are a strict OCR engine.\n"
    "TASK: Transcribe ONLY what is visibly written in the image.\n"
    "RULES:\n"
    "- Do NOT add, infer, guess, paraphrase, translate, summarize, or explain.\n"
    "- Do NOT 'fix' spelling or grammar.\n"
    "- Preserve line breaks and punctuation as seen.\n"
    "- If any character/word/line is unclear, output the token [UNCLEAR] in its place.\n"
    "- If the page contains no readable text, output exactly: [NO_TEXT]\n"
)

# JSON schema for structured output (supported subset).
# We keep it small and strict to reduce "storytelling".
_PAGE_OCR_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "Exact transcription of the page. Use [UNCLEAR] for unreadable parts; [NO_TEXT] if none.",
        },
        "has_unclear": {
            "type": "boolean",
            "description": "True if any [UNCLEAR] token was used.",
        },
        "unclear_count": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of [UNCLEAR] tokens in the text.",
        },
    },
    "required": ["text", "has_unclear", "unclear_count"],
    "additionalProperties": False,
}


# ---------------------------
# Behavior controls
# ---------------------------

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


def _build_generation_config() -> types.GenerateContentConfig:
    """
    Reduce creativity / hallucinations via deterministic-ish sampling
    + force JSON structured output.
    """
    return types.GenerateContentConfig(
        temperature=0.0,
        top_k=1,
        top_p=0.2,
        response_mime_type="application/json",
        response_json_schema=_PAGE_OCR_SCHEMA,
    )


def _normalize_text_for_compare(text: str) -> str:
    """
    Normalize to compare two OCR outputs (ignore trivial whitespace diffs).
    """
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    return t


def _similarity_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_text_for_compare(a), _normalize_text_for_compare(b)).ratio()


def _parse_page_json(resp_text: str, *, page_index: int) -> Dict[str, Any]:
    try:
        data = json.loads((resp_text or "").strip())
    except Exception as e:
        raise GeminiError(f"Gemini returned non-JSON on page {page_index}: {e}. Raw: {resp_text!r}") from e

    if not isinstance(data, dict):
        raise GeminiError(f"Gemini returned JSON that is not an object on page {page_index}: {data!r}")

    # Minimal validation (since schema enforcement is best-effort).
    for k in ("text", "has_unclear", "unclear_count"):
        if k not in data:
            raise GeminiError(f"Gemini JSON missing key '{k}' on page {page_index}: {data!r}")

    if not isinstance(data["text"], str):
        raise GeminiError(f"Gemini JSON 'text' is not a string on page {page_index}: {data!r}")
    if not isinstance(data["has_unclear"], bool):
        raise GeminiError(f"Gemini JSON 'has_unclear' is not bool on page {page_index}: {data!r}")
    if not isinstance(data["unclear_count"], int):
        raise GeminiError(f"Gemini JSON 'unclear_count' is not int on page {page_index}: {data!r}")

    return data


def _request_page_ocr_json(
    client: genai.Client,
    *,
    model_name: str,
    page: PageImage,
    language_hint: Optional[str],
) -> Dict[str, Any]:
    lang = (language_hint or "").strip()
    lang_line = f"Language hint: {lang}." if lang else "Language hint: unknown."

    prompt = (
        f"{_OCR_SYSTEM_PROMPT}\n"
        f"{lang_line}\n"
        "Return ONLY valid JSON that matches the schema.\n"
    )

    try:
        resp = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(
                    data=page.image_bytes,
                    mime_type="image/png",
                ),
            ],
            config=_build_generation_config(),
        )
    except Exception as e:
        raise GeminiError(f"Gemini request failed on page {page.page_index}: {e}") from e

    raw = (getattr(resp, "text", None) or "").strip()
    if not raw:
        raise GeminiError(f"Gemini returned empty response on page {page.page_index}")

    data = _parse_page_json(raw, page_index=page.page_index)
    return data


def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-2.5-flash",
    min_text_length: int = 20,
    # Anti-hallucination guard:
    double_pass: bool = True,
    consistency_min_ratio: float = 0.92,
) -> GeminiResult:
    """
    OCR each page independently (already per-page).
    Adds:
    - deterministic sampling params (temperature/top_k/top_p)
    - strict prompt that forbids completing/inventing
    - structured JSON output (schema enforced best-effort)
    - optional double-pass consistency guard
    """
    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: list[str] = []
    any_review = False

    for p in pages:
        # First pass
        data1 = _request_page_ocr_json(
            client,
            model_name=model_name,
            page=p,
            language_hint=language_hint,
        )
        text1 = (data1["text"] or "").strip()

        if not text1:
            raise GeminiError(f"Gemini returned empty OCR text on page {p.page_index}")

        # Basic review heuristics:
        page_review = _guess_needs_review(text1, min_len=min_text_length)
        page_review = page_review or bool(data1.get("has_unclear", False))
        # also treat explicit [NO_TEXT] as review-worthy
        if text1.strip() == "[NO_TEXT]":
            page_review = True

        # Double-pass guard (same image twice, same config)
        if double_pass:
            data2 = _request_page_ocr_json(
                client,
                model_name=model_name,
                page=p,
                language_hint=language_hint,
            )
            text2 = (data2["text"] or "").strip()

            # If second pass is empty/invalid, mark review.
            if not text2:
                page_review = True
            else:
                ratio = _similarity_ratio(text1, text2)
                if ratio < consistency_min_ratio:
                    # outputs differ too much => likely hallucination/instability
                    page_review = True
                    # choose the "more conservative" output:
                    # prefer the one with more [UNCLEAR] (less guessing)
                    unclear1 = int(data1.get("unclear_count", 0))
                    unclear2 = int(data2.get("unclear_count", 0))
                    if unclear2 > unclear1:
                        text1 = text2  # swap to more conservative text

        texts.append(text1)
        any_review = any_review or page_review

    full_text = "\n\n".join(t.strip() for t in texts if t.strip()).strip()
    if not full_text:
        raise GeminiError("Gemini returned empty full text")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name="gemini_2_5_flash",
    )
