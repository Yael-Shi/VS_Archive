"""Explicit activation of a COMPLETED corrected/current Transkribus sync attempt.

Writes snapshot canonical text into an explicit SOURCE_TEXT DocumentTextResult
and creates/repairs TranskribusTextResultBinding rows. Does not perform HTTP,
S3, Gemini, SQS, or provider calls. Does not update Document.processing_state_user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NoReturn

from django.contrib.auth.models import User
from django.db import transaction

from documents.models import (
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.document_access import is_document_admin
from documents.services.transkribus_corrected_current_sync import (
    _SnapshotPageMismatchError,
    _verify_snapshot_matches_attempt_pages,
)
from documents.services.transkribus_snapshot_binding import (
    TranskribusSnapshotBindingError,
    bind_text_result_to_snapshot,
)
from documents.services.transkribus_snapshot_parser import compute_sha256_hex
from documents.services.verified_text_result_edit import find_paired_hebrew_row


class CorrectedCurrentActivationErrorCode:
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"
    ATTEMPT_NOT_FOUND = "ATTEMPT_NOT_FOUND"
    ATTEMPT_DOCUMENT_MISMATCH = "ATTEMPT_DOCUMENT_MISMATCH"
    ATTEMPT_NOT_COMPLETED = "ATTEMPT_NOT_COMPLETED"
    SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
    SNAPSHOT_DOCUMENT_MISMATCH = "SNAPSHOT_DOCUMENT_MISMATCH"
    SNAPSHOT_NOT_READY = "SNAPSHOT_NOT_READY"
    SNAPSHOT_PAGE_MISMATCH = "SNAPSHOT_PAGE_MISMATCH"
    CANONICAL_TEXT_EMPTY = "CANONICAL_TEXT_EMPTY"
    CANONICAL_HASH_MISMATCH = "CANONICAL_HASH_MISMATCH"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_DOCUMENT_MISMATCH = "TARGET_DOCUMENT_MISMATCH"
    TARGET_NOT_SOURCE_TEXT = "TARGET_NOT_SOURCE_TEXT"
    HEBREW_MIRROR_MISSING = "HEBREW_MIRROR_MISSING"
    VERIFIED_BLOCKED = "VERIFIED_BLOCKED"
    HUMAN_EDITED_BLOCKED = "HUMAN_EDITED_BLOCKED"
    STALE_PREVIEW = "STALE_PREVIEW"
    BINDING_FAILED = "BINDING_FAILED"
    ACTOR_UNAUTHORIZED = "ACTOR_UNAUTHORIZED"


_SAFE_MESSAGES: dict[str, str] = {
    CorrectedCurrentActivationErrorCode.DOCUMENT_NOT_FOUND: ("Document was not found."),
    CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_FOUND: (
        "Corrected/current sync attempt was not found."
    ),
    CorrectedCurrentActivationErrorCode.ATTEMPT_DOCUMENT_MISMATCH: (
        "Corrected/current sync attempt does not belong to this document."
    ),
    CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_COMPLETED: (
        "Only a COMPLETED corrected/current sync attempt can be activated."
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_MISSING: (
        "Completed sync attempt is missing a resolved snapshot."
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_DOCUMENT_MISMATCH: (
        "Resolved snapshot does not belong to this document."
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_NOT_READY: (
        "Resolved snapshot is not READY."
    ),
    CorrectedCurrentActivationErrorCode.SNAPSHOT_PAGE_MISMATCH: (
        "Resolved snapshot pages do not match the sync attempt selections."
    ),
    CorrectedCurrentActivationErrorCode.CANONICAL_TEXT_EMPTY: (
        "Snapshot canonical text is empty and cannot be activated."
    ),
    CorrectedCurrentActivationErrorCode.CANONICAL_HASH_MISMATCH: (
        "Snapshot canonical text does not match its stored SHA-256."
    ),
    CorrectedCurrentActivationErrorCode.TARGET_NOT_FOUND: (
        "Source text result was not found."
    ),
    CorrectedCurrentActivationErrorCode.TARGET_DOCUMENT_MISMATCH: (
        "Source text result does not belong to this document."
    ),
    CorrectedCurrentActivationErrorCode.TARGET_NOT_SOURCE_TEXT: (
        "Activation target must be a SOURCE_TEXT result."
    ),
    CorrectedCurrentActivationErrorCode.HEBREW_MIRROR_MISSING: (
        "Hebrew document is missing a paired HEBREW_TEXT result for this engine."
    ),
    CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED: (
        "Cannot activate over a VERIFIED text result."
    ),
    CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED: (
        "Cannot activate over human-edited text without a trustworthy binding."
    ),
    CorrectedCurrentActivationErrorCode.STALE_PREVIEW: (
        "Source text revision or hash no longer matches the activation preview."
    ),
    CorrectedCurrentActivationErrorCode.BINDING_FAILED: (
        "Failed to create or repair the snapshot binding."
    ),
    CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED: (
        "Activation requires an active document-admin user."
    ),
}


class CorrectedCurrentActivationError(Exception):
    """Activation rejected (safe public message + stable code)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        safe = message or _SAFE_MESSAGES.get(
            code, "Corrected/current activation failed."
        )
        super().__init__(safe)


