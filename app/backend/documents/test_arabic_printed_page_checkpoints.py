from __future__ import annotations

import hashlib
import inspect
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    GeminiOcrAttempt,
    GeminiOcrPageCheckpoint,
)
from documents.services.archive_items import create_ocr_document
from documents.services.arabic_printed_page_checkpoints import (
    ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN,
    ArabicPrintedAttemptIdentity,
    ArabicPrintedBandPlan,
    ArabicPrintedCheckpointBusyError,
    ArabicPrintedCheckpointPersistenceRetryableError,
    ArabicPrintedIdentityMismatchError,
    ArabicPrintedPageClaimAction,
    ArabicPrintedPageSource,
    StaleArabicPrintedPageClaimError,
    apply_arabic_printed_band_diagnostics,
    assemble_arabic_printed_page,
    build_arabic_printed_attempt_identity,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
    mark_arabic_printed_band_cancel_pending,
    missing_pages_for_arabic_printed_attempt,
    persist_arabic_printed_band_failure,
    persist_arabic_printed_band_success,
    persist_arabic_printed_page_failure,
    persist_arabic_printed_vision_plan,
    reserve_arabic_printed_fallback_create,
    reserve_arabic_printed_primary_create,
    reserve_arabic_printed_vision_call,
    select_arabic_printed_band_cloud_vision_low_quality,
)


PAGE_WIDTH = 1000
PAGE_HEIGHT = 2000


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _document(title: str = "Arabic printed checkpoint document") -> Document:
    return create_ocr_document(
        title=title,
        doc_type=Document.DocType.PDF,
        language=Document.Language.ARABIC,
        text_input_type=Document.TextInputType.PRINTED,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.PROCESSING,
        file_s3_key=f"{title}.pdf",
        mime_type="application/pdf",
    )


def _pages(*labels: bytes) -> list[ArabicPrintedPageSource]:
    pages: list[ArabicPrintedPageSource] = []
    for index, label in enumerate(labels):
        pages.append(
            ArabicPrintedPageSource(
                page_index=index,
                mime_type="image/jpeg",
                source_identity="document.pdf",
                source_content_fingerprint=_sha256_bytes(label),
                oriented_image_sha256=_sha256_bytes(label + b"-oriented"),
                oriented_image_width=PAGE_WIDTH,
                oriented_image_height=PAGE_HEIGHT,
            )
        )
    return pages


def _identity(
    pages: list[ArabicPrintedPageSource],
    *,
    jpeg_quality: int = 95,
    prompt_contract_version: str | None = None,
) -> ArabicPrintedAttemptIdentity:
    kwargs = {
        "pages": pages,
        "language_hint": Document.Language.ARABIC,
        "text_input_type": Document.TextInputType.PRINTED,
        "engine_key": "ANTIGRAVITY",
        "prompt_variant": "printed",
        "jpeg_quality": jpeg_quality,
    }
    if prompt_contract_version is not None:
        kwargs["prompt_contract_version"] = prompt_contract_version
    return build_arabic_printed_attempt_identity(**kwargs)


def _band(
    index: int,
    *,
    draft: str = "مسودة",
    x: int = 0,
    y: int | None = None,
    width: int = PAGE_WIDTH,
    height: int = 40,
    crop_mime: str = "image/jpeg",
    crop_byte_length: int = 12,
) -> ArabicPrintedBandPlan:
    if y is None:
        y = index * height
    return ArabicPrintedBandPlan(
        band_index=index,
        rect_x=x,
        rect_y=y,
        rect_width=width,
        rect_height=height,
        crop_mime=crop_mime,
        crop_byte_length=crop_byte_length,
        crop_sha256=_sha256_bytes(f"crop-{index}".encode()),
        vision_draft_text=draft,
        vision_draft_byte_length=len(draft.encode("utf-8")),
        vision_draft_sha256=_sha256_text(draft),
    )


def _bands(*drafts: str) -> list[ArabicPrintedBandPlan]:
    return [_band(index, draft=draft) for index, draft in enumerate(drafts)]


