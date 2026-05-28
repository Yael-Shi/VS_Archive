import json
import logging
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Document, DocumentSourceFile, DocumentTextResult, Tag, DocumentMetadata
from .s3 import (
    build_document_source_file_s3_key,
    create_presigned_get,
    create_presigned_put,
    mime_type_to_extension,
)
from documents.services.review_backlog import (
    attach_review_summaries,
    documents_in_review_backlog,
    is_review_editable_text_result,
    is_review_pending_text_result,
    parse_review_reasons,
)
from documents.services.source_files import (
    MULTI_IMAGE_MAX_FILES,
    MULTI_IMAGE_MIN_FILES,
    all_expected_source_files_uploaded,
    get_source_file_for_order,
    is_multi_image_document,
    mirror_primary_document_from_source_file,
    sync_primary_document_source_file,
)
from documents.services.sqs import send_process_document_message
from documents.services.text_presentation import get_text_presentation_for_document

logger = logging.getLogger(__name__)


def _bad(msg: str, status: int = 400):
    # status kept for compatibility (caller may expect it)
    return HttpResponseBadRequest(msg)


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
    return bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))


def _require_admin(request):
    if not _is_admin(request.user):
        return HttpResponseForbidden("Admins only")
    return None


def _require_admin_page(request):
    if not _is_admin(request.user):
        return render(request, "public/forbidden.html", status=403)
    return None


def _base_queryset(
    *,
    q: str,
    upload_status: str,
    visibility: str,
    doc_type: str,
    metadata_status: str,
    is_admin: bool,
):
    qs = (
        Document.objects.all()
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
        qs = qs.filter(metadata_status=metadata_status)

    # visibility is admin-only operational field
    if is_admin and visibility:
        qs = qs.filter(visibility=visibility)

    q = (q or "").strip()
    if q:
        filters = (
            Q(title__icontains=q)
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

    payload = {
        "id": d.id,
        "title": d.title,
        "date_start": d.date_start.isoformat() if d.date_start else None,
        "date_end": d.date_end.isoformat() if d.date_end else None,
        "language": d.language,
        "text_input_type": d.text_input_type,
        "doc_type": d.doc_type,
        "category_event": d.category_event,
        "tags": [t.name for t in d.tags_m2m.all()],
        "metadata_status": getattr(d, "metadata_status", None),
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
                "visibility": d.visibility,
                "file_s3_key": d.file_s3_key,
                "file_original_name": d.file_original_name,
                "mime_type": d.mime_type,
                "size_bytes": d.size_bytes,
                "upload_error": d.upload_error,
            }
        )

    return payload


def _uploads_bucket_or_error():
    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    if not bucket:
        return None, JsonResponse(
            {"error": "Bucket not configured (set UPLOADS_BUCKET_NAME or S3_BUCKET)"},
            status=500,
        )
    return bucket, None


def _parse_create_upload_common(payload: dict):
    title = (payload.get("title") or "").strip()
    if not title:
        return None, _bad("title required")

    date_start_raw = payload.get("date_start")
    date_end_raw = payload.get("date_end")
    language = (payload.get("language") or "").strip() or None
    text_input_type_raw = payload.get("text_input_type")
    category_event = (payload.get("category_event") or "").strip() or None
    visibility = (payload.get("visibility") or "private").strip()
    tags = payload.get("tags", None)
    admin_meta = payload.get("admin_meta", None)

    try:
        ds = _parse_date_optional(date_start_raw, "date_start")
        de = _parse_date_optional(date_end_raw, "date_end")
        text_input_type = _parse_text_input_type(text_input_type_raw)
    except ValueError as e:
        return None, _bad(str(e))

    if tags is None:
        tags = []
    if not isinstance(tags, list):
        return None, _bad("tags must be a list")

    if admin_meta is None:
        admin_meta = {}
    if not isinstance(admin_meta, dict):
        return None, _bad("admin_meta must be an object")

    if visibility not in ("private", "public"):
        return None, _bad("visibility must be private or public")

    return {
        "title": title,
        "date_start": ds,
        "date_end": de,
        "language": language,
        "text_input_type": text_input_type,
        "category_event": category_event,
        "visibility": visibility,
        "tags": tags,
        "admin_meta": admin_meta,
    }, None


def _attach_document_tags_and_metadata(doc: Document, tags: list, admin_meta: dict) -> None:
    DocumentMetadata.objects.create(
        document=doc,
        notes=str(admin_meta.get("notes") or ""),
        donor=str(admin_meta.get("donor") or ""),
        collection=str(admin_meta.get("collection") or ""),
        original_location=str(admin_meta.get("original_location") or ""),
    )

    for raw in tags:
        if raw is None:
            continue
        name = str(raw).strip()
        if not name:
            continue
        tag_obj, _ = Tag.objects.get_or_create(name=name)
        doc.tags_m2m.add(tag_obj)


def _is_image_mime_type(mime_type: str) -> bool:
    return (mime_type or "").strip().lower().startswith("image/")


def _create_multi_image_upload(request, payload: dict, common: dict):
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

    parsed_files = []
    for index, entry in enumerate(files_raw):
        if not isinstance(entry, dict):
            return _bad(f"files[{index}] must be an object")
        if "order_index" in entry:
            return _bad("order_index must not be provided; order is defined by files[] position")

        original_name = (entry.get("original_name") or "").strip()
        if not original_name:
            return _bad(f"files[{index}].original_name is required")

        mime_type = (
            entry.get("mime_type") or entry.get("content_type") or ""
        ).strip()
        if not _is_image_mime_type(mime_type):
            return _bad(f"files[{index}].mime_type must be an image/* MIME type")

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

    bucket, bucket_err = _uploads_bucket_or_error()
    if bucket_err:
        return bucket_err

    doc = Document.objects.create(
        title=common["title"],
        doc_type=Document.DocType.IMAGE,
        date_start=common["date_start"],
        date_end=common["date_end"],
        language=common["language"],
        text_input_type=common["text_input_type"],
        category_event=common["category_event"],
        visibility=common["visibility"],
        upload_status=Document.UploadStatus.UPLOADING,
        expected_source_file_count=file_count,
    )
    _attach_document_tags_and_metadata(doc, common["tags"], common["admin_meta"])

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


def _create_single_file_upload(request, payload: dict, common: dict):
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

    bucket, bucket_err = _uploads_bucket_or_error()
    if bucket_err:
        return bucket_err

    doc = Document.objects.create(
        title=common["title"],
        doc_type=doc_type,
        date_start=common["date_start"],
        date_end=common["date_end"],
        language=common["language"],
        text_input_type=common["text_input_type"],
        category_event=common["category_event"],
        visibility=common["visibility"],
        upload_status=Document.UploadStatus.UPLOADING,
        file_original_name=original_name,
        mime_type=mime_type,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )
    _attach_document_tags_and_metadata(doc, common["tags"], common["admin_meta"])

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


@csrf_exempt
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
    if err:
        return err

    if "files" in payload:
        return _create_multi_image_upload(request, payload, common)

    return _create_single_file_upload(request, payload, common)


@csrf_exempt
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
            doc.save(update_fields=["upload_status", "upload_error", "processing_state_user"])
            return JsonResponse(
                {"error": "file_s3_key missing", "document_id": doc.id},
                status=400,
            )

        already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

        doc.upload_status = Document.UploadStatus.UPLOADED
        doc.upload_error = None

        if isinstance(payload.get("file_size"), int):
            doc.size_bytes = payload["file_size"]
        if isinstance(payload.get("file_mime"), str):
            doc.mime_type = payload["file_mime"]

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
        raw_err = (payload.get("error") or "upload failed")
        err = str(raw_err).strip() or "upload failed"

        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(update_fields=["upload_status", "upload_error", "processing_state_user"])

    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "processing_state_user": doc.processing_state_user,
        }
    )


