from __future__ import annotations

import json
import logging
import os
import re
import time
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


_HANDWRITTEN_LATIN_PROMPT = (
    "You are an expert paleographer and historian.\n"
    "TASK: Transcribe the handwritten text in the image as faithfully as possible.\n"
    "RULES:\n"
    "- The text is in Latin script, typically English or French, and may include a mix of both.\n"
    "- Transcribe the entire visible page from top to bottom; do not stop after the first section or paragraph.\n"
    "- Transcribe what is written, not what the writer probably meant.\n"
    "- Preserve wording, spelling, grammar, punctuation, capitalization, line breaks, dates, names, places, abbreviations, and unusual forms exactly as seen.\n"
    "- Do not correct spelling, grammar, capitalization, punctuation, or wording.\n"
    "- Do not modernize, normalize, smooth into clean prose, rewrite, summarize, translate, or explain.\n"
    "- Preserve visible meaningful marks: hyphens, dashes, apostrophes, quotation marks, commas, periods, parentheses, slashes, page numbers, headings, insertions, crossings-out, marginal notes, and visible corrections.\n"
    "- Do not add punctuation or capitalization that is not visible in the source.\n"
    "- If a word is partly legible or uncertain, give your best reading and mark it inline with [?]. Use [?] readily for names, places, institutions, unusual words, and doubtful readings.\n"
    "- If a word is completely illegible, use [UNCLEAR].\n"
    "- Do not present doubtful readings as certain.\n"
    "- Output only the transcription text.\n"
    "- Do not output JSON, markdown, comments, explanations, labels, or introductory text.\n"
)

_HEBREW_TRANSLATION_PROMPT = (
    "You are a careful Hebrew translator for historical archival documents.\n"
    "\n"
    "TASK:\n"
    "Translate the source transcription into Hebrew.\n"
    "\n"
    "RULES:\n"
    "\n"
    "* The source below is an excerpt from a longer document. Translate the entire excerpt faithfully; omit nothing.\n"
    "* The <source_excerpt> tags are delimiters only. Do not translate, copy, or mention them in the output.\n"
    "* Translate the source text faithfully into Hebrew.\n"
    "* Translate closely to the source wording, syntax, structure, and level of clarity, even if the Hebrew is somewhat awkward.\n"
    "* When the source sentence is awkward, incomplete, or grammatically broken, keep the Hebrew sentence similarly awkward or incomplete rather than repairing it.\n"
    "* Do not summarize, shorten, expand, explain, or add historical context.\n"
    "* Do not polish the Hebrew into elegant modern prose.\n"
    "* Preserve all names, dates, places, institutions, numbers, lists, headings, paragraph structure, and uncertainty markers such as [?] and [UNCLEAR].\n"
    "* Do not silently fix factual mistakes in the source. Translate what the source says.\n"
    "* If the source contains spelling mistakes, grammar mistakes, awkward syntax, or non-standard wording, keep the Hebrew close to the source and preserve the awkwardness where possible.\n"
    "* Do not invent artificial Hebrew spelling mistakes just to imitate source spelling mistakes.\n"
    "* If an important source error or odd form cannot be reflected naturally in Hebrew, add [כך במקור] only when necessary.\n"
    "* Preserve original line breaks and list structure as much as practical.\n"
    "* If the source includes English and French, translate both into Hebrew.\n"
    "* Keep the output in Hebrew only, except for names, abbreviations, or unclear source tokens that should remain as written.\n"
    "* Output only the Hebrew translation text.\n"
    "* Do not output JSON, markdown, comments, explanations, labels, or introductory text.\n"
    "\n"
    "{{source_text}}\n"
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
    DocumentTextResult.OcrPromptVariant.HANDWRITTEN: _HANDWRITTEN_LATIN_PROMPT,
    DocumentTextResult.OcrPromptVariant.PRINTED: _PRINTED_TEXT_PROMPT,
}

_REQUIRED_KEYS = ("text", "has_unclear", "unclear_count")
_TRANSLATION_CHUNK_MAX_CHARS = 2200
_MIN_TRANSLATION_LENGTH_RATIO = 0.20

_LATIN_LANGUAGE_HINTS = frozenset(
    {"en", "eng", "english", "fr", "fra", "fre", "french"}
)