class ArabicPrintedCheckpointIdentityTests(TestCase):
    def setUp(self) -> None:
        self.document = _document()

    def test_identical_contract_reuses_attempt(self):
        identity = _identity(_pages(b"one", b"two"))

        first = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=identity,
        )
        second = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=identity,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(ArabicPrintedOcrAttempt.objects.count(), 1)
        self.assertEqual(first.status, ArabicPrintedOcrAttempt.Status.IN_PROGRESS)
        self.assertEqual(first.missing_page_indices, [0, 1])
        self.assertIsNone(first.completed_at)

    def test_source_and_config_changes_prevent_reuse(self):
        original = _identity(_pages(b"one", b"two"))
        replaced = _identity(_pages(b"one", b"replacement"))
        config_changed = _identity(_pages(b"one", b"two"), jpeg_quality=90)
        prompt_changed = _identity(
            _pages(b"one", b"two"),
            prompt_contract_version="arabic-printed-banded-prompt-v2",
        )

        self.assertNotEqual(original.identity_fingerprint, replaced.identity_fingerprint)
        self.assertNotEqual(original.source_fingerprint, replaced.source_fingerprint)
        self.assertNotEqual(
            original.identity_fingerprint,
            config_changed.identity_fingerprint,
        )
        self.assertNotEqual(
            original.prompt_fingerprint,
            prompt_changed.prompt_fingerprint,
        )

        get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=original,
        )
        get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=replaced,
        )
        self.assertEqual(ArabicPrintedOcrAttempt.objects.count(), 2)

    def test_persisted_component_mismatch_is_identity_error(self):
        identity = _identity(_pages(b"one"))
        attempt = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=identity,
        )
        ArabicPrintedOcrAttempt.objects.filter(pk=attempt.pk).update(
            source_fingerprint="0" * 64
        )

        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            get_or_create_arabic_printed_attempt(
                document_id=self.document.id,
                identity=identity,
            )

    def test_identity_requires_contiguous_zero_based_pages(self):
        pages = _pages(b"one")
        pages[0] = ArabicPrintedPageSource(
            page_index=1,
            mime_type=pages[0].mime_type,
            source_identity=pages[0].source_identity,
            source_content_fingerprint=pages[0].source_content_fingerprint,
            oriented_image_sha256=pages[0].oriented_image_sha256,
            oriented_image_width=pages[0].oriented_image_width,
            oriented_image_height=pages[0].oriented_image_height,
        )

        with self.assertRaisesRegex(ValueError, "contiguous 0-based"):
            _identity(pages)

    def test_ensure_compares_dimensions_strategy_and_ratio(self):
        identity = _identity(_pages(b"one"))
        attempt = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=attempt.id,
            identity=identity,
        )
        ArabicPrintedOcrPageCheckpoint.objects.filter(
            attempt=attempt,
            page_index=0,
        ).update(oriented_image_width=999, banding_strategy="other", max_band_height_ratio="0.200")

        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            ensure_arabic_printed_page_checkpoints(
                attempt_id=attempt.id,
                identity=identity,
            )