def _parse_upload_success_payload(request):
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


@csrf_exempt
@login_required
def upload_part_complete(request, doc_id: int, order_index: int):
    deny = _require_admin(request)
    if deny:
        return deny

    payload, success, err = _parse_upload_success_payload(request)
    if err:
        return err

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
            if file_mime and not _is_image_mime_type(file_mime):
                return JsonResponse(
                    {
                        "error": "file_mime must be an image/* MIME type",
                        "document_id": doc.id,
                        "order_index": order_index,
                    },
                    status=400,
                )

        source_file.upload_status = DocumentSourceFile.UploadStatus.UPLOADED
        source_file.upload_error = None
        if isinstance(payload.get("file_size"), int):
            source_file.size_bytes = payload["file_size"]
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
        raw_err = (payload.get("error") or "upload failed")
        err = str(raw_err).strip() or "upload failed"
        source_file.upload_status = DocumentSourceFile.UploadStatus.FAILED
        source_file.upload_error = err
        source_file.save(update_fields=["upload_status", "upload_error", "updated_at"])

        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(update_fields=["upload_status", "upload_error", "processing_state_user"])

    return JsonResponse(
        {
            "document_id": doc.id,
            "order_index": order_index,
            "upload_status": source_file.upload_status,
            "document_upload_status": doc.upload_status,
        }
    )


