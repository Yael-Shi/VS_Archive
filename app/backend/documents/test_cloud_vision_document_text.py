from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from io import BytesIO
from typing import Any
from unittest.mock import patch

import requests
from django.test import SimpleTestCase
from PIL import Image, ImageOps

from documents.services.cloud_vision_document_text import (
    CLOUD_VISION_ANNOTATE_URL,
    FAILURE_EMPTY_DRAFT,
    FAILURE_EMPTY_GEOMETRY,
    FAILURE_HTTP_ERROR,
    FAILURE_INVALID_IMAGE,
    FAILURE_INVALID_JSON,
    FAILURE_INVALID_REQUEST,
    FAILURE_INVALID_TIMEOUT,
    FAILURE_MALFORMED_GEOMETRY,
    FAILURE_MISSING_ANNOTATION,
    FAILURE_NETWORK,
    FAILURE_OUT_OF_BOUNDS,
    FAILURE_PROVIDER_ERROR,
    FAILURE_TIMEOUT,
    JPEG_MIME,
    JPEG_QUALITY,
    VISION_HTTP_TIMEOUT_CAP_SECONDS,
    CloudVisionDocumentTextError,
    CloudVisionSymbol,
    detect_arabic_printed_document_text,
    encode_arabic_printed_band_crop,
    parse_cloud_vision_words,
    prepare_arabic_printed_working_image,
    reconstruct_draft_from_word_indexes,
    reconstruct_text_from_symbols,
)

VISION_API_KEY = "test-vision-key-DO-NOT-STORE-cv456"
SECRET_TEST_KEY = "SECRET_TEST_KEY"
POISON = "POISON_TEXTANNOTATIONS_DESCRIPTION_do_not_use"
IMAGE_WIDTH = 200
IMAGE_HEIGHT = 1000


