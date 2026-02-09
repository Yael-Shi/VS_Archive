from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from google.cloud import vision

from documents.services.page_extraction import PageImage


class HtrNotImplementedError(RuntimeError):
    pass


@dataclass(frozen=True)
class HtrResult:
    text: str
    needs_review: bool = False
    engine_name: str = "google_vision_v1"


def _guess_needs_review(text: str) -> bool:
    # שמרני ל-MVP: אם יצא ממש מעט טקסט, נסמן לבדיקה
    stripped = (text or "").strip()
    return len(stripped) < 20


def transcribe_pages(
    pages: List[PageImage],
    language_hint: Optional[str],
) -> HtrResult:
    """
    MVP: Use Google Cloud Vision to OCR page images (PNG bytes).
    """
    client = vision.ImageAnnotatorClient()

    texts: list[str] = []
    any_review = False

    for p in pages:
        image = vision.Image(content=p.image_bytes)
        # document_text_detection לרוב טוב יותר למסמכים מאשר text_detection
        resp = client.document_text_detection(image=image)

        if resp.error and resp.error.message:
            raise RuntimeError(
                f"Google Vision error on page {p.page_index}: {resp.error.message}"
            )

        page_text = (resp.full_text_annotation.text or "").strip()
        texts.append(page_text)
        any_review = any_review or _guess_needs_review(page_text)

    full_text = "\n\n".join([t for t in texts if t])

    # אם יצא ריק לגמרי – זה לא הצליח מבחינת MVP “טקסט אמיתי”
    if not full_text.strip():
        raise RuntimeError("Google Vision returned empty text")

    return HtrResult(
        text=full_text,
        needs_review=any_review,
        engine_name="google_vision_v1",
    )
