import json
import logging
from datetime import date, datetime
from typing import Optional, TypedDict

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import (
    BooleanField,
    Case,
    Exists,
    IntegerField,
    OuterRef,
    Q,
    Value,
    When,
)
from django.db.models.functions import Length, Trim
from django.http import (
    Http404,
    HttpResponseBadRequest,
    JsonResponse,
    HttpResponseForbidden,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode
from django.views.decorators.http import require_POST

from .models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveMetadataSuggestion,
    Document,
    DocumentMetadata,
    DocumentSourceFile,
    DocumentTextResult,
    PhotoContent,
    Tag,
    TranscriptionEditSuggestion,
)
from documents.services.archive_catalog_metadata_validation import (
    parse_ocr_catalog_metadata_form,
)
from documents.services.archive_discovery_metadata_validation import (
    discovery_metadata_option_querysets,
    empty_discovery_metadata_form_fields,
    parse_archive_item_discovery_metadata_form,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    discovery_metadata_form_data_from_item,
    shared_archive_item_for_document,
    update_archive_item_discovery_metadata,
    update_manual_text_archive_item,
    update_photo_archive_item_metadata,
    update_ocr_document_catalog_metadata,
    update_ocr_document_metadata,
)
from botocore.exceptions import BotoCoreError, ClientError

from .s3 import (
    build_document_source_file_s3_key,
    create_presigned_get,
    create_presigned_put,
    head_s3_object,
    mime_type_to_extension,
)
from documents.services.review_backlog import (
    attach_review_summaries,
    documents_in_review_backlog,
    is_review_editable_text_result,
    is_review_pending_text_result,
    parse_review_reasons,
)
from documents.services.archive_metadata_validation import (
    parse_archive_metadata_form,
    parse_public_note,
    validate_source_metadata_fields,
)
from documents.services.upload_validation import (
    normalize_upload_mime_type,
    upload_mime_types_match,
    validate_image_upload_metadata,
    validate_single_file_upload_metadata,
)
from documents.services.source_files import (
    MULTI_IMAGE_MAX_FILES,
    all_expected_source_files_uploaded,
    build_source_preview,
    get_source_file_for_order,
    is_multi_image_document,
    mirror_primary_document_from_source_file,
    sync_primary_document_source_file,
)
from documents.services.document_access import (
    document_queryset_for_user,
    get_viewable_document,
    is_document_admin,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
    get_viewable_archive_item,
)
from documents.services.archive_metadata_suggestions import (
    SUGGESTION_CONTENT_REQUIRED_ERROR,
    format_current_metadata_labels,
    has_suggestion_content,
    normalize_suggestion_text,
)
from documents.services.archive_item_validation import (
    DATE_PRECISION_UI_CHOICES,
    TEXT_INPUT_TYPE_UI_CHOICES,
    parse_date_precision,
)
from documents.services.manual_text_validation import parse_manual_text_form
from documents.services.photo_upload import (
    create_photo_upload_plan,
    finalize_photo_upload,
    parse_create_photo_upload_metadata,
)
from documents.services.photo_metadata_validation import (
    empty_photo_metadata_form_data,
    photo_metadata_form_data_from_content,
    parse_photo_staff_metadata_form,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_TYPE_FILTER_CHOICES,
    archive_browse_displayable_text_results_prefetch,
    archive_manage_item_type_ui_choices,
    archive_metadata_status_ui_choices,
    archive_visibility_ui_choices,
    build_archive_browse_cards,
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
    normalize_archive_public_list_type_filter,
    normalize_archive_list_search_query,
)
from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    apply_ocr_reprocess,
    is_ocr_reprocess_ui_eligible,
)
from documents.services.hebrew_translation_retry import (
    HebrewTranslationRetryError,
    enqueue_hebrew_translation_retry,
    is_hebrew_translation_retry_ui_eligible,
)
from documents.services.sqs import send_process_document_message
from documents.services.text_presentation import (
    get_displayed_transcription_text,
    get_text_presentation_for_document,
    text_presentation_results_prefetch,
)
from documents.services.transcription_edit_suggestions import (
    IDENTICAL_TEXT_ERROR,
    NAME_REQUIRED_ERROR,
    SUGGESTED_TEXT_REQUIRED_ERROR,
    is_honeypot_triggered,
    normalize_transcription_text,
    render_transcription_diff_html,
    suggestion_status_label,
    texts_are_equivalent,
)
from documents.services.archive_metadata_suggestion_review import (
    ArchiveMetadataSuggestionReviewError,
    approve_suggestion as approve_archive_metadata_suggestion,
    reject_suggestion as reject_archive_metadata_suggestion,
)
from documents.services.transcription_suggestion_review import (
    TranscriptionSuggestionReviewError,
    approve_suggestion,
    reject_suggestion,
)
from documents.services.verified_text_result_edit import (
    VerifiedTextResultEditError,
    edit_verified_text_result,
    is_hebrew_translation_stale,
    is_verified_editable_text_result,
)

logger = logging.getLogger(__name__)

ARCHIVE_ITEM_TYPE_MANUAL_TEXT = "manual_text"
ARCHIVE_ITEM_TYPE_OCR_DOCUMENT = "ocr_document"
ARCHIVE_ITEM_TYPE_PHOTO = "photo"
_VALID_ARCHIVE_ITEM_CREATE_TYPES = frozenset(
    {
        ARCHIVE_ITEM_TYPE_MANUAL_TEXT,
        ARCHIVE_ITEM_TYPE_OCR_DOCUMENT,
        ARCHIVE_ITEM_TYPE_PHOTO,
    }
)
DEFAULT_PAGE_SIZE = 50
PRESIGNED_GET_EXPIRY_SECONDS = 3600


def _bad(msg: str):
    return HttpResponseBadRequest(msg)


class _ParsedImageFileMeta(TypedDict):
    original_name: str
    mime_type: str
    size_bytes: int | None


class _CreateUploadCommon(TypedDict):
    title: str
    date_start: date | None
    date_end: date | None
    date_precision: str
    language: str | None
    text_input_type: str
    visibility: str
    admin_meta: dict
    author_name: str
    source_title: str
    public_note: str
    discovery_metadata: dict


