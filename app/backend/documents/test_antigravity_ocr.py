from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import requests

from django.test import SimpleTestCase

from documents.services.antigravity_defaults import DEFAULT_ANTIGRAVITY_AGENT_ID
from documents.services.antigravity_engine import (
    AntigravityError,
    build_antigravity_ocr_prompt,
    build_multimodal_input,
    normalize_antigravity_image_headings,
    output_text_from_steps,
    summarize_antigravity_interaction,
    transcribe_pages_with_antigravity,
)
from documents.services.htr_adapters.antigravity_adapter import AntigravityAdapter
from documents.services.htr_adapters.base import EnginePermanentError
from documents.services.page_extraction import PageImage


_ENGINE_LOGGER = "documents.services.antigravity_engine"
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


def _ok_json_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.ok = True
    response.json.return_value = payload
    return response


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


class NormalizeAntigravityImageHeadingsTests(SimpleTestCase):
    def test_single_image_heading_is_removed(self):
        raw = "[IMAGE 1: page-1.png]\nArabic text here"
        self.assertEqual(
            normalize_antigravity_image_headings(raw),
            "Arabic text here",
        )

    def test_multi_image_headings_become_page_separators(self):
        raw = (
            "[IMAGE 1: page-1.png]\n"
            "First page body\n\n"
            "[IMAGE 2: page-2.png]\n"
            "Second page body"
        )
        self.assertEqual(
            normalize_antigravity_image_headings(raw),
            "עמוד 1\nFirst page body\n\nעמוד 2\nSecond page body",
        )

    def test_body_text_remains_unchanged(self):
        body = "Line one\nLine two\n  indented"
        raw = f"[IMAGE 1: cover.png]\n{body}"
        self.assertEqual(normalize_antigravity_image_headings(raw), body)

    def test_text_without_image_headings_is_unchanged(self):
        text = "Plain OCR output\nwith no headings"
        self.assertEqual(normalize_antigravity_image_headings(text), text)


class AntigravityEngineTests(SimpleTestCase):
    def test_output_text_from_steps_extracts_model_output(self):
        steps = [
            {"type": "tool_call", "content": []},
            {
                "type": "model_output",
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"},
                ],
            },
        ]
        self.assertEqual(output_text_from_steps(steps), "line one\nline two")

    def test_build_prompt_includes_image_labels_and_rules(self):
        prompt = build_antigravity_ocr_prompt(["cover.png", "page-2.png"])
        self.assertIn("OCR/transcription only. Do not translate.", prompt)
        self.assertIn("Preserve Arabic, Hebrew, and Latin scripts", prompt)
        self.assertIn("[UNCLEAR]", prompt)
        self.assertIn("- IMAGE 1: cover.png", prompt)
        self.assertIn("[IMAGE 2: page-2.png]", prompt)

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

        with self.assertRaisesMessage(AntigravityError, "HTTP 403: permission denied"):
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )
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

        with self.assertRaisesMessage(
            AntigravityError, "completed with no OCR output text"
        ):
            transcribe_pages_with_antigravity(
                _one_page(),
                api_key="key",
                background=False,
            )
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
            _completed_interaction("[IMAGE 1: page-1.png]\nArabic text here"),
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
        self.assertIn("elapsed_seconds=4.000", poll_summaries[0])
        self.assertIn("status=completed", poll_summaries[0])

    @patch(
        "documents.services.antigravity_engine._get_interaction",
        side_effect=_unexpected_get,
    )
    @patch("documents.services.antigravity_engine.requests.post")
    def test_success_returns_transcription(self, mock_post, mock_get):
        ocr_text = "[IMAGE 1: page-1.png]\nArabic text here"
        mock_post.return_value = _ok_json_response(_completed_interaction(ocr_text))

        pages = [
            PageImage(page_index=2, image_bytes=b"b", mime_type="image/png"),
            PageImage(page_index=1, image_bytes=b"a", mime_type="image/png"),
        ]
        result = transcribe_pages_with_antigravity(
            pages,
            api_key="key",
            background=False,
        )

        self.assertEqual(result.text, "Arabic text here")
        self.assertEqual(result.engine_name, DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertTrue(result.needs_review)
        mock_get.assert_not_called()

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["agent"], DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertEqual(payload["environment"], "remote")
        prompt = payload["input"][0]["text"]
        self.assertIn("- IMAGE 1: page-1.png", prompt)
        self.assertIn("- IMAGE 2: page-2.png", prompt)
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
                        "content": [{"type": "text", "text": _POST_OCR_TEXT}],
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
        mock_get.return_value = _completed_interaction(
            f"[IMAGE 1: page-1.png]\n{_OCR_SENTINEL}",
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
            self.assertIn("document_id=320", message)
            self.assertIn(f"page_index={page.page_index}", message)
            self.assertIn(f"base64_character_length={len(encoded)}", message)
            self.assertIn("data_is_str=True", message)
            self.assertIn("data_nonempty=True", message)
            self.assertIn("starts_with_data_url_prefix=False", message)
            self.assertNotIn(encoded, message)
            self.assertNotIn("raw_byte_length=", message)
            self.assertNotIn("mime_type=", message)


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
        mock_get.return_value = _completed_interaction(
            f"[IMAGE 1: page-1.png]\n{_OCR_SENTINEL}",
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
        mock_post.side_effect = requests.ReadTimeout(
            "create timeout body MUST-NOT-LOG"
        )
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
            _completed_interaction(_POST_OCR_TEXT, interaction_id="ix-done")
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
        self.assertNotIn("phase=final_get", "\n".join(
            record.getMessage() for record in captured.records
        ))


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
            ocr_text=f"[IMAGE 1: page-1.png]\n{_OCR_SENTINEL}"
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
