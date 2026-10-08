"""Reject pending contained non-person hits left from before overlap suppression.

Repeated ``--item`` is required. There is no ``--all``. Dry-run is the
default and writes nothing. ``--apply`` requires ``--actor-id`` and rejects
eligible candidates through ``reject_candidate``. Proposal, candidate,
match, and event rows are not deleted. The detector is not changed.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.models import ArchiveItem
from documents.services.document_access import is_document_admin
from documents.services.non_person_entity_overlap_cleanup import (
    cleanup_non_person_overlap_candidates,
    format_overlap_cleanup_report,
)


class Command(BaseCommand):
    help = (
        "Report pending non-person occurrence candidates that current "
        "overlap suppression would not emit. Default is dry-run and writes "
        "nothing. Pass one or more --item ids. There is no --all. Pass "
        "--apply and --actor-id for a staff or superuser to reject each "
        "still-eligible candidate through reject_candidate. This command "
        "does not delete rows and does not change the detector."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--item",
            action="append",
            type=int,
            dest="item_ids",
            required=True,
            help=(
                "Archive item id to examine. Repeat for more than one item. "
                "This command does not scan the corpus."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Reject eligible candidates through reject_candidate. "
                "Requires --actor-id for a staff or superuser. "
                "Omit this flag for a dry run."
            ),
        )
        parser.add_argument(
            "--actor-id",
            type=int,
            dest="actor_id",
            help=(
                "Staff or superuser id stored on the REJECT event. "
                "Required with --apply. Uses the archive-management gate."
            ),
        )

    def handle(self, *args, **options):
        item_ids = _explicit_item_ids(options.get("item_ids"))
        _require_known_items(item_ids)
        actor = _actor(options["actor_id"], apply=bool(options["apply"]))
        report = cleanup_non_person_overlap_candidates(
            item_ids,
            apply=bool(options["apply"]),
            actor=actor,
        )
        self.stdout.write(format_overlap_cleanup_report(report))
        if report.errors:
            raise CommandError(
                f"Overlap cleanup finished with errors: {report.errors}."
            )


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


def _require_known_items(item_ids: list[int]) -> None:
    found = set(
        ArchiveItem.objects.filter(pk__in=item_ids).values_list("pk", flat=True)
    )
    missing = [str(item_id) for item_id in item_ids if item_id not in found]
    if missing:
        raise CommandError("Unknown archive item id: " + ", ".join(missing))


def _actor(actor_id: int | None, *, apply: bool):
    if not apply:
        return None
    if actor_id is None:
        raise CommandError("--apply requires --actor-id.")
    if actor_id < 1:
        raise CommandError(f"Invalid --actor-id: {actor_id}")
    user_model = get_user_model()
    try:
        actor = user_model.objects.get(pk=actor_id)
    except user_model.DoesNotExist as exc:
        raise CommandError(f"Unknown --actor-id: {actor_id}") from exc
    if not is_document_admin(actor):
        raise CommandError(
            f"--actor-id {actor_id} is not authorized for archive management."
        )
    return actor
