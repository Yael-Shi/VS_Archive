from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.historical_person_tag_reconciliation import (
    HistoricalPersonTagReconciliationError,
    ReconciliationPlan,
    ReconciliationRow,
    apply_historical_person_tag_reconciliation,
    build_historical_person_tag_reconciliation_plan,
)


class Command(BaseCommand):
    help = (
        "Reconcile missing ArchiveItemPerson links for frozen historical "
        "person-name Tags. Default is dry-run (no writes). Pass --apply to "
        "create missing item-level person links only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Create missing ArchiveItemPerson links from the frozen "
                "Tag.id → Person.id map. Default is dry-run."
            ),
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply"])
        try:
            if apply_mode:
                plan = apply_historical_person_tag_reconciliation()
            else:
                plan = build_historical_person_tag_reconciliation_plan()
        except HistoricalPersonTagReconciliationError as exc:
            raise CommandError(str(exc)) from exc

        self._write_report(plan, apply_mode=apply_mode)

    def _write_report(self, plan: ReconciliationPlan, *, apply_mode: bool) -> None:
        self.stdout.write("Historical person-tag reconciliation")
        self.stdout.write(f"mode: {'apply' if apply_mode else 'dry-run'}")
        self.stdout.write(f"planned: {plan.planned_count}")
        self.stdout.write(f"created: {plan.created_count}")
        self.stdout.write(f"already_present: {plan.already_present_count}")
        self._write_rows("planned rows", plan.planned)
        self._write_rows("created rows", plan.created)
        self._write_rows("already-present rows", plan.already_present)

    def _write_rows(self, heading: str, rows: list[ReconciliationRow]) -> None:
        self.stdout.write(f"{heading}:")
        if not rows:
            self.stdout.write("  (none)")
            return
        for row in rows:
            self.stdout.write(f"  {row.as_tuple()}")
