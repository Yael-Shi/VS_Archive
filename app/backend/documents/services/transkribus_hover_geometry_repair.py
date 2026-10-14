"""Rebind documents 358 and 393 onto parser-v2 hover geometry.

The replacement snapshot is parsed from the PAGE XML already stored for the
original READY snapshot. This service does not fetch Transkribus, rerun OCR,
change transcription bytes, or update an existing READY snapshot.

Dry-run is ``plan_hover_geometry_repair``. ``apply_hover_geometry_repair`` is a
separate call and re-checks the plan under row locks before rebinding.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusParagraphMapping,
    TranskribusSnapshotLine,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.s3 import get_object_bytes
from documents.services.document_access import is_document_admin
from documents.services.s3_orphan_cleanup import uploads_bucket_name
from documents.services.transkribus_binding_freshness import (
    is_binding_structurally_fresh,
)
from documents.services.transkribus_page_xml_types import SnapshotPageInput
from documents.services.transkribus_snapshot_binding import (
    TranskribusSnapshotBindingError,
    bind_text_result_to_snapshot,
)
from documents.services.transkribus_snapshot_parser import (
    PARSER_VERSION_V1,
    PARSER_VERSION_V2,
    ParsedSnapshotDocument,
    compute_sha256_hex,
    parse_document_pages_for_snapshot,
)
from documents.services.transkribus_snapshot_storage import (
    TranskribusSnapshotStorageError,
    store_transkribus_transcript_snapshot,
)
from documents.services.text_presentation import (
    resolve_displayed_transcription_result,
)

logger = logging.getLogger(__name__)

# Production identities confirmed by the hover audit. Document 106 is absent
# on purpose: it is unbound and out of scope.
HOVER_GEOMETRY_REPAIR_TARGETS: dict[int, int] = {
    358: 297,
    393: 307,
}


class HoverGeometryRepairError(ValueError):
    """Fail-closed repair refusal. ``code`` is the stable operator token."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class HoverGeometryRepairPlan:
    """Read-only description of a rebind. Does not include transcription text."""

    document_id: int
    source_snapshot_id: int
    source_parser_version: str
    proposed_parser_version: str
    source_result_id: int
    hebrew_result_id: int
    source_binding_id: int
    hebrew_binding_id: int
    source_revision: int
    based_on_source_revision: int
    bound_source_revision: int
    source_verification_status: str
    hebrew_verification_status: str
    canonical_text_sha256: str
    source_text_sha256: str
    hebrew_text_sha256: str
    canonical_text_chars: int
    geometry_capability: str
    hover_eligible: bool
    normalized_polygon_warnings: tuple[str, ...]
    paragraph_mapping_id: int | None
    outcome: str
    replacement_snapshot_id: int | None
    snapshot_inputs: tuple[SnapshotPageInput, ...]


@dataclass(frozen=True)
class HoverGeometryRepairResult:
    document_id: int
    source_snapshot_id: int
    replacement_snapshot_id: int
    source_result_id: int
    hebrew_result_id: int
    source_binding_id: int
    hebrew_binding_id: int
    source_revision: int
    bound_source_revision: int
    source_verification_status: str
    hebrew_verification_status: str
    outcome: str


def _raise(code: str, message: str) -> None:
    raise HoverGeometryRepairError(code, message)


def _require_target(*, document_id: int, snapshot_id: int) -> None:
    expected = HOVER_GEOMETRY_REPAIR_TARGETS.get(document_id)
    if expected is None:
        _raise(
            "OUT_OF_SCOPE",
            f"Document id={document_id} is not in the hover-geometry repair set.",
        )
    if snapshot_id != expected:
        _raise(
            "SNAPSHOT_NOT_IN_SCOPE",
            f"Document id={document_id} repair requires snapshot id={expected}.",
        )


