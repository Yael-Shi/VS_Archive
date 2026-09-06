"""Persist Gemini Hebrew translation for non-Hebrew OCR documents."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, List, Optional

from documents.models import Document, DocumentTextResult
from documents.services.review_reasons import (
    AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW,
    HAS_UNCLEAR,
    MIN_TEXT_LENGTH,
    NEEDS_REVIEW_FLAG,
)
from documents.services.text_quality import capped_inherited_base_quality

if TYPE_CHECKING:
    from documents.services.gemini_engine import GeminiResult


def _dedupe_strings_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def derive_hebrew_translation_review_reasons(
    text: str,
    needs_review: bool,
    engine_reasons: Optional[List[str]],
    *,
    min_text_length: int,
    include_automatic_policy: bool = True,
) -> List[str]:
    reasons: List[str] = []
    if include_automatic_policy:
        reasons.append(AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW)
    if needs_review:
        reasons.append(NEEDS_REVIEW_FLAG)

    stripped = (text or "").strip()
    if len(stripped) < min_text_length:
        reasons.append(MIN_TEXT_LENGTH)
    if "[UNCLEAR]" in stripped:
        reasons.append(HAS_UNCLEAR)

    if engine_reasons:
        for reason in engine_reasons:
            if reason:
                reasons.append(reason)

    return _dedupe_strings_preserve_order(reasons)


def persist_hebrew_translation_result(
    doc: Document,
    engine: str,
    *,
    translation: GeminiResult | None = None,
    error: Exception | None = None,
    min_text_length: int,
) -> None:
    """Write HEBREW_TEXT from a translation attempt. Does not modify SOURCE_TEXT."""
    if translation is not None and error is not None:
        raise ValueError("Provide translation or error, not both.")
    if translation is None and error is None:
        raise ValueError("Provide translation or error.")

    if error is not None:
        DocumentTextResult.objects.update_or_create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=engine,
            defaults={
                "status": DocumentTextResult.Status.FAILED,
                "text": None,
                "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
                "prompt_variant": DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
                "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                "error_code": "HEBREW_TRANSLATION_FAILED",
                "error_details": str(error),
                "review_reasons": "",
            },
        )
        return

    assert translation is not None
    review_reasons = derive_hebrew_translation_review_reasons(
        translation.text,
        translation.needs_review,
        translation.review_reasons,
        min_text_length=min_text_length,
        include_automatic_policy=True,
    )
    source_row = (
        DocumentTextResult.objects.filter(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
        )
        .values("source_revision", "quality")
        .first()
    )
    if source_row is None:
        source_revision = None
        source_quality = None
    else:
        source_revision = source_row["source_revision"]
        source_quality = source_row["quality"]
    # No independent translation score: inherit persisted SOURCE_TEXT base
    # quality. Missing/invalid source fails closed to UNKNOWN, not a new
    # persistence failure. Do not use verification_status / effective public
    # quality (HUMAN_VERIFIED / NEEDS_CORRECTION are never persisted).
    inherited_quality = capped_inherited_base_quality(source_quality)
    DocumentTextResult.objects.update_or_create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=engine,
        defaults={
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "text": translation.text,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
            "error_code": None,
            "error_details": None,
            "review_reasons": json.dumps(review_reasons),
            "based_on_source_revision": source_revision,
            "quality": inherited_quality,
        },
    )
