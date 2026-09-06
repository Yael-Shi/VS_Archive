"""PR1 multi-author data foundation: models, backfill, dual-write, isolation."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from documents.admin import ArchiveItemAdmin
from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Author,
    Document,
    ManualTextContent,
    Person,
    PhotoContent,
    PhotoPerson,
    VideoContent,
)
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_authors import (
    AMBIGUOUS_AUTHOR_ERROR,
    ArchiveItemAuthorError,
    apply_legacy_author_name,
    ordered_author_links,
    ordered_authors,
    replace_archive_item_authors,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
    update_manual_text_archive_item,
    update_ocr_document_metadata,
    update_photo_archive_item_metadata,
    update_video_archive_item,
)
from documents.services.archive_search_index import build_archive_item_search_content
from documents.services.photo_upload import create_photo_upload_plan

SCHEMA_MIGRATION = "0058_author_foundation"
DATA_MIGRATION = "0059_backfill_authors_from_author_name"
YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _create_archive_item(
    *,
    title: str = "Letter",
    item_type: str = ArchiveItem.ItemType.MANUAL_TEXT,
    author_name: str = "",
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=item_type,
        title=title,
        visibility=ArchiveItem.Visibility.PRIVATE,
        author_name=author_name,
    )


def _create_ocr_document(**kwargs):
    kwargs.setdefault("title", "OCR item")
    kwargs.setdefault("doc_type", Document.DocType.IMAGE)
    kwargs.setdefault("text_input_type", Document.TextInputType.HANDWRITTEN)
    kwargs.setdefault("visibility", Document.Visibility.PRIVATE)
    return create_ocr_document(**kwargs)


def _person_row_counts() -> tuple[int, int, int]:
    return (
        Person.objects.count(),
        ArchiveItemPerson.objects.count(),
        PhotoPerson.objects.count(),
    )


def _search_index_metadata(item: ArchiveItem) -> str:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk).metadata_text


def _assert_single_author_at_position_zero(item: ArchiveItem, name: str) -> Author:
    links = ordered_author_links(item)
    authors = ordered_authors(item)
    assert len(links) == 1
    assert links[0].position == 0
    assert authors[0].name == name
    return authors[0]


class AuthorModelTests(TestCase):
    def test_author_can_be_created_without_unique_name(self):
        first = Author.objects.create(name="Ada Lovelace")
        second = Author.objects.create(name="Ada Lovelace")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(Author.objects.filter(name="Ada Lovelace").count(), 2)
        self.assertIsNotNone(first.created_at)
        self.assertIsNotNone(first.updated_at)
        self.assertEqual(str(first), "Ada Lovelace")

    def test_author_is_not_a_person(self):
        Author.objects.create(name="Ada Lovelace")
        self.assertEqual(Person.objects.count(), 0)
        self.assertFalse(hasattr(Author(), "biography"))
        self.assertIsNone(Author.objects.get(name="Ada Lovelace").person_id)


class ArchiveItemAuthorRelationTests(TestCase):
    def test_one_author_can_relate_to_multiple_archive_items(self):
        author = Author.objects.create(name="Ada")
        first_item = _create_archive_item(title="Letter A")
        second_item = _create_archive_item(title="Letter B")
        ArchiveItemAuthor.objects.create(
            archive_item=first_item, author=author, position=0
        )
        ArchiveItemAuthor.objects.create(
            archive_item=second_item, author=author, position=0
        )
        self.assertEqual(author.archive_items.count(), 2)
        self.assertCountEqual(
            author.archive_items.values_list("pk", flat=True),
            [first_item.pk, second_item.pk],
        )

    def test_one_archive_item_can_have_ordered_authors(self):
        item = _create_archive_item(title="Joint letter")
        first = Author.objects.create(name="Ada")
        second = Author.objects.create(name="Charles")
        replace_archive_item_authors(item, [first, second])
        self.assertEqual(
            [(link.position, link.author_id) for link in ordered_author_links(item)],
            [(0, first.pk), (1, second.pk)],
        )
        self.assertEqual(ordered_authors(item), [first, second])
        self.assertCountEqual(
            item.authors.values_list("pk", flat=True),
            [first.pk, second.pk],
        )

    def test_duplicate_archive_item_author_relation_is_rejected(self):
        item = _create_archive_item(title="Duplicate author")
        author = Author.objects.create(name="Ada")
        ArchiveItemAuthor.objects.create(archive_item=item, author=author, position=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemAuthor.objects.create(
                    archive_item=item, author=author, position=1
                )
        self.assertEqual(ArchiveItemAuthor.objects.count(), 1)

    def test_duplicate_archive_item_author_position_is_rejected(self):
        item = _create_archive_item(title="Duplicate position")
        first = Author.objects.create(name="Ada")
        second = Author.objects.create(name="Charles")
        ArchiveItemAuthor.objects.create(archive_item=item, author=first, position=0)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ArchiveItemAuthor.objects.create(
                    archive_item=item, author=second, position=0
                )
        self.assertEqual(ArchiveItemAuthor.objects.count(), 1)

    def test_author_links_default_ordering_is_position(self):
        item = _create_archive_item(title="Order")
        later = Author.objects.create(name="Zed")
        earlier = Author.objects.create(name="Ann")
        ArchiveItemAuthor.objects.create(archive_item=item, author=later, position=1)
        ArchiveItemAuthor.objects.create(archive_item=item, author=earlier, position=0)
        self.assertEqual(
            list(item.author_links.values_list("position", "author_id")),
            [(0, earlier.pk), (1, later.pk)],
        )


class AuthorAdminExposureTests(TestCase):
    def setUp(self):
        self.request = RequestFactory().get("/admin/")
        self.request.user = User.objects.create_superuser(
            username="author_admin_exposure",
            password="test-pass",
            email="author-admin@example.com",
        )
        self.site = AdminSite()

    def test_archive_item_admin_form_does_not_include_authors(self):
        item = _create_archive_item()
        admin = ArchiveItemAdmin(ArchiveItem, self.site)
        add_form = admin.get_form(self.request)
        change_form = admin.get_form(self.request, obj=item)
        self.assertNotIn("authors", add_form.base_fields)
        self.assertNotIn("authors", change_form.base_fields)
        self.assertNotIn("authors", admin.get_fields(self.request, obj=item))
        self.assertIn("authors", admin.get_exclude(self.request) or ())
        self.assertIn("people", admin.get_exclude(self.request) or ())

    def test_author_models_are_not_registered_in_django_admin(self):
        self.assertFalse(django_admin.site.is_registered(Author))
        self.assertFalse(django_admin.site.is_registered(ArchiveItemAuthor))


class LegacyAuthorDualWriteTests(TestCase):
    def test_manual_text_create_dual_writes_exact_name_at_position_zero(self):
        before_people = _person_row_counts()
        item = create_manual_text_archive_item(
            title="Manual create",
            body="body",
            author_name="  Ada Lovelace  ",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "Ada Lovelace")
        _assert_single_author_at_position_zero(item, "Ada Lovelace")
        self.assertIn("Ada Lovelace", _search_index_metadata(item))
        self.assertEqual(_person_row_counts(), before_people)

    def test_manual_text_create_empty_author_creates_no_relations(self):
        item = create_manual_text_archive_item(
            title="Manual empty",
            body="body",
            author_name="   ",
        )
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.author_links.count(), 0)
        self.assertEqual(Author.objects.count(), 0)

    def test_manual_text_create_does_not_split_commas(self):
        item = create_manual_text_archive_item(
            title="Comma create",
            body="body",
            author_name="Ada Lovelace, Charles Babbage",
        )
        self.assertEqual(item.author_name, "Ada Lovelace, Charles Babbage")
        _assert_single_author_at_position_zero(item, "Ada Lovelace, Charles Babbage")
        self.assertEqual(Author.objects.count(), 1)

    def test_manual_text_create_keeps_bilingual_name_unsplit(self):
        item = create_manual_text_archive_item(
            title="Bilingual create",
            body="body",
            author_name="Ada Lovelace / עדה לאבלייס",
        )
        self.assertEqual(item.author_name, "Ada Lovelace / עדה לאבלייס")
        _assert_single_author_at_position_zero(item, "Ada Lovelace / עדה לאבלייס")
        self.assertEqual(Author.objects.count(), 1)

    def test_manual_text_edit_replaces_and_clear_dual_write(self):
        item = create_manual_text_archive_item(
            title="Manual edit",
            body="body",
            author_name="Before author",
        )
        first = _assert_single_author_at_position_zero(item, "Before author")
        update_manual_text_archive_item(
            item,
            title="Manual edit",
            body="body",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            author_name="After author",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "After author")
        second = _assert_single_author_at_position_zero(item, "After author")
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(item.author_links.count(), 1)
        edited_metadata = _search_index_metadata(item)
        self.assertIn("After author", edited_metadata)
        self.assertNotIn("Before author", edited_metadata)

        update_manual_text_archive_item(
            item,
            title="Manual edit",
            body="body",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            author_name="   ",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("After author", _search_index_metadata(item))

    def test_ocr_create_edit_replace_and_clear_dual_write(self):
        doc = _create_ocr_document(author_name="  יוסף לוי  ")
        item = doc.archive_item
        self.assertEqual(item.author_name, "יוסף לוי")
        _assert_single_author_at_position_zero(item, "יוסף לוי")
        self.assertIn("יוסף לוי", _search_index_metadata(item))

        update_ocr_document_metadata(
            doc,
            title=item.title,
            visibility=item.visibility,
            date_start=None,
            date_end=None,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            author_name="רחל כהן",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "רחל כהן")
        _assert_single_author_at_position_zero(item, "רחל כהן")
        ocr_edited = _search_index_metadata(item)
        self.assertIn("רחל כהן", ocr_edited)
        self.assertNotIn("יוסף לוי", ocr_edited)

        update_ocr_document_metadata(
            doc,
            title=item.title,
            visibility=item.visibility,
            date_start=None,
            date_end=None,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            author_name="",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("רחל כהן", _search_index_metadata(item))
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    def test_video_create_edit_replace_and_clear_dual_write(self):
        item = create_video_archive_item(
            title="Video create",
            source_url=YOUTUBE_URL,
            author_name="Video Author",
        )
        self.assertEqual(item.author_name, "Video Author")
        _assert_single_author_at_position_zero(item, "Video Author")
        self.assertIn("Video Author", _search_index_metadata(item))

        update_video_archive_item(
            item,
            title="Video create",
            source_url=YOUTUBE_URL,
            visibility=item.visibility,
            date_start=None,
            date_end=None,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            author_name="Replacement Author",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "Replacement Author")
        _assert_single_author_at_position_zero(item, "Replacement Author")
        video_edited = _search_index_metadata(item)
        self.assertIn("Replacement Author", video_edited)
        self.assertNotIn("Video Author", video_edited)

        update_video_archive_item(
            item,
            title="Video create",
            source_url=YOUTUBE_URL,
            visibility=item.visibility,
            date_start=None,
            date_end=None,
            date_precision=item.date_precision,
            metadata_status=item.metadata_status,
            author_name="",
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("Replacement Author", _search_index_metadata(item))

    def test_exact_existing_author_is_reused(self):
        first = create_manual_text_archive_item(
            title="First reuse",
            body="body",
            author_name="Ada Lovelace",
        )
        second = create_manual_text_archive_item(
            title="Second reuse",
            body="body",
            author_name="Ada Lovelace",
        )
        first_author = _assert_single_author_at_position_zero(first, "Ada Lovelace")
        second_author = _assert_single_author_at_position_zero(second, "Ada Lovelace")
        self.assertEqual(first_author.pk, second_author.pk)
        self.assertEqual(Author.objects.filter(name="Ada Lovelace").count(), 1)

    def test_spelling_variants_and_titles_are_not_merged(self):
        titled = create_manual_text_archive_item(
            title="Titled",
            body="body",
            author_name="ד״ר רחל כהן",
        )
        variant = create_manual_text_archive_item(
            title="Variant",
            body="body",
            author_name="רחל כהן",
        )
        self.assertEqual(titled.author_name, "ד״ר רחל כהן")
        self.assertEqual(variant.author_name, "רחל כהן")
        self.assertEqual(Author.objects.count(), 2)
        self.assertNotEqual(
            ordered_authors(titled)[0].pk,
            ordered_authors(variant)[0].pk,
        )

    def test_ambiguous_duplicate_authors_fail_closed_without_partial_writes(self):
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        item = create_manual_text_archive_item(
            title="Keep title",
            body="keep body",
            author_name="",
        )
        before_authors = Author.objects.filter(name="Ada").count()
        with self.assertRaises(ArchiveItemAuthorError) as raised:
            update_manual_text_archive_item(
                item,
                title="Should not stick",
                body="should not stick",
                visibility=ArchiveItem.Visibility.PUBLIC,
                date_start=None,
                date_end=None,
                date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                author_name="Ada",
            )
        self.assertEqual(raised.exception.message, AMBIGUOUS_AUTHOR_ERROR)
        item.refresh_from_db()
        item.manual_text_content.refresh_from_db()
        self.assertEqual(item.title, "Keep title")
        self.assertEqual(item.manual_text_content.body, "keep body")
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("Ada", _search_index_metadata(item))
        self.assertEqual(Author.objects.filter(name="Ada").count(), before_authors)
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    def test_duplicate_authors_roll_back_manual_text_create_completely(self):
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        before_items = ArchiveItem.objects.count()
        before_content = ManualTextContent.objects.count()
        before_index = ArchiveItemSearchIndex.objects.count()
        with self.assertRaises(ArchiveItemAuthorError) as raised:
            create_manual_text_archive_item(
                title="Orphan manual create",
                body="orphan body",
                author_name="Ada",
            )
        self.assertEqual(raised.exception.message, AMBIGUOUS_AUTHOR_ERROR)
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(ManualTextContent.objects.count(), before_content)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), before_index)
        self.assertFalse(
            ArchiveItem.objects.filter(title="Orphan manual create").exists()
        )
        self.assertEqual(Author.objects.filter(name="Ada").count(), 2)
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    def test_duplicate_authors_roll_back_ocr_create_and_update_completely(self):
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        before_items = ArchiveItem.objects.count()
        before_docs = Document.objects.count()
        before_index = ArchiveItemSearchIndex.objects.count()
        with self.assertRaises(ArchiveItemAuthorError):
            _create_ocr_document(title="Orphan OCR create", author_name="Ada")
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(Document.objects.count(), before_docs)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), before_index)
        self.assertFalse(ArchiveItem.objects.filter(title="Orphan OCR create").exists())

        doc = _create_ocr_document(title="OCR keep title", author_name="")
        item = doc.archive_item
        with self.assertRaises(ArchiveItemAuthorError):
            update_ocr_document_metadata(
                doc,
                title="Should not stick OCR",
                visibility=ArchiveItem.Visibility.PUBLIC,
                date_start=None,
                date_end=None,
                date_precision=item.date_precision,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                author_name="Ada",
            )
        item.refresh_from_db()
        self.assertEqual(item.title, "OCR keep title")
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("Ada", _search_index_metadata(item))
        self.assertEqual(Author.objects.filter(name="Ada").count(), 2)

    def test_duplicate_authors_roll_back_video_create_and_update_completely(self):
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        before_items = ArchiveItem.objects.count()
        before_videos = VideoContent.objects.count()
        before_index = ArchiveItemSearchIndex.objects.count()
        with self.assertRaises(ArchiveItemAuthorError):
            create_video_archive_item(
                title="Orphan video create",
                source_url=YOUTUBE_URL,
                author_name="Ada",
            )
        self.assertEqual(ArchiveItem.objects.count(), before_items)
        self.assertEqual(VideoContent.objects.count(), before_videos)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), before_index)
        self.assertFalse(
            ArchiveItem.objects.filter(title="Orphan video create").exists()
        )

        item = create_video_archive_item(
            title="Video keep title",
            source_url=YOUTUBE_URL,
            author_name="",
        )
        with self.assertRaises(ArchiveItemAuthorError):
            update_video_archive_item(
                item,
                title="Should not stick video",
                source_url=YOUTUBE_URL,
                visibility=ArchiveItem.Visibility.PUBLIC,
                date_start=None,
                date_end=None,
                date_precision=item.date_precision,
                metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
                author_name="Ada",
            )
        item.refresh_from_db()
        self.assertEqual(item.title, "Video keep title")
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.visibility, ArchiveItem.Visibility.PRIVATE)
        self.assertEqual(item.author_links.count(), 0)
        self.assertNotIn("Ada", _search_index_metadata(item))
        self.assertEqual(Author.objects.filter(name="Ada").count(), 2)

    def test_apply_legacy_author_name_fail_closed_does_not_write_string_or_relations(
        self,
    ):
        item = _create_archive_item(title="Standalone", author_name="Kept")
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        with self.assertRaises(ArchiveItemAuthorError):
            apply_legacy_author_name(item, "Ada")
        item.refresh_from_db()
        self.assertEqual(item.author_name, "Kept")
        self.assertEqual(item.author_links.count(), 0)
        self.assertEqual(Author.objects.filter(name="Ada").count(), 2)


class PhotoAuthorIsolationTests(TestCase):
    @override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_photo_create_does_not_dual_write_authors(self, _mock_put):
        item, _photo, _url = create_photo_upload_plan(
            bucket="test-uploads-bucket",
            title="Photo create",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            original_name="photo.jpg",
            mime_type="image/jpeg",
            discovery_metadata={
                "category_names": [],
                "event_names": [],
                "tag_names": [],
            },
        )
        self.assertEqual(item.author_name, "")
        self.assertEqual(item.author_links.count(), 0)
        self.assertEqual(Author.objects.count(), 0)

    def test_photo_metadata_update_does_not_dual_write_stored_author_name(self):
        item = _create_archive_item(
            title="Photo stored author",
            item_type=ArchiveItem.ItemType.PHOTO,
            author_name="Stored Photo Author",
        )
        PhotoContent.objects.create(
            archive_item=item,
            original_file_key="photos/1/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        update_photo_archive_item_metadata(
            item,
            title="Photo stored author edited",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
        )
        item.refresh_from_db()
        self.assertEqual(item.title, "Photo stored author edited")
        self.assertEqual(item.author_name, "Stored Photo Author")
        self.assertEqual(item.author_links.count(), 0)
        self.assertEqual(Author.objects.count(), 0)

    def test_photo_staff_forms_still_omit_author_name(self):
        staff = User.objects.create_user(
            username="photo_author_staff",
            password="test-pass",
            is_staff=True,
        )
        item = _create_archive_item(
            title="Photo form",
            item_type=ArchiveItem.ItemType.PHOTO,
        )
        PhotoContent.objects.create(
            archive_item=item,
            original_file_key="photos/form/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        self.client.force_login(staff)
        create_resp = self.client.get(
            reverse("archive-manage-new"), {"item_type": "photo"}
        )
        edit_resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertEqual(create_resp.status_code, 200)
        self.assertEqual(edit_resp.status_code, 200)
        self.assertNotContains(create_resp, 'name="author_name"')
        self.assertNotContains(edit_resp, 'name="author_name"')
        self.assertNotContains(create_resp, 'name="author_ids"')
        self.assertNotContains(edit_resp, 'name="author_ids"')
        self.assertNotContains(create_resp, 'name="new_author_name"')
        self.assertNotContains(edit_resp, 'name="new_author_name"')


class AuthorCompatibilityBehaviorTests(TestCase):
    def test_public_display_still_uses_author_name_not_unlinked_author(self):
        item = create_manual_text_archive_item(
            title="Display author",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        item.author_name = "Visible Author"
        item.save(update_fields=["author_name", "updated_at"])
        Author.objects.create(name="Hidden Author")
        resp = self.client.get(f"/archive/{item.id}/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "מחבר/ת")
        self.assertContains(resp, "Visible Author")
        self.assertNotContains(resp, "Hidden Author")

    def test_q_search_indexes_structured_author_names(self):
        item = create_manual_text_archive_item(
            title="Search author item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            author_name="uniqauthorfoundationtoken",
        )
        Author.objects.create(name="uniqhiddenauthortoken")
        public_qs = ArchiveItem.objects.filter(visibility=ArchiveItem.Visibility.PUBLIC)
        matched = filter_archive_items_by_search_query(
            public_qs, "uniqauthorfoundationtoken"
        )
        hidden = filter_archive_items_by_search_query(
            public_qs, "uniqhiddenauthortoken"
        )
        self.assertTrue(matched.filter(pk=item.pk).exists())
        self.assertFalse(hidden.filter(pk=item.pk).exists())
        content = build_archive_item_search_content(item)
        self.assertIn("uniqauthorfoundationtoken", content.metadata_text)
        self.assertNotIn("uniqhiddenauthortoken", content.metadata_text)

    def test_advanced_author_filter_matches_structured_author_id(self):
        match = create_manual_text_archive_item(
            title="Author match",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            author_name="Exact Author",
        )
        other = create_manual_text_archive_item(
            title="Author other",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            author_name="Other Author",
        )
        filters = normalize_archive_advanced_filters(
            {"author": str(match.author_links.get().author_id)}
        )
        ids = list(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(), filters
            ).values_list("pk", flat=True)
        )
        self.assertEqual(ids, [match.pk])
        self.assertNotIn(other.pk, ids)


class AuthorFoundationMigrationContractTests(TestCase):
    def test_schema_migration_matches_models(self):
        migration_module = import_module(f"documents.migrations.{SCHEMA_MIGRATION}")
        Migration = migration_module.Migration
        self.assertEqual(
            Migration.dependencies, [("documents", "0057_person_biography")]
        )
        operations = Migration.operations
        create_author = next(
            op
            for op in operations
            if op.__class__.__name__ == "CreateModel" and op.name == "Author"
        )
        self.assertEqual(
            {name for name, _field in create_author.fields},
            {"id", "name", "created_at", "updated_at"},
        )
        self.assertFalse(create_author.options.get("unique_together"))
        name_field = dict(create_author.fields)["name"]
        self.assertEqual(name_field.max_length, 255)
        self.assertFalse(name_field.unique)

        create_link = next(
            op
            for op in operations
            if op.__class__.__name__ == "CreateModel" and op.name == "ArchiveItemAuthor"
        )
        field_names = {name for name, _field in create_link.fields}
        self.assertEqual(
            field_names,
            {"id", "position", "created_at", "archive_item", "author"},
        )
        self.assertEqual(
            create_link.options["ordering"],
            ["archive_item", "position", "id"],
        )
        add_m2m = next(
            op
            for op in operations
            if op.__class__.__name__ == "AddField" and op.name == "authors"
        )
        self.assertEqual(add_m2m.model_name, "archiveitem")
        through = add_m2m.field.remote_field.through
        through_name = (
            through if isinstance(through, str) else through._meta.object_name
        )
        self.assertIn("ArchiveItemAuthor", through_name)
        constraint_names = {
            op.constraint.name
            for op in operations
            if op.__class__.__name__ == "AddConstraint"
        }
        self.assertEqual(
            constraint_names,
            {"uniq_archive_item_author", "uniq_archive_item_author_position"},
        )

    def test_data_migration_is_reversible_runpython(self):
        migration_module = import_module(f"documents.migrations.{DATA_MIGRATION}")
        Migration = migration_module.Migration
        self.assertEqual(Migration.dependencies, [("documents", SCHEMA_MIGRATION)])
        self.assertEqual(len(Migration.operations), 1)
        operation = Migration.operations[0]
        self.assertEqual(
            operation.code, migration_module.backfill_authors_from_author_name
        )
        self.assertEqual(
            operation.reverse_code,
            migration_module.reverse_backfill_authors_from_author_name,
        )


class AuthorFoundationApplyMigrationTests(TransactionTestCase):
    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_forward_backfill_is_one_to_one_and_reverse_keeps_author_name(self):
        migrate_from = [("documents", "0057_person_biography")]
        migrate_schema = [("documents", SCHEMA_MIGRATION)]
        migrate_to = [("documents", DATA_MIGRATION)]
        try:
            old_apps = self._migrate(migrate_from)
            ArchiveItemModel = old_apps.get_model("documents", "ArchiveItem")
            PersonModel = old_apps.get_model("documents", "Person")

            shared = ArchiveItemModel.objects.create(
                title="Shared Ada",
                item_type="MANUAL_TEXT",
                visibility="public",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="Ada Lovelace",
            )
            ArchiveItemModel.objects.create(
                title="Second Ada",
                item_type="OCR_DOCUMENT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="Ada Lovelace",
            )
            comma = ArchiveItemModel.objects.create(
                title="Comma names",
                item_type="VIDEO",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="Ada Lovelace, Charles Babbage",
            )
            titled = ArchiveItemModel.objects.create(
                title="Titled name",
                item_type="MANUAL_TEXT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="ד״ר רחל כהן",
            )
            photo = ArchiveItemModel.objects.create(
                title="Photo with author",
                item_type="PHOTO",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="Photo Author",
            )
            empty = ArchiveItemModel.objects.create(
                title="Empty author",
                item_type="MANUAL_TEXT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="",
            )
            whitespace = ArchiveItemModel.objects.create(
                title="Whitespace author",
                item_type="MANUAL_TEXT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="   ",
            )
            variant = ArchiveItemModel.objects.create(
                title="Case variant",
                item_type="MANUAL_TEXT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="ada lovelace",
            )
            bilingual = ArchiveItemModel.objects.create(
                title="Bilingual name",
                item_type="OCR_DOCUMENT",
                visibility="private",
                date_precision="UNKNOWN",
                metadata_status="NEEDS_COMPLETION",
                author_name="Ada Lovelace / עדה לאבלייס",
            )
            self.assertEqual(PersonModel.objects.count(), 0)
            shared_id = shared.pk
            comma_id = comma.pk
            titled_id = titled.pk
            photo_id = photo.pk
            empty_id = empty.pk
            whitespace_id = whitespace.pk
            variant_id = variant.pk
            bilingual_id = bilingual.pk

            new_apps = self._migrate(migrate_to)
            MigratedAuthor = new_apps.get_model("documents", "Author")
            MigratedLink = new_apps.get_model("documents", "ArchiveItemAuthor")
            MigratedItem = new_apps.get_model("documents", "ArchiveItem")
            MigratedPerson = new_apps.get_model("documents", "Person")
            MigratedItemPerson = new_apps.get_model("documents", "ArchiveItemPerson")
            MigratedPhotoPerson = new_apps.get_model("documents", "PhotoPerson")

            self.assertEqual(MigratedAuthor.objects.count(), 6)
            self.assertEqual(
                MigratedAuthor.objects.filter(name="Ada Lovelace").count(), 1
            )
            self.assertEqual(
                MigratedAuthor.objects.filter(
                    name="Ada Lovelace, Charles Babbage"
                ).count(),
                1,
            )
            self.assertEqual(
                MigratedAuthor.objects.filter(
                    name="Ada Lovelace / עדה לאבלייס"
                ).count(),
                1,
            )
            self.assertEqual(
                MigratedAuthor.objects.get(name="ד״ר רחל כהן").name, "ד״ר רחל כהן"
            )
            self.assertEqual(
                MigratedLink.objects.filter(archive_item_id=shared_id).count(), 1
            )
            shared_link = MigratedLink.objects.get(archive_item_id=shared_id)
            self.assertEqual(shared_link.position, 0)
            self.assertEqual(
                MigratedLink.objects.filter(author=shared_link.author).count(),
                2,
            )
            comma_item = MigratedItem.objects.get(pk=comma_id)
            self.assertEqual(comma_item.author_name, "Ada Lovelace, Charles Babbage")
            self.assertEqual(
                MigratedLink.objects.filter(archive_item=comma_item).count(), 1
            )
            self.assertEqual(
                MigratedLink.objects.get(archive_item=comma_item).author.name,
                "Ada Lovelace, Charles Babbage",
            )
            self.assertEqual(
                MigratedItem.objects.get(pk=titled_id).author_name, "ד״ר רחל כהן"
            )
            self.assertEqual(
                MigratedLink.objects.filter(archive_item_id=photo_id).count(), 1
            )
            self.assertEqual(
                MigratedLink.objects.filter(archive_item_id=empty_id).count(), 0
            )
            self.assertEqual(
                MigratedLink.objects.filter(archive_item_id=whitespace_id).count(), 0
            )
            self.assertEqual(
                MigratedItem.objects.get(pk=whitespace_id).author_name, "   "
            )
            self.assertEqual(
                MigratedItem.objects.get(pk=variant_id).author_name, "ada lovelace"
            )
            self.assertEqual(
                MigratedItem.objects.get(pk=bilingual_id).author_name,
                "Ada Lovelace / עדה לאבלייס",
            )
            self.assertEqual(
                MigratedLink.objects.filter(archive_item_id=bilingual_id).count(), 1
            )
            self.assertEqual(MigratedPerson.objects.count(), 0)
            self.assertEqual(MigratedItemPerson.objects.count(), 0)
            self.assertEqual(MigratedPhotoPerson.objects.count(), 0)

            reversed_apps = self._migrate(migrate_schema)
            ReversedAuthor = reversed_apps.get_model("documents", "Author")
            ReversedLink = reversed_apps.get_model("documents", "ArchiveItemAuthor")
            ReversedItem = reversed_apps.get_model("documents", "ArchiveItem")
            self.assertEqual(ReversedAuthor.objects.count(), 0)
            self.assertEqual(ReversedLink.objects.count(), 0)
            self.assertEqual(
                ReversedItem.objects.get(pk=shared_id).author_name, "Ada Lovelace"
            )
            self.assertEqual(
                ReversedItem.objects.get(pk=comma_id).author_name,
                "Ada Lovelace, Charles Babbage",
            )
            self.assertEqual(
                ReversedItem.objects.get(pk=photo_id).author_name, "Photo Author"
            )
            self.assertEqual(
                ReversedItem.objects.get(pk=whitespace_id).author_name, "   "
            )
            self.assertEqual(
                ReversedItem.objects.get(pk=bilingual_id).author_name,
                "Ada Lovelace / עדה לאבלייס",
            )
        finally:
            self._migrate([("documents", DATA_MIGRATION)])
