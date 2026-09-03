from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.services.photo_person_reviewed_import import (
    ReviewedPhotoPersonImportError,
    apply_reviewed_photo_person_import,
    plan_reviewed_photo_person_import,
)


class Command(BaseCommand):
    help = (
        "Import a reviewed photo-person-reviewed-import-v1 artifact. "
        "Default is dry-run (no writes). Pass --apply to create Person rows, "
        "import bindings, aliases, and PhotoPerson links in one transaction."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "artifact",
            help="Path to the reviewed JSON artifact.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist planned CREATE/ADD operations. Default is dry-run.",
        )

    def handle(self, *args, **options):
        path = Path(options["artifact"])
        apply_mode = bool(options["apply"])
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise CommandError(f"could not read artifact: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise CommandError(f"artifact is not valid JSON: {exc}") from exc

        try:
            if apply_mode:
                plan = apply_reviewed_photo_person_import(payload)
            else:
                plan = plan_reviewed_photo_person_import(payload)
        except ReviewedPhotoPersonImportError as exc:
            self.stdout.write("Reviewed PhotoPerson import")
            self.stdout.write(f"mode: {'apply' if apply_mode else 'dry-run'}")
            self.stdout.write("CREATE: 0")
            self.stdout.write("ADD: 0")
            self.stdout.write("NOOP: 0")
            self.stdout.write("ERROR: 1")
            op_id = exc.operation_id or "-"
            self.stdout.write(f"  {op_id}\tERROR\t{exc.message}")
            raise CommandError(exc.message) from exc

        self.stdout.write("Reviewed PhotoPerson import")
        self.stdout.write(f"mode: {'apply' if apply_mode else 'dry-run'}")
        self.stdout.write(f"CREATE: {plan.create_count}")
        self.stdout.write(f"ADD: {plan.add_count}")
        self.stdout.write(f"NOOP: {plan.noop_count}")
        self.stdout.write(f"ERROR: {plan.error_count}")
        for row in plan.operations:
            self.stdout.write(
                f"  {row.operation_id}\t{row.op}\t{row.status}\t{row.reason}"
            )
        if plan.search_index_item_ids:
            self.stdout.write(
                f"search_indexes_refreshed: {list(plan.search_index_item_ids)}"
            )
        else:
            self.stdout.write("search_indexes_refreshed: []")