def _load_original_snapshot(
    *,
    document_id: int,
    snapshot_id: int,
) -> TranskribusTranscriptSnapshot:
    try:
        snapshot = TranskribusTranscriptSnapshot.objects.select_related(
            "document",
            "paragraph_mapping",
        ).get(pk=snapshot_id)
    except TranskribusTranscriptSnapshot.DoesNotExist:
        _raise("SNAPSHOT_NOT_FOUND", f"Snapshot id={snapshot_id} does not exist.")
    if snapshot.document_id != document_id:
        _raise(
            "SNAPSHOT_DOCUMENT_MISMATCH",
            f"Snapshot id={snapshot_id} does not belong to document id={document_id}.",
        )
    if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        _raise(
            "SNAPSHOT_NOT_READY",
            f"Snapshot id={snapshot_id} is {snapshot.storage_status}.",
        )
    if snapshot.parser_version != PARSER_VERSION_V1:
        _raise(
            "SNAPSHOT_PARSER_VERSION",
            f"Snapshot id={snapshot_id} parser_version={snapshot.parser_version!r}.",
        )
    canonical = snapshot.canonical_text or ""
    stored_sha = (snapshot.canonical_text_sha256 or "").strip()
    if not canonical.strip() or compute_sha256_hex(canonical) != stored_sha:
        _raise(
            "CANONICAL_HASH_MISMATCH",
            f"Snapshot id={snapshot_id} canonical text does not match its SHA-256.",
        )
    return snapshot


def _page_inputs_from_stored_xml(
    snapshot: TranskribusTranscriptSnapshot,
) -> tuple[SnapshotPageInput, ...]:
    bucket = uploads_bucket_name()
    if not bucket:
        _raise("PAGE_XML_UNAVAILABLE", "UPLOADS_BUCKET_NAME is not configured.")
    pages = list(snapshot.pages.order_by("page_index"))
    if not pages:
        _raise("PAGE_XML_UNAVAILABLE", f"Snapshot id={snapshot.pk} has no pages.")
    inputs: list[SnapshotPageInput] = []
    for page in pages:
        key = (page.page_xml_s3_key or "").strip()
        expected_sha = (page.page_xml_sha256 or "").strip()
        if not key or not expected_sha:
            _raise(
                "PAGE_XML_UNAVAILABLE",
                f"Snapshot page_index={page.page_index} is missing stored PAGE XML.",
            )
        try:
            body, _content_type = get_object_bytes(bucket, key)
        except Exception as exc:
            logger.info(
                "hover geometry repair PAGE XML read failed document_id=%s "
                "snapshot_id=%s page_index=%s exception_class=%s",
                snapshot.document_id,
                snapshot.pk,
                page.page_index,
                type(exc).__name__,
            )
            _raise(
                "PAGE_XML_UNAVAILABLE",
                f"Stored PAGE XML for page_index={page.page_index} could not be read.",
            )
        actual_sha = compute_sha256_hex(body)
        if actual_sha != expected_sha:
            _raise(
                "PAGE_XML_HASH_MISMATCH",
                f"Stored PAGE XML SHA changed for page_index={page.page_index}.",
            )
        inputs.append(
            SnapshotPageInput(
                page_index=page.page_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                page_xml=body,
                provider_page_id=page.provider_page_id,
                remote_transcript_status=page.remote_transcript_status or None,
            )
        )
    return tuple(inputs)


def _line_identity(
    *,
    page_index: int,
    line: TranskribusSnapshotLine | object,
) -> tuple[object, ...]:
    return (
        page_index,
        int(line.order_index),
        line.provider_line_id or "",
        bool(line.contributes_to_canonical),
        line.text or "",
        int(line.char_start),
        int(line.char_end),
    )


def _assert_line_offsets_unchanged(
    snapshot: TranskribusTranscriptSnapshot,
    parsed: ParsedSnapshotDocument,
) -> None:
    stored = [
        _line_identity(page_index=page.page_index, line=line)
        for page in snapshot.pages.order_by("page_index")
        for line in page.lines.order_by("order_index")
    ]
    proposed = [
        (
            page.page_index,
            line.order_index,
            line.provider_line_id or "",
            line.contributes_to_canonical,
            line.text or "",
            line.char_start,
            line.char_end,
        )
        for page in parsed.pages
        for line in page.lines
    ]
    if stored != proposed:
        _raise(
            "LINE_OFFSET_MISMATCH",
            "Parser v2 line order or character offsets differ from the stored snapshot.",
        )


