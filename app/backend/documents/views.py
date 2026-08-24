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
from django.utils.cache import add_never_cache_headers
from django.views.decorators.cache import never_cache
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
    Person,
    PersonAlias,
    PhotoContent,
    Tag,
    TranscriptionEditSuggestion,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_catalog_metadata_validation import (
    parse_ocr_catalog_metadata_form,
)
from documents.services.archive_discovery_metadata_validation import (
    discovery_metadata_option_querysets,
    empty_discovery_metadata_form_fields,
    parse_archive_item_discovery_metadata_form,
)
from documents.services.archive_item_people import (
    ArchiveItemPersonError,
    archive_item_people_form_data_from_item,
    empty_archive_item_people_form_fields,
    parse_archive_item_people_form,
    set_archive_item_people,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
    discovery_metadata_form_data_from_item,
    shared_archive_item_for_document,
    update_archive_item_discovery_metadata,
    update_manual_text_archive_item,
    update_photo_archive_item_metadata,
    update_ocr_document_catalog_metadata,
    update_ocr_document_metadata,
    update_video_archive_item,
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
from documents.services.archive_date_input import (
    archive_date_form_data,
    parse_archive_date_bounds,
)
from documents.services.archive_metadata_validation import (
    archive_metadata_form_data_for_template,
    parse_archive_metadata_form,
    parse_public_note,
    parse_visibility,
    validate_archive_metadata_fields,
    validate_source_metadata_fields,
)
from documents.services.upload_validation import (
    normalize_upload_mime_type,
    upload_mime_types_match,
    validate_image_upload_metadata,
    validate_single_file_upload_metadata,
)
from documents.services.exif_orientation import (
    ExifNormalizationError,
    ExifNormalizationResult,
    is_upload_image_mime,
    normalize_uploaded_image_exif_in_s3,
)
from documents.services.document_thumbnail import (
    schedule_document_thumbnail_after_upload,
)
from documents.services.photo_s3_cleanup import (
    schedule_photo_s3_cleanup_after_commit,
)
from documents.services.source_files import (
    MULTI_IMAGE_MAX_FILES,
    all_expected_source_files_uploaded,
    build_source_preview,
    get_source_file_for_order,
    is_incremental_multi_image_draft,
    is_multi_image_document,
    mirror_primary_document_from_source_file,
    next_incremental_part_order_index,
    sync_primary_document_source_file,
    uses_multi_image_part_endpoints,
    validate_incremental_finalize_ready,
)
from documents.services.document_access import (
    document_queryset_for_user,
    filter_documents_for_user,
    get_viewable_document,
    is_document_admin,
)
from documents.services.transkribus_page_xml_geometry import (
    TranskribusPageXmlGeometryError,
    resolve_audit_transkribus_run,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
    archive_item_queryset_for_user,
    get_accessible_archive_item,
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
    HANDWRITING_TYPE_UI_CHOICES,
    TEXT_INPUT_TYPE_UI_CHOICES,
    parse_date_precision,
)
from documents.services.manual_text_validation import parse_manual_text_form
from documents.services.video_url_contract import (
    PROVIDER_YOUTUBE,
    video_provider_display_label,
)
from documents.services.video_presentation import build_video_public_presentation
from documents.services.video_validation import (
    format_video_time_for_form,
    parse_video_archive_item_form,
    video_presentation_mode_explanation,
)
from documents.services.photo_upload import (
    create_additional_photo_upload_plan,
    create_photo_upload_plan,
    finalize_photo_upload,
    parse_add_photo_upload_metadata,
    parse_create_photo_upload_metadata,
)
from documents.services.photo_metadata_validation import (
    empty_photo_metadata_form_data,
    parse_photo_content_staff_form,
    photo_content_staff_form_data,
)
from documents.services.photo_content_management import (
    ARCHIVE_ITEM_NOT_PHOTO_ERROR,
    LAST_PHOTO_DELETE_ERROR,
    PHOTO_NOT_IN_ITEM_ERROR,
    PhotoContentManagementError,
    build_staff_person_choices,
    build_staff_photo_manage_rows,
    create_person_alias,
    delete_one_photo_content,
    delete_person_alias,
    reorder_photo_contents,
    staff_person_aliases_prefetch,
    staff_photo_contents_queryset,
    update_person_alias,
    update_person_name,
    update_photo_content_metadata,
)
from documents.services.document_archive_urls import (
    apply_document_thumbnail_urls_to_browse_cards,
)
from documents.services.photo_archive_urls import (
    apply_photo_thumbnail_urls_to_browse_cards,
)
from documents.services.photo_gallery import build_public_photo_gallery
from documents.services.archive_advanced_search import (
    EMPTY_ARCHIVE_ADVANCED_FILTER_CHOICE_CONTEXT,
    archive_advanced_filter_choice_context,
    archive_advanced_filter_template_context,
    archive_advanced_panel_is_requested,
    archive_advanced_year_form_values,
    filter_archive_items_by_advanced_filters,
    filters_for_archive_list_search,
    should_load_archive_advanced_filter_choices,
    validate_archive_advanced_year_fields,
)
from documents.services.archive_item_presentation import (
    archive_browse_displayable_text_results_prefetch,
    archive_manage_item_type_ui_choices,
    archive_metadata_status_ui_choices,
    archive_public_list_active_filter_summary_context,
    archive_public_list_filter_context,
    archive_public_list_pagination_context,
    archive_visibility_ui_choices,
    aggregate_archive_public_list_type_counts,
    build_archive_browse_cards,
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
    normalize_archive_public_list_page,
    normalize_archive_public_list_per_page,
    normalize_archive_public_list_type_filter,
    normalize_archive_list_search_query,
)
from documents.services.archive_search_snippets import (
    apply_archive_search_match_presentation_to_cards,
)
from documents.services.archive_search_match_ranges import (
    resolve_archive_search_geometry_matches,
)
from documents.services.archive_search_overlay_payload import (
    build_archive_search_overlay_targets,
)
from documents.services.archive_search_overlay_pages import (
    build_archive_search_overlay_pages,
)
from documents.services.archive_search_overlay_presentation import (
    apply_archive_search_overlay_to_source_previews,
    build_archive_search_single_image_overlay,
)
from documents.services.archive_search_transcription_presentation import (
    build_archive_search_transcription_presentation,
)
from documents.services.transkribus_paragraph_presentation import (
    build_transkribus_paragraph_presentation,
)
from documents.services.transkribus_paragraph_staff import (
    MSG_ADOPTED,
    MSG_SAVED,
    ParagraphEditorError,
    adopt_paragraph_editor_mapping,
    build_paragraph_editor_context,
    build_paragraph_mapping_staff_status,
    save_paragraph_editor_mapping,
)
from documents.services.text_line_hover_presentation import (
    apply_text_line_hover_overlay_to_source_previews,
    build_text_line_hover_overlay_pages,
    build_text_line_hover_presentation,
    build_text_line_hover_single_image_overlay,
)
from documents.services.env_validation import EnvConfigError, validate_required_env
from documents.services.ocr_reprocess import (
    OcrReprocessError,
    is_ocr_reprocess_ui_eligible,
)
from documents.services.process_document_ocr_reprocess_enqueue import (
    apply_ocr_reprocess,
)
from documents.services.hebrew_translation_retry import (
    HebrewTranslationRetryError,
    is_hebrew_translation_retry_ui_eligible,
)
from documents.services.process_document_hebrew_translation_retry_enqueue import (
    HebrewTranslationRetryEnqueueError,
    enqueue_hebrew_translation_retry,
)
from documents.services.process_document_upload_enqueue import (
    UploadProcessEnqueueError,
    enqueue_uploaded_document_processing,
)
from documents.services.text_presentation import (
    build_document_detail_jump_nav,
    document_detail_has_source_viewer,
    get_displayed_transcription_text,
    get_text_presentation_for_document,
    resolve_displayable_source_text_result,
    text_presentation_results_prefetch,
)
from documents.services.transkribus_corrected_current_activation import (
    CorrectedCurrentActivationError,
    CorrectedCurrentActivationErrorCode,
    CorrectedCurrentActivationResult,
    activate_corrected_current_sync_attempt,
)
from documents.services.transkribus_corrected_current_sync_enqueue import (
    CorrectedCurrentSyncEnqueueError,
    EnqueueResult,
    enqueue_transkribus_corrected_current_sync,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
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
    PendingTextResultEditError,
    VerifiedTextResultEditError,
    edit_pending_text_result,
    edit_verified_text_result,
    is_hebrew_translation_stale,
    is_verified_editable_text_result,
    verify_pending_text_result,
)

logger = logging.getLogger(__name__)

ARCHIVE_ITEM_TYPE_MANUAL_TEXT = "manual_text"
ARCHIVE_ITEM_TYPE_OCR_DOCUMENT = "ocr_document"
ARCHIVE_ITEM_TYPE_PHOTO = "photo"
ARCHIVE_ITEM_TYPE_VIDEO = "video"
_VALID_ARCHIVE_ITEM_CREATE_TYPES = frozenset(
    {
        ARCHIVE_ITEM_TYPE_MANUAL_TEXT,
        ARCHIVE_ITEM_TYPE_OCR_DOCUMENT,
        ARCHIVE_ITEM_TYPE_PHOTO,
        ARCHIVE_ITEM_TYPE_VIDEO,
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
    handwriting_type: str
    visibility: str
    admin_meta: dict
    author_name: str
    source_title: str
    public_note: str
    discovery_metadata: dict
    people: dict


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


def _default_page_offset(request) -> int:
    return _parse_int(request.GET.get("offset"), default=0, min_value=0)


def _pagination_context(
    *, total: int, offset: int, limit: int
) -> dict[str, int | None]:
    return {
        "offset": offset,
        "limit": limit,
        "total": total,
        "prev_offset": max(0, offset - limit),
        "next_offset": (offset + limit) if (offset + limit) < total else None,
    }


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
        Document.TextInputType.MIXED,
    }
    if value not in valid:
        raise ValueError("text_input_type must be HANDWRITTEN, PRINTED, or MIXED")
    return value


def _parse_handwriting_type(
    raw_value: Optional[str],
    *,
    field_present: bool,
    language: str | None,
    text_input_type: str,
) -> str:
    is_hebrew_handwritten = (
        language == Document.Language.HEBREW
        and text_input_type == Document.TextInputType.HANDWRITTEN
    )

    if not is_hebrew_handwritten:
        if field_present:
            raise ValueError(
                "handwriting_type is only allowed for Hebrew handwritten documents"
            )
        return Document.HandwritingType.VS

    value = (raw_value or "").strip().upper()
    if not value:
        return Document.HandwritingType.VS

    valid = {choice.value for choice in Document.HandwritingType}
    if value not in valid:
        raise ValueError("handwriting_type is invalid")
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


def _normalize_uploaded_image_exif_or_error(
    *,
    bucket: str,
    key: str,
    mime_type: str,
    document_id: int,
    order_index: Optional[int] = None,
) -> tuple[ExifNormalizationResult, Optional[JsonResponse]]:
    """
    Normalize EXIF orientation for supported image uploads after S3 verification.

    Returns (result, error_response). error_response is set when normalization fails.
    """
    if not is_upload_image_mime(mime_type):
        return ExifNormalizationResult(rewritten=False), None

    try:
        result = normalize_uploaded_image_exif_in_s3(
            bucket=bucket,
            key=key,
            mime_type=mime_type,
        )
    except ExifNormalizationError:
        logger.exception(
            "image exif normalization failed during upload completion",
            extra={
                "document_id": document_id,
                "order_index": order_index,
                "s3_key": key,
            },
        )
        body: dict = {
            "error": "image exif normalization failed",
            "document_id": document_id,
        }
        if order_index is not None:
            body["order_index"] = order_index
        return ExifNormalizationResult(rewritten=False), JsonResponse(body, status=500)

    return result, None


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
    *,
    user=None,
) -> tuple[_CreateUploadCommon | None, HttpResponseBadRequest | None]:
    title = (payload.get("title") or "").strip()
    if not title:
        return None, _bad("title required")

    language = (payload.get("language") or "").strip() or None
    text_input_type_raw = payload.get("text_input_type")
    handwriting_type_present = "handwriting_type" in payload
    handwriting_type_raw = payload.get("handwriting_type")
    admin_meta = payload.get("admin_meta", None)

    try:
        visibility = parse_visibility(payload.get("visibility"), user=user)
        text_input_type = _parse_text_input_type(text_input_type_raw)
        handwriting_type = _parse_handwriting_type(
            handwriting_type_raw,
            field_present=handwriting_type_present,
            language=language,
            text_input_type=text_input_type,
        )
        date_precision = parse_date_precision(payload.get("date_precision"))
        ds, de, _, date_errors = parse_archive_date_bounds(
            date_precision=date_precision,
            post_data=payload,
        )
    except ValueError as e:
        return None, _bad(str(e))

    if date_errors:
        return None, _bad(date_errors[0])

    field_errors = validate_archive_metadata_fields(
        title=title,
        visibility=visibility,
        metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        date_precision=date_precision,
        date_start=ds,
        date_end=de,
        user=user,
    )
    if field_errors:
        return None, _bad(field_errors[0])

    if admin_meta is None:
        admin_meta = {}
    if not isinstance(admin_meta, dict):
        return None, _bad("admin_meta must be an object")

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

    parsed_people, people_errors = parse_archive_item_people_form(payload)
    if people_errors:
        return None, _bad(people_errors[0])

    return {
        "title": title,
        "date_start": ds,
        "date_end": de,
        "date_precision": date_precision,
        "language": language,
        "text_input_type": text_input_type,
        "handwriting_type": handwriting_type,
        "visibility": visibility,
        "admin_meta": admin_meta,
        "author_name": author_name,
        "source_title": source_title,
        "public_note": public_note,
        "discovery_metadata": parsed_discovery,
        "people": parsed_people,
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


def _apply_created_ocr_relations(doc: Document, common: _CreateUploadCommon) -> None:
    _attach_document_admin_metadata(doc, common["admin_meta"])
    _save_archive_item_people(doc.archive_item, common["people"])
    _apply_upload_discovery_metadata(doc, common["discovery_metadata"])


def _parse_image_file_entry(
    entry: object,
    *,
    field_prefix: str,
) -> tuple[_ParsedImageFileMeta | None, HttpResponseBadRequest | None]:
    if not isinstance(entry, dict):
        return None, _bad(f"{field_prefix} must be an object")

    original_name = (entry.get("original_name") or "").strip()
    if not original_name:
        return None, _bad(f"{field_prefix}.original_name is required")

    mime_type = (entry.get("mime_type") or entry.get("content_type") or "").strip()
    file_err = validate_image_upload_metadata(
        mime_type=mime_type,
        original_name=original_name,
        field_prefix=field_prefix,
    )
    if file_err:
        return None, _bad(file_err)

    size_bytes = entry.get("size_bytes")
    if size_bytes is not None and not isinstance(size_bytes, int):
        return None, _bad(f"{field_prefix}.size_bytes must be an integer")

    return {
        "original_name": original_name,
        "mime_type": mime_type,
        "size_bytes": size_bytes if isinstance(size_bytes, int) else None,
    }, None


def _create_source_file_presigned_upload(
    *,
    doc: Document,
    bucket: str,
    order_index: int,
    file_meta: _ParsedImageFileMeta,
) -> dict:
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
    return {
        "order_index": order_index,
        "s3_key": key,
        "upload_url": upload_url,
        "original_name": file_meta["original_name"],
        "mime_type": file_meta["mime_type"],
        "size_bytes": file_meta["size_bytes"],
    }


def _create_incremental_multi_image_upload(
    request, payload: dict, common: _CreateUploadCommon
):
    if payload.get("files") is not None:
        return _bad("incremental upload must not include files[]")

    legacy_file_fields = [
        field
        for field in ("original_name", "mime_type", "content_type", "size_bytes")
        if field in payload
    ]
    if legacy_file_fields:
        joined = ", ".join(legacy_file_fields)
        return _bad(
            "incremental upload must not include top-level single-file fields: "
            f"{joined}"
        )

    doc_type = (payload.get("doc_type") or "").strip()
    if doc_type and doc_type != Document.DocType.IMAGE:
        return _bad("incremental upload requires doc_type=IMAGE")

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response

    with transaction.atomic():
        doc = create_ocr_document(
            title=common["title"],
            doc_type=Document.DocType.IMAGE,
            date_start=common["date_start"],
            date_end=common["date_end"],
            date_precision=common["date_precision"],
            language=common["language"],
            text_input_type=common["text_input_type"],
            handwriting_type=common["handwriting_type"],
            visibility=common["visibility"],
            author_name=common["author_name"],
            source_title=common["source_title"],
            public_note=common["public_note"],
            upload_status=Document.UploadStatus.UPLOADING,
            expected_source_file_count=None,
        )
        _apply_created_ocr_relations(doc, common)

    return JsonResponse(
        {
            "document_id": doc.id,
            "upload_status": doc.upload_status,
            "doc_type": doc.doc_type,
            "incremental": True,
            "expected_source_file_count": None,
        },
        status=201,
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
        if "order_index" in entry:
            return _bad(
                "order_index must not be provided; order is defined by files[] position"
            )

        parsed, parse_err = _parse_image_file_entry(
            entry,
            field_prefix=f"files[{index}]",
        )
        if parse_err is not None:
            return parse_err
        assert parsed is not None
        parsed_files.append(parsed)

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    with transaction.atomic():
        doc = create_ocr_document(
            title=common["title"],
            doc_type=Document.DocType.IMAGE,
            date_start=common["date_start"],
            date_end=common["date_end"],
            date_precision=common["date_precision"],
            language=common["language"],
            text_input_type=common["text_input_type"],
            handwriting_type=common["handwriting_type"],
            visibility=common["visibility"],
            author_name=common["author_name"],
            source_title=common["source_title"],
            public_note=common["public_note"],
            upload_status=Document.UploadStatus.UPLOADING,
            expected_source_file_count=file_count,
        )
        _apply_created_ocr_relations(doc, common)

    uploads = []
    for order_index, file_meta in enumerate(parsed_files):
        uploads.append(
            _create_source_file_presigned_upload(
                doc=doc,
                bucket=bucket,
                order_index=order_index,
                file_meta=file_meta,
            )
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

    with transaction.atomic():
        doc = create_ocr_document(
            title=common["title"],
            doc_type=doc_type,
            date_start=common["date_start"],
            date_end=common["date_end"],
            date_precision=common["date_precision"],
            language=common["language"],
            text_input_type=common["text_input_type"],
            handwriting_type=common["handwriting_type"],
            visibility=common["visibility"],
            author_name=common["author_name"],
            source_title=common["source_title"],
            public_note=common["public_note"],
            upload_status=Document.UploadStatus.UPLOADING,
            file_original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes if isinstance(size_bytes, int) else None,
        )
        _apply_created_ocr_relations(doc, common)

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

    common, err = _parse_create_upload_common(payload, user=request.user)
    if err is not None:
        return err
    assert common is not None

    if payload.get("incremental") is True:
        return _create_incremental_multi_image_upload(request, payload, common)

    if "files" in payload:
        return _create_multi_image_upload(request, payload, common)

    return _create_single_file_upload(request, payload, common)


def _enqueue_uploaded_document_processing_or_error(
    request,
    doc: Document,
) -> JsonResponse | None:
    """
    Enqueue upload-finalize processing outside the upload transaction.

    Expected durable enqueue failures are returned through a safe API boundary.
    Unexpected programming failures continue to propagate.
    """
    try:
        enqueue_uploaded_document_processing(
            document_id=doc.pk,
            initiated_by=request.user,
        )
    except UploadProcessEnqueueError as exc:
        logger.warning(
            "upload PROCESS_DOCUMENT enqueue did not complete",
            extra={
                "document_id": doc.pk,
                "enqueue_error_code": exc.code,
                "enqueue_outcome": exc.outcome,
            },
        )
        doc.refresh_from_db(
            fields=[
                "upload_status",
                "processing_state_user",
                "upload_error",
            ]
        )
        return JsonResponse(
            {
                "error": exc.public_message,
                "code": exc.code,
                "document_id": doc.pk,
            },
            status=exc.http_status,
        )

    doc.refresh_from_db(
        fields=[
            "upload_status",
            "processing_state_user",
            "upload_error",
        ]
    )
    return None


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

    if uses_multi_image_part_endpoints(doc):
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

        norm_result, norm_err = _normalize_uploaded_image_exif_or_error(
            bucket=bucket,
            key=doc.file_s3_key,
            mime_type=expected_mime,
            document_id=doc.id,
        )
        if norm_err:
            return norm_err

        already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

        with transaction.atomic():
            doc.upload_status = Document.UploadStatus.UPLOADED
            doc.upload_error = None

            if norm_result.rewritten and norm_result.size_bytes is not None:
                doc.size_bytes = norm_result.size_bytes
            elif isinstance(payload.get("file_size"), int):
                doc.size_bytes = payload["file_size"]
            if file_mime:
                doc.mime_type = file_mime

            if not already_uploaded:
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
            schedule_document_thumbnail_after_upload(
                doc,
                bucket=bucket,
                already_uploaded=already_uploaded,
            )

        enqueue_error = _enqueue_uploaded_document_processing_or_error(
            request,
            doc,
        )
        if enqueue_error is not None:
            return enqueue_error

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
def upload_part_add(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return _bad("POST only")

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return _bad("invalid json")

    try:
        doc = Document.objects.get(id=doc_id)
    except Document.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if not is_incremental_multi_image_draft(doc):
        return JsonResponse(
            {
                "error": "incremental part add requires an incremental multi-image draft",
                "document_id": doc.id,
            },
            status=400,
        )

    if doc.upload_status == Document.UploadStatus.FAILED:
        return _multi_image_upload_terminal_failed_response(doc)

    if doc.upload_status != Document.UploadStatus.UPLOADING:
        return JsonResponse(
            {
                "error": "document is not accepting new parts",
                "document_id": doc.id,
            },
            status=400,
        )

    file_meta, parse_err = _parse_image_file_entry(payload, field_prefix="file")
    if parse_err is not None:
        return parse_err
    assert file_meta is not None

    order_index = next_incremental_part_order_index(doc)
    if order_index >= MULTI_IMAGE_MAX_FILES:
        return _bad(
            f"documents may contain at most {MULTI_IMAGE_MAX_FILES} image parts"
        )

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    upload_entry = _create_source_file_presigned_upload(
        doc=doc,
        bucket=bucket,
        order_index=order_index,
        file_meta=file_meta,
    )

    return JsonResponse(
        {
            "document_id": doc.id,
            **upload_entry,
        },
        status=201,
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

    if not uses_multi_image_part_endpoints(doc):
        return JsonResponse(
            {"error": "not a multi-image document", "document_id": doc.id},
            status=400,
        )

    if doc.upload_status == Document.UploadStatus.FAILED and success:
        return _multi_image_upload_terminal_failed_response(doc)

    if is_multi_image_document(doc):
        expected = doc.expected_source_file_count
        assert expected is not None
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
    elif is_incremental_multi_image_draft(doc):
        if order_index < 0:
            return JsonResponse(
                {"error": "order_index must be >= 0", "document_id": doc.id},
                status=400,
            )
    else:
        return JsonResponse(
            {"error": "not a multi-image document", "document_id": doc.id},
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

        norm_result, norm_err = _normalize_uploaded_image_exif_or_error(
            bucket=bucket,
            key=source_file.file_s3_key,
            mime_type=expected_mime,
            document_id=doc.id,
            order_index=order_index,
        )
        if norm_err:
            return norm_err

        source_file.upload_status = DocumentSourceFile.UploadStatus.UPLOADED
        source_file.upload_error = None
        file_size = payload.get("file_size")
        if norm_result.rewritten and norm_result.size_bytes is not None:
            source_file.size_bytes = norm_result.size_bytes
        elif isinstance(file_size, int):
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

    if not uses_multi_image_part_endpoints(doc):
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

    if is_incremental_multi_image_draft(doc):
        ready, ready_err, uploaded_count = validate_incremental_finalize_ready(doc)
        if not ready:
            return JsonResponse(
                {"error": ready_err, "document_id": doc.id},
                status=400,
            )
        doc.expected_source_file_count = uploaded_count
        doc.save(update_fields=["expected_source_file_count", "updated_at"])
    else:
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

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    already_uploaded = doc.upload_status == Document.UploadStatus.UPLOADED

    with transaction.atomic():
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
        schedule_document_thumbnail_after_upload(
            doc,
            bucket=bucket,
            already_uploaded=already_uploaded,
        )

    enqueue_error = _enqueue_uploaded_document_processing_or_error(
        request,
        doc,
    )
    if enqueue_error is not None:
        return enqueue_error

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

    parsed, err = parse_create_photo_upload_metadata(payload, user=request.user)
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
        person_ids=parsed["archive_item_person_ids"],
        new_person_name=parsed["new_archive_item_person_name"],
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
def add_photo_upload(request):
    deny = _require_admin(request)
    if deny:
        return deny

    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        return JsonResponse({"error": "invalid json"}, status=400)

    parsed, err = parse_add_photo_upload_metadata(payload)
    if err is not None:
        return JsonResponse({"error": err}, status=400)
    assert parsed is not None

    bucket_or_response = _uploads_bucket_or_error()
    if isinstance(bucket_or_response, JsonResponse):
        return bucket_or_response
    bucket = bucket_or_response

    try:
        archive_item = ArchiveItem.objects.get(pk=parsed["archive_item_id"])
    except ArchiveItem.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)

    if archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        return JsonResponse({"error": "not a photo archive item"}, status=400)

    try:
        archive_item, photo_content, upload_url = create_additional_photo_upload_plan(
            archive_item=archive_item,
            bucket=bucket,
            original_name=parsed["original_name"],
            mime_type=parsed["mime_type"],
            description=parsed["description"],
            location=parsed["location"],
            context=parsed["context"],
            people_present=parsed["people_present"],
            notes=parsed["notes"],
            date_start=parsed["date_start"],
            date_end=parsed["date_end"],
            date_precision=parsed["date_precision"],
        )
    except PhotoContentManagementError as exc:
        status = 409
        if exc.message == ARCHIVE_ITEM_NOT_PHOTO_ERROR:
            status = 400
        return JsonResponse({"error": exc.message}, status=status)

    return JsonResponse(
        {
            "archive_item_id": archive_item.id,
            "photo_content_id": photo_content.id,
            "position": photo_content.position,
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
    offset = _default_page_offset(request)

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
        **_pagination_context(total=total, offset=offset, limit=limit),
        "doc_type_choices": Document.DocType.choices,
        "metadata_status_choices": Document.MetadataStatus.choices,
        "upload_status_choices": Document.UploadStatus.choices,
        "visibility_choices": archive_visibility_ui_choices(request.user),
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
    offset = _default_page_offset(request)

    only_missing_tags = (request.GET.get("only_missing_tags") or "").strip() == "1"
    only_missing_admin_meta = (
        request.GET.get("only_missing_admin_meta") or ""
    ).strip() == "1"

    base_qs = filter_documents_for_user(
        request.user,
        Document.objects.select_related("admin_meta", "archive_item")
        .prefetch_related("tags_m2m")
        .filter(
            archive_item__metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION
        )
        .order_by("-created_at")
        .distinct(),
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
        **_pagination_context(total=total_filtered, offset=offset, limit=limit),
        "total_backlog": total_backlog,
        "missing_tags_count": missing_tags_count,
        "missing_admin_meta_count": missing_admin_meta_count,
        "only_missing_tags": only_missing_tags,
        "only_missing_admin_meta": only_missing_admin_meta,
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
    offset = _default_page_offset(request)

    qs = filter_documents_for_user(
        request.user,
        documents_in_review_backlog(
            q=q,
            language=language,
            text_input_type=text_input_type,
            processing_state_user=processing_state_user,
            engine_key=engine_key,
            result_type=result_type,
            verification_status=verification_status,
        ),
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
        **_pagination_context(total=total, offset=offset, limit=limit),
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

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related(
            "admin_meta", "archive_item"
        ).prefetch_related(
            "tags_m2m", "text_results", "transkribus_runs", "source_files"
        ),
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
        "show_transkribus_corrected_current_sync_action": (
            _is_transkribus_corrected_current_sync_ui_eligible(doc)
        ),
    }

    logger.info(
        "review_detail_page user=%s doc_id=%s text_results=%s transkribus_runs=%s",
        getattr(request.user, "username", None),
        doc.id,
        len(text_results),
        len(transkribus_runs),
    )
    return render(request, "documents/review_detail.html", context)


_CORRECTED_CURRENT_SYNC_STATUS_LABELS: dict[str, str] = {
    TranskribusCorrectedCurrentSyncAttempt.Status.STARTED.value: "בתהליך",
    TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED.value: "הושלם",
    TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED.value: "סורב בבחירה",
    TranskribusCorrectedCurrentSyncAttempt.Status.FAILED.value: "נכשל",
}

_CORRECTED_CURRENT_SYNC_STORAGE_OUTCOME_LABELS: dict[str, str] = {
    TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.CREATED.value: "נוצר חדש",
    TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.REUSED_EXISTING.value: (
        "נעשה שימוש חוזר בקיים"
    ),
    TranskribusCorrectedCurrentSyncAttempt.StorageOutcome.REUSED_CONCURRENT_WINNER.value: (
        "נעשה שימוש חוזר במנצח מקבילי"
    ),
}

_CORRECTED_CURRENT_SYNC_PAGE_OUTCOME_LABELS: dict[str, str] = {
    "SELECTED": "נבחר",
    "REFUSED": "סורב",
}

_CORRECTED_CURRENT_SYNC_REMOTE_STATUS_LABELS: dict[str, str] = {
    "IN_PROGRESS": "בתהליך",
    "DONE": "הושלם",
    "FINAL": "סופי",
    "GT": "אמת מידה",
    "NEW": "חדש",
}


def _corrected_current_sync_status_label(status: str) -> str:
    return _CORRECTED_CURRENT_SYNC_STATUS_LABELS.get(status, status)


def _corrected_current_sync_storage_outcome_label(outcome: str | None) -> str:
    if not outcome:
        return "—"
    return _CORRECTED_CURRENT_SYNC_STORAGE_OUTCOME_LABELS.get(outcome, outcome)


def _corrected_current_sync_remote_status_label(status: str | None) -> str:
    if not status:
        return "—"
    normalized = str(status).strip()
    if not normalized:
        return "—"
    return _CORRECTED_CURRENT_SYNC_REMOTE_STATUS_LABELS.get(normalized.upper(), "אחר")


def _corrected_current_sync_attempts_queryset(*, with_pages: bool = False):
    qs = TranskribusCorrectedCurrentSyncAttempt.objects.select_related(
        "initiated_by",
        "resolved_snapshot",
    )
    if with_pages:
        qs = qs.prefetch_related("pages")
    return qs


_CORRECTED_CURRENT_ACTIVATION_CONFIRM_FIELD = "confirm_replace"
_CORRECTED_CURRENT_ACTIVATION_CONFIRM_VALUE = "1"

_CORRECTED_CURRENT_ACTIVATION_MSG_MISSING_CONFIRM = (
    "יש לאשר במפורש את החלפת התעתוק לפני ביצוע הפעולה."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_INVALID_BASELINE = (
    "לא ניתן לבצע את ההחלפה. רעננו את הדף ונסו שוב."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC = (
    "לא ניתן להשלים את ההחלפה כעת. רעננו את הדף ונסו שוב מאוחר יותר."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_STALE = (
    "התעתוק המוצג השתנה מאז התצוגה המקדימה. רעננו את הדף ובדקו שוב לפני ההחלפה."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_VERIFIED = "לא ניתן להחליף תעתוק שאומת על ידי אדם."
_CORRECTED_CURRENT_ACTIVATION_MSG_HUMAN_EDITED = (
    "לא ניתן להחליף תעתוק שנערך ידנית ומוגן."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_UNAUTHORIZED = "אין הרשאה לבצע החלפה זו."
_CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE = (
    "גרסת ה־Transkribus הזו כבר אינה זמינה להחלפה."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE = (
    "התעתוק המוצג הוחלף בגרסת Transkribus."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_HEBREW_MIRROR = (
    "התעתוק בעברית עודכן בהתאם לגרסת Transkribus. תעתוק המקור לא השתנה."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_BINDING_ONLY = (
    "הקישור לגרסת Transkribus עודכן. התעתוק המוצג לא השתנה."
)
_CORRECTED_CURRENT_ACTIVATION_MSG_ALREADY_ACTIVE = (
    "גרסת Transkribus הזו כבר פעילה. לא בוצע שינוי."
)

_CORRECTED_CURRENT_ACTIVATION_ERROR_MESSAGES_HE: dict[str, str] = {
    CorrectedCurrentActivationErrorCode.STALE_PREVIEW: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_STALE
    ),
    CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_VERIFIED
    ),
    CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_HUMAN_EDITED
    ),
    CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_UNAUTHORIZED
    ),
    CorrectedCurrentActivationErrorCode.DOCUMENT_NOT_FOUND: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_FOUND: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.ATTEMPT_DOCUMENT_MISMATCH: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_COMPLETED: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_MISSING: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_DOCUMENT_MISMATCH: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_NOT_READY: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_PAGE_MISMATCH: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.TARGET_NOT_FOUND: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.TARGET_DOCUMENT_MISMATCH: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.TARGET_NOT_SOURCE_TEXT: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_NOT_ELIGIBLE
    ),
    CorrectedCurrentActivationErrorCode.CANONICAL_TEXT_EMPTY: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC
    ),
    CorrectedCurrentActivationErrorCode.CANONICAL_HASH_MISMATCH: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC
    ),
    CorrectedCurrentActivationErrorCode.HEBREW_MIRROR_MISSING: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC
    ),
    CorrectedCurrentActivationErrorCode.BINDING_FAILED: (
        _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC
    ),
}


def _corrected_current_activation_error_message(code: str) -> str:
    return _CORRECTED_CURRENT_ACTIVATION_ERROR_MESSAGES_HE.get(
        code,
        _CORRECTED_CURRENT_ACTIVATION_MSG_GENERIC,
    )


def _corrected_current_activation_success_message(
    result: CorrectedCurrentActivationResult,
) -> str:
    if result.outcome == "ALREADY_ACTIVE":
        return _CORRECTED_CURRENT_ACTIVATION_MSG_ALREADY_ACTIVE
    if result.source_text_changed:
        return _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_SOURCE
    if result.hebrew_mirror_updated:
        return _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_HEBREW_MIRROR
    return _CORRECTED_CURRENT_ACTIVATION_MSG_APPLIED_BINDING_ONLY


def _corrected_current_activation_detail_url(*, doc_id: int, attempt_id: int) -> str:
    return reverse(
        "corrected-current-sync-attempt-detail",
        kwargs={"doc_id": doc_id, "attempt_id": attempt_id},
    )


def _corrected_current_sync_attempts_url(*, doc_id: int) -> str:
    return reverse("corrected-current-sync-attempts", kwargs={"doc_id": doc_id})


_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_CREATED = (
    "בקשת משיכת תעתוק עדכני מ־Transkribus נשלחה לעיבוד ברקע."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_QUEUED = (
    "בקשת משיכת תעתוק עדכני מ־Transkribus כבר ממתינה בתור."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_RUNNING = (
    "סנכרון תעתוק מ־Transkribus כבר מתבצע."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_BLOCKED_RECOVERY = (
    "לא ניתן להתחיל סנכרון חדש כעת. נדרש טיפול במצב קיים לפני משיכה נוספת."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_FAILED = (
    "לא ניתן היה לשלוח את בקשת הסנכרון לתור. אפשר לנסות שוב."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_OUTCOME_UNKNOWN = (
    "לא ניתן לאשר אם בקשת הסנכרון התקבלה בתור. "
    "בדקו את רשימת הניסיונות או נסו שוב מאוחר יותר."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_TERMINAL = (
    "עיבוד בקשת הסנכרון כבר הסתיים. התעתוק המוצג לא הוחלף."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_INELIGIBLE = (
    "לא ניתן למשוך תעתוק עדכני מ־Transkribus עבור מסמך זה."
)
_CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_GENERIC = (
    "לא ניתן לשלוח בקשת סנכרון כעת. נסו שוב מאוחר יותר."
)


def _corrected_current_sync_enqueue_message_level_and_text(
    result: EnqueueResult,
) -> tuple[str, str]:
    """Map enqueue service outcomes to (messages level, Hebrew copy)."""
    outcome = result.outcome
    if outcome in {"CREATED_AND_ENQUEUED", "REENQUEUED"}:
        return "success", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_CREATED
    if outcome == "ALREADY_QUEUED":
        return "success", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_QUEUED
    if outcome == "ALREADY_RUNNING":
        return "success", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_RUNNING
    if outcome == "BLOCKED_RECOVERY_REQUIRED":
        return "error", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_BLOCKED_RECOVERY
    if outcome == "ENQUEUE_FAILED":
        return "error", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_FAILED
    if outcome == "ENQUEUE_OUTCOME_UNKNOWN":
        return "warning", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_OUTCOME_UNKNOWN
    if outcome == "ALREADY_TERMINAL":
        # Realistic when SendMessage is accepted and the worker terminalizes
        # before post-send CAS reload. Does not activate displayed text.
        return "success", _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_ALREADY_TERMINAL
    raise AssertionError(f"Unhandled corrected/current sync enqueue outcome: {outcome}")


@login_required
def corrected_current_sync_attempts_page(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )
    attempts = list(
        _corrected_current_sync_attempts_queryset()
        .filter(document_id=doc.id)
        .order_by("-created_at")
    )
    attempt_rows = [
        {
            "attempt": attempt,
            "status_label": _corrected_current_sync_status_label(attempt.status),
            "storage_outcome_label": _corrected_current_sync_storage_outcome_label(
                attempt.storage_outcome
            ),
            "initiated_by_username": (
                attempt.initiated_by.username if attempt.initiated_by_id else "—"
            ),
        }
        for attempt in attempts
    ]

    show_enqueue_action = _is_transkribus_corrected_current_sync_ui_eligible(doc)
    paragraph_status = build_paragraph_mapping_staff_status(doc)
    logger.info(
        "corrected_current_sync_attempts_page user=%s doc_id=%s attempts=%s "
        "show_enqueue=%s paragraph_status=%s",
        getattr(request.user, "username", None),
        doc.id,
        len(attempt_rows),
        show_enqueue_action,
        paragraph_status.code,
    )
    return render(
        request,
        "documents/corrected_current_sync_attempts.html",
        {
            "doc": doc,
            "attempt_rows": attempt_rows,
            "show_transkribus_corrected_current_sync_enqueue_action": (
                show_enqueue_action
            ),
            "paragraph_status": paragraph_status,
        },
    )


@login_required
@require_POST
def corrected_current_sync_enqueue(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )
    list_url = _corrected_current_sync_attempts_url(doc_id=doc.id)

    if not _is_transkribus_corrected_current_sync_ui_eligible(doc):
        messages.error(request, _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_INELIGIBLE)
        logger.info(
            "corrected_current_sync_enqueue ineligible user=%s doc_id=%s",
            getattr(request.user, "username", None),
            doc.id,
        )
        return redirect(list_url)

    try:
        result = enqueue_transkribus_corrected_current_sync(
            document_id=doc.id,
            initiated_by=request.user,
        )
    except CorrectedCurrentSyncEnqueueError:
        messages.error(request, _CORRECTED_CURRENT_SYNC_ENQUEUE_MSG_GENERIC)
        logger.warning(
            "corrected_current_sync_enqueue service error user=%s doc_id=%s",
            getattr(request.user, "username", None),
            doc.id,
            exc_info=True,
        )
        return redirect(list_url)

    level, text = _corrected_current_sync_enqueue_message_level_and_text(result)
    getattr(messages, level)(request, text)
    logger.info(
        "corrected_current_sync_enqueue user=%s doc_id=%s request_id=%s "
        "outcome=%s message_sent=%s observed_status=%s",
        getattr(request.user, "username", None),
        doc.id,
        result.request.pk,
        result.outcome,
        result.message_sent,
        result.observed_status,
    )
    return redirect(list_url)


@login_required
def corrected_current_sync_attempt_detail_page(request, doc_id: int, attempt_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item").prefetch_related(
            text_presentation_results_prefetch()
        ),
    )
    attempt = get_object_or_404(
        _corrected_current_sync_attempts_queryset(with_pages=True),
        id=attempt_id,
        document_id=doc.id,
    )

    page_rows = [
        {
            "page": page,
            "outcome_label": _CORRECTED_CURRENT_SYNC_PAGE_OUTCOME_LABELS.get(
                page.outcome, page.outcome
            ),
            "remote_status_label": _corrected_current_sync_remote_status_label(
                page.remote_transcript_status
            ),
        }
        for page in attempt.pages.all()
    ]

    source_row = resolve_displayable_source_text_result(doc)
    source_text = (source_row.text or "") if source_row is not None else ""
    snapshot = attempt.resolved_snapshot
    snapshot_ready = (
        snapshot is not None
        and snapshot.storage_status == TranskribusTranscriptSnapshot.StorageStatus.READY
    )
    show_snapshot_preview = (
        attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED
        and snapshot is not None
    )
    show_activation_section = (
        attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED
        and snapshot_ready
    )
    activation_form_available = False
    activation_source_text_result_id: int | None = None
    activation_expected_source_revision: int | None = None
    activation_expected_source_sha256: str | None = None
    if show_activation_section and source_row is not None:
        activation_form_available = True
        activation_source_text_result_id = source_row.id
        activation_expected_source_revision = source_row.source_revision
        activation_expected_source_sha256 = compute_sha256_hex(source_row.text or "")
    snapshot_text = snapshot.canonical_text if show_snapshot_preview else ""
    diff_html = None
    if show_snapshot_preview and source_row is not None:
        diff_html = render_transcription_diff_html(source_text, snapshot_text)

    logger.info(
        "corrected_current_sync_attempt_detail_page user=%s doc_id=%s attempt_id=%s "
        "status=%s pages=%s has_source_baseline=%s activation_form=%s",
        getattr(request.user, "username", None),
        doc.id,
        attempt.id,
        attempt.status,
        len(page_rows),
        source_row is not None,
        activation_form_available,
    )
    return render(
        request,
        "documents/corrected_current_sync_attempt_detail.html",
        {
            "doc": doc,
            "attempt": attempt,
            "status_label": _corrected_current_sync_status_label(attempt.status),
            "storage_outcome_label": _corrected_current_sync_storage_outcome_label(
                attempt.storage_outcome
            ),
            "initiated_by_username": (
                attempt.initiated_by.username if attempt.initiated_by_id else "—"
            ),
            "page_rows": page_rows,
            "source_row": source_row,
            "source_text": source_text,
            "show_snapshot_preview": show_snapshot_preview,
            "snapshot": snapshot if show_snapshot_preview else None,
            "snapshot_text": snapshot_text,
            "diff_html": diff_html,
            "show_activation_section": show_activation_section,
            "activation_form_available": activation_form_available,
            "activation_source_text_result_id": activation_source_text_result_id,
            "activation_expected_source_revision": activation_expected_source_revision,
            "activation_expected_source_sha256": activation_expected_source_sha256,
            "is_started": (
                attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.STARTED
            ),
            "is_refused": (
                attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.REFUSED
            ),
            "is_failed": (
                attempt.status == TranskribusCorrectedCurrentSyncAttempt.Status.FAILED
            ),
            "is_completed": (
                attempt.status
                == TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED
            ),
        },
    )


@login_required
@require_POST
def corrected_current_sync_attempt_activate(request, doc_id: int, attempt_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    # Visibility + URL ownership/existence parity with GET detail (404, no messages).
    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )
    get_object_or_404(
        TranskribusCorrectedCurrentSyncAttempt.objects.only("id"),
        id=attempt_id,
        document_id=doc.id,
    )

    detail_url = _corrected_current_activation_detail_url(
        doc_id=doc_id,
        attempt_id=attempt_id,
    )

    confirm = (
        request.POST.get(_CORRECTED_CURRENT_ACTIVATION_CONFIRM_FIELD) or ""
    ).strip()
    if confirm != _CORRECTED_CURRENT_ACTIVATION_CONFIRM_VALUE:
        messages.error(request, _CORRECTED_CURRENT_ACTIVATION_MSG_MISSING_CONFIRM)
        return redirect(detail_url)

    raw_source_id = request.POST.get("source_text_result_id")
    raw_revision = request.POST.get("expected_source_revision")
    expected_source_sha256 = (request.POST.get("expected_source_sha256") or "").strip()
    try:
        source_text_result_id = int(raw_source_id)
        expected_source_revision = int(raw_revision)
    except (TypeError, ValueError):
        messages.error(request, _CORRECTED_CURRENT_ACTIVATION_MSG_INVALID_BASELINE)
        return redirect(detail_url)

    if not expected_source_sha256:
        messages.error(request, _CORRECTED_CURRENT_ACTIVATION_MSG_INVALID_BASELINE)
        return redirect(detail_url)

    try:
        result = activate_corrected_current_sync_attempt(
            document_id=doc_id,
            attempt_id=attempt_id,
            source_text_result_id=source_text_result_id,
            activated_by=request.user,
            expected_source_revision=expected_source_revision,
            expected_source_sha256=expected_source_sha256,
        )
    except CorrectedCurrentActivationError as exc:
        messages.error(
            request,
            _corrected_current_activation_error_message(exc.code),
        )
        logger.info(
            "corrected_current_sync_attempt_activate rejected user=%s doc_id=%s "
            "attempt_id=%s code=%s",
            getattr(request.user, "username", None),
            doc_id,
            attempt_id,
            exc.code,
        )
        return redirect(detail_url)

    messages.success(
        request,
        _corrected_current_activation_success_message(result),
    )
    logger.info(
        "corrected_current_sync_attempt_activate ok user=%s doc_id=%s attempt_id=%s "
        "outcome=%s source_text_changed=%s hebrew_mirror_updated=%s",
        getattr(request.user, "username", None),
        doc_id,
        attempt_id,
        result.outcome,
        result.source_text_changed,
        result.hebrew_mirror_updated,
    )
    return redirect(detail_url)


def _review_text_result_not_eligible_response() -> HttpResponseBadRequest:
    return HttpResponseBadRequest(
        "transcription result is not eligible for review action"
    )


def _wants_review_async_json(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _review_async_error(request, message: str, *, status: int):
    if _wants_review_async_json(request):
        return JsonResponse({"ok": False, "error": message}, status=status)
    if status == 403:
        return HttpResponseForbidden(message)
    if status == 404:
        raise Http404(message)
    return HttpResponseBadRequest(message)


def _review_mutation_success(
    request,
    *,
    row: DocumentTextResult,
    action: str,
    text_saved: bool,
):
    if _wants_review_async_json(request):
        return JsonResponse(
            {
                "ok": True,
                "action": action,
                "result_id": row.id,
                "document_id": row.document_id,
                "verification_status": row.verification_status,
                "text_saved": text_saved,
            }
        )
    return redirect(reverse("review-detail-page", kwargs={"doc_id": row.document_id}))


def _get_admin_viewable_text_result(request, result_id: int) -> DocumentTextResult:
    """Load a text-result row only when its parent document is viewable (404 otherwise)."""
    return get_object_or_404(
        DocumentTextResult.objects.select_related(
            "document",
            "document__archive_item",
        ).filter(document__in=document_queryset_for_user(request.user)),
        id=result_id,
    )


@login_required
@require_POST
def review_text_result_verify(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return _review_async_error(request, "Admins only", status=403)

    try:
        row = _get_admin_viewable_text_result(request, result_id)
    except Http404:
        return _review_async_error(request, "לא נמצא.", status=404)

    if not is_review_pending_text_result(row):
        if _wants_review_async_json(request):
            return _review_async_error(
                request,
                "transcription result is not eligible for review action",
                status=400,
            )
        return _review_text_result_not_eligible_response()

    submitted = request.POST.get("text")
    if submitted is None or not submitted.strip():
        return _review_async_error(
            request,
            "text is required and must be non-empty",
            status=400,
        )

    try:
        outcome = verify_pending_text_result(
            result_id=row.id,
            new_text=submitted,
            editor=request.user,
        )
    except DocumentTextResult.DoesNotExist:
        return _review_async_error(request, "לא נמצא.", status=404)
    except PendingTextResultEditError as exc:
        message = str(exc)
        if message == "transcription result is not eligible for review action":
            if _wants_review_async_json(request):
                return _review_async_error(request, message, status=400)
            return _review_text_result_not_eligible_response()
        return _review_async_error(request, message, status=400)

    row = outcome.row
    logger.info(
        "review_text_result_verify user=%s result_id=%s document_id=%s text_saved=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
        outcome.text_saved,
    )
    return _review_mutation_success(
        request,
        row=row,
        action="verify",
        text_saved=outcome.text_saved,
    )


@login_required
@require_POST
def review_text_result_reject(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return _review_async_error(request, "Admins only", status=403)

    try:
        row = _get_admin_viewable_text_result(request, result_id)
    except Http404:
        return _review_async_error(request, "לא נמצא.", status=404)

    if not is_review_pending_text_result(row):
        if _wants_review_async_json(request):
            return _review_async_error(
                request,
                "transcription result is not eligible for review action",
                status=400,
            )
        return _review_text_result_not_eligible_response()

    # Reject only — never persist unsaved textarea edits from the client.
    row.verification_status = DocumentTextResult.VerificationStatus.REJECTED
    row.save(update_fields=["verification_status", "updated_at"])

    logger.info(
        "review_text_result_reject user=%s result_id=%s document_id=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
    )
    return _review_mutation_success(
        request,
        row=row,
        action="reject",
        text_saved=False,
    )


@login_required
@require_POST
def review_text_result_update_text(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return _review_async_error(request, "Admins only", status=403)

    try:
        row = _get_admin_viewable_text_result(request, result_id)
    except Http404:
        return _review_async_error(request, "לא נמצא.", status=404)

    if not is_review_editable_text_result(row):
        if _wants_review_async_json(request):
            return _review_async_error(
                request,
                "transcription result is not eligible for review action",
                status=400,
            )
        return _review_text_result_not_eligible_response()

    submitted = request.POST.get("text")
    if submitted is None or not submitted.strip():
        return _review_async_error(
            request,
            "text is required and must be non-empty",
            status=400,
        )

    try:
        outcome = edit_pending_text_result(
            result_id=row.id,
            new_text=submitted,
            editor=request.user,
        )
    except DocumentTextResult.DoesNotExist:
        return _review_async_error(request, "לא נמצא.", status=404)
    except PendingTextResultEditError as exc:
        message = str(exc)
        if message == "transcription result is not eligible for review action":
            if _wants_review_async_json(request):
                return _review_async_error(request, message, status=400)
            return _review_text_result_not_eligible_response()
        return _review_async_error(request, message, status=400)

    row = outcome.row
    logger.info(
        "review_text_result_update_text user=%s result_id=%s document_id=%s text_saved=%s",
        getattr(request.user, "username", None),
        row.id,
        row.document_id,
        outcome.text_saved,
    )
    return _review_mutation_success(
        request,
        row=row,
        action="save",
        text_saved=outcome.text_saved,
    )


@login_required
@require_POST
def review_text_result_verified_edit(request, result_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    # Authorize before mutation so restricted documents do not leak via edit paths.
    _get_admin_viewable_text_result(request, result_id)

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
    return redirect(reverse("review-detail-page", kwargs={"doc_id": row.document_id}))


def _is_transkribus_corrected_current_sync_ui_eligible(
    doc: Document,
) -> bool:
    try:
        resolve_audit_transkribus_run(doc.id)
    except TranskribusPageXmlGeometryError:
        return False
    return True


def _paragraph_editor_url(*, doc_id: int) -> str:
    return reverse("transkribus-paragraphs", kwargs={"doc_id": doc_id})


def _paragraph_editor_hover_source_context(doc: Document, source_context: dict) -> dict:
    text_line_hover = build_text_line_hover_presentation(
        doc,
        source_preview_items=source_context["source_preview_items"],
        content_url=source_context["content_url"],
    )
    text_line_hover_overlay_pages = build_text_line_hover_overlay_pages(
        doc,
        source_preview_items=source_context["source_preview_items"],
        content_url=source_context["content_url"],
        overlay_targets=text_line_hover.overlay_targets,
    )
    editor_source_preview_items = apply_text_line_hover_overlay_to_source_previews(
        source_context["source_preview_items"],
        text_line_hover_overlay_pages,
    )
    text_line_hover_single_image_overlay = build_text_line_hover_single_image_overlay(
        doc,
        content_url=source_context["content_url"],
        overlay_pages=text_line_hover_overlay_pages,
    )
    hover_line_ids = {
        target.hover_line_id for target in text_line_hover.overlay_targets
    }
    return {
        "text_line_hover": text_line_hover,
        "text_line_hover_single_image_overlay": text_line_hover_single_image_overlay,
        "editor_source_preview_items": editor_source_preview_items,
        "hover_line_ids": hover_line_ids,
    }


@login_required
def transkribus_paragraph_editor_page(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item").prefetch_related(
            text_presentation_results_prefetch(),
            "source_files",
        ),
    )
    editor_url = _paragraph_editor_url(doc_id=doc.id)

    if request.method == "POST":
        try:
            save_paragraph_editor_mapping(doc, request.POST, actor=request.user)
        except ParagraphEditorError as exc:
            messages.error(request, exc.staff_message)
            logger.info(
                "transkribus_paragraph_editor_page save refused user=%s doc_id=%s "
                "message=%s",
                getattr(request.user, "username", None),
                doc.id,
                exc.staff_message,
            )
            return redirect(editor_url)
        messages.success(request, MSG_SAVED)
        logger.info(
            "transkribus_paragraph_editor_page saved user=%s doc_id=%s",
            getattr(request.user, "username", None),
            doc.id,
        )
        return redirect(editor_url)

    source_context = _document_source_preview_context(doc)
    hover_context = _paragraph_editor_hover_source_context(doc, source_context)
    editor = build_paragraph_editor_context(
        doc,
        hover_line_ids=hover_context["hover_line_ids"],
    )
    logger.info(
        "transkribus_paragraph_editor_page user=%s doc_id=%s available=%s "
        "lines=%s status=%s suggestions=%s",
        getattr(request.user, "username", None),
        doc.id,
        editor.available,
        len(editor.lines),
        editor.status.code,
        len(editor.adoption_suggestions),
    )
    return render(
        request,
        "documents/transkribus_paragraph_editor.html",
        {
            "doc": doc,
            **source_context,
            "text_line_hover": hover_context["text_line_hover"],
            "text_line_hover_single_image_overlay": hover_context[
                "text_line_hover_single_image_overlay"
            ],
            "editor_source_preview_items": hover_context["editor_source_preview_items"],
            "editor_available": editor.available,
            "unavailable_message": editor.unavailable_message,
            "freshness": editor.freshness,
            "editor_lines": editor.lines,
            "paragraph_status": editor.status,
            "source_is_rtl": editor.source_is_rtl,
            "adoption_suggestions": editor.adoption_suggestions,
            "adoption_intro": editor.adoption_intro,
        },
    )


@login_required
@require_POST
def transkribus_paragraph_adopt(request, doc_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item").prefetch_related(
            text_presentation_results_prefetch(),
            "source_files",
        ),
    )
    editor_url = _paragraph_editor_url(doc_id=doc.id)

    try:
        adopt_paragraph_editor_mapping(doc, request.POST, actor=request.user)
    except ParagraphEditorError as exc:
        messages.error(request, exc.staff_message)
        logger.info(
            "transkribus_paragraph_adopt refused user=%s doc_id=%s message=%s",
            getattr(request.user, "username", None),
            doc.id,
            exc.staff_message,
        )
        return redirect(editor_url)
    messages.success(request, MSG_ADOPTED)
    logger.info(
        "transkribus_paragraph_adopt saved user=%s doc_id=%s",
        getattr(request.user, "username", None),
        doc.id,
    )
    return redirect(editor_url)


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

    archive_search_query = normalize_archive_list_search_query(request.GET.get("q", ""))
    archive_search_geometry_matches = (
        resolve_archive_search_geometry_matches(
            doc,
            search_query=archive_search_query,
        )
        if archive_search_query
        else ()
    )
    archive_search_overlay_targets = build_archive_search_overlay_targets(
        archive_search_geometry_matches
    )
    archive_search_overlay_pages = build_archive_search_overlay_pages(
        doc,
        source_preview_items=source_context["source_preview_items"],
        content_url=source_context["content_url"],
        overlay_targets=archive_search_overlay_targets,
    )
    archive_search_source_preview_items = (
        apply_archive_search_overlay_to_source_previews(
            source_context["source_preview_items"],
            archive_search_overlay_pages,
        )
    )
    archive_search_single_image_overlay = build_archive_search_single_image_overlay(
        doc,
        content_url=source_context["content_url"],
        overlay_pages=archive_search_overlay_pages,
    )

    text_line_hover = build_text_line_hover_presentation(
        doc,
        source_preview_items=source_context["source_preview_items"],
        content_url=source_context["content_url"],
    )
    text_line_hover_overlay_pages = build_text_line_hover_overlay_pages(
        doc,
        source_preview_items=source_context["source_preview_items"],
        content_url=source_context["content_url"],
        overlay_targets=text_line_hover.overlay_targets,
    )
    text_line_hover_targets_by_page = {
        int(item["display_number"]): item.get("text_line_hover_overlay_targets", ())
        for item in apply_text_line_hover_overlay_to_source_previews(
            source_context["source_preview_items"],
            text_line_hover_overlay_pages,
        )
    }
    archive_search_source_preview_items = [
        {
            **item,
            "text_line_hover_overlay_targets": text_line_hover_targets_by_page.get(
                int(item["display_number"]),
                (),
            ),
        }
        for item in archive_search_source_preview_items
    ]
    text_line_hover_single_image_overlay = build_text_line_hover_single_image_overlay(
        doc,
        content_url=source_context["content_url"],
        overlay_pages=text_line_hover_overlay_pages,
    )
    archive_search_transcription = build_archive_search_transcription_presentation(
        doc,
        geometry_matches=archive_search_geometry_matches,
        text_line_hover=text_line_hover,
    )
    transkribus_paragraph_presentation = build_transkribus_paragraph_presentation(
        doc,
        text_line_hover=text_line_hover,
        archive_search_transcription=archive_search_transcription,
    )

    detail_jump_nav = build_document_detail_jump_nav(
        doc,
        text_presentation,
        has_source_viewer=document_detail_has_source_viewer(
            doc,
            content_url=source_context["content_url"],
            source_preview_items=source_context["source_preview_items"],
            source_preview_unavailable_count=source_context[
                "source_preview_unavailable_count"
            ],
        ),
    )

    show_transkribus_action = (
        is_admin and _is_transkribus_corrected_current_sync_ui_eligible(doc)
    )
    context = {
        "doc": doc,
        **source_context,
        "admin_meta": admin_meta,
        "text_presentation": text_presentation,
        "detail_jump_nav": detail_jump_nav,
        "displayed_transcription_text": displayed_transcription_text,
        "archive_search_query": archive_search_query,
        "archive_search_geometry_matches": archive_search_geometry_matches,
        "archive_search_overlay_targets": archive_search_overlay_targets,
        "archive_search_overlay_pages": archive_search_overlay_pages,
        "archive_search_source_preview_items": archive_search_source_preview_items,
        "archive_search_single_image_overlay": archive_search_single_image_overlay,
        "text_line_hover": text_line_hover,
        "text_line_hover_overlay_pages": text_line_hover_overlay_pages,
        "text_line_hover_single_image_overlay": text_line_hover_single_image_overlay,
        "archive_search_transcription": archive_search_transcription,
        "transkribus_paragraph_presentation": transkribus_paragraph_presentation,
        "is_admin": is_admin,
        "show_transkribus_corrected_current_sync_action": show_transkribus_action,
        "show_ocr_reprocess_action": is_admin and is_ocr_reprocess_ui_eligible(doc),
        "show_hebrew_translation_retry_action": is_admin
        and is_hebrew_translation_retry_ui_eligible(doc),
        "paragraph_status": (
            build_paragraph_mapping_staff_status(doc)
            if show_transkribus_action
            else None
        ),
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


def _transcription_suggestions_queryset_for_user(user):
    """Staff suggestion backlog/detail scoped to documents the user may view."""
    return TranscriptionEditSuggestion.objects.filter(
        document__in=document_queryset_for_user(user)
    ).select_related(
        "document",
        "document__archive_item",
        "submitter_user",
        "reviewed_by",
    )


@login_required
def transcription_suggestion_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    suggestions = list(
        _transcription_suggestions_queryset_for_user(request.user)
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
        _transcription_suggestions_queryset_for_user(request.user).prefetch_related(
            "document__source_files"
        ),
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

    # Authorize before mutation (404; no existence leak for restricted docs).
    get_object_or_404(
        _transcription_suggestions_queryset_for_user(request.user),
        id=suggestion_id,
    )

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

    get_object_or_404(
        _transcription_suggestions_queryset_for_user(request.user),
        id=suggestion_id,
    )

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


def _archive_metadata_suggestions_queryset_for_user(user):
    """Staff metadata-suggestion backlog scoped to archive items the user may view."""
    return ArchiveMetadataSuggestion.objects.filter(
        archive_item__in=archive_item_queryset_for_user(user)
    ).select_related(
        "archive_item",
        "submitter_user",
        "reviewed_by",
    )


@login_required
def archive_metadata_suggestion_backlog_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny

    suggestions = list(
        _archive_metadata_suggestions_queryset_for_user(request.user)
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

    get_object_or_404(
        _archive_metadata_suggestions_queryset_for_user(request.user),
        id=suggestion_id,
    )

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

    get_object_or_404(
        _archive_metadata_suggestions_queryset_for_user(request.user),
        id=suggestion_id,
    )

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

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )
    try:
        worker_env = validate_required_env()
    except EnvConfigError as exc:
        messages.error(request, f"שגיאת תצורה: {exc}")
        return redirect("documents-detail-page", doc_id=doc.id)

    collection_id = worker_env.transkribus_collection_id or ""
    model_id = worker_env.transkribus_model_id or ""

    try:
        apply_result = apply_ocr_reprocess(
            doc.id,
            collection_id=collection_id,
            model_id=model_id,
            initiated_by=request.user,
        )
    except OcrReprocessError as exc:
        messages.error(request, str(exc))
        return redirect("documents-detail-page", doc_id=doc.id)

    outcome = apply_result.enqueue_result.outcome
    if outcome in {"CREATED_AND_ENQUEUED", "REENQUEUED"}:
        success_message = "העיבוד מחדש תוזמן."
    elif outcome == "ALREADY_QUEUED":
        success_message = "העיבוד מחדש כבר ממתין בתור."
    elif outcome == "ALREADY_RUNNING":
        success_message = "העיבוד מחדש כבר מתבצע."
    elif outcome == "ALREADY_TERMINAL":
        success_message = "העיבוד מחדש כבר הסתיים."
    else:
        raise AssertionError(f"Unhandled OCR reprocess success outcome: {outcome}")

    messages.success(request, success_message)
    logger.info(
        "document_ocr_reprocess user=%s doc_id=%s request_id=%s "
        "retry_mode=%s enqueue_outcome=%s",
        getattr(request.user, "username", None),
        doc.id,
        apply_result.enqueue_result.request.pk,
        apply_result.assessment.retry_mode.value,
        outcome,
    )
    return redirect("documents-detail-page", doc_id=doc.id)


@login_required
@require_POST
def document_hebrew_translation_retry(request, doc_id: int):
    deny = _require_admin(request)
    if deny:
        return deny

    doc = get_viewable_document(
        request.user,
        doc_id,
        queryset=Document.objects.select_related("archive_item"),
    )
    try:
        enqueue_result = enqueue_hebrew_translation_retry(
            doc.id,
            initiated_by=request.user,
        )
    except HebrewTranslationRetryEnqueueError as exc:
        logger.warning(
            "document_hebrew_translation_retry enqueue rejected "
            "user=%s doc_id=%s code=%s outcome=%s",
            getattr(request.user, "username", None),
            doc.id,
            exc.code,
            exc.outcome,
        )
        messages.error(request, exc.public_message)
        return redirect("documents-detail-page", doc_id=doc.id)
    except HebrewTranslationRetryError:
        messages.error(request, "לא ניתן לשלוח תרגום לעברית לעיבוד כעת.")
        return redirect("documents-detail-page", doc_id=doc.id)

    outcome = enqueue_result.outcome
    if outcome in {"CREATED_AND_ENQUEUED", "REENQUEUED"}:
        success_message = "תרגום לעברית נשלח לעיבוד."
    elif outcome == "ALREADY_QUEUED":
        success_message = "תרגום לעברית כבר ממתין בתור."
    elif outcome == "ALREADY_RUNNING":
        success_message = "תרגום לעברית כבר מתבצע."
    elif outcome == "ALREADY_TERMINAL":
        success_message = "עיבוד התרגום לעברית כבר הסתיים."
    else:
        raise AssertionError(
            f"Unhandled Hebrew translation retry success outcome: {outcome}"
        )

    messages.success(request, success_message)
    logger.info(
        "document_hebrew_translation_retry user=%s doc_id=%s "
        "request_id=%s enqueue_outcome=%s",
        getattr(request.user, "username", None),
        doc.id,
        enqueue_result.request.pk,
        outcome,
    )
    return redirect("documents-detail-page", doc_id=doc.id)


UPLOAD_UI_REVISION = "2026-08-05.1"


def _upload_form_context(*, user=None) -> dict:
    form_data = {
        **empty_discovery_metadata_form_fields(),
        **empty_archive_item_people_form_fields(),
    }
    return {
        "doc_type_choices": Document.DocType.choices,
        "text_input_type_choices": TEXT_INPUT_TYPE_UI_CHOICES,
        "handwriting_type_choices": HANDWRITING_TYPE_UI_CHOICES,
        "handwriting_type_default": Document.HandwritingType.VS,
        "hebrew_language_value": Document.Language.HEBREW,
        "handwritten_text_input_value": Document.TextInputType.HANDWRITTEN,
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "visibility_choices": archive_visibility_ui_choices(user),
        "empty_date_form_data": archive_date_form_data(
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        ),
        "form_data": form_data,
        "discovery_tags_input_name": "discovery_tags",
        "discovery_tags_input_id": "discovery_tags",
        "upload_ui_revision": UPLOAD_UI_REVISION,
        **discovery_metadata_option_querysets(),
        **_archive_item_people_staff_form_context(
            item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
            form_data=form_data,
        ),
    }


def _photo_upload_form_context(*, user=None) -> dict:
    form_data = {
        **_empty_archive_metadata_form_data(),
        **empty_photo_metadata_form_data(),
        **empty_discovery_metadata_form_fields(),
        **empty_archive_item_people_form_fields(),
    }
    return {
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "visibility_choices": archive_visibility_ui_choices(user),
        "metadata_status_choices": archive_metadata_status_ui_choices(),
        "form_data": form_data,
        "discovery_tags_input_name": "discovery_tags",
        "discovery_tags_input_id": "discovery_tags",
        **discovery_metadata_option_querysets(),
        **_archive_item_people_staff_form_context(
            item_type=ArchiveItem.ItemType.PHOTO,
            form_data=form_data,
        ),
    }


@login_required
@never_cache
def upload_page(request):
    deny = _require_admin_page(request)
    if deny:
        return deny
    return render(
        request,
        "documents/upload.html",
        context=_upload_form_context(user=request.user),
    )


def _archive_metadata_form_context(
    *,
    form_data: dict,
    form_errors: list[str],
    page_title: str,
    submit_label: str,
    user=None,
) -> dict:
    return {
        "form_data": form_data,
        "form_errors": form_errors,
        "page_title": page_title,
        "submit_label": submit_label,
        "visibility_choices": archive_visibility_ui_choices(user),
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "metadata_status_choices": archive_metadata_status_ui_choices(),
    }


def _manual_text_form_context(
    *,
    form_data: dict,
    form_errors: list[str],
    page_title: str,
    submit_label: str,
    user=None,
) -> dict:
    return _archive_metadata_form_context(
        form_data=form_data,
        form_errors=form_errors,
        page_title=page_title,
        submit_label=submit_label,
        user=user,
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
    return archive_metadata_form_data_for_template(
        title=title,
        visibility=visibility,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
        metadata_status=metadata_status,
        author_name=author_name,
        source_title=source_title,
        public_note=public_note,
    )


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
    item = document.archive_item
    return {
        **_archive_metadata_form_data_from_document(document),
        **_ocr_catalog_form_data_from_document(document),
        **discovery_metadata_form_data_from_item(item),
        **archive_item_people_form_data_from_item(item),
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
        **empty_archive_item_people_form_fields(),
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
        **archive_item_people_form_data_from_item(item),
    }


def _photo_form_data_from_item(item: ArchiveItem) -> dict:
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
        **discovery_metadata_form_data_from_item(item),
        **archive_item_people_form_data_from_item(item),
    }


def _staff_photo_manage_rows(item: ArchiveItem) -> list:
    photos = list(staff_photo_contents_queryset(item))
    return build_staff_photo_manage_rows(
        item,
        photos=photos,
        bucket=getattr(settings, "UPLOADS_BUCKET_NAME", ""),
        expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
    )


def _get_staff_photo_archive_item(request, item_id: int) -> ArchiveItem:
    item = get_accessible_archive_item(
        request.user,
        item_id,
        queryset=ArchiveItem.objects.prefetch_related("categories", "events", "tags"),
    )
    if item.item_type != ArchiveItem.ItemType.PHOTO:
        raise Http404()
    return item


def _get_staff_photo_content(
    request, item_id: int, photo_id: int
) -> tuple[ArchiveItem, PhotoContent]:
    item = _get_staff_photo_archive_item(request, item_id)
    photo_content = (
        PhotoContent.objects.filter(pk=photo_id, archive_item_id=item.pk)
        .prefetch_related("people")
        .first()
    )
    if photo_content is None:
        raise Http404()
    return item, photo_content


PERSON_NAME_UPDATED_MSG = "שם התצוגה עודכן."
PERSON_ALIAS_ADDED_MSG = "השם החלופי נוסף."
PERSON_ALIAS_UPDATED_MSG = "השם החלופי עודכן."
PERSON_ALIAS_DELETED_MSG = "השם החלופי נמחק."
ARCHIVE_ITEM_UPDATED_MSG = "הפריט עודכן."
ARCHIVE_ITEM_PEOPLE_HEADING = "אנשים קשורים"
PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING = "אנשים קשורים לפריט"
ARCHIVE_ITEM_PEOPLE_CURRENT_HEADING = "אנשים קשורים לפריט זה"
ARCHIVE_ITEM_PEOPLE_HINT = (
    "בחירה מרשומות אדם קיימות. שמות חלופיים מוצגים בסוגריים לזיהוי בלבד, "
    "ואינם זהויות נפרדות."
)
PHOTO_ARCHIVE_ITEM_PEOPLE_HINT = (
    "קשר ברמת פריט הארכיון, לא הופעה בתמונה. "
    "אנשים מזוהים בתמונה נערכים בדף התמונה עצמו."
)


def _archive_item_people_staff_form_context(*, item_type: str, form_data: dict) -> dict:
    selected_person_ids = [
        int(person_id) for person_id in form_data.get("archive_item_person_ids") or []
    ]
    person_choices, selected_people = build_staff_person_choices(
        selected_person_ids=selected_person_ids
    )
    is_photo = item_type == ArchiveItem.ItemType.PHOTO
    return {
        "show_archive_item_people": True,
        "archive_item_person_choices": person_choices,
        "archive_item_selected_people": selected_people,
        "archive_item_people_heading": (
            PHOTO_ARCHIVE_ITEM_PEOPLE_HEADING
            if is_photo
            else ARCHIVE_ITEM_PEOPLE_HEADING
        ),
        "archive_item_people_current_heading": ARCHIVE_ITEM_PEOPLE_CURRENT_HEADING,
        "archive_item_people_hint": (
            PHOTO_ARCHIVE_ITEM_PEOPLE_HINT if is_photo else ARCHIVE_ITEM_PEOPLE_HINT
        ),
    }


def _parse_archive_item_people_post(request, form_data: dict, form_errors: list[str]):
    parsed_people, people_errors = parse_archive_item_people_form(request.POST)
    return {**form_data, **parsed_people}, form_errors + people_errors, parsed_people


def _save_archive_item_people(
    item: ArchiveItem,
    parsed_people: dict,
    *,
    refresh_search_index: bool = False,
) -> None:
    set_archive_item_people(
        archive_item=item,
        person_ids=list(parsed_people.get("archive_item_person_ids") or []),
        new_person_name=parsed_people.get("new_archive_item_person_name") or "",
        refresh_search_index=refresh_search_index,
    )


def _photo_content_edit_form_context(
    *,
    item: ArchiveItem,
    photo_content: PhotoContent,
    form_data: dict,
    form_errors: list[str],
) -> dict:
    selected_person_ids = [
        int(person_id) for person_id in form_data.get("person_ids") or []
    ]
    person_choices, selected_people = build_staff_person_choices(
        selected_person_ids=selected_person_ids
    )
    return {
        "item": item,
        "photo_content": photo_content,
        "form_data": form_data,
        "form_errors": form_errors,
        "page_title": "עריכת תמונה בפריט",
        "submit_label": "עדכון",
        "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        "person_choices": person_choices,
        "selected_people": selected_people,
        "selected_person_ids": set(selected_person_ids),
    }


def _empty_video_form_data() -> dict:
    return {
        **_empty_archive_metadata_form_data(),
        "source_url": "",
        "provider": "",
        "presentation_mode": "",
        "provider_video_id": "",
        "start_seconds": None,
        "end_seconds": None,
        "start_seconds_display": "",
        "end_seconds_display": "",
        "presentation_mode_explanation": "",
        "provider_display_label": "",
        **empty_discovery_metadata_form_fields(),
        **empty_archive_item_people_form_fields(),
    }


def _video_form_data_from_item(item: ArchiveItem) -> dict:
    content = item.video_content
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
        "source_url": content.source_url,
        "provider": content.provider,
        "presentation_mode": content.presentation_mode,
        "provider_video_id": content.provider_video_id,
        "start_seconds": content.start_seconds,
        "end_seconds": content.end_seconds,
        "start_seconds_display": format_video_time_for_form(content.start_seconds),
        "end_seconds_display": format_video_time_for_form(content.end_seconds),
        "presentation_mode_explanation": video_presentation_mode_explanation(
            content.presentation_mode,
            provider=content.provider,
        ),
        "provider_display_label": video_provider_display_label(content.provider),
        **discovery_metadata_form_data_from_item(item),
        **archive_item_people_form_data_from_item(item),
    }


def _video_form_context(
    *,
    form_data: dict,
    form_errors: list[str],
    page_title: str,
    submit_label: str,
    user=None,
) -> dict:
    provider = (form_data.get("provider") or "").strip().upper()
    return {
        **_archive_metadata_form_context(
            form_data=form_data,
            form_errors=form_errors,
            page_title=page_title,
            submit_label=submit_label,
            user=user,
        ),
        "show_video_time_fields": provider in ("", PROVIDER_YOUTUBE),
    }


def _video_service_time_kwargs(parsed: dict) -> dict:
    """Pass form-resolved times into create/update (already merged with URL defaults)."""
    return {
        "start_seconds": parsed["start_seconds"],
        "end_seconds": parsed["end_seconds"],
    }


def _submit_video_create(request):
    parsed, form_errors = parse_video_archive_item_form(request.POST, user=request.user)
    form_data = parsed
    form_data, form_errors, parsed_people = _parse_archive_item_people_post(
        request, form_data, form_errors
    )
    if form_errors:
        return None, form_data, form_errors
    with transaction.atomic():
        item = create_video_archive_item(
            title=parsed["title"],
            source_url=parsed["source_url"],
            visibility=parsed["visibility"],
            date_start=parsed["date_start_value"],
            date_end=parsed["date_end_value"],
            date_precision=parsed["date_precision"],
            metadata_status=parsed["metadata_status"],
            author_name=parsed["author_name"],
            source_title=parsed["source_title"],
            public_note=parsed["public_note"],
            category_names=parsed["category_names"],
            event_names=parsed["event_names"],
            tag_names=parsed["tag_names"],
            user=request.user,
            **_video_service_time_kwargs(parsed),
        )
        _save_archive_item_people(item, parsed_people, refresh_search_index=True)
    return redirect("archive-manage-list"), form_data, form_errors


def _archive_item_type_choices() -> list[tuple[str, str]]:
    return archive_manage_item_type_ui_choices()


def _normalized_archive_item_type(raw: str | None) -> str:
    value = (raw or "").strip()
    if value in _VALID_ARCHIVE_ITEM_CREATE_TYPES:
        return value
    return ""


def _submit_manual_text_create(request):
    parsed, form_errors = parse_manual_text_form(request.POST, user=request.user)
    parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
        request.POST,
        tags_field="tags",
    )
    form_errors = form_errors + discovery_errors
    form_data = {**parsed, **parsed_discovery}
    form_data, form_errors, parsed_people = _parse_archive_item_people_post(
        request, form_data, form_errors
    )
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
        _save_archive_item_people(item, parsed_people)
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
        "video_content",
    ).prefetch_related(
        "photo_contents",
        "categories",
        "events",
        "tags",
        archive_browse_displayable_text_results_prefetch(),
    )


def _archive_browse_items_queryset(user, **filter_kwargs):
    return _archive_browse_select_related(
        archive_browse_queryset_for_user(user).filter(**filter_kwargs)
    ).order_by("-created_at")


def _archive_browse_cards_for_items(items, *, search_query: str = ""):
    cards = build_archive_browse_cards(items, search_query=search_query)
    bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
    cards = apply_photo_thumbnail_urls_to_browse_cards(
        cards,
        bucket=bucket,
        expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
    )
    return apply_document_thumbnail_urls_to_browse_cards(
        cards,
        bucket=bucket,
        expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
    )


def _archive_browse_page_context(*, page_title: str, items, user) -> dict:
    return {
        "page_title": page_title,
        "items": items,
        "browse_cards": _archive_browse_cards_for_items(items),
        "is_admin": _is_admin(user),
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
            user=request.user,
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
            user=request.user,
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
            user=request.user,
        ),
    )


def archive_list_page(request):
    item_type_filter = normalize_archive_public_list_type_filter(
        request.GET.get("item_type")
    )
    search_query = normalize_archive_list_search_query(request.GET.get("q"))
    year_validation = validate_archive_advanced_year_fields(request.GET)
    advanced_filters = filters_for_archive_list_search(
        request.GET,
        year_validation=year_validation,
    )
    year_validation_failed = not year_validation.is_valid
    advanced_panel_open = (
        archive_advanced_panel_is_requested(request.GET) or year_validation_failed
    )
    load_advanced_choices = should_load_archive_advanced_filter_choices(
        panel_open=advanced_panel_open,
        advanced_filters_active=advanced_filters.is_active(),
    )

    authorized_items = archive_browse_queryset_for_user(request.user)
    if load_advanced_choices:
        choice_context = archive_advanced_filter_choice_context(authorized_items)
    else:
        choice_context = dict(EMPTY_ARCHIVE_ADVANCED_FILTER_CHOICE_CONTEXT)

    # Any authoritative year-validation failure blocks result execution until
    # the form is corrected. Non-date filters remain in the redisplayed form
    # (via advanced_filters / q) but must not produce a successful result set.
    #
    # Type-tab counts use the authorized + advanced + q universe BEFORE
    # item_type filtering so switching tabs does not change sibling counts.
    browse_base = authorized_items.order_by("-created_at")
    if year_validation_failed:
        filtered_universe = browse_base.none()
    else:
        filtered_universe = filter_archive_items_by_advanced_filters(
            browse_base,
            advanced_filters,
        )
        filtered_universe = filter_archive_items_by_search_query(
            filtered_universe,
            search_query,
        )
    type_counts = aggregate_archive_public_list_type_counts(filtered_universe)
    items = filter_archive_items_by_public_list_type(
        filtered_universe,
        item_type_filter,
    )
    items = _archive_browse_select_related(items)
    total_count = type_counts.get(item_type_filter, type_counts[""])
    per_page = normalize_archive_public_list_per_page(request.GET.get("per_page"))
    page = normalize_archive_public_list_page(
        request.GET.get("page"),
        total_count=total_count,
        per_page=per_page,
    )
    offset = (page - 1) * per_page
    page_items = list(items[offset : offset + per_page])
    browse_cards = _archive_browse_cards_for_items(
        page_items,
        search_query=search_query,
    )
    # PR4: snippets/match-source only for the authorized page slice (no N+1).
    browse_cards = apply_archive_search_match_presentation_to_cards(
        browse_cards,
        search_query=search_query,
    )
    search_or_filters_active = bool(
        search_query or advanced_filters.is_active() or year_validation_failed
    )
    return render(
        request,
        "documents/archive/list.html",
        context={
            "items": page_items,
            "browse_cards": browse_cards,
            "is_admin": _is_admin(request.user),
            "item_type_filter": item_type_filter,
            "q": search_query,
            "advanced_panel_open": advanced_panel_open,
            "advanced_year_validation_errors": year_validation.errors,
            "advanced_year_validation_failed": year_validation_failed,
            "search_or_filters_active": search_or_filters_active,
            "load_advanced_choices": load_advanced_choices,
            **archive_advanced_filter_template_context(advanced_filters),
            **archive_advanced_year_form_values(advanced_filters, year_validation),
            **choice_context,
            **archive_public_list_filter_context(
                q=search_query,
                item_type_filter=item_type_filter,
                per_page=per_page,
                advanced_filters=advanced_filters,
                advanced_open=advanced_panel_open,
                type_counts=type_counts,
            ),
            **archive_public_list_active_filter_summary_context(
                q=search_query,
                item_type_filter=item_type_filter,
                per_page=per_page,
                advanced_filters=advanced_filters,
                category_choices=choice_context["advanced_filter_category_choices"],
                event_choices=choice_context["advanced_filter_event_choices"],
                tag_choices=choice_context["advanced_filter_tag_choices"],
                person_choices=choice_context["advanced_filter_person_choices"],
            ),
            **archive_public_list_pagination_context(
                total_count=total_count,
                page=page,
                per_page=per_page,
                q=search_query,
                item_type_filter=item_type_filter,
                advanced_filters=advanced_filters,
            ),
        },
    )


def archive_detail_page(request, item_id: int):
    detail_qs = ArchiveItem.objects.prefetch_related("categories", "events", "tags")
    item = get_viewable_archive_item(request.user, item_id, queryset=detail_qs)

    if item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        doc = Document.objects.filter(archive_item_id=item.id).first()
        if doc is None:
            raise Http404()

        detail_url = reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        search_query = normalize_archive_list_search_query(request.GET.get("q"))
        if search_query:
            detail_url = f"{detail_url}?{urlencode({'q': search_query})}"
        return redirect(detail_url)

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
        bucket = getattr(settings, "UPLOADS_BUCKET_NAME", "")
        photo_gallery = build_public_photo_gallery(
            item,
            selected_photo_param=request.GET.get("photo"),
            bucket=bucket,
            expires_in=PRESIGNED_GET_EXPIRY_SECONDS,
        )
        if photo_gallery is None:
            raise Http404()

        photo_content = photo_gallery.selected
        photo_url = None
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
                "photo_gallery": photo_gallery,
                "is_admin": _is_admin(request.user),
            },
        )

    if item.item_type == ArchiveItem.ItemType.VIDEO:
        video_presentation = build_video_public_presentation(item)
        if video_presentation is None:
            raise Http404()
        return render(
            request,
            "documents/archive/detail.html",
            context={
                "item": item,
                "body": None,
                "photo_url": None,
                "video_presentation": video_presentation,
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
        archive_item_queryset_for_user(request.user)
        .select_related(
            "manual_text_content",
            "ocr_document",
            "video_content",
        )
        .prefetch_related("photo_contents")
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
    if item_type == ARCHIVE_ITEM_TYPE_VIDEO:
        form_data = _empty_video_form_data()
    else:
        form_data = _empty_manual_text_form_data()

    if request.method == "POST" and item_type == ARCHIVE_ITEM_TYPE_MANUAL_TEXT:
        success_redirect, form_data, form_errors = _submit_manual_text_create(request)
        if success_redirect:
            return success_redirect
    elif request.method == "POST" and item_type == ARCHIVE_ITEM_TYPE_VIDEO:
        success_redirect, form_data, form_errors = _submit_video_create(request)
        if success_redirect:
            return success_redirect

    if item_type == ARCHIVE_ITEM_TYPE_VIDEO:
        context = {
            "item_type": item_type,
            "item_type_choices": _archive_item_type_choices(),
            **_video_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="יצירת פריט חדש",
                submit_label="שמירה",
                user=request.user,
            ),
            **_manual_text_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=ArchiveItem.ItemType.VIDEO,
                form_data=form_data,
            ),
        }
    else:
        context = {
            "item_type": item_type,
            "item_type_choices": _archive_item_type_choices(),
            **_manual_text_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="יצירת פריט חדש",
                submit_label="שמירה",
                user=request.user,
            ),
        }
        if item_type == ARCHIVE_ITEM_TYPE_MANUAL_TEXT:
            context.update(_manual_text_discovery_metadata_form_context())
            context.update(
                _archive_item_people_staff_form_context(
                    item_type=ArchiveItem.ItemType.MANUAL_TEXT,
                    form_data=form_data,
                )
            )
        elif item_type == ARCHIVE_ITEM_TYPE_OCR_DOCUMENT:
            context.update(_upload_form_context(user=request.user))
        elif item_type == ARCHIVE_ITEM_TYPE_PHOTO:
            context.update(_photo_upload_form_context(user=request.user))
    response = render(
        request,
        "documents/archive/manage_new.html",
        context=context,
    )
    if item_type == ARCHIVE_ITEM_TYPE_OCR_DOCUMENT:
        add_never_cache_headers(response)
    return response


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
                user=request.user,
            ),
            **_manual_text_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=ArchiveItem.ItemType.MANUAL_TEXT,
                form_data=form_data,
            ),
        },
    )


@login_required
def archive_manage_edit_page(request, item_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    item = get_accessible_archive_item(
        request.user,
        item_id,
        queryset=ArchiveItem.objects.select_related(
            "manual_text_content",
            "ocr_document",
            "video_content",
        ).prefetch_related("photo_contents", "categories", "events", "tags"),
    )

    if item.item_type == ArchiveItem.ItemType.MANUAL_TEXT:
        return _archive_manage_edit_manual_text(request, item)
    if item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT:
        return _archive_manage_edit_ocr_document(request, item)
    if item.item_type == ArchiveItem.ItemType.PHOTO:
        return _archive_manage_edit_photo(request, item)
    if item.item_type == ArchiveItem.ItemType.VIDEO:
        return _archive_manage_edit_video(request, item)
    raise Http404()


def _archive_manage_edit_manual_text(request, item: ArchiveItem):
    form_errors: list[str] = []
    form_data = _manual_text_form_data_from_item(item)

    if request.method == "POST":
        parsed, form_errors = parse_manual_text_form(request.POST, user=request.user)
        parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
            request.POST,
            tags_field="tags",
        )
        form_errors = form_errors + discovery_errors
        form_data = {**parsed, **parsed_discovery}
        form_data, form_errors, parsed_people = _parse_archive_item_people_post(
            request, form_data, form_errors
        )
        if not form_errors:
            try:
                with transaction.atomic():
                    _save_archive_item_people(item, parsed_people)
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
            except ArchiveItemPersonError as exc:
                form_errors = [exc.message]
            else:
                messages.success(request, ARCHIVE_ITEM_UPDATED_MSG)
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
                user=request.user,
            ),
            **_manual_text_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=item.item_type, form_data=form_data
            ),
        },
    )


def _archive_manage_edit_photo(request, item: ArchiveItem):
    form_errors: list[str] = []
    form_data = _photo_form_data_from_item(item)

    if request.method == "POST":
        parsed, form_errors = parse_archive_metadata_form(
            request.POST, user=request.user
        )
        parsed_discovery, discovery_errors = parse_archive_item_discovery_metadata_form(
            request.POST,
            tags_field="tags",
        )
        form_errors = form_errors + discovery_errors
        form_data = {**parsed, **parsed_discovery}
        form_data, form_errors, parsed_people = _parse_archive_item_people_post(
            request, form_data, form_errors
        )
        if not form_errors:
            try:
                with transaction.atomic():
                    _save_archive_item_people(item, parsed_people)
                    update_photo_archive_item_metadata(
                        item,
                        title=parsed["title"],
                        visibility=parsed["visibility"],
                        date_start=parsed["date_start_value"],
                        date_end=parsed["date_end_value"],
                        date_precision=parsed["date_precision"],
                        metadata_status=parsed["metadata_status"],
                        public_note=parsed["public_note"],
                    )
                    update_archive_item_discovery_metadata(
                        item,
                        category_names=parsed_discovery["category_names"],
                        event_names=parsed_discovery["event_names"],
                        tag_names=parsed_discovery["tag_names"],
                    )
            except ArchiveItemPersonError as exc:
                form_errors = [exc.message]
            else:
                messages.success(request, ARCHIVE_ITEM_UPDATED_MSG)
                return redirect("archive-manage-list")

    return render(
        request,
        "documents/archive/photo_form.html",
        context={
            "item": item,
            "photo_rows": _staff_photo_manage_rows(item),
            **_archive_metadata_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="עריכת תמונה",
                submit_label="עדכון",
                user=request.user,
            ),
            **_manual_text_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=item.item_type, form_data=form_data
            ),
        },
    )


@login_required
def archive_manage_photo_add_page(request, item_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    item = _get_staff_photo_archive_item(request, item_id)
    form_data = {
        **empty_photo_metadata_form_data(),
        **archive_date_form_data(
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        ),
    }
    return render(
        request,
        "documents/archive/photo_add.html",
        context={
            "item": item,
            "form_data": form_data,
            "form_errors": [],
            "page_title": "הוספת תמונה לפריט",
            "date_precision_choices": DATE_PRECISION_UI_CHOICES,
        },
    )


@login_required
def archive_manage_photo_edit_page(request, item_id: int, photo_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    item, photo_content = _get_staff_photo_content(request, item_id, photo_id)
    form_errors: list[str] = []
    form_data = photo_content_staff_form_data(photo_content)

    if request.method == "POST":
        parsed, form_errors = parse_photo_content_staff_form(request.POST)
        form_data = parsed
        if not form_errors:
            try:
                update_photo_content_metadata(
                    photo_content,
                    description=parsed["description"],
                    location=parsed["location"],
                    context=parsed["context"],
                    people_present=parsed["people_present"],
                    notes=parsed["notes"],
                    date_start=parsed["date_start_value"],
                    date_end=parsed["date_end_value"],
                    date_precision=parsed["date_precision"],
                    person_ids=parsed["person_ids"],
                    new_person_name=parsed["new_person_name"],
                )
            except PhotoContentManagementError as exc:
                form_errors = [exc.message]
            else:
                return redirect("archive-manage-edit", item_id=item.id)

    return render(
        request,
        "documents/archive/photo_content_edit.html",
        context=_photo_content_edit_form_context(
            item=item,
            photo_content=photo_content,
            form_data=form_data,
            form_errors=form_errors,
        ),
    )


@login_required
def archive_manage_photo_reorder(request, item_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    item = _get_staff_photo_archive_item(request, item_id)
    raw_ids = request.POST.getlist("photo_ids")
    ordered_ids: list[int] = []
    try:
        for raw in raw_ids:
            ordered_ids.append(int(raw))
    except (TypeError, ValueError):
        messages.error(request, PHOTO_NOT_IN_ITEM_ERROR)
        return redirect("archive-manage-edit", item_id=item.id)

    try:
        reorder_photo_contents(item, ordered_ids)
    except PhotoContentManagementError as exc:
        messages.error(request, exc.message)
    return redirect("archive-manage-edit", item_id=item.id)


@login_required
def archive_manage_photo_delete_page(request, item_id: int, photo_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    item, photo_content = _get_staff_photo_content(request, item_id, photo_id)
    form_errors: list[str] = []

    if request.method == "POST":
        try:
            delete_one_photo_content(
                photo_content,
                bucket=settings.UPLOADS_BUCKET_NAME,
            )
        except PhotoContentManagementError as exc:
            form_errors = [exc.message]
        else:
            return redirect("archive-manage-edit", item_id=item.id)

    photo_count = item.photo_contents.count()
    return render(
        request,
        "documents/archive/photo_content_delete_confirm.html",
        context={
            "item": item,
            "photo_content": photo_content,
            "form_errors": form_errors,
            "is_last_photo": photo_count <= 1,
            "last_photo_error": LAST_PHOTO_DELETE_ERROR,
        },
    )


def _get_staff_person(person_id: int) -> Person:
    return get_object_or_404(
        Person.objects.prefetch_related(staff_person_aliases_prefetch()),
        pk=person_id,
    )


def _get_staff_person_alias(
    person_id: int, alias_id: int
) -> tuple[Person, PersonAlias]:
    person = _get_staff_person(person_id)
    alias = get_object_or_404(PersonAlias, pk=alias_id, person_id=person.pk)
    return person, alias


def _person_edit_form_context(
    *,
    person: Person,
    form_errors: list[str],
    canonical_name: str | None = None,
    alias_name: str = "",
) -> dict:
    return {
        "person": person,
        "aliases": list(person.aliases.all()),
        "canonical_name": person.name if canonical_name is None else canonical_name,
        "alias_name": alias_name,
        "form_errors": form_errors,
        "page_title": "עריכת אדם",
    }


@login_required
def archive_manage_person_edit_page(request, person_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    person = _get_staff_person(person_id)
    form_errors: list[str] = []
    canonical_name = person.name
    alias_name = ""

    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        if action == "update_name":
            submitted_name = request.POST.get("name") or ""
            try:
                person = update_person_name(person, name=submitted_name)
            except PhotoContentManagementError as exc:
                form_errors = [exc.message]
                canonical_name = submitted_name
            else:
                messages.success(request, PERSON_NAME_UPDATED_MSG)
                return redirect("archive-manage-person-edit", person_id=person.id)
        elif action == "add_alias":
            submitted_alias = request.POST.get("alias_name") or ""
            try:
                create_person_alias(person, name=submitted_alias)
            except PhotoContentManagementError as exc:
                form_errors = [exc.message]
                alias_name = submitted_alias
            else:
                messages.success(request, PERSON_ALIAS_ADDED_MSG)
                return redirect("archive-manage-person-edit", person_id=person.id)
        else:
            return HttpResponseBadRequest("פעולה לא תקינה.")

        person = _get_staff_person(person.id)

    return render(
        request,
        "documents/archive/person_edit.html",
        context=_person_edit_form_context(
            person=person,
            form_errors=form_errors,
            canonical_name=canonical_name,
            alias_name=alias_name,
        ),
    )


@login_required
def archive_manage_person_alias_edit_page(request, person_id: int, alias_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    person, alias = _get_staff_person_alias(person_id, alias_id)
    form_errors: list[str] = []
    alias_name = alias.name

    if request.method == "POST":
        submitted_name = request.POST.get("name") or ""
        try:
            update_person_alias(alias, name=submitted_name)
        except PhotoContentManagementError as exc:
            form_errors = [exc.message]
            alias_name = submitted_name
        else:
            messages.success(request, PERSON_ALIAS_UPDATED_MSG)
            return redirect("archive-manage-person-edit", person_id=person.id)

    return render(
        request,
        "documents/archive/person_alias_edit.html",
        context={
            "person": person,
            "alias": alias,
            "alias_name": alias_name,
            "form_errors": form_errors,
            "page_title": "עריכת שם חלופי",
        },
    )


@login_required
def archive_manage_person_alias_delete_page(request, person_id: int, alias_id: int):
    deny = _require_admin_page(request)
    if deny:
        return deny

    person, alias = _get_staff_person_alias(person_id, alias_id)
    form_errors: list[str] = []

    if request.method == "POST":
        try:
            delete_person_alias(alias)
        except PhotoContentManagementError as exc:
            form_errors = [exc.message]
        else:
            messages.success(request, PERSON_ALIAS_DELETED_MSG)
            return redirect("archive-manage-person-edit", person_id=person.id)

    return render(
        request,
        "documents/archive/person_alias_delete_confirm.html",
        context={
            "person": person,
            "alias": alias,
            "form_errors": form_errors,
            "page_title": "מחיקת שם חלופי",
        },
    )


def _archive_manage_edit_video(request, item: ArchiveItem):
    form_errors: list[str] = []
    form_data = _video_form_data_from_item(item)

    if request.method == "POST":
        parsed, form_errors = parse_video_archive_item_form(
            request.POST, user=request.user
        )
        form_data = parsed
        form_data, form_errors, parsed_people = _parse_archive_item_people_post(
            request, form_data, form_errors
        )
        if not form_errors:
            try:
                with transaction.atomic():
                    _save_archive_item_people(item, parsed_people)
                    update_video_archive_item(
                        item,
                        title=parsed["title"],
                        source_url=parsed["source_url"],
                        visibility=parsed["visibility"],
                        date_start=parsed["date_start_value"],
                        date_end=parsed["date_end_value"],
                        date_precision=parsed["date_precision"],
                        metadata_status=parsed["metadata_status"],
                        author_name=parsed["author_name"],
                        source_title=parsed["source_title"],
                        public_note=parsed["public_note"],
                        category_names=parsed["category_names"],
                        event_names=parsed["event_names"],
                        tag_names=parsed["tag_names"],
                        user=request.user,
                        **_video_service_time_kwargs(parsed),
                    )
            except ArchiveItemPersonError as exc:
                form_errors = [exc.message]
            else:
                messages.success(request, ARCHIVE_ITEM_UPDATED_MSG)
                return redirect("archive-manage-list")

    return render(
        request,
        "documents/archive/video_form.html",
        context={
            "item": item,
            **_video_form_context(
                form_data=form_data,
                form_errors=form_errors,
                page_title="עריכת סרטון",
                submit_label="עדכון",
                user=request.user,
            ),
            **_manual_text_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=item.item_type, form_data=form_data
            ),
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
        parsed_shared, shared_errors = parse_archive_metadata_form(
            request.POST, user=request.user
        )
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
        form_data, form_errors, parsed_people = _parse_archive_item_people_post(
            request, form_data, form_errors
        )
        if not form_errors:
            try:
                with transaction.atomic():
                    _save_archive_item_people(item, parsed_people)
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
            except ArchiveItemPersonError as exc:
                form_errors = [exc.message]
            else:
                messages.success(request, ARCHIVE_ITEM_UPDATED_MSG)
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
                user=request.user,
            ),
            "show_discovery_metadata": True,
            **_ocr_discovery_metadata_form_context(),
            **_archive_item_people_staff_form_context(
                item_type=item.item_type, form_data=form_data
            ),
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

    item = get_accessible_archive_item(
        request.user,
        item_id,
        queryset=ArchiveItem.objects.select_related(
            "manual_text_content",
            "video_content",
        )
        .prefetch_related("photo_contents")
        .filter(
            item_type__in=(
                ArchiveItem.ItemType.MANUAL_TEXT,
                ArchiveItem.ItemType.PHOTO,
                ArchiveItem.ItemType.VIDEO,
            ),
        ),
    )

    if request.method == "POST":
        photo_cleanups: list[tuple[str, str, int | None]] = []
        if item.item_type == ArchiveItem.ItemType.PHOTO:
            photo_cleanups = [
                (
                    photo_content.original_file_key,
                    photo_content.thumbnail_file_key,
                    photo_content.pk,
                )
                for photo_content in item.photo_contents.all()
            ]

        with transaction.atomic():
            item.delete()
            for (
                original_file_key,
                thumbnail_file_key,
                photo_content_id,
            ) in photo_cleanups:
                schedule_photo_s3_cleanup_after_commit(
                    bucket=settings.UPLOADS_BUCKET_NAME,
                    original_file_key=original_file_key,
                    thumbnail_file_key=thumbnail_file_key,
                    photo_content_id=photo_content_id,
                )

        return redirect("archive-manage-list")

    if item.item_type == ArchiveItem.ItemType.PHOTO:
        template_name = "documents/archive/photo_delete_confirm.html"
    elif item.item_type == ArchiveItem.ItemType.VIDEO:
        template_name = "documents/archive/video_delete_confirm.html"
    else:
        template_name = "documents/archive/manual_text_delete_confirm.html"

    return render(
        request,
        template_name,
        context={"item": item},
    )
