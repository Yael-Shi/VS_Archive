"""Dry-run, then optionally apply, the approved 103 → 655 entity merge."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_bnei_akiva_shadow_correction import (
    CONFIRM_TOKEN,
    ShadowMergeApplyError,
    apply_bnei_akiva_shadow_merge,
    dry_run_bnei_akiva_shadow_merge,
    format_shadow_merge_report,
)


class Command(BaseCommand):
    help = (
        "Check the approved merge of non-person entity 103 into entity 655. "
        "The default is a read-only dry run. --apply writes only when --confirm "
        f"is exactly {CONFIRM_TOKEN}. Historical decision rows stay in place."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the two correction rows and the short-name alias.",
        )
        parser.add_argument(
            "--confirm",
            default="",
            help="Required with --apply. Must match the fixed confirmation token.",
        )

    def handle(self, *args, **options):
        if not options["apply"]:
            assessment = dry_run_bnei_akiva_shadow_merge()
            self.stdout.write(format_shadow_merge_report(assessment, applied=None))
            if assessment.status == "blocked":
                raise CommandError("shadow merge dry-run is blocked")
            return
        try:
            result = apply_bnei_akiva_shadow_merge(confirm=options["confirm"])
        except ShadowMergeApplyError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            format_shadow_merge_report(result.assessment, applied=result.applied)
        )
