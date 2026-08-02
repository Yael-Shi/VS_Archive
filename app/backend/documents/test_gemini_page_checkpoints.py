from __future__ import annotations

import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from django.db import DatabaseError, connection, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from documents.management.commands.run_worker import Command
from documents.models import (
    Document,
    DocumentTextResult,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
)
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import WorkerEnvConfig
from documents.services.gemini_engine import (
    GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
    GeminiError,
    GeminiResult,
    gemini_transcription_contract,
)
from documents.services.gemini_page_checkpoints import (
    GeminiPageClaimAction,
    StaleGeminiPageClaimError,
    assemble_gemini_attempt,
    build_gemini_attempt_identity,
    claim_gemini_page,
    get_or_create_gemini_attempt,
    persist_gemini_page_failure,
    persist_gemini_page_success,
)
from documents.services.htr_adapters.base import (
    EnginePageIncompleteError,
    EnginePageCheckpointPersistenceRetryableError,
    EnginePermanentError,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.page_extraction import PageImage
from documents.services.process_document_outcome import ProcessDocumentDisposition


def _document(title: str = "Gemini checkpoint document") -> Document:
    return create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        language=Document.Language.ENGLISH,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.PROCESSING,
        file_s3_key=f"{title}.pdf",
        mime_type="application/pdf",
    )


def _pages(*values: bytes) -> list[PageImage]:
    return [
        PageImage(
            page_index=index,
            image_bytes=value,
            mime_type="image/png",
            source_identity="document.pdf",
            source_content_fingerprint="a" * 64,
        )
        for index, value in enumerate(values, start=1)
    ]


def _identity(
    pages: list[PageImage],
    *,
    model_candidates: tuple[str, ...] = ("model-a",),
    temperature: float = 0.2,
    prompt_fingerprint: str | None = None,
    language_hint: str = Document.Language.ENGLISH,
    text_input_type: str = Document.TextInputType.HANDWRITTEN,
    prompt_variant: str = DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    max_output_tokens_hard_cap: int = 32768,
):
    contract = gemini_transcription_contract(
        prompt_variant=prompt_variant,
        language_hint=language_hint,
        temperature=temperature,
    )
    if prompt_fingerprint is not None:
        contract = replace(contract, prompt_fingerprint=prompt_fingerprint)
    return build_gemini_attempt_identity(
        pages=pages,
        language_hint=language_hint,
        text_input_type=text_input_type,
        handwriting_type=Document.HandwritingType.VS,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=prompt_variant,
        model_candidates=model_candidates,
        contract=contract,
        min_text_length=20,
        double_pass=False,
        consistency_min_ratio=0.85,
        temperature=temperature,
        top_k=40,
        top_p=0.95,
        max_output_tokens=8192,
        max_output_tokens_hard_cap=max_output_tokens_hard_cap,
    )


