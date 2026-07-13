"""Shared JPEG thumbnail generation from image bytes (EXIF-aware)."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

THUMBNAIL_MAX_EDGE = 400
THUMBNAIL_JPEG_MIME = "image/jpeg"
THUMBNAIL_JPEG_QUALITY = 85


class ImageThumbnailError(Exception):
    """Internal failure generating a JPEG thumbnail from image bytes."""


def compute_thumbnail_dimensions(
    width: int,
    height: int,
    *,
    max_edge: int = THUMBNAIL_MAX_EDGE,
) -> tuple[int, int]:
    """Return thumbnail (width, height) preserving aspect ratio within max_edge."""
    if width <= 0 or height <= 0:
        raise ImageThumbnailError("invalid image dimensions")
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


def generate_image_thumbnail_bytes(
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
    except ImageThumbnailError:
        raise
    except Exception as exc:
        raise ImageThumbnailError("failed to generate image thumbnail") from exc

    return jpeg_bytes, original_width, original_height