@csrf_exempt
@login_required
def upload_finalize(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    payload, success, err = _parse_upload_success_payload(request)
    if err:
        return err

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
        raw_err = (payload.get("error") or "upload finalize failed")
        err = str(raw_err).strip() or "upload finalize failed"
        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = err
        doc.processing_state_user = Document.ProcessingState.FAILED
        doc.save(update_fields=["upload_status", "upload_error", "processing_state_user"])
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

    mirror_primary_document_from_source_file(doc, primary)
    doc.upload_status = Document.UploadStatus.UPLOADED
    doc.upload_error = None
    doc.processing_state_user = Document.ProcessingState.PARTIAL
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

    return _finalize_response(doc)


@login_required
def documents_list_api(request):
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = _parse_int(request.GET.get("limit"), default=50, min_value=1, max_value=200)
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    is_admin = _is_admin(request.user)

    qs = _base_queryset(
        q=q,
        upload_status=upload_status,
        visibility=visibility,
        doc_type=doc_type,
        metadata_status=metadata_status,
        is_admin=is_admin,
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

    return JsonResponse({"count": total, "limit": limit, "offset": offset, "items": items})


@login_required
def documents_list_page(request):
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = 50
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    is_admin = _is_admin(request.user)

    qs = _base_queryset(
        q=q,
        upload_status=upload_status,
        visibility=visibility,
        doc_type=doc_type,
        metadata_status=metadata_status,
        is_admin=is_admin,
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

    limit = 50
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    only_missing_tags = (request.GET.get("only_missing_tags") or "").strip() == "1"
    only_missing_admin_meta = (request.GET.get("only_missing_admin_meta") or "").strip() == "1"

    base_qs = (
        Document.objects.select_related("admin_meta")
        .prefetch_related("tags_m2m")
        .filter(metadata_status=Document.MetadataStatus.NEEDS_COMPLETION)
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

    limit = 50
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
        qs.prefetch_related("text_results")[offset : offset + limit]
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
        "text_input_type_choices": Document.TextInputType.choices,
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
            return "תמלול מקור (עברית כפי שחולצה)"
        if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
            return "טקסט עברי לבדיקה"

    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return "תמלול מקור"
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return "טקסט עברי"
    return result_type


@login_required
def review_detail_page(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_object_or_404(
        Document.objects.select_related("admin_meta").prefetch_related(
            "tags_m2m", "text_results", "transkribus_runs"
        ),
        id=doc_id,
    )
    admin_meta = getattr(doc, "admin_meta", None)

    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    content_url = None
    if bucket and doc.file_s3_key:
        content_url = create_presigned_get(bucket=bucket, key=doc.file_s3_key, expires_in=3600)

    text_results = sorted(
        doc.text_results.all(),
        key=lambda r: (r.result_type, r.engine, -r.updated_at.timestamp()),
    )
    text_result_cards = []
    for row in text_results:
        text_result_cards.append(
            {
                "row": row,
                "result_type_label": _review_result_type_label(doc, row.result_type),
                "review_reasons": parse_review_reasons(row.review_reasons),
                "text_length": len((row.text or "").strip()),
                "is_pending_review": is_review_pending_text_result(row),
                "is_editable": is_review_editable_text_result(row),
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
        "content_url": content_url,
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
    return redirect(f"/api/ui/admin/review/{row.document_id}/")


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
    return redirect(f"/api/ui/admin/review/{row.document_id}/")


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
    return redirect(f"/api/ui/admin/review/{row.document_id}/")


@login_required
def document_detail_page(request, doc_id: int):
    try:
        doc = (
            Document.objects.select_related("admin_meta")
            .prefetch_related("tags_m2m", "text_results")
            .get(id=doc_id)
        )
        admin_meta = getattr(doc, "admin_meta", None)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    content_url = None

    if bucket and doc.file_s3_key:
        content_url = create_presigned_get(bucket=bucket, key=doc.file_s3_key, expires_in=3600)

    text_presentation = get_text_presentation_for_document(doc)

    context = {
        "doc": doc,
        "content_url": content_url,
        "admin_meta": admin_meta,
        "text_presentation": text_presentation,
        "is_admin": _is_admin(request.user),
    }

    logger.info(
        "document_detail_page user=%s doc_id=%s admin=%s has_content_url=%s mime_type=%r missing_text=%s",
        getattr(request.user, "username", None),
        doc.id,
        _is_admin(request.user),
        bool(content_url),
        doc.mime_type,
        text_presentation.missing,
    )

    return render(request, "documents/detail.html", context)


@login_required
def upload_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny
    return render(
        request,
        "documents/upload.html",
        context={
            "doc_type_choices": Document.DocType.choices,
            "text_input_type_choices": Document.TextInputType.choices,
        },
    )
