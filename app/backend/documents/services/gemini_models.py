from __future__ import annotations

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
FALLBACK_GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_36_FLASH_MODEL = "gemini-3.6-flash"
DEFAULT_GEMINI_MODEL_CANDIDATES = (DEFAULT_GEMINI_MODEL, FALLBACK_GEMINI_MODEL)
DEFAULT_HEBREW_PRINTED_GEMINI_MODEL = "gemini-3.1-flash-lite"

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
