"""Safe reason categories for transcript PAGE XML HTTP failures."""

from __future__ import annotations

from unittest.mock import patch

import requests
from django.test import SimpleTestCase

from documents.services.transkribus_engine import (
    TranskribusPermanentError,
    TranskribusRetryableError,
    fetch_transcript_xml,
)

_URL = "https://files.example/transcript/secret-path?sig=SUPER-SECRET"
_TOKEN = "super-secret-bearer-token"


class _XmlHttpResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.text = "provider-body with Bearer secret"
        self.content = b"provider-body"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"GET {_URL} failed")


class FetchTranscriptXmlSafeReasonTests(SimpleTestCase):
    def _fetch(self, request):
        with patch(
            "documents.services.transkribus_engine.requests.request",
            side_effect=request,
        ):
            fetch_transcript_xml(_URL, bearer_token=_TOKEN)

    def test_timeout_reason_excludes_url_and_token(self):
        def request(method, url, **kwargs):
            raise requests.Timeout(f"timed out {url} {_TOKEN}")

        with self.assertRaises(TranskribusRetryableError) as ctx:
            self._fetch(request)
        self.assertEqual(ctx.exception.safe_reason, "TIMEOUT")
        self.assertNotIn(_URL, str(ctx.exception))
        self.assertNotIn(_TOKEN, str(ctx.exception))
        self.assertNotIn(_URL, ctx.exception.safe_reason or "")

    def test_http_503_reason_is_status_only(self):
        def request(method, url, **kwargs):
            self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {_TOKEN}")
            return _XmlHttpResponse(503)

        with self.assertRaises(TranskribusRetryableError) as ctx:
            self._fetch(request)
        self.assertEqual(ctx.exception.safe_reason, "HTTP_503")
        self.assertNotIn(_URL, ctx.exception.safe_reason or "")
        self.assertNotIn(_TOKEN, ctx.exception.safe_reason or "")
        self.assertNotIn("provider-body", str(ctx.exception))

    def test_http_404_is_permanent_with_status_reason(self):
        with self.assertRaises(TranskribusPermanentError) as ctx:
            self._fetch(lambda method, url, **kwargs: _XmlHttpResponse(404))
        self.assertEqual(ctx.exception.safe_reason, "HTTP_404")

    def test_connection_error_reason(self):
        def request(method, url, **kwargs):
            raise requests.ConnectionError(f"failed {url}")

        with self.assertRaises(TranskribusRetryableError) as ctx:
            self._fetch(request)
        self.assertEqual(ctx.exception.safe_reason, "CONNECTION")
        self.assertNotIn(_URL, str(ctx.exception))
