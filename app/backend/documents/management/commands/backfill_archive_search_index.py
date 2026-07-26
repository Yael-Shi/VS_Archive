"""Idempotent backfill and read-only drift verification for ArchiveItemSearchIndex."""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from documents.models import ArchiveItem, ArchiveItemSearchIndex
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
)


DEFAULT_BATCH_SIZE = 100


@dataclass
class BackfillStats:
    scanned: int = 0
    succeeded: int = 0
    failed: int = 0
    failed_ids: list[int] = field(default_factory=list)


@dataclass
class DriftCheckStats:
    scanned: int = 0
    matched: int = 0
    missing_ids: list[int] = field(default_factory=list)
    content_mismatch_ids: list[int] = field(default_factory=list)
    null_vector_ids: list[int] = field(default_factory=list)
    extra_ids: list[int] = field(default_factory=list)

    @property
    def drift_count(self) -> int:
        return (
            len(self.missing_ids)
            + len(self.content_mismatch_ids)
            + len(self.null_vector_ids)
            + len(self.extra_ids)
        )


class Command(BaseCommand):
    help = (
        "Rebuild ArchiveItemSearchIndex rows from ArchiveItem source of truth, "
        "or verify drift with --check-only. Write mode updates only the "
        "search-index table. Idempotent; safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--archive-item-id",
            type=int,
            default=None,
            help="Rebuild or verify a single ArchiveItem by id.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=DEFAULT_BATCH_SIZE,
            help=f"Progress batch size for full runs (default {DEFAULT_BATCH_SIZE}).",
        )
        parser.add_argument(
            "--check-only",
            action="store_true",
            help=(
                "Read-only drift verification: compare stored index rows to "
                "builder output. Performs no writes. Exits non-zero on drift."
            ),
        )

    def handle(self, *args, **options):
        archive_item_id = options.get("archive_item_id")
        batch_size = options["batch_size"]
        check_only = options["check_only"]
        if batch_size < 1:
            raise CommandError("--batch-size must be >= 1")

        if check_only:
            self._check(archive_item_id=archive_item_id, batch_size=batch_size)
            return

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

    def _check(self, *, archive_item_id: int | None, batch_size: int) -> None:
        stats = DriftCheckStats()
        if archive_item_id is not None:
            if not ArchiveItem.objects.filter(pk=archive_item_id).exists():
                raise CommandError(f"ArchiveItem id={archive_item_id} does not exist")
            item_ids = [archive_item_id]
            index_ids = list(
                ArchiveItemSearchIndex.objects.filter(
                    archive_item_id=archive_item_id
                ).values_list("archive_item_id", flat=True)
            )
        else:
            item_ids = list(
                ArchiveItem.objects.order_by("pk").values_list("pk", flat=True)
            )
            index_ids = list(
                ArchiveItemSearchIndex.objects.order_by("archive_item_id").values_list(
                    "archive_item_id", flat=True
                )
            )

        item_id_set = set(item_ids)
        index_id_set = set(index_ids)
        stats.extra_ids = sorted(index_id_set - item_id_set)

        self.stdout.write(
            f"Checking ArchiveItemSearchIndex drift for {len(item_ids)} "
            f"ArchiveItem row(s)"
        )

        for offset in range(0, len(item_ids), batch_size):
            chunk_ids = item_ids[offset : offset + batch_size]
            self._check_batch(chunk_ids, stats)
            self.stdout.write(
                f"  progress scanned={stats.scanned}/{len(item_ids)} "
                f"matched={stats.matched} drift={stats.drift_count}"
            )

        self.stdout.write(
            f"Done. scanned={stats.scanned} matched={stats.matched} "
            f"missing={len(stats.missing_ids)} "
            f"content_mismatch={len(stats.content_mismatch_ids)} "
            f"null_vector={len(stats.null_vector_ids)} "
            f"extra={len(stats.extra_ids)}"
        )
        self._write_id_list("missing archive_item_id(s)", stats.missing_ids)
        self._write_id_list(
            "content_mismatch archive_item_id(s)", stats.content_mismatch_ids
        )
        self._write_id_list("null_vector archive_item_id(s)", stats.null_vector_ids)
        self._write_id_list("extra archive_item_id(s)", stats.extra_ids)

        if stats.drift_count:
            raise CommandError(
                f"Drift verification failed with {stats.drift_count} issue(s)"
            )

        self.stdout.write(self.style.SUCCESS("Drift verification passed"))

    def _check_batch(self, chunk_ids: list[int], stats: DriftCheckStats) -> None:
        items = {
            item.pk: item
            for item in archive_items_for_search_index_build(archive_item_ids=chunk_ids)
        }
        indexes = {
            index.archive_item_id: index
            for index in ArchiveItemSearchIndex.objects.filter(
                archive_item_id__in=chunk_ids
            )
        }
        for archive_item_id in chunk_ids:
            stats.scanned += 1
            item = items.get(archive_item_id)
            if item is None:
                # Deleted between id listing and load; treat as missing source.
                stats.missing_ids.append(archive_item_id)
                continue
            expected = build_archive_item_search_content(item)
            index = indexes.get(archive_item_id)
            if index is None:
                stats.missing_ids.append(archive_item_id)
                continue
            if (
                index.title_text != expected.title_text
                or index.metadata_text != expected.metadata_text
                or index.body_text != expected.body_text
            ):
                stats.content_mismatch_ids.append(archive_item_id)
                continue
            if index.search_vector is None:
                stats.null_vector_ids.append(archive_item_id)
                continue
            stats.matched += 1

    def _write_id_list(self, label: str, ids: list[int]) -> None:
        if not ids:
            return
        preview = ", ".join(str(i) for i in ids[:20])
        more = ""
        if len(ids) > 20:
            more = f" (+{len(ids) - 20} more)"
        self.stderr.write(self.style.ERROR(f"{label}: {preview}{more}"))
