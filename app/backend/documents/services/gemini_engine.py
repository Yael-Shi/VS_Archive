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


_OCR_STRICT_PROMPT = (
    "You are a strict OCR engine.\n"
    "TASK: Transcribe ONLY what is visibly written in the image.\n"
    "RULES:\n"
    "- Do NOT add, infer, guess, complete, paraphrase, translate, summarize, or explain.\n"
    "- Do NOT fix spelling or grammar.\n"
    "- Preserve line breaks and punctuation as seen.\n"
    "- If any character/word/line is unclear, output the token [UNCLEAR] in its place.\n"
    "- If the page contains no readable text, output exactly: [NO_TEXT]\n"
    "\n"
    "OUTPUT FORMAT (MUST be valid JSON ONLY, no markdown, no extra text):\n"
    '{"text": "...", "has_unclear": false, "unclear_count": 0}\n'
)

_REQUIRED_KEYS = ("text", "has_unclear", "unclear_count")


# ------------------------------------------------------------------ helpers

def _get_api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise GeminiError("Missing GEMINI_API_KEY")
    return key


def _create_client(api_key: str) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1"),
    )


def _build_generation_config(
    *,
    temperature: float,
    top_k: int,
    top_p: float,
    max_output_tokens: Optional[int],
) -> types.GenerateContentConfig:
    cfg = types.GenerateContentConfig(
        temperature=temperature,
        top_k=top_k,
        top_p=top_p,
    )

    if max_output_tokens is not None:
        cfg.max_output_tokens = max_output_tokens

    return cfg


def _parse_page_json_strict(raw: str, *, page_index: int) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    if raw.startswith("```"):
        raise GeminiError(
            f"Gemini returned fenced code instead of raw JSON on page {page_index}"
        )

    try:
        data = json.loads(raw)
    except Exception as e:
        raise GeminiError(
            f"Gemini returned non-JSON on page {page_index}: {e}. Raw: {raw[:200]!r}"
        ) from e

    if not isinstance(data, dict):
        raise GeminiError(f"Gemini JSON is not object on page {page_index}")

    for k in _REQUIRED_KEYS:
        if k not in data:
            raise GeminiError(f"Gemini JSON missing '{k}' on page {page_index}")

    if not isinstance(data["text"], str):
        raise GeminiError(f"'text' not string on page {page_index}")

    if not isinstance(data["has_unclear"], bool):
        raise GeminiError(f"'has_unclear' not bool on page {page_index}")

    if not isinstance(data["unclear_count"], int):
        raise GeminiError(f"'unclear_count' not int on page {page_index}")

    computed_unclear = data["text"].count("[UNCLEAR]")
    data["_unclear_mismatch"] = abs(computed_unclear - data["unclear_count"]) > 2

    if computed_unclear > 0 and not data["has_unclear"]:
        data["has_unclear"] = True

    return data


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _similarity_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


# ------------------------------------------------------------------ main

def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-2.5-flash",
    min_text_length: int = 20,
    double_pass: bool = True,
    consistency_min_ratio: float = 0.92,
    temperature: float = 0.0,
    top_k: int = 1,
    top_p: float = 0.2,
    max_output_tokens: Optional[int] = None,
) -> GeminiResult:

    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: List[str] = []
    any_review = False

    for page in pages:
        prompt = _OCR_STRICT_PROMPT
        if language_hint:
            prompt += f"\nLanguage hint: {language_hint}."

        def _run_once():
            resp = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(
                        data=page.image_bytes,
                        mime_type=page.mime_type or "image/png",
                    ),
                ],
                config=_build_generation_config(
                    temperature=temperature,
                    top_k=top_k,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                ),
            )
            raw = (getattr(resp, "text", None) or "").strip()
            return _parse_page_json_strict(raw, page_index=page.page_index)

        data1 = _run_once()
        text1 = data1["text"].strip()

        if not text1:
            raise GeminiError(f"Empty OCR text on page {page.page_index}")

        page_review = False

        if len(text1) < min_text_length:
            page_review = True

        if data1.get("has_unclear"):
            page_review = True

        if data1.get("_unclear_mismatch"):
            page_review = True

        if text1 == "[NO_TEXT]":
            page_review = True

        if double_pass:
            data2 = _run_once()
            text2 = data2["text"].strip()

            if not text2:
                page_review = True
            else:
                ratio = _similarity_ratio(text1, text2)
                if ratio < consistency_min_ratio:
                    page_review = True
                    if data2.get("unclear_count", 0) > data1.get("unclear_count", 0):
                        text1 = text2

        texts.append(text1)
        any_review = any_review or page_review

    full_text = "\n\n".join(texts).strip()

    if not full_text:
        raise GeminiError("Gemini returned empty full text")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name="gemini_2_5_flash",
    )