class GeminiPageCheckpointIdentityTests(TestCase):
    def setUp(self) -> None:
        self.document = _document()

    def test_identical_contract_reuses_attempt(self):
        identity = _identity(_pages(b"one", b"two"))

        first = get_or_create_gemini_attempt(
            document_id=self.document.id,
            identity=identity,
        )
        second = get_or_create_gemini_attempt(
            document_id=self.document.id,
            identity=identity,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(GeminiOcrAttempt.objects.count(), 1)

    def test_source_replacement_prevents_stale_reuse(self):
        original = _identity(_pages(b"one", b"two"))
        replaced = _identity(_pages(b"one", b"replacement"))

        self.assertNotEqual(
            original.identity_fingerprint,
            replaced.identity_fingerprint,
        )
        self.assertNotEqual(
            original.source_fingerprint,
            replaced.source_fingerprint,
        )

    def test_source_file_change_prevents_reuse_when_normalized_page_is_same(self):
        pages = _pages(b"same normalized page")
        source_replaced = [replace(pages[0], source_content_fingerprint="b" * 64)]

        original = _identity(pages)
        replaced = _identity(source_replaced)

        self.assertNotEqual(
            original.identity_fingerprint,
            replaced.identity_fingerprint,
        )
        self.assertNotEqual(
            original.source_fingerprint,
            replaced.source_fingerprint,
        )

    def test_page_reordering_prevents_stale_reuse(self):
        original = _identity(_pages(b"one", b"two"))
        reordered = _identity(_pages(b"two", b"one"))

        self.assertNotEqual(
            original.identity_fingerprint,
            reordered.identity_fingerprint,
        )

    def test_prompt_config_and_model_changes_prevent_stale_reuse(self):
        pages = _pages(b"one")
        original = _identity(pages)
        prompt_changed = _identity(pages, prompt_fingerprint="f" * 64)
        config_changed = _identity(pages, temperature=0.3)
        models_changed = _identity(pages, model_candidates=("model-a", "model-b"))

        fingerprints = {
            original.identity_fingerprint,
            prompt_changed.identity_fingerprint,
            config_changed.identity_fingerprint,
            models_changed.identity_fingerprint,
        }
        self.assertEqual(len(fingerprints), 4)

    def test_retry_policy_and_hard_cap_are_part_of_config_identity(self):
        pages = _pages(b"one")
        baseline = _identity(pages)
        duplicate = _identity(pages)
        hard_cap_changed = _identity(pages, max_output_tokens_hard_cap=65536)

        self.assertEqual(
            baseline.config_fingerprint,
            duplicate.config_fingerprint,
        )
        self.assertNotEqual(
            baseline.config_fingerprint,
            hard_cap_changed.config_fingerprint,
        )
        self.assertNotEqual(
            baseline.identity_fingerprint,
            hard_cap_changed.identity_fingerprint,
        )

        with patch(
            "documents.services.gemini_page_checkpoints."
            "GEMINI_OCR_PAGE_RETRY_POLICY_VERSION",
            "gemini-ocr-page-retry-test",
        ):
            retry_policy_changed = _identity(pages)
        self.assertNotEqual(
            baseline.identity_fingerprint,
            retry_policy_changed.identity_fingerprint,
        )

    def test_hebrew_printed_keeps_v2_prompt_version_with_new_config_identity(self):
        # PR D changes attempt identity for every Gemini OCR route, including
        # PR C Hebrew printed, via the config fingerprint. The route-specific
        # prompt-contract version from PR C is unchanged.
        pages = _pages(b"hebrew printed page")
        baseline = _identity(
            pages,
            language_hint=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.PRINTED,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        )
        hard_cap_changed = _identity(
            pages,
            language_hint=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.PRINTED,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            max_output_tokens_hard_cap=65536,
        )

        self.assertEqual(
            baseline.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        self.assertEqual(
            hard_cap_changed.prompt_contract_version,
            GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION,
        )
        self.assertNotEqual(
            baseline.identity_fingerprint,
            hard_cap_changed.identity_fingerprint,
        )

    def test_identity_requires_contiguous_one_based_pages(self):
        pages = [
            PageImage(
                page_index=2,
                image_bytes=b"page",
                mime_type="image/png",
            )
        ]

        with self.assertRaisesRegex(ValueError, "contiguous 1-based"):
            _identity(pages)


class GeminiPageCheckpointPersistenceTests(TestCase):
    def setUp(self) -> None:
        self.document = _document()
        self.pages = _pages(b"one", b"two")
        self.identity = _identity(self.pages)
        self.attempt = get_or_create_gemini_attempt(
            document_id=self.document.id,
            identity=self.identity,
        )

    def _claim(self, page_index: int):
        return claim_gemini_page(
            attempt_id=self.attempt.id,
            page_index=page_index,
            page_fingerprint=self.identity.page_fingerprints[page_index],
            source_content_fingerprint=(
                self.identity.source_content_fingerprints[page_index]
            ),
        )

    def _succeed(
        self,
        page_index: int,
        *,
        model: str = "model-a",
        text: str | None = None,
    ):
        claim = self._claim(page_index)
        assert claim.lease_token is not None
        persist_gemini_page_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            actual_model=model,
            text=text or f"text {page_index}",
            needs_review=page_index == 2,
            review_reasons=["PAGE_REVIEW"] if page_index == 2 else [],
        )
        return claim

    def test_success_is_saved_immediately_and_reused(self):
        self._succeed(1)

        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt=self.attempt,
            page_index=1,
        )
        self.assertEqual(
            checkpoint.status,
            GeminiOcrPageCheckpoint.Status.SUCCEEDED,
        )
        self.assertEqual(checkpoint.text, "text 1")

        duplicate = self._claim(1)
        self.assertEqual(duplicate.action, GeminiPageClaimAction.REUSE)
        self.assertIsNone(duplicate.lease_token)

    def test_failure_records_exact_missing_pages_and_later_reclaims_failed_page(self):
        self._succeed(1)
        failed = self._claim(2)
        assert failed.lease_token is not None

        missing = persist_gemini_page_failure(
            checkpoint_id=failed.checkpoint_id,
            lease_token=failed.lease_token,
            failure_code="MAX_TOKENS",
            failure_message="safe metadata",
        )

        self.assertEqual(missing, [2])
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, GeminiOcrAttempt.Status.PARTIAL)
        self.assertEqual(self.attempt.missing_page_indices, [2])

        retry = self._claim(2)
        self.assertEqual(retry.action, GeminiPageClaimAction.EXECUTE)
        self.assertNotEqual(retry.lease_token, failed.lease_token)
        self.assertEqual(self._claim(1).action, GeminiPageClaimAction.REUSE)

    def test_active_claim_is_busy(self):
        first = self._claim(1)
        second = self._claim(1)

        self.assertEqual(first.action, GeminiPageClaimAction.EXECUTE)
        self.assertEqual(second.action, GeminiPageClaimAction.BUSY)

    def test_claim_rejects_page_outside_attempt_range(self):
        with self.assertRaisesRegex(ValueError, "outside the attempt page range"):
            claim_gemini_page(
                attempt_id=self.attempt.id,
                page_index=3,
                page_fingerprint="f" * 64,
                source_content_fingerprint="a" * 64,
            )

    def test_expired_claim_is_reclaimed_and_stale_writer_is_rejected(self):
        first = self._claim(1)
        assert first.lease_token is not None
        GeminiOcrPageCheckpoint.objects.filter(pk=first.checkpoint_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

        reclaimed = self._claim(1)
        assert reclaimed.lease_token is not None
        self.assertNotEqual(reclaimed.lease_token, first.lease_token)

        with self.assertRaises(StaleGeminiPageClaimError):
            persist_gemini_page_success(
                checkpoint_id=first.checkpoint_id,
                lease_token=first.lease_token,
                actual_model="model-a",
                text="late text",
                needs_review=False,
                review_reasons=[],
            )

    def test_uniform_model_assembly_is_ordered_and_idempotent(self):
        self._succeed(2, text="second")
        self._succeed(1, text="first")

        first = assemble_gemini_attempt(attempt_id=self.attempt.id)
        second = assemble_gemini_attempt(attempt_id=self.attempt.id)

        assert first is not None
        self.assertEqual(first.text, "first\n\nsecond")
        self.assertEqual(first.engine_name, "model-a")
        self.assertTrue(first.needs_review)
        self.assertEqual(first.review_reasons, ["PAGE_REVIEW"])
        self.assertEqual(second, first)

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, GeminiOcrAttempt.Status.COMPLETED)
        self.assertEqual(self.attempt.missing_page_indices, [])
        self.assertIsNotNone(self.attempt.completed_at)

    def test_mixed_model_assembly_uses_deterministic_aggregate_engine(self):
        self._succeed(1, model="model-a")
        self._succeed(2, model="model-b")

        assembled = assemble_gemini_attempt(attempt_id=self.attempt.id)

        assert assembled is not None
        self.assertRegex(assembled.engine_name, r"^gemini-mixed:[0-9a-f]{48}$")
        self.assertLessEqual(len(assembled.engine_name), 64)

    def test_partial_assembly_does_not_return_document_text(self):
        self._succeed(1)

        assembled = assemble_gemini_attempt(attempt_id=self.attempt.id)

        self.assertIsNone(assembled)
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.missing_page_indices, [2])


