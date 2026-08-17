"""PHOTO search aggregation: renderable PhotoContent/PhotoPerson text, one ArchiveItem hit."""

from __future__ import annotations

from datetime import date
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.s3 import S3HeadObjectResult
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    build_archive_browse_card,
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    SEARCH_SEGMENT_SEPARATOR,
    archive_item_ids_for_person_photo_appearances,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.archive_search_snippets import (
    MATCH_SOURCE_ITEM_DETAILS,
    build_archive_search_match_presentation,
)
from documents.services.photo_content_management import (
    delete_one_photo_content,
    reorder_photo_contents,
    update_person_name,
    update_photo_content_metadata,
)
from documents.services.photo_thumbnail import generate_and_persist_photo_thumbnail
from documents.services.photo_upload import (
    create_additional_photo_upload_plan,
    create_photo_upload_plan,
    finalize_photo_upload,
)
from documents.test_archive_item import create_viewable_ocr_document


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    return rebuild_archive_item_search_index(_load_item(archive_item_id))


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


def _create_photo_item(
    *,
    title: str,
    visibility: str = ArchiveItem.Visibility.PUBLIC,
    public_note: str = "",
    date_start=None,
    date_end=None,
    date_precision: str = ArchiveItem.DatePrecision.UNKNOWN,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
        public_note=public_note,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    description: str = "",
    location: str = "",
    context: str = "",
    people_present: str = "",
    notes: str = "",
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
    original_filename: str = "photo.jpg",
    date_start=None,
    date_end=None,
    date_precision: str = ArchiveItem.DatePrecision.UNKNOWN,
) -> PhotoContent:
    if original_file_key is None:
        original_file_key = f"photos/{item.pk}-{position}/original.jpg"
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=original_file_key,
        original_filename=original_filename,
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
        upload_error="",
        description=description,
        location=location,
        context=context,
        people_present=people_present,
        notes=notes,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )


def _metadata_update_kwargs(**overrides) -> dict:
    values = {
        "description": "",
        "location": "",
        "context": "",
        "people_present": "",
        "notes": "",
        "date_start": None,
        "date_end": None,
        "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        "person_ids": [],
        "new_person_name": "",
    }
    values.update(overrides)
    return values


def _s3_head_ok(*, content_type: str = "image/jpeg", content_length: int = 2048):
    return S3HeadObjectResult(
        exists=True,
        content_type=content_type,
        content_length=content_length,
    )


def _create_photo_plan_kwargs(**overrides) -> dict:
    values = {
        "bucket": "test-uploads-bucket",
        "title": "Created album",
        "visibility": ArchiveItem.Visibility.PUBLIC,
        "date_start": None,
        "date_end": None,
        "date_precision": ArchiveItem.DatePrecision.UNKNOWN,
        "metadata_status": ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        "original_name": "photo.jpg",
        "mime_type": "image/jpeg",
        "discovery_metadata": {
            "category_names": [],
            "event_names": [],
            "tag_names": [],
        },
        "description": "create-description-token",
        "location": "create-location-token",
    }
    values.update(overrides)
    return values


