"""PR4 historical Transkribus paragraph suggestion and adoption tests."""

from __future__ import annotations

import hashlib
import re
from datetime import timedelta
from html.parser import HTMLParser
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.dateformat import format as format_datetime
from django.utils.timezone import localtime

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
from documents.services.transkribus_paragraph_adoption import (
    ParagraphMappingAdoptionError,
    ParagraphMappingAdoptionRefusal,
    adopt_historical_paragraph_mapping,
    remap_break_after_line_ids,
)
from documents.services.transkribus_paragraph_correspondence import (
    prove_contributing_line_correspondence,
)
from documents.services.transkribus_paragraph_mapping import (
    get_paragraph_mapping_for_snapshot,
    save_paragraph_mapping,
)
from documents.services.transkribus_paragraph_staff import (
    ADOPTION_ACTION_LABEL,
    ADOPTION_INTRO_MULTIPLE,
    ADOPTION_INTRO_SINGLE,
    MSG_ADOPTED,
    MSG_ADOPT_ALREADY_EXISTS,
    MSG_ADOPT_UNAVAILABLE,
    MSG_SAVED,
    MSG_STALE_SUBMIT,
    STATUS_HISTORICAL_NOTE,
    STATUS_NEVER_SAVED,
    STATUS_ONE_PARAGRAPH,
    build_paragraph_editor_context,
    paragraph_count_phrase,
    status_n_paragraphs,
)

User = get_user_model()