class ArabicPrintedCheckpointPersistenceTests(TestCase):
    def setUp(self) -> None:
        self.document = _document()
        self.pages = _pages(b"one", b"two")
        self.identity = _identity(self.pages)
        self.attempt = get_or_create_arabic_printed_attempt(
            document_id=self.document.id,
            identity=self.identity,
        )
        ensure_arabic_printed_page_checkpoints(
            attempt_id=self.attempt.id,
            identity=self.identity,
        )

    def _claim(self, page_index: int):
        return claim_arabic_printed_page(
            attempt_id=self.attempt.id,
            page_index=page_index,
            page_fingerprint=self.identity.page_fingerprints[page_index],
            source_content_fingerprint=(
                self.identity.source_content_fingerprints[page_index]
            ),
            oriented_image_sha256=self.identity.oriented_image_sha256s[page_index],
        )

    def _expire(self, checkpoint_id: int) -> None:
        ArabicPrintedOcrPageCheckpoint.objects.filter(pk=checkpoint_id).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1)
        )

    def _reserve_vision(self, claim) -> None:
        assert claim.lease_token is not None
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )

    def _plan(self, claim, *drafts: str, reserve: bool = True):
        assert claim.lease_token is not None
        if reserve:
            page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
            if page.cloud_vision_call_count == 0:
                self._reserve_vision(claim)
        return persist_arabic_printed_vision_plan(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
            bands=_bands(*drafts),
        )

    def _succeed_unassisted(self, claim, band_index: int, *, text: str):
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
        )
        persist_arabic_printed_band_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
            selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
            transcription_text=text,
            transcription_sha256=_sha256_text(text),
            transcription_byte_length=len(text.encode("utf-8")),
        )

    def _succeed_assisted(self, claim, band_index: int, *, text: str):
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
        )
        persist_arabic_printed_band_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=band_index,
            selected_result=(
                ArabicPrintedOcrBandCheckpoint.SelectedResult.ASSISTED_FALLBACK
            ),
            transcription_text=text,
            transcription_sha256=_sha256_text(text),
            transcription_byte_length=len(text.encode("utf-8")),
        )

    def test_initial_attempt_and_page_state_is_planning(self):
        pages = list(
            ArabicPrintedOcrPageCheckpoint.objects.filter(
                attempt=self.attempt
            ).order_by("page_index")
        )
        self.assertEqual(len(pages), 2)
        self.assertEqual([page.page_index for page in pages], [0, 1])
        for page in pages:
            self.assertEqual(page.status, ArabicPrintedOcrPageCheckpoint.Status.PLANNING)
            self.assertIsNone(page.lease_token)
            self.assertIsNone(page.assembled_text)
            self.assertEqual(page.cloud_vision_call_count, 0)
            self.assertEqual(page.band_count, 0)

    def test_page_and_band_uniqueness(self):
        page = ArabicPrintedOcrPageCheckpoint.objects.get(
            attempt=self.attempt,
            page_index=0,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArabicPrintedOcrPageCheckpoint.objects.create(
                    attempt=self.attempt,
                    page_index=0,
                    page_fingerprint=page.page_fingerprint,
                    source_content_fingerprint=page.source_content_fingerprint,
                    oriented_image_sha256=page.oriented_image_sha256,
                    oriented_image_width=page.oriented_image_width,
                    oriented_image_height=page.oriented_image_height,
                    banding_contract_fingerprint=page.banding_contract_fingerprint,
                    banding_strategy=page.banding_strategy,
                )

        claim = self._claim(0)
        self._plan(claim, "a")
        existing = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArabicPrintedOcrBandCheckpoint.objects.create(
                    page_checkpoint_id=claim.checkpoint_id,
                    band_index=0,
                    rect_x=existing.rect_x,
                    rect_y=existing.rect_y,
                    rect_width=existing.rect_width,
                    rect_height=existing.rect_height,
                    crop_mime=existing.crop_mime,
                    crop_byte_length=existing.crop_byte_length,
                    crop_sha256=existing.crop_sha256,
                    vision_draft_text=existing.vision_draft_text,
                    vision_draft_byte_length=existing.vision_draft_byte_length,
                    vision_draft_sha256=existing.vision_draft_sha256,
                )

    def test_active_lease_reports_busy(self):
        first = self._claim(0)
        self.assertEqual(first.action, ArabicPrintedPageClaimAction.EXECUTE)
        with self.assertRaises(ArabicPrintedCheckpointBusyError):
            self._claim(0)

    def test_expired_lease_can_be_reclaimed(self):
        first = self._claim(0)
        assert first.lease_token is not None
        self._expire(first.checkpoint_id)

        reclaimed = self._claim(0)
        assert reclaimed.lease_token is not None
        self.assertEqual(reclaimed.action, ArabicPrintedPageClaimAction.EXECUTE)
        self.assertNotEqual(reclaimed.lease_token, first.lease_token)

    def test_expired_token_is_stale_before_reclaim(self):
        claim = self._claim(0)
        assert claim.lease_token is not None
        self._expire(claim.checkpoint_id)

        with self.assertRaises(StaleArabicPrintedPageClaimError):
            reserve_arabic_printed_vision_call(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=_bands("draft"),
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_page_failure(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                failure_code="LATE",
                failure_message="expired",
            )

        live = self._claim(0)
        self._plan(live, "draft")
        self._expire(live.checkpoint_id)
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            reserve_arabic_printed_primary_create(
                checkpoint_id=live.checkpoint_id,
                lease_token=live.lease_token,
                band_index=0,
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_band_success(
                checkpoint_id=live.checkpoint_id,
                lease_token=live.lease_token,
                band_index=0,
                selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
                transcription_text="late",
                transcription_sha256=_sha256_text("late"),
                transcription_byte_length=4,
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            assemble_arabic_printed_page(
                checkpoint_id=live.checkpoint_id,
                lease_token=live.lease_token,
            )

    def test_stale_token_cannot_write_plan_band_success_page_success_or_failure(self):
        first = self._claim(0)
        assert first.lease_token is not None
        self._expire(first.checkpoint_id)
        reclaimed = self._claim(0)
        assert reclaimed.lease_token is not None

        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_vision_plan(
                checkpoint_id=first.checkpoint_id,
                lease_token=first.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=_bands("draft"),
            )

        self._plan(reclaimed, "draft")
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_band_success(
                checkpoint_id=first.checkpoint_id,
                lease_token=first.lease_token,
                band_index=0,
                selected_result=(
                    ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED
                ),
                transcription_text="late",
                transcription_sha256=_sha256_text("late"),
                transcription_byte_length=len("late".encode("utf-8")),
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            assemble_arabic_printed_page(
                checkpoint_id=first.checkpoint_id,
                lease_token=first.lease_token,
            )
        with self.assertRaises(StaleArabicPrintedPageClaimError):
            persist_arabic_printed_page_failure(
                checkpoint_id=first.checkpoint_id,
                lease_token=first.lease_token,
                failure_code="LATE",
                failure_message="stale writer",
            )

    def test_vision_reservation_is_exactly_once_and_required_for_plan(self):
        claim = self._claim(0)
        assert claim.lease_token is not None
        with self.assertRaisesRegex(ValueError, "existing Vision reservation"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=_bands("draft"),
            )

        reserved = reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self.assertEqual(reserved.cloud_vision_call_count, 1)
        self.assertEqual(reserved.band_count, 0)
        self.assertEqual(reserved.cloud_vision_response_sha256, "")
        with self.assertRaisesRegex(ValueError, "Ambiguous Vision reservation"):
            reserve_arabic_printed_vision_call(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )

        self._plan(claim, "draft", reserve=False)
        with self.assertRaisesRegex(ValueError, "already reserved"):
            reserve_arabic_printed_vision_call(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )

    def test_ambiguous_vision_reservation_cannot_be_repeated_after_reclaim(self):
        first = self._claim(0)
        self._reserve_vision(first)
        self._expire(first.checkpoint_id)
        reclaimed = self._claim(0)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=reclaimed.checkpoint_id)
        self.assertEqual(page.cloud_vision_call_count, 1)
        self.assertEqual(page.band_count, 0)
        with self.assertRaisesRegex(ValueError, "Ambiguous Vision reservation"):
            reserve_arabic_printed_vision_call(
                checkpoint_id=reclaimed.checkpoint_id,
                lease_token=reclaimed.lease_token,
            )

    def test_vision_plan_and_band_rows_persist_atomically(self):
        claim = self._claim(0)
        self._reserve_vision(claim)
        with patch(
            "documents.services.arabic_printed_page_checkpoints."
            "ArabicPrintedOcrPageCheckpoint.save",
            side_effect=DatabaseError("SECRET_DB"),
        ):
            with self.assertRaises(ArabicPrintedCheckpointPersistenceRetryableError):
                persist_arabic_printed_vision_plan(
                    checkpoint_id=claim.checkpoint_id,
                    lease_token=claim.lease_token,
                    cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                    bands=_bands("one", "two"),
                )

        self.assertEqual(
            ArabicPrintedOcrBandCheckpoint.objects.filter(
                page_checkpoint_id=claim.checkpoint_id
            ).count(),
            0,
        )
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(page.cloud_vision_call_count, 1)

        bands = self._plan(claim, "one", "two", reserve=False)
        page.refresh_from_db()
        self.assertEqual(len(bands), 2)
        self.assertEqual(page.band_count, 2)
        self.assertEqual(page.cloud_vision_call_count, 1)
        self.assertEqual(
            [band.band_index for band in page.band_checkpoints.order_by("band_index")],
            [0, 1],
        )

    def test_invalid_rectangles_and_more_than_six_bands_are_rejected(self):
        claim = self._claim(0)
        self._reserve_vision(claim)
        assert claim.lease_token is not None
        with self.assertRaisesRegex(ValueError, "full width"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=[_band(0, width=0)],
            )
        with self.assertRaisesRegex(ValueError, "full width"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=[_band(0, x=1)],
            )
        with self.assertRaisesRegex(ValueError, "outside"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=[_band(0, y=1990, height=40)],
            )
        with self.assertRaisesRegex(ValueError, "ordered and non-overlapping"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=[_band(0, y=40), _band(1, y=0)],
            )
        with self.assertRaisesRegex(ValueError, "ordered and non-overlapping"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=[_band(0, y=0, height=80), _band(1, y=40, height=40)],
            )
        with self.assertRaisesRegex(ValueError, "1 to 6"):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=_bands("a", "b", "c", "d", "e", "f", "g"),
            )

    def test_existing_plan_identity_comparison_is_complete(self):
        claim = self._claim(0)
        self._plan(claim, "one", "two")
        assert claim.lease_token is not None
        original = _bands("one", "two")
        changed_crop = [
            original[0],
            replace(original[1], crop_sha256="b" * 64, crop_byte_length=99),
        ]
        changed_draft = [
            original[0],
            replace(
                original[1],
                vision_draft_text="other",
                vision_draft_sha256=_sha256_text("other"),
                vision_draft_byte_length=len("other".encode("utf-8")),
            ),
        ]
        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=changed_crop,
            )
        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
                bands=changed_draft,
            )
        with self.assertRaises(ArabicPrintedIdentityMismatchError):
            persist_arabic_printed_vision_plan(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                cloud_vision_response_sha256=_sha256_bytes(b"other-vision"),
                bands=_bands("one", "two"),
            )
        reused = persist_arabic_printed_vision_plan(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
            bands=_bands("one", "two"),
        )
        self.assertEqual(len(reused), 2)

    def test_primary_and_fallback_reservations_are_monotonic(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        first = reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        self.assertEqual(first.create_call_count, 1)
        self.assertEqual(
            first.status,
            ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
        )
        with self.assertRaisesRegex(ValueError, "already reserved"):
            reserve_arabic_printed_primary_create(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        with self.assertRaisesRegex(ValueError, "terminal or cancel-confirmed"):
            reserve_arabic_printed_fallback_create(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )

        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"primary_interaction_id": "primary-1"},
        )
        mark_arabic_printed_band_cancel_pending(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "terminal or cancel-confirmed"):
            reserve_arabic_printed_fallback_create(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"cancel_confirmed_status": "cancelled"},
        )
        fallback = reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        self.assertEqual(fallback.create_call_count, 2)
        with self.assertRaisesRegex(ValueError, "already reserved or primary missing"):
            reserve_arabic_printed_fallback_create(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(page.antigravity_create_count, 2)

    def test_selected_result_source_and_count_invariants(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        with self.assertRaisesRegex(ValueError, "Antigravity selected result"):
            persist_arabic_printed_band_success(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                selected_result="",
                transcription_text="ok",
                transcription_sha256=_sha256_text("ok"),
                transcription_byte_length=2,
            )
        with self.assertRaisesRegex(ValueError, "reserved primary"):
            persist_arabic_printed_band_success(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
                transcription_text="ok",
                transcription_sha256=_sha256_text("ok"),
                transcription_byte_length=2,
            )
        with self.assertRaisesRegex(ValueError, "failed band or confirmed cancellation"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )

        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        failed = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        self.assertEqual(failed.selected_result, "")
        self.assertIsNone(failed.transcription_text)
        self.assertEqual(failed.create_call_count, 1)

        select_arabic_printed_band_cloud_vision_low_quality(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        lq = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        self.assertEqual(
            lq.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )
        self.assertEqual(lq.create_call_count, 1)
        self.assertEqual(lq.transcription_text, "draft")
        self.assertEqual(lq.transcription_sha256, _sha256_text("draft"))

    def test_unsupported_diagnostic_fields_cannot_be_written(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        original = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "Unsupported diagnostic fields"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={
                    "rect_x": 99,
                    "create_call_count": 2,
                    "selected_result": "UNASSISTED",
                    "lease_token": "nope",
                },
            )
        original.refresh_from_db()
        self.assertEqual(original.rect_x, 0)
        self.assertEqual(original.create_call_count, 0)
        self.assertEqual(original.selected_result, "")

    def test_fallback_requires_exact_cancelled_status(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        mark_arabic_printed_band_cancel_pending(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"primary_interaction_id": "primary-1"},
        )
        for status in ("completed", "failed", "unknown", "other", ""):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={"cancel_confirmed_status": status},
            )
            with self.assertRaisesRegex(ValueError, "terminal or cancel-confirmed"):
                reserve_arabic_printed_fallback_create(
                    checkpoint_id=claim.checkpoint_id,
                    lease_token=claim.lease_token,
                    band_index=0,
                )
        persist_arabic_printed_band_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
            transcription_text="primary completed",
            transcription_sha256=_sha256_text("primary completed"),
            transcription_byte_length=len("primary completed".encode("utf-8")),
        )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        self.assertEqual(
            band.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
        )
        self.assertEqual(band.create_call_count, 1)

    def test_missing_interaction_id_prevents_cancel_pending(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "in-flight interaction id"):
            mark_arabic_printed_band_cancel_pending(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        self.assertFalse(band.cancel_attempted)
        self.assertEqual(
            band.status,
            ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING,
        )

        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "in-flight interaction id"):
            mark_arabic_printed_band_cancel_pending(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        band.refresh_from_db()
        self.assertFalse(band.cancel_attempted)
        self.assertEqual(
            band.status,
            ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING,
        )
        self.assertEqual(band.create_call_count, 2)

    def test_fallback_cancellation_keeps_create_count_two(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        cancelled = mark_arabic_printed_band_cancel_pending(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"fallback_interaction_id": "fallback-1"},
        )
        self.assertEqual(
            cancelled.status,
            ArabicPrintedOcrBandCheckpoint.Status.CANCEL_PENDING,
        )
        self.assertEqual(cancelled.create_call_count, 2)
        self.assertTrue(cancelled.cancel_attempted)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        self.assertEqual(page.antigravity_create_count, 2)

    def test_low_quality_rejects_active_attempts_and_uses_stored_draft(self):
        claim = self._claim(0)
        self._plan(claim, "  draft text  ")
        assert claim.lease_token is not None
        params = inspect.signature(
            select_arabic_printed_band_cloud_vision_low_quality
        ).parameters
        self.assertNotIn("transcription_text", params)
        self.assertNotIn("transcription_sha256", params)
        self.assertNotIn("transcription_byte_length", params)
        with self.assertRaises(TypeError):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                transcription_text="attacker",
            )

        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "failed band or confirmed cancellation"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )

        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "failed band or confirmed cancellation"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )

        mark_arabic_printed_band_cancel_pending(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"fallback_interaction_id": "fallback-1"},
        )
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"cancel_confirmed_status": "unknown"},
        )
        with self.assertRaisesRegex(ValueError, "failed band or confirmed cancellation"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"cancel_confirmed_status": "cancelled"},
        )
        selected = select_arabic_printed_band_cloud_vision_low_quality(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        self.assertEqual(selected.transcription_text, "draft text")
        self.assertEqual(
            selected.transcription_sha256,
            _sha256_text("draft text"),
        )
        self.assertEqual(
            selected.selected_result,
            ArabicPrintedOcrBandCheckpoint.SelectedResult.CLOUD_VISION_LOW_QUALITY,
        )

    def test_low_quality_rejects_empty_or_corrupt_stored_draft(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code="PRIMARY_FAILED",
            failure_message="safe",
        )
        ArabicPrintedOcrBandCheckpoint.objects.filter(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        ).update(vision_draft_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "Stored Vision draft hash"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )
        whitespace = "   "
        ArabicPrintedOcrBandCheckpoint.objects.filter(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        ).update(
            vision_draft_text=whitespace,
            vision_draft_sha256=_sha256_text(whitespace),
            vision_draft_byte_length=len(whitespace.encode("utf-8")),
        )
        with self.assertRaisesRegex(ValueError, "Stored Vision draft is empty"):
            select_arabic_printed_band_cloud_vision_low_quality(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
            )

    def test_prior_attempts_are_bounded_and_privacy_safe(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        valid_entries = [
            {
                "kind": "primary",
                "interaction_id": f"id-{index}",
                "provider_status": "failed",
                "failure_type": "timeout",
                "latency_ms": index,
            }
            for index in range(4)
        ]
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"prior_attempts": valid_entries},
        )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id,
            band_index=0,
        )
        self.assertEqual(len(band.prior_attempts), 4)

        with self.assertRaisesRegex(ValueError, "more than four"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={
                    "prior_attempts": [
                        *valid_entries,
                        {"kind": "fallback", "interaction_id": "id-4"},
                    ]
                },
            )
        with self.assertRaisesRegex(ValueError, "sensitive keys"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={
                    "prior_attempts": [{"transcription": "secret", "kind": "primary"}]
                },
            )
        with self.assertRaisesRegex(ValueError, "unsupported keys"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={"prior_attempts": [{"notes": "nope"}]},
            )
        with self.assertRaisesRegex(ValueError, "JSON scalars"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={"prior_attempts": [{"kind": {"nested": True}}]},
            )
        with self.assertRaisesRegex(ValueError, "string values exceed"):
            apply_arabic_printed_band_diagnostics(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                diagnostics={"prior_attempts": [{"kind": "x" * 129}]},
            )
        band.refresh_from_db()
        self.assertEqual(len(band.prior_attempts), 4)
        self.assertEqual(band.prior_attempts[0]["interaction_id"], "id-0")

    def test_success_requires_text_and_matching_hash(self):
        claim = self._claim(0)
        self._plan(claim, "draft")
        assert claim.lease_token is not None
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        with self.assertRaisesRegex(ValueError, "empty"):
            persist_arabic_printed_band_success(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                selected_result=(
                    ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED
                ),
                transcription_text="   ",
                transcription_sha256=_sha256_text("   "),
                transcription_byte_length=3,
            )
        with self.assertRaisesRegex(ValueError, "hash metadata"):
            persist_arabic_printed_band_success(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
                band_index=0,
                selected_result=(
                    ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED
                ),
                transcription_text="ok",
                transcription_sha256="0" * 64,
                transcription_byte_length=2,
            )

    def test_page_cannot_assemble_with_missing_or_failed_bands(self):
        claim = self._claim(0)
        assert claim.lease_token is not None
        self._plan(claim, "one", "two")
        self._succeed_unassisted(claim, 0, text="first")

        with self.assertRaisesRegex(ValueError, "missing or failed"):
            assemble_arabic_printed_page(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )

        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=1,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=1,
            failure_code="BAND_FAILED",
            failure_message="safe",
        )
        with self.assertRaisesRegex(ValueError, "missing or failed"):
            assemble_arabic_printed_page(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )

    def test_successful_assembly_uses_band_order_and_exactly_one_newline(self):
        claim = self._claim(0)
        self._plan(claim, "d0", "d1")
        self._succeed_unassisted(claim, 1, text="second")
        self._succeed_unassisted(claim, 0, text="first")

        assembled = assemble_arabic_printed_page(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self.assertEqual(assembled.assembled_text, "first\nsecond")
        self.assertEqual(
            assembled.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED,
        )
        self.assertEqual(
            assembled.runtime_engine_marker,
            "antigravity-banded:unassisted",
        )
        self.assertLessEqual(len(assembled.runtime_engine_marker), 64)
        self.assertEqual(
            assembled.status,
            ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED,
        )

        reused = self._claim(0)
        self.assertEqual(reused.action, ArabicPrintedPageClaimAction.REUSE)

    def test_mixed_quality_rolls_up_deterministically_within_dtr_length(self):
        claim = self._claim(0)
        self._plan(claim, "d0", "d1")
        self._succeed_unassisted(claim, 0, text="first")
        self._succeed_assisted(claim, 1, text="second")
        first = assemble_arabic_printed_page(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self.assertEqual(
            first.page_quality,
            ArabicPrintedOcrPageCheckpoint.PageQuality.MIXED,
        )
        self.assertRegex(
            first.runtime_engine_marker,
            rf"^antigravity-banded:mixed:[0-9a-f]{{{ARABIC_PRINTED_RUNTIME_ENGINE_DIGEST_LEN}}}$",
        )
        self.assertLessEqual(len(first.runtime_engine_marker), 64)

        ArabicPrintedOcrPageCheckpoint.objects.filter(pk=claim.checkpoint_id).update(
            status=ArabicPrintedOcrPageCheckpoint.Status.RUNNING,
            lease_token=claim.lease_token,
            lease_expires_at=timezone.now() + timedelta(minutes=45),
            assembled_text=None,
            page_quality="",
            runtime_engine_marker="",
            completed_at=None,
        )
        second = assemble_arabic_printed_page(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self.assertEqual(second.runtime_engine_marker, first.runtime_engine_marker)
        self.assertLessEqual(len(second.runtime_engine_marker), 64)

    def test_attempt_missing_page_indices_are_ordered(self):
        first = self._claim(1)
        self._plan(first, "only")
        self._succeed_unassisted(first, 0, text="page-one")
        assemble_arabic_printed_page(
            checkpoint_id=first.checkpoint_id,
            lease_token=first.lease_token,
        )

        failed = self._claim(0)
        missing = persist_arabic_printed_page_failure(
            checkpoint_id=failed.checkpoint_id,
            lease_token=failed.lease_token,
            failure_code="BANDING_UNSAFE",
            failure_message="safe",
        )
        self.assertEqual(missing, [0])
        self.assertEqual(
            missing_pages_for_arabic_printed_attempt(self.attempt.id),
            [0],
        )
        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ArabicPrintedOcrAttempt.Status.PARTIAL)
        self.assertEqual(self.attempt.missing_page_indices, [0])

    def test_final_page_completion_makes_the_attempt_completed(self):
        for page_index, text in ((0, "page-zero"), (1, "page-one")):
            claim = self._claim(page_index)
            self._plan(claim, "draft")
            self._succeed_unassisted(claim, 0, text=text)
            assemble_arabic_printed_page(
                checkpoint_id=claim.checkpoint_id,
                lease_token=claim.lease_token,
            )

        self.attempt.refresh_from_db()
        self.assertEqual(self.attempt.status, ArabicPrintedOcrAttempt.Status.COMPLETED)
        self.assertEqual(self.attempt.missing_page_indices, [])
        self.assertIsNotNone(self.attempt.completed_at)

    def test_persistence_errors_surface_as_retryable_checkpoint_errors(self):
        with patch(
            "documents.services.arabic_printed_page_checkpoints."
            "ArabicPrintedOcrAttempt.objects.get_or_create",
            side_effect=DatabaseError("SECRET_DB"),
        ):
            with self.assertRaises(
                ArabicPrintedCheckpointPersistenceRetryableError
            ) as raised:
                get_or_create_arabic_printed_attempt(
                    document_id=self.document.id,
                    identity=self.identity,
                )
        self.assertEqual(raised.exception.stage, "get_or_create_attempt")
        self.assertNotIn("SECRET_DB", raised.exception.safe_message)

    def test_gemini_checkpoint_behavior_and_schema_remain_untouched(self):
        self.assertEqual(
            {field.name for field in GeminiOcrAttempt._meta.fields},
            {
                "id",
                "document",
                "identity_fingerprint",
                "source_fingerprint",
                "route_fingerprint",
                "prompt_fingerprint",
                "config_fingerprint",
                "prompt_contract_version",
                "model_candidates",
                "expected_page_count",
                "status",
                "missing_page_indices",
                "created_at",
                "updated_at",
                "completed_at",
            },
        )
        self.assertEqual(
            {constraint.name for constraint in GeminiOcrAttempt._meta.constraints},
            {
                "uniq_gem_ocr_attempt_identity",
                "gem_ocr_attempt_page_count_gte_1",
                "gem_ocr_attempt_status_valid",
                "gem_ocr_attempt_completed_shape",
                "gem_ocr_attempt_noncompleted_shape",
            },
        )
        self.assertEqual(
            {constraint.name for constraint in GeminiOcrPageCheckpoint._meta.constraints},
            {
                "uniq_gem_ocr_attempt_page",
                "gem_ocr_page_index_gte_1",
                "gem_ocr_page_status_valid",
                "gem_ocr_page_running_shape",
                "gem_ocr_page_succeeded_shape",
                "gem_ocr_page_failed_shape",
            },
        )
        self.assertEqual(
            ArabicPrintedOcrPageCheckpoint._meta.get_field(
                "runtime_engine_marker"
            ).max_length,
            64,
        )
        self.assertFalse(
            GeminiOcrAttempt.objects.filter(document=self.document).exists()
        )
        self.assertNotEqual(
            ArabicPrintedOcrAttempt._meta.db_table,
            GeminiOcrAttempt._meta.db_table,
        )
        self.assertNotIn(
            "PLANNING",
            GeminiOcrPageCheckpoint.Status.values,
        )
