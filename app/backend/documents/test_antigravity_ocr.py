from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from io import BytesIO
from unittest.mock import MagicMock, patch

import requests
from PIL import Image

from django.test import SimpleTestCase, TestCase

from documents.management.commands.run_worker import Command
from documents.models import Document, DocumentTextResult
from documents.services.antigravity_defaults import (
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    DEFAULT_POLL_GET_TIMEOUT_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INTERACTIONS_BASE_URL,
)
from documents.services.antigravity_engine import (
    AntigravityError,
    AntigravityHttpError,
    AntigravityOutputValidationError,
    OUTBOUND_JPEG_MIME,
    antigravity_outbound_image,
    build_antigravity_ocr_prompt,
    build_multimodal_input,
    output_text_from_steps,
    summarize_antigravity_interaction,
    transcribe_pages_with_antigravity,
)
from documents.services.antigravity_ocr_contract import (
    OcrContractError,
    PAGE_HEADING_PREFIX,
    REASON_EMPTY_TRANSCRIPTION,
    REASON_INPUT_UNAVAILABLE,
    REASON_INVALID_CONTRACT,
    REASON_INVALID_JSON,
    REASON_MISSING_MODEL_OUTPUT,
    REASON_NO_TRANSCRIBED_TEXT,
    REASON_PAGE_COUNT_MISMATCH,
    REASON_PAGE_INDEX_MISMATCH,
    REASON_UNEXPECTED_TOOL_USE,
    extract_final_model_output_text,
    render_validated_ocr_text,
    validate_antigravity_ocr_output,
)
from documents.services.archive_items import create_ocr_document
from documents.services.htr_adapters.antigravity_adapter import AntigravityAdapter
from documents.services.htr_adapters.base import EnginePermanentError
from documents.services.ocr_reprocess import (
    OcrRetryMode,
    assess_ocr_reprocess,
    is_ocr_reprocess_ui_eligible,
)
from documents.services.page_extraction import (
    PageImage,
    extract_pages,
    source_file_bytes_to_page,
)


_ENGINE_LOGGER = "documents.services.antigravity_engine"
_ADAPTER_LOGGER = "documents.services.htr_adapters.antigravity_adapter"
_ALLOWLISTED_SUMMARY_KEYS = {
    "interaction_id",
    "status",
    "environment_id",
    "step_types",
    "step_types_total",
    "step_types_truncated",
    "user_input_content_types",
    "user_input_content_types_total",
    "user_input_content_types_truncated",
    "stored_image_content_count",
    "input_tokens_by_modality",
    "input_tokens_by_modality_total",
    "input_tokens_by_modality_truncated",
    "total_input_tokens",
    "total_output_tokens",
    "total_thought_tokens",
    "total_tool_use_tokens",
    "total_tokens",
}
_PROMPT_SENTINEL = "You are transcribing historical archive document page images."
_OCR_SENTINEL = "OCR_OUTPUT_MUST_NOT_APPEAR_IN_LOGS"
_API_KEY_SENTINEL = "secret-api-key-DO-NOT-LOG"
_NESTED_RESPONSE_SENTINEL = "NESTED_RESPONSE_SECRET_MUST_NOT_LOG"
_NESTED_USAGE_SENTINEL = "NESTED_USAGE_SECRET_MUST_NOT_LOG"
_USER_TEXT_SENTINEL = "USER_INPUT_TEXT_MUST_NOT_APPEAR"
_POST_OCR_TEXT = "POST_OCR_TEXT_MUST_SURVIVE"
_POLL_HTTP_BODY_SENTINEL = "504_RESPONSE_BODY_MUST_NOT_LOG"
_DOCUMENT_320_GREETING = "Hello! How can I help you today?"
_NO_IMAGE_FILES_PROSE = "No image files were found to transcribe."


def _make_worker_env(**overrides):
    from documents.services.env_validation import WorkerEnvConfig

    base = {
        "gemini_api_key": "test-api-key",
        "gemini_confidence_threshold": 0.55,
        "min_text_length": 20,
        "max_retries": 3,
        "retry_delay_seconds_1": 30,
        "retry_delay_seconds_2": 300,
        "report_window_start": "00:00",
        "report_send_time": "08:00",
        "free_tier_alert_pct": 80,
        "gemini_free_daily_request_limit": 200,
        "gemini_free_daily_image_limit": 200,
        "transkribus_free_monthly_credits": 500,
        "enable_hybrid_htr": False,
        "enable_daily_report": False,
        "smtp_host": None,
        "smtp_port": None,
        "smtp_username": None,
        "smtp_password": None,
        "default_from_email": None,
        "transkribus_api_token": None,
        "transkribus_username": None,
        "transkribus_password": None,
        "gemini_temperature": 0.2,
        "gemini_top_k": 40,
        "gemini_top_p": 0.95,
        "gemini_max_output_tokens": 8192,
        "gemini_double_pass": False,
        "gemini_consistency_min_ratio": 0.85,
        "enable_antigravity_arabic_printed": True,
        "antigravity_agent_id": DEFAULT_ANTIGRAVITY_AGENT_ID,
    }
    base.update(overrides)
    return WorkerEnvConfig(**base)


def _ocr_contract_json(*page_texts: str, outcomes: list[str] | None = None) -> str:
    pages = []
    for index, text in enumerate(page_texts, start=1):
        if outcomes is None:
            outcome = "blank" if text == "" else "transcribed"
        else:
            outcome = outcomes[index - 1]
        pages.append(
            {
                "page_index": index,
                "outcome": outcome,
                "text": text,
            }
        )
    return json.dumps({"schema_version": 1, "pages": pages}, ensure_ascii=False)


