from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError
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


_PHOTO_MIME_TO_S3_EXTENSION: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/tiff": "tiff",
    "image/webp": "webp",
}


def photo_mime_to_s3_extension(mime_type: str) -> str:
    """Canonical S3 key extension for validated photo MIME types."""
    mime = (mime_type or "").strip().lower().split(";", 1)[0].strip()
    try:
        return _PHOTO_MIME_TO_S3_EXTENSION[mime]
    except KeyError as exc:
        raise ValueError(f"unsupported photo mime type: {mime_type!r}") from exc


def build_photo_original_s3_key(photo_content_id: int, mime_type: str) -> str:
    ext = photo_mime_to_s3_extension(mime_type)
    return f"photos/{photo_content_id}/original.{ext}"


def build_photo_thumbnail_s3_key(photo_content_id: int) -> str:
    """Deterministic private S3 key for a PHOTO JPEG thumbnail (max edge 400)."""
    return f"photos/{photo_content_id}/thumb_400.jpg"


def build_document_thumbnail_s3_key(document_id: int) -> str:
    """Deterministic private S3 key for an OCR document JPEG thumbnail (max edge 400)."""
    return f"documents/{document_id}/thumb_400.jpg"


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


@dataclass(frozen=True)
class S3HeadObjectResult:
    exists: bool
    content_type: Optional[str] = None
    content_length: Optional[int] = None


def head_s3_object(bucket: str, key: str) -> S3HeadObjectResult:
    """
    HeadObject for upload verification.

    Returns exists=False for missing keys. Raises BotoCoreError or ClientError
    for unexpected AWS/client failures.
    """
    s3 = get_s3_client()
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _S3_NOT_FOUND_ERROR_CODES:
            return S3HeadObjectResult(exists=False)
        raise

    content_type = resp.get("ContentType")
    if isinstance(content_type, str):
        content_type = content_type.strip() or None
    else:
        content_type = None

    content_length = resp.get("ContentLength")
    if content_length is not None:
        try:
            content_length = int(content_length)
        except (TypeError, ValueError):
            content_length = None
        if content_length is not None and content_length < 0:
            content_length = None

    return S3HeadObjectResult(
        exists=True,
        content_type=content_type,
        content_length=content_length,
    )


def s3_object_exists(bucket: str, key: str) -> bool:
    """
    Return True when the object exists in S3 (HeadObject succeeds).

    Raises BotoCoreError or ClientError for unexpected AWS/client failures.
    """
    return head_s3_object(bucket, key).exists


def get_object_bytes(bucket: str, key: str) -> Tuple[bytes, Optional[str]]:
    """
    Download an object from S3 and return (bytes, content_type).
    """
    s3 = get_s3_client()
    resp = s3.get_object(Bucket=bucket, Key=key)
    body = resp["Body"].read()
    content_type = resp.get("ContentType")
    return body, content_type


def put_object_bytes(
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
) -> int:
    """
    Upload bytes to S3 at the given key, returning the stored content length.
    """
    s3 = get_s3_client()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )
    return len(body)


@dataclass(frozen=True)
class S3DeleteObjectResult:
    deleted: bool
    not_found: bool = False


def delete_s3_object(bucket: str, key: str) -> S3DeleteObjectResult:
    """
    Delete an S3 object.

    Returns ``not_found=True`` when the key is already absent. Raises
    ``ClientError`` for unexpected AWS failures.
    """
    normalized_key = (key or "").strip()
    if not normalized_key:
        return S3DeleteObjectResult(deleted=False, not_found=True)

    s3 = get_s3_client()
    try:
        s3.delete_object(Bucket=bucket, Key=normalized_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _S3_NOT_FOUND_ERROR_CODES:
            return S3DeleteObjectResult(deleted=False, not_found=True)
        raise
    return S3DeleteObjectResult(deleted=True)
