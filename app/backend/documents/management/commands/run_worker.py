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
        gcp_json = os.environ.get("GCP_SA_JSON")
        self.stdout.write(
            f"[run_worker] gcp_sa_json_present={bool(gcp_json)} gcp_sa_json_len={len(gcp_json) if gcp_json else 0}"
        )

        gcp_json = os.environ.get("GCP_SA_JSON")
        if gcp_json:
            creds_path = Path("/tmp/gcp-sa.json")
            creds_path.write_text(gcp_json, encoding="utf-8")
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(creds_path)
            self.stdout.write(
                f"[run_worker] wrote_gcp_creds_file={creds_path} size={creds_path.stat().st_size}"
            )
        else:
            self.stdout.write(
                "[run_worker] no GCP_SA_JSON; Google Vision will not work"
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
                self.stdout.write(
                    "[run_worker] processed one message; exiting (--once)."
                )
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
                VisibilityTimeout=60,
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
            self.stderr.write(
                self.style.ERROR(
                    f"[run_worker] unknown job type: {payload.get('type')!r}"
                )
            )
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
                    return True  # delete message

                # mark as processing
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])

                is_he = _is_hebrew_language(doc.language)

                # --- Fetch file bytes from S3 ---
                bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
                if not bucket:
                    raise RuntimeError("UPLOADS_BUCKET_NAME is not configured")

                if not doc.file_s3_key:
                    raise RuntimeError(f"Document {doc.id} missing file_s3_key")

                file_bytes, s3_content_type = get_object_bytes(
                    bucket=bucket, key=doc.file_s3_key
                )
                effective_mime = (doc.mime_type or s3_content_type or "").strip()

                # --- Extract pages ---
                pages = extract_pages(file_bytes=file_bytes, mime_type=effective_mime)

                # --- Run HTR/OCR engine ---
                engine_name = "google_vision_v1"

                def _upsert_result(
                    *,
                    result_type: str,
                    status: str,
                    text: str | None,
                    error_code: str | None = None,
                    error_details: str | None = None,
                ) -> DocumentTextResult:
                    obj, _created = DocumentTextResult.objects.update_or_create(
                        document=doc,
                        result_type=result_type,
                        engine=engine_name,
                        defaults={
                            "status": status,
                            "verification_status": DocumentTextResult.VerificationStatus.UNVERIFIED,
                            "text": text,
                            "error_code": error_code,
                            "error_details": error_details,
                        },
                    )
                    return obj

                try:
                    htr = transcribe_pages(pages=pages, language_hint=doc.language)
                    status = (
                        DocumentTextResult.Status.NEEDS_REVIEW
                        if htr.needs_review
                        else DocumentTextResult.Status.SUCCEEDED
                    )

                    if is_he:
                        _upsert_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=status,
                            text=htr.text,
                        )
                        # MVP decision: when text is Hebrew, also mirror into SOURCE_TEXT
                        _upsert_result(
                            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                            status=status,
                            text=htr.text,
                        )
                    else:
                        _upsert_result(
                            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                            status=status,
                            text=htr.text,
                        )

                except Exception as e:
                    # OCR/HTR failed: record FAILED result (no placeholder)
                    if is_he:
                        _upsert_result(
                            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="OCR_FAILED",
                            error_details=str(e),
                        )
                    else:
                        _upsert_result(
                            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                            status=DocumentTextResult.Status.FAILED,
                            text=None,
                            error_code="OCR_FAILED",
                            error_details=str(e),
                        )
                    raise

                # --- Decide user-visible processing state ---
                expected_types = (
                    [DocumentTextResult.ResultType.HEBREW_TEXT]
                    if is_he
                    else [
                        DocumentTextResult.ResultType.SOURCE_TEXT,
                        DocumentTextResult.ResultType.HEBREW_TEXT,
                    ]
                )

                # Look at latest results per expected type/engine (we upsert so 1 row per type+engine)
                qs = doc.text_results.filter(
                    result_type__in=expected_types,
                    engine=engine_name,
                )

                any_needs_review = qs.filter(
                    status=DocumentTextResult.Status.NEEDS_REVIEW
                ).exists()

                if any_needs_review:
                    doc.processing_state_user = Document.ProcessingState.ACTION_REQUIRED
                else:
                    existing_count = qs.count()
                    succeeded = qs.filter(
                        status=DocumentTextResult.Status.SUCCEEDED
                    ).count()
                    failed = qs.filter(status=DocumentTextResult.Status.FAILED).count()
                    missing = len(expected_types) - existing_count

                    if missing == 0 and succeeded == len(expected_types):
                        doc.processing_state_user = Document.ProcessingState.READY
                    elif missing == 0 and failed == len(expected_types):
                        doc.processing_state_user = Document.ProcessingState.FAILED
                    else:
                        # Any mix of missing/succeeded/failed => PARTIAL
                        doc.processing_state_user = Document.ProcessingState.PARTIAL

                doc.save(update_fields=["processing_state_user"])

        except Document.DoesNotExist:
            self.stderr.write(
                self.style.ERROR(f"[run_worker] Document {document_id} not found")
            )
            return True  # delete message; stale job
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] processing failed: {e}"))
            return False

        self.stdout.write(
            self.style.SUCCESS(
                f"[run_worker] document {document_id} processed; state={doc.processing_state_user}"
            )
        )
        return True