def _parse_int(value, default, min_value=None, max_value=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if min_value is not None:
        n = max(min_value, n)
    if max_value is not None:
        n = min(max_value, n)
    return n


def _parse_date_optional(value: Optional[str], field_name: str):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid {field_name} format, expected YYYY-MM-DD")


def _parse_text_input_type(raw_value: Optional[str]) -> str:
    value = (raw_value or "").strip().upper()
    valid = {
        Document.TextInputType.HANDWRITTEN,
        Document.TextInputType.PRINTED,
    }
    if value not in valid:
        raise ValueError("text_input_type must be HANDWRITTEN or PRINTED")
    return value


def _is_admin(user) -> bool:
    return is_document_admin(user)


def _require_admin(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden("Admins only")
    return None


def _require_admin_page(request):
    if not _is_admin(request.user):
        return render(request, "public/forbidden.html", status=403)
    return None


def _user_has_usable_email(user) -> bool:
    return bool((getattr(user, "email", "") or "").strip())


def _base_queryset(
    *,
    user,
    q: str,
    upload_status: str,
    visibility: str,
    doc_type: str,
    metadata_status: str,
):
    is_admin = _is_admin(user)
    qs = (
        document_queryset_for_user(user)
        .select_related("archive_item")
        .prefetch_related("tags_m2m")
        .order_by("-created_at")
    )

    if is_admin:
        qs = qs.select_related("admin_meta")

    if upload_status:
        qs = qs.filter(upload_status=upload_status)

    if doc_type:
        qs = qs.filter(doc_type=doc_type)

    if metadata_status:
        qs = qs.filter(archive_item__metadata_status=metadata_status)

    # visibility is admin-only operational field
    if is_admin and visibility:
        qs = qs.filter(archive_item__visibility=visibility)

    q = (q or "").strip()
    if q:
        filters = (
            Q(archive_item__title__icontains=q)
            | Q(category_event__icontains=q)
            | Q(tags_m2m__name__icontains=q)
        )

        if is_admin:
            filters = filters | (
                Q(file_original_name__icontains=q)
                | Q(admin_meta__notes__icontains=q)
                | Q(admin_meta__donor__icontains=q)
                | Q(admin_meta__collection__icontains=q)
                | Q(admin_meta__original_location__icontains=q)
            )

        qs = qs.filter(filters).distinct()

    return qs


def _serialize_doc(d: Document, *, is_admin: bool) -> dict:
    admin_meta = None
    if is_admin and getattr(d, "admin_meta", None) is not None:
        m = d.admin_meta
        admin_meta = {
            "notes": m.notes,
            "donor": m.donor,
            "collection": m.collection,
            "original_location": m.original_location,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }

    item = shared_archive_item_for_document(d)
    payload = {
        "id": d.id,
        "title": item.title,
        "date_start": item.date_start.isoformat() if item.date_start else None,
        "date_end": item.date_end.isoformat() if item.date_end else None,
        "language": d.language,
        "text_input_type": d.text_input_type,
        "doc_type": d.doc_type,
        "category_event": d.category_event,
        "tags": [t.name for t in d.tags_m2m.all()],
        "metadata_status": getattr(item, "metadata_status", None),
        "upload_status": d.upload_status,
        "processing_state_user": d.processing_state_user,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }

    # Admin-only operational + admin metadata
    if is_admin:
        payload.update(
            {
                "admin_meta": admin_meta,
                "visibility": item.visibility,
                "file_s3_key": d.file_s3_key,
                "file_original_name": d.file_original_name,
                "mime_type": d.mime_type,
                "size_bytes": d.size_bytes,
                "upload_error": d.upload_error,
            }
        )

    return payload


def _uploads_bucket_or_error() -> str | JsonResponse:
    bucket_raw = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    if not isinstance(bucket_raw, str) or not bucket_raw:
        return JsonResponse(
            {"error": "Bucket not configured (set UPLOADS_BUCKET_NAME or S3_BUCKET)"},
            status=500,
        )
    return bucket_raw


def _verify_uploaded_s3_object_metadata(
    *,
    bucket: str,
    key: str,
    document_id: int,
    expected_mime: str,
    order_index: Optional[int] = None,
) -> Optional[JsonResponse]:
    """
    Verify that an uploaded S3 object exists and that its stored ContentType
    matches the expected MIME before marking the upload complete.

    Returns a JsonResponse on verification failure; None when checks pass.
    """
    body: dict = {"document_id": document_id}
    if order_index is not None:
        body["order_index"] = order_index

    if not normalize_upload_mime_type(expected_mime):
        body["error"] = "expected mime type missing"
        return JsonResponse(body, status=400)

    try:
        head = head_s3_object(bucket, key)
    except (BotoCoreError, ClientError):
        logger.exception(
            "s3 head_object failed during upload verification",
            extra={
                "document_id": document_id,
                "order_index": order_index,
                "s3_key": key,
            },
        )
        body["error"] = "s3 verification failed"
        return JsonResponse(body, status=502)

    if not head.exists:
        body["error"] = "s3 object not found"
        return JsonResponse(body, status=400)

    if not head.content_type:
        body["error"] = "s3 content type missing"
        return JsonResponse(body, status=400)

    if not upload_mime_types_match(expected_mime, head.content_type):
        body["error"] = "s3 content type mismatch"
        return JsonResponse(body, status=400)

    return None


def _json_value_as_discovery_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


def _parse_create_upload_discovery_metadata(payload: dict):
    form_data = {
        **empty_discovery_metadata_form_fields(),
        "categories": _json_value_as_discovery_string(payload.get("categories")),
        "events": _json_value_as_discovery_string(payload.get("events")),
        "discovery_tags": _json_value_as_discovery_string(
            payload.get("discovery_tags")
        ),
        "selected_categories": payload.get("selected_categories"),
        "selected_events": payload.get("selected_events"),
        "selected_tags": payload.get("selected_tags"),
    }
    return parse_archive_item_discovery_metadata_form(
        form_data,
        tags_field="discovery_tags",
    )


def _parse_create_upload_common(
    payload: dict,
) -> tuple[_CreateUploadCommon | None, HttpResponseBadRequest | None]:
    title = (payload.get("title") or "").strip()
    if not title:
        return None, _bad("title required")

    date_start_raw = payload.get("date_start")
    date_end_raw = payload.get("date_end")
    language = (payload.get("language") or "").strip() or None
    text_input_type_raw = payload.get("text_input_type")
    visibility = (payload.get("visibility") or "private").strip()
    admin_meta = payload.get("admin_meta", None)

    try:
        ds = _parse_date_optional(date_start_raw, "date_start")
        de = _parse_date_optional(date_end_raw, "date_end")
        text_input_type = _parse_text_input_type(text_input_type_raw)
        date_precision = parse_date_precision(payload.get("date_precision"))
    except ValueError as e:
        return None, _bad(str(e))

    if admin_meta is None:
        admin_meta = {}
    if not isinstance(admin_meta, dict):
        return None, _bad("admin_meta must be an object")

    if visibility not in ("private", "public"):
        return None, _bad("visibility must be private or public")

    author_name = (payload.get("author_name") or "").strip()
    source_title = (payload.get("source_title") or "").strip()
    public_note = parse_public_note(payload.get("public_note"))
    source_errors = validate_source_metadata_fields(
        author_name=author_name,
        source_title=source_title,
    )
    if source_errors:
        return None, _bad(source_errors[0])

    parsed_discovery, discovery_errors = _parse_create_upload_discovery_metadata(
        payload
    )
    if discovery_errors:
        return None, _bad(discovery_errors[0])

    return {
        "title": title,
        "date_start": ds,
        "date_end": de,
        "date_precision": date_precision,
        "language": language,
        "text_input_type": text_input_type,
        "visibility": visibility,
        "admin_meta": admin_meta,
        "author_name": author_name,
        "source_title": source_title,
        "public_note": public_note,
        "discovery_metadata": parsed_discovery,
    }, None


def _attach_document_admin_metadata(doc: Document, admin_meta: dict) -> None:
    DocumentMetadata.objects.create(
        document=doc,
        notes=str(admin_meta.get("notes") or ""),
        donor=str(admin_meta.get("donor") or ""),
        collection=str(admin_meta.get("collection") or ""),
        original_location=str(admin_meta.get("original_location") or ""),
    )


def _apply_upload_discovery_metadata(doc: Document, discovery_metadata: dict) -> None:
    update_archive_item_discovery_metadata(
        doc.archive_item,
        category_names=discovery_metadata["category_names"],
        event_names=discovery_metadata["event_names"],
        tag_names=discovery_metadata["tag_names"],
    )


def _create_multi_image_upload(request, payload: dict, common: _CreateUploadCommon):
    files_raw = payload.get("files")
    if not isinstance(files_raw, list):
        return _bad("files must be a list")

    file_count = len(files_raw)
    if file_count == 0:
        return _bad("files must contain at least 2 image files")
    if file_count == 1:
        return _bad(
            "multi-image upload requires at least 2 files; "
            "use single-file upload for one image"
        )
    if file_count > MULTI_IMAGE_MAX_FILES:
        return _bad(f"files must contain at most {MULTI_IMAGE_MAX_FILES} images")

    legacy_file_fields = [
        field
        for field in ("original_name", "mime_type", "content_type", "size_bytes")
        if field in payload
    ]
    if legacy_file_fields:
        joined = ", ".join(legacy_file_fields)
        return _bad(
            "multi-image upload must not include top-level single-file fields: "
            f"{joined}; provide file metadata inside files[]"
        )

    doc_type = (payload.get("doc_type") or "").strip()
    if doc_type and doc_type != Document.DocType.IMAGE:
        return _bad("multi-image upload requires doc_type=IMAGE")

    parsed_files: list[_ParsedImageFileMeta] = []
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            return _bad(f"files[{index}] must be an object")
        if "order_index" in entry:
            return _bad(
                "order_index must not be provided; order is defined by files[] position"
            )

        original_name = (entry.get("original_name") or "").strip()
        if not original_name:
            return _bad(f"files[{index}].original_name is required")

        mime_type = (entry.get("mime_type") or entry.get("content_type") or "").strip()
        file_err = validate_image_upload_metadata(
            mime_type=mime_type,
            original_name=original_name,
            field_prefix=f"files[{index}]",
        )
        if file_err:
            return _bad(file_err)

        size_bytes = entry.get("size_bytes")
        if size_bytes is not None and not isinstance(size_bytes, int):
            return _bad(f"files[{index}].size_bytes must be an integer")

        parsed_files.append(
            {
                "original_name": original_name,
                "mime_type": mime_type,
                "size_bytes": size_bytes if isinstance(size_bytes, int) else None,
            }
        )

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    doc = create_ocr_document(
        title=common["title"],
        doc_type=Document.DocType.IMAGE,
        date_start=common["date_start"],
        date_end=common["date_end"],
        date_precision=common["date_precision"],
        language=common["language"],
        text_input_type=common["text_input_type"],
        visibility=common["visibility"],
        author_name=common["author_name"],
        source_title=common["source_title"],
        public_note=common["public_note"],
        upload_status=Document.UploadStatus.UPLOADING,
        expected_source_file_count=file_count,
    )
    _attach_document_admin_metadata(doc, common["admin_meta"])
    _apply_upload_discovery_metadata(doc, common["discovery_metadata"])

    uploads = []
    for order_index, file_meta in enumerate(parsed_files):
        key = build_document_source_file_s3_key(
            document_id=doc.id,
            order_index=order_index,
            mime_type=file_meta["mime_type"],
        )
        DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=key,
            file_original_name=file_meta["original_name"],
            mime_type=file_meta["mime_type"],
            size_bytes=file_meta["size_bytes"],
            upload_status=DocumentSourceFile.UploadStatus.PENDING,
        )
        upload_url = create_presigned_put(
            bucket=bucket,
            key=key,
            content_type=file_meta["mime_type"],
        )
        uploads.append(
            {
                "order_index": order_index,
                "s3_key": key,
                "upload_url": upload_url,
                "original_name": file_meta["original_name"],
                "mime_type": file_meta["mime_type"],
                "size_bytes": file_meta["size_bytes"],
            }
        )

    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "doc_type": doc.doc_type,
            "expected_source_file_count": file_count,
            "uploads": uploads,
        },
        status=201,
    )


def _create_single_file_upload(request, payload: dict, common: _CreateUploadCommon):
    doc_type = (payload.get("doc_type") or "").strip()
    if doc_type not in ("PDF", "IMAGE"):
        return _bad("doc_type must be PDF or IMAGE")

    mime_type = (
        payload.get("mime_type")
        or payload.get("content_type")
        or "application/octet-stream"
    ).strip()
    original_name = (payload.get("original_name") or "").strip()
    size_bytes = payload.get("size_bytes")

    metadata_err = validate_single_file_upload_metadata(
        doc_type=doc_type,
        mime_type=mime_type,
        original_name=original_name,
    )
    if metadata_err:
        return _bad(metadata_err)

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    doc = create_ocr_document(
        title=common["title"],
        doc_type=doc_type,
        date_start=common["date_start"],
        date_end=common["date_end"],
        date_precision=common["date_precision"],
        language=common["language"],
        text_input_type=common["text_input_type"],
        visibility=common["visibility"],
        author_name=common["author_name"],
        source_title=common["source_title"],
        public_note=common["public_note"],
        upload_status=Document.UploadStatus.UPLOADING,
        file_original_name=original_name,
        mime_type=mime_type,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )
    _attach_document_admin_metadata(doc, common["admin_meta"])
    _apply_upload_discovery_metadata(doc, common["discovery_metadata"])

    ext = mime_type_to_extension(mime_type)
    key = f"documents/{doc.id}/original.{ext}"
    doc.file_s3_key = key
    doc.save(update_fields=["file_s3_key"])

    upload_url = create_presigned_put(bucket=bucket, key=key, content_type=mime_type)

    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "s3_key": key,
            "upload_url": upload_url,
        },
        status=201,
    )


@login_required
def create_upload(request):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return _bad("POST only")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _bad("invalid json")

    common, err = _parse_create_upload_common(payload)
    if err is not None:
        return err
    assert common is not None

    if "files" in payload:
        return _create_multi_image_upload(request, payload, common)

    return _create_single_file_upload(request, payload, common)


