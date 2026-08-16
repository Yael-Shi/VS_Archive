"""Staff Transkribus paragraph editor and status UI (PR3) tests."""

from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from django.contrib.auth import get_user_model
from django.db import connection
from django.http import QueryDict
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusParagraphMapping,
    TranskribusRun,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_paragraph_mapping import (
    get_paragraph_mapping_for_snapshot,
    save_paragraph_mapping,
)
from documents.services.transkribus_paragraph_staff import (
    MSG_DUPLICATE,
    MSG_FINAL_LINE,
    MSG_MALFORMED,
    MSG_NON_CONTRIBUTING,
    MSG_OTHER_SNAPSHOT,
    MSG_SAVED,
    MSG_STALE_BINDING,
    MSG_STALE_SUBMIT,
    MSG_UNAVAILABLE,
    STATUS_HISTORICAL_NOTE,
    STATUS_NEVER_SAVED,
    STATUS_ONE_PARAGRAPH,
    ParagraphEditorError,
    build_paragraph_mapping_staff_status,
    parse_break_after_line_ids,
    status_n_paragraphs,
)

User = get_user_model()

_PARSER = "test_paragraph_editor_v1"
_ENGINE = "transkribus-pylaia:42"
_TEXT = "Alpha\nBeta\nGamma"
_PAGED = "Alpha\nBeta\n\nGamma"
_HEBREW = "שלום\nעולם\nשורה"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _visible_text(html: str) -> str:
    collector = _TextCollector()
    collector.feed(html)
    collector.close()
    return "".join(collector.parts)


