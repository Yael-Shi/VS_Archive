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

from documents.models import DocumentTextResult
from documents.services.gemini_defaults import (
    DEFAULT_GEMINI_TEMPERATURE,
    DEFAULT_GEMINI_TOP_K,
    DEFAULT_GEMINI_TOP_P,
)
from documents.services.gemini_models import DEFAULT_GEMINI_MODEL
from documents.services.page_extraction import PageImage

logger = logging.getLogger(__name__)

class GeminiError(RuntimeError):
    pass


@dataclass(frozen=True)
class GeminiResult:
    text: str
    needs_review: bool = False
    engine_name: str = DEFAULT_GEMINI_MODEL
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

_PRINTED_TEXT_PROMPT = (
    "You are an OCR assistant for printed historical archival documents.\n"
    "TASK: Transcribe the meaningful printed text from the image as faithfully as possible.\n"
    "RULES:\n"
    "- Preserve line breaks and original structure as much as practical.\n"
    "- Preserve the original reading order of the document text.\n"
    "- Keep punctuation, spelling, wording, dates, names, URLs, email headers, quoted messages, footer text, and readable navigation text exactly as seen.\n"
    "- Preserve typos, non-standard spelling, unusual Hebrew forms, and apparent mistakes exactly as seen. Do not correct them.\n"
    "- Do NOT summarize, rewrite, modernize, normalize, correct grammar, correct punctuation, or improve the wording.\n"
    "- Do NOT add Hebrew vowel marks or diacritics unless they are clearly visible in the source.\n"
    "- Do NOT silently omit visible words, including unclear words, short words, or words at line endings.\n"
    "- Do NOT omit readable URLs, dates, email/listserv headers, or footer lines.\n"
    "- Pay special attention to short Hebrew words, Hebrew words at the end of lines, names, addresses, and personal details.\n"
    "- Do not replace an unclear name or personal detail with a more common word. If a name or word is visible but uncertain, include your best reading and mark uncertainty inline, for example [מילה?] or [?].\n"
    "- If text is completely unreadable, output the token [UNCLEAR].\n"
    "- For mixed Hebrew/English documents, preserve Hebrew and English document text in the order it appears. Do not reorder or clean up email/listserv formatting.\n"
    "- Ignore purely decorative UI icons, toolbar buttons, browser controls, and repeated icon placeholders unless they contain meaningful printed text.\n"
    "- Output ONLY the JSON object. Do not add explanations, summaries, comments, or markdown.\n"
    "\n"
    "OUTPUT FORMAT (MUST be valid JSON):\n"
    '{"text": "...", "has_unclear": false, "unclear_count": 0}\n'
)

_PROMPT_BY_VARIANT = {
    DocumentTextResult.OcrPromptVariant.HANDWRITTEN: _HTR_EXPERT_PROMPT,
    DocumentTextResult.OcrPromptVariant.PRINTED: _PRINTED_TEXT_PROMPT,
}

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

    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(.*?)\s*(?:```|$)", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
        else:
            raw = raw.replace("```json", "").replace("```", "").strip()

    if raw.startswith("{") and not raw.endswith("}"):
        logger.warning(f"Detected truncated JSON on page {page_index}, attempting to close it.")
        current = raw.rstrip()
        if not current.endswith('"'):
            current += '"'
        if not current.endswith('}'):
            current += '}'
        raw = current

    try:
        data = json.loads(raw, strict=False)
    except Exception as e:
        logger.error(f"JSON Parse Error on page {page_index}: {e}. Raw: {raw[:100]}...")
        raise GeminiError(f"JSON Parse Error on page {page_index}: {e}")

    # Validation and defaults
    for k in _REQUIRED_KEYS:
        if k not in data:
            if k == "text":
                data[k] = ""
            elif k == "has_unclear":
                data[k] = False
            else:
                data[k] = 0
    
    return data


# ------------------------------------------------------------------ main

def transcribe_pages_with_gemini(
    pages: List[PageImage],
    language_hint: Optional[str],
    *,
    prompt_variant: str,
    model_name: str = DEFAULT_GEMINI_MODEL,
    min_text_length: int = 20,
    double_pass: bool = False,
    consistency_min_ratio: float = 0.85,
    temperature: float = DEFAULT_GEMINI_TEMPERATURE,
    top_k: int = DEFAULT_GEMINI_TOP_K,
    top_p: float = DEFAULT_GEMINI_TOP_P,
    max_output_tokens: Optional[int] = 8192,
) -> GeminiResult:
    prompt_base = _PROMPT_BY_VARIANT.get(prompt_variant)
    if prompt_base is None:
        raise GeminiError(f"Unsupported Gemini prompt_variant: {prompt_variant!r}")

    api_key = _get_api_key()
    client = _create_client(api_key)

    texts: List[str] = []
    any_review = False
    engine_reasons: List[str] = []

    for page in pages:
        prompt = prompt_base
        if language_hint:
            prompt += f"\nLanguage hint: {language_hint}."

        success = False
        attempts = 0
        
        while not success and attempts < 2:
            try:
                resp = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=page.image_bytes, mime_type=page.mime_type or "image/png"),
                    ],
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        top_k=top_k,
                        top_p=top_p,
                        max_output_tokens=max_output_tokens,
                    ),
                )
                data = _parse_page_json_strict(resp.text, page_index=page.page_index)
                success = True
            except Exception as e:
                err_str = str(e).upper()
                if any(x in err_str for x in ["429", "RESOURCE_EXHAUSTED", "QUOTA"]):
                    if "LIMIT: 0" in err_str:
                        raise GeminiError(f"QUOTA_EXHAUSTED: {model_name}")
                    time.sleep(5)
                    attempts += 1
                else:
                    raise GeminiError(f"Gemini API Error: {e}")

        if not success:
            raise GeminiError(f"QUOTA_EXHAUSTED: {model_name} after retries")

        text = data["text"].strip()
        texts.append(text)
        if len(text) < min_text_length or data.get("has_unclear"):
            any_review = True

    return GeminiResult(
        text="\n\n".join(texts).strip(),
        needs_review=any_review,
        engine_name=model_name,
        review_reasons=engine_reasons,
    )
