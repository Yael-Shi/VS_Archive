"""PR3/PR4 public archive FTS: matching, ranking, snippets, auth, and query plans."""

from __future__ import annotations

from datetime import timedelta
from html import escape
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveCategory,
    ArchiveItem,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    PhotoContent,
    Tag,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_LIST_SEARCH_NO_MATCHES,
    ARCHIVE_LIST_SEARCH_NO_SEARCH,
    ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH,
    ARCHIVE_LIST_SEARCH_SEARCH,
    build_archive_browse_cards,
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
    normalize_archive_list_search_query,
    resolve_archive_list_search_terms,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    update_archive_item_discovery_metadata,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.services.archive_search_snippets import (
    MATCH_SOURCE_AUTHOR,
    MATCH_SOURCE_CATEGORIES,
    MATCH_SOURCE_ITEM_DETAILS,
    MATCH_SOURCE_MANUAL_BODY,
    MATCH_SOURCE_OCR_BODY,
    MATCH_SOURCE_PUBLIC_NOTE,
    apply_archive_search_match_presentation_to_cards,
    build_highlighted_snippet_segments,
    load_archive_search_indexes_for_item_ids,
    select_snippet_window,
)
from documents.test_archive_item import create_viewable_ocr_document


def _load_item(archive_item_id: int) -> ArchiveItem:
    return archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    return rebuild_archive_item_search_index(_load_item(archive_item_id))


def _create_text_result(
    doc: Document,
    *,
    text: str,
    status: str = DocumentTextResult.Status.NEEDS_REVIEW,
    verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
    result_type: str = DocumentTextResult.ResultType.SOURCE_TEXT,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=result_type,
        engine="engine-a",
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=status,
        verification_status=verification_status,
        text=text,
    )


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


class ArchiveListSearchQueryNormalizationTests(TestCase):
    def test_trim_collapse_punctuation_underscore_and_outcomes(self):
        self.assertEqual(normalize_archive_list_search_query("  a  b  "), "a  b")

        blank = resolve_archive_list_search_terms("   ")
        self.assertEqual(blank.outcome, ARCHIVE_LIST_SEARCH_NO_SEARCH)
        self.assertEqual(blank.terms, ())

        punctuation_only = resolve_archive_list_search_terms("... !!!")
        self.assertEqual(punctuation_only.outcome, ARCHIVE_LIST_SEARCH_NO_MATCHES)
        self.assertEqual(punctuation_only.terms, ())

        hebrew = resolve_archive_list_search_terms("  יוסף   מרזוק  ")
        self.assertEqual(hebrew.outcome, ARCHIVE_LIST_SEARCH_SEARCH)
        self.assertEqual(hebrew.terms, ("יוסף", "מרזוק"))

        punctuated = resolve_archive_list_search_terms("יוסף, מרזוק; test")
        self.assertEqual(punctuated.outcome, ARCHIVE_LIST_SEARCH_SEARCH)
        self.assertEqual(punctuated.terms, ("יוסף", "מרזוק", "test"))

        underscored = resolve_archive_list_search_terms("alpha_beta")
        self.assertEqual(underscored.outcome, ARCHIVE_LIST_SEARCH_SEARCH)
        self.assertEqual(underscored.terms, ("alpha", "beta"))

    def test_overlong_query_is_no_matches_outcome(self):
        overlong = "x" * (ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH + 1)
        resolved = resolve_archive_list_search_terms(overlong)
        self.assertEqual(resolved.outcome, ARCHIVE_LIST_SEARCH_NO_MATCHES)
        self.assertEqual(resolved.terms, ())
        self.assertEqual(
            len(normalize_archive_list_search_query(overlong)),
            ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH + 1,
        )

    def test_punctuation_only_query_returns_zero_results_not_full_browse(self):
        item = create_manual_text_archive_item(
            title="Punctuation only should not browse",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)
        blank_ids = _ids(
            filter_archive_items_by_search_query(ArchiveItem.objects.all(), "   ")
        )
        self.assertIn(item.pk, blank_ids)
        punct_q = "... !!!"
        punct_ids = _ids(
            filter_archive_items_by_search_query(ArchiveItem.objects.all(), punct_q)
        )
        self.assertEqual(punct_ids, [])

        resp = self.client.get(reverse("archive-list"), {"q": punct_q})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertNotContains(resp, item.title)
        self.assertEqual(resp.context["q"], punct_q)


