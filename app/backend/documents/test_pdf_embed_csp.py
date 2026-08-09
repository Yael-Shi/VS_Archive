from django.test import SimpleTestCase

from vs_archive import settings as project_settings


class PdfEmbedCspTests(SimpleTestCase):
    def test_frame_src_allows_configured_upload_bucket_origins(self):
        frame_src = project_settings.SECURE_CSP["frame-src"]
        bucket = project_settings.UPLOADS_BUCKET_NAME
        region = project_settings.AWS_REGION

        if not bucket:
            self.skipTest("UPLOADS_BUCKET_NAME is not configured")

        self.assertIn(
            f"https://{bucket}.s3.{region}.amazonaws.com",
            frame_src,
        )
        self.assertIn(
            f"https://{bucket}.s3.amazonaws.com",
            frame_src,
        )

    def test_frame_src_keeps_nocookie_without_broad_wildcards(self):
        frame_src = project_settings.SECURE_CSP["frame-src"]

        self.assertIn("https://www.youtube-nocookie.com", frame_src)
        self.assertNotIn("https:", frame_src)
        self.assertNotIn("https://*.amazonaws.com", frame_src)
        self.assertNotIn("*", frame_src)