_PARSER = "test_paragraph_adoption_v1"
_ENGINE = "transkribus-pylaia:42"
_SOURCE_IDS = ("line-a", "line-b", "line-c")
_TARGET_IDS = ("line-a", "line-b", "line-c")


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
        title="Paragraph adoption doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.READY,
        file_s3_key="paragraph-adoption.pdf",
        mime_type="application/pdf",
        visibility=Document.Visibility.PUBLIC,
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _ready_snapshot(
    document: Document,
    *,
    text: str,
    parser_version: str = _PARSER,
    created_at=None,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{TranskribusTranscriptSnapshot.objects.count()}:{text}"
    snapshot = TranskribusTranscriptSnapshot.objects.create(
        document=document,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
        parser_version=parser_version,
        provider_identity_fingerprint=_sha(f"prov:{unique}"),
        raw_xml_fingerprint=_sha(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=True,
        storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
    )
    if created_at is not None:
        TranskribusTranscriptSnapshot.objects.filter(pk=snapshot.pk).update(
            created_at=created_at
        )
        snapshot.refresh_from_db()
    return snapshot


def _add_page(
    snapshot: TranskribusTranscriptSnapshot, page_index: int
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
    provider_line_id: str,
    contributes: bool = True,
) -> TranskribusSnapshotLine:
    return TranskribusSnapshotLine.objects.create(
        page=page,
        order_index=order,
        provider_region_id="region-ignored",
        provider_line_id=provider_line_id,
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


def _three_line_snapshot(
    document: Document,
    *,
    text: str,
    provider_ids: tuple[str, str, str] = _SOURCE_IDS,
    created_at=None,
    parser_version: str = _PARSER,
) -> tuple[TranskribusTranscriptSnapshot, list[TranskribusSnapshotLine]]:
    words = text.split("\n")
    snapshot = _ready_snapshot(
        document,
        text=text,
        parser_version=parser_version,
        created_at=created_at,
    )
    page = _add_page(snapshot, 1)
    lines = []
    cursor = 0
    for order, (provider_id, word) in enumerate(zip(provider_ids, words, strict=True)):
        line = _add_line(
            page,
            order,
            word,
            start=cursor,
            end=cursor + max(len(word), 1),
            provider_line_id=provider_id,
        )
        lines.append(line)
        cursor = cursor + len(word) + 1
    return snapshot, lines


def _source_row(doc: Document, *, text: str) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=_ENGINE,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
        source_revision=1,
    )


def _bind(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
) -> TranskribusTextResultBinding:
    return TranskribusTextResultBinding.objects.create(
        text_result=text_result,
        snapshot=snapshot,
        binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        bound_text_sha256=_sha(text_result.text or ""),
        bound_source_revision=1,
    )


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


def _version_label(created_at) -> str:
    return format_datetime(localtime(created_at), "d.m.Y H:i")


class RemapBreakTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()
        self.now = timezone.now()
        self.source, self.source_lines = _three_line_snapshot(
            self.doc,
            text="Alpha\nBeta\nGamma",
            created_at=self.now - timedelta(hours=2),
        )
        self.target, self.target_lines = _three_line_snapshot(
            self.doc,
            text="Alfa\nBetta\nGama",
            provider_ids=_TARGET_IDS,
            created_at=self.now,
        )

    def test_remaps_source_break_pks_to_target_pks(self):
        proof = prove_contributing_line_correspondence(self.source, self.target)
        remapped = remap_break_after_line_ids(proof, [self.source_lines[0].pk])
        self.assertEqual(remapped, [self.target_lines[0].pk])
        self.assertNotEqual(self.source_lines[0].pk, self.target_lines[0].pk)

    def test_zero_breaks_remap_to_empty(self):
        proof = prove_contributing_line_correspondence(self.source, self.target)
        self.assertEqual(remap_break_after_line_ids(proof, []), [])

    def test_unknown_source_break_refuses(self):
        proof = prove_contributing_line_correspondence(self.source, self.target)
        with self.assertRaises(ParagraphMappingAdoptionError) as raised:
            remap_break_after_line_ids(proof, [999999])
        self.assertEqual(
            raised.exception.code,
            ParagraphMappingAdoptionRefusal.BREAK_REMAP_FAILED,
        )


class AdoptHistoricalMappingServiceTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="para-adopt-svc",
            password="x",
            is_staff=True,
        )
        self.doc = _create_doc()
        self.now = timezone.now()
        self.source, self.source_lines = _three_line_snapshot(
            self.doc,
            text="Alpha\nBeta\nGamma",
            created_at=self.now - timedelta(hours=3),
        )
        self.target, self.target_lines = _three_line_snapshot(
            self.doc,
            text="Alfa\nBetta\nGama",
            provider_ids=_TARGET_IDS,
            created_at=self.now,
        )

    def test_creates_new_target_mapping_and_leaves_source_unchanged(self):
        source_mapping = save_paragraph_mapping(
            self.source, [self.source_lines[0].pk], actor=self.user
        )
        created = adopt_historical_paragraph_mapping(
            source_mapping,
            self.target,
            actor=self.user,
        )
        self.assertNotEqual(created.pk, source_mapping.pk)
        self.assertEqual(created.snapshot_id, self.target.pk)
        self.assertEqual(created.copied_from_id, source_mapping.pk)
        self.assertEqual(
            list(created.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[0].pk],
        )
        source_mapping.refresh_from_db()
        self.assertEqual(
            list(source_mapping.breaks.values_list("after_line_id", flat=True)),
            [self.source_lines[0].pk],
        )
        self.assertIsNone(source_mapping.copied_from_id)
        self.assertEqual(created.created_by_id, self.user.pk)

    def test_zero_break_adoption_creates_explicit_empty_target(self):
        source_mapping = save_paragraph_mapping(self.source, [])
        created = adopt_historical_paragraph_mapping(source_mapping, self.target)
        self.assertEqual(created.breaks.count(), 0)
        self.assertEqual(created.copied_from_id, source_mapping.pk)
        self.assertTrue(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_refuses_when_target_mapping_exists(self):
        source_mapping = save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        current = save_paragraph_mapping(self.target, [self.target_lines[1].pk])
        with self.assertRaises(ParagraphMappingAdoptionError) as raised:
            adopt_historical_paragraph_mapping(source_mapping, self.target)
        self.assertEqual(
            raised.exception.code,
            ParagraphMappingAdoptionRefusal.TARGET_MAPPING_EXISTS,
        )
        current.refresh_from_db()
        self.assertEqual(
            list(current.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[1].pk],
        )
        self.assertIsNone(current.copied_from_id)

    def test_refuses_unsafe_correspondence(self):
        unsafe, unsafe_lines = _three_line_snapshot(
            self.doc,
            text="X\nY\nZ",
            provider_ids=("line-x", "line-y", "line-z"),
            created_at=self.now - timedelta(hours=1),
        )
        mapping = save_paragraph_mapping(unsafe, [unsafe_lines[0].pk])
        with self.assertRaises(ParagraphMappingAdoptionError) as raised:
            adopt_historical_paragraph_mapping(mapping, self.target)
        self.assertEqual(
            raised.exception.code,
            ParagraphMappingAdoptionRefusal.CORRESPONDENCE_UNPROVEN,
        )
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_refuses_source_that_is_not_historical(self):
        newer, newer_lines = _three_line_snapshot(
            self.doc,
            text="New\nBeta\nGamma",
            created_at=self.now + timedelta(hours=1),
        )
        mapping = save_paragraph_mapping(newer, [newer_lines[0].pk])
        with self.assertRaises(ParagraphMappingAdoptionError) as raised:
            adopt_historical_paragraph_mapping(mapping, self.target)
        self.assertEqual(
            raised.exception.code,
            ParagraphMappingAdoptionRefusal.SOURCE_NOT_HISTORICAL,
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class HistoricalSuggestionEditorTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-adopt-ui",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.now = timezone.now()
        self.source, self.source_lines = _three_line_snapshot(
            self.doc,
            text="Alpha\nBeta\nGamma",
            created_at=self.now - timedelta(hours=5),
        )
        self.target, self.target_lines = _three_line_snapshot(
            self.doc,
            text="Alfa\nBetta\nGama",
            provider_ids=_TARGET_IDS,
            created_at=self.now,
        )
        self.result = _source_row(self.doc, text="Alfa\nBetta\nGama")
        _bind(text_result=self.result, snapshot=self.target)
        _upload_run(self.doc)
        self.editor_url = reverse(
            "transkribus-paragraphs", kwargs={"doc_id": self.doc.id}
        )
        self.adopt_url = reverse(
            "transkribus-paragraphs-adopt", kwargs={"doc_id": self.doc.id}
        )
        self.versions_url = reverse(
            "corrected-current-sync-attempts", kwargs={"doc_id": self.doc.id}
        )
        self.detail_url = reverse(
            "documents-detail-page", kwargs={"doc_id": self.doc.id}
        )

    def test_eligible_historical_mapping_is_offered_when_current_has_none(self):
        mapping = save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        response = self.client.get(self.editor_url)
        self.assertEqual(response.status_code, 200)
        suggestions = list(response.context["adoption_suggestions"])
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].mapping_id, mapping.pk)
        self.assertEqual(suggestions[0].paragraph_count, 2)
        self.assertContains(response, ADOPTION_INTRO_SINGLE)
        self.assertContains(response, ADOPTION_ACTION_LABEL)
        self.assertContains(response, suggestions[0].label)
        self.assertContains(response, _version_label(self.source.created_at))
        self.assertContains(response, paragraph_count_phrase(2))
        html = response.content.decode()
        self.assertIn(self.adopt_url, html)
        checked = re.findall(
            r'name="break_after"[^>]*checked|checked[^>]*name="break_after"',
            html,
        )
        self.assertEqual(checked, [])

    def test_historical_mapping_alone_does_not_count_as_current(self):
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        status = build_paragraph_editor_context(self.doc).status
        self.assertEqual(status.code, "NEVER_SAVED")
        versions = self.client.get(self.versions_url)
        self.assertContains(versions, STATUS_NEVER_SAVED)
        self.assertContains(versions, STATUS_HISTORICAL_NOTE)
        self.assertNotContains(versions, ADOPTION_ACTION_LABEL)
        self.assertNotIn(self.adopt_url, versions.content.decode())
        detail = self.client.get(self.detail_url)
        self.assertContains(detail, STATUS_NEVER_SAVED)
        self.assertNotContains(detail, ADOPTION_ACTION_LABEL)
        self.assertNotIn(self.adopt_url, detail.content.decode())

    def test_no_suggestion_when_correspondence_is_unsafe(self):
        unsafe, unsafe_lines = _three_line_snapshot(
            self.doc,
            text="X\nY\nZ",
            provider_ids=("line-x", "line-y", "line-z"),
            created_at=self.now - timedelta(hours=2),
        )
        save_paragraph_mapping(unsafe, [unsafe_lines[0].pk])
        response = self.client.get(self.editor_url)
        self.assertEqual(list(response.context["adoption_suggestions"]), [])
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)
        self.assertNotIn(self.adopt_url, response.content.decode())
        self.assertNotContains(response, "IDENTITY_SEQUENCE_MISMATCH")
        self.assertNotContains(response, "MISSING_PROVIDER_LINE_ID")

    def test_duplicate_provider_line_ids_refuse_eligibility(self):
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        self.target_lines[1].provider_line_id = self.target_lines[0].provider_line_id
        self.target_lines[1].save(update_fields=["provider_line_id"])
        response = self.client.get(self.editor_url)
        self.assertEqual(list(response.context["adoption_suggestions"]), [])
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)

    def test_empty_provider_line_ids_refuse_eligibility(self):
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        self.target_lines[1].provider_line_id = ""
        self.target_lines[1].save(update_fields=["provider_line_id"])
        response = self.client.get(self.editor_url)
        self.assertEqual(list(response.context["adoption_suggestions"]), [])
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)

    def test_changed_line_sequence_refuses_eligibility(self):
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        self.target_lines[0].provider_line_id = "line-b"
        self.target_lines[1].provider_line_id = "line-a"
        self.target_lines[0].save(update_fields=["provider_line_id"])
        self.target_lines[1].save(update_fields=["provider_line_id"])
        response = self.client.get(self.editor_url)
        self.assertEqual(list(response.context["adoption_suggestions"]), [])
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)

    def test_zero_break_historical_mapping_is_offered(self):
        mapping = save_paragraph_mapping(self.source, [])
        response = self.client.get(self.editor_url)
        suggestions = list(response.context["adoption_suggestions"])
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0].mapping_id, mapping.pk)
        self.assertEqual(suggestions[0].break_count, 0)
        self.assertEqual(suggestions[0].paragraph_count, 1)
        self.assertContains(response, "פסקה אחת")
        self.assertContains(response, ADOPTION_ACTION_LABEL)

    def test_technical_ids_are_not_visible(self):
        mapping = save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        response = self.client.get(self.editor_url)
        visible = _visible_text(response.content.decode())
        label = response.context["adoption_suggestions"][0].label
        self.assertNotIn(f"snapshot={self.source.pk}", visible)
        self.assertNotIn(f"snapshot={self.target.pk}", visible)
        self.assertNotIn(f"mapping={mapping.pk}", visible)
        self.assertNotIn(f"binding={self.result.pk}", visible)
        self.assertNotIn("line-a", visible)
        self.assertNotIn(self.source.provider_identity_fingerprint, visible)
        self.assertNotIn("copied_from", visible)
        self.assertNotIn("snapshot", visible.lower())
        self.assertNotIn("binding", visible.lower())
        self.assertNotIn("line-a", label)
        self.assertIn(_version_label(self.source.created_at), label)
        self.assertIn(paragraph_count_phrase(2), label)

    def test_no_suggestion_when_current_mapping_exists(self):
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        save_paragraph_mapping(self.target, [self.target_lines[0].pk])
        response = self.client.get(self.editor_url)
        self.assertEqual(list(response.context["adoption_suggestions"]), [])
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)

    @patch(
        "documents.services.transkribus_paragraph_staff."
        "discover_transferable_historical_mappings"
    )
    def test_versions_and_detail_do_not_run_discovery(self, mock_discover):
        mock_discover.return_value = ()
        save_paragraph_mapping(self.source, [self.source_lines[0].pk])
        self.client.get(self.versions_url)
        self.client.get(self.detail_url)
        mock_discover.assert_not_called()
        self.client.get(self.editor_url)
        mock_discover.assert_called_once()


