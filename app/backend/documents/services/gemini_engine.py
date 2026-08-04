from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import Enum
from typing import Any, Dict, List, Optional, cast

from google import genai
from google.genai import types

from documents.models import DocumentTextResult
from documents.services.gemini_defaults import (
    DEFAULT_GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP,
    DEFAULT_GEMINI_TEMPERATURE,
    DEFAULT_GEMINI_TOP_K,
    DEFAULT_GEMINI_TOP_P,
)
from documents.services.gemini_models import (
    DEFAULT_GEMINI_MODEL,
    FRENCH_HANDWRITTEN_GEMINI_MODEL,
)
from documents.services.page_extraction import PageImage

logger = logging.getLogger(__name__)

GEMINI_OCR_PROMPT_CONTRACT_VERSION = "gemini-ocr-prompt-v1"

# Route-specific v2 marker for Hebrew printed only (PR C): that route moved
# from the JSON response contract to plain text. Together with the changed
# effective prompt fingerprint, this prevents reuse of Hebrew printed
# checkpoints created under the v1 JSON-era contract. All other routes keep
# GEMINI_OCR_PROMPT_CONTRACT_VERSION and their existing checkpoint identities.
GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION = "gemini-hebrew-printed-prompt-v2"

# Route-specific marker for the MIXED printed/handwritten route (PR E). The
# mixed route has its own approved prompt contract and plain-text output; the
# explicit version prevents checkpoint reuse across incompatible prompt
# contracts. All other routes keep their existing contract versions and
# checkpoint identities.
GEMINI_MIXED_PROMPT_CONTRACT_VERSION = "gemini-mixed-content-prompt-v1"

# Bounded per-page OCR recovery. Version v2 adds RECITATION-only model
# fallback for Latin handwriting while preserving one global three-call budget.
GEMINI_OCR_PAGE_RETRY_POLICY_VERSION = "gemini-ocr-page-retry-v2"
GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS = 3
_GEMINI_OCR_EMPTY_RESPONSE_BACKOFF_SECONDS = (1.0, 2.0)
_GEMINI_OCR_MIN_ESCALATED_OUTPUT_TOKENS = 8192


class GeminiError(RuntimeError):
    pass


class GeminiResponseFailureCode(str, Enum):
    EMPTY_RESPONSE = "EMPTY_RESPONSE"
    MAX_TOKENS = "MAX_TOKENS"
    SAFETY = "SAFETY"
    RECITATION = "RECITATION"
    LANGUAGE = "LANGUAGE"
    SPII = "SPII"
    BLOCKED = "BLOCKED"
    NO_CANDIDATES = "NO_CANDIDATES"
    JSON_PARSE = "JSON_PARSE"
    JSON_SCHEMA = "JSON_SCHEMA"
    OTHER = "OTHER"
    API_ERROR = "API_ERROR"


@dataclass(frozen=True)
class GeminiResponseMetadata:
    model: str
    page_index: int
    attempt: int
    max_output_tokens: Optional[int]
    candidate_count: int
    finish_reason: Optional[str]
    block_reason: Optional[str]
    raw_output_length: int
    output_length: int
    trailing_whitespace_chars: int
    prompt_token_count: Optional[int]
    candidates_token_count: Optional[int]
    thoughts_token_count: Optional[int]
    total_token_count: Optional[int]

    def safe_details(self) -> str:
        return (
            f"page_index={self.page_index}, "
            f"attempt={self.attempt}, "
            f"finish_reason={self.finish_reason}, "
            f"block_reason={self.block_reason}, "
            f"candidate_count={self.candidate_count}, "
            f"raw_output_length={self.raw_output_length}, "
            f"output_length={self.output_length}, "
            f"trailing_whitespace_chars={self.trailing_whitespace_chars}, "
            f"prompt_token_count={self.prompt_token_count}, "
            f"candidates_token_count={self.candidates_token_count}, "
            f"thoughts_token_count={self.thoughts_token_count}, "
            f"total_token_count={self.total_token_count}, "
            f"max_output_tokens={self.max_output_tokens}, "
            f"model={self.model}"
        )


class GeminiResponseError(GeminiError):
    def __init__(
        self,
        failure_code: GeminiResponseFailureCode,
        metadata: GeminiResponseMetadata,
    ) -> None:
        self.failure_code = failure_code
        self.metadata = metadata
        super().__init__(f"{failure_code.value}: {metadata.safe_details()}")


class GeminiApiError(GeminiError):
    failure_code = GeminiResponseFailureCode.API_ERROR

    def __init__(self, exception_class: str) -> None:
        self.exception_class = exception_class
        super().__init__(
            f"{self.failure_code.value}: exception_class={exception_class}"
        )


class GeminiQuotaError(GeminiError):
    def __init__(
        self,
        *,
        model_name: str,
        provider_calls_used: int,
        after_retries: bool = False,
    ) -> None:
        self.provider_calls_used = provider_calls_used
        suffix = " after retries" if after_retries else ""
        super().__init__(f"QUOTA_EXHAUSTED: {model_name}{suffix}")


@dataclass(frozen=True)
class GeminiResult:
    text: str
    needs_review: bool = False
    engine_name: str = DEFAULT_GEMINI_MODEL
    review_reasons: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeminiTranscriptionContract:
    prompt_fingerprint: str
    prompt_contract_version: str
    output_mode: str
    api_version: str
    effective_temperature: float


@dataclass(frozen=True)
class _HebrewTranslationChunkAttemptResult:
    text: str
    data: Dict[str, Any]
    finish_reason: Optional[str]
    finish_message_present: bool
    usage: Dict[str, Optional[int]]


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


