"""Public presentation for effective text quality (PR2).

Business rules stay in ``text_quality``. This module only builds labels,
accessible phrasing, CSS modifiers, and tooltip copy for templates.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import DocumentTextResult, ManualTextContent
from documents.services.text_quality import (
    HUMAN_VERIFIED,
    NEEDS_CORRECTION,
    PUBLIC_TEXT_QUALITY_HEADING,
    PUBLIC_TEXT_QUALITY_LABELS,
    effective_public_text_quality_for_manual_text,
    effective_public_text_quality_for_result,
)

TEXT_QUALITY_TOOLTIP_TITLE = "מה המשמעות של דירוג האיכות?"
TEXT_QUALITY_TOOLTIP_INTRO = (
    "הדירוג מסייע להעריך עד כמה ניתן להסתמך על הטקסט המוצג."
)
TEXT_QUALITY_TOOLTIP_FOOTER = (
    "ייתכנו שגיאות גם בטקסטים המדורגים באיכות טובה."
)
# General explanation only. Does not mean HEBREW_TEXT quality is persisted
# as inherited/capped from SOURCE_TEXT (that writer is still deferred).
TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE = (
    "בתרגום לעברית, האיכות תלויה גם באיכות התעתוק שעליו הוא מבוסס."
)

_TOOLTIP_LEVEL_EXPLANATIONS: tuple[tuple[str, str], ...] = (
    (
        DocumentTextResult.Quality.UNKNOWN,
        "עדיין אין מספיק מידע להערכת האיכות.",
    ),
    (
        DocumentTextResult.Quality.LOW,
        "הטקסט עלול להכיל שגיאות משמעותיות.",
    ),
    (
        DocumentTextResult.Quality.MEDIUM,
        "הטקסט שימושי, אך חלקים ממנו עשויים להיות פחות מדויקים.",
    ),
    (
        DocumentTextResult.Quality.GOOD,
        "הטקסט נראה אמין, אך לא עבר אישור אנושי.",
    ),
    (
        HUMAN_VERIFIED,
        "הטקסט הוזן או נבדק על ידי אדם.",
    ),
    (
        NEEDS_CORRECTION,
        "הטקסט נבדק ונמצא שהוא דורש תיקון.",
    ),
)

_CSS_MODIFIER = {
    DocumentTextResult.Quality.UNKNOWN: "unknown",
    DocumentTextResult.Quality.LOW: "low",
    DocumentTextResult.Quality.MEDIUM: "medium",
    DocumentTextResult.Quality.GOOD: "good",
    HUMAN_VERIFIED: "human-verified",
    NEEDS_CORRECTION: "needs-correction",
}


@dataclass(frozen=True)
class PublicTextQualityIndicator:
    quality: str
    heading: str
    label: str
    accessible_name: str
    css_modifier: str
    show_verified_mark: bool
    tooltip_title: str
    tooltip_intro: str
    tooltip_levels: tuple[tuple[str, str], ...]
    tooltip_footer: str
    tooltip_translation_note: str
    help_dom_id: str


def _tooltip_levels() -> tuple[tuple[str, str], ...]:
    return tuple(
        (PUBLIC_TEXT_QUALITY_LABELS[quality], explanation)
        for quality, explanation in _TOOLTIP_LEVEL_EXPLANATIONS
    )


def _indicator_for_effective_quality(
    quality: str,
    *,
    help_dom_id: str,
    include_translation_note: bool = False,
) -> PublicTextQualityIndicator:
    label = PUBLIC_TEXT_QUALITY_LABELS[quality]
    return PublicTextQualityIndicator(
        quality=quality,
        heading=PUBLIC_TEXT_QUALITY_HEADING,
        label=label,
        accessible_name=f"{PUBLIC_TEXT_QUALITY_HEADING}: {label}",
        css_modifier=_CSS_MODIFIER[quality],
        show_verified_mark=quality == HUMAN_VERIFIED,
        tooltip_title=TEXT_QUALITY_TOOLTIP_TITLE,
        tooltip_intro=TEXT_QUALITY_TOOLTIP_INTRO,
        tooltip_levels=_tooltip_levels(),
        tooltip_footer=TEXT_QUALITY_TOOLTIP_FOOTER,
        tooltip_translation_note=(
            TEXT_QUALITY_TOOLTIP_TRANSLATION_NOTE if include_translation_note else ""
        ),
        help_dom_id=help_dom_id,
    )


def public_text_quality_indicator_for_result(
    row: DocumentTextResult,
    *,
    help_dom_id: str = "text-quality-help-transcription",
    include_translation_note: bool = False,
) -> PublicTextQualityIndicator | None:
    if not (row.text or "").strip():
        return None
    return _indicator_for_effective_quality(
        effective_public_text_quality_for_result(row),
        help_dom_id=help_dom_id,
        include_translation_note=include_translation_note,
    )


def public_text_quality_indicator_for_manual_text(
    content: ManualTextContent,
    *,
    help_dom_id: str = "text-quality-help-manual",
) -> PublicTextQualityIndicator | None:
    if not (content.body or "").strip():
        return None
    return _indicator_for_effective_quality(
        effective_public_text_quality_for_manual_text(content),
        help_dom_id=help_dom_id,
    )
