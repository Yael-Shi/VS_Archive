from __future__ import annotations

import hashlib
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
)
from documents.services.transkribus_transcript_versions import (
    TranskribusTranscriptVersionsError,
    _best_parsed_timestamp,
    _collect_provider_version_signals,
    _is_truthy_flag,
    _normalize_numeric_epoch_seconds,
    _parse_timestamp_epoch,
    audit_page_transcript_versions,
    audit_to_json_dict,
    build_transcript_version_metadata,
    fetch_document_transcript_version_audit,
    summarize_transcript_page_xml,
    validate_audit_payload,
    validate_json_payload,
)

_EPOCH_2020 = 1600000000
_EPOCH_2024_ISO = "2024-06-01T10:00:00Z"

FULL_GEOMETRY_BODY = """
  <Page imageFilename="page-1.png" imageWidth="3000" imageHeight="4000">
    <TextRegion id="r1">
      <TextLine id="l1">
        <Coords points="10,20 100,20 100,80 10,80"/>
        <Baseline points="10,75 100,75"/>
        <TextEquiv><Unicode>First line text</Unicode></TextEquiv>
      </TextLine>
    </TextRegion>
  </Page>
"""


def _page_xml(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<PcGts xmlns="{tr.PAGE_XML_NS}">\n'
        f"{body}\n"
        "</PcGts>"
    ).encode("utf-8")


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


class TranskribusTimestampParsingTests(SimpleTestCase):
    def test_parse_numeric_epoch_seconds(self):
        self.assertEqual(_parse_timestamp_epoch(1700000000), 1700000000)
        self.assertEqual(_parse_timestamp_epoch("1700000000"), 1700000000)

    def test_parse_epoch_milliseconds(self):
        ms = 1700000000000
        self.assertEqual(_normalize_numeric_epoch_seconds(ms), 1700000000)
        self.assertEqual(_parse_timestamp_epoch(ms), 1700000000)
        self.assertEqual(_parse_timestamp_epoch(str(ms)), 1700000000)

    def test_parse_epoch_microseconds(self):
        us = 1700000000000000
        self.assertEqual(_normalize_numeric_epoch_seconds(us), 1700000000)
        self.assertEqual(_parse_timestamp_epoch(us), 1700000000)

    def test_implausible_epoch_values_are_rejected(self):
        self.assertIsNone(_parse_timestamp_epoch(100))
        self.assertIsNone(_parse_timestamp_epoch(50000000000000000))
        self.assertIsNone(_normalize_numeric_epoch_seconds(-1))

    def test_parse_iso8601_with_z(self):
        parsed = _parse_timestamp_epoch("2024-01-15T12:30:00Z")
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 0)

    def test_parse_iso8601_with_timezone_offset(self):
        parsed = _parse_timestamp_epoch("2024-01-15T12:30:00+02:00")
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 0)

    def test_unparseable_timestamp_returns_none(self):
        self.assertIsNone(_parse_timestamp_epoch("not-a-date"))
        self.assertIsNone(_parse_timestamp_epoch(""))

    def test_best_parsed_timestamp_uses_highest_comparable_value(self):
        fields = {
            "timestamp": _EPOCH_2020,
            "modified": _EPOCH_2024_ISO,
        }
        best = _best_parsed_timestamp(fields)
        iso_epoch = _parse_timestamp_epoch(_EPOCH_2024_ISO)
        assert iso_epoch is not None
        self.assertEqual(best, iso_epoch)
        self.assertGreater(best, _EPOCH_2020)

    def test_mixed_seconds_milliseconds_and_iso_compare_correctly(self):
        iso_epoch = _parse_timestamp_epoch(_EPOCH_2024_ISO)
        assert iso_epoch is not None
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "timestamp": _EPOCH_2020,
                },
                {
                    "tsId": "8",
                    "timestamp": iso_epoch * 1000,
                },
                {
                    "tsId": "20",
                    "timestamp": _EPOCH_2024_ISO,
                },
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertTrue(
            any(
                "Comparable parsed timestamps" in s
                for s in page.provider_version_signals
            )
        )
        self.assertTrue(any("tsId=20" in s for s in page.provider_version_signals))

    def test_unparseable_timestamps_do_not_drive_rank(self):
        meta_a = build_transcript_version_metadata(
            {"tsId": "1", "timestamp": "not-a-date"},
            list_position=1,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        meta_b = build_transcript_version_metadata(
            {"tsId": "2", "timestamp": "also-bad"},
            list_position=2,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {"tsId": "1", "timestamp": "not-a-date"},
                {"tsId": "2", "timestamp": "also-bad"},
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertFalse(
            any(
                "Comparable parsed timestamps" in s
                for s in page.provider_version_signals
            )
        )
        self.assertFalse(
            any("list order does not match" in a.lower() for a in page.ambiguities)
        )
        self.assertEqual(meta_a.classification, "NON_MATCHING_VERSION")
        self.assertEqual(meta_b.classification, "NON_MATCHING_VERSION")

    def test_mixed_numeric_and_iso_timestamps_report_newest(self):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "timestamp": _EPOCH_2020,
                },
                {
                    "tsId": "20",
                    "timestamp": _EPOCH_2024_ISO,
                },
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertTrue(
            any(
                "Comparable parsed timestamps" in s
                for s in page.provider_version_signals
            )
        )
        self.assertIn("tsId=20", page.provider_version_signals[-1])


class TranskribusProviderFlagTests(SimpleTestCase):
    def test_truthy_flag_normalization(self):
        self.assertTrue(_is_truthy_flag(True))
        self.assertTrue(_is_truthy_flag("true"))
        self.assertTrue(_is_truthy_flag(1))
        self.assertFalse(_is_truthy_flag(False))
        self.assertFalse(_is_truthy_flag("false"))
        self.assertFalse(_is_truthy_flag(0))

    def test_false_latest_flag_is_not_marked_latest(self):
        transcripts = (
            build_transcript_version_metadata(
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "isLatest": False,
                },
                list_position=1,
                stored_job_id="job-1",
                stored_model_id="42",
            ),
        )
        signals = _collect_provider_version_signals(transcripts)
        self.assertFalse(any("explicitly truthy latest flags" in s for s in signals))
        self.assertTrue(any("not explicitly true" in s for s in signals))

    def test_true_latest_flag_is_reported(self):
        transcripts = (
            build_transcript_version_metadata(
                {"tsId": "12", "isLatest": True, "timestamp": 1700000500},
                list_position=1,
                stored_job_id="job-1",
                stored_model_id="42",
            ),
        )
        signals = _collect_provider_version_signals(transcripts)
        self.assertTrue(any("explicitly truthy latest flags" in s for s in signals))