def _completed_interaction(text: str, *, interaction_id: str = "ix-1") -> dict:
    return {
        "id": interaction_id,
        "status": "completed",
        "agent": DEFAULT_ANTIGRAVITY_AGENT_ID,
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def _completed_ocr_interaction(
    *page_texts: str,
    interaction_id: str = "ix-1",
    outcomes: list[str] | None = None,
) -> dict:
    return _completed_interaction(
        _ocr_contract_json(*page_texts, outcomes=outcomes),
        interaction_id=interaction_id,
    )


def _model_output_step(*texts: str) -> dict:
    return {
        "type": "model_output",
        "content": [{"type": "text", "text": text} for text in texts],
    }


def _page_entry(
    page_index: int = 1,
    outcome: str = "transcribed",
    text: str = "visible text",
    extra: dict | None = None,
) -> dict:
    entry = {
        "page_index": page_index,
        "outcome": outcome,
        "text": text,
    }
    if extra:
        entry.update(extra)
    return entry


def _contract_object(
    pages: list[dict],
    *,
    schema_version: object = 1,
    extra: dict | None = None,
) -> dict:
    payload: dict = {"schema_version": schema_version, "pages": pages}
    if extra:
        payload.update(extra)
    return payload


def _steps_for_contract_object(payload: dict) -> list[dict]:
    return [_model_output_step(json.dumps(payload, ensure_ascii=False))]


def _ok_json_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.json.return_value = payload
    return response


def _error_json_response(
    status_code: int,
    message: str,
    *,
    raw_text: str | None = None,
) -> MagicMock:
    response = MagicMock()
    response.ok = False
    response.status_code = status_code
    response.text = message if raw_text is None else raw_text
    response.json.return_value = {"error": {"message": message}}
    return response


def _solid_image_bytes(fmt: str, color=(255, 0, 0), size=(10, 6)) -> bytes:
    image = Image.new("RGB", size, color)
    buf = BytesIO()
    image.save(buf, format=fmt)
    return buf.getvalue()


def _one_page() -> list[PageImage]:
    return [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]


def _five_page_images() -> list[PageImage]:
    raw_pages = [b"p1-raw", b"p2-raw", b"p3-raw", b"p4-raw", b"p5-raw"]
    return [
        PageImage(page_index=5, image_bytes=raw_pages[4], mime_type="image/png"),
        PageImage(page_index=3, image_bytes=raw_pages[2], mime_type="image/png"),
        PageImage(page_index=1, image_bytes=raw_pages[0], mime_type="image/png"),
        PageImage(page_index=4, image_bytes=raw_pages[3], mime_type="image/png"),
        PageImage(page_index=2, image_bytes=raw_pages[1], mime_type="image/png"),
    ]


def _five_image_user_input_interaction(*, ocr_text: str) -> dict:
    image_blocks = [
        {
            "type": "image",
            "data": f"synthetic-image-data-{index}",
            "mime_type": "image/png",
        }
        for index in range(1, 6)
    ]
    return {
        "id": "ix-stored",
        "status": "completed",
        "environment_id": "env-remote-9",
        "unexpected": {"text": _NESTED_RESPONSE_SENTINEL, "data": "nested-data"},
        "steps": [
            {
                "type": "user_input",
                "content": [
                    {"type": "text", "text": _USER_TEXT_SENTINEL},
                    *image_blocks,
                ],
            },
            {
                "type": "model_output",
                "content": [{"type": "text", "text": ocr_text}],
            },
        ],
        "usage": {
            "input_tokens_by_modality": [
                {"modality": "text", "tokens": 11},
                {"modality": "image", "tokens": 55},
            ],
            "total_input_tokens": 66,
            "total_output_tokens": 8,
            "total_thought_tokens": 2,
            "total_tool_use_tokens": 0,
            "total_tokens": 76,
            "unexpected_nested": {"data": _NESTED_USAGE_SENTINEL},
            "prompt_tokens": 999,
        },
    }


def _flatten_summary_values(value):
    if isinstance(value, dict):
        for nested in value.values():
            yield from _flatten_summary_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _flatten_summary_values(nested)
    else:
        yield value


def _messages_for_phase(captured, phase: str) -> list[str]:
    token = f"phase={phase} "
    return [
        record.getMessage()
        for record in captured.records
        if token in record.getMessage()
    ]


def _unexpected_get(*_args, **_kwargs):
    raise AssertionError("unexpected Antigravity GET")


def _reachable_exceptions(exc: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    stack: list[BaseException | None] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        found.append(current)
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return found


def _assert_no_provider_text_on_exception(
    test_case, exc: BaseException, forbidden: str
):
    test_case.assertIsNone(exc.__cause__)
    test_case.assertIsNone(exc.__context__)
    test_case.assertNotIn(forbidden, str(exc))
    test_case.assertNotIn(forbidden, repr(exc))
    for linked in _reachable_exceptions(exc):
        test_case.assertNotIn(forbidden, str(linked))
        test_case.assertNotIn(forbidden, repr(linked))
        test_case.assertNotIn(
            forbidden, json.dumps(getattr(linked, "__dict__", {}), default=str)
        )
        doc = getattr(linked, "doc", None)
        if isinstance(doc, str):
            test_case.assertNotIn(forbidden, doc)


class _MonotonicClock:
    def __init__(self, values=None, start: float = 0.0, step: float = 1.0):
        self._queued = list(values or [])
        self._now = start if not self._queued else self._queued[-1]
        self._step = step

    def __call__(self) -> float:
        if self._queued:
            value = self._queued.pop(0)
            self._now = value
            return value
        self._now += self._step
        return self._now


class AntigravityOcrRenderTests(SimpleTestCase):
    def test_single_transcribed_page_has_no_heading(self):
        output = validate_antigravity_ocr_output(
            [_model_output_step(_ocr_contract_json("Arabic text here"))],
            expected_page_count=1,
        )
        self.assertEqual(render_validated_ocr_text(output), "Arabic text here")

    def test_multi_page_headings_are_deterministic(self):
        output = validate_antigravity_ocr_output(
            [
                _model_output_step(
                    _ocr_contract_json("First page body", "Second page body")
                )
            ],
            expected_page_count=2,
        )
        self.assertEqual(
            render_validated_ocr_text(output),
            f"{PAGE_HEADING_PREFIX} 1\nFirst page body\n\n"
            f"{PAGE_HEADING_PREFIX} 2\nSecond page body",
        )

    def test_blank_pages_keep_heading_without_invented_text(self):
        output = validate_antigravity_ocr_output(
            [
                _model_output_step(
                    _ocr_contract_json(
                        "Visible text",
                        "",
                        "More text",
                        outcomes=["transcribed", "blank", "transcribed"],
                    )
                )
            ],
            expected_page_count=3,
        )
        self.assertEqual(
            render_validated_ocr_text(output),
            f"{PAGE_HEADING_PREFIX} 1\nVisible text\n\n"
            f"{PAGE_HEADING_PREFIX} 2\n\n"
            f"{PAGE_HEADING_PREFIX} 3\nMore text",
        )


class AntigravityEngineTests(SimpleTestCase):
    def test_output_text_from_steps_uses_last_model_output_only(self):
        steps = [
            _model_output_step("earlier draft"),
            {"type": "thought", "content": [{"type": "text", "text": "thinking"}]},
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"},
                ],
            },
        ]
        self.assertEqual(output_text_from_steps(steps), "line oneline two")
        self.assertEqual(extract_final_model_output_text(steps), "line oneline two")

    def test_build_prompt_includes_page_count_and_json_only_rules(self):
        prompt = build_antigravity_ocr_prompt(2)
        self.assertIn("exactly 2 inline images", prompt)
        self.assertIn("Transcribe the inline images only", prompt)
        self.assertIn("Never translate, summarize, explain", prompt)
        self.assertIn("JSON only", prompt)
        self.assertIn('"schema_version": 1', prompt)
        self.assertIn("[UNCLEAR]", prompt)
        self.assertNotIn("unless strictly needed", prompt)
        self.assertNotIn("[IMAGE ", prompt)
        self.assertNotIn("cover.png", prompt)

    def test_build_multimodal_input_orders_text_then_images(self):
        pages = [
            PageImage(page_index=1, image_bytes=b"abc", mime_type="image/png"),
            PageImage(page_index=2, image_bytes=b"def", mime_type="image/jpeg"),
        ]
        blocks = build_multimodal_input("prompt", pages)
        self.assertEqual(blocks[0], {"type": "text", "text": "prompt"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["mime_type"], "image/png")
        self.assertEqual(blocks[2]["mime_type"], "image/jpeg")

    def test_missing_api_key_raises(self):
        with self.assertRaisesMessage(AntigravityError, "Missing GEMINI_API_KEY"):
            transcribe_pages_with_antigravity(_one_page(), api_key="")

    def test_empty_pages_raises(self):
        with self.assertRaisesMessage(AntigravityError, "No page images supplied"):
            transcribe_pages_with_antigravity([], api_key="key")

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_http_failure_raises(self, mock_post, mock_get):
        response = MagicMock()
        response.ok = False
        response.status_code = 403
        response.text = "denied"
        response.json.return_value = {"error": {"message": "permission denied"}}
        mock_post.return_value = response

        with self.assertRaisesMessage(
            AntigravityError, "HTTP 403: permission denied"
        ) as ctx:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )
        self.assertIsInstance(ctx.exception, AntigravityHttpError)
        self.assertEqual(ctx.exception.status_code, 403)
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_completed_without_text_raises(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-empty",
                "status": "completed",
                "steps": [],
            }
        )

        with self.assertRaises(AntigravityOutputValidationError) as ctx:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )
        self.assertEqual(ctx.exception.reason, REASON_MISSING_MODEL_OUTPUT)
        self.assertIsInstance(ctx.exception, AntigravityError)
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_non_completed_status_raises(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-fail", "status": "failed", "steps": []}
        )

        with self.assertRaisesMessage(
            AntigravityError, "finished with status='failed'"
        ):
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )
        mock_get.assert_not_called()

    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_timeout_raises(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-progress", "status": "in_progress"}
        )
        mock_get.return_value = {"id": "ix-progress", "status": "in_progress"}
        clock = _MonotonicClock(values=[0.0, 0.1, 0.2, 301.0])

        with self.assertLogs(_ENGINE_LOGGER, level="WARNING") as captured:
            with self.assertRaisesMessage(AntigravityError, "Timed out after 300.0s"):
                transcribe_pages_with_antigravity(
                    _one_page(),
                    api_key="key",
                    timeout_seconds=300.0,
                    poll_seconds=0.0,
                    monotonic_fn=clock,
                    sleep_fn=lambda _seconds: None,
                )

        mock_get.assert_not_called()
        deadline_logs = _messages_for_phase(captured, "poll_deadline")
        self.assertEqual(len(deadline_logs), 1)
        self.assertIn("interaction_id=ix-progress", deadline_logs[0])
        self.assertIn("last_status=in_progress", deadline_logs[0])
        self.assertIn("elapsed_seconds=300.800", deadline_logs[0])
        self.assertIn("poll_attempts=0", deadline_logs[0])
        self.assertEqual(_messages_for_phase(captured, "poll"), [])

    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_read_timeout_retries_same_interaction_then_succeeds(
        self, mock_post, mock_get
    ):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-progress", "status": "in_progress"}
        )
        mock_get.side_effect = [
            requests.ReadTimeout("temporary poll timeout"),
            _completed_ocr_interaction("Arabic text here"),
        ]
        clock = _MonotonicClock(start=0.0, step=1.0)

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            result = transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                timeout_seconds=300.0,
                poll_seconds=0.0,
                monotonic_fn=clock,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(result.text, "Arabic text here")
        self.assertEqual(mock_get.call_count, 2)
        mock_get.assert_called_with("key", "ix-progress")

        poll_warnings = _messages_for_phase(captured, "poll")
        poll_warnings = [
            message
            for message in poll_warnings
            if "exception_class=ReadTimeout" in message
        ]
        self.assertEqual(len(poll_warnings), 1)
        self.assertIn("attempt=1", poll_warnings[0])
        self.assertIn("elapsed_seconds=2.000", poll_warnings[0])
        self.assertIn("last_status=in_progress", poll_warnings[0])

        poll_summaries = [
            message
            for message in _messages_for_phase(captured, "poll")
            if "poll_attempts=2" in message
        ]
        self.assertEqual(len(poll_summaries), 1)
        self.assertIn("poll_successes=1", poll_summaries[0])
        self.assertIn("poll_transport_timeouts=1", poll_summaries[0])
        self.assertIn("poll_http_retries=0", poll_summaries[0])
        self.assertIn("elapsed_seconds=4.000", poll_summaries[0])
        self.assertIn("status=completed", poll_summaries[0])

    def test_poll_retryable_http_statuses_are_explicit(self):
        from documents.services.antigravity_engine import (
            _POLL_RETRYABLE_HTTP_STATUSES,
        )

        self.assertEqual(
            _POLL_RETRYABLE_HTTP_STATUSES,
            frozenset({408, 429, 500, 502, 503, 504}),
        )
        self.assertNotIn(400, _POLL_RETRYABLE_HTTP_STATUSES)
        self.assertNotIn(403, _POLL_RETRYABLE_HTTP_STATUSES)

    def test_poll_http_retry_delay_is_bounded_exponential(self):
        from documents.services.antigravity_engine import (
            _poll_http_retry_delay_seconds,
        )

        def zero_jitter() -> float:
            return 0.5

        self.assertEqual(_poll_http_retry_delay_seconds(1, random_fn=zero_jitter), 1.0)
        self.assertEqual(_poll_http_retry_delay_seconds(2, random_fn=zero_jitter), 2.0)
        self.assertEqual(_poll_http_retry_delay_seconds(3, random_fn=zero_jitter), 4.0)
        self.assertEqual(_poll_http_retry_delay_seconds(6, random_fn=zero_jitter), 30.0)
        self.assertEqual(
            _poll_http_retry_delay_seconds(20, random_fn=zero_jitter), 30.0
        )

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_get_504_retries_same_interaction_then_succeeds(
        self, mock_post, mock_get
    ):
        interaction_id = "ix-progress"
        mock_post.return_value = _ok_json_response(
            {"id": interaction_id, "status": "in_progress"}
        )
        mock_get.side_effect = [
            _error_json_response(
                504,
                _POLL_HTTP_BODY_SENTINEL,
                raw_text=_POLL_HTTP_BODY_SENTINEL,
            ),
            _ok_json_response(
                _completed_ocr_interaction(
                    "Arabic text here",
                    interaction_id=interaction_id,
                )
            ),
        ]
        slept: list[float] = []
        clock = _MonotonicClock(start=0.0, step=1.0)

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            result = transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                document_id=42,
                timeout_seconds=300.0,
                poll_seconds=5.0,
                monotonic_fn=clock,
                sleep_fn=slept.append,
                random_fn=lambda: 0.5,
            )

        self.assertEqual(result.text, "Arabic text here")
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in mock_get.call_args_list],
            [f"{INTERACTIONS_BASE_URL}/{interaction_id}"] * 2,
        )
        self.assertEqual(slept, [5.0, 1.0])

        retry_logs = _messages_for_phase(captured, "poll_http_retry")
        self.assertEqual(len(retry_logs), 1)
        self.assertIn("document_id=42", retry_logs[0])
        self.assertIn(f"interaction_id={interaction_id}", retry_logs[0])
        self.assertIn("http_status=504", retry_logs[0])
        self.assertIn("retry_count=1", retry_logs[0])
        self.assertIn("delay_seconds=1.000", retry_logs[0])
        self.assertIn("elapsed_seconds=2.000", retry_logs[0])
        for record in captured.records:
            self.assertNotIn(_POLL_HTTP_BODY_SENTINEL, record.getMessage())

        poll_summaries = [
            message
            for message in _messages_for_phase(captured, "poll")
            if "poll_attempts=2" in message
        ]
        self.assertEqual(len(poll_summaries), 1)
        self.assertIn("poll_successes=1", poll_summaries[0])
        self.assertIn("poll_http_retries=1", poll_summaries[0])
        self.assertIn("poll_transport_timeouts=0", poll_summaries[0])

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_get_retryable_http_stops_at_overall_deadline(
        self, mock_post, mock_get
    ):
        interaction_id = "ix-progress"
        mock_post.return_value = _ok_json_response(
            {"id": interaction_id, "status": "in_progress"}
        )
        mock_get.return_value = _error_json_response(
            504,
            _POLL_HTTP_BODY_SENTINEL,
            raw_text=_POLL_HTTP_BODY_SENTINEL,
        )
        clock = _MonotonicClock(start=0.0, step=1.0)

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            with self.assertRaisesMessage(AntigravityError, "Timed out after 5.0s"):
                transcribe_pages_with_antigravity(
                    _one_page(),
                    api_key="key",
                    document_id=42,
                    timeout_seconds=5.0,
                    poll_seconds=0.0,
                    monotonic_fn=clock,
                    sleep_fn=lambda _seconds: None,
                    random_fn=lambda: 0.5,
                )

        self.assertEqual(mock_post.call_count, 1)
        self.assertGreaterEqual(mock_get.call_count, 1)
        self.assertEqual(
            {call.args[0] for call in mock_get.call_args_list},
            {f"{INTERACTIONS_BASE_URL}/{interaction_id}"},
        )
        deadline_logs = _messages_for_phase(captured, "poll_deadline")
        self.assertEqual(len(deadline_logs), 1)
        self.assertIn("document_id=42", deadline_logs[0])
        self.assertIn(f"interaction_id={interaction_id}", deadline_logs[0])
        self.assertIn("poll_http_retries=", deadline_logs[0])
        self.assertGreaterEqual(
            int(deadline_logs[0].split("poll_http_retries=")[1].split()[0]),
            1,
        )
        for record in captured.records:
            self.assertNotIn(_POLL_HTTP_BODY_SENTINEL, record.getMessage())

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_get_400_fails_immediately(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-progress", "status": "in_progress"}
        )
        mock_get.return_value = _error_json_response(400, "bad request")

        with self.assertRaises(AntigravityError) as ctx:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                poll_seconds=0.0,
                sleep_fn=lambda _seconds: None,
            )

        self.assertIsInstance(ctx.exception, AntigravityHttpError)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("HTTP 400: bad request", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_get_403_fails_immediately(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-progress", "status": "in_progress"}
        )
        mock_get.return_value = _error_json_response(403, "permission denied")

        with self.assertRaises(AntigravityError) as ctx:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                poll_seconds=0.0,
                sleep_fn=lambda _seconds: None,
            )

        self.assertIsInstance(ctx.exception, AntigravityHttpError)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("HTTP 403: permission denied", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(mock_get.call_count, 1)

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_create_post_504_is_not_retried(self, mock_post, mock_get):
        mock_post.return_value = _error_json_response(504, "gateway timeout")

        with self.assertRaises(AntigravityError) as ctx:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )

        self.assertIsInstance(ctx.exception, AntigravityHttpError)
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertIn("HTTP 504: gateway timeout", str(ctx.exception))
        self.assertEqual(mock_post.call_count, 1)
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_success_returns_transcription(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            _completed_ocr_interaction("First page", "Second page")
        )

        pages = [
            PageImage(page_index=2, image_bytes=b"b", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"a", mime_type="image/png"),
        ]
        result = transcribe_pages_with_antigravity(
            pages,
            api_key="key",
            background=False,
        )

        self.assertEqual(
            result.text,
            f"{PAGE_HEADING_PREFIX} 1\nFirst page\n\n"
            f"{PAGE_HEADING_PREFIX} 2\nSecond page",
        )
        self.assertEqual(result.engine_name, DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertTrue(result.needs_review)
        mock_get.assert_not_called()

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["agent"], DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertEqual(payload["environment"], "remote")
        self.assertEqual(payload["tool_choice"], "none")
        self.assertNotIn("response_format", payload)
        self.assertNotIn("tools", payload)
        prompt = payload["input"][0]["text"]
        self.assertIn("exactly 2 inline images", prompt)
        self.assertIn("JSON only", prompt)
        self.assertEqual(len(payload["input"]), 3)

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_background_true_completed_without_id_raises(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": _POST_OCR_TEXT}],
                    }
                ],
            }
        )

        with self.assertRaisesMessage(
            AntigravityError, "Interaction response missing id"
        ):
            transcribe_pages_with_antigravity(_one_page(), api_key="key")
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_background_false_completed_without_id_uses_post_text(
        self, mock_post, mock_get
    ):
        mock_post.return_value = _ok_json_response(
            {
                "status": "completed",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [
                            {
                                "type": "text",
                                "text": _ocr_contract_json(_POST_OCR_TEXT),
                            }
                        ],
                    }
                ],
            }
        )

        result = transcribe_pages_with_antigravity(
            _one_page(),
            api_key="key",
            background=False,
        )

        self.assertEqual(result.text, _POST_OCR_TEXT)
        mock_get.assert_not_called()


