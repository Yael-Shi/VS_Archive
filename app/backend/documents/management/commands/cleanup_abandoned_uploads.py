from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.abandoned_upload_cleanup import (
    AbandonedUploadCleanupReport,
    apply_abandoned_upload_cleanup,
    build_abandoned_upload_cleanup_report,
)


class Command(BaseCommand):
    help = (
        "Find and optionally clean abandoned incremental OCR upload drafts "
        "(unfinished IMAGE uploads with no fixed part count). Default is dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Delete candidate S3 objects and remove draft documents from the DB.",
        )
        parser.add_argument(
            "--stale-hours",
            type=int,
            default=24,
            help=(
                "Only drafts with updated_at older than this many hours (default: 24)."
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
        stale_hours = int(options["stale_hours"])
        if stale_hours < 1:
            raise CommandError("--stale-hours must be >= 1.")

        commit_mode = bool(options["commit"])

        try:
            report = build_abandoned_upload_cleanup_report(
                stale_hours=stale_hours,
                document_id=options.get("document_id"),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        apply_result = None
        if commit_mode:
            try:
                apply_result = apply_abandoned_upload_cleanup(report)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        if options.get("json"):
            payload = report.to_json_dict()
            payload["mode"] = "commit" if commit_mode else "dry-run"
            if apply_result is not None:
                payload["commit"] = {
                    "documents_deleted": apply_result.documents_deleted,
                    "s3_keys_deleted": apply_result.s3_keys_deleted,
                    "s3_keys_not_found": apply_result.s3_keys_not_found,
                    "s3_delete_failures": [
                        {
                            "document_id": failure.document_id,
                            "s3_key": failure.s3_key,
                            "error": failure.error,
                        }
                        for failure in apply_result.s3_delete_failures
                    ],
                }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            return

        if commit_mode:
            assert apply_result is not None
            self._write_commit_output(report, apply_result)
        else:
            self._write_dry_run_output(report)

    def _write_candidate_rows(self, report: AbandonedUploadCleanupReport) -> None:
        if not report.candidates:
            self.stdout.write("")
            self.stdout.write("No abandoned incremental OCR upload drafts found.")
            return

        self.stdout.write("")
        self.stdout.write("Candidates:")
        for row in report.candidates:
            self.stdout.write(f"  document_id={row.document_id}")
            if row.title:
                self.stdout.write(f"    title: {row.title!r}")
            self.stdout.write(f"    created_at: {row.created_at}")
            self.stdout.write(f"    updated_at: {row.updated_at}")
            self.stdout.write(f"    upload_status: {row.upload_status}")
            self.stdout.write(f"    processing_state_user: {row.processing_state_user}")
            self.stdout.write(f"    source_file_count: {row.source_file_count}")
            if row.s3_keys:
                self.stdout.write("    s3_keys:")
                for key in row.s3_keys:
                    self.stdout.write(f"      - {key}")
            else:
                self.stdout.write("    s3_keys: (none)")

    def _write_dry_run_output(self, report: AbandonedUploadCleanupReport) -> None:
        self.stdout.write("Abandoned incremental OCR upload cleanup (dry run)")
        self.stdout.write(
            "Mode: dry-run (no writes). Pass --commit to delete S3 objects "
            "and remove draft documents."
        )
        self.stdout.write(
            f"Filters: stale_hours={report.stale_hours}, "
            f"document_id={report.document_id_filter}, "
            f"bucket={report.bucket or '(not configured)'}"
        )
        self.stdout.write(f"Candidates: {report.candidate_count}")
        self._write_candidate_rows(report)
        if report.candidates:
            self.stdout.write("")
            self.stdout.write("Commit hint:")
            self.stdout.write("  --commit   delete S3 objects and remove draft rows")

    def _write_commit_output(
        self,
        report: AbandonedUploadCleanupReport,
        apply_result,
    ) -> None:
        self.stdout.write("Abandoned incremental OCR upload cleanup (commit)")
        self.stdout.write("Mode: commit (S3 deletes attempted; draft rows removed).")
        self.stdout.write(
            f"Filters: stale_hours={report.stale_hours}, "
            f"document_id={report.document_id_filter}, "
            f"bucket={report.bucket}"
        )
        self.stdout.write(f"Candidates scanned: {report.candidate_count}")
        self._write_candidate_rows(report)
        self.stdout.write("")
        self.stdout.write("Commit results:")
        self.stdout.write(f"  documents_deleted: {apply_result.documents_deleted}")
        self.stdout.write(f"  s3_keys_deleted: {apply_result.s3_keys_deleted}")
        self.stdout.write(f"  s3_keys_not_found: {apply_result.s3_keys_not_found}")
        if apply_result.s3_delete_failures:
            self.stdout.write("  s3_delete_failures:")
            for failure in apply_result.s3_delete_failures:
                self.stdout.write(
                    "    - "
                    f"document_id={failure.document_id} "
                    f"s3_key={failure.s3_key!r} "
                    f"error={failure.error!r}"
                )
            for failure in apply_result.s3_delete_failures:
                self.stderr.write(
                    self.style.WARNING(
                        "S3 delete failed: "
                        f"document_id={failure.document_id} "
                        f"s3_key={failure.s3_key!r} "
                        f"error={failure.error!r}"
                    )
                )
            self.stderr.write(
                self.style.WARNING(
                    "Draft DB rows were still removed for affected documents."
                )
            )
