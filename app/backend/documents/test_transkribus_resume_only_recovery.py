"""Staff-authorized Transkribus resume-only recovery."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.db import connection, connections
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    ProcessDocumentRequest,
    TranskribusRun,
    TranskribusRunAutomaticSnapshot,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services import transkribus_engine as tr
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.process_document_request_staff_recovery import (
    STAFF_ABANDONED_FAILURE_CODE,
    ProcessDocumentRequestStaffAbandonError,
    abandon_process_document_request,
)
from documents.services.process_document_request_worker import (
    terminalize_parked_process_document_after_staff_resume,
)
from documents.services.transkribus_engine import (
    PylaiaTranscriptionOutcome,
    SelectedTranscriptPage,
)
from documents.services.transkribus_local_completion import (
    associate_run_with_automatic_snapshot,
    complete_transkribus_local_success,
    get_run_automatic_snapshot_association,
    store_automatic_snapshot_from_selected_pages,
)
from documents.services.transkribus_resume_only_recovery import (
    TranskribusResumeOnlyRecoveryError,
    _associate_snapshot_under_staff_resume_lock,
    apply_transkribus_resume_only_recovery,
    assess_transkribus_resume_only_recovery,
)
from documents.services.transkribus_snapshot_pages import (
    snapshot_pages_from_upload_mapping,
)
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION,
    compute_sha256_hex,
)


COLLECTION_ID = "2339723"
MODEL_ID = "615449"
REMOTE_DOC_ID = "19713320"
RECOGNITION_JOB_ID = "32239972"
ENGINE = f"transkribus-pylaia:{MODEL_ID}"
PAGE_COUNT = 16
PAGES_QUERY = f"1-{PAGE_COUNT}"
PAGE_MAP = {i: i for i in range(1, PAGE_COUNT + 1)}


def _finished_job(**overrides) -> dict:
    payload = {
        "jobId": int(RECOGNITION_JOB_ID),
        "state": "FINISHED",
        "success": True,
        "nrOfErrors": 0,
    }
    payload.update(overrides)
    return payload


def _job_fetcher(payload: dict | None = None, *, error: Exception | None = None):
    job = payload if payload is not None else _finished_job()

    def _fetch(job_id: str) -> dict:
        if error is not None:
            raise error
        assert job_id == RECOGNITION_JOB_ID
        return job

    return _fetch


def _page_xml(line: str = "שלום") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        '  <Page imageFilename="p.png" imageWidth="100" imageHeight="100">\n'
        '    <TextRegion id="r1"><TextLine id="l1">'
        f"<TextEquiv><Unicode>{line}</Unicode></TextEquiv>"
        "</TextLine></TextRegion></Page></PcGts>"
    ).encode("utf-8")


def _selected_pages(count: int = PAGE_COUNT, *, text: str = "שלום") -> tuple:
    pages = []
    for page_nr in range(1, count + 1):
        pages.append(
            SelectedTranscriptPage(
                page_nr=page_nr,
                transcript_ts_id=f"ts-{page_nr}",
                page_xml=_page_xml(f"{text}-{page_nr}"),
                url=f"http://example.test/page-{page_nr}.xml",
                provider_page_id=1000 + page_nr,
                remote_transcript_status="DONE",
            )
        )
    return tuple(pages)


def _outcome(
    *, page_count: int = PAGE_COUNT, text: str = "שלום"
) -> PylaiaTranscriptionOutcome:
    pages = _selected_pages(page_count, text=text)
    assembled = "\n\n".join(f"{text}-{p.page_nr}" for p in pages)
    return PylaiaTranscriptionOutcome(
        text=assembled,
        review_reasons=[],
        recognition_job_id=RECOGNITION_JOB_ID,
        selected_pages=pages,
    )


def _hebrew_doc(**kwargs) -> Document:
    defaults = {
        "title": "Resume-only recovery",
        "doc_type": Document.DocType.IMAGE,
        "language": Document.Language.HEBREW,
        "text_input_type": Document.TextInputType.HANDWRITTEN,
        "upload_status": Document.UploadStatus.UPLOADED,
        "processing_state_user": Document.ProcessingState.RECOVERY_REQUIRED,
        "file_s3_key": f"resume-{uuid.uuid4().hex}.png",
        "mime_type": "image/png",
    }
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _failed_source_run(document: Document) -> TranskribusRun:
    return TranskribusRun.objects.create(
        document=document,
        status=TranskribusRun.Status.FAILED,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id=COLLECTION_ID,
        model_id=MODEL_ID,
        remote_doc_id=REMOTE_DOC_ID,
        pages_query=PAGES_QUERY,
        recognition_job_id="old-failed-job",
        page_index_to_page_nr=dict(PAGE_MAP),
        error_code="TRANSKRIBUS_FAILED",
    )


def _rr_request(
    document: Document,
    *,
    source_run: TranskribusRun,
    **overrides,
) -> ProcessDocumentRequest:
    now = timezone.now()
    values = {
        "document": document,
        "status": ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
        "operation": ProcessDocumentRequest.Operation.OCR,
        "origin": ProcessDocumentRequest.Origin.OCR_REPROCESS,
        "ocr_retry_mode": (
            ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY
        ),
        "source_transkribus_run": source_run,
        "lease_token": uuid.uuid4(),
        "lease_expires_at": None,
        "started_at": now - timedelta(hours=1),
        "completed_at": None,
    }
    values.update(overrides)
    return ProcessDocumentRequest.objects.create(**values)


def _current_run(document: Document, **overrides) -> TranskribusRun:
    values = {
        "document": document,
        "status": TranskribusRun.Status.RECOGNITION_STARTED,
        "mode": TranskribusRun.Mode.UPLOAD_CREATED,
        "collection_id": COLLECTION_ID,
        "model_id": MODEL_ID,
        "remote_doc_id": REMOTE_DOC_ID,
        "pages_query": PAGES_QUERY,
        "recognition_job_id": RECOGNITION_JOB_ID,
        "page_index_to_page_nr": dict(PAGE_MAP),
    }
    values.update(overrides)
    return TranskribusRun.objects.create(**values)


def _failed_dtrs(document: Document) -> None:
    # Matches document 521 history: prior failed OCR rows must not block resume.
    for engine in ("ocr-dispatch", "gemini-failed-marker"):
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="",
            error_code="OCR_FAILED",
        )


def _doc521_shape():
    document = _hebrew_doc()
    source_run = _failed_source_run(document)
    request = _rr_request(document, source_run=source_run)
    run = _current_run(document)
    _failed_dtrs(document)
    return document, request, run, source_run


def _ready_snapshot(
    *,
    document: Document,
    run: TranskribusRun,
    text: str = "שלום",
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{run.pk}:{text}:{uuid.uuid4().hex}"
    snap = TranskribusTranscriptSnapshot.objects.create(
        document=document,
        transkribus_run=run,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        remote_doc_id=str(run.remote_doc_id or ""),
        collection_id=str(run.collection_id or ""),
        model_id=str(run.model_id or ""),
        recognition_job_id=str(run.recognition_job_id or ""),
        parser_version=PARSER_VERSION,
        provider_identity_fingerprint=compute_sha256_hex(f"prov:{unique}"),
        raw_xml_fingerprint=compute_sha256_hex(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=compute_sha256_hex(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=False,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )
    associate_run_with_automatic_snapshot(
        run=run,
        snapshot=snap,
        mapping_trusted=True,
        review_reasons=[],
    )
    return snap


def _route() -> OcrRouteConfig:
    return OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    )


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class StaffResumeOnlyRecoveryTests(TestCase):
    def test_doc521_shape_fetch_and_complete_sixteen_pages(self):
        document, request, run, source = _doc521_shape()
        self.assertEqual(run.pages_query, PAGES_QUERY)
        self.assertEqual(len(run.page_index_to_page_nr), PAGE_COUNT)
        self.assertEqual(source.pages_query, PAGES_QUERY)
        self.assertEqual(
            request.ocr_retry_mode,
            ProcessDocumentRequest.OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
        )
        self.assertEqual(request.origin, ProcessDocumentRequest.Origin.OCR_REPROCESS)
        self.assertEqual(request.source_transkribus_run_id, source.pk)
        token = request.lease_token

        pre = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(),
        )
        self.assertEqual(pre.phase, "FETCH_AND_COMPLETE")
        self.assertTrue(pre.apply_permitted)

        with (
            patch(
                "documents.services.transkribus_snapshot_storage.put_object_bytes",
                return_value=None,
            ),
            patch(
                "documents.services.transkribus_engine.start_pylaia_recognition"
            ) as start_recognition,
        ):
            result = apply_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
                job_fetcher=_job_fetcher(),
                page_completer=lambda _run: _outcome(page_count=PAGE_COUNT),
            )

        start_recognition.assert_not_called()
        self.assertEqual(result.phase, "ALREADY_DONE")

        request.refresh_from_db()
        document.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertIsNone(request.lease_token)
        self.assertNotEqual(request.lease_token, token)
        self.assertEqual(document.processing_state_user, Document.ProcessingState.READY)
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)

        source_text = DocumentTextResult.objects.get(
            document=document,
            engine=ENGINE,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        hebrew = DocumentTextResult.objects.get(
            document=document,
            engine=ENGINE,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        self.assertEqual(source_text.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            source_text.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(hebrew.text, source_text.text)
        self.assertIn("AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW", source_text.review_reasons)
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(
                text_result=source_text
            ).exists()
        )
        assoc = get_run_automatic_snapshot_association(run)
        self.assertIsNotNone(assoc)
        self.assertEqual(assoc.snapshot.pages.count(), PAGE_COUNT)
        self.assertEqual(
            DocumentTextResult.objects.filter(
                document=document, status=DocumentTextResult.Status.FAILED
            ).count(),
            2,
        )

    def test_page_count_mismatch_against_sixteen_page_mapping_fails_closed(self):
        document, request, run, _source = _doc521_shape()
        with (
            patch(
                "documents.services.transkribus_snapshot_storage.put_object_bytes",
                return_value=None,
            ),
            self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx,
        ):
            apply_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
                job_fetcher=_job_fetcher(),
                page_completer=lambda _run: _outcome(page_count=1),
            )
        self.assertEqual(ctx.exception.code, "SNAPSHOT_STORE_FAILED")
        self.assertFalse(
            TranskribusRunAutomaticSnapshot.objects.filter(run=run).exists()
        )
        request.refresh_from_db()
        self.assertEqual(
            request.status, ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        )

    def test_recognition_only_rejects_source_run_and_lineage_mismatch(self):
        document, request, _run, source = _doc521_shape()
        with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx:
            assess_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=source.pk,
                recognition_job_id="old-failed-job",
                job_fetcher=_job_fetcher(
                    {
                        "jobId": "old-failed-job",
                        "state": "FINISHED",
                        "success": True,
                    }
                ),
            )
        self.assertEqual(ctx.exception.code, "AMBIGUOUS_RUN_PROVENANCE")

        other = _current_run(
            document,
            recognition_job_id="88888888",
            remote_doc_id="different-remote",
        )
        with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx2:
            assess_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=other.pk,
                recognition_job_id="88888888",
                job_fetcher=_job_fetcher(
                    {"jobId": 88888888, "state": "FINISHED", "success": True}
                ),
            )
        self.assertEqual(ctx2.exception.code, "AMBIGUOUS_RUN_PROVENANCE")

    def test_abandon_between_store_and_associate_leaves_unassociated_snapshot(self):
        document, request, run, _source = _doc521_shape()
        token = request.lease_token
        with patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes",
            return_value=None,
        ):
            page_inputs = snapshot_pages_from_upload_mapping(
                _selected_pages(PAGE_COUNT),
                run.page_index_to_page_nr,
            )
            snapshot = store_automatic_snapshot_from_selected_pages(
                document=document,
                run=run,
                page_inputs=page_inputs,
                mapping_trusted=True,
                review_reasons=[],
                create_association=False,
            )

        abandon_process_document_request(request_id=request.pk, document_id=document.pk)
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.FAILED)
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        self.assertIsNone(request.lease_token)
        self.assertNotEqual(request.lease_token, token)

        with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx:
            _associate_snapshot_under_staff_resume_lock(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
                snapshot=snapshot,
                mapping_trusted=True,
                review_reasons=[],
            )
        self.assertEqual(ctx.exception.code, "REQUEST_STATUS_NOT_PARKED_RECOVERY")
        self.assertIsNone(get_run_automatic_snapshot_association(run))
        snapshot.refresh_from_db()
        self.assertEqual(
            snapshot.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=document, engine=ENGINE).exists()
        )

    def test_usable_same_engine_source_without_association_is_ambiguous(self):
        document, request, run, _source = _doc521_shape()
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="existing usable",
        )
        assessment = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(),
        )
        self.assertEqual(assessment.reason, "AMBIGUOUS_SNAPSHOT_STATE")
        self.assertFalse(assessment.apply_permitted)

    def test_same_engine_failed_source_is_replaced_on_resume_success(self):
        """FAILED same-engine rows are not usable blockers; update_or_create reuses them.

        Matches uniq_document_resulttype_engine + worker OCR persistence: one row
        per (document, result_type, engine). Historical FAILED is overwritten in
        place, not preserved as a second row.
        """
        document, request, run, _source = _doc521_shape()
        failed_source = DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="",
            error_code="TRANSKRIBUS_FAILED",
            error_details="prior recognition attempt failed",
            source_revision=1,
        )
        failed_hebrew = DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.FAILED,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="",
            error_code="TRANSKRIBUS_FAILED",
            based_on_source_revision=1,
        )
        failed_source_id = failed_source.pk
        failed_hebrew_id = failed_hebrew.pk

        pre = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(),
        )
        self.assertEqual(pre.phase, "FETCH_AND_COMPLETE")

        snap = _ready_snapshot(document=document, run=run, text="שלום-הצלחה")
        with patch(
            "documents.services.transkribus_engine.start_pylaia_recognition"
        ) as start_recognition:
            apply_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
            )
        start_recognition.assert_not_called()

        request.refresh_from_db()
        document.refresh_from_db()
        run.refresh_from_db()
        failed_source.refresh_from_db()
        failed_hebrew.refresh_from_db()

        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertIsNone(request.lease_token)
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertEqual(document.processing_state_user, Document.ProcessingState.READY)
        self.assertEqual(
            get_run_automatic_snapshot_association(run).snapshot_id, snap.pk
        )

        self.assertEqual(failed_source.pk, failed_source_id)
        self.assertEqual(failed_hebrew.pk, failed_hebrew_id)
        self.assertEqual(failed_source.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(failed_hebrew.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(failed_source.text, "שלום-הצלחה")
        self.assertEqual(failed_hebrew.text, failed_source.text)
        self.assertIsNone(failed_source.error_code)
        self.assertEqual(
            failed_source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(
            DocumentTextResult.objects.filter(
                document=document,
                engine=ENGINE,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).count(),
            1,
        )
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(
                text_result_id=failed_source_id, snapshot_id=snap.pk
            ).exists()
        )
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(
                text_result_id=failed_hebrew_id, snapshot_id=snap.pk
            ).exists()
        )
        # Unrelated failed engines from the 521-shaped fixture remain.
        self.assertEqual(
            DocumentTextResult.objects.filter(
                document=document, status=DocumentTextResult.Status.FAILED
            ).count(),
            2,
        )

    def test_complete_from_snapshot_terminalize_and_idempotent_apply(self):
        document, request, run, _source = _doc521_shape()
        snap = _ready_snapshot(document=document, run=run)

        apply_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)
        self.assertEqual(
            TranskribusRunAutomaticSnapshot.objects.filter(run=run).count(), 1
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(pk=snap.pk).count(), 1
        )

        second = apply_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        self.assertEqual(second.phase, "ALREADY_DONE")

    def test_terminalize_only_and_refuses_without_structural_completion(self):
        document, request, run, _source = _doc521_shape()
        snap = _ready_snapshot(document=document, run=run)
        complete_transkribus_local_success(
            document_id=document.pk,
            run_id=run.pk,
            snapshot_id=snap.pk,
            text="שלום",
            engine=ENGINE,
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=20,
            staff_resume_request_id=request.pk,
            staff_resume_recognition_job_id=RECOGNITION_JOB_ID,
        )
        document.refresh_from_db()
        self.assertEqual(document.processing_state_user, Document.ProcessingState.READY)

        pre = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        self.assertEqual(pre.phase, "TERMINALIZE_ONLY")
        apply_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)

        other_doc = _hebrew_doc(
            processing_state_user=Document.ProcessingState.READY,
            title="other ready",
        )
        other_source = _failed_source_run(other_doc)
        other_request = _rr_request(other_doc, source_run=other_source)
        other_run = _current_run(other_doc)
        self.assertFalse(
            terminalize_parked_process_document_after_staff_resume(
                request_id=other_request.pk,
                document_id=other_doc.pk,
                run_id=other_run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
            )
        )

    def test_staff_auth_binds_exact_run_and_job(self):
        document, request, run, _source = _doc521_shape()
        snap = _ready_snapshot(document=document, run=run)
        other_run = _current_run(
            document,
            recognition_job_id="99999999",
            remote_doc_id=REMOTE_DOC_ID,
            pages_query=PAGES_QUERY,
            page_index_to_page_nr=dict(PAGE_MAP),
        )

        with self.assertRaises(Exception):
            complete_transkribus_local_success(
                document_id=document.pk,
                run_id=other_run.pk,
                snapshot_id=snap.pk,
                text="שלום",
                engine=ENGINE,
                route=_route(),
                needs_review=False,
                review_reasons=[],
                min_text_length=20,
                staff_resume_request_id=request.pk,
                staff_resume_recognition_job_id=RECOGNITION_JOB_ID,
            )

    def test_verified_fence_and_failed_request_not_already_done(self):
        document, request, run, _source = _doc521_shape()
        DocumentTextResult.objects.create(
            document=document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="other-engine",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="ground truth",
        )
        assessment = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(),
        )
        self.assertEqual(assessment.reason, "VERIFIED_WRITE_FENCE")

        abandon_process_document_request(request_id=request.pk, document_id=document.pk)
        request.refresh_from_db()
        self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
        with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx:
            apply_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
            )
        self.assertEqual(ctx.exception.code, "REQUEST_ALREADY_TERMINAL")

    def test_page_fetch_failure_and_dry_run_leave_parked_state(self):
        document, request, run, _source = _doc521_shape()
        token = request.lease_token

        assessment = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(),
        )
        self.assertEqual(assessment.phase, "FETCH_AND_COMPLETE")
        request.refresh_from_db()
        self.assertEqual(request.lease_token, token)
        self.assertFalse(
            TranskribusRunAutomaticSnapshot.objects.filter(run=run).exists()
        )

        with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx:
            apply_transkribus_resume_only_recovery(
                document_id=document.pk,
                request_id=request.pk,
                run_id=run.pk,
                recognition_job_id=RECOGNITION_JOB_ID,
                job_fetcher=_job_fetcher(),
                page_completer=lambda _run: (_ for _ in ()).throw(
                    RuntimeError("PAGE XML boom")
                ),
            )
        self.assertEqual(ctx.exception.code, "PAGE_FETCH_FAILED")
        request.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(
            request.status, ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        )
        self.assertEqual(request.lease_token, token)
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)

    def test_interrupted_after_snapshot_resumes_via_complete_from_snapshot(self):
        document, request, run, _source = _doc521_shape()
        _ready_snapshot(document=document, run=run)

        pre = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        self.assertEqual(pre.phase, "COMPLETE_FROM_SNAPSHOT")

        apply_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
        )
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)

    def test_terminalize_false_rolls_back_local_success_writes(self):
        document, request, run, _source = _doc521_shape()
        token = request.lease_token
        snap = _ready_snapshot(document=document, run=run)

        with patch(
            "documents.services.transkribus_resume_only_recovery."
            "terminalize_parked_process_document_after_staff_resume",
            return_value=False,
        ):
            with self.assertRaises(TranskribusResumeOnlyRecoveryError) as ctx:
                apply_transkribus_resume_only_recovery(
                    document_id=document.pk,
                    request_id=request.pk,
                    run_id=run.pk,
                    recognition_job_id=RECOGNITION_JOB_ID,
                )
        self.assertEqual(ctx.exception.code, "TERMINALIZE_REFUSED")

        request.refresh_from_db()
        document.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual(
            request.status, ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        )
        self.assertEqual(request.lease_token, token)
        self.assertEqual(
            document.processing_state_user,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertFalse(
            DocumentTextResult.objects.filter(document=document, engine=ENGINE).exists()
        )
        # Pre-existing association remains; local-success TX rolled back.
        self.assertEqual(
            get_run_automatic_snapshot_association(run).snapshot_id, snap.pk
        )
        self.assertEqual(
            DocumentTextResult.objects.filter(
                document=document, status=DocumentTextResult.Status.FAILED
            ).count(),
            2,
        )

    def test_remote_job_id_mismatch_and_missing_state_follow_engine_contract(self):
        document, request, run, _source = _doc521_shape()
        bad = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(_finished_job(jobId=111)),
        )
        self.assertEqual(bad.reason, "REMOTE_JOB_NOT_SUCCESS")

        ok = assess_transkribus_resume_only_recovery(
            document_id=document.pk,
            request_id=request.pk,
            run_id=run.pk,
            recognition_job_id=RECOGNITION_JOB_ID,
            job_fetcher=_job_fetcher(
                {"jobId": int(RECOGNITION_JOB_ID), "success": True, "state": ""}
            ),
        )
        self.assertEqual(ok.phase, "FETCH_AND_COMPLETE")

    def test_command_snapshot_apply_and_fetch_requires_evidence(self):
        document, request, run, _source = _doc521_shape()
        _ready_snapshot(document=document, run=run)

        stdout = StringIO()
        call_command(
            "resume_transkribus_local_completion",
            f"--document-id={document.pk}",
            f"--request-id={request.pk}",
            f"--run-id={run.pk}",
            f"--recognition-job-id={RECOGNITION_JOB_ID}",
            stdout=stdout,
        )
        self.assertIn("COMPLETE_FROM_SNAPSHOT", stdout.getvalue())

        with patch(
            "documents.services.transkribus_engine.start_pylaia_recognition"
        ) as start_recognition:
            call_command(
                "resume_transkribus_local_completion",
                f"--document-id={document.pk}",
                f"--request-id={request.pk}",
                f"--run-id={run.pk}",
                f"--recognition-job-id={RECOGNITION_JOB_ID}",
                "--apply",
            )
        start_recognition.assert_not_called()
        request.refresh_from_db()
        self.assertEqual(request.status, ProcessDocumentRequest.Status.COMPLETED)

        document2, request2, run2, _ = _doc521_shape()
        stdout2 = StringIO()
        call_command(
            "resume_transkribus_local_completion",
            f"--document-id={document2.pk}",
            f"--request-id={request2.pk}",
            f"--run-id={run2.pk}",
            f"--recognition-job-id={RECOGNITION_JOB_ID}",
            stdout=stdout2,
        )
        self.assertIn("REMOTE_JOB_EVIDENCE_REQUIRED", stdout2.getvalue())
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "resume_transkribus_local_completion",
                f"--document-id={document2.pk}",
                f"--request-id={request2.pk}",
                f"--run-id={run2.pk}",
                f"--recognition-job-id={RECOGNITION_JOB_ID}",
                "--apply",
            )
        self.assertIn("REMOTE_JOB_EVIDENCE_REQUIRED", str(ctx.exception))


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class StaffResumeOnlyConcurrencyTests(TransactionTestCase):
    """Cross-thread Document→Request contention vs staff abandon.

    Limitation: covers COMPLETE_FROM_SNAPSHOT lock serialization on PostgreSQL
    for one contended pair. Does not prove PAGE XML/S3 windows (those are
    intentionally outside DB locks; association is separately locked).
    """

    def setUp(self) -> None:
        if connection.vendor != "postgresql":
            self.skipTest("Staff resume concurrency test requires PostgreSQL")

    def test_abandon_during_complete_from_snapshot_is_serialized(self):
        document, request, run, _source = _doc521_shape()
        snap = _ready_snapshot(document=document, run=run)
        barrier = threading.Barrier(2, timeout=10)
        outcomes: list[tuple[str, str, str | None]] = []
        unexpected: list[tuple[str, BaseException]] = []
        lock = threading.Lock()

        def staff_apply() -> None:
            connections.close_all()
            try:
                barrier.wait()
                result = apply_transkribus_resume_only_recovery(
                    document_id=document.pk,
                    request_id=request.pk,
                    run_id=run.pk,
                    recognition_job_id=RECOGNITION_JOB_ID,
                )
                with lock:
                    outcomes.append(("apply", "ok", result.phase))
            except TranskribusResumeOnlyRecoveryError as exc:
                with lock:
                    outcomes.append(("apply", "refused", exc.code))
            except BaseException as exc:
                with lock:
                    unexpected.append(("apply", exc))
            finally:
                connections.close_all()

        def staff_abandon() -> None:
            connections.close_all()
            try:
                barrier.wait()
                abandon_process_document_request(
                    request_id=request.pk,
                    document_id=document.pk,
                )
                with lock:
                    outcomes.append(("abandon", "ok", None))
            except ProcessDocumentRequestStaffAbandonError as exc:
                with lock:
                    outcomes.append(("abandon", "refused", str(exc.code)))
            except BaseException as exc:
                with lock:
                    unexpected.append(("abandon", exc))
            finally:
                connections.close_all()

        first = threading.Thread(target=staff_apply)
        second = threading.Thread(target=staff_abandon)
        first.start()
        second.start()
        first.join(timeout=20)
        second.join(timeout=20)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        if unexpected:
            side, exc = unexpected[0]
            self.fail(f"Unexpected {side} failure: {type(exc).__name__}: {exc}")

        request.refresh_from_db()
        document.refresh_from_db()
        run.refresh_from_db()
        assoc = get_run_automatic_snapshot_association(run)

        self.assertEqual(len(outcomes), 2)
        apply_outcome = next(o for o in outcomes if o[0] == "apply")
        abandon_outcome = next(o for o in outcomes if o[0] == "abandon")

        self.assertNotEqual(
            request.status, ProcessDocumentRequest.Status.RECOVERY_REQUIRED
        )
        self.assertIsNotNone(assoc)
        self.assertEqual(assoc.snapshot_id, snap.pk)

        if request.status == ProcessDocumentRequest.Status.COMPLETED:
            # A. Recovery wins (complete+terminalize one TX).
            self.assertEqual(apply_outcome[1], "ok")
            self.assertIsNone(request.lease_token)
            self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
            self.assertEqual(
                document.processing_state_user, Document.ProcessingState.READY
            )
            self.assertTrue(
                DocumentTextResult.objects.filter(
                    document=document,
                    engine=ENGINE,
                    result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                    status=DocumentTextResult.Status.NEEDS_REVIEW,
                ).exists()
            )
            self.assertTrue(
                DocumentTextResult.objects.filter(
                    document=document,
                    engine=ENGINE,
                    result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                ).exists()
            )
            self.assertIn(abandon_outcome[1], {"ok", "refused"})
        elif request.status == ProcessDocumentRequest.Status.FAILED:
            # B. Abandon wins before recovery commits local success.
            self.assertEqual(request.failure_code, STAFF_ABANDONED_FAILURE_CODE)
            self.assertIsNone(request.lease_token)
            self.assertEqual(abandon_outcome, ("abandon", "ok", None))
            self.assertEqual(apply_outcome[1], "refused")
            self.assertIn(
                apply_outcome[2],
                {
                    "REQUEST_ALREADY_TERMINAL",
                    "LOCAL_COMPLETION_FAILED",
                    "TERMINALIZE_REFUSED",
                    "REQUEST_STATUS_NOT_PARKED_RECOVERY",
                },
            )
            self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
            self.assertFalse(
                DocumentTextResult.objects.filter(
                    document=document, engine=ENGINE
                ).exists()
            )
            self.assertEqual(
                DocumentTextResult.objects.filter(
                    document=document, status=DocumentTextResult.Status.FAILED
                ).count(),
                2,
            )
            self.assertNotEqual(
                document.processing_state_user,
                Document.ProcessingState.RECOVERY_REQUIRED,
            )
        else:
            self.fail(f"Unexpected request status={request.status}")
