"""ArchiveItemPerson public q search: item-level identities, not photo appearance."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_people import (
    ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR,
    ArchiveItemPersonError,
    create_archive_item_person,
    delete_archive_item_person,
)
from documents.services.archive_item_presentation import (
    build_archive_browse_card,
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.services.archive_search_index import (
    SEARCH_SEGMENT_SEPARATOR,
    archive_item_ids_for_person_item_links,
    archive_item_ids_for_person_photo_appearances,
    archive_item_ids_for_person_search_refresh,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
)
from documents.services.archive_search_snippets import (
    MATCH_SOURCE_ITEM_DETAILS,
    MATCH_SOURCE_TAGS,
    build_archive_search_match_presentation,
)
from documents.services.photo_content_management import (
    create_person_alias,
    delete_person_alias,
    update_person_alias,
    update_person_name,
)
from documents.test_archive_item import create_viewable_ocr_document

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


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
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    description: str = "",
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
) -> PhotoContent:
    if original_file_key is None:
        original_file_key = f"photos/{item.pk}-{position}/original.jpg"
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=original_file_key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
        upload_error="",
        description=description,
    )


def _grant_restricted_permission(user: User) -> User:
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    if hasattr(user, "_perm_cache"):
        delattr(user, "_perm_cache")
    if hasattr(user, "_user_perm_cache"):
        delattr(user, "_user_perm_cache")
    return user


def _add_ocr_source_text(document: Document, text: str) -> None:
    DocumentTextResult.objects.create(
        document=document,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine="engine-a",
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
    )


class ArchiveItemPersonSearchBuilderTests(TestCase):
    def test_indexes_canonical_name_and_aliases_in_person_then_alias_order(self):
        item = create_manual_text_archive_item(
            title="Order item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        zed = Person.objects.create(name="Zed Canonical")
        amy = Person.objects.create(name="Amy Canonical")
        PersonAlias.objects.create(person=zed, name="ZedAliasB")
        PersonAlias.objects.create(person=zed, name="ZedAliasA")
        PersonAlias.objects.create(person=amy, name="AmyAlias")
        ArchiveItemPerson.objects.create(archive_item=item, person=zed)
        ArchiveItemPerson.objects.create(archive_item=item, person=amy)

        content = build_archive_item_search_content(_load_item(item.pk))
        segments = content.metadata_text.split(SEARCH_SEGMENT_SEPARATOR)
        self.assertEqual(
            segments,
            [
                "Amy Canonical",
                "Zed Canonical",
                "AmyAlias",
                "ZedAliasA",
                "ZedAliasB",
            ],
        )

    def test_tag_and_archive_item_person_same_name_is_one_segment(self):
        item = create_manual_text_archive_item(
            title="Shared name",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="SharedPersonTagToken")
        tag = Tag.objects.create(name="SharedPersonTagToken")
        item.tags.add(tag)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("SharedPersonTagToken"), 1)

    def test_archive_item_person_and_photo_person_same_person_is_one_segment(self):
        item = _create_photo_item(title="Both relations")
        photo = _add_photo(item, position=1, description="crowd")
        person = Person.objects.create(name="BothRelationToken")
        PersonAlias.objects.create(person=person, name="BothAliasToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.metadata_text.count("BothRelationToken"), 1)
        self.assertEqual(content.metadata_text.count("BothAliasToken"), 1)

    def test_non_renderable_photo_person_stays_excluded_without_item_link(self):
        item = _create_photo_item(title="Pending only")
        pending = _add_photo(
            item,
            position=1,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        person = Person.objects.create(name="PendingOnlyPersonToken")
        PersonAlias.objects.create(person=person, name="PendingOnlyAliasToken")
        PhotoPerson.objects.create(photo_content=pending, person=person)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertNotIn("PendingOnlyPersonToken", content.metadata_text)
        self.assertNotIn("PendingOnlyAliasToken", content.metadata_text)

    def test_item_link_indexes_even_when_photo_person_is_not_renderable(self):
        item = _create_photo_item(title="Item link plus pending photo")
        pending = _add_photo(
            item,
            position=1,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        person = Person.objects.create(name="ItemLinkDespitePendingToken")
        PersonAlias.objects.create(person=person, name="ItemLinkPendingAliasToken")
        PhotoPerson.objects.create(photo_content=pending, person=person)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertIn("ItemLinkDespitePendingToken", content.metadata_text)
        self.assertIn("ItemLinkPendingAliasToken", content.metadata_text)


class ArchiveItemPersonSearchQueryTests(TestCase):
    def test_canonical_name_matches_q_once(self):
        item = create_manual_text_archive_item(
            title="Linked letter",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        other = create_manual_text_archive_item(
            title="Unrelated letter",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="CanonicalSearchToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        _rebuild(item.pk)
        _rebuild(other.pk)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "CanonicalSearchToken"
                )
            ),
            [item.pk],
        )

    def test_alias_matches_q_once(self):
        item = create_manual_text_archive_item(
            title="Alias letter",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="AliasSearchToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        _rebuild(item.pk)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "AliasSearchToken"
                )
            ),
            [item.pk],
        )

    def test_tag_plus_archive_item_person_same_name_is_one_result(self):
        item = create_manual_text_archive_item(
            title="Tagged person item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="DupNameSearchToken")
        tag = Tag.objects.create(name="DupNameSearchToken")
        item.tags.add(tag)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        _rebuild(item.pk)
        ids = _ids(
            filter_archive_items_by_search_query(
                ArchiveItem.objects.all(), "DupNameSearchToken"
            )
        )
        self.assertEqual(ids, [item.pk])

    def test_both_relations_on_one_photo_item_is_one_result(self):
        item = _create_photo_item(title="Photo both links")
        photo = _add_photo(item, position=1)
        person = Person.objects.create(name="BothSearchToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        _rebuild(item.pk)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "BothSearchToken"
                )
            ),
            [item.pk],
        )

    def test_item_person_only_hit_is_not_photo_appearance_or_deeplink(self):
        item = _create_photo_item(title="Public item person album")
        photo = _add_photo(item, position=1, description="crowd")
        person = Person.objects.create(name="ItemOnlyHitToken")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)
        index = _rebuild(item.pk)

        ids = _ids(
            filter_archive_items_by_search_query(
                archive_browse_queryset_for_user(None),
                "ItemOnlyHitToken",
            )
        )
        self.assertEqual(ids, [item.pk])
        self.assertFalse(photo.people.filter(pk=person.pk).exists())
        self.assertEqual(PhotoPerson.objects.filter(person=person).count(), 0)

        resp = self.client.get(reverse("archive-list"), {"q": "ItemOnlyHitToken"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        card = resp.context["browse_cards"][0]
        expected = reverse("archive-detail", kwargs={"item_id": item.pk})
        self.assertEqual(card.detail_url, expected)
        self.assertNotIn("photo=", card.detail_url)
        self.assertNotIn(str(photo.pk), card.detail_url)
        self.assertNotIn("person=", card.detail_url)

        presentation = build_archive_search_match_presentation(
            archive_item=item,
            search_index=index,
            terms=("ItemOnlyHitToken",),
        )
        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertEqual(presentation.match_source_label, MATCH_SOURCE_ITEM_DETAILS)
        self.assertEqual(presentation.snippet_segments, ())
        self.assertFalse(presentation.replaces_preview)

        built = build_archive_browse_card(item, search_query="ItemOnlyHitToken")
        self.assertEqual(built.detail_url, expected)
        self.assertNotIn("photo=", built.detail_url)

    def test_video_ocr_manual_and_photo_item_types_all_match(self):
        person = Person.objects.create(name="AllTypesPersonToken")
        PersonAlias.objects.create(person=person, name="AllTypesAliasToken")

        manual = create_manual_text_archive_item(
            title="Manual linked",
            body="manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ocr = create_viewable_ocr_document(
            title="OCR linked",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _add_ocr_source_text(ocr, "ocr body")
        video = create_video_archive_item(
            title="Video linked",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = _create_photo_item(title="Photo linked")
        _add_photo(photo, position=1)

        for item in (manual, ocr.archive_item, video, photo):
            ArchiveItemPerson.objects.create(archive_item=item, person=person)
            _rebuild(item.pk)

        qs = archive_browse_queryset_for_user(None)
        canonical_ids = _ids(
            filter_archive_items_by_search_query(qs, "AllTypesPersonToken")
        )
        alias_ids = _ids(filter_archive_items_by_search_query(qs, "AllTypesAliasToken"))
        expected = {manual.pk, ocr.archive_item_id, video.pk, photo.pk}
        self.assertEqual(set(canonical_ids), expected)
        self.assertEqual(set(alias_ids), expected)

    def test_tag_q_and_tag_filter_behavior_unchanged(self):
        tagged = create_manual_text_archive_item(
            title="Tagged item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        linked_only = create_manual_text_archive_item(
            title="Person linked only",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        tag = Tag.objects.create(name="ExistingTagSearchToken")
        tagged.tags.add(tag)
        person = Person.objects.create(name="UnrelatedPersonToken")
        ArchiveItemPerson.objects.create(archive_item=linked_only, person=person)
        _rebuild(tagged.pk)
        _rebuild(linked_only.pk)

        qs = ArchiveItem.objects.all()
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "ExistingTagSearchToken")),
            [tagged.pk],
        )
        tag_filter_ids = _ids(
            filter_archive_items_by_advanced_filters(
                qs,
                normalize_archive_advanced_filters([("tag", str(tag.id))]),
            )
        )
        self.assertEqual(tag_filter_ids, [tagged.pk])
        self.assertNotIn(linked_only.pk, tag_filter_ids)

        presentation = build_archive_search_match_presentation(
            archive_item=tagged,
            search_index=_index_for(tagged.pk),
            terms=("ExistingTagSearchToken",),
        )
        self.assertIsNotNone(presentation)
        assert presentation is not None
        self.assertEqual(presentation.match_source_label, MATCH_SOURCE_TAGS)


class ArchiveItemPersonSearchAccessTests(TestCase):
    def setUp(self):
        self.family = User.objects.create_user(
            username="aip_family",
            password="test-pass",
        )
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(
                username="aip_restricted",
                password="test-pass",
            )
        )

    def test_private_and_restricted_item_links_do_not_leak_anonymously(self):
        private = create_manual_text_archive_item(
            title="Private linked",
            body="body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted = create_manual_text_archive_item(
            title="Restricted linked",
            body="body",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        person = Person.objects.create(name="HiddenPersonSearchToken")
        ArchiveItemPerson.objects.create(archive_item=private, person=person)
        ArchiveItemPerson.objects.create(archive_item=restricted, person=person)
        _rebuild(private.pk)
        _rebuild(restricted.pk)

        token = "HiddenPersonSearchToken"
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    archive_browse_queryset_for_user(None), token
                )
            ),
            [],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    archive_browse_queryset_for_user(self.family), token
                )
            ),
            [private.pk],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    archive_browse_queryset_for_user(self.restricted_user), token
                )
            ),
            [restricted.pk],
        )

        resp = self.client.get(reverse("archive-list"), {"q": token})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertNotContains(
            resp, reverse("archive-detail", kwargs={"item_id": private.pk})
        )
        self.assertNotContains(
            resp, reverse("archive-detail", kwargs={"item_id": restricted.pk})
        )


class ArchiveItemPersonSearchRefreshTests(TestCase):
    def test_create_and_delete_writer_refreshes_index(self):
        item = create_manual_text_archive_item(
            title="Writer item",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="WriterPersonToken")
        PersonAlias.objects.create(person=person, name="WriterAliasToken")
        _rebuild(item.pk)
        self.assertNotIn("WriterPersonToken", _index_for(item.pk).metadata_text)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            link = create_archive_item_person(archive_item=item, person=person)
        self.assertEqual(wrapped.call_count, 1)
        self.assertEqual(wrapped.call_args.args, (item.pk,))
        self.assertIn("WriterPersonToken", _index_for(item.pk).metadata_text)
        self.assertIn("WriterAliasToken", _index_for(item.pk).metadata_text)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "WriterPersonToken"
                )
            ),
            [item.pk],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            delete_archive_item_person(link)
        self.assertEqual(wrapped.call_count, 1)
        self.assertNotIn("WriterPersonToken", _index_for(item.pk).metadata_text)
        self.assertNotIn("WriterAliasToken", _index_for(item.pk).metadata_text)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(), "WriterPersonToken"
                )
            ),
            [],
        )

    def test_delete_keeps_identity_when_photo_person_still_present(self):
        item = _create_photo_item(title="Keep photo person")
        photo = _add_photo(item, position=1)
        person = Person.objects.create(name="KeepPhotoPersonToken")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        link = ArchiveItemPerson.objects.create(archive_item=item, person=person)
        _rebuild(item.pk)
        delete_archive_item_person(link)
        self.assertIn("KeepPhotoPersonToken", _index_for(item.pk).metadata_text)
        self.assertTrue(PhotoPerson.objects.filter(photo_content=photo).exists())

    def test_duplicate_create_raises_without_second_row(self):
        item = create_manual_text_archive_item(
            title="Dup link",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="DupLinkPerson")
        create_archive_item_person(archive_item=item, person=person)
        with self.assertRaises(ArchiveItemPersonError) as ctx:
            create_archive_item_person(archive_item=item, person=person)
        self.assertEqual(str(ctx.exception), ARCHIVE_ITEM_PERSON_DUPLICATE_ERROR)
        self.assertEqual(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).count(),
            1,
        )

    def test_sync_failure_rolls_back_create(self):
        item = create_manual_text_archive_item(
            title="Rollback link",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="RollbackPersonToken")
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                create_archive_item_person(archive_item=item, person=person)
        self.assertFalse(
            ArchiveItemPerson.objects.filter(archive_item=item, person=person).exists()
        )
        self.assertNotIn("RollbackPersonToken", _index_for(item.pk).metadata_text)

    def test_raw_model_create_does_not_sync_index(self):
        item = create_manual_text_archive_item(
            title="Raw create",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="RawCreatePersonToken")
        _rebuild(item.pk)
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index"
        ) as mocked:
            ArchiveItemPerson.objects.create(archive_item=item, person=person)
        mocked.assert_not_called()
        self.assertNotIn("RawCreatePersonToken", _index_for(item.pk).metadata_text)

    def test_person_rename_refreshes_item_and_photo_links_once(self):
        photo_item = _create_photo_item(title="Photo linked")
        item_only = create_manual_text_archive_item(
            title="Item linked",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        both = _create_photo_item(title="Both linked")
        unrelated = create_manual_text_archive_item(
            title="Unrelated",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        person = Person.objects.create(name="OldCanonicalName")
        other = Person.objects.create(name="OtherCanonical")
        PhotoPerson.objects.create(
            photo_content=_add_photo(photo_item, position=1), person=person
        )
        ArchiveItemPerson.objects.create(archive_item=item_only, person=person)
        PhotoPerson.objects.create(
            photo_content=_add_photo(both, position=1), person=person
        )
        ArchiveItemPerson.objects.create(archive_item=both, person=person)
        ArchiveItemPerson.objects.create(archive_item=unrelated, person=other)
        for archive_item in (photo_item, item_only, both, unrelated):
            _rebuild(archive_item.pk)

        self.assertEqual(
            archive_item_ids_for_person_item_links(person.pk),
            [item_only.pk, both.pk],
        )
        self.assertEqual(
            archive_item_ids_for_person_photo_appearances(person.pk),
            [photo_item.pk, both.pk],
        )
        self.assertEqual(
            archive_item_ids_for_person_search_refresh(person.pk),
            [photo_item.pk, item_only.pk, both.pk],
        )

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            update_person_name(person, name="NewCanonicalNameToken")
        self.assertEqual(wrapped.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in wrapped.call_args_list],
            [photo_item.pk, item_only.pk, both.pk],
        )
        self.assertIn("NewCanonicalNameToken", _index_for(photo_item.pk).metadata_text)
        self.assertIn("NewCanonicalNameToken", _index_for(item_only.pk).metadata_text)
        self.assertIn("NewCanonicalNameToken", _index_for(both.pk).metadata_text)
        self.assertNotIn("OldCanonicalName", _index_for(both.pk).metadata_text)
        self.assertNotIn(
            "NewCanonicalNameToken", _index_for(unrelated.pk).metadata_text
        )

    def test_alias_create_edit_delete_refreshes_item_and_photo_links_once(self):
        photo_item = _create_photo_item(title="Photo linked")
        item_only = create_manual_text_archive_item(
            title="Item linked",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        both = _create_photo_item(title="Both linked")
        person = Person.objects.create(name="יעקב כהן")
        PhotoPerson.objects.create(
            photo_content=_add_photo(photo_item, position=1), person=person
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(both, position=1), person=person
        )
        ArchiveItemPerson.objects.create(archive_item=item_only, person=person)
        ArchiveItemPerson.objects.create(archive_item=both, person=person)
        for archive_item in (photo_item, item_only, both):
            _rebuild(archive_item.pk)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            alias = create_person_alias(person, name="Jacob Cohen")
        self.assertEqual(wrapped.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in wrapped.call_args_list],
            [photo_item.pk, item_only.pk, both.pk],
        )
        self.assertIn("Jacob Cohen", _index_for(item_only.pk).metadata_text)
        self.assertIn("Jacob Cohen", _index_for(both.pk).metadata_text)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            update_person_alias(alias, name="Yaakov Cohen")
        self.assertEqual(wrapped.call_count, 3)
        self.assertIn("Yaakov Cohen", _index_for(item_only.pk).metadata_text)
        self.assertNotIn("Jacob Cohen", _index_for(item_only.pk).metadata_text)

        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            wraps=sync_archive_item_search_index,
        ) as wrapped:
            delete_person_alias(alias)
        self.assertEqual(wrapped.call_count, 3)
        self.assertNotIn("Yaakov Cohen", _index_for(item_only.pk).metadata_text)
        self.assertIn("יעקב כהן", _index_for(item_only.pk).metadata_text)


class ArchiveItemPersonSearchPerformanceTests(TestCase):
    def test_full_index_build_query_count_does_not_scale_with_person_count(self):
        few = create_manual_text_archive_item(
            title="Few people",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        many = create_manual_text_archive_item(
            title="Many people",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        few_people = [
            Person.objects.create(name=f"Few person {index}") for index in range(2)
        ]
        many_people = [
            Person.objects.create(name=f"Many person {index}") for index in range(8)
        ]
        for person in few_people:
            ArchiveItemPerson.objects.create(archive_item=few, person=person)
            PersonAlias.objects.create(person=person, name=f"{person.name} alias")
        for person in many_people:
            ArchiveItemPerson.objects.create(archive_item=many, person=person)
            for alias_index in range(1, 5):
                PersonAlias.objects.create(
                    person=person,
                    name=f"{person.name} alias {alias_index}",
                )

        with CaptureQueriesContext(connection) as few_load:
            few_item = list(
                archive_items_for_search_index_build(archive_item_ids=[few.pk])
            )[0]
        with CaptureQueriesContext(connection) as many_load:
            many_item = list(
                archive_items_for_search_index_build(archive_item_ids=[many.pk])
            )[0]
        self.assertEqual(len(few_load), len(many_load))

        with CaptureQueriesContext(connection) as few_rebuild:
            rebuild_archive_item_search_index(few_item)
        with CaptureQueriesContext(connection) as many_rebuild:
            rebuild_archive_item_search_index(many_item)
        self.assertEqual(len(few_rebuild), len(many_rebuild))
        self.assertEqual(
            build_archive_item_search_content(few_item).metadata_text.count(
                "Few person"
            ),
            4,
        )
        self.assertGreater(
            build_archive_item_search_content(many_item).metadata_text.count(
                "Many person"
            ),
            8,
        )
