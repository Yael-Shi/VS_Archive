from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from documents.services.page_extraction import PageImage


class HtrNotImplementedError(RuntimeError):
    pass


@dataclass(frozen=True)
class HtrResult:
    """
    Represents an HTR (handwriting recognition) result for the entire document.
    """

    text: str
    # In V2 MVP we don't have confidence. We still keep a hook for review signaling.
    needs_review: bool = False
    engine_name: str = "htr_placeholder_v1"


def transcribe_pages(
    pages: List[PageImage],
    language_hint: Optional[str],
) -> HtrResult:
    """
    Placeholder: connect this to Transkribus/metagrapho later.

    For now it fails explicitly so we can drive proper FAILED/ACTION_REQUIRED states
    rather than silently output dummy text.
    """
    raise HtrNotImplementedError(
        "HTR engine is not implemented yet. Next step: implement Transkribus/metagrapho adapter."
    )
