"""Operator Arabic printed ambiguous-fence resolution: fail-closed, dry-run default."""

from __future__ import annotations

import hashlib
import inspect
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    DocumentTextResult,
)
from documents.services.archive_items import create_ocr_document
from documents.services.antigravity_engine import _band_interaction_id
from documents.services.antigravity_interaction_id import (
    ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN,
    ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN,
    is_antigravity_interaction_id,
)
from documents.services.arabic_printed_ambiguous_fence_resolution import (
    AUDIT_MARKER,
    BAND_FAILURE_FALLBACK_AMBIGUOUS,
    BAND_FAILURE_PRIMARY,
    BAND_FAILURE_PRIMARY_AMBIGUOUS,
    MODE_BIND_INTERACTION,
    MODE_NO_PROVIDER_CALL,
    OPERATOR_AUDIT_SCHEMA,
    PAGE_FAILURE_OPERATOR_RESOLVED,
    PAGE_FAILURE_VISION_AMBIGUOUS,
    ArabicPrintedAmbiguousFenceResolutionError,
    apply_arabic_printed_ambiguous_fence_resolution,
    plan_arabic_printed_ambiguous_fence_resolution,
)
from documents.services.arabic_printed_banded_ocr import (
    BAND_FAILURE_FALLBACK_AMBIGUOUS as ORCH_FALLBACK_AMBIGUOUS,
    BAND_FAILURE_PRIMARY_AMBIGUOUS as ORCH_PRIMARY_AMBIGUOUS,
    PAGE_FAILURE_VISION_AMBIGUOUS as ORCH_VISION_AMBIGUOUS,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedBandPlan,
    ArabicPrintedPageSource,
    apply_arabic_printed_band_diagnostics,
    assemble_arabic_printed_page,
    build_arabic_printed_attempt_identity,
    claim_arabic_printed_page,
    ensure_arabic_printed_page_checkpoints,
    get_or_create_arabic_printed_attempt,
    persist_arabic_printed_band_failure,
    persist_arabic_printed_band_success,
    persist_arabic_printed_page_failure,
    persist_arabic_printed_vision_plan,
    reserve_arabic_printed_fallback_create,
    reserve_arabic_printed_primary_create,
    reserve_arabic_printed_vision_call,
)

