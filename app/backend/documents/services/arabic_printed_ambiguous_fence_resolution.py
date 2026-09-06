"""Operator-reviewed resolution of Arabic printed ambiguous Vision/create fences.

Inspection and planning are side-effect free. Apply writes only after every
safety precondition is re-evaluated from rows locked in the same transaction.
This module must not call Cloud Vision, Antigravity/Gemini, SQS, OCR, or search.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from documents.models import (
    ArabicPrintedOcrAttempt,
    ArabicPrintedOcrBandCheckpoint,
    ArabicPrintedOcrPageCheckpoint,
    Document,
    DocumentTextResult,
)
from documents.services.antigravity_interaction_id import (
    ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN,
    is_antigravity_interaction_id,
)
from documents.services.arabic_printed_page_checkpoints import (
    ArabicPrintedPageSource,
    build_arabic_printed_attempt_identity,
)

MODE_NO_PROVIDER_CALL = "no-provider-call"
MODE_BIND_INTERACTION = "bind-interaction"
SUPPORTED_MODES = frozenset({MODE_NO_PROVIDER_CALL, MODE_BIND_INTERACTION})

TARGET_VISION = "vision"
TARGET_PRIMARY = "primary"
TARGET_FALLBACK = "fallback"

PAGE_FAILURE_VISION_AMBIGUOUS = "ARABIC_PRINTED_VISION_AMBIGUOUS"
BAND_FAILURE_PRIMARY_AMBIGUOUS = "ARABIC_PRINTED_PRIMARY_AMBIGUOUS"
BAND_FAILURE_FALLBACK_AMBIGUOUS = "ARABIC_PRINTED_FALLBACK_AMBIGUOUS"
BAND_FAILURE_PRIMARY = "ARABIC_PRINTED_PRIMARY_FAILED"
PAGE_FAILURE_OPERATOR_RESOLVED = "ARABIC_PRINTED_OPERATOR_RESOLVED"

AUDIT_MARKER = "operator_ambiguous_fence_resolution"
OPERATOR_AUDIT_SCHEMA = "arabic-printed-operator-fence-resolution-v1"
OPERATOR_AUDIT_MAX_EVENTS = 8
OPERATOR_AUDIT_MAX_JSON_BYTES = 2048
_MAX_REASON_CHARS = 200
_MAX_DIAGNOSTIC_CHARS = 512


class ArabicPrintedAmbiguousFenceResolutionError(RuntimeError):
    """Fail-closed operator fence resolution. No partial apply."""

    def __init__(self, message: str, *, code: str = "PRECONDITION_FAILED"):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class FieldChange:
    model: str
    field: str
    before: object
    after: object


@dataclass(frozen=True)
class FenceResolutionPlan:
    document_id: int
    attempt_id: int
    page_checkpoint_id: int
    page_index: int
    band_checkpoint_id: int | None
    band_index: int | None
    mode: str
    target: str
    expected_failure_code: str
    reason: str
    changes: tuple[FieldChange, ...]
    audit_field: str
    applied: bool = False


@dataclass(frozen=True)
class _ResolutionLoad:
    document: Document
    attempt: ArabicPrintedOcrAttempt
    pages: tuple[ArabicPrintedOcrPageCheckpoint, ...]
    page: ArabicPrintedOcrPageCheckpoint
    bands: tuple[ArabicPrintedOcrBandCheckpoint, ...]
    band: ArabicPrintedOcrBandCheckpoint | None
    mode: str
    reason: str
    expected_failure_code: str
    interaction_id: str | None
    page_index: int
    band_index: int | None
    now: datetime


def plan_arabic_printed_ambiguous_fence_resolution(
    *,
    document_id: int,
    page_index: int,
    mode: str,
    expected_failure_code: str,
    reason: str,
    band_index: int | None = None,
    interaction_id: str | None = None,
    now: datetime | None = None,
) -> FenceResolutionPlan:
    """Inspect current rows (ordinary reads) and return the exact writes."""
    loaded = _load_resolution_context(
        document_id=document_id,
        page_index=page_index,
        mode=mode,
        expected_failure_code=expected_failure_code,
        reason=reason,
        band_index=band_index,
        interaction_id=interaction_id,
        now=now or timezone.now(),
        for_update=False,
    )
    return _build_plan(loaded)


def apply_arabic_printed_ambiguous_fence_resolution(
    *,
    document_id: int,
    page_index: int,
    mode: str,
    expected_failure_code: str,
    reason: str,
    band_index: int | None = None,
    interaction_id: str | None = None,
    now: datetime | None = None,
) -> FenceResolutionPlan:
    """Lock Document → DTRs → attempt → pages → bands, then plan and write."""
    clock = now or timezone.now()
    with transaction.atomic():
        loaded = _load_resolution_context(
            document_id=document_id,
            page_index=page_index,
            mode=mode,
            expected_failure_code=expected_failure_code,
            reason=reason,
            band_index=band_index,
            interaction_id=interaction_id,
            now=clock,
            for_update=True,
        )
        plan = _build_plan(loaded)
        _apply_changes(page=loaded.page, band=loaded.band, plan=plan)
        if plan.target in {TARGET_PRIMARY, TARGET_FALLBACK}:
            _refresh_page_create_count(loaded.page)
        return FenceResolutionPlan(
            document_id=plan.document_id,
            attempt_id=plan.attempt_id,
            page_checkpoint_id=plan.page_checkpoint_id,
            page_index=plan.page_index,
            band_checkpoint_id=plan.band_checkpoint_id,
            band_index=plan.band_index,
            mode=plan.mode,
            target=plan.target,
            expected_failure_code=plan.expected_failure_code,
            reason=plan.reason,
            changes=plan.changes,
            audit_field=plan.audit_field,
            applied=True,
        )


def _load_resolution_context(
    *,
    document_id: int,
    page_index: int,
    mode: str,
    expected_failure_code: str,
    reason: str,
    band_index: int | None,
    interaction_id: str | None,
    now: datetime,
    for_update: bool,
) -> _ResolutionLoad:
    resolved_mode = _require_mode(mode)
    resolved_reason = _require_reason(reason)
    _require_non_negative_index(page_index, label="page_index")
    if band_index is not None:
        _require_non_negative_index(band_index, label="band_index")
    expected_code = (expected_failure_code or "").strip()
    if not expected_code:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "expected_failure_code is required."
        )
    if type(document_id) is not int or document_id < 1:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "document_id must be a positive integer."
        )

    document_qs = Document.objects.all()
    if for_update:
        document_qs = document_qs.select_for_update()
    try:
        document = document_qs.get(pk=document_id)
    except Document.DoesNotExist as exc:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Document id={document_id} does not exist."
        ) from exc

    text_qs = DocumentTextResult.objects.filter(document_id=document.id).order_by("id")
    if for_update:
        text_qs = text_qs.select_for_update()
    text_results = list(text_qs)
    if any(
        row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED
        for row in text_results
    ):
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Document id={document.id} has VERIFIED DocumentTextResult row(s); "
            "ambiguous fence resolution is blocked."
        )

    route_fp, prompt_fp, config_fp = _current_contract_fingerprints(document)
    attempt_qs = ArabicPrintedOcrAttempt.objects.filter(
        document_id=document.id,
        route_fingerprint=route_fp,
        prompt_fingerprint=prompt_fp,
        config_fingerprint=config_fp,
    ).order_by("id")
    if for_update:
        attempt_qs = attempt_qs.select_for_update()
    matching_attempts = list(attempt_qs)
    if not matching_attempts:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Document id={document.id} has no worker-reusable current-contract "
            "Arabic printed OCR attempt."
        )
    attempt = max(matching_attempts, key=lambda row: (row.updated_at, row.pk))

    page_qs = ArabicPrintedOcrPageCheckpoint.objects.filter(
        attempt_id=attempt.id
    ).order_by("id")
    if for_update:
        page_qs = page_qs.select_for_update()
    pages = tuple(page_qs)
    _reject_live_lease_on_pages(pages, attempt_id=attempt.id, now=now)
    page = next((row for row in pages if row.page_index == page_index), None)
    if page is None:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"No Arabic printed page checkpoint for document_id={document.id} "
            f"page_index={page_index} on the current-contract attempt."
        )
    _reject_succeeded_page(page)

    band_qs = ArabicPrintedOcrBandCheckpoint.objects.filter(
        page_checkpoint_id=page.id
    ).order_by("id")
    if for_update:
        band_qs = band_qs.select_for_update()
    bands = tuple(band_qs)
    band = None
    if band_index is not None:
        band = next((row for row in bands if row.band_index == band_index), None)
        if band is None:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                f"No Arabic printed band checkpoint for page_index={page.page_index} "
                f"band_index={band_index}."
            )

    return _ResolutionLoad(
        document=document,
        attempt=attempt,
        pages=pages,
        page=page,
        bands=bands,
        band=band,
        mode=resolved_mode,
        reason=resolved_reason,
        expected_failure_code=expected_code,
        interaction_id=interaction_id,
        page_index=page_index,
        band_index=band_index,
        now=now,
    )


def _build_plan(loaded: _ResolutionLoad) -> FenceResolutionPlan:
    expected_code = loaded.expected_failure_code
    if expected_code == PAGE_FAILURE_VISION_AMBIGUOUS:
        return _plan_vision(loaded)
    if expected_code == BAND_FAILURE_PRIMARY_AMBIGUOUS:
        return _plan_band(
            loaded,
            target=TARGET_PRIMARY,
            expected_create_count=1,
            expected_running_status=(
                ArabicPrintedOcrBandCheckpoint.Status.PRIMARY_RUNNING
            ),
        )
    if expected_code == BAND_FAILURE_FALLBACK_AMBIGUOUS:
        return _plan_band(
            loaded,
            target=TARGET_FALLBACK,
            expected_create_count=2,
            expected_running_status=(
                ArabicPrintedOcrBandCheckpoint.Status.FALLBACK_RUNNING
            ),
        )
    raise ArabicPrintedAmbiguousFenceResolutionError(
        "expected_failure_code is not an ambiguous Arabic printed fence code."
    )


def _require_mode(mode: str) -> str:
    resolved = (mode or "").strip()
    if resolved not in SUPPORTED_MODES:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "mode must be no-provider-call or bind-interaction."
        )
    return resolved


def _require_reason(reason: str) -> str:
    resolved = " ".join((reason or "").split())
    if not resolved:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "reason is required for operator audit."
        )
    if len(resolved) > _MAX_REASON_CHARS:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"reason must be at most {_MAX_REASON_CHARS} characters."
        )
    return resolved


def _require_non_negative_index(value: int, *, label: str) -> None:
    if type(value) is not int or value < 0:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"{label} must be a non-negative integer."
        )


def _current_contract_fingerprints(document: Document) -> tuple[str, str, str]:
    placeholder = "0" * 64
    identity = build_arabic_printed_attempt_identity(
        pages=[
            ArabicPrintedPageSource(
                page_index=0,
                mime_type="image/jpeg",
                source_identity="operator-fence-resolution",
                source_content_fingerprint=placeholder,
                oriented_image_sha256=placeholder,
                oriented_image_width=1,
                oriented_image_height=1,
            )
        ],
        language_hint=document.language,
        text_input_type=document.text_input_type or Document.TextInputType.PRINTED,
        engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
    )
    return (
        identity.route_fingerprint,
        identity.prompt_fingerprint,
        identity.config_fingerprint,
    )


def _reject_live_lease_on_pages(
    pages: tuple[ArabicPrintedOcrPageCheckpoint, ...],
    *,
    attempt_id: int,
    now: datetime,
) -> None:
    for page in pages:
        if (
            page.status == ArabicPrintedOcrPageCheckpoint.Status.RUNNING
            and page.lease_expires_at is not None
            and page.lease_expires_at > now
        ):
            raise ArabicPrintedAmbiguousFenceResolutionError(
                f"Attempt id={attempt_id} has a live page lease; resolution is blocked."
            )


def _reject_succeeded_page(page: ArabicPrintedOcrPageCheckpoint) -> None:
    if page.status == ArabicPrintedOcrPageCheckpoint.Status.SUCCEEDED:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Page checkpoint id={page.id} is SUCCEEDED and cannot be changed."
        )


def _require_interaction_id(interaction_id: str | None) -> str:
    resolved = interaction_id if isinstance(interaction_id, str) else ""
    if not resolved:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "bind-interaction requires --interaction-id."
        )
    if not is_antigravity_interaction_id(
        resolved, max_length=ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN
    ):
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "interaction_id is not a valid Antigravity interaction id "
            f"(A-Za-z0-9._:- , max {ANTIGRAVITY_INTERACTION_ID_STORED_MAX_LEN} chars)."
        )
    return resolved


def _forbid_interaction_id(interaction_id: str | None) -> None:
    if (interaction_id or "").strip():
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "no-provider-call does not accept an interaction id."
        )


def _plan_vision(loaded: _ResolutionLoad) -> FenceResolutionPlan:
    if loaded.mode != MODE_NO_PROVIDER_CALL:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Vision ambiguous fences support only no-provider-call."
        )
    _forbid_interaction_id(loaded.interaction_id)
    if loaded.band_index is not None:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Vision ambiguous resolution must not include band_index."
        )
    page = loaded.page
    if page.status != ArabicPrintedOcrPageCheckpoint.Status.FAILED:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Vision ambiguous fence requires page status FAILED."
        )
    if page.failure_code != PAGE_FAILURE_VISION_AMBIGUOUS:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Page failure_code does not match ARABIC_PRINTED_VISION_AMBIGUOUS."
        )
    if page.cloud_vision_call_count != 1:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Vision ambiguous fence requires cloud_vision_call_count=1."
        )
    if page.band_count != 0 or page.cloud_vision_response_sha256 or loaded.bands:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Vision no-provider-call requires reserved-without-plan shape "
            "(no band rows, band_count=0, empty Vision response hash). "
            "Partial plans are not deleted."
        )
    audit_after = _append_vision_operator_audit(page, reason=loaded.reason)
    changes = (
        FieldChange(
            "page",
            "cloud_vision_call_count",
            page.cloud_vision_call_count,
            0,
        ),
        FieldChange(
            "page",
            "failure_code",
            page.failure_code,
            PAGE_FAILURE_OPERATOR_RESOLVED,
        ),
        FieldChange(
            "page",
            "operator_resolution_audit",
            page.operator_resolution_audit,
            audit_after,
        ),
    )
    return FenceResolutionPlan(
        document_id=loaded.document.id,
        attempt_id=loaded.attempt.id,
        page_checkpoint_id=page.id,
        page_index=page.page_index,
        band_checkpoint_id=None,
        band_index=None,
        mode=loaded.mode,
        target=TARGET_VISION,
        expected_failure_code=PAGE_FAILURE_VISION_AMBIGUOUS,
        reason=loaded.reason,
        changes=changes,
        audit_field="page.operator_resolution_audit",
    )


def _plan_band(
    loaded: _ResolutionLoad,
    *,
    target: str,
    expected_create_count: int,
    expected_running_status: str,
) -> FenceResolutionPlan:
    if loaded.band_index is None or loaded.band is None:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Band ambiguous resolution requires band_index."
        )
    if not _page_has_durable_vision_plan(loaded.page, loaded.bands):
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Band fence resolution requires a durable Vision plan on the page."
        )
    band = loaded.band
    if band.status == ArabicPrintedOcrBandCheckpoint.Status.SUCCEEDED:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Band checkpoint id={band.id} is SUCCEEDED and cannot be changed."
        )
    if band.status != ArabicPrintedOcrBandCheckpoint.Status.FAILED:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Ambiguous create fence requires band status FAILED."
        )
    if band.failure_code != loaded.expected_failure_code:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Band failure_code does not match {loaded.expected_failure_code}."
        )
    if band.create_call_count != expected_create_count:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            f"Ambiguous {target} fence requires create_call_count="
            f"{expected_create_count}."
        )
    primary_id = band.primary_interaction_id.strip()
    fallback_id = band.fallback_interaction_id.strip()
    if target == TARGET_PRIMARY:
        if primary_id:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                "Conflicting primary_interaction_id is already persisted."
            )
        if fallback_id:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                "Primary resolution is unsafe because fallback_interaction_id is set."
            )
    else:
        if fallback_id:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                "Conflicting fallback_interaction_id is already persisted."
            )
        if not primary_id:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                "Fallback resolution requires a persisted primary_interaction_id."
            )

    if loaded.mode == MODE_NO_PROVIDER_CALL:
        _forbid_interaction_id(loaded.interaction_id)
        return _plan_no_provider_call_band(loaded, target=target)
    bound_id = _require_interaction_id(loaded.interaction_id)
    return _plan_bind_band(
        loaded,
        target=target,
        interaction_id=bound_id,
        running_status=expected_running_status,
    )


def _page_has_durable_vision_plan(
    page: ArabicPrintedOcrPageCheckpoint,
    bands: tuple[ArabicPrintedOcrBandCheckpoint, ...],
) -> bool:
    return (
        page.cloud_vision_call_count == 1
        and page.band_count >= 1
        and bool(page.cloud_vision_response_sha256)
        and bool(bands)
    )


def _plan_no_provider_call_band(
    loaded: _ResolutionLoad,
    *,
    target: str,
) -> FenceResolutionPlan:
    band = loaded.band
    assert band is not None
    audit_field, audit_before, audit_after = _band_audit_change(
        band, target, loaded.reason
    )
    if target == TARGET_PRIMARY:
        changes = (
            FieldChange("band", "create_call_count", band.create_call_count, 0),
            FieldChange(
                "band",
                "status",
                band.status,
                ArabicPrintedOcrBandCheckpoint.Status.PENDING,
            ),
            FieldChange("band", "failure_code", band.failure_code, ""),
            FieldChange("band", "failure_message", band.failure_message, ""),
            FieldChange("band", "completed_at", band.completed_at, None),
            FieldChange("band", audit_field, audit_before, audit_after),
        )
    else:
        changes = (
            FieldChange("band", "create_call_count", band.create_call_count, 1),
            FieldChange(
                "band",
                "failure_code",
                band.failure_code,
                BAND_FAILURE_PRIMARY,
            ),
            FieldChange(
                "band",
                "failure_message",
                band.failure_message,
                f"{AUDIT_MARKER}:{MODE_NO_PROVIDER_CALL}:{loaded.reason}"[:512],
            ),
            FieldChange("band", audit_field, audit_before, audit_after),
        )
    return FenceResolutionPlan(
        document_id=loaded.document.id,
        attempt_id=loaded.attempt.id,
        page_checkpoint_id=loaded.page.id,
        page_index=loaded.page.page_index,
        band_checkpoint_id=band.id,
        band_index=band.band_index,
        mode=MODE_NO_PROVIDER_CALL,
        target=target,
        expected_failure_code=loaded.expected_failure_code,
        reason=loaded.reason,
        changes=changes,
        audit_field=f"band.{audit_field}",
    )


def _plan_bind_band(
    loaded: _ResolutionLoad,
    *,
    target: str,
    interaction_id: str,
    running_status: str,
) -> FenceResolutionPlan:
    band = loaded.band
    assert band is not None
    id_field = (
        "primary_interaction_id"
        if target == TARGET_PRIMARY
        else "fallback_interaction_id"
    )
    current_id = getattr(band, id_field)
    audit_field, audit_before, audit_after = _band_audit_change(
        band, target, loaded.reason
    )
    changes = (
        FieldChange("band", id_field, current_id, interaction_id),
        FieldChange("band", "status", band.status, running_status),
        FieldChange("band", "failure_code", band.failure_code, ""),
        FieldChange("band", "failure_message", band.failure_message, ""),
        FieldChange("band", "completed_at", band.completed_at, None),
        FieldChange("band", audit_field, audit_before, audit_after),
    )
    return FenceResolutionPlan(
        document_id=loaded.document.id,
        attempt_id=loaded.attempt.id,
        page_checkpoint_id=loaded.page.id,
        page_index=loaded.page.page_index,
        band_checkpoint_id=band.id,
        band_index=band.band_index,
        mode=MODE_BIND_INTERACTION,
        target=target,
        expected_failure_code=loaded.expected_failure_code,
        reason=loaded.reason,
        changes=changes,
        audit_field=f"band.{audit_field}",
    )


def _append_vision_operator_audit(
    page: ArabicPrintedOcrPageCheckpoint,
    *,
    reason: str,
) -> dict:
    existing = page.operator_resolution_audit
    if existing in (None, {}):
        payload: dict = {"schema": OPERATOR_AUDIT_SCHEMA, "events": []}
    elif (
        isinstance(existing, dict)
        and existing.get("schema") == OPERATOR_AUDIT_SCHEMA
        and isinstance(existing.get("events"), list)
    ):
        payload = {
            "schema": OPERATOR_AUDIT_SCHEMA,
            "events": list(existing["events"]),
        }
    else:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Page operator_resolution_audit is not the expected v1 contract."
        )
    if len(payload["events"]) >= OPERATOR_AUDIT_MAX_EVENTS:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Page operator_resolution_audit already has the maximum number of events."
        )
    payload["events"].append(
        {
            "mode": MODE_NO_PROVIDER_CALL,
            "target": TARGET_VISION,
            "reason": reason,
            "expected_failure_code": PAGE_FAILURE_VISION_AMBIGUOUS,
            "cloud_vision_call_count_before": page.cloud_vision_call_count,
            "page_index": page.page_index,
        }
    )
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    if len(encoded) > OPERATOR_AUDIT_MAX_JSON_BYTES:
        raise ArabicPrintedAmbiguousFenceResolutionError(
            "Page operator_resolution_audit would exceed the bounded size."
        )
    return payload


def _band_audit_change(
    band: ArabicPrintedOcrBandCheckpoint,
    target: str,
    reason: str,
) -> tuple[str, str, str]:
    field = (
        "primary_safe_diagnostics"
        if target == TARGET_PRIMARY
        else "fallback_safe_diagnostics"
    )
    before = getattr(band, field) or ""
    marker = f"{AUDIT_MARKER}:{reason}"
    if before.strip():
        after = f"{before.rstrip()} | {marker}"
    else:
        after = marker
    return field, before, after[:_MAX_DIAGNOSTIC_CHARS]


def _apply_changes(
    *,
    page: ArabicPrintedOcrPageCheckpoint,
    band: ArabicPrintedOcrBandCheckpoint | None,
    plan: FenceResolutionPlan,
) -> None:
    page_fields: list[str] = []
    band_fields: list[str] = []
    for change in plan.changes:
        if change.model == "page":
            current = getattr(page, change.field)
            if current != change.before:
                raise ArabicPrintedAmbiguousFenceResolutionError(
                    f"Page field {change.field} changed before apply."
                )
            setattr(page, change.field, change.after)
            page_fields.append(change.field)
            continue
        if band is None:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                "Band writes were planned without a band row."
            )
        current = getattr(band, change.field)
        if current != change.before:
            raise ArabicPrintedAmbiguousFenceResolutionError(
                f"Band field {change.field} changed before apply."
            )
        setattr(band, change.field, change.after)
        band_fields.append(change.field)
    if page_fields:
        page.save(update_fields=[*dict.fromkeys(page_fields), "updated_at"])
    if band is not None and band_fields:
        band.save(update_fields=[*dict.fromkeys(band_fields), "updated_at"])


def _refresh_page_create_count(checkpoint: ArabicPrintedOcrPageCheckpoint) -> None:
    total = (
        ArabicPrintedOcrBandCheckpoint.objects.filter(
            page_checkpoint=checkpoint
        ).aggregate(total=Sum("create_call_count"))["total"]
        or 0
    )
    if checkpoint.antigravity_create_count == total:
        return
    checkpoint.antigravity_create_count = total
    checkpoint.save(update_fields=["antigravity_create_count", "updated_at"])
