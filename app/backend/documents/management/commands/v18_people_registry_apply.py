from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.services.v18_people_registry_apply import (
    APPLY_CONFIRM_TOKEN,
    ApplyError,
    ApplySearchRefreshError,
    load_approved_live_preflight,
    run_v18_people_registry_apply,
)
from documents.services.v18_people_registry_preflight import (
    PreflightError,
    load_candidate,
    sha256_path,
)


class Command(BaseCommand):
    help = (
        "Apply the reviewed V18 people-registry candidate using an approved "
        "LIVE_PREFLIGHT_PASS artifact. Supports --dry-run (zero writes) or "
        "--apply with an explicit confirmation token. Never merges by name."
    )

    def add_arguments(self, parser):
        parser.add_argument("--candidate", required=True)
        parser.add_argument("--expected-sha256", required=True)
        parser.add_argument("--approved-live-preflight", required=True)
        parser.add_argument("--expected-preflight-sha256", required=True)
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Read-only: classify READY_TO_APPLY / ALREADY_APPLIED.",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Mutate the registry inside one atomic transaction.",
        )
        parser.add_argument(
            "--confirm",
            default="",
            help=f"Required for --apply. Must be exactly {APPLY_CONFIRM_TOKEN}.",
        )
        parser.add_argument("--output")

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        do_apply = bool(options["apply"])

        if dry_run == do_apply:
            raise CommandError("exactly one of --dry-run or --apply is required")

        if do_apply and options["confirm"] != APPLY_CONFIRM_TOKEN:
            raise CommandError(f"--apply requires --confirm {APPLY_CONFIRM_TOKEN}")

        candidate_path = Path(options["candidate"])
        expected_sha = options["expected_sha256"].lower()
        approved_path = Path(options["approved_live_preflight"])
        expected_preflight_sha = options["expected_preflight_sha256"].lower()

        try:
            candidate = load_candidate(candidate_path, expected_sha)
            candidate_sha256 = sha256_path(candidate_path)
            approved = load_approved_live_preflight(
                approved_path,
                expected_preflight_sha256=expected_preflight_sha,
                expected_candidate_sha256=candidate_sha256,
            )
            result = run_v18_people_registry_apply(
                candidate=candidate,
                approved_database=approved["database"],
                candidate_sha256=candidate_sha256,
                approved_preflight_sha256=expected_preflight_sha,
                mode="dry-run" if dry_run else "apply",
            )
        except ApplySearchRefreshError as exc:
            payload = (
                json.dumps(
                    exc.result,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
            if options["output"]:
                Path(options["output"]).write_text(payload, encoding="utf-8")
            else:
                self.stdout.write(payload)
            raise CommandError(str(exc)) from exc
        except (ApplyError, PreflightError) as exc:
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
            output.write_text(payload, encoding="utf-8")
            self.stdout.write(str(output))
        else:
            self.stdout.write(payload)
