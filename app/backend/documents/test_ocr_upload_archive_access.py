"""Archive browse/detail access for non-finalized OCR documents."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from documents.models import ArchiveItem, Document
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
    archive_item_queryset_for_user,
)
from documents.services.archive_items import create_ocr_document


class OcrUploadArchiveAccessTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="ocr_access_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)

        self.uploading_doc = create_ocr_document(
            title="Uploading OCR doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADING,
            expected_source_file_count=None,
        )
        self.uploading_item = self.uploading_doc.archive_item

        self.uploaded_doc = create_ocr_document(
            title="Uploaded OCR doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/99/original.jpeg",
            mime_type="image/jpeg",
        )
        self.uploaded_item = self.uploaded_doc.archive_item

    def test_access_queryset_includes_uploading_public_ocr(self):
        qs = archive_item_queryset_for_user(self.family_user)
        self.assertTrue(qs.filter(pk=self.uploading_item.pk).exists())
        self.assertTrue(qs.filter(pk=self.uploaded_item.pk).exists())

    def test_browse_queryset_excludes_uploading_ocr(self):
        access_qs = archive_item_queryset_for_user(self.family_user)
        browse_qs = archive_browse_queryset_for_user(self.family_user)
        self.assertTrue(access_qs.filter(pk=self.uploading_item.pk).exists())
        self.assertFalse(browse_qs.filter(pk=self.uploading_item.pk).exists())

    def test_browse_queryset_includes_uploaded_ocr(self):
        browse_qs = archive_browse_queryset_for_user(self.family_user)
        self.assertTrue(browse_qs.filter(pk=self.uploaded_item.pk).exists())

    def test_document_detail_404_for_uploading_public_ocr(self):
        self.client.force_login(self.family_user)
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.uploading_doc.pk})
        )
        self.assertEqual(resp.status_code, 404)

    def test_anonymous_document_detail_404_for_uploading_public_ocr(self):
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.uploading_doc.pk})
        )
        self.assertEqual(resp.status_code, 404)

    def test_staff_can_view_uploading_ocr_document_detail(self):
        staff = User.objects.create_user(
            username="ocr_detail_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.uploading_doc.pk})
        )
        self.assertEqual(resp.status_code, 200)

    def test_archive_detail_404_for_uploading_public_ocr(self):
        self.client.force_login(self.family_user)
        url = reverse(
            "archive-detail",
            kwargs={"item_id": self.uploading_item.pk},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_archive_detail_allows_uploaded_public_ocr(self):
        self.client.force_login(self.family_user)
        url = reverse(
            "archive-detail",
            kwargs={"item_id": self.uploaded_item.pk},
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(
            resp["Location"],
            reverse("documents-detail-page", kwargs={"doc_id": self.uploaded_doc.pk}),
        )
