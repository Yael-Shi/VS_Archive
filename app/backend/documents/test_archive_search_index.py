"""PR1 ArchiveItemSearchIndex foundation: builder, persistence, backfill."""

from __future__ import annotations

from datetime import date
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemSearchIndex,
    Document,
    DocumentMetadata,
    DocumentTextResult,
    PhotoContent,
    Tag,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    SEARCH_SEGMENT_SEPARATOR,
    archive_items_for_search_index_build,
    build_archive_item_search_content,
    persist_archive_item_search_content,
    rebuild_archive_item_search_index,
)
from documents.services.text_presentation import get_displayed_transcription_text
from documents.test_archive_item import create_viewable_ocr_document


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _create_text_result(
    doc: Document,
    *,
    result_type: str,
    text: str,
    status: str,
    engine: str = "engine-a",
    engine_key: str = DocumentTextResult.OcrEngineKey.GEMINI,
    prompt_variant: str = DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
    verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=result_type,
        engine=engine,
        engine_key=engine_key,
        prompt_variant=prompt_variant,
        status=status,
        verification_status=verification_status,
        text=text,
    )


class ArchiveItemSearchIndexModelTests(TestCase):
    def test_cascade_delete_with_archive_item(self):
        item = create_manual_text_archive_item(title="Delete index", body="Body")
        rebuild_archive_item_search_index(_load_item(item.pk))
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).exists()
        )
        item_id = item.pk
        item.delete()
        self.assertFalse(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item_id).exists()
        )

    def test_one_to_one_related_name(self):
        item = create_manual_text_archive_item(title="Related", body="Body")
        index = rebuild_archive_item_search_index(_load_item(item.pk))
        item.refresh_from_db()
        self.assertEqual(item.search_index.pk, index.pk)

    def test_migration_declares_gin_and_search_vector(self):
        import importlib

        migration_module = importlib.import_module(
            "documents.migrations.0042_archive_item_search_index"
        )
        Migration = migration_module.Migration

        create_op = Migration.operations[0]
        self.assertEqual(create_op.name, "ArchiveItemSearchIndex")
        field_names = {name for name, _field in create_op.fields}
        self.assertIn("search_vector", field_names)
        self.assertIn("title_text", field_names)
        self.assertIn("metadata_text", field_names)
        self.assertIn("body_text", field_names)
        indexes = create_op.options["indexes"]
        self.assertEqual(len(indexes), 1)
        self.assertEqual(indexes[0].name, "archive_item_search_vector_gin")
        self.assertEqual(list(indexes[0].fields), ["search_vector"])


