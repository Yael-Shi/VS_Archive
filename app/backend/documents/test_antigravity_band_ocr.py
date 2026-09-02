from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from documents.services.antigravity_defaults import (
    ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS,
    ANTIGRAVITY_CANCEL_HTTP_TIMEOUT_SECONDS,
    ANTIGRAVITY_REMOTE_ENVIRONMENT,
    ANTIGRAVITY_REQUESTED_MODEL,
    ARABIC_PRINTED_DOCUMENT_SAFETY_MARGIN_SECONDS,
    ARABIC_PRINTED_PAGE_BUDGET_CAP_SECONDS,
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    DEFAULT_POLL_GET_TIMEOUT_SECONDS,
    DEFAULT_POLL_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INTERACTIONS_API_REVISION,
    INTERACTIONS_BASE_URL,
)
from documents.services.antigravity_engine import (
    NO_SYNTHETIC_ELLIPSIS_INSTRUCTION,
    AntigravityBandCancelResult,
    AntigravityBandCheckpointError,
    BAND_ATTEMPT_ASSISTED_FALLBACK,
    BAND_ATTEMPT_UNASSISTED,
    cancel_antigravity_interaction,
    create_arabic_printed_band_interaction,
    transcribe_band_with_antigravity,
    transcribe_pages_with_antigravity,
)
from documents.services.arabic_printed_ocr_contract import (
    COMPLETION_MARKER,
    ArabicPrintedOcrFailureKind,
)
from documents.services.page_extraction import PageImage

API_KEY = "band-api-key-DO-NOT-LEAK"
PADDED_API_KEY = f"  {API_KEY}  "
SECRET_THOUGHT = "SECRET_THOUGHT_MUST_NOT_APPEAR"
FAILED_OUTPUT = "FAILED_RAW_OUTPUT_MUST_NOT_APPEAR"
DRAFT = "UNIQUE_CLOUD_VISION_DRAFT_TOKEN_مرحبا"
JPEG_BYTES = b"\xff\xd8fake-jpeg-crop-bytes\xff\xd9"
JPEG_MIME = "image/jpeg"
INTERACTION_ID = "ix-band-1"
USAGE = {
    "total_input_tokens": 11,
    "total_output_tokens": 7,
    "total_thought_tokens": 2,
    "total_tokens": 20,
}


class FakeResponse:
    def __init__(self, status_code: int, body: Any, text: str | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.ok = 200 <= status_code < 400
        if text is not None:
            self.text = text
        elif body is None:
            self.text = ""
        else:
            self.text = json.dumps(body)

    def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _marked(text: str) -> str:
    return f"{text}\n{COMPLETION_MARKER}"


def _steps(
    text: str | None, *, thought: str | None = None, step_type: str = "model_output"
):
    steps: list[dict[str, Any]] = []
    if thought is not None:
        steps.append(
            {
                "type": "thought",
                "content": [{"type": "text", "text": thought}],
            }
        )
    if text is None and step_type == "model_output":
        steps.append({"type": step_type, "content": []})
        return steps
    content = [{"type": "text", "text": text}] if text is not None else []
    steps.append({"type": step_type, "content": content})
    return steps


def _interaction(
    *,
    status: str,
    text: str | None = None,
    thought: str | None = SECRET_THOUGHT,
    step_type: str = "model_output",
    interaction_id: str = INTERACTION_ID,
) -> dict[str, Any]:
    return {
        "id": interaction_id,
        "status": status,
        "usage": USAGE,
        "steps": _steps(text, thought=thought, step_type=step_type),
    }


def _json_ocr_interaction() -> dict[str, Any]:
    payload = json.dumps(
        {
            "schema_version": 1,
            "pages": [
                {"page_index": 1, "outcome": "transcribed", "text": "Arabic text here"}
            ],
        },
        ensure_ascii=False,
    )
    return {
        "id": "ix-legacy-json",
        "status": "completed",
        "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": payload}]}
        ],
    }