class GeminiCheckpointAdapterTests(TestCase):
    def setUp(self) -> None:
        self.document = _document("Gemini adapter checkpoint document")
        self.pages = _pages(b"one", b"two")
        self.adapter = GeminiAdapter()
        self.kwargs = {
            "pages": self.pages,
            "language_hint": Document.Language.ENGLISH,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            "document_id": self.document.id,
            "text_input_type": Document.TextInputType.HANDWRITTEN,
            "handwriting_type": Document.HandwritingType.VS,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "model_candidates": ["model-a"],
        }

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_model_candidates_are_normalized_before_identity_and_execution(
        self,
        mock_transcribe,
    ):
        mock_transcribe.side_effect = lambda *, model_name, **_kwargs: GeminiResult(
            text="page text",
            engine_name=model_name,
        )
        self.kwargs["model_candidates"] = [" model-a "]

        result = self.adapter.execute(**self.kwargs)

        self.assertEqual(result.engine_name, "model-a")
        self.assertEqual(
            [call.kwargs["model_name"] for call in mock_transcribe.mock_calls],
            ["model-a", "model-a"],
        )
        attempt = GeminiOcrAttempt.objects.get(document=self.document)
        self.assertEqual(attempt.model_candidates, ["model-a"])

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_blank_normalized_model_candidate_is_rejected(self, mock_transcribe):
        self.kwargs["model_candidates"] = ["   "]

        with self.assertRaisesRegex(
            EnginePermanentError,
            "No Gemini model candidates configured",
        ):
            self.adapter.execute(**self.kwargs)

        mock_transcribe.assert_not_called()
        self.assertFalse(
            GeminiOcrAttempt.objects.filter(document=self.document).exists()
        )

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_execution_after_crash_reuses_already_persisted_page(
        self,
        mock_transcribe,
    ):
        identity = _identity(self.pages)
        attempt = get_or_create_gemini_attempt(
            document_id=self.document.id,
            identity=identity,
        )
        page_one_claim = claim_gemini_page(
            attempt_id=attempt.id,
            page_index=1,
            page_fingerprint=identity.page_fingerprints[1],
            source_content_fingerprint=identity.source_content_fingerprints[1],
        )
        assert page_one_claim.lease_token is not None
        persist_gemini_page_success(
            checkpoint_id=page_one_claim.checkpoint_id,
            lease_token=page_one_claim.lease_token,
            actual_model="model-a",
            text="page one",
            needs_review=False,
            review_reasons=[],
        )
        mock_transcribe.return_value = GeminiResult(
            text="page two",
            engine_name="model-a",
        )

        result = self.adapter.execute(**self.kwargs)

        self.assertEqual(result.text, "page one\n\npage two")
        self.assertEqual(mock_transcribe.call_count, 1)
        self.assertEqual(
            mock_transcribe.call_args.kwargs["pages"][0].page_index,
            2,
        )

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_later_request_processes_only_failed_page(self, mock_transcribe):
        def first_delivery(*, pages, model_name, **_kwargs):
            page_index = pages[0].page_index
            if page_index == 2:
                raise GeminiError("safe page failure")
            return GeminiResult(text="page one", engine_name=model_name)

        mock_transcribe.side_effect = first_delivery
        with self.assertRaises(EnginePageIncompleteError) as raised:
            self.adapter.execute(**self.kwargs)

        self.assertEqual(raised.exception.missing_page_indices, (2,))
        self.assertEqual(
            [call.kwargs["pages"][0].page_index for call in mock_transcribe.mock_calls],
            [1, 2],
        )
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )

        mock_transcribe.reset_mock()
        mock_transcribe.side_effect = None
        mock_transcribe.return_value = GeminiResult(
            text="page two",
            engine_name="model-a",
        )
        result = self.adapter.execute(**self.kwargs)

        self.assertEqual(result.text, "page one\n\npage two")
        self.assertEqual(
            [call.kwargs["pages"][0].page_index for call in mock_transcribe.mock_calls],
            [2],
        )

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_model_fallback_is_per_page_and_preserves_mixed_provenance(
        self,
        mock_transcribe,
    ):
        def execute(*, pages, model_name, **_kwargs):
            page_index = pages[0].page_index
            if page_index == 2 and model_name == "model-a":
                raise GeminiError("QUOTA_EXHAUSTED: model-a")
            return GeminiResult(
                text=f"page {page_index}",
                engine_name=model_name,
            )

        mock_transcribe.side_effect = execute
        self.kwargs["model_candidates"] = ["model-a", "model-b"]

        result = self.adapter.execute(**self.kwargs)

        self.assertRegex(result.engine_name, r"^gemini-mixed:[0-9a-f]{48}$")
        checkpoints = list(
            GeminiOcrPageCheckpoint.objects.filter(
                attempt__document=self.document
            ).order_by("page_index")
        )
        self.assertEqual(
            [checkpoint.actual_model for checkpoint in checkpoints],
            ["model-a", "model-b"],
        )

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_unexpected_provider_content_is_not_persisted(self, mock_transcribe):
        marker = "PRIVATE_PROVIDER_CONTENT_7823"
        mock_transcribe.side_effect = RuntimeError(marker)

        with self.assertRaises(EnginePageIncompleteError):
            self.adapter.execute(**self.kwargs)

        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=self.document,
            page_index=1,
        )
        self.assertEqual(checkpoint.failure_code, "API_ERROR")
        self.assertEqual(checkpoint.failure_message, "exception_class=RuntimeError")
        self.assertNotIn(marker, checkpoint.failure_message)

    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_generic_gemini_error_text_is_not_persisted(self, mock_transcribe):
        marker = "PRIVATE_GEMINI_ERROR_CONTENT_9146"
        mock_transcribe.side_effect = GeminiError(marker)

        with self.assertRaises(EnginePageIncompleteError):
            self.adapter.execute(**self.kwargs)

        checkpoint = GeminiOcrPageCheckpoint.objects.get(
            attempt__document=self.document,
            page_index=1,
        )
        self.assertEqual(checkpoint.failure_code, "GEMINI_ERROR")
        self.assertEqual(checkpoint.failure_message, "exception_class=GeminiError")
        self.assertNotIn(marker, checkpoint.failure_message)


