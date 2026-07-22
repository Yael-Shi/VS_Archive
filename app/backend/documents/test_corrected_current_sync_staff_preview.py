"""Staff read-only UI for corrected/current Transkribus sync attempt history."""

from __future__ import annotations

import hashlib
from typing import Any

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusCorrectedCurrentSyncPage,
    TranskribusRun,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.text_presentation import resolve_displayable_source_text_result

User = get_user_model()

_TEST_PARSER_VERSION = "test_parser_preview_v1"


def _sha256_hex(data: bytes | str) -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(payload).hexdigest()


@override_settings(UPLOADS_BUCKET_NAME="")
class ResolveDisplayableSourceTextResultTests(TestCase):
    def _create_doc(self, *, language: str, title: str) -> Document:
        return create_ocr_document(
            title=title,
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=language,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/preview/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_result(
        self,
        doc: Document,
        *,
        result_type: str,
        text: str,
        engine: str = "engine-a",
        status: str = DocumentTextResult.Status.NEEDS_REVIEW,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=result_type,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=status,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            source_revision=1,
        )

    def test_returns_source_text_only_for_non_hebrew(self):
        doc = self._create_doc(language=Document.Language.ENGLISH, title="EN source")
        source = self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Hello",
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="שלום",
        )

        resolved = resolve_displayable_source_text_result(doc)
        self.assertEqual(resolved, source)
        self.assertEqual(resolved.text, "Hello")

    def test_never_falls_back_to_hebrew_when_source_missing(self):
        doc = self._create_doc(
            language=Document.Language.ENGLISH, title="EN hebrew only"
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="שלום בלבד",
        )

        self.assertIsNone(resolve_displayable_source_text_result(doc))

    def test_hebrew_document_still_returns_source_not_hebrew(self):
        doc = self._create_doc(language=Document.Language.HEBREW, title="HE both")
        source = self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="מקור",
        )
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="עברית",
        )

        resolved = resolve_displayable_source_text_result(doc)
        self.assertEqual(resolved, source)
        self.assertEqual(resolved.text, "מקור")

    def test_prefers_succeeded_over_needs_review(self):
        doc = self._create_doc(language=Document.Language.ENGLISH, title="EN status")
        self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="needs review",
            engine="engine-old",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
        )
        succeeded = self._create_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="succeeded",
            engine="engine-new",
            status=DocumentTextResult.Status.SUCCEEDED,
        )

        self.assertEqual(resolve_displayable_source_text_result(doc), succeeded)


