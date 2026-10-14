"""Parser v2 minor polygon clamping and the scoped hover-geometry repair."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

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
from documents.services import transkribus_engine as tr
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_binding_freshness import (
    is_binding_trusted_for_hover,
)
from documents.services.transkribus_hover_geometry_repair import (
    HoverGeometryRepairError,
    apply_hover_geometry_repair,
    plan_hover_geometry_repair,
)
from documents.services.transkribus_page_xml_types import SnapshotPageInput
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION_V1,
    PARSER_VERSION_V2,
    compute_sha256_hex,
    parse_document_pages_for_snapshot,
    parse_page_xml_for_snapshot,
)
from documents.services.transkribus_snapshot_storage import store_transkribus_transcript_snapshot

_ENGINE = "transkribus-pylaia:42"


def _page_xml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        f"{body}\n"
        "</PcGts>"
    ).encode("utf-8")


def _line_xml(*, line_id: str, points: str, text: str) -> str:
    return f"""
      <TextLine id="{line_id}">
        <Coords points="{points}"/>
        <Baseline points="10,20 40,20"/>
        <TextEquiv><Unicode>{text}</Unicode></TextEquiv>
      </TextLine>
"""


def _page_body(*, lines: str, width: int = 3000, height: int = 4000) -> str:
    return f"""
  <Page imageFilename="page.png" imageWidth="{width}" imageHeight="{height}">
    <TextRegion id="r1">
      {lines}
    </TextRegion>
  </Page>
"""


class PolygonNormalizationParserTests(TestCase):
    def test_document_358_point_clamps_and_preserves_text(self):
        xml = _page_xml(
            _page_body(lines=_line_xml(line_id="l29464", points="3001,1219 10,10 20,30", text="שורה"))
        )
        v1 = parse_page_xml_for_snapshot(
            xml,
            page_index=2,
            page_nr=2,
            transcript_ts_id="ts",
            parser_version=PARSER_VERSION_V1,
        )
        v2 = parse_page_xml_for_snapshot(
            xml,
            page_index=2,
            page_nr=2,
            transcript_ts_id="ts",
            parser_version=PARSER_VERSION_V2,
        )
        self.assertFalse(v1.lines[0].coords_valid)
        self.assertEqual(v1.page_geometry_capability, "PARTIAL")
        self.assertEqual(v1.lines[0].polygon_points[0], (3001.0, 1219.0))
        self.assertTrue(v2.lines[0].coords_valid)
        self.assertEqual(v2.lines[0].polygon_points[0], (3000.0, 1219.0))
        self.assertEqual(v2.lines[0].bbox.min_x, 10.0)
        self.assertEqual(v2.lines[0].bbox.max_x, 3000.0)
        self.assertEqual(v2.page_geometry_capability, "VERIFIED")
        self.assertEqual(v2.canonical_text, v1.canonical_text)
        self.assertEqual(v2.lines[0].char_start, v1.lines[0].char_start)
        self.assertEqual(v2.lines[0].char_end, v1.lines[0].char_end)
        self.assertEqual(compute_sha256_hex(xml), v2.page_xml_sha256)
        self.assertIn("max_excursion_px=1", v2.warnings[0])

    def test_document_393_negative_point_clamps_to_zero(self):
        xml = _page_xml(
            _page_body(lines=_line_xml(line_id="l30787", points="-5,1377 10,10 20,30", text="שורה"))
        )
        parsed = parse_page_xml_for_snapshot(
            xml, page_index=2, page_nr=2, transcript_ts_id="ts"
        )
        self.assertEqual(parsed.lines[0].polygon_points[0], (0.0, 1377.0))
        self.assertTrue(parsed.lines[0].coords_valid)
        self.assertEqual(parsed.page_geometry_capability, "VERIFIED")

    def test_five_pixel_boundary_is_clamped_and_six_is_rejected(self):
        inside = _page_xml(
            _page_body(
                lines=_line_xml(line_id="edge", points="3005,10 10,10 20,30", text="א"),
            )
        )
        outside = _page_xml(
            _page_body(
                lines=_line_xml(line_id="far", points="3006,10 10,10 20,30", text="א"),
            )
        )
        clamped = parse_page_xml_for_snapshot(
            inside, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        rejected = parse_page_xml_for_snapshot(
            outside, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        self.assertEqual(clamped.lines[0].polygon_points[0], (3000.0, 10.0))
        self.assertTrue(clamped.lines[0].coords_valid)
        self.assertEqual(clamped.page_geometry_capability, "VERIFIED")
        self.assertFalse(rejected.lines[0].coords_valid)
        self.assertEqual(rejected.lines[0].polygon_points[0], (3006.0, 10.0))
        self.assertEqual(rejected.polygons_rejected_coordinate_excursion, 1)
        self.assertEqual(rejected.page_geometry_capability, "PARTIAL")

    def test_malformed_degenerate_and_missing_dimensions_stay_invalid(self):
        malformed = _page_xml(
            _page_body(
                lines="""
      <TextLine id="bad">
        <Coords points="bad"/>
        <TextEquiv><Unicode>א</Unicode></TextEquiv>
      </TextLine>
