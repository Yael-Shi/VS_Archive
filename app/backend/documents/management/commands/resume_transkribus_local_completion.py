"""Staff-authorized Transkribus resume-only local-completion CLI.

Dry-run by default. ``--apply`` performs the classified recovery phase without
starting recognition or adopting the worker lease_token.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

import requests
from django.core.management.base import BaseCommand, CommandError

from documents.models import TranskribusRun
from documents.services import transkribus_engine as tr
from documents.services.transkribus_resume_only_recovery import (
    TranskribusResumeOnlyRecoveryAssessment,
    TranskribusResumeOnlyRecoveryError,
    apply_transkribus_resume_only_recovery,
    assess_transkribus_resume_only_recovery,
)


class Command(BaseCommand):
    help = (
        "Resume-only local completion for a finished Transkribus recognition "
        "job on a parked RECOVERY_REQUIRED ProcessDocumentRequest. "
        "Default is dry-run. --apply never starts recognition."
    )

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--request-id", type=int, required=True)
        parser.add_argument("--run-id", type=int, required=True)
        parser.add_argument("--recognition-job-id", required=True)
        parser.add_argument(
            "--remote-job-json",
            default="",
            help="Path to GET /jobs/{id} JSON. Mutually exclusive with --fetch-remote-job.",
        )
        parser.add_argument(
            "--fetch-remote-job",
            action="store_true",
            help=(
                "Login and GET the recognition job. Required for "
                "FETCH_AND_COMPLETE --apply (also fetches PAGE XML)."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apply the classified recovery phase. Default is dry-run.",
        )

    @staticmethod
    def _positive_id(value: int, *, option: str) -> int:
        if value < 1:
            raise CommandError(f"{option} must be a positive integer.")
        return value

    def _job_fetcher_from_json_path(self, path_text: str):
        path = Path(path_text)
        if not path.is_file():
            raise CommandError(f"--remote-job-json file not found: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CommandError(
                f"Failed to read --remote-job-json {path}: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CommandError("--remote-job-json must contain a JSON object.")

        def _fetch(_job_id: str) -> Mapping:
            return payload

        return _fetch

    def _live_session_helpers(self):
        username = (os.getenv("TRANSKRIBUS_USERNAME") or "").strip()
        password = (os.getenv("TRANSKRIBUS_PASSWORD") or "").strip()
        bearer = (os.getenv("TRANSKRIBUS_API_TOKEN") or "").strip()
        if not username or not password:
            raise CommandError(
                "--fetch-remote-job requires TRANSKRIBUS_USERNAME and "
                "TRANSKRIBUS_PASSWORD."
            )

        def job_fetcher(job_id: str) -> Mapping:
            with requests.Session() as session:
                tr.login_trp_server(session, username=username, password=password)
                return tr.get_job(session, job_id)

        def page_completer(run: TranskribusRun):
            with requests.Session() as session:
                tr.login_trp_server(session, username=username, password=password)
                return tr.complete_pylaia_transcription_after_job(
                    session,
                    recognition_job_id=str(run.recognition_job_id).strip(),
                    collection_id=str(run.collection_id).strip(),
                    model_id=str(run.model_id).strip(),
                    document_id=str(run.remote_doc_id).strip(),
                    pages_query=str(run.pages_query).strip(),
                    bearer_token=bearer,
                )

        return job_fetcher, page_completer

    def _write_assessment(
        self,
        assessment: TranskribusResumeOnlyRecoveryAssessment,
        *,
        mode: str,
    ) -> None:
        self.stdout.write(f"mode={mode}")
        self.stdout.write(
            f"document_id={assessment.document_id} "
            f"request_id={assessment.request_id} "
            f"run_id={assessment.run_id} "
            f"recognition_job_id={assessment.recognition_job_id}"
        )
        self.stdout.write(
            f"document_processing_state={assessment.document_processing_state} "
            f"request_status={assessment.request_status} "
            f"run_status={assessment.run_status}"
        )
        self.stdout.write(
            f"phase={assessment.phase} apply_permitted={assessment.apply_permitted} "
            f"reason={assessment.reason}"
        )
        self.stdout.write(
            f"association_present={assessment.association_present} "
            f"snapshot_id={assessment.snapshot_id} "
            f"remote_job_state={assessment.remote_job_state} "
            f"remote_job_success={assessment.remote_job_success}"
        )
        self.stdout.write(f"message={assessment.message}")
        self.stdout.write(
            f"review_preservation_note={assessment.review_preservation_note}"
        )

    def handle(self, *args, **options):
        document_id = self._positive_id(
            int(options["document_id"]), option="--document-id"
        )
        request_id = self._positive_id(
            int(options["request_id"]), option="--request-id"
        )
        run_id = self._positive_id(int(options["run_id"]), option="--run-id")
        recognition_job_id = str(options["recognition_job_id"]).strip()
        if not recognition_job_id:
            raise CommandError("--recognition-job-id must be non-empty.")
        apply_mode = bool(options["apply"])
        remote_job_json = str(options.get("remote_job_json") or "").strip()
        fetch_remote_job = bool(options["fetch_remote_job"])

        if remote_job_json and fetch_remote_job:
            raise CommandError(
                "Use only one of --remote-job-json or --fetch-remote-job."
            )

        page_completer = None
        if fetch_remote_job:
            job_fetcher, page_completer = self._live_session_helpers()
        elif remote_job_json:
            job_fetcher = self._job_fetcher_from_json_path(remote_job_json)
        else:
            job_fetcher = None

        mode = "apply" if apply_mode else "dry-run"
        try:
            if apply_mode:
                pre = assess_transkribus_resume_only_recovery(
                    document_id=document_id,
                    request_id=request_id,
                    run_id=run_id,
                    recognition_job_id=recognition_job_id,
                    job_fetcher=job_fetcher,
                )
                if (
                    pre.phase == "FETCH_AND_COMPLETE"
                    and page_completer is None
                    and pre.apply_permitted
                ):
                    raise CommandError(
                        "FETCH_AND_COMPLETE --apply requires --fetch-remote-job "
                        "so PAGE XML can be fetched without starting recognition."
                    )
                assessment = apply_transkribus_resume_only_recovery(
                    document_id=document_id,
                    request_id=request_id,
                    run_id=run_id,
                    recognition_job_id=recognition_job_id,
                    job_fetcher=job_fetcher,
                    page_completer=page_completer,
                )
                self._write_assessment(assessment, mode=mode)
                self.stdout.write("apply completed")
            else:
                assessment = assess_transkribus_resume_only_recovery(
                    document_id=document_id,
                    request_id=request_id,
                    run_id=run_id,
                    recognition_job_id=recognition_job_id,
                    job_fetcher=job_fetcher,
                )
                self._write_assessment(assessment, mode=mode)
                self.stdout.write("no changes made (dry-run)")
        except TranskribusResumeOnlyRecoveryError as exc:
            raise CommandError(f"{exc.code}: {exc.message}") from exc