class TranskribusTranscriptVersionMetadataTests(SimpleTestCase):
    def test_original_htr_transcript_only(self):
        meta = build_transcript_version_metadata(
            {
                "tsId": "7",
                "jobId": "job-1",
                "modelId": "42",
                "url": "https://secret.example/transcript/7",
                "timestamp": _EPOCH_2020,
            },
            list_position=1,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(meta.classification, "ORIGINAL_HTR")
        self.assertTrue(meta.matches_stored_htr_job_model)

    def test_original_plus_non_matching_version(self):
        original = build_transcript_version_metadata(
            {
                "tsId": "7",
                "jobId": "job-1",
                "modelId": "42",
                "timestamp": _EPOCH_2020,
            },
            list_position=1,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        later = build_transcript_version_metadata(
            {
                "tsId": "12",
                "userName": "archivist",
                "timestamp": 1700000500,
                "isLatest": True,
            },
            list_position=2,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(original.classification, "ORIGINAL_HTR")
        self.assertEqual(later.classification, "NON_MATCHING_VERSION")
        self.assertIn("user/editor metadata present", later.observed_edit_signals[0])

    def test_missing_job_model_is_non_matching_not_human_edited(self):
        meta = build_transcript_version_metadata(
            {"tsId": "3", "timestamp": 1700000200},
            list_position=1,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(meta.classification, "NON_MATCHING_VERSION")
        self.assertFalse(meta.matches_stored_htr_job_model)
        self.assertNotIn("human-edited", " ".join(meta.classification_reasons).lower())

    def test_insufficient_metadata_transcript(self):
        meta = build_transcript_version_metadata(
            {"url": "https://secret.example/transcript"},
            list_position=1,
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(meta.classification, "INSUFFICIENT_METADATA")
        self.assertIsNone(meta.ts_id)

    def test_multiple_non_matching_corrected_version_candidates(self):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "timestamp": _EPOCH_2020,
                },
                {
                    "tsId": "11",
                    "userName": "editor-a",
                    "timestamp": 1700000400,
                },
                {
                    "tsId": "12",
                    "userName": "editor-b",
                    "timestamp": 1700000450,
                    "isLatest": True,
                },
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(page.original_htr_ts_ids, ("7",))
        self.assertEqual(page.non_matching_version_ts_ids, ("11", "12"))
        self.assertTrue(
            any(
                "possible corrected-version candidates" in a.lower()
                for a in page.ambiguities
            )
        )

    def test_non_chronological_provider_list_order(self):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "timestamp": _EPOCH_2020,
                },
                {
                    "tsId": "20",
                    "timestamp": _EPOCH_2024_ISO,
                },
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        self.assertEqual(page.transcripts[0].ts_id, "7")
        self.assertEqual(page.transcripts[1].ts_id, "20")
        self.assertTrue(
            any("list order does not match" in a.lower() for a in page.ambiguities)
        )


class TranskribusTranscriptVersionFetchTests(SimpleTestCase):
    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
    )
    def test_fetch_lists_all_transcripts_without_original_match(self, mock_resolve):
        run = _mock_upload_run(
            pages_query="1",
            page_index_to_page_nr={1: 1},
        )
        mock_resolve.return_value = run
        pages_meta = [
            tr.TrpPageMetadata(
                page_nr=1,
                page_id=501,
                doc_id=999,
                page_url="https://secret.example/page",
                transcripts=[
                    {
                        "tsId": "20",
                        "userName": "archivist",
                        "timestamp": 1700000900,
                        "url": "https://secret.example/transcript/20",
                    }
                ],
            )
        ]

        audit = fetch_document_transcript_version_audit(
            document_id=249,
            username="user",
            password="pass",
            bearer_token="token",
            login=lambda session, username, password: None,
            fetch_pages_metadata=lambda session, **kwargs: pages_meta,
        )
        self.assertEqual(audit.document_id, 249)
        self.assertEqual(len(audit.pages), 1)
        self.assertFalse(audit.pages[0].original_htr_present)
        self.assertIn(
            "stored HTR job/model match missing",
            audit.candidate_selection_rules[-1],
        )

    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
    )
    def test_fetch_inspects_transcript_page_xml(self, mock_resolve):
        run = _mock_upload_run(
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
                        "url": "https://secret.example/transcript/7",
                    }
                ],
            )
        ]
        xml_bytes = _page_xml(FULL_GEOMETRY_BODY)

        audit = fetch_document_transcript_version_audit(
            document_id=123,
            transcript_id="7",
            include_sample_text=True,
            username="user",
            password="pass",
            bearer_token="token",
            login=lambda session, username, password: None,
            fetch_pages_metadata=lambda session, **kwargs: pages_meta,
            fetch_xml=lambda url, *, bearer_token: xml_bytes,
        )
        inspected = audit.inspected_transcript
        assert inspected is not None
        self.assertEqual(inspected.ts_id, "7")
        self.assertEqual(inspected.text_line_count, 1)
        self.assertEqual(
            inspected.content_sha256, hashlib.sha256(xml_bytes).hexdigest()
        )
        self.assertEqual(inspected.sample_line_text, "First line text")

    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
    )
    def test_provider_error_bubbles(self, mock_resolve):
        mock_resolve.return_value = _mock_upload_run()

        def boom(*args, **kwargs):
            raise tr.TranskribusPermanentError(
                "Transkribus pages metadata returned empty list"
            )

        with self.assertRaises(tr.TranskribusPermanentError):
            fetch_document_transcript_version_audit(
                document_id=123,
                username="user",
                password="pass",
                bearer_token="token",
                login=lambda session, username, password: None,
                fetch_pages_metadata=boom,
            )

    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
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
                        "url": "https://secret.example/transcript/7",
                    }
                ],
            )
        ]

        def forbid_save(*args, **kwargs):
            raise AssertionError(
                "fetch_document_transcript_version_audit must not write models"
            )

        with patch.object(TranskribusRun, "save", side_effect=forbid_save):
            with patch.object(Document, "save", side_effect=forbid_save):
                audit = fetch_document_transcript_version_audit(
                    document_id=123,
                    username="user",
                    password="pass",
                    bearer_token="token",
                    login=lambda session, username, password: None,
                    fetch_pages_metadata=lambda session, **kwargs: pages_meta,
                )
        self.assertEqual(len(audit.pages), 1)

    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
    )
    def test_fetch_requires_bearer_for_transcript_inspection(self, mock_resolve):
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
                        "url": "https://secret.example/transcript/7",
                    }
                ],
            )
        ]
        with self.assertRaises(TranskribusTranscriptVersionsError) as ctx:
            fetch_document_transcript_version_audit(
                document_id=123,
                transcript_id="7",
                username="user",
                password="pass",
                bearer_token="",
                login=lambda session, username, password: None,
                fetch_pages_metadata=lambda session, **kwargs: pages_meta,
            )
        self.assertIn("TRANSKRIBUS_API_TOKEN", str(ctx.exception))


