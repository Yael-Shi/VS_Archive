from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.document_thumbnail_backfill import (
    DocumentThumbnailBackfillApplyResult,
    DocumentThumbnailBackfillReport,
    apply_document_thumbnail_backfill,
    build_document_thumbnail_backfill_report,
)


class Command(BaseCommand):
    help = (
        "Backfill first-page thumbnails for uploaded IMAGE documents missing "
        "thumbnail_file_key (whitespace-only counts as missing). Default is "
        "dry-run (no S3 or DB writes). Pass --commit to generate and persist "
        "thumbnails. candidate_count is the number of candidates selected "
        "for this run after applying --limit."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Generate and persist thumbnails. Default is dry-run.",
        )
        parser.add_argument(
            "--document-id",
            type=int,
            default=None,
            help="Optional Document id filter.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help=(
                "Maximum eligible candidates to select for this run "
                "(lowest pk first). candidate_count reflects this cap."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options):
        commit_mode = bool(options["commit"])
        document_id = options.get("document_id")
        limit = options.get("limit")

        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive integer.")

        try:
            report = build_document_thumbnail_backfill_report(
                document_id=document_id,
                limit=limit,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if commit_mode and not report.bucket:
            raise CommandError(
                "UPLOADS_BUCKET_NAME is not configured; cannot generate document thumbnails."
            )

        apply_result: DocumentThumbnailBackfillApplyResult | None = None
        if commit_mode:
            try:
                apply_result = apply_document_thumbnail_backfill(report)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        if options.get("json"):
            mode = "commit" if commit_mode else "dry-run"
            payload = report.to_json_dict(mode=mode, apply_result=apply_result)
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if commit_mode:
            assert apply_result is not None
            self._write_commit_output(report, apply_result)
        else:
            self._write_dry_run_output(report)

    def _write_target_status(self, report: DocumentThumbnailBackfillReport) -> None:
        target = report.target
        if target is None:
            return

        document_id = target.document_id
        if target.disposition == "not_found":
            self.stdout.write(f"Document id={document_id} does not exist.")
            return
        if target.disposition == "has_thumbnail":
            self.stdout.write(
                f"Document id={document_id} already has thumbnail_file_key="
                f"{target.thumbnail_file_key!r}."
            )
            return
        if target.disposition == "ineligible":
            self.stdout.write(
                f"Document id={document_id} is not eligible: {target.reason}."
            )
            return

    def _write_candidates(self, report: DocumentThumbnailBackfillReport) -> None:
        if not report.candidates:
            if report.target is None:
                self.stdout.write("")
                self.stdout.write("No document thumbnail backfill candidates found.")
            return

        self.stdout.write("")
        self.stdout.write("Candidates:")
        for row in report.candidates:
            self.stdout.write(f"  document_id={row.document_id}")
            self.stdout.write(f"    doc_type: {row.doc_type}")
            self.stdout.write(f"    upload_status: {row.upload_status}")
            self.stdout.write(f"    source_file_key: {row.source_file_key}")

    def _write_summary_counts(
        self,
        report: DocumentThumbnailBackfillReport,
        apply_result: DocumentThumbnailBackfillApplyResult | None,
    ) -> None:
        self.stdout.write(
            f"  candidate_count: {report.candidate_count} "
            "(candidates selected this run; respects --limit)"
        )
        if apply_result is None:
            dry_results = report.dry_run_results()
            skipped_count = sum(1 for row in dry_results if row.status == "skipped")
            self.stdout.write("  generated_count: 0")
            self.stdout.write("  failed_count: 0")
            self.stdout.write(f"  skipped_count: {skipped_count}")
            return

        self.stdout.write(f"  generated_count: {apply_result.generated_count}")
        self.stdout.write(f"  failed_count: {apply_result.failed_count}")
        self.stdout.write(f"  skipped_count: {apply_result.skipped_count}")

    def _write_dry_run_output(self, report: DocumentThumbnailBackfillReport) -> None:
        self.stdout.write("Document thumbnail backfill (dry run)")
        self.stdout.write(
            "Mode: dry-run (no S3 access, no DB writes). Pass --commit to "
            "generate and persist thumbnails."
        )
        if report.document_id_filter is not None:
            self.stdout.write(
                f"Document filter: document_id={report.document_id_filter}"
            )
        if report.limit is not None:
            self.stdout.write(f"Limit: {report.limit}")
        self.stdout.write("")
        self._write_target_status(report)
        self.stdout.write("")
        self.stdout.write("Summary:")
        self._write_summary_counts(report, None)
        self._write_candidates(report)
        if report.candidate_count:
            self.stdout.write("")
            self.stdout.write("Commit hint:")
            self.stdout.write("  --commit   generate and persist thumbnails")

    def _write_commit_output(
        self,
        report: DocumentThumbnailBackfillReport,
        apply_result: DocumentThumbnailBackfillApplyResult,
    ) -> None:
        self.stdout.write("Document thumbnail backfill (commit)")
        self.stdout.write("Mode: commit (S3 read/write and DB metadata updates).")
        self.stdout.write(f"Bucket: {report.bucket}")
        if report.document_id_filter is not None:
            self.stdout.write(
                f"Document filter: document_id={report.document_id_filter}"
            )
        if report.limit is not None:
            self.stdout.write(f"Limit: {report.limit}")
        self.stdout.write("")
        self._write_target_status(report)
        self.stdout.write("")
        self.stdout.write("Summary:")
        self._write_summary_counts(report, apply_result)
        self._write_candidates(report)

        if apply_result.results:
            self.stdout.write("")
            self.stdout.write("Per-document results:")
            for row in apply_result.results:
                line = f"  document_id={row.document_id} status={row.status}"
                if row.thumbnail_file_key:
                    line += f" thumbnail_file_key={row.thumbnail_file_key!r}"
                if row.reason:
                    line += f" reason={row.reason!r}"
                self.stdout.write(line)
