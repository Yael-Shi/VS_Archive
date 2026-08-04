from __future__ import annotations

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
FALLBACK_GEMINI_MODEL = "gemini-3.1-flash-lite"
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
FRENCH_HANDWRITTEN_GEMINI_MODEL = "gemini-3.6-flash"
FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES = (FRENCH_HANDWRITTEN_GEMINI_MODEL,)

LATIN_PRINTED_GEMINI_MODEL = "gemini-2.5-flash"