def _bindings_for_snapshot(
    snapshot_id: int,
) -> dict[str, TranskribusTextResultBinding]:
    rows = list(
        TranskribusTextResultBinding.objects.filter(snapshot_id=snapshot_id)
        .select_related("text_result", "snapshot")
        .order_by("pk")
    )
    by_role: dict[str, TranskribusTextResultBinding] = {}
    for binding in rows:
        if binding.binding_role in by_role:
            _raise(
                "BINDING_ROLE_DUPLICATE",
                f"Snapshot id={snapshot_id} has more than one {binding.binding_role}.",
            )
        by_role[binding.binding_role] = binding
    return by_role


def _require_fresh_pair(
    *,
    source_binding: TranskribusTextResultBinding,
    hebrew_binding: TranskribusTextResultBinding,
    canonical_sha: str,
) -> tuple[DocumentTextResult, DocumentTextResult]:
    source = source_binding.text_result
    hebrew = hebrew_binding.text_result
    if source.document_id != hebrew.document_id:
        _raise("TEXT_RESULT_DOCUMENT_MISMATCH", "Bound text results differ by document.")
    if source.result_type != DocumentTextResult.ResultType.SOURCE_TEXT:
        _raise("TARGET_NOT_SOURCE_TEXT", "SNAPSHOT_SOURCE binding is not SOURCE_TEXT.")
    if hebrew.result_type != DocumentTextResult.ResultType.HEBREW_TEXT:
        _raise("TARGET_NOT_HEBREW_TEXT", "HEBREW_MIRROR binding is not HEBREW_TEXT.")
    if not is_binding_structurally_fresh(source, binding=source_binding):
        _raise("SOURCE_BINDING_STALE", "SOURCE_TEXT binding is not structurally fresh.")
    if not is_binding_structurally_fresh(hebrew, binding=hebrew_binding):
        _raise("HEBREW_BINDING_STALE", "HEBREW_TEXT binding is not structurally fresh.")
    source_sha = compute_sha256_hex(source.text or "")
    hebrew_sha = compute_sha256_hex(hebrew.text or "")
    if source_sha != canonical_sha or hebrew_sha != canonical_sha:
        _raise(
            "TEXT_HASH_MISMATCH",
            "Displayed transcription SHA does not match the snapshot canonical SHA.",
        )
    if (source.text or "") != (hebrew.text or ""):
        _raise("TEXT_MIRROR_MISMATCH", "SOURCE_TEXT and HEBREW_TEXT bytes differ.")
    if int(source_binding.bound_source_revision) != int(source.source_revision):
        _raise("REVISION_MISMATCH", "SOURCE binding revision does not match the row.")
    if int(hebrew_binding.bound_source_revision) != int(source.source_revision):
        _raise("REVISION_MISMATCH", "Hebrew binding revision does not match SOURCE.")
    if int(hebrew.based_on_source_revision or 0) != int(source.source_revision):
        _raise(
            "REVISION_MISMATCH",
            "Hebrew based_on_source_revision does not match SOURCE.",
        )
    displayed = resolve_displayed_transcription_result(source.document)
    if displayed is None or displayed.pk != hebrew.pk:
        _raise(
            "DISPLAYED_TEXT_MISMATCH",
            "The Hebrew binding is not the transcription currently displayed.",
        )
    return source, hebrew


def _paragraph_mapping_id(snapshot: TranskribusTranscriptSnapshot) -> int | None:
    try:
        return int(snapshot.paragraph_mapping.pk)
    except ObjectDoesNotExist:
        return None


def _plan_identity(plan: HoverGeometryRepairPlan) -> tuple[object, ...]:
    return (
        plan.document_id,
        plan.source_snapshot_id,
        plan.source_result_id,
        plan.hebrew_result_id,
        plan.source_binding_id,
        plan.hebrew_binding_id,
        plan.source_revision,
        plan.based_on_source_revision,
        plan.bound_source_revision,
        plan.source_verification_status,
        plan.hebrew_verification_status,
        plan.canonical_text_sha256,
        plan.source_text_sha256,
        plan.hebrew_text_sha256,
        plan.geometry_capability,
        plan.hover_eligible,
        plan.normalized_polygon_warnings,
        plan.outcome,
        tuple(compute_sha256_hex(page.page_xml) for page in plan.snapshot_inputs),
    )


