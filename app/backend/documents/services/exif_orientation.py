from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps

from documents.s3 import get_object_bytes, put_object_bytes
from documents.services.upload_validation import (
    ALLOWED_IMAGE_MIMES,
    normalize_upload_mime_type,
)

_EXIF_ORIENTATION_TAG = 274

_MIME_TO_PIL_FORMAT: dict[str, str] = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/tiff": "TIFF",
    "image/webp": "WEBP",
}


class ExifNormalizationError(Exception):
    """Raised when EXIF orientation normalization cannot be completed."""


@dataclass(frozen=True)
class ExifNormalizationResult:
    rewritten: bool
    size_bytes: Optional[int] = None
    normalized_bytes: Optional[bytes] = None


def is_upload_image_mime(mime_type: str) -> bool:
    return normalize_upload_mime_type(mime_type) in ALLOWED_IMAGE_MIMES


def _read_exif_orientation(image: Image.Image) -> Optional[int]:
    exif = image.getexif()
    if not exif:
        return None
    orientation = exif.get(_EXIF_ORIENTATION_TAG)
    if orientation is None:
        return None
    try:
        return int(orientation)
    except (TypeError, ValueError):
        return None


def _encode_normalized_image(image: Image.Image, mime_type: str) -> bytes:
    pil_format = _MIME_TO_PIL_FORMAT.get(mime_type)
    if pil_format is None:
        raise ExifNormalizationError(f"unsupported image mime type: {mime_type}")

    buffer = BytesIO()
    save_kwargs: dict = {"format": pil_format}
    if pil_format == "JPEG":
        save_kwargs["quality"] = 95
    elif pil_format == "WEBP":
        save_kwargs["quality"] = 90

    image.save(buffer, **save_kwargs)
    return buffer.getvalue()


def normalize_image_bytes_exif_orientation(
    image_bytes: bytes,
    mime_type: str,
) -> ExifNormalizationResult:
    """
    Physically apply EXIF orientation when required.

    Returns rewritten=False without re-encoding when orientation is missing or 1.
    """
    normalized_mime = normalize_upload_mime_type(mime_type)
    if normalized_mime not in ALLOWED_IMAGE_MIMES:
        return ExifNormalizationResult(rewritten=False)

    try:
        with Image.open(BytesIO(image_bytes)) as image:
            orientation = _read_exif_orientation(image)
            if orientation in (None, 1):
                return ExifNormalizationResult(rewritten=False)

            transposed = ImageOps.exif_transpose(image)
            normalized_bytes = _encode_normalized_image(transposed, normalized_mime)
    except ExifNormalizationError:
        raise
    except Exception as exc:
        raise ExifNormalizationError(
            "failed to normalize image EXIF orientation"
        ) from exc

    return ExifNormalizationResult(
        rewritten=True,
        size_bytes=len(normalized_bytes),
        normalized_bytes=normalized_bytes,
    )


def normalize_uploaded_image_exif_in_s3(
    *,
    bucket: str,
    key: str,
    mime_type: str,
) -> ExifNormalizationResult:
    """
    Download an uploaded image from S3, normalize EXIF orientation when needed,
    and overwrite the object at the same key when a transform is required.
    """
    normalized_mime = normalize_upload_mime_type(mime_type)
    if normalized_mime not in ALLOWED_IMAGE_MIMES:
        return ExifNormalizationResult(rewritten=False)

    try:
        image_bytes, content_type = get_object_bytes(bucket, key)
    except (BotoCoreError, ClientError) as exc:
        raise ExifNormalizationError("failed to download image from s3") from exc

    result = normalize_image_bytes_exif_orientation(image_bytes, normalized_mime)
    if not result.rewritten:
        return result

    assert result.normalized_bytes is not None

    stored_content_type = (
        normalize_upload_mime_type(content_type or "")
        if content_type
        else normalized_mime
    )
    if stored_content_type not in ALLOWED_IMAGE_MIMES:
        stored_content_type = normalized_mime

    try:
        put_object_bytes(
            bucket=bucket,
            key=key,
            body=result.normalized_bytes,
            content_type=stored_content_type,
        )
    except (BotoCoreError, ClientError) as exc:
        raise ExifNormalizationError("failed to upload normalized image to s3") from exc

    return result
