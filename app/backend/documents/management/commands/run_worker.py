import json
import os
import time
import logging
from typing import Any, Dict, Optional, List

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from documents.models import Document, DocumentTextResult
from documents.s3 import get_object_bytes
from documents.services.env_validation import EnvConfigError, WorkerEnvConfig, validate_required_env
from documents.services.expected_outputs import expected_result_types_for_document
from documents.services.htr_adapters.base import UnsupportedEngineError
from documents.services.htr_engine import transcribe_pages
from documents.services.ocr_routing import OcrRouteConfig, select_ocr_route
from documents.services.page_extraction import extract_pages

logger = logging.getLogger(__name__)

UNRESOLVED_ROUTE_METADATA = "UNRESOLVED"
AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW = "AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"


def _dedupe_strings_preserve_order(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value

def _is_hebrew_language(language: Optional[str]) -> bool:
    lang = (language or "").strip().lower()
    return lang in ("he", "heb", "hebrew")

class Command(BaseCommand):
    help = "Run the async worker: poll SQS and process document jobs."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--sleep-seconds", type=int, default=2)
        parser.add_argument("--max-messages", type=int, default=1)
        parser.add_argument("--wait-seconds", type=int, default=20)

    def handle(self, *args, **options):
        try:
            self._cfg: WorkerEnvConfig = validate_required_env()
        except EnvConfigError as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] env error: {e}"))
            raise SystemExit(1)

        queue_url = _env("SQS_QUEUE_URL")
        region = os.getenv("AWS_REGION") or "eu-central-1"
        sqs = boto3.client("sqs", region_name=region)

        self.stdout.write(
            self.style.SUCCESS(f"[run_worker] starting | queue={queue_url}")
        )

        while True:
            msg = self._receive_one(
                sqs,
                queue_url,
                options["max_messages"],
                options["wait_seconds"],
            )

            if msg is None:
                if options["once"]:
                    return
                time.sleep(options["sleep_seconds"])
                continue

            ok = self._process_message(msg)

            if ok:
                self._delete_message(sqs, queue_url, msg)

            if options["once"]:
                return

    # ------------------------------------------------------------------ SQS Helpers

    def _receive_one(self, sqs, queue_url, max_msgs, wait):
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max(1, min(max_msgs, 10)),
                WaitTimeSeconds=max(0, min(wait, 20)),
                VisibilityTimeout=300,
            )
            msgs = resp.get("Messages") or []
            return msgs[0] if msgs else None
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"SQS receive error: {e}"))
            return None

    def _delete_message(self, sqs, queue_url, msg):
        try:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=msg["ReceiptHandle"])
        except Exception as e:
            self.stderr.write(f"SQS delete error: {e}")

    # ------------------------------------------------------------------ Core Logic

    def _process_message(self, msg: Dict[str, Any]) -> bool:
        try:
            payload = json.loads(msg.get("Body", "{}"))
        except Exception:
            return True

        if payload.get("type") != "PROCESS_DOCUMENT":
            return True

        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            return True

        # Phase 1: Mark PROCESSING
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)
                if doc.upload_status != Document.UploadStatus.UPLOADED:
                    return True
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])
        except Document.DoesNotExist:
            return True

        # Phase 2: Heavy work
        error: Optional[str] = None
        htr_result = None
        processing_exc: Optional[Exception] = None
        route: Optional[OcrRouteConfig] = None

        try:
            bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
            file_bytes, s3_mime = get_object_bytes(bucket=bucket, key=doc.file_s3_key)
            effective_mime = (doc.mime_type or s3_mime or "").strip()
            pages = extract_pages(file_bytes=file_bytes, mime_type=effective_mime)

            route = select_ocr_route(doc.language, doc.text_input_type)
            htr_result = transcribe_pages(
                pages=pages,
                language_hint=doc.language,
                text_input_type=doc.text_input_type,
                route=route,
                worker_env=self._cfg,
            )

        except Exception as e:
            processing_exc = e
            error = str(e)
            self.stderr.write(self.style.ERROR(f"Processing error for doc {document_id}: {e}"))

        # Phase 3: Save results
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)
                if htr_result:
                    final_engine = htr_result.engine_name
                elif isinstance(processing_exc, UnsupportedEngineError):
                    final_engine = f"unsupported:{processing_exc.engine_key}"
                else:
                    final_engine = "ocr-dispatch"
                is_he = _is_hebrew_language(doc.language)

                if error:
                    self._save_ocr_failure(doc, final_engine, is_he, error)
                else:
                    if route is None or htr_result is None:
                        raise RuntimeError(
                            "Internal error: OCR success path missing route or HTR result"
                        )
                    self._save_htr_results(doc, final_engine, is_he, htr_result, route)

                self._update_processing_state(doc, final_engine)
                doc.save(update_fields=["processing_state_user"])
        except Document.DoesNotExist:
            pass

        return True

    # ------------------------------------------------------------------ DB Helpers

    def _derive_review_reasons(
        self,
        text: str,
        adapter_needs_review: bool,
        engine_reasons: Optional[List[str]],
        *,
        include_automatic_policy: bool,
    ) -> List[str]:
        reasons: List[str] = []
        if include_automatic_policy:
            reasons.append(AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW)
        if adapter_needs_review:
            reasons.append("NEEDS_REVIEW_FLAG")

        stripped = (text or "").strip()
        if len(stripped) < self._cfg.min_text_length:
            reasons.append("MIN_TEXT_LENGTH")
        if "[UNCLEAR]" in stripped:
            reasons.append("HAS_UNCLEAR")

        if engine_reasons:
            for r in engine_reasons:
                if r:
                    reasons.append(r)

        return _dedupe_strings_preserve_order(reasons)

    def _save_htr_results(
        self,
        doc: Document,
        engine: str,
        is_he: bool,
        htr,
        route: OcrRouteConfig,
    ):
        status = DocumentTextResult.Status.NEEDS_REVIEW
        review_reasons = self._derive_review_reasons(
            htr.text,
            htr.needs_review,
            getattr(htr, "review_reasons", None),
            include_automatic_policy=True,
        )

        target_types = [DocumentTextResult.ResultType.SOURCE_TEXT]
        if is_he:
            target_types.append(DocumentTextResult.ResultType.HEBREW_TEXT)

        for r_type in target_types:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=r_type,
                engine=engine,
                defaults={
                    "status": status,
                    "text": htr.text,
                    "engine_key": route.engine_key,
                    "prompt_variant": route.prompt_variant,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": None,
                    "error_details": None,
                    "review_reasons": json.dumps(review_reasons),
                },
            )

    def _save_ocr_failure(self, doc, engine, is_he, details):
        route_metadata = self._route_metadata_for_failure(doc)
        has_valid_route = route_metadata is not None
        if has_valid_route:
            engine_key, prompt_variant = route_metadata
            error_code = "OCR_FAILED"
        else:
            # Keep failure persistence explicit when routing metadata is invalid.
            # Avoid misleading fallback metadata such as GEMINI/handwritten.
            engine_key = UNRESOLVED_ROUTE_METADATA
            prompt_variant = UNRESOLVED_ROUTE_METADATA
            error_code = "OCR_ROUTING_INVALID"
        target_types = [DocumentTextResult.ResultType.SOURCE_TEXT]
        if is_he:
            target_types.append(DocumentTextResult.ResultType.HEBREW_TEXT)
        for r_type in target_types:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=r_type,
                engine=engine,
                defaults={
                    "status": DocumentTextResult.Status.FAILED,
                    "text": None,
                    "engine_key": engine_key,
                    "prompt_variant": prompt_variant,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": error_code,
                    "error_details": details,
                    "review_reasons": "",
                },
            )

    def _route_metadata_for_failure(self, doc: Document):
        """
        Re-select route on failure paths (no successful HtrResult).
        Success paths use the route selected in Phase 2 and passed into persistence.
        """
        try:
            route = select_ocr_route(doc.language, doc.text_input_type)
            return route.engine_key, route.prompt_variant
        except ValueError:
            return None

    def _update_processing_state(self, doc, engine):
        expected_types = expected_result_types_for_document(doc)
        qs = doc.text_results.filter(engine=engine, result_type__in=expected_types)

        rows_by_type: dict[str, Optional[DocumentTextResult]] = {}
        for rt in expected_types:
            rows_by_type[rt] = qs.filter(result_type=rt).first()

        all_rows: list[DocumentTextResult] = []
        for rt in expected_types:
            row = rows_by_type[rt]
            if row is None:
                doc.processing_state_user = Document.ProcessingState.PARTIAL
                return
            all_rows.append(row)

        def _row_usable(row: DocumentTextResult) -> bool:
            if row.status not in (
                DocumentTextResult.Status.SUCCEEDED,
                DocumentTextResult.Status.NEEDS_REVIEW,
            ):
                return False
            return bool((row.text or "").strip())

        all_failed = all(r.status == DocumentTextResult.Status.FAILED for r in all_rows)
        if all_failed:
            doc.processing_state_user = Document.ProcessingState.FAILED
            return

        if all(_row_usable(r) for r in all_rows):
            doc.processing_state_user = Document.ProcessingState.READY
            return

        doc.processing_state_user = Document.ProcessingState.PARTIAL