def _plan_from_pair(
    *,
    snapshot: TranskribusTranscriptSnapshot,
    parsed: ParsedSnapshotDocument,
    inputs: tuple[SnapshotPageInput, ...],
    source_binding: TranskribusTextResultBinding,
    hebrew_binding: TranskribusTextResultBinding,
    outcome: str,
    replacement_snapshot_id: int | None,
) -> HoverGeometryRepairPlan:
    source, hebrew = _require_fresh_pair(
        source_binding=source_binding,
        hebrew_binding=hebrew_binding,
        canonical_sha=parsed.canonical_text_sha256,
    )
    if not parsed.hover_eligible:
        _raise(
            "REPLACEMENT_NOT_HOVER_ELIGIBLE",
            "Parser v2 did not mark the replacement snapshot hover-eligible.",
        )
    if (
        parsed.geometry_capability
        != TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
    ):
        _raise(
            "REPLACEMENT_GEOMETRY_NOT_VERIFIED",
            f"Parser v2 geometry_capability={parsed.geometry_capability}.",
        )
    warnings = tuple(
        warning
        for page in parsed.pages
        for warning in page.warnings
        if warning.startswith("normalized_polygon ")
    )
    if outcome == "READY_TO_APPLY" and not warnings:
        _raise(
            "NORMALIZATION_NOT_NEEDED",
            "Parser v2 did not clamp any polygon on this snapshot.",
        )
    return HoverGeometryRepairPlan(
        document_id=snapshot.document_id,
        source_snapshot_id=snapshot.pk,
        source_parser_version=snapshot.parser_version,
        proposed_parser_version=parsed.parser_version,
        source_result_id=source.pk,
        hebrew_result_id=hebrew.pk,
        source_binding_id=source_binding.pk,
        hebrew_binding_id=hebrew_binding.pk,
        source_revision=int(source.source_revision),
        based_on_source_revision=int(hebrew.based_on_source_revision or 0),
        bound_source_revision=int(source_binding.bound_source_revision),
        source_verification_status=source.verification_status,
        hebrew_verification_status=hebrew.verification_status,
        canonical_text_sha256=parsed.canonical_text_sha256,
        source_text_sha256=compute_sha256_hex(source.text or ""),
        hebrew_text_sha256=compute_sha256_hex(hebrew.text or ""),
        canonical_text_chars=len(parsed.canonical_text),
        geometry_capability=parsed.geometry_capability,
        hover_eligible=True,
        normalized_polygon_warnings=warnings,
        paragraph_mapping_id=_paragraph_mapping_id(snapshot),
        outcome=outcome,
        replacement_snapshot_id=replacement_snapshot_id,
        snapshot_inputs=inputs,
    )


