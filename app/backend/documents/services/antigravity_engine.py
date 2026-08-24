from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import requests

from documents.services.antigravity_defaults import (
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    INTERACTIONS_API_REVISION,
    INTERACTIONS_BASE_URL,
)
from documents.services.page_extraction import PageImage

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 300.0
COMPLETED_STATUS = "completed"
IN_PROGRESS_STATUS = "in_progress"

_IMAGE_HEADING_RE = re.compile(
    r"^\[IMAGE\s+(\d+)\s*:\s*[^\]]+\]\s*$",
    re.MULTILINE,
)


class AntigravityError(RuntimeError):
    """Raised when Antigravity Interactions OCR fails."""


@dataclass(frozen=True)
class AntigravityResult:
    text: str
    engine_name: str
    needs_review: bool = True


def _page_label(page: PageImage) -> str:
    mime = (page.mime_type or "").lower()
    if mime == "image/jpeg" or mime == "image/jpg":
        ext = "jpg"
    elif mime == "image/webp":
        ext = "webp"
    elif mime == "image/gif":
        ext = "gif"
    elif mime == "image/tiff":
        ext = "tiff"
    else:
        ext = "png"
    return f"page-{page.page_index}.{ext}"


def _sorted_pages(pages: list[PageImage]) -> list[PageImage]:
    return sorted(pages, key=lambda page: page.page_index)


def _request_headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
        "Api-Revision": INTERACTIONS_API_REVISION,
    }


def _raise_for_api_error(response: requests.Response) -> None:
    if response.ok:
        return
    message = response.text
    try:
        payload = response.json()
        error = payload.get("error") or {}
        message = error.get("message") or message
    except ValueError:
        pass
    raise AntigravityError(f"HTTP {response.status_code}: {message}")


def normalize_antigravity_image_headings(text: str) -> str:
    """
    Strip or replace Antigravity multi-image section headings for stored OCR text.

    Single-image output drops the ``[IMAGE N: filename]`` line entirely.
    Multi-image output replaces each heading with ``עמוד N`` (no filenames).
    """
    headings = list(_IMAGE_HEADING_RE.finditer(text))
    if not headings:
        return text
    if len(headings) == 1:
        match = headings[0]
        start, end = match.span()
        if end < len(text) and text[end] == "\n":
            end += 1
        return text[:start] + text[end:]
    return _IMAGE_HEADING_RE.sub(
        lambda match: f"עמוד {int(match.group(1))}",
        text,
    )


def output_text_from_steps(steps: list[dict[str, Any]] | None) -> str | None:
    chunks: list[str] = []
    for step in steps or []:
        if step.get("type") != "model_output":
            continue
        for content in step.get("content") or []:
            if content.get("type") == "text":
                text = content.get("text")
                if text:
                    chunks.append(text)
    joined = "\n".join(chunks)
    return joined if joined.strip() else None


def build_antigravity_ocr_prompt(image_labels: list[str]) -> str:
    image_order = "\n".join(
        f"- IMAGE {index}: {name}" for index, name in enumerate(image_labels, start=1)
    )
    heading_examples = "\n\n".join(
        f"[IMAGE {index}: {name}]\n<transcription for image {index}>"
        for index, name in enumerate(image_labels, start=1)
    )
    return (
        "You are transcribing historical archive document page images.\n"
        "TASK: OCR/transcription only. Do not translate.\n"
        "RULES:\n"
        "- Preserve Arabic, Hebrew, and Latin scripts exactly as written.\n"
        "- Preserve names, dates, page numbers, document numbers, and punctuation.\n"
        "- Include cover/catalog page text when present.\n"
        "- Include occasional handwritten additions when visible.\n"
        "- Prefer [UNCLEAR] over inventing confident text.\n"
        "- Do not browse the web, run code, or use tools unless strictly needed for OCR.\n"
        "- Reply with plain text only.\n"
        "\n"
        f"Images are attached in this order ({len(image_labels)} total):\n"
        f"{image_order}\n"
        "\n"
        "Return one section per image, in order, using headings exactly like:\n"
        f"{heading_examples}"
    )


