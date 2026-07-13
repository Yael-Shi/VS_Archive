"""Generate PHOTO thumbnails from validated S3 originals (no OCR/worker)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps

from documents.models import PhotoContent
from documents.s3 import (
    build_photo_thumbnail_s3_key,
    get_object_bytes,
    put_object_bytes,
)

logger = logging.getLogger(__name__)

THUMBNAIL_MAX_EDGE = 400
THUMBNAIL_JPEG_MIME = "image/jpeg"
THUMBNAIL_JPEG_QUALITY = 85


class PhotoThumbnailError(Exception):
    """Internal failure generating or storing a photo thumbnail."""


@dataclass(frozen=True)
class PhotoThumbnailResult:
    width: int
    height: int
    thumbnail_file_key: str
    thumbnail_mime_type: str
    thumbnail_size_bytes: int


def compute_thumbnail_dimensions(
    width: int,
    height: int,
    *,
    max_edge: int = THUMBNAIL_MAX_EDGE,
) -> tuple[int, int]:
    """Return thumbnail (width, height) preserving aspect ratio within max_edge."""
    if width <= 0 or height <= 0:
        raise PhotoThumbnailError("invalid image dimensions")
    if width <= max_edge and height <= max_edge:
        return width, height
    if width >= height:
        new_width = max_edge
        new_height = max(1, round(height * max_edge / width))
    else:
        new_height = max_edge
        new_width = max(1, round(width * max_edge / height))
    return new_width, new_height


def _image_for_jpeg(image: Image.Image) -> Image.Image:
    """Convert PIL image modes to RGB suitable for JPEG encoding."""
    if image.mode in ("RGB", "L"):
        return image.convert("RGB") if image.mode == "L" else image
    if image.mode == "RGBA":
        background = Image.new("RGB", image.size, (255, 255, 255))
        background.paste(image, mask=image.split()[3])
        return background
    if image.mode == "P":
        if "transparency" in image.info:
            return _image_for_jpeg(image.convert("RGBA"))
        return image.convert("RGB")
    return image.convert("RGB")


def generate_photo_thumbnail_bytes(
    image_bytes: bytes,
    *,
    max_edge: int = THUMBNAIL_MAX_EDGE,
) -> tuple[bytes, int, int]:
    """
    Open image bytes, apply EXIF transpose, and encode a JPEG thumbnail.

    Returns (jpeg_bytes, transposed_width, transposed_height).
    """
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            transposed = ImageOps.exif_transpose(image)
            original_width, original_height = transposed.size
            thumb_width, thumb_height = compute_thumbnail_dimensions(
                original_width,
                original_height,
                max_edge=max_edge,
            )
            if (thumb_width, thumb_height) != transposed.size:
                resized = transposed.resize(
                    (thumb_width, thumb_height),
                    Image.Resampling.LANCZOS,
                )
            else:
                resized = transposed
            rgb = _image_for_jpeg(resized)
            buffer = BytesIO()
            rgb.save(buffer, format="JPEG", quality=THUMBNAIL_JPEG_QUALITY)
            jpeg_bytes = buffer.getvalue()
    except PhotoThumbnailError:
        raise
    except Exception as exc:
        raise PhotoThumbnailError("failed to generate photo thumbnail") from exc

    return jpeg_bytes, original_width, original_height


def generate_and_persist_photo_thumbnail(
    photo_content: PhotoContent,
    *,
    bucket: str,
) -> PhotoThumbnailResult | None:
    """
    Download the validated original, generate a JPEG thumbnail, upload to S3,
    and persist dimension/thumbnail metadata on ``photo_content``.

    Returns a result on success. On failure logs the exception and returns None
    without raising (callers must keep upload_status=UPLOADED).

    If S3 upload succeeds but DB metadata persistence fails, the deterministic
    thumbnail object may remain at ``thumbnail_file_key``. That orphan is safe
    for future retry/backfill because the key is idempotent per PhotoContent id.
    """
    original_key = (photo_content.original_file_key or "").strip()
    if not original_key:
        logger.warning(
            "photo thumbnail skipped: missing original_file_key",
            extra={"photo_content_id": photo_content.pk},
        )
        return None

    thumbnail_key = build_photo_thumbnail_s3_key(photo_content.pk)
    log_extra = {
        "photo_content_id": photo_content.pk,
        "original_file_key": original_key,
        "thumbnail_file_key": thumbnail_key,
    }

    try:
        image_bytes, _content_type = get_object_bytes(bucket, original_key)
        jpeg_bytes, width, height = generate_photo_thumbnail_bytes(image_bytes)
        thumbnail_size = put_object_bytes(
            bucket=bucket,
            key=thumbnail_key,
            body=jpeg_bytes,
            content_type=THUMBNAIL_JPEG_MIME,
        )
    except Exception:
        logger.exception("photo thumbnail generation failed", extra=log_extra)
        return None

    try:
        preserved_width = photo_content.width
        preserved_height = photo_content.height
        preserved_thumbnail_file_key = photo_content.thumbnail_file_key
        preserved_thumbnail_mime_type = photo_content.thumbnail_mime_type
        preserved_thumbnail_size_bytes = photo_content.thumbnail_size_bytes

        photo_content.width = width
        photo_content.height = height
        photo_content.thumbnail_file_key = thumbnail_key
        photo_content.thumbnail_mime_type = THUMBNAIL_JPEG_MIME
        photo_content.thumbnail_size_bytes = thumbnail_size
        photo_content.save(
            update_fields=[
                "width",
                "height",
                "thumbnail_file_key",
                "thumbnail_mime_type",
                "thumbnail_size_bytes",
                "updated_at",
            ]
        )
    except Exception:
        photo_content.width = preserved_width
        photo_content.height = preserved_height
        photo_content.thumbnail_file_key = preserved_thumbnail_file_key
        photo_content.thumbnail_mime_type = preserved_thumbnail_mime_type
        photo_content.thumbnail_size_bytes = preserved_thumbnail_size_bytes
        logger.exception(
            "photo thumbnail metadata persistence failed",
            extra=log_extra,
        )
        return None

    return PhotoThumbnailResult(
        width=width,
        height=height,
        thumbnail_file_key=thumbnail_key,
        thumbnail_mime_type=THUMBNAIL_JPEG_MIME,
        thumbnail_size_bytes=thumbnail_size,
    )
