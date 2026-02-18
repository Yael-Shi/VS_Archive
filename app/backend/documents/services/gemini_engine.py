from __future__ import annotations

import json
import os
import re
import time
import logging
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from google import genai
from google.genai import types

from documents.services.page_extraction import PageImage

logger = logging.getLogger(__name__)

class GeminiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiResult:
    text: str
    needs_review: bool = False
    engine_name: str = "gemini-2.0-flash"
    review_reasons: List[str] = field(default_factory=list)


_HTR_EXPERT_PROMPT = (
    "You are an expert paleographer and historian.\n"
    "TASK: Transcribe the text from the image as accurately as possible.\n"
    "RULES:\n"
    "- This is handwritten text. Use linguistic and historical context to decipher difficult words.\n"
    "- If a word is partially legible, provide your best educated guess.\n"
    "- If a word is completely illegible, output the token [UNCLEAR].\n"
    "- Preserve line breaks and original structure.\n"
    "- Do NOT summarize, explain, or fix grammar. Output ONLY the transcription.\n"
    "\n"
    "OUTPUT FORMAT (MUST be valid JSON):\n"
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

def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())

def _similarity_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()

def _parse_page_json_strict(raw: str, *, page_index: int) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    had_fence = False
    if "```" in raw:
        had_fence = True
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
        else:
            raw = raw.replace("```json", "").replace("```", "").strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start : end + 1]

    try:
        data = json.loads(raw)
    except Exception as e:
        raise GeminiError(f"JSON Parse Error on page {page_index}: {e}")

    # Validation and defaults
    for k in _REQUIRED_KEYS:
        if k not in data:
            data[k] = "" if k == "text" else (False if k == "has_unclear" else 0)
    
    data["_had_fence"] = had_fence
    return data


# ------------------------------------------------------------------ main

def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    model_name: str = "gemini-2.0-flash",
    min_text_length: int = 20,
    double_pass: bool = False,
    consistency_min_ratio: float = 0.85,
    temperature: float = 0.2,
    top_k: int = 40,
    top_p: float = 0.95,
    max_output_tokens: Optional[int] = None,
) -> GeminiResult:
    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: List[str] = []
    any_review = False
    engine_reasons: List[str] = []
    had_fence_any = False

    for page in pages:
        prompt = _HTR_EXPERT_PROMPT
        if language_hint:
            prompt += f"\nLanguage hint: {language_hint}."

        def _run_once():
            attempts = 0
            while attempts < 2:
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_text(text=prompt),
                            types.Part.from_bytes(data=page.image_bytes, mime_type=page.mime_type or "image/png"),
                        ],
                        config=types.GenerateContentConfig(
                            temperature=temperature, top_k=top_k, top_p=top_p,
                            max_output_tokens=max_output_tokens,
                        ),
                    )
                    return _parse_page_json_strict(resp.text, page_index=page.page_index)
                except Exception as e:
                    err_str = str(e)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        wait_match = re.search(r"retry in ([\d\.]+)s", err_str)
                        wait_time = float(wait_match.group(1)) if wait_match else 30
                        logger.warning(f"Quota hit. Waiting {wait_time}s...")
                        time.sleep(wait_time + 1)
                        attempts += 1
                    else:
                        raise GeminiError(f"API Error: {err_str}")
            raise GeminiError(f"Quota exceeded for model {model_name} after retries.")

        data1 = _run_once()
        text1 = data1["text"].strip()
        
        page_review = False
        if len(text1) < min_text_length or data1.get("has_unclear") or data1.get("_had_fence"):
            page_review = True
        
        if data1.get("_had_fence"):
            had_fence_any = True

        if double_pass:
            data2 = _run_once()
            if _similarity_ratio(text1, data2["text"]) < consistency_min_ratio:
                page_review = True

        texts.append(text1)
        any_review = any_review or page_review

    full_text = "\n\n".join(texts).strip()
    if had_fence_any: engine_reasons.append("HAD_FENCE")

    return GeminiResult(
        text=full_text,
        needs_review=any_review,
        engine_name=model_name,
        review_reasons=engine_reasons,
    )