@override_settings(UPLOADS_BUCKET_NAME="uploads")
class GeminiCheckpointWorkerPartialTests(TestCase):
    def setUp(self) -> None:
        self.document = _document("Gemini worker partial document")
        self.command = Command()
        self.command._cfg = cast(
            WorkerEnvConfig,
            SimpleNamespace(
                min_text_length=20,
                gemini_double_pass=False,
                gemini_consistency_min_ratio=0.85,
                gemini_temperature=0.2,
                gemini_top_k=40,
                gemini_top_p=0.95,
                gemini_max_output_tokens=8192,
            ),
        )

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.select_ocr_route")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_page_incomplete_is_partial_without_whole_document_failure_row(
        self,
        mock_get_object,
        mock_extract,
        mock_route,
        mock_transcribe,
    ):
        mock_get_object.return_value = (b"source", "application/pdf")
        mock_extract.return_value = _pages(b"one", b"two", b"three")
        mock_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        mock_transcribe.side_effect = EnginePageIncompleteError(
            [2, 3],
            failure_code="GEMINI_PAGES_INCOMPLETE",
        )

        outcome = self.command._execute_process_document_payload(
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
            }
        )

        self.assertEqual(outcome.disposition, ProcessDocumentDisposition.PARTIAL)
        self.assertEqual(outcome.failure_code, "GEMINI_PAGES_INCOMPLETE")
        self.assertEqual(outcome.failure_message, "missing_pages=2,3")
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PARTIAL,
        )
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )

    @patch("documents.services.htr_adapters.gemini_adapter.persist_gemini_page_failure")
    @patch("documents.services.htr_adapters.gemini_adapter.persist_gemini_page_success")
    @patch(
        "documents.services.htr_adapters.gemini_adapter.transcribe_pages_with_gemini"
    )
    def test_success_persistence_failure_is_retryable_not_provider_failure(
        self,
        mock_transcribe,
        mock_persist_success,
        mock_persist_failure,
    ):
        marker = "PRIVATE_DB_ERROR_CONTENT_4732"
        mock_transcribe.return_value = GeminiResult(
            text="page text",
            engine_name="model-a",
        )
        mock_persist_success.side_effect = DatabaseError(marker)
        adapter = GeminiAdapter()

        with self.assertRaises(EnginePageCheckpointPersistenceRetryableError) as raised:
            adapter.execute(
                pages=_pages(b"one"),
                language_hint=Document.Language.ENGLISH,
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                document_id=self.document.id,
                text_input_type=Document.TextInputType.HANDWRITTEN,
                handwriting_type=Document.HandwritingType.VS,
                engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                model_candidates=["model-a"],
            )

        self.assertEqual(raised.exception.stage, "success")
        self.assertEqual(raised.exception.page_index, 1)
        self.assertNotIn(marker, raised.exception.safe_message)
        mock_persist_failure.assert_not_called()

    @patch("documents.management.commands.run_worker.transcribe_pages")
    @patch("documents.management.commands.run_worker.select_ocr_route")
    @patch("documents.management.commands.run_worker.extract_pages")
    @patch("documents.management.commands.run_worker.get_object_bytes")
    def test_checkpoint_persistence_failure_is_retryable_without_failure_row(
        self,
        mock_get_object,
        mock_extract,
        mock_route,
        mock_transcribe,
    ):
        mock_get_object.return_value = (b"source", "application/pdf")
        mock_extract.return_value = _pages(b"one")
        mock_route.return_value = OcrRouteConfig(
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        mock_transcribe.side_effect = EnginePageCheckpointPersistenceRetryableError(
            stage="success",
            page_index=1,
        )

        outcome = self.command._execute_process_document_payload(
            {
                "type": "PROCESS_DOCUMENT",
                "document_id": self.document.id,
            }
        )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.RETRYABLE,
        )
        self.assertFalse(outcome.should_ack)
        self.assertEqual(
            outcome.failure_code,
            "OCR_PAGE_CHECKPOINT_PERSISTENCE_RETRYABLE",
        )
        self.assertEqual(outcome.failure_message, "stage=success page_index=1")
        self.document.refresh_from_db()
        self.assertEqual(
            self.document.processing_state_user,
            Document.ProcessingState.PROCESSING,
        )
        self.assertFalse(
            DocumentTextResult.objects.filter(document=self.document).exists()
        )


class GeminiPageCheckpointConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Gemini checkpoint concurrency test requires PostgreSQL")
        self.document = _document("Concurrent Gemini checkpoint document")
        self.identity = _identity(_pages(b"one"))
        self.attempt = get_or_create_gemini_attempt(
            document_id=self.document.id,
            identity=self.identity,
        )

    def test_competing_page_claims_execute_provider_once(self):
        barrier = threading.Barrier(2, timeout=10)
        claims = []
        errors = []

        def claim() -> None:
            close_old_connections()
            try:
                barrier.wait()
                result = claim_gemini_page(
                    attempt_id=self.attempt.id,
                    page_index=1,
                    page_fingerprint=self.identity.page_fingerprints[1],
                    source_content_fingerprint=(
                        self.identity.source_content_fingerprints[1]
                    ),
                )
                claims.append(result)
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(claim.action for claim in claims),
            sorted(
                [
                    GeminiPageClaimAction.EXECUTE,
                    GeminiPageClaimAction.BUSY,
                ]
            ),
        )
        self.assertEqual(
            GeminiOcrPageCheckpoint.objects.filter(
                attempt=self.attempt,
                page_index=1,
            ).count(),
            1,
        )
