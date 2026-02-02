import json
import os
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from documents.models import Document, DocumentTextResult

# NOTE:
# This command is meant to run inside the ECS "worker" container.
# It long-polls SQS for jobs and processes them one by one.


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


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
        region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-central-1"

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
            else:
                # Do not delete; let SQS visibility timeout expire and retry.
                # (Later we can add a DLQ policy / max receives.)
                pass

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
            self.stderr.write(self.style.ERROR("[run_worker] missing ReceiptHandle; cannot delete"))
            return
        try:
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt)
            self.stdout.write(self.style.SUCCESS("[run_worker] deleted message from SQS"))
        except (BotoCoreError, ClientError) as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] SQS delete failed: {e}"))

    def _process_message(self, msg: Dict[str, Any]) -> bool:
        body = msg.get("Body", "")
        try:
            payload = json.loads(body)
        except Exception:
            self.stderr.write(self.style.ERROR(f"[run_worker] invalid JSON body: {body!r}"))
            return False

        job_type = payload.get("type")
        if job_type != "PROCESS_DOCUMENT":
            self.stderr.write(self.style.ERROR(f"[run_worker] unknown job type: {job_type!r}"))
            return False

        document_id = payload.get("document_id")
        if not isinstance(document_id, int):
            self.stderr.write(self.style.ERROR("[run_worker] missing/invalid document_id"))
            return False

        self.stdout.write(f"[run_worker] processing document_id={document_id}")

        # Slice 1 behavior: dummy processing.
        # We mark the doc as READY (or FAILED) and persist a simple marker.
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

                # mark processing → ready (dummy slice 1)
                doc.processing_state_user = Document.ProcessingState.PROCESSING
                doc.save(update_fields=["processing_state_user"])

                DocumentTextResult.objects.create(
                    document=doc,
                    result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
                    engine="engine_v1",
                    status=DocumentTextResult.Status.SUCCEEDED,
                    verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
                    text=f"dummy source text for document_id={doc.id}",
                )

                doc.processing_state_user = Document.ProcessingState.READY
                doc.save(update_fields=["processing_state_user"])

        except Document.DoesNotExist:
            self.stderr.write(self.style.ERROR(f"[run_worker] Document {document_id} not found"))
            return True  # delete message; job is stale
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"[run_worker] processing failed: {e}"))
            return False

        self.stdout.write(self.style.SUCCESS(f"[run_worker] document {document_id} marked READY"))
        return True
