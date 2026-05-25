from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from documents.services.transkribus_cleanup_report import (
    build_transkribus_cleanup_report,
)


class Command(BaseCommand):
    help = (
        "DRY RUN: report Transkribus cleanup/retention buckets from local DB state "
        "only. Makes no remote calls, deletes nothing, and does not mutate rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document-id",
            type=int,
            default=None,
            help="Optional VS-Archive document id filter.",
        )
        parser.add_argument(
            "--collection-id",
            type=str,
            default=None,
            help="Optional Transkribus collection id filter.",
        )
        parser.add_argument(
            "--model-id",
            type=str,
            default=None,
            help="Optional Transkribus model id filter.",
        )
        parser.add_argument(
            "--stale-hours",
            type=int,
            default=24,
            help="Mark in-progress runs older than this many hours as stale (default: 24).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the full dry-run report as JSON.",
        )

    def handle(self, *args, **options):
        stale_hours = int(options["stale_hours"])
        if stale_hours < 1:
            raise CommandError("--stale-hours must be >= 1.")

        try:
            report = build_transkribus_cleanup_report(
                document_id=options.get("document_id"),
                collection_id=options.get("collection_id"),
                model_id=options.get("model_id"),
                stale_hours=stale_hours,
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        if options.get("json"):
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return

        summary = report["summary"]
        filters = report["filters"]
        self.stdout.write("Transkribus cleanup report (dry run)")
        self.stdout.write(
            "Filters: "
            f"document_id={filters['document_id']}, "
            f"collection_id={filters['collection_id']}, "
            f"model_id={filters['model_id']}, "
            f"stale_hours={filters['stale_hours']}"
        )
        self.stdout.write(
            "Totals: "
            f"lineages={summary['lineage_count']}, "
            f"runs={summary['run_count']}, "
            f"remote_docs={summary['remote_doc_count']}"
        )
        self.stdout.write("Remote doc buckets:")
        for bucket, count in sorted(summary["remote_doc_bucket_counts"].items()):
            self.stdout.write(f"  - {bucket}: {count}")
        self.stdout.write("Run buckets:")
        for bucket, count in sorted(summary["run_bucket_counts"].items()):
            self.stdout.write(f"  - {bucket}: {count}")
