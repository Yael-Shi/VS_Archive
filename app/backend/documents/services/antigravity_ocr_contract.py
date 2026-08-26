from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, NoReturn

SCHEMA_VERSION = 1
OUTCOME_TRANSCRIBED = "transcribed"
OUTCOME_BLANK = "blank"
OUTCOME_UNAVAILABLE = "unavailable"
ALLOWED_OUTCOMES = frozenset({OUTCOME_TRANSCRIBED, OUTCOME_BLANK, OUTCOME_UNAVAILABLE})
TOP_LEVEL_KEYS = frozenset({"schema_version", "pages"})
PAGE_ENTRY_KEYS = frozenset({"page_index", "outcome", "text"})
MODEL_OUTPUT_STEP_TYPE = "model_output"
TEXT_CONTENT_TYPE = "text"
TOOL_STEP_TYPES = frozenset(
    {
        "function_call",
        "function_result",
        "code_execution_call",
        "code_execution_result",
        "tool_call",
        "tool_result",
    }
)

REASON_MISSING_MODEL_OUTPUT = "missing_model_output"
REASON_UNEXPECTED_TOOL_USE = "unexpected_tool_use"
REASON_INVALID_JSON = "invalid_json"
REASON_INVALID_CONTRACT = "invalid_contract"
REASON_PAGE_COUNT_MISMATCH = "page_count_mismatch"
REASON_PAGE_INDEX_MISMATCH = "page_index_mismatch"
REASON_EMPTY_TRANSCRIPTION = "empty_transcription"
REASON_INPUT_UNAVAILABLE = "input_unavailable"
REASON_NO_TRANSCRIBED_TEXT = "no_transcribed_text"

PAGE_HEADING_PREFIX = "עמוד"


