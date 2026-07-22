from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

import requests

from documents.services import transkribus_engine as tr
from documents.services.transkribus_page_xml_geometry import (
    TranskribusPageXmlGeometryError,
    analyze_page_xml_geometry,
    resolve_audit_transkribus_run,
    resolve_page_indices_to_audit,
    _normalize_page_index_map,
    _page_metadata_by_nr,
)

TranscriptClassification = (
    str  # ORIGINAL_HTR | NON_MATCHING_VERSION | INSUFFICIENT_METADATA
)

_SAFE_TRANSCRIPT_SCALAR_KEYS = (
    "tsId",
    "jobId",
    "modelId",
    "status",
    "timestamp",
    "time",
    "created",
    "createdAt",
    "uploadTimestamp",
    "modified",
    "modifiedAt",
    "updated",
    "updatedAt",
    "userId",
    "userName",
    "username",
    "editor",
    "editorId",
    "editorName",
    "creator",
    "creatorId",
    "creatorName",
    "parentId",
    "parentTsId",
    "parent",
    "version",
    "versionNumber",
    "revision",
    "isCurrent",
    "isLatest",
    "current",
    "latest",
    "primary",
    "type",
    "source",
    "label",
)

_TIMESTAMP_FIELD_KEYS = frozenset(
    {
        "timestamp",
        "time",
        "created",
        "createdAt",
        "uploadTimestamp",
        "modified",
        "modifiedAt",
        "updated",
        "updatedAt",
    }
)

_USER_EDITOR_FIELD_KEYS = frozenset(
    {
        "userId",
        "userName",
        "username",
        "editor",
        "editorId",
        "editorName",
        "creator",
        "creatorId",
        "creatorName",
    }
)

_VERSION_INDICATOR_FIELD_KEYS = frozenset(
    {
        "parentId",
        "parentTsId",
        "parent",
        "version",
        "versionNumber",
        "revision",
        "isCurrent",
        "isLatest",
        "current",
        "latest",
        "primary",
    }
)

_CURRENT_FLAG_KEYS = frozenset({"isCurrent", "current", "primary"})
_LATEST_FLAG_KEYS = frozenset({"isLatest", "latest"})

_REDACTED_KEY_FRAGMENTS = (
    "url",
    "token",
    "secret",
    "password",
    "authorization",
    "xml",
    "text",
    "transcript",
    "content",
    "body",
)

_METADATA_VALUE_MAX_LEN = 200
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_LIKE_RE = re.compile(r"^https?://", re.IGNORECASE)
_URL_ANYWHERE_RE = re.compile(r"https?://", re.IGNORECASE)
_XML_LIKE_RE = re.compile(r"^\s*<")
_BEARER_LIKE_RE = re.compile(r"^bearer\s+", re.IGNORECASE)
_CREDENTIAL_LIKE_RE = re.compile(
    r"(api[_-]?key|access[_-]?token|secret|password|authorization)",
    re.IGNORECASE,
)

_ISO8601_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:[Zz]|[+-]\d{2}:?\d{2})?$"
)

# Plausible transcript timestamp window (epoch seconds).
_EPOCH_SECONDS_MIN = 946684800  # 2000-01-01T00:00:00Z
_EPOCH_SECONDS_MAX = 4102444800  # 2100-01-01T00:00:00Z


class TranskribusTranscriptVersionsError(ValueError):
    """Local validation failure for transcript-version audit."""


@dataclass(frozen=True)
class TranscriptVersionMetadata:
    list_position: int
    ts_id: str | None
    job_id: str | None
    model_id: str | None
    status: str | None
    timestamp_fields: dict[str, str | int | float]
    user_editor_fields: dict[str, str | int]
    version_indicator_fields: dict[str, str | int | float | bool]
    other_safe_fields: dict[str, str | int | float | bool]
    observed_edit_signals: tuple[str, ...]
    matches_stored_htr_job_model: bool
    classification: TranscriptClassification
    classification_reasons: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptPageXmlSummary:
    ts_id: str
    page_index: int
    page_nr: int
    image_width: int | None
    image_height: int | None
    text_region_count: int
    text_line_count: int
    lines_with_non_empty_text: int
    lines_with_text_and_valid_coords: int
    lines_with_baseline: int
    lines_with_text_and_valid_baseline: int
    page_namespace: str | None
    content_sha256: str
    sample_line_text: str | None = None
    sample_line_id: str | None = None


