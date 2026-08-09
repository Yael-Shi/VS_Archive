from django.test import SimpleTestCase, TestCase

from documents.models import Document, DocumentTextResult
from documents.services.archive_search_match_ranges import (
    _literal_case_insensitive_ranges,
    resolve_archive_search_text_matches,
)
from documents.test_archive_item import create_viewable_ocr_document


def _result(
    doc: Document,
    *,
    result_type: str,
    text: str,
    engine: str,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=result_type,
        engine=engine,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        text=text,
        source_revision=1,
    )


class LiteralCaseInsensitiveRangeTests(SimpleTestCase):
    def test_exact_offsets_preserve_original_text_coordinates(self):
        self.assertEqual(
            _literal_case_insensitive_ranges("  Alpha beta Alpha  ", "alpha"),
            ((2, 7), (13, 18)),
        )

    def test_non_overlapping_occurrences(self):
        self.assertEqual(
            _literal_case_insensitive_ranges("aaaa", "aa"),
            ((0, 2), (2, 4)),
        )

    def test_empty_values_fail_closed(self):
        self.assertEqual(_literal_case_insensitive_ranges("", "x"), ())
        self.assertEqual(_literal_case_insensitive_ranges("x", ""), ())

    def test_case_insensitive_literal_match_preserves_offsets(self):
        self.assertEqual(
            _literal_case_insensitive_ranges("Alpha beta", "ALPHA"),
            ((0, 5),),
        )

    def test_unrelated_unicode_does_not_hide_safe_literal_match(self):
        self.assertEqual(
            _literal_case_insensitive_ranges("Straße target", "target"),
            ((7, 13),),
        )


class ArchiveSearchTextMatchTests(TestCase):
    def test_hebrew_document_uses_displayed_hebrew_transcription(self):
        doc = create_viewable_ocr_document(
            title="Hebrew",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="source token",
            engine="source-engine",
        )
        hebrew = _result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="לפני מילה אחרי",
            engine="hebrew-engine",
        )

        matches = resolve_archive_search_text_matches(
            doc,
            search_query="מילה",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].text_result.pk, hebrew.pk)
        self.assertEqual(
            hebrew.text[matches[0].start : matches[0].end],
            "מילה",
        )

    def test_non_hebrew_searches_source_and_translation(self):
        doc = create_viewable_ocr_document(
            title="English",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before sourceword after",
            engine="source-engine",
        )
        translation = _result(
            doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            text="לפני תרגוםמילה אחרי",
            engine="translation-engine",
        )

        source_matches = resolve_archive_search_text_matches(
            doc,
            search_query="sourceword",
        )
        translation_matches = resolve_archive_search_text_matches(
            doc,
            search_query="תרגוםמילה",
        )

        self.assertEqual(len(source_matches), 1)
        self.assertEqual(source_matches[0].text_result.pk, source.pk)
        self.assertEqual(len(translation_matches), 1)
        self.assertEqual(translation_matches[0].text_result.pk, translation.pk)

    def test_offsets_are_against_unstripped_original_text(self):
        doc = create_viewable_ocr_document(
            title="Offsets",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="   target word   ",
            engine="source-engine",
        )

        matches = resolve_archive_search_text_matches(
            doc,
            search_query="target",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].start, 3)
        self.assertEqual(matches[0].end, 9)
        self.assertEqual(source.text[matches[0].start : matches[0].end], "target")

    def test_query_uses_public_search_punctuation_tokenization(self):
        doc = create_viewable_ocr_document(
            title="Terms",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="alpha something beta",
            engine="source-engine",
        )

        matches = resolve_archive_search_text_matches(
            doc,
            search_query="alpha---beta",
        )

        self.assertEqual([match.term for match in matches], ["alpha", "beta"])

    def test_punctuation_only_and_blank_return_no_matches(self):
        doc = create_viewable_ocr_document(
            title="No terms",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="anything",
            engine="source-engine",
        )

        self.assertEqual(
            resolve_archive_search_text_matches(doc, search_query="... !!!"),
            (),
        )
        self.assertEqual(
            resolve_archive_search_text_matches(doc, search_query="   "),
            (),
        )

    def test_literal_prefix_inside_longer_word_has_exact_range(self):
        doc = create_viewable_ocr_document(
            title="Literal prefix",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        source = _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="running quickly",
            engine="source-engine",
        )

        matches = resolve_archive_search_text_matches(
            doc,
            search_query="run",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].text_result.pk, source.pk)
        self.assertEqual((matches[0].start, matches[0].end), (0, 3))
        self.assertEqual(
            source.text[matches[0].start : matches[0].end],
            "run",
        )


