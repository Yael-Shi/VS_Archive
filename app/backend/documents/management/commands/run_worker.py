import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from documents.models import Document, DocumentTextResult
from documents.s3 import get_object_bytes
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
        queue_url = _env("SQS_QUEUE_URL")
        region = (
            os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-central-1"
        )

        # Google credentials (MVP)
        gcp_json = os.environ.get("GCP_SA_JSON")
        if gcp_json:
            creds_path = Path("/tmp/gcp-sa.json")
            creds_path.write_text(gcp_json, encoding="utf-8")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)

        sqs = boto3.client("sqs", region_name=region)

        self.stdout.write(
            self.style.SUCCESS(
                f"[run_worker] starting | region={region} | queue={queue_url}"
            )
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
        # Parse payload
        try:
            payload = json.loads(msg.get("Body", "{}"))
        except Exception:
            self.stderr.write("[run_worker] invalid JSON body")
            return True  # poison message → delete

        if payload.get("type") != "PROCESS_DOCUMENT":
            self.stderr.write(f"[run_worker] unknown job type: {payload.get('type')!r}")
            return True

        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            self.stderr.write("[run_worker] invalid document_id")
            return True

        # Phase 1: mark PROCESSING (short transaction)
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)

                if doc.upload_status != Document.UploadStatus.UPLOADED:
                    return True  # stale job

                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])
        except Document.DoesNotExist:
            return True

        # Phase 2: heavy work (no DB locks)
        error: Optional[str] = None
        htr_result = None

        try:
            bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
            if not bucket:
                raise RuntimeError("UPLOADS_BUCKET_NAME is not configured")

            if not (doc.file_s3_key or "").strip():
                raise RuntimeError("Document file_s3_key is missing")

            file_bytes, s3_mime = get_object_bytes(
                bucket=bucket,
                key=doc.file_s3_key,
            )

            effective_mime = (doc.mime_type or s3_mime or "").strip()

            pages = extract_pages(
                file_bytes=file_bytes,
                mime_type=effective_mime,
            )

            htr_result = transcribe_pages(
                pages=pages,
                language_hint=doc.language,
            )

        except Exception as e:
            error = str(e)
            self.stderr.write(
                self.style.ERROR(
                    f"[run_worker] processing error for doc {document_id}: {e}"
                )
            )

        # Phase 3: save results + final state (short transaction)
        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)

                engine = "google_vision_v1"
                is_he = _is_hebrew_language(doc.language)

                if error:
                    self._save_ocr_failure(doc, engine, is_he, error)
                else:
                    self._save_htr_results(doc, engine, is_he, htr_result)

                self._update_processing_state(doc, engine, is_he)
                doc.save(update_fields=["processing_state_user"])

        except Document.DoesNotExist:
            return True

        return True

    # ------------------------------------------------------------------ HELPERS

    def _save_htr_results(self, doc: Document, engine: str, is_he: bool, htr):
        status = (
            DocumentTextResult.Status.NEEDS_REVIEW
            if getattr(htr, "needs_review", False)
            else DocumentTextResult.Status.SUCCEEDED
        )

        DocumentTextResult.objects.update_or_create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
            defaults={
                "status": status,
                "text": getattr(htr, "text", None),
                "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                "error_code": None,
                "error_details": None,
            },
        )

        if is_he:
            # Hebrew docs: only HEBREW_TEXT is expected.
            DocumentTextResult.objects.update_or_create(
                document=doc,
                result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                engine=engine,
                defaults={
                    "status": status,
                    "text": getattr(htr, "text", None),
                    "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                    "error_code": None,
                    "error_details": None,
                },
            )

    def _save_ocr_failure(self, doc, engine, is_he, details):
        # Non-Hebrew: only SOURCE_TEXT in current MVP (Hebrew translation is a separate step).
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
                },
            )

    def _update_processing_state(self, doc, engine, is_he):
        expected_types = (
            [DocumentTextResult.ResultType.HEBREW_TEXT]
            if is_he
            else [
                DocumentTextResult.ResultType.SOURCE_TEXT,
                DocumentTextResult.ResultType.HEBREW_TEXT,
            ]
        )

        qs = doc.text_results.filter(engine=engine, result_type__in=expected_types)

        # We do not expose ACTION_REQUIRED to non-admin users; NEEDS_REVIEW maps to PARTIAL.
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