# ------------------------------------------------------------------ helpers

def _is_latin_language_hint(language_hint: Optional[str]) -> bool:
    if not language_hint:
        return False
    return language_hint.strip().lower() in _LATIN_LANGUAGE_HINTS


def _uses_plain_text_transcription(
    prompt_variant: str,
    language_hint: Optional[str],
) -> bool:
    return (
        prompt_variant == DocumentTextResult.OcrPromptVariant.HANDWRITTEN
        and _is_latin_language_hint(language_hint)
    )


def _get_api_key() -> str:
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise GeminiError("Missing GEMINI_API_KEY")
    return key


def _create_client(
    api_key: str,
    *,
    api_version: str = "v1",
) -> genai.Client:
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(api_version=api_version),
    )


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _similarity_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _is_retryable_gemini_response_error(exc: Exception) -> bool:
    message = str(exc).upper()
    return any(
        token in message
        for token in (
            "JSON PARSE ERROR",
            "EMPTY RESPONSE",
        )
    )


def _plain_text_response_to_page_data(raw: str, *, page_index: int) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    return {
        "text": text,
        "has_unclear": "[UNCLEAR]" in text or "[?]" in text,
        "unclear_count": text.count("[UNCLEAR]") + text.count("[?]"),
    }


def _split_oversized_block(block: str, *, max_chars: int) -> List[str]:
    parts: List[str] = []
    current = ""

    for line in block.splitlines(keepends=True):
        if current and len(current) + len(line) > max_chars:
            parts.append(current.strip())
            current = ""

        if len(line) > max_chars:
            if current.strip():
                parts.append(current.strip())
                current = ""

            for start in range(0, len(line), max_chars):
                part = line[start : start + max_chars].strip()
                if part:
                    parts.append(part)
            continue

        current += line

    if current.strip():
        parts.append(current.strip())

    return parts


def _split_text_for_translation(
    source_text: str,
    *,
    max_chars: int = _TRANSLATION_CHUNK_MAX_CHARS,
) -> List[str]:
    stripped = (source_text or "").strip()
    if not stripped:
        return []

    chunks: List[str] = []
    current_blocks: List[str] = []
    current_len = 0

    for block in re.split(r"\n\s*\n", stripped):
        block = block.strip()
        if not block:
            continue

        if len(block) > max_chars:
            if current_blocks:
                chunks.append("\n\n".join(current_blocks).strip())
                current_blocks = []
                current_len = 0

            chunks.extend(_split_oversized_block(block, max_chars=max_chars))
            continue

        projected_len = current_len + len(block) + (2 if current_blocks else 0)
        if current_blocks and projected_len > max_chars:
            chunks.append("\n\n".join(current_blocks).strip())
            current_blocks = [block]
            current_len = len(block)
        else:
            current_blocks.append(block)
            current_len = projected_len

    if current_blocks:
        chunks.append("\n\n".join(current_blocks).strip())

    return [chunk for chunk in chunks if chunk]


def _extract_finish_reason(resp: Any) -> Optional[str]:
    candidates = getattr(resp, "candidates", None)
    if not candidates:
        return None
    finish_reason = getattr(candidates[0], "finish_reason", None)
    if finish_reason is None:
        return None
    return str(finish_reason)


def _is_translation_chunk_truncated(
    source_chunk: str,
    translated_text: str,
    *,
    min_text_length: int,
) -> bool:
    if len(source_chunk) < 1000:
        return False
    min_expected_length = int(len(source_chunk) * _MIN_TRANSLATION_LENGTH_RATIO)
    return len(translated_text) < max(min_text_length, min_expected_length)