def _noop_created(_interaction_id: str) -> None:
    return None


class AntigravityBandOcrTests(SimpleTestCase):
    def _transcribe(self, fake_post, fake_get, **kwargs):
        clock = kwargs.pop("clock", Clock())
        sleeps: list[float] = []
        created: list[str] = []
        caller_hook = kwargs.pop("on_interaction_created", None)

        def sleep_fn(seconds):
            sleeps.append(seconds)
            clock.now += seconds

        def on_created(interaction_id: str) -> None:
            created.append(interaction_id)
            if caller_hook is not None:
                caller_hook(interaction_id)

        with (
            patch(
                "documents.services.antigravity_engine.validate_antigravity_ocr_output",
                side_effect=AssertionError("JSON validator must not run"),
            ),
            patch(
                "documents.services.antigravity_engine._split_contiguous_pages",
                side_effect=AssertionError("JSON splitter must not run"),
            ),
            patch(
                "documents.services.antigravity_engine._transcribe_pages_recovering_truncated_json",
                side_effect=AssertionError("truncated-JSON recovery must not run"),
            ),
            patch("documents.services.antigravity_engine.requests.post", fake_post),
            patch("documents.services.antigravity_engine.requests.get", fake_get),
        ):
            result = transcribe_band_with_antigravity(
                api_key=kwargs.pop("api_key", API_KEY),
                jpeg_bytes=kwargs.pop("jpeg_bytes", JPEG_BYTES),
                mime_type=kwargs.pop("mime_type", JPEG_MIME),
                vision_draft_text=kwargs.pop("vision_draft_text", DRAFT),
                attempt_kind=kwargs.pop("attempt_kind", BAND_ATTEMPT_UNASSISTED),
                absolute_deadline_monotonic=kwargs.pop(
                    "absolute_deadline_monotonic", 1_000.0
                ),
                on_interaction_created=on_created,
                poll_seconds=kwargs.pop("poll_seconds", 0.0),
                sleep_fn=kwargs.pop("sleep_fn", sleep_fn),
                monotonic_fn=kwargs.pop("monotonic_fn", clock),
            )
        return result, created, sleeps, clock

    def _assert_privacy(self, value: object) -> None:
        text = repr(value) + str(value)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(PADDED_API_KEY, text)
        self.assertNotIn(SECRET_THOUGHT, text)
        self.assertNotIn(FAILED_OUTPUT, text)
        self.assertNotIn(DRAFT, text)
        self.assertNotIn(JPEG_BYTES.decode("latin1"), text)

    def _assert_payload_envelope(self, payload: dict[str, Any]) -> None:
        self.assertEqual(
            set(payload),
            {"agent", "input", "environment", "background", "tools", "agent_config"},
        )
        self.assertEqual(payload["agent"], DEFAULT_ANTIGRAVITY_AGENT_ID)
        self.assertEqual(payload["background"], True)
        self.assertEqual(payload["tools"], [])
        self.assertEqual(
            payload["environment"],
            {
                "type": ANTIGRAVITY_REMOTE_ENVIRONMENT["type"],
                "network": ANTIGRAVITY_REMOTE_ENVIRONMENT["network"],
            },
        )
        self.assertEqual(
            payload["agent_config"],
            {"type": "antigravity", "model": ANTIGRAVITY_REQUESTED_MODEL},
        )
        self.assertNotIn("max_total_tokens", payload["agent_config"])
        self.assertNotIn("response_format", payload)
        self.assertNotIn("previous_interaction_id", payload)
        self.assertNotIn("generation_config", payload)
        self.assertEqual(len(payload["input"]), 2)
        self.assertEqual(payload["input"][0]["type"], "text")
        self.assertEqual(payload["input"][1]["type"], "image")
        self.assertEqual(payload["input"][1]["mime_type"], JPEG_MIME)

    def test_legacy_constants_unchanged(self):
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 1200.0)
        self.assertEqual(DEFAULT_POLL_SECONDS, 5.0)
        self.assertEqual(DEFAULT_POLL_GET_TIMEOUT_SECONDS, 120.0)
        self.assertEqual(DEFAULT_ANTIGRAVITY_AGENT_ID, "antigravity-preview-05-2026")
        self.assertEqual(ANTIGRAVITY_REQUESTED_MODEL, "gemini-3.7-flash")
        self.assertEqual(
            INTERACTIONS_BASE_URL,
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        self.assertEqual(INTERACTIONS_API_REVISION, "2026-05-20")
        self.assertEqual(ANTIGRAVITY_BAND_ATTEMPT_TIMEOUT_CAP_SECONDS, 90.0)
        self.assertEqual(ANTIGRAVITY_CANCEL_HTTP_TIMEOUT_SECONDS, 30.0)
        self.assertEqual(ARABIC_PRINTED_PAGE_BUDGET_CAP_SECONDS, 240.0)
        self.assertEqual(ARABIC_PRINTED_DOCUMENT_SAFETY_MARGIN_SECONDS, 60.0)

    @patch("documents.services.antigravity_engine.requests.get")
    @patch("documents.services.antigravity_engine.requests.post")
    def test_legacy_json_path_still_sends_json_contract(self, mock_post, mock_get):
        mock_post.return_value = FakeResponse(200, _json_ocr_interaction())
        mock_get.side_effect = AssertionError("legacy completed create must not GET")
        result = transcribe_pages_with_antigravity(
            [PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            api_key="legacy-key",
            background=False,
        )
        self.assertEqual(result.text, "Arabic text here")
        self.assertEqual(mock_post.call_count, 1)
        payload = mock_post.call_args.kwargs["json"]
        self.assertIn("JSON only", payload["input"][0]["text"])
        self.assertIn("schema_version", payload["input"][0]["text"])
        self.assertNotIn(COMPLETION_MARKER, payload["input"][0]["text"])
        self.assertEqual(payload["agent_config"]["model"], ANTIGRAVITY_REQUESTED_MODEL)
        mock_get.assert_not_called()

    def test_primary_payload_contract_and_no_draft(self):
        posts: list[dict[str, Any]] = []

        def fake_post(*args, **kwargs):
            posts.append({"url": args[0], **kwargs})
            return FakeResponse(
                200,
                _interaction(status="completed", text=_marked(DRAFT)),
            )

        def fake_get(*args, **kwargs):
            raise AssertionError("completed create must not GET")

        result, created, _sleeps, _clock = self._transcribe(fake_post, fake_get)
        self.assertTrue(result.accepted)
        self.assertEqual(result.transcription, DRAFT)
        self.assertTrue(result.marker_seen)
        self.assertEqual(result.interaction_id, INTERACTION_ID)
        self.assertEqual(created, [INTERACTION_ID])
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["url"], INTERACTIONS_BASE_URL)
        self.assertEqual(posts[0]["headers"]["x-goog-api-key"], API_KEY)
        self.assertEqual(posts[0]["headers"]["Api-Revision"], INTERACTIONS_API_REVISION)
        self.assertEqual(posts[0]["timeout"], 90.0)
        payload = posts[0]["json"]
        self._assert_payload_envelope(payload)
        prompt = payload["input"][0]["text"]
        self.assertIn(COMPLETION_MARKER, prompt)
        self.assertIn(NO_SYNTHETIC_ELLIPSIS_INSTRUCTION.strip(), prompt)
        self.assertIn("Tools are disabled", prompt)
        self.assertNotIn(DRAFT, prompt)
        self.assertNotIn("CLOUD VISION DRAFT", prompt)
        self._assert_privacy(result)

    def test_assisted_draft_is_final_prompt_content(self):
        posts: list[dict[str, Any]] = []

        def fake_post(*args, **kwargs):
            posts.append(kwargs)
            return FakeResponse(
                200,
                _interaction(status="completed", text=_marked(DRAFT)),
            )

        result, _created, _sleeps, _clock = self._transcribe(
            fake_post,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no GET")),
            attempt_kind=BAND_ATTEMPT_ASSISTED_FALLBACK,
        )
        self.assertTrue(result.accepted)
        prompt = posts[0]["json"]["input"][0]["text"]
        self.assertIn(DRAFT, prompt)
        self.assertTrue(prompt.endswith(f"{DRAFT}\n"))
        self.assertGreater(prompt.rfind(DRAFT), prompt.rfind("CLOUD VISION DRAFT"))
        self.assertIn("fallible", prompt.lower())
        suffix = prompt[prompt.rfind(DRAFT) + len(DRAFT) :]
        self.assertEqual(suffix, "\n")
        self._assert_privacy(result)

    def test_callback_before_first_get_and_one_create(self):
        order: list[str] = []
        posts = {"n": 0}

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            order.append("create")
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            order.append("get")
            return FakeResponse(
                200,
                _interaction(status="completed", text=_marked(DRAFT)),
            )

        def on_created(interaction_id: str) -> None:
            order.append(f"created:{interaction_id}")
            self.assertNotIn("get", order)

        result, _created, _sleeps, _clock = self._transcribe(
            fake_post,
            fake_get,
            on_interaction_created=on_created,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(posts["n"], 1)
        self.assertEqual(order, ["create", f"created:{INTERACTION_ID}", "get"])

    def test_checkpoint_error_is_not_swallowed_or_retried(self):
        posts = {"n": 0}

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            raise AssertionError("must not poll after checkpoint failure")

        def boom(interaction_id: str) -> None:
            raise RuntimeError(f"persist {DRAFT} {API_KEY}")

        with self.assertRaises(AntigravityBandCheckpointError) as ctx:
            self._transcribe(
                fake_post,
                fake_get,
                on_interaction_created=boom,
            )
        self.assertEqual(posts["n"], 1)
        self.assertEqual(ctx.exception.interaction_id, INTERACTION_ID)
        self.assertEqual(ctx.exception.exception_class, "RuntimeError")
        self._assert_privacy(ctx.exception)

    def test_non_callable_callback_is_zero_http_invalid_request(self):
        posts = {"n": 0}
        gets = {"n": 0}

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            raise AssertionError("HTTP must not run")

        def fake_get(*args, **kwargs):
            gets["n"] += 1
            raise AssertionError("HTTP must not run")

        for callback in (None, "not-callable", 123, object()):
            with self.subTest(callback=callback):
                with (
                    patch(
                        "documents.services.antigravity_engine.requests.post",
                        fake_post,
                    ),
                    patch(
                        "documents.services.antigravity_engine.requests.get",
                        fake_get,
                    ),
                ):
                    result = transcribe_band_with_antigravity(
                        api_key=API_KEY,
                        jpeg_bytes=JPEG_BYTES,
                        mime_type=JPEG_MIME,
                        vision_draft_text=DRAFT,
                        attempt_kind=BAND_ATTEMPT_UNASSISTED,
                        absolute_deadline_monotonic=1_000.0,
                        on_interaction_created=callback,
                        poll_seconds=0.0,
                        sleep_fn=lambda _seconds: None,
                        monotonic_fn=Clock(),
                    )
                self.assertFalse(result.accepted)
                self.assertEqual(result.failure_kind, "invalid_request")
                self.assertIsNone(result.interaction_id)
                self.assertFalse(result.create_returned_interaction)
                self._assert_privacy(result)
        self.assertEqual(posts["n"], 0)
        self.assertEqual(gets["n"], 0)

    def test_callback_runtimeerror_keeps_actual_id_and_does_not_poll(self):
        posts = {"n": 0}
        gets = {"n": 0}

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            gets["n"] += 1
            raise AssertionError("must not poll after callback failure")

        def boom(interaction_id: str) -> None:
            raise RuntimeError(f"persist {DRAFT} {API_KEY}")

        with (
            patch("documents.services.antigravity_engine.requests.post", fake_post),
            patch("documents.services.antigravity_engine.requests.get", fake_get),
        ):
            with self.assertRaises(AntigravityBandCheckpointError) as ctx:
                transcribe_band_with_antigravity(
                    api_key=API_KEY,
                    jpeg_bytes=JPEG_BYTES,
                    mime_type=JPEG_MIME,
                    vision_draft_text=DRAFT,
                    attempt_kind=BAND_ATTEMPT_UNASSISTED,
                    absolute_deadline_monotonic=1_000.0,
                    on_interaction_created=boom,
                    poll_seconds=0.0,
                    sleep_fn=lambda _seconds: None,
                    monotonic_fn=Clock(),
                )
        self.assertEqual(posts["n"], 1)
        self.assertEqual(gets["n"], 0)
        self.assertEqual(ctx.exception.interaction_id, INTERACTION_ID)
        self.assertEqual(ctx.exception.exception_class, "RuntimeError")
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self._assert_privacy(ctx.exception)

    def test_callback_forged_checkpoint_error_is_rewrapped_with_actual_id(self):
        posts = {"n": 0}
        gets = {"n": 0}
        forged_id = "ix-forged-other-id"

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            gets["n"] += 1
            raise AssertionError("must not poll after callback failure")

        def boom(interaction_id: str) -> None:
            raise AntigravityBandCheckpointError(
                interaction_id=forged_id,
                exception_class="Forged",
            )

        with (
            patch("documents.services.antigravity_engine.requests.post", fake_post),
            patch("documents.services.antigravity_engine.requests.get", fake_get),
        ):
            with self.assertRaises(AntigravityBandCheckpointError) as ctx:
                transcribe_band_with_antigravity(
                    api_key=API_KEY,
                    jpeg_bytes=JPEG_BYTES,
                    mime_type=JPEG_MIME,
                    vision_draft_text=DRAFT,
                    attempt_kind=BAND_ATTEMPT_UNASSISTED,
                    absolute_deadline_monotonic=1_000.0,
                    on_interaction_created=boom,
                    poll_seconds=0.0,
                    sleep_fn=lambda _seconds: None,
                    monotonic_fn=Clock(),
                )
        self.assertEqual(posts["n"], 1)
        self.assertEqual(gets["n"], 0)
        self.assertEqual(ctx.exception.interaction_id, INTERACTION_ID)
        self.assertNotEqual(ctx.exception.interaction_id, forged_id)
        self.assertEqual(ctx.exception.exception_class, "AntigravityBandCheckpointError")
        self.assertIsInstance(ctx.exception.__cause__, AntigravityBandCheckpointError)
        self.assertEqual(ctx.exception.__cause__.interaction_id, forged_id)
        self._assert_privacy(ctx.exception)

    def test_create_failure_without_id_is_distinguishable(self):
        def fake_post(*args, **kwargs):
            return FakeResponse(500, {"error": {"message": f"denied {API_KEY}"}})

        result, created, _sleeps, _clock = self._transcribe(
            fake_post,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no GET")),
        )
        self.assertEqual(created, [])
        self.assertIsNone(result.interaction_id)
        self.assertFalse(result.create_returned_interaction)
        self.assertEqual(result.failure_kind, "create_http")
        self.assertEqual(result.polling_outcome, "create_error")
        self._assert_privacy(result)

    def test_evaluation_fail_closed_cases(self):
        cases = [
            (
                "missing",
                "مرحبا",
                ArabicPrintedOcrFailureKind.INCOMPLETE_OUTPUT,
            ),
            (
                "greeting",
                _marked("Hello! How can I help you today?"),
                ArabicPrintedOcrFailureKind.GREETING_OR_UNRELATED,
            ),
            (
                "empty",
                _marked(""),
                ArabicPrintedOcrFailureKind.EMPTY_OUTPUT,
            ),
            (
                "ellipsis",
                _marked("visible text..."),
                ArabicPrintedOcrFailureKind.TERMINAL_ELLIPSIS,
            ),
            (
                "coverage_low",
                _marked("x"),
                ArabicPrintedOcrFailureKind.COVERAGE_RATIO,
            ),
            (
                "coverage_high",
                _marked("x" * 80),
                ArabicPrintedOcrFailureKind.COVERAGE_RATIO,
            ),
        ]
        for name, text, kind in cases:
            with self.subTest(name=name):
                body = _interaction(
                    status="completed",
                    text=text,
                    thought=SECRET_THOUGHT,
                )

                def fake_post(*args, _body=body, **kwargs):
                    return FakeResponse(200, _body)

                result, _created, _sleeps, _clock = self._transcribe(
                    fake_post,
                    lambda *a, **k: (_ for _ in ()).throw(AssertionError("no GET")),
                    vision_draft_text=DRAFT,
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.transcription, "")
                self.assertEqual(result.failure_kind, kind)
                self._assert_privacy(result)

    def test_tools_incomplete_and_unknown_status_fail_closed(self):
        tool_body = _interaction(
            status="completed",
            text=_marked(DRAFT),
            step_type="function_call",
            thought=None,
        )
        incomplete_body = _interaction(status="incomplete", text=_marked(DRAFT))
        unknown_body = _interaction(status="not-a-status", text=_marked(DRAFT))
        for body, kind in (
            (tool_body, ArabicPrintedOcrFailureKind.UNEXPECTED_TOOL_USE),
            (incomplete_body, ArabicPrintedOcrFailureKind.INCOMPLETE_STATUS),
            (unknown_body, ArabicPrintedOcrFailureKind.OTHER_STATUS),
        ):

            def fake_post(*args, _body=body, **kwargs):
                return FakeResponse(200, _body)

            result, _created, _sleeps, _clock = self._transcribe(
                fake_post,
                lambda *a, **k: (_ for _ in ()).throw(AssertionError("no GET")),
            )
            self.assertFalse(result.accepted)
            self.assertEqual(result.failure_kind, kind)
            self.assertEqual(result.transcription, "")
            self._assert_privacy(result)

    def test_shared_90s_deadline_and_remaining_get_timeout(self):
        clock = Clock(0.0)
        get_timeouts: list[float] = []
        sleeps: list[float] = []

        def fake_post(*args, **kwargs):
            self.assertEqual(kwargs["timeout"], 90.0)
            clock.now = 80.0
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            get_timeouts.append(kwargs["timeout"])
            return FakeResponse(
                200,
                _interaction(status="completed", text=_marked(DRAFT)),
            )

        def sleep_fn(seconds):
            sleeps.append(seconds)
            self.assertLessEqual(clock.now + seconds, 90.0)
            clock.now += seconds

        result, _created, _recorded, _clock = self._transcribe(
            fake_post,
            fake_get,
            clock=clock,
            sleep_fn=sleep_fn,
            poll_seconds=5.0,
            absolute_deadline_monotonic=1_000.0,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(sleeps, [5.0])
        self.assertEqual(get_timeouts, [5.0])

    def test_timeout_retains_id_and_does_not_cancel(self):
        clock = Clock(0.0)
        cancels = {"n": 0}
        gets = {"n": 0}

        def fake_post(*args, **kwargs):
            url = args[0]
            if str(url).rstrip("/").endswith("/cancel"):
                cancels["n"] += 1
                raise AssertionError("transcribe must not cancel")
            clock.now = 90.1
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            gets["n"] += 1
            raise AssertionError("deadline already passed")

        result, created, _sleeps, _clock = self._transcribe(
            fake_post,
            fake_get,
            clock=clock,
            absolute_deadline_monotonic=1_000.0,
        )
        self.assertEqual(created, [INTERACTION_ID])
        self.assertEqual(result.interaction_id, INTERACTION_ID)
        self.assertTrue(result.create_returned_interaction)
        self.assertEqual(result.last_status, "in_progress")
        self.assertEqual(result.polling_outcome, "timeout")
        self.assertEqual(result.failure_kind, "poll_timeout")
        self.assertFalse(result.accepted)
        self.assertEqual(gets["n"], 0)
        self.assertEqual(cancels["n"], 0)
        self._assert_privacy(result)

    def test_poll_error_retains_id_and_does_not_cancel(self):
        cancels = {"n": 0}

        def fake_post(*args, **kwargs):
            if str(args[0]).rstrip("/").endswith("/cancel"):
                cancels["n"] += 1
                raise AssertionError("must not cancel")
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            raise requests.ConnectionError("poll down")

        result, created, _sleeps, _clock = self._transcribe(
            fake_post, fake_get, poll_seconds=0.0
        )
        self.assertEqual(created, [INTERACTION_ID])
        self.assertEqual(result.interaction_id, INTERACTION_ID)
        self.assertEqual(result.last_status, "in_progress")
        self.assertEqual(result.polling_outcome, "poll_error")
        self.assertEqual(result.failure_kind, "poll_error")
        self.assertEqual(cancels["n"], 0)
        self._assert_privacy(result)

    def test_invalid_inputs_are_zero_http(self):
        posts = {"n": 0}

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            raise AssertionError("HTTP must not run")

        for kwargs in (
            {"api_key": "   "},
            {"jpeg_bytes": b""},
            {"mime_type": "image/png"},
            {"attempt_kind": "hybrid"},
            {"vision_draft_text": None},
            {"absolute_deadline_monotonic": 0.0},
        ):
            with self.subTest(kwargs=kwargs):
                result, created, _sleeps, _clock = self._transcribe(
                    fake_post,
                    fake_post,
                    clock=Clock(1.0),
                    **kwargs,
                )
                self.assertEqual(created, [])
                self.assertIsNone(result.interaction_id)
                self.assertFalse(result.accepted)
        self.assertEqual(posts["n"], 0)

    def test_public_create_does_not_poll(self):
        gets = {"n": 0}

        def fake_post(*args, **kwargs):
            return FakeResponse(
                200, _interaction(status="in_progress", text=None, thought=None)
            )

        def fake_get(*args, **kwargs):
            gets["n"] += 1
            raise AssertionError("public create must not GET")

        with patch("documents.services.antigravity_engine.requests.post", fake_post):
            with patch("documents.services.antigravity_engine.requests.get", fake_get):
                created = create_arabic_printed_band_interaction(
                    api_key=API_KEY,
                    jpeg_bytes=JPEG_BYTES,
                    mime_type=JPEG_MIME,
                    vision_draft_text=DRAFT,
                    attempt_kind=BAND_ATTEMPT_UNASSISTED,
                    timeout_seconds=12.0,
                )
        self.assertEqual(created.interaction_id, INTERACTION_ID)
        self.assertEqual(created.last_status, "in_progress")
        self.assertEqual(gets["n"], 0)
        self._assert_privacy(created)


class AntigravityBandCancelTests(SimpleTestCase):
    def _assert_privacy(self, value: object) -> None:
        text = repr(value) + str(value)
        self.assertNotIn(API_KEY, text)
        self.assertNotIn(SECRET_THOUGHT, text)
        self.assertNotIn(DRAFT, text)
        self.assertNotIn(FAILED_OUTPUT, text)

    def test_cancel_contract_one_call_no_retry(self):
        posts: list[dict[str, Any]] = []

        def fake_post(*args, **kwargs):
            posts.append({"url": args[0], **kwargs})
            return FakeResponse(200, {"id": INTERACTION_ID, "status": "cancelled"})

        with patch("documents.services.antigravity_engine.requests.post", fake_post):
            result = cancel_antigravity_interaction(
                api_key=PADDED_API_KEY,
                interaction_id=INTERACTION_ID,
                vision_draft_text=DRAFT,
            )
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0]["url"], f"{INTERACTIONS_BASE_URL}/{INTERACTION_ID}/cancel"
        )
        self.assertEqual(posts[0]["json"], {})
        self.assertEqual(posts[0]["timeout"], 30.0)
        self.assertEqual(posts[0]["headers"]["x-goog-api-key"], API_KEY)
        self.assertEqual(result.cancel_outcome, "cancelled")
        self.assertEqual(result.last_status, "cancelled")
        self.assertIsInstance(result, AntigravityBandCancelResult)
        self.assertNotEqual(result.cancel_outcome, "accepted")
        self._assert_privacy(result)

    def test_cancel_completed_race_evaluates_but_is_not_ocr_success(self):
        body = _interaction(status="completed", text=_marked(DRAFT))

        def fake_post(*args, **kwargs):
            return FakeResponse(200, body)

        with patch("documents.services.antigravity_engine.requests.post", fake_post):
            result = cancel_antigravity_interaction(
                api_key=API_KEY,
                interaction_id=INTERACTION_ID,
                vision_draft_text=DRAFT,
            )
        self.assertEqual(result.cancel_outcome, "completed")
        self.assertEqual(result.last_status, "completed")
        self.assertTrue(result.evaluation_accepted)
        self.assertEqual(result.transcription, DRAFT)
        self.assertIsInstance(result, AntigravityBandCancelResult)
        self._assert_privacy(result)

    def test_cancel_failed_unknown_http_timeout_network(self):
        def cancelled_failed():
            return FakeResponse(200, {"id": INTERACTION_ID, "status": "failed"})

        def unknown():
            return FakeResponse(200, {"id": INTERACTION_ID, "status": "nope"})

        def missing():
            return FakeResponse(200, {"id": INTERACTION_ID}, text=" ")

        def http_err():
            return FakeResponse(
                500,
                {
                    "error": {
                        "status": f"{INTERACTIONS_BASE_URL}?key={API_KEY}",
                        "message": f"denied {API_KEY}",
                    }
                },
            )

        with patch(
            "documents.services.antigravity_engine.requests.post",
            lambda *a, **k: cancelled_failed(),
        ):
            failed = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(failed.cancel_outcome, "failed")

        with patch(
            "documents.services.antigravity_engine.requests.post",
            lambda *a, **k: unknown(),
        ):
            other = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(other.cancel_outcome, "other")
        self.assertEqual(other.last_status, "other")

        with patch(
            "documents.services.antigravity_engine.requests.post",
            lambda *a, **k: missing(),
        ):
            missing_status = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(missing_status.cancel_outcome, "other")
        self.assertIsNone(missing_status.last_status)

        with patch(
            "documents.services.antigravity_engine.requests.post",
            lambda *a, **k: http_err(),
        ):
            http = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(http.cancel_outcome, "http_error")
        self.assertEqual(http.http_status, 500)
        self.assertNotIn(API_KEY, http.provider_error_code or "")
        self._assert_privacy(http)

        with patch(
            "documents.services.antigravity_engine.requests.post",
            side_effect=requests.Timeout("cancel timeout"),
        ):
            timed = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(timed.cancel_outcome, "timeout")

        with patch(
            "documents.services.antigravity_engine.requests.post",
            side_effect=requests.ConnectionError("offline"),
        ):
            network = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id=INTERACTION_ID
            )
        self.assertEqual(network.cancel_outcome, "network_error")

        posts = {"n": 0}

        def blocked(*args, **kwargs):
            posts["n"] += 1
            raise AssertionError("invalid id must not POST")

        with patch("documents.services.antigravity_engine.requests.post", blocked):
            invalid = cancel_antigravity_interaction(
                api_key=API_KEY, interaction_id="bad id with spaces"
            )
        self.assertEqual(posts["n"], 0)
        self.assertEqual(invalid.cancel_outcome, "other")
        for item in (failed, other, missing_status, http, timed, network, invalid):
            self._assert_privacy(item)