@override_settings(UPLOADS_BUCKET_NAME="")
class MultipleHistoricalSourceTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-adopt-multi",
            password="x",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.now = timezone.now()
        self.oldest, self.oldest_lines = _three_line_snapshot(
            self.doc,
            text="Old\nBeta\nGamma",
            created_at=self.now - timedelta(hours=9),
        )
        self.middle, self.middle_lines = _three_line_snapshot(
            self.doc,
            text="Mid\nBeta\nGamma",
            provider_ids=_TARGET_IDS,
            created_at=self.now - timedelta(hours=4),
        )
        self.unsafe, self.unsafe_lines = _three_line_snapshot(
            self.doc,
            text="X\nY\nZ",
            provider_ids=("line-x", "line-y", "line-z"),
            created_at=self.now - timedelta(hours=2),
        )
        self.target, self.target_lines = _three_line_snapshot(
            self.doc,
            text="Now\nBeta\nGamma",
            provider_ids=_TARGET_IDS,
            created_at=self.now,
        )
        self.result = _source_row(self.doc, text="Now\nBeta\nGamma")
        _bind(text_result=self.result, snapshot=self.target)
        self.oldest_mapping = save_paragraph_mapping(
            self.oldest, [self.oldest_lines[0].pk, self.oldest_lines[1].pk]
        )
        self.middle_mapping = save_paragraph_mapping(
            self.middle, [self.middle_lines[1].pk]
        )
        save_paragraph_mapping(self.unsafe, [self.unsafe_lines[0].pk])
        self.editor_url = reverse(
            "transkribus-paragraphs", kwargs={"doc_id": self.doc.id}
        )
        self.adopt_url = reverse(
            "transkribus-paragraphs-adopt", kwargs={"doc_id": self.doc.id}
        )

    def _freshness(self):
        response = self.client.get(self.editor_url)
        return response.context["freshness"], response

    def _adopt(self, mapping, snapshot, *, freshness=None):
        token = freshness or self._freshness()[0]
        return self.client.post(
            self.adopt_url,
            data={
                "expected_document_id": str(token.document_id),
                "expected_text_result_id": str(token.text_result_id),
                "expected_snapshot_id": str(token.snapshot_id),
                "expected_source_mapping_id": str(mapping.pk),
                "expected_source_snapshot_id": str(snapshot.pk),
            },
        )

    def test_two_eligible_mappings_are_both_displayed_newest_first(self):
        freshness, response = self._freshness()
        suggestions = list(response.context["adoption_suggestions"])
        self.assertEqual(len(suggestions), 2)
        self.assertEqual(
            [item.mapping_id for item in suggestions],
            [self.middle_mapping.pk, self.oldest_mapping.pk],
        )
        self.assertContains(response, ADOPTION_INTRO_MULTIPLE)
        self.assertContains(response, suggestions[0].label)
        self.assertContains(response, suggestions[1].label)
        self.assertEqual(suggestions[0].paragraph_count, 2)
        self.assertEqual(suggestions[1].paragraph_count, 3)
        self.assertEqual(response.content.decode().count(ADOPTION_ACTION_LABEL), 2)
        html = response.content.decode()
        middle_at = html.find(suggestions[0].label)
        oldest_at = html.find(suggestions[1].label)
        self.assertLess(middle_at, oldest_at)
        self.assertIsNotNone(freshness)
        checked = re.findall(
            r'name="break_after"[^>]*checked|checked[^>]*name="break_after"',
            html,
        )
        self.assertEqual(checked, [])

    def test_neither_candidate_is_automatically_adopted(self):
        self.client.get(self.editor_url)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_adopting_newest_candidate_copies_that_mapping_not_the_older(self):
        response = self._adopt(self.middle_mapping, self.middle)
        self.assertEqual(response.status_code, 302)
        created = get_paragraph_mapping_for_snapshot(self.target)
        self.assertEqual(created.copied_from_id, self.middle_mapping.pk)
        self.assertEqual(
            list(created.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[1].pk],
        )
        self.assertNotEqual(created.copied_from_id, self.oldest_mapping.pk)

    def test_adopting_older_candidate_copies_that_mapping_not_the_newer(self):
        response = self._adopt(self.oldest_mapping, self.oldest)
        self.assertEqual(response.status_code, 302)
        created = get_paragraph_mapping_for_snapshot(self.target)
        self.assertEqual(created.copied_from_id, self.oldest_mapping.pk)
        self.assertEqual(
            set(created.breaks.values_list("after_line_id", flat=True)),
            {self.target_lines[0].pk, self.target_lines[1].pk},
        )

    def test_unsafe_candidate_does_not_hide_safe_candidates(self):
        _, response = self._freshness()
        ids = [item.mapping_id for item in response.context["adoption_suggestions"]]
        self.assertEqual(ids, [self.middle_mapping.pk, self.oldest_mapping.pk])
        self.assertNotIn(self.unsafe.pk, ids)


