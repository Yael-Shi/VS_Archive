"""Automatic Transkribus snapshot integration: resume, binding, worker ack."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from documents.management.commands.run_worker import Command
from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusRunAutomaticSnapshot,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.env_validation import WorkerEnvConfig
from documents.services import transkribus_engine as tr
from documents.services.htr_adapters.base import (
    HtrResult,
    TranskribusLocalPersistenceRetryableError,
)
from documents.services.htr_adapters.transkribus_adapter import TranskribusAdapter
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.page_extraction import PageImage
from documents.services.transkribus_engine import (
    OrderedTranscriptSelection,
    PylaiaTranscriptionOutcome,
    SelectedTranscriptPage,
    ordered_transcript_selections,
    pick_transcript,
)
from documents.services.transkribus_local_completion import (
    TranskribusLocalCompletionError,
    associate_run_with_automatic_snapshot,
    complete_transkribus_local_success,
    find_existing_server_local_completion_resume_run,
    find_upload_local_completion_resume_run,
    inspect_local_completion_bindings,
    reconstruct_engine_review_reasons_from_snapshot,
    store_automatic_snapshot_from_selected_pages,
)
from documents.services.transkribus_snapshot_binding import (
    TranskribusSnapshotBindingError,
    bind_text_result_to_snapshot,
)
from documents.services.transkribus_snapshot_pages import (
    TranskribusPageMappingError,
    normalize_page_index_to_page_nr,
    snapshot_pages_from_existing_server_traversal,
    snapshot_pages_from_upload_mapping,
)
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION,
    compute_sha256_hex,
)
from documents.services.transkribus_snapshot_storage import (
    SnapshotStorageOutcome,
    SnapshotStorageResult,
)
from documents.services.process_document_outcome import ProcessDocumentDisposition


def _page_xml(line: str = "Hello") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        '  <Page imageFilename="p.png" imageWidth="100" imageHeight="100">\n'
        '    <TextRegion id="r1">\n'
        '      <TextLine id="l1">\n'
        '        <Coords points="10,20 50,20 50,40 10,40"/>\n'
        '        <Baseline points="10,35 50,35"/>\n'
        f"        <TextEquiv><Unicode>{line}</Unicode></TextEquiv>\n"
        "      </TextLine>\n"
        "    </TextRegion>\n"
        "  </Page>\n"
        "</PcGts>"
    ).encode("utf-8")


def _selected_page(
    *,
    line: str = "Hello",
    page_nr: int = 1,
    ts_id: str = "ts-1",
    url: str = "http://example.test/page.xml",
    page_xml: bytes | None = None,
) -> SelectedTranscriptPage:
    return SelectedTranscriptPage(
        page_nr=page_nr,
        transcript_ts_id=ts_id,
        page_xml=page_xml if page_xml is not None else _page_xml(line),
        url=url,
        provider_page_id=10,
        remote_transcript_status="DONE",
    )


def _outcome(
    *,
    text: str = "Hello",
    job_id: str = "job-1",
    pages: tuple[SelectedTranscriptPage, ...] | None = None,
    review_reasons: list[str] | None = None,
) -> PylaiaTranscriptionOutcome:
    selected = pages if pages is not None else (_selected_page(line=text),)
    return PylaiaTranscriptionOutcome(
        text=text,
        review_reasons=list(review_reasons or []),
        recognition_job_id=job_id,
        selected_pages=selected,
    )


def _worker_env(**overrides) -> WorkerEnvConfig:
    base = WorkerEnvConfig(
        gemini_api_key="k",
        gemini_confidence_threshold=0.7,
        min_text_length=1,
        max_retries=1,
        retry_delay_seconds_1=0,
        retry_delay_seconds_2=0,
        report_window_start="00:00",
        report_send_time="08:00",
        free_tier_alert_pct=80,
        gemini_free_daily_request_limit=1500,
        gemini_free_daily_image_limit=1000,
        transkribus_free_monthly_credits=500,
        enable_hybrid_htr=False,
        enable_daily_report=False,
        smtp_host=None,
        smtp_port=None,
        smtp_username=None,
        smtp_password=None,
        default_from_email=None,
        transkribus_api_token="token",
        transkribus_username="u",
        transkribus_password="p",
        gemini_temperature=0.2,
        gemini_top_k=40,
        gemini_top_p=0.95,
        gemini_max_output_tokens=2048,
        gemini_double_pass=False,
        gemini_consistency_min_ratio=0.7,
        transkribus_collection_id="col",
        transkribus_model_id="42",
        transkribus_dev_upload_mode=True,
        transkribus_use_existing_server_document=False,
        transkribus_force_reprocess=False,
        transkribus_recognition_only_retry=False,
    )
    return replace(base, **overrides)


def _create_he_doc(**kwargs) -> Document:
    defaults = dict(
        title="Hebrew snapshot doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key="he.pdf",
        mime_type="application/pdf",
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _ready_snapshot(
    *,
    document: Document,
    run: TranskribusRun,
    text: str = "Hello",
    hover_eligible: bool = False,
    associate: bool = True,
    mapping_trusted: bool = True,
    review_reasons: list[str] | None = None,
) -> TranskribusTranscriptSnapshot:
    unique = (
        f"{document.pk}:{run.pk}:{text}:{TranskribusTranscriptSnapshot.objects.count()}"
    )
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
        hover_eligible=hover_eligible,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )
    if associate:
        associate_run_with_automatic_snapshot(
            run=run,
            snapshot=snap,
            mapping_trusted=mapping_trusted,
            review_reasons=review_reasons or [],
        )
    return snap


def _route() -> OcrRouteConfig:
    return OcrRouteConfig(
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    )


class PickTranscriptSelectorTests(TestCase):
    def test_job_and_model_preferred(self):
        chosen = pick_transcript(
            [
                {"tsId": "1", "jobId": "9", "modelId": "8", "url": "http://a"},
                {"tsId": "2", "jobId": "10", "modelId": "20", "url": "http://b"},
            ],
            job_id="10",
            model_id="20",
        )
        assert chosen is not None
        self.assertEqual(chosen["url"], "http://b")

    def test_job_only_fallback(self):
        chosen = pick_transcript(
            [
                {"tsId": "1", "jobId": "10", "modelId": "99", "url": "http://job"},
                {"tsId": "2", "jobId": "11", "modelId": "20", "url": "http://other"},
            ],
            job_id="10",
            model_id="20",
        )
        assert chosen is not None
        self.assertEqual(chosen["url"], "http://job")

    def test_model_only_fallback(self):
        chosen = pick_transcript(
            [
                {"tsId": "1", "jobId": "1", "modelId": "20", "url": "http://model"},
                {"tsId": "2", "jobId": "2", "modelId": "99", "url": "http://other"},
            ],
            job_id="10",
            model_id="20",
        )
        assert chosen is not None
        self.assertEqual(chosen["url"], "http://model")


class RetainedSelectionTests(TestCase):
    @patch("documents.services.transkribus_engine.fetch_transcript_xml")
    @patch("documents.services.transkribus_engine.fetch_pages_metadata")
    @patch("documents.services.transkribus_engine.poll_job_until_done")
    def test_retained_xml_matches_exact_chosen_transcript(
        self, m_poll, m_pages, m_fetch
    ):
        m_poll.return_value = {"state": "FINISHED", "success": True}
        m_pages.return_value = [
            tr.TrpPageMetadata(
                page_nr=1,
                page_id=5,
                doc_id=9,
                page_url=None,
                transcripts=[
                    {
                        "tsId": "wrong",
                        "jobId": "9",
                        "modelId": "8",
                        "url": "http://wrong",
                        "status": "OLD",
                    },
                    {
                        "tsId": "right",
                        "jobId": "10",
                        "modelId": "20",
                        "url": "http://right",
                        "status": "DONE",
                        "timestamp": 200,
                    },
                ],
            )
        ]
        xml = _page_xml("Chosen")
        m_fetch.return_value = xml
        session = MagicMock()
        outcome = tr.complete_pylaia_transcription_after_job(
            session,
            recognition_job_id="10",
            collection_id="c",
            model_id="20",
            document_id="9",
            pages_query="1",
            bearer_token="t",
        )
        self.assertEqual(len(outcome.selected_pages), 1)
        page = outcome.selected_pages[0]
        self.assertEqual(page.transcript_ts_id, "right")
        self.assertEqual(page.url, "http://right")
        self.assertEqual(page.page_xml, xml)
        self.assertEqual(page.remote_transcript_status, "DONE")
        m_fetch.assert_called_once_with(
            "http://right", bearer_token="t", timeout_sec=tr.DEFAULT_HTTP_TIMEOUT_SEC
        )

    def test_ordered_selections_do_not_reselect(self):
        pages_meta = [
            tr.TrpPageMetadata(
                page_nr=2,
                page_id=1,
                doc_id=1,
                page_url=None,
                transcripts=[
                    {
                        "tsId": "a",
                        "jobId": "j",
                        "modelId": "m",
                        "url": "http://a",
                        "timestamp": 1,
                    },
                    {
                        "tsId": "b",
                        "jobId": "j",
                        "modelId": "m",
                        "url": "http://b",
                        "timestamp": 2,
                    },
                ],
            )
        ]
        sels = ordered_transcript_selections(pages_meta, job_id="j", model_id="m")
        self.assertEqual(len(sels), 1)
        self.assertIsInstance(sels[0], OrderedTranscriptSelection)
        self.assertEqual(sels[0].url, "http://b")
        self.assertEqual(sels[0].transcript_ts_id, "b")


class PageMappingTests(TestCase):
    def test_upload_mapping_assigns_trusted_indexes(self):
        pages = snapshot_pages_from_upload_mapping(
            [_selected_page(page_nr=2, line="A"), _selected_page(page_nr=1, line="B")],
            {1: 1, 2: 2},
        )
        self.assertEqual([p.page_index for p in pages], [1, 2])
        self.assertEqual([p.page_nr for p in pages], [1, 2])

    def test_upload_mapping_rejects_mismatch(self):
        with self.assertRaises(TranskribusPageMappingError):
            snapshot_pages_from_upload_mapping(
                [_selected_page(page_nr=1)],
                {1: 1, 2: 2},
            )

    def test_valid_zero_based_mapping_converts_to_one_based(self):
        normalized = normalize_page_index_to_page_nr({0: 1, 1: 2})
        self.assertEqual(normalized, {1: 1, 2: 2})
        pages = snapshot_pages_from_upload_mapping(
            [
                _selected_page(page_nr=1, line="A"),
                _selected_page(page_nr=2, line="B"),
            ],
            {0: 1, 1: 2},
        )
        self.assertEqual([(p.page_index, p.page_nr) for p in pages], [(1, 1), (2, 2)])
        self.assertEqual(pages[0].page_nr, 1)
        self.assertEqual(pages[1].page_nr, 2)

    def test_valid_one_based_mapping_preserved(self):
        self.assertEqual(
            normalize_page_index_to_page_nr({1: 10, 2: 11}),
            {1: 10, 2: 11},
        )

    def test_gapped_zero_based_mapping_rejected(self):
        with self.assertRaises(TranskribusPageMappingError):
            normalize_page_index_to_page_nr({0: 1, 2: 2})

    def test_gapped_one_based_mapping_rejected(self):
        with self.assertRaises(TranskribusPageMappingError):
            normalize_page_index_to_page_nr({1: 1, 3: 2})

    def test_string_int_key_collision_after_coercion_rejected(self):
        with self.assertRaises(TranskribusPageMappingError):
            normalize_page_index_to_page_nr({1: 1, "1": 2})

    def test_existing_server_traversal_indexes(self):
        pages = snapshot_pages_from_existing_server_traversal(
            [
                _selected_page(page_nr=5, line="A"),
                _selected_page(page_nr=9, line="B"),
            ]
        )
        self.assertEqual([(p.page_index, p.page_nr) for p in pages], [(1, 5), (2, 9)])


@override_settings(UPLOADS_BUCKET_NAME="test-bucket")
class AutomaticSnapshotLifecycleTests(TestCase):
    def _recognition_started_run(self, doc: Document, **kwargs) -> TranskribusRun:
        defaults = dict(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        defaults.update(kwargs)
        return TranskribusRun.objects.create(**defaults)

    @patch(
        "documents.services.transkribus_snapshot_storage.put_object_bytes",
        return_value=None,
    )
    def test_store_existing_server_new_snapshot_hover_false(self, _m_put):
        doc = _create_he_doc()
        run = self._recognition_started_run(
            doc, mode=TranskribusRun.Mode.EXISTING_SERVER, page_index_to_page_nr=None
        )
        page_inputs = snapshot_pages_from_existing_server_traversal(
            [_selected_page(line="Geom")]
        )
        snapshot = store_automatic_snapshot_from_selected_pages(
            document=doc,
            run=run,
            page_inputs=page_inputs,
            mapping_trusted=False,
        )
        self.assertEqual(
            snapshot.storage_status,
            TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        self.assertFalse(snapshot.hover_eligible)
        assoc = TranskribusRunAutomaticSnapshot.objects.get(run=run)
        self.assertFalse(assoc.mapping_trusted)
        self.assertEqual(assoc.snapshot_id, snapshot.pk)

    @patch(
        "documents.services.transkribus_local_completion.store_transkribus_transcript_snapshot"
    )
    def test_cross_run_reuse_associates_without_mutating_hover(self, m_store):
        doc = _create_he_doc()
        run_a = self._recognition_started_run(doc, recognition_job_id="job-a")
        snap = _ready_snapshot(
            document=doc,
            run=run_a,
            text="Shared",
            hover_eligible=True,
            mapping_trusted=True,
        )
        m_store.return_value = SnapshotStorageResult(
            snapshot=snap,
            outcome=SnapshotStorageOutcome.REUSED_EXISTING,
        )
        run_b = self._recognition_started_run(
            doc,
            recognition_job_id="job-b",
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            page_index_to_page_nr=None,
        )
        page_inputs = snapshot_pages_from_existing_server_traversal(
            [_selected_page(line="Shared")]
        )
        out = store_automatic_snapshot_from_selected_pages(
            document=doc,
            run=run_b,
            page_inputs=page_inputs,
            mapping_trusted=False,
            review_reasons=["EMPTY_TRANSCRIPT_PAGE"],
        )
        self.assertEqual(out.pk, snap.pk)
        snap.refresh_from_db()
        self.assertTrue(snap.hover_eligible)  # immutable READY history
        self.assertEqual(snap.transkribus_run_id, run_a.pk)  # origin provenance
        assoc_b = TranskribusRunAutomaticSnapshot.objects.get(run=run_b)
        self.assertEqual(assoc_b.snapshot_id, snap.pk)
        self.assertFalse(assoc_b.mapping_trusted)
        self.assertEqual(assoc_b.review_reasons, ["EMPTY_TRANSCRIPT_PAGE"])

    def test_run_b_reused_snapshot_completes_and_resumes(self):
        doc = _create_he_doc()
        run_a = self._recognition_started_run(doc, recognition_job_id="job-a")
        snap = _ready_snapshot(
            document=doc, run=run_a, text="Hello", hover_eligible=True
        )
        run_a.status = TranskribusRun.Status.SUCCEEDED
        run_a.save(update_fields=["status"])
        run_b = self._recognition_started_run(doc, recognition_job_id="job-b")
        associate_run_with_automatic_snapshot(
            run=run_b,
            snapshot=snap,
            mapping_trusted=True,
            review_reasons=[],
        )
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run_b.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        run_b.refresh_from_db()
        self.assertEqual(run_b.status, TranskribusRun.Status.SUCCEEDED)
        found = find_upload_local_completion_resume_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
            engine_runtime="transkribus-pylaia:42",
            is_hebrew=True,
        )
        self.assertEqual(found.pk, run_b.pk)
        result = TranskribusAdapter()._htr_result_from_associated_snapshot(
            run=run_b,
            snapshot=snap,
            engine_runtime="transkribus-pylaia:42",
            association=TranskribusRunAutomaticSnapshot.objects.get(run=run_b),
        )
        self.assertEqual(result.transkribus_snapshot_id, snap.id)
        self.assertEqual(result.text, "Hello")

    def test_associate_cannot_reassign_run_from_snapshot_a_to_b(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap_a = _ready_snapshot(document=doc, run=run, text="A")
        other_run = self._recognition_started_run(doc, recognition_job_id="job-other")
        snap_b = _ready_snapshot(document=doc, run=other_run, text="B", associate=False)
        same = associate_run_with_automatic_snapshot(
            run=run,
            snapshot=snap_a,
            mapping_trusted=True,
            review_reasons=["EMPTY_TRANSCRIPT_PAGE"],
        )
        self.assertEqual(same.snapshot_id, snap_a.pk)
        self.assertEqual(same.review_reasons, ["EMPTY_TRANSCRIPT_PAGE"])
        with self.assertRaises(TranskribusLocalCompletionError):
            associate_run_with_automatic_snapshot(
                run=run,
                snapshot=snap_b,
                mapping_trusted=True,
                review_reasons=[],
            )
        assoc = TranskribusRunAutomaticSnapshot.objects.get(run=run)
        self.assertEqual(assoc.snapshot_id, snap_a.pk)
        self.assertEqual(
            TranskribusRunAutomaticSnapshot.objects.filter(run=run).count(), 1
        )

    def test_complete_local_success_writes_dtr_bindings_and_succeeds_run(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        source = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:42",
        )
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
        )
        self.assertEqual(source.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(source.source_revision, 1)
        self.assertEqual(hebrew.based_on_source_revision, 1)
        self.assertEqual(source.text, hebrew.text)
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        self.assertEqual(
            src_bind.binding_role,
            TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        )
        self.assertEqual(
            he_bind.binding_role,
            TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
        )
        self.assertEqual(src_bind.bound_source_revision, 1)
        self.assertEqual(he_bind.bound_source_revision, 1)
        self.assertEqual(src_bind.bound_text_sha256, snap.canonical_text_sha256)
        self.assertEqual(he_bind.snapshot_id, snap.id)

    def test_verified_other_engine_fences_late_automatic_dtr_and_search_index(
        self,
    ):
        doc = _create_he_doc()
        verified_text = "human verified gemini text"
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text=verified_text,
            source_revision=1,
        )
        hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-2.0-flash",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text=verified_text,
            based_on_source_revision=1,
        )
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="late transkribus text")
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mock_sync:
            complete_transkribus_local_success(
                document_id=doc.id,
                run_id=run.id,
                snapshot_id=snap.id,
                text="late transkribus text",
                engine="transkribus-pylaia:42",
                route=_route(),
                needs_review=False,
                review_reasons=[],
                min_text_length=1,
            )
        mock_sync.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=doc,
                engine="transkribus-pylaia:42",
            ).exists()
        )
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, verified_text)
        self.assertEqual(hebrew.text, verified_text)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(source.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertFalse(TranskribusTextResultBinding.objects.exists())

    def test_mixed_engine_verified_source_does_not_downgrade_ready(self):
        doc = _create_he_doc(processing_state_user=Document.ProcessingState.READY)
        source_a = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="gemini-engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified source on engine A",
            source_revision=1,
        )
        hebrew_b = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="gemini-engine-b",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="usable hebrew on engine B",
            based_on_source_revision=1,
        )
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="late transkribus text")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="late transkribus text",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
            pre_run_processing_state=Document.ProcessingState.READY,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=doc,
                engine="transkribus-pylaia:42",
            ).exists()
        )
        source_a.refresh_from_db()
        hebrew_b.refresh_from_db()
        self.assertEqual(source_a.text, "verified source on engine A")
        self.assertEqual(hebrew_b.text, "usable hebrew on engine B")
        self.assertEqual(
            source_a.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            hebrew_b.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        doc.refresh_from_db()
        self.assertEqual(doc.processing_state_user, Document.ProcessingState.READY)

    def test_duplicate_delivery_does_not_overwrite_human_edit(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:42",
        )
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
        )
        source.text = "Human edit"
        source.source_revision = 2
        source.save(update_fields=["text", "source_revision"])
        status = inspect_local_completion_bindings(
            document_id=doc.id,
            engine="transkribus-pylaia:42",
            snapshot=snap,
            is_hebrew=True,
        )
        self.assertTrue(status.structurally_complete)
        self.assertTrue(status.human_edited_after_bind)
        self.assertFalse(status.corrupt)
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        self.assertEqual(src_bind.bound_text_sha256, snap.canonical_text_sha256)
        self.assertEqual(he_bind.bound_text_sha256, snap.canonical_text_sha256)
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source.refresh_from_db()
        hebrew.refresh_from_db()
        self.assertEqual(source.text, "Human edit")
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.text, "Hello")

    def test_corrupt_bound_hash_is_not_idempotent_noop(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:42",
        )
        src_bind = TranskribusTextResultBinding.objects.get(text_result=source)
        src_bind.bound_text_sha256 = "0" * 64
        src_bind.save(update_fields=["bound_text_sha256"])
        status = inspect_local_completion_bindings(
            document_id=doc.id,
            engine="transkribus-pylaia:42",
            snapshot=snap,
            is_hebrew=True,
        )
        self.assertTrue(status.corrupt)
        self.assertFalse(status.structurally_complete)
        with self.assertRaises(TranskribusLocalCompletionError):
            complete_transkribus_local_success(
                document_id=doc.id,
                run_id=run.id,
                snapshot_id=snap.id,
                text="Hello",
                engine="transkribus-pylaia:42",
                route=_route(),
                needs_review=False,
                review_reasons=[],
                min_text_length=1,
            )
        source.refresh_from_db()
        self.assertEqual(source.text, "Hello")

    def test_mismatched_hebrew_bound_revisions_are_corrupt(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
        )
        he_bind = TranskribusTextResultBinding.objects.get(text_result=hebrew)
        he_bind.bound_source_revision = 99
        he_bind.save(update_fields=["bound_source_revision"])
        status = inspect_local_completion_bindings(
            document_id=doc.id,
            engine="transkribus-pylaia:42",
            snapshot=snap,
            is_hebrew=True,
        )
        self.assertTrue(status.corrupt)
        self.assertFalse(status.structurally_complete)
        with self.assertRaises(TranskribusLocalCompletionError):
            complete_transkribus_local_success(
                document_id=doc.id,
                run_id=run.id,
                snapshot_id=snap.id,
                text="Hello",
                engine="transkribus-pylaia:42",
                route=_route(),
                needs_review=False,
                review_reasons=[],
                min_text_length=1,
            )

    def test_unchanged_text_keeps_revision_changed_bumps(self):
        doc = _create_he_doc()
        run1 = self._recognition_started_run(doc, recognition_job_id="job-1")
        snap1 = _ready_snapshot(document=doc, run=run1, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run1.id,
            snapshot_id=snap1.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:42",
        )
        self.assertEqual(source.source_revision, 1)

        run2 = self._recognition_started_run(doc, recognition_job_id="job-2")
        snap2 = _ready_snapshot(document=doc, run=run2, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run2.id,
            snapshot_id=snap2.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source.refresh_from_db()
        self.assertEqual(source.source_revision, 1)

        run3 = self._recognition_started_run(doc, recognition_job_id="job-3")
        snap3 = _ready_snapshot(document=doc, run=run3, text="Changed")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run3.id,
            snapshot_id=snap3.id,
            text="Changed",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        source.refresh_from_db()
        hebrew = DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:42",
        )
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)

    def test_binding_rejects_non_ready_and_cross_document(self):
        doc = _create_he_doc()
        other = _create_he_doc(title="Other")
        run = self._recognition_started_run(doc)
        pending = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            transkribus_run=run,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version=PARSER_VERSION,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
            canonical_text="Hello",
            canonical_text_sha256=compute_sha256_hex("Hello"),
        )
        row = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="e",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="Hello",
        )
        with self.assertRaises(TranskribusSnapshotBindingError):
            bind_text_result_to_snapshot(
                text_result=row,
                snapshot=pending,
                binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
                bound_source_revision=1,
            )
        ready_other = _ready_snapshot(
            document=other, run=self._recognition_started_run(other), text="Hello"
        )
        with self.assertRaises(TranskribusSnapshotBindingError):
            bind_text_result_to_snapshot(
                text_result=row,
                snapshot=ready_other,
                binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
                bound_source_revision=1,
            )

    def test_crash_after_ready_preserves_review_reasons_on_resume(self):
        doc = _create_he_doc()
        run = self._recognition_started_run(doc)
        snap = _ready_snapshot(
            document=doc,
            run=run,
            text="",
            review_reasons=["EMPTY_TRANSCRIPT_PAGE"],
        )
        TranskribusSnapshotPage.objects.create(
            snapshot=snap,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-1",
            lines_with_non_empty_text=0,
            text_region_count=1,
            text_line_count=1,
            page_xml_sha256=compute_sha256_hex(b"x"),
            page_xml_s3_key="k",
        )
        reconstructed = reconstruct_engine_review_reasons_from_snapshot(snap)
        self.assertEqual(reconstructed, ["EMPTY_TRANSCRIPT_PAGE"])
        result = TranskribusAdapter()._htr_result_from_associated_snapshot(
            run=run,
            snapshot=snap,
            engine_runtime="transkribus-pylaia:42",
            association=TranskribusRunAutomaticSnapshot.objects.get(run=run),
        )
        self.assertEqual(result.review_reasons, ["EMPTY_TRANSCRIPT_PAGE"])
        self.assertTrue(result.needs_review)


class ResumeEligibilityTests(TestCase):
    def test_recognition_started_with_association_is_resumable(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        _ready_snapshot(document=doc, run=run, text="Hello")
        found = find_upload_local_completion_resume_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
            engine_runtime="transkribus-pylaia:42",
            is_hebrew=True,
        )
        self.assertEqual(found.pk, run.pk)

    def test_historical_succeeded_without_association_not_resumable(self):
        doc = _create_he_doc()
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        found = find_upload_local_completion_resume_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
            engine_runtime="transkribus-pylaia:42",
            is_hebrew=True,
        )
        self.assertIsNone(found)

    def test_succeeded_with_association_missing_binding_is_resumable(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
            engine_runtime="transkribus-pylaia:42",
        )
        _ready_snapshot(document=doc, run=run, text="Hello")
        found = find_upload_local_completion_resume_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
            engine_runtime="transkribus-pylaia:42",
            is_hebrew=True,
        )
        self.assertEqual(found.pk, run.pk)

    def test_existing_server_fully_completed_not_selected(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            collection_id="col",
            model_id="42",
            remote_doc_id="99",
            pages_query="1",
            recognition_job_id="job-1",
            engine_runtime="transkribus-pylaia:42",
        )
        snap = _ready_snapshot(
            document=doc, run=run, text="Hello", mapping_trusted=False
        )
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        found = find_existing_server_local_completion_resume_run(
            document_id=doc.id,
            collection_id="col",
            model_id="42",
            engine_runtime="transkribus-pylaia:42",
            is_hebrew=True,
        )
        self.assertIsNone(found)

    def test_existing_server_ambiguous_recognition_started_fails(self):
        doc = _create_he_doc()
        for _ in range(2):
            TranskribusRun.objects.create(
                document=doc,
                status=TranskribusRun.Status.RECOGNITION_STARTED,
                mode=TranskribusRun.Mode.EXISTING_SERVER,
                collection_id="col",
                model_id="42",
                remote_doc_id="99",
                pages_query="1",
                recognition_job_id="job-1",
            )
        with self.assertRaises(TranskribusLocalCompletionError):
            find_existing_server_local_completion_resume_run(
                document_id=doc.id,
                collection_id="col",
                model_id="42",
                engine_runtime="transkribus-pylaia:42",
                is_hebrew=True,
            )


class AdapterResumeAndSnapshotFailureTests(TestCase):
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.store_automatic_snapshot_from_selected_pages"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_new_recognition_leaves_run_recognition_started_with_snapshot_ids(
        self, m_login, m_upload, m_start, m_complete, m_store
    ):
        doc = _create_he_doc()
        m_upload.return_value = tr.TrpUploadOutcome(
            collection_id="col",
            doc_id="777",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_start.return_value = "job-1"
        m_complete.return_value = _outcome(text="Hello", job_id="job-1")
        snap = MagicMock()
        snap.pk = 55
        m_store.return_value = snap

        result = TranskribusAdapter().execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_worker_env(),
            document_id=doc.id,
        )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertEqual(result.transkribus_run_id, run.id)
        self.assertEqual(result.transkribus_snapshot_id, 55)
        m_start.assert_called_once()
        m_store.assert_called_once()
        self.assertTrue(m_store.call_args.kwargs["mapping_trusted"])

    @patch(
        "documents.services.htr_adapters.transkribus_adapter.store_automatic_snapshot_from_selected_pages",
        side_effect=TranskribusLocalPersistenceRetryableError("s3 down"),
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_snapshot_s3_failure_keeps_recognition_started_no_dtr(
        self, m_login, m_upload, m_start, m_complete, m_store
    ):
        doc = _create_he_doc()
        m_upload.return_value = tr.TrpUploadOutcome(
            collection_id="col",
            doc_id="777",
            upload_id=1,
            ingest_job_id="ingest-1",
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        m_start.return_value = "job-1"
        m_complete.return_value = _outcome(text="Hello", job_id="job-1")
        with self.assertRaises(TranskribusLocalPersistenceRetryableError):
            TranskribusAdapter().execute(
                pages=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_worker_env(),
                document_id=doc.id,
            )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())

    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_resume_with_ready_snapshot_skips_upload_and_recognition(
        self, m_login, m_upload, m_start
    ):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        snap = _ready_snapshot(
            document=doc,
            run=run,
            text="Hello",
            review_reasons=["EMPTY_TRANSCRIPT_PAGE"],
        )
        result = TranskribusAdapter().execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_worker_env(),
            document_id=doc.id,
        )
        m_upload.assert_not_called()
        m_start.assert_not_called()
        m_login.assert_not_called()
        self.assertEqual(result.transkribus_snapshot_id, snap.id)
        self.assertEqual(result.text, "Hello")
        self.assertEqual(result.review_reasons, ["EMPTY_TRANSCRIPT_PAGE"])
        self.assertTrue(result.needs_review)

    @patch(
        "documents.services.htr_adapters.transkribus_adapter.store_automatic_snapshot_from_selected_pages"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_resume_without_ready_refetches_job_without_start(
        self, m_login, m_upload, m_start, m_complete, m_store
    ):
        doc = _create_he_doc()
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        m_complete.return_value = _outcome(text="Hello", job_id="job-1")
        snap = MagicMock()
        snap.pk = 77
        m_store.return_value = snap
        result = TranskribusAdapter().execute(
            pages=[PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")],
            language_hint="he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            worker_env=_worker_env(),
            document_id=doc.id,
        )
        m_upload.assert_not_called()
        m_start.assert_not_called()
        m_complete.assert_called_once()
        self.assertEqual(m_complete.call_args.kwargs["recognition_job_id"], "job-1")
        self.assertEqual(result.transkribus_snapshot_id, 77)

    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.complete_pylaia_transcription_after_job",
        side_effect=tr.TranskribusRetryableError("transient poll"),
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.start_pylaia_recognition"
    )
    @patch(
        "documents.services.htr_adapters.transkribus_adapter.tr.run_trp_upload_page_images_through_ingest"
    )
    @patch("documents.services.htr_adapters.transkribus_adapter.tr.login_trp_server")
    def test_resume_refetch_transient_maps_to_local_persistence_retryable(
        self, m_login, m_upload, m_start, m_complete
    ):
        doc = _create_he_doc()
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        with self.assertRaises(TranskribusLocalPersistenceRetryableError):
            TranskribusAdapter().execute(
                pages=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
                language_hint="he",
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                worker_env=_worker_env(),
                document_id=doc.id,
            )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())


class WorkerAckAndIdempotencyTests(TestCase):
    def _cmd(self) -> Command:
        cmd = Command()
        cmd._cfg = _worker_env(min_text_length=1)
        return cmd

    def test_local_persistence_retryable_returns_false_without_dtr(self):
        doc = _create_he_doc()
        cmd = self._cmd()
        with patch.dict(
            os.environ, {"ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN": "true"}, clear=False
        ):
            with (
                patch(
                    "documents.management.commands.run_worker.get_object_bytes",
                    return_value=(b"%PDF", "application/pdf"),
                ),
                patch(
                    "documents.management.commands.run_worker.extract_pages",
                    return_value=[
                        PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                    ],
                ),
                patch(
                    "documents.management.commands.run_worker.transcribe_pages",
                    side_effect=TranskribusLocalPersistenceRetryableError("s3"),
                ) as m_transcribe,
            ):
                ok = cmd._process_message(
                    {
                        "Body": json.dumps(
                            {"type": "PROCESS_DOCUMENT", "document_id": doc.id}
                        )
                    }
                )
            m_transcribe.assert_called_once()
        self.assertFalse(ok)
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())

    def test_worker_resume_refetch_transient_no_ocr_failure_no_ack(self):
        doc = _create_he_doc()
        TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        cmd = self._cmd()
        with (
            patch(
                "documents.management.commands.run_worker.get_object_bytes",
                return_value=(b"%PDF", "application/pdf"),
            ),
            patch(
                "documents.management.commands.run_worker.extract_pages",
                return_value=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
            ),
            patch(
                "documents.management.commands.run_worker.select_ocr_route",
                return_value=_route(),
            ),
            patch(
                "documents.management.commands.run_worker.transcribe_pages",
                side_effect=TranskribusLocalPersistenceRetryableError(
                    "transient refetch"
                ),
            ),
        ):
            ok = cmd._process_message(
                {
                    "Body": json.dumps(
                        {"type": "PROCESS_DOCUMENT", "document_id": doc.id}
                    )
                }
            )
        self.assertFalse(ok)
        self.assertFalse(
            DocumentTextResult.objects.filter(
                document=doc, status=DocumentTextResult.Status.FAILED
            ).exists()
        )
        run = TranskribusRun.objects.get(document=doc)
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)

    def test_binding_failure_rolls_back_and_leaves_run_recognition_started(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        with patch(
            "documents.services.transkribus_local_completion.bind_text_result_to_snapshot",
            side_effect=TranskribusSnapshotBindingError("boom"),
        ):
            with self.assertRaises(TranskribusSnapshotBindingError):
                complete_transkribus_local_success(
                    document_id=doc.id,
                    run_id=run.id,
                    snapshot_id=snap.id,
                    text="Hello",
                    engine="transkribus-pylaia:42",
                    route=_route(),
                    needs_review=False,
                    review_reasons=[],
                    min_text_length=1,
                )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.RECOGNITION_STARTED)
        self.assertFalse(DocumentTextResult.objects.filter(document=doc).exists())

    def test_duplicate_delivery_after_success_is_idempotent(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        complete_transkribus_local_success(
            document_id=doc.id,
            run_id=run.id,
            snapshot_id=snap.id,
            text="Hello",
            engine="transkribus-pylaia:42",
            route=_route(),
            needs_review=False,
            review_reasons=[],
            min_text_length=1,
        )
        self.assertEqual(DocumentTextResult.objects.filter(document=doc).count(), 2)
        self.assertEqual(
            TranskribusTextResultBinding.objects.filter(
                text_result__document=doc
            ).count(),
            2,
        )
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)

    def test_worker_local_completion_partial_outcome_acks(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        snap = _ready_snapshot(document=doc, run=run, text="")
        cmd = self._cmd()
        htr = HtrResult(
            text="",
            needs_review=True,
            engine_name="transkribus-pylaia:42",
            review_reasons=[],
            transkribus_run_id=run.id,
            transkribus_snapshot_id=snap.id,
        )

        with (
            patch(
                "documents.management.commands.run_worker.get_object_bytes",
                return_value=(b"%PDF", "application/pdf"),
            ),
            patch(
                "documents.management.commands.run_worker.extract_pages",
                return_value=[
                    PageImage(
                        page_index=1,
                        image_bytes=b"x",
                        mime_type="image/png",
                    )
                ],
            ),
            patch(
                "documents.management.commands.run_worker.select_ocr_route",
                return_value=_route(),
            ),
            patch(
                "documents.management.commands.run_worker.transcribe_pages",
                return_value=htr,
            ),
        ):
            outcome = cmd._execute_process_document_payload(
                {
                    "type": "PROCESS_DOCUMENT",
                    "document_id": doc.id,
                }
            )

        self.assertEqual(
            outcome.disposition,
            ProcessDocumentDisposition.PARTIAL,
        )
        self.assertEqual(
            outcome.failure_code,
            "PROCESS_DOCUMENT_PARTIAL",
        )
        self.assertTrue(outcome.should_ack)

        doc.refresh_from_db()
        self.assertEqual(
            doc.processing_state_user,
            Document.ProcessingState.PARTIAL,
        )

        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)

    def test_worker_success_path_acks_after_local_completion(self):
        doc = _create_he_doc()
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.RECOGNITION_STARTED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        snap = _ready_snapshot(document=doc, run=run, text="Hello")
        cmd = self._cmd()
        htr = HtrResult(
            text="Hello",
            needs_review=False,
            engine_name="transkribus-pylaia:42",
            review_reasons=[],
            transkribus_run_id=run.id,
            transkribus_snapshot_id=snap.id,
        )
        with (
            patch(
                "documents.management.commands.run_worker.get_object_bytes",
                return_value=(b"%PDF", "application/pdf"),
            ),
            patch(
                "documents.management.commands.run_worker.extract_pages",
                return_value=[
                    PageImage(page_index=1, image_bytes=b"x", mime_type="image/png")
                ],
            ),
            patch(
                "documents.management.commands.run_worker.select_ocr_route",
                return_value=_route(),
            ),
            patch(
                "documents.management.commands.run_worker.transcribe_pages",
                return_value=htr,
            ),
        ):
            ok = cmd._process_message(
                {
                    "Body": json.dumps(
                        {"type": "PROCESS_DOCUMENT", "document_id": doc.id}
                    )
                }
            )
        self.assertTrue(ok)
        run.refresh_from_db()
        self.assertEqual(run.status, TranskribusRun.Status.SUCCEEDED)
        self.assertTrue(
            TranskribusTextResultBinding.objects.filter(
                text_result__document=doc
            ).exists()
        )
