"""Guarded one-time import of reconstructed stage 2 occurrence links."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.services.non_person_entity_stage2_occurrence_preflight import (
    APPLY_CONFIRM_TOKEN,
    RECORDED_CANDIDATE_SHA256,
    Stage2OccurrenceApplyError,
    Stage2OccurrencePreflightError,
    apply_stage2_occurrence_links,
    preflight_stage2_occurrence_links,
)


class Command(BaseCommand):
    help = (
        "Compare the reconstructed stage 2 occurrence-link CSV with current "
        "authoritative text and reviewed decisions. The default is a dry run "
        "and writes no database rows. --apply inserts ArchiveItemEntityOccurrence "
        "rows only when --confirm is the recorded token and --expected-sha256 "
        f"is {RECORDED_CANDIDATE_SHA256}."
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
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Insert validated occurrence rows in one transaction. "
                "Requires --confirm."
            ),
        )
        parser.add_argument(
            "--confirm",
            default="",
            help=(f"Required with --apply. Must equal {APPLY_CONFIRM_TOKEN}."),
        )

    def handle(self, *args, **options):
        expected = options["expected_sha256"].strip().lower()
        if options["apply"]:
            if options["confirm"] != APPLY_CONFIRM_TOKEN:
                raise CommandError("confirmation token rejected")
        if expected != RECORDED_CANDIDATE_SHA256:
            raise CommandError(
                "expected sha256 does not match the recorded candidate file"
            )
        if options["apply"]:
            try:
                result = apply_stage2_occurrence_links(
                    Path(options["csv"]),
                    expected_sha256=expected,
                    output_dir=Path(options["output_dir"]),
                    confirm=options["confirm"],
                )
            except (Stage2OccurrenceApplyError, Stage2OccurrencePreflightError) as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(result.summary)
            return
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