@login_required
def upload_complete(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("invalid json")

    success = payload.get("success")
    if success not in (True, False):
        return HttpResponseBadRequest("success must be true|false")

    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if is_multi_image_document(doc):
        return JsonResponse(
            {
                "error": (
                    "multi-image documents use part completion and finalize endpoints"
                ),
                "document_id": doc.id,
            },
            status=400,
        )

    if success:
        if not doc.file_s3_key:
            doc.upload_status = Document.UploadStatus.FAILED
            doc.upload_error = "upload complete called but file_s3_key is missing"
            doc.processing_state_user = Document.ProcessingState.FAILED
            doc.save(
                update_fields=["upload_status", "upload_error", "processing_state_user"]
            )
            return JsonResponse(
                {"error": "file_s3_key missing", "document_id": doc.id},
                status=400,
            )

        file_mime_raw = payload.get("file_mime")
        file_mime: str | None = None
        if isinstance(file_mime_raw, str):
            file_mime = file_mime_raw.strip()
            if file_mime:
                metadata_err = validate_single_file_upload_metadata(
                    doc_type=doc.doc_type,
                    mime_type=file_mime,
                    original_name=doc.file_original_name or "",
                )
                if metadata_err:
                    return JsonResponse(
                        {
                            "error": metadata_err.replace("mime_type", "file_mime"),
                            "document_id": doc.id,
                        },
                        status=400,
                    )

        bucket_or_response = _uploads_bucket_or_error()
        if isinstance(bucket_or_response, JsonResponse):
            return bucket_or_response
        bucket = bucket_or_response

        expected_mime = file_mime or (doc.mime_type or "")
        s3_err = _verify_uploaded_s3_object_metadata(
            bucket=bucket,
            key=doc.file_s3_key,
            document_id=doc.id,
            expected_mime=expected_mime,
        )
        if s3_err:
            return s3_err

        already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

        doc.upload_status = Document.UploadStatus.UPLOADED
        doc.upload_error = None

        if isinstance(payload.get("file_size"), int):
            doc.size_bytes = payload["file_size"]
        if file_mime:
            doc.mime_type = file_mime

        doc.processing_state_user = Document.ProcessingState.PROCESSING
        doc.save(
            update_fields=[
                "upload_status",
                "upload_error",
                "size_bytes",
                "mime_type",
                "processing_state_user",
            ]
        )

        sync_primary_document_source_file(doc)

        if not already_uploaded:
            try:
                send_process_document_message(document_id=doc.id)
            except Exception as e:
                logger.exception(
                    "enqueue failed in upload_complete",
                    extra={"document_id": doc.id},
                )
                doc.processing_state_user = Document.ProcessingState.FAILED
                doc.upload_error = f"enqueue failed: {e}"
                doc.save(update_fields=["processing_state_user", "upload_error"])
                return JsonResponse(
                    {"error": "enqueue failed", "details": str(e)},
                    status=500,
                )

    else:
        raw_err = payload.get("error") or "upload failed"
        err = str(raw_err).strip() or "upload failed"

        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(
            update_fields=["upload_status", "upload_error", "processing_state_user"]
        )

    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "processing_state_user": doc.processing_state_user,
        }
    )


def _parse_upload_success_payload(
    request,
) -> tuple[dict | None, bool | None, HttpResponseBadRequest | None]:
    if request.method != "POST":
        return None, None, HttpResponseBadRequest("POST only")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return None, None, HttpResponseBadRequest("invalid json")

    success = payload.get("success")
    if success not in (True, False):
        return None, None, HttpResponseBadRequest("success must be true|false")

    return payload, success, None


def _finalize_response(doc: Document) -> JsonResponse:
    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "processing_state_user": doc.processing_state_user,
        }
    )


def _multi_image_upload_terminal_failed_response(doc: Document) -> JsonResponse:
    return JsonResponse(
        {
            "error": (
                "multi-image upload failed; create a new upload to retry "
                "(per-part retry is not supported in V1)"
            ),
            "document_id": doc.id,
        },
        status=400,
    )


@login_required
def upload_part_complete(request, doc_id: int, order_index: int):
    deny = _require_admin(request)
    if deny:
        return deny

    payload, success, err = _parse_upload_success_payload(request)
    if err is not None:
        return err
    assert payload is not None
    assert success is not None

    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if not is_multi_image_document(doc):
        return JsonResponse(
            {"error": "not a multi-image document", "document_id": doc.id},
            status=400,
        )

    if doc.upload_status == Document.UploadStatus.FAILED and success:
        return _multi_image_upload_terminal_failed_response(doc)

    expected = doc.expected_source_file_count
    assert expected is not None  # guaranteed when is_multi_image_document is true
    if order_index < 0 or order_index >= expected:
        return JsonResponse(
            {
                "error": (
                    f"order_index must be between 0 and {expected - 1} inclusive"
                ),
                "document_id": doc.id,
            },
            status=400,
        )

    source_file = get_source_file_for_order(doc, order_index)
    if source_file is None:
        return JsonResponse(
            {
                "error": f"source file missing for order_index={order_index}",
                "document_id": doc.id,
            },
            status=400,
        )

    if success:
        file_mime = payload.get("file_mime")
        if isinstance(file_mime, str):
            file_mime = file_mime.strip()
            if file_mime:
                metadata_err = validate_image_upload_metadata(
                    mime_type=file_mime,
                    original_name=source_file.file_original_name or "",
                )
                if metadata_err:
                    return JsonResponse(
                        {
                            "error": metadata_err.replace("mime_type", "file_mime"),
                            "document_id": doc.id,
                            "order_index": order_index,
                        },
                        status=400,
                    )

        if not source_file.file_s3_key:
            return JsonResponse(
                {
                    "error": "file_s3_key missing",
                    "document_id": doc.id,
                    "order_index": order_index,
                },
                status=400,
            )

        bucket_or_response = _uploads_bucket_or_error()
        if isinstance(bucket_or_response, JsonResponse):
            return bucket_or_response
        bucket = bucket_or_response

        payload_file_mime = payload.get("file_mime")
        if isinstance(payload_file_mime, str) and payload_file_mime.strip():
            expected_mime = payload_file_mime.strip()
        else:
            expected_mime = source_file.mime_type or ""

        s3_err = _verify_uploaded_s3_object_metadata(
            bucket=bucket,
            key=source_file.file_s3_key,
            document_id=doc.id,
            expected_mime=expected_mime,
            order_index=order_index,
        )
        if s3_err:
            return s3_err

        source_file.upload_status = DocumentSourceFile.UploadStatus.UPLOADED
        source_file.upload_error = None
        file_size = payload.get("file_size")
        if isinstance(file_size, int):
            source_file.size_bytes = file_size
        if isinstance(file_mime, str) and file_mime:
            source_file.mime_type = file_mime
        source_file.save(
            update_fields=[
                "upload_status",
                "upload_error",
                "size_bytes",
                "mime_type",
                "updated_at",
            ]
        )
    else:
        raw_err = payload.get("error") or "upload failed"
        upload_err = str(raw_err).strip() or "upload failed"
        source_file.upload_status = DocumentSourceFile.UploadStatus.FAILED
        source_file.upload_error = upload_err
        source_file.save(update_fields=["upload_status", "upload_error", "updated_at"])

        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = upload_err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(
            update_fields=["upload_status", "upload_error", "processing_state_user"]
        )

    return JsonResponse(
        {
            "document_id": doc.id,
            "order_index": order_index,
            "upload_status": source_file.upload_status,
            "document_upload_status": doc.upload_status,
        }
    )


@login_required
def upload_finalize(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    payload, success, err = _parse_upload_success_payload(request)
    if err is not None:
        return err
    assert payload is not None
    assert success is not None

    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if not is_multi_image_document(doc):
        return JsonResponse(
            {"error": "not a multi-image document", "document_id": doc.id},
            status=400,
        )

    if doc.upload_status == Document.UploadStatus.FAILED:
        return _multi_image_upload_terminal_failed_response(doc)

    if not success:
        raw_err = payload.get("error") or "upload finalize failed"
        upload_err = str(raw_err).strip() or "upload finalize failed"
        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = upload_err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(
            update_fields=["upload_status", "upload_error", "processing_state_user"]
        )
        return _finalize_response(doc)

    ready, ready_err = all_expected_source_files_uploaded(doc)
    if not ready:
        return JsonResponse(
            {"error": ready_err, "document_id": doc.id},
            status=400,
        )

    primary = get_source_file_for_order(doc, 0)
    if primary is None:
        return JsonResponse(
            {"error": "primary source file missing", "document_id": doc.id},
            status=400,
        )

    already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

    mirror_primary_document_from_source_file(doc, primary)
    doc.upload_status = Document.UploadStatus.UPLOADED
    doc.upload_error = None
    if not already_uploaded:
        doc.processing_state_user = Document.ProcessingState.PROCESSING
    doc.save(
        update_fields=[
            "file_s3_key",
            "file_original_name",
            "mime_type",
            "size_bytes",
            "upload_status",
            "upload_error",
            "processing_state_user",
        ]
    )

    if not already_uploaded:
        try:
            send_process_document_message(document_id=doc.id)
        except Exception as e:
            logger.exception(
                "enqueue failed in upload_finalize",
                extra={"document_id": doc.id},
            )
            doc.processing_state_user = Document.ProcessingState.FAILED
            doc.upload_error = f"enqueue failed: {e}"
            doc.save(update_fields=["processing_state_user", "upload_error"])
            return JsonResponse(
                {"error": "enqueue failed", "details": str(e)},
                status=500,
            )

    return _finalize_response(doc)


@login_required
def create_photo_upload(request):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "invalid json"}, status=400)

    parsed, err = parse_create_photo_upload_metadata(payload)
    if err is not None:
        return JsonResponse({"error": err}, status=400)
    assert parsed is not None

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    archive_item, photo_content, upload_url = create_photo_upload_plan(
        bucket=bucket,
        title=parsed["title"],
        visibility=parsed["visibility"],
        date_start=parsed["date_start"],
        date_end=parsed["date_end"],
        date_precision=parsed["date_precision"],
        metadata_status=parsed["metadata_status"],
        original_name=parsed["original_name"],
        mime_type=parsed["mime_type"],
        discovery_metadata=parsed["discovery_metadata"],
        description=parsed["description"],
        location=parsed["location"],
        context=parsed["context"],
        people_present=parsed["people_present"],
        notes=parsed["notes"],
        public_note=parsed["public_note"],
    )

    return JsonResponse(
        {
            "archive_item_id": archive_item.id,
            "photo_content_id": photo_content.id,
            "s3_key": photo_content.original_file_key,
            "upload_url": upload_url,
            "upload_status": photo_content.upload_status,
        },
        status=201,
    )


