"""Public VIDEO browse/detail/security presentation tests (PR3)."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from documents.models import ArchiveItem, ManualTextContent, PhotoContent, VideoContent
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
)
from documents.services.archive_items import create_video_archive_item
from documents.services.video_presentation import (
    YOUTUBE_NOCOOKIE_EMBED_ORIGIN,
    _normalized_youtube_times,
    _youtube_embed_src_is_approved,
    build_video_public_presentation,
    build_youtube_nocookie_embed_src,
)
from documents.services.video_url import parse_video_url
from documents.services.video_url_contract import (
    PROVIDER_KAN,
    PROVIDER_OTHER,
    PROVIDER_YOUTUBE,
    video_provider_display_label,
)
from documents.services.video_validation import (
    video_provider_display_label as validation_provider_label,
)

User = get_user_model()

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
YOUTUBE_URL_TIMES = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12"
KAN_URL = "https://www.kan.org.il/content/kan/item/999/"
OTHER_URL = "https://example.com/videos/clip-1"


def _extract_click_to_load_script(html: str) -> str:
    """Return the inline click-to-load script body from a rendered detail page."""
    marker = 'document.querySelector("[data-video-embed]")'
    start = html.find(marker)
    if start == -1:
        raise AssertionError("click-to-load script not found in rendered HTML")
    script_open = html.rfind("<script>", 0, start)
    script_close = html.find("</script>", start)
    if script_open == -1 or script_close == -1:
        raise AssertionError("click-to-load script tags not found")
    return html[script_open + len("<script>") : script_close]


def _eval_is_approved_embed_src_with_node(
    script: str, urls: list[str]
) -> dict[str, bool] | None:
    """Execute rendered ``isApprovedEmbedSrc`` in Node; return None if Node missing."""
    if shutil.which("node") is None:
        return None
    # Isolate validator helpers from DOM wiring / activateEmbed.
    helpers_start = script.find("var ALLOWED_EMBED_PREFIX")
    helpers_end = script.find("function activateEmbed")
    if helpers_start == -1 or helpers_end == -1:
        raise AssertionError("validator helpers not found in click-to-load script")
    helpers = script[helpers_start:helpers_end]
    payload = json.dumps(urls)
    node_program = (
        helpers
        + "\n"
        + "var urls = "
        + payload
        + ";\n"
        + "var out = {};\n"
        + "for (var i = 0; i < urls.length; i++) {\n"
        + "  out[urls[i]] = isApprovedEmbedSrc(urls[i]);\n"
        + "}\n"
        + "process.stdout.write(JSON.stringify(out));\n"
    )
    completed = subprocess.run(
        ["node", "-e", node_program],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "node failed to evaluate isApprovedEmbedSrc: "
            f"{completed.stderr or completed.stdout}"
        )
    parsed: Any = json.loads(completed.stdout)
    if not isinstance(parsed, dict):
        raise AssertionError("unexpected node evaluator payload")
    return {str(k): bool(v) for k, v in parsed.items()}


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    return User.objects.get(pk=user.pk)


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoPublicPresentationHelperTests(TestCase):
    def test_provider_display_label_is_canonical_across_modules(self):
        self.assertEqual(video_provider_display_label(PROVIDER_YOUTUBE), "YouTube")
        self.assertEqual(video_provider_display_label(PROVIDER_KAN), "כאן")
        self.assertEqual(video_provider_display_label(PROVIDER_OTHER), "אתר חיצוני")
        self.assertEqual(video_provider_display_label(""), "")
        self.assertEqual(video_provider_display_label(None), "")
        self.assertEqual(video_provider_display_label("UNKNOWN"), "אתר חיצוני")
        # Management re-exports the same canonical helper.
        self.assertIs(validation_provider_label, video_provider_display_label)

    def test_youtube_embed_src_uses_nocookie_and_omits_autoplay(self):
        src = build_youtube_nocookie_embed_src(
            "dQw4w9WgXcQ",
            start_seconds=12,
            end_seconds=90,
        )
        self.assertIsNotNone(src)
        assert src is not None
        self.assertTrue(src.startswith(f"{YOUTUBE_NOCOOKIE_EMBED_ORIGIN}/embed/"))
        self.assertIn("start=12", src)
        self.assertIn("end=90", src)
        self.assertNotIn("autoplay", src.lower())
        self.assertIsNone(build_youtube_nocookie_embed_src("<script>"))
        self.assertIsNone(build_youtube_nocookie_embed_src("short"))

    def test_youtube_time_contract_is_shared_and_rejects_bool(self):
        self.assertEqual(_normalized_youtube_times(None, None), (None, None))
        self.assertEqual(_normalized_youtube_times(12, 90), (12, 90))
        self.assertEqual(_normalized_youtube_times(None, 5), (None, 5))
        self.assertIsNone(_normalized_youtube_times(-1, None))
        self.assertIsNone(_normalized_youtube_times(10, 10))
        self.assertIsNone(_normalized_youtube_times(10, 5))
        self.assertIsNone(_normalized_youtube_times(None, 0))
        # bool must not count as int (shared by embed builder + presentation).
        self.assertIsNone(_normalized_youtube_times(True, None))
        self.assertIsNone(
            build_youtube_nocookie_embed_src(
                "dQw4w9WgXcQ",
                start_seconds=True,
            )
        )
        self.assertIsNone(
            build_youtube_nocookie_embed_src(
                "dQw4w9WgXcQ",
                start_seconds=10,
                end_seconds=5,
            )
        )

    def test_youtube_embed_src_defense_in_depth_rejects_deceptive_urls(self):
        video_id = "dQw4w9WgXcQ"
        valid = build_youtube_nocookie_embed_src(
            video_id, start_seconds=12, end_seconds=90
        )
        self.assertIsNotNone(valid)
        assert valid is not None
        self.assertTrue(
            _youtube_embed_src_is_approved(
                valid,
                video_id=video_id,
                start_seconds=12,
                end_seconds=90,
            )
        )
        # Deceptive prefix / unexpected host / port / path / query.
        self.assertFalse(
            _youtube_embed_src_is_approved(
                "https://www.youtube-nocookie.com.evil.example/embed/" + video_id,
                video_id=video_id,
                start_seconds=None,
                end_seconds=None,
            )
        )
        self.assertFalse(
            _youtube_embed_src_is_approved(
                f"https://www.youtube-nocookie.com:8443/embed/{video_id}?playsinline=1",
                video_id=video_id,
                start_seconds=None,
                end_seconds=None,
            )
        )
        self.assertFalse(
            _youtube_embed_src_is_approved(
                f"https://www.youtube-nocookie.com/embed/{video_id}/extra?playsinline=1",
                video_id=video_id,
                start_seconds=None,
                end_seconds=None,
            )
        )
        self.assertFalse(
            _youtube_embed_src_is_approved(
                f"https://www.youtube-nocookie.com/embed/{video_id}?playsinline=1&autoplay=1",
                video_id=video_id,
                start_seconds=None,
                end_seconds=None,
            )
        )
        self.assertFalse(
            _youtube_embed_src_is_approved(
                f"https://user:pass@www.youtube-nocookie.com/embed/{video_id}?playsinline=1",
                video_id=video_id,
                start_seconds=None,
                end_seconds=None,
            )
        )

    def test_build_presentation_fails_closed_without_content(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Broken video",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.assertIsNone(build_video_public_presentation(item))

    def test_youtube_explicit_start_end_override_still_allowed(self):
        item = create_video_archive_item(
            title="Override times video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            start_seconds=30,
            end_seconds=90,
        )
        presentation = build_video_public_presentation(item)
        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertTrue(presentation.is_youtube_embed)
        self.assertEqual(presentation.start_seconds, 30)
        self.assertEqual(presentation.end_seconds, 90)
        self.assertIn("start=30", presentation.youtube_embed_src or "")
        self.assertIn("end=90", presentation.youtube_embed_src or "")


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoPublicSemanticRevalidationTests(TestCase):
    """Render-time revalidation: DB-valid but inconsistent rows fail closed."""

    def test_youtube_row_with_unrelated_https_source_url_fails_closed(self):
        item = create_video_archive_item(
            title="YouTube inconsistent URL",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        VideoContent.objects.filter(archive_item=item).update(
            source_url="https://example.com/unrelated-video"
        )
        item = ArchiveItem.objects.select_related("video_content").get(pk=item.pk)
        self.assertIsNone(build_video_public_presentation(item))
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 404)

    def test_kan_row_with_other_https_url_fails_closed(self):
        item = create_video_archive_item(
            title="KAN inconsistent URL",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        VideoContent.objects.filter(archive_item=item).update(
            source_url="https://example.com/not-kan"
        )
        item = ArchiveItem.objects.select_related("video_content").get(pk=item.pk)
        self.assertEqual(item.video_content.provider, VideoContent.Provider.KAN)
        self.assertIsNone(build_video_public_presentation(item))
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 404)

    def test_other_row_with_http_url_fails_closed(self):
        item = create_video_archive_item(
            title="OTHER http URL",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        VideoContent.objects.filter(archive_item=item).update(
            source_url="http://example.com/videos/clip-1"
        )
        item = ArchiveItem.objects.select_related("video_content").get(pk=item.pk)
        self.assertIsNone(build_video_public_presentation(item))
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 404)

    def test_non_canonical_stored_url_fails_closed(self):
        item = create_video_archive_item(
            title="Non-canonical YouTube URL",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        non_canonical = "https://youtu.be/dQw4w9WgXcQ"
        canonical = parse_video_url(non_canonical).source_url
        self.assertNotEqual(non_canonical, canonical)
        VideoContent.objects.filter(archive_item=item).update(source_url=non_canonical)
        item = ArchiveItem.objects.select_related("video_content").get(pk=item.pk)
        self.assertEqual(item.video_content.source_url, non_canonical)
        self.assertIsNone(build_video_public_presentation(item))
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 404)

    def test_valid_youtube_kan_other_still_present(self):
        youtube = create_video_archive_item(
            title="Valid YouTube semantic",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        kan = create_video_archive_item(
            title="Valid KAN semantic",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        other = create_video_archive_item(
            title="Valid OTHER semantic",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        yt_p = build_video_public_presentation(youtube)
        kan_p = build_video_public_presentation(kan)
        other_p = build_video_public_presentation(other)
        self.assertIsNotNone(yt_p)
        self.assertIsNotNone(kan_p)
        self.assertIsNotNone(other_p)
        assert yt_p is not None and kan_p is not None and other_p is not None
        self.assertTrue(yt_p.is_youtube_embed)
        self.assertFalse(kan_p.is_youtube_embed)
        self.assertFalse(other_p.is_youtube_embed)
        self.assertEqual(yt_p.source_url, parse_video_url(YOUTUBE_URL).source_url)
        self.assertEqual(kan_p.source_url, parse_video_url(KAN_URL).source_url)
        self.assertEqual(other_p.source_url, parse_video_url(OTHER_URL).source_url)
        self.assertTrue(other_p.source_url.startswith("https://"))


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoPublicBrowseAndFilterTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.staff = User.objects.create_user(
            username="video_public_staff",
            password="x",
            is_staff=True,
        )
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="video_family",
            password="x",
        )
        self.family_user.groups.add(self.family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(
                username="video_restricted_viewer",
                password="x",
                is_staff=True,
            )
        )

    def test_type_filter_includes_videos_label(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הכל")
        self.assertContains(resp, "מסמכים וטקסטים")
        self.assertContains(resp, "תמונות")
        self.assertContains(resp, "סרטונים")
        self.assertContains(resp, "?item_type=video")

    def test_public_video_appears_in_browse_and_links_to_detail(self):
        item = create_video_archive_item(
            title="Public browse video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            public_note="Local note only",
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Public browse video")
        self.assertContains(resp, "סרטון")
        self.assertContains(resp, "YouTube")
        detail_href = reverse("archive-detail", kwargs={"item_id": item.id})
        self.assertContains(resp, f'href="{detail_href}"')
        self.assertNotContains(resp, "i.ytimg.com")
        self.assertNotContains(resp, "img.youtube.com")
        self.assertNotContains(resp, "youtube.com/vi/")
        self.assertNotContains(resp, "<iframe", html=False)

    def test_private_video_excluded_anonymously(self):
        create_video_archive_item(
            title="Private browse video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        resp = self.client.get(reverse("archive-list"))
        self.assertNotContains(resp, "Private browse video")

    def test_restricted_video_visibility_follows_access_helpers(self):
        item = create_video_archive_item(
            title="Restricted browse video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.restricted_user,
        )
        anon = self.client.get(reverse("archive-list"))
        self.assertNotContains(anon, "Restricted browse video")

        self.client.force_login(self.staff)
        staff_resp = self.client.get(reverse("archive-list"))
        self.assertNotContains(staff_resp, "Restricted browse video")

        self.client.force_login(self.restricted_user)
        allowed = self.client.get(reverse("archive-list"))
        self.assertContains(allowed, "Restricted browse video")
        detail = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(detail.status_code, 200)

    def test_videos_filter_includes_only_video(self):
        video = create_video_archive_item(
            title="Filter only video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        manual = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Filter only manual",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ManualTextContent.objects.create(archive_item=manual, body="body")
        photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Filter only photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=photo,
            original_file_key="photos/filter/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )

        resp = self.client.get(reverse("archive-list"), {"item_type": "video"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, video.title)
        self.assertNotContains(resp, manual.title)
        self.assertNotContains(resp, photo.title)

        docs = self.client.get(
            reverse("archive-list"), {"item_type": "documents_and_texts"}
        )
        self.assertContains(docs, manual.title)
        self.assertNotContains(docs, video.title)

        photos = self.client.get(reverse("archive-list"), {"item_type": "photo"})
        self.assertContains(photos, photo.title)
        self.assertNotContains(photos, video.title)

    def test_search_finds_video_by_title_metadata_only(self):
        create_video_archive_item(
            title="SearchableVideoTitleXYZ",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            author_name="Video Author Meta",
        )
        resp = self.client.get(
            reverse("archive-list"), {"q": "SearchableVideoTitleXYZ"}
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "SearchableVideoTitleXYZ")
        # Provider id / raw URL must not be required for search hits.
        self.assertNotContains(resp, "dQw4w9WgXcQ")


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoPublicDetailTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(
                username="video_detail_restricted",
                password="x",
                is_staff=True,
            )
        )

    def test_youtube_detail_placeholder_without_iframe_or_media_request(self):
        item = create_video_archive_item(
            title="YouTube detail video",
            source_url=YOUTUBE_URL_TIMES,
            visibility=ArchiveItem.Visibility.PUBLIC,
            start_seconds=12,
            end_seconds=120,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertNotIn("<iframe", html.lower())
        self.assertNotIn("autoplay=1", html.lower())
        self.assertNotIn("autoplay", html.lower().split("data-embed-src")[0])
        self.assertNotIn("i.ytimg.com", html)
        self.assertNotIn("img.youtube.com", html)
        self.assertContains(resp, "הפעלת הסרטון")
        self.assertContains(resp, "פתיחה ב־YouTube")
        self.assertContains(resp, "data-video-embed")
        self.assertContains(resp, "youtube-nocookie.com/embed/dQw4w9WgXcQ")
        self.assertContains(resp, "start=12")
        self.assertContains(resp, "end=120")
        self.assertContains(
            resp, 'data-iframe-title="נגן YouTube: YouTube detail video"'
        )
        # Fallback uses normalized source URL, not an arbitrary iframe HTML blob.
        self.assertContains(resp, item.video_content.source_url)
        self.assertContains(resp, "youtube-nocookie.com")

        # One unified media wrapper contains placeholder + hidden player.
        facade_start = html.find('class="video-embed-facade"')
        self.assertNotEqual(facade_start, -1)
        section_end = html.find("</section>", facade_start)
        self.assertNotEqual(section_end, -1)
        section_html = html[facade_start:section_end]
        media_start = section_html.find('class="video-embed-facade__media"')
        self.assertNotEqual(media_start, -1)
        fallback_start = section_html.find("archive-detail-video__fallback")
        self.assertNotEqual(fallback_start, -1)
        media_region = section_html[media_start:fallback_start]
        self.assertIn("data-video-placeholder", media_region)
        self.assertIn("data-video-player", media_region)
        self.assertIn("data-video-player hidden", media_region)
        # Exactly one player region, inside the unified media wrapper only.
        self.assertEqual(section_html.count("data-video-player"), 1)
        self.assertEqual(section_html.count("video-embed-facade__player"), 1)
        self.assertEqual(section_html.count("video-embed-facade__media"), 1)
        self.assertNotIn("data-video-player", section_html[fallback_start:])

        # Placeholder facade includes the ArchiveItem title and activation control.
        start = html.find("data-video-placeholder")
        self.assertNotEqual(start, -1)
        end = html.find("data-video-player", start)
        self.assertNotEqual(end, -1)
        placeholder_region = html[start:end]
        self.assertIn("YouTube detail video", placeholder_region)
        self.assertIn("data-video-activate", placeholder_region)
        self.assertIn("הפעלת הסרטון", placeholder_region)
        self.assertIn('class="video-embed-facade__title"', placeholder_region)
        self.assertIn("video-embed-facade__explainer", placeholder_region)
        self.assertIn("video-embed-facade__play", placeholder_region)

    def test_youtube_placeholder_title_escapes_html_like_metadata(self):
        dangerous = 'Clip <script>alert(1)</script> & "quote"'
        item = create_video_archive_item(
            title=dangerous,
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        start = html.find("data-video-placeholder")
        end = html.find("data-video-player", start)
        placeholder_region = html[start:end]
        self.assertIn("Clip &lt;script&gt;alert(1)&lt;/script&gt;", placeholder_region)
        self.assertIn("&amp;", placeholder_region)
        self.assertNotIn("<script>alert(1)</script>", placeholder_region)
        self.assertNotIn("<iframe", html.lower())
        self.assertNotIn("i.ytimg.com", html)
        self.assertContains(resp, "הפעלת הסרטון")

    def test_kan_and_other_are_external_link_only(self):
        kan = create_video_archive_item(
            title="KAN detail video",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        other = create_video_archive_item(
            title="OTHER detail video",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        kan_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": kan.id})
        )
        other_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": other.id})
        )
        self.assertEqual(kan_resp.status_code, 200)
        self.assertEqual(other_resp.status_code, 200)
        for resp in (kan_resp, other_resp):
            html = resp.content.decode("utf-8")
            self.assertNotIn("<iframe", html.lower())
            self.assertNotIn("data-video-embed", html)
            self.assertNotIn("data-video-activate", html)
            self.assertNotIn("הפעלת הסרטון", html)

        self.assertContains(kan_resp, "צפייה בסרטון באתר כאן")
        self.assertContains(kan_resp, kan.video_content.source_url)
        self.assertContains(other_resp, "צפייה בסרטון באתר המקורי")
        self.assertContains(other_resp, other.video_content.source_url)
        self.assertContains(other_resp, "example.com")

    def test_missing_video_content_fails_closed(self):
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Missing content video",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 404)

    def test_malformed_youtube_content_fails_closed(self):
        self.assertIsNone(build_youtube_nocookie_embed_src("bad"))
        self.assertIsNone(
            build_youtube_nocookie_embed_src(
                "dQw4w9WgXcQ",
                start_seconds=-1,
            )
        )
        self.assertIsNone(
            build_youtube_nocookie_embed_src(
                "dQw4w9WgXcQ",
                start_seconds=10,
                end_seconds=5,
            )
        )

        # Inconsistent provider/mode shape is rejected by presentation helper.
        item = create_video_archive_item(
            title="Other external for shape check",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        presentation = build_video_public_presentation(item)
        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertFalse(presentation.is_youtube_embed)
        self.assertIsNone(presentation.youtube_embed_src)

    def test_private_and_restricted_detail_access(self):
        private = create_video_archive_item(
            title="Private detail video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted = create_video_archive_item(
            title="Restricted detail video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            user=self.restricted_user,
        )
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": private.id})
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": restricted.id})
            ).status_code,
            404,
        )
        self.client.force_login(self.restricted_user)
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": restricted.id})
            ).status_code,
            200,
        )


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoSecurityHeaderTests(TestCase):
    def test_public_responses_include_csp_and_referrer_policy(self):
        item = create_video_archive_item(
            title="Header video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        list_resp = self.client.get(reverse("archive-list"))
        detail_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id})
        )
        about_resp = self.client.get(reverse("public-about"))

        for resp in (list_resp, detail_resp, about_resp):
            csp = resp.get("Content-Security-Policy", "")
            self.assertIn("frame-src", csp)
            self.assertIn("https://www.youtube-nocookie.com", csp)
            self.assertIn("'self'", csp)
            self.assertNotIn("https://www.youtube.com", csp)
            self.assertNotIn("*", csp.split("frame-src")[-1].split(";")[0])
            # Smallest CSP: do not weaken other directives by inventing open ones.
            self.assertNotIn("default-src", csp)
            self.assertNotIn("script-src", csp)
            self.assertNotIn("object-src", csp)
            self.assertEqual(resp.get("Referrer-Policy"), "same-origin")

    def test_click_to_load_script_sets_iframe_referrer_policy_strict_origin(self):
        item = create_video_archive_item(
            title="Referrer iframe video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertNotIn("<iframe", html.lower())
        self.assertIn(
            'iframe.referrerPolicy = "strict-origin-when-cross-origin"',
            html,
        )
        self.assertNotIn('iframe.referrerPolicy = "no-referrer"', html)
        self.assertNotIn('referrerPolicy = "no-referrer"', html)
        self.assertNotIn('referrerpolicy="no-referrer"', html.lower())
        self.assertIn('rel="noopener noreferrer"', html)
        self.assertNotIn("autoplay", html.lower().split("data-embed-src")[0])
        self.assertNotIn("autoplay=1", html.lower())
        # Script contract: hide facade, reveal player, one-time activation.
        self.assertIn("placeholder.hidden = true", html)
        self.assertIn("player.hidden = false", html)
        self.assertIn('data-video-activated", "1"', html)
        self.assertIn("EMBED_PATH_RE = /^\\/embed\\/[A-Za-z0-9_-]{11}$/", html)
        # Site-wide header remains same-origin (origin-only for iframe Referer).
        self.assertEqual(resp.get("Referrer-Policy"), "same-origin")
        csp = resp.get("Content-Security-Policy", "")
        self.assertIn("https://www.youtube-nocookie.com", csp)
        self.assertNotIn("https://www.youtube.com", csp)

    def test_click_to_load_script_keeps_iframe_keyboard_reachable(self):
        item = create_video_archive_item(
            title="Keyboard focus video",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        script = _extract_click_to_load_script(html)
        self.assertNotIn('tabindex", "-1"', script)
        self.assertNotIn("tabindex='-1'", script)
        self.assertNotIn('tabindex="-1"', script)
        self.assertNotIn('setAttribute("tabindex", "-1")', script)
        # Native iframe focusability: focus after insert; no tabindex=-1 removal
        # from sequential Tab order. Explicit tabindex=0 is optional and unused.
        self.assertIn("iframe.focus()", script)
        self.assertNotIn('tabindex", "0"', script)
        # Disable only after successful iframe creation/insertion.
        create_idx = script.find('document.createElement("iframe")')
        append_idx = script.find("player.appendChild(iframe)")
        disable_idx = script.find('activate.setAttribute("disabled", "")')
        focus_idx = script.find("iframe.focus()")
        self.assertNotEqual(create_idx, -1)
        self.assertNotEqual(append_idx, -1)
        self.assertNotEqual(disable_idx, -1)
        self.assertNotEqual(focus_idx, -1)
        self.assertLess(create_idx, append_idx)
        self.assertLess(append_idx, disable_idx)
        self.assertLess(disable_idx, focus_idx)
        # Early return on invalid src happens before createElement/disable.
        reject_idx = script.find("if (!isApprovedEmbedSrc(src))")
        self.assertNotEqual(reject_idx, -1)
        self.assertLess(reject_idx, create_idx)

    def test_click_to_load_script_query_allowlist_contract(self):
        item = create_video_archive_item(
            title="Query allowlist video",
            source_url=YOUTUBE_URL_TIMES,
            visibility=ArchiveItem.Visibility.PUBLIC,
            start_seconds=12,
            end_seconds=120,
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        script = _extract_click_to_load_script(html)
        # Allowlist matches server-emittable keys only.
        self.assertIn("ALLOWED_QUERY_KEYS", script)
        self.assertIn("playsinline: true", script)
        self.assertIn("start: true", script)
        self.assertIn("end: true", script)
        self.assertIn("function isApprovedEmbedQuery", script)
        self.assertIn("function queryHasDuplicateKeys", script)
        self.assertIn("CANONICAL_NONNEG_INT_RE", script)
        self.assertIn("CANONICAL_POS_INT_RE", script)
        # Empty query allowed; playsinline must be exactly "1" when present.
        self.assertIn("if (!search)", script)
        self.assertIn('playsinlineRaw !== "1"', script)
        # Time ordering and integer shape.
        self.assertIn("parseInt(endRaw, 10) <= parseInt(startRaw, 10)", script)
        self.assertIn("/^(0|[1-9]\\d*)$/", script)
        self.assertIn("/^[1-9]\\d*$/", script)
        # Unknown keys / autoplay rejected via allowlist (not a special-case only).
        self.assertIn("if (!ALLOWED_QUERY_KEYS[key])", script)
        self.assertIn("queryHasDuplicateKeys(search)", script)
        # Path/port/hash defenses remain.
        self.assertIn("EMBED_PATH_RE", script)
        self.assertIn('parsed.port !== "443"', script)
        self.assertIn("if (parsed.hash)", script)
        # Server-built data-embed-src still uses approved params (no autoplay).
        self.assertIn("playsinline=1", html)
        self.assertIn("start=12", html)
        self.assertIn("end=120", html)
        self.assertNotIn("autoplay", html.lower().split("<script>")[0])

        # Execute the rendered validator in Node when available (no JS framework).
        cases = [
            ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", True),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?playsinline=1",
                True,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&start=12&end=120",
                True,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&start=0",
                True,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&foo=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&playsinline=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&start=12a",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&start=01",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&start=20&end=10",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&autoplay=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
                "?playsinline=1&Autoplay=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?playsinline=1#t=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ/extra"
                "?playsinline=1",
                False,
            ),
            (
                "https://www.youtube-nocookie.com:8443/embed/dQw4w9WgXcQ?playsinline=1",
                False,
            ),
        ]
        node_results = _eval_is_approved_embed_src_with_node(
            script, [c[0] for c in cases]
        )
        if node_results is None:
            self.skipTest(
                "node is required to execute click-to-load query allowlist cases"
            )
        for src, expected in cases:
            self.assertEqual(
                node_results[src],
                expected,
                msg=f"isApprovedEmbedSrc({src!r}) expected {expected}",
            )

    def test_external_provider_links_retain_noreferrer(self):
        kan = create_video_archive_item(
            title="KAN noreferrer",
            source_url=KAN_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        other = create_video_archive_item(
            title="OTHER noreferrer",
            source_url=OTHER_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for item in (kan, other):
            resp = self.client.get(
                reverse("archive-detail", kwargs={"item_id": item.id})
            )
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, 'rel="noopener noreferrer"')
            self.assertNotContains(resp, "<iframe")

    def test_about_page_includes_video_privacy_copy(self):
        resp = self.client.get(reverse("public-about"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "נגן YouTube נטען רק לאחר לחיצה")
        self.assertContains(resp, "youtube-nocookie.com")
        self.assertContains(resp, "VS-Archive אינו מארח")


@override_settings(ALLOWED_HOSTS=["testserver", "localhost", "127.0.0.1"])
class VideoManageListPublicDetailLinkTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="video_manage_pr3_staff",
            password="x",
            is_staff=True,
        )

    def test_manage_list_video_title_links_to_public_detail(self):
        item = create_video_archive_item(
            title="Manage list public link",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        detail_href = reverse("archive-detail", kwargs={"item_id": item.id})
        edit_href = reverse("archive-manage-edit", kwargs={"item_id": item.id})
        delete_href = reverse("archive-manage-delete", kwargs={"item_id": item.id})
        self.assertContains(
            resp,
            f'<a href="{detail_href}">Manage list public link</a>',
            html=True,
        )
        self.assertContains(resp, f'href="{edit_href}"')
        self.assertContains(resp, f'href="{delete_href}"')