class ArchiveItemSearchIndexBuilderTests(TestCase):
    def test_includes_title_author_source_public_note_and_sorted_m2m_names(self):
        item = create_manual_text_archive_item(
            title="  Title  One  ",
            body="Body text",
            author_name="  Author Name ",
            source_title=" Source Title ",
            public_note=" Public note here ",
        )
        cat_b = ArchiveCategory.objects.create(name="Beta Cat", slug="beta-cat")
        cat_a = ArchiveCategory.objects.create(name="Alpha Cat", slug="alpha-cat")
        event_b = ArchiveEvent.objects.create(name="Beta Event", slug="beta-event")
        event_a = ArchiveEvent.objects.create(name="Alpha Event", slug="alpha-event")
        tag_b = Tag.objects.create(name="beta-tag")
        tag_a = Tag.objects.create(name="alpha-tag")
        item.categories.add(cat_b, cat_a)
        item.events.add(event_b, event_a)
        item.tags.add(tag_b, tag_a)

        content = build_archive_item_search_content(_load_item(item.pk))

        self.assertEqual(content.title_text, "Title One")
        self.assertEqual(
            content.metadata_text,
            SEARCH_SEGMENT_SEPARATOR.join(
                [
                    "Author Name",
                    "Source Title",
                    "Alpha Cat",
                    "Beta Cat",
                    "Alpha Event",
                    "Beta Event",
                    "alpha-tag",
                    "beta-tag",
                    "Public note here",
                ]
            ),
        )
        self.assertEqual(content.body_text, "Body text")

    def test_builder_is_deterministic_across_calls(self):
        item = create_manual_text_archive_item(
            title="Stable",
            body="Same body",
            author_name="A",
        )
        item.tags.add(Tag.objects.create(name="z"), Tag.objects.create(name="a"))
        loaded = _load_item(item.pk)
        first = build_archive_item_search_content(loaded)
        second = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(first, second)

    def test_excludes_document_metadata_dates_photo_details_and_technical_fields(self):
        doc = create_viewable_ocr_document(
            title="OCR isolation",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            date_start=date(1950, 1, 1),
            date_end=date(1950, 12, 31),
            date_precision=Document.DatePrecision.YEAR,
        )
        item = doc.archive_item
        item.public_note = "Allowed note"
        item.save(update_fields=["public_note", "updated_at"])
        DocumentMetadata.objects.create(
            document=doc,
            donor="secret-donor-term",
            collection="secret-collection-term",
            original_location="secret-location-term",
            notes="secret-notes-term",
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Displayed OCR body",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
        )

        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Photo item",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=photo_item,
            original_file_key="photos/x/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=10,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            description="photo-description-secret",
            location="photo-location-secret",
            context="photo-context-secret",
            people_present="photo-people-secret",
            notes="photo-notes-secret",
        )

        ocr_content = build_archive_item_search_content(_load_item(item.pk))
        photo_content = build_archive_item_search_content(_load_item(photo_item.pk))

        blob = f"{ocr_content.title_text}\n{ocr_content.metadata_text}\n{ocr_content.body_text}"
        self.assertIn("Allowed note", blob)
        self.assertIn("Displayed OCR body", blob)
        for forbidden in (
            "secret-donor-term",
            "secret-collection-term",
            "secret-location-term",
            "secret-notes-term",
            "1950",
            "GEMINI",
            "engine-a",
        ):
            self.assertNotIn(forbidden, blob)

        photo_blob = (
            f"{photo_content.title_text}\n"
            f"{photo_content.metadata_text}\n"
            f"{photo_content.body_text}"
        )
        for forbidden in (
            "photo-description-secret",
            "photo-location-secret",
            "photo-context-secret",
            "photo-people-secret",
            "photo-notes-secret",
        ):
            self.assertNotIn(forbidden, photo_blob)
        self.assertEqual(photo_content.body_text, "")

    def test_manual_text_body_selection(self):
        item = create_manual_text_archive_item(
            title="Manual",
            body="  Manual body content  ",
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        self.assertEqual(content.body_text, "Manual body content")

    def test_ocr_body_matches_displayed_transcription_helper(self):
        doc = create_viewable_ocr_document(
            title="Hebrew OCR",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Source only text",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="Hebrew preferred text",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine="engine-he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        )
        loaded = _load_item(doc.archive_item_id)
        content = build_archive_item_search_content(loaded)
        self.assertEqual(
            content.body_text,
            get_displayed_transcription_text(loaded.ocr_document),
        )
        self.assertEqual(content.body_text, "Hebrew preferred text")

    def test_failed_and_empty_text_excluded_via_display_rules(self):
        doc = create_viewable_ocr_document(
            title="Failed OCR",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Should not appear",
            status=DocumentTextResult.Status.FAILED,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine="engine-he",
            prompt_variant=DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
        )
        content = build_archive_item_search_content(_load_item(doc.archive_item_id))
        self.assertEqual(content.body_text, "")

    def test_multi_engine_prefers_displayable_selection(self):
        doc = create_viewable_ocr_document(
            title="Multi engine",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        older = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Older needs review",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            engine="engine-old",
        )
        newer_succeeded = _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Newer succeeded text",
            status=DocumentTextResult.Status.SUCCEEDED,
            engine="engine-new",
        )
        self.assertGreater(newer_succeeded.created_at, older.created_at)
        content = build_archive_item_search_content(_load_item(doc.archive_item_id))
        self.assertEqual(content.body_text, "Newer succeeded text")

    def test_displayable_rejected_text_is_included(self):
        doc = create_viewable_ocr_document(
            title="Rejected still displayable",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="Rejected but displayable body",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        content = build_archive_item_search_content(_load_item(doc.archive_item_id))
        self.assertEqual(content.body_text, "Rejected but displayable body")


class ArchiveItemSearchIndexPersistenceTests(TestCase):
    def _search_vector_text(self, index: ArchiveItemSearchIndex) -> str:
        from django.db import connection

        table = ArchiveItemSearchIndex._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT search_vector::text FROM {table} WHERE id = %s",
                [index.pk],
            )
            row = cursor.fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row[0])
        return row[0]

    def test_create_update_idempotent_and_materializes_weighted_search_vector(self):
        title_token = "uniqtitleaweight"
        meta_token = "uniqmetabweight"
        body_token = "uniqbodycweight"
        item = create_manual_text_archive_item(
            title=title_token,
            body=body_token,
            author_name=meta_token,
        )
        content = build_archive_item_search_content(_load_item(item.pk))
        first = persist_archive_item_search_content(content)
        self.assertEqual(first.title_text, title_token)
        self.assertEqual(first.body_text, body_token)
        self.assertIn(meta_token, first.metadata_text)

        vector_text = self._search_vector_text(first)
        self.assertRegex(vector_text, rf"'{title_token}':\d+A")
        self.assertRegex(vector_text, rf"'{meta_token}':\d+B")
        self.assertRegex(vector_text, rf"'{body_token}':\d+C")

        second = persist_archive_item_search_content(content)
        self.assertEqual(second.pk, first.pk)
        self.assertEqual(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).count(),
            1,
        )
        self.assertEqual(second.title_text, first.title_text)
        self.assertEqual(second.metadata_text, first.metadata_text)
        self.assertEqual(second.body_text, first.body_text)
        self.assertEqual(self._search_vector_text(second), vector_text)

        updated_title = "uniqtitleaweightupdated"
        item.title = updated_title
        item.save(update_fields=["title", "updated_at"])
        updated_content = build_archive_item_search_content(_load_item(item.pk))
        third = persist_archive_item_search_content(updated_content)
        self.assertEqual(third.pk, first.pk)
        self.assertEqual(third.title_text, updated_title)
        updated_vector = self._search_vector_text(third)
        self.assertRegex(updated_vector, rf"'{updated_title}':\d+A")
        self.assertRegex(updated_vector, rf"'{meta_token}':\d+B")
        self.assertRegex(updated_vector, rf"'{body_token}':\d+C")
        self.assertNotRegex(updated_vector, rf"'{title_token}':\d+A")