def _create_doc(**kwargs) -> Document:
    defaults = dict(
        title="Paragraph editor doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.READY,
        file_s3_key="paragraph-editor.pdf",
        mime_type="application/pdf",
        visibility=Document.Visibility.PUBLIC,
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _upload_run(doc: Document) -> TranskribusRun:
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
    document: Document,
    *,
    text: str = _TEXT,
    parser_version: str = _PARSER,
    hover_eligible: bool = True,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{TranskribusTranscriptSnapshot.objects.count()}:{text}"
    return TranskribusTranscriptSnapshot.objects.create(
        document=document,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        parser_version=parser_version,
        provider_identity_fingerprint=_sha(f"prov:{unique}"),
        raw_xml_fingerprint=_sha(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=hover_eligible,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )


def _add_page(
    snapshot: TranskribusTranscriptSnapshot,
    page_index: int,
) -> TranskribusSnapshotPage:
    return TranskribusSnapshotPage.objects.create(
        snapshot=snapshot,
        page_index=page_index,
        page_nr=page_index,
        transcript_ts_id=f"ts-{snapshot.pk}-{page_index}",
        page_geometry_capability=TranskribusSnapshotPage.GeometryCapability.VERIFIED,
        image_width=1000,
        image_height=1500,
    )


def _add_line(
    page: TranskribusSnapshotPage,
    order: int,
    text: str,
    *,
    start: int,
    end: int,
    provider_line_id: str | None = None,
    contributes: bool = True,
) -> TranskribusSnapshotLine:
    return TranskribusSnapshotLine.objects.create(
        page=page,
        order_index=order,
        provider_region_id=f"region-{page.page_index}",
        provider_line_id=(
            provider_line_id
            if provider_line_id is not None
            else f"line-{page.page_index}-{order}"
        ),
        text=text,
        contributes_to_canonical=contributes,
        char_start=start,
        char_end=end,
        polygon_points=[[10.0, 10.0], [100.0, 10.0], [100.0, 20.0]],
        bbox_min_x=10.0,
        bbox_min_y=10.0,
        bbox_max_x=100.0,
        bbox_max_y=20.0,
        coords_valid=True,
        has_meaningful_geometry=True,
    )


def _three_line_snapshot(document: Document, **kwargs) -> tuple:
    snapshot = _ready_snapshot(document, **kwargs)
    page = _add_page(snapshot, 1)
    alpha = _add_line(page, 0, "Alpha", start=0, end=5)
    beta = _add_line(page, 1, "Beta", start=6, end=10)
    gamma = _add_line(page, 2, "Gamma", start=11, end=16)
    return snapshot, page, alpha, beta, gamma


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


def _bind(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
    bound_source_revision: int = 1,
) -> TranskribusTextResultBinding:
    return TranskribusTextResultBinding.objects.create(
        text_result=text_result,
        snapshot=snapshot,
        binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        bound_text_sha256=_sha(text_result.text or ""),
        bound_source_revision=bound_source_revision,
    )


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorAccessTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-staff",
            password="x",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="para-editor-viewer",
            password="x",
            is_staff=False,
        )
        self.doc = _create_doc()
        snapshot, _page, _a, _b, _c = _three_line_snapshot(self.doc)
        result = _source_row(self.doc)
        _bind(text_result=result, snapshot=snapshot)
        self.url = reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})

    def test_anonymous_redirects_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_non_staff_gets_403(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_staff_can_open_editor(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "חלוקת פסקאות בתעתוק Transkribus")

    def test_non_staff_post_is_refused(self):
        self.client.force_login(self.viewer)
        response = self.client.post(self.url, data={})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorGetTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-get",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _url(self, doc_id: int) -> str:
        return reverse("transkribus-paragraphs", kwargs={"doc_id": doc_id})

    def test_current_snapshot_lines_in_page_order(self):
        doc = _create_doc()
        snapshot, _page, alpha, beta, gamma = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        alpha_at = html.find("Alpha")
        beta_at = html.find("Beta")
        gamma_at = html.find("Gamma")
        self.assertLess(alpha_at, beta_at)
        self.assertLess(beta_at, gamma_at)
        self.assertEqual(response.context["freshness"].snapshot_id, snapshot.pk)
        self.assertEqual(
            [line.line_id for line in response.context["editor_lines"]],
            [alpha.pk, beta.pk, gamma.pk],
        )

    def test_no_control_after_final_contributing_line(self):
        doc = _create_doc()
        snapshot, _page, alpha, beta, gamma = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        html = response.content.decode()
        self.assertIn(f'value="{alpha.pk}"', html)
        self.assertIn(f'value="{beta.pk}"', html)
        self.assertNotIn(f'value="{gamma.pk}"', html)
        lines = list(response.context["editor_lines"])
        self.assertTrue(lines[0].can_break_after)
        self.assertTrue(lines[1].can_break_after)
        self.assertFalse(lines[2].can_break_after)

    def test_page_transition_shown_but_not_selected(self):
        doc = _create_doc()
        snapshot = _ready_snapshot(doc, text=_PAGED)
        page1 = _add_page(snapshot, 1)
        page2 = _add_page(snapshot, 2)
        alpha = _add_line(page1, 0, "Alpha", start=0, end=5)
        beta = _add_line(page1, 1, "Beta", start=6, end=10)
        gamma = _add_line(page2, 0, "Gamma", start=12, end=17)
        result = _source_row(doc, text=_PAGED)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        html = response.content.decode()
        self.assertContains(response, "מעבר לעמוד 2")
        self.assertNotContains(response, "adopt")
        self.assertNotContains(response, "העתק")
        lines = list(response.context["editor_lines"])
        self.assertFalse(lines[0].is_page_start)
        self.assertFalse(lines[1].is_page_start)
        self.assertTrue(lines[2].is_page_start)
        self.assertFalse(any(line.break_after_selected for line in lines))
        checked = re.findall(
            r'name="break_after"[^>]*checked|checked[^>]*name="break_after"',
            html,
        )
        self.assertEqual(checked, [])
        self.assertEqual(
            [line.line_id for line in lines], [alpha.pk, beta.pk, gamma.pk]
        )

    def test_hebrew_rtl_structure(self):
        doc = _create_doc(title="Hebrew editor")
        snapshot = _ready_snapshot(doc, text=_HEBREW)
        page = _add_page(snapshot, 1)
        _add_line(page, 0, "שלום", start=0, end=4)
        _add_line(page, 1, "עולם", start=5, end=9)
        _add_line(page, 2, "שורה", start=10, end=14)
        result = _source_row(doc, text=_HEBREW)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        self.assertContains(response, 'dir="rtl"')
        self.assertContains(response, "שלום")
        self.assertTrue(response.context["source_is_rtl"])

    def test_english_ltr_structure(self):
        doc = _create_doc(
            title="English editor",
            language=Document.Language.ENGLISH,
        )
        snapshot, _page, _a, _b, _c = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        self.assertContains(response, 'dir="ltr"')
        self.assertFalse(response.context["source_is_rtl"])

    def test_technical_ids_not_visible_as_labels(self):
        doc = _create_doc()
        snapshot, page, alpha, _b, _c = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        response = self.client.get(self._url(doc.id))
        visible = _visible_text(response.content.decode())
        self.assertNotIn(f"snapshot={snapshot.pk}", visible)
        self.assertNotIn(alpha.provider_line_id, visible)
        self.assertNotIn("p1-o0", visible)
        self.assertNotIn("canonical", visible.lower())
        self.assertNotIn("snapshot", visible.lower())
        self.assertNotIn("binding", visible.lower())

    def test_existing_breaks_preselected(self):
        doc = _create_doc()
        snapshot, _page, alpha, beta, _gamma = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        save_paragraph_mapping(snapshot, [alpha.pk, beta.pk])
        response = self.client.get(self._url(doc.id))
        lines = list(response.context["editor_lines"])
        self.assertTrue(lines[0].break_after_selected)
        self.assertTrue(lines[1].break_after_selected)
        self.assertFalse(lines[2].break_after_selected)
        self.assertContains(response, status_n_paragraphs(3))

    def test_structurally_stale_binding_refuses_editor(self):
        doc = _create_doc()
        snapshot, _page, _a, _b, _c = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        result.text = "Alpha\nBeta\nGamma edited"
        result.source_revision = 2
        result.save(update_fields=["text", "source_revision", "updated_at"])
        response = self.client.get(self._url(doc.id))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["editor_available"])
        self.assertContains(response, MSG_STALE_BINDING)
        self.assertNotContains(response, 'name="break_after"')


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorMappingStatusTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-status",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.snapshot, _page, self.alpha, self.beta, self.gamma = _three_line_snapshot(
            self.doc
        )
        self.result = _source_row(self.doc)
        _bind(text_result=self.result, snapshot=self.snapshot)
        _upload_run(self.doc)
        self.editor_url = reverse(
            "transkribus-paragraphs", kwargs={"doc_id": self.doc.id}
        )
        self.versions_url = reverse(
            "corrected-current-sync-attempts", kwargs={"doc_id": self.doc.id}
        )
        self.detail_url = reverse(
            "documents-detail-page", kwargs={"doc_id": self.doc.id}
        )

    def test_no_mapping_status(self):
        status = build_paragraph_mapping_staff_status(self.doc)
        self.assertEqual(status.code, "NEVER_SAVED")
        self.assertEqual(status.label, STATUS_NEVER_SAVED)
        response = self.client.get(self.editor_url)
        self.assertContains(response, STATUS_NEVER_SAVED)
        versions = self.client.get(self.versions_url)
        self.assertContains(versions, "תעתוק Transkribus המוצג כעת")
        self.assertContains(versions, STATUS_NEVER_SAVED)
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, STATUS_NEVER_SAVED)

    def test_zero_break_status_is_one_paragraph(self):
        save_paragraph_mapping(self.snapshot, [])
        status = build_paragraph_mapping_staff_status(self.doc)
        self.assertEqual(status.code, "ONE_PARAGRAPH")
        self.assertEqual(status.label, STATUS_ONE_PARAGRAPH)
        self.assertEqual(status.paragraph_count, 1)
        response = self.client.get(self.editor_url)
        self.assertContains(response, STATUS_ONE_PARAGRAPH)
        self.assertNotContains(response, STATUS_NEVER_SAVED)

    def test_one_break_status_is_two_paragraphs(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        status = build_paragraph_mapping_staff_status(self.doc)
        self.assertEqual(status.code, "N_PARAGRAPHS")
        self.assertEqual(status.paragraph_count, 2)
        self.assertContains(self.client.get(self.editor_url), status_n_paragraphs(2))

    def test_multiple_breaks_status(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk, self.beta.pk])
        status = build_paragraph_mapping_staff_status(self.doc)
        self.assertEqual(status.paragraph_count, 3)
        self.assertContains(self.client.get(self.versions_url), status_n_paragraphs(3))
        self.assertContains(self.client.get(self.detail_url), status_n_paragraphs(3))

    def test_historical_mapping_alone_is_not_current(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        new_text = "New\nBeta\nGamma"
        new_snapshot, *_rest = _three_line_snapshot(self.doc, text=new_text)
        self.result.text = new_text
        self.result.save(update_fields=["text", "updated_at"])
        binding = TranskribusTextResultBinding.objects.get(text_result=self.result)
        binding.snapshot = new_snapshot
        binding.bound_text_sha256 = _sha(new_text)
        binding.save(update_fields=["snapshot", "bound_text_sha256"])

        status = build_paragraph_mapping_staff_status(self.doc)
        self.assertEqual(status.code, "NEVER_SAVED")
        self.assertTrue(status.has_historical_mapping)
        self.assertEqual(status.historical_note, STATUS_HISTORICAL_NOTE)
        versions = self.client.get(self.versions_url)
        self.assertContains(versions, STATUS_NEVER_SAVED)
        self.assertContains(versions, STATUS_HISTORICAL_NOTE)
        self.assertNotContains(versions, status_n_paragraphs(2))
        self.assertNotContains(versions, "אימוץ")
        self.assertNotIn(
            reverse("transkribus-paragraphs-adopt", kwargs={"doc_id": self.doc.id}),
            versions.content.decode(),
        )
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, STATUS_NEVER_SAVED)
        self.assertNotContains(detail, status_n_paragraphs(2))
        self.assertNotContains(detail, "אימוץ")


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorSaveTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-save",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.snapshot, self.page, self.alpha, self.beta, self.gamma = (
            _three_line_snapshot(self.doc)
        )
        self.result = _source_row(self.doc)
        _bind(text_result=self.result, snapshot=self.snapshot)
        self.url = reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})

    def _freshness(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        return response.context["freshness"]

    def _post(self, *, breaks=(), freshness=None, extra=None):
        token = freshness or self._freshness()
        data = {
            "expected_document_id": str(token.document_id),
            "expected_text_result_id": str(token.text_result_id),
            "expected_snapshot_id": str(token.snapshot_id),
            "break_after": [str(item) for item in breaks],
        }
        if extra:
            data.update(extra)
        return self.client.post(self.url, data=data)

    def test_post_zero_breaks_creates_explicit_mapping(self):
        response = self._post(breaks=())
        self.assertEqual(response.status_code, 302)
        mapping = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertIsNotNone(mapping)
        self.assertEqual(mapping.breaks.count(), 0)
        self.assertIsNone(mapping.copied_from_id)
        self.assertEqual(mapping.created_by_id, self.staff.pk)
        self.assertEqual(mapping.updated_by_id, self.staff.pk)
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_SAVED)
        self.assertContains(follow, STATUS_ONE_PARAGRAPH)

    def test_post_boundaries_creates_break_rows(self):
        self._post(breaks=[self.alpha.pk])
        mapping = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertEqual(
            list(mapping.breaks.values_list("after_line_id", flat=True)),
            [self.alpha.pk],
        )

    def test_resave_replaces_previous_boundaries(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk], actor=self.staff)
        self._post(breaks=[self.beta.pk])
        mapping = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertEqual(mapping.breaks.count(), 1)
        self.assertEqual(mapping.breaks.get().after_line_id, self.beta.pk)

    def test_ordinary_resave_clears_copied_from(self):
        source = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        other_text = "Other\nBeta\nGamma"
        other_snapshot, _page, other_alpha, _b, _c = _three_line_snapshot(
            self.doc, text=other_text
        )
        copied = save_paragraph_mapping(
            other_snapshot,
            [other_alpha.pk],
            copied_from=source,
        )
        self.assertEqual(copied.copied_from_id, source.pk)
        self.result.text = other_text
        self.result.save(update_fields=["text", "updated_at"])
        binding = TranskribusTextResultBinding.objects.get(text_result=self.result)
        binding.snapshot = other_snapshot
        binding.bound_text_sha256 = _sha(other_text)
        binding.save(update_fields=["snapshot", "bound_text_sha256"])

        self.url = reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})
        self._post(breaks=[other_alpha.pk])
        copied.refresh_from_db()
        self.assertIsNone(copied.copied_from_id)

    def test_canonical_text_and_lines_unchanged(self):
        canonical = self.snapshot.canonical_text
        dtr_text = self.result.text
        line_payload = list(
            TranskribusSnapshotLine.objects.filter(page__snapshot=self.snapshot).values(
                "pk",
                "text",
                "char_start",
                "char_end",
                "polygon_points",
                "provider_line_id",
            )
        )
        self._post(breaks=[self.alpha.pk, self.beta.pk])
        self.snapshot.refresh_from_db()
        self.result.refresh_from_db()
        self.assertEqual(self.snapshot.canonical_text, canonical)
        self.assertEqual(self.result.text, dtr_text)
        self.assertEqual(
            list(
                TranskribusSnapshotLine.objects.filter(
                    page__snapshot=self.snapshot
                ).values(
                    "pk",
                    "text",
                    "char_start",
                    "char_end",
                    "polygon_points",
                    "provider_line_id",
                )
            ),
            line_payload,
        )

    def test_cancel_does_not_save(self):
        response = self.client.get(self.url)
        self.assertContains(
            response,
            reverse("corrected-current-sync-attempts", kwargs={"doc_id": self.doc.id}),
        )
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_form_includes_csrf(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')

    def test_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        freshness = self._freshness()
        response = csrf_client.post(
            self.url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorValidationTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-validate",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.snapshot, self.page, self.alpha, self.beta, self.gamma = (
            _three_line_snapshot(self.doc)
        )
        self.result = _source_row(self.doc)
        _bind(text_result=self.result, snapshot=self.snapshot)
        self.url = reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})
        get = self.client.get(self.url)
        self.freshness = get.context["freshness"]

    def _post(self, **data):
        payload = {
            "expected_document_id": str(self.freshness.document_id),
            "expected_text_result_id": str(self.freshness.text_result_id),
            "expected_snapshot_id": str(self.freshness.snapshot_id),
        }
        payload.update(data)
        return self.client.post(self.url, data=payload)

    def test_malformed_break_rejected(self):
        response = self._post(break_after="not-an-id")
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_MALFORMED)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_line_from_another_snapshot_rejected(self):
        other, _page, other_alpha, _b, _c = _three_line_snapshot(
            self.doc, text="Other\nBeta\nGamma"
        )
        self._post(break_after=str(other_alpha.pk))
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_OTHER_SNAPSHOT)
        self.assertNotIn(f"snapshot={other.pk}", _visible_text(follow.content.decode()))
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_non_contributing_line_rejected(self):
        empty = _add_line(
            self.page,
            3,
            "",
            start=16,
            end=16,
            provider_line_id="empty",
            contributes=False,
        )
        follow = self.client.get(self.url)
        self.assertNotIn(f'value="{empty.pk}"', follow.content.decode())
        self._post(break_after=str(empty.pk))
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_NON_CONTRIBUTING)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_final_line_rejected(self):
        self._post(break_after=str(self.gamma.pk))
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_FINAL_LINE)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_active_snapshot_changed_does_not_write(self):
        new_text = "New\nBeta\nGamma"
        new_snapshot, *_rest = _three_line_snapshot(self.doc, text=new_text)
        self.result.text = new_text
        self.result.save(update_fields=["text", "updated_at"])
        binding = TranskribusTextResultBinding.objects.get(text_result=self.result)
        binding.snapshot = new_snapshot
        binding.bound_text_sha256 = _sha(new_text)
        binding.save(update_fields=["snapshot", "bound_text_sha256"])
        response = self._post(break_after=str(self.alpha.pk))
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_STALE_SUBMIT)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.snapshot).exists()
        )
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=new_snapshot).exists()
        )

    def test_structurally_stale_post_does_not_write(self):
        self.result.text = "Alpha\nBeta\nGamma edited"
        self.result.source_revision = 2
        self.result.save(update_fields=["text", "source_revision", "updated_at"])
        self._post(break_after=str(self.alpha.pk))
        follow = self.client.get(self.url)
        self.assertContains(follow, MSG_STALE_BINDING)
        self.assertFalse(TranskribusParagraphMapping.objects.exists())

    def test_unavailable_post_does_not_write(self):
        other = _create_doc(title="No snapshot", file_s3_key="no-snap.pdf")
        url = reverse("transkribus-paragraphs", kwargs={"doc_id": other.id})
        response = self.client.post(
            url,
            data={
                "expected_document_id": str(other.id),
                "expected_text_result_id": "1",
                "expected_snapshot_id": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(url)
        self.assertContains(follow, MSG_UNAVAILABLE)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(document=other).exists()
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorPageBoundaryTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-page",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.snapshot = _ready_snapshot(self.doc, text=_PAGED)
        page1 = _add_page(self.snapshot, 1)
        page2 = _add_page(self.snapshot, 2)
        self.alpha = _add_line(page1, 0, "Alpha", start=0, end=5)
        self.beta = _add_line(page1, 1, "Beta", start=6, end=10)
        self.gamma = _add_line(page2, 0, "Gamma", start=12, end=17)
        self.result = _source_row(self.doc, text=_PAGED)
        _bind(text_result=self.result, snapshot=self.snapshot)
        self.url = reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})

    def test_break_around_page_transition_is_optional(self):
        get = self.client.get(self.url)
        freshness = get.context["freshness"]
        self.client.post(
            self.url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
                "break_after": [str(self.alpha.pk)],
            },
        )
        mapping = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertEqual(
            list(mapping.breaks.values_list("after_line_id", flat=True)),
            [self.alpha.pk],
        )
        self.assertFalse(mapping.breaks.filter(after_line_id=self.beta.pk).exists())

    def test_crossing_page_without_break_is_preserved(self):
        get = self.client.get(self.url)
        freshness = get.context["freshness"]
        self.client.post(
            self.url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
            },
        )
        mapping = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertEqual(mapping.breaks.count(), 0)
        follow = self.client.get(self.url)
        self.assertContains(follow, "מעבר לעמוד 2")
        self.assertContains(follow, STATUS_ONE_PARAGRAPH)
        self.assertFalse(
            any(line.break_after_selected for line in follow.context["editor_lines"])
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorQueryTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-query",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def _fixture(self, *, n_lines: int, with_run: bool = True) -> Document:
        words = [f"L{index:02d}" for index in range(n_lines)]
        text = "\n".join(words)
        doc = _create_doc(title=f"Query {n_lines}", file_s3_key=f"q-{n_lines}.pdf")
        snapshot = _ready_snapshot(doc, text=text)
        page = _add_page(snapshot, 1)
        cursor = 0
        line_ids: list[int] = []
        for index, word in enumerate(words):
            line = _add_line(
                page,
                index,
                word,
                start=cursor,
                end=cursor + len(word),
            )
            line_ids.append(line.pk)
            cursor = cursor + len(word) + 1
        result = _source_row(doc, text=text)
        _bind(text_result=result, snapshot=snapshot)
        if n_lines > 2:
            save_paragraph_mapping(snapshot, line_ids[:2])
        if with_run:
            _upload_run(doc)
        return doc

    def test_editor_query_count_does_not_grow_per_line(self):
        small = self._fixture(n_lines=5)
        large = self._fixture(n_lines=40)
        small_url = reverse("transkribus-paragraphs", kwargs={"doc_id": small.id})
        large_url = reverse("transkribus-paragraphs", kwargs={"doc_id": large.id})
        self.client.get(small_url)
        with CaptureQueriesContext(connection) as small_ctx:
            small_resp = self.client.get(small_url)
        self.client.get(large_url)
        with CaptureQueriesContext(connection) as large_ctx:
            large_resp = self.client.get(large_url)
        self.assertEqual(small_resp.status_code, 200)
        self.assertEqual(large_resp.status_code, 200)
        self.assertEqual(len(large_resp.context["editor_lines"]), 40)

        def _line_queries(ctx) -> int:
            return sum(
                1
                for item in ctx.captured_queries
                if "documents_transkribussnapshotline" in item["sql"]
            )

        self.assertEqual(_line_queries(small_ctx), _line_queries(large_ctx))
        self.assertLessEqual(_line_queries(large_ctx), 3)
        self.assertLessEqual(len(large_ctx), len(small_ctx) + 2)

    def test_status_query_count_does_not_grow_per_line(self):
        small = self._fixture(n_lines=5)
        large = self._fixture(n_lines=40)
        versions_small = reverse(
            "corrected-current-sync-attempts", kwargs={"doc_id": small.id}
        )
        versions_large = reverse(
            "corrected-current-sync-attempts", kwargs={"doc_id": large.id}
        )
        detail_small = reverse("documents-detail-page", kwargs={"doc_id": small.id})
        detail_large = reverse("documents-detail-page", kwargs={"doc_id": large.id})
        self.client.get(versions_small)
        with CaptureQueriesContext(connection) as versions_small_ctx:
            self.client.get(versions_small)
        with CaptureQueriesContext(connection) as versions_large_ctx:
            self.client.get(versions_large)

        def _para_queries(ctx) -> int:
            return sum(
                1
                for item in ctx.captured_queries
                if "documents_transkribusparagraph" in item["sql"]
            )

        self.assertEqual(
            _para_queries(versions_small_ctx), _para_queries(versions_large_ctx)
        )
        self.assertLessEqual(_para_queries(versions_large_ctx), 4)

        self.client.get(detail_small)
        with CaptureQueriesContext(connection) as detail_small_ctx:
            self.client.get(detail_small)
        with CaptureQueriesContext(connection) as detail_large_ctx:
            self.client.get(detail_large)
        self.assertEqual(
            _para_queries(detail_small_ctx), _para_queries(detail_large_ctx)
        )
        self.assertLessEqual(_para_queries(detail_large_ctx), 6)


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphEditorNavigationRegressionTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-editor-nav",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        snapshot, _page, _a, _b, _c = _three_line_snapshot(self.doc)
        result = _source_row(self.doc)
        _bind(text_result=result, snapshot=snapshot)
        _upload_run(self.doc)

    def test_review_detail_has_no_paragraph_controls(self):
        response = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": self.doc.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "עריכת חלוקת פסקאות")
        self.assertNotContains(response, "חלוקת פסקאות טרם נשמרה")
        self.assertNotContains(response, 'name="break_after"')

    def test_editor_does_not_self_link_in_staff_nav(self):
        html = self.client.get(
            reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id})
        ).content.decode()
        nav = html.split('aria-label="ניווט ניהול מסמך"', 1)[1].split("</nav>", 1)[0]
        self.assertNotIn(
            reverse("transkribus-paragraphs", kwargs={"doc_id": self.doc.id}),
            nav,
        )
        self.assertNotIn(
            reverse("corrected-current-sync-attempts", kwargs={"doc_id": self.doc.id}),
            nav,
        )

    def test_non_staff_detail_hides_status(self):
        viewer = User.objects.create_user(
            username="para-editor-public",
            password="x",
            is_staff=False,
        )
        self.client.force_login(viewer)
        response = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.doc.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "עריכת חלוקת פסקאות")
        self.assertNotContains(response, STATUS_NEVER_SAVED)


class ParagraphEditorParseTests(SimpleTestCase):
    def test_empty_break_list_is_valid(self):
        post = QueryDict(mutable=True)
        self.assertEqual(parse_break_after_line_ids(post), [])

    def test_duplicate_values_rejected_before_service(self):
        post = QueryDict(mutable=True)
        post.setlist("break_after", ["1", "1"])
        with self.assertRaises(ParagraphEditorError) as ctx:
            parse_break_after_line_ids(post)
        self.assertEqual(ctx.exception.staff_message, MSG_DUPLICATE)
