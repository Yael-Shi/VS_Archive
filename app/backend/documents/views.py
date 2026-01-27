import json
import logging
from datetime import datetime

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .models import Document
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


def _base_queryset(q: str, status: str, visibility: str):
    qs = Document.objects.all().order_by("-created_at")

    if status:
        qs = qs.filter(upload_status=status)

    if visibility:
        qs = qs.filter(visibility=visibility)

    q = (q or "").strip()
    if q:
        # For V1 simplicity: cast JSON fields to text and do an icontains search.
        qs = qs.annotate(
            metadata_text=Cast("metadata", output_field=TextField()),
            tags_text=Cast("tags", output_field=TextField()),
        ).filter(
            Q(title__icontains=q)
            | Q(category_event__icontains=q)
            | Q(file_original_name__icontains=q)
            | Q(metadata_text__icontains=q)
            | Q(tags_text__icontains=q)
        )

    return qs


def _serialize_doc(d: Document) -> dict:
    return {
        "id": d.id,
        "title": d.title,
        "date_start": d.date_start.isoformat() if d.date_start else None,
        "date_end": d.date_end.isoformat() if d.date_end else None,
        "language": d.language,
        "doc_type": d.doc_type,
        "category_event": d.category_event,
        "tags": d.tags,
        "metadata": d.metadata,
        "visibility": d.visibility,
        "upload_status": d.upload_status,
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

    # Required by the SoT
    title = (payload.get("title") or "").strip()
    if not title:
        return _bad("title required")

    date_start = payload.get("date_start")
    date_end = payload.get("date_end")
    language = (payload.get("language") or "").strip()
    doc_type = (payload.get("doc_type") or "").strip()  # PDF / IMAGE
    category_event = (payload.get("category_event") or "").strip()
    tags = payload.get("tags")
    metadata = payload.get("metadata")
    visibility = (payload.get("visibility") or "private").strip()

    # File info
    mime_type = (payload.get("mime_type") or payload.get("content_type") or "application/octet-stream").strip()
    original_name = (payload.get("original_name") or "").strip()
    size_bytes = payload.get("size_bytes")

    # Basic validation
    if not date_start or not date_end:
        return _bad("date_start and date_end required (YYYY-MM-DD)")
    try:
        ds = datetime.strptime(date_start, "%Y-%m-%d").date()
        de = datetime.strptime(date_end, "%Y-%m-%d").date()
    except ValueError:
        return _bad("invalid date format, expected YYYY-MM-DD")

    if not language:
        return _bad("language required")
    if doc_type not in ("PDF", "IMAGE"):
        return _bad("doc_type must be PDF or IMAGE")
    if not category_event:
        return _bad("category_event required")
    if not isinstance(tags, list):
        return _bad("tags must be a list")
    if not isinstance(metadata, dict):
        return _bad("metadata must be an object")
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
        date_start=ds,
        date_end=de,
        language=language,
        doc_type=doc_type,
        category_event=category_event,
        tags=tags,
        metadata=metadata,
        visibility=visibility,
        upload_status=Document.UploadStatus.UPLOADING,
        file_original_name=original_name,
        mime_type=mime_type,
        size_bytes=size_bytes if isinstance(size_bytes, int) else None,
    )

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
        doc.upload_status = Document.UploadStatus.UPLOADED
        doc.upload_error = None
        if isinstance(payload.get("file_size"), int):
            doc.size_bytes = payload["file_size"]
        if isinstance(payload.get("file_mime"), str):
            doc.mime_type = payload["file_mime"]
        doc.save(update_fields=["upload_status", "upload_error", "size_bytes", "mime_type"])
    else:
        doc.upload_status = Document.UploadStatus.FAILED
        doc.upload_error = (payload.get("error") or "upload failed").strip()
        doc.save(update_fields=["upload_status", "upload_error"])

    return JsonResponse({"document_id": doc.id, "upload_status": doc.upload_status})


@login_required
def documents_list_api(request):
    # Additive V1 endpoint: list documents with free-text search over metadata.
    q = request.GET.get("q", "") or ""
    status = (request.GET.get("status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()

    limit = _parse_int(request.GET.get("limit"), default=50, min_value=1, max_value=200)
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    qs = _base_queryset(q=q, status=status, visibility=visibility)
    total = qs.count()

    docs = list(qs[offset : offset + limit])
    items = [_serialize_doc(d) for d in docs]

    logger.info(
        "documents_list_api user=%s q=%r status=%r visibility=%r limit=%s offset=%s total=%s returned=%s",
        getattr(request.user, "username", None),
        q,
        status,
        visibility,
        limit,
        offset,
        total,
        len(items),
    )

    return JsonResponse({"count": total, "limit": limit, "offset": offset, "items": items})


@login_required
def documents_list_page(request):
    # Minimal V1 UI (server-rendered) behind login.
    q = request.GET.get("q", "") or ""
    status = (request.GET.get("status") or "").strip()
    visibility = (request.GET.get("visibility") or "").strip()

    limit = 50
    offset = _parse_int(request.GET.get("offset"), default=0, min_value=0)

    qs = _base_queryset(q=q, status=status, visibility=visibility)
    total = qs.count()
    docs = list(qs[offset : offset + limit])

    context = {
        "docs": docs,
        "q": q,
        "status": status,
        "visibility": visibility,
        "offset": offset,
        "limit": limit,
        "total": total,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total else None,
    }
    return render(request, "documents/list.html", context)

@login_required
def document_detail_page(request, doc_id: int):
    # Minimal V1 document viewer (inline): PDF in iframe, IMAGE in img
    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    content_url = None

    # If file_s3_key exists, create a presigned URL for inline viewing
    if bucket and doc.file_s3_key:
        content_url = create_presigned_get(bucket=bucket, key=doc.file_s3_key, expires_in=3600)

    context = {
        "doc": doc,
        "content_url": content_url,
    }
    return render(request, "documents/detail.html", context)


@login_required
def upload_page(request):
    # Minimal V1 upload UI page (Desktop).
    # The actual upload flow is executed in the browser using existing API endpoints:
    # create_upload -> presigned PUT -> upload_complete
    return render(request, "documents/upload.html")