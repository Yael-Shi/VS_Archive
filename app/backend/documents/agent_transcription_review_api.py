"""Thin HTTP views for Pilot 01 transcription-review agent read APIs."""

from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from documents.services.transcription_review_agent_read import (
    ScanQueryError,
    build_review_bundle_response,
    build_scan_response,
    parse_scan_query,
)


@require_GET
def transcription_review_documents_scan(request):
    try:
        after_id, limit = parse_scan_query(
            after_id_raw=request.GET.get("after_id"),
            limit_raw=request.GET.get("limit"),
        )
    except ScanQueryError as exc:
        return JsonResponse(
            {"error": exc.code, "detail": exc.detail},
            status=400,
        )

    payload = build_scan_response(
        user=request.user,
        after_id=after_id,
        limit=limit,
    )
    return JsonResponse(payload)


@require_GET
def transcription_review_document_review_bundle(request, document_id: int):
    payload = build_review_bundle_response(
        user=request.user,
        document_id=document_id,
    )
    return JsonResponse(payload)