class AntigravityAdapterTests(SimpleTestCase):
    @patch(
        "documents.services.htr_adapters.antigravity_adapter.transcribe_pages_with_antigravity"
    )
    def test_disabled_feature_flag_raises(self, mock_transcribe):
        adapter = AntigravityAdapter()
        worker_env = _make_worker_env(enable_antigravity_arabic_printed=False)

        with self.assertRaisesMessage(
            EnginePermanentError, "ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true"
        ):
            adapter.execute(
                pages=_one_page(),
                language_hint="ar",
                prompt_variant="printed",
                worker_env=worker_env,
            )

        mock_transcribe.assert_not_called()

    @patch(
        "documents.services.htr_adapters.antigravity_adapter.transcribe_pages_with_antigravity"
    )
    def test_success_maps_to_htr_result(self, mock_transcribe):
        from documents.services.antigravity_engine import AntigravityResult

        mock_transcribe.return_value = AntigravityResult(
            text="transcript",
            engine_name=DEFAULT_ANTIGRAVITY_AGENT_ID,
            needs_review=True,
        )
        adapter = AntigravityAdapter()
        worker_env = _make_worker_env()

        result = adapter.execute(
            pages=_one_page(),
            language_hint="ar",
            prompt_variant="printed",
            worker_env=worker_env,
        )

        self.assertEqual(result.text, "transcript")
        self.assertEqual(result.engine_name, DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertTrue(result.needs_review)
        mock_transcribe.assert_called_once()
        kwargs = mock_transcribe.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "test-api-key")
        self.assertEqual(kwargs["agent_id"], DEFAULT_ANTIGRAVITY_AGENT_ID)

    @patch(
        "documents.services.htr_adapters.antigravity_adapter.transcribe_pages_with_antigravity"
    )
    def test_engine_error_becomes_engine_permanent_error(self, mock_transcribe):
        mock_transcribe.side_effect = AntigravityError("HTTP 500: boom")
        adapter = AntigravityAdapter()

        with self.assertRaisesMessage(EnginePermanentError, "HTTP 500: boom"):
            adapter.execute(
                pages=_one_page(),
                language_hint="ar",
                prompt_variant="printed",
                worker_env=_make_worker_env(),
            )


