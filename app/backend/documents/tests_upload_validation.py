from django.test import SimpleTestCase

from documents.services.upload_validation import (
    validate_image_upload_metadata,
    validate_single_file_upload_metadata,
)


class UploadValidationTests(SimpleTestCase):
    def test_validate_image_accepts_jpeg_jpg(self):
        self.assertIsNone(
            validate_image_upload_metadata(
                mime_type="image/jpeg",
                original_name="scan.jpg",
            )
        )

    def test_validate_image_rejects_mime_extension_mismatch(self):
        err = validate_image_upload_metadata(
            mime_type="image/jpeg",
            original_name="scan.png",
        )
        self.assertIsNotNone(err)
        self.assertIn("does not match", err)

    def test_validate_single_pdf_accepts_matching_metadata(self):
        self.assertIsNone(
            validate_single_file_upload_metadata(
                doc_type="PDF",
                mime_type="application/pdf",
                original_name="doc.pdf",
            )
        )