class ArchiveItemSearchIndexBackfillCommandTests(TestCase):
    def test_full_run_and_repeat_are_idempotent(self):
        a = create_manual_text_archive_item(title="A", body="Body A")
        b = create_manual_text_archive_item(title="B", body="Body B")
        out = StringIO()
        call_command("backfill_archive_search_index", stdout=out, batch_size=1)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), 2)
        first_a = ArchiveItemSearchIndex.objects.get(archive_item_id=a.pk)
        first_b = ArchiveItemSearchIndex.objects.get(archive_item_id=b.pk)

        call_command("backfill_archive_search_index", stdout=StringIO(), batch_size=10)
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), 2)
        second_a = ArchiveItemSearchIndex.objects.get(archive_item_id=a.pk)
        second_b = ArchiveItemSearchIndex.objects.get(archive_item_id=b.pk)
        self.assertEqual(second_a.pk, first_a.pk)
        self.assertEqual(second_b.pk, first_b.pk)
        self.assertEqual(second_a.body_text, first_a.body_text)
        self.assertEqual(second_b.body_text, first_b.body_text)

    def test_single_item_run(self):
        target = create_manual_text_archive_item(title="Target", body="Only me")
        create_manual_text_archive_item(title="Other", body="Skip")
        call_command(
            "backfill_archive_search_index",
            archive_item_id=target.pk,
            stdout=StringIO(),
        )
        self.assertEqual(ArchiveItemSearchIndex.objects.count(), 1)
        self.assertEqual(
            ArchiveItemSearchIndex.objects.get().archive_item_id,
            target.pk,
        )

    def test_partial_failure_is_recoverable(self):
        from django.db import DatabaseError

        ok = create_manual_text_archive_item(title="OK", body="ok-body")
        bad = create_manual_text_archive_item(title="Bad", body="bad-body")
        original = rebuild_archive_item_search_index

        def flaky(item: ArchiveItem):
            if item.pk == bad.pk:
                raise DatabaseError("simulated database failure")
            return original(item)

        err = StringIO()
        with patch(
            "documents.management.commands.backfill_archive_search_index."
            "rebuild_archive_item_search_index",
            side_effect=flaky,
        ):
            with self.assertRaises(CommandError):
                call_command(
                    "backfill_archive_search_index",
                    stdout=StringIO(),
                    stderr=err,
                    batch_size=10,
                )
        err_text = err.getvalue()
        self.assertIn(f"archive_item_id={bad.pk}", err_text)
        self.assertIn("DatabaseError", err_text)
        self.assertNotIn("bad-body", err_text)
        self.assertNotIn("simulated database failure", err_text)
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=ok.pk).exists()
        )
        self.assertFalse(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=bad.pk).exists()
        )

        call_command("backfill_archive_search_index", stdout=StringIO())
        self.assertTrue(
            ArchiveItemSearchIndex.objects.filter(archive_item_id=bad.pk).exists()
        )


class ArchiveItemSearchIndexPublicSearchUnchangedTests(TestCase):
    def test_filter_still_does_not_match_manual_or_ocr_body(self):
        manual = create_manual_text_archive_item(
            title="Visible title only",
            body="unique-manual-body-token-xyz",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        doc = create_viewable_ocr_document(
            title="OCR title only",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="unique-ocr-body-token-xyz",
            status=DocumentTextResult.Status.NEEDS_REVIEW,
        )
        rebuild_archive_item_search_index(_load_item(manual.pk))
        rebuild_archive_item_search_index(_load_item(doc.archive_item_id))

        qs = ArchiveItem.objects.all()
        self.assertFalse(
            filter_archive_items_by_search_query(
                qs, "unique-manual-body-token-xyz"
            ).exists()
        )
        self.assertFalse(
            filter_archive_items_by_search_query(
                qs, "unique-ocr-body-token-xyz"
            ).exists()
        )
        self.assertTrue(
            filter_archive_items_by_search_query(qs, "Visible title only")
            .filter(pk=manual.pk)
            .exists()
        )

    def test_archive_list_body_query_still_empty(self):
        create_manual_text_archive_item(
            title="List title",
            body="unique-list-body-token-abc",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        rebuild_archive_item_search_index(
            _load_item(ArchiveItem.objects.get(title="List title").pk)
        )
        resp = self.client.get(
            reverse("archive-list"),
            {"q": "unique-list-body-token-abc"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "List title")
        self.assertContains(resp, "לא נמצאו פריטים התואמים את החיפוש")