class PhotoSearchAggregationBuilderTests(TestCase):
    def test_aggregates_non_primary_photo_text_fields_in_position_id_order(self):
        item = _create_photo_item(title="Album title", public_note="Shared note")
        _add_photo(item, position=2, description="second-description-token")
        _add_photo(
            item,
            position=1,
            description="first-description-token",
            location="first-location-token",
        )
        _add_photo(
            item,
            position=3,
            context="third-context-token",
            notes="third-notes-token",
        )

        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.title_text, "Album title")
        self.assertEqual(content.body_text, "")
        self.assertIn("Shared note", content.metadata_text)
        self.assertIn("first-description-token", content.metadata_text)
        self.assertIn("first-location-token", content.metadata_text)
        self.assertIn("second-description-token", content.metadata_text)
        self.assertIn("third-context-token", content.metadata_text)
        self.assertIn("third-notes-token", content.metadata_text)

        first_desc = content.metadata_text.find("first-description-token")
        first_loc = content.metadata_text.find("first-location-token")
        second_desc = content.metadata_text.find("second-description-token")
        third_ctx = content.metadata_text.find("third-context-token")
        third_notes = content.metadata_text.find("third-notes-token")
        self.assertLess(first_desc, first_loc)
        self.assertLess(first_loc, second_desc)
        self.assertLess(second_desc, third_ctx)
        self.assertLess(third_ctx, third_notes)

    def test_indexes_people_present_separately_from_person_names(self):
        item = _create_photo_item(title="People album")
        photo = _add_photo(
            item,
            position=1,
            people_present="maybe uncle in the back",
        )
        identified = Person.objects.create(name="Ada Lovelace")
        PhotoPerson.objects.create(photo_content=photo, person=identified)

        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("maybe uncle in the back", content.metadata_text)
        self.assertIn("Ada Lovelace", content.metadata_text)

    def test_person_names_are_deduped_and_ordered_by_name_then_id(self):
        item = _create_photo_item(title="Named people")
        first = _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        zeta = Person.objects.create(name="Zeta")
        ada = Person.objects.create(name="Ada")
        PhotoPerson.objects.create(photo_content=first, person=zeta)
        PhotoPerson.objects.create(photo_content=second, person=zeta)
        PhotoPerson.objects.create(photo_content=second, person=ada)

        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("Zeta"), 1)
        ada_at = content.metadata_text.find("Ada")
        zeta_at = content.metadata_text.find("Zeta")
        self.assertLess(ada_at, zeta_at)

    def test_skips_empty_segments_and_excludes_technical_fields(self):
        item = _create_photo_item(title="Technical album")
        _add_photo(
            item,
            position=1,
            description="  keep-me  ",
            location="   ",
            original_file_key="photos/secret-key/original.jpg",
            original_filename="secret-filename.jpg",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("keep-me", content.metadata_text)
        self.assertNotIn("secret-key", content.metadata_text)
        self.assertNotIn("secret-filename.jpg", content.metadata_text)
        self.assertNotIn("PENDING", content.metadata_text)
        self.assertNotIn(
            SEARCH_SEGMENT_SEPARATOR + SEARCH_SEGMENT_SEPARATOR,
            content.metadata_text,
        )

    def test_excludes_pending_photo_metadata(self):
        item = _create_photo_item(title="Mixed status")
        _add_photo(
            item,
            position=1,
            description="uploaded-caption",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        _add_photo(
            item,
            position=2,
            description="pending-caption-token",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("uploaded-caption", content.metadata_text)
        self.assertNotIn("pending-caption-token", content.metadata_text)

    def test_excludes_failed_photo_metadata(self):
        item = _create_photo_item(title="Failed child")
        _add_photo(
            item,
            position=1,
            description="uploaded-caption",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        _add_photo(
            item,
            position=2,
            description="failed-caption-token",
            upload_status=PhotoContent.UploadStatus.FAILED,
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("uploaded-caption", content.metadata_text)
        self.assertNotIn("failed-caption-token", content.metadata_text)

    def test_excludes_empty_key_photo_metadata(self):
        item = _create_photo_item(title="Empty key")
        _add_photo(
            item,
            position=1,
            description="uploaded-caption",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        _add_photo(
            item,
            position=2,
            description="empty-key-caption-token",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="",
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("uploaded-caption", content.metadata_text)
        self.assertNotIn("empty-key-caption-token", content.metadata_text)

    def test_includes_uploaded_photo_with_valid_original_key(self):
        item = _create_photo_item(title="Renderable child")
        _add_photo(
            item,
            position=1,
            description="uploaded-caption-token",
            location="uploaded-location-token",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="photos/renderable/original.jpg",
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("uploaded-caption-token", content.metadata_text)
        self.assertIn("uploaded-location-token", content.metadata_text)

    def test_person_on_non_renderable_photo_is_excluded(self):
        item = _create_photo_item(title="Pending person")
        renderable = _add_photo(item, position=1, description="visible-caption")
        pending = _add_photo(
            item,
            position=2,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        visible = Person.objects.create(name="VisiblePersonToken")
        hidden = Person.objects.create(name="HiddenPendingPersonToken")
        PhotoPerson.objects.create(photo_content=renderable, person=visible)
        PhotoPerson.objects.create(photo_content=pending, person=hidden)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("VisiblePersonToken", content.metadata_text)
        self.assertNotIn("HiddenPendingPersonToken", content.metadata_text)

    def test_person_shared_by_renderable_and_non_renderable_appears_once(self):
        item = _create_photo_item(title="Shared person")
        renderable = _add_photo(item, position=1)
        pending = _add_photo(
            item,
            position=2,
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        shared = Person.objects.create(name="SharedPersonToken")
        PhotoPerson.objects.create(photo_content=renderable, person=shared)
        PhotoPerson.objects.create(photo_content=pending, person=shared)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("SharedPersonToken"), 1)

    def test_archive_item_person_alone_is_not_indexed_as_photo_appearance(self):
        item = _create_photo_item(title="Item person only")
        _add_photo(item, position=1, description="photo-caption")
        related = Person.objects.create(name="Charles-item-only-token")
        ArchiveItemPerson.objects.create(archive_item=item, person=related)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertNotIn("Charles-item-only-token", content.metadata_text)

    def test_repeated_photo_text_is_not_duplicated(self):
        item = _create_photo_item(title="Duplicate fragments")
        _add_photo(item, position=1, description="shared-caption-token")
        _add_photo(item, position=2, description="shared-caption-token")
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("shared-caption-token"), 1)


class PhotoSearchAggregationQueryTests(TestCase):
    def test_finds_item_by_non_primary_photo_fields_once(self):
        item = _create_photo_item(title="Shared album title")
        _add_photo(item, position=1, description="primary-only-caption")
        _add_photo(
            item,
            position=2,
            description="second-description-token",
            location="haifa-location-token",
            context="wedding-context-token",
            notes="album-notes-token",
            people_present="people-present-token",
        )
        other = _create_photo_item(title="Other album")
        _add_photo(item=other, position=1, description="unrelated-caption")
        _rebuild(item.pk)
        _rebuild(other.pk)

        qs = ArchiveItem.objects.all()
        for token in (
            "second-description-token",
            "haifa-location-token",
            "wedding-context-token",
            "album-notes-token",
            "people-present-token",
        ):
            ids = _ids(filter_archive_items_by_search_query(qs, token))
            self.assertEqual(ids, [item.pk], token)

        both_photos = _ids(
            filter_archive_items_by_search_query(
                qs, "primary-only-caption second-description-token"
            )
        )
        self.assertEqual(both_photos, [item.pk])

    def test_finds_item_by_identified_person_name(self):
        item = _create_photo_item(title="Named photo")
        photo = _add_photo(item, position=2, description="crowd")
        _add_photo(item, position=1)
        person = Person.objects.create(name="Rivkasearchtoken")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        _rebuild(item.pk)

        ids = _ids(
            filter_archive_items_by_search_query(
                ArchiveItem.objects.all(), "Rivkasearchtoken"
            )
        )
        self.assertEqual(ids, [item.pk])

    def test_item_level_title_and_note_still_match(self):
        item = _create_photo_item(
            title="UmbrellaTitleToken",
            public_note="UmbrellaNoteToken",
        )
        _add_photo(item, position=1, description="component-caption")
        _rebuild(item.pk)
        qs = ArchiveItem.objects.all()
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "UmbrellaTitleToken")),
            [item.pk],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "UmbrellaNoteToken")),
            [item.pk],
        )

    def test_isolation_from_other_item_types(self):
        photo = _create_photo_item(title="Photo isolation")
        _add_photo(photo, position=1, description="photo-only-search-token")
        manual = create_manual_text_archive_item(
            title="Manual isolation",
            body="manual-only-search-token",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        video = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.VIDEO,
            title="Video isolation",
            visibility=ArchiveItem.Visibility.PUBLIC,
            public_note="video-only-search-token",
        )
        ocr = create_viewable_ocr_document(
            title="OCR isolation",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        DocumentTextResult.objects.create(
            document=ocr,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="ocr-only-search-token",
        )
        _rebuild(photo.pk)
        _rebuild(manual.pk)
        _rebuild(ocr.archive_item_id)
        _rebuild(video.pk)

        qs = ArchiveItem.objects.all()
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "photo-only-search-token")),
            [photo.pk],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "manual-only-search-token")),
            [manual.pk],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "ocr-only-search-token")),
            [ocr.archive_item_id],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "video-only-search-token")),
            [video.pk],
        )

    def test_public_search_ignores_non_renderable_child_metadata(self):
        item = _create_photo_item(title="Public mixed album")
        _add_photo(item, position=1, description="primary-renderable-caption")
        _add_photo(
            item,
            position=2,
            description="pending-only-search-token",
            upload_status=PhotoContent.UploadStatus.PENDING,
        )
        _rebuild(item.pk)
        qs = archive_browse_queryset_for_user(None)
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "pending-only-search-token")),
            [],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(qs, "primary-renderable-caption")
            ),
            [item.pk],
        )

    def test_year_filter_still_uses_archive_item_dates_not_photo_dates(self):
        item = _create_photo_item(
            title="Dated umbrella",
            date_start=date(1950, 1, 1),
            date_end=date(1950, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        _add_photo(
            item,
            position=1,
            date_start=date(1977, 1, 1),
            date_end=date(1977, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        _rebuild(item.pk)
        qs = ArchiveItem.objects.filter(pk=item.pk)
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    qs, normalize_archive_advanced_filters({"year": "1950"})
                )
            ),
            [item.pk],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    qs, normalize_archive_advanced_filters({"year": "1977"})
                )
            ),
            [],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "1977")),
            [],
        )


