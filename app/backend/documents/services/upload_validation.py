from __future__ import annotations

from typing import Optional

ALLOWED_IMAGE_MIMES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/webp",
    }
)

ALLOWED_IMAGE_EXTENSIONS = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".tif",
        ".tiff",
        ".webp",
    }
)

PDF_MIME = "application/pdf"
PDF_EXTENSION = ".pdf"

MIME_TO_EXTENSIONS: dict[str, frozenset[str]] = {
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "image/tiff": frozenset({".tif", ".tiff"}),
    "image/webp": frozenset({".webp"}),
    PDF_MIME: frozenset({PDF_EXTENSION}),
}


def _normalize_mime_type(mime_type: str) -> str:
    return (mime_type or "").strip().lower().split(";", 1)[0].strip()


def file_extension(original_name: str) -> str:
    name = (original_name or "").strip().lower()
    if not name or "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[-1]


def _field_label(field_prefix: str, field: str) -> str:
    if field_prefix:
        return f"{field_prefix}.{field}"
    return field


def validate_image_upload_metadata(
    *,
    mime_type: str,
    original_name: str,
    field_prefix: str = "",
) -> Optional[str]:
    """
    Validate allowed image MIME + extension and that they match.
    Returns an error message suitable for a 400 response, or None if valid.
    """
    mime = _normalize_mime_type(mime_type)
    ext = file_extension(original_name)

    if mime not in ALLOWED_IMAGE_MIMES:
        return (
            f"{_field_label(field_prefix, 'mime_type')} must be one of: "
            f"{', '.join(sorted(ALLOWED_IMAGE_MIMES))}"
        )

    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return (
            f"{_field_label(field_prefix, 'original_name')} must have an allowed image extension: "
            f"{', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"
        )

    allowed_for_mime = MIME_TO_EXTENSIONS.get(mime, frozenset())
    if ext not in allowed_for_mime:
        return (
            f"{_field_label(field_prefix, 'mime_type')} does not match "
            f"{_field_label(field_prefix, 'original_name')} extension"
        )

    return None


def validate_allowed_image_mime(
    mime_type: str,
    *,
    field_prefix: str = "",
) -> Optional[str]:
    """
    Validate MIME is on the image allowlist without checking filename extension.
    """
    mime = _normalize_mime_type(mime_type)
    if mime not in ALLOWED_IMAGE_MIMES:
        return (
            f"{_field_label(field_prefix, 'mime_type')} must be one of: "
            f"{', '.join(sorted(ALLOWED_IMAGE_MIMES))}"
        )
    return None


def validate_single_file_upload_metadata(
    *,
    doc_type: str,
    mime_type: str,
    original_name: str,
) -> Optional[str]:
    """
    Validate legacy single-file upload metadata before presigned URL issuance.
    """
    normalized_doc_type = (doc_type or "").strip().upper()

    if normalized_doc_type == "IMAGE":
        return validate_image_upload_metadata(
            mime_type=mime_type,
            original_name=original_name,
        )

    if normalized_doc_type == "PDF":
        mime = _normalize_mime_type(mime_type)
        ext = file_extension(original_name)

        if mime != PDF_MIME:
            return f"mime_type must be {PDF_MIME} for PDF uploads"

        if ext != PDF_EXTENSION:
            return f"original_name must have extension {PDF_EXTENSION} for PDF uploads"

        return None

    return "doc_type must be PDF or IMAGE"
