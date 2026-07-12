from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.document_s3_orphan_cleanup import (
    DocumentS3OrphanCleanupApplyResult,
    DocumentS3OrphanCleanupReport,
    apply_document_s3_orphan_cleanup,
    build_document_s3_orphan_cleanup_report,
)


class Command(BaseCommand):
    help = (
        "Audit and optionally delete orphaned S3 objects under documents/ that are "
        "not referenced by Document or DocumentSourceFile rows. Default is dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Delete candidate orphan S3 objects.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help=(
                "Only S3 objects last modified older than this many hours "
                "(default: 24)."
            ),
        )
        parser.add_argument(
            "--prefix",
            default="documents/",
            help="S3 prefix to scan (default: documents/). Must stay within documents/.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional maximum number of orphan candidates to process.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    def handle(self, *args, **options):
        older_than_hours = int(options["older_than_hours"])
        if older_than_hours < 1:
            raise CommandError("--older-than-hours must be >= 1.")

        limit = options.get("limit")
        if limit is not None and limit <= 0:
            raise CommandError("--limit must be > 0.")

        commit_mode = bool(options["commit"])

        try:
            report = build_document_s3_orphan_cleanup_report(
                older_than_hours=older_than_hours,
                prefix=options["prefix"],
                limit=limit,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        apply_result = None
        if commit_mode:
            try:
                apply_result = apply_document_s3_orphan_cleanup(report)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc

        if options.get("json"):
            payload = report.to_json_dict()
            payload["mode"] = "commit" if commit_mode else "dry-run"
            if apply_result is not None:
                payload["commit"] = {
                    "s3_keys_deleted": apply_result.s3_keys_deleted,
                    "s3_keys_not_found": apply_result.s3_keys_not_found,
                    "s3_delete_failures": [
                        {
                            "s3_key": failure.s3_key,
                            "error": failure.error,
                        }
                        for failure in apply_result.s3_delete_failures
                    ],
                }
            self.stdout.write(json.dumps(payload, indent=2, sort_keys=True))
            if apply_result is not None and apply_result.has_delete_failures:
                raise CommandError(
                    "One or more S3 delete operations failed; see s3_delete_failures."
                )
            return

        if commit_mode:
            assert apply_result is not None
            self._write_commit_output(report, apply_result)
            if apply_result.has_delete_failures:
                raise CommandError(
                    "One or more S3 delete operations failed; see output above."
                )
        else:
            self._write_dry_run_output(report)

    def _write_candidate_rows(self, report: DocumentS3OrphanCleanupReport) -> None:
        if not report.candidates:
            self.stdout.write("")
            self.stdout.write("No orphaned document S3 objects found.")
            return

        self.stdout.write("")
        self.stdout.write("Candidates:")
        for row in report.candidates:
            self.stdout.write(f"  key={row.key}")
            self.stdout.write(f"    last_modified: {row.last_modified}")
            self.stdout.write(f"    size: {row.size}")

    def _write_dry_run_output(self, report: DocumentS3OrphanCleanupReport) -> None:
        self.stdout.write("Document S3 orphan cleanup (dry run)")
        self.stdout.write(
            "Mode: dry-run (no writes). Pass --commit to delete orphan S3 objects."
        )
        self.stdout.write(
            f"Filters: older_than_hours={report.older_than_hours}, "
            f"prefix={report.prefix!r}, "
            f"limit={report.limit}, "
            f"bucket={report.bucket or '(not configured)'}"
        )
        self.stdout.write(f"Candidates: {report.candidate_count}")
        self._write_candidate_rows(report)
        if report.candidates:
            self.stdout.write("")
            self.stdout.write("Commit hint:")
            self.stdout.write("  --commit   delete orphan S3 objects")

    def _write_commit_output(
        self,
        report: DocumentS3OrphanCleanupReport,
        apply_result: DocumentS3OrphanCleanupApplyResult,
    ) -> None:
        self.stdout.write("Document S3 orphan cleanup (commit)")
        self.stdout.write("Mode: commit (orphan S3 deletes attempted).")
        self.stdout.write(
            f"Filters: older_than_hours={report.older_than_hours}, "
            f"prefix={report.prefix!r}, "
            f"limit={report.limit}, "
            f"bucket={report.bucket}"
        )
        self.stdout.write(f"Candidates scanned: {report.candidate_count}")
        self._write_candidate_rows(report)
        self.stdout.write("")
        self.stdout.write("Commit results:")
        self.stdout.write(f"  s3_keys_deleted: {apply_result.s3_keys_deleted}")
        self.stdout.write(f"  s3_keys_not_found: {apply_result.s3_keys_not_found}")
        if apply_result.s3_delete_failures:
            self.stdout.write("  s3_delete_failures:")
            for failure in apply_result.s3_delete_failures:
                self.stdout.write(
                    f"    - s3_key={failure.s3_key!r} error={failure.error!r}"
                )
            for failure in apply_result.s3_delete_failures:
                self.stderr.write(
                    self.style.WARNING(
                        "S3 delete failed: "
                        f"s3_key={failure.s3_key!r} "
                        f"error={failure.error!r}"
                    )
                )
