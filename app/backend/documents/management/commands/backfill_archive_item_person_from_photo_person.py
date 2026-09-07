from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.photo_person_archive_item_person_backfill import (
    PhotoPersonArchiveItemPersonBackfillError,
    apply_photo_person_archive_item_person_backfill,
    build_photo_person_archive_item_person_backfill_plan,
)


class Command(BaseCommand):
    help = (
        "Ensure ArchiveItemPerson exists for every PhotoPerson. "
        "Default is dry-run (no writes). Pass --apply to create missing "
        "item-level person links only. Does not write PhotoPerson, Person, "
        "Author, aliases, or people_present. Safe to run repeatedly."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Create missing ArchiveItemPerson rows implied by PhotoPerson. "
                "Default is dry-run."
            ),
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply"])
        try:
            if apply_mode:
                plan = apply_photo_person_archive_item_person_backfill()
            else:
                plan = build_photo_person_archive_item_person_backfill_plan()
        except PhotoPersonArchiveItemPersonBackfillError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("PhotoPerson → ArchiveItemPerson backfill")
        self.stdout.write(f"mode: {'apply' if apply_mode else 'dry-run'}")
        self.stdout.write(f"CREATE: {plan.create_count}")
        self.stdout.write(f"NOOP: {plan.noop_count}")
        self.stdout.write(f"ERROR: {plan.error_count}")
        for row in plan.rows:
            self.stdout.write(
                "  "
                f"photo_person={row.photo_person_id}\t"
                f"photo={row.photo_content_id}\t"
                f"item={row.archive_item_id}\t"
                f"person={row.person_id}\t"
                f"{row.status}\t{row.reason}"
            )
        if plan.error_count:
            raise CommandError(
                "photo-person AIP backfill found integrity errors; refusing to continue"
            )
        if plan.applied:
            self.stdout.write(
                f"search_indexes_refreshed: {list(plan.created_archive_item_ids)}"
            )
        else:
            self.stdout.write("search_indexes_refreshed: []")
