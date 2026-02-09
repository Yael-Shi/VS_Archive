import json
import logging
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Q
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import Document, Tag, DocumentMetadata
from documents.services.sqs import send_process_document_message
from documents.services.text_presentation import get_text_presentation_for_document
from .s3 import create_presigned_put, create_presigned_get

logger = logging.getLogger(__name__)


def _bad(msg: str, status: int = 400):
    # Note: status is kept for compatibility/future use (currently returns HttpResponseBadRequest only).
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
    # Parse YYYY-MM-DD if provided; otherwise return None.
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"invalid {field_name} format, expected YYYY-MM-DD")


def _is_admin(user):
    return bool(
        getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)
    )


def _base_queryset(
    q: str,
    upload_status: str,
    visibility: str,
    doc_type: str,
    metadata_status: str,
):
    # admin_meta is a OneToOne relation; tags_m2m is ManyToMany.
    qs = (
        Document.objects.all()
        .select_related("admin_meta")
        .prefetch_related("tags_m2m")
        .order_by("-created_at")
    )

    if upload_status:
        qs = qs.filter(upload_status=upload_status)

    if visibility:
        qs = qs.filter(visibility=visibility)

    if doc_type:
        qs = qs.filter(doc_type=doc_type)

    if metadata_status:
        qs = qs.filter(metadata_status=metadata_status)

    q = (q or "").strip()
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(category_event__icontains=q)
            | Q(file_original_name__icontains=q)
            | Q(tags_m2m__name__icontains=q)
            | Q(admin_meta__notes__icontains=q)
            | Q(admin_meta__donor__icontains=q)
            | Q(admin_meta__collection__icontains=q)
            | Q(admin_meta__original_location__icontains=q)
        ).distinct()

    return qs


