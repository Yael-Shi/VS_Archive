"""Server-side validation helpers for VIDEO archive item create/update."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError

from documents.services.archive_discovery_metadata_validation import (
    parse_archive_item_discovery_metadata_form,
)
from documents.services.archive_metadata_validation import (
    parse_archive_metadata_form,
    parse_public_note,
    validate_archive_metadata_fields,
)
from documents.services.video_url import (
    VIDEO_URL_INVALID_ERROR,
    VIDEO_URL_UNSUPPORTED_ERROR,
    parse_video_time_input,
    parse_video_url,
)
from documents.services.video_url_contract import (
    MODE_EMBEDDED,
    MODE_EXTERNAL_LINK,
    PROVIDER_KAN,
    PROVIDER_OTHER,
    PROVIDER_YOUTUBE,
    ParsedVideoUrl,
    is_valid_youtube_video_id,
)

_UNSET = object()

VIDEO_TIME_UNSET = _UNSET

VIDEO_SOURCE_URL_REQUIRED_ERROR = "יש להזין קישור לסרטון"
VIDEO_SOURCE_URL_INVALID_ERROR = "קישור הסרטון אינו תקין"
VIDEO_SOURCE_URL_UNSUPPORTED_ERROR = "קישור הסרטון אינו נתמך"
VIDEO_START_TIME_INVALID_ERROR = "זמן התחלה אינו תקין. הזינו שניות או פורמט כמו 1h2m3s"
VIDEO_END_TIME_INVALID_ERROR = "זמן סיום אינו תקין. הזינו שניות או פורמט כמו 1h2m3s"
VIDEO_TIME_YOUTUBE_ONLY_ERROR = "זמני התחלה וסיום רלוונטיים ליוטיוב בלבד"
VIDEO_END_AFTER_START_ERROR = "זמן הסיום חייב להיות גדול מזמן ההתחלה"

VIDEO_PRESENTATION_EMBEDDED_HINT = "הסרטון יוצג כאן באתר"
VIDEO_PRESENTATION_EXTERNAL_HINT = "הצפייה תיפתח באתר המקורי"

_VIDEO_URL_ERROR_MESSAGES = {
    VIDEO_URL_INVALID_ERROR: VIDEO_SOURCE_URL_INVALID_ERROR,
    VIDEO_URL_UNSUPPORTED_ERROR: VIDEO_SOURCE_URL_UNSUPPORTED_ERROR,
}


def video_presentation_mode_explanation(
    presentation_mode: str | None,
    *,
    provider: str | None = None,
) -> str:
    """Return the Hebrew management-UI explanation for a presentation mode."""
    mode = (presentation_mode or "").strip().upper()
    if mode == MODE_EMBEDDED:
        return VIDEO_PRESENTATION_EMBEDDED_HINT
    if mode == MODE_EXTERNAL_LINK:
        return VIDEO_PRESENTATION_EXTERNAL_HINT
    provider_key = (provider or "").strip().upper()
    if provider_key == PROVIDER_YOUTUBE:
        return VIDEO_PRESENTATION_EMBEDDED_HINT
    if provider_key in {PROVIDER_KAN, PROVIDER_OTHER}:
        return VIDEO_PRESENTATION_EXTERNAL_HINT
    return ""


def video_provider_display_label(provider: str | None) -> str:
    """Short Hebrew/Latin provider label for management UI hints."""
    key = (provider or "").strip().upper()
    if key == PROVIDER_YOUTUBE:
        return "YouTube"
    if key == PROVIDER_KAN:
        return "כאן"
    if key == PROVIDER_OTHER:
        return "אתר חיצוני"
    return ""


def format_video_time_for_form(seconds: int | None) -> str:
    """Format stored seconds for optional time inputs (empty when unset)."""
    if seconds is None:
        return ""
    return str(seconds)


def apply_parsed_video_url(
    *,
    parsed: ParsedVideoUrl,
    start_seconds: Any = _UNSET,
    end_seconds: Any = _UNSET,
) -> dict[str, Any]:
    """Merge URL-derived fields with optional explicit YouTube time overrides."""
    final_start = parsed.start_seconds if start_seconds is _UNSET else start_seconds
    final_end = parsed.end_seconds if end_seconds is _UNSET else end_seconds

    if final_start is not None:
        if not isinstance(final_start, int) or isinstance(final_start, bool):
            raise ValueError("start_seconds must be a non-negative integer")
        if final_start < 0:
            raise ValueError("start_seconds must be a non-negative integer")
    if final_end is not None:
        if not isinstance(final_end, int) or isinstance(final_end, bool):
            raise ValueError("end_seconds must be a positive integer")
        if final_end <= 0:
            raise ValueError("end_seconds must be a positive integer")

    if parsed.provider != PROVIDER_YOUTUBE:
        if final_start is not None or final_end is not None:
            raise ValueError(
                "start_seconds and end_seconds are allowed only for YouTube"
            )
    elif final_end is not None:
        start = 0 if final_start is None else final_start
        if final_end <= start:
            raise ValueError("end_seconds must be greater than start_seconds")

    return {
        "source_url": parsed.source_url,
        "provider": parsed.provider,
        "presentation_mode": parsed.presentation_mode,
        "provider_video_id": parsed.provider_video_id,
        "start_seconds": final_start,
        "end_seconds": final_end,
    }


def parse_video_content_from_source_url(
    source_url: str,
    *,
    start_seconds: Any = _UNSET,
    end_seconds: Any = _UNSET,
) -> dict[str, Any]:
    """Parse ``source_url`` and return VideoContent field values."""
    parsed = parse_video_url(source_url)
    return apply_parsed_video_url(
        parsed=parsed,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def validate_video_content_fields(
    *,
    source_url: str,
    provider: str,
    presentation_mode: str,
    provider_video_id: str,
    start_seconds: int | None,
    end_seconds: int | None,
) -> dict[str, str]:
    """Return field-error map when persisted VIDEO fields are semantically invalid.

    Canonical rules:
    - ``source_url`` must parse successfully and equal the normalized parser output
    - provider / presentation_mode / provider_video_id must match the parse result
    - YouTube IDs must match the approved 11-character contract
    - start/end may differ from URL-derived times but remain YouTube-only + ordered
    """
    errors: dict[str, str] = {}
    try:
        parsed = parse_video_url(source_url)
    except ValueError as exc:
        errors["source_url"] = str(exc)
        return errors

    if source_url != parsed.source_url:
        errors["source_url"] = "source_url must be a valid normalized video URL."

    if provider != parsed.provider:
        errors["provider"] = "provider must match the parsed source_url."
    if presentation_mode != parsed.presentation_mode:
        errors["presentation_mode"] = (
            "presentation_mode must match the parsed source_url."
        )
    if (provider_video_id or "") != parsed.provider_video_id:
        errors["provider_video_id"] = (
            "provider_video_id must match the parsed source_url."
        )

    if provider == PROVIDER_YOUTUBE:
        if presentation_mode != MODE_EMBEDDED:
            errors["presentation_mode"] = (
                "YouTube videos must use presentation_mode=EMBEDDED."
            )
        if not is_valid_youtube_video_id(provider_video_id):
            errors["provider_video_id"] = (
                "YouTube embedded videos require a valid provider_video_id."
            )
    elif provider in {PROVIDER_KAN, PROVIDER_OTHER}:
        if presentation_mode != MODE_EXTERNAL_LINK:
            errors["presentation_mode"] = (
                f"{provider} videos must use presentation_mode=EXTERNAL_LINK."
            )
        if provider_video_id:
            errors["provider_video_id"] = (
                f"{provider} videos must not set provider_video_id."
            )
        if start_seconds is not None or end_seconds is not None:
            errors["start_seconds"] = (
                "start_seconds and end_seconds are allowed only for YouTube."
            )
    else:
        errors["provider"] = "provider is invalid."

    if start_seconds is not None and start_seconds < 0:
        errors["start_seconds"] = "start_seconds must be a non-negative integer."
    if end_seconds is not None and end_seconds <= 0:
        errors["end_seconds"] = "end_seconds must be a positive integer."
    if end_seconds is not None:
        start = 0 if start_seconds is None else start_seconds
        if end_seconds <= start:
            errors["end_seconds"] = "end_seconds must be greater than start_seconds."

    return errors


def validate_video_content_instance(content) -> None:
    """Raise ``ValidationError`` when a VideoContent instance is invalid."""
    from documents.models import ArchiveItem

    errors: dict[str, str] = {}
    if (
        content.archive_item_id
        and content.archive_item.item_type != ArchiveItem.ItemType.VIDEO
    ):
        errors["archive_item"] = (
            "VideoContent requires ArchiveItem with item_type=VIDEO."
        )

    field_errors = validate_video_content_fields(
        source_url=content.source_url or "",
        provider=content.provider or "",
        presentation_mode=content.presentation_mode or "",
        provider_video_id=content.provider_video_id or "",
        start_seconds=content.start_seconds,
        end_seconds=content.end_seconds,
    )
    errors.update(field_errors)
    if errors:
        raise ValidationError(errors)


def validate_video_archive_item_metadata(
    *,
    title: str,
    visibility: str,
    metadata_status: str,
    date_precision: str,
    date_start,
    date_end,
    author_name: str = "",
    source_title: str = "",
    user=None,
) -> None:
    """Raise ``ValueError`` when shared ArchiveItem metadata is invalid."""
    errors = validate_archive_metadata_fields(
        title=title,
        visibility=visibility,
        metadata_status=metadata_status,
        date_precision=date_precision,
        date_start=date_start,
        date_end=date_end,
        author_name=author_name,
        source_title=source_title,
        user=user,
    )
    if errors:
        raise ValueError(errors[0])


def _humanize_video_url_error(exc: Exception) -> str:
    message = str(exc)
    return _VIDEO_URL_ERROR_MESSAGES.get(message, message)


def _parse_optional_time_override(
    raw_value: Any,
    *,
    allow_zero: bool,
    invalid_error: str,
) -> tuple[Any, str | None]:
    """Return ``(override, error)`` for an optional YouTube time form field.

    Blank / missing → ``VIDEO_TIME_UNSET`` so URL-derived times remain.
    """
    if raw_value is _UNSET:
        return _UNSET, None
    text = str(raw_value).strip()
    if not text:
        return _UNSET, None
    try:
        return parse_video_time_input(text, allow_zero=allow_zero), None
    except ValueError:
        return _UNSET, invalid_error


def parse_video_archive_item_form(
    post_data: dict[str, Any],
    *,
    user=None,
) -> tuple[dict[str, Any], list[str]]:
    """Parse VIDEO create/edit POST fields for management UI.

    Provider, presentation mode, and provider video id remain server-derived.
    Optional time fields accept seconds or friendly clock syntax and apply only
    to YouTube; blank times keep URL-derived values.
    """
    parsed, errors = parse_archive_metadata_form(post_data, user=user)
    discovery, discovery_errors = parse_archive_item_discovery_metadata_form(post_data)
    errors.extend(discovery_errors)
    parsed.update(discovery)

    source_url_raw = post_data.get("source_url") or ""
    source_url = str(source_url_raw).strip()
    start_raw = post_data.get("start_seconds", _UNSET)
    end_raw = post_data.get("end_seconds", _UNSET)
    start_display = "" if start_raw is _UNSET else str(start_raw)
    end_display = "" if end_raw is _UNSET else str(end_raw)

    parsed["source_url"] = source_url
    parsed["start_seconds_display"] = start_display
    parsed["end_seconds_display"] = end_display
    parsed["public_note"] = parse_public_note(
        post_data.get("public_note", parsed.get("public_note"))
    )

    start_override, start_error = _parse_optional_time_override(
        start_raw,
        allow_zero=True,
        invalid_error=VIDEO_START_TIME_INVALID_ERROR,
    )
    end_override, end_error = _parse_optional_time_override(
        end_raw,
        allow_zero=False,
        invalid_error=VIDEO_END_TIME_INVALID_ERROR,
    )
    if start_error:
        errors.append(start_error)
    if end_error:
        errors.append(end_error)

    video_fields: dict[str, Any] | None = None
    if not errors:
        if not source_url:
            errors.append(VIDEO_SOURCE_URL_REQUIRED_ERROR)
        else:
            try:
                video_fields = parse_video_content_from_source_url(
                    source_url,
                    start_seconds=start_override,
                    end_seconds=end_override,
                )
            except ValueError as exc:
                message = str(exc)
                if (
                    message
                    == "start_seconds and end_seconds are allowed only for YouTube"
                ):
                    errors.append(VIDEO_TIME_YOUTUBE_ONLY_ERROR)
                elif message == "end_seconds must be greater than start_seconds":
                    errors.append(VIDEO_END_AFTER_START_ERROR)
                else:
                    errors.append(_humanize_video_url_error(exc))

    if video_fields is not None:
        parsed.update(video_fields)
        if not str(parsed.get("start_seconds_display") or "").strip():
            parsed["start_seconds_display"] = format_video_time_for_form(
                video_fields.get("start_seconds")
            )
        if not str(parsed.get("end_seconds_display") or "").strip():
            parsed["end_seconds_display"] = format_video_time_for_form(
                video_fields.get("end_seconds")
            )
    else:
        parsed.setdefault("provider", "")
        parsed.setdefault("presentation_mode", "")
        parsed.setdefault("provider_video_id", "")
        parsed.setdefault("start_seconds", None)
        parsed.setdefault("end_seconds", None)

    parsed["presentation_mode_explanation"] = video_presentation_mode_explanation(
        parsed.get("presentation_mode"),
        provider=parsed.get("provider"),
    )
    parsed["provider_display_label"] = video_provider_display_label(
        parsed.get("provider")
    )
    return parsed, errors