@login_required
def photo_upload_complete(request, photo_content_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "invalid json"}, status=400)

    try:
        photo_content = PhotoContent.objects.select_related("archive_item").get(
            id=photo_content_id
        )
    except PhotoContent.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if photo_content.archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        return JsonResponse({"error": "not a photo upload"}, status=400)

    success = bool(payload.get("success"))
    file_mime = (payload.get("file_mime") or "").strip() or None
    client_error = (payload.get("error") or "").strip() or None

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    photo_content, verify_err = finalize_photo_upload(
        photo_content,
        bucket=bucket,
        success=success,
        file_mime=file_mime,
        client_error=client_error,
    )

    body = {
        "archive_item_id": photo_content.archive_item_id,
        "photo_content_id": photo_content.id,
        "upload_status": photo_content.upload_status,
        "upload_error": photo_content.upload_error,
        "upload_complete": (
            photo_content.upload_status == PhotoContent.UploadStatus.UPLOADED
        ),
        "original_size_bytes": photo_content.original_size_bytes,
    }

    if verify_err:
        body["error"] = verify_err.message
        return JsonResponse(body, status=verify_err.status)

    if not success:
        body["error"] = photo_content.upload_error or "upload failed"
        return JsonResponse(body, status=200)

    return JsonResponse(body, status=200)


def documents_list_api(request):
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = _parse_int(
        request.GET.get("limit"),
        default=DEFAULT_PAGE_SIZE,
        min_value=1,
        max_value=200,
    )
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    is_admin = _is_admin(request.user)

    qs = _base_queryset(
        user=request.user,
        q=q,
        upload_status=upload_status,
        visibility=visibility,
        doc_type=doc_type,
        metadata_status=metadata_status,
    )
    total = qs.count()

    docs = list(qs[offset : offset + limit])
    items = [_serialize_doc(d, is_admin=is_admin) for d in docs]

    logger.info(
        "documents_list_api user=%s admin=%s q=%r upload_status=%r visibility=%r doc_type=%r metadata_status=%r limit=%s offset=%s total=%s returned=%s",
        getattr(request.user, "username", None),
        is_admin,
        q,
        upload_status,
        visibility,
        doc_type,
        metadata_status,
        limit,
        offset,
        total,
        len(items),
    )

    return JsonResponse(
        {"count": total, "limit": limit, "offset": offset, "items": items}
    )


def documents_list_page(request):
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = DEFAULT_PAGE_SIZE
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    is_admin = _is_admin(request.user)

    qs = _base_queryset(
        user=request.user,
        q=q,
        upload_status=upload_status,
        visibility=visibility,
        doc_type=doc_type,
        metadata_status=metadata_status,
    )
    total = qs.count()
    docs = list(qs[offset : offset + limit])

    context = {
        "docs": docs,
        "q": q,
        "upload_status": upload_status if is_admin else "",
        "visibility": visibility if is_admin else "",
        "doc_type": doc_type,
        "metadata_status": metadata_status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total else None,
        "doc_type_choices": Document.DocType.choices,
        "metadata_status_choices": Document.MetadataStatus.choices,
        "upload_status_choices": Document.UploadStatus.choices,
        "visibility_choices": archive_visibility_ui_choices(),
        "is_admin": is_admin,
    }

    logger.info(
        "documents_list_page user=%s admin=%s q=%r upload_status=%r visibility=%r doc_type=%r metadata_status=%r offset=%s limit=%s total=%s returned=%s",
        getattr(request.user, "username", None),
        is_admin,
        q,
        upload_status,
        visibility,
        doc_type,
        metadata_status,
        offset,
        limit,
        total,
        len(docs),
    )
    return render(request, "documents/list.html", context)


@login_required
def admin_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    limit = DEFAULT_PAGE_SIZE
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    only_missing_tags = (request.GET.get("only_missing_tags") or "").strip() == "1"
    only_missing_admin_meta = (
        request.GET.get("only_missing_admin_meta") or ""
    ).strip() == "1"

    base_qs = (
        Document.objects.select_related("admin_meta", "archive_item")
        .prefetch_related("tags_m2m")
        .filter(
            archive_item__metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION
        )
        .order_by("-created_at")
        .distinct()
    )

    total_backlog = base_qs.count()
    missing_tags_count = base_qs.filter(tags_m2m__isnull=True).distinct().count()

    missing_admin_meta_count = base_qs.filter(
        Q(admin_meta__donor="")
        & Q(admin_meta__collection="")
        & Q(admin_meta__original_location="")
        & Q(admin_meta__notes="")
    ).count()

    qs = base_qs

    if only_missing_tags:
        qs = qs.filter(tags_m2m__isnull=True).distinct()

    if only_missing_admin_meta:
        qs = qs.filter(
            Q(admin_meta__donor="")
            & Q(admin_meta__collection="")
            & Q(admin_meta__original_location="")
            & Q(admin_meta__notes="")
        )

    total_filtered = qs.count()
    docs = list(qs[offset : offset + limit])

    context = {
        "docs": docs,
        "offset": offset,
        "limit": limit,
        "total": total_filtered,
        "total_backlog": total_backlog,
        "missing_tags_count": missing_tags_count,
        "missing_admin_meta_count": missing_admin_meta_count,
        "only_missing_tags": only_missing_tags,
        "only_missing_admin_meta": only_missing_admin_meta,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total_filtered else None,
    }

    logger.info(
        "admin_backlog_page user=%s offset=%s limit=%s total_backlog=%s total_filtered=%s only_missing_tags=%s only_missing_admin_meta=%s returned=%s",
        getattr(request.user, "username", None),
        offset,
        limit,
        total_backlog,
        total_filtered,
        only_missing_tags,
        only_missing_admin_meta,
        len(docs),
    )
    return render(request, "documents/backlog.html", context)


@login_required
def review_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    q = request.GET.get("q", "") or ""
    language = (request.GET.get("language") or "").strip()
    text_input_type = (request.GET.get("text_input_type") or "").strip()
    processing_state_user = (request.GET.get("processing_state_user") or "").strip()
    engine_key = (request.GET.get("engine_key") or "").strip()
    result_type = (request.GET.get("result_type") or "").strip()
    verification_status = (request.GET.get("verification_status") or "").strip()

    limit = DEFAULT_PAGE_SIZE
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    qs = documents_in_review_backlog(
        q=q,
        language=language,
        text_input_type=text_input_type,
        processing_state_user=processing_state_user,
        engine_key=engine_key,
        result_type=result_type,
        verification_status=verification_status,
    )
    total = qs.count()
    # prefetch text_results for attach_review_summaries (batched; avoids N+1).
    docs = list(
        qs.select_related("archive_item").prefetch_related("text_results")[
            offset : offset + limit
        ]
    )
    rows = attach_review_summaries(docs)

    context = {
        "rows": rows,
        "q": q,
        "language": language,
        "text_input_type": text_input_type,
        "processing_state_user": processing_state_user,
        "engine_key": engine_key,
        "result_type": result_type,
        "verification_status": verification_status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total else None,
        "language_choices": Document.Language.choices,
        "text_input_type_choices": TEXT_INPUT_TYPE_UI_CHOICES,
        "processing_state_choices": Document.ProcessingState.choices,
        "engine_key_choices": DocumentTextResult.OcrEngineKey.choices,
        "result_type_choices": DocumentTextResult.ResultType.choices,
        "verification_status_choices": DocumentTextResult.VerificationStatus.choices,
    }

    logger.info(
        "review_backlog_page user=%s offset=%s limit=%s total=%s returned=%s",
        getattr(request.user, "username", None),
        offset,
        limit,
        total,
        len(docs),
    )
    return render(request, "documents/review_backlog.html", context)


def _review_result_type_label(doc: Document, result_type: str) -> str:
    if doc.language == Document.Language.HEBREW:
        if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
            return "תעתוק מקור (עברית כפי שחולצה)"
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            return "טקסט עברי לבדיקה"

    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return "תעתוק מקור"
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return "טקסט עברי"
    return result_type


def _review_result_type_description(doc: Document, result_type: str) -> str:
    """One-line reviewer-facing explanation of what a text result represents."""
    if doc.language == Document.Language.HEBREW:
        if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
            return "טקסט המקור כפי שחולץ אוטומטית מן המסמך."
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            return "הטקסט העברי שמיועד לבדיקה ולאישור."

    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return "טקסט בשפת המקור כפי שחולץ אוטומטית."
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return "תרגום לעברית (אם קיים)."
    return ""


def _review_non_actionable_reason(row: DocumentTextResult) -> Optional[str]:
    """
    Human-readable reason when edit/approve/reject controls are unavailable.

    Display-only; uses the same eligibility rules as ``is_review_pending_text_result``.
    """
    if is_review_pending_text_result(row):
        return None

    if row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED:
        if is_verified_editable_text_result(row):
            return None
        return "התעתוק כבר אושר אנושית — אין פעולות בקרה זמינות במסך זה."

    if row.status == DocumentTextResult.Status.FAILED:
        return "תעתוק זה נכשל בעיבוד — לא ניתן לבדוק או לאשר."

    if not (row.text or "").strip():
        return "אין טקסט זמין לבדיקה."

    if row.status != DocumentTextResult.Status.NEEDS_REVIEW:
        return "תוצאה זו אינה ממתינה לבקרה."

    return "פעולות בקרה אינן זמינות לתוצאה זו."


def _document_source_preview_context(doc: Document) -> dict:
    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    source_preview = build_source_preview(doc, bucket)
    content_url = None
    if not is_multi_image_document(doc) and bucket and doc.file_s3_key:
        content_url = create_presigned_get(
            bucket=bucket,
            key=doc.file_s3_key,
            expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
        )

    return {
        "content_url": content_url,
        "source_preview_items": source_preview.items,
        "source_preview_unavailable_count": source_preview.non_uploaded_count,
    }


