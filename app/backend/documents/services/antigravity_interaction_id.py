"""Antigravity Interactions id charset and length contract.

Shared so worker poll/create sanitization and operator bind-interaction accept
the same ids. This module must not import Gemini/HTTP client code.
"""

from __future__ import annotations

import re

# Engine poll/create/log contract (antigravity_engine).
ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN = 512
# ArabicPrintedOcrBandCheckpoint.primary/fallback_interaction_id max_length.
ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN = 128
ANTIGRAVITY_INTERACTION_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")


def is_antigravity_interaction_id(
    value: object,
    *,
    max_length: int = ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN,
) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) < 1 or len(value) > max_length:
        return False
    return ANTIGRAVITY_INTERACTION_ID_RE.fullmatch(value) is not None


def antigravity_interaction_id_or_none(
    value: object,
    *,
    max_length: int = ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN,
) -> str | None:
    if is_antigravity_interaction_id(value, max_length=max_length):
        return str(value)
    return None
