"""Focused tests for the isolated VIDEO URL parser/normalizer."""

from __future__ import annotations

from django.test import SimpleTestCase

from documents.models import VideoContent
from documents.services.video_url import (
    VIDEO_URL_INVALID_ERROR,
    VIDEO_URL_UNSUPPORTED_ERROR,
    parse_video_url,
)


class VideoUrlParserTests(SimpleTestCase):
    def test_youtube_watch_url(self):
        parsed = parse_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(parsed.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(
            parsed.presentation_mode,
            VideoContent.PresentationMode.EMBEDDED,
        )
        self.assertEqual(parsed.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(
            parsed.source_url,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertIsNone(parsed.start_seconds)
        self.assertIsNone(parsed.end_seconds)

    def test_youtu_be_url(self):
        parsed = parse_video_url("https://youtu.be/dQw4w9WgXcQ")
        self.assertEqual(parsed.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(parsed.provider, VideoContent.Provider.YOUTUBE)

    def test_youtube_shorts_url(self):
        parsed = parse_video_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual(parsed.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(
            parsed.presentation_mode,
            VideoContent.PresentationMode.EMBEDDED,
        )

    def test_youtube_embed_url(self):
        parsed = parse_video_url("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ")
        self.assertEqual(parsed.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(
            parsed.source_url,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_youtube_start_time_seconds_and_clock_syntax(self):
        parsed_t = parse_video_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=90")
        self.assertEqual(parsed_t.start_seconds, 90)

        parsed_clock = parse_video_url("https://youtu.be/dQw4w9WgXcQ?t=1h2m3s")
        self.assertEqual(parsed_clock.start_seconds, 3723)

        parsed_start = parse_video_url(
            "https://www.youtube.com/embed/dQw4w9WgXcQ?start=15&end=45"
        )
        self.assertEqual(parsed_start.start_seconds, 15)
        self.assertEqual(parsed_start.end_seconds, 45)

    def test_kan_exact_host(self):
        parsed = parse_video_url("https://www.kan.org.il/content/kan/item/12345/")
        self.assertEqual(parsed.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            parsed.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )
        self.assertEqual(parsed.provider_video_id, "")
        self.assertIsNone(parsed.start_seconds)
        self.assertIsNone(parsed.end_seconds)
        self.assertTrue(parsed.source_url.startswith("https://www.kan.org.il/"))

    def test_other_valid_https_provider(self):
        parsed = parse_video_url("https://vimeo.com/123456789")
        self.assertEqual(parsed.provider, VideoContent.Provider.OTHER)
        self.assertEqual(
            parsed.presentation_mode,
            VideoContent.PresentationMode.EXTERNAL_LINK,
        )
        self.assertEqual(parsed.provider_video_id, "")
        self.assertEqual(parsed.source_url, "https://vimeo.com/123456789")

    def test_spoofed_provider_hosts_are_rejected(self):
        spoofed = [
            "https://youtube.com.attacker.example/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com.attacker.example/watch?v=dQw4w9WgXcQ",
            "https://youtu.be.attacker.example/dQw4w9WgXcQ",
            "https://kan.org.il.attacker.example/item/1",
            "https://www.kan.org.il.attacker.example/item/1",
            "https://evil.youtube.com/watch?v=dQw4w9WgXcQ",
        ]
        for url in spoofed:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_video_url(url)
                self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_unrelated_lookalike_hosts_remain_other(self):
        parsed = parse_video_url("https://notyoutube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(parsed.provider, VideoContent.Provider.OTHER)
        self.assertEqual(
            parsed.source_url, "https://notyoutube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_other_https_preserves_port_query_and_fragment(self):
        parsed = parse_video_url("https://example.com:8443/video?id=1#chapter-2")
        self.assertEqual(parsed.provider, VideoContent.Provider.OTHER)
        self.assertEqual(
            parsed.source_url,
            "https://example.com:8443/video?id=1#chapter-2",
        )

    def test_kan_default_ports_normalize_to_https_without_port(self):
        http_no_port = parse_video_url("http://www.kan.org.il/content/item/1")
        self.assertEqual(http_no_port.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            http_no_port.source_url,
            "https://www.kan.org.il/content/item/1",
        )

        http_80 = parse_video_url("http://www.kan.org.il:80/content/item/1?x=1#part")
        self.assertEqual(http_80.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            http_80.source_url,
            "https://www.kan.org.il/content/item/1?x=1#part",
        )

        https_443 = parse_video_url(
            "https://www.kan.org.il:443/content/item/1?x=1#part"
        )
        self.assertEqual(https_443.provider, VideoContent.Provider.KAN)
        self.assertEqual(
            https_443.source_url,
            "https://www.kan.org.il/content/item/1?x=1#part",
        )

    def test_kan_nonstandard_ports_rejected(self):
        for url in (
            "http://www.kan.org.il:8080/content/item/1",
            "https://www.kan.org.il:8443/content/item/1",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_video_url(url)
                self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_trailing_dot_hosts_normalize_for_provider_recognition(self):
        youtube = parse_video_url("https://www.youtube.com./watch?v=dQw4w9WgXcQ")
        self.assertEqual(youtube.provider, VideoContent.Provider.YOUTUBE)
        self.assertEqual(youtube.provider_video_id, "dQw4w9WgXcQ")
        self.assertEqual(
            youtube.source_url,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

        kan = parse_video_url("https://www.kan.org.il./content/item/1")
        self.assertEqual(kan.provider, VideoContent.Provider.KAN)
        self.assertEqual(kan.source_url, "https://www.kan.org.il/content/item/1")

    def test_userinfo_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://user:pass@example.com/video")
        self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_invalid_port_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://example.com:99999/video")
        self.assertEqual(str(ctx.exception), VIDEO_URL_INVALID_ERROR)

    def test_youtube_nonstandard_port_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://www.youtube.com:8443/watch?v=dQw4w9WgXcQ")
        self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_ipv6_literal_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://[2001:db8::1]/video")
        self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_malformed_url_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://")
        self.assertEqual(str(ctx.exception), VIDEO_URL_INVALID_ERROR)

    def test_unsupported_scheme_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("ftp://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

        with self.assertRaises(ValueError):
            parse_video_url("javascript:alert(1)")

    def test_missing_youtube_video_id_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://www.youtube.com/watch")
        self.assertEqual(str(ctx.exception), VIDEO_URL_INVALID_ERROR)

        with self.assertRaises(ValueError):
            parse_video_url("https://www.youtube.com/watch?v=")

    def test_invalid_youtube_video_id_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            parse_video_url("https://www.youtube.com/watch?v=short")
        self.assertEqual(str(ctx.exception), VIDEO_URL_INVALID_ERROR)

        with self.assertRaises(ValueError):
            parse_video_url("https://youtu.be/not valid!!")

    def test_playlist_channel_search_and_clip_rejected(self):
        unsupported = [
            "https://www.youtube.com/playlist?list=PLxxxxxxxx",
            "https://www.youtube.com/channel/UCxxxxxxxx",
            "https://www.youtube.com/@somechannel",
            "https://www.youtube.com/results?search_query=archive",
            "https://www.youtube.com/clip/UgkxInvalidClipId01",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxxxxxxxx",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
        ]
        for url in unsupported:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    parse_video_url(url)
                self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_iframe_or_html_input_rejected(self):
        html_inputs = [
            '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>',
            "<script>alert(1)</script>",
            'https://www.youtube.com/watch?v=dQw4w9WgXcQ"><iframe>',
        ]
        for raw in html_inputs:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError) as ctx:
                    parse_video_url(raw)
                self.assertEqual(str(ctx.exception), VIDEO_URL_UNSUPPORTED_ERROR)

    def test_other_http_rejected(self):
        with self.assertRaises(ValueError):
            parse_video_url("http://example.com/video")
