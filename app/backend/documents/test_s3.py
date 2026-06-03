from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from documents.s3 import s3_object_exists


class S3ObjectExistsTests(SimpleTestCase):
    def _client_error(self, code: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": "not found"}},
            "HeadObject",
        )

    @patch("documents.s3.get_s3_client")
    def test_returns_true_when_head_object_succeeds(self, mock_get_client):
        mock_get_client.return_value.head_object.return_value = {}

        self.assertTrue(s3_object_exists("bucket", "documents/1/original.jpg"))

        mock_get_client.return_value.head_object.assert_called_once_with(
            Bucket="bucket",
            Key="documents/1/original.jpg",
        )

    @patch("documents.s3.get_s3_client")
    def test_returns_false_for_not_found_codes(self, mock_get_client):
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                mock_get_client.return_value.head_object.side_effect = self._client_error(
                    code
                )
                self.assertFalse(s3_object_exists("bucket", "missing/key"))

    @patch("documents.s3.get_s3_client")
    def test_reraises_unexpected_client_errors(self, mock_get_client):
        mock_get_client.return_value.head_object.side_effect = self._client_error(
            "AccessDenied"
        )

        with self.assertRaises(ClientError):
            s3_object_exists("bucket", "documents/1/original.jpg")