def _build_hebrew_translation_prompt(
    chunk: str,
    language_hint: Optional[str],
) -> str:
    wrapped_chunk = f"<source_excerpt>\n{chunk}\n</source_excerpt>"
    prompt = _HEBREW_TRANSLATION_PROMPT.replace("{{source_text}}", wrapped_chunk)
    if language_hint:
        prompt += f"\nSource language hint: {language_hint}."
    return prompt


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

    try:
        data = json.loads(raw, strict=False)
    except Exception as e:
        logger.error(
            "JSON Parse Error on page %s: %s. Raw: %s...",
            page_index,
            e,
            raw[:2000],
        )
        raise GeminiError(f"JSON Parse Error on page {page_index}: {e}")

    # Validation and defaults
    for key in _REQUIRED_KEYS:
        if key not in data:
            if key == "text":
                data[key] = ""
            elif key == "has_unclear":
                data[key] = False
            else:
                data[key] = 0

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

    uses_plain_text_transcription = _uses_plain_text_transcription(
        prompt_variant,
        language_hint,
    )

    api_key = _get_api_key()
    client = _create_client(
        api_key,
        api_version="v1beta" if uses_plain_text_transcription else "v1",
    )

    effective_temperature = 0.0 if uses_plain_text_transcription else temperature

    config_kwargs: Dict[str, Any] = {
        "temperature": effective_temperature,
        "top_k": top_k,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
    }
    if uses_plain_text_transcription:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=0,
        )

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
                        types.Part.from_bytes(
                            data=page.image_bytes,
                            mime_type=page.mime_type or "image/png",
                        ),
                    ],
                    config=types.GenerateContentConfig(**config_kwargs),
                )

                finish_reason = _extract_finish_reason(resp)
                output_text = (resp.text or "").strip()
                logger.info(
                    "Gemini transcription page completed: "
                    "page=%s output_length=%s finish_reason=%s model=%s",
                    page.page_index,
                    len(output_text),
                    finish_reason,
                    model_name,
                )

                if (
                    uses_plain_text_transcription
                    and finish_reason
                    and "MAX_TOKENS" in finish_reason.upper()
                ):
                    attempts += 1
                    if attempts < 2:
                        logger.warning(
                            "Retrying truncated Gemini transcription page %s "
                            "after MAX_TOKENS with model %s",
                            page.page_index,
                            model_name,
                        )
                        continue

                    raise GeminiError(
                        "Gemini transcription page reached MAX_TOKENS after retry: "
                        f"page_index={page.page_index}, "
                        f"output_length={len(output_text)}, "
                        f"finish_reason={finish_reason}, "
                        f"model={model_name}"
                    )

                if uses_plain_text_transcription:
                    data = _plain_text_response_to_page_data(
                        output_text,
                        page_index=page.page_index,
                    )
                else:
                    data = _parse_page_json_strict(
                        output_text,
                        page_index=page.page_index,
                    )

                success = True
            except GeminiError as exc:
                if _is_retryable_gemini_response_error(exc):
                    attempts += 1
                    if attempts < 2:
                        logger.warning(
                            "Retrying Gemini response format failure on page %s "
                            "with model %s: %s",
                            page.page_index,
                            model_name,
                            exc,
                        )
                        continue
                raise
            except Exception as exc:
                err_str = str(exc).upper()
                if any(
                    token in err_str
                    for token in ("429", "RESOURCE_EXHAUSTED", "QUOTA")
                ):
                    if "LIMIT: 0" in err_str:
                        raise GeminiError(f"QUOTA_EXHAUSTED: {model_name}")
                    time.sleep(5)
                    attempts += 1
                    continue

                raise GeminiError(f"Gemini API Error: {exc}")

        if not success:
            raise GeminiError(f"QUOTA_EXHAUSTED: {model_name} after retries")

        page_text = data["text"].strip()
        texts.append(page_text)
        if len(page_text) < min_text_length or data.get("has_unclear"):
            any_review = True

    return GeminiResult(
        text="\n\n".join(texts).strip(),
        needs_review=any_review,
        engine_name=model_name,
        review_reasons=engine_reasons,
    )


