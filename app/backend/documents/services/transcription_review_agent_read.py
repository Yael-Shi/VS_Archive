"""Read-only Pilot 01 transcription-review agent scan and review-bundle APIs.

Discovery scan returns EN/FR OCR documents as DISCOVERY candidates only.
The review-bundle endpoint classifies a single document as REVIEWABLE,
NON_ELIGIBLE, or BLOCKING_INTEGRITY_ERROR using existing access helpers and
``resolve_displayed_transcription_result``.

External-agent cursor note (not implemented here): on BLOCKING_INTEGRITY_ERROR
the agent STOP and must NOT advance ``last_scanned_document_id`` past the
blocked document. 404 and NON_ELIGIBLE may advance the cursor. Discovery
exhaustion with fewer than five REVIEWABLE documents yields a short batch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db.models import QuerySet

from documents.models import (
    ArchiveItem,
    Document,
    DocumentSourceFile,
    DocumentTextResult,
)
from documents.s3 import create_presigned_get
from documents.services.document_access import (
    document_queryset_for_user,
    get_viewable_document,
)
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.source_files import (
    is_multi_image_document,
    ordered_source_files_for_document,
)
from documents.services.text_presentation import resolve_displayed_transcription_result

logger = logging.getLogger(__name__)

SCAN_SCHEMA_VERSION = "transcription-review-scan-v1"
BUNDLE_SCHEMA_VERSION = "transcription-review-bundle-v1"

OUTCOME_REVIEWABLE = "REVIEWABLE"
OUTCOME_NON_ELIGIBLE = "NON_ELIGIBLE"
OUTCOME_BLOCKING = "BLOCKING_INTEGRITY_ERROR"

PILOT_LANGUAGES = frozenset(
    {
        Document.Language.ENGLISH,
        Document.Language.FRENCH,
    }
)

DEFAULT_SCAN_LIMIT = 50
MIN_SCAN_LIMIT = 1
MAX_SCAN_LIMIT = 200
PRESIGNED_GET_EXPIRY_SECONDS = 3600

CODE_LANGUAGE_NOT_IN_SCOPE = "LANGUAGE_NOT_IN_SCOPE"
CODE_NOT_OCR_DOCUMENT = "NOT_OCR_DOCUMENT"
CODE_NOT_READY_UPLOAD = "NOT_READY_UPLOAD"
CODE_NOT_READY_PROCESSING = "NOT_READY_PROCESSING"
CODE_VERIFIED_TRANSCRIPTION_OUT_OF_SCOPE = "VERIFIED_TRANSCRIPTION_OUT_OF_SCOPE"
CODE_MISSING_DISPLAYED_TRANSCRIPTION = "MISSING_DISPLAYED_TRANSCRIPTION"
CODE_INCOMPLETE_MULTI_IMAGE_SOURCES = "INCOMPLETE_MULTI_IMAGE_SOURCES"
CODE_MISSING_SOURCE_FILE = "MISSING_SOURCE_FILE"
CODE_MISSING_PRIMARY_SOURCE = "MISSING_PRIMARY_SOURCE"
CODE_UPLOADED_DISPLAY_ONLY_SOURCE_MISSING_KEY = (
    "UPLOADED_DISPLAY_ONLY_SOURCE_MISSING_KEY"
)
CODE_UNSUPPORTED_SOURCE_MIME = "UNSUPPORTED_SOURCE_MIME"
CODE_SOURCE_URL_UNAVAILABLE = "SOURCE_URL_UNAVAILABLE"

SOURCE_KIND_SOURCE_FILE = "SOURCE_FILE"
SOURCE_KIND_PRIMARY_FILE = "PRIMARY_FILE"


class ScanQueryError(ValueError):
    """Invalid scan query parameters."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _IntegrityFailure:
    code: str
    message: str
    details: dict[str, Any] | None = None


