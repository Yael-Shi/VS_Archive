"""Provider-independent VIDEO URL contracts shared by parser and model validation.

This module must not import Django models (avoids circular imports with
``VideoContent.clean()`` / ``video_url`` / ``video_validation``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

YOUTUBE_VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"
YOUTUBE_VIDEO_ID_RE = re.compile(YOUTUBE_VIDEO_ID_PATTERN)

# Exact hosts approved for YouTube recognition (not substring matching).
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "youtube-nocookie.com",
        "www.youtube-nocookie.com",
    }
)
YOUTUBE_BE_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
KAN_HOSTS = frozenset({"kan.org.il", "www.kan.org.il"})

# Apex domains protected against impersonation / unapproved subdomains.
# Exact allowlisted hosts are accepted before lookalike checks.
PROTECTED_PROVIDER_APEX_DOMAINS = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "youtube-nocookie.com",
        "kan.org.il",
    }
)

PROVIDER_YOUTUBE = "YOUTUBE"
PROVIDER_KAN = "KAN"
PROVIDER_OTHER = "OTHER"
MODE_EMBEDDED = "EMBEDDED"
MODE_EXTERNAL_LINK = "EXTERNAL_LINK"

PROVIDER_DISPLAY_LABELS: dict[str, str] = {
    PROVIDER_YOUTUBE: "YouTube",
    PROVIDER_KAN: "כאן",
    PROVIDER_OTHER: "אתר חיצוני",
}


def video_provider_display_label(provider: str | None) -> str:
    """Short provider label for management UI and public presentation.

    Blank/missing provider → empty string. Known providers → fixed labels.
    Unrecognized non-empty values fall back to the OTHER label.
    """
    key = str(provider or "").strip().upper()
    if not key:
        return ""
    return PROVIDER_DISPLAY_LABELS.get(key, PROVIDER_DISPLAY_LABELS[PROVIDER_OTHER])


@dataclass(frozen=True, slots=True)
class ParsedVideoUrl:
    source_url: str
    provider: str
    presentation_mode: str
    provider_video_id: str
    start_seconds: int | None
    end_seconds: int | None


def is_valid_youtube_video_id(value: str | None) -> bool:
    """Return True when ``value`` matches the approved YouTube video-id contract."""
    if value is None:
        return False
    return bool(YOUTUBE_VIDEO_ID_RE.fullmatch(str(value)))


def is_provider_impersonation_host(host: str) -> bool:
    """Return True for obvious YouTube/KAN lookalikes that are not exact allowlisted hosts.

    Rejects:
    - unapproved subdomains of protected apexes (``evil.youtube.com``)
    - dotted prefix lookalikes (``youtube.com.attacker.example``,
      ``www.youtube.com.attacker.example``, ``kan.org.il.attacker.example``)

    Does **not** use loose substring matching, so unrelated hosts such as
    ``notyoutube.com`` or ``mykan.org.example`` remain ordinary OTHER candidates.
    """
    normalized = (host or "").strip().lower().rstrip(".")
    if not normalized or normalized in YOUTUBE_HOSTS or normalized in KAN_HOSTS:
        return False

    labels = normalized.split(".")
    for apex in PROTECTED_PROVIDER_APEX_DOMAINS:
        if normalized == apex or normalized.endswith("." + apex):
            return True
        apex_labels = apex.split(".")
        if len(labels) > len(apex_labels) and labels[: len(apex_labels)] == apex_labels:
            return True
        for prefix in ("www", "m"):
            need = 1 + len(apex_labels)
            if (
                len(labels) > need
                and labels[0] == prefix
                and labels[1:need] == apex_labels
            ):
                return True
    return False
