"""Focused tests for TranskribusTextResultBinding freshness / hover trust."""

from __future__ import annotations

import hashlib

from django.test import TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusRun,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_binding_freshness import (
    assess_binding_freshness,
    is_binding_structurally_fresh,
    is_binding_trusted_for_hover,
)

_TEST_PARSER_VERSION = "test_parser_binding_freshness_v1"
_ENGINE = "transkribus-pylaia:42"
_TEXT = "Bound canonical text"


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


def _create_doc(*, language: str = Document.Language.HEBREW, **kwargs) -> Document:
    defaults = dict(
        title="Binding freshness doc",
        doc_type=Document.DocType.PDF,
        language=language,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key=f"binding-freshness-{language}.pdf",
        mime_type="application/pdf",
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _upload_run(doc: Document, **kwargs) -> TranskribusRun:
    defaults = dict(
        document=doc,
        status=TranskribusRun.Status.SUCCEEDED,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id="col",
        model_id="42",
        remote_doc_id="777",
        pages_query="1",
        recognition_job_id="job-1",
        page_index_to_page_nr={1: 1},
        engine_runtime=_ENGINE,
    )
    defaults.update(kwargs)
    return TranskribusRun.objects.create(**defaults)


def _ready_snapshot(
    *,
    document: Document,
    run: TranskribusRun,
    text: str = _TEXT,
    hover_eligible: bool = True,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{run.pk}:{TranskribusTranscriptSnapshot.objects.count()}"
    return TranskribusTranscriptSnapshot.objects.create(
        document=document,
        transkribus_run=run,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        remote_doc_id=str(run.remote_doc_id or ""),
        collection_id=str(run.collection_id or ""),
        model_id=str(run.model_id or ""),
        recognition_job_id=str(run.recognition_job_id or ""),
        parser_version=_TEST_PARSER_VERSION,
        provider_identity_fingerprint=_sha256_hex(f"prov:{unique}"),
        raw_xml_fingerprint=_sha256_hex(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha256_hex(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=hover_eligible,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )


def _source_row(
    doc: Document,
    *,
    text: str = _TEXT,
    source_revision: int = 1,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=_ENGINE,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
        source_revision=source_revision,
    )


def _hebrew_row(
    doc: Document,
    *,
    text: str = _TEXT,
    based_on_source_revision: int = 1,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine=_ENGINE,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
        based_on_source_revision=based_on_source_revision,
    )


def _bind(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
    role: str,
    bound_source_revision: int,
    text_for_hash: str | None = None,
) -> TranskribusTextResultBinding:
    text = text_for_hash if text_for_hash is not None else (text_result.text or "")
    return TranskribusTextResultBinding.objects.create(
        text_result=text_result,
        snapshot=snapshot,
        binding_role=role,
        bound_text_sha256=_sha256_hex(text),
        bound_source_revision=bound_source_revision,
    )


class BindingFreshnessTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()
        self.run = _upload_run(self.doc)
        self.snapshot = _ready_snapshot(
            document=self.doc, run=self.run, hover_eligible=True
        )

    def test_valid_source_binding_is_structurally_fresh_and_hover_trusted(self):
        source = _source_row(self.doc)
        _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        assessment = assess_binding_freshness(source)
        self.assertTrue(assessment.has_binding)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertTrue(assessment.is_trusted_for_hover)
        self.assertTrue(is_binding_structurally_fresh(source))
        self.assertTrue(is_binding_trusted_for_hover(source))

    def test_valid_hebrew_mirror_binding_is_structurally_fresh_and_hover_trusted(self):
        hebrew = _hebrew_row(self.doc)
        _bind(
            text_result=hebrew,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_source_revision=1,
        )
        assessment = assess_binding_freshness(hebrew)
        self.assertTrue(assessment.has_binding)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertTrue(assessment.is_trusted_for_hover)

    def test_no_binding_fails_closed(self):
        source = _source_row(self.doc)
        assessment = assess_binding_freshness(source)
        self.assertFalse(assessment.has_binding)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)

    def test_wrong_role_fails_closed(self):
        source = _source_row(self.doc)
        _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_source_revision=1,
        )
        assessment = assess_binding_freshness(source)
        self.assertTrue(assessment.has_binding)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)

    def test_cross_document_snapshot_mismatch_fails_closed(self):
        source = _source_row(self.doc)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        other_doc = _create_doc(file_s3_key="other-doc.pdf")
        other_run = _upload_run(other_doc, remote_doc_id="888")
        other_snapshot = _ready_snapshot(document=other_doc, run=other_run)
        # Bypass model.save() same-document validation for the mismatch case.
        TranskribusTextResultBinding.objects.filter(pk=binding.pk).update(
            snapshot_id=other_snapshot.pk
        )
        source.refresh_from_db()
        binding = TranskribusTextResultBinding.objects.select_related("snapshot").get(
            pk=binding.pk
        )
        assessment = assess_binding_freshness(source, binding=binding)
        self.assertTrue(assessment.has_binding)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)

    def test_non_ready_snapshot_fails_closed(self):
        source = _source_row(self.doc)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        TranskribusTranscriptSnapshot.objects.filter(pk=self.snapshot.pk).update(
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD
        )
        binding = TranskribusTextResultBinding.objects.select_related("snapshot").get(
            pk=binding.pk
        )
        self.assertFalse(is_binding_structurally_fresh(source, binding=binding))
        self.assertFalse(is_binding_trusted_for_hover(source, binding=binding))

    def test_missing_canonical_sha_fails_closed(self):
        source = _source_row(self.doc)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        binding = TranskribusTextResultBinding.objects.select_related("snapshot").get(
            pk=binding.pk
        )
        # READY + empty canonical SHA cannot be persisted (DB check constraint);
        # evaluate the in-memory fail-closed predicate on a mutated snapshot.
        binding.snapshot.canonical_text_sha256 = ""
        self.assertFalse(is_binding_structurally_fresh(source, binding=binding))

    def test_canonical_text_hash_integrity_mismatch_fails_closed(self):
        source = _source_row(self.doc)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        TranskribusTranscriptSnapshot.objects.filter(pk=self.snapshot.pk).update(
            canonical_text="Tampered text bytes"
        )
        binding = TranskribusTextResultBinding.objects.select_related("snapshot").get(
            pk=binding.pk
        )
        self.assertFalse(is_binding_structurally_fresh(source, binding=binding))
        self.assertFalse(is_binding_trusted_for_hover(source, binding=binding))

    def test_bound_hash_mismatch_fails_closed(self):
        source = _source_row(self.doc)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        binding.bound_text_sha256 = "0" * 64
        binding.save(update_fields=["bound_text_sha256"])
        self.assertFalse(is_binding_structurally_fresh(source))

    def test_current_text_drift_fails_closed(self):
        source = _source_row(self.doc)
        _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        source.text = "Human edited after bind"
        source.save(update_fields=["text"])
        assessment = assess_binding_freshness(source)
        self.assertTrue(assessment.has_binding)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)

    def test_source_revision_drift_fails_closed(self):
        source = _source_row(self.doc, source_revision=1)
        _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        source.source_revision = 2
        source.save(update_fields=["source_revision"])
        self.assertFalse(is_binding_structurally_fresh(source))

    def test_hebrew_based_on_source_revision_drift_fails_closed(self):
        hebrew = _hebrew_row(self.doc, based_on_source_revision=1)
        _bind(
            text_result=hebrew,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_source_revision=1,
        )
        hebrew.based_on_source_revision = 2
        hebrew.save(update_fields=["based_on_source_revision"])
        self.assertFalse(is_binding_structurally_fresh(hebrew))

    def test_bound_source_revision_below_one_fails_closed(self):
        source = _source_row(self.doc, source_revision=1)
        binding = _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        TranskribusTextResultBinding.objects.filter(pk=binding.pk).update(
            bound_source_revision=0
        )
        source.source_revision = 0
        source.save(update_fields=["source_revision"])
        binding = TranskribusTextResultBinding.objects.select_related("snapshot").get(
            pk=binding.pk
        )
        self.assertFalse(is_binding_structurally_fresh(source, binding=binding))

    def test_unsupported_result_type_fails_closed(self):
        # DocumentTextResult only allows SOURCE/HEBREW in choices; force an
        # unsupported value via queryset update for fail-closed coverage.
        source = _source_row(self.doc)
        _bind(
            text_result=source,
            snapshot=self.snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        DocumentTextResult.objects.filter(pk=source.pk).update(
            result_type="UNSUPPORTED_TYPE"
        )
        source.refresh_from_db()
        assessment = assess_binding_freshness(source)
        self.assertTrue(assessment.has_binding)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)

    def test_hover_eligible_false_keeps_structural_freshness_but_rejects_hover(self):
        snapshot = _ready_snapshot(
            document=self.doc,
            run=self.run,
            text="Hover false text",
            hover_eligible=False,
        )
        source = _source_row(self.doc, text="Hover false text")
        _bind(
            text_result=source,
            snapshot=snapshot,
            role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=1,
        )
        assessment = assess_binding_freshness(source)
        self.assertTrue(assessment.has_binding)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertFalse(assessment.is_trusted_for_hover)
        self.assertTrue(is_binding_structurally_fresh(source))
        self.assertFalse(is_binding_trusted_for_hover(source))