@login_required
def review_detail_page(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_object_or_404(
        Document.objects.select_related("admin_meta", "archive_item").prefetch_related(
            "tags_m2m", "text_results", "transkribus_runs", "source_files"
        ),
        id=doc_id,
    )
    admin_meta = getattr(doc, "admin_meta", None)

    source_context = _document_source_preview_context(doc)

    text_results = sorted(
        doc.text_results.all(),
        key=lambda r: (r.result_type, r.engine, -r.updated_at.timestamp()),
    )
    source_by_engine = {
        r.engine: r
        for r in text_results
        if r.result_type == DocumentTextResult.ResultType.SOURCE_TEXT
    }
    text_result_cards = []
    for row in text_results:
        paired_source = (
            source_by_engine.get(row.engine)
            if row.result_type == DocumentTextResult.ResultType.HEBREW_TEXT
            else None
        )
        text_result_cards.append(
            {
                "row": row,
                "result_type_label": _review_result_type_label(doc, row.result_type),
                "result_type_description": _review_result_type_description(
                    doc, row.result_type
                ),
                "review_reasons": parse_review_reasons(row.review_reasons),
                "text_length": len((row.text or "").strip()),
                "is_pending_review": is_review_pending_text_result(row),
                "is_editable": is_review_editable_text_result(row),
                "is_verified_editable": is_verified_editable_text_result(row),
                "hebrew_translation_stale": is_hebrew_translation_stale(
                    row, paired_source
                ),
                "non_actionable_reason": _review_non_actionable_reason(row),
            }
        )

    transkribus_runs = sorted(
        doc.transkribus_runs.all(),
        key=lambda r: r.created_at,
        reverse=True,
    )
    latest_transkribus_run = transkribus_runs[0] if transkribus_runs else None

    context = {
        "doc": doc,
        "admin_meta": admin_meta,
        **source_context,
        "text_result_cards": text_result_cards,
        "latest_transkribus_run": latest_transkribus_run,
        "transkribus_run_count": len(transkribus_runs),
    }

    logger.info(
        "review_detail_page user=%s doc_id=%s text_results=%s transkribus_runs=%s",
        getattr(request.user, "username", None),
        doc.id,
        len(text_results),
        len(transkribus_runs),
    )
    return render(request, "documents/review_detail.html", context)


def _review_text_result_not_eligible_response() -> HttpResponseBadRequest:
    return HttpResponseBadRequest(
        "transcription result is not eligible for review action"
    )


@login_required
@require_POST
def review_text_result_verify(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    row = get_object_or_404(DocumentTextResult, id=result_id)
    if not is_review_pending_text_result(row):
        return _review_text_result_not_eligible_response()

    row.verification_status = DocumentTextResult.VerificationStatus.VERIFIED
    row.save(update_fields=["verification_status", "updated_at"])

    logger.info(
        "review_text_result_verify user=%s result_id=%s document_id=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
    )
    return redirect(
        reverse("review-detail-page", kwargs={"doc_id": row.document_id})
    )


@login_required
@require_POST
def review_text_result_reject(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    row = get_object_or_404(DocumentTextResult, id=result_id)
    if not is_review_pending_text_result(row):
        return _review_text_result_not_eligible_response()

    row.verification_status = DocumentTextResult.VerificationStatus.REJECTED
    row.save(update_fields=["verification_status", "updated_at"])

    logger.info(
        "review_text_result_reject user=%s result_id=%s document_id=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
    )
    return redirect(
        reverse("review-detail-page", kwargs={"doc_id": row.document_id})
    )


@login_required
@require_POST
def review_text_result_update_text(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    row = get_object_or_404(DocumentTextResult, id=result_id)
    if not is_review_editable_text_result(row):
        return _review_text_result_not_eligible_response()

    submitted = request.POST.get("text")
    if submitted is None or not submitted.strip():
        return HttpResponseBadRequest("text is required and must be non-empty")

    row.text = submitted
    row.save(update_fields=["text", "updated_at"])

    logger.info(
        "review_text_result_update_text user=%s result_id=%s document_id=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
    )
    return redirect(
        reverse("review-detail-page", kwargs={"doc_id": row.document_id})
    )


@login_required
@require_POST
def review_text_result_verified_edit(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    submitted = request.POST.get("text")
    if submitted is None:
        return HttpResponseBadRequest("text is required")

    try:
        row = edit_verified_text_result(
            result_id=result_id,
            new_text=submitted,
            editor=request.user,
        )
    except DocumentTextResult.DoesNotExist:
        raise Http404() from None
    except VerifiedTextResultEditError as exc:
        return HttpResponseBadRequest(str(exc))

    logger.info(
        "review_text_result_verified_edit user=%s result_id=%s document_id=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
    )
    return redirect(
        reverse("review-detail-page", kwargs={"doc_id": row.document_id})
    )


def document_detail_page(request, doc_id: int):
    is_admin = _is_admin(request.user)
    detail_qs = Document.objects.select_related(
        "admin_meta",
        "archive_item",
    ).prefetch_related(
        text_presentation_results_prefetch(),
        "source_files",
        "archive_item__categories",
        "archive_item__events",
        "archive_item__tags",
    )
    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=detail_qs,
    )

    admin_meta = getattr(doc, "admin_meta", None) if is_admin else None

    source_context = _document_source_preview_context(doc)

    text_presentation = get_text_presentation_for_document(doc)
    displayed_transcription_text = get_displayed_transcription_text(doc)

    context = {
        "doc": doc,
        **source_context,
        "admin_meta": admin_meta,
        "text_presentation": text_presentation,
        "displayed_transcription_text": displayed_transcription_text,
        "is_admin": is_admin,
        "show_ocr_reprocess_action": is_admin and is_ocr_reprocess_ui_eligible(doc),
        "show_hebrew_translation_retry_action": is_admin
        and is_hebrew_translation_retry_ui_eligible(doc),
    }

    logger.info(
        "document_detail_page user=%s doc_id=%s admin=%s has_content_url=%s mime_type=%r missing_text=%s",
        getattr(request.user, "username", None),
        doc.id,
        is_admin,
        bool(source_context["content_url"]),
        doc.mime_type,
        text_presentation.missing,
    )

    return render(request, "documents/detail.html", context)


def _suggestion_form_queryset():
    return Document.objects.select_related("archive_item").prefetch_related(
        "text_results",
        "source_files",
    )


def _load_transcription_suggestion_document(
    request, doc_id: int
) -> tuple[Document, str]:
    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=_suggestion_form_queryset(),
    )

    archive_item = doc.archive_item
    if (
        archive_item is None
        or archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT
    ):
        raise Http404()

    displayed_text = get_displayed_transcription_text(doc)
    if not normalize_transcription_text(displayed_text):
        raise Http404()

    return doc, displayed_text


def _transcription_suggestion_source_context(doc: Document) -> dict:
    return _document_source_preview_context(doc)


def _empty_suggestion_field_values() -> dict[str, str]:
    return {
        "submitter_name": "",
        "submitter_email": "",
        "submitter_note": "",
        "suggested_text": "",
    }


def transcription_suggestion_form(request, doc_id: int):
    doc, displayed_text = _load_transcription_suggestion_document(request, doc_id)

    form_errors: list[str] = []
    field_values = _empty_suggestion_field_values()
    field_values["suggested_text"] = displayed_text

    if request.method == "POST":
        field_values = {
            "submitter_name": (request.POST.get("submitter_name") or "").strip(),
            "submitter_email": (request.POST.get("submitter_email") or "").strip(),
            "submitter_note": (request.POST.get("submitter_note") or "").strip(),
            "suggested_text": request.POST.get("suggested_text") or "",
        }

        if is_honeypot_triggered(request.POST):
            return redirect(
                reverse("transcription-suggestion-thanks", kwargs={"doc_id": doc.id})
            )

        if not field_values["submitter_name"]:
            form_errors.append(NAME_REQUIRED_ERROR)
        if not normalize_transcription_text(field_values["suggested_text"]):
            form_errors.append(SUGGESTED_TEXT_REQUIRED_ERROR)
        elif texts_are_equivalent(displayed_text, field_values["suggested_text"]):
            form_errors.append(IDENTICAL_TEXT_ERROR)

        if not form_errors:
            submitter_user = (
                request.user
                if getattr(request.user, "is_authenticated", False)
                else None
            )
            TranscriptionEditSuggestion.objects.create(
                document=doc,
                current_text_snapshot=displayed_text,
                suggested_text=field_values["suggested_text"],
                submitter_name=field_values["submitter_name"],
                submitter_email=field_values["submitter_email"],
                submitter_note=field_values["submitter_note"],
                submitter_user=submitter_user,
            )
            return redirect(
                reverse("transcription-suggestion-thanks", kwargs={"doc_id": doc.id})
            )

    context = {
        "doc": doc,
        "displayed_text": displayed_text,
        "form_errors": form_errors,
        "field_values": field_values,
        **_transcription_suggestion_source_context(doc),
    }
    return render(request, "documents/transcription_suggestion_form.html", context)


def transcription_suggestion_thanks(request, doc_id: int):
    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )

    archive_item = doc.archive_item
    if (
        archive_item is None
        or archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT
    ):
        raise Http404()

    return render(
        request,
        "documents/transcription_suggestion_thanks.html",
        {"doc": doc},
    )


@login_required
def transcription_suggestion_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    suggestions = list(
        TranscriptionEditSuggestion.objects.select_related(
            "document",
            "document__archive_item",
            "submitter_user",
            "reviewed_by",
        )
        .annotate(
            pending_first=Case(
                When(
                    status=TranscriptionEditSuggestion.Status.PENDING,
                    then=Value(0),
                ),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("pending_first", "-created_at")
    )

    logger.info(
        "transcription_suggestion_backlog_page user=%s returned=%s",
        getattr(request.user, "username", None),
        len(suggestions),
    )
    return render(
        request,
        "documents/transcription_suggestion_backlog.html",
        {"suggestions": suggestions},
    )


@login_required
def transcription_suggestion_detail_page(request, suggestion_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    suggestion = get_object_or_404(
        TranscriptionEditSuggestion.objects.select_related(
            "document",
            "document__archive_item",
            "submitter_user",
            "reviewed_by",
        ).prefetch_related("document__source_files"),
        id=suggestion_id,
    )
    doc = suggestion.document

    source_context = _document_source_preview_context(doc)

    diff_html = render_transcription_diff_html(
        suggestion.current_text_snapshot,
        suggestion.suggested_text,
    )
    live_text = get_displayed_transcription_text(doc)
    live_text_changed = not texts_are_equivalent(
        live_text,
        suggestion.current_text_snapshot,
    )

    return render(
        request,
        "documents/transcription_suggestion_detail.html",
        {
            "suggestion": suggestion,
            "doc": doc,
            **source_context,
            "diff_html": diff_html,
            "status_label": suggestion_status_label(suggestion.status),
            "live_text": live_text,
            "live_text_changed": live_text_changed,
        },
    )


@login_required
@require_POST
def transcription_suggestion_approve(request, suggestion_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    detail_url = reverse(
        "transcription-suggestion-detail",
        kwargs={"suggestion_id": suggestion_id},
    )
    approved_text = request.POST.get("approved_text")
    if approved_text is None:
        messages.error(request, "יש להזין טקסט מאושר.")
        return redirect(detail_url)

    try:
        approve_suggestion(
            suggestion_id,
            approved_text=approved_text,
            reviewer=request.user,
        )
    except TranscriptionEditSuggestion.DoesNotExist:
        raise Http404()
    except TranscriptionSuggestionReviewError as exc:
        messages.error(request, str(exc))

    return redirect(detail_url)


@login_required
@require_POST
def transcription_suggestion_reject(request, suggestion_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    detail_url = reverse(
        "transcription-suggestion-detail",
        kwargs={"suggestion_id": suggestion_id},
    )

    try:
        reject_suggestion(suggestion_id, reviewer=request.user)
    except TranscriptionEditSuggestion.DoesNotExist:
        raise Http404()
    except TranscriptionSuggestionReviewError as exc:
        messages.error(request, str(exc))

    return redirect(detail_url)


def _empty_archive_metadata_suggestion_field_values() -> dict[str, str]:
    return {
        "submitter_name": "",
        "submitter_email": "",
        "submitter_note": "",
        "suggested_categories": "",
        "suggested_events": "",
        "suggested_tags": "",
    }


def _load_archive_metadata_suggestion_item(
    request,
    item_id: int,
) -> ArchiveItem:
    detail_qs = ArchiveItem.objects.prefetch_related("categories", "events", "tags")
    return get_viewable_archive_item(request.user, item_id, queryset=detail_qs)


def archive_metadata_suggestion_form(request, item_id: int):
    item = _load_archive_metadata_suggestion_item(request, item_id)

    form_errors: list[str] = []
    field_values = _empty_archive_metadata_suggestion_field_values()
    current_metadata = format_current_metadata_labels(item)

    if request.method == "POST":
        field_values = {
            "submitter_name": (request.POST.get("submitter_name") or "").strip(),
            "submitter_email": (request.POST.get("submitter_email") or "").strip(),
            "submitter_note": (request.POST.get("submitter_note") or "").strip(),
            "suggested_categories": request.POST.get("suggested_categories") or "",
            "suggested_events": request.POST.get("suggested_events") or "",
            "suggested_tags": request.POST.get("suggested_tags") or "",
        }

        if is_honeypot_triggered(request.POST):
            return redirect(
                reverse(
                    "archive-metadata-suggestion-thanks",
                    kwargs={"item_id": item.id},
                )
            )

        if not field_values["submitter_name"]:
            form_errors.append(NAME_REQUIRED_ERROR)
        if not has_suggestion_content(
            suggested_categories=field_values["suggested_categories"],
            suggested_events=field_values["suggested_events"],
            suggested_tags=field_values["suggested_tags"],
            submitter_note=field_values["submitter_note"],
        ):
            form_errors.append(SUGGESTION_CONTENT_REQUIRED_ERROR)

        if not form_errors:
            submitter_user = (
                request.user
                if getattr(request.user, "is_authenticated", False)
                else None
            )
            ArchiveMetadataSuggestion.objects.create(
                archive_item=item,
                suggested_categories=normalize_suggestion_text(
                    field_values["suggested_categories"]
                ),
                suggested_events=normalize_suggestion_text(
                    field_values["suggested_events"]
                ),
                suggested_tags=normalize_suggestion_text(
                    field_values["suggested_tags"]
                ),
                submitter_name=field_values["submitter_name"],
                submitter_email=field_values["submitter_email"],
                submitter_note=field_values["submitter_note"],
                submitter_user=submitter_user,
            )
            return redirect(
                reverse(
                    "archive-metadata-suggestion-thanks",
                    kwargs={"item_id": item.id},
                )
            )

    context = {
        "item": item,
        "current_metadata": current_metadata,
        "form_errors": form_errors,
        "field_values": field_values,
    }
    return render(
        request,
        "documents/archive/metadata_suggestion_form.html",
        context,
    )


def archive_metadata_suggestion_thanks(request, item_id: int):
    item = _load_archive_metadata_suggestion_item(request, item_id)

    return render(
        request,
        "documents/archive/metadata_suggestion_thanks.html",
        {"item": item},
    )


@login_required
def archive_metadata_suggestion_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    suggestions = list(
        ArchiveMetadataSuggestion.objects.select_related(
            "archive_item",
            "submitter_user",
            "reviewed_by",
        )
        .annotate(
            pending_first=Case(
                When(
                    status=ArchiveMetadataSuggestion.Status.PENDING,
                    then=Value(0),
                ),
                default=Value(1),
                output_field=IntegerField(),
            )
        )
        .order_by("pending_first", "-created_at")
    )

    logger.info(
        "archive_metadata_suggestion_backlog_page user=%s returned=%s",
        getattr(request.user, "username", None),
        len(suggestions),
    )
    return render(
        request,
        "documents/archive/metadata_suggestion_backlog.html",
        {"suggestions": suggestions},
    )


@login_required
@require_POST
def archive_metadata_suggestion_approve(request, suggestion_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    backlog_url = reverse("archive-metadata-suggestion-backlog")

    try:
        approve_archive_metadata_suggestion(
            suggestion_id,
            reviewer=request.user,
        )
    except ArchiveMetadataSuggestion.DoesNotExist:
        raise Http404()
    except ArchiveMetadataSuggestionReviewError as exc:
        messages.error(request, str(exc))

    return redirect(backlog_url)


@login_required
@require_POST
def archive_metadata_suggestion_reject(request, suggestion_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    backlog_url = reverse("archive-metadata-suggestion-backlog")

    try:
        reject_archive_metadata_suggestion(
            suggestion_id,
            reviewer=request.user,
        )
    except ArchiveMetadataSuggestion.DoesNotExist:
        raise Http404()
    except ArchiveMetadataSuggestionReviewError as exc:
        messages.error(request, str(exc))

    return redirect(backlog_url)


@login_required
@require_POST
def document_ocr_reprocess(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    doc = get_object_or_404(
        Document.objects.select_related("archive_item"),
        id=doc_id,
    )
    try:
        worker_env = validate_required_env()
    except EnvConfigError as exc:
        messages.error(request, f"שגיאת תצורה: {exc}")
        return redirect("documents-detail-page", doc_id=doc.id)

    collection_id = worker_env.transkribus_collection_id or ""
    model_id = worker_env.transkribus_model_id or ""

    try:
        assessment = apply_ocr_reprocess(
            doc.id,
            collection_id=collection_id,
            model_id=model_id,
        )
    except OcrReprocessError as exc:
        messages.error(request, str(exc))
        return redirect("documents-detail-page", doc_id=doc.id)

    messages.success(
        request,
        f"בוצע תזמון עיבוד מחדש. מצב: {assessment.retry_mode.value}.",
    )
    logger.info(
        "document_ocr_reprocess user=%s doc_id=%s retry_mode=%s",
        getattr(request.user, "username", None),
        doc.id,
        assessment.retry_mode.value,
    )
    return redirect("documents-detail-page", doc_id=doc.id)


@login_required
@require_POST
def document_hebrew_translation_retry(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    doc = get_object_or_404(
        Document.objects.select_related("archive_item"),
        id=doc_id,
    )
    try:
        enqueue_hebrew_translation_retry(doc.id)
    except HebrewTranslationRetryError:
        messages.error(request, "לא ניתן לשלוח תרגום לעברית לעיבוד כעת.")
        return redirect("documents-detail-page", doc_id=doc.id)
    except Exception:
        logger.exception(
            "document_hebrew_translation_retry enqueue failed user=%s doc_id=%s",
            getattr(request.user, "username", None),
            doc.id,
        )
        messages.error(request, "שליחת התרגום לעיבוד נכשלה. נסו שוב מאוחר יותר.")
        return redirect("documents-detail-page", doc_id=doc.id)

    messages.success(request, "תרגום לעברית נשלח לעיבוד.")
    logger.info(
        "document_hebrew_translation_retry user=%s doc_id=%s",
        getattr(request.user, "username", None),
        doc.id,
    )
    return redirect("documents-detail-page", doc_id=doc.id)


def _upload_form_context() -> dict:
    return {
        "doc_type_choices": Document.DocType.choices,
        "text_input_type_choices": TEXT_INPUT_TYPE_UI_CHOICES,
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "form_data": empty_discovery_metadata_form_fields(),
        "discovery_tags_input_name": "discovery_tags",
        "discovery_tags_input_id": "discovery_tags",
        **discovery_metadata_option_querysets(),
    }


def _photo_upload_form_context() -> dict:
    return {
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "visibility_choices": archive_visibility_ui_choices(),
        "metadata_status_choices": archive_metadata_status_ui_choices(),
        "form_data": {
            **_empty_archive_metadata_form_data(),
            **empty_photo_metadata_form_data(),
            **empty_discovery_metadata_form_fields(),
        },
        "discovery_tags_input_name": "discovery_tags",
        "discovery_tags_input_id": "discovery_tags",
        **discovery_metadata_option_querysets(),
    }


@login_required
def upload_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny
    return render(
        request,
        "documents/upload.html",
        context=_upload_form_context(),
    )


def _archive_metadata_form_context(
    *,
    form_data: dict,
    form_errors: list[str],
    page_title: str,
    submit_label: str,
) -> dict:
    return {
        "form_data": form_data,
        "form_errors": form_errors,
        "page_title": page_title,
        "submit_label": submit_label,
        "visibility_choices": archive_visibility_ui_choices(),
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "metadata_status_choices": archive_metadata_status_ui_choices(),
    }


def _manual_text_form_context(
    *,
    form_data: dict,
    form_errors: list[str],
    page_title: str,
    submit_label: str,
) -> dict:
    return _archive_metadata_form_context(
        form_data=form_data,
        form_errors=form_errors,
        page_title=page_title,
        submit_label=submit_label,
    )


def _archive_metadata_form_data(
    *,
    title: str,
    visibility: str,
    date_start,
    date_end,
    date_precision: str,
    metadata_status: str,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
) -> dict:
    return {
        "title": title,
        "visibility": visibility,
        "date_start": date_start.isoformat() if date_start else "",
        "date_end": date_end.isoformat() if date_end else "",
        "date_precision": date_precision,
        "metadata_status": metadata_status,
        "author_name": author_name,
        "source_title": source_title,
        "public_note": public_note,
    }


def _archive_metadata_form_data_from_document(document: Document) -> dict:
    item = shared_archive_item_for_document(document)
    return _archive_metadata_form_data(
        title=item.title,
        visibility=item.visibility,
        date_start=item.date_start,
        date_end=item.date_end,
        date_precision=item.date_precision,
        metadata_status=item.metadata_status,
        author_name=item.author_name,
        source_title=item.source_title,
        public_note=item.public_note,
    )


def _ocr_catalog_form_data_from_document(document: Document) -> dict:
    admin_meta = getattr(document, "admin_meta", None)
    return {
        "donor": admin_meta.donor if admin_meta else "",
        "collection": admin_meta.collection if admin_meta else "",
        "original_location": admin_meta.original_location if admin_meta else "",
        "notes": admin_meta.notes if admin_meta else "",
    }


def _ocr_document_edit_form_data_from_document(document: Document) -> dict:
    return {
        **_archive_metadata_form_data_from_document(document),
        **_ocr_catalog_form_data_from_document(document),
        **discovery_metadata_form_data_from_item(document.archive_item),
    }


def _empty_archive_metadata_form_data() -> dict:
    return _archive_metadata_form_data(
        title="",
        visibility=ArchiveItem.Visibility.PRIVATE,
        date_start=None,
        date_end=None,
        date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
    )


def _empty_manual_text_form_data() -> dict:
    return {
        **_empty_archive_metadata_form_data(),
        "body": "",
        **empty_discovery_metadata_form_fields(),
    }


def _manual_text_discovery_metadata_form_context() -> dict:
    return {
        "show_discovery_metadata": True,
        "discovery_tags_input_name": "tags",
        "discovery_tags_input_id": "tags",
        **discovery_metadata_option_querysets(),
    }


def _ocr_discovery_metadata_form_context() -> dict:
    return {
        "discovery_tags_input_name": "discovery_tags",
        "discovery_tags_input_id": "discovery_tags",
        **discovery_metadata_option_querysets(),
    }


def _manual_text_form_data_from_item(item: ArchiveItem) -> dict:
    return {
        **_archive_metadata_form_data(
            title=item.title,
            visibility=item.visibility,
            date_start=item.date_start,
            date_end=item.date_end,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            author_name=item.author_name,
            source_title=item.source_title,
            public_note=item.public_note,
        ),
        "body": item.manual_text_content.body,
        **discovery_metadata_form_data_from_item(item),
    }


def _photo_form_data_from_item(item: ArchiveItem) -> dict:
    photo_content = getattr(item, "photo_content", None)
    return {
        **_archive_metadata_form_data(
            title=item.title,
            visibility=item.visibility,
            date_start=item.date_start,
            date_end=item.date_end,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            public_note=item.public_note,
        ),
        **photo_metadata_form_data_from_content(photo_content),
        **discovery_metadata_form_data_from_item(item),
    }


def _archive_item_type_choices() -> list[tuple[str, str]]:
    return archive_manage_item_type_ui_choices()


def _normalized_archive_item_type(raw: str | None) -> str:
    value = (raw or "").strip()
    if value in _VALID_ARCHIVE_ITEM_CREATE_TYPES:
        return value
    return ""


def _submit_manual_text_create(request):
    parsed, form_errors = parse_manual_text_form(request.POST)
    parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
        request.POST,
        tags_field="tags",
    )
    form_errors = form_errors + discovery_errors
    form_data = {**parsed, **parsed_discovery}
    if form_errors:
        return None, form_data, form_errors
    with transaction.atomic():
        item = create_manual_text_archive_item(
            title=parsed["title"],
            body=parsed["body"],
            visibility=parsed["visibility"],
            date_start=parsed["date_start_value"],
            date_end=parsed["date_end_value"],
            date_precision=parsed["date_precision"],
            metadata_status=parsed["metadata_status"],
            author_name=parsed["author_name"],
            source_title=parsed["source_title"],
            public_note=parsed["public_note"],
        )
        update_archive_item_discovery_metadata(
            item,
            category_names=parsed_discovery["category_names"],
            event_names=parsed_discovery["event_names"],
            tag_names=parsed_discovery["tag_names"],
        )
    return redirect("archive-detail", item_id=item.id), form_data, form_errors


def _archive_browse_select_related(queryset):
    return queryset.select_related(
        "manual_text_content",
        "ocr_document",
        "photo_content",
    ).prefetch_related(
        "categories",
        "events",
        "tags",
        archive_browse_displayable_text_results_prefetch(),
    )


def _archive_browse_items_queryset(user, **filter_kwargs):
    return _archive_browse_select_related(
        archive_browse_queryset_for_user(user).filter(**filter_kwargs)
    ).order_by("-created_at")


def _archive_browse_page_context(*, page_title: str, items) -> dict:
    return {
        "page_title": page_title,
        "items": items,
        "browse_cards": build_archive_browse_cards(items),
    }


def archive_category_browse_page(request, category_id: int):
    try:
        category = ArchiveCategory.objects.get(id=category_id)
    except ArchiveCategory.DoesNotExist:
        raise Http404() from None

    items = _archive_browse_items_queryset(request.user, categories=category)
    return render(
        request,
        "documents/archive/browse.html",
        context=_archive_browse_page_context(
            page_title=f"קטגוריה: {category.name}",
            items=items,
        ),
    )


def archive_event_browse_page(request, event_id: int):
    try:
        event = ArchiveEvent.objects.get(id=event_id)
    except ArchiveEvent.DoesNotExist:
        raise Http404() from None

    items = _archive_browse_items_queryset(request.user, events=event)
    return render(
        request,
        "documents/archive/browse.html",
        context=_archive_browse_page_context(
            page_title=f"אירוע: {event.name}",
            items=items,
        ),
    )


def archive_tag_browse_page(request, tag_id: int):
    try:
        tag = Tag.objects.get(id=tag_id)
    except Tag.DoesNotExist:
        raise Http404() from None

    items = _archive_browse_items_queryset(request.user, tags=tag)
    return render(
        request,
        "documents/archive/browse.html",
        context=_archive_browse_page_context(
            page_title=f"תגית: {tag.name}",
            items=items,
        ),
    )


def archive_list_page(request):
    item_type_filter = normalize_archive_public_list_type_filter(
        request.GET.get("item_type")
    )
    search_query = normalize_archive_list_search_query(request.GET.get("q"))
    items = _archive_browse_select_related(
        archive_browse_queryset_for_user(request.user)
    ).order_by("-created_at")
    items = filter_archive_items_by_public_list_type(items, item_type_filter)
    items = filter_archive_items_by_search_query(items, search_query)
    return render(
        request,
        "documents/archive/list.html",
        context={
            "items": items,
            "browse_cards": build_archive_browse_cards(items),
            "is_admin": _is_admin(request.user),
            "item_type_filter": item_type_filter,
            "item_type_filter_choices": ARCHIVE_PUBLIC_LIST_TYPE_FILTER_CHOICES,
            "q": search_query,
        },
    )


def archive_detail_page(request, item_id: int):
    detail_qs = ArchiveItem.objects.prefetch_related("categories", "events", "tags")
    item = get_viewable_archive_item(request.user, item_id, queryset=detail_qs)

    if item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        doc = Document.objects.filter(archive_item_id=item.id).first()
        if doc is None:
            raise Http404()
        return redirect("documents-detail-page", doc_id=doc.id)

    if item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        return render(
            request,
            "documents/archive/detail.html",
            context={
                "item": item,
                "body": item.manual_text_content.body,
                "photo_url": None,
                "is_admin": _is_admin(request.user),
            },
        )

    if item.item_type == ArchiveItem.ItemType.PHOTO:
        photo_content = getattr(item, "photo_content", None)
        if (
            photo_content is None
            or photo_content.upload_status != PhotoContent.UploadStatus.UPLOADED
            or not (photo_content.original_file_key or "").strip()
        ):
            raise Http404()

        photo_url = None
        bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
        if bucket:
            photo_url = create_presigned_get(
                bucket=bucket,
                key=photo_content.original_file_key,
                expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
            )

        return render(
            request,
            "documents/archive/detail.html",
            context={
                "item": item,
                "body": None,
                "photo_url": photo_url,
                "photo_content": photo_content,
                "is_admin": _is_admin(request.user),
            },
        )

    raise Http404()


@login_required
def archive_manage_list_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    items = (
        ArchiveItem.objects.all()
        .select_related("manual_text_content", "ocr_document", "photo_content")
        .order_by("-created_at")
    )
    return render(
        request,
        "documents/archive/manage_list.html",
        context={"items": items},
    )


@login_required
def archive_manage_new_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    item_type = _normalized_archive_item_type(
        request.POST.get("item_type") or request.GET.get("item_type")
    )
    form_errors: list[str] = []
    form_data = _empty_manual_text_form_data()

    if request.method == "POST" and item_type == ARCHIVE_ITEM_TYPE_MANUAL_TEXT:
        success_redirect, form_data, form_errors = _submit_manual_text_create(request)
        if success_redirect:
            return success_redirect

    context = {
        "item_type": item_type,
        "item_type_choices": _archive_item_type_choices(),
        **_manual_text_form_context(
            form_data=form_data,
            form_errors=form_errors,
            page_title="יצירת פריט חדש",
            submit_label="שמירה",
        ),
    }
    if item_type == ARCHIVE_ITEM_TYPE_MANUAL_TEXT:
        context.update(_manual_text_discovery_metadata_form_context())
    elif item_type == ARCHIVE_ITEM_TYPE_OCR_DOCUMENT:
        context.update(_upload_form_context())
    elif item_type == ARCHIVE_ITEM_TYPE_PHOTO:
        context.update(_photo_upload_form_context())
    return render(
        request,
        "documents/archive/manage_new.html",
        context=context,
    )


@login_required
def archive_manage_manual_text_create_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    form_errors: list[str] = []
    form_data = _empty_manual_text_form_data()

    if request.method == "POST":
        success_redirect, form_data, form_errors = _submit_manual_text_create(request)
        if success_redirect:
            return success_redirect

    return render(
        request,
        "documents/archive/manual_text_form.html",
        context={
            **_manual_text_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="יצירת טקסט מוקלד",
                submit_label="שמירה",
            ),
            **_manual_text_discovery_metadata_form_context(),
        },
    )


@login_required
def archive_manage_edit_page(request, item_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    try:
        item = (
            ArchiveItem.objects.select_related(
                "manual_text_content",
                "ocr_document",
                "photo_content",
            )
            .prefetch_related("categories", "events", "tags")
            .get(id=item_id)
        )
    except ArchiveItem.DoesNotExist:
        raise Http404() from None

    if item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        return _archive_manage_edit_manual_text(request, item)
    if item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        return _archive_manage_edit_ocr_document(request, item)
    if item.item_type == ArchiveItem.ItemType.PHOTO:
        return _archive_manage_edit_photo(request, item)
    raise Http404()


def _archive_manage_edit_manual_text(request, item: ArchiveItem):
    form_errors: list[str] = []
    form_data = _manual_text_form_data_from_item(item)

    if request.method == "POST":
        parsed, form_errors = parse_manual_text_form(request.POST)
        parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
            request.POST,
            tags_field="tags",
        )
        form_errors = form_errors + discovery_errors
        form_data = {**parsed, **parsed_discovery}
        if not form_errors:
            with transaction.atomic():
                update_manual_text_archive_item(
                    item,
                    title=parsed["title"],
                    body=parsed["body"],
                    visibility=parsed["visibility"],
                    date_start=parsed["date_start_value"],
                    date_end=parsed["date_end_value"],
                    date_precision=parsed["date_precision"],
                    metadata_status=parsed["metadata_status"],
                    author_name=parsed["author_name"],
                    source_title=parsed["source_title"],
                    public_note=parsed["public_note"],
                )
                update_archive_item_discovery_metadata(
                    item,
                    category_names=parsed_discovery["category_names"],
                    event_names=parsed_discovery["event_names"],
                    tag_names=parsed_discovery["tag_names"],
                )
            return redirect("archive-detail", item_id=item.id)

    return render(
        request,
        "documents/archive/manual_text_form.html",
        context={
            **_manual_text_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="עריכת טקסט מוקלד",
                submit_label="עדכון",
            ),
            **_manual_text_discovery_metadata_form_context(),
        },
    )


def _archive_manage_edit_photo(request, item: ArchiveItem):
    form_errors: list[str] = []
    form_data = _photo_form_data_from_item(item)

    if request.method == "POST":
        parsed, form_errors = parse_photo_staff_metadata_form(request.POST)
        parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
            request.POST,
            tags_field="tags",
        )
        form_errors = form_errors + discovery_errors
        form_data = {**parsed, **parsed_discovery}
        if not form_errors:
            with transaction.atomic():
                update_photo_archive_item_metadata(
                    item,
                    title=parsed["title"],
                    visibility=parsed["visibility"],
                    date_start=parsed["date_start_value"],
                    date_end=parsed["date_end_value"],
                    date_precision=parsed["date_precision"],
                    metadata_status=parsed["metadata_status"],
                    description=parsed["description"],
                    location=parsed["location"],
                    context=parsed["context"],
                    people_present=parsed["people_present"],
                    notes=parsed["notes"],
                    public_note=parsed["public_note"],
                )
                update_archive_item_discovery_metadata(
                    item,
                    category_names=parsed_discovery["category_names"],
                    event_names=parsed_discovery["event_names"],
                    tag_names=parsed_discovery["tag_names"],
                )
            return redirect("archive-manage-list")

    return render(
        request,
        "documents/archive/photo_form.html",
        context={
            "item": item,
            **_archive_metadata_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="עריכת תמונה",
                submit_label="עדכון",
            ),
            **_manual_text_discovery_metadata_form_context(),
        },
    )


def _archive_manage_edit_ocr_document(request, item: ArchiveItem):
    doc = (
        Document.objects.select_related("archive_item", "admin_meta")
        .filter(archive_item_id=item.id)
        .first()
    )
    if doc is None:
        raise Http404()

    form_errors: list[str] = []
    form_data = _ocr_document_edit_form_data_from_document(doc)

    if request.method == "POST":
        parsed_shared, shared_errors = parse_archive_metadata_form(request.POST)
        parsed_catalog, catalog_errors = parse_ocr_catalog_metadata_form(request.POST)
        parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
            request.POST,
            tags_field="discovery_tags",
        )
        form_errors = shared_errors + catalog_errors + discovery_errors
        form_data = {
            **parsed_shared,
            **parsed_catalog,
            **parsed_discovery,
        }
        if not form_errors:
            with transaction.atomic():
                update_ocr_document_metadata(
                    doc,
                    title=parsed_shared["title"],
                    visibility=parsed_shared["visibility"],
                    date_start=parsed_shared["date_start_value"],
                    date_end=parsed_shared["date_end_value"],
                    date_precision=parsed_shared["date_precision"],
                    metadata_status=parsed_shared["metadata_status"],
                    author_name=parsed_shared["author_name"],
                    source_title=parsed_shared["source_title"],
                    public_note=parsed_shared["public_note"],
                )
                update_ocr_document_catalog_metadata(
                    doc,
                    donor=parsed_catalog["donor"],
                    collection=parsed_catalog["collection"],
                    original_location=parsed_catalog["original_location"],
                    notes=parsed_catalog["notes"],
                )
                update_archive_item_discovery_metadata(
                    item,
                    category_names=parsed_discovery["category_names"],
                    event_names=parsed_discovery["event_names"],
                    tag_names=parsed_discovery["tag_names"],
                )
            return redirect("documents-detail-page", doc_id=doc.id)

    return render(
        request,
        "documents/archive/ocr_document_form.html",
        context={
            **_archive_metadata_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="עריכת מטא־דאטה",
                submit_label="עדכון",
            ),
            "show_discovery_metadata": True,
            **_ocr_discovery_metadata_form_context(),
        },
    )


@login_required
def archive_manage_family_access_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    User = get_user_model()

    try:
        family_group = Group.objects.get(name=ARCHIVE_FAMILY_GROUP_NAME)
    except Group.DoesNotExist:
        return render(
            request,
            "documents/archive/manage_family_access.html",
            context={
                "group_missing": True,
                "users": User.objects.none(),
                "search_query": "",
            },
        )

    search_query = (request.GET.get("q") or request.POST.get("q") or "").strip()

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action not in ("add", "remove"):
            return HttpResponseBadRequest("פעולה לא תקינה.")

        user_id_raw = request.POST.get("user_id")
        try:
            target_user_id = int(user_id_raw)
        except (TypeError, ValueError):
            return HttpResponseBadRequest("מזהה משתמש לא תקין.")

        target_user = get_object_or_404(User, pk=target_user_id)

        if target_user.is_staff or target_user.is_superuser:
            return HttpResponseBadRequest(
                "לא ניתן לשנות גישת משפחה למשתמשי צוות או מנהלים."
            )

        if action == "add":
            if not _user_has_usable_email(target_user):
                return HttpResponseBadRequest(
                    "לא ניתן להוסיף גישת משפחה למשתמש ללא דוא״ל."
                )
            target_user.groups.add(family_group)
            messages.success(request, "גישת המשפחה נוספה למשתמש.")
        else:
            target_user.groups.remove(family_group)
            messages.success(request, "גישת המשפחה הוסרה מהמשתמש.")

        redirect_url = reverse("archive-manage-family-access")
        if search_query:
            redirect_url = f"{redirect_url}?{urlencode({'q': search_query})}"
        return redirect(redirect_url)

    family_membership = Group.objects.filter(
        pk=family_group.pk,
        user=OuterRef("pk"),
    )
    users = (
        User.objects.annotate(has_family_access=Exists(family_membership))
        .annotate(_trimmed_email_len=Length(Trim("email")))
        .annotate(
            has_usable_email=Case(
                When(_trimmed_email_len__gt=0, then=Value(True)),
                default=Value(False),
                output_field=BooleanField(),
            )
        )
        .order_by("username")
    )
    if search_query:
        users = users.filter(
            Q(username__icontains=search_query)
            | Q(email__icontains=search_query)
            | Q(first_name__icontains=search_query)
            | Q(last_name__icontains=search_query)
        )

    return render(
        request,
        "documents/archive/manage_family_access.html",
        context={
            "group_missing": False,
            "users": users,
            "search_query": search_query,
        },
    )


@login_required
def archive_manage_delete_page(request, item_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    try:
        item = ArchiveItem.objects.select_related(
            "manual_text_content",
            "photo_content",
        ).get(
            id=item_id,
            item_type__in=(
                ArchiveItem.ItemType.MANUAL_TEXT,
                ArchiveItem.ItemType.PHOTO,
            ),
        )
    except ArchiveItem.DoesNotExist:
        raise Http404() from None

    if request.method == "POST":
        with transaction.atomic():
            item.delete()
        return redirect("archive-manage-list")

    if item.item_type == ArchiveItem.ItemType.PHOTO:
        template_name = "documents/archive/photo_delete_confirm.html"
    else:
        template_name = "documents/archive/manual_text_delete_confirm.html"

    return render(
        request,
        template_name,
        context={"item": item},
    )