def parse_scan_query(
    *,
    after_id_raw: str | None,
    limit_raw: str | None,
) -> tuple[int, int]:
    """Parse ``after_id`` / ``limit``; raise ``ScanQueryError`` on invalid input."""
    if after_id_raw is None or str(after_id_raw).strip() == "":
        after_id = 0
    else:
        try:
            after_id = int(str(after_id_raw).strip())
        except (TypeError, ValueError) as exc:
            raise ScanQueryError(
                "invalid_after_id",
                "after_id must be a non-negative integer",
            ) from exc
        if after_id < 0:
            raise ScanQueryError(
                "invalid_after_id",
                "after_id must be a non-negative integer",
            )

    if limit_raw is None or str(limit_raw).strip() == "":
        limit = DEFAULT_SCAN_LIMIT
    else:
        try:
            limit = int(str(limit_raw).strip())
        except (TypeError, ValueError) as exc:
            raise ScanQueryError(
                "invalid_limit",
                f"limit must be between {MIN_SCAN_LIMIT} and {MAX_SCAN_LIMIT}",
            ) from exc
        if limit < MIN_SCAN_LIMIT or limit > MAX_SCAN_LIMIT:
            raise ScanQueryError(
                "invalid_limit",
                f"limit must be between {MIN_SCAN_LIMIT} and {MAX_SCAN_LIMIT}",
            )

    return after_id, limit


def discovery_queryset_for_user(user) -> QuerySet[Document]:
    """EN/FR OCR_DOCUMENT rows the caller may view, unordered (caller orders)."""
    return (
        document_queryset_for_user(user)
        .filter(
            language__in=sorted(PILOT_LANGUAGES),
            archive_item__item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        )
        .select_related("archive_item")
    )


def build_scan_response(*, user, after_id: int, limit: int) -> dict[str, Any]:
    """Return DISCOVERY candidates only; no transcription or source work."""
    qs = discovery_queryset_for_user(user).filter(id__gt=after_id).order_by("id")
    docs = list(qs[:limit])
    items = [_serialize_discovery_item(doc) for doc in docs]
    if items:
        next_after_id = items[-1]["document_id"]
        exhausted = False
    else:
        next_after_id = None
        exhausted = True
    return {
        "schema_version": SCAN_SCHEMA_VERSION,
        "after_id": after_id,
        "limit": limit,
        "next_after_id": next_after_id,
        "exhausted": exhausted,
        "items": items,
    }


def _serialize_discovery_item(doc: Document) -> dict[str, Any]:
    item = doc.archive_item
    return {
        "document_id": doc.id,
        "archive_item_id": doc.archive_item_id,
        "language": doc.language,
        "doc_type": doc.doc_type,
        "item_type": item.item_type if item is not None else None,
        "upload_status": doc.upload_status,
        "processing_state_user": doc.processing_state_user,
        "visibility": item.visibility if item is not None else None,
        "expected_source_file_count": doc.expected_source_file_count,
    }


