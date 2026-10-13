"""French and Hebrew MIXED Gemini 2.5 primary with bounded Gemini 3.6 fallback.

language=fr or language=he with text_input_type=MIXED uses this chain. It
reuses the printed MAX_TOKENS / RECITATION recovery rule. English and Arabic
MIXED stay on the default candidate chain. Hebrew printed crop recovery and
Hebrew handwritten routing stay unchanged.
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
from documents.services.gemini_hebrew_printed_crop_recovery import (
    hebrew_printed_recitation_crop_recovery_policy,
)
from documents.services.gemini_hebrew_printed_mixed_script import (
    hebrew_printed_mixed_script_region_fallback_policy,
)
from documents.services.gemini_models import (
    DEFAULT_GEMINI_MODEL_CANDIDATES,
    FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    GEMINI_36_FLASH_MODEL,
    HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
    LATIN_PRINTED_GEMINI_MODEL,
    LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
    MIXED_GEMINI_36_MODEL_CANDIDATES,
    hebrew_printed_gemini_model_candidates,
    mixed_gemini_36_fallback_eligible,
    primary_then_gemini_36_fallback_eligible,
)
from documents.services.gemini_page_checkpoints import (
    GeminiPageClaimAction,
    build_gemini_attempt_identity,
    claim_gemini_page,
    get_or_create_gemini_attempt,
    persist_gemini_page_failure,
    persist_gemini_page_success,
)
from documents.services.htr_adapters.gemini_adapter import GeminiAdapter
from documents.services.ocr_routing import gemini_model_candidates, select_ocr_route
from documents.services.page_extraction import PageImage

_PAGE = PageImage(
    page_index=1,
    image_bytes=b"synthetic-french-mixed-page",
    mime_type="image/jpeg",
    source_identity="page-1.jpg",
    source_content_fingerprint="a" * 64,
)
_PAGE_TEXT = "Synthetic mixed transcription long enough to keep."
_MIXED_FALLBACK_LANGUAGES = (
    Document.Language.FRENCH,
    Document.Language.HEBREW,
)


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


def _run_claimed_page(
    adapter: GeminiAdapter,
    responses,
    *,
    language: str = Document.Language.FRENCH,
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
                prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
                model_candidates=list(MIXED_GEMINI_36_MODEL_CANDIDATES),
                recitation_model_fallback_enabled=False,
                hebrew_general_model_fallback_enabled=False,
                latin_printed_model_fallback_enabled=False,
                mixed_gemini_36_fallback_enabled=True,
                kwargs={
                    "max_output_tokens": 4096,
                    "max_output_tokens_hard_cap": 32768,
                },
                checkpoint_id=11,
                lease_token=uuid.uuid4(),
                attempt_id=77,
            )
        except _ExpectedIncomplete:
            pass
    return SimpleNamespace(
        client=client,
        persist_success=mock_persist_success,
        persist_failure=mock_persist_failure,
    )


class FrenchMixedModelCandidateTests(SimpleTestCase):
    def test_french_and_hebrew_mixed_are_25_then_36(self):
        for language in _MIXED_FALLBACK_LANGUAGES:
            with self.subTest(language=language):
                route = select_ocr_route(
                    language,
                    Document.TextInputType.MIXED,
                    handwriting_type=Document.HandwritingType.VS,
                )
                self.assertEqual(
                    route.engine_key,
                    DocumentTextResult.OcrEngineKey.GEMINI,
                )
                self.assertEqual(
                    route.prompt_variant,
                    DocumentTextResult.OcrPromptVariant.MIXED,
                )
                candidates = gemini_model_candidates(
                    route,
                    language=language,
                    text_input_type=Document.TextInputType.MIXED,
                    gemini_hebrew_printed_model="unused-model",
                )
                self.assertEqual(candidates, MIXED_GEMINI_36_MODEL_CANDIDATES)
                self.assertEqual(
                    candidates,
                    (LATIN_PRINTED_GEMINI_MODEL, GEMINI_36_FLASH_MODEL),
                )
                self.assertTrue(
                    mixed_gemini_36_fallback_eligible(
                        language,
                        Document.TextInputType.MIXED,
                    )
                )
                self.assertTrue(
                    primary_then_gemini_36_fallback_eligible(
                        language,
                        Document.TextInputType.MIXED,
                    )
                )

    def test_hebrew_mixed_does_not_use_printed_recovery(self):
        self.assertEqual(
            hebrew_printed_recitation_crop_recovery_policy(
                language_hint=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.MIXED,
            ),
            "",
        )
        self.assertEqual(
            hebrew_printed_mixed_script_region_fallback_policy(
                language_hint=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.MIXED,
            ),
            "",
        )
        self.assertFalse(
            mixed_gemini_36_fallback_eligible(
                Document.Language.HEBREW,
                Document.TextInputType.PRINTED,
            )
        )
        self.assertFalse(
            mixed_gemini_36_fallback_eligible(
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
            )
        )

    def test_neighboring_routes_keep_their_candidates(self):
        cases = (
            (
                Document.Language.ENGLISH,
                Document.TextInputType.MIXED,
                DEFAULT_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.ARABIC,
                Document.TextInputType.MIXED,
                DEFAULT_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.FRENCH,
                Document.TextInputType.PRINTED,
                LATIN_PRINTED_GEMINI_MODEL_CANDIDATES,
            ),
            (
                Document.Language.FRENCH,
                Document.TextInputType.HANDWRITTEN,
                FRENCH_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
            ),
        )
        for language, text_type, expected in cases:
            with self.subTest(language=language, text_type=text_type):
                self.assertFalse(mixed_gemini_36_fallback_eligible(language, text_type))
                route = select_ocr_route(language, text_type)
                self.assertEqual(
                    gemini_model_candidates(
                        route,
                        language=language,
                        text_input_type=text_type,
                        gemini_hebrew_printed_model="gemini-3.1-flash-lite",
                    ),
                    expected,
                )

    @patch.dict(
        "os.environ",
        {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "false"},
        clear=False,
    )
    def test_hebrew_printed_and_handwritten_candidates_stay_unchanged(self):
        printed = select_ocr_route(
            Document.Language.HEBREW,
            Document.TextInputType.PRINTED,
        )
        self.assertEqual(
            gemini_model_candidates(
                printed,
                language=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.PRINTED,
                gemini_hebrew_printed_model="gemini-3.1-flash-lite",
            ),
            hebrew_printed_gemini_model_candidates("gemini-3.1-flash-lite"),
        )
        general = select_ocr_route(
            Document.Language.HEBREW,
            Document.TextInputType.HANDWRITTEN,
            handwriting_type=Document.HandwritingType.GENERAL,
        )
        self.assertEqual(
            gemini_model_candidates(
                general,
                language=Document.Language.HEBREW,
                text_input_type=Document.TextInputType.HANDWRITTEN,
                gemini_hebrew_printed_model="unused-model",
            ),
            HEBREW_GENERAL_HANDWRITTEN_GEMINI_MODEL_CANDIDATES,
        )
        with self.assertRaises(ValueError):
            select_ocr_route(
                Document.Language.HEBREW,
                Document.TextInputType.HANDWRITTEN,
                handwriting_type=Document.HandwritingType.VS,
            )


class FrenchMixedFallbackExecutionTests(SimpleTestCase):
    def test_primary_success_does_not_call_gemini_36(self):
        for language in _MIXED_FALLBACK_LANGUAGES:
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

    def test_recitation_advances_to_gemini_36_without_another_primary_call(self):
        for language in _MIXED_FALLBACK_LANGUAGES:
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
        fallback = calls[1].kwargs["config"]
        self.assertIsNone(fallback.temperature)
        thinking_level = fallback.thinking_config.thinking_level
        self.assertEqual(str(thinking_level).rsplit(".", 1)[-1].lower(), "minimal")
        self.assertEqual(
            run.persist_success.call_args.kwargs["actual_model"],
            GEMINI_36_FLASH_MODEL,
        )
        self.assertEqual(run.persist_success.call_args.kwargs["text"], _PAGE_TEXT)
        run.persist_failure.assert_not_called()

    def test_max_tokens_advances_to_gemini_36_inside_three_calls(self):
        for language in _MIXED_FALLBACK_LANGUAGES:
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
        self.assertEqual(
            run.persist_success.call_args.kwargs["actual_model"],
            GEMINI_36_FLASH_MODEL,
        )
        self.assertEqual(run.persist_success.call_args.kwargs["text"], _PAGE_TEXT)
        run.persist_failure.assert_not_called()

    def test_safety_does_not_advance(self):
        for language in _MIXED_FALLBACK_LANGUAGES:
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
        for language in _MIXED_FALLBACK_LANGUAGES:
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
        for language in _MIXED_FALLBACK_LANGUAGES:
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


class FrenchMixedCheckpointIdentityTests(TestCase):
    def test_new_candidate_chain_does_not_reuse_the_old_page_success(self):
        document = create_ocr_document(
            title="French mixed identity",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.FRENCH,
            text_input_type=Document.TextInputType.MIXED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/fr-mixed-identity/source/0.jpeg",
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
            PageImage(
                page_index=3,
                image_bytes=b"page-three",
                mime_type="image/jpeg",
                source_identity="page-3.jpg",
                source_content_fingerprint="c" * 64,
            ),
            PageImage(
                page_index=4,
                image_bytes=b"page-four",
                mime_type="image/jpeg",
                source_identity="page-4.jpg",
                source_content_fingerprint="d" * 64,
            ),
        ]
        contract = gemini_transcription_contract(
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            language_hint=Document.Language.FRENCH,
            temperature=0.0,
        )
        shared = {
            "pages": pages,
            "language_hint": Document.Language.FRENCH,
            "text_input_type": Document.TextInputType.MIXED,
            "handwriting_type": None,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.MIXED,
            "contract": contract,
            "min_text_length": 20,
            "double_pass": False,
            "consistency_min_ratio": 0.85,
            "temperature": 0.0,
            "top_k": 40,
            "top_p": 0.95,
            "max_output_tokens": 4096,
            "max_output_tokens_hard_cap": 32768,
        }
        previous = build_gemini_attempt_identity(
            model_candidates=DEFAULT_GEMINI_MODEL_CANDIDATES,
            **shared,
        )
        current = build_gemini_attempt_identity(
            model_candidates=MIXED_GEMINI_36_MODEL_CANDIDATES,
            **shared,
        )

        self.assertEqual(previous.prompt_fingerprint, current.prompt_fingerprint)
        self.assertEqual(previous.source_fingerprint, current.source_fingerprint)
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
        self.assertEqual(claim.action, GeminiPageClaimAction.EXECUTE)
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
        self.assertEqual(
            old_attempt.model_candidates,
            list(DEFAULT_GEMINI_MODEL_CANDIDATES),
        )
        new_attempt.refresh_from_db()
        self.assertEqual(
            new_attempt.model_candidates,
            list(MIXED_GEMINI_36_MODEL_CANDIDATES),
        )

    def test_hebrew_mixed_new_chain_does_not_reuse_old_max_tokens_failure(self):
        document = create_ocr_document(
            title="Hebrew mixed identity",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.MIXED,
            handwriting_type=Document.HandwritingType.VS,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/he-mixed-identity/source/0.jpeg",
            mime_type="image/jpeg",
        )
        pages = [
            PageImage(
                page_index=1,
                image_bytes=b"hebrew-mixed-page",
                mime_type="image/jpeg",
                source_identity="page-1.jpg",
                source_content_fingerprint="e" * 64,
            )
        ]
        contract = gemini_transcription_contract(
            prompt_variant=DocumentTextResult.OcrPromptVariant.MIXED,
            language_hint=Document.Language.HEBREW,
            temperature=0.0,
        )
        shared = {
            "pages": pages,
            "language_hint": Document.Language.HEBREW,
            "text_input_type": Document.TextInputType.MIXED,
            "handwriting_type": Document.HandwritingType.VS,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.MIXED,
            "contract": contract,
            "min_text_length": 20,
            "double_pass": False,
            "consistency_min_ratio": 0.85,
            "temperature": 0.0,
            "top_k": 40,
            "top_p": 0.95,
            "max_output_tokens": 4096,
            "max_output_tokens_hard_cap": 32768,
        }
        previous = build_gemini_attempt_identity(
            model_candidates=DEFAULT_GEMINI_MODEL_CANDIDATES,
            **shared,
        )
        current = build_gemini_attempt_identity(
            model_candidates=MIXED_GEMINI_36_MODEL_CANDIDATES,
            **shared,
        )
        self.assertEqual(previous.prompt_fingerprint, current.prompt_fingerprint)
        self.assertEqual(previous.route_fingerprint, current.route_fingerprint)
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
        persist_gemini_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code="MAX_TOKENS",
            failure_message="bounded provider failure",
        )
        new_attempt = get_or_create_gemini_attempt(
            document_id=document.id,
            identity=current,
        )
        self.assertNotEqual(old_attempt.id, new_attempt.id)
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
        self.assertEqual(old_page.status, GeminiOcrPageCheckpoint.Status.FAILED)
        self.assertEqual(old_page.failure_code, "MAX_TOKENS")
        self.assertEqual(
            old_attempt.model_candidates,
            list(DEFAULT_GEMINI_MODEL_CANDIDATES),
        )
        new_attempt.refresh_from_db()
        self.assertEqual(
            new_attempt.model_candidates,
            list(MIXED_GEMINI_36_MODEL_CANDIDATES),
        )
