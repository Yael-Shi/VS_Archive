"""EXIF-oriented working image and Cloud Vision DOCUMENT_TEXT_DETECTION client.

Working-image orientation uses the same ``ImageOps.exif_transpose`` call as
``documents.services.exif_orientation``. This module does not read environment
variables, mutate caller bytes, retry HTTP, or persist provider payloads.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping, Sequence

import requests
from PIL import Image

from documents.services.exif_orientation import ImageOps

CLOUD_VISION_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"
VISION_FEATURE = "DOCUMENT_TEXT_DETECTION"
VISION_LANGUAGE_HINTS = ("ar",)
JPEG_MIME = "image/jpeg"
JPEG_QUALITY = 95
VISION_HTTP_TIMEOUT_CAP_SECONDS = 60.0
PROVIDER_MESSAGE_MAX_CHARS = 400

FAILURE_INVALID_IMAGE = "invalid_image"
FAILURE_INVALID_TIMEOUT = "invalid_timeout"
FAILURE_INVALID_REQUEST = "invalid_request"
FAILURE_TIMEOUT = "timeout"
FAILURE_NETWORK = "network"
FAILURE_HTTP_ERROR = "http_error"
FAILURE_INVALID_JSON = "invalid_json"
FAILURE_PROVIDER_ERROR = "provider_error"
FAILURE_MISSING_ANNOTATION = "missing_annotation"
FAILURE_MALFORMED_GEOMETRY = "malformed_geometry"
FAILURE_EMPTY_GEOMETRY = "empty_geometry"
FAILURE_EMPTY_DRAFT = "empty_draft"
FAILURE_OUT_OF_BOUNDS = "out_of_bounds"

BREAK_WHITESPACE = {
    "SPACE": " ",
    "SURE_SPACE": " ",
    "EOL_SURE_SPACE": "\n",
    "LINE_BREAK": "\n",
    "HYPHEN": "\n",
}

_KEYED_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+[?&]key=[^\s\"'<>\\]*",
    re.IGNORECASE,
)
_KEY_QUERY_RE = re.compile(r"[?&]key=[^&\s]*", re.IGNORECASE)
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")


class CloudVisionDocumentTextError(Exception):
    """Privacy-safe Cloud Vision / working-image failure."""

    def __init__(
        self,
        *,
        failure_kind: str,
        exception_class: str,
        http_status: int | None = None,
        provider_error_code: str | None = None,
        provider_error_message: str | None = None,
    ) -> None:
        self.failure_kind = failure_kind
        self.exception_class = exception_class
        self.http_status = http_status
        self.provider_error_code = provider_error_code
        self.provider_error_message = provider_error_message
        super().__init__(self._safe_text())

    def _safe_text(self) -> str:
        parts = [
            f"failure_kind={self.failure_kind}",
            f"exception_class={self.exception_class}",
        ]
        if self.http_status is not None:
            parts.append(f"http_status={self.http_status}")
        if self.provider_error_code is not None:
            parts.append(f"provider_error_code={self.provider_error_code}")
        return "CloudVisionDocumentTextError(" + ", ".join(parts) + ")"

    def __repr__(self) -> str:
        return self._safe_text()

    def __str__(self) -> str:
        return self._safe_text()


@dataclass(frozen=True)
class ArabicPrintedWorkingImage:
    width: int
    height: int
    jpeg_bytes: bytes
    mime_type: str
    sha256: str
    byte_length: int
    rgb_pixels: bytes

    def __repr__(self) -> str:
        return (
            "ArabicPrintedWorkingImage("
            f"width={self.width}, height={self.height}, mime_type={self.mime_type!r}, "
            f"sha256={self.sha256!r}, byte_length={self.byte_length})"
        )


@dataclass(frozen=True)
class ArabicPrintedBandCrop:
    width: int
    height: int
    jpeg_bytes: bytes
    mime_type: str
    sha256: str
    byte_length: int

    def __repr__(self) -> str:
        return (
            "ArabicPrintedBandCrop("
            f"width={self.width}, height={self.height}, mime_type={self.mime_type!r}, "
            f"sha256={self.sha256!r}, byte_length={self.byte_length})"
        )


@dataclass(frozen=True)
class CloudVisionSymbol:
    text: str
    break_type: str | None
    is_prefix: bool

    def __repr__(self) -> str:
        return (
            "CloudVisionSymbol("
            f"break_type={self.break_type!r}, is_prefix={self.is_prefix})"
        )


@dataclass(frozen=True)
class CloudVisionWord:
    index: int
    xmin: int
    ymin: int
    xmax: int
    ymax: int
    symbols: tuple[CloudVisionSymbol, ...]

    def __repr__(self) -> str:
        return (
            "CloudVisionWord("
            f"index={self.index}, xmin={self.xmin}, ymin={self.ymin}, "
            f"xmax={self.xmax}, ymax={self.ymax}, symbol_count={len(self.symbols)})"
        )


@dataclass(frozen=True)
class CloudVisionDocumentTextResult:
    words: tuple[CloudVisionWord, ...]
    draft_text: str
    response_sha256: str

    def __repr__(self) -> str:
        return (
            "CloudVisionDocumentTextResult("
            f"word_count={len(self.words)}, response_sha256={self.response_sha256!r})"
        )


def _fail(
    failure_kind: str,
    *,
    exception_class: str,
    http_status: int | None = None,
    provider_error_code: str | None = None,
    provider_error_message: str | None = None,
    api_key: str = "",
    extra_keys: tuple[str, ...] = (),
) -> CloudVisionDocumentTextError:
    if provider_error_code is not None:
        provider_error_code = _redact_provider_text(
            str(provider_error_code), api_key=api_key, extra_keys=extra_keys
        )
    if provider_error_message is not None:
        provider_error_message = _redact_provider_text(
            provider_error_message, api_key=api_key, extra_keys=extra_keys
        )
    return CloudVisionDocumentTextError(
        failure_kind=failure_kind,
        exception_class=exception_class,
        http_status=http_status,
        provider_error_code=provider_error_code,
        provider_error_message=provider_error_message,
    )


def _redact_provider_text(
    text: str, *, api_key: str, extra_keys: tuple[str, ...] = ()
) -> str:
    redacted = _KEYED_URL_RE.sub("<redacted-keyed-url>", text)
    redacted = _KEY_QUERY_RE.sub("<redacted-key-query>", redacted)
    for key in (api_key, *extra_keys):
        stripped = (key or "").strip()
        if stripped:
            redacted = redacted.replace(stripped, "<redacted-api-key>")
        if key and key != stripped:
            redacted = redacted.replace(key, "<redacted-api-key>")
    redacted = _LONG_B64_RE.sub("<redacted-bytes>", redacted)
    redacted = redacted.replace("\x00", "")
    if len(redacted) > PROVIDER_MESSAGE_MAX_CHARS:
        return redacted[:PROVIDER_MESSAGE_MAX_CHARS] + "…"
    return redacted


def _copy_image_bytes(image_bytes: object) -> bytes:
    if isinstance(image_bytes, memoryview):
        return image_bytes.tobytes()
    if isinstance(image_bytes, (bytes, bytearray)):
        return bytes(image_bytes)
    raise _fail(
        FAILURE_INVALID_IMAGE,
        exception_class="CloudVisionDocumentTextError",
    )


def _jpeg_bytes(image: Image.Image) -> bytes:
    try:
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        return buffer.getvalue()
    except CloudVisionDocumentTextError:
        raise
    except Exception:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        ) from None


def prepare_arabic_printed_working_image(
    image_bytes: object,
) -> ArabicPrintedWorkingImage:
    """Apply EXIF orientation once, convert to RGB, and encode JPEG quality 95."""
    source = _copy_image_bytes(image_bytes)
    if not source:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    try:
        with Image.open(BytesIO(source)) as src:
            src.load()
            oriented = ImageOps.exif_transpose(src)
            rgb = (oriented or src).convert("RGB")
            working = rgb.copy()
    except CloudVisionDocumentTextError:
        raise
    except Exception:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        ) from None
    width, height = working.size
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    jpeg_bytes = _jpeg_bytes(working)
    digest = hashlib.sha256(jpeg_bytes).hexdigest()
    try:
        rgb_pixels = bytes(working.tobytes())
    except CloudVisionDocumentTextError:
        raise
    except Exception:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        ) from None
    return ArabicPrintedWorkingImage(
        width=width,
        height=height,
        jpeg_bytes=jpeg_bytes,
        mime_type=JPEG_MIME,
        sha256=digest,
        byte_length=len(jpeg_bytes),
        rgb_pixels=rgb_pixels,
    )


def _is_int(value: object) -> bool:
    return type(value) is int


def _validate_working_image(working_image: object) -> ArabicPrintedWorkingImage:
    if not isinstance(working_image, ArabicPrintedWorkingImage):
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    if (
        not _is_int(working_image.width)
        or not _is_int(working_image.height)
        or working_image.width <= 0
        or working_image.height <= 0
    ):
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    if working_image.mime_type != JPEG_MIME:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    jpeg_bytes = working_image.jpeg_bytes
    if type(jpeg_bytes) is not bytes or not jpeg_bytes:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    if working_image.byte_length != len(jpeg_bytes):
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    if working_image.sha256 != hashlib.sha256(jpeg_bytes).hexdigest():
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    pixels = working_image.rgb_pixels
    if type(pixels) is not bytes:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    expected = working_image.width * working_image.height * 3
    if len(pixels) != expected:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        )
    return working_image


def encode_arabic_printed_band_crop(
    working_image: ArabicPrintedWorkingImage,
    *,
    left: object,
    top: object,
    right: object,
    bottom: object,
) -> ArabicPrintedBandCrop:
    """Crop from retained oriented RGB pixels. Never reopens JPEG or re-applies EXIF."""
    image = _validate_working_image(working_image)
    for value in (left, top, right, bottom):
        if not _is_int(value):
            raise _fail(
                FAILURE_OUT_OF_BOUNDS,
                exception_class="CloudVisionDocumentTextError",
            )
    if left != 0 or right != image.width:
        raise _fail(
            FAILURE_OUT_OF_BOUNDS,
            exception_class="CloudVisionDocumentTextError",
        )
    if not (0 <= top < bottom <= image.height) or not (
        0 <= left < right <= image.width
    ):
        raise _fail(
            FAILURE_OUT_OF_BOUNDS,
            exception_class="CloudVisionDocumentTextError",
        )
    try:
        source = Image.frombytes("RGB", (image.width, image.height), image.rgb_pixels)
        cropped = source.crop((left, top, right, bottom))
        crop_width, crop_height = cropped.size
    except CloudVisionDocumentTextError:
        raise
    except Exception:
        raise _fail(
            FAILURE_INVALID_IMAGE,
            exception_class="CloudVisionDocumentTextError",
        ) from None
    if crop_width <= 0 or crop_height <= 0:
        raise _fail(
            FAILURE_OUT_OF_BOUNDS,
            exception_class="CloudVisionDocumentTextError",
        )
    jpeg_bytes = _jpeg_bytes(cropped)
    digest = hashlib.sha256(jpeg_bytes).hexdigest()
    return ArabicPrintedBandCrop(
        width=crop_width,
        height=crop_height,
        jpeg_bytes=jpeg_bytes,
        mime_type=JPEG_MIME,
        sha256=digest,
        byte_length=len(jpeg_bytes),
    )


def _vision_timeout(remaining_timeout_seconds: object) -> float:
    if type(remaining_timeout_seconds) is bool or type(
        remaining_timeout_seconds
    ) not in {
        int,
        float,
    }:
        raise _fail(
            FAILURE_INVALID_TIMEOUT,
            exception_class="CloudVisionDocumentTextError",
        )
    remaining = float(remaining_timeout_seconds)
    if not math.isfinite(remaining) or remaining <= 0:
        raise _fail(
            FAILURE_INVALID_TIMEOUT,
            exception_class="CloudVisionDocumentTextError",
        )
    timeout = min(VISION_HTTP_TIMEOUT_CAP_SECONDS, remaining)
    if timeout <= 0:
        raise _fail(
            FAILURE_INVALID_TIMEOUT,
            exception_class="CloudVisionDocumentTextError",
        )
    return timeout


def _require_api_key(api_key: object) -> tuple[str, tuple[str, ...]]:
    if type(api_key) is not str:
        raise _fail(
            FAILURE_INVALID_REQUEST,
            exception_class="CloudVisionDocumentTextError",
        )
    normalized = api_key.strip()
    if not normalized:
        raise _fail(
            FAILURE_INVALID_REQUEST,
            exception_class="CloudVisionDocumentTextError",
        )
    extras: tuple[str, ...] = (api_key,) if api_key != normalized else ()
    return normalized, extras


def _vision_payload(image_jpeg: bytes) -> dict[str, Any]:
    return {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_jpeg).decode("ascii")},
                "features": [{"type": VISION_FEATURE}],
                "imageContext": {"languageHints": list(VISION_LANGUAGE_HINTS)},
            }
        ]
    }


def _provider_error_fields(response: requests.Response) -> tuple[str | None, str]:
    message = response.text or ""
    code: str | None = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            raw_status = error.get("status")
            raw_code = error.get("code")
            if isinstance(raw_status, str) and raw_status.strip():
                code = raw_status.strip()
            elif raw_code is not None:
                code = str(raw_code)
            error_message = error.get("message")
            if isinstance(error_message, str) and error_message.strip():
                message = error_message
        elif isinstance(error, str) and error.strip():
            message = error
    return code, message


def _embedded_cloud_vision_error(
    body: Mapping[str, Any],
) -> tuple[str | None, str] | None:
    responses = body.get("responses")
    if not isinstance(responses, list) or not responses:
        return None
    first = responses[0]
    if not isinstance(first, dict):
        return None
    error = first.get("error")
    if not error:
        return None
    if isinstance(error, str):
        message = error.strip()
        return (None, message or "Cloud Vision response contained an embedded error")
    if not isinstance(error, dict):
        return None, "Cloud Vision response contained an embedded error"
    code: str | None = None
    raw_status = error.get("status")
    raw_code = error.get("code")
    if isinstance(raw_status, str) and raw_status.strip():
        code = raw_status.strip()
    elif raw_code is not None:
        code = str(raw_code)
    message = error.get("message")
    if not isinstance(message, str) or not message.strip():
        message = "Cloud Vision response contained an embedded error"
    return code, message


def _finite_int_coordinate(value: object) -> int | None:
    if type(value) is bool:
        return None
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value) or not value.is_integer():
            return None
        return int(value)
    return None


def _vertex_xy(vertex: Any) -> tuple[int, int] | None:
    if not isinstance(vertex, dict):
        return None
    x = vertex["x"] if "x" in vertex else 0
    y = vertex["y"] if "y" in vertex else 0
    parsed_x = _finite_int_coordinate(x)
    parsed_y = _finite_int_coordinate(y)
    if parsed_x is None or parsed_y is None:
        return None
    return parsed_x, parsed_y


def _box_from_vertices(vertices: Any) -> tuple[int, int, int, int] | None:
    if not isinstance(vertices, list) or not vertices:
        return None
    xs: list[int] = []
    ys: list[int] = []
    for vertex in vertices:
        parsed = _vertex_xy(vertex)
        if parsed is None:
            return None
        xs.append(parsed[0])
        ys.append(parsed[1])
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _word_box(word: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    box = word.get("boundingBox")
    if not isinstance(box, dict):
        return None
    return _box_from_vertices(box.get("vertices"))


def _parse_symbol(raw_symbol: Mapping[str, Any]) -> CloudVisionSymbol:
    raw_text = raw_symbol.get("text")
    glyph = raw_text if isinstance(raw_text, str) else ""
    prop = raw_symbol.get("property")
    brk = prop.get("detectedBreak") if isinstance(prop, dict) else None
    if not isinstance(brk, dict):
        return CloudVisionSymbol(text=glyph, break_type=None, is_prefix=False)
    break_type = brk.get("type")
    return CloudVisionSymbol(
        text=glyph,
        break_type=break_type if isinstance(break_type, str) else None,
        is_prefix=bool(brk.get("isPrefix")),
    )


def reconstruct_text_from_symbols(
    symbols: Sequence[CloudVisionSymbol | Mapping[str, Any]],
) -> str:
    parts: list[str] = []
    for symbol in symbols:
        if isinstance(symbol, CloudVisionSymbol):
            glyph = symbol.text
            extra = BREAK_WHITESPACE.get(symbol.break_type or "", "")
            is_prefix = symbol.is_prefix
        elif isinstance(symbol, dict):
            raw_text = symbol.get("text")
            glyph = raw_text if isinstance(raw_text, str) else ""
            prop = symbol.get("property")
            brk = prop.get("detectedBreak") if isinstance(prop, dict) else None
            extra = ""
            is_prefix = False
            if isinstance(brk, dict):
                extra = (
                    BREAK_WHITESPACE.get(brk.get("type"), "")
                    if isinstance(brk.get("type"), str)
                    else ""
                )
                is_prefix = bool(brk.get("isPrefix"))
        else:
            continue
        if is_prefix:
            parts.append(extra)
            parts.append(glyph)
        else:
            parts.append(glyph)
            parts.append(extra)
    return "".join(parts)


def reconstruct_draft_from_word_indexes(
    words: Sequence[CloudVisionWord],
    word_indexes: Sequence[int],
) -> str:
    by_index = {word.index: word for word in words}
    symbols: list[CloudVisionSymbol] = []
    seen: set[int] = set()
    for raw_index in word_indexes:
        if type(raw_index) is not int or raw_index in seen or raw_index not in by_index:
            raise _fail(
                FAILURE_MALFORMED_GEOMETRY,
                exception_class="CloudVisionDocumentTextError",
            )
        seen.add(raw_index)
        symbols.extend(by_index[raw_index].symbols)
    draft = reconstruct_text_from_symbols(symbols)
    if not draft.strip():
        raise _fail(
            FAILURE_EMPTY_DRAFT,
            exception_class="CloudVisionDocumentTextError",
        )
    return draft


def _full_text_annotation(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    responses = payload.get("responses")
    if not isinstance(responses, list) or not responses:
        raise _fail(
            FAILURE_MISSING_ANNOTATION,
            exception_class="CloudVisionDocumentTextError",
        )
    if len(responses) != 1 or not isinstance(responses[0], dict):
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    nested = responses[0].get("fullTextAnnotation")
    if not isinstance(nested, dict):
        raise _fail(
            FAILURE_MISSING_ANNOTATION,
            exception_class="CloudVisionDocumentTextError",
        )
    return nested


def _validate_page_dimensions(
    page: Mapping[str, Any],
    *,
    image_width: int,
    image_height: int,
) -> None:
    width = page.get("width")
    height = page.get("height")
    if width is None and height is None:
        return
    if type(width) is not int or type(height) is not int:
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    if width != image_width or height != image_height:
        raise _fail(
            FAILURE_OUT_OF_BOUNDS,
            exception_class="CloudVisionDocumentTextError",
        )


def _validate_word_bounds(
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    *,
    image_width: int,
    image_height: int,
) -> None:
    if xmin > xmax or ymin > ymax:
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    if xmin < 0 or ymin < 0 or xmax >= image_width or ymax >= image_height:
        raise _fail(
            FAILURE_OUT_OF_BOUNDS,
            exception_class="CloudVisionDocumentTextError",
        )


def parse_cloud_vision_words(
    payload: Mapping[str, Any],
    *,
    image_width: int,
    image_height: int,
) -> tuple[CloudVisionWord, ...]:
    fta = _full_text_annotation(payload)
    pages = fta.get("pages")
    if not isinstance(pages, list) or not pages:
        raise _fail(
            FAILURE_MISSING_ANNOTATION,
            exception_class="CloudVisionDocumentTextError",
        )
    if len(pages) != 1 or not isinstance(pages[0], dict):
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    page = pages[0]
    words: list[CloudVisionWord] = []
    _validate_page_dimensions(page, image_width=image_width, image_height=image_height)
    blocks = page.get("blocks")
    if blocks is None:
        blocks = []
    if not isinstance(blocks, list):
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    for block in blocks:
        if not isinstance(block, dict):
            raise _fail(
                FAILURE_MALFORMED_GEOMETRY,
                exception_class="CloudVisionDocumentTextError",
            )
        paragraphs = block.get("paragraphs")
        if paragraphs is None:
            continue
        if not isinstance(paragraphs, list):
            raise _fail(
                FAILURE_MALFORMED_GEOMETRY,
                exception_class="CloudVisionDocumentTextError",
            )
        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                raise _fail(
                    FAILURE_MALFORMED_GEOMETRY,
                    exception_class="CloudVisionDocumentTextError",
                )
            raw_words = paragraph.get("words")
            if raw_words is None:
                continue
            if not isinstance(raw_words, list):
                raise _fail(
                    FAILURE_MALFORMED_GEOMETRY,
                    exception_class="CloudVisionDocumentTextError",
                )
            for raw_word in raw_words:
                if not isinstance(raw_word, dict):
                    raise _fail(
                        FAILURE_MALFORMED_GEOMETRY,
                        exception_class="CloudVisionDocumentTextError",
                    )
                box = _word_box(raw_word)
                if box is None:
                    raise _fail(
                        FAILURE_MALFORMED_GEOMETRY,
                        exception_class="CloudVisionDocumentTextError",
                    )
                xmin, ymin, xmax, ymax = box
                _validate_word_bounds(
                    xmin,
                    ymin,
                    xmax,
                    ymax,
                    image_width=image_width,
                    image_height=image_height,
                )
                raw_symbols = raw_word.get("symbols")
                if raw_symbols is None:
                    symbols: tuple[CloudVisionSymbol, ...] = ()
                elif not isinstance(raw_symbols, list) or any(
                    not isinstance(symbol, dict) for symbol in raw_symbols
                ):
                    raise _fail(
                        FAILURE_MALFORMED_GEOMETRY,
                        exception_class="CloudVisionDocumentTextError",
                    )
                else:
                    symbols = tuple(_parse_symbol(symbol) for symbol in raw_symbols)
                words.append(
                    CloudVisionWord(
                        index=len(words),
                        xmin=xmin,
                        ymin=ymin,
                        xmax=xmax,
                        ymax=ymax,
                        symbols=symbols,
                    )
                )
    if not words:
        raise _fail(
            FAILURE_EMPTY_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
        )
    draft = reconstruct_text_from_symbols(
        [symbol for word in words for symbol in word.symbols]
    )
    if not draft.strip():
        raise _fail(
            FAILURE_EMPTY_DRAFT,
            exception_class="CloudVisionDocumentTextError",
        )
    return tuple(words)


def detect_arabic_printed_document_text(
    *,
    api_key: str,
    working_image: ArabicPrintedWorkingImage,
    remaining_timeout_seconds: float,
) -> CloudVisionDocumentTextResult:
    """POST DOCUMENT_TEXT_DETECTION once. Never retries."""
    _validate_working_image(working_image)
    key, extra_keys = _require_api_key(api_key)
    timeout = _vision_timeout(remaining_timeout_seconds)
    try:
        response = requests.post(
            CLOUD_VISION_ANNOTATE_URL,
            headers={"Content-Type": "application/json"},
            params={"key": key},
            json=_vision_payload(working_image.jpeg_bytes),
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise _fail(
            FAILURE_TIMEOUT,
            exception_class=type(exc).__name__,
            provider_error_message=str(exc),
            api_key=key,
            extra_keys=extra_keys,
        ) from None
    except requests.RequestException as exc:
        raise _fail(
            FAILURE_NETWORK,
            exception_class=type(exc).__name__,
            provider_error_message=str(exc),
            api_key=key,
            extra_keys=extra_keys,
        ) from None

    raw = response.content if isinstance(response.content, (bytes, bytearray)) else b""
    response_sha256 = hashlib.sha256(bytes(raw)).hexdigest()
    if not response.ok:
        code, message = _provider_error_fields(response)
        raise _fail(
            FAILURE_HTTP_ERROR,
            exception_class="CloudVisionDocumentTextError",
            http_status=response.status_code,
            provider_error_code=code,
            provider_error_message=message,
            api_key=key,
            extra_keys=extra_keys,
        )
    try:
        body = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise _fail(
            FAILURE_INVALID_JSON,
            exception_class="ValueError",
            http_status=response.status_code,
        ) from None
    if not isinstance(body, dict):
        raise _fail(
            FAILURE_INVALID_JSON,
            exception_class="CloudVisionDocumentTextError",
            http_status=response.status_code,
        )
    responses = body.get("responses")
    if not isinstance(responses, list) or not responses:
        raise _fail(
            FAILURE_MISSING_ANNOTATION,
            exception_class="CloudVisionDocumentTextError",
            http_status=response.status_code,
        )
    if len(responses) != 1 or not isinstance(responses[0], dict):
        raise _fail(
            FAILURE_MALFORMED_GEOMETRY,
            exception_class="CloudVisionDocumentTextError",
            http_status=response.status_code,
        )
    embedded = _embedded_cloud_vision_error(body)
    if embedded is not None:
        code, message = embedded
        raise _fail(
            FAILURE_PROVIDER_ERROR,
            exception_class="CloudVisionDocumentTextError",
            http_status=response.status_code,
            provider_error_code=code,
            provider_error_message=message,
            api_key=key,
            extra_keys=extra_keys,
        )
    words = parse_cloud_vision_words(
        body,
        image_width=working_image.width,
        image_height=working_image.height,
    )
    draft = reconstruct_text_from_symbols(
        [symbol for word in words for symbol in word.symbols]
    )
    return CloudVisionDocumentTextResult(
        words=words,
        draft_text=draft,
        response_sha256=response_sha256,
    )
