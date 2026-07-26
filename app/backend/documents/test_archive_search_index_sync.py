"""PR2a ArchiveItemSearchIndex write-path sync and drift verification."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from documents.admin import ArchiveCategoryAdmin, ArchiveEventAdmin, TagAdmin
from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemSearchIndex,
    ArchiveMetadataSuggestion,
    Document,
    Tag,
)
from documents.services.archive_discovery_metadata_backfill import (
    apply_archive_discovery_metadata_backfill,
    build_archive_discovery_metadata_backfill_report,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    update_archive_item_discovery_metadata,
    update_manual_text_archive_item,
    update_ocr_document_metadata,
    update_photo_archive_item_metadata,
)
from documents.services.archive_metadata_suggestion_review import approve_suggestion
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    rebuild_archive_item_search_index,
    sync_archive_item_search_index,
    sync_archive_item_search_indexes,
)
from documents.services.photo_upload import create_photo_upload_plan


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _index_for(archive_item_id: int) -> ArchiveItemSearchIndex:
    return ArchiveItemSearchIndex.objects.get(archive_item_id=archive_item_id)


class SyncArchiveItemSearchIndexApiTests(TestCase):
    def test_reloads_fresh_m2m_instead_of_stale_prefetch(self):
        item = create_manual_text_archive_item(title="Stale prefetch", body="Body")
        stale = _load_item(item.pk)
        self.assertEqual(list(stale.categories.all()), [])

        cat = ArchiveCategory.objects.create(name="Fresh Cat", slug="fresh-cat")
        ArchiveItem.objects.get(pk=item.pk).categories.add(cat)

        stale_content = build_archive_item_search_content(stale)
        self.assertNotIn("Fresh Cat", stale_content.metadata_text)

        index = sync_archive_item_search_index(item.pk)
        self.assertIsNotNone(index)
        assert index is not None
        self.assertIn("Fresh Cat", index.metadata_text)

    def test_missing_item_returns_none_for_delete_race(self):
        self.assertIsNone(sync_archive_item_search_index(9_999_999))

    def test_unexpected_error_propagates(self):
        item = create_manual_text_archive_item(title="Boom", body="Body")
        with patch(
            "documents.services.archive_search_index.rebuild_archive_item_search_index",
            side_effect=RuntimeError("unexpected rebuild failure"),
        ):
            with self.assertRaises(RuntimeError):
                sync_archive_item_search_index(item.pk)

    def test_sync_failure_rolls_back_source_write(self):
        item = create_manual_text_archive_item(title="Before title", body="Body")
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_index",
            side_effect=DatabaseError("index write failed"),
        ):
            with self.assertRaises(DatabaseError):
                update_manual_text_archive_item(
                    item,
                    title="After title",
                    body="Body",
                    visibility=ArchiveItem.Visibility.PRIVATE,
                    date_start=None,
                    date_end=None,
                    date_precision=ArchiveItem.DatePrecision.UNKNOWN,
                    metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
                )
        item.refresh_from_db()
        self.assertEqual(item.title, "Before title")

    def test_bulk_helper_dedupes_and_skips_missing(self):
        a = create_manual_text_archive_item(title="A", body="a")
        b = create_manual_text_archive_item(title="B", body="b")
        self.assertLess(a.pk, b.pk)
        synced = sync_archive_item_search_indexes([b.pk, a.pk, a.pk, 9_999_999, b.pk])
        self.assertEqual([row.archive_item_id for row in synced], [a.pk, b.pk])

    def test_fan_out_failure_rolls_back_earlier_index_updates(self):
        first = create_manual_text_archive_item(title="First", body="a")
        second = create_manual_text_archive_item(title="Second", body="b")
        self.assertLess(first.pk, second.pk)
        ArchiveItemSearchIndex.objects.filter(archive_item_id=first.pk).update(
            title_text="stale-first"
        )
        ArchiveItemSearchIndex.objects.filter(archive_item_id=second.pk).update(
            title_text="stale-second"
        )
        original = rebuild_archive_item_search_index

        def flaky(item: ArchiveItem):
            if item.pk == second.pk:
                raise DatabaseError("later id failed")
            return original(item)

        with patch(
            "documents.services.archive_search_index.rebuild_archive_item_search_index",
            side_effect=flaky,
        ):
            with self.assertRaises(DatabaseError):
                sync_archive_item_search_indexes([second.pk, first.pk])

        self.assertEqual(_index_for(first.pk).title_text, "stale-first")
        self.assertEqual(_index_for(second.pk).title_text, "stale-second")


class ManualTextSearchIndexSyncTests(TestCase):
    def test_create_and_update_sync_index(self):
        item = create_manual_text_archive_item(
            title="Manual create",
            body="Create body",
            author_name="Author",
            public_note="Note",
        )
        index = _index_for(item.pk)
        self.assertEqual(index.title_text, "Manual create")
        self.assertEqual(index.body_text, "Create body")
        self.assertIn("Author", index.metadata_text)
        self.assertIn("Note", index.metadata_text)

        update_manual_text_archive_item(
            item,
            title="Manual updated",
            body="Updated body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            author_name="New author",
            source_title="Source",
            public_note="New note",
        )
        index.refresh_from_db()
        self.assertEqual(index.title_text, "Manual updated")
        self.assertEqual(index.body_text, "Updated body")
        self.assertIn("New author", index.metadata_text)
        self.assertIn("Source", index.metadata_text)
        self.assertIn("New note", index.metadata_text)


class OcrMetadataSearchIndexSyncTests(TestCase):
    def test_create_and_update_sync_without_ocr_body_hooks(self):
        doc = create_ocr_document(
            title="OCR create title",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            author_name="OCR author",
            source_title="OCR source",
            public_note="OCR note",
        )
        index = _index_for(doc.archive_item_id)
        self.assertEqual(index.title_text, "OCR create title")
        self.assertIn("OCR author", index.metadata_text)
        self.assertIn("OCR source", index.metadata_text)
        self.assertIn("OCR note", index.metadata_text)
        self.assertEqual(index.body_text, "")

        update_ocr_document_metadata(
            doc,
            title="OCR updated title",
            visibility=Document.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            author_name="Updated author",
            source_title="Updated source",
            public_note="Updated note",
        )
        index.refresh_from_db()
        self.assertEqual(index.title_text, "OCR updated title")
        self.assertIn("Updated author", index.metadata_text)
        self.assertIn("Updated source", index.metadata_text)
        self.assertIn("Updated note", index.metadata_text)
        self.assertEqual(index.body_text, "")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoSearchIndexSyncTests(TestCase):
    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_create_with_empty_discovery_syncs_index(self, _mock_put):
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
            public_note="Photo note",
        )
        index = _index_for(item.pk)
        self.assertEqual(index.title_text, "Photo create")
        self.assertIn("Photo note", index.metadata_text)
        self.assertEqual(index.body_text, "")

    @patch(
        "documents.services.photo_upload.create_presigned_put",
        return_value="https://example.test/put",
    )
    def test_update_syncs_title_and_public_note(self, _mock_put):
        item, _photo, _url = create_photo_upload_plan(
            bucket="test-uploads-bucket",
            title="Photo before",
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
        update_photo_archive_item_metadata(
            item,
            title="Photo after",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=None,
            date_end=None,
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
            metadata_status=ArchiveItem.MetadataStatus.COMPLETED,
            public_note="Updated photo note",
        )
        index = _index_for(item.pk)
        self.assertEqual(index.title_text, "Photo after")
        self.assertIn("Updated photo note", index.metadata_text)


class DiscoveryMetadataSearchIndexSyncTests(TestCase):
    def test_replace_and_clear_sync(self):
        item = create_manual_text_archive_item(title="Discovery", body="Body")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Cat One"],
            event_names=["Event One"],
            tag_names=["tag-one"],
        )
        index = _index_for(item.pk)
        self.assertIn("Cat One", index.metadata_text)
        self.assertIn("Event One", index.metadata_text)
        self.assertIn("tag-one", index.metadata_text)

        update_archive_item_discovery_metadata(
            item,
            category_names=[],
            event_names=[],
            tag_names=[],
        )
        index.refresh_from_db()
        self.assertNotIn("Cat One", index.metadata_text)
        self.assertNotIn("Event One", index.metadata_text)
        self.assertNotIn("tag-one", index.metadata_text)


class MetadataSuggestionSearchIndexSyncTests(TestCase):
    def test_approve_suggestion_syncs_added_names(self):
        staff = User.objects.create_user(
            username="suggestion_sync_staff",
            password="test-pass",
            is_staff=True,
        )
        item = create_manual_text_archive_item(title="Suggest", body="Body")
        suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="Submitter",
            suggested_categories="Suggested Cat",
            suggested_events="Suggested Event",
            suggested_tags="suggested-tag",
            status=ArchiveMetadataSuggestion.Status.PENDING,
        )
        approve_suggestion(suggestion.id, reviewer=staff)
        index = _index_for(item.pk)
        self.assertIn("Suggested Cat", index.metadata_text)
        self.assertIn("Suggested Event", index.metadata_text)
        self.assertIn("suggested-tag", index.metadata_text)


class DiscoveryBackfillSearchIndexSyncTests(TestCase):
    def test_apply_additive_links_syncs_index(self):
        doc = create_ocr_document(
            title="Backfill sync",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PRIVATE,
            category_event="Legacy Category",
        )
        tag = Tag.objects.create(name="legacy-tag")
        doc.tags_m2m.add(tag)
        index_before = _index_for(doc.archive_item_id)
        self.assertNotIn("legacy-tag", index_before.metadata_text)
        self.assertNotIn("Legacy Category", index_before.metadata_text)

        report = build_archive_discovery_metadata_backfill_report(
            document_id=doc.pk,
        )
        apply_archive_discovery_metadata_backfill(report)

        index = _index_for(doc.archive_item_id)
        self.assertIn("legacy-tag", index.metadata_text)
        self.assertIn("Legacy Category", index.metadata_text)


class TaxonomyRenameSearchIndexSyncTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get("/admin/")
        self.request.user = User.objects.create_superuser(
            username="taxonomy_admin",
            email="admin@example.com",
            password="test-pass",
        )
        self.site = AdminSite()

    def test_tag_category_and_event_rename_fan_out(self):
        item = create_manual_text_archive_item(title="Taxonomy", body="Body")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Old Category"],
            event_names=["Old Event"],
            tag_names=["old-tag"],
        )
        index = _index_for(item.pk)
        self.assertIn("Old Category", index.metadata_text)
        self.assertIn("Old Event", index.metadata_text)
        self.assertIn("old-tag", index.metadata_text)

        category = ArchiveCategory.objects.get(name="Old Category")
        category.name = "Renamed Category"
        ArchiveCategoryAdmin(ArchiveCategory, self.site).save_model(
            self.request, category, form=MagicMock(), change=True
        )

        event = ArchiveEvent.objects.get(name="Old Event")
        event.name = "Renamed Event"
        ArchiveEventAdmin(ArchiveEvent, self.site).save_model(
            self.request, event, form=MagicMock(), change=True
        )

        tag = Tag.objects.get(name="old-tag")
        tag.name = "renamed-tag"
        TagAdmin(Tag, self.site).save_model(
            self.request, tag, form=MagicMock(), change=True
        )

        index.refresh_from_db()
        self.assertIn("Renamed Category", index.metadata_text)
        self.assertIn("Renamed Event", index.metadata_text)
        self.assertIn("renamed-tag", index.metadata_text)
        self.assertNotIn("Old Category", index.metadata_text)
        self.assertNotIn("Old Event", index.metadata_text)
        self.assertNotIn("old-tag", index.metadata_text)

    def test_save_model_unchanged_name_does_not_fan_out(self):
        item = create_manual_text_archive_item(title="Unchanged taxonomy", body="Body")
        update_archive_item_discovery_metadata(
            item,
            category_names=[],
            event_names=[],
            tag_names=["stable-tag"],
        )
        tag = Tag.objects.get(name="stable-tag")
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mock_fan_out:
            TagAdmin(Tag, self.site).save_model(
                self.request, tag, form=MagicMock(), change=True
            )
            mock_fan_out.assert_not_called()


class ArchiveItemDeleteSearchIndexTests(TestCase):
    def test_cascade_still_removes_index(self):
        item = create_manual_text_archive_item(title="Delete me", body="Body")
        item_id = item.pk
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item_id).exists()
        )
        item.delete()
        self.assertFalse(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item_id).exists()
        )


class DriftCheckOnlyCommandTests(TestCase):
    def test_check_only_passes_on_correct_index(self):
        create_manual_text_archive_item(title="Healthy", body="Body")
        out = StringIO()
        call_command(
            "backfill_archive_search_index",
            check_only=True,
            stdout=out,
        )
        self.assertIn("Drift verification passed", out.getvalue())

    def test_check_only_detects_missing_row(self):
        item = create_manual_text_archive_item(title="Missing", body="Body")
        ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).delete()
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "backfill_archive_search_index",
                check_only=True,
                stdout=StringIO(),
                stderr=err,
            )
        self.assertIn(f"archive_item_id(s): {item.pk}", err.getvalue())
        self.assertIn("missing", err.getvalue())

    def test_check_only_detects_content_mismatch(self):
        item = create_manual_text_archive_item(title="Match me", body="Body")
        ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).update(
            title_text="drifted-title"
        )
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "backfill_archive_search_index",
                check_only=True,
                archive_item_id=item.pk,
                stdout=StringIO(),
                stderr=err,
            )
        err_text = err.getvalue()
        self.assertIn(f"archive_item_id(s): {item.pk}", err_text)
        self.assertIn("content_mismatch", err_text)
        self.assertNotIn("drifted-title", err_text)
        self.assertNotIn("Match me", err_text)

    def test_check_only_detects_null_vector(self):
        item = create_manual_text_archive_item(title="Null vector", body="Body")
        ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).update(
            search_vector=None
        )
        err = StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "backfill_archive_search_index",
                check_only=True,
                archive_item_id=item.pk,
                stdout=StringIO(),
                stderr=err,
            )
        self.assertIn("null_vector", err.getvalue())
        self.assertIn(str(item.pk), err.getvalue())

    def test_check_only_detects_extra_rows(self):
        item = create_manual_text_archive_item(title="Extra", body="Body")
        err = StringIO()
        with patch(
            "documents.management.commands.backfill_archive_search_index."
            "ArchiveItem.objects.order_by",
            return_value=ArchiveItem.objects.none(),
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "backfill_archive_search_index",
                    check_only=True,
                    stdout=StringIO(),
                    stderr=err,
                )
        err_text = err.getvalue()
        self.assertIn("extra", err_text)
        self.assertIn(str(item.pk), err_text)

    def test_check_only_performs_no_writes(self):
        item = create_manual_text_archive_item(title="No writes", body="Body")
        index = _index_for(item.pk)
        before_updated_at = index.updated_at
        before_title = index.title_text
        with patch(
            "documents.management.commands.backfill_archive_search_index."
            "rebuild_archive_item_search_index",
            side_effect=AssertionError("check-only must not rebuild"),
        ):
            call_command(
                "backfill_archive_search_index",
                check_only=True,
                archive_item_id=item.pk,
                stdout=StringIO(),
            )
        index.refresh_from_db()
        self.assertEqual(index.updated_at, before_updated_at)
        self.assertEqual(index.title_text, before_title)

    def test_check_only_unknown_item_errors(self):
        with self.assertRaises(CommandError):
            call_command(
                "backfill_archive_search_index",
                check_only=True,
                archive_item_id=9_999_999,
                stdout=StringIO(),
            )


class PublicSearchUnchangedWithSyncTests(TestCase):
    def test_public_archive_q_still_ignores_manual_body(self):
        item = create_manual_text_archive_item(
            title="Visible title sync",
            body="unique-sync-body-token-zzz",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).exists()
        )
        qs = ArchiveItem.objects.all()
        self.assertFalse(
            filter_archive_items_by_search_query(
                qs, "unique-sync-body-token-zzz"
            ).exists()
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "unique-sync-body-token-zzz"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "Visible title sync")