class AntigravityMultimodalPayloadTests(SimpleTestCase):
    def test_five_pages_encode_one_text_and_five_image_blocks(self):
        pages = _five_page_images()
        ordered = sorted(pages, key=lambda page: page.page_index)
        blocks = build_multimodal_input("prompt", ordered)

        self.assertEqual(len(blocks), 6)
        self.assertEqual(blocks[0], {"type": "text", "text": "prompt"})
        self.assertEqual([block["type"] for block in blocks[1:]], ["image"] * 5)

        for index, (block, page) in enumerate(zip(blocks[1:], ordered), start=1):
            self.assertEqual(page.page_index, index)
            self.assertEqual(block["mime_type"], "image/png")
            self.assertIsInstance(block["data"], str)
            self.assertTrue(block["data"])
            self.assertFalse(block["data"].startswith("data:"))
            self.assertEqual(base64.b64decode(block["data"]), page.image_bytes)

    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_transcribe_posts_sorted_five_image_blocks(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-five",
                "status": "in_progress",
                "environment_id": "env-1",
            }
        )
        mock_get.return_value = _completed_ocr_interaction(
            *([_OCR_SENTINEL] * 5),
            interaction_id="ix-five",
        )
        pages = _five_page_images()
        ordered = sorted(pages, key=lambda page: page.page_index)

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            transcribe_pages_with_antigravity(
                pages,
                api_key="key",
                document_id=320,
                poll_seconds=0.0,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(mock_get.call_count, 1)
        mock_get.assert_called_once_with("key", "ix-five")

        payload = mock_post.call_args.kwargs["json"]
        blocks = payload["input"]
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(len(blocks), 6)
        for block, page in zip(blocks[1:], ordered):
            self.assertEqual(block["type"], "image")
            self.assertEqual(block["mime_type"], "image/png")
            self.assertIsInstance(block["data"], str)
            self.assertTrue(block["data"])
            self.assertFalse(block["data"].startswith("data:"))
            self.assertEqual(base64.b64decode(block["data"]), page.image_bytes)

        image_block_logs = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity image block ")
        ]
        self.assertEqual(len(image_block_logs), 5)
        for page, message in zip(ordered, image_block_logs):
            encoded = base64.b64encode(page.image_bytes).decode("ascii")
            outbound_sha = hashlib.sha256(page.image_bytes).hexdigest()[:16]
            self.assertIn("document_id=320", message)
            self.assertIn(f"page_index={page.page_index}", message)
            self.assertIn("outbound_mime_type=image/png", message)
            self.assertIn(f"outbound_byte_length={len(page.image_bytes)}", message)
            self.assertIn(f"outbound_sha256={outbound_sha}", message)
            self.assertIn(f"base64_character_length={len(encoded)}", message)
            self.assertIn("data_is_str=True", message)
            self.assertIn("data_nonempty=True", message)
            self.assertIn("starts_with_data_url_prefix=False", message)
            self.assertNotIn(encoded, message)
            self.assertNotIn("raw_byte_length=", message)


class AntigravityInteractionSummaryTests(SimpleTestCase):
    def test_five_images_and_documented_tokens_schema(self):
        interaction = _five_image_user_input_interaction(ocr_text=_OCR_SENTINEL)
        summary = summarize_antigravity_interaction(interaction)

        self.assertEqual(set(summary), _ALLOWLISTED_SUMMARY_KEYS)
        self.assertEqual(summary["interaction_id"], "ix-stored")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["environment_id"], "env-remote-9")
        self.assertEqual(summary["step_types"], ["user_input", "model_output"])
        self.assertEqual(summary["step_types_total"], 2)
        self.assertFalse(summary["step_types_truncated"])
        self.assertEqual(
            summary["user_input_content_types"],
            ["text", "image", "image", "image", "image", "image"],
        )
        self.assertEqual(summary["stored_image_content_count"], 5)
        self.assertEqual(
            summary["input_tokens_by_modality"],
            [
                {"modality": "text", "tokens": 11},
                {"modality": "image", "tokens": 55},
            ],
        )
        self.assertEqual(summary["total_input_tokens"], 66)
        self.assertEqual(summary["total_output_tokens"], 8)
        self.assertEqual(summary["total_thought_tokens"], 2)
        self.assertEqual(summary["total_tool_use_tokens"], 0)
        self.assertEqual(summary["total_tokens"], 76)
        self.assertNotIn("prompt_tokens", summary)
        self.assertNotIn("token_count", summary)
        self.assertNotIn("unexpected", summary)

        flattened = " ".join(str(value) for value in _flatten_summary_values(summary))
        self.assertNotIn(_OCR_SENTINEL, flattened)
        self.assertNotIn(_USER_TEXT_SENTINEL, flattened)
        self.assertNotIn(_NESTED_RESPONSE_SENTINEL, flattened)
        self.assertNotIn(_NESTED_USAGE_SENTINEL, flattened)
        self.assertNotIn("synthetic-image-data-1", flattened)

    def test_documented_tokens_key_is_used_not_token_count_alias(self):
        summary = summarize_antigravity_interaction(
            {
                "id": "ix-tokens",
                "status": "completed",
                "usage": {
                    "input_tokens_by_modality": [
                        {"modality": "image", "tokens": 55, "token_count": 99},
                        {"modality": "text", "token_count": 11},
                    ]
                },
            }
        )
        self.assertEqual(
            summary["input_tokens_by_modality"],
            [{"modality": "image", "tokens": 55}],
        )

    def test_malformed_steps_content_and_usage_do_not_raise(self):
        cases = (
            None,
            [],
            "not-an-object",
            {"steps": None, "usage": None},
            {"steps": "not-a-list", "usage": "not-a-dict"},
            {
                "id": {"nested": _NESTED_RESPONSE_SENTINEL},
                "status": ["completed"],
                "steps": [
                    None,
                    {"type": 123, "content": "x"},
                    {
                        "type": "user_input",
                        "content": [
                            {"type": "image", "data": "SECRET_IMAGE_DATA"},
                            None,
                            "str",
                            {"type": {"nested": True}},
                        ],
                    },
                ],
                "usage": {
                    "input_tokens_by_modality": {
                        "image": 9,
                        1: 2,
                        "text": "bad",
                    },
                    "total_input_tokens": "nope",
                    "mystery": {"data": _NESTED_USAGE_SENTINEL},
                },
            },
        )
        for case in cases:
            summary = summarize_antigravity_interaction(case)
            self.assertEqual(set(summary), _ALLOWLISTED_SUMMARY_KEYS)
            flattened = " ".join(
                str(value) for value in _flatten_summary_values(summary)
            )
            self.assertNotIn(_NESTED_RESPONSE_SENTINEL, flattened)
            self.assertNotIn(_NESTED_USAGE_SENTINEL, flattened)
            self.assertNotIn("SECRET_IMAGE_DATA", flattened)
        dict_usage = summarize_antigravity_interaction(cases[-1])
        self.assertEqual(dict_usage["input_tokens_by_modality"], [])
        self.assertEqual(dict_usage["stored_image_content_count"], 1)

    def test_valid_interaction_id_is_preserved_intact(self):
        official_id = (
            "v1_ChdPU0F4YWFtNkFwS2kxZThQZ05lbXdROBIXT1NBeGFhbTZBcEtpMWU4UGdOZW13UTg"
        )
        summary = summarize_antigravity_interaction(
            {"id": "ix-abc_123:ok.-9", "status": "completed"}
        )
        self.assertEqual(summary["interaction_id"], "ix-abc_123:ok.-9")
        official = summarize_antigravity_interaction(
            {"id": official_id, "status": "completed"}
        )
        self.assertEqual(official["interaction_id"], official_id)

    def test_control_characters_and_newlines_are_not_logged(self):
        summary = summarize_antigravity_interaction(
            {
                "id": "ix-1\nstatus=forged",
                "status": "completed\n",
                "environment_id": "env-\x00secret",
            }
        )
        self.assertEqual(summary["interaction_id"], "invalid")
        self.assertEqual(summary["status"], "other")
        self.assertEqual(summary["environment_id"], "invalid")
        flattened = " ".join(str(value) for value in _flatten_summary_values(summary))
        self.assertNotIn("status=forged", flattened)
        self.assertNotIn("\n", flattened)
        self.assertNotIn("\x00", flattened)

    def test_excessively_long_values_and_type_lists_are_bounded(self):
        long_env = "e" * 200
        two_hundred_id = "a" * 200
        over_limit_id = "a" * 513
        steps = [{"type": "model_output"} for _ in range(40)]
        steps[0] = {"type": "unexpected_raw_type"}
        summary = summarize_antigravity_interaction(
            {
                "id": two_hundred_id,
                "status": "completed",
                "environment_id": long_env,
                "steps": steps,
            }
        )
        self.assertEqual(summary["interaction_id"], two_hundred_id)
        self.assertEqual(summary["environment_id"], "invalid")
        self.assertEqual(len(summary["step_types"]), 32)
        self.assertEqual(summary["step_types_total"], 40)
        self.assertTrue(summary["step_types_truncated"])
        self.assertEqual(summary["step_types"][0], "other")
        self.assertNotIn("unexpected_raw_type", summary["step_types"])
        over_limit = summarize_antigravity_interaction(
            {"id": over_limit_id, "status": "completed"}
        )
        self.assertEqual(over_limit["interaction_id"], "invalid")

    def test_unexpected_modality_is_marked_other(self):
        summary = summarize_antigravity_interaction(
            {
                "id": "ix-mod",
                "status": "completed",
                "usage": {
                    "input_tokens_by_modality": [
                        {"modality": "speech", "tokens": 7},
                        {"modality": "image", "tokens": 0},
                    ]
                },
            }
        )
        self.assertEqual(
            summary["input_tokens_by_modality"],
            [
                {"modality": "other", "tokens": 7},
                {"modality": "image", "tokens": 0},
            ],
        )