def _serialize_doc(d: Document) -> dict:
    admin_meta = None
    # select_related("admin_meta") ensures this is not an extra query.
    if getattr(d, "admin_meta", None) is not None:
        m = d.admin_meta
        admin_meta = {
            "notes": m.notes,
            "donor": m.donor,
            "collection": m.collection,
            "original_location": m.original_location,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        }

    return {
        "id": d.id,
        "title": d.title,
        "date_start": d.date_start.isoformat() if d.date_start else None,
        "date_end": d.date_end.isoformat() if d.date_end else None,
        "language": d.language,
        "doc_type": d.doc_type,
        "category_event": d.category_event,
        "tags": [t.name for t in d.tags_m2m.all()],
        "admin_meta": admin_meta,
        "metadata_status": getattr(d, "metadata_status", None),
        "visibility": d.visibility,
        "upload_status": d.upload_status,
        "processing_state_user": d.processing_state_user,
        "file_s3_key": d.file_s3_key,
        "file_original_name": d.file_original_name,
        "mime_type": d.mime_type,
        "size_bytes": d.size_bytes,
        "upload_error": d.upload_error,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


@csrf_exempt
def create_upload(request):
    if request.method != "POST":
        return _bad("POST only")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _bad("invalid json")

    # Required for V1: title + doc_type
    title = (payload.get("title") or "").strip()
    if not title:
        return _bad("title required")

    doc_type = (payload.get("doc_type") or "").strip()  # PDF / IMAGE
    if doc_type not in ("PDF", "IMAGE"):
        return _bad("doc_type must be PDF or IMAGE")

    # Optional metadata
    date_start_raw = payload.get("date_start")
    date_end_raw = payload.get("date_end")
    language = (payload.get("language") or "").strip() or None
    category_event = (payload.get("category_event") or "").strip() or None
    visibility = (payload.get("visibility") or "private").strip()

    tags = payload.get("tags", None)

    admin_meta = payload.get("admin_meta", None)

    # File info
    mime_type = (
        payload.get("mime_type")
        or payload.get("content_type")
        or "application/octet-stream"
    ).strip()
    original_name = (payload.get("original_name") or "").strip()
    size_bytes = payload.get("size_bytes")

    # Optional date parsing
    try:
        ds = _parse_date_optional(date_start_raw, "date_start")
        de = _parse_date_optional(date_end_raw, "date_end")
    except ValueError as e:
        return _bad(str(e))

    # Optional fields validation (only if provided)
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        return _bad("tags must be a list")

    if admin_meta is None:
        admin_meta = {}
    if not isinstance(admin_meta, dict):
        return _bad("admin_meta must be an object")

    if visibility not in ("private", "public"):
        return _bad("visibility must be private or public")

    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    if not bucket:
        return JsonResponse(
            {"error": "Bucket not configured (set UPLOADS_BUCKET_NAME or S3_BUCKET)"},
            status=500,
        )

    # Create Document in UPLOADING state
    doc = Document.objects.create(
        title=title,
        doc_type=doc_type,
        date_start=ds,
        date_end=de,
        language=language,
        category_event=category_event,
        visibility=visibility,
        upload_status=Document.UploadStatus.UPLOADING,
        file_original_name=original_name,
        mime_type=mime_type,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )

    DocumentMetadata.objects.create(
        document=doc,
        notes=str(admin_meta.get("notes") or ""),
        donor=str(admin_meta.get("donor") or ""),
        collection=str(admin_meta.get("collection") or ""),
        original_location=str(admin_meta.get("original_location") or ""),
    )

    # Add tags via M2M
    for raw in tags:
        if raw is None:
            continue
        name = str(raw).strip()
        if not name:
            continue
        tag_obj, _ = Tag.objects.get_or_create(name=name)
        doc.tags_m2m.add(tag_obj)

    # Create a stable key
    ext = "bin"
    if mime_type == "application/pdf":
        ext = "pdf"
    elif mime_type.startswith("image/"):
        ext = mime_type.split("/", 1)[1] or "img"

    key = f"documents/{doc.id}/original.{ext}"
    doc.file_s3_key = key
    doc.save(update_fields=["file_s3_key"])

    # Presigned PUT
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
def upload_complete(request, doc_id: int):
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

    if success:
        # Prevent double-enqueue if the client calls complete twice
        already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

        doc.upload_status = Document.UploadStatus.UPLOADED
        doc.upload_error = None

        if isinstance(payload.get("file_size"), int):
            doc.size_bytes = payload["file_size"]
        if isinstance(payload.get("file_mime"), str):
            doc.mime_type = payload["file_mime"]

        # user-facing processing begins now
        doc.processing_state_user = Document.ProcessingState.PROCESSING

        update_fields = [
            "upload_status",
            "upload_error",
            "size_bytes",
            "mime_type",
            "processing_state_user",
        ]
        doc.save(update_fields=update_fields)

        # Enqueue exactly once
        if not already_uploaded:
            try:
                send_process_document_message(document_id=doc.id)
            except Exception as e:
                logger.exception(
                    "enqueue failed in upload_complete",
                    extra={"document_id": doc.id},
                )
                # If enqueue fails, reflect it clearly in user state
                doc.processing_state_user = Document.ProcessingState.FAILED
                doc.upload_error = f"enqueue failed: {e}"
                doc.save(update_fields=["processing_state_user", "upload_error"])
                return JsonResponse(
                    {"error": "enqueue failed", "details": str(e)},
                    status=500,
                )

    else:
        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = (payload.get("error") or "upload failed").strip()
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


@login_required
def documents_list_api(request):
    # List documents with free-text search over core fields + admin_meta + tags.
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = _parse_int(request.GET.get("limit"), default=50, min_value=1, max_value=200)
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    qs = _base_queryset(
        q=q,
        upload_status=upload_status,
        visibility=visibility,
        doc_type=doc_type,
        metadata_status=metadata_status,
    )
    total = qs.count()

    docs = list(qs[offset : offset + limit])
    items = [_serialize_doc(d) for d in docs]

    logger.info(
        "documents_list_api user=%s q=%r upload_status=%r visibility=%r doc_type=%r metadata_status=%r limit=%s offset=%s total=%s returned=%s",
        getattr(request.user, "username", None),
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


@login_required
def documents_list_page(request):
    # Minimal V1 UI (server-rendered) behind login.
    q = request.GET.get("q", "") or ""
    upload_status = (request.GET.get("upload_status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()
    doc_type = (request.GET.get("doc_type") or "").strip()
    metadata_status = (request.GET.get("metadata_status") or "").strip()

    limit = 50
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    qs = _base_queryset(
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
        "upload_status": upload_status,
        "visibility": visibility,
        "doc_type": doc_type,
        "metadata_status": metadata_status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total else None,
        "doc_type_choices": Document.DocType.choices,
        "metadata_status_choices": getattr(Document, "MetadataStatus", None).choices
        if hasattr(Document, "MetadataStatus")
        else [],
    }
    logger.info(
        "documents_list_page user=%s q=%r upload_status=%r visibility=%r doc_type=%r metadata_status=%r offset=%s limit=%s total=%s returned=%s",
        getattr(request.user, "username", None),
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
@user_passes_test(_is_admin)
def admin_backlog_page(request):
    # Admin-only backlog: documents with incomplete metadata.
    limit = 50
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    # Lightweight filters (UI-only; no new models)
    only_missing_tags = (request.GET.get("only_missing_tags") or "").strip() == "1"
    only_missing_admin_meta = (
        request.GET.get("only_missing_admin_meta") or ""
    ).strip() == "1"

    base_qs = (
        Document.objects.select_related("admin_meta")
        .prefetch_related("tags_m2m")
        .filter(metadata_status=Document.MetadataStatus.NEEDS_COMPLETION)
        .order_by("-created_at")
        .distinct()
    )

    # Summary counts (computed on the base backlog set, before applying UI filters)
    total_backlog = base_qs.count()
    missing_tags_count = base_qs.filter(tags_m2m__isnull=True).distinct().count()

    # "Missing admin meta content" means: admin_meta exists but all key fields are empty.
    # In your create_upload flow you always create DocumentMetadata, so this is typically
    # about "empty fields", not "missing row".
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
        "total": total_filtered,  # total shown for current filter set
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

    # If file_s3_key exists, create a presigned URL for inline viewing
    if bucket and doc.file_s3_key:
        content_url = create_presigned_get(
            bucket=bucket, key=doc.file_s3_key, expires_in=3600
        )

    text_presentation = get_text_presentation_for_document(doc)

    context = {
        "doc": doc,
        "content_url": content_url,
        "admin_meta": admin_meta,
        "text_presentation": text_presentation,
    }
    logger.info(
        "document_detail_page user=%s doc_id=%s has_content_url=%s mime_type=%r missing_text=%s",
        getattr(request.user, "username", None),
        doc.id,
        bool(content_url),
        doc.mime_type,
        text_presentation.missing,
    )

    return render(request, "documents/detail.html", context)


@login_required
def upload_page(request):
    # Minimal V1 upload UI page (Desktop).
    # The actual upload flow is executed in the browser using existing API endpoints:
    # create_upload -> presigned PUT -> upload_complete
    return render(
        request,
        "documents/upload.html",
        context={
            "doc_type_choices": Document.DocType.choices,
        },
    )