def plan_hover_geometry_repair(
    *,
    document_id: int,
    snapshot_id: int,
) -> HoverGeometryRepairPlan:
    """Read S3 and the database. Does not write."""
    _require_target(document_id=document_id, snapshot_id=snapshot_id)
    snapshot = _load_original_snapshot(
        document_id=document_id,
        snapshot_id=snapshot_id,
    )
    inputs = _page_inputs_from_stored_xml(snapshot)
    parsed = parse_document_pages_for_snapshot(
        inputs,
        parser_version=PARSER_VERSION_V2,
    )
    if parsed.parser_version != PARSER_VERSION_V2:
        _raise("PARSER_VERSION", "Replacement parse did not use parser v2.")
    if parsed.canonical_text != (snapshot.canonical_text or ""):
        _raise(
            "CANONICAL_TEXT_CHANGED",
            "Parser v2 canonical text differs from the stored snapshot.",
        )
    if parsed.canonical_text_sha256 != (snapshot.canonical_text_sha256 or ""):
        _raise(
            "CANONICAL_HASH_MISMATCH",
            "Parser v2 canonical SHA differs from the stored snapshot.",
        )
    _assert_line_offsets_unchanged(snapshot, parsed)

    original_bindings = _bindings_for_snapshot(snapshot.pk)
    source_role = TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE
    hebrew_role = TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
    if source_role in original_bindings and hebrew_role in original_bindings:
        if _paragraph_mapping_id(snapshot) is not None:
            _raise(
                "PARAGRAPH_MAPPING_PRESENT",
                "The original snapshot has a paragraph mapping.",
            )
        return _plan_from_pair(
            snapshot=snapshot,
            parsed=parsed,
            inputs=inputs,
            source_binding=original_bindings[source_role],
            hebrew_binding=original_bindings[hebrew_role],
            outcome="READY_TO_APPLY",
            replacement_snapshot_id=None,
        )

    replacement = (
        TranskribusTranscriptSnapshot.objects.filter(
            document_id=document_id,
            parser_version=PARSER_VERSION_V2,
            raw_xml_fingerprint=parsed.raw_xml_fingerprint,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
        )
        .order_by("id")
        .first()
    )
    if replacement is None:
        _raise(
            "BINDING_MISSING",
            f"Snapshot id={snapshot.pk} has no SOURCE_TEXT and HEBREW_TEXT bindings.",
        )
    replacement_bindings = _bindings_for_snapshot(replacement.pk)
    if source_role not in replacement_bindings or hebrew_role not in replacement_bindings:
        _raise(
            "BINDING_MISSING",
            f"Snapshot id={snapshot.pk} is not bound, and its v2 replacement is not bound.",
        )
    return _plan_from_pair(
        snapshot=snapshot,
        parsed=parsed,
        inputs=inputs,
        source_binding=replacement_bindings[source_role],
        hebrew_binding=replacement_bindings[hebrew_role],
        outcome="ALREADY_APPLIED",
        replacement_snapshot_id=replacement.pk,
    )


def _require_actor(actor: User | None) -> User:
    if (
        not isinstance(actor, User)
        or not actor.is_active
        or not is_document_admin(actor)
    ):
        _raise(
            "ACTOR_FORBIDDEN",
            "An active document administrator is required to apply the repair.",
        )
    return actor


def _paragraph_mapping_id_locked(snapshot_id: int) -> int | None:
    mapping_id = (
        TranskribusParagraphMapping.objects.filter(snapshot_id=snapshot_id)
        .values_list("pk", flat=True)
        .first()
    )
    if mapping_id is None:
        return None
    return int(mapping_id)


