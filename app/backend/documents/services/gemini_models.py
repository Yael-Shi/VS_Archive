from __future__ import annotations

from documents.models import Document

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
FALLBACK_GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_36_FLASH_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_MODEL_CANDIDATES = (DEFAULT_GEMINI_MODEL, FALLBACK_GEMINI_MODEL)
DEFAULT_HEBREW_PRINTED_GEMINI_MODEL = "gemini-3.1-flash-lite"
# Hebrew printed keeps 3.1 Flash-Lite first. RECITATION on that primary
# immediately advances to the already-approved 3.6 Flash recovery model.
HEBREW_PRINTED_GEMINI_RECITATION_FALLBACK_MODEL = GEMINI_36_FLASH_MODEL


def hebrew_printed_gemini_model_candidates(primary: str) -> tuple[str, ...]:
    """Ordered Hebrew printed candidates: configured primary, then 3.6."""
    normalized = str(primary).strip() or DEFAULT_HEBREW_PRINTED_GEMINI_MODEL
    if normalized == HEBREW_PRINTED_GEMINI_RECITATION_FALLBACK_MODEL:
        return (normalized,)
    return (normalized, HEBREW_PRINTED_GEMINI_RECITATION_FALLBACK_MODEL)


LATIN_HANDWRITTEN_GEMINI_MODEL = "gemini-2.5-flash"
LATIN_HANDWRITTEN_GEMINI_MODEL_CANDIDATES = (
    LATIN_HANDWRITTEN_GEMINI_MODEL,
    FALLBACK_GEMINI_MODEL,
)

# Live full-page probes on French handwritten document 273 selected Gemini 3.6
# Flash over the existing 2.5 Flash -> 3.1 Flash-Lite chain. Keep this
# French-only so the established English handwritten route remains unchanged.
FRENCH_HANDWRITTEN_GEMINI_MODEL = GEMINI_36_FLASH_MODEL
FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES = (FRENCH_HANDWRITTEN_GEMINI_MODEL,)

# Keep the inexpensive 2.5 Flash model first for ordinary Hebrew general
# handwriting. If its first page call returns MAX_TOKENS or RECITATION, the
# checkpoint-backed adapter advances to the 3.6 Flash model selected by the
# successful full-page probes. Hebrew VS handwriting remains on Transkribus.
HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL = GEMINI_36_FLASH_MODEL
HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES = (
    DEFAULT_GEMINI_MODEL,
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL,
)

LATIN_PRINTED_GEMINI_MODEL = "gemini-2.5-flash"
# English and French printed keep 2.5 Flash for a normal page. MAX_TOKENS or
# RECITATION on that primary may advance to 3.6 Flash inside the shared
# three-call page budget.
LATIN_PRINTED_GEMINI_MODEL_CANDIDATES = (
    LATIN_PRINTED_GEMINI_MODEL,
    GEMINI_36_FLASH_MODEL,
)
# French and Hebrew MIXED keep the same primary and the same bounded 3.6
# recovery. Live probes succeeded on 3.6 after 2.5 returned RECITATION
# (French document 436) or exhausted MAX_TOKENS (Hebrew document 438).
# English and Arabic MIXED stay on the default chain.
MIXED_GEMINI_36_MODEL_CANDIDATES = (
    DEFAULT_GEMINI_MODEL,
    GEMINI_36_FLASH_MODEL,
)
# Kept for callers that imported the English-printed name.
ENGLISH_PRINTED_GEMINI_MODEL_CANDIDATES = LATIN_PRINTED_GEMINI_MODEL_CANDIDATES

_LATIN_PRINTED_FALLBACK_LANGUAGES = frozenset(
    {
        Document.Language.ENGLISH,
        Document.Language.FRENCH,
    }
)


def latin_printed_model_fallback_eligible(
    language: str | None,
    text_input_type: str | None,
) -> bool:
    """English and French printed share the 2.5 then 3.6 fallback chain."""
    lang = (language or "").strip().lower()
    text_type = (text_input_type or "").strip().upper()
    return (
        lang in _LATIN_PRINTED_FALLBACK_LANGUAGES
        and text_type == Document.TextInputType.PRINTED
    )


_MIXED_GEMINI_36_FALLBACK_LANGUAGES = frozenset(
    {
        Document.Language.FRENCH,
        Document.Language.HEBREW,
    }
)


def mixed_gemini_36_fallback_eligible(
    language: str | None,
    text_input_type: str | None,
) -> bool:
    """French and Hebrew MIXED fall back from 2.5 Flash to Gemini 3.6."""
    lang = (language or "").strip().lower()
    text_type = (text_input_type or "").strip().upper()
    return (
        lang in _MIXED_GEMINI_36_FALLBACK_LANGUAGES
        and text_type == Document.TextInputType.MIXED
    )


def primary_then_gemini_36_fallback_eligible(
    language: str | None,
    text_input_type: str | None,
) -> bool:
    """Routes that keep 2.5 Flash first and may advance to 3.6 Flash.

    Advancement stays inside the existing checkpoint page budget and is limited
    to MAX_TOKENS or RECITATION. English and French printed share one chain.
    French and Hebrew MIXED use the same recovery rule. Hebrew printed crop
    recovery and mixed-script region fallback stay on printed documents.
    """
    return latin_printed_model_fallback_eligible(
        language,
        text_input_type,
    ) or mixed_gemini_36_fallback_eligible(language, text_input_type)