class AntigravityLifecycleObservabilityTests(SimpleTestCase):
    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_successful_create_logs_phase_id_status_and_environment(
        self, mock_post, mock_get
    ):
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-create",
                "status": "in_progress",
                "environment_id": "env-remote-1",
            }
        )
        mock_get.return_value = _completed_ocr_interaction(
            _OCR_SENTINEL,
            interaction_id="ix-create",
        )
        clock = _MonotonicClock(start=10.0, step=0.5)

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                document_id=320,
                poll_seconds=0.0,
                monotonic_fn=clock,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(mock_get.call_count, 1)
        create_outcome = [
            message
            for message in _messages_for_phase(captured, "create")
            if "elapsed_seconds=0.500" in message
        ]
        self.assertEqual(len(create_outcome), 1)
        self.assertIn("document_id=320", create_outcome[0])
        self.assertIn("interaction_id=ix-create", create_outcome[0])
        self.assertIn("status=in_progress", create_outcome[0])
        self.assertIn("environment_id=env-remote-1", create_outcome[0])

        request_logs = [
            message
            for message in _messages_for_phase(captured, "create")
            if "create_timeout_seconds=120.0" in message
        ]
        self.assertEqual(len(request_logs), 1)

        summaries = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity interaction summary ")
        ]
        self.assertEqual(len(summaries), 1)
        self.assertIn("response_source=poll_get", summaries[0])

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_create_read_timeout_is_identified_as_phase_create(
        self, mock_post, mock_get
    ):
        mock_post.side_effect = requests.ReadTimeout("create timeout body MUST-NOT-LOG")
        clock = _MonotonicClock(start=0.0, step=2.5)

        with self.assertLogs(_ENGINE_LOGGER, level="WARNING") as captured:
            with self.assertRaises(requests.ReadTimeout):
                transcribe_pages_with_antigravity(
                    _one_page(),
                    api_key="key",
                    document_id=320,
                    background=False,
                    monotonic_fn=clock,
                )

        mock_get.assert_not_called()
        create_logs = _messages_for_phase(captured, "create")
        self.assertEqual(len(create_logs), 1)
        self.assertIn("exception_class=ReadTimeout", create_logs[0])
        self.assertIn("document_id=320", create_logs[0])
        self.assertIn("pages=1", create_logs[0])
        self.assertIn("interaction_id_available=false", create_logs[0])
        self.assertIn("elapsed_seconds=2.500", create_logs[0])
        self.assertNotIn("MUST-NOT-LOG", create_logs[0])

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_immediate_completed_post_performs_zero_gets(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            _completed_ocr_interaction(_POST_OCR_TEXT, interaction_id="ix-done")
        )

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            result = transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
            )

        self.assertEqual(result.text, _POST_OCR_TEXT)
        mock_get.assert_not_called()
        summaries = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity interaction summary ")
        ]
        self.assertEqual(len(summaries), 1)
        self.assertIn("response_source=create", summaries[0])
        self.assertNotIn(
            "phase=final_get",
            "\n".join(record.getMessage() for record in captured.records),
        )


class AntigravityObservabilityPrivacyTests(SimpleTestCase):
    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_lifecycle_logs_omit_payload_content_and_nested_fields(
        self, mock_post, mock_get
    ):
        pages = _five_page_images()
        ordered = sorted(pages, key=lambda page: page.page_index)
        encoded = [
            base64.b64encode(page.image_bytes).decode("ascii") for page in ordered
        ]
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-private",
                "status": "in_progress",
                "environment_id": "env-private",
            }
        )
        mock_get.return_value = _five_image_user_input_interaction(
            ocr_text=_ocr_contract_json(*([_OCR_SENTINEL] * 5))
        )

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            transcribe_pages_with_antigravity(
                pages,
                api_key=_API_KEY_SENTINEL,
                document_id=320,
                poll_seconds=0.0,
                sleep_fn=lambda _seconds: None,
            )

        self.assertEqual(mock_get.call_count, 1)
        mock_get.assert_called_once_with(_API_KEY_SENTINEL, "ix-private")
        summaries = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity interaction summary ")
        ]
        self.assertEqual(len(summaries), 1)
        self.assertIn("response_source=poll_get", summaries[0])
        self.assertIn("stored_image_content_count=5", summaries[0])
        self.assertIn("{'modality': 'image', 'tokens': 55}", summaries[0])

        logs = "\n".join(record.getMessage() for record in captured.records)
        forbidden = [
            _PROMPT_SENTINEL,
            _OCR_SENTINEL,
            _API_KEY_SENTINEL,
            _NESTED_RESPONSE_SENTINEL,
            _NESTED_USAGE_SENTINEL,
            _USER_TEXT_SENTINEL,
            *encoded,
            "p1-raw",
            "synthetic-image-data-1",
        ]
        for sentinel in forbidden:
            self.assertNotIn(sentinel, logs)


