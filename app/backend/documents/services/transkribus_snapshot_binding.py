"""Bind DocumentTextResult rows to a READY Transkribus transcript snapshot."""

from __future__ import annotations

from documents.models import (
    DocumentTextResult,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex


class TranskribusSnapshotBindingError(ValueError):
    """Binding rejected (non-READY, cross-document, hash mismatch, etc.)."""


def _require_ready_same_document(
    *,
    snapshot: TranskribusTranscriptSnapshot,
    text_result: DocumentTextResult,
) -> None:
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        raise TranskribusSnapshotBindingError(
            f"Cannot bind non-READY snapshot id={snapshot.pk} "
            f"(status={snapshot.storage_status})."
        )
    if snapshot.document_id != text_result.document_id:
        raise TranskribusSnapshotBindingError(
            "Cannot bind snapshot and text result from different documents."
        )


def bind_text_result_to_snapshot(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
    binding_role: str,
    bound_source_revision: int,
) -> TranskribusTextResultBinding:
    """Create or update a binding after READY snapshot + DTR rows exist.

    Caller must hold the local-completion transaction. Does not perform S3/HTTP.
    """
    _require_ready_same_document(snapshot=snapshot, text_result=text_result)

    text = text_result.text or ""
    text_sha = compute_sha256_hex(text)
    canonical_sha = (snapshot.canonical_text_sha256 or "").strip()
    if not canonical_sha:
        raise TranskribusSnapshotBindingError(
            f"READY snapshot id={snapshot.pk} is missing canonical_text_sha256."
        )
    if text_sha != canonical_sha:
        raise TranskribusSnapshotBindingError(
            "Bound text SHA-256 does not match snapshot canonical_text_sha256."
        )
    if bound_source_revision < 1:
        raise TranskribusSnapshotBindingError(
            f"bound_source_revision must be >= 1, got {bound_source_revision}."
        )

    valid_roles = {c.value for c in TranskribusTextResultBinding.BindingRole}
    if binding_role not in valid_roles:
        raise TranskribusSnapshotBindingError(f"Invalid binding_role={binding_role!r}.")

    binding, _created = TranskribusTextResultBinding.objects.update_or_create(
        text_result=text_result,
        defaults={
            "snapshot": snapshot,
            "binding_role": binding_role,
            "bound_text_sha256": text_sha,
            "bound_source_revision": bound_source_revision,
        },
    )
    return binding
