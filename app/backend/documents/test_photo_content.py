from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase

from documents.admin import PhotoContentAdmin
from documents.models import ArchiveItem, Document, PhotoContent


def _create_photo_archive_item(*, title: str = "Family photo") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _photo_content_defaults(**overrides) -> dict:
    values = {
        "original_file_key": "photos/1/original.jpg",
        "original_filename": "scan.jpg",
        "original_mime_type": "image/jpeg",
        "original_size_bytes": 2048,
        "upload_status": PhotoContent.UploadStatus.UPLOADED,
        "upload_error": "",
    }
    values.update(overrides)
    return values


class PhotoContentModelTests(TestCase):
    def test_photo_content_can_be_created_for_photo_archive_item(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.archive_item_id, archive_item.id)
        self.assertEqual(content.original_filename, "scan.jpg")
        self.assertEqual(archive_item.photo_content.id, content.id)

    def test_photo_content_cannot_validate_for_non_photo_archive_item(self):
        manual_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            title="Manual note",
        )
        content = PhotoContent(
            archive_item=manual_item,
            **_photo_content_defaults(),
        )
        with self.assertRaises(ValidationError) as ctx:
            content.full_clean()
        self.assertIn("archive_item", ctx.exception.message_dict)

    def test_one_to_one_prevents_multiple_photo_content_rows(self):
        archive_item = _create_photo_archive_item()
        PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        with self.assertRaises(IntegrityError):
            PhotoContent.objects.create(
                archive_item=archive_item,
                original_file_key="photos/1/original-duplicate.jpg",
                original_filename="duplicate.jpg",
                original_mime_type="image/jpeg",
                original_size_bytes=1024,
            )

    def test_deleting_archive_item_cascades_to_photo_content(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        content_id = content.id
        archive_item.delete()
        self.assertFalse(PhotoContent.objects.filter(pk=content_id).exists())

    def test_thumbnail_fields_are_optional_in_v1(self):
        archive_item = _create_photo_archive_item()
        content = PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertEqual(content.thumbnail_file_key, "")
        self.assertEqual(content.thumbnail_mime_type, "")
        self.assertIsNone(content.thumbnail_size_bytes)
        self.assertIsNone(content.width)
        self.assertIsNone(content.height)

    def test_no_document_is_required_for_photo_content(self):
        archive_item = _create_photo_archive_item()
        PhotoContent.objects.create(
            archive_item=archive_item,
            **_photo_content_defaults(),
        )
        self.assertFalse(Document.objects.filter(archive_item=archive_item).exists())
        self.assertEqual(Document.objects.count(), 0)

    def test_photo_content_admin_is_view_only(self):
        request = RequestFactory().get("/admin/")
        request.user = User.objects.create_superuser(
            username="photo_content_admin",
            password="test-pass",
            email="photo-admin@example.com",
        )
        site = AdminSite()
        admin = PhotoContentAdmin(PhotoContent, site)
        self.assertTrue(admin.has_view_permission(request))
        self.assertFalse(admin.has_add_permission(request))
        self.assertFalse(admin.has_change_permission(request))
        self.assertFalse(admin.has_delete_permission(request))
