from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.photo_thumbnail_backfill import (
    PhotoThumbnailBackfillApplyResult,
    PhotoThumbnailBackfillReport,
    apply_photo_thumbnail_backfill,
    build_photo_thumbnail_backfill_report,
)


class Command(BaseCommand):
    help = (
        "Backfill PHOTO thumbnails for uploaded PhotoContent rows missing "
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
            "--photo-id",
            type=int,
            default=None,
            help="Optional PhotoContent id filter.",
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
        photo_id = options.get("photo_id")
        limit = options.get("limit")

        if limit is not None and limit < 1:
            raise CommandError("--limit must be a positive integer.")

        try:
            report = build_photo_thumbnail_backfill_report(
                photo_id=photo_id,
                limit=limit,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if commit_mode and not report.bucket:
            raise CommandError(
                "UPLOADS_BUCKET_NAME is not configured; cannot generate photo thumbnails."
            )

        apply_result: PhotoThumbnailBackfillApplyResult | None = None
        if commit_mode:
            try:
                apply_result = apply_photo_thumbnail_backfill(report)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        if options.get("json"):
            payload = report.to_json_dict()
            payload["mode"] = "commit" if commit_mode else "dry-run"
            if apply_result is not None:
                payload["commit"] = apply_result.to_json_dict()
            else:
                payload["commit"] = None
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if commit_mode:
            assert apply_result is not None
            self._write_commit_output(report, apply_result)
        else:
            self._write_dry_run_output(report)

    def _write_target_status(self, report: PhotoThumbnailBackfillReport) -> None:
        target = report.target
        if target is None:
            return

        photo_id = target.photo_content_id
        if target.disposition == "not_found":
            self.stdout.write(f"PhotoContent id={photo_id} does not exist.")
            return
        if target.disposition == "has_thumbnail":
            self.stdout.write(
                f"PhotoContent id={photo_id} already has thumbnail_file_key="
                f"{target.thumbnail_file_key!r}."
            )
            return
        if target.disposition == "ineligible":
            self.stdout.write(
                f"PhotoContent id={photo_id} is not eligible: {target.reason}."
            )
            return

    def _write_candidates(self, report: PhotoThumbnailBackfillReport) -> None:
        if not report.candidates:
            if report.target is None:
                self.stdout.write("")
                self.stdout.write("No photo thumbnail backfill candidates found.")
            return

        self.stdout.write("")
        self.stdout.write("Candidates:")
        for row in report.candidates:
            self.stdout.write(f"  photo_content_id={row.photo_content_id}")
            self.stdout.write(f"    upload_status: {row.upload_status}")
            self.stdout.write(f"    original_file_key: {row.original_file_key}")

    def _write_summary_counts(
        self,
        report: PhotoThumbnailBackfillReport,
        apply_result: PhotoThumbnailBackfillApplyResult | None,
    ) -> None:
        self.stdout.write(
            f"  candidate_count: {report.candidate_count} "
            "(candidates selected this run; respects --limit)"
        )
        if apply_result is None:
            self.stdout.write("  processed_count: 0")
            self.stdout.write("  succeeded_count: 0")
            self.stdout.write("  failed_count: 0")
            self.stdout.write("  skipped_count: 0")
            return

        self.stdout.write(f"  processed_count: {apply_result.processed_count}")
        self.stdout.write(f"  succeeded_count: {apply_result.succeeded_count}")
        self.stdout.write(f"  failed_count: {apply_result.failed_count}")
        self.stdout.write(f"  skipped_count: {apply_result.skipped_count}")

    def _write_dry_run_output(self, report: PhotoThumbnailBackfillReport) -> None:
        self.stdout.write("Photo thumbnail backfill (dry run)")
        self.stdout.write(
            "Mode: dry-run (no S3 access, no DB writes). Pass --commit to "
            "generate and persist thumbnails."
        )
        if report.photo_id_filter is not None:
            self.stdout.write(
                f"Photo filter: photo_content_id={report.photo_id_filter}"
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
        report: PhotoThumbnailBackfillReport,
        apply_result: PhotoThumbnailBackfillApplyResult,
    ) -> None:
        self.stdout.write("Photo thumbnail backfill (commit)")
        self.stdout.write("Mode: commit (S3 read/write and DB metadata updates).")
        self.stdout.write(f"Bucket: {report.bucket}")
        if report.photo_id_filter is not None:
            self.stdout.write(
                f"Photo filter: photo_content_id={report.photo_id_filter}"
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
            self.stdout.write("Per-photo results:")
            for row in apply_result.results:
                line = (
                    f"  photo_content_id={row.photo_content_id} outcome={row.outcome}"
                )
                if row.thumbnail_file_key:
                    line += f" thumbnail_file_key={row.thumbnail_file_key!r}"
                if row.reason:
                    line += f" reason={row.reason!r}"
                self.stdout.write(line)
