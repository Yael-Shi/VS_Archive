from types import SimpleNamespace

from django.test import TestCase

from documents.management.commands.run_worker import Command, UNRESOLVED_ROUTE_METADATA
from documents.models import Document, DocumentTextResult


class WorkerRoutingPersistenceTests(TestCase):
    def setUp(self):
        self.doc = Document.objects.create(
            title="Routing test doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
        )
        self.command = Command()
        self.command._cfg = SimpleNamespace(min_text_length=5)

    def test_success_path_persists_engine_key_and_prompt_variant(self):
        htr = SimpleNamespace(
            text="some extracted text",
            needs_review=False,
            review_reasons=[],
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

        self.command._save_htr_results(
            doc=self.doc,
            engine="gemini-2.0-flash",
            is_he=True,
            htr=htr,
        )

        result = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
        )
        self.assertEqual(result.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            result.prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

    def test_failure_path_with_valid_route_persists_route_metadata(self):
        self.command._save_ocr_failure(
            doc=self.doc,
            engine="gemini-fallback",
            is_he=False,
            details="upstream OCR failure",
        )

        result = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-fallback",
        )
        self.assertEqual(result.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(result.error_code, "OCR_FAILED")
        self.assertEqual(result.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertEqual(
            result.prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )

    def test_failure_path_with_invalid_routing_does_not_silent_fallback(self):
        self.doc.language = "xx"
        self.doc.text_input_type = "INVALID_INPUT_TYPE"
        self.doc.save(update_fields=["language", "text_input_type"])

        self.command._save_ocr_failure(
            doc=self.doc,
            engine="gemini-fallback",
            is_he=False,
            details="routing metadata invalid",
        )

        result = DocumentTextResult.objects.get(
            document=self.doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-fallback",
        )
        self.assertEqual(result.status, DocumentTextResult.Status.FAILED)
        self.assertEqual(result.error_code, "OCR_ROUTING_INVALID")
        self.assertEqual(result.engine_key, UNRESOLVED_ROUTE_METADATA)
        self.assertEqual(result.prompt_variant, UNRESOLVED_ROUTE_METADATA)
        self.assertNotEqual(result.engine_key, DocumentTextResult.OcrEngineKey.GEMINI)
        self.assertNotEqual(
            result.prompt_variant,
            DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
