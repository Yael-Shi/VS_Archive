import json
import os
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings

from documents.models import Document, DocumentTextResult
from documents.s3 import get_object_bytes
from documents.services.page_extraction import extract_pages
from documents.services.htr_engine import HtrNotImplementedError, transcribe_pages


# NOTE:
# This command is meant to run inside the ECS "worker" container.
# It long-polls SQS for jobs and processes them one by one.


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
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process at most one message and exit (useful for debugging).",
        )
        parser.add_argument(
            "--sleep-seconds",
            type=int,
            default=2,
            help="Sleep between empty polls (default: 2).",
        )
        parser.add_argument(
            "--max-messages",
            type=int,
            default=1,
            help="Max messages per poll (default: 1).",
        )
        parser.add_argument(
            "--wait-seconds",
            type=int,
            default=20,
            help="SQS long-poll wait time (0-20). Default: 20.",
        )

    def handle(self, *args, **options):
        queue_url = _env("SQS_QUEUE_URL")
        region = (
            os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-central-1"
        )

        once: bool = options["once"]
        sleep_seconds: int = options["sleep_seconds"]
        max_messages: int = options["max_messages"]
        wait_seconds: int = options["wait_seconds"]

        self.stdout.write(
            self.style.SUCCESS(
                f"[run_worker] starting | region={region} | queue={queue_url} | once={once}"
            )
        )

        sqs = boto3.client("sqs", region_name=region)

        while True:
            msg = self._receive_one(
                sqs=sqs,
                queue_url=queue_url,
                max_messages=max_messages,
                wait_seconds=wait_seconds,
            )

            if msg is None:
                if once:
                    self.stdout.write("[run_worker] no messages; exiting (--once).")
                    return
                time.sleep(sleep_seconds)
                continue

            ok = self._process_message(msg)
            if ok:
                self._delete_message(sqs, queue_url, msg)

            if once:
                self.stdout.write("[run_worker] processed one message; exiting (--once).")
                return

    def _receive_one(
        self,
        sqs,
        queue_url: str,
        max_messages: int,
        wait_seconds: int,
    ) -> Optional[Dict[str, Any]]:
        try:
            resp = sqs.receive_message(
                QueueUrl=queue_url,
                MaxNumberOfMessages=max(1, min(max_messages, 10)),
                WaitTimeSeconds=max(0, min(wait_seconds, 20)),
                VisibilityTimeout=60,  # seconds; keep short for now
            )
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] SQS receive failed: {e}"))
            return None

        messages = resp.get("Messages") or []
        if not messages:
            return None
        return messages[0]

    def _delete_message(self, sqs, queue_url: str, msg: Dict[str, Any]) -> None:
        receipt = msg.get("ReceiptHandle")
        if not receipt:
            self.stderr.write(
                self.style.ERROR("[run_worker] missing ReceiptHandle; cannot delete")
            )
            return
        try:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            self.stdout.write(
                self.style.SUCCESS("[run_worker] deleted message from SQS")
            )
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] SQS delete failed: {e}"))

    def _process_message(self, msg: Dict[str, Any]) -> bool:
        body = msg.get("Body", "")
        try:
            payload = json.loads(body)
        except Exception:
            self.stderr.write(
                self.style.ERROR(f"[run_worker] invalid JSON body: {body!r}")
            )
            return False

        if payload.get("type") != "PROCESS_DOCUMENT":
            self.stderr.write(self.style.ERROR(f"[run_worker] unknown job type: {payload.get('type')!r}"))
            return False

        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            self.stderr.write(
                self.style.ERROR("[run_worker] missing/invalid document_id")
            )
            return False

        self.stdout.write(f"[run_worker] processing document_id={document_id}")

        try:
            with transaction.atomic():
                doc = Document.objects.select_for_update().get(id=document_id)

                if doc.upload_status != Document.UploadStatus.UPLOADED:
                    self.stderr.write(
                        self.style.WARNING(
                            f"[run_worker] doc {doc.id} upload_status={doc.upload_status}; skipping"
                        )
                    )
                    return True  # delete message; nothing to do

                # mark as processing
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])

                is_he = _is_hebrew_language(doc.language)

                                # --- V2: Fetch file bytes from S3 ---
                bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
                if not bucket:
                    raise RuntimeError("UPLOADS_BUCKET_NAME is not configured")

                if not doc.file_s3_key:
                    raise RuntimeError(f"Document {doc.id} missing file_s3_key")

                file_bytes, s3_content_type = get_object_bytes(bucket=bucket, key=doc.file_s3_key)
                effective_mime = (doc.mime_type or s3_content_type or "").strip()

                # --- V2: Extract per-page images (PDF -> pages, IMAGE -> single page) ---
                pages = extract_pages(file_bytes=file_bytes, mime_type=effective_mime)

                # --- V2: Run HTR/OCR engine (placeholder for now) ---
                # In V2 MVP we explicitly fail until the engine adapter is implemented,
                # so we can evolve states correctly (FAILED / ACTION_REQUIRED) without dummy data.
                engine_name = "htr_placeholder_v1"

                def _create_result(result_type: str, status: str, text: str | None, error_code: str | None = None, error_details: str | None = None):
                    return DocumentTextResult.objects.create(
                        document=doc,
                        result_type=result_type,
                        engine=engine_name,
                        status=status,
                        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
                        text=text,
                        error_code=error_code,
                        error_details=error_details,
                    )

                # We treat Hebrew docs as "HEBREW_TEXT transcription".
                # Non-Hebrew docs: "SOURCE_TEXT transcription" + (future) "HEBREW_TEXT translation".
                try:
                    if is_he:
                        htr = transcribe_pages(pages=pages, language_hint=doc.language)
                        status = (
                            DocumentTextResult.Status.NEEDS_REVIEW
                            if htr.needs_review
                            else DocumentTextResult.Status.SUCCEEDED
                        )
                        _create_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=status,
                            text=htr.text,
                        )
                    else:
                        htr = transcribe_pages(pages=pages, language_hint=doc.language)
                        status = (
                            DocumentTextResult.Status.NEEDS_REVIEW
                            if htr.needs_review
                            else DocumentTextResult.Status.SUCCEEDED
                        )
                        _create_result(
                            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                            status=status,
                            text=htr.text,
                        )

                        # Translation is a separate engine/result in SOT.
                        # For step-1 we DO NOT implement translation yet.
                        # We create a FAILED placeholder so states become PARTIAL (not READY),
                        # making the missing translation explicit.
                        _create_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="TRANSLATION_NOT_IMPLEMENTED",
                            error_details="Translation engine is not implemented yet (V2 step-2).",
                        )

                except HtrNotImplementedError as e:
                    # Explicitly mark expected outputs as FAILED until engine exists.
                    if is_he:
                        _create_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="HTR_NOT_IMPLEMENTED",
                            error_details=str(e),
                        )
                    else:
                        _create_result(
                            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="HTR_NOT_IMPLEMENTED",
                            error_details=str(e),
                        )
                        _create_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="TRANSLATION_NOT_IMPLEMENTED",
                            error_details="Translation engine is not implemented yet (V2 step-2).",
                        )

                # Decide user-visible processing state based on expected outputs
                    expected_types = (
                    [DocumentTextResult.ResultType.HEBREW_TEXT]
                    if is_he
                    else [
                        DocumentTextResult.ResultType.SOURCE_TEXT,
                        DocumentTextResult.ResultType.HEBREW_TEXT,
                    ]
                )

                # If any expected result is NEEDS_REVIEW -> ACTION_REQUIRED
                any_needs_review = doc.text_results.filter(
                    result_type__in=expected_types,
                    status=DocumentTextResult.Status.NEEDS_REVIEW,
                ).exists()

                if any_needs_review:
                    doc.processing_state_user = Document.ProcessingState.ACTION_REQUIRED
                else:
                    # Count succeeded/failed across expected outputs
                    succeeded = doc.text_results.filter(
                        result_type__in=expected_types,
                        status=DocumentTextResult.Status.SUCCEEDED,
                    ).count()
                    failed = doc.text_results.filter(
                        result_type__in=expected_types,
                        status=DocumentTextResult.Status.FAILED,
                    ).count()

                    if succeeded == len(expected_types):
                        doc.processing_state_user = Document.ProcessingState.READY
                    elif failed == len(expected_types):
                        doc.processing_state_user = Document.ProcessingState.FAILED
                    else:
                        doc.processing_state_user = Document.ProcessingState.PARTIAL

                doc.save(update_fields=["processing_state_user"])

        except Document.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"[run_worker] Document {document_id} not found"))
            return True  # delete message; job is stale
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] processing failed: {e}"))
            return False

        self.stdout.write(self.style.SUCCESS(f"[run_worker] document {document_id} processed; state={doc.processing_state_user}"))
        return True
