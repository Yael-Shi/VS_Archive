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


# ============================================================
# Hardening goals (v1-only):
# 1) Reduce creativity: temperature/top_k/top_p
# 2) Strict prompt: forbid completion / guessing
# 3) "Structured output" enforced client-side: JSON-only + strict parsing
# 4) OCR per-page (already implemented)
# 5) Consistency guard: optional double-pass per page
# ============================================================

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
    Create Gemini client using stable v1 API (NOT v1beta) – per requirement.
    """
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version="v1"),
    )


def _build_generation_config_plain() -> types.GenerateContentConfig:
    """
    Reduce creativity / hallucinations via deterministic-ish sampling.
    Note: v1 endpoint rejects structured-output fields (responseMimeType/responseSchema),
    so we enforce JSON via prompt + strict parsing.
    """
    return types.GenerateContentConfig(
        temperature=0.0,
        top_k=1,
        top_p=0.2,
    )


def _parse_page_json_strict(raw: str, *, page_index: int) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    # Hard fail if JSON is wrapped in markdown fences
    if raw.startswith("```"):
        raise GeminiError(
            f"Gemini returned fenced code instead of raw JSON on page {page_index}. "
            f"Raw (prefix): {raw[:80]!r}"
        )

    try:
        data = json.loads(raw)
    except Exception as e:
        raise GeminiError(
            f"Gemini returned non-JSON on page {page_index}: {e}. Raw (prefix): {raw[:200]!r}"
        ) from e

    if not isinstance(data, dict):
        raise GeminiError(f"Gemini JSON is not an object on page {page_index}: {data!r}")

    for k in _REQUIRED_KEYS:
        if k not in data:
            raise GeminiError(f"Gemini JSON missing '{k}' on page {page_index}: {data!r}")

    if not isinstance(data["text"], str):
        raise GeminiError(f"Gemini JSON 'text' not string on page {page_index}: {data!r}")
    if not isinstance(data["has_unclear"], bool):
        raise GeminiError(f"Gemini JSON 'has_unclear' not bool on page {page_index}: {data!r}")
    if not isinstance(data["unclear_count"], int):
        raise GeminiError(f"Gemini JSON 'unclear_count' not int on page {page_index}: {data!r}")

    # Sanity checks to detect suspicious outputs (mark only; don't hard fail)
    computed_unclear = data["text"].count("[UNCLEAR]")
    data["_computed_unclear"] = computed_unclear

    # If mismatch is huge, treat as suspicious (we'll use it to trigger review)
    data["_unclear_count_mismatch"] = abs(computed_unclear - data["unclear_count"]) > 2

    # If text contains [UNCLEAR] but has_unclear=false, fix it (conservative)
    if computed_unclear > 0 and data["has_unclear"] is False:
        data["has_unclear"] = True
        data["_has_unclear_fixed"] = True
    else:
        data["_has_unclear_fixed"] = False

    # Clamp negative counts defensively (shouldn't happen with int check)
    if data["unclear_count"] < 0:
        data["unclear_count"] = 0
        data["_unclear_count_clamped"] = True
    else:
        data["_unclear_count_clamped"] = False

    return data


def _normalize_text_for_compare(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)  # collapse whitespace
    return t


def _similarity_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize_text_for_compare(a), _normalize_text_for_compare(b)).ratio()


def _request_page_json_v1(
    client: genai.Client,
    *,
    model_name: str,
    page: PageImage,
    language_hint: Optional[str],
) -> Dict[str, Any]:
    lang = (language_hint or "").strip()
    lang_line = f"Language hint: {lang}." if lang else "Language hint: unknown."
    prompt = f"{_OCR_STRICT_PROMPT}\n{lang_line}\n"

    try:
        resp = client.models.generate_content(
            model=model_name,
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(
                    data=page.image_bytes,
                    mime_type=page.mime_type or "image/png",
                ),
            ],
            config=_build_generation_config_plain(),
        )
    except Exception as e:
        raise GeminiError(f"Gemini request failed on page {page.page_index}: {e}") from e

    raw = (getattr(resp, "text", None) or "").strip()
    return _parse_page_json_strict(raw, page_index=page.page_index)


def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-2.5-flash",
    min_text_length: int = 20,
    # Consistency guard (anti-hallucination / instability)
    double_pass: bool = True,
    consistency_min_ratio: float = 0.92,
) -> GeminiResult:
    """
    Gemini OCR (v1 only), per-page.

    Hardening included:
    - Low creativity sampling (temperature/top_k/top_p)
    - Strict prompt to forbid completion / invention
    - Client-side JSON-only parsing & validation
    - Optional double-pass consistency guard per page
    """
    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: list[str] = []
    any_review = False

    for p in pages:
        # Pass 1
        data1 = _request_page_json_v1(
            client,
            model_name=model_name,
            page=p,
            language_hint=language_hint,
        )
        text1 = (data1["text"] or "").strip()

        if not text1:
            raise GeminiError(f"Gemini returned empty OCR text on page {p.page_index}")

        # Review heuristics (no true confidence available)
        page_review = _guess_needs_review(text1, min_len=min_text_length)
        page_review = page_review or bool(data1.get("has_unclear", False))
        page_review = page_review or bool(data1.get("_unclear_count_mismatch", False))
        if text1 == "[NO_TEXT]":
            page_review = True

        # Pass 2 (consistency guard)
        if double_pass:
            data2 = _request_page_json_v1(
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
                    # Choose the more conservative output: prefer more [UNCLEAR]
                    unclear1 = int(data1.get("unclear_count", 0))
                    unclear2 = int(data2.get("unclear_count", 0))
                    if unclear2 > unclear1:
                        text1 = text2

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