class OcrContractError(Exception):
    """Structural OCR contract failure. Message must not include provider text."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


@dataclass(frozen=True)
class ValidatedOcrPage:
    page_index: int
    outcome: str
    text: str


@dataclass(frozen=True)
class ValidatedOcrOutput:
    pages: tuple[ValidatedOcrPage, ...]


def _fail(
    reason: str,
    message: str,
    **details: Any,
) -> NoReturn:
    raise OcrContractError(message, reason=reason, details=details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def unexpected_tool_step_types(steps: list[Any] | None) -> list[str]:
    found: list[str] = []
    for step in steps or []:
        if not isinstance(step, dict):
            continue
        step_type = step.get("type")
        if isinstance(step_type, str) and step_type in TOOL_STEP_TYPES:
            found.append(step_type)
    return found


def extract_final_model_output_text(steps: list[Any] | None) -> str | None:
    """Return text from the last model_output step only.

    Text content blocks of that step are concatenated in their existing
    order with no inserted separator. Earlier model_output steps,
    thoughts, and tool output are ignored.
    """
    last_model_step: dict[str, Any] | None = None
    for step in steps or []:
        if isinstance(step, dict) and step.get("type") == MODEL_OUTPUT_STEP_TYPE:
            last_model_step = step
    if last_model_step is None:
        return None

    chunks: list[str] = []
    for content in last_model_step.get("content") or []:
        if not isinstance(content, dict):
            continue
        if content.get("type") != TEXT_CONTENT_TYPE:
            continue
        text = content.get("text")
        if isinstance(text, str) and text:
            chunks.append(text)
    joined = "".join(chunks)
    return joined if joined.strip() else None


def build_antigravity_ocr_prompt(page_count: int) -> str:
    if page_count < 1:
        raise ValueError("page_count must be >= 1")

    if page_count == 1:
        example_pages = '{"page_index": 1, "outcome": "transcribed", "text": "..."}'
    else:
        example_pages = (
            '{"page_index": 1, "outcome": "transcribed", "text": "..."},\n'
            f'    {{"page_index": {page_count}, "outcome": "transcribed", '
            '"text": "..."}'
        )

    image_noun = "image" if page_count == 1 else "images"
    return (
        "You are transcribing historical archive document page images.\n"
        "TASK: Transcribe the inline images only. This job is transcription of "
        "the supplied inline images. Never translate, summarize, explain, "
        "complete missing content, or converse.\n"
        "RULES:\n"
        "- Transcribe visible text only. Do not translate, summarize, explain, "
        "complete, or hold a conversation.\n"
        "- Preserve the original language, spelling, character order, and "
        "meaningful line breaks.\n"
        "- Preserve Arabic, Hebrew, Latin, digits, stamps, and mixed scripts "
        "exactly as written. Do not require any one script to be present.\n"
        "- Include cover/catalog page text and visible handwritten additions.\n"
        "- Prefer [UNCLEAR] over inventing confident text.\n"
        "- Do not look up filenames, search a filesystem, browse the web, run "
        "code, or call tools.\n"
        "- Tools are disabled. Do not attempt tool calls.\n"
        f"- There are exactly {page_count} inline {image_noun} attached, in order.\n"
        "- Return exactly one JSON object and nothing else. No Markdown, no "
        "code fences, no surrounding prose.\n"
        "\n"
        "OUTPUT CONTRACT:\n"
        "Return a single JSON object with exactly these keys:\n"
        "{\n"
        '  "schema_version": 1,\n'
        '  "pages": [\n'
        f"    {example_pages}\n"
        "  ]\n"
        "}\n"
        "\n"
        f"- schema_version must be the integer {SCHEMA_VERSION}.\n"
        f"- pages must contain exactly {page_count} entries, in input-image "
        f"order, with page_index values 1 through {page_count} and no "
        "duplicates or gaps.\n"
        "- Each entry has exactly page_index, outcome, and text.\n"
        "- outcome is one of: transcribed, blank, unavailable.\n"
        "- transcribed: the page has visible text; text must be non-empty.\n"
        "- blank: use only for a genuinely blank page; text must be empty.\n"
        "- unavailable: use only if an image cannot actually be inspected; "
        "text must be empty.\n"
        "- JSON only.\n"
    )


def parse_ocr_contract_json(raw_text: str) -> dict[str, Any]:
    parsed: Any = None
    decode_failed = False
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        decode_failed = True
    if decode_failed:
        _fail(
            REASON_INVALID_JSON,
            "Antigravity OCR validation failed: reason=invalid_json",
        )
    if not isinstance(parsed, dict):
        _fail(
            REASON_INVALID_JSON,
            "Antigravity OCR validation failed: reason=invalid_json",
        )
    return parsed


def _validate_page_entry(
    entry: Any,
    *,
    expected_index: int,
) -> ValidatedOcrPage:
    if not isinstance(entry, dict):
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={expected_index} entry_type=non_object",
            expected_page_index=expected_index,
        )
    if set(entry.keys()) != PAGE_ENTRY_KEYS:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={expected_index} unexpected_or_missing_fields",
            expected_page_index=expected_index,
        )

    page_index = entry.get("page_index")
    outcome = entry.get("outcome")
    text = entry.get("text")

    if not _is_int(page_index):
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={expected_index} page_index_type_invalid",
            expected_page_index=expected_index,
        )
    if not isinstance(outcome, str):
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={expected_index} outcome_type_invalid",
            expected_page_index=expected_index,
        )
    if not isinstance(text, str):
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={expected_index} text_type_invalid",
            expected_page_index=expected_index,
        )
    if outcome not in ALLOWED_OUTCOMES:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={page_index} outcome_invalid",
            page_index=page_index,
        )

    stripped = text.strip()
    if outcome == OUTCOME_TRANSCRIBED and not stripped:
        _fail(
            REASON_EMPTY_TRANSCRIPTION,
            "Antigravity OCR validation failed: reason=empty_transcription "
            f"page_index={page_index}",
            page_index=page_index,
        )
    if outcome == OUTCOME_BLANK and stripped:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={page_index} blank_text_nonempty",
            page_index=page_index,
        )
    if outcome == OUTCOME_UNAVAILABLE and stripped:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            f"page_index={page_index} unavailable_text_nonempty",
            page_index=page_index,
        )
    if outcome == OUTCOME_UNAVAILABLE:
        _fail(
            REASON_INPUT_UNAVAILABLE,
            "Antigravity OCR validation failed: reason=input_unavailable "
            f"page_index={page_index}",
            page_index=page_index,
        )

    return ValidatedOcrPage(page_index=page_index, outcome=outcome, text=text)


def validate_ocr_contract_object(
    payload: dict[str, Any],
    *,
    expected_page_count: int,
) -> ValidatedOcrOutput:
    if set(payload.keys()) != TOP_LEVEL_KEYS:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            "top_level_fields",
        )

    schema_version = payload.get("schema_version")
    if not _is_int(schema_version) or schema_version != SCHEMA_VERSION:
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            "unsupported_schema_version",
        )

    pages = payload.get("pages")
    if not isinstance(pages, list):
        _fail(
            REASON_INVALID_CONTRACT,
            "Antigravity OCR validation failed: reason=invalid_contract "
            "pages_type_invalid",
        )
    actual_count = len(pages)
    if actual_count != expected_page_count:
        _fail(
            REASON_PAGE_COUNT_MISMATCH,
            "Antigravity OCR validation failed: reason=page_count_mismatch "
            f"expected={expected_page_count} actual={actual_count}",
            expected_page_count=expected_page_count,
            actual_page_count=actual_count,
        )

    expected_indexes = list(range(1, expected_page_count + 1))
    actual_indexes: list[Any] = []
    validated: list[ValidatedOcrPage] = []
    for offset, entry in enumerate(pages):
        page = _validate_page_entry(entry, expected_index=offset + 1)
        actual_indexes.append(page.page_index)
        validated.append(page)

    if actual_indexes != expected_indexes:
        _fail(
            REASON_PAGE_INDEX_MISMATCH,
            "Antigravity OCR validation failed: reason=page_index_mismatch "
            f"expected={expected_indexes} actual={actual_indexes}",
            expected_page_indexes=expected_indexes,
            actual_page_indexes=actual_indexes,
        )

    if not any(page.outcome == OUTCOME_TRANSCRIBED for page in validated):
        _fail(
            REASON_NO_TRANSCRIBED_TEXT,
            "Antigravity OCR validation failed: reason=no_transcribed_text "
            f"page_count={expected_page_count}",
            expected_page_count=expected_page_count,
        )

    return ValidatedOcrOutput(pages=tuple(validated))


def validate_antigravity_ocr_output(
    steps: list[Any] | None,
    *,
    expected_page_count: int,
) -> ValidatedOcrOutput:
    tool_types = unexpected_tool_step_types(steps)
    if tool_types:
        unique_types = sorted(set(tool_types))
        _fail(
            REASON_UNEXPECTED_TOOL_USE,
            "Antigravity OCR validation failed: reason=unexpected_tool_use "
            f"step_types={unique_types}",
            step_types=unique_types,
        )

    raw_text = extract_final_model_output_text(steps)
    if raw_text is None:
        _fail(
            REASON_MISSING_MODEL_OUTPUT,
            "Antigravity OCR validation failed: reason=missing_model_output",
        )

    payload = parse_ocr_contract_json(raw_text)
    return validate_ocr_contract_object(
        payload, expected_page_count=expected_page_count
    )


def render_validated_ocr_text(output: ValidatedOcrOutput) -> str:
    pages = output.pages
    if len(pages) == 1:
        return pages[0].text

    sections: list[str] = []
    for page in pages:
        heading = f"{PAGE_HEADING_PREFIX} {page.page_index}"
        if page.outcome == OUTCOME_BLANK:
            sections.append(heading)
            continue
        sections.append(f"{heading}\n{page.text}")
    return "\n\n".join(sections)