_HEBREW_GENERAL_HANDWRITTEN_PROMPT = (
    "You are an expert transcriber of handwritten Hebrew historical and archival documents. "
    "Your absolute priority is extreme visual fidelity and verbatim accuracy. "
    "Do not correct, complete, infer, or assume text from context.\n"
    "\n"
    "TASK:\n"
    "Transcribe all visible handwritten text in the image as faithfully and completely as possible.\n"
    "\n"
    "CRITICAL ACCURACY DIRECTIVE:\n"
    "- Faithfulness to the visible writing is strictly more important than readability, fluency, "
    "grammatical completeness, or producing a coherent text. Visual evidence always takes priority.\n"
    "- Base every reading on the visible letterforms, strokes, and spatial relationships in the image.\n"
    "- Never guess, extrapolate, or complete a word based on context, grammar, meaning, familiarity, "
    "or what the writer probably intended.\n"
    "- Transcribe exactly what is visibly written. Use the visible letterforms and strokes as evidence; "
    "do not complete or correct the text from linguistic context.\n"
    "- If the visible evidence does not support a responsible reading, mark the text as uncertain or unclear.\n"
    "- It is a serious error to output a word as certain when the visible writing does not support that "
    "reading, even if the intended word seems obvious from context.\n"
    "- Do not replace an unclear name, place, date, institution, abbreviation, or unusual word with a "
    "more common or plausible alternative.\n"
    "\n"
    "TRANSCRIPTION RULES:\n"
    "1. The primary text is handwritten Hebrew. Preserve text in other languages or scripts exactly "
    "where it appears.\n"
    "2. Transcribe the entire visible page from beginning to end. Do not stop after the first paragraph, "
    "section, column, or clearly legible area.\n"
    "3. Follow the document’s visible reading order.\n"
    "4. Transcribe exactly what is visibly written, not what the writer probably intended or what would "
    "make sense in context.\n"
    "5. Preserve the original wording, spelling, typos, grammar, punctuation, capitalization, "
    "abbreviations, numbers, dates, names, places, and unusual forms exactly as seen.\n"
    "6. Preserve paragraph boundaries and line breaks as closely as practical.\n"
    "7. Preserve headings, lists, page numbers, signatures, addresses, marginal notes, insertions, "
    "crossings-out, interlinear additions, and visible corrections when they contain text.\n"
    "8. Do not correct spelling, grammar, punctuation, wording, factual errors, or non-standard Hebrew.\n"
    "9. Do not modernize, normalize, rewrite, summarize, translate, explain, or improve the text.\n"
    "10. Do not add Hebrew vowel marks or diacritics unless they are clearly visible in the source.\n"
    "11. Do not silently omit short words, repeated words, words at line endings, isolated letters, or "
    "text that appears faint, crowded, crossed out, or in the margins.\n"
    "12. Do not invent or restore text that is cropped, damaged, obscured, erased, covered, or outside "
    "the image.\n"
    "\n"
    "UNCERTAINTY AND UNREADABLE TEXT:\n"
    "- Use the exact marker [?] whenever a reading is uncertain.\n"
    "- If one character is unclear but the surrounding characters are responsibly readable, replace "
    "only the unclear character with [?]. Example: ב[?]ית.\n"
    "- If a word or short phrase has a plausible visual reading but remains uncertain, write the reading "
    "and add [?] immediately after it. Example: ירושלים[?].\n"
    "- Give an uncertain reading only when it is supported by visible letterforms. Do not provide a best "
    "guess based mainly on context.\n"
    "- If no responsible reading is possible, use [UNCLEAR].\n"
    "- If several consecutive words are completely unreadable, use a single [UNCLEAR] for the unreadable "
    "span rather than inventing its length or content.\n"
    "- Never present an uncertain reading as certain.\n"
    "- For uncertainty annotations that you add, use only the exact strings [?] and [UNCLEAR] as "
    "demonstrated in the examples.\n"
    "- Preserve ordinary question marks and other punctuation when they are visibly present in the "
    "source text.\n"
    "\n"
    "OUTPUT:\n"
    "- Output only the transcription text.\n"
    "- Do not output JSON, markdown, code fences, comments, explanations, labels, confidence scores, or "
    "introductory text.\n"
    "- Do not describe illustrations, stains, non-textual marks, handwriting style, page condition, or "
    "layout.\n"
    "- Transcribe readable text that appears inside stamps, seals, forms, labels, or other visual elements.\n"
)