class PhotoSearchAggregationAuthAndSnippetTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="photo_search_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family = User.objects.create_user(
            username="photo_search_family",
            password="test-pass",
        )
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family.groups.add(family_group)

    def test_private_photo_child_metadata_is_not_exposed_anonymously(self):
        private = _create_photo_item(
            title="Private album",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _add_photo(private, position=1, description="private-photo-child-token")
        _rebuild(private.pk)

        anon_ids = _ids(
            filter_archive_items_by_search_query(
                archive_browse_queryset_for_user(None),
                "private-photo-child-token",
            )
        )
        self.assertEqual(anon_ids, [])

        family_ids = _ids(
            filter_archive_items_by_search_query(
                archive_browse_queryset_for_user(self.family),
                "private-photo-child-token",
            )
        )
        self.assertEqual(family_ids, [private.pk])

        resp = self.client.get(
            reverse("archive-list"),
            {"q": "private-photo-child-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertNotContains(
            resp,
            reverse("archive-detail", kwargs={"item_id": private.pk}),
        )

    def test_public_result_url_stays_item_detail_without_photo_query(self):
        item = _create_photo_item(title="Public gallery album")
        _add_photo(item, position=1, description="first caption")
        second = _add_photo(
            item, position=2, description="matched-second-caption-token"
        )
        index = _rebuild(item.pk)

        resp = self.client.get(
            reverse("archive-list"),
            {"q": "matched-second-caption-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        card = resp.context["browse_cards"][0]
        expected = reverse("archive-detail", kwargs={"item_id": item.pk})
        self.assertEqual(card.detail_url, expected)
        self.assertNotIn("photo=", card.detail_url)
        self.assertNotIn(str(second.pk), card.detail_url)

        presentation = build_archive_search_match_presentation(
            archive_item=item,
            search_index=index,
            terms=("matched-second-caption-token",),
        )
        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertEqual(presentation.match_source_label, MATCH_SOURCE_ITEM_DETAILS)
        self.assertEqual(presentation.snippet_segments, ())
        self.assertFalse(presentation.replaces_preview)

        built = build_archive_browse_card(
            item, search_query="matched-second-caption-token"
        )
        self.assertEqual(built.detail_url, expected)


class PhotoSearchIndexRefreshTests(TestCase):
    def test_editing_photo_content_updates_search_once(self):
        item = _create_photo_item(title="Edit album")
        photo = _add_photo(item, position=1, description="before-caption")
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            update_photo_content_metadata(
                photo,
                **_metadata_update_kwargs(description="after-caption-token"),
            )
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args, (item.pk,))
        index = _index_for(item.pk)
        self.assertIn("after-caption-token", index.metadata_text)
        self.assertNotIn("before-caption", index.metadata_text)
        ids = _ids(
            filter_archive_items_by_search_query(
                ArchiveItem.objects.all(), "after-caption-token"
            )
        )
        self.assertEqual(ids, [item.pk])

    def test_deleting_photo_content_removes_unique_text(self):
        item = _create_photo_item(title="Delete album")
        keep = _add_photo(item, position=1, description="keep-caption-token")
        gone = _add_photo(item, position=2, description="gone-caption-token")
        _rebuild(item.pk)
        delete_one_photo_content(gone, bucket="test-uploads-bucket")
        index = _index_for(item.pk)
        self.assertIn("keep-caption-token", index.metadata_text)
        self.assertNotIn("gone-caption-token", index.metadata_text)
        self.assertTrue(PhotoContent.objects.filter(pk=keep.pk).exists())
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "gone-caption-token"
                )
            ),
            [],
        )

    def test_adding_and_removing_photo_person_updates_search(self):
        item = _create_photo_item(title="Person links")
        photo = _add_photo(item, position=1)
        person = Person.objects.create(name="LinkedPersonToken")
        _rebuild(item.pk)
        update_photo_content_metadata(
            photo,
            **_metadata_update_kwargs(person_ids=[person.pk]),
        )
        self.assertIn("LinkedPersonToken", _index_for(item.pk).metadata_text)
        update_photo_content_metadata(photo, **_metadata_update_kwargs(person_ids=[]))
        self.assertNotIn("LinkedPersonToken", _index_for(item.pk).metadata_text)

    def test_renaming_person_refreshes_all_linked_photo_items_once_each(self):
        first = _create_photo_item(title="First linked")
        second = _create_photo_item(title="Second linked")
        unrelated = _create_photo_item(title="Unrelated")
        person = Person.objects.create(name="OldPersonName")
        other = Person.objects.create(name="OtherPerson")
        PhotoPerson.objects.create(
            photo_content=_add_photo(first, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(first, position=2), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(second, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(unrelated, position=1), person=other
        )
        ArchiveItemPerson.objects.create(archive_item=unrelated, person=person)
        for archive_item in (first, second, unrelated):
            _rebuild(archive_item.pk)

        self.assertEqual(
            archive_item_ids_for_person_photo_appearances(person.pk),
            [first.pk, second.pk],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            update_person_name(person, name="NewPersonNameToken")
        self.assertEqual(wrapped.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in wrapped.call_args_list],
            [first.pk, second.pk],
        )
        self.assertIn("NewPersonNameToken", _index_for(first.pk).metadata_text)
        self.assertIn("NewPersonNameToken", _index_for(second.pk).metadata_text)
        self.assertNotIn("OldPersonName", _index_for(first.pk).metadata_text)
        self.assertNotIn("NewPersonNameToken", _index_for(unrelated.pk).metadata_text)

    def test_sync_failure_rolls_back_photo_metadata_write(self):
        item = _create_photo_item(title="Rollback album")
        photo = _add_photo(item, position=1, description="committed-caption")
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                update_photo_content_metadata(
                    photo,
                    **_metadata_update_kwargs(description="uncommitted-caption"),
                )
        photo.refresh_from_db()
        self.assertEqual(photo.description, "committed-caption")
        self.assertNotIn("uncommitted-caption", _index_for(item.pk).metadata_text)

    def test_reorder_keeps_unique_fragments_without_duplication(self):
        item = _create_photo_item(title="Reorder album")
        first = _add_photo(item, position=1, description="alpha-fragment-token")
        second = _add_photo(item, position=2, description="beta-fragment-token")
        _rebuild(item.pk)
        reorder_photo_contents(item, [second.pk, first.pk])
        index = _index_for(item.pk)
        self.assertEqual(index.metadata_text.count("alpha-fragment-token"), 1)
        self.assertEqual(index.metadata_text.count("beta-fragment-token"), 1)
        self.assertLess(
            index.metadata_text.find("beta-fragment-token"),
            index.metadata_text.find("alpha-fragment-token"),
        )


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoSearchManagementIntegrationTests(TestCase):
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_create_plan_does_not_index_pending_photo_metadata(self, _mock_put):
        item, photo, _url = create_photo_upload_plan(**_create_photo_plan_kwargs())
        index = _index_for(item.pk)
        self.assertEqual(index.title_text, "Created album")
        self.assertNotIn("create-description-token", index.metadata_text)
        self.assertNotIn("create-location-token", index.metadata_text)
        self.assertNotIn(photo.original_file_key, index.metadata_text)
        self.assertNotIn("photo.jpg", index.metadata_text)

    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_add_photo_plan_does_not_index_pending_row_metadata(self, _mock_put):
        item = _create_photo_item(title="Existing album")
        _add_photo(item, position=1, description="existing-caption")
        _rebuild(item.pk)
        create_additional_photo_upload_plan(
            archive_item=item,
            bucket="test-uploads-bucket",
            original_name="second.jpg",
            mime_type="image/jpeg",
            description="added-photo-description-token",
            people_present="added-people-present-token",
        )
        index = _index_for(item.pk)
        self.assertIn("existing-caption", index.metadata_text)
        self.assertNotIn("added-photo-description-token", index.metadata_text)
        self.assertNotIn("added-people-present-token", index.metadata_text)

    @patch(
        "documents.services.photo_upload.generate_and_persist_photo_thumbnail",
        return_value=None,
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=_s3_head_ok(),
    )
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_successful_primary_finalize_indexes_photo_metadata(
        self, _mock_put, _mock_head, mock_thumbnail
    ):
        item, photo, _url = create_photo_upload_plan(**_create_photo_plan_kwargs())
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            finalize_photo_upload(
                photo,
                bucket="test-uploads-bucket",
                success=True,
                file_mime="image/jpeg",
            )
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args, (item.pk,))
        mock_thumbnail.assert_called_once()
        photo.refresh_from_db()
        self.assertEqual(photo.upload_status, PhotoContent.UploadStatus.UPLOADED)
        index = _index_for(item.pk)
        self.assertIn("create-description-token", index.metadata_text)
        self.assertIn("create-location-token", index.metadata_text)
        self.assertNotIn(photo.original_file_key, index.metadata_text)

    @patch(
        "documents.services.photo_upload.generate_and_persist_photo_thumbnail",
        return_value=None,
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=_s3_head_ok(),
    )
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_successful_additional_finalize_indexes_new_photo_metadata(
        self, _mock_put, _mock_head, mock_thumbnail
    ):
        item = _create_photo_item(title="Existing album")
        _add_photo(item, position=1, description="existing-caption")
        _rebuild(item.pk)
        _item, pending, _url = create_additional_photo_upload_plan(
            archive_item=item,
            bucket="test-uploads-bucket",
            original_name="second.jpg",
            mime_type="image/jpeg",
            description="added-photo-description-token",
            people_present="added-people-present-token",
        )
        self.assertNotIn(
            "added-photo-description-token", _index_for(item.pk).metadata_text
        )
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            finalize_photo_upload(
                pending,
                bucket="test-uploads-bucket",
                success=True,
                file_mime="image/jpeg",
            )
        self.assertEqual(wrapped.call_count, 1)
        mock_thumbnail.assert_called_once()
        index = _index_for(item.pk)
        self.assertIn("existing-caption", index.metadata_text)
        self.assertIn("added-photo-description-token", index.metadata_text)
        self.assertIn("added-people-present-token", index.metadata_text)

    @patch(
        "documents.services.photo_upload.generate_and_persist_photo_thumbnail",
        return_value=None,
    )
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_failed_finalize_leaves_additional_photo_metadata_absent(
        self, _mock_put, mock_thumbnail
    ):
        item = _create_photo_item(title="Existing album")
        _add_photo(item, position=1, description="existing-caption")
        _rebuild(item.pk)
        _item, pending, _url = create_additional_photo_upload_plan(
            archive_item=item,
            bucket="test-uploads-bucket",
            original_name="second.jpg",
            mime_type="image/jpeg",
            description="failed-photo-description-token",
        )
        finalize_photo_upload(
            pending,
            bucket="test-uploads-bucket",
            success=False,
            file_mime="image/jpeg",
            client_error="client aborted",
        )
        pending.refresh_from_db()
        self.assertEqual(pending.upload_status, PhotoContent.UploadStatus.FAILED)
        mock_thumbnail.assert_not_called()
        index = _index_for(item.pk)
        self.assertIn("existing-caption", index.metadata_text)
        self.assertNotIn("failed-photo-description-token", index.metadata_text)

    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=_s3_head_ok(),
    )
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_finalize_sync_failure_rolls_back_uploaded_status(
        self, _mock_put, _mock_head
    ):
        item = _create_photo_item(title="Existing album")
        _add_photo(item, position=1, description="existing-caption")
        _rebuild(item.pk)
        _item, pending, _url = create_additional_photo_upload_plan(
            archive_item=item,
            bucket="test-uploads-bucket",
            original_name="second.jpg",
            mime_type="image/jpeg",
            description="uncommitted-photo-token",
        )
        with (
            patch(
                "documents.services.archive_search_index.sync_archive_item_search_index",
                side_effect=DatabaseError("index write failed"),
            ),
            patch(
                "documents.services.photo_upload.generate_and_persist_photo_thumbnail"
            ) as mock_thumbnail,
        ):
            with self.assertRaises(DatabaseError):
                finalize_photo_upload(
                    pending,
                    bucket="test-uploads-bucket",
                    success=True,
                    file_mime="image/jpeg",
                )
        pending.refresh_from_db()
        self.assertEqual(pending.upload_status, PhotoContent.UploadStatus.PENDING)
        mock_thumbnail.assert_not_called()
        self.assertNotIn("uncommitted-photo-token", _index_for(item.pk).metadata_text)

    @patch(
        "documents.services.photo_upload.generate_and_persist_photo_thumbnail",
        return_value=None,
    )
    @patch(
        "documents.services.photo_upload.head_s3_object",
        return_value=_s3_head_ok(),
    )
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_public_search_finds_item_only_after_child_becomes_renderable(
        self, _mock_put, _mock_head, _mock_thumbnail
    ):
        item = _create_photo_item(title="Public gallery album")
        _add_photo(item, position=1, description="primary-renderable-caption")
        _rebuild(item.pk)
        _item, pending, _url = create_additional_photo_upload_plan(
            archive_item=item,
            bucket="test-uploads-bucket",
            original_name="second.jpg",
            mime_type="image/jpeg",
            description="child-only-search-token",
        )
        qs = archive_browse_queryset_for_user(None)
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "child-only-search-token")),
            [],
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "child-only-search-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)

        finalize_photo_upload(
            pending,
            bucket="test-uploads-bucket",
            success=True,
            file_mime="image/jpeg",
        )
        qs = archive_browse_queryset_for_user(None)
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "child-only-search-token")),
            [item.pk],
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "child-only-search-token"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertContains(
            resp,
            reverse("archive-detail", kwargs={"item_id": item.pk}),
        )

    def test_thumbnail_generation_does_not_refresh_search_index(self):
        item = _create_photo_item(title="Thumbnail album")
        photo = _add_photo(item, position=1, description="already-indexed-caption")
        _rebuild(item.pk)
        with (
            patch(
                "documents.services.photo_thumbnail.get_object_bytes",
                return_value=(b"img", "image/jpeg"),
            ),
            patch(
                "documents.services.photo_thumbnail.generate_photo_thumbnail_bytes",
                return_value=(b"jpeg", 10, 10),
            ),
            patch(
                "documents.services.photo_thumbnail.put_object_bytes",
                return_value=12,
            ),
            patch(
                "documents.services.archive_search_index.sync_archive_item_search_index"
            ) as mocked_sync,
        ):
            result = generate_and_persist_photo_thumbnail(
                photo, bucket="test-uploads-bucket"
            )
        self.assertIsNotNone(result)
        mocked_sync.assert_not_called()
        self.assertIn("already-indexed-caption", _index_for(item.pk).metadata_text)

    def test_backfill_rebuilds_photo_aggregation(self):
        item = _create_photo_item(title="Backfill album")
        _add_photo(item, position=1, description="first-backfill")
        _add_photo(item, position=2, description="second-backfill-token")
        ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).delete()
        call_command(
            "backfill_archive_search_index",
            archive_item_id=item.pk,
            stdout=StringIO(),
        )
        index = _index_for(item.pk)
        self.assertIn("second-backfill-token", index.metadata_text)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "second-backfill-token"
                )
            ),
            [item.pk],
        )


