"""Bounded mixed-script region fallback for Hebrew printed RECITATION crops.

After crop recovery, a failed Hebrew printed crop may contain a substantial
structural candidate that is then OCRed with the existing Latin printed path.
This module does not call Gemini. Pillow ink analysis plans structure only;
Latin acceptance comes from OCR output plus Unicode script dominance.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from documents.models import Document
from documents.services.page_extraction import PageImage
from documents.services.review_reasons import (
    HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK,
)

REVIEW_REASON_HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK = (
    HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK
)

HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK_POLICY = (
    "hebrew-printed-mixed-script-region-fallback-v1"
)

# Substantial-region gates. A few names, URLs, or citations stay incidental.
MIN_DOMINANT_SCRIPT_LETTERS = 40
SCRIPT_DOMINANCE_RATIO = 0.80
MAX_STRUCTURAL_CANDIDATE_CONTENT_SPANS = 1
MAX_LATIN_REGION_PROVIDER_CALLS = 1
INK_LUMA_MAX = 180
MIN_CONTENT_ROWS = 32
MIN_GAP_ROWS = 16
CONTENT_HEIGHT_DENOMINATOR = 12
GAP_HEIGHT_DENOMINATOR = 30
MIN_INK_COLUMNS_DENOMINATOR = 40
MIN_INK_COLUMNS = 3
REGION_PAD_PX = 4

# Technical tokens excluded from Hebrew-vs-Latin letter dominance only.
# Ordinary Latin words, names, titles, and sentences are still counted.
_URL_TOKEN_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"']+")
_EMAIL_TOKEN_RE = re.compile(
    r"(?i)\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)
_DOMAIN_TOKEN_RE = re.compile(
    r"(?i)\b(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,}\b"
)


class ScriptDominance(StrEnum):
    HEBREW = "hebrew"
    LATIN = "latin"
    AMBIGUOUS = "ambiguous"
    EMPTY = "empty"


class MixedScriptPlanDecision(StrEnum):
    NO_REGION = "no_region"
    STRUCTURAL_AMBIGUOUS = "structural_ambiguous"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class ScriptLetterCounts:
    hebrew: int
    latin: int
    other: int
    technical_token_letters_excluded: int = 0

    @property
    def identified(self) -> int:
        return self.hebrew + self.latin


@dataclass(frozen=True)
class ScriptDominanceEvaluation:
    dominance: ScriptDominance
    latin_letters: int
    hebrew_letters: int
    identified_letters: int
    latin_ratio: float
    hebrew_ratio: float
    technical_token_letters_excluded: int


@dataclass(frozen=True)
class MixedScriptRegionBox:
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class MixedScriptStructuralCandidatePlan:
    decision: MixedScriptPlanDecision
    box: MixedScriptRegionBox | None = None
    region: PageImage | None = None


@dataclass(frozen=True)
class MixedScriptRegionProvenance:
    order: int
    script: str
    model: str
    source: str
    region_box: MixedScriptRegionBox | None = None


def hebrew_printed_mixed_script_region_fallback_policy(
    *,
    language_hint: str | None,
    text_input_type: str | None,
) -> str:
    if (
        language_hint == Document.Language.HEBREW
        and text_input_type == Document.TextInputType.PRINTED
    ):
        return HEBREW_PRINTED_MIXED_SCRIPT_REGION_FALLBACK_POLICY
    return ""


def count_script_letters(text: str) -> ScriptLetterCounts:
    source = text or ""
    stripped = _text_without_technical_latin_tokens(source)
    hebrew = 0
    latin = 0
    other = 0
    for char in stripped:
        if not char.isalpha():
            continue
        code = ord(char)
        if 0x0590 <= code <= 0x05FF:
            hebrew += 1
        elif _is_latin_letter(code):
            latin += 1
        else:
            other += 1
    return ScriptLetterCounts(
        hebrew=hebrew,
        latin=latin,
        other=other,
        technical_token_letters_excluded=(
            _alpha_letter_count(source) - _alpha_letter_count(stripped)
        ),
    )


def evaluate_script_dominance(text: str) -> ScriptDominanceEvaluation:
    counts = count_script_letters(text)
    identified = counts.identified
    if identified > 0:
        hebrew_ratio = counts.hebrew / identified
        latin_ratio = counts.latin / identified
    else:
        hebrew_ratio = 0.0
        latin_ratio = 0.0
    if counts.hebrew < MIN_DOMINANT_SCRIPT_LETTERS and (
        counts.latin < MIN_DOMINANT_SCRIPT_LETTERS
    ):
        dominance = ScriptDominance.EMPTY
    elif identified <= 0:
        dominance = ScriptDominance.EMPTY
    elif (
        counts.hebrew >= MIN_DOMINANT_SCRIPT_LETTERS
        and hebrew_ratio >= SCRIPT_DOMINANCE_RATIO
    ):
        dominance = ScriptDominance.HEBREW
    elif (
        counts.latin >= MIN_DOMINANT_SCRIPT_LETTERS
        and latin_ratio >= SCRIPT_DOMINANCE_RATIO
    ):
        dominance = ScriptDominance.LATIN
    else:
        dominance = ScriptDominance.AMBIGUOUS
    return ScriptDominanceEvaluation(
        dominance=dominance,
        latin_letters=counts.latin,
        hebrew_letters=counts.hebrew,
        identified_letters=identified,
        latin_ratio=latin_ratio,
        hebrew_ratio=hebrew_ratio,
        technical_token_letters_excluded=counts.technical_token_letters_excluded,
    )


def script_dominance(text: str) -> ScriptDominance:
    return evaluate_script_dominance(text).dominance


def hebrew_text_is_reusable_for_mixed_script(text: str) -> bool:
    """Successful Hebrew printed crop text must be Hebrew-dominant.

    Incidental Latin names still count toward dominance. URL, email, and
    domain-like technical tokens are ignored in that count only. They do not
    create a split and they do not block reuse of that crop.
    """
    return script_dominance(text) == ScriptDominance.HEBREW


def latin_text_is_acceptably_dominant(text: str) -> bool:
    return script_dominance(text) == ScriptDominance.LATIN


def plan_structural_candidate_region(
    page: PageImage,
) -> MixedScriptStructuralCandidatePlan:
    """Find the single substantial structural candidate band in a failed crop.

    Layout uses a local horizontal ink profile only. It does not classify
    script. Exactly one substantial span is required before a Latin printed
    OCR probe. Zero spans skip mixed-script recovery. Two or more spans are
    structurally ambiguous and fail closed without a probe.
    """
    try:
        with Image.open(io.BytesIO(page.image_bytes)) as image:
            image.load()
            working = image.convert("RGB")
            width, height = working.size
            spans = _content_spans(working)
    except Exception:
        return MixedScriptStructuralCandidatePlan(
            decision=MixedScriptPlanDecision.NO_REGION
        )

    if not spans:
        return MixedScriptStructuralCandidatePlan(
            decision=MixedScriptPlanDecision.NO_REGION
        )
    if len(spans) != MAX_STRUCTURAL_CANDIDATE_CONTENT_SPANS:
        return MixedScriptStructuralCandidatePlan(
            decision=MixedScriptPlanDecision.STRUCTURAL_AMBIGUOUS
        )

    top, bottom = spans[0]
    top = max(0, top - REGION_PAD_PX)
    bottom = min(height, bottom + REGION_PAD_PX)
    if bottom - top < MIN_CONTENT_ROWS:
        return MixedScriptStructuralCandidatePlan(
            decision=MixedScriptPlanDecision.NO_REGION
        )

    box = MixedScriptRegionBox(left=0, top=top, right=width, bottom=bottom)
    try:
        with Image.open(io.BytesIO(page.image_bytes)) as image:
            image.load()
            cropped = image.convert("RGB").crop(
                (box.left, box.top, box.right, box.bottom)
            )
            region = PageImage(
                page_index=page.page_index,
                image_bytes=_encode_region_png(cropped, box),
                mime_type="image/png",
                source_identity=page.source_identity,
                source_content_fingerprint=page.source_content_fingerprint,
            )
    except Exception:
        return MixedScriptStructuralCandidatePlan(
            decision=MixedScriptPlanDecision.NO_REGION
        )
    return MixedScriptStructuralCandidatePlan(
        decision=MixedScriptPlanDecision.CANDIDATE,
        box=box,
        region=region,
    )


def mixed_script_assembly_engine_name(
    regions: Sequence[MixedScriptRegionProvenance],
) -> str:
    """Return mixed-region provenance, never a bare runtime model id."""
    if not regions:
        raise ValueError("Mixed-script assembly requires region provenance")
    mapping = [
        {
            "model": region.model.strip(),
            "order": region.order,
            "region_box": _canonical_region_box(region.region_box),
            "script": region.script,
            "source": region.source,
        }
        for region in regions
    ]
    if any(not item["model"] for item in mapping):
        raise ValueError("Mixed-script assembly requires concrete model names")
    encoded = json.dumps(
        mapping,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"gemini-regions:{hashlib.sha256(encoded).hexdigest()[:48]}"


def _canonical_region_box(
    box: MixedScriptRegionBox | None,
) -> dict[str, int] | None:
    if box is None:
        return None
    return {
        "bottom": box.bottom,
        "left": box.left,
        "right": box.right,
        "top": box.top,
    }


def _alpha_letter_count(text: str) -> int:
    return sum(1 for char in text if char.isalpha())


def _text_without_technical_latin_tokens(text: str) -> str:
    """Drop identifiable URL/email/domain tokens before script letter counts."""
    stripped = _URL_TOKEN_RE.sub(" ", text or "")
    stripped = _EMAIL_TOKEN_RE.sub(" ", stripped)
    return _DOMAIN_TOKEN_RE.sub(" ", stripped)


def _is_latin_letter(code: int) -> bool:
    return (
        0x0041 <= code <= 0x005A
        or 0x0061 <= code <= 0x007A
        or 0x00C0 <= code <= 0x024F
        or 0x1E00 <= code <= 0x1EFF
    )


def _content_spans(image: Image.Image) -> tuple[tuple[int, int], ...]:
    width, height = image.size
    gray = image.convert("L")
    data = gray.tobytes()
    min_ink = max(MIN_INK_COLUMNS, width // MIN_INK_COLUMNS_DENOMINATOR)
    min_content = max(MIN_CONTENT_ROWS, height // CONTENT_HEIGHT_DENOMINATOR)
    min_gap = max(MIN_GAP_ROWS, height // GAP_HEIGHT_DENOMINATOR)
    content_rows = []
    for y in range(height):
        row = data[y * width : (y + 1) * width]
        ink = sum(1 for pixel in row if pixel <= INK_LUMA_MAX)
        content_rows.append(ink >= min_ink)

    raw_spans: list[tuple[int, int]] = []
    start: int | None = None
    for y, is_content in enumerate(content_rows):
        if is_content and start is None:
            start = y
        elif not is_content and start is not None:
            raw_spans.append((start, y))
            start = None
    if start is not None:
        raw_spans.append((start, height))

    if not raw_spans:
        return ()

    merged: list[tuple[int, int]] = [raw_spans[0]]
    for span_start, span_end in raw_spans[1:]:
        prev_start, prev_end = merged[-1]
        if span_start - prev_end < min_gap:
            merged[-1] = (prev_start, span_end)
        else:
            merged.append((span_start, span_end))

    substantial = tuple(
        (span_start, span_end)
        for span_start, span_end in merged
        if span_end - span_start >= min_content
    )
    return substantial


def _encode_region_png(cropped: Image.Image, box: MixedScriptRegionBox) -> bytes:
    pnginfo = PngInfo()
    pnginfo.add_text("vs_archive_hebrew_printed_mixed_script", "structural-candidate")
    pnginfo.add_text(
        "vs_archive_hebrew_printed_mixed_script_box",
        f"{box.left},{box.top},{box.right},{box.bottom}",
    )
    encoded = io.BytesIO()
    cropped.save(encoded, format="PNG", pnginfo=pnginfo)
    return encoded.getvalue()
