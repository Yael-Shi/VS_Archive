from django.test import Client, SimpleTestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["vs-archive.com"],
    DEBUG=False,
)
class AlbHealthCheckMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.client = Client()

    def test_health_with_invalid_http_host_returns_200(self):
        resp = self.client.get("/health/", HTTP_HOST="10.0.1.42")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")

    def test_normal_path_with_invalid_http_host_returns_400(self):
        resp = self.client.get("/", HTTP_HOST="10.0.1.42")
        self.assertEqual(resp.status_code, 400)

    def test_normal_health_with_valid_http_host_returns_200(self):
        resp = self.client.get("/health/", HTTP_HOST="vs-archive.com")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")