class PhotoSearchIndexPerformanceTests(TestCase):
    def test_rebuild_query_count_does_not_scale_with_photo_or_person_count(self):
        small = _create_photo_item(title="Small album")
        large = _create_photo_item(title="Large album")
        people = [Person.objects.create(name=f"Person {index}") for index in range(4)]
        for position in range(1, 4):
            photo = _add_photo(
                small, position=position, description=f"small-{position}"
            )
            photo.people.set(people[:2])
        for position in range(1, 7):
            photo = _add_photo(
                large, position=position, description=f"large-{position}"
            )
            photo.people.set(people)

        small_item = _load_item(small.pk)
        large_item = _load_item(large.pk)
        with CaptureQueriesContext(connection) as small_ctx:
            rebuild_archive_item_search_index(small_item)
        with CaptureQueriesContext(connection) as large_ctx:
            rebuild_archive_item_search_index(large_item)
        self.assertLessEqual(len(small_ctx), 12)
        self.assertEqual(len(small_ctx), len(large_ctx))

    def test_sync_one_item_does_not_loop_per_photo(self):
        item = _create_photo_item(title="Sync once")
        people = [
            Person.objects.create(name=f"Sync person {index}") for index in range(3)
        ]
        for position in range(1, 5):
            photo = _add_photo(item, position=position, description=f"sync-{position}")
            photo.people.set(people)
        with patch(
            "documents.services.archive_search_index.rebuild_archive_item_search_index",
            wraps=rebuild_archive_item_search_index,
        ) as wrapped:
            with CaptureQueriesContext(connection) as ctx:
                sync_archive_item_search_index(item.pk)
        self.assertEqual(wrapped.call_count, 1)
        self.assertLessEqual(len(ctx), 20)
