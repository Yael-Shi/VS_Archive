from __future__ import annotations

import hashlib
from unittest.mock import patch

from django.test import TestCase

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
    Document,
)
from documents.services.arabic_printed_banded_document_ocr import (
    DOCUMENT_FAILURE_DEADLINE,
    DOCUMENT_FAILURE_IMAGE_LOAD,
    OUTCOME_COMPLETED,
    OUTCOME_PARTIAL,
    ArabicPrintedBandedDocumentResult,
    ArabicPrintedDocumentPageInput,
    arabic_printed_page_absolute_deadline,
    process_arabic_printed_banded_document,
)
from documents.services.arabic_printed_banded_ocr import (
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    ArabicPrintedBandedPageResult,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedBandPlan,
    ArabicPrintedCheckpointBusyError,
    ArabicPrintedCheckpointPersistenceRetryableError,
    ArabicPrintedIdentityMismatchError,
    ArabicPrintedPageClaimAction,
    StaleArabicPrintedPageClaimError,
    assemble_arabic_printed_page,
    missing_pages_for_arabic_printed_attempt,
    persist_arabic_printed_band_success,
    persist_arabic_printed_page_failure,
    persist_arabic_printed_vision_plan,
    reserve_arabic_printed_primary_create,
    reserve_arabic_printed_vision_call,
)
from documents.services.archive_items import create_ocr_document
from documents.services.cloud_vision_document_text import ArabicPrintedWorkingImage

MODULE = "documents.services.arabic_printed_banded_document_ocr"
GEMINI_KEY = "gemini-doc-key-DO-NOT-LEAK"
VISION_KEY = "vision-doc-key-DO-NOT-LEAK"
PAGE_TEXT_ZERO = "النص الكامل للصفحة صفر"
PAGE_TEXT_ONE = "النص الكامل للصفحة واحد"
ENGINE_UNASSISTED = "antigravity-banded:unassisted"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _working_image(
    *, label: bytes, width: int = 40, height: int = 60
) -> ArabicPrintedWorkingImage:
    digest = _sha_bytes(label)
    return ArabicPrintedWorkingImage(
        width=width,
        height=height,
        jpeg_bytes=label,
        mime_type="image/jpeg",
        sha256=digest,
        byte_length=len(label),
        rgb_pixels=label,
    )


def _page_input(
    index: int,
    image: ArabicPrintedWorkingImage,
    *,
    label: bytes,
) -> ArabicPrintedDocumentPageInput:
    return ArabicPrintedDocumentPageInput(
        page_index=index,
        mime_type="image/jpeg",
        source_identity="arabic-printed-document.pdf",
        source_content_fingerprint=_sha_bytes(label + b"-source"),
        oriented_image_sha256=image.sha256,
        oriented_image_width=image.width,
        oriented_image_height=image.height,
    )


def _page_result(
    claim,
    *,
    outcome: str,
    text: str = "",
    failure_code: str | None = None,
) -> ArabicPrintedBandedPageResult:
    return ArabicPrintedBandedPageResult(
        checkpoint_id=claim.checkpoint_id,
        page_index=claim.page_index,
        outcome=outcome,
        assembled_text=text,
        page_quality="UNASSISTED" if outcome == OUTCOME_SUCCEEDED else "",
        runtime_engine_marker=ENGINE_UNASSISTED if outcome == OUTCOME_SUCCEEDED else "",
        failure_code=failure_code,
    )