PAGE_WIDTH = 1000
PAGE_HEIGHT = 2000
PROVIDER_PATCHES = (
    "documents.services.cloud_vision_document_text.detect_arabic_printed_document_text",
    "documents.services.antigravity_engine.transcribe_band_with_antigravity",
    "documents.services.antigravity_engine.poll_arabic_printed_band_interaction",
    "documents.services.sqs.send_process_document_request_message",
    "documents.services.archive_search_index.sync_archive_item_search_indexes",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _forbid_provider_call(*_args, **_kwargs):
    raise AssertionError("provider, SQS, or search-index must not be called")


def _page_sources(*labels: bytes) -> list[ArabicPrintedPageSource]:
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


def _identity(pages, *, prompt_contract_version=None):
    kwargs = {
        "pages": pages,
        "language_hint": Document.Language.ARABIC,
        "text_input_type": Document.TextInputType.PRINTED,
        "engine_key": "ANTIGRAVITY",
        "prompt_variant": "printed",
    }
    if prompt_contract_version is not None:
        kwargs["prompt_contract_version"] = prompt_contract_version
    return build_arabic_printed_attempt_identity(**kwargs)


def _band_plan(index: int = 0, *, draft: str = "مسودة") -> ArabicPrintedBandPlan:
    height = 40
    return ArabicPrintedBandPlan(
        band_index=index,
        rect_x=0,
        rect_y=index * height,
        rect_width=PAGE_WIDTH,
        rect_height=height,
        crop_mime="image/jpeg",
        crop_byte_length=12,
        crop_sha256=_sha256_bytes(f"crop-{index}".encode()),
        vision_draft_text=draft,
        vision_draft_byte_length=len(draft.encode("utf-8")),
        vision_draft_sha256=_sha256_text(draft),
    )


def _page_identity_snapshot(page: ArabicPrintedOcrPageCheckpoint) -> dict:
    return {
        "page_fingerprint": page.page_fingerprint,
        "source_content_fingerprint": page.source_content_fingerprint,
        "oriented_image_sha256": page.oriented_image_sha256,
        "oriented_image_width": page.oriented_image_width,
        "oriented_image_height": page.oriented_image_height,
        "banding_contract_fingerprint": page.banding_contract_fingerprint,
        "banding_strategy": page.banding_strategy,
        "cloud_vision_response_sha256": page.cloud_vision_response_sha256,
        "band_count": page.band_count,
        "max_band_height_ratio": str(page.max_band_height_ratio),
        "lease_token": page.lease_token,
        "lease_expires_at": page.lease_expires_at,
    }


def _attempt_identity_snapshot(attempt: ArabicPrintedOcrAttempt) -> dict:
    return {
        "identity_fingerprint": attempt.identity_fingerprint,
        "source_fingerprint": attempt.source_fingerprint,
        "route_fingerprint": attempt.route_fingerprint,
        "prompt_fingerprint": attempt.prompt_fingerprint,
        "config_fingerprint": attempt.config_fingerprint,
        "prompt_contract_version": attempt.prompt_contract_version,
    }


def _band_provenance_snapshot(band: ArabicPrintedOcrBandCheckpoint) -> dict:
    return {
        "rect_x": band.rect_x,
        "rect_y": band.rect_y,
        "rect_width": band.rect_width,
        "rect_height": band.rect_height,
        "crop_mime": band.crop_mime,
        "crop_byte_length": band.crop_byte_length,
        "crop_sha256": band.crop_sha256,
        "vision_draft_text": band.vision_draft_text,
        "vision_draft_byte_length": band.vision_draft_byte_length,
        "vision_draft_sha256": band.vision_draft_sha256,
        "prior_attempts": band.prior_attempts,
        "cancel_attempted": band.cancel_attempted,
        "primary_interaction_id": band.primary_interaction_id,
        "fallback_interaction_id": band.fallback_interaction_id,
        "primary_provider_status": band.primary_provider_status,
        "fallback_provider_status": band.fallback_provider_status,
    }


class ArabicPrintedAmbiguousFenceResolutionTests(TransactionTestCase):
    def _document(self, *, title: str = "Arabic fence document") -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.PDF,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key=f"documents/{title}/source/0.jpeg",
            mime_type="image/jpeg",
        )

    def _seed(self, doc: Document, *labels: bytes, prompt_contract_version=None):
        pages = _page_sources(*labels)
        identity = _identity(pages, prompt_contract_version=prompt_contract_version)
        attempt = get_or_create_arabic_printed_attempt(
            document_id=doc.id,
            identity=identity,
        )
        ensure_arabic_printed_page_checkpoints(attempt_id=attempt.id, identity=identity)
        return identity, attempt

    def _claim(self, attempt, identity, page_index: int = 0):
        return claim_arabic_printed_page(
            attempt_id=attempt.id,
            page_index=page_index,
            page_fingerprint=identity.page_fingerprints[page_index],
            source_content_fingerprint=identity.source_content_fingerprints[page_index],
            oriented_image_sha256=identity.oriented_image_sha256s[page_index],
        )

    def _fence_vision(self, attempt, identity, page_index: int = 0):
        claim = self._claim(attempt, identity, page_index)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code=PAGE_FAILURE_VISION_AMBIGUOUS,
            failure_message="vision reserved without a durable plan",
        )
        return claim

    def _persist_plan(self, claim, *drafts: str):
        persist_arabic_printed_vision_plan(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            cloud_vision_response_sha256=_sha256_bytes(b"vision-response"),
            bands=[
                _band_plan(index, draft=draft) for index, draft in enumerate(drafts)
            ],
        )

    def _fence_primary(self, attempt, identity, page_index: int = 0):
        claim = self._claim(attempt, identity, page_index)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self._persist_plan(claim, "مسودة")
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
            failure_message="primary reserved without an interaction id",
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            failure_message="band_index=0 did not reach success",
        )
        return claim

    def _fence_fallback(self, attempt, identity, page_index: int = 0):
        claim = self._claim(attempt, identity, page_index)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self._persist_plan(claim, "مسودة")
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        apply_arabic_printed_band_diagnostics(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            diagnostics={"primary_interaction_id": "ix-primary-known"},
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code=BAND_FAILURE_PRIMARY,
            failure_message="primary rejected",
        )
        reserve_arabic_printed_fallback_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            failure_code=BAND_FAILURE_FALLBACK_AMBIGUOUS,
            failure_message="fallback reserved without an interaction id",
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            failure_message="band_index=0 did not reach success",
        )
        return claim

    def _provider_patches(self):
        return [
            patch(target, side_effect=_forbid_provider_call)
            for target in PROVIDER_PATCHES
        ]

    def _kwargs(self, doc, **overrides):
        payload = {
            "document_id": doc.id,
            "page_index": 0,
            "mode": MODE_NO_PROVIDER_CALL,
            "expected_failure_code": PAGE_FAILURE_VISION_AMBIGUOUS,
            "reason": "operator verified no Vision request",
        }
        payload.update(overrides)
        return payload

    def test_failure_codes_match_orchestrator(self):
        self.assertEqual(PAGE_FAILURE_VISION_AMBIGUOUS, ORCH_VISION_AMBIGUOUS)
        self.assertEqual(BAND_FAILURE_PRIMARY_AMBIGUOUS, ORCH_PRIMARY_AMBIGUOUS)
        self.assertEqual(BAND_FAILURE_FALLBACK_AMBIGUOUS, ORCH_FALLBACK_AMBIGUOUS)

    def test_dry_run_makes_no_writes(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"only")
        self._fence_vision(attempt, identity)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        before = (
            page.cloud_vision_call_count,
            page.failure_code,
            page.failure_message,
            page.updated_at,
        )
        patches = self._provider_patches()
        for patched in patches:
            patched.start()
        try:
            plan = plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
            stdout = StringIO()
            call_command(
                "resolve_arabic_printed_ambiguous_fence",
                str(doc.id),
                "--page-index",
                "0",
                "--mode",
                MODE_NO_PROVIDER_CALL,
                "--expected-failure-code",
                PAGE_FAILURE_VISION_AMBIGUOUS,
                "--reason",
                "operator verified no Vision request",
                stdout=stdout,
            )
        finally:
            for patched in patches:
                patched.stop()
        page.refresh_from_db()
        self.assertFalse(plan.applied)
        self.assertIn("dry-run", stdout.getvalue())
        self.assertIn("no changes made", stdout.getvalue())
        self.assertIn("operator_resolution_audit", stdout.getvalue())
        self.assertEqual(
            (
                page.cloud_vision_call_count,
                page.failure_code,
                page.failure_message,
                page.updated_at,
            ),
            before,
        )

    def test_wrong_document_page_band_fails_closed(self):
        doc = self._document(title="fenced")
        other = self._document(title="other")
        identity, attempt = self._seed(doc, b"page-a", b"page-b")
        self._seed(other, b"other")
        self._fence_vision(attempt, identity, 0)
        self._fence_primary(attempt, identity, 1)

        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    other, expected_failure_code=PAGE_FAILURE_VISION_AMBIGUOUS
                )
            )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc,
                    page_index=1,
                    expected_failure_code=PAGE_FAILURE_VISION_AMBIGUOUS,
                )
            )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc,
                    page_index=1,
                    band_index=3,
                    expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                )
            )

    def test_current_contract_mismatch_fails_closed(self):
        doc = self._document()
        identity, attempt = self._seed(
            doc,
            b"stale",
            prompt_contract_version="arabic-printed-banded-prompt-stale",
        )
        self._fence_vision(attempt, identity)
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("current-contract", str(ctx.exception))

    def test_live_lease_fails_closed(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"leased")
        self._claim(attempt, identity, 0)
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("live page lease", str(ctx.exception))

    def test_no_provider_call_vision_success_preserves_identity(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"vision")
        self._fence_vision(attempt, identity)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        page_before = _page_identity_snapshot(page)
        attempt_before = _attempt_identity_snapshot(attempt)
        patches = self._provider_patches()
        for patched in patches:
            patched.start()
        try:
            plan = apply_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        finally:
            for patched in patches:
                patched.stop()
        page.refresh_from_db()
        attempt.refresh_from_db()
        self.assertTrue(plan.applied)
        self.assertEqual(page.cloud_vision_call_count, 0)
        self.assertEqual(page.failure_code, PAGE_FAILURE_OPERATOR_RESOLVED)
        self.assertEqual(
            page.operator_resolution_audit["schema"], OPERATOR_AUDIT_SCHEMA
        )
        self.assertEqual(
            page.operator_resolution_audit["events"][0]["mode"], MODE_NO_PROVIDER_CALL
        )
        self.assertEqual(
            page.operator_resolution_audit["events"][0]["target"], "vision"
        )
        self.assertIn(
            "operator verified no Vision request",
            page.operator_resolution_audit["events"][0]["reason"],
        )
        self.assertEqual(_page_identity_snapshot(page), page_before)
        self.assertEqual(_attempt_identity_snapshot(attempt), attempt_before)

    def test_no_provider_call_primary_success_when_safe(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"primary")
        self._fence_primary(attempt, identity)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        band = page.band_checkpoints.get(band_index=0)
        provenance = _band_provenance_snapshot(band)
        vision_count = page.cloud_vision_call_count
        vision_hash = page.cloud_vision_response_sha256
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc,
                band_index=0,
                expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                reason="operator verified primary create never sent",
            )
        )
        band.refresh_from_db()
        page.refresh_from_db()
        self.assertEqual(band.create_call_count, 0)
        self.assertEqual(band.status, ArabicPrintedOcrBandCheckpoint.Status.PENDING)
        self.assertEqual(band.failure_code, "")
        self.assertEqual(page.cloud_vision_call_count, vision_count)
        self.assertEqual(page.antigravity_create_count, 0)
        self.assertIn(AUDIT_MARKER, band.primary_safe_diagnostics)
        after = _band_provenance_snapshot(band)
        self.assertEqual(after["crop_sha256"], provenance["crop_sha256"])
        self.assertEqual(
            after["vision_draft_sha256"], provenance["vision_draft_sha256"]
        )
        self.assertEqual(after["primary_interaction_id"], "")
        self.assertEqual(after["fallback_interaction_id"], "")
        self.assertEqual(page.cloud_vision_response_sha256, vision_hash)

    def test_no_provider_call_primary_unsafe_with_interaction_id(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"unsafe-primary")
        claim = self._fence_primary(attempt, identity)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.primary_interaction_id = "ix-already"
        band.save(update_fields=["primary_interaction_id", "updated_at"])
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc,
                    band_index=0,
                    expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                )
            )

    def test_no_provider_call_fallback_success_when_safe(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"fallback")
        self._fence_fallback(attempt, identity)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint__attempt=attempt, band_index=0
        )
        primary_id = band.primary_interaction_id
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc,
                band_index=0,
                expected_failure_code=BAND_FAILURE_FALLBACK_AMBIGUOUS,
                reason="operator verified fallback create never sent",
            )
        )
        band.refresh_from_db()
        self.assertEqual(band.create_call_count, 1)
        self.assertEqual(band.status, ArabicPrintedOcrBandCheckpoint.Status.FAILED)
        self.assertEqual(band.failure_code, BAND_FAILURE_PRIMARY)
        self.assertEqual(band.primary_interaction_id, primary_id)
        self.assertEqual(band.fallback_interaction_id, "")
        self.assertIn(AUDIT_MARKER, band.fallback_safe_diagnostics)

    def test_bind_interaction_primary_and_fallback(self):
        doc = self._document(title="bind-primary")
        identity, attempt = self._seed(doc, b"bind-p")
        self._fence_primary(attempt, identity)
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc,
                mode=MODE_BIND_INTERACTION,
                band_index=0,
                expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                interaction_id="ix-recovered-primary",
                reason="provider console shows this primary interaction",
            )
        )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint__attempt=attempt, band_index=0
        )
        self.assertEqual(band.primary_interaction_id, "ix-recovered-primary")
        self.assertEqual(
            band.status, ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
        )
        self.assertEqual(band.create_call_count, 1)
        self.assertEqual(band.failure_code, "")

        doc_f = self._document(title="bind-fallback")
        identity_f, attempt_f = self._seed(doc_f, b"bind-f")
        self._fence_fallback(attempt_f, identity_f)
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc_f,
                mode=MODE_BIND_INTERACTION,
                band_index=0,
                expected_failure_code=BAND_FAILURE_FALLBACK_AMBIGUOUS,
                interaction_id="ix-recovered-fallback",
                reason="provider console shows this fallback interaction",
            )
        )
        band_f = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint__attempt=attempt_f, band_index=0
        )
        self.assertEqual(band_f.fallback_interaction_id, "ix-recovered-fallback")
        self.assertEqual(band_f.primary_interaction_id, "ix-primary-known")
        self.assertEqual(
            band_f.status, ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING
        )
        self.assertEqual(band_f.create_call_count, 2)

    def test_conflicting_existing_interaction_id_fails_closed(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"conflict")
        claim = self._fence_primary(attempt, identity)
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint_id=claim.checkpoint_id, band_index=0
        )
        band.primary_interaction_id = "ix-other"
        band.save(update_fields=["primary_interaction_id", "updated_at"])
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc,
                    mode=MODE_BIND_INTERACTION,
                    band_index=0,
                    expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                    interaction_id="ix-recovered-primary",
                    reason="bind",
                )
            )
        self.assertIn("Conflicting primary_interaction_id", str(ctx.exception))

    def test_succeeded_page_and_band_cannot_be_changed(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"ok")
        claim = self._claim(attempt, identity, 0)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        self._persist_plan(claim, "مسودة")
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
        )
        text = "ok"
        persist_arabic_printed_band_success(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
            band_index=0,
            selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
            transcription_text=text,
            transcription_sha256=_sha256_text(text),
            transcription_byte_length=len(text.encode("utf-8")),
        )
        assemble_arabic_printed_page(
            checkpoint_id=claim.checkpoint_id,
            lease_token=claim.lease_token,
        )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("SUCCEEDED", str(ctx.exception))

        doc_b = self._document(title="succeeded-band")
        identity_b, attempt_b = self._seed(doc_b, b"band-ok")
        claim_b = self._claim(attempt_b, identity_b, 0)
        reserve_arabic_printed_vision_call(
            checkpoint_id=claim_b.checkpoint_id,
            lease_token=claim_b.lease_token,
        )
        self._persist_plan(claim_b, "مسودة")
        reserve_arabic_printed_primary_create(
            checkpoint_id=claim_b.checkpoint_id,
            lease_token=claim_b.lease_token,
            band_index=0,
        )
        persist_arabic_printed_band_success(
            checkpoint_id=claim_b.checkpoint_id,
            lease_token=claim_b.lease_token,
            band_index=0,
            selected_result=ArabicPrintedOcrBandCheckpoint.SelectedResult.UNASSISTED,
            transcription_text=text,
            transcription_sha256=_sha256_text(text),
            transcription_byte_length=len(text.encode("utf-8")),
        )
        persist_arabic_printed_page_failure(
            checkpoint_id=claim_b.checkpoint_id,
            lease_token=claim_b.lease_token,
            failure_code="ARABIC_PRINTED_BANDS_UNRESOLVED",
            failure_message="unfinished sibling",
        )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc_b,
                    band_index=0,
                    expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                )
            )
        self.assertIn("SUCCEEDED", str(ctx.exception))

    def test_verified_text_result_fails_closed(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"verified")
        self._fence_vision(attempt, identity)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity-banded:unassisted",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified",
        )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("VERIFIED", str(ctx.exception))

    def test_apply_command_writes_and_does_not_call_providers(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"cmd")
        self._fence_vision(attempt, identity)
        patches = self._provider_patches()
        stdout = StringIO()
        for patched in patches:
            patched.start()
        try:
            call_command(
                "resolve_arabic_printed_ambiguous_fence",
                str(doc.id),
                "--page-index",
                "0",
                "--mode",
                MODE_NO_PROVIDER_CALL,
                "--expected-failure-code",
                PAGE_FAILURE_VISION_AMBIGUOUS,
                "--reason",
                "operator verified no Vision request",
                "--apply",
                stdout=stdout,
            )
        finally:
            for patched in patches:
                patched.stop()
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        self.assertEqual(page.cloud_vision_call_count, 0)
        self.assertIn("apply", stdout.getvalue())
        self.assertIn("operator_resolution_audit", stdout.getvalue())

    def test_expired_lease_on_other_page_does_not_block(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"fenced", b"expired")
        self._fence_vision(attempt, identity, 0)
        claim = self._claim(attempt, identity, 1)
        page = ArabicPrintedOcrPageCheckpoint.objects.get(pk=claim.checkpoint_id)
        page.lease_expires_at = timezone.now() - timedelta(minutes=1)
        page.save(update_fields=["lease_expires_at", "updated_at"])
        plan = plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertEqual(plan.target, "vision")
        self.assertFalse(plan.applied)

    def test_command_apply_fail_closed_raises_command_error(self):
        with self.assertRaises(CommandError):
            call_command(
                "resolve_arabic_printed_ambiguous_fence",
                "999999",
                "--page-index",
                "0",
                "--mode",
                MODE_NO_PROVIDER_CALL,
                "--expected-failure-code",
                PAGE_FAILURE_VISION_AMBIGUOUS,
                "--reason",
                "missing document",
            )

    def test_apply_revalidates_under_locks_and_does_not_use_unlocked_plan(self):
        source = inspect.getsource(apply_arabic_printed_ambiguous_fence_resolution)
        self.assertNotIn("plan_arabic_printed_ambiguous_fence_resolution", source)
        self.assertIn("for_update=True", source)
        self.assertIn("_load_resolution_context", source)
        self.assertIn("_build_plan", source)

        doc = self._document()
        identity, attempt = self._seed(doc, b"lock-order")
        self._fence_vision(attempt, identity)
        with CaptureQueriesContext(connection) as captured:
            apply_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        order = _select_for_update_model_order(captured.captured_queries)
        self.assertEqual(
            order[:4],
            [
                "document",
                "documenttextresult",
                "arabicprintedocrattempt",
                "arabicprintedocrpagecheckpoint",
            ],
        )
        self.assertIn("arabicprintedocrbandcheckpoint", order)

    def test_apply_fails_closed_if_verified_after_unlocked_plan(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"race-verified")
        self._fence_vision(attempt, identity)
        plan = plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertFalse(plan.applied)
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="antigravity-banded:unassisted",
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified after plan",
        )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            apply_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("VERIFIED", str(ctx.exception))
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        self.assertEqual(page.cloud_vision_call_count, 1)
        self.assertEqual(page.failure_code, PAGE_FAILURE_VISION_AMBIGUOUS)

    def test_apply_fails_closed_if_lease_becomes_live_after_unlocked_plan(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"race-lease-a", b"race-lease-b")
        self._fence_vision(attempt, identity, 0)
        plan = plan_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertFalse(plan.applied)
        self._claim(attempt, identity, 1)
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError) as ctx:
            apply_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        self.assertIn("live page lease", str(ctx.exception))
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        self.assertEqual(page.cloud_vision_call_count, 1)

    def test_vision_operator_audit_survives_page_claim(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"audit-claim")
        self._fence_vision(attempt, identity)
        apply_arabic_printed_ambiguous_fence_resolution(**self._kwargs(doc))
        page = ArabicPrintedOcrPageCheckpoint.objects.get(attempt=attempt, page_index=0)
        audit_before = page.operator_resolution_audit
        self._claim(attempt, identity, 0)
        page.refresh_from_db()
        self.assertEqual(page.status, ArabicPrintedOcrPageCheckpoint.Status.RUNNING)
        self.assertEqual(page.failure_code, "")
        self.assertEqual(page.failure_message, "")
        self.assertEqual(page.operator_resolution_audit, audit_before)
        self.assertEqual(audit_before["schema"], OPERATOR_AUDIT_SCHEMA)

    def test_band_safe_diagnostics_audit_survives_page_claim(self):
        doc = self._document()
        identity, attempt = self._seed(doc, b"band-audit-claim")
        self._fence_primary(attempt, identity)
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc,
                band_index=0,
                expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                reason="operator verified primary create never sent",
            )
        )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint__attempt=attempt, band_index=0
        )
        diagnostics = band.primary_safe_diagnostics
        self.assertIn(AUDIT_MARKER, diagnostics)
        self._claim(attempt, identity, 0)
        band.refresh_from_db()
        self.assertEqual(band.primary_safe_diagnostics, diagnostics)
        self.assertEqual(band.create_call_count, 0)

    def test_bind_interaction_id_matches_engine_contract(self):
        charset_invalid = (
            "ix 1",
            " ix-1",
            "ix-1\n",
            "ix@host",
            "ix/1",
            "ix-אב",
            "ix-é",
        )
        for invalid in charset_invalid:
            with self.subTest(invalid=invalid):
                self.assertIsNone(_band_interaction_id(invalid))
                self.assertFalse(is_antigravity_interaction_id(invalid))

        doc = self._document()
        identity, attempt = self._seed(doc, b"ix-valid")
        self._fence_primary(attempt, identity)
        for invalid in charset_invalid:
            with self.subTest(plan_invalid=invalid):
                with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
                    plan_arabic_printed_ambiguous_fence_resolution(
                        **self._kwargs(
                            doc,
                            mode=MODE_BIND_INTERACTION,
                            band_index=0,
                            expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                            interaction_id=invalid,
                            reason="reject invalid id",
                        )
                    )
        too_long = "a" * (ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN + 1)
        self.assertEqual(
            _band_interaction_id(too_long),
            too_long,
        )
        with self.assertRaises(ArabicPrintedAmbiguousFenceResolutionError):
            plan_arabic_printed_ambiguous_fence_resolution(
                **self._kwargs(
                    doc,
                    mode=MODE_BIND_INTERACTION,
                    band_index=0,
                    expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                    interaction_id=too_long,
                    reason="reject oversize stored id",
                )
            )

        valid = "ix-abc_123:ok.-9"
        self.assertTrue(
            is_antigravity_interaction_id(
                valid, max_length=ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN
            )
        )
        self.assertEqual(_band_interaction_id(valid), valid)
        apply_arabic_printed_ambiguous_fence_resolution(
            **self._kwargs(
                doc,
                mode=MODE_BIND_INTERACTION,
                band_index=0,
                expected_failure_code=BAND_FAILURE_PRIMARY_AMBIGUOUS,
                interaction_id=valid,
                reason="bind valid punctuation",
            )
        )
        band = ArabicPrintedOcrBandCheckpoint.objects.get(
            page_checkpoint__attempt=attempt, band_index=0
        )
        self.assertEqual(band.primary_interaction_id, valid)

        stored_ok = "a" * ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN
        engine_only = "a" * (ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN + 1)
        self.assertTrue(
            is_antigravity_interaction_id(
                engine_only, max_length=ANTIGRAVITY_INTERACTION_ID_ENGINE_MAX_LEN
            )
        )
        self.assertFalse(
            is_antigravity_interaction_id(
                engine_only, max_length=ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN
            )
        )
        self.assertTrue(
            is_antigravity_interaction_id(
                stored_ok, max_length=ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN
            )
        )


def _select_for_update_model_order(captured_queries):
    order = []
    for query in captured_queries:
        if "FOR UPDATE" not in query["sql"].upper():
            continue
        sql = query["sql"].replace("`", '"').lower()
        if "documents_documenttextresult" in sql:
            order.append("documenttextresult")
        elif "documents_arabicprintedocrbandcheckpoint" in sql:
            order.append("arabicprintedocrbandcheckpoint")
        elif "documents_arabicprintedocrpagecheckpoint" in sql:
            order.append("arabicprintedocrpagecheckpoint")
        elif "documents_arabicprintedocrattempt" in sql:
            order.append("arabicprintedocrattempt")
        elif "documents_document" in sql:
            order.append("document")
    return order
