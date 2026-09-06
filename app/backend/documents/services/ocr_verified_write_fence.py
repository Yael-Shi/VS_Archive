"""Write-time fence against late automated OCR persistence after VERIFIED text.

Enqueue/assessment already blocks a new OCR reprocess when any
``DocumentTextResult`` is ``VERIFIED``. A run that started while rows were
still unverified can still reach persistence after a human verifies. This
helper is the persist-time check for that race.

Caller must invoke it inside the persistence transaction after locking the
``Document`` (or this helper re-locks the document first). It then locks all
text-result rows in ``id`` order so a Document-first verification writer and
OCR persistence cannot commit past each other.

``REJECTED`` is not a fence.

``verified_engine`` is observability only. Callers must not use it to recompute
``Document.processing_state_user``; displayed SOURCE/HEBREW may belong to
different engines, and engine-local rollup can spuriously downgrade READY.
The worker restores the pre-Phase-1 processing state instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from documents.models import Document, DocumentTextResult


@dataclass(frozen=True)
class AutomatedOcrVerifiedWriteFence:
    blocked: bool
    verified_engine: str | None


def inspect_automated_ocr_verified_write_fence(
    document_id: int,
) -> AutomatedOcrVerifiedWriteFence:
    """Return whether automated OCR writes must be skipped for this document.

    Lock order: ``Document``, then ``DocumentTextResult`` rows ordered by ``id``.
    """
    Document.objects.select_for_update().get(pk=document_id)
    rows = list(
        DocumentTextResult.objects.select_for_update()
        .filter(document_id=document_id)
        .order_by("id")
    )
    for row in rows:
        if row.verification_status == DocumentTextResult.VerificationStatus.VERIFIED:
            return AutomatedOcrVerifiedWriteFence(
                blocked=True,
                verified_engine=row.engine,
            )
    return AutomatedOcrVerifiedWriteFence(blocked=False, verified_engine=None)
