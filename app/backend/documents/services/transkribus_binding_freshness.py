"""Fail-closed trust / freshness checks for TranskribusTextResultBinding.

Read-only: does not create, mutate, or delete bindings or snapshots.

Active geometry/hover association to displayed text must go through a current,
trustworthy ``TranskribusTextResultBinding``. Document ownership of a
``TranskribusTranscriptSnapshot`` alone is never sufficient.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from documents.models import (
    DocumentTextResult,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)


def _sha256_hex(text: str) -> str:
    # Local helper avoids importing snapshot_parser (circular via local_completion).
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BindingFreshnessAssessment:
    """Result of inspecting a DocumentTextResult's Transkribus binding trust."""

    has_binding: bool
    is_structurally_fresh: bool
    is_trusted_for_hover: bool


def expected_binding_role_for_result_type(result_type: str) -> str | None:
    """Return the binding role required for ``result_type``, or None if unsupported."""
    if result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE
    if result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
    return None


def snapshot_has_verified_canonical_integrity(
    snapshot: TranskribusTranscriptSnapshot,
) -> bool:
    """READY snapshot with non-empty stored SHA matching sha256(canonical_text)."""
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        return False
    stored_sha = (snapshot.canonical_text_sha256 or "").strip()
    if not stored_sha:
        return False
    return _sha256_hex(snapshot.canonical_text or "") == stored_sha


def binding_has_valid_original_metadata(
    binding: TranskribusTextResultBinding,
    *,
    expected_role: str,
    text_result: DocumentTextResult,
) -> bool:
    """Trustworthy prior binding provenance (no hover_eligible requirement)."""
    if binding.binding_role != expected_role:
        return False
    if int(binding.bound_source_revision or 0) < 1:
        return False

    snapshot = binding.snapshot
    if snapshot.document_id != text_result.document_id:
        return False
    if not snapshot_has_verified_canonical_integrity(snapshot):
        return False

    verified_canonical_sha = (snapshot.canonical_text_sha256 or "").strip()
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if not bound_sha or bound_sha != verified_canonical_sha:
        return False
    return True


def binding_matches_current_baseline(
    row: DocumentTextResult,
    binding: TranskribusTextResultBinding,
) -> bool:
    """True when binding metadata matches the row's current text/revision."""
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if _sha256_hex(row.text or "") != bound_sha:
        return False
    bound_rev = int(binding.bound_source_revision)
    if row.result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return int(row.source_revision) == bound_rev
    if row.result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return int(row.based_on_source_revision or 0) == bound_rev
    return False


def _resolve_binding_for_row(
    text_result: DocumentTextResult,
    binding: TranskribusTextResultBinding | None,
) -> TranskribusTextResultBinding | None:
    if binding is not None:
        if int(binding.text_result_id) != int(text_result.pk):
            return None
        return binding
    return (
        TranskribusTextResultBinding.objects.filter(text_result_id=text_result.pk)
        .select_related("snapshot", "text_result")
        .first()
    )


def assess_binding_freshness(
    text_result: DocumentTextResult,
    *,
    binding: TranskribusTextResultBinding | None = None,
) -> BindingFreshnessAssessment:
    """Assess structural freshness and hover trust for a text result binding.

    Fail closed: missing binding, unsupported result type, role mismatch,
    cross-document snapshot, non-READY / broken canonical integrity, hash or
    revision drift, or ``bound_source_revision < 1`` → not structurally fresh.
    Hover trust additionally requires ``snapshot.hover_eligible`` is True.
    """
    resolved = _resolve_binding_for_row(text_result, binding)
    has_binding = resolved is not None

    expected_role = expected_binding_role_for_result_type(text_result.result_type)
    if expected_role is None or resolved is None:
        return BindingFreshnessAssessment(
            has_binding=has_binding,
            is_structurally_fresh=False,
            is_trusted_for_hover=False,
        )

    structurally_fresh = binding_has_valid_original_metadata(
        resolved,
        expected_role=expected_role,
        text_result=text_result,
    ) and binding_matches_current_baseline(text_result, resolved)

    trusted_for_hover = structurally_fresh and bool(resolved.snapshot.hover_eligible)
    return BindingFreshnessAssessment(
        has_binding=has_binding,
        is_structurally_fresh=structurally_fresh,
        is_trusted_for_hover=trusted_for_hover,
    )


def is_binding_structurally_fresh(
    text_result: DocumentTextResult,
    *,
    binding: TranskribusTextResultBinding | None = None,
) -> bool:
    """True when the binding is a trustworthy current baseline (hover optional)."""
    return assess_binding_freshness(text_result, binding=binding).is_structurally_fresh


def is_binding_trusted_for_hover(
    text_result: DocumentTextResult,
    *,
    binding: TranskribusTextResultBinding | None = None,
) -> bool:
    """True when the binding is structurally fresh and the snapshot is hover_eligible."""
    return assess_binding_freshness(text_result, binding=binding).is_trusted_for_hover


__all__ = [
    "BindingFreshnessAssessment",
    "assess_binding_freshness",
    "binding_has_valid_original_metadata",
    "binding_matches_current_baseline",
    "expected_binding_role_for_result_type",
    "is_binding_structurally_fresh",
    "is_binding_trusted_for_hover",
    "snapshot_has_verified_canonical_integrity",
]
