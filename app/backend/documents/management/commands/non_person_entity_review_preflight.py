from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_review_preflight import (
    PreflightError,
    format_preflight_report,
    preflight_authoritative_workbook,
)


class Command(BaseCommand):
    help = (
        "Read-only preflight of the v3 non-person entity review workbook. "
        "Verifies the fixed SHA-256, parses FINAL_RECON_2026-10-04, and "
        "prints parser counts plus future apply blockers. "
        "This command performs no database writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workbook",
            required=True,
            help="Path to the authoritative v3 xlsx workbook.",
        )

    def handle(self, *args, **options):
        try:
            result = preflight_authoritative_workbook(options["workbook"])
        except PreflightError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(format_preflight_report(result))
