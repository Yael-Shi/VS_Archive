from django.core.management import call_command
from django.test import Client, SimpleTestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["vs-archive.com"],
    CSRF_TRUSTED_ORIGINS=["https://vs-archive.com"],
    DEBUG=False,
    SECRET_KEY="test-static-files-only",
    WHITENOISE_USE_FINDERS=False,
)
class StaticFilesServingTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("collectstatic", interactive=False, verbosity=0)

    def test_public_app_css_is_served_in_production_mode(self):
        client = Client()
        resp = client.get("/static/public/app.css", HTTP_HOST="vs-archive.com")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            resp["Content-Type"].startswith("text/css"),
            msg=resp["Content-Type"],
        )