def _rgb_jpeg(
    width: int = 8, height: int = 12, color: tuple[int, int, int] = (12, 34, 56)
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()


def _oriented_jpeg(
    width: int,
    height: int,
    orientation: int | None,
    *,
    left_color: tuple[int, int, int] = (255, 0, 0),
    right_color: tuple[int, int, int] = (0, 0, 255),
) -> bytes:
    image = Image.new("RGB", (width, height))
    midpoint = width // 2
    for x in range(width):
        color = left_color if x < midpoint else right_color
        for y in range(height):
            image.putpixel((x, y), color)
    buffer = BytesIO()
    if orientation is None:
        image.save(buffer, format="JPEG")
    else:
        exif = image.getexif()
        exif[274] = orientation
        image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _rgba_png(width: int = 6, height: int = 7) -> bytes:
    buffer = BytesIO()
    Image.new("RGBA", (width, height), (10, 20, 30, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _vertices(xmin: int, ymin: int, xmax: int, ymax: int) -> list[dict[str, int]]:
    return [
        {"x": xmin, "y": ymin},
        {"x": xmax, "y": ymin},
        {"x": xmax, "y": ymax},
        {"x": xmin, "y": ymax},
    ]


def _symbol(
    text: str,
    *,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    break_type: str | None = None,
    is_prefix: bool = False,
) -> dict[str, Any]:
    symbol: dict[str, Any] = {
        "text": text,
        "boundingBox": {"vertices": _vertices(xmin, ymin, xmax, ymax)},
    }
    if break_type is not None:
        detected: dict[str, Any] = {"type": break_type}
        if is_prefix:
            detected["isPrefix"] = True
        symbol["property"] = {"detectedBreak": detected}
    return symbol


def _word(
    text: str,
    *,
    xmin: int,
    ymin: int,
    xmax: int,
    ymax: int,
    trailing_break: str | None = None,
) -> dict[str, Any]:
    symbols: list[dict[str, Any]] = []
    width = max(len(text), 1)
    span = max(xmax - xmin, width)
    step = span / width
    for index, char in enumerate(text):
        left = xmin + int(index * step)
        right = xmin + int((index + 1) * step)
        symbols.append(
            _symbol(
                char,
                xmin=left,
                ymin=ymin,
                xmax=max(left + 1, right),
                ymax=ymax,
                break_type=trailing_break if index == len(text) - 1 else None,
            )
        )
    return {
        "boundingBox": {"vertices": _vertices(xmin, ymin, xmax, ymax)},
        "symbols": symbols,
    }


def _vision_body(
    words: list[dict[str, Any]],
    *,
    width: int = IMAGE_WIDTH,
    height: int = IMAGE_HEIGHT,
    poison: str = POISON,
) -> dict[str, Any]:
    blocks = [
        {"blockType": "TEXT", "paragraphs": [{"words": [word]}]} for word in words
    ]
    return {
        "responses": [
            {
                "textAnnotations": [
                    {
                        "locale": "ar",
                        "description": poison,
                        "boundingPoly": {"vertices": _vertices(0, 0, width, height)},
                    }
                ],
                "fullTextAnnotation": {
                    "pages": [
                        {
                            "width": width,
                            "height": height,
                            "blocks": blocks,
                        }
                    ],
                    "text": poison,
                },
            }
        ]
    }


class FakeResponse:
    def __init__(
        self, status_code: int, body: object, *, content: bytes | None = None
    ) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        if content is not None:
            self.content = content
            self.text = content.decode("utf-8", errors="replace")
        elif isinstance(body, (bytes, bytearray)):
            self.content = bytes(body)
            self.text = self.content.decode("utf-8", errors="replace")
        else:
            self.content = json.dumps(body).encode("utf-8")
            self.text = self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)


class CloudVisionWorkingImageTests(SimpleTestCase):
    def test_exif_rotations_and_rgb_conversion(self):
        source = _oriented_jpeg(8, 16, 6)
        working = prepare_arabic_printed_working_image(source)
        self.assertEqual((working.width, working.height), (16, 8))
        self.assertEqual(working.mime_type, JPEG_MIME)
        with Image.open(BytesIO(working.jpeg_bytes)) as jpeg:
            self.assertEqual(jpeg.mode, "RGB")
            self.assertEqual(jpeg.size, (16, 8))

        upright = prepare_arabic_printed_working_image(_oriented_jpeg(8, 16, 1))
        self.assertEqual((upright.width, upright.height), (8, 16))

        rgba = prepare_arabic_printed_working_image(_rgba_png())
        self.assertEqual((rgba.width, rgba.height), (6, 7))
        with Image.open(BytesIO(rgba.jpeg_bytes)) as jpeg:
            self.assertEqual(jpeg.mode, "RGB")

    def test_exif_transpose_applied_once(self):
        calls = {"n": 0}
        original = ImageOps.exif_transpose

        def wrapped(image):
            calls["n"] += 1
            return original(image)

        with patch(
            "documents.services.cloud_vision_document_text.ImageOps.exif_transpose",
            wrapped,
        ):
            prepare_arabic_printed_working_image(_oriented_jpeg(8, 16, 6))
        self.assertEqual(calls["n"], 1)

    def test_deterministic_jpeg_encoding(self):
        source = _rgb_jpeg()
        first = prepare_arabic_printed_working_image(source)
        second = prepare_arabic_printed_working_image(source)
        self.assertEqual(first.jpeg_bytes, second.jpeg_bytes)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.sha256, hashlib.sha256(first.jpeg_bytes).hexdigest())
        self.assertEqual(first.byte_length, len(first.jpeg_bytes))
        self.assertEqual(JPEG_QUALITY, 95)
        self.assertEqual(len(first.rgb_pixels), first.width * first.height * 3)
        self.assertNotIn(first.rgb_pixels[:12].hex(), repr(first))

    def test_does_not_mutate_caller_bytes(self):
        source = bytearray(_rgb_jpeg())
        snapshot = bytes(source)
        prepare_arabic_printed_working_image(source)
        self.assertEqual(bytes(source), snapshot)

    def test_invalid_images_fail_closed(self):
        for payload in (b"", b"not-an-image", None, 123):
            with self.subTest(payload=payload):
                with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                    prepare_arabic_printed_working_image(payload)
                self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
                self.assertNotIn("not-an-image", repr(ctx.exception))


class CloudVisionReconstructionTests(SimpleTestCase):
    def test_rtl_latin_hebrew_breaks_and_prefix(self):
        rtl = [
            CloudVisionSymbol("م", None, False),
            CloudVisionSymbol("ر", None, False),
            CloudVisionSymbol("ح", None, False),
            CloudVisionSymbol("ب", None, False),
            CloudVisionSymbol("ا", "SPACE", False),
            CloudVisionSymbol("ع", None, False),
            CloudVisionSymbol("ا", None, False),
            CloudVisionSymbol("ل", None, False),
            CloudVisionSymbol("م", "LINE_BREAK", False),
        ]
        self.assertEqual(reconstruct_text_from_symbols(rtl), "مرحبا عالم\n")
        mixed = [
            CloudVisionSymbol("A", "SURE_SPACE", False),
            CloudVisionSymbol("B", "EOL_SURE_SPACE", False),
            CloudVisionSymbol("C", "HYPHEN", False),
            CloudVisionSymbol("D", "SPACE", True),
            CloudVisionSymbol("E", None, False),
            CloudVisionSymbol("ש", None, False),
        ]
        self.assertEqual(reconstruct_text_from_symbols(mixed), "A B\nC\n DEש")
        self.assertNotIn(POISON, reconstruct_text_from_symbols(rtl))

    def test_band_helper_uses_only_selected_word_symbols(self):
        words = parse_cloud_vision_words(
            _vision_body(
                [
                    _word(
                        "BANDONE",
                        xmin=10,
                        ymin=10,
                        xmax=90,
                        ymax=80,
                        trailing_break="SPACE",
                    ),
                    _word(
                        "BANDTWO",
                        xmin=10,
                        ymin=600,
                        xmax=90,
                        ymax=670,
                        trailing_break="LINE_BREAK",
                    ),
                ]
            ),
            image_width=IMAGE_WIDTH,
            image_height=IMAGE_HEIGHT,
        )
        self.assertEqual(reconstruct_draft_from_word_indexes(words, (0,)), "BANDONE ")
        self.assertEqual(reconstruct_draft_from_word_indexes(words, (1,)), "BANDTWO\n")
        self.assertNotIn(POISON, reconstruct_draft_from_word_indexes(words, (0, 1)))


class CloudVisionClientTests(SimpleTestCase):
    def _working(self, *, width: int = IMAGE_WIDTH, height: int = IMAGE_HEIGHT):
        return prepare_arabic_printed_working_image(_rgb_jpeg(width, height))

    def _detect(self, fake_post, **kwargs):
        with patch(
            "documents.services.cloud_vision_document_text.requests.post",
            fake_post,
        ):
            return detect_arabic_printed_document_text(
                api_key=kwargs.pop("api_key", VISION_API_KEY),
                working_image=kwargs.pop("working_image", self._working()),
                remaining_timeout_seconds=kwargs.pop("remaining_timeout_seconds", 90.0),
            )

    def test_successful_post_contract_and_poison_unused(self):
        posts: list[dict[str, Any]] = []
        body = _vision_body(
            [
                _word(
                    "مرحبا",
                    xmin=10,
                    ymin=10,
                    xmax=90,
                    ymax=40,
                    trailing_break="LINE_BREAK",
                ),
            ]
        )
        raw = json.dumps(body).encode("utf-8")

        def fake_post(*args, **kwargs):
            posts.append({"args": args, "kwargs": kwargs})
            return FakeResponse(200, body, content=raw)

        result = self._detect(fake_post)
        self.assertEqual(len(posts), 1)
        request = posts[0]
        self.assertEqual(request["args"][0], CLOUD_VISION_ANNOTATE_URL)
        self.assertEqual(
            request["kwargs"]["headers"], {"Content-Type": "application/json"}
        )
        self.assertEqual(request["kwargs"]["params"], {"key": VISION_API_KEY})
        self.assertEqual(request["kwargs"]["timeout"], VISION_HTTP_TIMEOUT_CAP_SECONDS)
        payload = request["kwargs"]["json"]["requests"][0]
        self.assertEqual(payload["features"], [{"type": "DOCUMENT_TEXT_DETECTION"}])
        self.assertEqual(payload["imageContext"]["languageHints"], ["ar"])
        self.assertEqual(result.response_sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(result.words[0].index, 0)
        self.assertIn("مرحبا", result.draft_text)
        self.assertNotIn(POISON, result.draft_text)
        self.assertNotIn(VISION_API_KEY, repr(result))
        self.assertNotIn("مرحبا", repr(result))
        self.assertNotIn(VISION_API_KEY, json.dumps(payload["image"]))

    def test_timeout_cap_and_remaining_below_cap(self):
        posts: list[float] = []

        def fake_post(*args, **kwargs):
            posts.append(kwargs["timeout"])
            return FakeResponse(
                200,
                _vision_body(
                    [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
                ),
            )

        self._detect(fake_post, remaining_timeout_seconds=90.0)
        self._detect(fake_post, remaining_timeout_seconds=12.5)
        self.assertEqual(posts, [60.0, 12.5])

    def test_non_positive_timeout_rejects_before_http(self):
        posts: list[Any] = []

        def fake_post(*args, **kwargs):
            posts.append(kwargs)
            raise AssertionError("HTTP must not run")

        for remaining in (0, -1, float("nan"), float("inf"), True, None, "30"):
            with self.subTest(remaining=remaining):
                with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                    self._detect(fake_post, remaining_timeout_seconds=remaining)
                self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_TIMEOUT)
        self.assertEqual(posts, [])

    def test_timeout_and_network_failures_do_not_retry(self):
        posts = {"n": 0}

        def timeout_post(*args, **kwargs):
            posts["n"] += 1
            raise requests.Timeout(f"timed out {VISION_API_KEY}")

        with self.assertRaises(CloudVisionDocumentTextError) as ctx:
            self._detect(timeout_post)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_TIMEOUT)
        self.assertEqual(posts["n"], 1)
        self.assertNotIn(VISION_API_KEY, repr(ctx.exception))
        self.assertNotIn(VISION_API_KEY, ctx.exception.provider_error_message or "")

        posts["n"] = 0
        keyed = f"{CLOUD_VISION_ANNOTATE_URL}?key={SECRET_TEST_KEY}"

        def network_post(*args, **kwargs):
            posts["n"] += 1
            raise requests.ConnectionError(f"Failed to connect to {keyed}")

        with self.assertRaises(CloudVisionDocumentTextError) as ctx:
            self._detect(network_post, api_key=SECRET_TEST_KEY)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_NETWORK)
        self.assertEqual(posts["n"], 1)
        self.assertNotIn(SECRET_TEST_KEY, repr(ctx.exception))
        self.assertNotIn(SECRET_TEST_KEY, ctx.exception.provider_error_message or "")
        self.assertNotIn(keyed, ctx.exception.provider_error_message or "")

    def test_http_invalid_json_embedded_error_missing_and_malformed(self):
        cases = []

        def run(body=None, status=200, content=None, exc_kind=None):
            posts = {"n": 0}

            def fake_post(*args, **kwargs):
                posts["n"] += 1
                if content is not None:
                    return FakeResponse(status, {}, content=content)
                return FakeResponse(status, body)

            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                self._detect(fake_post)
            self.assertEqual(posts["n"], 1)
            self.assertEqual(ctx.exception.failure_kind, exc_kind)
            self.assertNotIn(VISION_API_KEY, repr(ctx.exception))
            cases.append(ctx.exception.failure_kind)

        keyed = f"{CLOUD_VISION_ANNOTATE_URL}?key={VISION_API_KEY}"
        run(
            {
                "error": {
                    "status": "PERMISSION_DENIED",
                    "message": f"Request failed for {keyed}",
                }
            },
            status=403,
            exc_kind=FAILURE_HTTP_ERROR,
        )
        run(content=b"not-json", exc_kind=FAILURE_INVALID_JSON)
        embedded = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        embedded["responses"][0]["error"] = {
            "code": 3,
            "status": "INVALID_ARGUMENT",
            "message": f"Embedded Vision error with key={VISION_API_KEY}",
        }
        run(embedded, exc_kind=FAILURE_PROVIDER_ERROR)
        run({"responses": [{}]}, exc_kind=FAILURE_MISSING_ANNOTATION)
        malformed = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        malformed["responses"][0]["fullTextAnnotation"]["pages"][0]["blocks"] = "bad"
        run(malformed, exc_kind=FAILURE_MALFORMED_GEOMETRY)

    def test_invalid_vertices_empty_words_and_out_of_bounds(self):
        posts = {"n": 0}

        def detect_body(body):
            posts["n"] = 0

            def fake_post(*args, **kwargs):
                posts["n"] += 1
                return FakeResponse(200, body)

            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                self._detect(fake_post)
            self.assertEqual(posts["n"], 1)
            return ctx.exception.failure_kind

        bad_vertices = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        bad_vertices["responses"][0]["fullTextAnnotation"]["pages"][0]["blocks"][0][
            "paragraphs"
        ][0]["words"][0]["boundingBox"] = {"vertices": [{"x": True, "y": 1}]}
        self.assertEqual(detect_body(bad_vertices), FAILURE_MALFORMED_GEOMETRY)

        empty_words = {
            "responses": [
                {
                    "fullTextAnnotation": {
                        "pages": [
                            {"width": IMAGE_WIDTH, "height": IMAGE_HEIGHT, "blocks": []}
                        ],
                        "text": POISON,
                    }
                }
            ]
        }
        self.assertEqual(detect_body(empty_words), FAILURE_EMPTY_GEOMETRY)

        empty_draft = _vision_body(
            [
                {
                    "boundingBox": {"vertices": _vertices(1, 1, 2, 2)},
                    "symbols": [_symbol("", xmin=1, ymin=1, xmax=2, ymax=2)],
                }
            ]
        )
        self.assertEqual(detect_body(empty_draft), FAILURE_EMPTY_DRAFT)

        out_of_bounds = _vision_body(
            [
                _word(
                    "A",
                    xmin=1,
                    ymin=1,
                    xmax=2,
                    ymax=IMAGE_HEIGHT,
                    trailing_break="SPACE",
                )
            ]
        )
        self.assertEqual(detect_body(out_of_bounds), FAILURE_OUT_OF_BOUNDS)

        wrong_page = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")],
            width=12,
            height=12,
        )
        self.assertEqual(detect_body(wrong_page), FAILURE_OUT_OF_BOUNDS)

    def test_malformed_symbols_fail_closed(self):
        posts = {"n": 0}
        body = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        body["responses"][0]["fullTextAnnotation"]["pages"][0]["blocks"][0][
            "paragraphs"
        ][0]["words"][0]["symbols"] = ["bad"]

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            return FakeResponse(200, body)

        with self.assertRaises(CloudVisionDocumentTextError) as ctx:
            self._detect(fake_post)
        self.assertEqual(posts["n"], 1)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_MALFORMED_GEOMETRY)

    def test_working_image_repr_omits_jpeg_bytes(self):
        working = self._working(width=8, height=8)
        self.assertNotIn(working.jpeg_bytes[:12].hex(), repr(working))
        self.assertNotIn(working.rgb_pixels[:12].hex(), repr(working))
        self.assertIn("byte_length", repr(working))

    def test_crop_uses_oriented_rgb_and_does_not_reopen_or_transpose(self):
        source = _oriented_jpeg(8, 16, 6)
        calls = {"n": 0}
        original = ImageOps.exif_transpose

        def wrapped(image):
            calls["n"] += 1
            return original(image)

        with patch(
            "documents.services.cloud_vision_document_text.ImageOps.exif_transpose",
            wrapped,
        ):
            working = prepare_arabic_printed_working_image(source)
        self.assertEqual(calls["n"], 1)
        self.assertEqual((working.width, working.height), (16, 8))
        expected = Image.frombytes(
            "RGB", (working.width, working.height), working.rgb_pixels
        )
        expected_crop = expected.crop((0, 0, working.width, 4))
        buffer = BytesIO()
        expected_crop.save(buffer, format="JPEG", quality=JPEG_QUALITY)
        with patch(
            "documents.services.cloud_vision_document_text.Image.open",
            side_effect=AssertionError("crop must not call Image.open"),
        ):
            crop = encode_arabic_printed_band_crop(
                working, left=0, top=0, right=working.width, bottom=4
            )
        self.assertEqual(calls["n"], 1)
        self.assertEqual(crop.jpeg_bytes, buffer.getvalue())
        self.assertEqual(crop.width, working.width)
        self.assertEqual(crop.height, 4)
        self.assertEqual(crop.mime_type, JPEG_MIME)
        self.assertEqual(crop.sha256, hashlib.sha256(crop.jpeg_bytes).hexdigest())
        self.assertNotIn(crop.jpeg_bytes[:12].hex(), repr(crop))
        with self.assertRaises(CloudVisionDocumentTextError) as ctx:
            encode_arabic_printed_band_crop(
                working, left=1, top=0, right=working.width, bottom=4
            )
        self.assertEqual(ctx.exception.failure_kind, FAILURE_OUT_OF_BOUNDS)

    def test_working_image_integrity_rejects_before_http(self):
        posts: list[Any] = []

        def fake_post(*args, **kwargs):
            posts.append(kwargs)
            raise AssertionError("HTTP must not run")

        working = self._working(width=8, height=8)
        tampered = [
            replace(working, width=0),
            replace(working, mime_type="image/png"),
            replace(working, jpeg_bytes=b""),
            replace(working, byte_length=working.byte_length + 1),
            replace(working, sha256="0" * 64),
            replace(working, rgb_pixels=working.rgb_pixels[:-1]),
        ]
        for image in tampered:
            with self.subTest(image=image):
                with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                    self._detect(fake_post, working_image=image)
                self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
                self.assertNotIn(VISION_API_KEY, repr(ctx.exception))
        self.assertEqual(posts, [])

    def test_working_image_rejects_mutable_buffers_before_http_and_crop(self):
        posts: list[Any] = []

        def blocked(*args, **kwargs):
            posts.append(kwargs)
            raise AssertionError("HTTP must not run")

        working = self._working(width=8, height=8)
        tampered = [
            replace(working, jpeg_bytes=bytearray(working.jpeg_bytes)),
            replace(working, rgb_pixels=bytearray(working.rgb_pixels)),
            replace(working, jpeg_bytes=memoryview(working.jpeg_bytes)),
            replace(working, rgb_pixels=memoryview(working.rgb_pixels)),
        ]
        for image in tampered:
            with self.subTest(
                image=type(image.jpeg_bytes), pixels=type(image.rgb_pixels)
            ):
                with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                    self._detect(blocked, working_image=image)
                self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
                with self.assertRaises(CloudVisionDocumentTextError) as crop_ctx:
                    encode_arabic_printed_band_crop(
                        image, left=0, top=0, right=image.width, bottom=4
                    )
                self.assertEqual(crop_ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
        self.assertEqual(posts, [])

    def test_api_key_strip_and_reject_whitespace(self):
        posts: list[dict[str, Any]] = []
        body = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )

        def fake_post(*args, **kwargs):
            posts.append(kwargs)
            return FakeResponse(200, body)

        padded = f"  {VISION_API_KEY}  "
        self._detect(fake_post, api_key=padded)
        self.assertEqual(posts[0]["params"], {"key": VISION_API_KEY})
        self.assertNotIn(padded, repr(posts[0]["params"]))

        posts.clear()

        def blocked(*args, **kwargs):
            posts.append(kwargs)
            raise AssertionError("HTTP must not run")

        for key in ("", "   ", "\n\t", None, 123):
            with self.subTest(key=key):
                with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                    self._detect(blocked, api_key=key)
                self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_REQUEST)
                self.assertNotIn("   ", repr(ctx.exception))
                if isinstance(key, str) and key.strip() == VISION_API_KEY:
                    self.fail("whitespace key should not authenticate")
        self.assertEqual(posts, [])

    def test_single_response_and_single_page_required(self):
        posts = {"n": 0}

        def detect_body(body):
            posts["n"] = 0

            def fake_post(*args, **kwargs):
                posts["n"] += 1
                return FakeResponse(200, body)

            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                self._detect(fake_post)
            self.assertEqual(posts["n"], 1)
            return ctx.exception.failure_kind

        top_level_only = {
            "fullTextAnnotation": _vision_body(
                [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
            )["responses"][0]["fullTextAnnotation"]
        }
        self.assertEqual(detect_body(top_level_only), FAILURE_MISSING_ANNOTATION)

        two_responses = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        two_responses["responses"].append(two_responses["responses"][0])
        self.assertEqual(detect_body(two_responses), FAILURE_MALFORMED_GEOMETRY)

        two_pages = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        page = two_pages["responses"][0]["fullTextAnnotation"]["pages"][0]
        two_pages["responses"][0]["fullTextAnnotation"]["pages"] = [page, page]
        self.assertEqual(detect_body(two_pages), FAILURE_MALFORMED_GEOMETRY)

    def test_vertex_missing_zero_rejects_nonfinite_and_fractional(self):
        posts = {"n": 0}

        def detect_body(body):
            posts["n"] = 0

            def fake_post(*args, **kwargs):
                posts["n"] += 1
                return FakeResponse(200, body)

            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                self._detect(fake_post)
            self.assertEqual(posts["n"], 1)
            return ctx.exception.failure_kind

        missing_xy = _vision_body(
            [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
        )
        missing_xy["responses"][0]["fullTextAnnotation"]["pages"][0]["blocks"][0][
            "paragraphs"
        ][0]["words"][0]["boundingBox"] = {
            "vertices": [{}, {"x": 2}, {"y": 2}, {"x": 2, "y": 2}]
        }
        words = parse_cloud_vision_words(
            missing_xy, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
        )
        self.assertEqual(words[0].xmin, 0)
        self.assertEqual(words[0].ymin, 0)

        for bad in (float("nan"), float("inf"), 1.5):
            body = _vision_body(
                [_word("A", xmin=1, ymin=1, xmax=2, ymax=2, trailing_break="SPACE")]
            )
            body["responses"][0]["fullTextAnnotation"]["pages"][0]["blocks"][0][
                "paragraphs"
            ][0]["words"][0]["boundingBox"] = {
                "vertices": [
                    {"x": bad, "y": 1},
                    {"x": 2, "y": 1},
                    {"x": 2, "y": 2},
                    {"x": 1, "y": 2},
                ]
            }
            self.assertEqual(detect_body(body), FAILURE_MALFORMED_GEOMETRY)

    def test_provider_error_code_is_redacted_in_repr(self):
        posts = {"n": 0}
        keyed = f"{CLOUD_VISION_ANNOTATE_URL}?key={SECRET_TEST_KEY}"
        body = {
            "error": {
                "status": keyed,
                "message": f"denied {SECRET_TEST_KEY}",
            }
        }

        def fake_post(*args, **kwargs):
            posts["n"] += 1
            return FakeResponse(403, body)

        with self.assertRaises(CloudVisionDocumentTextError) as ctx:
            self._detect(fake_post, api_key=SECRET_TEST_KEY)
        self.assertEqual(posts["n"], 1)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_HTTP_ERROR)
        self.assertNotIn(SECRET_TEST_KEY, repr(ctx.exception))
        self.assertNotIn(SECRET_TEST_KEY, str(ctx.exception))
        self.assertNotIn(SECRET_TEST_KEY, ctx.exception.provider_error_code or "")
        self.assertNotIn(SECRET_TEST_KEY, ctx.exception.provider_error_message or "")

    def test_jpeg_encode_failures_are_typed(self):
        source = _rgb_jpeg()
        working = self._working(width=8, height=8)

        def boom(*args, **kwargs):
            raise OSError(f"encode failed {VISION_API_KEY}")

        with patch("PIL.Image.Image.save", boom):
            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                prepare_arabic_printed_working_image(source)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
        self.assertNotIn(VISION_API_KEY, repr(ctx.exception))

        with patch("PIL.Image.Image.save", boom):
            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                encode_arabic_printed_band_crop(
                    working, left=0, top=0, right=working.width, bottom=4
                )
        self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
        self.assertNotIn(VISION_API_KEY, repr(ctx.exception))

    def test_rgb_tobytes_failure_is_typed(self):
        source = _rgb_jpeg()

        def fake_save(self, fp, *args, **kwargs):
            fp.write(source)

        def boom(*args, **kwargs):
            raise OSError(f"tobytes failed {VISION_API_KEY}")

        with (
            patch("PIL.Image.Image.save", fake_save),
            patch("PIL.Image.Image.tobytes", boom),
        ):
            with self.assertRaises(CloudVisionDocumentTextError) as ctx:
                prepare_arabic_printed_working_image(source)
        self.assertEqual(ctx.exception.failure_kind, FAILURE_INVALID_IMAGE)
        self.assertNotIn(VISION_API_KEY, repr(ctx.exception))