@dataclass(frozen=True)
class CorrectedCurrentActivationResult:
    attempt_id: int
    snapshot_id: int
    source_result_id: int
    hebrew_result_id: int | None
    engine: str
    bound_source_revision: int
    outcome: Literal["APPLIED", "ALREADY_ACTIVE"]
    # True only when SOURCE_TEXT bytes changed (revision bump + SOURCE edit row).
    source_text_changed: bool
    # True when Hebrew mirror text and/or based_on_source_revision was updated.
    hebrew_mirror_updated: bool


def _is_hebrew_document(doc: Document) -> bool:
    return doc.language == Document.Language.HEBREW


def _is_verified(row: DocumentTextResult) -> bool:
    return row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED


def _raise(code: str) -> NoReturn:
    raise CorrectedCurrentActivationError(code)


def _require_activation_actor(activated_by: object | None) -> User:
    """Require an active document-admin persisted User (same policy as staff pages)."""
    if activated_by is None:
        _raise(CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED)
    if not isinstance(activated_by, User):
        _raise(CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED)
    if not bool(activated_by.is_active):
        _raise(CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED)
    if not is_document_admin(activated_by):
        _raise(CorrectedCurrentActivationErrorCode.ACTOR_UNAUTHORIZED)
    return activated_by


def _binding_for_row(
    row: DocumentTextResult,
) -> TranskribusTextResultBinding | None:
    return (
        TranskribusTextResultBinding.objects.filter(text_result_id=row.pk)
        .select_related("snapshot", "text_result")
        .first()
    )


def _snapshot_has_verified_canonical_integrity(
    snapshot: TranskribusTranscriptSnapshot,
) -> bool:
    """READY snapshot with non-empty stored SHA matching sha256(canonical_text)."""
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        return False
    stored_sha = (snapshot.canonical_text_sha256 or "").strip()
    if not stored_sha:
        return False
    return compute_sha256_hex(snapshot.canonical_text or "") == stored_sha


