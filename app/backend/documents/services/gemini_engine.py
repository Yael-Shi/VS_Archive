from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
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
    # Reasons coming from the engine itself (protocol/format/guards)
    review_reasons: List[str] = field(default_factory=list)


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

# Used only on retry when Gemini returns non-JSON / malformed output
_OCR_STRICT_REPAIR_PROMPT_SUFFIX = (
    "\n\nIMPORTANT:\n"
    "- Return ONLY a JSON object.\n"
    "- Do NOT wrap in ``` fences.\n"
    "- Do NOT prefix with the word 'json'.\n"
    "- The first non-whitespace character MUST be '{' and the last MUST be '}'.\n"
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


def _strip_leading_json_label(raw: str) -> str:
    """
    Gemini sometimes returns:
        json\\n{...}
    without code fences. This is NOT JSON and breaks json.loads.
    We remove a single leading 'json' label line if present.
    """
    s = (raw or "").lstrip()
    if not s:
        return s

    first_line, sep, rest = s.partition("\n")
    fl = first_line.strip().lower()

    # Accept common variants like 'json' or 'json:' (but only if the remainder looks like JSON)
    if fl in ("json", "json:"):
        candidate = rest.lstrip()
        if candidate.startswith("{"):
            return candidate

    return (raw or "").strip()


def _parse_page_json_strict(raw: str, *, page_index: int) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    # Handle "json\n{...}" without code fences
    raw = _strip_leading_json_label(raw)

    had_fence = False

    # If the model wrapped JSON in markdown fences (``` or ```json), unwrap it safely.
    if raw.startswith("```"):
        had_fence = True
        lines = raw.splitlines()
        if len(lines) >= 3 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()
        else:
            raw = raw.strip("`").strip()

        # After stripping fences, also strip possible leading 'json' label
        raw = _strip_leading_json_label(raw)

    raw = raw.strip()

    # Extract first JSON object if there is extra text around it
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            raw = raw[start : end + 1].strip()
        elif start != -1 and end == -1:
            # Looks like it started JSON but got truncated before closing brace
            raise GeminiError(
                f"Gemini returned truncated JSON on page {page_index}. "
                f"Raw_len={len(raw)} Raw_prefix={raw[:200]!r}"
            )

    try:
        data = json.loads(raw)
    except Exception as e:
        # Add suffix too — helps detect truncation / fence leakage quickly
        prefix = raw[:200]
        suffix = raw[-200:] if len(raw) > 200 else raw
        raise GeminiError(
            f"Gemini returned non-JSON on page {page_index}: {e}. "
            f"Raw_len={len(raw)} Raw_prefix={prefix!r} Raw_suffix={suffix!r}"
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

    computed_unclear = data["text"].count("[UNCLEAR]")
    data["_computed_unclear"] = computed_unclear
    data["_unclear_count_mismatch"] = abs(computed_unclear - data["unclear_count"]) > 2

    if computed_unclear > 0 and data["has_unclear"] is False:
        data["has_unclear"] = True
        data["_has_unclear_fixed"] = True
    else:
        data["_has_unclear_fixed"] = False

    if data["unclear_count"] < 0:
        data["unclear_count"] = 0
        data["_unclear_count_clamped"] = True
    else:
        data["_unclear_count_clamped"] = False

    data["_had_fence"] = had_fence
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
    # NEW: format/JSON retries (uses env-driven worker config)
    max_retries: int = 0,
    retry_delay_seconds_1: int = 0,
    retry_delay_seconds_2: int = 0,
) -> GeminiResult:
    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: List[str] = []
    any_review = False

    # engine-level reasons we want to surface to admin backlog
    engine_reasons: List[str] = []
    had_fence_any = False
    had_format_retry_any = False

    def _sleep_for_retry(attempt_index: int) -> None:
        # attempt_index is 1-based: 1 => first retry
        if attempt_index <= 0:
            return
        if attempt_index == 1:
            delay = max(0, int(retry_delay_seconds_1))
        else:
            delay = max(0, int(retry_delay_seconds_2))
        if delay > 0:
            time.sleep(delay)

    def _run_once(page: PageImage, *, repair_mode: bool) -> Dict[str, Any]:
        prompt = _OCR_STRICT_PROMPT
        if language_hint:
            prompt += f"\nLanguage hint: {language_hint}."
        if repair_mode:
            prompt += _OCR_STRICT_REPAIR_PROMPT_SUFFIX

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

    def _run_with_format_retries(page: PageImage) -> Dict[str, Any]:
        nonlocal had_format_retry_any

        last_err: Optional[Exception] = None

        # attempt 0 = normal prompt
        # attempts 1..max_retries = repair prompt + optional sleep
        for attempt in range(0, max(0, int(max_retries)) + 1):
            repair_mode = attempt > 0
            if attempt > 0:
                had_format_retry_any = True
                _sleep_for_retry(attempt)

            try:
                return _run_once(page, repair_mode=repair_mode)
            except GeminiError as e:
                last_err = e
                # Only retry on protocol/format issues (which surface here as GeminiError)
                # If we've exhausted retries, re-raise.
                if attempt >= max(0, int(max_retries)):
                    raise
                continue
            except Exception as e:
                # Non-protocol errors: do not mask with retries
                raise

        # Should never reach here
        raise GeminiError(f"Gemini failed with retries on page {page.page_index}: {last_err}")

    for page in pages:
        data1 = _run_with_format_retries(page)
        text1 = data1["text"].strip()

        if not text1:
            raise GeminiError(f"Empty OCR text on page {page.page_index}")

        page_review = False

        if len(text1) < min_text_length:
            page_review = True

        if data1.get("has_unclear"):
            page_review = True

        if data1.get("_unclear_count_mismatch"):
            page_review = True

        if text1 == "[NO_TEXT]":
            page_review = True

        # Policy B: accept OCR but mark review if fenced JSON detected
        if data1.get("_had_fence"):
            page_review = True
            had_fence_any = True

        if double_pass:
            data2 = _run_with_format_retries(page)
            text2 = data2["text"].strip()

            if not text2:
                page_review = True
            else:
                ratio = _similarity_ratio(text1, text2)
                if ratio < consistency_min_ratio:
                    page_review = True
                    if data2.get("unclear_count", 0) > data1.get("unclear_count", 0):
                        text1 = text2

                # If 2nd pass also had fence, count it
                if data2.get("_had_fence"):
                    had_fence_any = True

        texts.append(text1)
        any_review = any_review or page_review

    full_text = "\n\n".join(texts).strip()
    if not full_text:
        raise GeminiError("Gemini returned empty full text")

    if had_fence_any:
        engine_reasons.append("HAD_FENCE")

    if had_format_retry_any:
        engine_reasons.append("FORMAT_RETRY")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name="gemini_2_5_flash",
        review_reasons=engine_reasons,
    )
