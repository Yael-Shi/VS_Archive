import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from typing import Tuple, Optional

_S3_NOT_FOUND_ERROR_CODES = frozenset({"404", "NoSuchKey", "NotFound"})


def get_s3_client():
    return boto3.client(
        "s3",
        region_name=getattr(settings, "AWS_REGION", None),
    )


def mime_type_to_extension(mime_type: str) -> str:
    mime = (mime_type or "").strip().lower()
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return mime.split("/", 1)[1] or "img"
    return "bin"


def build_document_source_file_s3_key(
    document_id: int,
    order_index: int,
    mime_type: str,
) -> str:
    ext = mime_type_to_extension(mime_type)
    return f"documents/{document_id}/source/{order_index}.{ext}"


def create_presigned_put(
    bucket: str,
    key: str,
    content_type: str,
    expires_in: int = 3600,
) -> str:
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires_in,
    )


def create_presigned_get(
    bucket: str,
    key: str,
    expires_in: int = 3600,
) -> str:
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        ClientMethod="get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": "inline",
        },
        ExpiresIn=expires_in,
    )


def s3_object_exists(bucket: str, key: str) -> bool:
    """
    Return True when the object exists in S3 (HeadObject succeeds).

    Raises BotoCoreError or ClientError for unexpected AWS/client failures.
    """
    s3 = get_s3_client()
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _S3_NOT_FOUND_ERROR_CODES:
            return False
        raise


def get_object_bytes(bucket: str, key: str) -> Tuple[bytes, Optional[str]]:
    """
    Download an object from S3 and return (bytes, content_type).
    """
    s3 = get_s3_client()
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    content_type = resp.get("ContentType")
    return body, content_type