class ArchiveFullTextSearchMatchTests(TestCase):
    def test_title_metadata_manual_body_and_ocr_body_match(self):
        title_item = create_manual_text_archive_item(
            title="uniqtitleftstoken",
            body="unrelated body alpha",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        meta_item = create_manual_text_archive_item(
            title="Metadata carrier",
            body="unrelated body beta",
            author_name="uniqauthorftstoken",
            source_title="uniqsourceftstoken",
            public_note="uniqpublicnotefstoken",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        category = ArchiveCategory.objects.create(
            name="uniqcategoryftstoken",
            slug="uniq-category-fts-token",
        )
        meta_item.categories.add(category)
        tag = Tag.objects.create(name="uniqtagftstoken")
        meta_item.tags.add(tag)
        manual = create_manual_text_archive_item(
            title="Manual carrier",
            body="uniqmanualbodyftstoken lives here",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        doc = create_viewable_ocr_document(
            title="OCR carrier",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(doc, text="uniqocrbodyftstoken in transcript")
        for item_id in (
            title_item.pk,
            meta_item.pk,
            manual.pk,
            doc.archive_item_id,
        ):
            _rebuild(item_id)

        qs = ArchiveItem.objects.all()
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "uniqtitleftstoken")),
            [title_item.pk],
        )
        self.assertIn(
            meta_item.pk,
            _ids(filter_archive_items_by_search_query(qs, "uniqauthorftstoken")),
        )
        self.assertIn(
            meta_item.pk,
            _ids(filter_archive_items_by_search_query(qs, "uniqsourceftstoken")),
        )
        self.assertIn(
            meta_item.pk,
            _ids(filter_archive_items_by_search_query(qs, "uniqpublicnotefstoken")),
        )
        self.assertIn(
            meta_item.pk,
            _ids(filter_archive_items_by_search_query(qs, "uniqcategoryftstoken")),
        )
        self.assertIn(
            meta_item.pk,
            _ids(filter_archive_items_by_search_query(qs, "uniqtagftstoken")),
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "uniqmanualbodyftstoken")),
            [manual.pk],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "uniqocrbodyftstoken")),
            [doc.archive_item_id],
        )

    def test_rejected_displayable_ocr_body_is_searchable(self):
        doc = create_viewable_ocr_document(
            title="Rejected OCR searchable",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            text="uniqrejectedocrbodytoken",
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        _rebuild(doc.archive_item_id)
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(),
                    "uniqrejectedocrbodytoken",
                )
            ),
            [doc.archive_item_id],
        )

    def test_multi_term_and_not_or(self):
        both = create_manual_text_archive_item(
            title="andbothalpha",
            body="andbothbeta content",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        only_alpha = create_manual_text_archive_item(
            title="andbothalpha only",
            body="other",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        only_beta = create_manual_text_archive_item(
            title="other title",
            body="andbothbeta only",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for item in (both, only_alpha, only_beta):
            _rebuild(item.pk)

        ids = _ids(
            filter_archive_items_by_search_query(
                ArchiveItem.objects.all(),
                "andbothalpha andbothbeta",
            )
        )
        self.assertEqual(ids, [both.pk])

    def test_cross_source_and_order_and_adjacency_do_not_matter(self):
        item = create_manual_text_archive_item(
            title="יוסף cross source",
            body="other words מרזוק later words",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        partial_title = create_manual_text_archive_item(
            title="יוסף only title",
            body="no second term here",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        partial_body = create_manual_text_archive_item(
            title="no first term",
            body="מרזוק only body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for row in (item, partial_title, partial_body):
            _rebuild(row.pk)

        qs = ArchiveItem.objects.all()
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "יוסף מרזוק")),
            [item.pk],
        )
        self.assertEqual(
            _ids(filter_archive_items_by_search_query(qs, "מרזוק יוסף")),
            [item.pk],
        )

    def test_short_field_substring_but_not_body_substring(self):
        title_item = create_manual_text_archive_item(
            title="prefixmarzuksuffix title",
            body="unrelated body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        body_item = create_manual_text_archive_item(
            title="Body substring carrier",
            body="prefixmarzuksuffix in a single body token",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(title_item.pk)
        _rebuild(body_item.pk)

        qs = ArchiveItem.objects.all()
        matched = _ids(filter_archive_items_by_search_query(qs, "marzuk"))
        self.assertEqual(matched, [title_item.pk])
        self.assertNotIn(body_item.pk, matched)

    def test_missing_index_does_not_match_or_crash(self):
        item = create_manual_text_archive_item(
            title="Missing index title token",
            body="Missing index body token",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ArchiveItemSearchIndex.objects.filter(archive_item_id=item.pk).delete()
        qs = filter_archive_items_by_search_query(
            ArchiveItem.objects.all(),
            "Missing index title token",
        )
        self.assertEqual(_ids(qs), [])

    def test_overlong_query_returns_empty_preserving_display_normalize(self):
        item = create_manual_text_archive_item(
            title="Overlong query title",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)
        overlong = "Overlong " + ("q" * ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH)
        trimmed = normalize_archive_list_search_query(overlong)
        self.assertEqual(trimmed, overlong.strip())
        self.assertEqual(
            resolve_archive_list_search_terms(overlong).outcome,
            ARCHIVE_LIST_SEARCH_NO_MATCHES,
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(),
                    overlong,
                )
            ),
            [],
        )

        resp = self.client.get(reverse("archive-list"), {"q": overlong})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertNotContains(resp, item.title)
        self.assertEqual(resp.context["q"], trimmed)


class ArchiveFullTextSearchRankingTests(TestCase):
    def test_title_outranks_metadata_outranks_body(self):
        shared = "rankweightsharedtoken"
        body_hit = create_manual_text_archive_item(
            title="Body rank carrier",
            body=f"intro {shared} outro",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        meta_hit = create_manual_text_archive_item(
            title="Meta rank carrier",
            body="no shared token here",
            author_name=shared,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        title_hit = create_manual_text_archive_item(
            title=shared,
            body="no shared token in body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for item in (body_hit, meta_hit, title_hit):
            _rebuild(item.pk)

        ids = _ids(
            filter_archive_items_by_search_query(ArchiveItem.objects.all(), shared)
        )
        self.assertEqual(ids, [title_hit.pk, meta_hit.pk, body_hit.pk])

    def test_deterministic_tie_break_created_at_then_pk(self):
        token = "ranktietoken"
        older = create_manual_text_archive_item(
            title=token,
            body="tie older",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        newer = create_manual_text_archive_item(
            title=token,
            body="tie newer",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        now = timezone.now()
        ArchiveItem.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(days=2)
        )
        ArchiveItem.objects.filter(pk=newer.pk).update(
            created_at=now - timedelta(days=1)
        )
        _rebuild(older.pk)
        _rebuild(newer.pk)

        ids = _ids(
            filter_archive_items_by_search_query(ArchiveItem.objects.all(), token)
        )
        self.assertEqual(ids, [newer.pk, older.pk])

        same_time = now - timedelta(hours=3)
        ArchiveItem.objects.filter(pk__in=[older.pk, newer.pk]).update(
            created_at=same_time
        )
        low_pk, high_pk = sorted([older.pk, newer.pk])
        ids_same = _ids(
            filter_archive_items_by_search_query(ArchiveItem.objects.all(), token)
        )
        self.assertEqual(ids_same, [low_pk, high_pk])


class ArchiveFullTextSearchNoQueryAndFiltersTests(TestCase):
    def test_empty_query_preserves_created_at_ordering(self):
        older = create_manual_text_archive_item(
            title="Noquery older",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        newer = create_manual_text_archive_item(
            title="Noquery newer",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        now = timezone.now()
        ArchiveItem.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(days=5)
        )
        ArchiveItem.objects.filter(pk=newer.pk).update(
            created_at=now - timedelta(days=1)
        )

        base = ArchiveItem.objects.all().order_by("-created_at")
        self.assertEqual(
            _ids(base), _ids(filter_archive_items_by_search_query(base, ""))
        )
        self.assertEqual(
            _ids(base), _ids(filter_archive_items_by_search_query(base, "   "))
        )

    def test_type_filter_and_one_result_per_item(self):
        manual = create_manual_text_archive_item(
            title="Typefilter sharedtoken",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        ocr = create_viewable_ocr_document(
            title="Typefilter sharedtoken ocr",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            visibility=Document.Visibility.PUBLIC,
        )
        photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Typefilter sharedtoken photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=photo,
            original_file_key="photos/typefilter/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        category_a = ArchiveCategory.objects.create(
            name="Typefilter cat alpha",
            slug="typefilter-cat-alpha",
        )
        category_b = ArchiveCategory.objects.create(
            name="Typefilter cat beta",
            slug="typefilter-cat-beta",
        )
        manual.categories.add(category_a, category_b)
        for item_id in (manual.pk, ocr.archive_item_id, photo.pk):
            _rebuild(item_id)

        qs = filter_archive_items_by_public_list_type(
            ArchiveItem.objects.all(),
            "documents_and_texts",
        )
        ids = _ids(filter_archive_items_by_search_query(qs, "Typefilter sharedtoken"))
        self.assertCountEqual(ids, [manual.pk, ocr.archive_item_id])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertNotIn(photo.pk, ids)

    def test_pagination_intact_on_archive_list(self):
        token = "pageftstoken"
        items = [
            create_manual_text_archive_item(
                title=f"{token} {index}",
                body="body",
                visibility=ArchiveItem.Visibility.PUBLIC,
            )
            for index in range(3)
        ]
        for item in items:
            _rebuild(item.pk)

        resp = self.client.get(
            reverse("archive-list"),
            {"q": token, "per_page": "24", "page": "1"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 3)
        self.assertEqual(resp.context["per_page"], 24)
        self.assertEqual(resp.context["page"], 1)
        self.assertEqual(resp.context["q"], token)


class ArchiveFullTextSearchAuthorizationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="fts_staff",
            password="test-pass",
            is_staff=True,
        )
        self.family = User.objects.create_user(
            username="fts_family",
            password="test-pass",
        )
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family.groups.add(family_group)

    def test_anonymous_family_staff_visibility(self):
        public = create_manual_text_archive_item(
            title="Auth public unique title",
            body="auth public unique body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private = create_manual_text_archive_item(
            title="Auth private unique title",
            body="auth private unique body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _rebuild(public.pk)
        _rebuild(private.pk)

        anon_qs = filter_archive_items_by_search_query(
            archive_browse_queryset_for_user(None),
            "Auth private unique title",
        )
        self.assertEqual(_ids(anon_qs), [])

        family_qs = filter_archive_items_by_search_query(
            archive_browse_queryset_for_user(self.family),
            "Auth private unique title",
        )
        self.assertEqual(_ids(family_qs), [private.pk])

        staff_qs = filter_archive_items_by_search_query(
            archive_browse_queryset_for_user(self.staff),
            "Auth private unique body",
        )
        self.assertEqual(_ids(staff_qs), [private.pk])

        resp = self.client.get(
            reverse("archive-list"),
            {"q": "Auth private unique title"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 0)
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertEqual(page_ids, [])
        self.assertNotIn(private.pk, page_ids)
        self.assertNotContains(
            resp,
            reverse("archive-detail", kwargs={"item_id": private.pk}),
        )

    def test_unauthorized_private_cannot_affect_count_rank_or_pagination(self):
        token = "leakranktoken"
        public_low = create_manual_text_archive_item(
            title="Public low",
            body=token,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private_high = create_manual_text_archive_item(
            title=token,
            body="private title weight should not leak",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        public_mid = create_manual_text_archive_item(
            title="Public mid",
            body=f"also {token}",
            author_name=token,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        now = timezone.now()
        ArchiveItem.objects.filter(pk=public_low.pk).update(
            created_at=now - timedelta(days=3)
        )
        ArchiveItem.objects.filter(pk=private_high.pk).update(
            created_at=now - timedelta(days=2)
        )
        ArchiveItem.objects.filter(pk=public_mid.pk).update(
            created_at=now - timedelta(days=1)
        )
        for item_id in (public_low.pk, private_high.pk, public_mid.pk):
            _rebuild(item_id)

        anon_ids = _ids(
            filter_archive_items_by_search_query(
                archive_browse_queryset_for_user(None),
                token,
            )
        )
        self.assertEqual(anon_ids, [public_mid.pk, public_low.pk])
        self.assertNotIn(private_high.pk, anon_ids)

        resp = self.client.get(
            reverse("archive-list"),
            {"q": token, "per_page": "24"},
        )
        self.assertEqual(resp.context["total_count"], 2)
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertEqual(page_ids, [public_mid.pk, public_low.pk])
        self.assertNotIn(private_high.pk, page_ids)
        self.assertNotContains(
            resp,
            reverse("archive-detail", kwargs={"item_id": private_high.pk}),
        )


class ArchiveFullTextSearchQueryPlanTests(TestCase):
    def test_public_search_fts_branch_can_use_gin_via_union(self):
        """
        Combined ``@@ OR ILIKE OR ILIKE`` can hide GIN. The public filter must
        decompose per-term candidates with UNION so the FTS SELECT is independently
        indexable; EXPLAIN the real ranked public-search queryset.
        """
        item = create_manual_text_archive_item(
            title="Gin plan title",
            body="uniqginplanbodytoken and more words for the vector",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        # Second item ensures substring arms exist in the plan without matching FTS.
        create_manual_text_archive_item(
            title="unrelated substring carrier",
            body="no gin token here",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        qs = filter_archive_items_by_search_query(
            archive_browse_queryset_for_user(None).order_by("-created_at"),
            "uniqginplanbodytoken",
        )
        sql, params = qs.query.sql_with_params()
        self.assertIn("UNION", sql.upper())

        with connection.cursor() as cursor:
            # Tiny fixtures often prefer seq scans; force index consideration.
            cursor.execute("SET LOCAL enable_seqscan = off")
            cursor.execute(f"EXPLAIN {sql}", params)
            plan = "\n".join(row[0] for row in cursor.fetchall())

        self.assertIn("archive_item_search_vector_gin", plan)
        self.assertRegex(plan, r"(Index Scan|Bitmap Index Scan|Bitmap Heap Scan)")


def _card_by_item_id(cards, item_id: int):
    for card in cards:
        if card.item.pk == item_id:
            return card
    raise AssertionError(f"card for item {item_id} not found")


def _snippet_plain_text(card) -> str:
    return "".join(segment.text for segment in card.search_snippet_segments)


class ArchiveSearchSnippetPresentationTests(TestCase):
    def test_ocr_body_match_shows_transcription_label_and_contextual_snippet(self):
        prefix = "מילה " * 40
        doc = create_viewable_ocr_document(
            title="OCR snippet title",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(
            doc,
            text=f"{prefix}יוסף מרזוק המשך הטקסט אחרי ההתאמה",
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        _rebuild(doc.archive_item_id)

        resp = self.client.get(reverse("archive-list"), {"q": "יוסף מרזוק"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, MATCH_SOURCE_OCR_BODY)
        self.assertContains(resp, "<mark")
        self.assertContains(resp, "יוסף")
        self.assertContains(resp, "מרזוק")
        card = _card_by_item_id(resp.context["browse_cards"], doc.archive_item_id)
        self.assertTrue(card.show_search_snippet)
        self.assertEqual(card.search_match_source_label, MATCH_SOURCE_OCR_BODY)
        plain = _snippet_plain_text(card)
        self.assertIn("יוסף", plain)
        self.assertIn("מרזוק", plain)
        self.assertTrue(plain.startswith("…"))
        self.assertNotEqual(card.preview_text, plain)

    def test_manual_body_match_shows_text_label(self):
        item = create_manual_text_archive_item(
            title="Manual snippet title",
            body=("רקע " * 30) + "מילתחיפוש ייחודית להמחשה בהמשך",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"), {"q": "מילתחיפוש"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, MATCH_SOURCE_MANUAL_BODY)
        card = _card_by_item_id(resp.context["browse_cards"], item.pk)
        self.assertTrue(card.show_search_snippet)
        self.assertEqual(card.search_match_source_label, MATCH_SOURCE_MANUAL_BODY)
        self.assertIn("מילתחיפוש", _snippet_plain_text(card))

    def test_no_query_keeps_ordinary_beginning_preview(self):
        body = "Opening preview sentence for browse without search."
        item = create_manual_text_archive_item(
            title="No query preview",
            body=body,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"))
        card = _card_by_item_id(resp.context["browse_cards"], item.pk)
        self.assertFalse(card.show_search_snippet)
        self.assertEqual(card.search_match_source_label, "")
        self.assertEqual(card.search_snippet_segments, ())
        self.assertIn("Opening preview sentence", card.preview_text)
        self.assertNotContains(resp, MATCH_SOURCE_MANUAL_BODY)
        self.assertNotContains(resp, 'class="archive-browse-card__mark"')

    def test_title_only_match_does_not_show_unrelated_body_excerpt(self):
        item = create_manual_text_archive_item(
            title="uniqtitleonlytoken",
            body=(
                "This body never contains the title token. "
                "It starts with ordinary preview words for the card."
            ),
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"), {"q": "uniqtitleonlytoken"})
        card = _card_by_item_id(resp.context["browse_cards"], item.pk)
        self.assertFalse(card.show_search_snippet)
        self.assertEqual(card.search_snippet_segments, ())
        self.assertEqual(card.search_match_source_label, "")
        self.assertIn("This body never contains", card.preview_text)
        self.assertNotContains(resp, MATCH_SOURCE_MANUAL_BODY)
        self.assertNotContains(resp, MATCH_SOURCE_OCR_BODY)

    def test_metadata_and_discovery_match_source_labels(self):
        author_item = create_manual_text_archive_item(
            title="Author carrier",
            body="unrelated body for author match",
            author_name="uniqauthorsnippettoken",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        note_item = create_manual_text_archive_item(
            title="Note carrier",
            body="unrelated body for note match",
            public_note="uniqpublicnotesnippettoken",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        discovery_item = create_manual_text_archive_item(
            title="Discovery carrier",
            body="unrelated body for discovery match",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        update_archive_item_discovery_metadata(
            discovery_item,
            category_names=["uniqcategorysnippettoken"],
            event_names=[],
            tag_names=[],
        )
        multi_item = create_manual_text_archive_item(
            title="Multi meta carrier",
            body="unrelated body for multi metadata",
            author_name="uniqmultiauthortoken",
            public_note="uniqmultinotetoken",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        for item_id in (
            author_item.pk,
            note_item.pk,
            discovery_item.pk,
            multi_item.pk,
        ):
            _rebuild(item_id)

        author_resp = self.client.get(
            reverse("archive-list"), {"q": "uniqauthorsnippettoken"}
        )
        author_card = _card_by_item_id(
            author_resp.context["browse_cards"], author_item.pk
        )
        self.assertEqual(author_card.search_match_source_label, MATCH_SOURCE_AUTHOR)
        self.assertFalse(author_card.show_search_snippet)

        note_resp = self.client.get(
            reverse("archive-list"), {"q": "uniqpublicnotesnippettoken"}
        )
        note_card = _card_by_item_id(note_resp.context["browse_cards"], note_item.pk)
        self.assertEqual(note_card.search_match_source_label, MATCH_SOURCE_PUBLIC_NOTE)

        discovery_resp = self.client.get(
            reverse("archive-list"), {"q": "uniqcategorysnippettoken"}
        )
        discovery_card = _card_by_item_id(
            discovery_resp.context["browse_cards"], discovery_item.pk
        )
        self.assertEqual(
            discovery_card.search_match_source_label, MATCH_SOURCE_CATEGORIES
        )

        multi_resp = self.client.get(
            reverse("archive-list"),
            {"q": "uniqmultiauthortoken uniqmultinotetoken"},
        )
        multi_card = _card_by_item_id(multi_resp.context["browse_cards"], multi_item.pk)
        self.assertEqual(
            multi_card.search_match_source_label, MATCH_SOURCE_ITEM_DETAILS
        )

    def test_multi_term_nearby_and_far_apart_single_snippet(self):
        nearby = create_manual_text_archive_item(
            title="Nearby terms",
            body="הקדמה קצרה יוסף מרזוק סיום קצר",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        far = create_manual_text_archive_item(
            title="Far terms",
            body=("alpha " + ("padword " * 80) + "beta " + ("tailword " * 20)),
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(nearby.pk)
        _rebuild(far.pk)

        nearby_resp = self.client.get(reverse("archive-list"), {"q": "יוסף מרזוק"})
        nearby_card = _card_by_item_id(nearby_resp.context["browse_cards"], nearby.pk)
        nearby_plain = _snippet_plain_text(nearby_card)
        self.assertIn("יוסף", nearby_plain)
        self.assertIn("מרזוק", nearby_plain)
        matched = [
            seg.text for seg in nearby_card.search_snippet_segments if seg.is_match
        ]
        self.assertEqual(matched, ["יוסף", "מרזוק"])

        far_resp = self.client.get(reverse("archive-list"), {"q": "alpha beta"})
        far_card = _card_by_item_id(far_resp.context["browse_cards"], far.pk)
        far_plain = _snippet_plain_text(far_card)
        # One deterministic excerpt: earliest suitable region (alpha), not two snippets.
        self.assertIn("alpha", far_plain)
        self.assertNotIn("beta", far_plain)
        self.assertEqual(far_plain.count("…"), 1)
        self.assertTrue(far_card.show_search_snippet)

    def test_punctuation_underscore_terms_align_with_pr3_and_highlight(self):
        item = create_manual_text_archive_item(
            title="Punctuation snippet",
            body="hello alpha beta world",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"), {"q": "alpha_beta"})
        self.assertEqual(resp.context["total_count"], 1)
        card = _card_by_item_id(resp.context["browse_cards"], item.pk)
        matched = [seg.text for seg in card.search_snippet_segments if seg.is_match]
        self.assertEqual(matched, ["alpha", "beta"])

    def test_snippet_ellipses_and_deterministic_window_selection(self):
        filler = "מילה "
        body = f"{filler * 50}מוקדם {filler * 50}מאוחר"
        item = create_manual_text_archive_item(
            title="Window selection",
            body=body,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        index = _rebuild(item.pk)
        terms = ("מוקדם", "מאוחר")
        window = select_snippet_window(index.body_text, terms)
        self.assertIsNotNone(window)
        assert window is not None
        start, end = window
        excerpt = " ".join(index.body_text.split())[start:end]
        self.assertIn("מוקדם", excerpt)
        self.assertNotIn("מאוחר", excerpt)

        segments = build_highlighted_snippet_segments(index.body_text, ("מוקדם",))
        self.assertEqual(segments[0].text, "…")
        self.assertEqual(segments[-1].text, "…")
        self.assertTrue(any(seg.is_match and seg.text == "מוקדם" for seg in segments))

    def test_malicious_html_is_escaped_only_mark_is_markup(self):
        malicious = (
            'prefix <script>alert("xss")</script> יוסף '
            "<img src=x onerror=alert(1)> & more"
        )
        item = create_manual_text_archive_item(
            title="XSS snippet item",
            body=malicious,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"), {"q": "יוסף"})
        content = resp.content.decode("utf-8")
        self.assertIn("<mark", content)
        self.assertIn("יוסף", content)
        self.assertIn(escape("<script>"), content)
        self.assertNotIn("<script>alert", content)
        self.assertNotIn("<img src=x", content)
        self.assertIn(escape("<img src=x onerror=alert(1)>"), content)

        card = _card_by_item_id(resp.context["browse_cards"], item.pk)
        plain = _snippet_plain_text(card)
        self.assertIn("<script>", plain)
        self.assertIn("<img", plain)

    def test_snippet_mark_keeps_adjacent_punctuation_without_template_spaces(self):
        item = create_manual_text_archive_item(
            title="Punctuation adjacency snippet",
            body="prefix(term),suffix",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        _rebuild(item.pk)

        resp = self.client.get(reverse("archive-list"), {"q": "term"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            'prefix(<mark class="archive-browse-card__mark">term</mark>),suffix',
            html=False,
        )

    def test_unauthorized_private_never_appears_in_snippet_or_source(self):
        private_token = "privateuniquesnippetleakoken"
        private_sentinel = "privateonlysnippetbodyuniquezzz"
        public = create_manual_text_archive_item(
            title="Public sibling",
            body="public body without private token",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        private = create_manual_text_archive_item(
            title="Private snippet title",
            body=f"secret {private_token} {private_sentinel} content",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _rebuild(public.pk)
        _rebuild(private.pk)

        resp = self.client.get(reverse("archive-list"), {"q": private_token})
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])
        self.assertEqual(list(resp.context["browse_cards"]), [])
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertNotIn(private.pk, page_ids)
        self.assertNotContains(
            resp,
            reverse("archive-detail", kwargs={"item_id": private.pk}),
        )
        self.assertNotContains(resp, MATCH_SOURCE_MANUAL_BODY)
        # Private-only body text (not the submitted q) must never render.
        self.assertNotContains(resp, private_sentinel)

    def test_snippets_only_for_current_page_slice(self):
        token = "pageonlysnippettoken"
        per_page = 24
        # Enough matches for two pages at the supported minimum page size.
        item_count = per_page + 1
        shared_body = f"{token} contextual body " + ("pad " * 20)
        items = [
            create_manual_text_archive_item(
                title=f"Page slice {index}",
                body=shared_body,
                visibility=ArchiveItem.Visibility.PUBLIC,
            )
            for index in range(item_count)
        ]
        now = timezone.now()
        for offset, item in enumerate(items):
            ArchiveItem.objects.filter(pk=item.pk).update(
                created_at=now - timedelta(days=offset)
            )
            _rebuild(item.pk)

        # Identical body rank → newest first: items[0]..items[23] on page 1;
        # items[24] is a known page-2 id that must not be loaded for snippets.
        expected_page1_ids = [item.pk for item in items[:per_page]]
        known_page2_id = items[per_page].pk

        with patch(
            "documents.services.archive_search_snippets."
            "load_archive_search_indexes_for_item_ids",
            wraps=load_archive_search_indexes_for_item_ids,
        ) as mock_load:
            resp = self.client.get(
                reverse("archive-list"),
                {"q": token, "per_page": str(per_page), "page": "1"},
            )

        self.assertEqual(resp.status_code, 200)
        self.assertGreater(resp.context["total_count"], per_page)
        self.assertEqual(resp.context["total_count"], item_count)
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertEqual(page_ids, expected_page1_ids)
        self.assertNotIn(known_page2_id, page_ids)

        mock_load.assert_called_once()
        loaded_ids = list(mock_load.call_args.args[0])
        self.assertEqual(loaded_ids, expected_page1_ids)
        self.assertNotIn(known_page2_id, loaded_ids)

        for card in resp.context["browse_cards"]:
            self.assertIn(card.item.pk, set(expected_page1_ids))
            self.assertTrue(card.show_search_snippet)

    def test_snippet_generation_does_not_create_n_plus_one_queries(self):
        token = "nplusonesnippettoken"

        def build_cards(count: int):
            ArchiveItem.objects.all().delete()
            created = [
                create_manual_text_archive_item(
                    title=f"N+1 item {index}",
                    body=f"{token} contextual body {index} " + ("word " * 25),
                    visibility=ArchiveItem.Visibility.PUBLIC,
                )
                for index in range(count)
            ]
            for item in created:
                _rebuild(item.pk)
            cards = build_archive_browse_cards(
                [_load_item(item.pk) for item in created]
            )
            with CaptureQueriesContext(connection) as context:
                apply_archive_search_match_presentation_to_cards(
                    cards,
                    search_query=token,
                )
            return len(context)

        self.assertEqual(build_cards(2), build_cards(5))
        self.assertEqual(build_cards(2), 1)

    def test_type_filter_and_pagination_intact_with_snippets(self):
        token = "filterpagesnippettoken"
        manual = create_manual_text_archive_item(
            title="Filter manual",
            body=f"{token} manual body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        doc = create_viewable_ocr_document(
            title="Filter OCR",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        _create_text_result(doc, text=f"{token} ocr body")
        photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title=f"{token} photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        PhotoContent.objects.create(
            archive_item=photo,
            original_file_key="photos/snippet/original.jpg",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=100,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )
        for item_id in (manual.pk, doc.archive_item_id, photo.pk):
            _rebuild(item_id)

        resp = self.client.get(
            reverse("archive-list"),
            {
                "q": token,
                "item_type": "documents_and_texts",
                "per_page": "24",
                "page": "1",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 2)
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertCountEqual(page_ids, [manual.pk, doc.archive_item_id])
        self.assertNotIn(photo.pk, page_ids)
        labels = {
            card.item.pk: card.search_match_source_label
            for card in resp.context["browse_cards"]
        }
        self.assertEqual(labels[manual.pk], MATCH_SOURCE_MANUAL_BODY)
        self.assertEqual(labels[doc.archive_item_id], MATCH_SOURCE_OCR_BODY)

    def test_pr3_ranking_order_unchanged_when_snippets_attached(self):
        token = "rankunchangedtoken"
        body_hit = create_manual_text_archive_item(
            title="Body rank",
            body=token,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        meta_hit = create_manual_text_archive_item(
            title="Meta rank",
            body="no token in body",
            author_name=token,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        title_hit = create_manual_text_archive_item(
            title=token,
            body="no token in body either",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        now = timezone.now()
        ArchiveItem.objects.filter(pk=body_hit.pk).update(
            created_at=now - timedelta(days=3)
        )
        ArchiveItem.objects.filter(pk=meta_hit.pk).update(
            created_at=now - timedelta(days=2)
        )
        ArchiveItem.objects.filter(pk=title_hit.pk).update(
            created_at=now - timedelta(days=1)
        )
        for item_id in (body_hit.pk, meta_hit.pk, title_hit.pk):
            _rebuild(item_id)

        expected = _ids(
            filter_archive_items_by_search_query(
                archive_browse_queryset_for_user(None),
                token,
            )
        )
        self.assertEqual(expected, [title_hit.pk, meta_hit.pk, body_hit.pk])

        resp = self.client.get(reverse("archive-list"), {"q": token})
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertEqual(page_ids, expected)

    def test_help_text_and_placeholder_no_longer_claim_date_or_place(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(
            resp,
            "אפשר לחפש לפי כותרת, מחבר/ת, קטגוריות, אירועים, תגיות או מילים מתוך הטקסט.",
        )
        self.assertContains(
            resp,
            'placeholder="כותרת, מחבר/ת, קטגוריות, אירועים, תגיות או מילים מהטקסט..."',
        )
        self.assertNotContains(resp, "שם, מקום, נושא, תאריך")
        self.assertNotContains(resp, "תאריך או מילת מפתח")
