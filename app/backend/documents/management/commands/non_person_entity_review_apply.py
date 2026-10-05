from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_review_apply import (
    ApplyError,
    apply_non_person_entity_review,
    format_apply_report,
)
from documents.services.non_person_entity_review_dry_run import DryRunError
from documents.services.non_person_entity_review_preflight import PreflightError


class Command(BaseCommand):
    help = (
        "Apply the reviewed v6 non-person entity workbook in one transaction. "
        "Requires --confirm APPLY_NON_PERSON_FINAL_RECON_2026_10_05. "
        "Refuses the batch when any candidate is STATE_DRIFT or BLOCKED. "
        "Does not write Person, Author, Tag, category, event, or search rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workbook",
            required=True,
            help="Path to the authoritative v6 xlsx workbook.",
        )
        parser.add_argument(
            "--confirm",
            required=True,
            help=(
                "Required confirmation token APPLY_NON_PERSON_FINAL_RECON_2026_10_05."
            ),
        )

    def handle(self, *args, **options):
        try:
            result = apply_non_person_entity_review(
                options["workbook"],
                confirm=options["confirm"],
            )
        except (ApplyError, PreflightError, DryRunError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(format_apply_report(result))
