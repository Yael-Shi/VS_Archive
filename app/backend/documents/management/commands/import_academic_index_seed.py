"""Import the approved Academic Index pilot seed.

The default is a dry run and writes no rows, including decision events.
``--apply`` requires ``--confirm`` and a separate ``--owner-authorization``.
The evidence note is the owner-approved Hebrew text in the seed fixture.
A provisional or placeholder note is rejected before any write.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.academic_index_seed_v2 import (
    APPLY_CONFIRM_TOKEN,
    OWNER_AUTHORIZATION_TOKEN,
)
from documents.services.academic_index_seed import (
    AcademicIndexSeedError,
    apply_approved_seed,
    inspect_approved_seed,
)


class Command(BaseCommand):
    help = (
        "Dry-run the approved Academic Index seed by default, with zero writes. "
        f"--apply requires --confirm {APPLY_CONFIRM_TOKEN} and "
        f"--owner-authorization {OWNER_AUTHORIZATION_TOKEN}. "
        "IMPORT events use evidence kind OWNER_APPROVAL and the owner-approved "
        "Hebrew evidence note. Provisional and placeholder notes, including "
        "IMAGE_NOT_COLLATED, are rejected before any write. "
        "These flags do not by themselves authorize a real archive database."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Write the seed in one transaction. Requires --confirm and "
                "--owner-authorization."
            ),
        )
        parser.add_argument(
            "--confirm",
            default="",
            help=f"Required with --apply. Must equal {APPLY_CONFIRM_TOKEN}.",
        )
        parser.add_argument(
            "--owner-authorization",
            default="",
            help=(
                "Separate from --confirm. Required with --apply. Must equal "
                f"{OWNER_AUTHORIZATION_TOKEN}."
            ),
        )

    def handle(self, *args, **options):
        if not options["apply"]:
            try:
                inspection = inspect_approved_seed()
            except AcademicIndexSeedError as exc:
                raise CommandError(str(exc)) from exc
            if inspection.outcome == "aborted":
                raise CommandError(inspection.summary)
            self.stdout.write(inspection.summary)
            return
        try:
            result = apply_approved_seed(
                confirm=options["confirm"],
                owner_authorization=options["owner_authorization"],
            )
        except AcademicIndexSeedError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(result.summary)