def _document_payload(doc: Document) -> dict[str, Any]:
    item = doc.archive_item
    return {
        "document_id": doc.id,
        "archive_item_id": doc.archive_item_id,
        "language": doc.language,
        "doc_type": doc.doc_type,
        "item_type": item.item_type if item is not None else None,
        "upload_status": doc.upload_status,
        "processing_state_user": doc.processing_state_user,
        "visibility": item.visibility if item is not None else None,
        "expected_source_file_count": doc.expected_source_file_count,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


def _serialize_transcription(row: DocumentTextResult) -> dict[str, Any]:
    text = row.text if row.text is not None else ""
    return {
        "text_result_id": row.id,
        "result_type": row.result_type,
        "status": row.status,
        "verification_status": row.verification_status,
        "engine_key": row.engine_key,
        "engine": row.engine,
        "source_revision": row.source_revision,
        "based_on_source_revision": row.based_on_source_revision,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "text": text,
        "text_sha256": source_text_sha256(text),
    }


def _error_payload(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return body


def _non_eligible(
    doc: Document,
    *,
    code: str,
    message: str,
    transcription: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "outcome": OUTCOME_NON_ELIGIBLE,
        "document": _document_payload(doc),
        "error": _error_payload(code, message),
        "transcription": transcription,
        "source_pages": [],
    }


def _blocking(
    doc: Document,
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    transcription: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "outcome": OUTCOME_BLOCKING,
        "document": _document_payload(doc),
        "error": _error_payload(code, message, details),
        "transcription": transcription,
        "source_pages": [],
    }


def _uploads_bucket() -> str:
    bucket_raw = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    if not isinstance(bucket_raw, str):
        return ""
    return bucket_raw.strip()


def _is_image_mime(mime_type: str) -> bool:
    return (mime_type or "").strip().lower().startswith("image/")


def _is_pdf_mime(mime_type: str) -> bool:
    return (mime_type or "").strip().lower() == "application/pdf"


def _presign_get(bucket: str, key: str) -> str:
    return create_presigned_get(
        bucket=bucket,
        key=key,
        expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
    )


def _build_multi_image_source_pages(
    doc: Document, *, bucket: str
) -> tuple[list[dict[str, Any]] | None, _IntegrityFailure | None]:
    expected = doc.expected_source_file_count
    assert expected is not None and expected >= 2

    by_index = {row.order_index: row for row in ordered_source_files_for_document(doc)}
    missing_or_not_uploaded: list[int] = []
    for order_index in range(expected):
        row = by_index.get(order_index)
        if row is None:
            missing_or_not_uploaded.append(order_index)
            continue
        if row.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            missing_or_not_uploaded.append(order_index)
            continue
        if not (row.file_s3_key or "").strip():
            missing_or_not_uploaded.append(order_index)

    if missing_or_not_uploaded:
        # Distinguish wholly missing rows vs incomplete upload status.
        wholly_missing = [idx for idx in missing_or_not_uploaded if idx not in by_index]
        if wholly_missing and len(wholly_missing) == len(missing_or_not_uploaded):
            return None, _IntegrityFailure(
                code=CODE_MISSING_SOURCE_FILE,
                message="Expected source file row is missing",
                details={
                    "expected_source_file_count": expected,
                    "missing_or_not_uploaded_order_indexes": missing_or_not_uploaded,
                },
            )
        return None, _IntegrityFailure(
            code=CODE_INCOMPLETE_MULTI_IMAGE_SOURCES,
            message="Expected source file not uploaded",
            details={
                "expected_source_file_count": expected,
                "missing_or_not_uploaded_order_indexes": missing_or_not_uploaded,
            },
        )

    # Display set: expected range plus additional UPLOADED rows after it.
    # PENDING/FAILED extras stay outside the display set. An UPLOADED extra
    # with an empty key is a blocking integrity defect (not a silent omit).
    display_rows: list[DocumentSourceFile] = []
    for order_index in range(expected):
        display_rows.append(by_index[order_index])
    extras: list[DocumentSourceFile] = []
    for row in ordered_source_files_for_document(doc):
        if row.order_index < expected:
            continue
        if row.upload_status != DocumentSourceFile.UploadStatus.UPLOADED:
            continue
        if not (row.file_s3_key or "").strip():
            return None, _IntegrityFailure(
                code=CODE_UPLOADED_DISPLAY_ONLY_SOURCE_MISSING_KEY,
                message="Uploaded display-only source file has an empty S3 key",
                details={"order_index": row.order_index},
            )
        extras.append(row)
    display_rows.extend(extras)

    if not bucket:
        return None, _IntegrityFailure(
            code=CODE_SOURCE_URL_UNAVAILABLE,
            message="Uploads bucket is not configured",
        )

    pages: list[dict[str, Any]] = []
    for row in display_rows:
        mime = (row.mime_type or "").strip()
        if not _is_image_mime(mime):
            return None, _IntegrityFailure(
                code=CODE_UNSUPPORTED_SOURCE_MIME,
                message="Source mime_type is not supported for review bundle",
                details={"order_index": row.order_index, "mime_type": mime},
            )
        try:
            url = _presign_get(bucket, row.file_s3_key)
        except Exception:
            logger.exception(
                "transcription review agent presign failed",
                extra={
                    "document_id": doc.id,
                    "order_index": row.order_index,
                },
            )
            return None, _IntegrityFailure(
                code=CODE_SOURCE_URL_UNAVAILABLE,
                message="Failed to create source download URL",
                details={"order_index": row.order_index},
            )
        pages.append(
            {
                "source_file_id": row.id,
                "order_index": row.order_index,
                "display_number": row.order_index + 1,
                "source_kind": SOURCE_KIND_SOURCE_FILE,
                "file_s3_key": row.file_s3_key,
                "mime_type": mime,
                "size_bytes": row.size_bytes,
                "include_in_ocr": row.include_in_ocr,
                "upload_status": row.upload_status,
                "download_url": url,
                "download_url_expires_in": PRESIGNED_GET_EXPIRY_SECONDS,
            }
        )
    return pages, None


def _build_single_file_source_pages(
    doc: Document, *, bucket: str
) -> tuple[list[dict[str, Any]] | None, _IntegrityFailure | None]:
    key = (doc.file_s3_key or "").strip()
    if not key:
        return None, _IntegrityFailure(
            code=CODE_MISSING_PRIMARY_SOURCE,
            message="Document.file_s3_key is missing",
        )

    mime = (doc.mime_type or "").strip()
    if doc.doc_type == Document.DocType.PDF:
        if not _is_pdf_mime(mime):
            # Allow empty mime only when doc_type is PDF? Contract: PDF => application/pdf.
            if mime and not _is_pdf_mime(mime):
                return None, _IntegrityFailure(
                    code=CODE_UNSUPPORTED_SOURCE_MIME,
                    message="Source mime_type is not supported for review bundle",
                    details={"order_index": 0, "mime_type": mime},
                )
            if not mime:
                mime = "application/pdf"
    elif doc.doc_type == Document.DocType.IMAGE:
        if not _is_image_mime(mime):
            return None, _IntegrityFailure(
                code=CODE_UNSUPPORTED_SOURCE_MIME,
                message="Source mime_type is not supported for review bundle",
                details={"order_index": 0, "mime_type": mime},
            )
    else:
        if not (_is_image_mime(mime) or _is_pdf_mime(mime)):
            return None, _IntegrityFailure(
                code=CODE_UNSUPPORTED_SOURCE_MIME,
                message="Source mime_type is not supported for review bundle",
                details={"order_index": 0, "mime_type": mime},
            )

    if not bucket:
        return None, _IntegrityFailure(
            code=CODE_SOURCE_URL_UNAVAILABLE,
            message="Uploads bucket is not configured",
        )

    try:
        url = _presign_get(bucket, key)
    except Exception:
        logger.exception(
            "transcription review agent primary presign failed",
            extra={"document_id": doc.id},
        )
        return None, _IntegrityFailure(
            code=CODE_SOURCE_URL_UNAVAILABLE,
            message="Failed to create source download URL",
            details={"order_index": 0},
        )

    mirrored = (
        DocumentSourceFile.objects.filter(document=doc, order_index=0)
        .only("id")
        .first()
    )
    return (
        [
            {
                "source_file_id": mirrored.id if mirrored is not None else None,
                "order_index": 0,
                "display_number": 1,
                "source_kind": SOURCE_KIND_PRIMARY_FILE,
                "file_s3_key": key,
                "mime_type": mime,
                "size_bytes": doc.size_bytes,
                "include_in_ocr": True,
                "upload_status": DocumentSourceFile.UploadStatus.UPLOADED,
                "download_url": url,
                "download_url_expires_in": PRESIGNED_GET_EXPIRY_SECONDS,
            }
        ],
        None,
    )


def build_review_bundle_for_document(doc: Document) -> dict[str, Any]:
    """Classify one viewable document and build the review-bundle payload."""
    language = (doc.language or "").strip()
    if language not in PILOT_LANGUAGES:
        return _non_eligible(
            doc,
            code=CODE_LANGUAGE_NOT_IN_SCOPE,
            message="Document language is not en or fr",
        )

    item = doc.archive_item
    if item is None or item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        return _non_eligible(
            doc,
            code=CODE_NOT_OCR_DOCUMENT,
            message="Document is not an OCR_DOCUMENT archive item",
        )

    if doc.upload_status != Document.UploadStatus.UPLOADED:
        return _non_eligible(
            doc,
            code=CODE_NOT_READY_UPLOAD,
            message="Document upload is not complete",
        )

    displayed = resolve_displayed_transcription_result(doc)
    has_displayed = displayed is not None and bool((displayed.text or "").strip())

    if (
        doc.processing_state_user
        in (
            Document.ProcessingState.PROCESSING,
            Document.ProcessingState.RECOVERY_REQUIRED,
        )
        and not has_displayed
    ):
        return _non_eligible(
            doc,
            code=CODE_NOT_READY_PROCESSING,
            message=("Document is still processing and has no displayed transcription"),
        )

    if has_displayed and displayed is not None:
        if (
            displayed.verification_status
            == DocumentTextResult.VerificationStatus.VERIFIED
        ):
            # Cheap eligibility exit: do not presign sources for VERIFIED rows.
            return _non_eligible(
                doc,
                code=CODE_VERIFIED_TRANSCRIPTION_OUT_OF_SCOPE,
                message=(
                    "Displayed transcription is VERIFIED and out of Pilot 01 scope"
                ),
                transcription=_serialize_transcription(displayed),
            )

    if not has_displayed:
        # Readiness gate passed (not PROCESSING/RECOVERY_REQUIRED without text).
        # FAILED or READY/PARTIAL with no text is a blocking integrity defect.
        return _blocking(
            doc,
            code=CODE_MISSING_DISPLAYED_TRANSCRIPTION,
            message=(
                "No displayed transcription from resolve_displayed_transcription_result"
            ),
        )

    assert displayed is not None
    transcription_payload = _serialize_transcription(displayed)
    bucket = _uploads_bucket()

    if is_multi_image_document(doc):
        pages, failure = _build_multi_image_source_pages(doc, bucket=bucket)
    else:
        pages, failure = _build_single_file_source_pages(doc, bucket=bucket)

    if failure is not None:
        return _blocking(
            doc,
            code=failure.code,
            message=failure.message,
            details=failure.details,
            transcription=transcription_payload,
        )

    assert pages is not None
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "outcome": OUTCOME_REVIEWABLE,
        "document": _document_payload(doc),
        "transcription": transcription_payload,
        "source_pages": pages,
        "provenance": {
            "displayed_transcription_resolver": (
                "resolve_displayed_transcription_result"
            ),
            "source_set": (
                "display_physical" if is_multi_image_document(doc) else "primary_file"
            ),
            "presign_expires_in": PRESIGNED_GET_EXPIRY_SECONDS,
        },
    }


def get_viewable_document_for_review_bundle(user, document_id: int) -> Document:
    """Load a document the user may view, or raise Http404."""
    return get_viewable_document(
        user,
        document_id,
        queryset=Document.objects.select_related("archive_item").prefetch_related(
            "text_results",
            "source_files",
        ),
    )


def build_review_bundle_response(*, user, document_id: int) -> dict[str, Any]:
    """Load + classify; raises Http404 when missing or not viewable."""
    doc = get_viewable_document_for_review_bundle(user, document_id)
    return build_review_bundle_for_document(doc)


__all__ = [
    "ScanQueryError",
    "parse_scan_query",
    "build_scan_response",
    "build_review_bundle_response",
    "build_review_bundle_for_document",
    "get_viewable_document_for_review_bundle",
    "OUTCOME_REVIEWABLE",
    "OUTCOME_NON_ELIGIBLE",
    "OUTCOME_BLOCKING",
]
