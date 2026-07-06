from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from documents.services.antigravity_defaults import DEFAULT_ANTIGRAVITY_AGENT_ID
from documents.services.antigravity_engine import (
    AntigravityError,
    build_antigravity_ocr_prompt,
    build_multimodal_input,
    normalize_antigravity_image_headings,
    output_text_from_steps,
    transcribe_pages_with_antigravity,
)
from documents.services.htr_adapters.antigravity_adapter import AntigravityAdapter
from documents.services.htr_adapters.base import EnginePermanentError
from documents.services.page_extraction import PageImage


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
        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        with self.assertRaisesMessage(AntigravityError, "Missing GEMINI_API_KEY"):
            transcribe_pages_with_antigravity(pages, api_key="")

    def test_empty_pages_raises(self):
        with self.assertRaisesMessage(AntigravityError, "No page images supplied"):
            transcribe_pages_with_antigravity([], api_key="key")

    @patch("documents.services.antigravity_engine.requests.post")
    def test_http_failure_raises(self, mock_post):
        response = MagicMock()
        response.ok = False
        response.status_code = 403
        response.text = "denied"
        response.json.return_value = {"error": {"message": "permission denied"}}
        mock_post.return_value = response

        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        with self.assertRaisesMessage(AntigravityError, "HTTP 403: permission denied"):
            transcribe_pages_with_antigravity(
                pages,
                api_key="key",
                background=False,
            )

    @patch("documents.services.antigravity_engine.requests.post")
    def test_completed_without_text_raises(self, mock_post):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {
            "id": "ix-empty",
            "status": "completed",
            "steps": [],
        }
        mock_post.return_value = response

        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        with self.assertRaisesMessage(
            AntigravityError, "completed with no OCR output text"
        ):
            transcribe_pages_with_antigravity(
                pages,
                api_key="key",
                background=False,
            )

    @patch("documents.services.antigravity_engine.requests.post")
    def test_non_completed_status_raises(self, mock_post):
        response = MagicMock()
        response.ok = True
        response.json.return_value = {"id": "ix-fail", "status": "failed", "steps": []}
        mock_post.return_value = response

        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        with self.assertRaisesMessage(
            AntigravityError, "finished with status='failed'"
        ):
            transcribe_pages_with_antigravity(
                pages,
                api_key="key",
                background=False,
            )

    @patch("documents.services.antigravity_engine._get_interaction")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_poll_timeout_raises(self, mock_post, mock_get):
        mock_post.return_value = MagicMock(
            ok=True,
            json=lambda: {"id": "ix-progress", "status": "in_progress"},
        )
        mock_get.return_value = {"id": "ix-progress", "status": "in_progress"}

        pages = [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")]
        monotonic = iter([0.0, 301.0]).__next__

        with self.assertRaisesMessage(AntigravityError, "Timed out after 300.0s"):
            transcribe_pages_with_antigravity(
                pages,
                api_key="key",
                timeout_seconds=300.0,
                poll_seconds=0.0,
                monotonic_fn=monotonic,
                sleep_fn=lambda _seconds: None,
            )

    @patch("documents.services.antigravity_engine.requests.post")
    def test_success_returns_transcription(self, mock_post):
        ocr_text = "[IMAGE 1: page-1.png]\nArabic text here"
        response = MagicMock()
        response.ok = True
        response.json.return_value = _completed_interaction(ocr_text)
        mock_post.return_value = response

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

        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["agent"], DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertEqual(payload["environment"], "remote")
        prompt = payload["input"][0]["text"]
        self.assertIn("- IMAGE 1: page-1.png", prompt)
        self.assertIn("- IMAGE 2: page-2.png", prompt)
        self.assertEqual(len(payload["input"]), 3)


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
                pages=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
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
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
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
                pages=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
                language_hint="ar",
                prompt_variant="printed",
                worker_env=_make_worker_env(),
            )
