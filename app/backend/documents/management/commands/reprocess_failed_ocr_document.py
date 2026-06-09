from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    apply_ocr_reprocess,
    assess_ocr_reprocess,
)


class Command(BaseCommand):
    help = (
        "Assess or enqueue OCR reprocess for a failed OCR-backed Document. "
        "Default is dry-run (no writes, no SQS). Pass --apply to mutate and enqueue."
    )

    def add_arguments(self, parser):
        parser.add_argument("document_id", type=int, help="Document id to reprocess.")
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Set PROCESSING, clear upload_error when set, and enqueue PROCESS_DOCUMENT.",
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        apply_mode = bool(options["apply"])

        try:
            worker_env = validate_required_env()
        except EnvConfigError as exc:
            raise CommandError(f"env error: {exc}") from exc

        collection_id = worker_env.transkribus_collection_id or ""
        model_id = worker_env.transkribus_model_id or ""

        try:
            if apply_mode:
                assessment = apply_ocr_reprocess(
                    document_id,
                    collection_id=collection_id,
                    model_id=model_id,
                )
                mode_label = "apply"
            else:
                assessment = assess_ocr_reprocess(
                    document_id,
                    collection_id=collection_id,
                    model_id=model_id,
                )
                mode_label = "dry-run"
        except OcrReprocessError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"document_id={assessment.document_id} "
            f"mode={mode_label} "
            f"retry_mode={assessment.retry_mode.value} "
            f"collection_id={collection_id!r} "
            f"model_id={model_id!r}"
        )
        if assessment.source_transkribus_run_id is not None:
            self.stdout.write(
                f"source_transkribus_run_id={assessment.source_transkribus_run_id}"
            )
        if apply_mode:
            self.stdout.write(self.style.SUCCESS("enqueued PROCESS_DOCUMENT"))
        else:
            self.stdout.write("no changes made (dry-run)")
