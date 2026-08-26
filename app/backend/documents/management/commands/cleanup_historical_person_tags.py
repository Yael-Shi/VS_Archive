from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.historical_person_tag_cleanup import (
    ArchiveItemTagCleanupRow,
    DocumentTagCleanupRow,
    HistoricalPersonTagCleanupError,
    HistoricalPersonTagCleanupPlan,
    apply_historical_person_tag_relation_cleanup,
    build_historical_person_tag_cleanup_plan,
)


class Command(BaseCommand):
    help = (
        "D1: delete mapped historical person-name Tag relations only. "
        "Default is dry-run (no writes). Pass --apply-relations to delete "
        "mapped ArchiveItem.tags and Document.tags_m2m through rows and "
        "rebuild search indexes for affected ArchiveItems. Tag rows are "
        "never deleted. After D2b, all 29 mapped Tag rows may be absent; "
        "that is a no-op success. Partial mapped Tag presence fails closed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply-relations",
            action="store_true",
            help=(
                "Delete mapped ArchiveItem.tags and Document.tags_m2m through "
                "rows, then sync search indexes for affected ArchiveItems. "
                "Does not delete Tag rows. Default is dry-run."
            ),
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply_relations"])
        try:
            if apply_mode:
                plan = apply_historical_person_tag_relation_cleanup()
            else:
                plan = build_historical_person_tag_cleanup_plan()
        except HistoricalPersonTagCleanupError as exc:
            raise CommandError(str(exc)) from exc

        self._write_report(plan, apply_mode=apply_mode)

    def _write_report(
        self, plan: HistoricalPersonTagCleanupPlan, *, apply_mode: bool
    ) -> None:
        self.stdout.write("Historical person-tag cleanup D1")
        self.stdout.write(f"mode: {'apply-relations' if apply_mode else 'dry-run'}")
        self.stdout.write(f"planned: {plan.planned_count}")
        self.stdout.write(
            f"planned_archiveitem_tag_relations: {plan.planned_archive_item_tag_count}"
        )
        self.stdout.write(
            f"planned_document_tag_relations: {plan.planned_document_tag_count}"
        )
        self.stdout.write(f"deleted: {plan.deleted_count}")
        self.stdout.write(
            f"deleted_archiveitem_tag_relations: {plan.deleted_archive_item_tag_count}"
        )
        self.stdout.write(
            f"deleted_document_tag_relations: {plan.deleted_document_tag_count}"
        )
        self.stdout.write(
            f"affected_archive_item_ids: {plan.affected_archive_item_ids}"
        )
        self.stdout.write("mapped_tag_rows_kept: yes")
        self._write_item_rows(
            "planned ArchiveItem.tags through rows",
            plan.archive_item_tag_rows,
        )
        self._write_document_rows(
            "planned Document.tags_m2m through rows",
            plan.document_tag_rows,
        )
        self._write_item_rows(
            "deleted ArchiveItem.tags through rows",
            plan.deleted_archive_item_tag_rows,
        )
        self._write_document_rows(
            "deleted Document.tags_m2m through rows",
            plan.deleted_document_tag_rows,
        )

    def _write_item_rows(
        self, heading: str, rows: list[ArchiveItemTagCleanupRow]
    ) -> None:
        self.stdout.write(f"{heading}:")
        if not rows:
            self.stdout.write("  (none)")
            return
        for row in rows:
            self.stdout.write(f"  {row.as_tuple()}")

    def _write_document_rows(
        self, heading: str, rows: list[DocumentTagCleanupRow]
    ) -> None:
        self.stdout.write(f"{heading}:")
        if not rows:
            self.stdout.write("  (none)")
            return
        for row in rows:
            self.stdout.write(f"  {row.as_tuple()}")
