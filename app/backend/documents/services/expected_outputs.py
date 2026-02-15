from __future__ import annotations

from typing import Literal

from documents.models import Document


ResultTypeStr = Literal["SOURCE_TEXT", "HEBREW_TEXT"]


def expected_result_types_for_document(doc: Document) -> list[ResultTypeStr]:
    """
    V2.0 expected outputs policy:
    - Hebrew doc: HEBREW_TEXT
    - Non-Hebrew doc: SOURCE_TEXT + HEBREW_TEXT (translation expected)
    """
    lang = (doc.language or "").strip().lower()

    if lang in ("he", "heb", "hebrew"):
        return ["HEBREW_TEXT"]

    # Non-Hebrew: expect both source OCR and Hebrew translation
    return ["SOURCE_TEXT", "HEBREW_TEXT"]

