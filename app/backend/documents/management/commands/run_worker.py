import json
import os
import time
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
from documents.services.htr_engine import transcribe_pages
from documents.services.page_extraction import extract_pages


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
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-central-1"
        sqs = boto3.client("sqs", region_name=region)

        self.stdout.write(
            self.style.SUCCESS(f"[run_worker] starting | region={region} | queue={queue_url}")
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

    # ------------------------------------------------------------------ SQS

    def _receive_one(self, sqs, queue_url, max_msgs, wait):
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max(1, min(max_msgs, 10)),
                WaitTimeSeconds=max(0, min(wait, 20)),
                VisibilityTimeout=120,
            )
            msgs = resp.get("Messages") or []
            return msgs[0] if msgs else None
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"SQS receive error: {e}"))
            return None

    def _delete_message(self, sqs, queue_url, msg):
        try:
            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=msg["ReceiptHandle"],
            )
            self.stdout.write("[run_worker] deleted message from SQS")
        except Exception as e:
            self.stderr.write(f"SQS delete error: {e}")

    # ------------------------------------------------------------------ CORE

    def _process_message(self, msg: Dict[str, Any]) -> bool:
        try:
            payload = json.loads(msg.get("Body", "{}"))
        except Exception:
            self.stderr.write("[run_worker] invalid JSON body")
            return True

        if payload.get("type") != "PROCESS_DOCUMENT":
            return True

        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            return True

        # Phase 1: mark PROCESSING
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)
                if doc.upload_status != Document.UploadStatus.UPLOADED:
                    return True
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])
        except Document.DoesNotExist:
            return True

        # Phase 2: heavy work
        error: Optional[str] = None
        htr_result = None

        try:
            bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
            if not bucket:
                raise RuntimeError("UPLOADS_BUCKET_NAME is not configured")

            file_bytes, s3_mime = get_object_bytes(bucket=bucket, key=doc.file_s3_key)
            effective_mime = (doc.mime_type or s3_mime or "").strip()

            pages = extract_pages(file_bytes=file_bytes, mime_type=effective_mime)

            cfg = self._cfg

            htr_result = transcribe_pages(
                pages=pages,
                language_hint=doc.language,
                model_name="gemini-2.0-flash",
                min_text_length=cfg.min_text_length,
                double_pass=cfg.gemini_double_pass,
                consistency_min_ratio=cfg.gemini_consistency_min_ratio,
                temperature=cfg.gemini_temperature,
                top_k=cfg.gemini_top_k,
                top_p=cfg.gemini_top_p,
                max_output_tokens=cfg.gemini_max_output_tokens,
            )

        except Exception as e:
            error = str(e)
            self.stderr.write(
                self.style.ERROR(f"[run_worker] processing error for doc {document_id}: {e}")
            )

        # Phase 3: save results + final state
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)

                engine = htr_result.engine_name if htr_result else "gemini-2.0-flash"
                is_he = _is_hebrew_language(doc.language)

                if error:
                    self._save_ocr_failure(doc, engine, is_he, error)
                else:
                    self._save_htr_results(doc, engine, is_he, htr_result)

                self._update_processing_state(doc, engine)
                doc.save(update_fields=["processing_state_user"])
        except Document.DoesNotExist:
            return True

        return True

    # ------------------------------------------------------------------ HELPERS

    def _derive_review_reasons(self, text: str, needs_review: bool, engine_reasons: Optional[List[str]]) -> List[str]:
        reasons: List[str] = []
        stripped = (text or "").strip()

        if needs_review:
            reasons.append("NEEDS_REVIEW_FLAG")

        if len(stripped) < self._cfg.min_text_length:
            reasons.append("MIN_TEXT_LENGTH")

        if "[UNCLEAR]" in stripped:
            reasons.append("HAS_UNCLEAR")

        if stripped == "[NO_TEXT]":
            reasons.append("NO_TEXT_MARKER")

        if engine_reasons:
            for r in engine_reasons:
                if r and r not in reasons:
                    reasons.append(r)

        return reasons

    def _save_htr_results(self, doc: Document, engine: str, is_he: bool, htr):
        status = (
            DocumentTextResult.Status.NEEDS_REVIEW
            if htr.needs_review
            else DocumentTextResult.Status.SUCCEEDED
        )

        review_reasons = self._derive_review_reasons(
            htr.text,
            htr.needs_review,
            getattr(htr, "review_reasons", None),
        )

        DocumentTextResult.objects.update_or_create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            defaults={
                "status": status,
                "text": htr.text,
                "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                "error_code": None,
                "error_details": None,
                "review_reasons": json.dumps(review_reasons),
            },
        )

        if is_he:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=engine,
                defaults={
                    "status": status,
                    "text": htr.text,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": None,
                    "error_details": None,
                    "review_reasons": json.dumps(review_reasons),
                },
            )

    def _save_ocr_failure(self, doc, engine, is_he, details):
        DocumentTextResult.objects.update_or_create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            defaults={
                "status": DocumentTextResult.Status.FAILED,
                "text": None,
                "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                "error_code": "OCR_FAILED",
                "error_details": details,
                "review_reasons": "",
            },
        )

        if is_he:
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=engine,
                defaults={
                    "status": DocumentTextResult.Status.FAILED,
                    "text": None,
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": "OCR_FAILED",
                    "error_details": details,
                    "review_reasons": "",
                },
            )

    def _update_processing_state(self, doc, engine):
        expected_types = expected_result_types_for_document(doc)

        qs = doc.text_results.filter(
            engine=engine,
            result_type__in=expected_types,
        )

        if qs.filter(status=DocumentTextResult.Status.NEEDS_REVIEW).exists():
            doc.processing_state_user = Document.ProcessingState.PARTIAL
            return

        existing = qs.count()
        succeeded = qs.filter(status=DocumentTextResult.Status.SUCCEEDED).count()
        failed = qs.filter(status=DocumentTextResult.Status.FAILED).count()

        missing = len(expected_types) - existing

        if missing == 0 and succeeded == len(expected_types):
            doc.processing_state_user = Document.ProcessingState.READY
        elif missing == 0 and failed == len(expected_types):
            doc.processing_state_user = Document.ProcessingState.FAILED
        else:
            doc.processing_state_user = Document.ProcessingState.PARTIAL
