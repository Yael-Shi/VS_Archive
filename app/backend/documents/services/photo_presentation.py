"""Staff-facing Hebrew labels for PHOTO upload and archive renderability."""

from __future__ import annotations

from documents.models import PhotoContent

_PHOTO_UPLOAD_STATUS_LABELS: dict[str, str] = {
    PhotoContent.UploadStatus.PENDING.value: "ממתין להעלאה",
    PhotoContent.UploadStatus.UPLOADED.value: "הועלה",
    PhotoContent.UploadStatus.FAILED.value: "העלאה נכשלה",
}

_PHOTO_UPLOAD_STATUS_TONES: dict[str, str] = {
    PhotoContent.UploadStatus.PENDING.value: "badge-warn",
    PhotoContent.UploadStatus.UPLOADED.value: "badge-ok",
    PhotoContent.UploadStatus.FAILED.value: "badge-bad",
}

_ARCHIVE_RENDERABLE_LABEL = "מוצג בארכיון"
_ARCHIVE_NOT_RENDERABLE_LABEL = "לא מוצג בארכיון"
_ARCHIVE_RENDERABLE_TONE = "badge-ok"
_ARCHIVE_NOT_RENDERABLE_TONE = "badge-warn"


def photo_upload_status_label(photo_content: PhotoContent | None) -> str:
    """Human-readable Hebrew label for ``PhotoContent.upload_status``."""
    if photo_content is None:
        return ""
    key = str(photo_content.upload_status or "").strip()
    return _PHOTO_UPLOAD_STATUS_LABELS.get(key, key)


def photo_upload_status_tone(photo_content: PhotoContent | None) -> str:
    """Badge tone class for ``PhotoContent.upload_status``."""
    if photo_content is None:
        return ""
    key = str(photo_content.upload_status or "").strip()
    return _PHOTO_UPLOAD_STATUS_TONES.get(key, "")


def photo_is_archive_renderable(photo_content: PhotoContent | None) -> bool:
    """
    Whether PHOTO bytes are eligible for ``/archive/`` browse/detail surfaces.

    Matches ``filter_browse_renderable_photo_items`` upload/key checks only.
    Visibility and access rules are evaluated separately.
    """
    if photo_content is None:
        return False
    return (
        photo_content.upload_status == PhotoContent.UploadStatus.UPLOADED
        and bool((photo_content.original_file_key or "").strip())
    )


def photo_archive_renderability_label(photo_content: PhotoContent | None) -> str:
    """Staff-facing Hebrew label for archive browse/detail eligibility."""
    if photo_is_archive_renderable(photo_content):
        return _ARCHIVE_RENDERABLE_LABEL
    return _ARCHIVE_NOT_RENDERABLE_LABEL


def photo_archive_renderability_tone(photo_content: PhotoContent | None) -> str:
    """Badge tone class for archive browse/detail eligibility."""
    if photo_is_archive_renderable(photo_content):
        return _ARCHIVE_RENDERABLE_TONE
    return _ARCHIVE_NOT_RENDERABLE_TONE
