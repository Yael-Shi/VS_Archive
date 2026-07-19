from __future__ import annotations

import json
import os

from django.core.management.base import BaseCommand, CommandError

from documents.services import transkribus_engine as tr
from documents.services.transkribus_page_xml_geometry import (
    DocumentGeometryAudit,
    PageGeometryAudit,
    TranskribusPageXmlGeometryError,
    audit_to_json_dict,
    fetch_document_geometry_audit,
)


class Command(BaseCommand):
    help = (
        "Read-only audit of Transkribus PAGE XML line geometry for one document. "
        "Fetches the same transcript PAGE XML selected by the current PyLaia "
        "completion flow and reports geometry coverage. Makes no writes."
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
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )
        parser.add_argument(
            "--include-sample-text",
            action="store_true",
            help=(
                "Include one safely truncated sample line per inspected page "
                "(line id, truncated text, coords, bbox, baseline)."
            ),
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        page_index = options.get("page_index")
        as_json = bool(options.get("json"))
        include_sample_text = bool(options.get("include_sample_text"))

        username = (os.getenv("TRANSKRIBUS_USERNAME") or "").strip()
        password = (os.getenv("TRANSKRIBUS_PASSWORD") or "").strip()
        bearer_token = (os.getenv("TRANSKRIBUS_API_TOKEN") or "").strip()
        if not username or not password:
            raise CommandError(
                "Missing Transkribus session credentials. Set TRANSKRIBUS_USERNAME "
                "and TRANSKRIBUS_PASSWORD."
            )
        if not bearer_token:
            raise CommandError(
                "Missing Transkribus transcript bearer token. Set TRANSKRIBUS_API_TOKEN."
            )

        try:
            audit = fetch_document_geometry_audit(
                document_id=document_id,
                page_index=page_index,
                include_sample_text=include_sample_text,
                username=username,
                password=password,
                bearer_token=bearer_token,
            )
        except TranskribusPageXmlGeometryError as exc:
            raise CommandError(str(exc)) from exc
        except tr.TranskribusPermanentError as exc:
            raise CommandError(f"Transkribus provider error: {exc}") from exc
        except tr.TranskribusRetryableError as exc:
            raise CommandError(f"Transkribus provider error: {exc}") from exc

        if as_json:
            self.stdout.write(
                json.dumps(audit_to_json_dict(audit), indent=2, sort_keys=True)
            )
            return

        self._write_human_report(audit, include_sample_text=include_sample_text)

    def _write_human_report(
        self,
        audit: DocumentGeometryAudit,
        *,
        include_sample_text: bool,
    ) -> None:
        self.stdout.write(f"Document: {audit.document_id}")
        self.stdout.write(f"TranskribusRun: {audit.transkribus_run_id}")
        self.stdout.write(f"Remote document: {audit.remote_doc_id}")
        self.stdout.write(f"Mapping: {audit.mapping_description}")

        for page in audit.pages:
            self.stdout.write("")
            self._write_page_report(page, include_sample_text=include_sample_text)

        self.stdout.write("")
        self.stdout.write("Verdict")
        self.stdout.write(
            f"  line_geometry_capability: {audit.line_geometry_capability}"
        )
        self.stdout.write(
            f"  page_mapping_reliable: {'yes' if audit.page_mapping_reliable else 'no'}"
        )
        self.stdout.write(
            "  suitable_for_overlay_poc: "
            f"{'yes' if audit.suitable_for_overlay_poc else 'no'}"
        )
        self.stdout.write(
            "  baseline_only_fallback_needed: "
            f"{'yes' if audit.baseline_only_fallback_needed else 'no'}"
        )
        self.stdout.write(
            "  suitable_for_persistence_design: "
            f"{'yes' if audit.suitable_for_persistence_design else 'no'}"
        )
        for warning in audit.warnings:
            self.stdout.write(f"  warning: {warning}")

    def _write_page_report(
        self,
        page: PageGeometryAudit,
        *,
        include_sample_text: bool,
    ) -> None:
        self.stdout.write(f"Page {page.page_index}")
        self.stdout.write(f"  Transkribus pageNr: {page.page_nr}")
        if page.provider_page_id is not None:
            self.stdout.write(f"  Provider page id: {page.provider_page_id}")
        if page.transcript_ts_id is not None:
            self.stdout.write(f"  Transcript tsId: {page.transcript_ts_id}")
        if page.transcript_job_id is not None:
            self.stdout.write(f"  Transcript jobId: {page.transcript_job_id}")
        if page.transcript_model_id is not None:
            self.stdout.write(f"  Transcript modelId: {page.transcript_model_id}")

        size_bits: list[str] = []
        if page.image_width is not None:
            size_bits.append(str(page.image_width))
        else:
            size_bits.append("?")
        if page.image_height is not None:
            size_bits.append(str(page.image_height))
        else:
            size_bits.append("?")
        self.stdout.write(f"  Size: {' × '.join(size_bits)}")
        if page.image_filename:
            self.stdout.write(f"  imageFilename: {page.image_filename}")
        if page.page_namespace:
            self.stdout.write(f"  PAGE namespace: {page.page_namespace}")

        self.stdout.write(f"  Text regions: {page.text_region_count}")
        self.stdout.write(f"  Text lines: {page.text_line_count}")
        self.stdout.write(f"  Words: {page.word_count}")
        self.stdout.write(
            f"  Reading order present: {'yes' if page.reading_order_present else 'no'}"
        )
        if page.reading_order_resolved:
            self.stdout.write(
                "  Lines with XML order != reading order: "
                f"{page.lines_xml_order_differs_from_reading_order}"
            )

        total = page.lines_with_non_empty_text
        self.stdout.write(
            f"  Lines with text + valid coords: "
            f"{page.lines_with_text_and_valid_coords}/{total}"
        )
        self.stdout.write(
            f"  Lines with baseline: {page.lines_with_baseline}/{page.text_line_count}"
        )
        self.stdout.write(
            f"  Lines with text + valid baseline: "
            f"{page.lines_with_text_and_valid_baseline}/{total}"
        )
        self.stdout.write(f"  Malformed polygons: {page.malformed_polygons}")
        self.stdout.write(f"  Degenerate polygons: {page.degenerate_polygons}")
        if page.bounds_validation_available:
            self.stdout.write(
                f"  Out-of-bounds polygons: {page.polygons_outside_page_bounds}"
            )
        else:
            self.stdout.write(
                "  Bounds validation: unavailable (missing page dimensions)"
            )
        self.stdout.write(
            f"  Negative/outside coordinates: {page.negative_or_outside_coordinates}"
        )
        self.stdout.write(f"  Duplicate line ids: {page.duplicate_line_ids}")
        self.stdout.write(f"  Page capability: {page.page_capability}")

        if include_sample_text and page.sample_line is not None:
            sample = page.sample_line
            self.stdout.write("  Sample line:")
            self.stdout.write(f"    id: {sample.line_id}")
            self.stdout.write(f"    text: {sample.text}")
            self.stdout.write(f"    coords: {sample.coords_points}")
            if sample.bounding_box is not None:
                bb = sample.bounding_box
                self.stdout.write(
                    f"    bbox: {bb.min_x},{bb.min_y} .. {bb.max_x},{bb.max_y}"
                )
            if sample.baseline_points:
                self.stdout.write(f"    baseline: {sample.baseline_points}")
