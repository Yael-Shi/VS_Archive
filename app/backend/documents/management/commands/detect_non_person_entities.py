"""Explicit item-scoped detector for non-person occurrence proposals."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch

from documents.models import ArchiveItem, Document
from documents.services.non_person_entity_detector import (
    detect_non_person_entities_for_items,
    format_detection_report,
)
from documents.services.text_presentation import text_presentation_results_prefetch


class Command(BaseCommand):
    help = (
        "Detect non-person registry names in explicit archive items and "
        "report occurrence proposals. Default is dry-run and writes nothing. "
        "Pass --apply to insert proposal, candidate, and match rows. "
        "At least one --item is required. This command does not scan the corpus, "
        "approve candidates, or create entities, aliases, or occurrences."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--item",
            action="append",
            type=int,
            dest="item_ids",
            help=(
                "Archive item id to scan. Repeat for more than one item. "
                "Required. There is no whole-corpus mode."
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
        item_ids = _explicit_item_ids(options.get("item_ids"))
        items = _load_items(item_ids)
        report = detect_non_person_entities_for_items(
            items,
            apply=bool(options["apply"]),
        )
        self.stdout.write(format_detection_report(report))


def _explicit_item_ids(raw_ids: list[int] | None) -> list[int]:
    if not raw_ids:
        raise CommandError(
            "Pass at least one --item ID. This command does not scan the corpus."
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
        for item in ArchiveItem.objects.filter(pk__in=item_ids)
        .select_related("manual_text_content")
        .prefetch_related(
            Prefetch(
                "ocr_document",
                queryset=Document.objects.prefetch_related(
                    text_presentation_results_prefetch()
                ),
            )
        )
    }
    missing = [str(item_id) for item_id in item_ids if item_id not in found]
    if missing:
        raise CommandError("Unknown archive item id: " + ", ".join(missing))
    return [found[item_id] for item_id in item_ids]
