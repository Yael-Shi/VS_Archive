"""Effective public text quality (OCR DocumentTextResult + MANUAL_TEXT).

Persisted DocumentTextResult.quality is UNKNOWN/LOW/MEDIUM/GOOD only.
HUMAN_VERIFIED and NEEDS_CORRECTION are presentation-only and must not be
written onto DocumentTextResult.quality.

Effective DocumentTextResult precedence:
1. verification_status=VERIFIED -> HUMAN_VERIFIED
2. verification_status=REJECTED -> NEEDS_CORRECTION
3. otherwise -> persisted base quality

Public UI copy (rendered by text_quality_presentation / templates, not here):
heading איכות התעתוק; labels in PUBLIC_TEXT_QUALITY_LABELS.
"""

from __future__ import annotations

from documents.models import ArchiveItem, DocumentTextResult, ManualTextContent

HUMAN_VERIFIED = "HUMAN_VERIFIED"
NEEDS_CORRECTION = "NEEDS_CORRECTION"

PUBLIC_TEXT_QUALITY_HEADING = "איכות התעתוק"

PUBLIC_TEXT_QUALITY_LABELS = {
    DocumentTextResult.Quality.UNKNOWN: "טרם הוערך",
    DocumentTextResult.Quality.LOW: "איכות נמוכה",
    DocumentTextResult.Quality.MEDIUM: "איכות בינונית",
    DocumentTextResult.Quality.GOOD: "איכות טובה",
    HUMAN_VERIFIED: "נבדק ואושר",
    NEEDS_CORRECTION: "נדרש תיקון",
}

_PERSISTED_QUALITY_VALUES = frozenset(DocumentTextResult.Quality.values)

# Ordinal for a later SOURCE_TEXT → HEBREW_TEXT writer. UNKNOWN is the floor.
_PERSISTED_QUALITY_RANK = {
    DocumentTextResult.Quality.UNKNOWN: 0,
    DocumentTextResult.Quality.LOW: 1,
    DocumentTextResult.Quality.MEDIUM: 2,
    DocumentTextResult.Quality.GOOD: 3,
}


def _persisted_base_quality(value: str | None) -> str:
    if value in _PERSISTED_QUALITY_VALUES:
        return value
    return DocumentTextResult.Quality.UNKNOWN


def effective_public_text_quality_for_result(row: DocumentTextResult) -> str:
    """Public quality for one OCR/HTR/translation DocumentTextResult."""
    if row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED:
        return HUMAN_VERIFIED
    if row.verification_status == DocumentTextResult.VerificationStatus.REJECTED:
        return NEEDS_CORRECTION
    return _persisted_base_quality(row.quality)


def manual_text_content_is_staff_managed(content: ManualTextContent) -> bool:
    """Whether this MANUAL_TEXT body is the current human staff-managed path.

    Production create/edit is ``create_manual_text_archive_item`` /
    ``update_manual_text_archive_item`` (staff archive manage). Django admin
    cannot add or change ManualTextContent. There is no automated/import writer.

    A future imported/automated provenance should return False (or a more
    specific provenance flag) so effective quality is not HUMAN_VERIFIED.
    """
    return content.archive_item.item_type == ArchiveItem.ItemType.MANUAL_TEXT


def effective_public_text_quality_for_manual_text(
    content: ManualTextContent,
) -> str:
    """Public quality for a MANUAL_TEXT body (not a DocumentTextResult)."""
    if manual_text_content_is_staff_managed(content):
        return HUMAN_VERIFIED
    return DocumentTextResult.Quality.UNKNOWN


def capped_inherited_base_quality(
    source_quality: str | None,
    candidate_quality: str | None = None,
) -> str:
    """Hook for a later HEBREW_TEXT writer: inherit/cap from SOURCE_TEXT.

    Not called from ``persist_hebrew_translation_result`` yet (no independent
    translation scoring in this PR). Future persist should set
    ``defaults["quality"]`` from this helper:

    - ``candidate_quality`` UNKNOWN/omitted → inherit source base quality
    - otherwise → min(source, candidate) on the persisted ordinal
    - HUMAN_VERIFIED is not a persisted value and is treated as UNKNOWN
    """
    source = _persisted_base_quality(source_quality)
    candidate = _persisted_base_quality(candidate_quality)
    if candidate == DocumentTextResult.Quality.UNKNOWN:
        return source
    if _PERSISTED_QUALITY_RANK[candidate] <= _PERSISTED_QUALITY_RANK[source]:
        return candidate
    return source
