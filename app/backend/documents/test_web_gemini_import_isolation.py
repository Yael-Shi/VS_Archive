"""Regression: web URL/view imports must not load the Gemini SDK.

Gunicorn web workers on small ECS tasks timed out while importing
``documents.views`` because Hebrew translation retry pulled in
``gemini_engine`` → ``google.genai`` at module scope.

These tests run in a fresh subprocess so they are not affected by a
polluted parent ``sys.modules`` (other tests import ``gemini_engine``).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from django.test import SimpleTestCase

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_IMPORT_TIMEOUT_SECONDS = 60


def _run_isolated(script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("DJANGO_SETTINGS_MODULE", "vs_archive.settings")
    env.setdefault("DJANGO_DEBUG", "1")
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=_IMPORT_TIMEOUT_SECONDS,
        check=False,
    )


class WebGeminiImportIsolationTests(SimpleTestCase):
    def test_url_and_view_import_does_not_load_gemini_sdk(self):
        script = r"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vs_archive.settings")

from django.core.wsgi import get_wsgi_application
from django.urls import get_resolver

get_wsgi_application()


def _load_url_patterns(resolver):
    for pattern in resolver.url_patterns:
        nested = getattr(pattern, "url_patterns", None)
        if nested is not None:
            _load_url_patterns(pattern)


_load_url_patterns(get_resolver())

import documents.views  # noqa: E402
import documents.urls  # noqa: E402
import documents.archive_urls  # noqa: E402
import vs_archive.urls  # noqa: E402

forbidden = [
    name
    for name in (
        "documents.services.gemini_engine",
        "google.genai",
    )
    if name in sys.modules
]
if forbidden:
    raise SystemExit("unexpected modules loaded: " + ", ".join(forbidden))
"""
        result = _run_isolated(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "web URL/view import loaded Gemini SDK\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ),
        )

    def test_hebrew_translation_retry_import_does_not_load_gemini_sdk(self):
        script = r"""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vs_archive.settings")
import django

django.setup()

from documents.services.hebrew_translation_retry import (  # noqa: E402
    HebrewTranslationRetryError,
    is_hebrew_translation_retry_ui_eligible,
    validate_document_for_hebrew_translation_retry,
)
from documents.services.process_document_hebrew_translation_retry_enqueue import (  # noqa: E402
    HebrewTranslationRetryEnqueueError,
    enqueue_hebrew_translation_retry,
)

_ = (
    HebrewTranslationRetryError,
    HebrewTranslationRetryEnqueueError,
    is_hebrew_translation_retry_ui_eligible,
    validate_document_for_hebrew_translation_retry,
    enqueue_hebrew_translation_retry,
)

forbidden = [
    name
    for name in (
        "documents.services.gemini_engine",
        "google.genai",
    )
    if name in sys.modules
]
if forbidden:
    raise SystemExit("unexpected modules loaded: " + ", ".join(forbidden))
"""
        result = _run_isolated(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "Hebrew translation retry import loaded Gemini SDK\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ),
        )

    def test_worker_execution_wrapper_loads_and_calls_gemini_implementation(self):
        script = r"""
import os
import sys
import types

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "vs_archive.settings")
import django

django.setup()

from documents.services import hebrew_translation_retry as retry_mod  # noqa: E402

if "documents.services.gemini_engine" in sys.modules:
    raise SystemExit("gemini_engine loaded before execution wrapper")
if "google.genai" in sys.modules:
    raise SystemExit("google.genai loaded before execution wrapper")

fake = types.ModuleType("documents.services.gemini_engine")
calls = []


def _translate(*args, **kwargs):
    calls.append((args, kwargs))
    return "translated"


fake.translate_text_to_hebrew_with_gemini = _translate
sys.modules["documents.services.gemini_engine"] = fake

result = retry_mod.translate_text_to_hebrew_with_gemini(
    "source text",
    "en",
    model_name="gemini-2.5-flash",
)
if result != "translated":
    raise SystemExit(f"unexpected wrapper result: {result!r}")
if not calls:
    raise SystemExit("Gemini implementation was not called")
if calls[0][0] != ("source text", "en"):
    raise SystemExit(f"unexpected positional args: {calls[0][0]!r}")
if calls[0][1] != {"model_name": "gemini-2.5-flash"}:
    raise SystemExit(f"unexpected kwargs: {calls[0][1]!r}")
if "documents.services.gemini_engine" not in sys.modules:
    raise SystemExit("gemini_engine was not imported by the wrapper")
if "google.genai" in sys.modules:
    raise SystemExit("google.genai loaded while using a fake gemini_engine")
"""
        result = _run_isolated(script)
        self.assertEqual(
            result.returncode,
            0,
            msg=(
                "worker Gemini wrapper did not load/call gemini_engine\n"
                f"stdout:\n{result.stdout}\n"
                f"stderr:\n{result.stderr}"
            ),
        )
