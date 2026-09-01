"""Staff multi-author create/edit UX for OCR_DOCUMENT, MANUAL_TEXT, and VIDEO."""

from __future__ import annotations

import json
import re
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Author,
    Document,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_authors import (
    AMBIGUOUS_AUTHOR_ERROR,
    AUTHOR_IDS_FIELD,
    AUTHOR_JOINED_TOO_LONG_ERROR,
    AUTHOR_NAME_TOO_LONG_ERROR,
    AUTHOR_NAMES_COMMAS_ONLY_ERROR,
    AUTHOR_NOT_FOUND_ERROR,
    NEW_AUTHOR_NAME_FIELD,
    ArchiveItemAuthorError,
    apply_staff_archive_item_authors,
    ordered_author_links,
    ordered_authors,
    parse_archive_item_authors_form,
    split_comma_separated_author_names,
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
from documents.test_archive_date_payloads import merge_default_date_fields

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
MANAGE_NEW_URL = "/archive/manage/new/"
MANUAL_CREATE_URL = "/archive/manage/new/manual-text/"
EDIT_URL_TEMPLATE = "/archive/manage/{item_id}/edit/"


def _option_ids_for_field(html: str, field_name: str) -> list[int]:
    match = re.search(
        rf'<select[^>]*name="{field_name}"[^>]*>(.*?)</select>',
        html,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing select name={field_name}"
    return [int(value) for value in re.findall(r'value="(\d+)"', match.group(1))]


def _checked_author_ids(html: str) -> list[int]:
    return [
        int(value)
        for value in re.findall(
            r'<input[^>]*type="checkbox"[^>]*name="author_ids"[^>]*value="(\d+)"[^>]*checked',
            html,
        )
    ]


def _person_row_counts() -> tuple[int, int, int]:
    return (
        Person.objects.count(),
        ArchiveItemPerson.objects.count(),
        PhotoPerson.objects.count(),
    )


class StaffAuthorParserTests(TestCase):
    def test_split_trims_drops_empty_and_dedupes_in_order(self):
        self.assertEqual(
            split_comma_separated_author_names("  Ada, , Charles, Ada ,Ada Lovelace "),
            ["Ada", "Charles", "Ada Lovelace"],
        )

    def test_ascii_comma_only_does_not_split_hebrew_comma(self):
        self.assertEqual(
            split_comma_separated_author_names("עדה، צ'ארלס"),
            ["עדה، צ'ארלס"],
        )

    def test_commas_only_is_invalid_and_overlong_token_is_invalid(self):
        _data, comma_errors = parse_archive_item_authors_form(
            {NEW_AUTHOR_NAME_FIELD: " , , "}
        )
        self.assertEqual(comma_errors, [AUTHOR_NAMES_COMMAS_ONLY_ERROR])
        _data, length_errors = parse_archive_item_authors_form(
            {NEW_AUTHOR_NAME_FIELD: "a" * 256}
        )
        self.assertEqual(length_errors, [AUTHOR_NAME_TOO_LONG_ERROR])

    def test_typed_unique_existing_name_is_valid_and_invalid_id_is_rejected(self):
        Author.objects.create(name="Ada")
        _data, reuse_errors = parse_archive_item_authors_form(
            {NEW_AUTHOR_NAME_FIELD: "Ada"}
        )
        self.assertEqual(reuse_errors, [])
        _data, id_errors = parse_archive_item_authors_form({AUTHOR_IDS_FIELD: ["nope"]})
        self.assertEqual(id_errors, [AUTHOR_NOT_FOUND_ERROR])

    def test_ambiguous_exact_name_is_rejected_before_writes(self):
        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        _data, errors = parse_archive_item_authors_form({NEW_AUTHOR_NAME_FIELD: "Ada"})
        self.assertEqual(errors, [AMBIGUOUS_AUTHOR_ERROR])

    def test_joined_compatibility_string_over_255_is_rejected(self):
        first = "a" * 130
        second = "b" * 130
        _data, errors = parse_archive_item_authors_form(
            {NEW_AUTHOR_NAME_FIELD: f"{first}, {second}"}
        )
        self.assertEqual(errors, [AUTHOR_JOINED_TOO_LONG_ERROR])

    def test_mixed_selected_id_reused_name_and_new_name_preserve_order(self):
        selected = Author.objects.create(name="Zed")
        reused = Author.objects.create(name="Ann")
        data, errors = parse_archive_item_authors_form(
            {
                AUTHOR_IDS_FIELD: [str(selected.pk)],
                NEW_AUTHOR_NAME_FIELD: "Ann, Brand New, Zed",
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(data[AUTHOR_IDS_FIELD], [selected.pk])
        self.assertEqual(data[NEW_AUTHOR_NAME_FIELD], "Ann, Brand New, Zed")
        item = create_manual_text_archive_item(title="Mixed order", body="body")
        apply_staff_archive_item_authors(
            item,
            author_ids=[selected.pk],
            new_author_name="Ann, Brand New, Zed",
        )
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Zed", "Ann", "Brand New"],
        )
        self.assertEqual(ordered_authors(item)[1].pk, reused.pk)


class StaffAuthorServiceTests(TestCase):
    def test_staff_apply_appends_new_names_and_dual_writes_join(self):
        first = Author.objects.create(name="Zed")
        second = Author.objects.create(name="Ann")
        item = create_manual_text_archive_item(title="Staff apply", body="body")
        apply_staff_archive_item_authors(
            item,
            author_ids=[first.pk, second.pk],
            new_author_name="  New One, New Two, New One ",
        )
        item.refresh_from_db()
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Zed", "Ann", "New One", "New Two"],
        )
        self.assertEqual(item.author_name, "Zed, Ann, New One, New Two")
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    def test_staff_apply_reuses_unique_exact_name_and_suppresses_duplicates(self):
        existing = Author.objects.create(name="Ada")
        also_selected = Author.objects.create(name="Charles")
        item = create_manual_text_archive_item(title="Reuse apply", body="body")
        apply_staff_archive_item_authors(
            item,
            author_ids=[existing.pk, also_selected.pk, existing.pk],
            new_author_name="Ada, New One, Charles, New One",
        )
        item.refresh_from_db()
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Ada", "Charles", "New One"],
        )
        self.assertEqual(item.author_name, "Ada, Charles, New One")
        self.assertEqual(Author.objects.filter(name="Ada").count(), 1)
        self.assertEqual(Author.objects.filter(name="New One").count(), 1)

    def test_staff_apply_clear_and_ambiguous_name_rollback(self):
        item = create_manual_text_archive_item(
            title="Keep title",
            body="keep body",
            author_name="Keep",
        )
        apply_staff_archive_item_authors(item, author_ids=[], new_author_name="")
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(ordered_authors(item), [])

        Author.objects.create(name="Ada")
        Author.objects.create(name="Ada")
        before_authors = Author.objects.count()
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
                staff_author_ids=[],
                new_author_name="Ada",
            )
        self.assertEqual(raised.exception.message, AMBIGUOUS_AUTHOR_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.title, "Keep title")
        self.assertEqual(item.manual_text_content.body, "keep body")
        self.assertEqual(item.author_name, "")
        self.assertEqual(Author.objects.count(), before_authors)
        self.assertEqual(Author.objects.filter(name="Ada").count(), 2)
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    def test_staff_apply_unlinks_without_deleting_author_rows(self):
        old = Author.objects.create(name="Old Author")
        replacement = Author.objects.create(name="Replacement")
        item = create_manual_text_archive_item(title="Replace authors", body="body")
        apply_staff_archive_item_authors(item, author_ids=[old.pk])
        apply_staff_archive_item_authors(
            item,
            author_ids=[],
            new_author_name="Replacement",
        )
        item.refresh_from_db()
        self.assertEqual(ordered_authors(item), [replacement])
        self.assertEqual(item.author_name, "Replacement")
        self.assertTrue(Author.objects.filter(pk=old.pk, name="Old Author").exists())
        self.assertEqual(ArchiveItemAuthor.objects.filter(author=old).count(), 0)

    def test_legacy_author_name_path_still_does_not_split_commas(self):
        item = create_manual_text_archive_item(
            title="Legacy comma",
            body="body",
            author_name="Ada Lovelace, Charles Babbage",
        )
        authors = ordered_authors(item)
        self.assertEqual(len(authors), 1)
        self.assertEqual(authors[0].name, "Ada Lovelace, Charles Babbage")
        self.assertEqual(item.author_name, "Ada Lovelace, Charles Babbage")


class StaffAuthorHtmlFormTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_staff_ux",
            password="test-pass",
            is_staff=True,
        )

    def _manual_payload(self, **overrides):
        payload = {
            "title": "Manual authors",
            "body": "Body.",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def _ocr_payload(self, **overrides):
        payload = {
            "title": "OCR authors",
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.COMPLETED,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def _video_payload(self, **overrides):
        payload = {
            "item_type": "video",
            "title": "Video authors",
            "source_url": YOUTUBE_URL,
            "visibility": ArchiveItem.Visibility.PUBLIC,
            "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
            "start_seconds": "",
            "end_seconds": "",
            "categories": "",
            "events": "",
            "tags": "",
        }
        payload.update(overrides)
        return merge_default_date_fields(payload)

    def test_manual_create_and_edit_cover_order_clear_and_duplicate_reject(self):
        self.client.force_login(self.staff)
        existing = Author.objects.create(name="Selected Author")
        create_resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._manual_payload(
                **{
                    AUTHOR_IDS_FIELD: [str(existing.pk)],
                    NEW_AUTHOR_NAME_FIELD: " New One, New Two, New One ",
                }
            ),
        )
        self.assertEqual(create_resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Manual authors")
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Selected Author", "New One", "New Two"],
        )
        self.assertEqual(item.author_name, "Selected Author, New One, New Two")
        self.assertEqual(_person_row_counts(), (0, 0, 0))

        later = Author.objects.create(name="Zed Link")
        earlier = Author.objects.create(name="Ann Link")
        ArchiveItemAuthor.objects.filter(archive_item=item).delete()
        ArchiveItemAuthor.objects.create(archive_item=item, author=later, position=0)
        ArchiveItemAuthor.objects.create(archive_item=item, author=earlier, position=1)
        item.author_name = "Zed Link, Ann Link"
        item.save(update_fields=["author_name", "updated_at"])

        get_resp = self.client.get(EDIT_URL_TEMPLATE.format(item_id=item.id))
        html = get_resp.content.decode()
        self.assertEqual(_checked_author_ids(html), [later.pk, earlier.pk])
        picker_ids = _option_ids_for_field(html, AUTHOR_IDS_FIELD)
        self.assertNotIn(later.pk, picker_ids)
        self.assertNotIn(earlier.pk, picker_ids)
        self.assertContains(get_resp, "מחברים משויכים לפריט זה")
        self.assertContains(get_resp, "אין צורך ב-Ctrl")

        no_op = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(
                **{AUTHOR_IDS_FIELD: [str(later.pk), str(earlier.pk)]}
            ),
        )
        self.assertEqual(no_op.status_code, 302)
        self.assertEqual(
            [(link.position, link.author_id) for link in ordered_author_links(item)],
            [(0, later.pk), (1, earlier.pk)],
        )

        clear_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(),
        )
        self.assertEqual(clear_resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.author_name, "")
        self.assertEqual(ordered_authors(item), [])

        exists = Author.objects.create(name="Reuse Target")
        reuse_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(
                title="Should not rename",
                **{NEW_AUTHOR_NAME_FIELD: "Reuse Target"},
            ),
        )
        self.assertEqual(reuse_resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.title, "Should not rename")
        self.assertEqual(ordered_authors(item), [exists])
        self.assertEqual(item.author_name, "Reuse Target")
        self.assertEqual(Author.objects.filter(name="Reuse Target").count(), 1)

        Author.objects.create(name="Dup Ada")
        Author.objects.create(name="Dup Ada")
        ambiguous_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(
                title="Should stay renamed",
                **{NEW_AUTHOR_NAME_FIELD: "Dup Ada"},
            ),
        )
        self.assertEqual(ambiguous_resp.status_code, 200)
        self.assertContains(ambiguous_resp, AMBIGUOUS_AUTHOR_ERROR)
        self.assertContains(ambiguous_resp, 'value="Dup Ada"')
        item.refresh_from_db()
        self.assertEqual(item.title, "Should not rename")
        self.assertEqual(item.author_name, "Reuse Target")
        self.assertEqual(Author.objects.filter(name="Dup Ada").count(), 2)

        invalid_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(
                **{
                    AUTHOR_IDS_FIELD: ["not-an-id"],
                    NEW_AUTHOR_NAME_FIELD: "ShouldStay",
                }
            ),
        )
        self.assertEqual(invalid_resp.status_code, 200)
        self.assertContains(invalid_resp, AUTHOR_NOT_FOUND_ERROR)
        self.assertContains(invalid_resp, 'value="ShouldStay"')
        self.assertFalse(Author.objects.filter(name="ShouldStay").exists())

        commas_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(**{NEW_AUTHOR_NAME_FIELD: ", ,"}),
        )
        self.assertEqual(commas_resp.status_code, 200)
        self.assertContains(commas_resp, AUTHOR_NAMES_COMMAS_ONLY_ERROR)

        token_resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._manual_payload(
                title="Manual overlong token",
                **{NEW_AUTHOR_NAME_FIELD: "a" * 256},
            ),
        )
        self.assertEqual(token_resp.status_code, 200)
        self.assertContains(token_resp, AUTHOR_NAME_TOO_LONG_ERROR)
        self.assertFalse(
            ArchiveItem.objects.filter(title="Manual overlong token").exists()
        )

        joined_resp = self.client.post(
            MANUAL_CREATE_URL,
            data=self._manual_payload(
                title="Manual overlong join",
                **{NEW_AUTHOR_NAME_FIELD: f"{'a' * 130}, {'b' * 130}"},
            ),
        )
        self.assertEqual(joined_resp.status_code, 200)
        self.assertContains(joined_resp, AUTHOR_JOINED_TOO_LONG_ERROR)
        self.assertFalse(
            ArchiveItem.objects.filter(title="Manual overlong join").exists()
        )
        self.assertFalse(Author.objects.filter(name="a" * 130).exists())

    def test_manual_edit_validation_preserves_keep_remove_picker_and_typed_input(self):
        self.client.force_login(self.staff)
        kept = Author.objects.create(name="Keep Linked")
        removed = Author.objects.create(name="Remove Linked")
        added = Author.objects.create(name="Picker Added")
        item = create_manual_text_archive_item(
            title="Preserve authors",
            body="Body.",
            staff_author_ids=[kept.pk, removed.pk],
        )
        resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._manual_payload(
                title="Preserve authors",
                **{
                    AUTHOR_IDS_FIELD: [str(kept.pk), str(added.pk)],
                    NEW_AUTHOR_NAME_FIELD: ", ,",
                },
            ),
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertContains(resp, AUTHOR_NAMES_COMMAS_ONLY_ERROR)
        self.assertEqual(_checked_author_ids(html), [kept.pk, added.pk])
        picker_ids = _option_ids_for_field(html, AUTHOR_IDS_FIELD)
        self.assertNotIn(kept.pk, picker_ids)
        self.assertNotIn(added.pk, picker_ids)
        self.assertIn(removed.pk, picker_ids)
        self.assertContains(resp, 'id="author_keep_%s"' % kept.pk)
        self.assertContains(resp, 'value=", ,"')
        self.assertContains(resp, "<legend>מחברים משויכים לפריט זה</legend>")
        item.refresh_from_db()
        self.assertEqual(item.title, "Preserve authors")
        self.assertEqual(
            [author.pk for author in ordered_authors(item)],
            [kept.pk, removed.pk],
        )

    def test_ocr_edit_selects_existing_and_rejects_unknown_id(self):
        doc = create_ocr_document(
            title="OCR authors",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
        )
        first = Author.objects.create(name="OCR First")
        self.client.force_login(self.staff)
        ok = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_payload(
                **{
                    AUTHOR_IDS_FIELD: [str(first.pk)],
                    NEW_AUTHOR_NAME_FIELD: "OCR New",
                }
            ),
        )
        self.assertEqual(ok.status_code, 302)
        item = doc.archive_item
        item.refresh_from_db()
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["OCR First", "OCR New"],
        )
        self.assertEqual(item.author_name, "OCR First, OCR New")

        keep_first_remove_new = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_payload(**{AUTHOR_IDS_FIELD: [str(first.pk)]}),
        )
        self.assertEqual(keep_first_remove_new.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(ordered_authors(item), [first])
        self.assertTrue(Author.objects.filter(name="OCR New").exists())

        replacement = Author.objects.create(name="OCR Replacement")
        replace_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_payload(
                **{
                    AUTHOR_IDS_FIELD: [str(replacement.pk)],
                    NEW_AUTHOR_NAME_FIELD: "OCR Extra",
                }
            ),
        )
        self.assertEqual(replace_resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["OCR Replacement", "OCR Extra"],
        )
        self.assertEqual(item.author_name, "OCR Replacement, OCR Extra")
        self.assertTrue(Author.objects.filter(pk=first.pk).exists())
        self.assertIn(
            "OCR Replacement, OCR Extra",
            ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk).metadata_text,
        )

        bad = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=doc.archive_item_id),
            data=self._ocr_payload(**{AUTHOR_IDS_FIELD: ["999999"]}),
        )
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, AUTHOR_NOT_FOUND_ERROR)
        item.refresh_from_db()
        self.assertEqual(item.author_name, "OCR Replacement, OCR Extra")

    def test_video_create_and_edit_authors(self):
        existing = Author.objects.create(name="Video Selected")
        self.client.force_login(self.staff)
        create_resp = self.client.post(
            MANAGE_NEW_URL,
            data=self._video_payload(
                **{
                    AUTHOR_IDS_FIELD: [str(existing.pk)],
                    NEW_AUTHOR_NAME_FIELD: "Video New",
                }
            ),
        )
        self.assertEqual(create_resp.status_code, 302)
        item = ArchiveItem.objects.get(title="Video authors")
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Video Selected", "Video New"],
        )
        self.assertEqual(item.author_name, "Video Selected, Video New")

        edit_resp = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._video_payload(**{AUTHOR_IDS_FIELD: [str(existing.pk)]}),
        )
        self.assertEqual(edit_resp.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["Video Selected"],
        )
        self.assertEqual(item.author_name, "Video Selected")
        self.assertEqual(_person_row_counts(), (0, 0, 0))

        replace_video = self.client.post(
            EDIT_URL_TEMPLATE.format(item_id=item.id),
            data=self._video_payload(**{NEW_AUTHOR_NAME_FIELD: "Video Selected"}),
        )
        self.assertEqual(replace_video.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(ordered_authors(item), [existing])
        self.assertEqual(item.author_name, "Video Selected")
        self.assertEqual(Author.objects.filter(name="Video Selected").count(), 1)

    def test_photo_forms_and_writer_stay_isolated(self):
        staff = self.staff
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo form",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="Stored Photo Author",
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
        create_resp = self.client.get(MANAGE_NEW_URL, {"item_type": "photo"})
        edit_resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertNotContains(create_resp, 'name="author_ids"')
        self.assertNotContains(edit_resp, 'name="author_ids"')
        self.assertNotContains(create_resp, 'name="new_author_name"')
        self.assertNotContains(edit_resp, 'name="new_author_name"')

        update_photo_archive_item_metadata(
            item,
            title="Photo form",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        )
        item.refresh_from_db()
        self.assertEqual(item.author_name, "Stored Photo Author")
        self.assertEqual(ArchiveItemAuthor.objects.filter(archive_item=item).count(), 0)


class StaffAuthorOcrJsonTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_ocr_json",
            password="test-pass",
            is_staff=True,
        )

    def _post_create(self, payload: dict):
        self.client.force_login(self.staff)
        return self.client.post(
            "/api/uploads/create/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _base_payload(self, **overrides):
        payload = {
            "title": "Upload API authors",
            "doc_type": "IMAGE",
            "text_input_type": "HANDWRITTEN",
            "original_name": "scan.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": 1000,
        }
        payload.update(overrides)
        return payload

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_json_create_uses_author_ids_and_new_names(self, _mock_put):
        existing = Author.objects.create(name="JSON Selected")
        reused = Author.objects.create(name="JSON Reused")
        resp = self._post_create(
            self._base_payload(
                author_ids=[existing.pk],
                new_author_name=" JSON Reused, JSON New ",
            )
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        item = doc.archive_item
        self.assertEqual(
            [author.name for author in ordered_authors(item)],
            ["JSON Selected", "JSON Reused", "JSON New"],
        )
        self.assertEqual(ordered_authors(item)[1].pk, reused.pk)
        self.assertEqual(item.author_name, "JSON Selected, JSON Reused, JSON New")
        self.assertEqual(Author.objects.filter(name="JSON Reused").count(), 1)
        self.assertEqual(_person_row_counts(), (0, 0, 0))

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_json_create_reuses_existing_typed_name_without_new_row(self, _mock_put):
        existing = Author.objects.create(name="JSON Existing")
        before_authors = Author.objects.count()
        resp = self._post_create(self._base_payload(new_author_name="JSON Existing"))
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(ordered_authors(doc.archive_item), [existing])
        self.assertEqual(doc.archive_item.author_name, "JSON Existing")
        self.assertEqual(Author.objects.count(), before_authors)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_json_create_rejects_ambiguous_typed_name_without_rows(self, _mock_put):
        Author.objects.create(name="JSON Dup")
        Author.objects.create(name="JSON Dup")
        before_docs = Document.objects.count()
        before_authors = Author.objects.count()
        resp = self._post_create(self._base_payload(new_author_name="JSON Dup"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn(AMBIGUOUS_AUTHOR_ERROR.encode("utf-8"), resp.content)
        self.assertEqual(Document.objects.count(), before_docs)
        self.assertEqual(Author.objects.count(), before_authors)

    @override_settings(UPLOADS_BUCKET_NAME="test-bucket")
    @patch(
        "documents.views.create_presigned_put", return_value="https://example/upload"
    )
    def test_json_create_ignores_leftover_author_name_field(self, _mock_put):
        resp = self._post_create(
            self._base_payload(author_name="Leftover Compatibility String")
        )
        self.assertEqual(resp.status_code, 201)
        doc = Document.objects.get(id=resp.json()["document_id"])
        self.assertEqual(doc.archive_item.author_name, "")
        self.assertEqual(ordered_authors(doc.archive_item), [])
        self.assertFalse(
            Author.objects.filter(name="Leftover Compatibility String").exists()
        )

    def test_upload_js_sends_author_ids_not_author_name(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("upload-page"))
        self.assertContains(resp, 'id="author_ids"')
        self.assertContains(resp, 'id="new_author_name"')
        self.assertContains(resp, "meta.author_ids = readAuthorIds()")
        self.assertNotContains(resp, 'id="author_name"')
        self.assertNotContains(resp, "meta.author_name")


class StaffAuthorCompatibilityTests(TestCase):
    def test_q_search_and_public_display_still_use_author_name(self):
        item = create_manual_text_archive_item(
            title="Display author",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[],
            new_author_name="Visible Author, Second Author",
        )
        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertContains(resp, "Visible Author, Second Author")
        self.assertIn(
            "Visible Author, Second Author",
            ArchiveItemSearchIndex.objects.get(archive_item_id=item.pk).metadata_text,
        )

    def test_ocr_and_video_service_staff_kwargs_do_not_use_legacy_string(self):
        first = Author.objects.create(name="Svc First")
        doc = create_ocr_document(
            title="OCR staff kwargs",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            staff_author_ids=[first.pk],
            new_author_name="OCR Svc New",
        )
        self.assertEqual(
            [author.name for author in ordered_authors(doc.archive_item)],
            ["Svc First", "OCR Svc New"],
        )
        update_ocr_document_metadata(
            doc,
            title="OCR staff kwargs",
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            staff_author_ids=[],
            new_author_name="",
        )
        doc.archive_item.refresh_from_db()
        self.assertEqual(doc.archive_item.author_name, "")

        video = create_video_archive_item(
            title="Video staff kwargs",
            source_url=YOUTUBE_URL,
            staff_author_ids=[first.pk],
            new_author_name="Video Svc New",
        )
        self.assertEqual(video.author_name, "Svc First, Video Svc New")
        update_video_archive_item(
            video,
            title="Video staff kwargs",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PRIVATE,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
            staff_author_ids=[first.pk],
        )
        video.refresh_from_db()
        self.assertEqual(video.author_name, "Svc First")
        self.assertEqual(_person_row_counts(), (0, 0, 0))