def build_multimodal_input(prompt: str, pages: list[PageImage]) -> list[dict[str, Any]]:
    input_blocks: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for page in pages:
        input_blocks.append(
            {
                "type": "image",
                "data": base64.b64encode(page.image_bytes).decode("ascii"),
                "mime_type": page.mime_type or "image/png",
            }
        )
    return input_blocks


def _create_interaction(
    api_key: str,
    *,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    response = requests.post(
        INTERACTIONS_BASE_URL,
        headers=_request_headers(api_key),
        json=payload,
        timeout=timeout_seconds,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise AntigravityError("Unexpected non-object Interactions API response")
    return body


def _get_interaction(api_key: str, interaction_id: str) -> dict[str, Any]:
    response = requests.get(
        f"{INTERACTIONS_BASE_URL}/{interaction_id}",
        headers=_request_headers(api_key),
        timeout=60,
    )
    _raise_for_api_error(response)
    body = response.json()
    if not isinstance(body, dict):
        raise AntigravityError("Unexpected non-object Interactions API response")
    return body


def _poll_until_done(
    api_key: str,
    interaction: dict[str, Any],
    *,
    poll_seconds: float,
    timeout_seconds: float,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> dict[str, Any]:
    interaction_id = interaction.get("id")
    if not interaction_id:
        raise AntigravityError("Interaction response missing id")

    deadline = monotonic_fn() + timeout_seconds
    current = interaction
    while current.get("status") == IN_PROGRESS_STATUS:
        if monotonic_fn() >= deadline:
            raise AntigravityError(
                f"Timed out after {timeout_seconds}s waiting for interaction {interaction_id}"
            )
        sleep_fn(poll_seconds)
        try:
            current = _get_interaction(api_key, interaction_id)
        except requests.Timeout as exc:
            logger.warning(
                "Antigravity interaction poll timed out; retrying within overall deadline "
                "interaction_id=%s exception_class=%s",
                interaction_id,
                type(exc).__name__,
            )
            continue
    return current


def transcribe_pages_with_antigravity(
    pages: list[PageImage],
    *,
    api_key: str,
    agent_id: str = DEFAULT_ANTIGRAVITY_AGENT_ID,
    document_id: int | None = None,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    background: bool = True,
    sleep_fn=time.sleep,
    monotonic_fn=time.monotonic,
) -> AntigravityResult:
    if not (api_key or "").strip():
        raise AntigravityError("Missing GEMINI_API_KEY")

    ordered_pages = _sorted_pages(pages)
    if not ordered_pages:
        raise AntigravityError("No page images supplied for Antigravity OCR")

    image_labels = [_page_label(page) for page in ordered_pages]
    prompt = build_antigravity_ocr_prompt(image_labels)
    create_timeout = max(120.0, 30.0 * len(ordered_pages))
    multimodal_input = build_multimodal_input(prompt, ordered_pages)

    block_types = [
        block.get("type")
        for block in multimodal_input
        if isinstance(block, dict)
    ]

    logger.info(
        "Antigravity request payload document_id=%s agent=%s pages=%s "
        "input_blocks=%s image_blocks=%s block_types=%s",
        document_id,
        agent_id,
        len(ordered_pages),
        len(multimodal_input),
        block_types.count("image"),
        block_types,
    )

    interaction = _create_interaction(
        api_key,
        payload={
            "agent": agent_id,
            "input": multimodal_input,
            "environment": "remote",
            "background": background,
        },
        timeout_seconds=create_timeout,
    )

    if background or interaction.get("status") == IN_PROGRESS_STATUS:
        interaction = _poll_until_done(
            api_key,
            interaction,
            poll_seconds=poll_seconds,
            timeout_seconds=timeout_seconds,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
        )

    status = interaction.get("status")
    if status != COMPLETED_STATUS:
        raise AntigravityError(
            f"Antigravity interaction finished with status={status!r}"
        )

    text = output_text_from_steps(interaction.get("steps"))
    if not text:
        raise AntigravityError(
            "Antigravity interaction completed with no OCR output text"
        )

    return AntigravityResult(
        text=normalize_antigravity_image_headings(text),
        engine_name=agent_id,
    )
