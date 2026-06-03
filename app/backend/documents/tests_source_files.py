from django.test import TestCase

from documents.models import Document, DocumentSourceFile
from documents.services.source_files import (
    MULTI_IMAGE_MIN_FILES,
    MultiImageSourceFilesError,
    get_ordered_source_files_for_processing,
)


class GetOrderedSourceFilesForProcessingTests(TestCase):
    def _make_doc(self, expected_count: int = 2) -> Document:
        return Document.objects.create(
            title="Multi-image doc",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ENGLISH,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            expected_source_file_count=expected_count,
        )

    def _add_source(
        self,
        doc: Document,
        order_index: int,
        *,
        mime_type: str = "image/png",
        file_original_name: str | None = None,
    ) -> DocumentSourceFile:
        return DocumentSourceFile.objects.create(
            document=doc,
            order_index=order_index,
            file_s3_key=f"documents/{doc.id}/source/{order_index}.png",
            file_original_name=file_original_name
            if file_original_name is not None
            else f"page-{order_index}.png",
            mime_type=mime_type,
            size_bytes=100,
            upload_status=DocumentSourceFile.UploadStatus.UPLOADED,
        )

    def test_accepts_allowed_mime_matching_extension(self):
        doc = self._make_doc()
        self._add_source(doc, 0, mime_type="image/jpeg", file_original_name="page-0.jpg")
        self._add_source(doc, 1, mime_type="image/png", file_original_name="page-1.png")

        ordered = get_ordered_source_files_for_processing(doc)
        self.assertEqual([s.order_index for s in ordered], [0, 1])

    def test_rejects_non_allowlisted_mime(self):
        doc = self._make_doc()
        self._add_source(doc, 0)
        self._add_source(doc, 1, mime_type="application/pdf", file_original_name="page-1.pdf")

        with self.assertRaises(MultiImageSourceFilesError) as ctx:
            get_ordered_source_files_for_processing(doc)
        self.assertIn("must be one of", str(ctx.exception))

    def test_rejects_mime_extension_mismatch(self):
        doc = self._make_doc()
        self._add_source(doc, 0)
        self._add_source(
            doc,
            1,
            mime_type="image/jpeg",
            file_original_name="page-1.png",
        )

        with self.assertRaises(MultiImageSourceFilesError) as ctx:
            get_ordered_source_files_for_processing(doc)
        self.assertIn("does not match", str(ctx.exception))

    def test_mime_only_fallback_when_original_name_missing(self):
        doc = self._make_doc()
        self._add_source(doc, 0, mime_type="image/png", file_original_name="")
        self._add_source(doc, 1, mime_type="image/png", file_original_name="")

        ordered = get_ordered_source_files_for_processing(doc)
        self.assertEqual(len(ordered), MULTI_IMAGE_MIN_FILES)

    def test_mime_only_fallback_rejects_non_allowlisted_mime(self):
        doc = self._make_doc()
        self._add_source(doc, 0, mime_type="image/png", file_original_name="")
        self._add_source(doc, 1, mime_type="image/gif", file_original_name="")

        with self.assertRaises(MultiImageSourceFilesError):
            get_ordered_source_files_for_processing(doc)
