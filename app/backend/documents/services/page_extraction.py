from __future__ import annotations

import io
from dataclasses import dataclass
from typing import List, Optional, Tuple

import fitz
from PIL import Image


@dataclass(frozen=True)
class PageImage:
    page_index: int  # 1-based
    image_bytes: bytes
    mime_type: str  # e.g. "image/png"


def _normalize_image_to_png(image_bytes: bytes) -> Tuple[bytes, str]:
    """
    Ensure we output a stable format (PNG) for downstream HTR engines.
    """
    img = Image.open(io.BytesIO(image_bytes))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), "image/png"


def extract_pages(file_bytes: bytes, mime_type: Optional[str]) -> List[PageImage]:
    """
    Convert an uploaded file to a list of per-page images.

    V2 strategy:
    - If PDF (scanned): render each page to an image (PNG).
    - If IMAGE: treat as a single page image (normalize to PNG).
    """
    mt = (mime_type or "").strip().lower()

    if mt == "application/pdf":
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        pages: List[PageImage] = []
        # Render each page to pixmap, convert to PNG bytes.
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(
                dpi=200
            )  # 200dpi is a reasonable start for handwriting scans
            png_bytes = pix.tobytes("png")
            pages.append(
                PageImage(
                    page_index=i + 1, image_bytes=png_bytes, mime_type="image/png"
                )
            )
        return pages

    if mt.startswith("image/") or mt in (
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
    ):
        png_bytes, out_mt = _normalize_image_to_png(file_bytes)
        return [PageImage(page_index=1, image_bytes=png_bytes, mime_type=out_mt)]

    raise ValueError(f"Unsupported mime_type for page extraction: {mime_type!r}")


def source_file_bytes_to_page(
    order_index: int,
    file_bytes: bytes,
    mime_type: Optional[str],
) -> PageImage:
    """
    Build one ``PageImage`` for a multi-image ``DocumentSourceFile``.

    Multi-image V1 supports IMAGE source files only. Bytes are normalized to PNG, matching
    the legacy single-image path (``extract_pages``). ``page_index`` is 1-based and contiguous
    (``order_index + 1``) to preserve the existing PageImage convention and Transkribus pageNr
    semantics; the source mapping is ``page_index - 1 == order_index``.
    """
    mt = (mime_type or "").strip().lower()
    if not mt.startswith("image/"):
        raise ValueError(
            f"Unsupported mime_type for multi-image source file at order_index="
            f"{order_index}: {mime_type!r} (images only in V1)"
        )
    png_bytes, out_mt = _normalize_image_to_png(file_bytes)
    return PageImage(
        page_index=order_index + 1,
        image_bytes=png_bytes,
        mime_type=out_mt,
    )