"""
            )
        )
        collapsed = _page_xml(
            _page_body(
                lines=_line_xml(
                    line_id="collapse",
                    points="-1,0 0,-1 1,1",
                    text="א",
                ),
                width=10,
                height=10,
            )
        )
        missing_dimensions = _page_xml(
            """
  <Page>
    <TextLine id="neg">
      <Coords points="-1,10 10,10 10,20"/>
      <TextEquiv><Unicode>א</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        )
        bad = parse_page_xml_for_snapshot(
            malformed, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        degen = parse_page_xml_for_snapshot(
            collapsed, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        missing = parse_page_xml_for_snapshot(
            missing_dimensions, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        self.assertFalse(bad.lines[0].coords_valid)
        self.assertEqual(bad.lines[0].polygon_points, ())
        self.assertFalse(degen.lines[0].coords_valid)
        self.assertEqual(degen.lines[0].polygon_points[0], (-1.0, 0.0))
        self.assertFalse(missing.lines[0].coords_valid)
        self.assertEqual(missing.page_geometry_capability, "PARTIAL")

    def test_clamp_that_makes_points_collinear_stays_invalid(self):
        collapsed = _page_xml(
            _page_body(
                lines=_line_xml(line_id="flat", points="0,0 50,0 100,-1", text="א"),
                width=100,
                height=100,
            )
        )
        still_a_triangle = _page_xml(
            _page_body(
                lines=_line_xml(line_id="thin", points="0,-1 50,10 100,0", text="א"),
                width=100,
                height=100,
            )
        )
        degen = parse_page_xml_for_snapshot(
            collapsed, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        kept = parse_page_xml_for_snapshot(
            still_a_triangle, page_index=1, page_nr=1, transcript_ts_id="ts"
        )
        self.assertFalse(degen.lines[0].coords_valid)
        self.assertEqual(
            degen.lines[0].polygon_points,
            ((0.0, 0.0), (50.0, 0.0), (100.0, -1.0)),
        )
        self.assertEqual(degen.page_geometry_capability, "PARTIAL")
        self.assertEqual(degen.polygons_normalized_into_bounds, 0)
        self.assertEqual(kept.lines[0].polygon_points[0], (0.0, 0.0))
        self.assertTrue(kept.lines[0].coords_valid)
        self.assertEqual(kept.page_geometry_capability, "VERIFIED")

    def test_one_minor_page_makes_the_document_hover_eligible(self):
        good = _page_xml(
            _page_body(lines=_line_xml(line_id="ok", points="10,10 40,10 40,40", text="א"))
        )
        minor = _page_xml(
            _page_body(
                lines=_line_xml(line_id="edge", points="3001,1219 10,10 20,30", text="ב")
            )
        )
        pages = [
            SnapshotPageInput(1, 1, "ts-1", good),
            SnapshotPageInput(2, 2, "ts-2", minor),
        ]
        v1 = parse_document_pages_for_snapshot(pages, parser_version=PARSER_VERSION_V1)
        v2 = parse_document_pages_for_snapshot(pages, parser_version=PARSER_VERSION_V2)
        self.assertFalse(v1.hover_eligible)
        self.assertEqual(v1.geometry_capability, "PARTIAL")
        self.assertTrue(v2.hover_eligible)
        self.assertEqual(v2.geometry_capability, "VERIFIED")
        self.assertEqual(v2.parser_version, PARSER_VERSION_V2)
        self.assertEqual(v2.canonical_text, v1.canonical_text)
        self.assertEqual(v2.canonical_text_sha256, v1.canonical_text_sha256)
        self.assertEqual(v2.pages[1].lines[0].char_start, v1.pages[1].lines[0].char_start)

    def test_large_excursion_keeps_document_hover_ineligible(self):
        good = _page_xml(
            _page_body(lines=_line_xml(line_id="ok", points="10,10 40,10 40,40", text="א"))
        )
        far = _page_xml(
            _page_body(lines=_line_xml(line_id="far", points="3006,10 10,10 20,30", text="ב"))
        )
        parsed = parse_document_pages_for_snapshot(
            [
                SnapshotPageInput(1, 1, "ts-1", good),
                SnapshotPageInput(2, 2, "ts-2", far),
            ]
        )
        self.assertFalse(parsed.hover_eligible)
        self.assertEqual(parsed.geometry_capability, "PARTIAL")


def _force_pk(model, row, pk: int):
    model.objects.filter(pk=row.pk).update(id=pk)
    return model.objects.get(pk=pk)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class HoverGeometryRepairTests(TestCase):
    def _build(self, *, document_id: int, snapshot_id: int, points: str):
        doc = create_ocr_document(
            title=f"repair {document_id}",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key=f"repair-{document_id}.jpg",
            mime_type="image/jpeg",
        )
        doc = _force_pk(Document, doc, document_id)
        run = TranskribusRun.objects.create(
            document=doc,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1-2",
            recognition_job_id="job",
            page_index_to_page_nr={1: 1, 2: 2},
            engine_runtime=_ENGINE,
        )
        good = _page_xml(
            _page_body(lines=_line_xml(line_id="ok", points="10,10 40,10 40,40", text="א"))
        )
        edge = _page_xml(
            _page_body(lines=_line_xml(line_id="edge", points=points, text="ב"))
        )
        parsed = parse_document_pages_for_snapshot(
            [
                SnapshotPageInput(1, 1, "ts-1", good),
                SnapshotPageInput(2, 2, "ts-2", edge),
            ],
            parser_version=PARSER_VERSION_V1,
        )
        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            transkribus_run=run,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            remote_doc_id="777",
            collection_id="col",
            model_id="42",
            recognition_job_id="job",
            parser_version=PARSER_VERSION_V1,
            provider_identity_fingerprint=parsed.provider_identity_fingerprint,
            raw_xml_fingerprint=parsed.raw_xml_fingerprint,
            canonical_text=parsed.canonical_text,
            canonical_text_sha256=parsed.canonical_text_sha256,
            geometry_capability=parsed.geometry_capability,
            hover_eligible=parsed.hover_eligible,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        snapshot = _force_pk(TranskribusTranscriptSnapshot, snapshot, snapshot_id)
        xml_by_key = {}
        for page, body in zip(parsed.pages, (good, edge), strict=True):
            key = f"documents/{document_id}/transkribus/snapshots/{snapshot_id}/pages/{page.page_index}.page.xml"
            xml_by_key[key] = body
            page_row = TranskribusSnapshotPage.objects.create(
                snapshot=snapshot,
                page_index=page.page_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                page_xml_s3_key=key,
                page_xml_sha256=compute_sha256_hex(body),
                image_width=page.image_width,
                image_height=page.image_height,
            )
            for line in page.lines:
                TranskribusSnapshotLine.objects.create(
                    page=page_row,
                    order_index=line.order_index,
                    provider_line_id=line.provider_line_id or "",
                    text=line.text,
                    contributes_to_canonical=line.contributes_to_canonical,
                    char_start=line.char_start,
                    char_end=line.char_end,
                    polygon_points=[list(point) for point in line.polygon_points],
                    coords_valid=line.coords_valid,
                    has_meaningful_geometry=line.has_meaningful_geometry,
                )
        source = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text=parsed.canonical_text,
            source_revision=2,
        )
        hebrew = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text=parsed.canonical_text,
            based_on_source_revision=2,
        )
        text_sha = compute_sha256_hex(parsed.canonical_text)
        TranskribusTextResultBinding.objects.create(
            text_result=source,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=text_sha,
            bound_source_revision=2,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=hebrew,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            bound_text_sha256=text_sha,
            bound_source_revision=2,
        )
        return doc, snapshot, xml_by_key

    def _s3(self, xml_by_key):
        def get_object_bytes(bucket, key):
            self.assertEqual(bucket, "test-uploads-bucket")
            return xml_by_key[key], "application/xml"

        return patch(
            "documents.services.transkribus_hover_geometry_repair.get_object_bytes",
            side_effect=get_object_bytes,
        )

    def test_document_106_is_out_of_scope(self):
        with self.assertRaises(HoverGeometryRepairError) as caught:
            plan_hover_geometry_repair(document_id=106, snapshot_id=1)
        self.assertEqual(caught.exception.code, "OUT_OF_SCOPE")

    def test_dry_run_reports_358_without_writes(self):
        _doc, snapshot, xml_by_key = self._build(
            document_id=358,
            snapshot_id=297,
            points="3001,1219 10,10 20,30",
        )
        with self._s3(xml_by_key), patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes"
        ) as put:
            plan = plan_hover_geometry_repair(document_id=358, snapshot_id=297)
        put.assert_not_called()
        self.assertEqual(plan.outcome, "READY_TO_APPLY")
        self.assertTrue(plan.hover_eligible)
        self.assertEqual(plan.geometry_capability, "VERIFIED")
        self.assertEqual(plan.source_revision, 2)
        self.assertEqual(
            plan.source_verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            plan.hebrew_verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(plan.canonical_text_sha256, snapshot.canonical_text_sha256)
        self.assertIn("provider_line_id=edge", plan.normalized_polygon_warnings[0])
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document_id=358).count(),
            1,
        )
        snapshot.refresh_from_db()
        self.assertFalse(snapshot.hover_eligible)
        self.assertEqual(snapshot.parser_version, PARSER_VERSION_V1)

    def test_apply_rebinds_without_changing_verified_text(self):
        doc, snapshot, xml_by_key = self._build(
            document_id=393,
            snapshot_id=307,
            points="-5,1377 10,10 20,30",
        )
        actor = get_user_model().objects.create_user(
            username="repair-admin",
            password="x",
            is_staff=True,
            is_active=True,
        )
        before_ids = set(
            DocumentTextResult.objects.filter(document=doc).values_list("pk", flat=True)
        )
        with self._s3(xml_by_key), patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes"
        ) as put:
            plan = plan_hover_geometry_repair(document_id=393, snapshot_id=307)
            result = apply_hover_geometry_repair(plan=plan, actor=actor)
            again = apply_hover_geometry_repair(plan=plan, actor=actor)
        self.assertEqual(result.outcome, "APPLIED")
        self.assertEqual(again.outcome, "ALREADY_APPLIED")
        self.assertEqual(put.call_count, 2)
        uploaded = {call.kwargs["body"] for call in put.call_args_list}
        self.assertEqual(uploaded, set(xml_by_key.values()))
        snapshot.refresh_from_db()
        self.assertEqual(snapshot.parser_version, PARSER_VERSION_V1)
        self.assertFalse(snapshot.hover_eligible)
        replacement = TranskribusTranscriptSnapshot.objects.get(
            pk=result.replacement_snapshot_id
        )
        self.assertNotEqual(replacement.pk, snapshot.pk)
        self.assertEqual(replacement.parser_version, PARSER_VERSION_V2)
        self.assertTrue(replacement.hover_eligible)
        self.assertEqual(
            replacement.source_kind,
            TranskribusTranscriptSnapshot.SourceKind.HOVER_GEOMETRY_REPAIR,
        )
        self.assertEqual(replacement.canonical_text, snapshot.canonical_text)
        self.assertEqual(
            TranskribusTranscriptSnapshot.objects.filter(document=doc).count(),
            2,
        )
        source = DocumentTextResult.objects.get(pk=plan.source_result_id)
        hebrew = DocumentTextResult.objects.get(pk=plan.hebrew_result_id)
        self.assertEqual(source.text, snapshot.canonical_text)
        self.assertEqual(hebrew.text, snapshot.canonical_text)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(hebrew.based_on_source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            set(DocumentTextResult.objects.filter(document=doc).values_list("pk", flat=True)),
            before_ids,
        )
        self.assertTrue(is_binding_trusted_for_hover(hebrew))
        self.assertEqual(hebrew.transkribus_snapshot_binding.snapshot_id, replacement.pk)
        self.assertEqual(source.transkribus_snapshot_binding.snapshot_id, replacement.pk)

    def test_apply_refuses_when_text_changes_during_storage(self):
        _doc, _snapshot, xml_by_key = self._build(
            document_id=358,
            snapshot_id=297,
            points="3001,1219 10,10 20,30",
        )
        actor = get_user_model().objects.create_user(
            username="repair-admin-2",
            password="x",
            is_staff=True,
        )
        real_store = store_transkribus_transcript_snapshot

        def store_and_edit(*args, **kwargs):
            result = real_store(*args, **kwargs)
            source = DocumentTextResult.objects.get(
                document_id=358,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            )
            source.text = (source.text or "") + "x"
            source.source_revision = int(source.source_revision) + 1
            source.save(update_fields=["text", "source_revision", "updated_at"])
            return result

        with self._s3(xml_by_key), patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes"
        ), patch(
            "documents.services.transkribus_hover_geometry_repair.store_transkribus_transcript_snapshot",
            side_effect=store_and_edit,
        ):
            plan = plan_hover_geometry_repair(document_id=358, snapshot_id=297)
            with self.assertRaises(HoverGeometryRepairError) as caught:
                apply_hover_geometry_repair(plan=plan, actor=actor)
        self.assertEqual(caught.exception.code, "TEXT_CHANGED")
        source = DocumentTextResult.objects.get(
            document_id=358,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        self.assertEqual(source.transkribus_snapshot_binding.snapshot_id, 297)

    def test_non_admin_cannot_apply(self):
        _doc, _snapshot, xml_by_key = self._build(
            document_id=358,
            snapshot_id=297,
            points="3001,1219 10,10 20,30",
        )
        actor = get_user_model().objects.create_user(
            username="repair-viewer",
            password="x",
            is_staff=False,
        )
        with self._s3(xml_by_key):
            plan = plan_hover_geometry_repair(document_id=358, snapshot_id=297)
            with self.assertRaises(HoverGeometryRepairError) as caught:
                apply_hover_geometry_repair(plan=plan, actor=actor)
        self.assertEqual(caught.exception.code, "ACTOR_FORBIDDEN")

    def _apply_after_store(self, hook, username):
        _doc, snapshot, xml_by_key = self._build(
            document_id=358,
            snapshot_id=297,
            points="3001,1219 10,10 20,30",
        )
        actor = get_user_model().objects.create_user(
            username=username,
            password="x",
            is_staff=True,
        )
        real_store = store_transkribus_transcript_snapshot

        def wrapped(*args, **kwargs):
            result = real_store(*args, **kwargs)
            hook(result)
            return result

        with self._s3(xml_by_key), patch(
            "documents.services.transkribus_snapshot_storage.put_object_bytes"
        ), patch(
            "documents.services.transkribus_hover_geometry_repair.store_transkribus_transcript_snapshot",
            side_effect=wrapped,
        ):
            plan = plan_hover_geometry_repair(document_id=358, snapshot_id=297)
            with self.assertRaises(HoverGeometryRepairError) as caught:
                apply_hover_geometry_repair(plan=plan, actor=actor)
        source = DocumentTextResult.objects.get(pk=plan.source_result_id)
        self.assertEqual(source.transkribus_snapshot_binding.snapshot_id, snapshot.pk)
        self.assertEqual(source.source_revision, 2)
        self.assertEqual(
            source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(source.text, snapshot.canonical_text)
        return caught.exception.code

    def test_apply_refuses_binding_role_revision_or_hash_drift(self):
        role = self._apply_after_store(
            lambda _result: TranskribusTextResultBinding.objects.filter(
                text_result__document_id=358,
                text_result__result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).update(
                binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
            ),
            "repair-role",
        )
        self.assertEqual(role, "BINDING_ROLE_MISMATCH")

    def test_apply_refuses_bound_revision_drift(self):
        code = self._apply_after_store(
            lambda _result: TranskribusTextResultBinding.objects.filter(
                text_result__document_id=358,
                binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            ).update(bound_source_revision=9),
            "repair-revision",
        )
        self.assertEqual(code, "BINDING_REVISION_CHANGED")

    def test_apply_refuses_bound_hash_drift(self):
        code = self._apply_after_store(
            lambda _result: TranskribusTextResultBinding.objects.filter(
                text_result__document_id=358,
                binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
            ).update(bound_text_sha256="ab" * 32),
            "repair-hash",
        )
        self.assertEqual(code, "BINDING_HASH_MISMATCH")

    def test_apply_refuses_paragraph_mapping_on_original_snapshot(self):
        code = self._apply_after_store(
            lambda _result: TranskribusParagraphMapping.objects.create(
                snapshot_id=297,
                document_id=358,
            ),
            "repair-paragraph-original",
        )
        self.assertEqual(code, "PARAGRAPH_MAPPING_PRESENT")

    def test_apply_refuses_paragraph_mapping_on_replacement_snapshot(self):
        code = self._apply_after_store(
            lambda result: TranskribusParagraphMapping.objects.create(
                snapshot=result.snapshot,
                document_id=result.snapshot.document_id,
            ),
            "repair-paragraph-replacement",
        )
        self.assertEqual(code, "PARAGRAPH_MAPPING_PRESENT")

    def test_apply_refuses_when_displayed_hebrew_changes(self):
        code = self._apply_after_store(
            lambda _result: DocumentTextResult.objects.create(
                document_id=358,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine="transkribus-pylaia:displayed",
                engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
                prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
                status=DocumentTextResult.Status.SUCCEEDED,
                verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
                text="אחר",
                based_on_source_revision=2,
            ),
            "repair-displayed",
        )
        self.assertEqual(code, "DISPLAYED_TEXT_MISMATCH")

    def test_apply_refuses_when_result_type_changes(self):
        def change_result_type(_result):
            DocumentTextResult.objects.filter(
                document_id=358,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            ).update(engine="transkribus-pylaia:displaced")
            DocumentTextResult.objects.filter(
                document_id=358,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).update(result_type=DocumentTextResult.ResultType.HEBREW_TEXT)

        code = self._apply_after_store(change_result_type, "repair-result-type")
        self.assertEqual(code, "TEXT_RESULT_TYPE_MISMATCH")

    def test_large_excursion_is_not_repairable(self):
        _doc, _snapshot, xml_by_key = self._build(
            document_id=358,
            snapshot_id=297,
            points="3010,10 10,10 20,30",
        )
        with self._s3(xml_by_key):
            with self.assertRaises(HoverGeometryRepairError) as caught:
                plan_hover_geometry_repair(document_id=358, snapshot_id=297)
        self.assertEqual(caught.exception.code, "REPLACEMENT_NOT_HOVER_ELIGIBLE")
