"""Public VIDEO presentation helpers (no network I/O).

Builds safe, validated view/template contracts from stored ``VideoContent``.
Treat stored fields as untrusted at render time even though writes are validated:
re-parse ``source_url`` with the canonical PR1 parser and require stored
provider/mode/id/URL to match the normalized parse.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

from documents.models import ArchiveItem, VideoContent
from documents.services.video_url import parse_video_url
from documents.services.video_url_contract import (
    MODE_EMBEDDED,
    MODE_EXTERNAL_LINK,
    PROVIDER_KAN,
    PROVIDER_OTHER,
    PROVIDER_YOUTUBE,
    is_valid_youtube_video_id,
    video_provider_display_label,
)

YOUTUBE_NOCOOKIE_EMBED_ORIGIN = "https://www.youtube-nocookie.com"
YOUTUBE_NOCOOKIE_EMBED_HOST = "www.youtube-nocookie.com"
YOUTUBE_NOCOOKIE_EMBED_BASE = f"{YOUTUBE_NOCOOKIE_EMBED_ORIGIN}/embed"

EXTERNAL_BUTTON_LABEL_KAN = "צפייה בסרטון באתר כאן"
EXTERNAL_BUTTON_LABEL_OTHER = "צפייה בסרטון באתר המקורי"
YOUTUBE_ACTIVATE_LABEL = "הפעלת הסרטון"
YOUTUBE_OPEN_LABEL = "פתיחה ב־YouTube"
YOUTUBE_ACTIVATION_EXPLAINER = (
    "הסרטון לא נטען עדיין. לחיצה על ההפעלה תטען תוכן מאתר YouTube "
    "(דומיין הפרטיות youtube-nocookie.com). VS-Archive אינו מארח את הסרטון עצמו."
)
EXTERNAL_LINK_OPENS_NOTE = "הקישור נפתח באתר חיצוני בכרטיסייה חדשה."
PLACEHOLDER_TITLE_FALLBACK = "סרטון"


@dataclass(frozen=True, slots=True)
class VideoPublicPresentation:
    """Safe public detail/card contract for a VIDEO ArchiveItem."""

    provider: str
    provider_label: str
    presentation_mode: str
    source_url: str
    is_youtube_embed: bool
    youtube_video_id: str | None
    youtube_embed_src: str | None
    start_seconds: int | None
    end_seconds: int | None
    placeholder_title: str
    iframe_title: str
    activate_label: str
    open_on_youtube_label: str
    activation_explainer: str
    external_button_label: str
    source_domain_display: str
    external_link_opens_note: str


def video_placeholder_title(archive_item: ArchiveItem) -> str:
    """Safe local facade title from ArchiveItem.title (Hebrew fallback when blank)."""
    return (archive_item.title or "").strip() or PLACEHOLDER_TITLE_FALLBACK


def _normalized_youtube_times(
    start_seconds: int | None,
    end_seconds: int | None,
) -> tuple[int | None, int | None] | None:
    """Canonical YouTube start/end contract shared by presentation and embed URL build.

    ``bool`` is rejected (``type(x) is int``), even though ``bool`` subclasses ``int``.
    """
    start = start_seconds
    end = end_seconds
    if start is not None and (type(start) is not int or start < 0):
        return None
    if end is not None:
        if type(end) is not int or end <= 0:
            return None
        start_bound = 0 if start is None else start
        if end <= start_bound:
            return None
    return (start, end)


def _youtube_embed_src_is_approved(
    src: str,
    *,
    video_id: str,
    start_seconds: int | None,
    end_seconds: int | None,
) -> bool:
    """Defense-in-depth: approve only a server-built youtube-nocookie embed URL."""
    if not is_valid_youtube_video_id(video_id):
        return False
    try:
        parsed = urlparse(src)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.hostname != YOUTUBE_NOCOOKIE_EMBED_HOST:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.port is not None:
        return False
    if parsed.path != f"/embed/{video_id}":
        return False
    if parsed.params or parsed.fragment:
        return False

    query = parse_qs(parsed.query, keep_blank_values=True)
    expected: dict[str, list[str]] = {"playsinline": ["1"]}
    if start_seconds is not None:
        expected["start"] = [str(start_seconds)]
    if end_seconds is not None:
        expected["end"] = [str(end_seconds)]
    if set(query.keys()) != set(expected.keys()):
        return False
    for key, values in expected.items():
        if query.get(key) != values:
            return False
    return True


def build_youtube_nocookie_embed_src(
    video_id: str,
    *,
    start_seconds: int | None = None,
    end_seconds: int | None = None,
) -> str | None:
    """Build a youtube-nocookie embed URL from a validated id and integer times.

    Returns ``None`` when inputs are unsafe or inconsistent. Never accepts a raw
    iframe URL or untrusted query string.
    """
    if not is_valid_youtube_video_id(video_id):
        return None

    times = _normalized_youtube_times(start_seconds, end_seconds)
    if times is None:
        return None
    start, end = times

    params: dict[str, str] = {"playsinline": "1"}
    if start is not None:
        params["start"] = str(start)
    if end is not None:
        params["end"] = str(end)

    # Explicitly omit autoplay.
    query = urlencode(params)
    src = f"{YOUTUBE_NOCOOKIE_EMBED_BASE}/{video_id}?{query}"
    if not _youtube_embed_src_is_approved(
        src,
        video_id=video_id,
        start_seconds=start,
        end_seconds=end,
    ):
        return None
    return src


def _source_domain_display(source_url: str) -> str:
    try:
        host = (urlparse(source_url).hostname or "").strip().lower().rstrip(".")
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def build_video_public_presentation(
    archive_item: ArchiveItem,
) -> VideoPublicPresentation | None:
    """Return a public presentation contract, or ``None`` to fail closed.

    Re-parses ``VideoContent.source_url`` with ``parse_video_url()`` and requires
    stored provider / presentation mode / provider video id / source_url to match
    the canonical parse. Explicit YouTube start/end may differ from URL-derived
    times when they satisfy the integer/range/order contract.
    """
    if archive_item.item_type != ArchiveItem.ItemType.VIDEO:
        return None

    try:
        content = archive_item.video_content
    except VideoContent.DoesNotExist:
        return None

    try:
        parsed = parse_video_url(content.source_url)
    except ValueError:
        return None

    stored_url = str(content.source_url or "").strip()
    if stored_url != parsed.source_url:
        return None

    provider = str(content.provider or "").strip().upper()
    mode = str(content.presentation_mode or "").strip().upper()
    video_id = str(content.provider_video_id or "").strip()
    if provider != parsed.provider:
        return None
    if mode != parsed.presentation_mode:
        return None
    if video_id != str(parsed.provider_video_id or "").strip():
        return None

    # Public external links must be the parser's normalized HTTPS URL only.
    if urlparse(parsed.source_url).scheme != "https":
        return None

    source_url = parsed.source_url
    start = content.start_seconds
    end = content.end_seconds
    placeholder_title = video_placeholder_title(archive_item)
    provider_label = video_provider_display_label(provider)
    domain = _source_domain_display(source_url)

    if provider == PROVIDER_YOUTUBE and mode == MODE_EMBEDDED:
        # Times + embed URL share one canonical validation path.
        embed_src = build_youtube_nocookie_embed_src(
            video_id,
            start_seconds=start,
            end_seconds=end,
        )
        if embed_src is None:
            return None
        return VideoPublicPresentation(
            provider=provider,
            provider_label=provider_label,
            presentation_mode=mode,
            source_url=source_url,
            is_youtube_embed=True,
            youtube_video_id=video_id,
            youtube_embed_src=embed_src,
            start_seconds=start,
            end_seconds=end,
            placeholder_title=placeholder_title,
            iframe_title=f"נגן YouTube: {placeholder_title}",
            activate_label=YOUTUBE_ACTIVATE_LABEL,
            open_on_youtube_label=YOUTUBE_OPEN_LABEL,
            activation_explainer=YOUTUBE_ACTIVATION_EXPLAINER,
            external_button_label="",
            source_domain_display=domain,
            external_link_opens_note=EXTERNAL_LINK_OPENS_NOTE,
        )

    if provider == PROVIDER_KAN and mode == MODE_EXTERNAL_LINK:
        if video_id:
            return None
        if start is not None or end is not None:
            return None
        return VideoPublicPresentation(
            provider=provider,
            provider_label=provider_label,
            presentation_mode=mode,
            source_url=source_url,
            is_youtube_embed=False,
            youtube_video_id=None,
            youtube_embed_src=None,
            start_seconds=None,
            end_seconds=None,
            placeholder_title=placeholder_title,
            iframe_title="",
            activate_label="",
            open_on_youtube_label="",
            activation_explainer="",
            external_button_label=EXTERNAL_BUTTON_LABEL_KAN,
            source_domain_display=domain or "kan.org.il",
            external_link_opens_note=EXTERNAL_LINK_OPENS_NOTE,
        )

    if provider == PROVIDER_OTHER and mode == MODE_EXTERNAL_LINK:
        if video_id:
            return None
        if start is not None or end is not None:
            return None
        return VideoPublicPresentation(
            provider=provider,
            provider_label=provider_label,
            presentation_mode=mode,
            source_url=source_url,
            is_youtube_embed=False,
            youtube_video_id=None,
            youtube_embed_src=None,
            start_seconds=None,
            end_seconds=None,
            placeholder_title=placeholder_title,
            iframe_title="",
            activate_label="",
            open_on_youtube_label="",
            activation_explainer="",
            external_button_label=EXTERNAL_BUTTON_LABEL_OTHER,
            source_domain_display=domain,
            external_link_opens_note=EXTERNAL_LINK_OPENS_NOTE,
        )

    return None
