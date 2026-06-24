from unittest.mock import patch

from django.test import Client, TestCase, override_settings


@override_settings(
    ALLOWED_HOSTS=["vs-archive.com"],
    DEBUG=False,
)
class ReadyEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_ready_returns_200_when_database_is_reachable(self):
        resp = self.client.get("/ready/", HTTP_HOST="vs-archive.com")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"ok")

    @patch("vs_archive.urls.connection")
    def test_ready_returns_503_when_database_check_fails(self, mock_connection):
        mock_connection.cursor.side_effect = Exception("database unavailable")
        resp = self.client.get("/ready/", HTTP_HOST="vs-archive.com")
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.content, b"unavailable")
