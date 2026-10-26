"""Hebrew-aware order for public names.

Item people, item non-person links, and later card or directory work should
sort with ``hebrew_public_sort_key`` and then a stable identity id. The key
uses the name readers sort by. Callers that show a Person honorific keep that
honorific out of the key.

This module does not read or write the database.
"""

from __future__ import annotations

import unicodedata

# Long dashes and hyphen-like marks. Hebrew maqaf (U+05BE) is not included.
LONG_DASH_CHARACTERS: tuple[str, ...] = (
    "\u2010",  # hyphen
    "\u2011",  # non-breaking hyphen
    "\u2012",  # figure dash
    "\u2013",  # en dash
    "\u2014",  # em dash
    "\u2015",  # horizontal bar
    "\u2212",  # minus
    "\ufe58",  # small em dash
    "\ufe63",  # small hyphen-minus
    "\uff0d",  # fullwidth hyphen-minus
)

_DASH_FOLD = str.maketrans({ord(char): "-" for char in LONG_DASH_CHARACTERS})
_HEBREW_FINAL_TO_REGULAR = str.maketrans("ךםןףץ", "כמנפצ")

_HEBREW_RANK = 0
_LATIN_RANK = 1
_OTHER_RANK = 2
_EMPTY_RANK = 3


def fold_long_dashes(text: str) -> str:
    """Replace long dash characters with ASCII ``-``. Maqaf is unchanged."""

    return (text or "").translate(_DASH_FOLD)


def hebrew_public_sort_key(name: str) -> tuple[int, str]:
    """Return ``(script rank, comparison string)`` for one displayed name.

    NFC, casefold, collapsed whitespace, and long-dash folding happen first.
    Final Hebrew letters compare as their regular letters. Leading punctuation,
    marks, and symbols do not decide the script and are not part of the
    comparison string. Hebrew sorts before Latin, then every other script.
    Equal keys need the caller's identity id to stay stable.
    """

    text = unicodedata.normalize("NFC", name or "")
    text = fold_long_dashes(text)
    text = " ".join(text.split())
    text = text.casefold()
    text = text.translate(_HEBREW_FINAL_TO_REGULAR)
    meaningful = _without_leading_punctuation(text)
    if not meaningful:
        return (_EMPTY_RANK, "")
    return (_script_rank(meaningful), meaningful)


def _without_leading_punctuation(text: str) -> str:
    for index, char in enumerate(text):
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith(("M", "P", "S", "C")):
            continue
        return text[index:]
    return ""


def _script_rank(text: str) -> int:
    for char in text:
        if char.isspace():
            continue
        if "\u0590" <= char <= "\u05ff":
            return _HEBREW_RANK
        code = ord(char)
        if (
            ("a" <= char <= "z")
            or (0x00C0 <= code <= 0x024F)
            or (0x1E00 <= code <= 0x1EFF)
        ):
            return _LATIN_RANK
        return _OTHER_RANK
    return _EMPTY_RANK