_PRINTED_LATIN_PROMPT = (
    "You are an OCR assistant for printed historical archival documents.\n"
    "TASK: Transcribe the printed text in the image as faithfully as possible.\n"
    "RULES:\n"
    "- The text is in Latin script, typically English or French, and may include a mix of both.\n"
    "- Transcribe the entire visible page from top to bottom; do not stop after the first section or paragraph.\n"
    "- Preserve the original reading order, line breaks, headings, paragraphs, footnotes, page numbers, punctuation, spelling, capitalization, dates, names, places, abbreviations, and unusual forms exactly as seen.\n"
    "- Preserve visible hyphenation, quotations, marginal notes, footer lines, URLs, email headers, and visible corrections.\n"
    "- Do not correct spelling, grammar, punctuation, capitalization, wording, or factual mistakes.\n"
    "- Do not modernize, normalize, rewrite, summarize, translate, explain, or add context.\n"
    "- If a word is partly legible or uncertain, give your best reading and mark it inline with [?].\n"
    "- If text is completely unreadable, use [UNCLEAR].\n"
    "- Ignore purely decorative UI icons, toolbar buttons, and browser controls unless they contain meaningful printed text.\n"
    "- Stop immediately after the last visible meaningful text on the page.\n"
    "- Do not repeat text, continue with blank lines, or generate padding after the page content.\n"
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

# Plain-text contract for canonical Hebrew (`he`) printed OCR. Keeps the
# archival guardrails of the JSON-era Hebrew printed prompt but requires
# transcription text only; uncertainty uses the established [?] / [UNCLEAR]
# markers so review metadata can be derived from the text itself.
_HEBREW_PRINTED_PROMPT = (
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
    "- Do not replace an unclear name or personal detail with a more common word. If a word is visible but uncertain, write your best reading and add the exact marker [?] immediately after it. Example: ירושלים[?].\n"
    "- If text is completely unreadable, output the exact token [UNCLEAR].\n"
    "- For mixed Hebrew/English documents, preserve Hebrew and English document text in the order it appears. Do not reorder or clean up email/listserv formatting.\n"
    "- Ignore purely decorative UI icons, toolbar buttons, browser controls, and repeated icon placeholders unless they contain meaningful printed text.\n"
    "- Stop immediately after the last visible meaningful text on the page.\n"
    "- Do not repeat text, continue with blank lines, or generate padding after the page content.\n"
    "- Output only the transcription text.\n"
    "- Do not output JSON, markdown, code fences, comments, explanations, labels, or introductory text.\n"
)

# Remaining JSON response contract for printed pages whose language hint is
# neither Latin nor canonical Hebrew (currently Arabic printed on the Gemini
# route). Hebrew printed no longer uses this prompt.
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

# Approved mixed printed/handwritten prompt contract (PR E). This is a closed
# product contract: preserve the wording, punctuation, examples, ordering,
# markers, and backticks exactly. Do not rewrite, shorten, normalize, or merge
# it with another prompt. One mixed contract applies to every page of a MIXED
# document; a page may be entirely printed, entirely handwritten, mixed within
# the same page, or a printed form filled in by hand.
_MIXED_CONTENT_PROMPT = (
    "You are an expert transcriber of mixed printed and handwritten historical and archival documents.\n"
    "\n"
    "Your absolute priority is extreme visual fidelity, completeness, and verbatim accuracy. Do not correct, complete, infer, or assume text from context.\n"
    "\n"
    "TASK:\n"
    "Transcribe all meaningful visible text in the image as faithfully and completely as possible, including both printed and handwritten text.\n"
    "\n"
    "The page may be entirely printed, entirely handwritten, or contain any mixture of printed and handwritten text. Never omit visible text merely because it belongs to a different text type, writing style, language, script, page region, or visual element.\n"
    "\n"
    "CRITICAL ACCURACY DIRECTIVE:\n"
    "- Faithfulness to the visible source is strictly more important than readability, fluency, grammatical completeness, or producing a coherent document.\n"
    "- Base every reading on the visible letters, strokes, shapes, and spatial relationships in the image.\n"
    "- Apply the same strict accuracy standards to printed text (including faint, broken, or carbon-copy print) as to handwriting.\n"
    "- Never guess, extrapolate, restore, or complete text based on context, grammar, meaning, familiarity, document conventions, or what the writer or printer probably intended.\n"
    "- Transcribe exactly what is visibly present, even when the text contains errors, contradictions, repetitions, incomplete sentences, unusual forms, or inconsistent spelling.\n"
    "- If the visible evidence does not support a responsible reading, mark the text as uncertain or unclear.\n"
    "- It is a serious error to present a doubtful reading as certain, even when the intended word appears obvious from context.\n"
    "- Do not replace an unclear name, address, place, date, institution, abbreviation, number, or unusual word with a more common or plausible alternative.\n"
    "\n"
    "TEXT COVERAGE:\n"
    "1. Transcribe all meaningful printed and handwritten text visible on the page.\n"
    "2. The primary language may be Hebrew. Preserve text in every other visible language or script exactly where it appears.\n"
    "3. Transcribe the entire visible page. Do not stop after the first paragraph, section, column, printed block, handwritten passage, or clearly legible area.\n"
    "4. Preserve headings, paragraphs, lists, columns, page numbers, signatures, addresses, names, dates, numbers, stamps, seals, form fields, labels, captions, footnotes, headers, footer lines, URLs, email or listserv headers, and readable navigation text.\n"
    "5. In pre-printed forms filled in by hand, transcribe the printed field label followed naturally by the handwritten entry on the same line whenever visually aligned.\n"
    "6. Preserve handwritten additions, marginal notes, interlinear additions, insertions, crossings-out, annotations, and visible corrections whenever they contain readable text.\n"
    "7. Do not silently omit short words, repeated words, isolated letters, words at line endings, or text that is faint, crowded, crossed out, partially covered, or located in the margins.\n"
    "8. Transcribe readable text inside stamps, seals, forms, labels, tables, or other visual elements.\n"
    "9. Ignore purely decorative marks, illustrations, UI icons, toolbar buttons, browser controls, and repeated icon placeholders unless they contain meaningful readable text.\n"
    "\n"
    "ORDER AND STRUCTURE:\n"
    "- Follow the document’s visible reading order. Maintain the main body flow and do not interrupt a printed sentence mid-line to insert a marginal note.\n"
    "- Preserve paragraph boundaries, line breaks, columns, and separation between distinct text blocks as closely as practical.\n"
    "- Keep printed and handwritten text in their visible positions relative to the surrounding content.\n"
    "- When a handwritten note or interlinear addition is visibly attached to a specific passage, place it as close as practical to that passage (e.g., directly below or beside the relevant line/paragraph).\n"
    "- When the relationship between separate text regions is visually ambiguous, preserve their approximate spatial reading order without inventing a relationship.\n"
    "- Do not move all handwritten material to the end or group text by whether it is printed or handwritten.\n"
    "- Do not add labels such as “printed text”, “handwritten note”, “margin”, or “signature” unless those words are themselves visible in the source.\n"
    "\n"
    "VERBATIM TRANSCRIPTION:\n"
    "- Preserve the original wording, spelling, typographical errors, non-standard spelling, unusual Hebrew forms, grammar, punctuation, capitalization, abbreviations, numbers, dates, names, places, and repetitions exactly as seen.\n"
    "- Preserve visible hyphenation, dashes, apostrophes, quotation marks, parentheses, slashes, and other meaningful punctuation.\n"
    "- Do not correct spelling, grammar, punctuation, wording, capitalization, or factual errors.\n"
    "- Do not modernize, normalize, smooth into clean prose, rewrite, summarize, translate, explain, or add context.\n"
    "- Do not add Hebrew vowel marks or diacritics unless they are clearly visible in the source.\n"
    "- Do not invent or restore text that is cropped, damaged, obscured, erased, covered, or outside the image.\n"
    "\n"
    "UNCERTAINTY AND UNREADABLE TEXT:\n"
    "- Use the exact marker [?] whenever a reading is uncertain.\n"
    "- If one character is unclear but the surrounding characters are responsibly readable, replace only the unclear character with [?]. Example: ב[?]ית.\n"
    "- If a word or short phrase has a visually supported but uncertain reading, write that reading and add [?] immediately after it. Example: ירושלים[?].\n"
    "- Use an uncertain reading only when it is supported by visible evidence. Do not provide a best guess based mainly on context.\n"
    "- If no responsible reading is possible, use the exact token [UNCLEAR].\n"
    "- If several consecutive words are completely unreadable, use a single [UNCLEAR] for that unreadable span rather than inventing its length or content.\n"
    "- Preserve ordinary question marks and other punctuation that are visibly present in the source.\n"
    "- For uncertainty annotations that you add, use only the exact strings [?] and [UNCLEAR].\n"
    "\n"
    "OUTPUT:\n"
    "- Output raw plain text only.\n"
    "- Absolute strict rule: Do NOT use markdown code blocks (no ``` or ```text), JSON, comments, explanations, confidence scores, classifications, labels, or introductory/concluding text.\n"
    "- Do not describe the handwriting style, printing method, illustrations, stains, page condition, or layout.\n"
    "- Stop immediately after the last visible meaningful text on the page.\n"
    "- Do not repeat text, continue with blank lines, or generate padding after the page content.\n"
)

_PROMPT_BY_VARIANT = {
    DocumentTextResult.OcrPromptVariant.HANDWRITTEN: _HANDWRITTEN_LATIN_PROMPT,
    DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN: (
        _HEBREW_GENERAL_HANDWRITTEN_PROMPT
    ),
    DocumentTextResult.OcrPromptVariant.PRINTED: _PRINTED_TEXT_PROMPT,
    DocumentTextResult.OcrPromptVariant.MIXED: _MIXED_CONTENT_PROMPT,
}

_REQUIRED_KEYS = ("text", "has_unclear", "unclear_count")
_TRANSLATION_CHUNK_MAX_CHARS = 2200
_TRANSLATION_MAX_TOKENS_SPLIT_MAX_CHARS = 1100
_MIN_TRANSLATION_LENGTH_RATIO = 0.20
_TRUNCATION_RETRY_MIN_OUTPUT_TOKENS = 8192

_LATIN_LANGUAGE_HINTS = frozenset(
    {"en", "eng", "english", "fr", "fra", "fre", "french"}
)

# Only the canonical routing value counts as Hebrew for the printed plain-text
# contract. Other non-Latin hints and missing hints keep the JSON contract.
_HEBREW_CANONICAL_LANGUAGE_HINT = "he"


# ------------------------------------------------------------------ helpers


def _is_latin_language_hint(language_hint: Optional[str]) -> bool:
    if not language_hint:
        return False
    return language_hint.strip().lower() in _LATIN_LANGUAGE_HINTS


def _is_hebrew_language_hint(language_hint: Optional[str]) -> bool:
    if not language_hint:
        return False
    return language_hint.strip().lower() == _HEBREW_CANONICAL_LANGUAGE_HINT


def _is_hebrew_printed_plain_text_contract(
    prompt_variant: str,
    language_hint: Optional[str],
) -> bool:
    """Single predicate for the PR C Hebrew printed plain-text contract.

    Prompt selection, output-mode selection, and prompt-contract-version
    selection all use this predicate so they cannot drift apart.
    """
    return prompt_variant == DocumentTextResult.OcrPromptVariant.PRINTED and (
        _is_hebrew_language_hint(language_hint)
    )


def _is_mixed_content_contract(prompt_variant: str) -> bool:
    """Single predicate for the PR E mixed printed/handwritten contract.

    Prompt selection, output-mode selection, and prompt-contract-version
    selection all use this predicate so they cannot drift apart. The mixed
    contract is document-level and language-independent: every page of a
    MIXED document uses the same approved prompt and raw plain-text output.
    """
    return prompt_variant == DocumentTextResult.OcrPromptVariant.MIXED


def _uses_plain_text_transcription(
    prompt_variant: str,
    language_hint: Optional[str],
) -> bool:
    if prompt_variant == DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN:
        return True

    if _is_mixed_content_contract(prompt_variant):
        return True

    if _is_hebrew_printed_plain_text_contract(prompt_variant, language_hint):
        return True

    return prompt_variant in (
        DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        DocumentTextResult.OcrPromptVariant.PRINTED,
    ) and _is_latin_language_hint(language_hint)


def _prompt_base_for_variant(prompt_variant: str) -> str | None:
    try:
        variant_key = DocumentTextResult.OcrPromptVariant(prompt_variant)
    except ValueError:
        return None
    return _PROMPT_BY_VARIANT.get(variant_key)


def _effective_transcription_prompt(
    prompt_variant: str,
    language_hint: Optional[str],
) -> tuple[str, bool]:
    uses_plain_text_transcription = _uses_plain_text_transcription(
        prompt_variant,
        language_hint,
    )
    if (
        prompt_variant == DocumentTextResult.OcrPromptVariant.PRINTED
        and _is_latin_language_hint(language_hint)
    ):
        prompt_base: str | None = _PRINTED_LATIN_PROMPT
    elif _is_hebrew_printed_plain_text_contract(prompt_variant, language_hint):
        prompt_base = _HEBREW_PRINTED_PROMPT
    else:
        prompt_base = _prompt_base_for_variant(prompt_variant)
    if prompt_base is None:
        raise GeminiError(f"Unsupported Gemini prompt_variant: {prompt_variant!r}")

    prompt = prompt_base
    if language_hint:
        prompt += f"\nLanguage hint: {language_hint}."
    return prompt, uses_plain_text_transcription


def gemini_transcription_contract(
    *,
    prompt_variant: str,
    language_hint: Optional[str],
    temperature: float,
) -> GeminiTranscriptionContract:
    """Resolve safe identity metadata from the exact effective OCR prompt."""

    prompt, uses_plain_text_transcription = _effective_transcription_prompt(
        prompt_variant,
        language_hint,
    )
    if _is_mixed_content_contract(prompt_variant):
        prompt_contract_version = GEMINI_MIXED_PROMPT_CONTRACT_VERSION
    elif _is_hebrew_printed_plain_text_contract(prompt_variant, language_hint):
        prompt_contract_version = GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION
    else:
        prompt_contract_version = GEMINI_OCR_PROMPT_CONTRACT_VERSION

    return GeminiTranscriptionContract(
        prompt_fingerprint=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        prompt_contract_version=prompt_contract_version,
        output_mode="plain_text" if uses_plain_text_transcription else "json",
        api_version="v1beta" if uses_plain_text_transcription else "v1",
        effective_temperature=0.0 if uses_plain_text_transcription else temperature,
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
    if isinstance(exc, GeminiResponseError):
        return exc.failure_code in {
            GeminiResponseFailureCode.EMPTY_RESPONSE,
            GeminiResponseFailureCode.JSON_PARSE,
            GeminiResponseFailureCode.MAX_TOKENS,
        }

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


def _split_translation_chunk_for_max_tokens_retry(chunk: str) -> List[str]:
    stripped = (chunk or "").strip()
    if not stripped:
        return []

    max_chars = min(
        _TRANSLATION_MAX_TOKENS_SPLIT_MAX_CHARS,
        max(1, (len(stripped) + 1) // 2),
    )
    return _split_text_for_translation(stripped, max_chars=max_chars)


def _first_response_candidate(resp: Any) -> Any:
    candidates = getattr(resp, "candidates", None)
    if not candidates:
        return None
    return candidates[0]


def _normalize_provider_enum(value: Any) -> Optional[str]:
    if value is None:
        return None

    raw_value = getattr(value, "value", value)
    normalized = str(raw_value).strip()
    if not normalized:
        return None
    return normalized.rsplit(".", 1)[-1].upper()


def _extract_finish_reason(resp: Any) -> Optional[str]:
    candidate = _first_response_candidate(resp)
    finish_reason = getattr(candidate, "finish_reason", None)
    return _normalize_provider_enum(finish_reason)


def _is_max_tokens_finish_reason(finish_reason: Optional[str]) -> bool:
    if not finish_reason:
        return False
    return "MAX_TOKENS" in finish_reason.upper()


def _next_max_output_tokens_cap(
    current: Optional[int],
    *,
    hard_cap: int,
) -> Optional[int]:
    """Deterministic token-cap ladder for OCR MAX_TOKENS recovery."""
    if current is None or current < _GEMINI_OCR_MIN_ESCALATED_OUTPUT_TOKENS:
        next_cap = _GEMINI_OCR_MIN_ESCALATED_OUTPUT_TOKENS
    else:
        next_cap = current * 2
    next_cap = min(next_cap, hard_cap)
    if current is not None and next_cap <= current:
        return None
    return next_cap


def _transcription_empty_response_backoff_seconds(
    completed_attempt: int,
) -> Optional[float]:
    if completed_attempt == 1:
        return _GEMINI_OCR_EMPTY_RESPONSE_BACKOFF_SECONDS[0]
    if completed_attempt == 2:
        return _GEMINI_OCR_EMPTY_RESPONSE_BACKOFF_SECONDS[1]
    return None


_PERMANENT_TRANSCRIPTION_FAILURE_CODES = frozenset(
    {
        GeminiResponseFailureCode.SAFETY,
        GeminiResponseFailureCode.RECITATION,
        GeminiResponseFailureCode.LANGUAGE,
        GeminiResponseFailureCode.SPII,
        GeminiResponseFailureCode.BLOCKED,
        GeminiResponseFailureCode.JSON_SCHEMA,
        GeminiResponseFailureCode.NO_CANDIDATES,
        GeminiResponseFailureCode.OTHER,
    }
)


def _escalated_max_output_tokens(current: Optional[int]) -> int:
    return max(_TRUNCATION_RETRY_MIN_OUTPUT_TOKENS, current or 0)


def _extract_response_usage(resp: Any) -> Dict[str, Optional[int]]:
    usage = getattr(resp, "usage_metadata", None)
    return {
        "prompt_token_count": getattr(usage, "prompt_token_count", None),
        "candidates_token_count": getattr(
            usage,
            "candidates_token_count",
            None,
        ),
        "thoughts_token_count": getattr(usage, "thoughts_token_count", None),
        "total_token_count": getattr(usage, "total_token_count", None),
    }


def _extract_block_reason(resp: Any) -> Optional[str]:
    prompt_feedback = getattr(resp, "prompt_feedback", None)
    block_reason = _normalize_provider_enum(
        getattr(prompt_feedback, "block_reason", None)
    )
    if block_reason in {
        "UNSPECIFIED",
        "BLOCK_REASON_UNSPECIFIED",
        "BLOCKED_REASON_UNSPECIFIED",
    }:
        return None
    return block_reason


def _has_finish_message(resp: Any) -> bool:
    candidate = _first_response_candidate(resp)
    return bool(getattr(candidate, "finish_message", None))


def _extract_response_text(resp: Any) -> str:
    candidates = getattr(resp, "candidates", None)
    candidate = _first_response_candidate(resp)
    if candidate is None and candidates is not None:
        return ""

    if candidate is not None:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None)
        if parts is not None:
            text_parts = [
                str(text)
                for part in parts
                if (text := getattr(part, "text", None)) is not None
            ]
            if text_parts:
                return "".join(text_parts)

    try:
        return getattr(resp, "text", None) or ""
    except (AttributeError, TypeError, ValueError):
        return ""


def _response_metadata(
    resp: Any,
    *,
    model_name: str,
    page_index: int,
    attempt: int,
    max_output_tokens: Optional[int],
) -> tuple[GeminiResponseMetadata, str]:
    candidates = getattr(resp, "candidates", None)
    raw_output_text = _extract_response_text(resp)
    output_text = raw_output_text.strip()
    usage = _extract_response_usage(resp)
    metadata = GeminiResponseMetadata(
        model=model_name,
        page_index=page_index,
        attempt=attempt,
        max_output_tokens=max_output_tokens,
        candidate_count=(
            len(candidates) if candidates is not None else int(bool(raw_output_text))
        ),
        finish_reason=_extract_finish_reason(resp),
        block_reason=_extract_block_reason(resp),
        raw_output_length=len(raw_output_text),
        output_length=len(output_text),
        trailing_whitespace_chars=(
            len(raw_output_text) - len(raw_output_text.rstrip())
        ),
        prompt_token_count=usage["prompt_token_count"],
        candidates_token_count=usage["candidates_token_count"],
        thoughts_token_count=usage["thoughts_token_count"],
        total_token_count=usage["total_token_count"],
    )
    return metadata, output_text


_PERMANENT_FINISH_REASON_CODES = {
    "SAFETY": GeminiResponseFailureCode.SAFETY,
    "RECITATION": GeminiResponseFailureCode.RECITATION,
    "LANGUAGE": GeminiResponseFailureCode.LANGUAGE,
    "SPII": GeminiResponseFailureCode.SPII,
    "BLOCKLIST": GeminiResponseFailureCode.BLOCKED,
    "PROHIBITED_CONTENT": GeminiResponseFailureCode.BLOCKED,
    "IMAGE_SAFETY": GeminiResponseFailureCode.SAFETY,
}


def _classify_response_failure(
    metadata: GeminiResponseMetadata,
) -> Optional[GeminiResponseFailureCode]:
    if metadata.block_reason:
        if metadata.block_reason == "SAFETY":
            return GeminiResponseFailureCode.SAFETY
        return GeminiResponseFailureCode.BLOCKED

    if metadata.finish_reason == "MAX_TOKENS":
        return GeminiResponseFailureCode.MAX_TOKENS

    if metadata.finish_reason in _PERMANENT_FINISH_REASON_CODES:
        return _PERMANENT_FINISH_REASON_CODES[metadata.finish_reason]

    if metadata.finish_reason not in (None, "STOP"):
        return GeminiResponseFailureCode.OTHER

    if metadata.candidate_count == 0:
        return GeminiResponseFailureCode.NO_CANDIDATES

    if metadata.output_length == 0:
        return GeminiResponseFailureCode.EMPTY_RESPONSE

    return None


def _raise_for_response_failure(metadata: GeminiResponseMetadata) -> None:
    failure_code = _classify_response_failure(metadata)
    if failure_code is not None:
        raise GeminiResponseError(failure_code, metadata)


def _translation_min_expected_length(
    source_length: int,
    *,
    min_text_length: int,
) -> Optional[int]:
    """
    Minimum expected Hebrew length for ratio-based truncation checks.

    Returns None when the source is too short for a reliable ratio heuristic
    (sources under 1000 chars rely on MAX_TOKENS and min_text_length instead).
    """
    if source_length < 1000:
        return None

    return max(min_text_length, int(source_length * _MIN_TRANSLATION_LENGTH_RATIO))


def _is_translation_chunk_truncated(
    source_chunk: str,
    translated_text: str,
    *,
    min_text_length: int,
) -> bool:
    if len(translated_text) < min_text_length:
        return True

    min_expected_length = _translation_min_expected_length(
        len(source_chunk),
        min_text_length=min_text_length,
    )
    if min_expected_length is None:
        return False

    return len(translated_text) < min_expected_length


def _is_combined_translation_truncated(
    source_length: int,
    combined_translation_length: int,
    *,
    min_text_length: int,
) -> bool:
    min_expected_length = _translation_min_expected_length(
        source_length,
        min_text_length=min_text_length,
    )
    if min_expected_length is None:
        return False

    return combined_translation_length < min_expected_length


def _build_hebrew_translation_prompt(
    chunk: str,
    language_hint: Optional[str],
) -> str:
    wrapped_chunk = f"<source_excerpt>\n{chunk}\n</source_excerpt>"
    prompt = _HEBREW_TRANSLATION_PROMPT.replace("{{source_text}}", wrapped_chunk)
    if language_hint:
        prompt += f"\nSource language hint: {language_hint}."
    return prompt


_SOURCE_EXCERPT_OPEN_TAG = "<source_excerpt>"
_SOURCE_EXCERPT_CLOSE_TAG = "</source_excerpt>"


def _strip_outer_source_excerpt_wrapper(text: str) -> str:
    """
    Remove model-echoed outer <source_excerpt> wrapper tags only.

    Inner occurrences of the tag strings are preserved.
    """
    stripped = (text or "").strip()
    if not stripped.startswith(_SOURCE_EXCERPT_OPEN_TAG):
        return stripped
    if not stripped.endswith(_SOURCE_EXCERPT_CLOSE_TAG):
        return stripped

    inner = stripped[len(_SOURCE_EXCERPT_OPEN_TAG) : -len(_SOURCE_EXCERPT_CLOSE_TAG)]
    return inner.strip()


def _generate_hebrew_translation_chunk(
    client: Any,
    chunk: str,
    language_hint: Optional[str],
    *,
    chunk_index: int,
    total_chunks: int,
    model_name: str,
    temperature: float,
    top_k: int,
    top_p: float,
    max_output_tokens: Optional[int],
) -> _HebrewTranslationChunkAttemptResult:
    prompt = _build_hebrew_translation_prompt(chunk, language_hint)

    success = False
    attempts = 0
    finish_reason: Optional[str] = None
    finish_message_present = False
    usage: Dict[str, Optional[int]] = {}
    data: Dict[str, Any] = {}

    while not success and attempts < 2:
        try:
            contents = [types.Part.from_text(text=prompt)]
            resp = client.models.generate_content(
                model=model_name,
                contents=cast(types.ContentListUnionDict, contents),
                config=types.GenerateContentConfig(
                    temperature=temperature,
                    top_k=top_k,
                    top_p=top_p,
                    max_output_tokens=max_output_tokens,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                ),
            )
            finish_reason = _extract_finish_reason(resp)
            finish_message_present = _has_finish_message(resp)
            usage = _extract_response_usage(resp)
            data = _plain_text_response_to_page_data(
                resp.text or "",
                page_index=chunk_index,
            )
            success = True
        except GeminiError as e:
            if _is_retryable_gemini_response_error(e):
                attempts += 1
                if attempts < 2:
                    logger.warning(
                        "Retrying Gemini translation response format failure on chunk %s/%s "
                        "with model %s: %s",
                        chunk_index,
                        total_chunks,
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

            raise GeminiApiError(e.__class__.__name__) from None

    if not success:
        raise GeminiError(f"QUOTA_EXHAUSTED: {model_name} after retries")

    return _HebrewTranslationChunkAttemptResult(
        text=_strip_outer_source_excerpt_wrapper(data["text"]),
        data=data,
        finish_reason=finish_reason,
        finish_message_present=finish_message_present,
        usage=usage,
    )


def _parse_page_json_strict(
    raw: str,
    *,
    page_index: int,
    response_metadata: Optional[GeminiResponseMetadata] = None,
) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        if response_metadata is not None:
            raise GeminiResponseError(
                GeminiResponseFailureCode.EMPTY_RESPONSE,
                response_metadata,
            )
        raise GeminiError(f"Gemini returned empty response on page {page_index}")

    if "```" in raw:
        match = re.search(r"```(?:json)?\s*(.*?)\s*(?:```|$)", raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()
        else:
            raw = raw.replace("```json", "").replace("```", "").strip()

    try:
        data = json.loads(raw, strict=False)
    except json.JSONDecodeError as exc:
        if response_metadata is None:
            raise GeminiError(f"JSON Parse Error on page {page_index}") from exc
        error = GeminiResponseError(
            GeminiResponseFailureCode.JSON_PARSE,
            response_metadata,
        )
        logger.error("Gemini response JSON parse failed: %s", error)
        raise error from exc

    valid_schema = (
        isinstance(data, dict)
        and all(key in data for key in _REQUIRED_KEYS)
        and isinstance(data.get("text"), str)
        and bool(data["text"].strip())
        and type(data.get("has_unclear")) is bool
        and type(data.get("unclear_count")) is int
        and data["unclear_count"] >= 0
    )
    if not valid_schema:
        if response_metadata is None:
            raise GeminiError(f"JSON Schema Error on page {page_index}")
        error = GeminiResponseError(
            GeminiResponseFailureCode.JSON_SCHEMA,
            response_metadata,
        )
        logger.error("Gemini response JSON schema validation failed: %s", error)
        raise error

    return data


def _transcription_generation_config_kwargs(
    *,
    model_name: str,
    contract: GeminiTranscriptionContract,
    uses_plain_text_transcription: bool,
    top_k: int,
    top_p: float,
    max_output_tokens: Optional[int],
) -> Dict[str, Any]:
    if model_name == FRENCH_HANDWRITTEN_GEMINI_MODEL:
        # Gemini 3.6 Flash performed correctly in the live French handwritten
        # probes only with minimal thinking and model-default decoding. In
        # particular, do not inject the legacy thinking_budget=0 or explicit
        # temperature/top-k/top-p settings into this model.
        return {
            "max_output_tokens": max_output_tokens,
            "thinking_config": types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL,
            ),
        }

    config_kwargs: Dict[str, Any] = {
        "temperature": contract.effective_temperature,
        "top_k": top_k,
        "top_p": top_p,
        "max_output_tokens": max_output_tokens,
    }
    if uses_plain_text_transcription:
        config_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=0,
        )
    return config_kwargs


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
    max_output_tokens_hard_cap: int = DEFAULT_GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP,
    max_provider_calls: int = GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
    provider_call_offset: int = 0,
) -> GeminiResult:
    if type(max_provider_calls) is not int or max_provider_calls < 1:
        raise ValueError("max_provider_calls must be a positive integer")
    if type(provider_call_offset) is not int or provider_call_offset < 0:
        raise ValueError("provider_call_offset must be a non-negative integer")
    if provider_call_offset + max_provider_calls > GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS:
        raise ValueError("provider call window exceeds the per-page global call budget")

    contract = gemini_transcription_contract(
        prompt_variant=prompt_variant,
        language_hint=language_hint,
        temperature=temperature,
    )
    uses_plain_text_transcription = contract.output_mode == "plain_text"

    api_key = _get_api_key()
    client = _create_client(
        api_key,
        api_version=contract.api_version,
    )

    config_kwargs = _transcription_generation_config_kwargs(
        model_name=model_name,
        contract=contract,
        uses_plain_text_transcription=uses_plain_text_transcription,
        top_k=top_k,
        top_p=top_p,
        max_output_tokens=max_output_tokens,
    )

    texts: List[str] = []
    any_review = False
    engine_reasons: List[str] = []

    for page in pages:
        prompt, _ = _effective_transcription_prompt(
            prompt_variant,
            language_hint,
        )

        success = False
        attempt = 0
        attempt_max_output_tokens = max_output_tokens
        data: Dict[str, Any] = {}

        while not success and attempt < max_provider_calls:
            attempt += 1
            try:
                attempt_config_kwargs = dict(config_kwargs)
                attempt_config_kwargs["max_output_tokens"] = attempt_max_output_tokens

                contents = [
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(
                        data=page.image_bytes,
                        mime_type=page.mime_type or "image/png",
                    ),
                ]
                resp = client.models.generate_content(
                    model=model_name,
                    contents=cast(types.ContentListUnionDict, contents),
                    config=types.GenerateContentConfig(**attempt_config_kwargs),
                )

                response_metadata, output_text = _response_metadata(
                    resp,
                    model_name=model_name,
                    page_index=page.page_index,
                    attempt=provider_call_offset + attempt,
                    max_output_tokens=attempt_max_output_tokens,
                )
                trailing_whitespace_chars = response_metadata.trailing_whitespace_chars

                logger.info(
                    "Gemini transcription response received: "
                    "page=%s raw_output_length=%s output_length=%s "
                    "trailing_whitespace_chars=%s finish_reason=%s "
                    "block_reason=%s candidate_count=%s attempt=%s "
                    "prompt_token_count=%s "
                    "candidates_token_count=%s thoughts_token_count=%s "
                    "total_token_count=%s max_output_tokens=%s model=%s",
                    page.page_index,
                    response_metadata.raw_output_length,
                    response_metadata.output_length,
                    trailing_whitespace_chars,
                    response_metadata.finish_reason,
                    response_metadata.block_reason,
                    response_metadata.candidate_count,
                    response_metadata.attempt,
                    response_metadata.prompt_token_count,
                    response_metadata.candidates_token_count,
                    response_metadata.thoughts_token_count,
                    response_metadata.total_token_count,
                    attempt_max_output_tokens,
                    model_name,
                )

                failure_code = _classify_response_failure(response_metadata)
                if failure_code is not None:
                    if failure_code == GeminiResponseFailureCode.MAX_TOKENS:
                        if attempt < max_provider_calls:
                            retry_max_output_tokens = _next_max_output_tokens_cap(
                                attempt_max_output_tokens,
                                hard_cap=max_output_tokens_hard_cap,
                            )
                            if retry_max_output_tokens is not None:
                                logger.warning(
                                    "Retrying truncated Gemini transcription page %s "
                                    "after MAX_TOKENS with model %s: "
                                    "attempt=%s/%s max_output_tokens=%s -> %s "
                                    "raw_output_length=%s output_length=%s "
                                    "trailing_whitespace_chars=%s "
                                    "prompt_token_count=%s "
                                    "candidates_token_count=%s "
                                    "thoughts_token_count=%s total_token_count=%s",
                                    page.page_index,
                                    model_name,
                                    provider_call_offset + attempt,
                                    GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
                                    attempt_max_output_tokens,
                                    retry_max_output_tokens,
                                    response_metadata.raw_output_length,
                                    response_metadata.output_length,
                                    trailing_whitespace_chars,
                                    response_metadata.prompt_token_count,
                                    response_metadata.candidates_token_count,
                                    response_metadata.thoughts_token_count,
                                    response_metadata.total_token_count,
                                )
                                attempt_max_output_tokens = retry_max_output_tokens
                                continue
                        raise GeminiResponseError(
                            GeminiResponseFailureCode.MAX_TOKENS,
                            response_metadata,
                        )

                    if failure_code == GeminiResponseFailureCode.EMPTY_RESPONSE:
                        if attempt < max_provider_calls:
                            backoff_seconds = (
                                _transcription_empty_response_backoff_seconds(attempt)
                            )
                            if backoff_seconds is not None:
                                time.sleep(backoff_seconds)
                            logger.warning(
                                "Retrying empty Gemini transcription page %s "
                                "with model %s: attempt=%s/%s failure_code=%s",
                                page.page_index,
                                model_name,
                                provider_call_offset + attempt,
                                GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
                                failure_code.value,
                            )
                            continue
                        raise GeminiResponseError(
                            GeminiResponseFailureCode.EMPTY_RESPONSE,
                            response_metadata,
                        )

                    if failure_code in _PERMANENT_TRANSCRIPTION_FAILURE_CODES:
                        raise GeminiResponseError(failure_code, response_metadata)

                    raise GeminiResponseError(failure_code, response_metadata)

                if uses_plain_text_transcription:
                    data = _plain_text_response_to_page_data(
                        output_text,
                        page_index=page.page_index,
                    )
                else:
                    data = _parse_page_json_strict(
                        output_text,
                        page_index=page.page_index,
                        response_metadata=response_metadata,
                    )

                success = True
            except GeminiResponseError as exc:
                if (
                    exc.failure_code == GeminiResponseFailureCode.JSON_PARSE
                    and attempt < max_provider_calls
                ):
                    logger.warning(
                        "Retrying Gemini transcription JSON parse failure on page %s "
                        "with model %s: attempt=%s/%s failure_code=%s",
                        page.page_index,
                        model_name,
                        provider_call_offset + attempt,
                        GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
                        exc.failure_code.value,
                    )
                    continue
                raise
            except Exception as exc:
                err_str = str(exc).upper()
                if any(
                    token in err_str for token in ("429", "RESOURCE_EXHAUSTED", "QUOTA")
                ):
                    if "LIMIT: 0" in err_str:
                        raise GeminiQuotaError(
                            model_name=model_name,
                            provider_calls_used=attempt,
                        ) from None
                    if attempt < max_provider_calls:
                        time.sleep(5)
                        logger.warning(
                            "Retrying Gemini transcription quota/rate-limit on page %s "
                            "with model %s: attempt=%s/%s",
                            page.page_index,
                            model_name,
                            provider_call_offset + attempt,
                            GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
                        )
                        continue
                    raise GeminiQuotaError(
                        model_name=model_name,
                        provider_calls_used=attempt,
                        after_retries=True,
                    ) from None

                raise GeminiApiError(exc.__class__.__name__) from None

        if not success:
            raise GeminiQuotaError(
                model_name=model_name,
                provider_calls_used=attempt,
                after_retries=True,
            )

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
        chunk_max_output_tokens = max_output_tokens
        translated_text = ""
        data: Dict[str, Any] = {}

        for truncation_pass in range(2):
            is_truncation_retry = truncation_pass == 1
            attempt_result = _generate_hebrew_translation_chunk(
                client,
                chunk,
                language_hint,
                chunk_index=index,
                total_chunks=len(chunks),
                model_name=model_name,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                max_output_tokens=chunk_max_output_tokens,
            )
            translated_text = attempt_result.text
            data = attempt_result.data
            max_tokens_truncated = _is_max_tokens_finish_reason(
                attempt_result.finish_reason
            )
            ratio_truncated = _is_translation_chunk_truncated(
                chunk,
                translated_text,
                min_text_length=min_text_length,
            )

            logger.info(
                "Gemini Hebrew translation chunk %s/%s: source_length=%s output_length=%s "
                "finish_reason=%s finish_message_present=%s max_output_tokens=%s "
                "truncation_retry=%s prompt_token_count=%s candidates_token_count=%s "
                "thoughts_token_count=%s total_token_count=%s model=%s "
                "split_retry_used=%s",
                index,
                len(chunks),
                chunk_source_len,
                len(translated_text),
                attempt_result.finish_reason,
                attempt_result.finish_message_present,
                chunk_max_output_tokens,
                is_truncation_retry,
                attempt_result.usage.get("prompt_token_count"),
                attempt_result.usage.get("candidates_token_count"),
                attempt_result.usage.get("thoughts_token_count"),
                attempt_result.usage.get("total_token_count"),
                model_name,
                False,
            )

            if max_tokens_truncated or ratio_truncated:
                if truncation_pass == 0:
                    chunk_max_output_tokens = _escalated_max_output_tokens(
                        chunk_max_output_tokens
                    )
                    logger.warning(
                        "Retrying truncated Gemini Hebrew translation chunk %s/%s "
                        "with model %s: max_output_tokens=%s finish_reason=%s "
                        "finish_message_present=%s source_length=%s output_length=%s "
                        "truncation_retry=%s split_retry_used=%s",
                        index,
                        len(chunks),
                        model_name,
                        chunk_max_output_tokens,
                        attempt_result.finish_reason,
                        attempt_result.finish_message_present,
                        chunk_source_len,
                        len(translated_text),
                        True,
                        False,
                    )
                    continue

                if max_tokens_truncated:
                    split_chunks = _split_translation_chunk_for_max_tokens_retry(chunk)
                    logger.warning(
                        "Splitting truncated Gemini Hebrew translation chunk %s/%s "
                        "after MAX_TOKENS with model %s: source_length=%s "
                        "output_length=%s finish_reason=%s finish_message_present=%s "
                        "split_chunk_count=%s max_output_tokens=%s",
                        index,
                        len(chunks),
                        model_name,
                        chunk_source_len,
                        len(translated_text),
                        attempt_result.finish_reason,
                        attempt_result.finish_message_present,
                        len(split_chunks),
                        chunk_max_output_tokens,
                    )

                    split_translated_chunks: List[str] = []
                    split_has_unclear = False
                    for split_index, split_chunk in enumerate(split_chunks, start=1):
                        split_result = _generate_hebrew_translation_chunk(
                            client,
                            split_chunk,
                            language_hint,
                            chunk_index=index,
                            total_chunks=len(chunks),
                            model_name=model_name,
                            temperature=temperature,
                            top_k=top_k,
                            top_p=top_p,
                            max_output_tokens=chunk_max_output_tokens,
                        )
                        split_text = split_result.text
                        split_max_tokens_truncated = _is_max_tokens_finish_reason(
                            split_result.finish_reason
                        )
                        split_ratio_truncated = _is_translation_chunk_truncated(
                            split_chunk,
                            split_text,
                            min_text_length=min_text_length,
                        )

                        logger.info(
                            "Gemini Hebrew translation split retry chunk %s/%s.%s/%s: "
                            "source_length=%s output_length=%s finish_reason=%s "
                            "finish_message_present=%s max_output_tokens=%s "
                            "prompt_token_count=%s candidates_token_count=%s "
                            "thoughts_token_count=%s total_token_count=%s model=%s "
                            "split_retry_used=%s",
                            index,
                            len(chunks),
                            split_index,
                            len(split_chunks),
                            len(split_chunk),
                            len(split_text),
                            split_result.finish_reason,
                            split_result.finish_message_present,
                            chunk_max_output_tokens,
                            split_result.usage.get("prompt_token_count"),
                            split_result.usage.get("candidates_token_count"),
                            split_result.usage.get("thoughts_token_count"),
                            split_result.usage.get("total_token_count"),
                            model_name,
                            True,
                        )

                        if split_max_tokens_truncated or split_ratio_truncated:
                            raise GeminiError(
                                "Gemini Hebrew translation split retry chunk appears truncated: "
                                f"chunk_index={index}/{len(chunks)}, "
                                f"split_chunk_index={split_index}/{len(split_chunks)}, "
                                f"source_length={len(split_chunk)}, "
                                f"translation_length={len(split_text)}, "
                                f"finish_reason={split_result.finish_reason}, "
                                f"finish_message_present={split_result.finish_message_present}, "
                                f"max_output_tokens={chunk_max_output_tokens}, "
                                f"split_retry_used=True, "
                                f"prompt_token_count={split_result.usage.get('prompt_token_count')}, "
                                f"candidates_token_count={split_result.usage.get('candidates_token_count')}, "
                                f"thoughts_token_count={split_result.usage.get('thoughts_token_count')}, "
                                f"total_token_count={split_result.usage.get('total_token_count')}, "
                                f"model={model_name}"
                            )

                        split_translated_chunks.append(split_text)
                        split_has_unclear = split_has_unclear or bool(
                            split_result.data.get("has_unclear")
                        )

                    translated_text = "\n\n".join(split_translated_chunks).strip()
                    data = {
                        "text": translated_text,
                        "has_unclear": split_has_unclear,
                    }
                    if _is_translation_chunk_truncated(
                        chunk,
                        translated_text,
                        min_text_length=min_text_length,
                    ):
                        raise GeminiError(
                            "Gemini Hebrew translation split retry output appears truncated: "
                            f"chunk_index={index}/{len(chunks)}, "
                            f"source_length={chunk_source_len}, "
                            f"translation_length={len(translated_text)}, "
                            f"finish_reason={attempt_result.finish_reason}, "
                            f"finish_message_present={attempt_result.finish_message_present}, "
                            f"max_output_tokens={chunk_max_output_tokens}, "
                            f"split_retry_used=True, "
                            f"model={model_name}"
                        )
                    break

                raise GeminiError(
                    "Gemini Hebrew translation chunk appears truncated after retry: "
                    f"chunk_index={index}/{len(chunks)}, "
                    f"source_length={chunk_source_len}, "
                    f"translation_length={len(translated_text)}, "
                    f"finish_reason={attempt_result.finish_reason}, "
                    f"finish_message_present={attempt_result.finish_message_present}, "
                    f"max_output_tokens={chunk_max_output_tokens}, "
                    f"truncation_retry={is_truncation_retry}, "
                    f"split_retry_used=False, "
                    f"prompt_token_count={attempt_result.usage.get('prompt_token_count')}, "
                    f"candidates_token_count={attempt_result.usage.get('candidates_token_count')}, "
                    f"thoughts_token_count={attempt_result.usage.get('thoughts_token_count')}, "
                    f"total_token_count={attempt_result.usage.get('total_token_count')}, "
                    f"model={model_name}"
                )
            break

        translated_chunks.append(translated_text)
        if len(translated_text) < min_text_length or bool(data.get("has_unclear")):
            needs_review = True

    combined_text = "\n\n".join(translated_chunks).strip()
    if _is_combined_translation_truncated(
        len(stripped_source),
        len(combined_text),
        min_text_length=min_text_length,
    ):
        raise GeminiError(
            "Gemini Hebrew translation output appears truncated: "
            f"source_length={len(stripped_source)}, "
            f"translation_length={len(combined_text)}, "
            f"model={model_name}"
        )

    return GeminiResult(
        text=combined_text,
        needs_review=needs_review,
        engine_name=model_name,
        review_reasons=[],
    )