@override_settings(UPLOADS_BUCKET_NAME="")
class CorrectedCurrentSyncStaffPreviewTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="cc_preview_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="cc_preview_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_doc(self, **kwargs) -> Document:
        defaults: dict[str, Any] = dict(
            title="Corrected sync preview doc",
            doc_type=Document.DocType.PDF,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/cc-preview/original.pdf",
            mime_type="application/pdf",
            visibility=Document.Visibility.PUBLIC,
        )
        defaults.update(kwargs)
        return create_ocr_document(**defaults)

    def _upload_run(self, doc: Document) -> TranskribusRun:
        return TranskribusRun.objects.create(
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

    def _ready_snapshot(
        self,
        *,
        document: Document,
        run: TranskribusRun,
        canonical_text: str = "Snapshot text",
        hover_eligible: bool = True,
    ) -> TranskribusTranscriptSnapshot:
        unique = (
            f"{document.pk}:{run.pk}:{TranskribusTranscriptSnapshot.objects.count()}"
        )
        provider_fp = _sha256_hex(f"prov:{unique}")
        raw_fp = _sha256_hex(f"raw:{unique}")
        return TranskribusTranscriptSnapshot.objects.create(
            document=document,
            transkribus_run=run,
            source_kind=(
                TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC
            ),
            remote_doc_id=str(run.remote_doc_id or ""),
            collection_id=str(run.collection_id or ""),
            model_id=str(run.model_id or ""),
            recognition_job_id=str(run.recognition_job_id or ""),
            parser_version=_TEST_PARSER_VERSION,
            provider_identity_fingerprint=provider_fp,
            raw_xml_fingerprint=raw_fp,
            canonical_text=canonical_text,
            canonical_text_sha256=_sha256_hex(canonical_text),
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=hover_eligible,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            remote_status_summary={"pages": [{"status": "DONE"}]},
        )

    def _create_source_result(
        self,
        doc: Document,
        *,
        text: str,
        engine: str = "engine-a",
        source_revision: int = 3,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            source_revision=source_revision,
        )

    def _create_hebrew_result(self, doc: Document, *, text: str) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=text,
            based_on_source_revision=1,
        )

    def _list_url(self, doc_id: int) -> str:
        return reverse("corrected-current-sync-attempts", kwargs={"doc_id": doc_id})

    def _detail_url(self, doc_id: int, attempt_id: int) -> str:
        return reverse(
            "corrected-current-sync-attempt-detail",
            kwargs={"doc_id": doc_id, "attempt_id": attempt_id},
        )

    def test_anonymous_redirects_to_login(self):
        doc = self._create_doc()
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_non_staff_gets_403(self):
        doc = self._create_doc()
        self.client.force_login(self.viewer)
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 403)

    def test_staff_list_200_and_empty_state(self):
        doc = self._create_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(self._list_url(doc.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אין עדיין ניסיונות סנכרון")
        self.assertContains(resp, "אינה משנה את הארכיון הציבורי")

    def test_staff_can_inspect_private_document(self):
        doc = self._create_doc(visibility=Document.Visibility.PRIVATE)
        run = self._upload_run(doc)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "בתהליך")

    def test_attempts_are_document_scoped_and_mismatch_404(self):
        doc_a = self._create_doc(title="Doc A")
        doc_b = self._create_doc(title="Doc B")
        run_a = self._upload_run(doc_a)
        run_b = self._upload_run(doc_b)
        attempt_a = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc_a,
            transkribus_run=run_a,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        attempt_b = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc_b,
            transkribus_run=run_b,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        self.client.force_login(self.staff)

        list_resp = self.client.get(self._list_url(doc_a.id))
        self.assertEqual(list_resp.status_code, 200)
        listed_attempt_ids = [
            row["attempt"].id for row in list_resp.context["attempt_rows"]
        ]
        self.assertEqual(listed_attempt_ids, [attempt_a.id])
        self.assertNotIn(attempt_b.id, listed_attempt_ids)

        mismatch = self.client.get(self._detail_url(doc_a.id, attempt_b.id))
        self.assertEqual(mismatch.status_code, 404)

    def test_completed_shows_source_snapshot_and_diff_for_non_hebrew(self):
        doc = self._create_doc(
            title="EN completed",
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
        )
        run = self._upload_run(doc)
        source = self._create_source_result(doc, text="Hello")
        self._create_hebrew_result(doc, text="שלום")
        snapshot = self._ready_snapshot(
            document=doc,
            run=run,
            canonical_text="Hello sync",
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="297019349",
            remote_transcript_status="DONE",
            in_progress_warning=False,
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, f"#{source.id}")
        self.assertContains(resp, "revision 3")
        self.assertContains(resp, "Hello")
        self.assertContains(resp, "Hello sync")
        self.assertContains(resp, 'class="transcription-diff-ins"')
        self.assertContains(resp, "sync")
        self.assertNotContains(resp, "שלום")
        self.assertContains(resp, "CORRECTED_CURRENT_SYNC")
        self.assertContains(resp, str(snapshot.id))
        self.assertContains(resp, "297019349")
        self.assertNotContains(resp, snapshot.provider_identity_fingerprint)
        self.assertNotContains(resp, snapshot.raw_xml_fingerprint)
        self.assertNotContains(resp, "page_xml_s3_key")
        self.assertNotContains(resp, "remote_status_summary")
        self.assertNotContains(resp, '{"pages"')

    def test_hebrew_document_uses_source_text_baseline(self):
        doc = self._create_doc(title="HE completed")
        run = self._upload_run(doc)
        hebrew_sentinel = "HEBREW_TEXT_SHOULD_NOT_BE_BASELINE"
        self._create_source_result(doc, text="מקור")
        self._create_hebrew_result(doc, text=hebrew_sentinel)
        snapshot = self._ready_snapshot(
            document=doc,
            run=run,
            canonical_text="מקור מתוקן",
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מקור")
        self.assertContains(resp, "מקור מתוקן")
        self.assertNotContains(resp, hebrew_sentinel)

    def test_completed_without_source_shows_empty_baseline_and_no_diff(self):
        doc = self._create_doc(title="No source baseline")
        run = self._upload_run(doc)
        self._create_hebrew_result(doc, text="רק עברית")
        snapshot = self._ready_snapshot(
            document=doc,
            run=run,
            canonical_text="Snapshot only",
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אין תעתוק מקור (SOURCE_TEXT) ניתן להצגה")
        self.assertContains(resp, "אין בסיס SOURCE_TEXT להשוואה")
        self.assertContains(resp, "Snapshot only")
        self.assertNotContains(resp, 'class="transcription-diff')
        self.assertNotContains(resp, "רק עברית")

    def test_refused_shows_page_reasons_without_snapshot_preview(self):
        doc = self._create_doc(title="Refused attempt")
        run = self._upload_run(doc)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED,
            completed_at=timezone.now(),
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.REFUSED,
            selection_error_code=(
                TranskribusCorrectedCurrentSyncPage.SelectionErrorCode.MULTIPLE_TRANSCRIPTS
            ),
            selection_error_message="יותר מתעתיק אחד לעמוד",
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "MULTIPLE_TRANSCRIPTS")
        self.assertContains(resp, "יותר מתעתיק אחד לעמוד")
        self.assertContains(resp, "סורב בשלב בחירת התעתיקים")
        self.assertNotContains(resp, "טקסט קנוני מה־snapshot")
        self.assertNotContains(resp, "הבדלים מודגשים")

    def test_failed_shows_safe_failure_fields(self):
        doc = self._create_doc(title="Failed attempt")
        run = self._upload_run(doc)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.FAILED,
            failure_code="HTTP_TRANSCRIPT_XML_FAILED",
            failure_message="שליפת XML נכשלה",
            completed_at=timezone.now(),
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="111",
            remote_transcript_status="DONE",
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "HTTP_TRANSCRIPT_XML_FAILED")
        self.assertContains(resp, "שליפת XML נכשלה")
        self.assertContains(resp, "111")
        self.assertNotContains(resp, "טקסט קנוני מה־snapshot")

    def test_started_shown_as_in_progress_not_stale(self):
        doc = self._create_doc(title="Started attempt")
        run = self._upload_run(doc)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )
        TranskribusCorrectedCurrentSyncPage.objects.create(
            attempt=attempt,
            page_index=1,
            page_nr=1,
            outcome=TranskribusCorrectedCurrentSyncPage.Outcome.SELECTED,
            transcript_ts_id="222",
            remote_transcript_status="IN_PROGRESS",
            in_progress_warning=True,
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "בתהליך")
        self.assertContains(resp, "אזהרת IN_PROGRESS מהספק")
        self.assertNotContains(resp, "stale")
        self.assertNotContains(resp, "לא עדכני")
        self.assertNotContains(resp, "ישן")

    def test_script_like_text_is_escaped(self):
        doc = self._create_doc(title="Escape attempt")
        run = self._upload_run(doc)
        dangerous = '<script>alert("x")</script>'
        self._create_source_result(doc, text=dangerous)
        snapshot = self._ready_snapshot(
            document=doc,
            run=run,
            canonical_text=dangerous,
        )
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._detail_url(doc.id, attempt.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "&lt;script&gt;")
        self.assertNotContains(resp, '<script>alert("x")</script>')

    def test_get_does_not_mutate(self):
        doc = self._create_doc(title="Immutable get")
        run = self._upload_run(doc)
        snapshot = self._ready_snapshot(document=doc, run=run)
        attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=doc,
            transkribus_run=run,
            initiated_by=self.staff,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED,
            resolved_snapshot=snapshot,
            storage_outcome=(
                TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED
            ),
            completed_at=timezone.now(),
        )
        source = self._create_source_result(doc, text="Baseline")

        before = {
            "attempts": TranskribusCorrectedCurrentSyncAttempt.objects.count(),
            "pages": TranskribusCorrectedCurrentSyncPage.objects.count(),
            "snapshots": TranskribusTranscriptSnapshot.objects.count(),
            "results": DocumentTextResult.objects.count(),
            "source_text": source.text,
            "source_revision": source.source_revision,
            "attempt_status": attempt.status,
        }

        self.client.force_login(self.staff)
        self.client.get(self._list_url(doc.id))
        self.client.get(self._detail_url(doc.id, attempt.id))

        source.refresh_from_db()
        attempt.refresh_from_db()
        self.assertEqual(
            TranskribusCorrectedCurrentSyncAttempt.objects.count(),
            before["attempts"],
        )
        self.assertEqual(
            TranskribusCorrectedCurrentSyncPage.objects.count(),
            before["pages"],
        )
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.count(),
            before["snapshots"],
        )
        self.assertEqual(DocumentTextResult.objects.count(), before["results"])
        self.assertEqual(source.text, before["source_text"])
        self.assertEqual(source.source_revision, before["source_revision"])
        self.assertEqual(attempt.status, before["attempt_status"])

    def test_document_detail_links_to_attempt_list_for_staff(self):
        doc = self._create_doc()
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self._list_url(doc.id))
        self.assertContains(resp, "היסטוריית סנכרון Transkribus מתוקן")