class ArabicPrintedBandedDocumentOcrTests(TestCase):
    def setUp(self) -> None:
        self.document = create_ocr_document(
            title="Arabic printed banded document coordinator",
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PROCESSING,
            file_s3_key="arabic-printed-document.pdf",
            mime_type="application/pdf",
        )
        self.image_zero = _working_image(label=b"page-zero-pixels")
        self.image_one = _working_image(label=b"page-one-pixels")
        self.pages = (
            _page_input(0, self.image_zero, label=b"page-zero-pixels"),
            _page_input(1, self.image_one, label=b"page-one-pixels"),
        )
        self.images = {0: self.image_zero, 1: self.image_one}
        self.loader_calls: list[int] = []
        self.process_calls: list[dict] = []
        self.clock = Clock(0.0)

    def _load(self, page_index: int) -> ArabicPrintedWorkingImage:
        self.loader_calls.append(page_index)
        return self.images[page_index]

    def _run(self, *, deadline: float = 1_000.0, pages=None):
        return process_arabic_printed_banded_document(
            document_id=self.document.id,
            pages=self.pages if pages is None else pages,
            load_working_image=self._load,
            gemini_api_key=GEMINI_KEY,
            cloud_vision_api_key=VISION_KEY,
            absolute_deadline_monotonic=deadline,
            language_hint=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            poll_seconds=0.0,
            sleep_fn=lambda _seconds: None,
            monotonic_fn=self.clock,
        )

    def _persist_success(self, claim, text: str) -> None:
        token = claim.lease_token
        checkpoint_id = claim.checkpoint_id
        checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(pk=checkpoint_id)
        draft = f"draft-{claim.page_index}"
        reserve_arabic_printed_vision_call(
            checkpoint_id=checkpoint_id,
            lease_token=token,
        )
        persist_arabic_printed_vision_plan(
            checkpoint_id=checkpoint_id,
            lease_token=token,
            cloud_vision_response_sha256="b" * 64,
            bands=[
                ArabicPrintedBandPlan(
                    band_index=0,
                    rect_x=0,
                    rect_y=0,
                    rect_width=checkpoint.oriented_image_width,
                    rect_height=min(40, checkpoint.oriented_image_height),
                    crop_mime="image/jpeg",
                    crop_byte_length=12,
                    crop_sha256=_sha_bytes(f"crop-{claim.page_index}".encode()),
                    vision_draft_text=draft,
                    vision_draft_byte_length=len(draft.encode("utf-8")),
                    vision_draft_sha256=_sha_text(draft),
                )
            ],
        )
        reserve_arabic_printed_primary_create(
            checkpoint_id=checkpoint_id,
            lease_token=token,
            band_index=0,
        )
        persist_arabic_printed_band_success(
            checkpoint_id=checkpoint_id,
            lease_token=token,
            band_index=0,
            selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
            transcription_text=text,
            transcription_sha256=_sha_text(text.strip()),
            transcription_byte_length=len(text.strip().encode("utf-8")),
        )
        assemble_arabic_printed_page(
            checkpoint_id=checkpoint_id,
            lease_token=token,
        )

    def _fake_success(self, texts: dict[int, str]):
        def fake_process(**kwargs):
            self.process_calls.append(kwargs)
            claim = kwargs["claim"]
            if claim.action == ArabicPrintedPageClaimAction.EXECUTE:
                self._persist_success(claim, texts[claim.page_index])
            checkpoint = ArabicPrintedOcrPageCheckpoint.objects.get(
                pk=claim.checkpoint_id
            )
            return ArabicPrintedBandedPageResult(
                checkpoint_id=checkpoint.id,
                page_index=checkpoint.page_index,
                outcome=OUTCOME_SUCCEEDED,
                assembled_text=checkpoint.assembled_text or "",
                page_quality=checkpoint.page_quality,
                runtime_engine_marker=checkpoint.runtime_engine_marker,
                failure_code=None,
            )

        return fake_process

    def test_two_page_success_is_ordered_and_completed(self):
        texts = {0: PAGE_TEXT_ZERO, 1: PAGE_TEXT_ONE}
        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            result = self._run()

        self.assertEqual(result.outcome, OUTCOME_COMPLETED)
        self.assertEqual(result.document_id, self.document.id)
        self.assertEqual(result.missing_page_indices, ())
        self.assertIsNone(result.failure_code)
        self.assertEqual([page.page_index for page in result.pages], [0, 1])
        self.assertEqual(
            [page.assembled_text for page in result.pages],
            [PAGE_TEXT_ZERO, PAGE_TEXT_ONE],
        )
        self.assertEqual(self.loader_calls, [0, 1])
        self.assertEqual(len(self.process_calls), 2)
        self.assertEqual(
            [call["claim"].action for call in self.process_calls],
            [
                ArabicPrintedPageClaimAction.EXECUTE,
                ArabicPrintedPageClaimAction.EXECUTE,
            ],
        )
        attempt = ArabicPrintedOcrAttempt.objects.get(pk=result.attempt_id)
        self.assertEqual(attempt.status, ArabicPrintedOcrAttempt.Status.COMPLETED)
        self.assertEqual(attempt.expected_page_count, 2)
        self.assertEqual(attempt.missing_page_indices, [])

    def test_reuse_makes_no_loader_or_execute_provider_calls(self):
        texts = {0: PAGE_TEXT_ZERO, 1: PAGE_TEXT_ONE}
        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            first = self._run()
        self.assertEqual(first.outcome, OUTCOME_COMPLETED)
        self.loader_calls.clear()
        self.process_calls.clear()

        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            reused = self._run()

        self.assertEqual(reused.attempt_id, first.attempt_id)
        self.assertEqual(reused.outcome, OUTCOME_COMPLETED)
        self.assertEqual(self.loader_calls, [])
        self.assertEqual(len(self.process_calls), 2)
        self.assertEqual(
            [call["claim"].action for call in self.process_calls],
            [
                ArabicPrintedPageClaimAction.REUSE,
                ArabicPrintedPageClaimAction.REUSE,
            ],
        )
        self.assertEqual(
            [page.assembled_text for page in reused.pages],
            [PAGE_TEXT_ZERO, PAGE_TEXT_ONE],
        )
        for call in self.process_calls:
            self.assertEqual(call["working_image"].jpeg_bytes, b"")
            self.assertEqual(call["working_image"].byte_length, 0)

        self.loader_calls.clear()
        self.process_calls.clear()
        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            tight = self._run(deadline=60.0)
        self.assertEqual(tight.outcome, OUTCOME_COMPLETED)
        self.assertEqual(self.loader_calls, [])
        self.assertEqual(
            [call["claim"].action for call in self.process_calls],
            [
                ArabicPrintedPageClaimAction.REUSE,
                ArabicPrintedPageClaimAction.REUSE,
            ],
        )

    def test_first_ordinary_page_failure_is_partial_and_skips_later_page(self):
        def fake_process(**kwargs):
            self.process_calls.append(kwargs)
            claim = kwargs["claim"]
            persist_arabic_printed_page_failure(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
                failure_message="bands unresolved",
            )
            return _page_result(
                claim,
                outcome=OUTCOME_FAILED,
                failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            )

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", fake_process):
            result = self._run()

        self.assertEqual(result.outcome, OUTCOME_PARTIAL)
        self.assertEqual(result.missing_page_indices, (0, 1))
        self.assertEqual(result.failure_code, "ARABIC_PRINTED_BANDS_UNRESOLVED")
        self.assertEqual([page.page_index for page in result.pages], [0])
        self.assertEqual(self.loader_calls, [0])
        self.assertEqual(len(self.process_calls), 1)
        self.assertEqual(
            missing_pages_for_arabic_printed_attempt(result.attempt_id),
            [0, 1],
        )

    def test_insufficient_budget_persists_deadline_failure_on_first_page_only(self):
        with patch(f"{MODULE}.process_claimed_arabic_printed_page") as process:
            result = self._run(deadline=60.0)

        process.assert_not_called()
        self.assertEqual(self.loader_calls, [])
        self.assertEqual(result.outcome, OUTCOME_PARTIAL)
        self.assertEqual(result.pages, ())
        self.assertEqual(result.missing_page_indices, (0, 1))
        self.assertEqual(result.failure_code, DOCUMENT_FAILURE_DEADLINE)
        pages = list(
            ArabicPrintedOcrPageCheckpoint.objects.filter(
                attempt_id=result.attempt_id
            ).order_by("page_index")
        )
        self.assertEqual(pages[0].status, ArabicPrintedOcrPageCheckpoint.Status.FAILED)
        self.assertEqual(pages[0].failure_code, DOCUMENT_FAILURE_DEADLINE)
        self.assertEqual(
            pages[1].status, ArabicPrintedOcrPageCheckpoint.Status.PLANNING
        )
        self.assertEqual(pages[1].failure_code, "")
        attempt = ArabicPrintedOcrAttempt.objects.get(pk=result.attempt_id)
        self.assertEqual(attempt.status, ArabicPrintedOcrAttempt.Status.PARTIAL)
        self.assertEqual(attempt.missing_page_indices, [0, 1])

    def test_share_below_start_budget_returns_none_and_starts_no_provider(self):
        self.assertIsNone(
            arabic_printed_page_absolute_deadline(
                now=0.0,
                document_deadline_monotonic=209.0,
                unfinished_executable_pages=1,
            )
        )
        with patch(f"{MODULE}.process_claimed_arabic_printed_page") as process:
            result = self._run(deadline=209.0, pages=(self.pages[0],))
        process.assert_not_called()
        self.assertEqual(self.loader_calls, [])
        self.assertEqual(result.outcome, OUTCOME_PARTIAL)
        self.assertEqual(result.failure_code, DOCUMENT_FAILURE_DEADLINE)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt_id=result.attempt_id)
        self.assertEqual(page.status, ArabicPrintedOcrPageCheckpoint.Status.FAILED)
        self.assertEqual(page.failure_code, DOCUMENT_FAILURE_DEADLINE)

    def test_start_budget_of_150_seconds_is_permitted(self):
        deadline = arabic_printed_page_absolute_deadline(
            now=0.0,
            document_deadline_monotonic=210.0,
            unfinished_executable_pages=1,
        )
        self.assertEqual(deadline, 150.0)
        recorded: list[float] = []

        def fake_process(**kwargs):
            recorded.append(kwargs["absolute_deadline_monotonic"])
            claim = kwargs["claim"]
            self._persist_success(claim, PAGE_TEXT_ZERO)
            return _page_result(claim, outcome=OUTCOME_SUCCEEDED, text=PAGE_TEXT_ZERO)

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", fake_process):
            result = self._run(deadline=210.0, pages=(self.pages[0],))
        self.assertEqual(result.outcome, OUTCOME_COMPLETED)
        self.assertEqual(recorded, [150.0])
        self.assertEqual(self.loader_calls, [0])

    def test_deadline_after_first_success_persists_second_page_deadline_failure(self):
        def fake_process(**kwargs):
            self.process_calls.append(kwargs)
            claim = kwargs["claim"]
            self._persist_success(claim, PAGE_TEXT_ZERO)
            self.clock.now = 1_000.0
            return _page_result(claim, outcome=OUTCOME_SUCCEEDED, text=PAGE_TEXT_ZERO)

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", fake_process):
            result = self._run(deadline=1_000.0)

        self.assertEqual(result.outcome, OUTCOME_PARTIAL)
        self.assertEqual([page.page_index for page in result.pages], [0])
        self.assertEqual(result.missing_page_indices, (1,))
        self.assertEqual(result.failure_code, DOCUMENT_FAILURE_DEADLINE)
        self.assertEqual(self.loader_calls, [0])
        self.assertEqual(len(self.process_calls), 1)
        pages = list(
            ArabicPrintedOcrPageCheckpoint.objects.filter(
                attempt_id=result.attempt_id
            ).order_by("page_index")
        )
        self.assertEqual(
            pages[0].status, ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED
        )
        self.assertEqual(pages[1].status, ArabicPrintedOcrPageCheckpoint.Status.FAILED)
        self.assertEqual(pages[1].failure_code, DOCUMENT_FAILURE_DEADLINE)
        attempt = ArabicPrintedOcrAttempt.objects.get(pk=result.attempt_id)
        self.assertEqual(attempt.status, ArabicPrintedOcrAttempt.Status.PARTIAL)

    def test_page_deadlines_use_equal_share_then_240_cap(self):
        too_small = arabic_printed_page_absolute_deadline(
            now=0.0,
            document_deadline_monotonic=260.0,
            unfinished_executable_pages=2,
        )
        self.assertIsNone(too_small)
        share = arabic_printed_page_absolute_deadline(
            now=0.0,
            document_deadline_monotonic=460.0,
            unfinished_executable_pages=2,
        )
        self.assertEqual(share, 200.0)
        capped = arabic_printed_page_absolute_deadline(
            now=0.0,
            document_deadline_monotonic=10_000.0,
            unfinished_executable_pages=1,
        )
        self.assertEqual(capped, 240.0)
        none = arabic_printed_page_absolute_deadline(
            now=0.0,
            document_deadline_monotonic=60.0,
            unfinished_executable_pages=2,
        )
        self.assertIsNone(none)

        recorded: list[float] = []

        def fake_process(**kwargs):
            recorded.append(kwargs["absolute_deadline_monotonic"])
            claim = kwargs["claim"]
            self._persist_success(
                claim, PAGE_TEXT_ZERO if claim.page_index == 0 else PAGE_TEXT_ONE
            )
            return _page_result(
                claim,
                outcome=OUTCOME_SUCCEEDED,
                text=PAGE_TEXT_ZERO if claim.page_index == 0 else PAGE_TEXT_ONE,
            )

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", fake_process):
            result = self._run(deadline=460.0)

        self.assertEqual(result.outcome, OUTCOME_COMPLETED)
        self.assertEqual(recorded, [200.0, 240.0])

    def test_control_errors_propagate_unchanged(self):
        errors = (
            ArabicPrintedCheckpointBusyError("busy"),
            StaleArabicPrintedPageClaimError("stale"),
            ArabicPrintedIdentityMismatchError("identity"),
            ArabicPrintedCheckpointPersistenceRetryableError(
                stage="claim_page", page_index=0
            ),
        )
        for exc in errors:
            with self.subTest(error=type(exc).__name__):
                self.loader_calls.clear()
                with patch(f"{MODULE}.claim_arabic_printed_page", side_effect=exc):
                    with self.assertRaises(type(exc)) as raised:
                        self._run()
                self.assertIs(raised.exception, exc)
                self.assertEqual(self.loader_calls, [])

        def boom(**kwargs):
            raise ArabicPrintedIdentityMismatchError("page identity")

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", boom):
            with self.assertRaises(ArabicPrintedIdentityMismatchError):
                self._run()
        self.assertEqual(self.loader_calls, [0])

    def test_result_repr_is_privacy_safe(self):
        texts = {0: PAGE_TEXT_ZERO, 1: PAGE_TEXT_ONE}
        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            result = self._run()
        blob = repr(result) + str(result)
        for secret in (PAGE_TEXT_ZERO, PAGE_TEXT_ONE, GEMINI_KEY, VISION_KEY):
            self.assertNotIn(secret, blob)
        self.assertEqual(result.pages[0].assembled_text, PAGE_TEXT_ZERO)
        empty = ArabicPrintedBandedDocumentResult(
            attempt_id=1,
            document_id=2,
            outcome=OUTCOME_PARTIAL,
            pages=(
                ArabicPrintedBandedPageResult(
                    checkpoint_id=3,
                    page_index=0,
                    outcome=OUTCOME_SUCCEEDED,
                    assembled_text=PAGE_TEXT_ZERO,
                    page_quality="UNASSISTED",
                    runtime_engine_marker=ENGINE_UNASSISTED,
                    failure_code=None,
                ),
            ),
            missing_page_indices=(1,),
            failure_code=DOCUMENT_FAILURE_DEADLINE,
        )
        self.assertNotIn(PAGE_TEXT_ZERO, repr(empty))
        self.assertNotIn(PAGE_TEXT_ZERO, str(empty))

    def test_attempt_reuse_and_missing_pages_are_durable(self):
        def fail_first(**kwargs):
            claim = kwargs["claim"]
            persist_arabic_printed_page_failure(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
                failure_message="bands unresolved",
            )
            return _page_result(
                claim,
                outcome=OUTCOME_FAILED,
                failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            )

        with patch(f"{MODULE}.process_claimed_arabic_printed_page", fail_first):
            first = self._run()
        self.assertEqual(first.missing_page_indices, (0, 1))

        texts = {0: PAGE_TEXT_ZERO, 1: PAGE_TEXT_ONE}
        self.loader_calls.clear()
        with patch(
            f"{MODULE}.process_claimed_arabic_printed_page", self._fake_success(texts)
        ):
            second = self._run()

        self.assertEqual(second.attempt_id, first.attempt_id)
        self.assertEqual(second.outcome, OUTCOME_COMPLETED)
        self.assertEqual(second.missing_page_indices, ())
        self.assertEqual(self.loader_calls, [0, 1])
        attempt = ArabicPrintedOcrAttempt.objects.get(pk=second.attempt_id)
        self.assertEqual(attempt.status, ArabicPrintedOcrAttempt.Status.COMPLETED)
        self.assertEqual(
            list(
                ArabicPrintedOcrPageCheckpoint.objects.filter(attempt_id=attempt.id)
                .order_by("page_index")
                .values_list("page_index", "status")
            ),
            [
                (0, ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED),
                (1, ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED),
            ],
        )

    def test_loader_failure_fails_closed_without_starting_later_page(self):
        def exploding_loader(page_index: int) -> ArabicPrintedWorkingImage:
            self.loader_calls.append(page_index)
            raise RuntimeError("cannot decode page")

        with patch(f"{MODULE}.process_claimed_arabic_printed_page") as process:
            result = process_arabic_printed_banded_document(
                document_id=self.document.id,
                pages=self.pages,
                load_working_image=exploding_loader,
                gemini_api_key=GEMINI_KEY,
                cloud_vision_api_key=VISION_KEY,
                absolute_deadline_monotonic=1_000.0,
                language_hint=Document.Language.ARABIC,
                text_input_type=Document.TextInputType.PRINTED,
                monotonic_fn=self.clock,
            )

        process.assert_not_called()
        self.assertEqual(self.loader_calls, [0])
        self.assertEqual(result.outcome, OUTCOME_PARTIAL)
        self.assertEqual(result.failure_code, DOCUMENT_FAILURE_IMAGE_LOAD)
        self.assertEqual(result.missing_page_indices, (0, 1))