@dataclass(frozen=True)
class PageTranscriptVersionAudit:
    page_index: int
    page_nr: int
    provider_page_id: int | None
    transcript_count: int
    list_order_note: str
    stored_recognition_job_id: str
    stored_model_id: str
    original_htr_present: bool
    original_htr_ts_ids: tuple[str, ...]
    transcripts: tuple[TranscriptVersionMetadata, ...]
    non_matching_version_ts_ids: tuple[str, ...]
    insufficient_metadata_ts_ids: tuple[str, ...]
    provider_version_signals: tuple[str, ...]
    ambiguities: tuple[str, ...]


@dataclass(frozen=True)
class DocumentTranscriptVersionAudit:
    document_id: int
    transkribus_run_id: int
    remote_doc_id: str
    mapping_description: str
    page_mapping_reliable: bool
    stored_recognition_job_id: str
    stored_model_id: str
    pages: tuple[PageTranscriptVersionAudit, ...]
    candidate_selection_rules: tuple[str, ...]
    global_ambiguities: tuple[str, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)
    inspected_transcript: TranscriptPageXmlSummary | None = None


def _normalize_scalar(value: Any) -> str | int | float | bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return None


def _key_should_redact(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _REDACTED_KEY_FRAGMENTS)


def _sanitize_string_value(value: str) -> str | None:
    cleaned = _CONTROL_CHAR_RE.sub(" ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return None
    if len(cleaned) > _METADATA_VALUE_MAX_LEN:
        cleaned = cleaned[: _METADATA_VALUE_MAX_LEN - 3] + "..."
    if _URL_LIKE_RE.match(cleaned):
        return None
    if _URL_ANYWHERE_RE.search(cleaned):
        return None
    if _XML_LIKE_RE.match(cleaned):
        return None
    if _BEARER_LIKE_RE.match(cleaned):
        return None
    if _CREDENTIAL_LIKE_RE.search(cleaned):
        return None
    return cleaned


def _sanitize_metadata_value(value: Any) -> str | int | float | bool | None:
    normalized = _normalize_scalar(value)
    if normalized is None:
        return None
    if isinstance(normalized, bool):
        return normalized
    if isinstance(normalized, (int, float)):
        return normalized
    return _sanitize_string_value(str(normalized))


def _safe_string(value: Any) -> str | None:
    sanitized = _sanitize_metadata_value(value)
    if sanitized is None:
        return None
    return str(sanitized)


def _is_truthy_flag(value: Any) -> bool:
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        lowered = value.strip().lower()
        return lowered in {"1", "true", "yes"}
    return False


def _normalize_numeric_epoch_seconds(value: int | float) -> int | None:
    if isinstance(value, float):
        if value != value:
            return None
        numeric = int(value)
    else:
        numeric = value

    if numeric < 0:
        return None

    abs_value = abs(numeric)
    if abs_value >= 10**14:
        seconds = numeric // 1_000_000
    elif abs_value >= 10**11:
        seconds = numeric // 1_000
    else:
        seconds = numeric

    if seconds < _EPOCH_SECONDS_MIN or seconds > _EPOCH_SECONDS_MAX:
        return None
    return seconds


def _parse_timestamp_epoch(value: str | int | float) -> int | None:
    if isinstance(value, (int, float)):
        return _normalize_numeric_epoch_seconds(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return _normalize_numeric_epoch_seconds(int(stripped))
        if _ISO8601_TIMESTAMP_RE.match(stripped):
            iso = stripped.replace("Z", "+00:00").replace("z", "+00:00")
            if len(iso) >= 5 and iso[-5] in {"+", "-"} and iso[-3] != ":":
                iso = iso[:-2] + ":" + iso[-2:]
            try:
                parsed = datetime.fromisoformat(iso)
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = int(parsed.timestamp())
            if seconds < _EPOCH_SECONDS_MIN or seconds > _EPOCH_SECONDS_MAX:
                return None
            return seconds
    return None


def _best_parsed_timestamp(
    timestamp_fields: Mapping[str, str | int | float],
) -> int | None:
    parsed_values = [
        epoch
        for value in timestamp_fields.values()
        if (epoch := _parse_timestamp_epoch(value)) is not None
    ]
    if not parsed_values:
        return None
    return max(parsed_values)


def _collect_observed_edit_signals(
    *,
    user_editor_fields: Mapping[str, Any],
    version_indicator_fields: Mapping[str, Any],
    status: str | None,
    other_safe_fields: Mapping[str, Any],
) -> tuple[str, ...]:
    signals: list[str] = []
    if user_editor_fields:
        keys = ", ".join(sorted(user_editor_fields))
        signals.append(f"user/editor metadata present ({keys})")
    for key in sorted(version_indicator_fields):
        value = version_indicator_fields[key]
        if key in _CURRENT_FLAG_KEYS or key in _LATEST_FLAG_KEYS:
            if _is_truthy_flag(value):
                signals.append(f"{key} explicitly true")
            else:
                signals.append(f"{key} present but not explicitly true ({value!r})")
        else:
            signals.append(f"{key}={value!r}")
    if status and "manual" in status.lower():
        signals.append(f"status suggests manual edit ({status!r})")
    for key in ("type", "source", "label"):
        value = other_safe_fields.get(key)
        if isinstance(value, str) and "manual" in value.lower():
            signals.append(f"{key} suggests manual edit ({value!r})")
    return tuple(signals)


def _extract_safe_transcript_fields(
    raw: Mapping[str, Any],
) -> tuple[
    dict[str, str | int | float],
    dict[str, str | int],
    dict[str, str | int | float | bool],
    dict[str, str | int | float | bool],
]:
    timestamp_fields: dict[str, str | int | float] = {}
    user_editor_fields: dict[str, str | int] = {}
    version_indicator_fields: dict[str, str | int | float | bool] = {}
    other_safe_fields: dict[str, str | int | float | bool] = {}

    for key, value in raw.items():
        if _key_should_redact(key):
            continue
        sanitized = _sanitize_metadata_value(value)
        if sanitized is None:
            continue
        if key in _TIMESTAMP_FIELD_KEYS:
            timestamp_fields[key] = sanitized
        elif key in _USER_EDITOR_FIELD_KEYS:
            if isinstance(sanitized, (str, int)):
                user_editor_fields[key] = sanitized
        elif key in _VERSION_INDICATOR_FIELD_KEYS:
            version_indicator_fields[key] = sanitized
        elif key in _SAFE_TRANSCRIPT_SCALAR_KEYS:
            if isinstance(sanitized, (str, int, float, bool)):
                other_safe_fields[key] = sanitized

    return (
        timestamp_fields,
        user_editor_fields,
        version_indicator_fields,
        other_safe_fields,
    )


def _classify_transcript(
    *,
    job_id: str | None,
    model_id: str | None,
    stored_job_id: str,
    stored_model_id: str,
    timestamp_fields: Mapping[str, str | int | float],
    ts_id: str | None,
) -> tuple[TranscriptClassification, tuple[str, ...]]:
    reasons: list[str] = []
    matches_htr = (
        job_id == stored_job_id
        and model_id == stored_model_id
        and bool(stored_job_id)
        and bool(stored_model_id)
    )
    if matches_htr:
        reasons.append("jobId and modelId match stored UPLOAD_CREATED HTR metadata")
        return "ORIGINAL_HTR", tuple(reasons)

    has_ts_id = bool(ts_id)
    has_job = bool(job_id)
    has_model = bool(model_id)
    has_timestamps = bool(timestamp_fields)

    if not has_ts_id and not has_job and not has_model and not has_timestamps:
        reasons.append("missing tsId, jobId, modelId, and timestamp fields")
        return "INSUFFICIENT_METADATA", tuple(reasons)

    if not has_job or not has_model:
        reasons.append("missing jobId and/or modelId compared to stored HTR metadata")
    elif job_id != stored_job_id or model_id != stored_model_id:
        reasons.append("jobId/modelId differ from stored UPLOAD_CREATED HTR metadata")

    if has_ts_id or has_job or has_model or has_timestamps:
        reasons.append(
            "does not match stored HTR job/model; may be another HTR run, "
            "older version, manual edit, or incomplete metadata"
        )
        return "NON_MATCHING_VERSION", tuple(reasons)

    reasons.append("not enough metadata to classify transcript version relationship")
    return "INSUFFICIENT_METADATA", tuple(reasons)


def build_transcript_version_metadata(
    raw: Mapping[str, Any],
    *,
    list_position: int,
    stored_job_id: str,
    stored_model_id: str,
) -> TranscriptVersionMetadata:
    ts_id = _safe_string(raw.get("tsId"))
    job_id = _safe_string(raw.get("jobId"))
    model_id = _safe_string(raw.get("modelId"))
    status = _safe_string(raw.get("status"))
    (
        timestamp_fields,
        user_editor_fields,
        version_indicator_fields,
        other_safe_fields,
    ) = _extract_safe_transcript_fields(raw)

    matches_htr = (
        job_id == stored_job_id
        and model_id == stored_model_id
        and bool(stored_job_id)
        and bool(stored_model_id)
    )
    classification, reasons = _classify_transcript(
        job_id=job_id,
        model_id=model_id,
        stored_job_id=stored_job_id,
        stored_model_id=stored_model_id,
        timestamp_fields=timestamp_fields,
        ts_id=ts_id,
    )
    observed_edit_signals = _collect_observed_edit_signals(
        user_editor_fields=user_editor_fields,
        version_indicator_fields=version_indicator_fields,
        status=status,
        other_safe_fields=other_safe_fields,
    )

    return TranscriptVersionMetadata(
        list_position=list_position,
        ts_id=ts_id,
        job_id=job_id,
        model_id=model_id,
        status=status,
        timestamp_fields=dict(timestamp_fields),
        user_editor_fields=dict(user_editor_fields),
        version_indicator_fields=dict(version_indicator_fields),
        other_safe_fields=dict(other_safe_fields),
        observed_edit_signals=observed_edit_signals,
        matches_stored_htr_job_model=matches_htr,
        classification=classification,
        classification_reasons=reasons,
    )


def _transcripts_with_comparable_timestamps(
    transcripts: Sequence[TranscriptVersionMetadata],
) -> list[tuple[TranscriptVersionMetadata, int]]:
    comparable: list[tuple[TranscriptVersionMetadata, int]] = []
    for transcript in transcripts:
        parsed = _best_parsed_timestamp(transcript.timestamp_fields)
        if parsed is not None:
            comparable.append((transcript, parsed))
    return comparable


def _collect_provider_version_signals(
    transcripts: Sequence[TranscriptVersionMetadata],
) -> tuple[str, ...]:
    signals: list[str] = []

    current_truthy = [
        t
        for t in transcripts
        if any(
            _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _CURRENT_FLAG_KEYS
            if key in t.version_indicator_fields
        )
    ]
    latest_truthy = [
        t
        for t in transcripts
        if any(
            _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _LATEST_FLAG_KEYS
            if key in t.version_indicator_fields
        )
    ]
    current_present_not_truthy = [
        t
        for t in transcripts
        if any(
            key in t.version_indicator_fields
            and not _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _CURRENT_FLAG_KEYS
        )
    ]
    latest_present_not_truthy = [
        t
        for t in transcripts
        if any(
            key in t.version_indicator_fields
            and not _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _LATEST_FLAG_KEYS
        )
    ]

    if current_truthy:
        ts_ids = ", ".join(t.ts_id or "?" for t in current_truthy)
        signals.append(
            f"{len(current_truthy)} transcript(s) have explicitly truthy "
            f"current/primary flags (tsId: {ts_ids})"
        )
    if latest_truthy:
        ts_ids = ", ".join(t.ts_id or "?" for t in latest_truthy)
        signals.append(
            f"{len(latest_truthy)} transcript(s) have explicitly truthy "
            f"latest flags (tsId: {ts_ids})"
        )
    if current_present_not_truthy:
        ts_ids = ", ".join(t.ts_id or "?" for t in current_present_not_truthy)
        signals.append(
            f"{len(current_present_not_truthy)} transcript(s) expose current/primary "
            f"fields that are present but not explicitly true (tsId: {ts_ids})"
        )
    if latest_present_not_truthy:
        ts_ids = ", ".join(t.ts_id or "?" for t in latest_present_not_truthy)
        signals.append(
            f"{len(latest_present_not_truthy)} transcript(s) expose latest fields "
            f"that are present but not explicitly true (tsId: {ts_ids})"
        )

    status_values = sorted(
        {t.status for t in transcripts if t.status},
        key=lambda s: s or "",
    )
    if status_values:
        signals.append(f"Distinct status values observed: {', '.join(status_values)}")

    comparable = _transcripts_with_comparable_timestamps(transcripts)
    if len(comparable) >= 2:
        newest = max(comparable, key=lambda item: item[1])
        oldest = min(comparable, key=lambda item: item[1])
        if newest[1] > oldest[1]:
            signals.append(
                f"Comparable parsed timestamps suggest newest tsId={newest[0].ts_id or '?'} "
                f"(epoch={newest[1]}) vs oldest tsId={oldest[0].ts_id or '?'} "
                f"(epoch={oldest[1]})"
            )

    ts_id_numeric = []
    for t in transcripts:
        if t.ts_id and t.ts_id.isdigit():
            ts_id_numeric.append((int(t.ts_id), t.ts_id))
    if len(ts_id_numeric) >= 2:
        newest_ts = max(ts_id_numeric, key=lambda pair: pair[0])
        oldest_ts = min(ts_id_numeric, key=lambda pair: pair[0])
        if newest_ts[0] > oldest_ts[0]:
            signals.append(
                f"Highest numeric tsId={newest_ts[1]} vs lowest tsId={oldest_ts[1]}"
            )

    return tuple(signals)


def _page_ambiguities(
    transcripts: Sequence[TranscriptVersionMetadata],
    *,
    original_htr_present: bool,
) -> tuple[str, ...]:
    ambiguities: list[str] = []
    if not original_htr_present:
        ambiguities.append(
            "No transcript matches stored recognition_job_id + model_id on this page"
        )

    non_matching = [
        t for t in transcripts if t.classification == "NON_MATCHING_VERSION" and t.ts_id
    ]
    if len(non_matching) > 1:
        ambiguities.append(
            f"{len(non_matching)} non-matching-version transcripts "
            f"(tsId: {', '.join(t.ts_id or '?' for t in non_matching)}); "
            "multiple possible corrected-version candidates"
        )

    comparable = _transcripts_with_comparable_timestamps(transcripts)
    if len(comparable) >= 2:
        by_time = sorted(comparable, key=lambda item: item[1], reverse=True)
        by_list = sorted(comparable, key=lambda item: item[0].list_position)
        if [item[0].ts_id for item in by_time] != [item[0].ts_id for item in by_list]:
            ambiguities.append(
                "Provider list order does not match descending comparable timestamp order"
            )

    current_marked = [
        t
        for t in transcripts
        if any(
            _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _CURRENT_FLAG_KEYS
            if key in t.version_indicator_fields
        )
    ]
    latest_marked = [
        t
        for t in transcripts
        if any(
            _is_truthy_flag(t.version_indicator_fields.get(key))
            for key in _LATEST_FLAG_KEYS
            if key in t.version_indicator_fields
        )
    ]
    if len(current_marked) > 1:
        ambiguities.append(
            "Multiple transcripts have explicitly truthy current/primary flags"
        )
    if len(latest_marked) > 1:
        ambiguities.append("Multiple transcripts have explicitly truthy latest flags")
    if current_marked and latest_marked:
        current_ids = {t.ts_id for t in current_marked}
        latest_ids = {t.ts_id for t in latest_marked}
        if current_ids != latest_ids:
            ambiguities.append(
                "Explicitly truthy current/primary and latest flags refer to different tsId"
            )

    return tuple(ambiguities)


def audit_page_transcript_versions(
    *,
    page_index: int,
    page_nr: int,
    provider_page_id: int | None,
    raw_transcripts: Sequence[Mapping[str, Any]],
    stored_job_id: str,
    stored_model_id: str,
) -> PageTranscriptVersionAudit:
    transcripts = tuple(
        build_transcript_version_metadata(
            raw,
            list_position=position,
            stored_job_id=stored_job_id,
            stored_model_id=stored_model_id,
        )
        for position, raw in enumerate(raw_transcripts, start=1)
    )
    original_htr_ts_ids = tuple(
        t.ts_id for t in transcripts if t.classification == "ORIGINAL_HTR" and t.ts_id
    )
    non_matching = tuple(
        t.ts_id
        for t in transcripts
        if t.classification == "NON_MATCHING_VERSION" and t.ts_id
    )
    insufficient = tuple(
        t.ts_id
        for t in transcripts
        if t.classification == "INSUFFICIENT_METADATA" and t.ts_id
    )
    original_htr_present = bool(original_htr_ts_ids)

    return PageTranscriptVersionAudit(
        page_index=page_index,
        page_nr=page_nr,
        provider_page_id=provider_page_id,
        transcript_count=len(transcripts),
        list_order_note=(
            "Transcript list_position reflects provider tsList order only; "
            "order is not assumed chronological."
        ),
        stored_recognition_job_id=stored_job_id,
        stored_model_id=stored_model_id,
        original_htr_present=original_htr_present,
        original_htr_ts_ids=original_htr_ts_ids,
        transcripts=transcripts,
        non_matching_version_ts_ids=non_matching,
        insufficient_metadata_ts_ids=insufficient,
        provider_version_signals=_collect_provider_version_signals(transcripts),
        ambiguities=_page_ambiguities(
            transcripts,
            original_htr_present=original_htr_present,
        ),
    )


def _candidate_selection_rules(
    pages: Sequence[PageTranscriptVersionAudit],
    *,
    stored_job_id: str,
    stored_model_id: str,
) -> tuple[str, ...]:
    rules: list[str] = [
        (
            "Current production rule: pick_transcript(job_id=stored recognition_job_id, "
            f"model_id=stored model_id) — stored job={stored_job_id!r}, "
            f"model={stored_model_id!r}."
        ),
        (
            "Within job/model matches, production uses highest timestamp-like field, "
            "then highest numeric tsId (_transcript_newest_rank)."
        ),
        (
            "Possible corrected-version candidate rule (not implemented): choose transcript "
            "with latest comparable parsed timestamp when job/model no longer match."
        ),
        (
            "Possible corrected-version candidate rule (not implemented): choose highest "
            "numeric tsId when comparable timestamps are unavailable."
        ),
        (
            "Possible corrected-version candidate rule (not implemented): prefer transcript "
            "with explicitly truthy current/latest provider flags."
        ),
    ]
    missing_original = [p for p in pages if not p.original_htr_present]
    if missing_original:
        page_labels = ", ".join(f"page_index={p.page_index}" for p in missing_original)
        rules.append(
            f"Observed gap: stored HTR job/model match missing on {page_labels}; "
            "production pick_transcript would fail on those pages."
        )
    multi_non_matching = [p for p in pages if len(p.non_matching_version_ts_ids) > 1]
    if multi_non_matching:
        page_labels = ", ".join(
            f"page_index={p.page_index}" for p in multi_non_matching
        )
        rules.append(
            f"Ambiguity: multiple non-matching-version transcripts on {page_labels}; "
            "no single corrected-version candidate is identifiable from metadata alone."
        )
    return tuple(rules)


def _find_transcript_raw_by_ts_id(
    pages_meta: Sequence[tr.TrpPageMetadata],
    *,
    page_index_map: Mapping[int, int],
    page_indices: Sequence[int],
    transcript_id: str,
) -> tuple[int, int, int | None, Mapping[str, Any]]:
    target = str(transcript_id).strip()
    if not target:
        raise TranskribusTranscriptVersionsError("--transcript-id must be non-empty.")

    matches: list[tuple[int, int, int | None, Mapping[str, Any]]] = []
    for local_page_index in page_indices:
        page_nr = page_index_map[local_page_index]
        pm = _page_metadata_by_nr(pages_meta, page_nr)
        for raw in pm.transcripts:
            ts_id = _safe_string(raw.get("tsId"))
            if ts_id == target:
                matches.append((local_page_index, page_nr, pm.page_id, raw))

    if not matches:
        raise TranskribusTranscriptVersionsError(
            f"No transcript with tsId={target!r} found on selected page(s)."
        )
    if len(matches) > 1:
        pages = ", ".join(f"page_index={m[0]}" for m in matches)
        raise TranskribusTranscriptVersionsError(
            f"Transcript tsId={target!r} appears on multiple selected pages ({pages}); "
            "pass --page-index to disambiguate."
        )
    return matches[0]


def summarize_transcript_page_xml(
    page_xml: bytes,
    *,
    ts_id: str,
    page_index: int,
    page_nr: int,
    include_sample_text: bool = False,
) -> TranscriptPageXmlSummary:
    geometry = analyze_page_xml_geometry(
        page_xml,
        page_index=page_index,
        page_nr=page_nr,
        transcript_ts_id=ts_id,
        include_sample_text=include_sample_text,
    )
    sample_text = None
    sample_id = None
    if include_sample_text and geometry.sample_line is not None:
        sample_text = _sanitize_string_value(geometry.sample_line.text)
        sample_id = _sanitize_string_value(geometry.sample_line.line_id)

    return TranscriptPageXmlSummary(
        ts_id=ts_id,
        page_index=page_index,
        page_nr=page_nr,
        image_width=geometry.image_width,
        image_height=geometry.image_height,
        text_region_count=geometry.text_region_count,
        text_line_count=geometry.text_line_count,
        lines_with_non_empty_text=geometry.lines_with_non_empty_text,
        lines_with_text_and_valid_coords=geometry.lines_with_text_and_valid_coords,
        lines_with_baseline=geometry.lines_with_baseline,
        lines_with_text_and_valid_baseline=geometry.lines_with_text_and_valid_baseline,
        page_namespace=geometry.page_namespace,
        content_sha256=hashlib.sha256(page_xml).hexdigest(),
        sample_line_text=sample_text,
        sample_line_id=sample_id,
    )


def fetch_document_transcript_version_audit(
    *,
    document_id: int,
    page_index: int | None = None,
    transcript_id: str | None = None,
    include_sample_text: bool = False,
    username: str,
    password: str,
    bearer_token: str = "",
    session_factory: Callable[[], requests.Session] | None = None,
    fetch_xml: Callable[..., bytes] | None = None,
    fetch_pages_metadata: Callable[..., list[tr.TrpPageMetadata]] | None = None,
    login: Callable[..., None] | None = None,
) -> DocumentTranscriptVersionAudit:
    try:
        run = resolve_audit_transkribus_run(document_id)
        page_index_map = _normalize_page_index_map(run.page_index_to_page_nr)
        page_indices = resolve_page_indices_to_audit(
            page_index_map, page_index=page_index
        )
    except TranskribusPageXmlGeometryError as exc:
        raise TranskribusTranscriptVersionsError(str(exc)) from exc

    stored_job_id = str(run.recognition_job_id).strip()
    stored_model_id = str(run.model_id).strip()

    session_factory = session_factory or requests.Session
    fetch_xml_fn = fetch_xml or tr.fetch_transcript_xml
    fetch_pages_fn = fetch_pages_metadata or tr.fetch_pages_metadata
    login_fn = login or tr.login_trp_server

    pages: list[PageTranscriptVersionAudit] = []
    inspected: TranscriptPageXmlSummary | None = None

    with session_factory() as session:
        login_fn(session, username=username, password=password)
        pages_meta = fetch_pages_fn(
            session,
            collection_id=run.collection_id,
            document_id=str(run.remote_doc_id).strip(),
            pages_query=str(run.pages_query).strip(),
        )
        if not pages_meta:
            raise tr.TranskribusPermanentError(
                "Transkribus pages metadata returned empty list"
            )

        for local_page_index in page_indices:
            page_nr = page_index_map[local_page_index]
            pm = _page_metadata_by_nr(pages_meta, page_nr)
            pages.append(
                audit_page_transcript_versions(
                    page_index=local_page_index,
                    page_nr=page_nr,
                    provider_page_id=pm.page_id,
                    raw_transcripts=pm.transcripts,
                    stored_job_id=stored_job_id,
                    stored_model_id=stored_model_id,
                )
            )

        if transcript_id is not None:
            token = bearer_token.strip()
            if not token:
                raise TranskribusTranscriptVersionsError(
                    "Missing Transkribus transcript bearer token for PAGE XML inspection. "
                    "Set TRANSKRIBUS_API_TOKEN."
                )
            local_idx, page_nr, _provider_page_id, raw = _find_transcript_raw_by_ts_id(
                pages_meta,
                page_index_map=page_index_map,
                page_indices=page_indices,
                transcript_id=transcript_id,
            )
            url = raw.get("url")
            if not url or not isinstance(url, str):
                raise tr.TranskribusPermanentError(
                    f"Transcript URL missing for tsId={transcript_id}"
                )
            xml_bytes = fetch_xml_fn(url, bearer_token=token)
            inspected = summarize_transcript_page_xml(
                xml_bytes,
                ts_id=str(transcript_id).strip(),
                page_index=local_idx,
                page_nr=page_nr,
                include_sample_text=include_sample_text,
            )

    global_ambiguities: list[str] = []
    for page in pages:
        for item in page.ambiguities:
            global_ambiguities.append(f"page_index={page.page_index}: {item}")

    warnings = (
        "Read-only audit for one document; does not change production transcript selection.",
        "Classifications summarize observed metadata only; no corrected-version rule is applied.",
        "NON_MATCHING_VERSION does not imply manual editing; edit-related signals are reported separately.",
        "Transcript list order from the provider is reported but not treated as chronological.",
        "URLs, credentials, tokens, raw XML, and full transcription text are intentionally omitted.",
    )

    return DocumentTranscriptVersionAudit(
        document_id=document_id,
        transkribus_run_id=run.id,
        remote_doc_id=str(run.remote_doc_id).strip(),
        mapping_description="trusted upload-created mapping",
        page_mapping_reliable=True,
        stored_recognition_job_id=stored_job_id,
        stored_model_id=stored_model_id,
        pages=tuple(pages),
        candidate_selection_rules=_candidate_selection_rules(
            pages,
            stored_job_id=stored_job_id,
            stored_model_id=stored_model_id,
        ),
        global_ambiguities=tuple(global_ambiguities),
        warnings=warnings,
        inspected_transcript=inspected,
    )


def _transcript_to_dict(meta: TranscriptVersionMetadata) -> dict[str, Any]:
    return {
        "list_position": meta.list_position,
        "ts_id": meta.ts_id,
        "job_id": meta.job_id,
        "model_id": meta.model_id,
        "status": meta.status,
        "timestamp_fields": meta.timestamp_fields,
        "user_editor_fields": meta.user_editor_fields,
        "version_indicator_fields": meta.version_indicator_fields,
        "other_safe_fields": meta.other_safe_fields,
        "observed_edit_signals": list(meta.observed_edit_signals),
        "matches_stored_htr_job_model": meta.matches_stored_htr_job_model,
        "classification": meta.classification,
        "classification_reasons": list(meta.classification_reasons),
    }


def _page_to_dict(page: PageTranscriptVersionAudit) -> dict[str, Any]:
    return {
        "page_index": page.page_index,
        "page_nr": page.page_nr,
        "provider_page_id": page.provider_page_id,
        "transcript_count": page.transcript_count,
        "list_order_note": page.list_order_note,
        "stored_recognition_job_id": page.stored_recognition_job_id,
        "stored_model_id": page.stored_model_id,
        "original_htr_present": page.original_htr_present,
        "original_htr_ts_ids": list(page.original_htr_ts_ids),
        "non_matching_version_ts_ids": list(page.non_matching_version_ts_ids),
        "insufficient_metadata_ts_ids": list(page.insufficient_metadata_ts_ids),
        "provider_version_signals": list(page.provider_version_signals),
        "ambiguities": list(page.ambiguities),
        "transcripts": [_transcript_to_dict(t) for t in page.transcripts],
    }


def audit_to_json_dict(audit: DocumentTranscriptVersionAudit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "document_id": audit.document_id,
        "transkribus_run_id": audit.transkribus_run_id,
        "remote_doc_id": audit.remote_doc_id,
        "mapping_description": audit.mapping_description,
        "page_mapping_reliable": audit.page_mapping_reliable,
        "stored_recognition_job_id": audit.stored_recognition_job_id,
        "stored_model_id": audit.stored_model_id,
        "pages": [_page_to_dict(page) for page in audit.pages],
        "candidate_selection_rules": list(audit.candidate_selection_rules),
        "global_ambiguities": list(audit.global_ambiguities),
        "warnings": list(audit.warnings),
    }
    if audit.inspected_transcript is not None:
        inspected = audit.inspected_transcript
        payload["inspected_transcript"] = {
            "ts_id": inspected.ts_id,
            "page_index": inspected.page_index,
            "page_nr": inspected.page_nr,
            "image_width": inspected.image_width,
            "image_height": inspected.image_height,
            "text_region_count": inspected.text_region_count,
            "text_line_count": inspected.text_line_count,
            "lines_with_non_empty_text": inspected.lines_with_non_empty_text,
            "lines_with_text_and_valid_coords": inspected.lines_with_text_and_valid_coords,
            "lines_with_baseline": inspected.lines_with_baseline,
            "lines_with_text_and_valid_baseline": inspected.lines_with_text_and_valid_baseline,
            "page_namespace": inspected.page_namespace,
            "content_sha256": inspected.content_sha256,
        }
        if inspected.sample_line_text is not None:
            payload["inspected_transcript"]["sample_line"] = {
                "line_id": inspected.sample_line_id,
                "text": inspected.sample_line_text,
            }
    validate_json_payload(payload)
    return payload


def validate_audit_payload(payload: dict[str, Any]) -> None:
    assert_payload_redacted(payload)


def validate_json_payload(payload: dict[str, Any]) -> None:
    validate_audit_payload(payload)


def assert_payload_redacted(payload: dict[str, Any]) -> None:
    serialized = json.dumps(payload).lower()
    page_ns = tr.PAGE_XML_NS.lower()
    if page_ns in serialized:
        serialized = serialized.replace(page_ns, "")
    forbidden_fragments = (
        "https://",
        "http://",
        "bearer ",
        "authorization",
        "jsessionid",
        "<pcgts",
        "<page ",
        "secret transcription",
        "api_key",
        "access_token",
    )
    for fragment in forbidden_fragments:
        if fragment in serialized:
            raise ValueError(
                f"Redaction failure: found forbidden fragment {fragment!r}"
            )
