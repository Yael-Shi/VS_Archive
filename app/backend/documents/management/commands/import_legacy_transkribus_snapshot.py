from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.services.transkribus_legacy_import import (
    LegacyImportError,
    apply_legacy_transkribus_import,
    prepare_legacy_transkribus_import,
)

User = get_user_model()


def _parse_pair(raw: str, *, label: str) -> tuple[int, str]:
    left, sep, right = raw.partition(":")
    if not sep:
        raise CommandError(f"{label} must use LEFT:RIGHT format, got {raw!r}.")
    try:
        left_int = int(left)
    except ValueError as exc:
        raise CommandError(f"{label} left side must be an integer: {raw!r}.") from exc
    right = right.strip()
    if not right:
        raise CommandError(f"{label} right side must be non-empty: {raw!r}.")
    return left_int, right


class Command(BaseCommand):
    help = (
        "Dry-run or apply a trusted historical Transkribus PAGE-XML mapping "
        "to an existing HEBREW_TEXT result. Dry-run is the default."
    )

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--text-result-id", type=int, required=True)
        parser.add_argument("--collection-id", required=True)
        parser.add_argument("--remote-doc-id", required=True)
        parser.add_argument(
            "--page-map",
            action="append",
            required=True,
            metavar="LOCAL_INDEX:REMOTE_PAGE_NR",
            help="Repeat once per local page, e.g. --page-map 1:2",
        )
        parser.add_argument(
            "--transcript-map",
            action="append",
            default=[],
            metavar="REMOTE_PAGE_NR:TS_ID",
            help=(
                "Optional explicit transcript tsId for pages with multiple "
                "transcripts. Repeat as needed."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist snapshot/text alignment/binding. Omit for dry-run.",
        )
        parser.add_argument(
            "--user-id",
            type=int,
            help="Required with --apply; audit/binding actor.",
        )

    def handle(self, *args, **options):
        page_map: dict[int, int] = {}
        for raw in options["page_map"]:
            local_index, remote_raw = _parse_pair(raw, label="--page-map")
            try:
                remote_nr = int(remote_raw)
            except ValueError as exc:
                raise CommandError(
                    f"--page-map remote pageNr must be an integer: {raw!r}."
                ) from exc
            if local_index in page_map:
                raise CommandError(
                    f"Duplicate local page index in --page-map: {local_index}."
                )
            page_map[local_index] = remote_nr

        transcript_map: dict[int, str] = {}
        for raw in options["transcript_map"]:
            remote_nr, ts_id = _parse_pair(raw, label="--transcript-map")
            if remote_nr in transcript_map:
                raise CommandError(
                    f"Duplicate pageNr in --transcript-map: {remote_nr}."
                )
            transcript_map[remote_nr] = ts_id

        unknown_transcript_pages = set(transcript_map) - set(page_map.values())
        if unknown_transcript_pages:
            raise CommandError(
                "--transcript-map contains pageNr values not present in --page-map: "
                + ", ".join(str(value) for value in sorted(unknown_transcript_pages))
            )

        username = (os.getenv("TRANSKRIBUS_USERNAME") or "").strip()
        password = (os.getenv("TRANSKRIBUS_PASSWORD") or "").strip()
        bearer_token = (os.getenv("TRANSKRIBUS_API_TOKEN") or "").strip()

        missing = [
            name
            for name, value in (
                ("TRANSKRIBUS_USERNAME", username),
                ("TRANSKRIBUS_PASSWORD", password),
                ("TRANSKRIBUS_API_TOKEN", bearer_token),
            )
            if not value
        ]
        if missing:
            raise CommandError(
                "Missing required Transkribus environment variables: "
                + ", ".join(missing)
            )

        try:
            plan = prepare_legacy_transkribus_import(
                document_id=options["document_id"],
                text_result_id=options["text_result_id"],
                collection_id=options["collection_id"],
                remote_doc_id=options["remote_doc_id"],
                page_index_to_page_nr=page_map,
                username=username,
                password=password,
                bearer_token=bearer_token,
                transcript_ts_id_by_page_nr=transcript_map,
            )
        except LegacyImportError as exc:
            raise CommandError(str(exc)) from exc

        mode = "APPLY" if options["apply"] else "DRY_RUN"
        self.stdout.write(f"mode={mode}")
        self.stdout.write(f"document_id={plan.document_id}")
        self.stdout.write(f"text_result_id={plan.text_result_id}")
        self.stdout.write("result_type=HEBREW_TEXT")
        self.stdout.write(f"verification_status={plan.verification_status}")
        self.stdout.write(f"based_on_source_revision={plan.based_on_source_revision}")
        self.stdout.write(f"collection_id={plan.collection_id}")
        self.stdout.write(f"remote_doc_id={plan.remote_doc_id}")
        self.stdout.write(
            "page_map="
            + ",".join(
                f"{idx}:{nr}" for idx, nr in sorted(plan.page_index_to_page_nr.items())
            )
        )
        for selected in plan.selected_pages:
            self.stdout.write(
                "selected_page="
                f"{selected.page_index}:{selected.page_nr} "
                f"tsId={selected.transcript_ts_id} "
                f"status={selected.remote_transcript_status or ''}"
            )
        self.stdout.write(f"current_chars={plan.current_text_chars}")
        self.stdout.write(f"canonical_chars={plan.canonical_text_chars}")
        self.stdout.write(f"current_sha256={plan.current_text_sha256}")
        self.stdout.write(f"canonical_sha256={plan.canonical_text_sha256}")
        self.stdout.write(f"equivalence={plan.equivalence.value}")
        self.stdout.write(f"bytes_would_change={plan.bytes_would_change}")
        self.stdout.write(
            "verification_would_be_preserved="
            + str(plan.equivalence.value in {"EXACT", "WHITESPACE_ONLY"})
        )
        self.stdout.write("binding_role=HEBREW_MIRROR")
        self.stdout.write(f"geometry_capability={plan.geometry_capability}")
        self.stdout.write(f"hover_eligible={plan.hover_eligible}")

        if not plan.would_apply:
            self.stdout.write("decision=BLOCKED")
            if options["apply"]:
                raise CommandError("CONTENT_MISMATCH cannot be applied.")
            return

        self.stdout.write("decision=WOULD_APPLY")

        if not options["apply"]:
            return

        user_id = options.get("user_id")
        if not user_id:
            raise CommandError("--user-id is required with --apply.")

        try:
            actor = User.objects.get(pk=user_id)
        except User.DoesNotExist as exc:
            raise CommandError(f"User id={user_id} does not exist.") from exc

        try:
            result = apply_legacy_transkribus_import(plan=plan, actor=actor)
        except LegacyImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f"apply_outcome={result.outcome}")
        self.stdout.write(f"snapshot_id={result.snapshot_id}")
        self.stdout.write(f"storage_outcome={result.storage_outcome}")
        self.stdout.write(f"text_changed={result.text_changed}")
        self.stdout.write(f"verification_status_after={result.verification_status}")
        self.stdout.write(f"binding_id={result.binding_id}")
        self.stdout.write(
            f"binding_structurally_fresh={result.binding_structurally_fresh}"
        )
        self.stdout.write(f"hover_trusted={result.hover_trusted}")