class TranskribusTranscriptVersionRedactionTests(SimpleTestCase):
    def test_json_payload_redacts_urls_and_xml(self):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "url": "https://secret.example/transcript/7",
                    "token": "abc123",
                }
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        audit = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        payload = audit_to_json_dict(audit)
        validate_json_payload(payload)
        serialized = json.dumps(payload)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("abc123", serialized)

    def test_malicious_allowlisted_values_are_redacted_from_json(self):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "source": "https://evil.example/transcript.xml",
                    "label": "<Page><TextEquiv>secret</TextEquiv></Page>",
                    "type": "Bearer super-secret-token",
                }
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        audit = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        payload = audit_to_json_dict(audit)
        serialized = json.dumps(payload).lower()
        self.assertNotIn("https://", serialized)
        self.assertNotIn("<page", serialized)
        self.assertNotIn("bearer", serialized)
        self.assertNotIn("super-secret-token", serialized)

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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_malicious_allowlisted_values_are_redacted_from_human_output(
        self, mock_fetch
    ):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "source": "https://evil.example/transcript.xml",
                    "label": "<Page><TextEquiv>secret</TextEquiv></Page>",
                    "type": "Bearer super-secret-token",
                }
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            stdout=stdout,
        )
        output = stdout.getvalue().lower()
        self.assertNotIn("https://", output)
        self.assertNotIn("<page", output)
        self.assertNotIn("bearer", output)
        self.assertNotIn("super-secret-token", output)

    def test_summarize_page_xml_does_not_embed_raw_xml(self):
        xml_bytes = _page_xml(FULL_GEOMETRY_BODY)
        summary = summarize_transcript_page_xml(
            xml_bytes,
            ts_id="7",
            page_index=1,
            page_nr=1,
            include_sample_text=False,
        )
        self.assertNotIn("<Page", summary.content_sha256)
        self.assertIsNone(summary.sample_line_text)

    def test_unsafe_sample_text_is_sanitized_before_validation(self):
        body = """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Visit https://evil.example secret</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
        summary = summarize_transcript_page_xml(
            _page_xml(body),
            ts_id="7",
            page_index=1,
            page_nr=1,
            include_sample_text=True,
        )
        self.assertIsNone(summary.sample_line_text)


class TranskribusTranscriptVersionResolutionTests(SimpleTestCase):
    @patch("documents.services.transkribus_page_xml_geometry.TranskribusRun.objects")
    @patch("documents.services.transkribus_page_xml_geometry.Document.objects")
    def test_existing_server_only_is_rejected(
        self, mock_document_objects, mock_run_objects
    ):
        from documents.services.transkribus_page_xml_geometry import (
            resolve_audit_transkribus_run,
        )

        mock_document_objects.filter.return_value.exists.return_value = True
        existing = _mock_upload_run(
            mode=TranskribusRun.Mode.EXISTING_SERVER,
            page_index_to_page_nr=None,
            recognition_job_id=None,
        )
        mock_run_objects.filter.return_value.order_by.return_value = MagicMock(
            exists=MagicMock(return_value=True),
            __iter__=lambda self: iter([existing]),
            filter=MagicMock(
                return_value=MagicMock(exists=MagicMock(return_value=True))
            ),
        )
        with self.assertRaises(TranskribusPageXmlGeometryError) as ctx:
            resolve_audit_transkribus_run(123)
        self.assertIn("EXISTING_SERVER", str(ctx.exception))

    @patch(
        "documents.services.transkribus_transcript_versions.resolve_audit_transkribus_run"
    )
    def test_missing_transcript_id_maps_to_local_error(self, mock_resolve):
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
                        "url": "https://secret.example/transcript/7",
                    }
                ],
            )
        ]
        with self.assertRaises(TranskribusTranscriptVersionsError) as ctx:
            fetch_document_transcript_version_audit(
                document_id=123,
                transcript_id="missing",
                username="user",
                password="pass",
                bearer_token="token",
                login=lambda session, username, password: None,
                fetch_pages_metadata=lambda session, **kwargs: pages_meta,
            )
        self.assertIn("No transcript with tsId", str(ctx.exception))


class AuditTranskribusTranscriptVersionsCommandTests(SimpleTestCase):
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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_command_json_output(self, mock_fetch):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "timestamp": _EPOCH_2020,
                }
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            "--json",
            stdout=stdout,
        )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["document_id"], 123)
        self.assertEqual(payload["pages"][0]["original_htr_ts_ids"], ["7"])
        self.assertEqual(payload["pages"][0]["non_matching_version_ts_ids"], [])

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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_default_output_does_not_include_secrets(self, mock_fetch):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {
                    "tsId": "7",
                    "jobId": "job-1",
                    "modelId": "42",
                    "url": "https://secret.example/transcript/7",
                }
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            stdout=stdout,
        )
        output = stdout.getvalue()
        self.assertNotIn("https://", output)
        self.assertIn("Original HTR present: yes", output)
        self.assertNotIn("human-edited", output.lower())

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
            "TRANSKRIBUS_API_TOKEN": "token",
        },
        clear=False,
    )
    def test_include_sample_text_requires_transcript_id(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "audit_transkribus_transcript_versions",
                "--document-id=123",
                "--include-sample-text",
            )
        self.assertIn("--transcript-id", str(ctx.exception))

    def test_missing_credentials_raises(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(CommandError) as ctx:
                call_command(
                    "audit_transkribus_transcript_versions",
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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_remote_failure_returns_command_error(self, mock_fetch):
        mock_fetch.side_effect = tr.TranskribusPermanentError("provider down")
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "audit_transkribus_transcript_versions",
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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_command_delegates_without_local_writes(self, mock_fetch):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {"tsId": "7", "jobId": "job-1", "modelId": "42"},
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            stdout=StringIO(),
        )
        mock_fetch.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
        },
        clear=True,
    )
    @patch(
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_metadata_only_runs_without_bearer_token(self, mock_fetch):
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {"tsId": "7", "jobId": "job-1", "modelId": "42"},
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=None,
        )
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            stdout=StringIO(),
        )
        mock_fetch.assert_called_once_with(
            document_id=123,
            page_index=None,
            transcript_id=None,
            include_sample_text=False,
            username="user",
            password="pass",
            bearer_token="",
        )

    @patch.dict(
        "os.environ",
        {
            "TRANSKRIBUS_USERNAME": "user",
            "TRANSKRIBUS_PASSWORD": "pass",
        },
        clear=True,
    )
    def test_transcript_inspection_requires_bearer_token(self):
        with self.assertRaises(CommandError) as ctx:
            call_command(
                "audit_transkribus_transcript_versions",
                "--document-id=123",
                "--transcript-id=7",
            )
        self.assertIn("TRANSKRIBUS_API_TOKEN", str(ctx.exception))

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
        "documents.management.commands.audit_transkribus_transcript_versions.fetch_document_transcript_version_audit"
    )
    def test_human_output_validates_payload_before_formatting(self, mock_fetch):
        inspected = summarize_transcript_page_xml(
            _page_xml(
                """
  <Page imageWidth="100" imageHeight="100">
    <TextLine id="l1">
      <Coords points="1,1 2,1 2,2 1,2"/>
      <TextEquiv><Unicode>Safe sample only</Unicode></TextEquiv>
    </TextLine>
  </Page>
"""
            ),
            ts_id="7",
            page_index=1,
            page_nr=1,
            include_sample_text=True,
        )
        page = audit_page_transcript_versions(
            page_index=1,
            page_nr=1,
            provider_page_id=501,
            raw_transcripts=[
                {"tsId": "7", "jobId": "job-1", "modelId": "42"},
            ],
            stored_job_id="job-1",
            stored_model_id="42",
        )
        mock_fetch.return_value = SimpleNamespace(
            document_id=123,
            transkribus_run_id=10,
            remote_doc_id="999",
            mapping_description="trusted upload-created mapping",
            page_mapping_reliable=True,
            stored_recognition_job_id="job-1",
            stored_model_id="42",
            pages=(page,),
            candidate_selection_rules=("rule",),
            global_ambiguities=(),
            warnings=("warning",),
            inspected_transcript=inspected,
        )
        stdout = StringIO()
        call_command(
            "audit_transkribus_transcript_versions",
            "--document-id=123",
            "--transcript-id=7",
            "--include-sample-text",
            stdout=stdout,
        )
        output = stdout.getvalue()
        validate_audit_payload(audit_to_json_dict(mock_fetch.return_value))
        self.assertIn("Safe sample only", output)
        self.assertNotIn("https://", output.lower())