def _binding_has_valid_original_metadata(
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
    if not _snapshot_has_verified_canonical_integrity(snapshot):
        return False

    verified_canonical_sha = (snapshot.canonical_text_sha256 or "").strip()
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if not bound_sha or bound_sha != verified_canonical_sha:
        return False
    return True


def _binding_matches_current_baseline(
    row: DocumentTextResult,
    binding: TranskribusTextResultBinding,
) -> bool:
    """True when binding metadata matches the row's current text/revision."""
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if compute_sha256_hex(row.text or "") != bound_sha:
        return False
    bound_rev = int(binding.bound_source_revision)
    if row.result_type == DocumentTextResult.ResultType.SOURCE_TEXT:
        return int(row.source_revision) == bound_rev
    if row.result_type == DocumentTextResult.ResultType.HEBREW_TEXT:
        return int(row.based_on_source_revision or 0) == bound_rev
    return False


def _binding_indicates_human_drift(
    row: DocumentTextResult,
    *,
    expected_role: str,
) -> bool:
    """True when a trustworthy original binding exists but text/revision drifted."""
    binding = _binding_for_row(row)
    if binding is None:
        return False
    if not _binding_has_valid_original_metadata(
        binding,
        expected_role=expected_role,
        text_result=row,
    ):
        return False
    return not _binding_matches_current_baseline(row, binding)


def _source_has_human_edit_history(source_row: DocumentTextResult) -> bool:
    """True when staff/activation DocumentTextResultEdit rows exist for SOURCE."""
    return DocumentTextResultEdit.objects.filter(text_result_id=source_row.pk).exists()


def _source_has_proven_current_baseline(source_row: DocumentTextResult) -> bool:
    """Trustworthy SOURCE binding that matches current text/revision.

    Proves the current baseline is binding-trustworthy (any READY snapshot with
    verified canonical integrity). Used to allow activation-created edit history
    + fresh binding while blocking unbound or untrustworthy-binding human edits.
    """
    binding = _binding_for_row(source_row)
    if binding is None:
        return False
    if not _binding_has_valid_original_metadata(
        binding,
        expected_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        text_result=source_row,
    ):
        return False
    return _binding_matches_current_baseline(source_row, binding)


def _human_edit_history_blocks_activation(source_row: DocumentTextResult) -> bool:
    """Block when SOURCE has edit history without a trustworthy current binding."""
    if not _source_has_human_edit_history(source_row):
        return False
    return not _source_has_proven_current_baseline(source_row)


def _source_binding_is_fresh_for_snapshot(
    row: DocumentTextResult,
    *,
    snapshot: TranskribusTranscriptSnapshot,
) -> bool:
    binding = _binding_for_row(row)
    if binding is None:
        return False
    if binding.snapshot_id != snapshot.pk:
        return False
    if binding.binding_role != TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE:
        return False
    # Attempt-resolved snapshot integrity is verified earlier; still require
    # binding hash/revision alignment with that snapshot and current row.
    canonical_sha = (snapshot.canonical_text_sha256 or "").strip()
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if not canonical_sha or bound_sha != canonical_sha:
        return False
    if compute_sha256_hex(row.text or "") != bound_sha:
        return False
    if int(binding.bound_source_revision) != int(row.source_revision):
        return False
    return True


def _hebrew_binding_is_fresh_for_snapshot(
    row: DocumentTextResult,
    *,
    snapshot: TranskribusTranscriptSnapshot,
    bound_source_revision: int,
) -> bool:
    binding = _binding_for_row(row)
    if binding is None:
        return False
    if binding.snapshot_id != snapshot.pk:
        return False
    if binding.binding_role != TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR:
        return False
    canonical_sha = (snapshot.canonical_text_sha256 or "").strip()
    bound_sha = (binding.bound_text_sha256 or "").strip()
    if not canonical_sha or bound_sha != canonical_sha:
        return False
    if compute_sha256_hex(row.text or "") != bound_sha:
        return False
    if int(binding.bound_source_revision) != int(bound_source_revision):
        return False
    if int(row.based_on_source_revision or 0) != int(bound_source_revision):
        return False
    return True


def _lock_bindings_for_rows(row_ids: list[int]) -> None:
    binding_ids = list(
        TranskribusTextResultBinding.objects.filter(text_result_id__in=row_ids)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    if not binding_ids:
        return
    list(
        TranskribusTextResultBinding.objects.select_for_update()
        .filter(pk__in=binding_ids)
        .order_by("pk")
    )


def _bind_source_and_optional_hebrew(
    *,
    source_row: DocumentTextResult,
    hebrew_row: DocumentTextResult | None,
    snapshot: TranskribusTranscriptSnapshot,
    bound_source_revision: int,
    activated_by: User,
) -> None:
    try:
        bind_text_result_to_snapshot(
            text_result=source_row,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_source_revision=bound_source_revision,
            bound_by=activated_by,
        )
        if hebrew_row is not None:
            bind_text_result_to_snapshot(
                text_result=hebrew_row,
                snapshot=snapshot,
                binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
                bound_source_revision=bound_source_revision,
                bound_by=activated_by,
            )
    except TranskribusSnapshotBindingError:
        # Local validation only; do not chain messages that could leak details.
        raise CorrectedCurrentActivationError(
            CorrectedCurrentActivationErrorCode.BINDING_FAILED
        ) from None


def _apply_source_text_change(
    *,
    doc: Document,
    source_row: DocumentTextResult,
    hebrew_row: DocumentTextResult | None,
    canonical_text: str,
    activated_by: User,
) -> int:
    """Persist SOURCE canonical text + revision/audit. Returns new source_revision.

    Caller must only invoke this when SOURCE bytes differ from canonical.
    For Hebrew docs, also mirrors HEBREW to the same text/revision.
    """
    old_text = source_row.text or ""
    new_revision = int(source_row.source_revision) + 1

    source_row.text = canonical_text
    source_row.source_revision = new_revision
    source_row.save(update_fields=["text", "source_revision", "updated_at"])

    if _is_hebrew_document(doc):
        assert hebrew_row is not None
        hebrew_row.text = canonical_text
        hebrew_row.based_on_source_revision = new_revision
        hebrew_row.save(
            update_fields=["text", "based_on_source_revision", "updated_at"]
        )

    DocumentTextResultEdit.objects.create(
        text_result=source_row,
        editor=activated_by,
        old_text=old_text,
        new_text=canonical_text,
        edit_type=DocumentTextResultEdit.EditType.SOURCE_TEXT,
    )
    return new_revision


def _repair_hebrew_mirror_only(
    *,
    hebrew_row: DocumentTextResult,
    canonical_text: str,
    source_revision: int,
) -> bool:
    """Align Hebrew mirror to canonical + current SOURCE revision. No SOURCE edit.

    Returns True when Hebrew text and/or based_on_source_revision changed.
    """
    text_needs = (hebrew_row.text or "") != canonical_text
    rev_needs = int(hebrew_row.based_on_source_revision or 0) != int(source_revision)
    if not text_needs and not rev_needs:
        return False

    update_fields: list[str] = ["updated_at"]
    if text_needs:
        hebrew_row.text = canonical_text
        update_fields.append("text")
    if rev_needs:
        hebrew_row.based_on_source_revision = source_revision
        update_fields.append("based_on_source_revision")
    hebrew_row.save(update_fields=update_fields)
    return True


def activate_corrected_current_sync_attempt(
    *,
    document_id: int,
    attempt_id: int,
    source_text_result_id: int,
    activated_by: object | None,
    expected_source_revision: int,
    expected_source_sha256: str,
) -> CorrectedCurrentActivationResult:
    """Activate an explicit COMPLETED corrected/current attempt into SOURCE_TEXT.

    Caller must pass the exact SOURCE_TEXT row id shown in preview plus the
    revision/hash observed at preview time. Never infers latest attempt or engine.

    ``activated_by`` must be an active document-admin user (``is_document_admin``);
    authorization runs before the ALREADY_ACTIVE fast path.

    Same-request idempotency: if SOURCE (and Hebrew mirror when required) are
    already fresh for this snapshot, returns ALREADY_ACTIVE even when preview
    tokens are the pre-activation values from the original request.
    """
    actor = _require_activation_actor(activated_by)

    with transaction.atomic():
        # Lock order: Document → Attempt → Snapshot → DTR row(s) → bindings.
        try:
            doc = Document.objects.select_for_update().get(pk=document_id)
        except Document.DoesNotExist:
            _raise(CorrectedCurrentActivationErrorCode.DOCUMENT_NOT_FOUND)

        try:
            attempt = (
                TranskribusCorrectedCurrentSyncAttempt.objects.select_for_update().get(
                    pk=attempt_id
                )
            )
        except TranskribusCorrectedCurrentSyncAttempt.DoesNotExist:
            _raise(CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_FOUND)

        if attempt.document_id != doc.pk:
            _raise(CorrectedCurrentActivationErrorCode.ATTEMPT_DOCUMENT_MISMATCH)

        if attempt.status != TranskribusCorrectedCurrentSyncAttempt.Status.COMPLETED:
            _raise(CorrectedCurrentActivationErrorCode.ATTEMPT_NOT_COMPLETED)

        if not attempt.resolved_snapshot_id:
            _raise(CorrectedCurrentActivationErrorCode.SNAPSHOT_MISSING)

        try:
            snapshot = TranskribusTranscriptSnapshot.objects.select_for_update().get(
                pk=attempt.resolved_snapshot_id
            )
        except TranskribusTranscriptSnapshot.DoesNotExist:
            _raise(CorrectedCurrentActivationErrorCode.SNAPSHOT_MISSING)

        if snapshot.document_id != doc.pk:
            _raise(CorrectedCurrentActivationErrorCode.SNAPSHOT_DOCUMENT_MISMATCH)

        if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
            _raise(CorrectedCurrentActivationErrorCode.SNAPSHOT_NOT_READY)

        try:
            _verify_snapshot_matches_attempt_pages(attempt, snapshot)
        except _SnapshotPageMismatchError:
            _raise(CorrectedCurrentActivationErrorCode.SNAPSHOT_PAGE_MISMATCH)

        canonical_text = snapshot.canonical_text or ""
        if not canonical_text.strip():
            _raise(CorrectedCurrentActivationErrorCode.CANONICAL_TEXT_EMPTY)

        stored_sha = (snapshot.canonical_text_sha256 or "").strip()
        computed_sha = compute_sha256_hex(canonical_text)
        if not stored_sha or stored_sha != computed_sha:
            _raise(CorrectedCurrentActivationErrorCode.CANONICAL_HASH_MISMATCH)

        try:
            source_probe = DocumentTextResult.objects.get(pk=source_text_result_id)
        except DocumentTextResult.DoesNotExist:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_NOT_FOUND)

        if source_probe.document_id != doc.pk:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_DOCUMENT_MISMATCH)

        if source_probe.result_type != DocumentTextResult.ResultType.SOURCE_TEXT:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_NOT_SOURCE_TEXT)

        is_hebrew = _is_hebrew_document(doc)
        hebrew_probe: DocumentTextResult | None = None
        if is_hebrew:
            hebrew_probe = find_paired_hebrew_row(doc, engine=source_probe.engine)
            if hebrew_probe is None:
                _raise(CorrectedCurrentActivationErrorCode.HEBREW_MIRROR_MISSING)

        dtr_lock_ids = [source_probe.pk]
        if hebrew_probe is not None:
            dtr_lock_ids.append(hebrew_probe.pk)
        dtr_lock_ids = sorted(set(dtr_lock_ids))
        locked_rows = {
            row.pk: row
            for row in DocumentTextResult.objects.select_for_update()
            .filter(pk__in=dtr_lock_ids)
            .order_by("pk")
        }
        if source_probe.pk not in locked_rows:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_NOT_FOUND)
        source_row = locked_rows[source_probe.pk]
        if source_row.result_type != DocumentTextResult.ResultType.SOURCE_TEXT:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_NOT_SOURCE_TEXT)
        if source_row.document_id != doc.pk:
            _raise(CorrectedCurrentActivationErrorCode.TARGET_DOCUMENT_MISMATCH)

        hebrew_row: DocumentTextResult | None = None
        if hebrew_probe is not None:
            if hebrew_probe.pk not in locked_rows:
                _raise(CorrectedCurrentActivationErrorCode.HEBREW_MIRROR_MISSING)
            hebrew_row = locked_rows[hebrew_probe.pk]

        _lock_bindings_for_rows(dtr_lock_ids)

        current_sha = compute_sha256_hex(source_row.text or "")
        source_fresh = _source_binding_is_fresh_for_snapshot(
            source_row, snapshot=snapshot
        )
        hebrew_fresh = True
        if hebrew_row is not None:
            hebrew_fresh = _hebrew_binding_is_fresh_for_snapshot(
                hebrew_row,
                snapshot=snapshot,
                bound_source_revision=int(source_row.source_revision),
            )

        # Idempotent path: ignore stale pre-activation preview tokens.
        if source_fresh and hebrew_fresh and current_sha == computed_sha:
            return CorrectedCurrentActivationResult(
                attempt_id=attempt.pk,
                snapshot_id=snapshot.pk,
                source_result_id=source_row.pk,
                hebrew_result_id=hebrew_row.pk if hebrew_row is not None else None,
                engine=source_row.engine,
                bound_source_revision=int(source_row.source_revision),
                outcome="ALREADY_ACTIVE",
                source_text_changed=False,
                hebrew_mirror_updated=False,
            )

        if (
            int(source_row.source_revision) != int(expected_source_revision)
            or current_sha != (expected_source_sha256 or "").strip()
        ):
            _raise(CorrectedCurrentActivationErrorCode.STALE_PREVIEW)

        if _is_verified(source_row) or (
            hebrew_row is not None and _is_verified(hebrew_row)
        ):
            _raise(CorrectedCurrentActivationErrorCode.VERIFIED_BLOCKED)

        if _binding_indicates_human_drift(
            source_row,
            expected_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        ):
            _raise(CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED)

        if hebrew_row is not None and _binding_indicates_human_drift(
            hebrew_row,
            expected_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
        ):
            _raise(CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED)

        if _human_edit_history_blocks_activation(source_row):
            _raise(CorrectedCurrentActivationErrorCode.HUMAN_EDITED_BLOCKED)

        source_text_changed = (source_row.text or "") != canonical_text
        hebrew_mirror_updated = False
        bound_revision = int(source_row.source_revision)

        if source_text_changed:
            bound_revision = _apply_source_text_change(
                doc=doc,
                source_row=source_row,
                hebrew_row=hebrew_row,
                canonical_text=canonical_text,
                activated_by=actor,
            )
            if hebrew_row is not None:
                hebrew_mirror_updated = True
            source_row = DocumentTextResult.objects.select_for_update().get(
                pk=source_row.pk
            )
            if hebrew_row is not None:
                hebrew_row = DocumentTextResult.objects.select_for_update().get(
                    pk=hebrew_row.pk
                )
        elif hebrew_row is not None:
            hebrew_mirror_updated = _repair_hebrew_mirror_only(
                hebrew_row=hebrew_row,
                canonical_text=canonical_text,
                source_revision=bound_revision,
            )
            if hebrew_mirror_updated:
                hebrew_row = DocumentTextResult.objects.select_for_update().get(
                    pk=hebrew_row.pk
                )

        _bind_source_and_optional_hebrew(
            source_row=source_row,
            hebrew_row=hebrew_row,
            snapshot=snapshot,
            bound_source_revision=bound_revision,
            activated_by=actor,
        )

        if source_text_changed or hebrew_mirror_updated:
            from documents.services.archive_search_index import (
                sync_archive_item_search_index,
            )

            sync_archive_item_search_index(doc.archive_item_id)

        return CorrectedCurrentActivationResult(
            attempt_id=attempt.pk,
            snapshot_id=snapshot.pk,
            source_result_id=source_row.pk,
            hebrew_result_id=hebrew_row.pk if hebrew_row is not None else None,
            engine=source_row.engine,
            bound_source_revision=bound_revision,
            outcome="APPLIED",
            source_text_changed=source_text_changed,
            hebrew_mirror_updated=hebrew_mirror_updated,
        )


__all__ = [
    "CorrectedCurrentActivationError",
    "CorrectedCurrentActivationErrorCode",
    "CorrectedCurrentActivationResult",
    "activate_corrected_current_sync_attempt",
]
