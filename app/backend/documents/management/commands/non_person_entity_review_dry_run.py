from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_review_dry_run import (
    DryRunError,
    format_dry_run_report,
    run_non_person_entity_review_dry_run,
)
from documents.services.non_person_entity_review_preflight import PreflightError


class Command(BaseCommand):
    help = (
        "Read-only dry-run of the v6 non-person entity review workbook against "
        "the current database and authoritative source text. Prints "
        "READY_TO_APPLY, ALREADY_APPLIED, STATE_DRIFT, and BLOCKED. "
        "This command has no apply mode and performs no database writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workbook",
            required=True,
            help="Path to the authoritative v6 xlsx workbook.",
        )

    def handle(self, *args, **options):
        try:
            result = run_non_person_entity_review_dry_run(options["workbook"])
        except (PreflightError, DryRunError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(format_dry_run_report(result))