def _recheck_locked_rows(
    plan: HoverGeometryRepairPlan,
    *,
    replacement_snapshot_id: int,
) -> tuple[
    DocumentTextResult,
    DocumentTextResult,
    TranskribusTextResultBinding,
    TranskribusTextResultBinding,
]:
    try:
        document = Document.objects.select_for_update().get(pk=plan.document_id)
    except Document.DoesNotExist:
        _raise("DOCUMENT_NOT_FOUND", f"Document id={plan.document_id} does not exist.")
    try:
        original = TranskribusTranscriptSnapshot.objects.select_for_update().get(
            pk=plan.source_snapshot_id
        )
    except TranskribusTranscriptSnapshot.DoesNotExist:
        _raise(
            "SNAPSHOT_NOT_FOUND",
            f"Snapshot id={plan.source_snapshot_id} does not exist.",
        )
    try:
        replacement = TranskribusTranscriptSnapshot.objects.select_for_update().get(
            pk=replacement_snapshot_id
        )
    except TranskribusTranscriptSnapshot.DoesNotExist:
        _raise(
            "SNAPSHOT_NOT_FOUND",
            f"Snapshot id={replacement_snapshot_id} does not exist.",
        )
    if original.document_id != document.pk or replacement.document_id != document.pk:
        _raise("SNAPSHOT_DOCUMENT_MISMATCH", "Snapshot document changed.")
    if original.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        _raise("SNAPSHOT_NOT_READY", "Original snapshot is no longer READY.")
    if original.parser_version != plan.source_parser_version:
        _raise("SNAPSHOT_PARSER_VERSION", "Original snapshot parser version changed.")
    if (original.canonical_text_sha256 or "") != plan.canonical_text_sha256:
        _raise("CANONICAL_HASH_MISMATCH", "Original snapshot canonical SHA changed.")
    if (
        _paragraph_mapping_id_locked(original.pk) is not None
        or _paragraph_mapping_id_locked(replacement.pk) is not None
    ):
        _raise(
            "PARAGRAPH_MAPPING_PRESENT",
            "A paragraph mapping exists on the original or replacement snapshot.",
        )

    locked_results = {
        row.pk: row
        for row in DocumentTextResult.objects.select_for_update()
        .filter(pk__in=(plan.source_result_id, plan.hebrew_result_id))
        .order_by("pk")
    }
    source = locked_results.get(plan.source_result_id)
    hebrew = locked_results.get(plan.hebrew_result_id)
    if source is None or hebrew is None:
        _raise("TEXT_RESULT_MISSING", "A bound text result disappeared before apply.")
    if source.document_id != document.pk or hebrew.document_id != document.pk:
        _raise("TEXT_RESULT_DOCUMENT_MISMATCH", "A text result moved documents.")
    if source.result_type != DocumentTextResult.ResultType.SOURCE_TEXT:
        _raise("TEXT_RESULT_TYPE_MISMATCH", "SOURCE row is no longer SOURCE_TEXT.")
    if hebrew.result_type != DocumentTextResult.ResultType.HEBREW_TEXT:
        _raise("TEXT_RESULT_TYPE_MISMATCH", "Hebrew row is no longer HEBREW_TEXT.")
    if source.pk != plan.source_result_id or hebrew.pk != plan.hebrew_result_id:
        _raise("TEXT_RESULT_MISSING", "Locked text-result ids do not match the plan.")
    if compute_sha256_hex(source.text or "") != plan.source_text_sha256:
        _raise("TEXT_CHANGED", "SOURCE_TEXT changed after the repair plan.")
    if compute_sha256_hex(hebrew.text or "") != plan.hebrew_text_sha256:
        _raise("TEXT_CHANGED", "HEBREW_TEXT changed after the repair plan.")
    if int(source.source_revision) != plan.source_revision:
        _raise("REVISION_CHANGED", "SOURCE revision changed after the repair plan.")
    if int(hebrew.based_on_source_revision or 0) != plan.based_on_source_revision:
        _raise("REVISION_CHANGED", "Hebrew revision changed after the repair plan.")
    if source.verification_status != plan.source_verification_status:
        _raise(
            "VERIFICATION_CHANGED",
            "SOURCE verification status changed after the repair plan.",
        )
    if hebrew.verification_status != plan.hebrew_verification_status:
        _raise(
            "VERIFICATION_CHANGED",
            "Hebrew verification status changed after the repair plan.",
        )

    bindings = {
        binding.pk: binding
        for binding in TranskribusTextResultBinding.objects.select_for_update()
        .filter(pk__in=(plan.source_binding_id, plan.hebrew_binding_id))
        .select_related("snapshot", "text_result")
        .order_by("pk")
    }
    source_binding = bindings.get(plan.source_binding_id)
    hebrew_binding = bindings.get(plan.hebrew_binding_id)
    if source_binding is None or hebrew_binding is None:
        _raise("BINDING_MISSING", "A text-result binding disappeared before apply.")
    if source_binding.pk != plan.source_binding_id:
        _raise("BINDING_TARGET_CHANGED", "SOURCE binding id does not match the plan.")
    if hebrew_binding.pk != plan.hebrew_binding_id:
        _raise("BINDING_TARGET_CHANGED", "Hebrew binding id does not match the plan.")
    if source_binding.text_result_id != source.pk:
        _raise("BINDING_TARGET_CHANGED", "SOURCE binding no longer matches its row.")
    if hebrew_binding.text_result_id != hebrew.pk:
        _raise("BINDING_TARGET_CHANGED", "Hebrew binding no longer matches its row.")
    source_role = TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE
    hebrew_role = TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR
    if source_binding.binding_role != source_role or hebrew_binding.binding_role != hebrew_role:
        _raise(
            "BINDING_ROLE_MISMATCH",
            "Binding roles are no longer SNAPSHOT_SOURCE and HEBREW_MIRROR.",
        )
    if (
        int(source_binding.bound_source_revision) != plan.bound_source_revision
        or int(hebrew_binding.bound_source_revision) != plan.bound_source_revision
        or int(source_binding.bound_source_revision) != int(source.source_revision)
    ):
        _raise(
            "BINDING_REVISION_CHANGED",
            "bound_source_revision no longer matches the planned SOURCE revision.",
        )
    if (source_binding.bound_text_sha256 or "") != plan.source_text_sha256:
        _raise("BINDING_HASH_MISMATCH", "SOURCE bound_text_sha256 no longer matches.")
    if (hebrew_binding.bound_text_sha256 or "") != plan.hebrew_text_sha256:
        _raise("BINDING_HASH_MISMATCH", "Hebrew bound_text_sha256 no longer matches.")
    if not is_binding_structurally_fresh(source, binding=source_binding):
        _raise("SOURCE_BINDING_STALE", "SOURCE_TEXT binding is no longer structurally fresh.")
    if not is_binding_structurally_fresh(hebrew, binding=hebrew_binding):
        _raise("HEBREW_BINDING_STALE", "HEBREW_TEXT binding is no longer structurally fresh.")
    displayed = resolve_displayed_transcription_result(document)
    if displayed is None or displayed.pk != hebrew.pk:
        _raise(
            "DISPLAYED_TEXT_MISMATCH",
            "The displayed transcription is no longer the planned Hebrew result.",
        )
    return source, hebrew, source_binding, hebrew_binding


