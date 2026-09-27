from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.services.v18_people_registry_preflight import (
    PreflightError,
    database_preflight,
    load_candidate,
    sha256_path,
    static_snapshot,
)


class Command(BaseCommand):
    help = (
        "Read-only V18 people-registry preflight. "
        "Validates the candidate and optionally compares it with the current DB. "
        "This command performs no database writes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--candidate",
            required=True,
        )
        parser.add_argument(
            "--expected-sha256",
            required=True,
        )
        parser.add_argument("--output")
        parser.add_argument(
            "--static-only",
            action="store_true",
            help=("Validate candidate/plan without querying the database."),
        )

    def handle(self, *args, **options):
        candidate_path = Path(options["candidate"])
        expected_sha = options["expected_sha256"].lower()

        try:
            data = load_candidate(
                candidate_path,
                expected_sha,
            )

            result = {
                "candidate_path": str(candidate_path),
                "candidate_sha256": sha256_path(candidate_path),
                "static": static_snapshot(data),
            }

            if not options["static_only"]:
                result["database"] = database_preflight(data)

        except PreflightError as exc:
            raise CommandError(str(exc)) from exc

        payload = (
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )

        if options["output"]:
            output = Path(options["output"])
            output.write_text(
                payload,
                encoding="utf-8",
            )
            self.stdout.write(str(output))

        else:
            self.stdout.write(payload)

        database = result.get("database")

        if database and database["status"] != "LIVE_PREFLIGHT_PASS":
            raise CommandError(
                "V18 live preflight blocked: "
                f"{database['summary']['blockers']} "
                "blocker(s)"
            )