class AntigravityOutboundJpegPayloadTests(SimpleTestCase):
    def test_original_jpeg_is_sent_exactly_and_png_stays_on_pageimage(self):
        jpeg = _solid_image_bytes("JPEG")
        page = source_file_bytes_to_page(0, jpeg, "image/jpeg")
        self.assertEqual(page.mime_type, "image/png")
        self.assertTrue(page.image_bytes.startswith(b"\x89PNG"))
        self.assertEqual(page.original_image_bytes, jpeg)
        self.assertNotEqual(page.image_bytes, jpeg)

        outbound_bytes, outbound_mime = antigravity_outbound_image(page)
        self.assertEqual(outbound_bytes, jpeg)
        self.assertEqual(outbound_mime, OUTBOUND_JPEG_MIME)

        blocks = build_multimodal_input("prompt", [page])
        self.assertEqual(blocks[0], {"type": "text", "text": "prompt"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["mime_type"], OUTBOUND_JPEG_MIME)
        self.assertIsInstance(blocks[1]["data"], str)
        self.assertTrue(blocks[1]["data"])
        self.assertFalse(blocks[1]["data"].startswith("data:"))
        decoded = base64.b64decode(blocks[1]["data"])
        self.assertEqual(decoded, jpeg)
        self.assertNotEqual(decoded, page.image_bytes)
        self.assertTrue(decoded.startswith(b"\xff\xd8"))

    def test_image_jpg_alias_is_canonicalized_to_image_jpeg(self):
        jpeg = _solid_image_bytes("JPEG", color=(10, 20, 30))
        page = extract_pages(jpeg, "image/jpg")[0]
        blocks = build_multimodal_input("prompt", [page])
        self.assertEqual(blocks[1]["mime_type"], OUTBOUND_JPEG_MIME)
        self.assertEqual(base64.b64decode(blocks[1]["data"]), jpeg)

    def test_png_gif_and_pdf_fall_back_to_normalized_png(self):
        import fitz

        png = _solid_image_bytes("PNG", color=(0, 255, 0))
        gif = _solid_image_bytes("GIF", color=(0, 0, 255))
        pdf = fitz.open()
        pdf.new_page(width=40, height=40)
        pdf_bytes = pdf.tobytes()

        png_page = extract_pages(png, "image/png")[0]
        gif_page = source_file_bytes_to_page(0, gif, "image/gif")
        pdf_page = extract_pages(pdf_bytes, "application/pdf")[0]

        for page in (png_page, gif_page, pdf_page):
            blocks = build_multimodal_input("prompt", [page])
            self.assertEqual(blocks[1]["mime_type"], "image/png")
            decoded = base64.b64decode(blocks[1]["data"])
            self.assertEqual(decoded, page.image_bytes)
            self.assertTrue(decoded.startswith(b"\x89PNG"))

        gif_decoded = base64.b64decode(
            build_multimodal_input("prompt", [gif_page])[1]["data"]
        )
        self.assertNotEqual(gif_decoded, gif)
        self.assertIsNone(pdf_page.original_image_bytes)
        self.assertIsNone(pdf_page.original_mime_type)

    def test_mixed_pages_keep_order_and_per_page_outbound_choice(self):
        jpeg_a = _solid_image_bytes("JPEG", color=(255, 0, 0))
        jpeg_b = _solid_image_bytes("JPEG", color=(0, 255, 0))
        png = _solid_image_bytes("PNG", color=(0, 0, 255))
        pages = [
            source_file_bytes_to_page(2, jpeg_b, "image/jpeg"),
            source_file_bytes_to_page(0, png, "image/png"),
            source_file_bytes_to_page(1, jpeg_a, "image/jpg"),
        ]
        ordered = sorted(pages, key=lambda page: page.page_index)
        blocks = build_multimodal_input("prompt", ordered)

        self.assertEqual([page.page_index for page in ordered], [1, 2, 3])
        self.assertEqual(len(blocks), 4)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual([block["type"] for block in blocks[1:]], ["image"] * 3)

        decoded = [base64.b64decode(block["data"]) for block in blocks[1:]]
        self.assertEqual(blocks[1]["mime_type"], "image/png")
        self.assertEqual(decoded[0], ordered[0].image_bytes)
        self.assertTrue(decoded[0].startswith(b"\x89PNG"))

        self.assertEqual(blocks[2]["mime_type"], OUTBOUND_JPEG_MIME)
        self.assertEqual(decoded[1], jpeg_a)
        self.assertNotEqual(decoded[1], ordered[1].image_bytes)

        self.assertEqual(blocks[3]["mime_type"], OUTBOUND_JPEG_MIME)
        self.assertEqual(decoded[2], jpeg_b)
        self.assertFalse(any(block["data"].startswith("data:") for block in blocks[1:]))

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_transcribe_posts_original_jpeg_and_logs_outbound_bytes(
        self, mock_post, mock_get
    ):
        jpeg = _solid_image_bytes("JPEG")
        page = source_file_bytes_to_page(0, jpeg, "image/jpeg")
        encoded_jpeg = base64.b64encode(jpeg).decode("ascii")
        encoded_png = base64.b64encode(page.image_bytes).decode("ascii")
        mock_post.return_value = _ok_json_response(
            _completed_ocr_interaction(
                "Arabic text here",
                interaction_id="ix-jpeg",
            )
        )

        with self.assertLogs(_ENGINE_LOGGER, level="INFO") as captured:
            result = transcribe_pages_with_antigravity(
                [page],
                api_key="key",
                document_id=321,
                background=False,
            )

        self.assertEqual(result.text, "Arabic text here")
        mock_get.assert_not_called()
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["input"][1]["mime_type"], OUTBOUND_JPEG_MIME)
        self.assertEqual(base64.b64decode(payload["input"][1]["data"]), jpeg)
        self.assertIn("exactly 1 inline image", payload["input"][0]["text"])

        image_block_logs = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity image block ")
        ]
        self.assertEqual(len(image_block_logs), 1)
        message = image_block_logs[0]
        outbound_sha = hashlib.sha256(jpeg).hexdigest()[:16]
        self.assertIn("document_id=321", message)
        self.assertIn("outbound_mime_type=image/jpeg", message)
        self.assertIn(f"outbound_byte_length={len(jpeg)}", message)
        self.assertIn(f"outbound_sha256={outbound_sha}", message)
        self.assertIn(f"base64_character_length={len(encoded_jpeg)}", message)
        self.assertNotIn(encoded_jpeg, message)
        self.assertNotIn(encoded_png, message)
        png_sha = hashlib.sha256(page.image_bytes).hexdigest()[:16]
        self.assertNotEqual(outbound_sha, png_sha)


class AntigravityAdapterOutboundLoggingTests(SimpleTestCase):
    @patch(
        "documents.services.htr_adapters.antigravity_adapter.transcribe_pages_with_antigravity"
    )
    def test_adapter_logs_outbound_jpeg_not_normalized_png(self, mock_transcribe):
        from documents.services.antigravity_engine import AntigravityResult

        jpeg = _solid_image_bytes("JPEG")
        page = source_file_bytes_to_page(0, jpeg, "image/jpeg")
        mock_transcribe.return_value = AntigravityResult(
            text="transcript",
            engine_name=DEFAULT_ANTIGRAVITY_AGENT_ID,
            needs_review=True,
        )
        encoded_jpeg = base64.b64encode(jpeg).decode("ascii")

        with self.assertLogs(_ADAPTER_LOGGER, level="INFO") as captured:
            AntigravityAdapter().execute(
                pages=[page],
                language_hint="ar",
                prompt_variant="printed",
                worker_env=_make_worker_env(),
                document_id=321,
            )

        page_logs = [
            record.getMessage()
            for record in captured.records
            if record.getMessage().startswith("Antigravity input page ")
        ]
        self.assertEqual(len(page_logs), 1)
        message = page_logs[0]
        outbound_sha = hashlib.sha256(jpeg).hexdigest()[:16]
        png_sha = hashlib.sha256(page.image_bytes).hexdigest()[:16]
        self.assertIn("outbound_mime_type=image/jpeg", message)
        self.assertIn(f"outbound_byte_length={len(jpeg)}", message)
        self.assertIn(f"outbound_sha256={outbound_sha}", message)
        self.assertNotIn(encoded_jpeg, message)
        self.assertNotIn(f"outbound_sha256={png_sha}", message)
        self.assertNotIn("mime_type=image/png", message)


class AntigravityTimeoutDefaultTests(SimpleTestCase):
    def test_defaults_are_1200s_and_120s_finite_and_below_lease(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 1200.0)
        self.assertEqual(DEFAULT_POLL_GET_TIMEOUT_SECONDS, 120.0)
        self.assertEqual(DEFAULT_POLL_SECONDS, 5.0)
        self.assertTrue(math.isfinite(DEFAULT_TIMEOUT_SECONDS))
        self.assertTrue(math.isfinite(DEFAULT_POLL_GET_TIMEOUT_SECONDS))
        self.assertGreater(DEFAULT_TIMEOUT_SECONDS, 0)
        self.assertGreater(DEFAULT_POLL_GET_TIMEOUT_SECONDS, 0)
        self.assertLess(DEFAULT_TIMEOUT_SECONDS, 45 * 60)
        self.assertLess(DEFAULT_POLL_GET_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)

    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_default_poll_deadline_raises_after_1200s(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-progress", "status": "in_progress"}
        )
        mock_get.return_value = {"id": "ix-progress", "status": "in_progress"}
        clock = _MonotonicClock(values=[0.0, 0.1, 0.2, 1200.3])

        with self.assertLogs(_ENGINE_LOGGER, level="WARNING") as captured:
            with self.assertRaisesMessage(AntigravityError, "Timed out after 1200.0s"):
                transcribe_pages_with_antigravity(
                    _one_page(),
                    api_key="key",
                    poll_seconds=0.0,
                    monotonic_fn=clock,
                    sleep_fn=lambda _seconds: None,
                )

        mock_get.assert_not_called()
        deadline_logs = _messages_for_phase(captured, "poll_deadline")
        self.assertEqual(len(deadline_logs), 1)
        self.assertIn("interaction_id=ix-progress", deadline_logs[0])
        self.assertIn("last_status=in_progress", deadline_logs[0])
        self.assertIn("elapsed_seconds=1200.100", deadline_logs[0])
        self.assertIn("poll_attempts=0", deadline_logs[0])

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_get_uses_120_second_timeout(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {"id": "ix-timeout", "status": "in_progress"}
        )
        mock_get.return_value = _ok_json_response(
            _completed_ocr_interaction(
                "Arabic text here",
                interaction_id="ix-timeout",
            )
        )

        result = transcribe_pages_with_antigravity(
            _one_page(),
            api_key="key",
            poll_seconds=0.0,
            sleep_fn=lambda _seconds: None,
        )

        self.assertEqual(result.text, "Arabic text here")
        self.assertEqual(mock_get.call_count, 1)
        self.assertEqual(
            mock_get.call_args.kwargs["timeout"],
            DEFAULT_POLL_GET_TIMEOUT_SECONDS,
        )
        self.assertEqual(DEFAULT_POLL_GET_TIMEOUT_SECONDS, 120.0)


