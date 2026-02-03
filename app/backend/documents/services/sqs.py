import json
import os
from typing import Any, Dict

import boto3


def _required_env(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def send_process_document_message(document_id: int) -> None:
    queue_url = _required_env("SQS_QUEUE_URL")
    region = (
        os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "eu-central-1"
    )

    sqs = boto3.client("sqs", region_name=region)
    payload: Dict[str, Any] = {"type": "PROCESS_DOCUMENT", "document_id": document_id}
    sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(payload))
