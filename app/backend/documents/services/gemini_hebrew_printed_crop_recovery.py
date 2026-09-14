"""Bounded Hebrew printed RECITATION crop recovery (geometry and assembly).

Checkpoint-backed Hebrew printed Gemini OCR may split a full page into two
horizontal crops after every configured full-page model candidate has returned
``RECITATION``. This module does not call Gemini.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from typing import Sequence

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from documents.models import Document
from documents.services.page_extraction import PageImage
from documents.services.review_reasons import HEBREW_PRINTED_RECITATION_CROP_RECOVERY

REVIEW_REASON_HEBREW_PRINTED_RECITATION_CROP_RECOVERY = (
    HEBREW_PRINTED_RECITATION_CROP_RECOVERY
)

HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY = (
    "hebrew-printed-recitation-horizontal-crops-v1"
)
HEBREW_PRINTED_RECITATION_CROP_COUNT = 2
HEBREW_PRINTED_RECITATION_CROP_OVERLAP_DENOMINATOR = 8
HEBREW_PRINTED_RECITATION_CROP_OVERLAP_MIN_PX = 64
HEBREW_PRINTED_RECITATION_CROP_OVERLAP_MAX_PX = 160
HEBREW_PRINTED_RECITATION_CROP_MIN_HEIGHT_PX = 192


@dataclass(frozen=True)
class HebrewPrintedCropBox:
    crop_index: int
    left: int
    top: int
    right: int
    bottom: int


@dataclass(frozen=True)
class HebrewPrintedCropPlan:
    boxes: tuple[HebrewPrintedCropBox, ...]
    crops: tuple[PageImage, ...]


def hebrew_printed_recitation_crop_recovery_policy(
    *,
    language_hint: str | None,
    text_input_type: str | None,
) -> str:
    if (
        language_hint == Document.Language.HEBREW
        and text_input_type == Document.TextInputType.PRINTED
    ):
        return HEBREW_PRINTED_RECITATION_CROP_RECOVERY_POLICY
    return ""


def overlap_px_for_height(height: int) -> int:
    return min(
        HEBREW_PRINTED_RECITATION_CROP_OVERLAP_MAX_PX,
        max(
            HEBREW_PRINTED_RECITATION_CROP_OVERLAP_MIN_PX,
            height // HEBREW_PRINTED_RECITATION_CROP_OVERLAP_DENOMINATOR,
        ),
    )


def horizontal_crop_boxes(
    *, width: int, height: int
) -> tuple[HebrewPrintedCropBox, ...]:
    if width < 1 or height < HEBREW_PRINTED_RECITATION_CROP_MIN_HEIGHT_PX:
        return ()
    overlap = overlap_px_for_height(height)
    mid = height // 2
    if overlap >= mid:
        return ()
    top_bottom = min(height, mid + overlap)
    bottom_top = max(0, mid - overlap)
    return (
        HebrewPrintedCropBox(
            crop_index=1,
            left=0,
            top=0,
            right=width,
            bottom=top_bottom,
        ),
        HebrewPrintedCropBox(
            crop_index=2,
            left=0,
            top=bottom_top,
            right=width,
            bottom=height,
        ),
    )


def plan_hebrew_printed_recitation_crops(
    page: PageImage,
) -> HebrewPrintedCropPlan | None:
    try:
        with Image.open(io.BytesIO(page.image_bytes)) as image:
            image.load()
            working = image.convert("RGB")
            width, height = working.size
            boxes = horizontal_crop_boxes(width=width, height=height)
            if len(boxes) != HEBREW_PRINTED_RECITATION_CROP_COUNT:
                return None
            crops: list[PageImage] = []
            for box in boxes:
                cropped = working.crop((box.left, box.top, box.right, box.bottom))
                crops.append(
                    PageImage(
                        page_index=page.page_index,
                        image_bytes=_encode_crop_png(cropped, box),
                        mime_type="image/png",
                        source_identity=page.source_identity,
                        source_content_fingerprint=page.source_content_fingerprint,
                    )
                )
    except Exception:
        return None
    encoded_bytes = tuple(crop.image_bytes for crop in crops)
    if len(set(encoded_bytes)) != len(encoded_bytes):
        return None
    return HebrewPrintedCropPlan(boxes=boxes, crops=tuple(crops))


def _encode_crop_png(cropped: Image.Image, box: HebrewPrintedCropBox) -> bytes:
    """Encode one crop as PNG with a crop-identity chunk.

    Symmetric overlapping windows often have the same pixel size. On a uniform
    page those pixel buffers would otherwise serialize to identical PNG bytes,
    so crop 2 would be indistinguishable from crop 1 for provider calls and
    tests. The visible pixels stay the crop rectangle; only PNG tEXt identity
    differs.
    """
    pnginfo = PngInfo()
    pnginfo.add_text("vs_archive_hebrew_printed_crop_index", str(box.crop_index))
    pnginfo.add_text(
        "vs_archive_hebrew_printed_crop_box",
        f"{box.left},{box.top},{box.right},{box.bottom}",
    )
    encoded = io.BytesIO()
    cropped.save(encoded, format="PNG", pnginfo=pnginfo)
    return encoded.getvalue()


def merge_overlapping_crop_texts(upper: str, lower: str) -> str:
    """Join two reading-order crop transcripts, dropping an exact line overlap."""
    upper_lines = _boundary_stripped_lines(upper)
    lower_lines = _boundary_stripped_lines(lower)
    overlap_k = _exact_suffix_prefix_overlap(upper_lines, lower_lines)
    if overlap_k:
        merged = upper_lines + lower_lines[overlap_k:]
    else:
        merged = upper_lines + lower_lines
    return "\n".join(merged).strip()


def crop_assembly_engine_name(models: Sequence[str]) -> str:
    """Return crop-assembly provenance, never a bare runtime model id.

    Ordered ``(crop_index, model)`` pairs are hashed so a two-crop page is
    distinguishable from ordinary full-page OCR even when every crop used the
    same model.
    """
    normalized = [str(model).strip() for model in models]
    if not normalized or any(not model for model in normalized):
        raise ValueError("Crop assembly requires concrete model names")
    mapping = [
        {"crop_index": index, "model": model}
        for index, model in enumerate(normalized, start=1)
    ]
    encoded = json.dumps(
        mapping,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"gemini-crop:{hashlib.sha256(encoded).hexdigest()[:48]}"


def _boundary_stripped_lines(text: str) -> list[str]:
    lines = (text or "").splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _exact_suffix_prefix_overlap(
    upper_lines: Sequence[str],
    lower_lines: Sequence[str],
) -> int:
    max_k = min(len(upper_lines), len(lower_lines))
    for k in range(max_k, 0, -1):
        if list(upper_lines[-k:]) == list(lower_lines[:k]):
            return k
    return 0
