"""Idempotent backfill of ``ArchiveItemSearchIndex`` from source of truth (PR1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from documents.models import ArchiveItem
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)


DEFAULT_BATCH_SIZE = 100


@dataclass
class BackfillStats:
    scanned: int = 0
    succeeded: int = 0
    failed: int = 0
    failed_ids: list[int] = field(default_factory=list)


class Command(BaseCommand):
    help = (
        "Rebuild ArchiveItemSearchIndex rows from ArchiveItem source of truth. "
        "Writes only the search-index table. Idempotent; safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--archive-item-id",
            type=int,
            default=None,
            help="Rebuild a single ArchiveItem by id.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Progress batch size for full runs (default {DEFAULT_BATCH_SIZE}).",
        )

    def handle(self, *args, **options):
        archive_item_id = options.get("archive_item_id")
        batch_size = options["batch_size"]
        if batch_size < 1:
            raise CommandError("--batch-size must be >= 1")

        if archive_item_id is not None:
            self._rebuild_one(archive_item_id)
            return

        self._rebuild_all(batch_size=batch_size)

    def _rebuild_one(self, archive_item_id: int) -> None:
        qs = archive_items_for_search_index_build(archive_item_ids=[archive_item_id])
        item = qs.first()
        if item is None:
            raise CommandError(f"ArchiveItem id={archive_item_id} does not exist")
        try:
            rebuild_archive_item_search_index(item)
        except DatabaseError as exc:
            raise CommandError(
                f"Failed rebuilding ArchiveItemSearchIndex "
                f"archive_item_id={archive_item_id} item_type={item.item_type}: "
                f"{exc.__class__.__name__}"
            ) from None
        self.stdout.write(
            self.style.SUCCESS(
                f"Rebuilt ArchiveItemSearchIndex archive_item_id={archive_item_id}"
            )
        )

    def _rebuild_all(self, *, batch_size: int) -> None:
        stats = BackfillStats()
        all_ids = list(ArchiveItem.objects.order_by("pk").values_list("pk", flat=True))
        total = len(all_ids)
        self.stdout.write(
            f"Backfilling ArchiveItemSearchIndex for {total} ArchiveItem row(s)"
        )

        for offset in range(0, total, batch_size):
            chunk_ids = all_ids[offset : offset + batch_size]
            items = list(
                archive_items_for_search_index_build(archive_item_ids=chunk_ids)
            )
            self._process_batch(items, stats)
            self._write_progress(stats, total)

        self.stdout.write(
            f"Done. scanned={stats.scanned} succeeded={stats.succeeded} "
            f"failed={stats.failed}"
        )
        if stats.failed_ids:
            preview = ", ".join(str(i) for i in stats.failed_ids[:20])
            more = ""
            if len(stats.failed_ids) > 20:
                more = f" (+{len(stats.failed_ids) - 20} more)"
            self.stderr.write(
                self.style.ERROR(f"Failed archive_item_id(s): {preview}{more}")
            )
            raise CommandError(
                f"Backfill completed with {stats.failed} failure(s); "
                "re-run after fixing source data or retry failed ids"
            )

        self.stdout.write(self.style.SUCCESS("Backfill completed successfully"))

    def _process_batch(
        self,
        batch: list[ArchiveItem],
        stats: BackfillStats,
    ) -> None:
        for item in batch:
            stats.scanned += 1
            try:
                with transaction.atomic():
                    rebuild_archive_item_search_index(item)
            except DatabaseError as exc:
                stats.failed += 1
                stats.failed_ids.append(item.pk)
                self.stderr.write(
                    self.style.ERROR(
                        f"archive_item_id={item.pk} item_type={item.item_type} "
                        f"error={exc.__class__.__name__}"
                    )
                )
            else:
                stats.succeeded += 1

    def _write_progress(self, stats: BackfillStats, total: int) -> None:
        self.stdout.write(
            f"  progress scanned={stats.scanned}/{total} "
            f"succeeded={stats.succeeded} failed={stats.failed}"
        )
