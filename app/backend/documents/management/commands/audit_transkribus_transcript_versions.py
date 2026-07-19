from __future__ import annotations

import json
import os

from django.core.management.base import BaseCommand, CommandError

from documents.services import transkribus_engine as tr
from documents.services.transkribus_transcript_versions import (
    DocumentTranscriptVersionAudit,
    PageTranscriptVersionAudit,
    TranscriptPageXmlSummary,
    TranscriptVersionMetadata,
    TranskribusTranscriptVersionsError,
    audit_to_json_dict,
    fetch_document_transcript_version_audit,
    validate_audit_payload,
)


class Command(BaseCommand):
    help = (
        "Read-only audit of Transkribus transcript versions for one document. "
        "Lists safe metadata for every provider transcript on mapped pages and "
        "summarizes candidate corrected-version selection rules. Makes no writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document-id",
            type=int,
            required=True,
            help="VS-Archive Document id to audit.",
        )
        parser.add_argument(
            "--page-index",
            type=int,
            default=None,
            help="Optional local 1-based page index to audit (default: all mapped pages).",
        )
        parser.add_argument(
            "--transcript-id",
            type=str,
            default=None,
            help=(
                "Optional Transkribus tsId to fetch and inspect PAGE XML for "
                "(one explicitly selected transcript)."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )
        parser.add_argument(
            "--include-sample-text",
            action="store_true",
            help=(
                "When used with --transcript-id, include one sanitized truncated "
                "sample line from the inspected PAGE XML."
            ),
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        page_index = options.get("page_index")
        transcript_id = options.get("transcript_id")
        as_json = bool(options.get("json"))
        include_sample_text = bool(options.get("include_sample_text"))

        if include_sample_text and not transcript_id:
            raise CommandError(
                "--include-sample-text requires --transcript-id for PAGE XML inspection."
            )

        username = (os.getenv("TRANSKRIBUS_USERNAME") or "").strip()
        password = (os.getenv("TRANSKRIBUS_PASSWORD") or "").strip()
        bearer_token = (os.getenv("TRANSKRIBUS_API_TOKEN") or "").strip()
        if not username or not password:
            raise CommandError(
                "Missing Transkribus session credentials. Set TRANSKRIBUS_USERNAME "
                "and TRANSKRIBUS_PASSWORD."
            )
        if transcript_id and not bearer_token:
            raise CommandError(
                "Missing Transkribus transcript bearer token for PAGE XML inspection. "
                "Set TRANSKRIBUS_API_TOKEN."
            )

        try:
            audit = fetch_document_transcript_version_audit(
                document_id=document_id,
                page_index=page_index,
                transcript_id=transcript_id,
                include_sample_text=include_sample_text,
                username=username,
                password=password,
                bearer_token=bearer_token,
            )
        except TranskribusTranscriptVersionsError as exc:
            raise CommandError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            raise CommandError(f"Transkribus provider error: {exc}") from exc
        except tr.TranskribusRetryableError as exc:
            raise CommandError(f"Transkribus provider error: {exc}") from exc

        payload = audit_to_json_dict(audit)
        validate_audit_payload(payload)

        if as_json:
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        self._write_human_report(
            audit,
            include_sample_text=include_sample_text,
        )

    def _write_human_report(
        self,
        audit: DocumentTranscriptVersionAudit,
        *,
        include_sample_text: bool,
    ) -> None:
        self.stdout.write(f"Document: {audit.document_id}")
        self.stdout.write(f"TranskribusRun: {audit.transkribus_run_id}")
        self.stdout.write(f"Remote document: {audit.remote_doc_id}")
        self.stdout.write(f"Mapping: {audit.mapping_description}")
        self.stdout.write(
            f"Stored HTR job/model: {audit.stored_recognition_job_id} / "
            f"{audit.stored_model_id}"
        )

        for page in audit.pages:
            self.stdout.write("")
            self._write_page_report(page)

        if audit.inspected_transcript is not None:
            self.stdout.write("")
            self._write_inspected_transcript_report(
                audit.inspected_transcript,
                include_sample_text=include_sample_text,
            )

        self.stdout.write("")
        self.stdout.write("Candidate selection rules (observed; not applied)")
        for rule in audit.candidate_selection_rules:
            self.stdout.write(f"  - {rule}")

        if audit.global_ambiguities:
            self.stdout.write("")
            self.stdout.write("Ambiguities")
            for item in audit.global_ambiguities:
                self.stdout.write(f"  - {item}")

        self.stdout.write("")
        self.stdout.write("Warnings")
        for warning in audit.warnings:
            self.stdout.write(f"  - {warning}")

    def _write_page_report(self, page: PageTranscriptVersionAudit) -> None:
        self.stdout.write(f"Page {page.page_index}")
        self.stdout.write(f"  Transkribus pageNr: {page.page_nr}")
        if page.provider_page_id is not None:
            self.stdout.write(f"  Provider page id: {page.provider_page_id}")
        self.stdout.write(f"  Transcript count: {page.transcript_count}")
        self.stdout.write(f"  {page.list_order_note}")
        self.stdout.write(
            f"  Original HTR present: {'yes' if page.original_htr_present else 'no'}"
        )
        if page.original_htr_ts_ids:
            self.stdout.write(
                f"  Original HTR tsId(s): {', '.join(page.original_htr_ts_ids)}"
            )
        if page.non_matching_version_ts_ids:
            self.stdout.write(
                "  Non-matching-version tsId(s): "
                f"{', '.join(page.non_matching_version_ts_ids)}"
            )
        if page.insufficient_metadata_ts_ids:
            self.stdout.write(
                "  Insufficient-metadata tsId(s): "
                f"{', '.join(page.insufficient_metadata_ts_ids)}"
            )

        if page.provider_version_signals:
            self.stdout.write("  Provider version signals:")
            for signal in page.provider_version_signals:
                self.stdout.write(f"    - {signal}")

        for transcript in page.transcripts:
            self.stdout.write("")
            self._write_transcript_report(transcript)

        if page.ambiguities:
            self.stdout.write("")
            self.stdout.write("  Page ambiguities:")
            for item in page.ambiguities:
                self.stdout.write(f"    - {item}")

    def _write_transcript_report(self, transcript: TranscriptVersionMetadata) -> None:
        label = transcript.ts_id or "(missing tsId)"
        self.stdout.write(
            f"  Transcript list_position={transcript.list_position} tsId={label}"
        )
        self.stdout.write(f"    classification: {transcript.classification}")
        if transcript.job_id is not None:
            self.stdout.write(f"    jobId: {transcript.job_id}")
        if transcript.model_id is not None:
            self.stdout.write(f"    modelId: {transcript.model_id}")
        if transcript.status is not None:
            self.stdout.write(f"    status: {transcript.status}")
        self.stdout.write(
            "    matches stored HTR job/model: "
            f"{'yes' if transcript.matches_stored_htr_job_model else 'no'}"
        )
        if transcript.timestamp_fields:
            self.stdout.write(
                f"    timestamp fields: {self._format_field_map(transcript.timestamp_fields)}"
            )
        if transcript.user_editor_fields:
            self.stdout.write(
                f"    user/editor fields: {self._format_field_map(transcript.user_editor_fields)}"
            )
        if transcript.version_indicator_fields:
            self.stdout.write(
                "    version indicators: "
                f"{self._format_field_map(transcript.version_indicator_fields)}"
            )
        if transcript.other_safe_fields:
            self.stdout.write(
                f"    other safe fields: {self._format_field_map(transcript.other_safe_fields)}"
            )
        if transcript.observed_edit_signals:
            self.stdout.write("    observed edit-related signals:")
            for signal in transcript.observed_edit_signals:
                self.stdout.write(f"      - {signal}")
        if transcript.classification_reasons:
            self.stdout.write("    reasons:")
            for reason in transcript.classification_reasons:
                self.stdout.write(f"      - {reason}")

    def _write_inspected_transcript_report(
        self,
        inspected: TranscriptPageXmlSummary,
        *,
        include_sample_text: bool,
    ) -> None:
        self.stdout.write("Inspected transcript PAGE XML")
        self.stdout.write(f"  tsId: {inspected.ts_id}")
        self.stdout.write(f"  page_index: {inspected.page_index}")
        self.stdout.write(f"  Transkribus pageNr: {inspected.page_nr}")
        width = inspected.image_width if inspected.image_width is not None else "?"
        height = inspected.image_height if inspected.image_height is not None else "?"
        self.stdout.write(f"  page dimensions: {width} x {height}")
        self.stdout.write(f"  text regions: {inspected.text_region_count}")
        self.stdout.write(f"  text lines: {inspected.text_line_count}")
        self.stdout.write(
            f"  text-bearing lines: {inspected.lines_with_non_empty_text}"
        )
        self.stdout.write(
            "  lines with text + valid polygon: "
            f"{inspected.lines_with_text_and_valid_coords}/"
            f"{inspected.lines_with_non_empty_text}"
        )
        self.stdout.write(
            f"  lines with baseline: {inspected.lines_with_baseline}/"
            f"{inspected.text_line_count}"
        )
        self.stdout.write(
            "  lines with text + valid baseline: "
            f"{inspected.lines_with_text_and_valid_baseline}/"
            f"{inspected.lines_with_non_empty_text}"
        )
        if inspected.page_namespace:
            self.stdout.write(f"  PAGE namespace: {inspected.page_namespace}")
        self.stdout.write(f"  content sha256: {inspected.content_sha256}")
        if include_sample_text and inspected.sample_line_text is not None:
            self.stdout.write("  sample line:")
            self.stdout.write(f"    id: {inspected.sample_line_id}")
            self.stdout.write(f"    text: {inspected.sample_line_text}")

    @staticmethod
    def _format_field_map(fields: dict) -> str:
        parts = [f"{key}={value}" for key, value in sorted(fields.items())]
        return ", ".join(parts)
