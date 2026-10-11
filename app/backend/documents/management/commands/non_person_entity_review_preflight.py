from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_review_preflight import (
    PreflightError,
    format_preflight_report,
    preflight_authoritative_workbook,
)


class Command(BaseCommand):
    help = (
        "Read-only preflight of a recognized non-person review workbook. "
        "An unknown SHA-256 fails closed. This command performs no database writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workbook",
            required=True,
            help="Path to the authoritative v6 xlsx workbook.",
        )

    def handle(self, *args, **options):
        try:
            result = preflight_authoritative_workbook(options["workbook"])
        except PreflightError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(format_preflight_report(result))