@override_settings(UPLOADS_BUCKET_NAME="")
class ParagraphAdoptionPostTests(TestCase):
    def setUp(self) -> None:
        self.staff = User.objects.create_user(
            username="para-adopt-post",
            password="x",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="para-adopt-viewer",
            password="x",
            is_staff=False,
        )
        self.client.force_login(self.staff)
        self.doc = _create_doc()
        self.now = timezone.now()
        self.source, self.source_lines = _three_line_snapshot(
            self.doc,
            text="Alpha\nBeta\nGamma",
            created_at=self.now - timedelta(hours=2),
        )
        self.target, self.target_lines = _three_line_snapshot(
            self.doc,
            text="Alfa\nBetta\nGama",
            provider_ids=_TARGET_IDS,
            created_at=self.now,
        )
        self.result = _source_row(self.doc, text="Alfa\nBetta\nGama")
        _bind(text_result=self.result, snapshot=self.target)
        self.source_mapping = save_paragraph_mapping(
            self.source, [self.source_lines[0].pk]
        )
        _upload_run(self.doc)
        self.editor_url = reverse(
            "transkribus-paragraphs", kwargs={"doc_id": self.doc.id}
        )
        self.adopt_url = reverse(
            "transkribus-paragraphs-adopt", kwargs={"doc_id": self.doc.id}
        )
        self.detail_url = reverse(
            "documents-detail-page", kwargs={"doc_id": self.doc.id}
        )

    def _freshness(self):
        response = self.client.get(self.editor_url)
        self.assertEqual(response.status_code, 200)
        return response.context["freshness"]

    def _adopt_data(self, *, freshness=None, mapping=None, source_snapshot=None):
        token = freshness or self._freshness()
        source = mapping or self.source_mapping
        snapshot = source_snapshot or self.source
        return {
            "expected_document_id": str(token.document_id),
            "expected_text_result_id": str(token.text_result_id),
            "expected_snapshot_id": str(token.snapshot_id),
            "expected_source_mapping_id": str(source.pk),
            "expected_source_snapshot_id": str(snapshot.pk),
        }

    def test_explicit_post_creates_new_mapping_with_prg_and_success_message(self):
        response = self.client.post(self.adopt_url, data=self._adopt_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.editor_url)
        created = get_paragraph_mapping_for_snapshot(self.target)
        self.assertIsNotNone(created)
        self.assertEqual(created.copied_from_id, self.source_mapping.pk)
        self.assertEqual(
            list(created.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[0].pk],
        )
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_ADOPTED)
        self.assertContains(follow, status_n_paragraphs(2))
        self.assertEqual(list(follow.context["adoption_suggestions"]), [])
        self.source_mapping.refresh_from_db()
        self.assertEqual(
            list(self.source_mapping.breaks.values_list("after_line_id", flat=True)),
            [self.source_lines[0].pk],
        )

    def test_zero_break_post_creates_explicit_target_mapping(self):
        zero = save_paragraph_mapping(self.source, [])
        response = self.client.post(
            self.adopt_url,
            data=self._adopt_data(mapping=zero),
        )
        self.assertEqual(response.status_code, 302)
        created = get_paragraph_mapping_for_snapshot(self.target)
        self.assertEqual(created.breaks.count(), 0)
        self.assertEqual(created.copied_from_id, zero.pk)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_ADOPTED)
        self.assertContains(follow, STATUS_ONE_PARAGRAPH)

    def test_current_mapping_already_exists_refuses_unchanged(self):
        current = save_paragraph_mapping(self.target, [self.target_lines[1].pk])
        response = self.client.post(self.adopt_url, data=self._adopt_data())
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_ADOPT_ALREADY_EXISTS)
        current.refresh_from_db()
        self.assertEqual(
            list(current.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[1].pk],
        )
        self.assertIsNone(current.copied_from_id)
        self.assertEqual(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).count(),
            1,
        )

    def test_mapping_created_between_get_and_post_refuses(self):
        freshness = self._freshness()
        current = save_paragraph_mapping(self.target, [self.target_lines[1].pk])
        response = self.client.post(
            self.adopt_url,
            data=self._adopt_data(freshness=freshness),
        )
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_ADOPT_ALREADY_EXISTS)
        current.refresh_from_db()
        self.assertEqual(current.copied_from_id, None)
        self.assertEqual(
            list(current.breaks.values_list("after_line_id", flat=True)),
            [self.target_lines[1].pk],
        )

    def test_displayed_snapshot_change_between_get_and_post_refuses(self):
        freshness = self._freshness()
        new_text = "New\nBeta\nGamma"
        new_snapshot, _ = _three_line_snapshot(
            self.doc,
            text=new_text,
            created_at=self.now + timedelta(minutes=1),
        )
        self.result.text = new_text
        self.result.save(update_fields=["text", "updated_at"])
        binding = TranskribusTextResultBinding.objects.get(text_result=self.result)
        binding.snapshot = new_snapshot
        binding.bound_text_sha256 = _sha(new_text)
        binding.save(update_fields=["snapshot", "bound_text_sha256"])
        response = self.client.post(
            self.adopt_url,
            data=self._adopt_data(freshness=freshness),
        )
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_STALE_SUBMIT)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=new_snapshot).exists()
        )

    def test_source_mapping_deleted_between_get_and_post_refuses(self):
        freshness = self._freshness()
        mapping_id = self.source_mapping.pk
        snapshot_id = self.source.pk
        self.source_mapping.delete()
        response = self.client.post(
            self.adopt_url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
                "expected_source_mapping_id": str(mapping_id),
                "expected_source_snapshot_id": str(snapshot_id),
            },
        )
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_ADOPT_UNAVAILABLE)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_source_snapshot_token_mismatch_refuses(self):
        freshness = self._freshness()
        response = self.client.post(
            self.adopt_url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
                "expected_source_mapping_id": str(self.source_mapping.pk),
                "expected_source_snapshot_id": str(self.target.pk),
            },
        )
        follow = self.client.get(response["Location"])
        self.assertContains(follow, MSG_ADOPT_UNAVAILABLE)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_correspondence_valid_on_get_invalid_on_post_refuses(self):
        freshness = self._freshness()
        self.assertTrue(
            self.client.get(self.editor_url).context["adoption_suggestions"]
        )
        self.target_lines[1].provider_line_id = "line-changed"
        self.target_lines[1].save(update_fields=["provider_line_id"])
        response = self.client.post(
            self.adopt_url,
            data=self._adopt_data(freshness=freshness),
        )
        follow = self.client.get(response["Location"])
        self.assertContains(follow, MSG_ADOPT_UNAVAILABLE)
        self.assertNotContains(follow, "IDENTITY_SEQUENCE_MISMATCH")
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_ordinary_manual_save_is_not_a_copied_mapping(self):
        freshness = self._freshness()
        response = self.client.post(
            self.editor_url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
                "break_after": [str(self.target_lines[0].pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        mapping = get_paragraph_mapping_for_snapshot(self.target)
        self.assertIsNone(mapping.copied_from_id)
        follow = self.client.get(self.editor_url)
        self.assertContains(follow, MSG_SAVED)
        self.assertNotContains(follow, MSG_ADOPTED)

    def test_manual_zero_break_save_remains_copied_from_none(self):
        freshness = self._freshness()
        self.client.post(
            self.editor_url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
            },
        )
        mapping = get_paragraph_mapping_for_snapshot(self.target)
        self.assertEqual(mapping.breaks.count(), 0)
        self.assertIsNone(mapping.copied_from_id)

    def test_manual_resave_after_adoption_clears_copied_from(self):
        self.client.post(self.adopt_url, data=self._adopt_data())
        mapping = get_paragraph_mapping_for_snapshot(self.target)
        self.assertEqual(mapping.copied_from_id, self.source_mapping.pk)
        freshness = self.client.get(self.editor_url).context["freshness"]
        self.client.post(
            self.editor_url,
            data={
                "expected_document_id": str(freshness.document_id),
                "expected_text_result_id": str(freshness.text_result_id),
                "expected_snapshot_id": str(freshness.snapshot_id),
                "break_after": [str(self.target_lines[1].pk)],
            },
        )
        mapping.refresh_from_db()
        self.assertIsNone(mapping.copied_from_id)
        self.assertEqual(mapping.breaks.get().after_line_id, self.target_lines[1].pk)

    def test_anonymous_adopt_redirects_to_login(self):
        data = self._adopt_data()
        self.client.logout()
        response = self.client.post(self.adopt_url, data=data)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_non_staff_adopt_gets_403(self):
        self.client.force_login(self.viewer)
        response = self.client.post(self.adopt_url, data={"expected_document_id": "1"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_adopt_post_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        response = csrf_client.post(self.adopt_url, data=self._adopt_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=self.target).exists()
        )

    def test_public_page_does_not_activate_historical_mapping(self):
        public = self.client.get(self.detail_url)
        presentation = public.context["transkribus_paragraph_presentation"]
        self.assertFalse(presentation.enabled)
        self.assertNotIn(
            "document-detail-transcription-paragraph",
            public.content.decode(),
        )
        self.client.post(self.adopt_url, data=self._adopt_data())
        after = self.client.get(self.detail_url)
        self.assertTrue(after.context["transkribus_paragraph_presentation"].enabled)
        self.assertIn(
            "document-detail-transcription-paragraph",
            after.content.decode(),
        )
        self.assertNotContains(after, ADOPTION_ACTION_LABEL)

    def test_review_detail_has_no_adoption_controls(self):
        response = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": self.doc.id})
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, ADOPTION_ACTION_LABEL)
        self.assertNotIn(self.adopt_url, response.content.decode())
