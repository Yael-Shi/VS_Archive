from unittest.mock import patch

from botocore.exceptions import ClientError
from django.test import SimpleTestCase

from documents.s3 import S3HeadObjectResult, head_s3_object, s3_object_exists
from documents.services.upload_validation import (
    normalize_upload_mime_type,
    upload_mime_types_match,
)


class S3HeadObjectTests(SimpleTestCase):
    def _client_error(self, code: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": "not found"}},
            "HeadObject",
        )

    @patch("documents.s3.get_s3_client")
    def test_returns_exists_with_content_type(self, mock_get_client):
        mock_get_client.return_value.head_object.return_value = {
            "ContentType": "image/jpeg; charset=binary",
        }

        result = head_s3_object("bucket", "documents/1/original.jpg")

        self.assertTrue(result.exists)
        self.assertEqual(result.content_type, "image/jpeg; charset=binary")
        mock_get_client.return_value.head_object.assert_called_once_with(
            Bucket="bucket",
            Key="documents/1/original.jpg",
        )

    @patch("documents.s3.get_s3_client")
    def test_empty_content_type_becomes_none(self, mock_get_client):
        mock_get_client.return_value.head_object.return_value = {"ContentType": "   "}

        result = head_s3_object("bucket", "key")

        self.assertTrue(result.exists)
        self.assertIsNone(result.content_type)

    @patch("documents.s3.get_s3_client")
    def test_returns_not_exists_for_not_found_codes(self, mock_get_client):
        for code in ("404", "NoSuchKey", "NotFound"):
            with self.subTest(code=code):
                mock_get_client.return_value.head_object.side_effect = self._client_error(
                    code
                )
                result = head_s3_object("bucket", "missing/key")
                self.assertFalse(result.exists)
                self.assertIsNone(result.content_type)

    @patch("documents.s3.get_s3_client")
    def test_reraises_unexpected_client_errors(self, mock_get_client):
        mock_get_client.return_value.head_object.side_effect = self._client_error(
            "AccessDenied"
        )

        with self.assertRaises(ClientError):
            head_s3_object("bucket", "documents/1/original.jpg")


class S3ObjectExistsTests(SimpleTestCase):
    def _client_error(self, code: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": "not found"}},
            "HeadObject",
        )

    @patch("documents.s3.head_s3_object")
    def test_returns_true_when_head_reports_exists(self, mock_head):
        mock_head.return_value = S3HeadObjectResult(exists=True, content_type="image/jpeg")

        self.assertTrue(s3_object_exists("bucket", "documents/1/original.jpg"))

        mock_head.assert_called_once_with("bucket", "documents/1/original.jpg")

    @patch("documents.s3.head_s3_object")
    def test_returns_false_when_head_reports_missing(self, mock_head):
        mock_head.return_value = S3HeadObjectResult(exists=False)

        self.assertFalse(s3_object_exists("bucket", "missing/key"))


class UploadMimeNormalizationTests(SimpleTestCase):
    def test_strips_parameters_and_lowercases(self):
        self.assertEqual(
            normalize_upload_mime_type("IMAGE/JPEG; charset=binary"),
            "image/jpeg",
        )

    def test_maps_jpg_and_pjpeg_aliases(self):
        self.assertEqual(normalize_upload_mime_type("image/jpg"), "image/jpeg")
        self.assertEqual(normalize_upload_mime_type("image/pjpeg"), "image/jpeg")

    def test_match_accepts_aliases_and_parameters(self):
        self.assertTrue(upload_mime_types_match("image/jpeg", "image/jpg"))
        self.assertTrue(
            upload_mime_types_match("image/jpeg", "image/pjpeg; charset=binary")
        )
        self.assertFalse(upload_mime_types_match("image/jpeg", "image/png"))
        self.assertFalse(
            upload_mime_types_match("image/jpeg", "application/octet-stream")
        )
