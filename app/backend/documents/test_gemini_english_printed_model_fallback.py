"""Latin printed Gemini 2.5 primary with bounded Gemini 3.6 fallback.

English and French printed share one candidate chain and one fallback
predicate. French MIXED uses the same recovery rule with its own candidate
constant. Handwriting, other mixed languages, Hebrew, and Arabic routes stay
on their existing candidates.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
)
from documents.services.archive_items import create_ocr_document
from documents.services.gemini_engine import (
    GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS,
    gemini_transcription_contract,
)
from documents.services.gemini_models import (
    DEFAULT_GEMINI_MODEL_CANDIDATES,
    ENGLISH_PRINTED_GEMINI_MODEL_CANDIDATES,
    FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    GEMINI_36_FLASH_MODEL,
    MIXED_GEMINI_36_MODEL_CANDIDATES,
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    LATIN_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    LATIN_PRINTED_GEMINI_MODEL,
    LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
)
from documents.services.gemini_page_checkpoints import (
    GeminiPageClaim,
    GeminiPageClaimAction,
    build_gemini_attempt_identity,
    claim_gemini_page,
    get_or_create_gemini_attempt,
    persist_gemini_page_success,
)
from documents.services.htr_adapters.base import HtrResult
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.ocr_routing import (
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_ROUTE,
    gemini_model_candidates,
    select_ocr_route,
)
from documents.services.page_extraction import PageImage

_PAGE = PageImage(
    page_index=1,
    image_bytes=b"synthetic-english-printed-page",
    mime_type="image/jpeg",
    source_identity="page-1.jpg",
    source_content_fingerprint="a" * 64,
)
_PAGE_TEXT = "Synthetic English printed transcription long enough to keep."


def _provider_response(*, text: str, finish_reason: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
        prompt_feedback=SimpleNamespace(block_reason=None),
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=0,
            total_token_count=120,
        ),
    )


class _ExpectedIncomplete(Exception):
    pass


_LATIN_PRINTED_LANGUAGES = (
    Document.Language.ENGLISH,
    Document.Language.FRENCH,
)


def _run_claimed_page(
    adapter: GeminiAdapter,
    responses,
    *,
    language: str = Document.Language.ENGLISH,
):
    with (
        patch(
            "documents.services.gemini_engine._get_api_key",
            return_value="test-key",
        ),
        patch("documents.services.gemini_engine._create_client") as mock_create_client,
        patch("documents.services.gemini_engine.time.sleep"),
        patch(
            "documents.services.htr_adapters.gemini_adapter.persist_gemini_page_success"
        ) as mock_persist_success,
        patch(
            "documents.services.htr_adapters.gemini_adapter.persist_gemini_page_failure"
        ) as mock_persist_failure,
        patch.object(
            adapter,
            "_raise_incomplete",
            side_effect=_ExpectedIncomplete,
        ),
    ):
        client = Mock()
        client.models.generate_content.side_effect = list(responses)
        mock_create_client.return_value = client
        try:
            adapter._execute_claimed_page(
                page=_PAGE,
                language_hint=language,
                prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
                model_candidates=list(LATIN_PRINTED_GEMINI_MODEL_CANDIDATES),
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=False,
                latin_printed_model_fallback_enabled=True,
                kwargs={
                    "max_output_tokens": 4096,
                    "max_output_tokens_hard_cap": 32768,
                },
                checkpoint_id=11,
                lease_token=uuid.uuid4(),
                attempt_id=22,
            )
        except _ExpectedIncomplete:
            pass
    return SimpleNamespace(
        client=client,
        persist_success=mock_persist_success,
        persist_failure=mock_persist_failure,
    )


class EnglishPrintedModelCandidateTests(SimpleTestCase):
    def test_english_printed_is_25_then_36_and_other_routes_stay_put(self):
        hebrew_printed_model = "gemini-3.1-flash-lite"
        cases = (
            (
                Document.Language.ENGLISH,
                Document.TextInputType.PRINTED,
                None,
                LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.FRENCH,
                Document.TextInputType.PRINTED,
                None,
                LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.ENGLISH,
                Document.TextInputType.HANDWRITTEN,
                None,
                LATIN_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.FRENCH,
                Document.TextInputType.HANDWRITTEN,
                None,
                FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.ENGLISH,
                Document.TextInputType.MIXED,
                None,
                DEFAULT_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.FRENCH,
                Document.TextInputType.MIXED,
                None,
                MIXED_GEMINI_36_MODEL_CANDIDATES,
            ),
            (
                Document.Language.HEBREW,
                Document.TextInputType.MIXED,
                Document.HandwritingType.VS,
                MIXED_GEMINI_36_MODEL_CANDIDATES,
            ),
            (
                Document.Language.HEBREW,
                Document.TextInputType.PRINTED,
                None,
                (hebrew_printed_model, GEMINI_36_FLASH_MODEL),
            ),
            (
                Document.Language.ARABIC,
                Document.TextInputType.HANDWRITTEN,
                None,
                DEFAULT_GEMINI_MODEL_CANDIDATES,
            ),
        )
        for language, text_type, handwriting_type, expected in cases:
            with self.subTest(language=language, text_type=text_type):
                route = select_ocr_route(
                    language,
                    text_type,
                    handwriting_type=handwriting_type,
                )
                candidates = gemini_model_candidates(
                    route,
                    language=language,
                    text_input_type=text_type,
                    gemini_hebrew_printed_model=hebrew_printed_model,
                )
                self.assertEqual(candidates, expected)
        self.assertEqual(
            ENGLISH_PRINTED_GEMINI_MODEL_CANDIDATES,
            LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
        )

    def test_hebrew_general_handwritten_chain_is_unchanged(self):
        with patch.dict(
            "os.environ",
            {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
            clear=False,
        ):
            route = select_ocr_route(
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                handwriting_type=Document.HandwritingType.GENERAL,
            )
        self.assertEqual(route, HEBREW_GENERAL_HANDWRITTEN_GEMINI_ROUTE)
        self.assertEqual(
            gemini_model_candidates(
                route,
                language=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.HANDWRITTEN,
                gemini_hebrew_printed_model="gemini-3.1-flash-lite",
            ),
            HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
        )


class EnglishPrintedFallbackExecutionTests(SimpleTestCase):
    def test_primary_success_does_not_call_gemini_36(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_primary_success(language)

    def _assert_primary_success(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [_provider_response(text=_PAGE_TEXT, finish_reason="STOP")],
            language=language,
        )

        self.assertEqual(run.client.models.generate_content.call_count, 1)
        call = run.client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], LATIN_PRINTED_GEMINI_MODEL)
        self.assertEqual(call.kwargs["config"].max_output_tokens, 4096)
        self.assertEqual(call.kwargs["config"].temperature, 0.0)
        self.assertEqual(
            run.persist_success.call_args.kwargs["actual_model"],
            LATIN_PRINTED_GEMINI_MODEL,
        )
        run.persist_failure.assert_not_called()

    def test_max_tokens_advances_to_gemini_36_inside_three_calls(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_max_tokens_advances(language)

    def _assert_max_tokens_advances(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [
                _provider_response(text="partial", finish_reason="MAX_TOKENS"),
                _provider_response(text="partial", finish_reason="MAX_TOKENS"),
                _provider_response(text=_PAGE_TEXT, finish_reason="STOP"),
            ],
            language=language,
        )

        calls = run.client.models.generate_content.call_args_list
        self.assertEqual(len(calls), GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS)
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            [
                LATIN_PRINTED_GEMINI_MODEL,
                LATIN_PRINTED_GEMINI_MODEL,
                GEMINI_36_FLASH_MODEL,
            ],
        )
        self.assertEqual(
            [call.kwargs["config"].max_output_tokens for call in calls],
            [4096, 8192, 8192],
        )
        thinking_level = calls[2].kwargs["config"].thinking_config.thinking_level
        self.assertEqual(str(thinking_level).rsplit(".", 1)[-1].lower(), "minimal")
        self.assertIsNone(calls[2].kwargs["config"].temperature)
        self.assertEqual(
            run.persist_success.call_args.kwargs["actual_model"],
            GEMINI_36_FLASH_MODEL,
        )
        self.assertEqual(run.persist_success.call_args.kwargs["text"], _PAGE_TEXT)
        run.persist_failure.assert_not_called()

    def test_recitation_advances_without_another_primary_call(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_recitation_advances(language)

    def _assert_recitation_advances(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [
                _provider_response(text="blocked", finish_reason="RECITATION"),
                _provider_response(text=_PAGE_TEXT, finish_reason="STOP"),
            ],
            language=language,
        )

        calls = run.client.models.generate_content.call_args_list
        self.assertEqual(
            [call.kwargs["model"] for call in calls],
            [LATIN_PRINTED_GEMINI_MODEL, GEMINI_36_FLASH_MODEL],
        )
        self.assertEqual(
            [call.kwargs["config"].max_output_tokens for call in calls],
            [4096, 4096],
        )
        self.assertEqual(
            run.persist_success.call_args.kwargs["actual_model"],
            GEMINI_36_FLASH_MODEL,
        )
        run.persist_failure.assert_not_called()

    def test_safety_does_not_advance(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_safety_does_not_advance(language)

    def _assert_safety_does_not_advance(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [_provider_response(text="blocked", finish_reason="SAFETY")],
            language=language,
        )

        self.assertEqual(run.client.models.generate_content.call_count, 1)
        self.assertEqual(
            run.client.models.generate_content.call_args.kwargs["model"],
            LATIN_PRINTED_GEMINI_MODEL,
        )
        run.persist_success.assert_not_called()
        self.assertEqual(run.persist_failure.call_args.kwargs["failure_code"], "SAFETY")

    def test_empty_response_does_not_advance(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_empty_response_does_not_advance(language)

    def _assert_empty_response_does_not_advance(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [_provider_response(text="", finish_reason="STOP")] * 3,
            language=language,
        )

        calls = run.client.models.generate_content.call_args_list
        self.assertEqual(len(calls), GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS)
        self.assertEqual(
            {call.kwargs["model"] for call in calls},
            {LATIN_PRINTED_GEMINI_MODEL},
        )
        run.persist_success.assert_not_called()
        self.assertEqual(
            run.persist_failure.call_args.kwargs["failure_code"],
            "EMPTY_RESPONSE",
        )

    def test_quota_does_not_advance(self):
        for language in _LATIN_PRINTED_LANGUAGES:
            with self.subTest(language=language):
                self._assert_quota_does_not_advance(language)

    def _assert_quota_does_not_advance(self, language: str) -> None:
        run = _run_claimed_page(
            GeminiAdapter(),
            [RuntimeError("429 RESOURCE_EXHAUSTED")] * 3,
            language=language,
        )

        calls = run.client.models.generate_content.call_args_list
        self.assertEqual(len(calls), GEMINI_OCR_PAGE_MAX_PROVIDER_CALLS)
        self.assertEqual(
            {call.kwargs["model"] for call in calls},
            {LATIN_PRINTED_GEMINI_MODEL},
        )
        run.persist_success.assert_not_called()
        self.assertEqual(
            run.persist_failure.call_args.kwargs["failure_code"],
            "GEMINI_ERROR",
        )
        self.assertIn(
            "GeminiQuotaError",
            run.persist_failure.call_args.kwargs["failure_message"],
        )

    def test_execute_enables_fallback_for_english_and_french_printed(self):
        adapter = GeminiAdapter()
        pages = [_PAGE]
        cases = (
            (Document.Language.ENGLISH, Document.TextInputType.PRINTED, None, True),
            (Document.Language.FRENCH, Document.TextInputType.PRINTED, None, True),
            (
                Document.Language.ENGLISH,
                Document.TextInputType.HANDWRITTEN,
                None,
                False,
            ),
            (Document.Language.ENGLISH, Document.TextInputType.MIXED, None, False),
            (Document.Language.FRENCH, Document.TextInputType.MIXED, None, False),
            (
                Document.Language.HEBREW,
                Document.TextInputType.MIXED,
                Document.HandwritingType.VS,
                False,
            ),
            (Document.Language.HEBREW, Document.TextInputType.PRINTED, None, False),
            (
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                Document.HandwritingType.GENERAL,
                False,
            ),
            (Document.Language.FRENCH, Document.TextInputType.HANDWRITTEN, None, False),
            (Document.Language.ARABIC, Document.TextInputType.PRINTED, None, False),
        )
        for language, text_type, handwriting_type, enabled in cases:
            with self.subTest(language=language, text_type=text_type):
                with (
                    patch(
                        "documents.services.htr_adapters.gemini_adapter."
                        "get_or_create_gemini_attempt",
                        return_value=SimpleNamespace(id=1),
                    ),
                    patch(
                        "documents.services.htr_adapters.gemini_adapter."
                        "claim_gemini_page",
                        return_value=GeminiPageClaim(
                            action=GeminiPageClaimAction.EXECUTE,
                            checkpoint_id=4,
                            page_index=1,
                            lease_token=uuid.uuid4(),
                        ),
                    ),
                    patch(
                        "documents.services.htr_adapters.gemini_adapter."
                        "assemble_gemini_attempt",
                        return_value=SimpleNamespace(
                            text=_PAGE_TEXT,
                            needs_review=False,
                            engine_name=LATIN_PRINTED_GEMINI_MODEL,
                            review_reasons=[],
                        ),
                    ),
                    patch.object(adapter, "_execute_claimed_page") as mock_page,
                ):
                    result = adapter.execute(
                        pages=pages,
                        language_hint=language,
                        prompt_variant=(
                            DocumentTextResult.OcrPromptVariant.HEBREW_GENERAL_HANDWRITTEN
                            if handwriting_type == Document.HandwritingType.GENERAL
                            else DocumentTextResult.OcrPromptVariant.PRINTED
                            if text_type == Document.TextInputType.PRINTED
                            else DocumentTextResult.OcrPromptVariant.MIXED
                            if text_type == Document.TextInputType.MIXED
                            else DocumentTextResult.OcrPromptVariant.HANDWRITTEN
                        ),
                        document_id=1,
                        text_input_type=text_type,
                        handwriting_type=handwriting_type,
                        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
                        model_candidates=["model-a"],
                    )
                self.assertIsInstance(result, HtrResult)
                self.assertEqual(
                    mock_page.call_args.kwargs["latin_printed_model_fallback_enabled"],
                    enabled,
                )
                self.assertEqual(
                    mock_page.call_args.kwargs["mixed_gemini_36_fallback_enabled"],
                    text_type == Document.TextInputType.MIXED
                    and language
                    in {Document.Language.FRENCH, Document.Language.HEBREW},
                )
                if (
                    language == Document.Language.HEBREW
                    and text_type == Document.TextInputType.MIXED
                ):
                    self.assertFalse(
                        mock_page.call_args.kwargs[
                            "hebrew_printed_crop_recovery_enabled"
                        ]
                    )
                    self.assertFalse(
                        mock_page.call_args.kwargs[
                            "hebrew_printed_mixed_script_enabled"
                        ]
                    )
                    self.assertFalse(
                        mock_page.call_args.kwargs["recitation_model_fallback_enabled"]
                    )


class EnglishPrintedCheckpointIdentityTests(TestCase):
    def test_new_candidate_chain_does_not_reuse_the_old_page_success(self):
        cases = (
            (
                Document.Language.ENGLISH,
                "English printed identity",
                "documents/en-printed-identity/source/0.jpeg",
            ),
            (
                Document.Language.FRENCH,
                "French printed identity",
                "documents/fr-printed-identity/source/0.jpeg",
            ),
        )
        for language, title, s3_key in cases:
            with self.subTest(language=language):
                self._assert_new_chain_does_not_reuse_one_model_attempt(
                    language=language,
                    title=title,
                    s3_key=s3_key,
                )

    def _assert_new_chain_does_not_reuse_one_model_attempt(
        self,
        *,
        language: str,
        title: str,
        s3_key: str,
    ) -> None:
        document = create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            language=language,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key=s3_key,
            mime_type="image/jpeg",
        )
        pages = [
            PageImage(
                page_index=1,
                image_bytes=b"page-one",
                mime_type="image/jpeg",
                source_identity="page-1.jpg",
                source_content_fingerprint="a" * 64,
            ),
            PageImage(
                page_index=2,
                image_bytes=b"page-two",
                mime_type="image/jpeg",
                source_identity="page-2.jpg",
                source_content_fingerprint="b" * 64,
            ),
        ]
        contract = gemini_transcription_contract(
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            language_hint=language,
            temperature=0.2,
        )
        shared = {
            "pages": pages,
            "language_hint": language,
            "text_input_type": Document.TextInputType.PRINTED,
            "handwriting_type": None,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.PRINTED,
            "contract": contract,
            "min_text_length": 20,
            "double_pass": False,
            "consistency_min_ratio": 0.85,
            "temperature": 0.2,
            "top_k": 40,
            "top_p": 0.95,
            "max_output_tokens": 4096,
            "max_output_tokens_hard_cap": 32768,
        }
        previous = build_gemini_attempt_identity(
            model_candidates=(LATIN_PRINTED_GEMINI_MODEL,),
            **shared,
        )
        current = build_gemini_attempt_identity(
            model_candidates=LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
            **shared,
        )

        self.assertEqual(previous.prompt_fingerprint, current.prompt_fingerprint)
        self.assertEqual(
            previous.page_fingerprints[1],
            current.page_fingerprints[1],
        )
        self.assertNotEqual(previous.config_fingerprint, current.config_fingerprint)
        self.assertNotEqual(
            previous.identity_fingerprint,
            current.identity_fingerprint,
        )

        old_attempt = get_or_create_gemini_attempt(
            document_id=document.id,
            identity=previous,
        )
        claim = claim_gemini_page(
            attempt_id=old_attempt.id,
            page_index=1,
            page_fingerprint=previous.page_fingerprints[1],
            source_content_fingerprint=previous.source_content_fingerprints[1],
        )
        assert claim.lease_token is not None
        persist_gemini_page_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            actual_model=LATIN_PRINTED_GEMINI_MODEL,
            text=_PAGE_TEXT,
            needs_review=False,
            review_reasons=[],
        )

        new_attempt = get_or_create_gemini_attempt(
            document_id=document.id,
            identity=current,
        )
        self.assertNotEqual(old_attempt.id, new_attempt.id)
        self.assertEqual(
            GeminiOcrAttempt.objects.filter(document=document).count(),
            2,
        )
        new_claim = claim_gemini_page(
            attempt_id=new_attempt.id,
            page_index=1,
            page_fingerprint=current.page_fingerprints[1],
            source_content_fingerprint=current.source_content_fingerprints[1],
        )
        self.assertEqual(new_claim.action, GeminiPageClaimAction.EXECUTE)
        self.assertIsNotNone(new_claim.lease_token)

        old_page = GeminiOcrPageCheckpoint.objects.get(
            attempt=old_attempt,
            page_index=1,
        )
        self.assertEqual(old_page.status, GeminiOcrPageCheckpoint.Status.SUCCEEDED)
        self.assertEqual(old_page.text, _PAGE_TEXT)
        self.assertEqual(old_attempt.model_candidates, [LATIN_PRINTED_GEMINI_MODEL])
        new_attempt.refresh_from_db()
        self.assertEqual(
            new_attempt.model_candidates,
            list(LATIN_PRINTED_GEMINI_MODEL_CANDIDATES),
        )
