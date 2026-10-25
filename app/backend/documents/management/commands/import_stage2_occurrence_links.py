"""Read-only preflight for the one-time stage 2 occurrence-link import."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_stage2_occurrence_preflight import (
    RECORDED_CANDIDATE_SHA256,
    Stage2OccurrencePreflightError,
    preflight_stage2_occurrence_links,
)


class Command(BaseCommand):
    help = (
        "Compare the reconstructed stage 2 occurrence-link CSV with current "
        "authoritative text and reviewed decisions. Dry-run only: writes a "
        "report and does not change database rows, approved_for_apply, or "
        "pending proposals. There is no apply mode."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            required=True,
            help="Path to stage2_post_merge_technical_candidates.csv.",
        )
        parser.add_argument(
            "--expected-sha256",
            required=True,
            help=(
                "SHA-256 of the candidate file bytes. Must equal the recorded "
                f"hash {RECORDED_CANDIDATE_SHA256}."
            ),
        )
        parser.add_argument(
            "--output-dir",
            required=True,
            help=(
                "Directory for the CSV, JSON, and Markdown reports. "
                "The persistent audit directory is the operator target."
            ),
        )

    def handle(self, *args, **options):
        expected = options["expected_sha256"].strip().lower()
        if expected != RECORDED_CANDIDATE_SHA256:
            raise CommandError(
                "expected sha256 does not match the recorded candidate file"
            )
        try:
            result = preflight_stage2_occurrence_links(
                Path(options["csv"]),
                expected_sha256=expected,
                output_dir=Path(options["output_dir"]),
            )
        except Stage2OccurrencePreflightError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(result.summary)
        if not result.clean:
            raise CommandError("stage 2 occurrence preflight is not clean")
