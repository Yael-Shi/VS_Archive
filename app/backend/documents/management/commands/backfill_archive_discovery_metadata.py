from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from documents.services.archive_discovery_metadata_backfill import (
    ApplyResult,
    BackfillReport,
    apply_archive_discovery_metadata_backfill,
    build_archive_discovery_metadata_backfill_report,
)


class Command(BaseCommand):
    help = (
        "Report or backfill ArchiveItem discovery metadata from legacy OCR-side "
        "Document.tags_m2m and Document.category_event. Default is dry-run (no writes). "
        "Pass --apply to link tags and categories onto ArchiveItem."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply backfill (Document → ArchiveItem). Default is dry-run.",
        )
        parser.add_argument(
            "--document-id",
            type=int,
            default=None,
            help="Optional Document id filter.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply"])

        report = build_archive_discovery_metadata_backfill_report(
            document_id=options.get("document_id"),
        )

        apply_result: ApplyResult | None = None
        if apply_mode:
            apply_result = apply_archive_discovery_metadata_backfill(report)

        if options.get("json"):
            payload = report.to_json_dict()
            payload["mode"] = "apply" if apply_mode else "dry-run"
            if apply_result is not None:
                payload["apply"] = {
                    "documents_updated": apply_result.documents_updated,
                    "tag_links_added": apply_result.tag_links_added,
                    "categories_created": apply_result.categories_created,
                    "category_links_added": apply_result.category_links_added,
                }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if apply_mode:
            self._write_apply_output(report, apply_result)
        else:
            self._write_dry_run_output(report)

    def _write_summary(self, report: BackfillReport) -> None:
        self.stdout.write(f"  scanned_ocr_documents: {report.scanned_ocr_documents}")
        self.stdout.write(
            f"  documents_missing_archive_item: {report.documents_missing_archive_item}"
        )
        self.stdout.write(
            f"  documents_with_legacy_tags: {report.documents_with_legacy_tags}"
        )
        self.stdout.write(f"  tag_links_to_add: {report.tag_links_to_add}")
        self.stdout.write(f"  tag_links_skipped: {report.tag_links_skipped}")
        self.stdout.write(
            f"  documents_with_category_event: {report.documents_with_category_event}"
        )
        self.stdout.write(f"  categories_to_create: {report.categories_to_create}")
        self.stdout.write(f"  category_links_to_add: {report.category_links_to_add}")
        self.stdout.write(
            f"  category_links_skipped: {report.category_links_skipped}"
        )

    def _write_planned_rows(self, report: BackfillReport) -> None:
        if not report.rows:
            return
        self.stdout.write("")
        self.stdout.write("Planned changes:")
        for row in report.rows:
            self.stdout.write(
                f"  document_id={row.document_id} "
                f"archive_item_id={row.archive_item_id}"
            )
            if row.tag_names_to_add:
                self.stdout.write(
                    f"    tags to add: {', '.join(row.tag_names_to_add)!r}"
                )
            if row.tag_names_skipped:
                self.stdout.write(
                    f"    tags already linked (skipped): "
                    f"{', '.join(row.tag_names_skipped)!r}"
                )
            if row.category_link_to_add:
                created = " (new ArchiveCategory)" if row.category_would_be_created else ""
                self.stdout.write(
                    f"    category to add: {row.category_name!r}{created}"
                )
            if row.category_link_skipped:
                self.stdout.write(
                    f"    category already linked (skipped): {row.category_name!r}"
                )

    def _write_dry_run_output(self, report: BackfillReport) -> None:
        self.stdout.write("Archive discovery metadata backfill (dry run)")
        self.stdout.write(
            "Mode: dry-run (no writes). Pass --apply to link legacy tags and "
            "category_event onto ArchiveItem."
        )
        self.stdout.write("")
        self.stdout.write("Summary:")
        self._write_summary(report)
        self._write_planned_rows(report)
        if report.tag_links_to_add or report.category_links_to_add:
            self.stdout.write("")
            self.stdout.write("Apply hint:")
            self.stdout.write("  --apply   link legacy tags and categories onto ArchiveItem")

    def _write_apply_output(
        self,
        report: BackfillReport,
        apply_result: ApplyResult,
    ) -> None:
        self.stdout.write("Archive discovery metadata backfill (apply)")
        self.stdout.write("Mode: apply (legacy Document fields are not modified).")
        self.stdout.write("")
        self.stdout.write("Pre-apply scan:")
        self._write_summary(report)
        self._write_planned_rows(report)
        self.stdout.write("")
        self.stdout.write("Apply results:")
        self.stdout.write(f"  documents_updated: {apply_result.documents_updated}")
        self.stdout.write(f"  tag_links_added: {apply_result.tag_links_added}")
        self.stdout.write(f"  categories_created: {apply_result.categories_created}")
        self.stdout.write(
            f"  category_links_added: {apply_result.category_links_added}"
        )
