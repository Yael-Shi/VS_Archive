from __future__ import annotations

from unittest.mock import patch

from django.test import SimpleTestCase

from documents.models import Document, DocumentTextResult
from documents.services.antigravity_engine import AntigravityResult
from documents.services.gemini_engine import GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.htr_adapters.registry import _ADAPTERS
from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
from documents.services.htr_engine import transcribe_pages
from documents.services.ocr_routing import OcrRouteConfig
from documents.test_antigravity_ocr import (
    DEFAULT_ANTIGRAVITY_AGENT_ID,
    _BANDED_DEADLINE,
    _make_worker_env,
    _one_page,
)


def _raise_if_deadline(mock_name: str):
    def _side_effect(*args, **kwargs):
        if "absolute_deadline_monotonic" in kwargs:
            raise TypeError(
                f"{mock_name}() got an unexpected keyword argument "
                "'absolute_deadline_monotonic'"
            )
        if mock_name == "transcribe_pages_with_gemini":
            return GeminiResult(text="gemini-ok", engine_name="gemini-compat")
        if mock_name == "transcribe_pages_with_antigravity":
            return AntigravityResult(
                text="antigravity-json-ok",
                engine_name=DEFAULT_ANTIGRAVITY_AGENT_ID,
                needs_review=True,
            )
        return HtrResult(text="transkribus-ok", engine_name="transkribus-compat")

    return _side_effect


class RegisteredAdapterDeadlineCompatTests(SimpleTestCase):
    def test_every_registered_non_banded_route_ignores_generic_deadline(self):
        registered = set(_ADAPTERS)
        self.assertEqual(
            registered,
            {
                DocumentTextResult.OcrEngineKey.GEMINI,
                DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
                DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            },
        )
        self.assertNotIn("HYBRID", registered)

        pages = _one_page()
        deadline = _BANDED_DEADLINE

        with patch(
            "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini",
            side_effect=_raise_if_deadline("transcribe_pages_with_gemini"),
        ) as mock_gemini:
            gemini_result = transcribe_pages(
                pages,
                Document.Language.HEBREW,
                Document.TextInputType.PRINTED,
                route=OcrRouteConfig(
                    engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                ),
                worker_env=_make_worker_env(),
                absolute_deadline_monotonic=deadline,
            )
        self.assertEqual(gemini_result.text, "gemini-ok")
        mock_gemini.assert_called_once()
        self.assertNotIn(
            "absolute_deadline_monotonic", mock_gemini.call_args.kwargs
        )

        with patch.object(
            TranskribusAdapter,
            "_execute_dev_upload",
            side_effect=_raise_if_deadline("_execute_dev_upload"),
        ) as mock_transkribus:
            transkribus_result = transcribe_pages(
                pages,
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                route=OcrRouteConfig(
                    engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
                    prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                ),
                worker_env=_make_worker_env(transkribus_dev_upload_mode=True),
                document_id=17,
                absolute_deadline_monotonic=deadline,
            )
        self.assertEqual(transkribus_result.text, "transkribus-ok")
        mock_transkribus.assert_called_once()
        self.assertNotIn(
            "absolute_deadline_monotonic", mock_transkribus.call_args.kwargs
        )

        with patch(
            "documents.services.htr_adapters.antigravity_adapter."
            "process_arabic_printed_banded_document"
        ) as mock_coordinator, patch(
            "documents.services.htr_adapters.antigravity_adapter."
            "transcribe_pages_with_antigravity",
            side_effect=_raise_if_deadline("transcribe_pages_with_antigravity"),
        ) as mock_json:
            antigravity_result = transcribe_pages(
                pages,
                Document.Language.ARABIC,
                Document.TextInputType.PRINTED,
                route=OcrRouteConfig(
                    engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
                    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                ),
                worker_env=_make_worker_env(
                    enable_antigravity_arabic_printed=True,
                    enable_antigravity_arabic_printed_banded=False,
                ),
                document_id=9,
                absolute_deadline_monotonic=deadline,
            )
        self.assertEqual(antigravity_result.text, "antigravity-json-ok")
        mock_coordinator.assert_not_called()
        mock_json.assert_called_once()
        self.assertNotIn(
            "absolute_deadline_monotonic", mock_json.call_args.kwargs
        )
        self.assertNotIn("timeout_seconds", mock_json.call_args.kwargs)