def apply_hover_geometry_repair(
    *,
    plan: HoverGeometryRepairPlan,
    actor: User | None,
) -> HoverGeometryRepairResult:
    """Store a v2 snapshot from the plan's PAGE XML, then rebind.

    Snapshot storage runs before the row locks because it uploads to S3.
    A failed recheck leaves that new READY snapshot unused. A retry reuses it
    and binds only when the locked rows still match the plan.
    """
    actor = _require_actor(actor)
    confirmed = plan_hover_geometry_repair(
        document_id=plan.document_id,
        snapshot_id=plan.source_snapshot_id,
    )
    if confirmed.outcome == "ALREADY_APPLIED" and confirmed.replacement_snapshot_id:
        return HoverGeometryRepairResult(
            document_id=confirmed.document_id,
            source_snapshot_id=confirmed.source_snapshot_id,
            replacement_snapshot_id=confirmed.replacement_snapshot_id,
            source_result_id=confirmed.source_result_id,
            hebrew_result_id=confirmed.hebrew_result_id,
            source_binding_id=confirmed.source_binding_id,
            hebrew_binding_id=confirmed.hebrew_binding_id,
            source_revision=confirmed.source_revision,
            bound_source_revision=confirmed.bound_source_revision,
            source_verification_status=confirmed.source_verification_status,
            hebrew_verification_status=confirmed.hebrew_verification_status,
            outcome="ALREADY_APPLIED",
        )
    if _plan_identity(confirmed) != _plan_identity(plan):
        _raise("STALE_PLAN", "The repair plan no longer matches the database.")

    original = TranskribusTranscriptSnapshot.objects.get(pk=plan.source_snapshot_id)
    try:
        storage = store_transkribus_transcript_snapshot(
            document=original.document,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.HOVER_GEOMETRY_REPAIR,
            pages=plan.snapshot_inputs,
            transkribus_run=original.transkribus_run,
            remote_doc_id=original.remote_doc_id,
            collection_id=original.collection_id,
            model_id=original.model_id,
            recognition_job_id=original.recognition_job_id,
            created_by=actor,
            hover_eligible=None,
        )
    except TranskribusSnapshotStorageError as exc:
        raise HoverGeometryRepairError(
            "SNAPSHOT_STORAGE_FAILED",
            "Replacement snapshot storage failed.",
        ) from exc
    replacement = storage.snapshot
    if replacement.pk == original.pk:
        _raise("SNAPSHOT_REUSED_ORIGINAL", "Storage returned the original snapshot.")
    if replacement.parser_version != PARSER_VERSION_V2:
        _raise("PARSER_VERSION", "Stored replacement is not parser v2.")
    if replacement.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
        _raise("SNAPSHOT_NOT_READY", "Replacement snapshot is not READY.")
    if (replacement.canonical_text_sha256 or "") != plan.canonical_text_sha256:
        _raise(
            "CANONICAL_HASH_MISMATCH",
            "Stored replacement canonical SHA does not match the plan.",
        )
    if compute_sha256_hex(replacement.canonical_text or "") != plan.canonical_text_sha256:
        _raise(
            "CANONICAL_HASH_MISMATCH",
            "Stored replacement canonical text does not match its SHA-256.",
        )
    if not replacement.hover_eligible:
        _raise(
            "REPLACEMENT_NOT_HOVER_ELIGIBLE",
            "Stored replacement snapshot is not hover-eligible.",
        )

    with transaction.atomic():
        source, hebrew, source_binding, hebrew_binding = _recheck_locked_rows(
            plan,
            replacement_snapshot_id=replacement.pk,
        )
        if (
            source_binding.snapshot_id == replacement.pk
            and hebrew_binding.snapshot_id == replacement.pk
            and is_binding_structurally_fresh(source, binding=source_binding)
            and is_binding_structurally_fresh(hebrew, binding=hebrew_binding)
        ):
            outcome = "ALREADY_APPLIED"
            source_binding_id = source_binding.pk
            hebrew_binding_id = hebrew_binding.pk
        elif (
            source_binding.snapshot_id != original.pk
            or hebrew_binding.snapshot_id != original.pk
        ):
            _raise(
                "BINDING_TARGET_CHANGED",
                "A binding no longer points at the original snapshot.",
            )
        else:
            try:
                source_binding = bind_text_result_to_snapshot(
                    text_result=source,
                    snapshot=replacement,
                    binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
                    bound_source_revision=int(source.source_revision),
                    bound_by=actor,
                )
                hebrew_binding = bind_text_result_to_snapshot(
                    text_result=hebrew,
                    snapshot=replacement,
                    binding_role=TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
                    bound_source_revision=int(source.source_revision),
                    bound_by=actor,
                )
            except TranskribusSnapshotBindingError:
                _raise("BINDING_FAILED", "Rebinding the replacement snapshot failed.")
            outcome = "APPLIED"
            source_binding_id = source_binding.pk
            hebrew_binding_id = hebrew_binding.pk

        source.refresh_from_db()
        hebrew.refresh_from_db()
        if compute_sha256_hex(source.text or "") != plan.source_text_sha256:
            _raise("TEXT_CHANGED", "SOURCE_TEXT changed during rebind.")
        if int(source.source_revision) != plan.source_revision:
            _raise("REVISION_CHANGED", "SOURCE revision changed during rebind.")
        if source.verification_status != plan.source_verification_status:
            _raise("VERIFICATION_CHANGED", "SOURCE verification changed during rebind.")
        if hebrew.verification_status != plan.hebrew_verification_status:
            _raise("VERIFICATION_CHANGED", "Hebrew verification changed during rebind.")

    logger.info(
        "hover geometry repair %s document_id=%s source_snapshot_id=%s "
        "replacement_snapshot_id=%s source_result_id=%s hebrew_result_id=%s",
        outcome,
        plan.document_id,
        original.pk,
        replacement.pk,
        plan.source_result_id,
        plan.hebrew_result_id,
    )
    return HoverGeometryRepairResult(
        document_id=plan.document_id,
        source_snapshot_id=original.pk,
        replacement_snapshot_id=replacement.pk,
        source_result_id=plan.source_result_id,
        hebrew_result_id=plan.hebrew_result_id,
        source_binding_id=source_binding_id,
        hebrew_binding_id=hebrew_binding_id,
        source_revision=plan.source_revision,
        bound_source_revision=plan.bound_source_revision,
        source_verification_status=plan.source_verification_status,
        hebrew_verification_status=plan.hebrew_verification_status,
        outcome=outcome,
    )
