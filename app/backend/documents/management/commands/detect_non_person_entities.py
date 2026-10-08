"""Explicit detector for non-person occurrence proposals.

``--item`` scans named archive items. Those ids share one transaction when
``--apply`` is set. ``--all`` scans the supported text corpus in primary-key
order, one committed item at a time. Dry-run is the default and writes
nothing. Neither mode approves candidates or creates entities, aliases, or
occurrences.

In corpus output, ``last_completed_item_id`` is the safe resume cursor: the
highest id in the uninterrupted successful prefix. It freezes at the first
item failure, so ``--start-after`` that value includes the failed item again.
"""

from __future__ import annotations

from collections.abc import Iterator

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch, Q, QuerySet

from documents.models import ArchiveItem, Document
from documents.services.non_person_entity_detector import (
    NonPersonEntityCorpusRun,
    detect_non_person_entities_for_items,
    format_detection_report,
)
from documents.services.text_presentation import text_presentation_results_prefetch

CORPUS_CHUNK_SIZE = 50
_CORPUS_FLAGS = (
    ("text_kind", "--text-kind"),
    ("min_id", "--min-id"),
    ("max_id", "--max-id"),
    ("start_after", "--start-after"),
    ("limit", "--limit"),
)


class Command(BaseCommand):
    help = (
        "Detect non-person registry names and report occurrence proposals. "
        "Default is dry-run and writes nothing. Pass --apply to insert "
        "proposal, candidate, and match rows. Pass one or more --item ids, "
        "or pass --all to scan supported manual and OCR items. "
        "--all and --item cannot be combined. This command does not approve "
        "candidates or create entities, aliases, or occurrences."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--item",
            action="append",
            type=int,
            dest="item_ids",
            help=(
                "Archive item id to scan. Repeat for more than one item. "
                "Required unless --all is set. Not valid with --all."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help=(
                "Scan supported MANUAL_TEXT and OCR_DOCUMENT items in "
                "primary-key order. Dry-run unless --apply is also set."
            ),
        )
        parser.add_argument(
            "--text-kind",
            choices=("MANUAL_TEXT", "OCR_TRANSCRIPTION"),
            help="With --all, scan only this text kind.",
        )
        parser.add_argument(
            "--min-id",
            type=int,
            help="With --all, inclusive lower archive item id.",
        )
        parser.add_argument(
            "--max-id",
            type=int,
            help="With --all, inclusive upper archive item id.",
        )
        parser.add_argument(
            "--start-after",
            type=int,
            help=(
                "With --all, exclusive lower archive item id. "
                "Not valid with --min-id. Use the printed "
                "last_completed_item_id to resume; that cursor stops before "
                "the first failed item."
            ),
        )
        parser.add_argument(
            "--limit",
            type=int,
            help=(
                "With --all, maximum selected items to examine after id "
                "bounds, in primary-key order."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Insert missing proposal, candidate, and match rows. "
                "Omit this flag for a dry run."
            ),
        )

    def handle(self, *args, **options):
        _validate_scope(options)
        if options["all"]:
            self._handle_corpus(options)
            return
        item_ids = _explicit_item_ids(options.get("item_ids"))
        items = _load_items(item_ids)
        report = detect_non_person_entities_for_items(
            items,
            apply=bool(options["apply"]),
        )
        self.stdout.write(format_detection_report(report))

    def _handle_corpus(self, options) -> None:
        apply = bool(options["apply"])
        min_id = options["min_id"]
        max_id = options["max_id"]
        start_after = options["start_after"]
        limit = options["limit"]
        text_kind = options["text_kind"]
        excluded = _excluded_counts(
            min_id=min_id,
            max_id=max_id,
            start_after=start_after,
        )
        # Printed before registry load and item detection.
        self.stdout.write(
            _corpus_preamble(
                apply=apply,
                text_kind=text_kind,
                min_id=min_id,
                max_id=max_id,
                start_after=start_after,
                limit=limit,
                excluded=excluded,
            )
        )
        selected = _corpus_queryset(
            text_kind=text_kind,
            min_id=min_id,
            max_id=max_id,
            start_after=start_after,
        )
        run = NonPersonEntityCorpusRun(
            apply=apply,
            on_item_failure=self._write_corpus_failure,
        )
        try:
            for chunk in _iter_corpus_chunks(selected, limit=limit):
                for item in chunk:
                    run.process_item(item)
                self.stdout.write(run.progress_line() + "\n")
        except (KeyboardInterrupt, SystemExit):
            self.stdout.write(_corpus_result_text(run))
            raise
        self.stdout.write(_corpus_result_text(run))
        if run.failure_count:
            raise CommandError("Corpus detection finished with item failures.")

    def _write_corpus_failure(self, line: str) -> None:
        """Print one ordinary item failure as soon as that item fails."""

        self.stdout.write(line + "\n")


def _validate_scope(options) -> None:
    item_ids = options.get("item_ids")
    corpus = bool(options["all"])
    if corpus and item_ids:
        raise CommandError("--all and --item cannot be used together.")
    if not corpus:
        used = [flag for key, flag in _CORPUS_FLAGS if options.get(key) is not None]
        if used:
            raise CommandError("Corpus options require --all: " + ", ".join(used) + ".")
        return
    if options["start_after"] is not None and options["min_id"] is not None:
        raise CommandError("--start-after and --min-id cannot be used together.")
    for key, flag in _CORPUS_FLAGS:
        if key == "text_kind":
            continue
        value = options.get(key)
        if value is not None and value < 1:
            raise CommandError(f"Invalid {flag}: {value}")
    min_id = options["min_id"]
    max_id = options["max_id"]
    if min_id is not None and max_id is not None and min_id > max_id:
        raise CommandError(
            f"Invalid id range: --min-id {min_id} is greater than --max-id {max_id}."
        )


def _explicit_item_ids(raw_ids: list[int] | None) -> list[int]:
    if not raw_ids:
        raise CommandError(
            "Pass at least one --item ID, or pass --all. "
            "This command does not scan the corpus unless --all is set."
        )
    item_ids: list[int] = []
    seen: set[int] = set()
    for item_id in raw_ids:
        if item_id < 1:
            raise CommandError(f"Invalid archive item id: {item_id}")
        if item_id in seen:
            continue
        seen.add(item_id)
        item_ids.append(item_id)
    return item_ids


def _load_items(item_ids: list[int]) -> list[ArchiveItem]:
    found = {
        item.pk: item
        for item in _detection_queryset(ArchiveItem.objects.filter(pk__in=item_ids))
    }
    missing = [str(item_id) for item_id in item_ids if item_id not in found]
    if missing:
        raise CommandError("Unknown archive item id: " + ", ".join(missing))
    return [found[item_id] for item_id in item_ids]


def _corpus_queryset(
    *,
    text_kind: str | None,
    min_id: int | None,
    max_id: int | None,
    start_after: int | None,
) -> QuerySet[ArchiveItem]:
    clauses: list[Q] = []
    if text_kind in (None, "MANUAL_TEXT"):
        clauses.append(
            Q(
                item_type=ArchiveItem.ItemType.MANUAL_TEXT,
                manual_text_content__isnull=False,
            )
        )
    if text_kind in (None, "OCR_TRANSCRIPTION"):
        clauses.append(
            Q(
                item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
                ocr_document__isnull=False,
            )
        )
    selected = clauses[0]
    for clause in clauses[1:]:
        selected |= clause
    queryset = ArchiveItem.objects.filter(selected).distinct()
    return _apply_id_bounds(
        queryset,
        min_id=min_id,
        max_id=max_id,
        start_after=start_after,
    )


def _excluded_counts(
    *,
    min_id: int | None,
    max_id: int | None,
    start_after: int | None,
) -> dict[str, int]:
    """Id-bounded counts of rows corpus selection does not examine.

    ``--limit`` and ``--text-kind`` do not change these counts.
    """

    bounded = _apply_id_bounds(
        ArchiveItem.objects.all(),
        min_id=min_id,
        max_id=max_id,
        start_after=start_after,
    )
    return {
        "excluded_photo": bounded.filter(item_type=ArchiveItem.ItemType.PHOTO).count(),
        "excluded_video": bounded.filter(item_type=ArchiveItem.ItemType.VIDEO).count(),
        "excluded_manual_without_body": bounded.filter(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            manual_text_content__isnull=True,
        ).count(),
        "excluded_ocr_without_document": bounded.filter(
            item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
            ocr_document__isnull=True,
        ).count(),
    }


def _apply_id_bounds(
    queryset: QuerySet[ArchiveItem],
    *,
    min_id: int | None,
    max_id: int | None,
    start_after: int | None,
) -> QuerySet[ArchiveItem]:
    if start_after is not None:
        queryset = queryset.filter(pk__gt=start_after)
    if min_id is not None:
        queryset = queryset.filter(pk__gte=min_id)
    if max_id is not None:
        queryset = queryset.filter(pk__lte=max_id)
    return queryset


def _iter_corpus_chunks(
    queryset: QuerySet[ArchiveItem],
    *,
    limit: int | None,
) -> Iterator[list[ArchiveItem]]:
    """Keyset pages of selected items. Does not evaluate the whole corpus."""

    remaining = limit
    after_pk: int | None = None
    while True:
        chunk_size = CORPUS_CHUNK_SIZE
        size = chunk_size if remaining is None else min(chunk_size, remaining)
        if size <= 0:
            return
        page = queryset if after_pk is None else queryset.filter(pk__gt=after_pk)
        items = list(_detection_queryset(page).order_by("pk")[:size])
        if not items:
            return
        yield items
        after_pk = items[-1].pk
        if remaining is not None:
            remaining -= len(items)


def _detection_queryset(queryset: QuerySet[ArchiveItem]) -> QuerySet[ArchiveItem]:
    """Same item prefetch the explicit ``--item`` path uses for displayed text."""

    return queryset.select_related("manual_text_content").prefetch_related(
        Prefetch(
            "ocr_document",
            queryset=Document.objects.prefetch_related(
                text_presentation_results_prefetch()
            ),
        )
    )


def _corpus_preamble(
    *,
    apply: bool,
    text_kind: str | None,
    min_id: int | None,
    max_id: int | None,
    start_after: int | None,
    limit: int | None,
    excluded: dict[str, int],
) -> str:
    unbounded = max_id is None and limit is None
    lines = [
        f"mode: {'apply' if apply else 'dry-run'}",
        "selection: corpus",
        f"scope: {'unbounded' if unbounded else 'bounded'}",
        f"text_kind: {text_kind or 'all'}",
        "order: archive_item_id",
        f"min_id: {_dash(min_id)}",
        f"max_id: {_dash(max_id)}",
        f"start_after: {_dash(start_after)}",
        f"limit: {_dash(limit)}",
        f"excluded_photo: {excluded['excluded_photo']}",
        f"excluded_video: {excluded['excluded_video']}",
        f"excluded_manual_without_body: {excluded['excluded_manual_without_body']}",
        (f"excluded_ocr_without_document: {excluded['excluded_ocr_without_document']}"),
    ]
    return "\n".join(lines) + "\n"


def _corpus_result_text(run: NonPersonEntityCorpusRun) -> str:
    report = run.build_report()
    # last_completed_item_id is last_contiguous_completed_item_id: the
    # successful prefix frozen at the first failure, not the latest success.
    cursor = _dash(report.last_contiguous_completed_item_id)
    lines = [
        f"items_examined: {report.items_examined}",
        f"manual_sources_scanned: {report.manual_sources_scanned}",
        f"ocr_sources_scanned: {report.ocr_sources_scanned}",
        (
            "items_missing_authoritative_text: "
            f"{report.items_missing_authoritative_text}"
        ),
        f"last_completed_item_id: {cursor}",
        f"failures_truncated: {1 if report.failures_truncated else 0}",
    ]
    failures = "".join(f"{line}\n" for line in report.errors)
    return format_detection_report(report) + "\n".join(lines) + "\n" + failures


def _dash(value: int | None) -> str:
    if value is None:
        return "-"
    return str(value)