def translate_text_to_hebrew_with_gemini(
    source_text: str,
    language_hint: Optional[str],
    *,
    model_name: str = DEFAULT_GEMINI_MODEL,
    min_text_length: int = 20,
    temperature: float = 0.0,
    top_k: int = DEFAULT_GEMINI_TOP_K,
    top_p: float = DEFAULT_GEMINI_TOP_P,
    max_output_tokens: Optional[int] = 8192,
) -> GeminiResult:
    stripped_source = (source_text or "").strip()
    if not stripped_source:
        raise GeminiError("Cannot translate empty source text")

    chunks = _split_text_for_translation(stripped_source)
    if not chunks:
        raise GeminiError("Cannot translate empty source text")

    chunk_lengths = [len(chunk) for chunk in chunks]
    logger.info(
        "Gemini Hebrew translation starting: source_length=%s chunk_count=%s chunk_lengths=%s model=%s",
        len(stripped_source),
        len(chunks),
        chunk_lengths,
        model_name,
    )

    api_key = _get_api_key()
    client = _create_client(api_key, api_version="v1beta")

    translated_chunks: List[str] = []
    needs_review = False

    for index, chunk in enumerate(chunks, start=1):
        chunk_source_len = len(chunk)
        truncation_retries = 0
        translated_text = ""
        data: Dict[str, Any] = {}

        while True:
            prompt = _build_hebrew_translation_prompt(chunk, language_hint)

            success = False
            attempts = 0
            finish_reason: Optional[str] = None

            while not success and attempts < 2:
                try:
                    resp = client.models.generate_content(
                        model=model_name,
                        contents=[types.Part.from_text(text=prompt)],
                        config=types.GenerateContentConfig(
                            temperature=temperature,
                            top_k=top_k,
                            top_p=top_p,
                            max_output_tokens=max_output_tokens,
                            thinking_config=types.ThinkingConfig(thinking_budget=0),
                        ),
                    )
                    data = _plain_text_response_to_page_data(resp.text, page_index=index)
                    finish_reason = _extract_finish_reason(resp)
                    success = True
                except GeminiError as e:
                    if _is_retryable_gemini_response_error(e):
                        attempts += 1
                        if attempts < 2:
                            logger.warning(
                                "Retrying Gemini translation response format failure on chunk %s/%s with model %s: %s",
                                index,
                                len(chunks),
                                model_name,
                                e,
                            )
                            continue
                    raise
                except Exception as e:
                    err_str = str(e).upper()
                    if any(x in err_str for x in ["429", "RESOURCE_EXHAUSTED", "QUOTA"]):
                        if "LIMIT: 0" in err_str:
                            raise GeminiError(f"QUOTA_EXHAUSTED: {model_name}")
                        time.sleep(5)
                        attempts += 1
                        continue

                    raise GeminiError(f"Gemini API Error: {e}")

            if not success:
                raise GeminiError(f"QUOTA_EXHAUSTED: {model_name} after retries")

            translated_text = data["text"].strip()
            logger.info(
                "Gemini Hebrew translation chunk %s/%s: source_length=%s output_length=%s finish_reason=%s model=%s",
                index,
                len(chunks),
                chunk_source_len,
                len(translated_text),
                finish_reason,
                model_name,
            )

            if _is_translation_chunk_truncated(
                chunk,
                translated_text,
                min_text_length=min_text_length,
            ):
                if truncation_retries < 1:
                    truncation_retries += 1
                    logger.warning(
                        "Gemini Hebrew translation chunk %s/%s appears truncated; retrying once: source_length=%s output_length=%s model=%s",
                        index,
                        len(chunks),
                        chunk_source_len,
                        len(translated_text),
                        model_name,
                    )
                    continue

                raise GeminiError(
                    "Gemini Hebrew translation chunk appears truncated after retry: "
                    f"chunk_index={index}/{len(chunks)}, "
                    f"source_length={chunk_source_len}, "
                    f"translation_length={len(translated_text)}, "
                    f"finish_reason={finish_reason}, "
                    f"model={model_name}"
                )
            break

        translated_chunks.append(translated_text)
        if len(translated_text) < min_text_length or bool(data.get("has_unclear")):
            needs_review = True

    combined_text = "\n\n".join(translated_chunks).strip()
    min_expected_length = int(len(stripped_source) * _MIN_TRANSLATION_LENGTH_RATIO)
    if len(stripped_source) >= 1000 and len(combined_text) < max(min_text_length, min_expected_length):
        raise GeminiError(
            "Gemini Hebrew translation output appears truncated: "
            f"source_length={len(stripped_source)}, translation_length={len(combined_text)}"
        )

    return GeminiResult(
        text=combined_text,
        needs_review=needs_review,
        engine_name=model_name,
        review_reasons=[],
    )