class AntigravityCreatePayloadContractTests(SimpleTestCase):
    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_create_payload_disables_tools_without_structured_output(
        self, mock_post, mock_get
    ):
        mock_post.return_value = _ok_json_response(
            _completed_ocr_interaction("Arabic text here")
        )

        transcribe_pages_with_antigravity(
            _one_page(),
            api_key="key",
            background=False,
        )

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["tool_choice"], "none")
        self.assertNotIn("response_format", payload)
        self.assertNotIn("tools", payload)
        prompt = payload["input"][0]["text"]
        self.assertIn("exactly 1 inline image", prompt)
        self.assertIn("JSON only", prompt)
        self.assertIn("Tools are disabled", prompt)
        self.assertNotIn("unless strictly needed", prompt)
        mock_get.assert_not_called()


class AntigravityOcrContractValidationTests(SimpleTestCase):
    def _assert_reason(
        self,
        steps,
        reason: str,
        *,
        expected_page_count: int = 1,
        forbidden: str | None = None,
    ) -> OcrContractError:
        with self.assertRaises(OcrContractError) as ctx:
            validate_antigravity_ocr_output(
                steps, expected_page_count=expected_page_count
            )
        self.assertEqual(ctx.exception.reason, reason)
        message = str(ctx.exception)
        self.assertIn(f"reason={reason}", message)
        if forbidden is not None:
            self.assertNotIn(forbidden, message)
            flattened = json.dumps(ctx.exception.details, default=str)
            self.assertNotIn(forbidden, flattened)
        return ctx.exception

    def test_valid_one_page_response(self):
        output = validate_antigravity_ocr_output(
            [_model_output_step(_ocr_contract_json("Line one\nLine two"))],
            expected_page_count=1,
        )
        self.assertEqual(render_validated_ocr_text(output), "Line one\nLine two")

    def test_valid_multi_page_response_renders_deterministically(self):
        output = validate_antigravity_ocr_output(
            [_model_output_step(_ocr_contract_json("Alpha", "Beta"))],
            expected_page_count=2,
        )
        self.assertEqual(
            render_validated_ocr_text(output),
            f"{PAGE_HEADING_PREFIX} 1\nAlpha\n\n{PAGE_HEADING_PREFIX} 2\nBeta",
        )

    def test_mixed_transcribed_and_blank_pages(self):
        output = validate_antigravity_ocr_output(
            [
                _model_output_step(
                    _ocr_contract_json(
                        "Kept",
                        "",
                        outcomes=["transcribed", "blank"],
                    )
                )
            ],
            expected_page_count=2,
        )
        self.assertEqual(
            render_validated_ocr_text(output),
            f"{PAGE_HEADING_PREFIX} 1\nKept\n\n{PAGE_HEADING_PREFIX} 2",
        )

    def test_final_model_output_is_selected_without_earlier_output(self):
        steps = [
            _model_output_step(_ocr_contract_json("DRAFT_MUST_NOT_APPEAR")),
            {"type": "thought", "content": [{"type": "text", "text": "plan"}]},
            _model_output_step(_ocr_contract_json("Final transcription")),
        ]
        output = validate_antigravity_ocr_output(steps, expected_page_count=1)
        self.assertEqual(render_validated_ocr_text(output), "Final transcription")

    def test_document_320_greeting_is_rejected_as_non_contract(self):
        self._assert_reason(
            [_model_output_step(_DOCUMENT_320_GREETING)],
            REASON_INVALID_JSON,
            forbidden=_DOCUMENT_320_GREETING,
        )

    def test_historical_no_image_files_prose_is_rejected_as_non_contract(self):
        self._assert_reason(
            [_model_output_step(_NO_IMAGE_FILES_PROSE)],
            REASON_INVALID_JSON,
            forbidden=_NO_IMAGE_FILES_PROSE,
        )

    def test_markdown_fenced_json_is_rejected(self):
        fenced = f"```json\n{_ocr_contract_json('visible text')}\n```"
        self._assert_reason(
            [_model_output_step(fenced)],
            REASON_INVALID_JSON,
            forbidden="visible text",
        )

    def test_json_with_surrounding_prose_is_rejected(self):
        prose = f"Here is the OCR:\n{_ocr_contract_json('visible text')}"
        self._assert_reason(
            [_model_output_step(prose)],
            REASON_INVALID_JSON,
            forbidden="visible text",
        )

    def test_invalid_json_is_rejected(self):
        self._assert_reason(
            [_model_output_step("{not json")],
            REASON_INVALID_JSON,
            forbidden="{not json",
        )

    def test_invalid_json_exception_does_not_retain_provider_text(self):
        forbidden = _DOCUMENT_320_GREETING
        exc = self._assert_reason(
            [_model_output_step(forbidden)],
            REASON_INVALID_JSON,
            forbidden=forbidden,
        )
        _assert_no_provider_text_on_exception(self, exc, forbidden)

    def test_missing_page_count_mismatch(self):
        exc = self._assert_reason(
            [_model_output_step(_ocr_contract_json("only one"))],
            REASON_PAGE_COUNT_MISMATCH,
            expected_page_count=2,
        )
        self.assertEqual(exc.details["expected_page_count"], 2)
        self.assertEqual(exc.details["actual_page_count"], 1)

    def test_extra_page_count_mismatch(self):
        self._assert_reason(
            [_model_output_step(_ocr_contract_json("one", "two"))],
            REASON_PAGE_COUNT_MISMATCH,
            expected_page_count=1,
        )

    def test_duplicated_page_indexes(self):
        steps = _steps_for_contract_object(
            _contract_object(
                [
                    _page_entry(1, text="a"),
                    _page_entry(1, text="b"),
                ]
            )
        )
        exc = self._assert_reason(
            steps, REASON_PAGE_INDEX_MISMATCH, expected_page_count=2
        )
        self.assertEqual(exc.details["expected_page_indexes"], [1, 2])
        self.assertEqual(exc.details["actual_page_indexes"], [1, 1])

    def test_unordered_page_indexes(self):
        steps = _steps_for_contract_object(
            _contract_object(
                [
                    _page_entry(2, text="second"),
                    _page_entry(1, text="first"),
                ]
            )
        )
        self._assert_reason(steps, REASON_PAGE_INDEX_MISMATCH, expected_page_count=2)

    def test_out_of_range_page_index(self):
        steps = _steps_for_contract_object(
            _contract_object(
                [
                    _page_entry(1, text="first"),
                    _page_entry(3, text="third"),
                ]
            )
        )
        self._assert_reason(steps, REASON_PAGE_INDEX_MISMATCH, expected_page_count=2)

    def test_wrong_field_types_extra_fields_schema_and_outcome(self):
        cases = (
            _contract_object([_page_entry()], schema_version="1"),
            _contract_object([_page_entry()], schema_version=True),
            _contract_object([_page_entry()], schema_version=2),
            _contract_object([_page_entry()], extra={"notes": "nope"}),
            _contract_object([_page_entry(extra={"confidence": 0.9})]),
            _contract_object([_page_entry(outcome="partial")]),
            _contract_object([_page_entry(page_index="1")]),
            _contract_object([{"page_index": 1, "outcome": "transcribed"}]),
            _contract_object([_page_entry(text=None)]),
        )
        for payload in cases:
            with self.subTest(payload=payload):
                self._assert_reason(
                    _steps_for_contract_object(payload),
                    REASON_INVALID_CONTRACT,
                )

    def test_empty_transcribed_is_rejected(self):
        self._assert_reason(
            _steps_for_contract_object(
                _contract_object([_page_entry(outcome="transcribed", text="   ")])
            ),
            REASON_EMPTY_TRANSCRIPTION,
        )

    def test_nonempty_blank_is_rejected(self):
        self._assert_reason(
            _steps_for_contract_object(
                _contract_object([_page_entry(outcome="blank", text="should be empty")])
            ),
            REASON_INVALID_CONTRACT,
            forbidden="should be empty",
        )

    def test_unavailable_page_invalidates_entire_result(self):
        steps = _steps_for_contract_object(
            _contract_object(
                [
                    _page_entry(1, text="ok"),
                    _page_entry(2, outcome="unavailable", text=""),
                ]
            )
        )
        exc = self._assert_reason(
            steps, REASON_INPUT_UNAVAILABLE, expected_page_count=2
        )
        self.assertEqual(exc.details["page_index"], 2)

    def test_all_blank_result_is_rejected(self):
        self._assert_reason(
            _steps_for_contract_object(
                _contract_object(
                    [
                        _page_entry(1, outcome="blank", text=""),
                        _page_entry(2, outcome="blank", text=""),
                    ]
                )
            ),
            REASON_NO_TRANSCRIBED_TEXT,
            expected_page_count=2,
        )

    def test_missing_model_output_is_rejected(self):
        self._assert_reason(
            [{"type": "thought", "content": [{"type": "text", "text": "x"}]}],
            REASON_MISSING_MODEL_OUTPUT,
        )

    def test_unexpected_function_and_code_steps_reject_valid_looking_final_json(
        self,
    ):
        valid = _ocr_contract_json("plausible transcription")
        for step_type in (
            "function_call",
            "function_result",
            "code_execution_call",
            "code_execution_result",
        ):
            with self.subTest(step_type=step_type):
                steps = [
                    {
                        "type": step_type,
                        "arguments": {"query": _DOCUMENT_320_GREETING},
                        "content": [{"type": "text", "text": _DOCUMENT_320_GREETING}],
                    },
                    _model_output_step(valid),
                ]
                self._assert_reason(
                    steps,
                    REASON_UNEXPECTED_TOOL_USE,
                    forbidden=_DOCUMENT_320_GREETING,
                )


