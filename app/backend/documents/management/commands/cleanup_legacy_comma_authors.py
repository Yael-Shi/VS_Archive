from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.legacy_comma_author_cleanup import (
    STATUS_ALREADY_COMPLETE,
    STATUS_APPLIED,
    STATUS_DRY_RUN,
    LegacyCommaAuthorCleanupError,
    cleanup_legacy_comma_authors,
)


class Command(BaseCommand):
    help = (
        "One-time reviewed cleanup of four legacy comma-containing Author rows "
        "(ArchiveItem 311 split to Authors 29,68; delete unlinked aggregates "
        "4, 6, 61, 69). Not a general comma-splitting policy. Default is "
        "dry-run (no writes). Pass --apply to mutate inside one transaction."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Apply the reviewed mapping. Default is dry-run with zero writes."
            ),
        )

    def handle(self, *args, **options):
        apply_mode = bool(options["apply"])
        try:
            result = cleanup_legacy_comma_authors(apply=apply_mode)
        except LegacyCommaAuthorCleanupError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Legacy comma-author cleanup")
        self.stdout.write(f"mode: {result.status}")
        if result.status == STATUS_DRY_RUN:
            self.stdout.write("writes: none")
        elif result.status == STATUS_ALREADY_COMPLETE:
            self.stdout.write("already complete: yes")
            self.stdout.write("writes: none")
        elif result.status == STATUS_APPLIED:
            self.stdout.write("writes: applied")
        for line in result.verifications:
            self.stdout.write(f"verify: {line}")
        self.stdout.write(
            "planned_item_311_author_ids: "
            f"{list(result.planned_item_311_author_ids)}"
        )
        self.stdout.write(f"planned_author_name: {result.planned_author_name!r}")
        self.stdout.write(
            "authors_planned_unlinked_and_deleted: "
            f"{list(result.authors_planned_unlinked_and_deleted)}"
        )
        self.stdout.write(f"deleted_author_ids: {list(result.deleted_author_ids)}")
        self.stdout.write(
            f"search_indexes_refreshed: {result.search_indexes_refreshed}"
        )
