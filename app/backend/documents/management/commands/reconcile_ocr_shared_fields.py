from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.ocr_shared_field_reconciliation import (
    ApplyResult,
    ReconciliationReport,
    ReconciliationRow,
    serialize_reconciliation_value,
    apply_ocr_shared_field_reconciliation,
    build_ocr_shared_field_reconciliation_report,
)


class Command(BaseCommand):
    help = (
        "Compare OCR_DOCUMENT shared fields on Document vs linked ArchiveItem. "
        "Default is dry-run (no writes). Pass --apply to reconcile ArchiveItem "
        "from Document; visibility requires --include-visibility."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply reconciliation (Document → ArchiveItem). Default is dry-run.",
        )
        parser.add_argument(
            "--include-visibility",
            action="store_true",
            help=(
                "With --apply, also copy visibility (affects who can view items). "
                "Invalid without --apply."
            ),
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
        include_visibility = bool(options["include_visibility"])

        if include_visibility and not apply_mode:
            raise CommandError(
                "--include-visibility requires --apply. "
                "Dry-run detects visibility drift but never writes."
            )

        report = build_ocr_shared_field_reconciliation_report(
            document_id=options.get("document_id"),
        )

        apply_result: ApplyResult | None = None
        if apply_mode:
            apply_result = apply_ocr_shared_field_reconciliation(
                report,
                include_visibility=include_visibility,
            )

        if options.get("json"):
            payload = report.to_json_dict()
            payload["mode"] = "apply" if apply_mode else "dry-run"
            if apply_result is not None:
                payload["apply"] = {
                    "documents_updated": apply_result.documents_updated,
                    "fields_updated": apply_result.fields_updated,
                    "visibility_skipped_count": apply_result.visibility_skipped_count,
                    "include_visibility": apply_result.include_visibility,
                }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if apply_mode:
            self._write_apply_output(report, apply_result)
        else:
            self._write_dry_run_output(report)

    def _write_summary(self, report: ReconciliationReport) -> None:
        self.stdout.write(f"  documents_checked: {report.documents_checked}")
        self.stdout.write(f"  in_sync: {report.in_sync}")
        self.stdout.write(f"  with_mismatches: {report.with_mismatches}")
        self.stdout.write(f"  visibility_mismatches: {report.visibility_mismatches}")

    def _write_mismatch_counts(self, report: ReconciliationReport) -> None:
        self.stdout.write("")
        self.stdout.write("Mismatch counts by field:")
        for name, count in report.mismatch_counts_by_field.items():
            self.stdout.write(f"  {name}: {count}")

    def _write_mismatch_rows(self, report: ReconciliationReport) -> None:
        visibility_rows = [
            row for row in report.mismatched_rows if "visibility" in row.mismatches
        ]
        if visibility_rows:
            self.stdout.write("")
            self.stdout.write(
                "[!] VISIBILITY MISMATCHES (access-control field — "
                "review before --include-visibility)"
            )
            for row in visibility_rows:
                self._write_row(row, fields=("visibility",))

        rows_with_non_visibility = [
            row
            for row in report.mismatched_rows
            if any(name != "visibility" for name in row.mismatches)
        ]
        if rows_with_non_visibility:
            self.stdout.write("")
            self.stdout.write("Other mismatches:")
            for row in rows_with_non_visibility:
                fields = tuple(name for name in row.mismatches if name != "visibility")
                self._write_row(row, fields=fields)

    def _write_row(
        self,
        row: ReconciliationRow,
        *,
        fields: tuple[str, ...],
    ) -> None:
        self.stdout.write(
            f"  document_id={row.document_id} "
            f"archive_item_id={row.archive_item_id} "
            f'title="{row.title}"'
        )
        for name in fields:
            mismatch = row.mismatches[name]
            self.stdout.write(
                f"    {name}: "
                f"document={serialize_reconciliation_value(mismatch.document_value)!r} "
                f"archive_item={serialize_reconciliation_value(mismatch.archive_item_value)!r}"
            )

    def _write_apply_hint(self) -> None:
        self.stdout.write("")
        self.stdout.write("Apply hint:")
        self.stdout.write(
            "  --apply                     reconcile non-visibility fields only"
        )
        self.stdout.write(
            "  --apply --include-visibility  also copy visibility (affects access)"
        )

    def _write_dry_run_output(self, report: ReconciliationReport) -> None:
        self.stdout.write("OCR shared-field reconciliation (dry run)")
        self.stdout.write(
            "Mode: dry-run (no writes). Pass --apply to reconcile ArchiveItem "
            "from Document."
        )
        self.stdout.write("")
        self.stdout.write("Summary:")
        self._write_summary(report)
        self._write_mismatch_counts(report)
        if report.mismatched_rows:
            self._write_mismatch_rows(report)
        self._write_apply_hint()

    def _write_apply_output(
        self,
        report: ReconciliationReport,
        apply_result: ApplyResult,
    ) -> None:
        self.stdout.write("OCR shared-field reconciliation (apply)")
        if apply_result.include_visibility:
            self.stdout.write(
                "Mode: apply with visibility included — access may change."
            )
        else:
            self.stdout.write(
                "Mode: apply (visibility skipped — pass --include-visibility "
                "to copy visibility)."
            )
        self.stdout.write("")
        self.stdout.write("Pre-apply scan:")
        self._write_summary(report)
        self._write_mismatch_counts(report)
        if report.mismatched_rows:
            self._write_mismatch_rows(report)
        self.stdout.write("")
        self.stdout.write("Apply results:")
        self.stdout.write(f"  documents_updated: {apply_result.documents_updated}")
        self.stdout.write(f"  fields_updated: {apply_result.fields_updated}")
        self.stdout.write(
            f"  visibility_skipped_count: {apply_result.visibility_skipped_count}"
        )
