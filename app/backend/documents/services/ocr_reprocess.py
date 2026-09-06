"""Staff/admin OCR reprocess planning for OCR-backed documents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    DocumentTextResult,
    GeminiOcrPageCheckpoint,
    TranskribusRun,
)
from documents.services import transkribus_run_persistence as trp
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedPageSource,
    build_arabic_printed_attempt_identity,
)
from documents.services.ocr_routing import OcrRouteConfig, select_ocr_route


class OcrReprocessError(RuntimeError):
    """Raised when a document cannot be reprocessed."""


class OcrRetryMode(str, Enum):
    NORMAL_REENQUEUE = "normal_reenqueue"
    TRANSKRIBUS_RECOGNITION_ONLY = "transkribus_recognition_only"


OCR_RETRY_MODE_PAYLOAD_KEY = "ocr_retry_mode"
SOURCE_TRANSKRIBUS_RUN_ID_PAYLOAD_KEY = "source_transkribus_run_id"

# Keep these string-equal to arabic_printed_banded_ocr. Do not import that
# module here: staff reprocess runs in web processes that must not load
# Antigravity/Gemini provider engines.
_ARABIC_PRINTED_VISION_AMBIGUOUS = "ARABIC_PRINTED_VISION_AMBIGUOUS"
_ARABIC_PRINTED_AMBIGUOUS_BAND_FAILURE_CODES = frozenset(
    {
        "ARABIC_PRINTED_PRIMARY_AMBIGUOUS",
        "ARABIC_PRINTED_FALLBACK_AMBIGUOUS",
    }
)


@dataclass(frozen=True)
class OcrReprocessAssessment:
    document_id: int
    retry_mode: OcrRetryMode
    source_transkribus_run_id: int | None = None


def _get_document(document_id: int) -> Document:
    try:
        return Document.objects.select_related("archive_item").get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise OcrReprocessError(f"Document id={document_id} does not exist.") from exc


def _select_document_ocr_route(doc: Document) -> OcrRouteConfig:
    try:
        return select_ocr_route(
            doc.language,
            doc.text_input_type,
            handwriting_type=doc.handwriting_type,
        )
    except ValueError as exc:
        raise OcrReprocessError(
            f"Document id={doc.id} has no valid OCR route for reprocess."
        ) from exc


def _document_routes_to_gemini(doc: Document) -> bool:
    return (
        _select_document_ocr_route(doc).engine_key
        == DocumentTextResult.OcrEngineKey.GEMINI
    )


def _source_text_row_is_usable(row: DocumentTextResult) -> bool:
    if row.status not in (
        DocumentTextResult.Status.SUCCEEDED,
        DocumentTextResult.Status.NEEDS_REVIEW,
    ):
        return False
    return bool((row.text or "").strip())


def _latest_source_text_row(doc: Document) -> DocumentTextResult | None:
    return (
        DocumentTextResult.objects.filter(
            document_id=doc.id,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        )
        .order_by("-updated_at", "-pk")
        .first()
    )


def _source_text_row_is_failed_ocr(row: DocumentTextResult) -> bool:
    return (
        row.status == DocumentTextResult.Status.FAILED
        and row.error_code == "OCR_FAILED"
    )


def _arabic_printed_page_has_durable_vision_plan(
    page: ArabicPrintedOcrPageCheckpoint,
) -> bool:
    return (
        page.cloud_vision_call_count == 1
        and page.band_count >= 1
        and bool(page.cloud_vision_response_sha256)
        and bool(page.band_checkpoints.all())
    )


def _arabic_printed_page_is_permanently_fenced(
    page: ArabicPrintedOcrPageCheckpoint,
) -> bool:
    """True when reclaim cannot safely create, poll, cancel, or select LQ."""
    if page.failure_code == _ARABIC_PRINTED_VISION_AMBIGUOUS:
        return True
    if (
        page.cloud_vision_call_count != 0
        and not _arabic_printed_page_has_durable_vision_plan(page)
    ):
        return True
    return any(
        band.failure_code in _ARABIC_PRINTED_AMBIGUOUS_BAND_FAILURE_CODES
        for band in page.band_checkpoints.all()
    )


def _arabic_printed_page_has_active_lease(
    page: ArabicPrintedOcrPageCheckpoint,
    *,
    now,
) -> bool:
    return (
        page.status == ArabicPrintedOcrPageCheckpoint.Status.RUNNING
        and page.lease_expires_at is not None
        and page.lease_expires_at > now
    )


def _current_arabic_printed_contract_fingerprints(
    doc: Document,
) -> tuple[str, str, str]:
    """Route/prompt/config fingerprints the banded worker uses for this document.

    Source-byte fingerprints are omitted: staff eligibility must not load S3 or
    prepare images. Worker reuse still keys the full identity, including source.
    Attempts whose route/prompt/config no longer match current code are skipped.
    """
    placeholder = "0" * 64
    identity = build_arabic_printed_attempt_identity(
        pages=[
            ArabicPrintedPageSource(
                page_index=0,
                mime_type="image/jpeg",
                source_identity="staff-reprocess-eligibility",
                source_content_fingerprint=placeholder,
                oriented_image_sha256=placeholder,
                oriented_image_width=1,
                oriented_image_height=1,
            )
        ],
        language_hint=doc.language,
        text_input_type=doc.text_input_type or Document.TextInputType.PRINTED,
        engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    )
    return (
        identity.route_fingerprint,
        identity.prompt_fingerprint,
        identity.config_fingerprint,
    )


def _reusable_arabic_printed_attempt(
    doc: Document,
) -> ArabicPrintedOcrAttempt | None:
    """Latest attempt the current banded contract would still look up.

    Worker selection is get_or_create(document, identity_fingerprint) from
    current pages + current route/prompt/banding constants. Older identities
    (source or contract changes) remain in the table but are not reused.
    When several source identities share the current contract, the latest
    updated attempt is the last worker-touched identity under that contract.
    """
    route_fp, prompt_fp, config_fp = _current_arabic_printed_contract_fingerprints(doc)
    return (
        ArabicPrintedOcrAttempt.objects.filter(
            document_id=doc.id,
            route_fingerprint=route_fp,
            prompt_fingerprint=prompt_fp,
            config_fingerprint=config_fp,
        )
        .order_by("-updated_at", "-pk")
        .first()
    )


def _has_resumable_arabic_printed_checkpoint_evidence(doc: Document) -> bool:
    """True when the worker-reusable attempt has reclaimable unfinished pages."""
    attempt = _reusable_arabic_printed_attempt(doc)
    if attempt is None:
        return False

    pages = list(
        ArabicPrintedOcrPageCheckpoint.objects.filter(attempt_id=attempt.id)
        .prefetch_related("band_checkpoints")
    )
    if not pages:
        return False

    now = timezone.now()
    if any(_arabic_printed_page_has_active_lease(page, now=now) for page in pages):
        return False

    return any(
        page.status != ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED
        and not _arabic_printed_page_is_permanently_fenced(page)
        for page in pages
    )


def _has_failed_gemini_page_checkpoint(document_id: int) -> bool:
    return GeminiOcrPageCheckpoint.objects.filter(
        attempt__document_id=document_id,
        status=GeminiOcrPageCheckpoint.Status.FAILED,
    ).exists()


def _is_recoverable_partial_ocr_failure(doc: Document) -> bool:
    """Latest SOURCE_TEXT is a failed source OCR on a supported recoverable PARTIAL route."""
    if doc.processing_state_user != Document.ProcessingState.PARTIAL:
        return False

    route = _select_document_ocr_route(doc)
    if route.engine_key not in (
        DocumentTextResult.OcrEngineKey.GEMINI,
        DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
    ):
        return False

    latest_source = _latest_source_text_row(doc)
    if latest_source is None:
        # Checkpoint-backed Gemini or Arabic printed banded OCR can stop as
        # PARTIAL before a DocumentTextResult exists. Failed Gemini pages, or
        # unfinished Arabic pages that are not permanently fenced, are durable
        # resume evidence. Do not require the Antigravity routing flag here:
        # staff UI may assess on web (flag unset) while the worker resumes
        # banded checkpoints.
        return _has_failed_gemini_page_checkpoint(
            doc.id
        ) or _has_resumable_arabic_printed_checkpoint_evidence(doc)

    if _source_text_row_is_usable(latest_source):
        return False
    return _source_text_row_is_failed_ocr(latest_source)


def _processing_state_allows_ocr_reprocess(doc: Document) -> bool:
    if doc.processing_state_user == Document.ProcessingState.FAILED:
        return True
    # READY means expected outputs are usable/displayable, not human-verified.
    # Intentional staff reprocess may continue; VERIFIED remains the overwrite guard.
    if doc.processing_state_user == Document.ProcessingState.READY:
        return True
    return _is_recoverable_partial_ocr_failure(doc)


def validate_document_for_ocr_reprocess(doc: Document) -> None:
    if doc.upload_status != Document.UploadStatus.UPLOADED:
        raise OcrReprocessError(
            f"Document id={doc.id} upload_status={doc.upload_status!r} "
            "must be UPLOADED."
        )

    if not _processing_state_allows_ocr_reprocess(doc):
        raise OcrReprocessError(
            f"Document id={doc.id} processing_state_user="
            f"{doc.processing_state_user!r} is not eligible for OCR reprocess."
        )

    if not doc.archive_item_id:
        raise OcrReprocessError(
            f"Document id={doc.id} has no linked ArchiveItem; "
            "only OCR_DOCUMENT archive items are supported."
        )

    try:
        item_type = doc.archive_item.item_type
    except ArchiveItem.DoesNotExist as exc:
        raise OcrReprocessError(
            f"Document id={doc.id} is linked to missing ArchiveItem "
            f"id={doc.archive_item_id}; only OCR_DOCUMENT is supported."
        ) from exc

    if item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise OcrReprocessError(
            f"Document id={doc.id} is linked to ArchiveItem item_type="
            f"{item_type!r}; only OCR_DOCUMENT is supported."
        )

    if DocumentTextResult.objects.filter(
        document_id=doc.id,
        verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
    ).exists():
        raise OcrReprocessError(
            f"Document id={doc.id} has VERIFIED DocumentTextResult row(s); "
            "reprocess is blocked."
        )


def is_ocr_reprocess_ui_eligible(doc: Document) -> bool:
    try:
        validate_document_for_ocr_reprocess(doc)
    except OcrReprocessError:
        return False
    return True


def _transkribus_reprocess_config_present(collection_id: str, model_id: str) -> bool:
    return bool(str(collection_id).strip()) and bool(str(model_id).strip())


def _validate_transkribus_reprocess_config(
    doc: Document,
    *,
    route: OcrRouteConfig,
    collection_id: str,
    model_id: str,
) -> None:
    if route.engine_key != DocumentTextResult.OcrEngineKey.TRANSKRIBUS:
        return
    if _transkribus_reprocess_config_present(collection_id, model_id):
        return
    raise OcrReprocessError(
        f"Document id={doc.id} is Hebrew handwritten and requires "
        "TRANSKRIBUS_COLLECTION_ID and TRANSKRIBUS_MODEL_ID for OCR reprocess "
        "assessment, but one or both are missing in this environment. "
        "Cannot safely classify the Transkribus retry mode without them."
    )


def _has_succeeded_transkribus_upload_run(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
) -> bool:
    col = str(collection_id).strip()
    mid = str(model_id).strip()
    return TranskribusRun.objects.filter(
        document_id=document_id,
        mode=TranskribusRun.Mode.UPLOAD_CREATED,
        collection_id=col,
        model_id=mid,
        status=TranskribusRun.Status.SUCCEEDED,
    ).exists()


def classify_ocr_retry_mode(
    *,
    document_id: int,
    collection_id: str,
    model_id: str,
    route: OcrRouteConfig,
) -> OcrReprocessAssessment:
    if route.engine_key != DocumentTextResult.OcrEngineKey.TRANSKRIBUS:
        return OcrReprocessAssessment(
            document_id=document_id,
            retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
            source_transkribus_run_id=None,
        )

    reusable = trp.find_reusable_upload_run(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    )
    if reusable is not None and not _has_succeeded_transkribus_upload_run(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
    ):
        return OcrReprocessAssessment(
            document_id=document_id,
            retry_mode=OcrRetryMode.TRANSKRIBUS_RECOGNITION_ONLY,
            source_transkribus_run_id=reusable.id,
        )
    return OcrReprocessAssessment(
        document_id=document_id,
        retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
        source_transkribus_run_id=None,
    )


def assess_ocr_reprocess(
    document_id: int,
    *,
    collection_id: str,
    model_id: str,
) -> OcrReprocessAssessment:
    doc = _get_document(document_id)
    validate_document_for_ocr_reprocess(doc)
    route = _select_document_ocr_route(doc)
    _validate_transkribus_reprocess_config(
        doc,
        route=route,
        collection_id=collection_id,
        model_id=model_id,
    )
    return classify_ocr_retry_mode(
        document_id=document_id,
        collection_id=collection_id,
        model_id=model_id,
        route=route,
    )
