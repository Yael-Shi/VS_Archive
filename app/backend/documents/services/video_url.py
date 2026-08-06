"""Isolated server-side VIDEO URL parser/normalizer (no network I/O)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

from documents.services.video_url_contract import (
    KAN_HOSTS,
    MODE_EMBEDDED,
    MODE_EXTERNAL_LINK,
    PROVIDER_KAN,
    PROVIDER_OTHER,
    PROVIDER_YOUTUBE,
    ParsedVideoUrl,
    YOUTUBE_BE_HOSTS,
    YOUTUBE_HOSTS,
    is_provider_impersonation_host,
    is_valid_youtube_video_id,
)

VIDEO_URL_INVALID_ERROR = "video URL is invalid"
VIDEO_URL_UNSUPPORTED_ERROR = "video URL is not supported"

_HTML_MARKUP_RE = re.compile(r"<\s*(iframe|script|object|embed|html|body)\b", re.I)
_YOUTUBE_TIME_COMPONENT_RE = re.compile(
    r"(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?$",
    re.I,
)


def parse_video_url(raw_value: str | None) -> ParsedVideoUrl:
    """Parse and normalize a VIDEO source URL into provider fields.

    Performs no network requests, redirects, or metadata fetches.
    """
    if raw_value is None:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    raw = str(raw_value).strip()
    if not raw:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    if _looks_like_html_or_embed_markup(raw):
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    host = _require_hostname(parsed)
    if is_provider_impersonation_host(host):
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    if host in YOUTUBE_HOSTS:
        return _parse_youtube(parsed, host=host)
    if host in KAN_HOSTS:
        return _parse_kan(parsed)
    if scheme != "https":
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    return _parse_other(parsed)


def _looks_like_html_or_embed_markup(raw: str) -> bool:
    if "<" in raw or ">" in raw:
        return True
    lowered = raw.casefold()
    if "iframe" in lowered and ("src=" in lowered or "<iframe" in lowered):
        return True
    return bool(_HTML_MARKUP_RE.search(raw))


def _require_hostname(parsed) -> str:
    """Return a lowercased hostname with a single trailing DNS root dot stripped.

    V1 normalizes absolute FQDN forms (``www.youtube.com.``) before provider
    classification so recognition and impersonation checks stay consistent.
    """
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    # Absolute DNS names may include one trailing root dot; strip all trailing
    # dots so exact allowlists and lookalike checks see the same hostname.
    host = host.rstrip(".")
    if not host:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    # V1 rejects IPv6 literals rather than risk malformed bracketed reconstruction.
    if ":" in host:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    return host


def _validated_port(parsed) -> int | None:
    try:
        return parsed.port
    except ValueError as exc:
        raise ValueError(VIDEO_URL_INVALID_ERROR) from exc


def _netloc_for_host_port(host: str, port: int | None) -> str:
    if port is None:
        return host
    return f"{host}:{port}"


def _normalize_other_https_url(parsed, *, host: str) -> str:
    """Normalize OTHER HTTPS URLs without changing destination semantics."""
    port = _validated_port(parsed)
    path = parsed.path or ""
    query = parsed.query or ""
    fragment = parsed.fragment or ""
    netloc = _netloc_for_host_port(host, port)
    return urlunparse(("https", netloc, path, "", query, fragment))


def _normalize_kan_url(parsed, *, host: str) -> str:
    """Normalize KAN URLs to canonical HTTPS without corrupting ports.

    Contract:
    - ``http`` with no port or ``:80`` → ``https://host/...`` (no ``:80``)
    - ``https`` with no port or ``:443`` → ``https://host/...`` (no ``:443``)
    - any other port/scheme combination is rejected
    """
    scheme = (parsed.scheme or "").lower()
    port = _validated_port(parsed)
    if scheme == "http":
        if port not in {None, 80}:
            raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    elif scheme == "https":
        if port not in {None, 443}:
            raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    else:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    path = parsed.path or ""
    query = parsed.query or ""
    fragment = parsed.fragment or ""
    return urlunparse(("https", host, path, "", query, fragment))


def _parse_youtube(parsed, *, host: str) -> ParsedVideoUrl:
    port = _validated_port(parsed)
    scheme = (parsed.scheme or "").lower()
    allowed_ports = {None, 443} if scheme == "https" else {None, 80}
    if port not in allowed_ports:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    path = unquote(parsed.path or "")
    path_parts = [part for part in path.split("/") if part]
    query = parse_qs(parsed.query, keep_blank_values=True)

    if "list" in query:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    if path_parts and path_parts[0].casefold() == "clip":
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    if path_parts and path_parts[0].casefold() in {
        "playlist",
        "channel",
        "c",
        "user",
        "results",
        "feed",
        "live",
    }:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
    if path_parts and path_parts[0].startswith("@"):
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    video_id = ""
    if host in YOUTUBE_BE_HOSTS:
        if len(path_parts) != 1:
            raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
        video_id = path_parts[0]
    elif path_parts and path_parts[0].casefold() == "watch":
        values = query.get("v") or []
        if not values or not values[0]:
            raise ValueError(VIDEO_URL_INVALID_ERROR)
        if len(values) != 1:
            raise ValueError(VIDEO_URL_INVALID_ERROR)
        video_id = values[0]
    elif (
        len(path_parts) >= 2
        and path_parts[0].casefold() in {"shorts", "embed"}
        and path_parts[1]
    ):
        if len(path_parts) != 2:
            raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)
        video_id = path_parts[1]
    else:
        raise ValueError(VIDEO_URL_UNSUPPORTED_ERROR)

    if not is_valid_youtube_video_id(video_id):
        raise ValueError(VIDEO_URL_INVALID_ERROR)

    start_seconds = _extract_youtube_start_seconds(parsed, query)
    end_seconds = _extract_youtube_end_seconds(query)
    _validate_time_bounds(start_seconds, end_seconds)

    canonical = urlunparse(
        ("https", "www.youtube.com", "/watch", "", f"v={video_id}", "")
    )
    return ParsedVideoUrl(
        source_url=canonical,
        provider=PROVIDER_YOUTUBE,
        presentation_mode=MODE_EMBEDDED,
        provider_video_id=video_id,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def _extract_youtube_start_seconds(parsed, query: dict[str, list[str]]) -> int | None:
    for key in ("start", "t", "time_continue"):
        values = query.get(key) or []
        if not values:
            continue
        return _parse_youtube_time_value(values[0])

    fragment = (parsed.fragment or "").strip()
    if fragment.startswith("t="):
        return _parse_youtube_time_value(fragment[2:])
    if fragment:
        try:
            return _parse_youtube_time_value(fragment)
        except ValueError:
            pass
    return None


def _extract_youtube_end_seconds(query: dict[str, list[str]]) -> int | None:
    values = query.get("end") or []
    if not values:
        return None
    return _parse_youtube_time_value(values[0], allow_zero=False)


def parse_video_time_input(raw: str, *, allow_zero: bool = True) -> int:
    """Parse a friendly video time value into whole seconds.

    Accepts plain integer seconds (``90``) or YouTube-style clock components
    (``1h2m3s``). Performs no network I/O.
    """
    value = unquote(str(raw or "")).strip()
    if not value:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    if value.isdigit():
        seconds = int(value)
    else:
        match = _YOUTUBE_TIME_COMPONENT_RE.fullmatch(value)
        if not match or not any(match.groupdict().values()):
            raise ValueError(VIDEO_URL_INVALID_ERROR)
        hours = int(match.group("hours") or 0)
        minutes = int(match.group("minutes") or 0)
        secs = int(match.group("seconds") or 0)
        seconds = hours * 3600 + minutes * 60 + secs
    if seconds < 0 or (seconds == 0 and not allow_zero):
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    return seconds


def _parse_youtube_time_value(raw: str, *, allow_zero: bool = True) -> int:
    return parse_video_time_input(raw, allow_zero=allow_zero)


def _validate_time_bounds(start_seconds: int | None, end_seconds: int | None) -> None:
    if start_seconds is not None and start_seconds < 0:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    if end_seconds is not None and end_seconds <= 0:
        raise ValueError(VIDEO_URL_INVALID_ERROR)
    if end_seconds is not None:
        start = 0 if start_seconds is None else start_seconds
        if end_seconds <= start:
            raise ValueError(VIDEO_URL_INVALID_ERROR)


def _parse_kan(parsed) -> ParsedVideoUrl:
    host = _require_hostname(parsed)
    canonical = _normalize_kan_url(parsed, host=host)
    return ParsedVideoUrl(
        source_url=canonical,
        provider=PROVIDER_KAN,
        presentation_mode=MODE_EXTERNAL_LINK,
        provider_video_id="",
        start_seconds=None,
        end_seconds=None,
    )


def _parse_other(parsed) -> ParsedVideoUrl:
    host = _require_hostname(parsed)
    canonical = _normalize_other_https_url(parsed, host=host)
    return ParsedVideoUrl(
        source_url=canonical,
        provider=PROVIDER_OTHER,
        presentation_mode=MODE_EXTERNAL_LINK,
        provider_video_id="",
        start_seconds=None,
        end_seconds=None,
    )
