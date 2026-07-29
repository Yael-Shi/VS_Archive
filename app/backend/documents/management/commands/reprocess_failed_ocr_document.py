from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    assess_ocr_reprocess,
)
from documents.services.process_document_ocr_reprocess_enqueue import (
    apply_ocr_reprocess,
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
            help=(
                "Create, retry, or coalesce a durable PROCESS_DOCUMENT request "
                "and reflect its current state."
            ),
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
                apply_result = apply_ocr_reprocess(
                    document_id,
                    collection_id=collection_id,
                    model_id=model_id,
                    initiated_by=None,
                )
                assessment = apply_result.assessment
                mode_label = "apply"
            else:
                apply_result = None
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
            assert apply_result is not None
            self.stdout.write(
                f"request_id={apply_result.enqueue_result.request.pk} "
                f"enqueue_outcome={apply_result.enqueue_result.outcome}"
            )
            self.stdout.write(self.style.SUCCESS("PROCESS_DOCUMENT request handled"))
        else:
            self.stdout.write("no changes made (dry-run)")
