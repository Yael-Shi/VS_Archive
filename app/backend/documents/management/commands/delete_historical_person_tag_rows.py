from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.historical_person_tag_row_deletion import (
    HistoricalPersonTagRow,
    HistoricalPersonTagRowDeletionError,
    HistoricalPersonTagRowDeletionPlan,
    apply_historical_person_tag_row_deletion,
    build_historical_person_tag_row_deletion_plan,
)


class Command(BaseCommand):
    help = (
        "D2b: delete the 29 frozen historical person-name Tag rows only. "
        "Default is dry-run (no writes). Pass --apply-rows to delete the "
        "exact frozen (Tag.id, Tag.name) rows inside one transaction. "
        "Refuses when mapped through relations exist or when PENDING "
        "metadata suggestions contain retired Tag names. Does not delete "
        "Person, ArchiveItemPerson, PhotoPerson, or ordinary Tags, and "
        "does not rebuild search indexes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply-rows",
            action="store_true",
            help=(
                "Delete the 29 frozen historical person-name Tag rows. "
                "Default is dry-run."
            ),
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply_rows"])
        try:
            if apply_mode:
                plan = apply_historical_person_tag_row_deletion()
            else:
                plan = build_historical_person_tag_row_deletion_plan()
        except HistoricalPersonTagRowDeletionError as exc:
            raise CommandError(str(exc)) from exc

        self._write_report(plan, apply_mode=apply_mode)

    def _write_report(
        self, plan: HistoricalPersonTagRowDeletionPlan, *, apply_mode: bool
    ) -> None:
        self.stdout.write("Historical person-tag row deletion D2b")
        self.stdout.write(f"mode: {'apply-rows' if apply_mode else 'dry-run'}")
        self.stdout.write(f"state: {plan.state}")
        self.stdout.write(f"planned: {plan.planned_count}")
        self.stdout.write(f"deleted: {plan.deleted_count}")
        self.stdout.write(f"django_delete_total: {plan.django_delete_total}")
        self.stdout.write(f"django_delete_per_model: {plan.django_delete_per_model}")
        self.stdout.write(
            f"remaining_mapped_tag_rows: {plan.remaining_mapped_tag_rows}"
        )
        self.stdout.write(
            f"mapped_archiveitem_tag_relations: {plan.mapped_archiveitem_tag_relations}"
        )
        self.stdout.write(
            f"mapped_document_tag_relations: {plan.mapped_document_tag_relations}"
        )
        self.stdout.write(
            f"pending_retired_name_suggestions: {plan.pending_retired_name_suggestions}"
        )
        self._write_rows("planned Tag rows", plan.planned_tag_rows)
        self._write_rows("deleted Tag rows", plan.deleted_tag_rows)

    def _write_rows(self, heading: str, rows: list[HistoricalPersonTagRow]) -> None:
        self.stdout.write(f"{heading}:")
        if not rows:
            self.stdout.write("  (none)")
            return
        for row in rows:
            self.stdout.write(f"  {row.as_tuple()}")
