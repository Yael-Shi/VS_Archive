"""PR3 public archive FTS cutover: matching, ranking, auth, and query-plan coverage."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
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
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
    normalize_archive_list_search_query,
    resolve_archive_list_search_terms,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
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