class ArchiveSearchGeometryMatchTests(TestCase):
    def _trusted_transkribus_fixture(self):
        import hashlib

        from documents.models import (
            TranskribusSnapshotLine,
            TranskribusSnapshotPage,
            TranskribusTextResultBinding,
            TranskribusTranscriptSnapshot,
        )

        doc = create_viewable_ocr_document(
            title="Trusted geometry",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        text = "לפני מילה אחרי"
        result = DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-test",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text=text,
            source_revision=1,
        )
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()

        snapshot = TranskribusTranscriptSnapshot.objects.create(
            document=doc,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="g" * 64,
            raw_xml_fingerprint="h" * 64,
            canonical_text=text,
            canonical_text_sha256=sha,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            geometry_capability=(
                TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED
            ),
            hover_eligible=True,
        )
        TranskribusTextResultBinding.objects.create(
            text_result=result,
            snapshot=snapshot,
            binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
            bound_text_sha256=sha,
            bound_source_revision=1,
        )
        page = TranskribusSnapshotPage.objects.create(
            snapshot=snapshot,
            page_index=1,
            page_nr=1,
            transcript_ts_id="ts-geometry",
            page_geometry_capability=(
                TranskribusSnapshotPage.GeometryCapability.VERIFIED
            ),
        )
        TranskribusSnapshotLine.objects.create(
            page=page,
            order_index=0,
            provider_line_id="line-1",
            text=text,
            contributes_to_canonical=True,
            char_start=0,
            char_end=len(text),
            polygon_points=[
                [10, 20],
                [100, 20],
                [100, 40],
                [10, 40],
            ],
            bbox_min_x=10,
            bbox_min_y=20,
            bbox_max_x=100,
            bbox_max_y=40,
            coords_valid=True,
            has_meaningful_geometry=True,
        )
        return doc, result, snapshot

    def test_exact_search_match_resolves_to_trusted_geometry(self):
        from documents.services.archive_search_match_ranges import (
            resolve_archive_search_geometry_matches,
        )

        doc, result, _snapshot = self._trusted_transkribus_fixture()

        matches = resolve_archive_search_geometry_matches(
            doc,
            search_query="מילה",
        )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].text_result.pk, result.pk)
        self.assertEqual(
            result.text[matches[0].start : matches[0].end],
            "מילה",
        )
        self.assertEqual(len(matches[0].geometry), 1)
        self.assertEqual(matches[0].geometry[0].page_index, 1)
        self.assertEqual(matches[0].geometry[0].provider_line_id, "line-1")

    def test_literal_match_without_trusted_binding_is_omitted(self):
        from documents.services.archive_search_match_ranges import (
            resolve_archive_search_geometry_matches,
        )

        doc = create_viewable_ocr_document(
            title="No geometry",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _result(
            doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            text="before target after",
            engine="gemini-engine",
        )

        self.assertEqual(
            resolve_archive_search_geometry_matches(
                doc,
                search_query="target",
            ),
            (),
        )

    def test_stale_binding_geometry_match_is_omitted(self):
        from documents.services.archive_search_match_ranges import (
            resolve_archive_search_geometry_matches,
        )

        doc, result, _snapshot = self._trusted_transkribus_fixture()
        result.source_revision = 2
        result.save(update_fields=["source_revision"])

        self.assertEqual(
            resolve_archive_search_geometry_matches(
                doc,
                search_query="מילה",
            ),
            (),
        )

    def test_hover_ineligible_snapshot_geometry_match_is_omitted(self):
        from documents.services.archive_search_match_ranges import (
            resolve_archive_search_geometry_matches,
        )

        doc, _result_obj, snapshot = self._trusted_transkribus_fixture()
        snapshot.hover_eligible = False
        snapshot.save(update_fields=["hover_eligible"])

        self.assertEqual(
            resolve_archive_search_geometry_matches(
                doc,
                search_query="מילה",
            ),
            (),
        )
