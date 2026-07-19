from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from documents.models import Document, TranskribusRun
from documents.services import transkribus_engine as tr
from documents.services.transkribus_page_xml_geometry import (
    TranskribusPageXmlGeometryError,
    _normalize_page_index_map,
    _parse_baseline_points,
    analyze_page_xml_geometry,
    audit_to_json_dict,
    fetch_document_geometry_audit,
    resolve_audit_transkribus_run,
    resolve_page_indices_to_audit,
    sanitize_sample_text,
)


def _page_xml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        f"{body}\n"
        "</PcGts>"
    ).encode("utf-8")


FULL_GEOMETRY_BODY = """
  <Page imageFilename="page-1.png" imageWidth="3000" imageHeight="4000">
    <ReadingOrder>
      <OrderedGroup id="ro1">
        <RegionRef regionRef="r1"/>
      </OrderedGroup>
    </ReadingOrder>
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,20 100,20 100,80 10,80"/>
        <Baseline points="10,75 100,75"/>
        <TextEquiv><Unicode>First line text</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <Coords points="10,100 100,100 100,140 10,140"/>
        <Baseline points="10,135 100,135"/>
        <TextEquiv><Unicode>Second line</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""


class TranskribusPageXmlGeometryAnalyzerTests(SimpleTestCase):
    def test_full_geometry_page_is_verified(self):
        audit = analyze_page_xml_geometry(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
            provider_page_id=99,
        )
        self.assertEqual(audit.page_capability, "VERIFIED")
        self.assertEqual(audit.text_region_count, 1)
        self.assertEqual(audit.text_line_count, 2)
        self.assertEqual(audit.lines_with_non_empty_text, 2)
        self.assertEqual(audit.lines_with_text_and_valid_coords, 2)
        self.assertEqual(audit.lines_with_baseline, 2)
        self.assertEqual(audit.lines_with_text_and_valid_baseline, 2)
        self.assertEqual(audit.lines_with_provider_line_ids, 2)
        self.assertTrue(audit.bounds_validation_available)
        self.assertEqual(audit.polygons_outside_page_bounds, 0)

    def test_partial_geometry_when_some_lines_missing_coords(self):
        body = """
  <Page imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,10 50,10 50,50 10,50"/>
        <TextEquiv><Unicode>Has coords</Unicode></TextEquiv>
      </TextLine>
      <TextLine id="l2">
        <TextEquiv><Unicode>No coords</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.page_capability, "PARTIAL")
        self.assertEqual(audit.lines_with_text_and_valid_coords, 1)
        self.assertEqual(audit.lines_with_non_empty_text, 2)

    def test_text_only_page_is_not_available(self):
        body = """
  <Page imageWidth="1000" imageHeight="1000">
    <TextRegion id="r1">
      <TextLine id="l1"><TextEquiv><Unicode>Alpha</Unicode></TextEquiv></TextLine>
      <TextLine id="l2"><TextEquiv><Unicode>Beta</Unicode></TextEquiv></TextLine>
    </TextRegion>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.page_capability, "NOT_AVAILABLE")
        self.assertEqual(audit.lines_with_coords, 0)

    def test_missing_page_dimensions_still_reports_partial(self):
        body = """
  <Page>
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Line</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertFalse(audit.bounds_validation_available)
        self.assertEqual(audit.page_capability, "PARTIAL")

    def test_malformed_and_degenerate_polygons(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="bad"/>
      <TextEquiv><Unicode>Malformed</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="l2">
      <Coords points="1,1 1,1 1,1"/>
      <TextEquiv><Unicode>Degenerate</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.malformed_polygons, 1)
        self.assertEqual(audit.degenerate_polygons, 1)
        self.assertEqual(audit.page_capability, "PARTIAL")

    def test_out_of_page_coordinates(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="-1,10 50,10 50,50 -1,50"/>
      <TextEquiv><Unicode>Outside</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="l2">
      <Coords points="200,10 250,10 250,50 200,50"/>
      <TextEquiv><Unicode>Far outside</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertGreater(audit.negative_or_outside_coordinates, 0)
        self.assertEqual(audit.polygons_outside_page_bounds, 2)
        self.assertEqual(audit.page_capability, "PARTIAL")
        self.assertEqual(audit.lines_with_text_and_valid_coords, 0)

    def test_out_of_page_coordinates_document_not_overlay_ready(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="200,10 250,10 250,50 200,50"/>
      <TextEquiv><Unicode>Outside</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        page = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        audit = DocumentGeometryAudit(
            document_id=1,
            transkribus_run_id=2,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        self.assertEqual(audit.line_geometry_capability, "PARTIAL")
        self.assertFalse(audit.suitable_for_overlay_poc)

    def test_two_point_baseline_is_valid(self):
        parsed = _parse_baseline_points("10,75 100,75")
        self.assertFalse(parsed.malformed)
        self.assertFalse(parsed.degenerate)

    def test_one_point_baseline_is_degenerate(self):
        parsed = _parse_baseline_points("10,75")
        self.assertFalse(parsed.malformed)
        self.assertTrue(parsed.degenerate)

    def test_repeated_single_point_baseline_is_degenerate(self):
        parsed = _parse_baseline_points("10,75 10,75")
        self.assertFalse(parsed.malformed)
        self.assertTrue(parsed.degenerate)

    def test_baseline_only_fallback_uses_valid_two_point_baselines(self):
        body = """
  <Page imageWidth="1000" imageHeight="1000">
    <TextLine id="l1">
      <TextEquiv><Unicode>No coords</Unicode></TextEquiv>
      <Baseline points="10,75 100,75"/>
    </TextLine>
  </Page>
"""
        page = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(page.lines_with_text_and_valid_baseline, 1)
        self.assertEqual(page.lines_with_text_and_valid_coords, 0)
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        audit = DocumentGeometryAudit(
            document_id=1,
            transkribus_run_id=2,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        self.assertTrue(audit.baseline_only_fallback_needed)

    def test_duplicate_line_ids(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="dup">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>A</Unicode></TextEquiv>
    </TextLine>
    <TextLine id="dup">
      <Coords points="3,3 4,3 4,4 3,4"/>
      <TextEquiv><Unicode>B</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.duplicate_line_ids, 1)

    def test_empty_text_lines_mixed_with_non_empty(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode></Unicode></TextEquiv>
    </TextLine>
    <TextLine id="l2">
      <Coords points="3,3 4,3 4,4 3,4"/>
      <TextEquiv><Unicode>Visible</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.lines_with_non_empty_text, 1)
        self.assertEqual(audit.lines_with_text_and_valid_coords, 1)

    def test_namespace_fallback_without_prefix(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <TextEquiv><Unicode>only</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(_page_xml(body), page_index=1, page_nr=1)
        self.assertEqual(audit.lines_with_non_empty_text, 1)

    def test_include_sample_text_truncates_and_sanitizes(self):
        long_text = "A" * 200 + " tail"
        body = f"""
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>{long_text}</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        audit = analyze_page_xml_geometry(
            _page_xml(body),
            page_index=1,
            page_nr=1,
            include_sample_text=True,
        )
        self.assertIsNotNone(audit.sample_line)
        assert audit.sample_line is not None
        self.assertLessEqual(len(audit.sample_line.text), 120)
        self.assertTrue(audit.sample_line.text.endswith("..."))

    def test_sanitize_sample_text_strips_control_characters(self):
        self.assertEqual(sanitize_sample_text("  hello\x00\nworld  "), "hello world")


def _mock_upload_run(**overrides) -> SimpleNamespace:
    values = {
        "id": 10,
        "mode": TranskribusRun.Mode.UPLOAD_CREATED,
        "status": TranskribusRun.Status.SUCCEEDED,
        "collection_id": "1",
        "model_id": "42",
        "remote_doc_id": "999",
        "pages_query": "1-2",
        "page_index_to_page_nr": {1: 1, 2: 2},
        "recognition_job_id": "job-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _mock_run_queryset(runs: list[SimpleNamespace]) -> MagicMock:
    qs = MagicMock()
    qs.exists.return_value = bool(runs)
    qs.__iter__ = lambda self: iter(runs)
    return qs


class TranskribusPageXmlGeometryResolutionTests(SimpleTestCase):
    @patch("documents.services.transkribus_page_xml_geometry.TranskribusRun.objects")
    @patch("documents.services.transkribus_page_xml_geometry.Document.objects")
    def test_resolve_audit_run_prefers_upload_created(
        self, mock_document_objects, mock_run_objects
    ):
        mock_document_objects.filter.return_value.exists.return_value = True
        run = _mock_upload_run()
        mock_run_objects.filter.return_value.order_by.return_value = _mock_run_queryset(
            [run]
        )
        resolved = resolve_audit_transkribus_run(123)
        self.assertEqual(resolved.mode, TranskribusRun.Mode.UPLOAD_CREATED)

    @patch("documents.services.transkribus_page_xml_geometry.Document.objects")
    def test_missing_document_raises(self, mock_document_objects):
        mock_document_objects.filter.return_value.exists.return_value = False
        with self.assertRaises(TranskribusPageXmlGeometryError):
            resolve_audit_transkribus_run(999999)

    @patch("documents.services.transkribus_page_xml_geometry.TranskribusRun.objects")
    @patch("documents.services.transkribus_page_xml_geometry.Document.objects")
    def test_missing_transkribus_run_raises(
        self, mock_document_objects, mock_run_objects
    ):
        mock_document_objects.filter.return_value.exists.return_value = True
        mock_run_objects.filter.return_value.order_by.return_value = _mock_run_queryset(
            []
        )
        with self.assertRaises(TranskribusPageXmlGeometryError):
            resolve_audit_transkribus_run(123)

    @patch("documents.services.transkribus_page_xml_geometry.TranskribusRun.objects")
    @patch("documents.services.transkribus_page_xml_geometry.Document.objects")
    def test_existing_server_only_is_rejected(
        self, mock_document_objects, mock_run_objects
    ):
        mock_document_objects.filter.return_value.exists.return_value = True
        existing = _mock_upload_run(
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            page_index_to_page_nr=None,
            recognition_job_id=None,
        )
        mock_run_objects.filter.return_value.order_by.return_value = _mock_run_queryset(
            [existing]
        )
        with self.assertRaises(TranskribusPageXmlGeometryError) as ctx:
            resolve_audit_transkribus_run(123)
        self.assertIn("EXISTING_SERVER", str(ctx.exception))

    def test_page_index_selection(self):
        mapping = {1: 10, 2: 11}
        self.assertEqual(
            resolve_page_indices_to_audit(mapping, page_index=2),
            [2],
        )

    def test_invalid_page_index_raises(self):
        mapping = {1: 10}
        with self.assertRaises(TranskribusPageXmlGeometryError):
            resolve_page_indices_to_audit(mapping, page_index=2)


class TranskribusPageIndexMapValidationTests(SimpleTestCase):
    def test_rejects_page_index_below_one(self):
        with self.assertRaises(TranskribusPageXmlGeometryError):
            _normalize_page_index_map({0: 1})

    def test_rejects_page_nr_below_one(self):
        with self.assertRaises(TranskribusPageXmlGeometryError):
            _normalize_page_index_map({1: 0})

    def test_rejects_duplicate_page_nr_for_multiple_local_indexes(self):
        with self.assertRaises(TranskribusPageXmlGeometryError):
            _normalize_page_index_map({1: 10, 2: 10})


class TranskribusPageXmlGeometryFetchTests(SimpleTestCase):
    @patch(
        "documents.services.transkribus_page_xml_geometry.resolve_audit_transkribus_run"
    )
    def test_fetch_uses_transcript_selection_and_analyzes_page(self, mock_resolve):
        run = _mock_upload_run(
            id=55,
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        mock_resolve.return_value = run
        pages_meta = [
            tr.TrpPageMetadata(
                page_nr=1,
                page_id=501,
                doc_id=999,
                page_url=None,
                transcripts=[
                    {
                        "tsId": "7",
                        "jobId": "job-1",
                        "modelId": "42",
                        "url": "https://example.invalid/transcript",
                    }
                ],
            )
        ]

        def fake_fetch_xml(url, *, bearer_token):
            self.assertEqual(bearer_token, "token")
            return _page_xml(FULL_GEOMETRY_BODY)

        audit = fetch_document_geometry_audit(
            document_id=123,
            username="user",
            password="pass",
            bearer_token="token",
            login=lambda session, username, password: None,
            fetch_pages_metadata=lambda session, **kwargs: pages_meta,
            fetch_xml=fake_fetch_xml,
        )
        self.assertEqual(audit.transkribus_run_id, run.id)
        self.assertEqual(len(audit.pages), 1)
        self.assertEqual(audit.pages[0].page_capability, "VERIFIED")
        self.assertEqual(audit.pages[0].transcript_ts_id, "7")

    @patch(
        "documents.services.transkribus_page_xml_geometry.resolve_audit_transkribus_run"
    )
    def test_fetch_page_index_filters_to_one_page(self, mock_resolve):
        run = _mock_upload_run(
            pages_query="1-2",
            page_index_to_page_nr={1: 1, 2: 2},
        )
        mock_resolve.return_value = run

        pages_meta = [
            tr.TrpPageMetadata(
                page_nr=2,
                page_id=502,
                doc_id=999,
                page_url=None,
                transcripts=[
                    {
                        "tsId": "8",
                        "jobId": "job-1",
                        "modelId": "42",
                        "url": "https://example.invalid/transcript-2",
                    }
                ],
            )
        ]

        audit = fetch_document_geometry_audit(
            document_id=123,
            page_index=2,
            username="user",
            password="pass",
            bearer_token="token",
            login=lambda session, username, password: None,
            fetch_pages_metadata=lambda session, **kwargs: pages_meta,
            fetch_xml=lambda url, *, bearer_token: _page_xml(FULL_GEOMETRY_BODY),
        )
        self.assertEqual(len(audit.pages), 1)
        self.assertEqual(audit.pages[0].page_index, 2)
        self.assertEqual(audit.pages[0].page_nr, 2)

    @patch(
        "documents.services.transkribus_page_xml_geometry.resolve_audit_transkribus_run"
    )
    def test_remote_fetch_failure_bubbles_as_permanent_error(self, mock_resolve):
        mock_resolve.return_value = _mock_upload_run()

        def boom(*args, **kwargs):
            raise tr.TranskribusPermanentError(
                "Transkribus pages metadata returned empty list"
            )

        with self.assertRaises(tr.TranskribusPermanentError):
            fetch_document_geometry_audit(
                document_id=123,
                username="user",
                password="pass",
                bearer_token="token",
                login=lambda session, username, password: None,
                fetch_pages_metadata=boom,
            )

    @patch(
        "documents.services.transkribus_page_xml_geometry.resolve_audit_transkribus_run"
    )
    def test_fetch_does_not_persist_model_writes(self, mock_resolve):
        mock_resolve.return_value = _mock_upload_run(
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        pages_meta = [
            tr.TrpPageMetadata(
                page_nr=1,
                page_id=501,
                doc_id=999,
                page_url=None,
                transcripts=[
                    {
                        "tsId": "7",
                        "jobId": "job-1",
                        "modelId": "42",
                        "url": "https://example.invalid/transcript",
                    }
                ],
            )
        ]

        def forbid_save(*args, **kwargs):
            raise AssertionError("fetch_document_geometry_audit must not write models")

        with patch.object(TranskribusRun, "save", side_effect=forbid_save):
            with patch.object(Document, "save", side_effect=forbid_save):
                audit = fetch_document_geometry_audit(
                    document_id=123,
                    username="user",
                    password="pass",
                    bearer_token="token",
                    login=lambda session, username, password: None,
                    fetch_pages_metadata=lambda session, **kwargs: pages_meta,
                    fetch_xml=lambda url, *, bearer_token: _page_xml(
                        FULL_GEOMETRY_BODY
                    ),
                )
        self.assertEqual(len(audit.pages), 1)


class AuditTranskribusPageXmlGeometryCommandTests(SimpleTestCase):
    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_command_json_output(self, mock_fetch):
        page = analyze_page_xml_geometry(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
        )
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        mock_fetch.return_value = DocumentGeometryAudit(
            document_id=123,
            transkribus_run_id=1,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_page_xml_geometry",
            "--document-id=123",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["document_id"], 123)
        self.assertEqual(payload["verdict"]["line_geometry_capability"], "VERIFIED")

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_default_output_does_not_include_line_text(self, mock_fetch):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Secret transcription line</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        page = analyze_page_xml_geometry(
            _page_xml(body), page_index=1, page_nr=1, include_sample_text=False
        )
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        mock_fetch.return_value = DocumentGeometryAudit(
            document_id=123,
            transkribus_run_id=1,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_page_xml_geometry",
            "--document-id=123",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertNotIn("Secret transcription line", output)

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_include_sample_text_flag(self, mock_fetch):
        page = analyze_page_xml_geometry(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
            include_sample_text=True,
        )
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        mock_fetch.return_value = DocumentGeometryAudit(
            document_id=123,
            transkribus_run_id=1,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_page_xml_geometry",
            "--document-id=123",
            "--include-sample-text",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertIn("Sample line:", output)
        self.assertIn("First line text", output)

    def test_missing_credentials_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "audit_transkribus_page_xml_geometry",
                    "--document-id=123",
                )
        self.assertIn("TRANSKRIBUS_USERNAME", str(ctx.exception))

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_invalid_document_raises(self, mock_fetch):
        mock_fetch.side_effect = TranskribusPageXmlGeometryError(
            "Document id=999999 does not exist."
        )
        with self.assertRaises(CommandError):
            call_command(
                "audit_transkribus_page_xml_geometry",
                "--document-id=999999",
            )

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_remote_failure_returns_command_error(self, mock_fetch):
        mock_fetch.side_effect = tr.TranskribusPermanentError("provider down")
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "audit_transkribus_page_xml_geometry",
                "--document-id=123",
            )
        self.assertIn("provider down", str(ctx.exception))

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    @patch(
        "documents.management.commands.audit_transkribus_page_xml_geometry.fetch_document_geometry_audit"
    )
    def test_command_delegates_to_fetch_service_without_local_writes(self, mock_fetch):
        page = analyze_page_xml_geometry(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
        )
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        mock_fetch.return_value = DocumentGeometryAudit(
            document_id=123,
            transkribus_run_id=1,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        call_command(
            "audit_transkribus_page_xml_geometry",
            "--document-id=123",
            stdout=StringIO(),
        )
        mock_fetch.assert_called_once()


class TranskribusPageXmlGeometryJsonTests(SimpleTestCase):
    def test_audit_to_json_dict_contains_verdict(self):
        page = analyze_page_xml_geometry(
            _page_xml(FULL_GEOMETRY_BODY),
            page_index=1,
            page_nr=1,
        )
        from documents.services.transkribus_page_xml_geometry import (
            DocumentGeometryAudit,
        )

        audit = DocumentGeometryAudit(
            document_id=1,
            transkribus_run_id=2,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            pages=(page,),
        )
        payload = audit_to_json_dict(audit)
        self.assertEqual(payload["pages"][0]["page_capability"], "VERIFIED")
        self.assertEqual(payload["verdict"]["line_geometry_capability"], "VERIFIED")