class AntigravityCompletedOutputValidationEngineTests(SimpleTestCase):
    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_valid_one_page_completed_output(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            _completed_ocr_interaction("Single page body")
        )
        result = transcribe_pages_with_antigravity(
            _one_page(), api_key="key", background=False
        )
        self.assertEqual(result.text, "Single page body")
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_last_model_output_wins_through_engine(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-1",
                "status": "completed",
                "steps": [
                    _model_output_step(_ocr_contract_json("DRAFT_MUST_NOT_APPEAR")),
                    _model_output_step(_ocr_contract_json("Accepted page")),
                ],
            }
        )
        result = transcribe_pages_with_antigravity(
            _one_page(), api_key="key", background=False
        )
        self.assertEqual(result.text, "Accepted page")
        self.assertNotIn("DRAFT_MUST_NOT_APPEAR", result.text)
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_split_final_text_blocks_reconstruct_json_without_inserted_characters(
        self, mock_post, mock_get
    ):
        cases = (
            ("hello world", "hel"),
            ("line1\nline2", "line1"),
        )
        for transcript, split_before in cases:
            with self.subTest(transcript=transcript):
                full = _ocr_contract_json(transcript)
                split_at = full.index(split_before) + len(split_before)
                prefix = full[:split_at]
                suffix = full[split_at:]
                self.assertTrue(prefix.endswith(split_before))
                self.assertTrue(suffix)
                self.assertEqual(prefix + suffix, full)

                mock_post.return_value = _ok_json_response(
                    {
                        "id": "ix-split",
                        "status": "completed",
                        "steps": [
                            _model_output_step(
                                _ocr_contract_json("DRAFT_MUST_NOT_APPEAR")
                            ),
                            {
                                "type": "model_output",
                                "content": [
                                    {"type": "text", "text": prefix},
                                    {"type": "text", "text": suffix},
                                ],
                            },
                        ],
                    }
                )
                result = transcribe_pages_with_antigravity(
                    _one_page(), api_key="key", background=False
                )
                self.assertEqual(result.text, transcript)
                self.assertNotIn("DRAFT_MUST_NOT_APPEAR", result.text)
                self.assertNotEqual(
                    result.text,
                    split_before + "\n" + transcript[len(split_before) :],
                )
                self.assertNotEqual(
                    result.text,
                    split_before + " " + transcript[len(split_before) :],
                )
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_greeting_and_logs_omit_provider_text(self, mock_post, mock_get):
        mock_post.return_value = _ok_json_response(
            _completed_interaction(_DOCUMENT_320_GREETING)
        )
        with self.assertLogs(_ENGINE_LOGGER, level="WARNING") as captured:
            with self.assertRaises(AntigravityOutputValidationError) as ctx:
                transcribe_pages_with_antigravity(
                    _one_page(),
                    api_key="key",
                    background=False,
                    document_id=320,
                )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_JSON)
        self.assertNotIn(_DOCUMENT_320_GREETING, str(ctx.exception))
        logs = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn(_DOCUMENT_320_GREETING, logs)
        self.assertIn("reason=invalid_json", logs)
        mock_get.assert_not_called()

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_tool_steps_reject_valid_final_json_without_logging_tool_payload(
        self, mock_post, mock_get
    ):
        secret = "TOOL_ARGUMENT_MUST_NOT_LOG"
        mock_post.return_value = _ok_json_response(
            {
                "id": "ix-1",
                "status": "completed",
                "steps": [
                    {
                        "type": "function_call",
                        "arguments": {"q": secret},
                    },
                    _model_output_step(_ocr_contract_json("Looks valid")),
                ],
            }
        )
        with self.assertLogs(_ENGINE_LOGGER, level="WARNING") as captured:
            with self.assertRaises(AntigravityOutputValidationError) as ctx:
                transcribe_pages_with_antigravity(
                    _one_page(), api_key="key", background=False
                )
        self.assertEqual(ctx.exception.reason, REASON_UNEXPECTED_TOOL_USE)
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn("Looks valid", str(ctx.exception))
        logs = "\n".join(record.getMessage() for record in captured.records)
        self.assertNotIn(secret, logs)
        self.assertNotIn("Looks valid", logs)
        mock_get.assert_not_called()


class AntigravityInteractionSummaryToolStepTests(SimpleTestCase):
    def test_production_tool_step_types_are_allowlisted_not_other(self):
        summary = summarize_antigravity_interaction(
            {
                "id": "ix-tools",
                "status": "completed",
                "steps": [
                    {"type": "thought"},
                    {"type": "function_call"},
                    {"type": "function_result"},
                    {"type": "code_execution_call"},
                    {"type": "code_execution_result"},
                    {"type": "model_output"},
                ],
            }
        )
        self.assertEqual(
            summary["step_types"],
            [
                "thought",
                "function_call",
                "function_result",
                "code_execution_call",
                "code_execution_result",
                "model_output",
            ],
        )


class AntigravityWorkerInvalidCompletedOutputTests(TestCase):
    def setUp(self):
        self.command = Command()
        self.command._cfg = _make_worker_env()
        self.doc = create_ocr_document(
            title="Arabic printed Antigravity validation",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="doc-320.png",
            mime_type="image/png",
        )

    @patch(
        "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini"
    )
    @patch("documents.management.commands.run_worker.get_object_bytes")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_invalid_completed_output_is_durable_ocr_failure(
        self,
        mock_post,
        mock_get,
        mock_extract_pages,
        mock_get_object_bytes,
        mock_translate,
    ):
        mock_get_object_bytes.return_value = (b"png-bytes", "image/png")
        mock_extract_pages.return_value = _one_page()
        mock_post.return_value = _ok_json_response(
            _completed_interaction(_DOCUMENT_320_GREETING)
        )
        msg = {
            "Body": json.dumps({"type": "PROCESS_DOCUMENT", "document_id": self.doc.id})
        }

        with patch.dict(
            os.environ,
            {"ENABLE_ANTIGRAVITY_ARABIC_PRINTED": "true"},
            clear=False,
        ):
            self.assertTrue(self.command._process_message(msg))

        mock_translate.assert_not_called()
        mock_get.assert_not_called()

        failure = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        self.assertEqual(failure.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(failure.error_code, "OCR_FAILED")
        self.assertEqual(
            failure.engine_key, DocumentTextResult.OcrEngineKey.ANTIGRAVITY
        )
        self.assertEqual(
            failure.prompt_variant, DocumentTextResult.OcrPromptVariant.PRINTED
        )
        self.assertIsNone(failure.text)
        self.assertNotIn(_DOCUMENT_320_GREETING, failure.error_details or "")
        self.assertIn("reason=invalid_json", failure.error_details or "")

        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=self.doc,
                status=DocumentTextResult.Status.NEEDS_REVIEW,
            ).exists()
        )
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=self.doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            ).exists()
        )

        self.doc.refresh_from_db()
        self.assertNotEqual(
            self.doc.processing_state_user, Document.ProcessingState.READY
        )

        with patch.dict(
            os.environ,
            {"ENABLE_ANTIGRAVITY_ARABIC_PRINTED": "true"},
            clear=False,
        ):
            self.assertTrue(is_ocr_reprocess_ui_eligible(self.doc))
            assessment = assess_ocr_reprocess(
                self.doc.id,
                collection_id="col",
                model_id="42",
            )
        self.assertEqual(assessment.retry_mode, OcrRetryMode.NORMAL_REENQUEUE)
        self.assertEqual(assessment.document_id, self.doc.id)
        self.assertIsNone(assessment.source_transkribus_run_id)

        latest_source = (
            DocumentTextResult.objects.filter(
                document=self.doc,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            )
            .order_by("-updated_at", "-pk")
            .first()
        )
        self.assertIsNotNone(latest_source)
        self.assertEqual(latest_source.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(latest_source.error_code, "OCR_FAILED")
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=self.doc,
                verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            ).exists()
        )
