"""Public /archive/ per-type tab counts (pre-item_type universe, one aggregate)."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveCategory,
    ArchiveItem,
    Document,
    PhotoContent,
    Tag,
)
from documents.services.archive_advanced_search import (
    EMPTY_ARCHIVE_ADVANCED_FILTERS,
    ArchiveAdvancedFilters,
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL,
    ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
    ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO,
    ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO,
    aggregate_archive_public_list_type_counts,
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
    update_archive_item_discovery_metadata,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)

User = get_user_model()

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _rebuild(archive_item_id: int) -> None:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    rebuild_archive_item_search_index(item)


def _public_manual(*, title: str, author_name: str = "", **kwargs) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=kwargs.pop("body", "body"),
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
        **kwargs,
    )


def _public_photo(*, title: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="original.jpg",
        original_mime_type="image/jpeg",
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item


def _public_video(*, title: str) -> ArchiveItem:
    return create_video_archive_item(
        title=title,
        source_url=YOUTUBE_URL,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _public_ocr(*, title: str) -> ArchiveItem:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.PRINTED,
        visibility=Document.Visibility.PUBLIC,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key=f"documents/{title}/source.jpg",
    )
    return doc.archive_item


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _counts_by_slug(links) -> dict[str, int]:
    return {str(link["slug"]): int(link["count"]) for link in links}


def _legacy_typed_search_pipeline(
    authorized,
    *,
    q: str,
    item_type: str,
    advanced_filters: ArchiveAdvancedFilters | None = None,
):
    """Pre-type-count order: item_type → advanced → q."""
    filters = advanced_filters or EMPTY_ARCHIVE_ADVANCED_FILTERS
    qs = filter_archive_items_by_public_list_type(authorized, item_type)
    qs = filter_archive_items_by_advanced_filters(qs, filters)
    return filter_archive_items_by_search_query(qs, q)


def _current_typed_search_pipeline(
    authorized,
    *,
    q: str,
    item_type: str,
    advanced_filters: ArchiveAdvancedFilters | None = None,
):
    """Current type-count order: advanced → q → (counts) → item_type."""
    filters = advanced_filters or EMPTY_ARCHIVE_ADVANCED_FILTERS
    qs = filter_archive_items_by_advanced_filters(authorized, filters)
    qs = filter_archive_items_by_search_query(qs, q)
    return filter_archive_items_by_public_list_type(qs, item_type)


def _public_photo_with_author(*, title: str, author_name: str = "") -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=f"photos/{item.pk}/original.jpg",
        original_filename="original.jpg",
        original_mime_type="image/jpeg",
        upload_status=PhotoContent.UploadStatus.UPLOADED,
    )
    return item


class ArchiveListPipelineReorderEquivalenceTests(TestCase):
    """
    Type-count pipeline moves item_type after FTS. Page results for a selected
    tab must stay membership- and order-equivalent to the legacy order.
    """

    def setUp(self):
        self.url = reverse("archive-list")

    def test_q_documents_and_texts_preserves_fts_membership_and_rank_order(self):
        shared = "pipereordertokendocs"
        body_hit = _public_manual(
            title="Pipe reorder body",
            body=f"intro {shared} outro",
        )
        meta_hit = _public_manual(
            title="Pipe reorder meta",
            body="no shared token",
            author_name=shared,
        )
        title_hit = _public_manual(
            title=shared,
            body="no shared token in body",
        )
        # Same q hits this photo; selected tab must exclude it without reordering docs.
        photo_hit = _public_photo(title=f"{shared} photo distractor")
        for item in (body_hit, meta_hit, title_hit, photo_hit):
            _rebuild(item.pk)

        authorized = archive_browse_queryset_for_user(None).order_by("-created_at")
        legacy = _legacy_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
        )
        current = _current_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
        )
        expected = [title_hit.pk, meta_hit.pk, body_hit.pk]
        self.assertEqual(_ids(legacy), expected)
        self.assertEqual(_ids(current), expected)
        self.assertNotIn(photo_hit.pk, _ids(current))

        # Late item_type filter must keep FTS order_by (SQL may use SELECT ordinals).
        self.assertEqual(
            current.query.order_by,
            ("-archive_search_relevance", "-created_at", "pk"),
        )
        sql = str(current.query).lower()
        self.assertIn("order by", sql)
        order_clause = sql[sql.rfind("order by") :]
        self.assertIn("created_at", order_clause)
        self.assertIn("desc", order_clause)

        universe = filter_archive_items_by_search_query(authorized, shared)
        counts = aggregate_archive_public_list_type_counts(universe)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL], 4)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS], 3)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO], 1)

        resp = self.client.get(
            self.url,
            {
                "q": shared,
                "item_type": ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item.pk for item in resp.context["items"]], expected)
        self.assertEqual(resp.context["total_count"], 3)
        self.assertEqual(
            _counts_by_slug(resp.context["item_type_filter_links"]),
            {
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 4,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 3,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 1,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 0,
            },
        )

    def test_q_photo_preserves_fts_membership_and_rank_order(self):
        shared = "pipereordertokenphoto"
        meta_hit = _public_photo_with_author(
            title="Pipe photo meta carrier",
            author_name=shared,
        )
        title_hit = _public_photo_with_author(
            title=shared,
            author_name="",
        )
        # Older metadata-only hit: same boost as meta_hit, loses on -created_at.
        older_meta = _public_photo_with_author(
            title="Pipe photo older meta",
            author_name=shared,
        )
        now = timezone.now()
        ArchiveItem.objects.filter(pk=older_meta.pk).update(
            created_at=now - timedelta(days=3)
        )
        ArchiveItem.objects.filter(pk=meta_hit.pk).update(
            created_at=now - timedelta(days=1)
        )
        ArchiveItem.objects.filter(pk=title_hit.pk).update(
            created_at=now - timedelta(days=2)
        )
        doc_distractor = _public_manual(
            title=shared,
            body="doc distractor for photo tab",
        )
        for item in (meta_hit, title_hit, older_meta, doc_distractor):
            _rebuild(item.pk)

        authorized = archive_browse_queryset_for_user(None).order_by("-created_at")
        legacy = _legacy_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO,
        )
        current = _current_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO,
        )
        expected = [title_hit.pk, meta_hit.pk, older_meta.pk]
        self.assertEqual(_ids(legacy), expected)
        self.assertEqual(_ids(current), expected)
        self.assertNotIn(doc_distractor.pk, expected)

        self.assertEqual(
            current.query.order_by,
            ("-archive_search_relevance", "-created_at", "pk"),
        )
        sql = str(current.query).lower()
        self.assertIn("created_at", sql[sql.rfind("order by") :])

        resp = self.client.get(
            self.url,
            {"q": shared, "item_type": ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item.pk for item in resp.context["items"]], expected)
        self.assertEqual(
            _counts_by_slug(resp.context["item_type_filter_links"])[
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL
            ],
            4,
        )
        self.assertEqual(
            _counts_by_slug(resp.context["item_type_filter_links"])[
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO
            ],
            3,
        )
        self.assertEqual(resp.context["total_count"], 3)

    def test_q_item_type_and_advanced_filter_order_equivalent(self):
        shared = "pipereorderadvtoken"
        cat = ArchiveCategory.objects.create(
            name="Pipe Reorder Cat",
            slug="pipe-reorder-cat",
        )
        body_hit = _public_manual(
            title="Pipe adv body",
            body=f"intro {shared} outro",
        )
        meta_hit = _public_manual(
            title="Pipe adv meta",
            body="no shared",
            author_name=shared,
        )
        title_hit = _public_manual(
            title=shared,
            body="no shared body",
        )
        excluded_other_cat = _public_manual(
            title=shared,
            body="other category",
        )
        photo_same_cat = _public_photo(title=shared)
        update_archive_item_discovery_metadata(
            body_hit,
            category_names=["Pipe Reorder Cat"],
            event_names=[],
            tag_names=[],
        )
        update_archive_item_discovery_metadata(
            meta_hit,
            category_names=["Pipe Reorder Cat"],
            event_names=[],
            tag_names=[],
        )
        update_archive_item_discovery_metadata(
            title_hit,
            category_names=["Pipe Reorder Cat"],
            event_names=[],
            tag_names=[],
        )
        update_archive_item_discovery_metadata(
            excluded_other_cat,
            category_names=["Other Pipe Cat"],
            event_names=[],
            tag_names=[],
        )
        update_archive_item_discovery_metadata(
            photo_same_cat,
            category_names=["Pipe Reorder Cat"],
            event_names=[],
            tag_names=[],
        )
        for item in (
            body_hit,
            meta_hit,
            title_hit,
            excluded_other_cat,
            photo_same_cat,
        ):
            _rebuild(item.pk)

        filters = normalize_archive_advanced_filters({"category": str(cat.id)})
        authorized = archive_browse_queryset_for_user(None).order_by("-created_at")
        legacy = _legacy_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
            advanced_filters=filters,
        )
        current = _current_typed_search_pipeline(
            authorized,
            q=shared,
            item_type=ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
            advanced_filters=filters,
        )
        expected = [title_hit.pk, meta_hit.pk, body_hit.pk]
        self.assertEqual(_ids(legacy), expected)
        self.assertEqual(_ids(current), expected)
        self.assertNotIn(excluded_other_cat.pk, _ids(current))
        self.assertNotIn(photo_same_cat.pk, _ids(current))

        universe = filter_archive_items_by_search_query(
            filter_archive_items_by_advanced_filters(authorized, filters),
            shared,
        )
        counts = aggregate_archive_public_list_type_counts(universe)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL], 4)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS], 3)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO], 1)

        resp = self.client.get(
            self.url,
            {
                "q": shared,
                "item_type": ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS,
                "category": str(cat.id),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([item.pk for item in resp.context["items"]], expected)
        self.assertEqual(
            _counts_by_slug(resp.context["item_type_filter_links"]),
            {
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 4,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 3,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 1,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 0,
            },
        )


def _type_aggregate_query_count(captured_queries) -> int:
    """Count SELECT aggregates that bucket archive item types in one pass."""
    count = 0
    for query in captured_queries:
        sql = query["sql"].lower().replace('"', "")
        if "documents_archiveitem" not in sql:
            continue
        if "count(" not in sql:
            continue
        # Conditional / filtered aggregates used by the type-tab helper.
        if "filter" in sql or "case when" in sql:
            count += 1
    return count


class ArchiveListTypeCountTests(TestCase):
    def setUp(self):
        self.url = reverse("archive-list")
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )

    def test_aggregate_helper_counts_authorized_universe_by_public_tabs(self):
        manuals = [_public_manual(title=f"Count manual {i}") for i in range(2)]
        _public_ocr(title="Count OCR")
        photos = [_public_photo(title=f"Count photo {i}") for i in range(3)]
        videos = [_public_video(title=f"Count video {i}") for i in range(1)]
        private = create_manual_text_archive_item(
            title="Count private",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )

        authorized = archive_browse_queryset_for_user(None)
        with CaptureQueriesContext(connection) as ctx:
            counts = aggregate_archive_public_list_type_counts(authorized)

        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL], 7)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS], 3)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO], 3)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO], 1)
        self.assertEqual(_type_aggregate_query_count(ctx), 1)
        self.assertNotIn(private.pk, authorized.values_list("pk", flat=True))
        # Sanity: fixtures created as expected.
        self.assertEqual(len(manuals) + 1, 3)  # manuals + ocr
        self.assertEqual(len(photos), 3)
        self.assertEqual(len(videos), 1)

    def test_tab_counts_stable_when_switching_item_type(self):
        for i in range(4):
            _public_manual(title=f"Stable doc {i}")
        for i in range(2):
            _public_photo(title=f"Stable photo {i}")
        _public_video(title="Stable video")

        expected = {
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 7,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 4,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 2,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 1,
        }
        for params in (
            {},
            {"item_type": "documents_and_texts"},
            {"item_type": "photo"},
            {"item_type": "video"},
        ):
            with self.subTest(params=params):
                resp = self.client.get(self.url, params)
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(
                    _counts_by_slug(resp.context["item_type_filter_links"]),
                    expected,
                )
                html = resp.content.decode("utf-8")
                self.assertIn(
                    'הכל</span> <span class="archive-type-filter__count">(7)</span>',
                    html,
                )
                self.assertIn("(4)", html)
                self.assertIn("(2)", html)
                self.assertIn("(1)", html)

    def test_q_only_counts_use_search_universe_before_item_type(self):
        hit_manual = _public_manual(title="UniqueQTerm manual")
        hit_photo = _public_photo(title="UniqueQTerm photo")
        hit_video = _public_video(title="UniqueQTerm video")
        _public_manual(title="Other manual")
        for item in (hit_manual, hit_photo, hit_video):
            _rebuild(item.pk)

        resp_all = self.client.get(self.url, {"q": "UniqueQTerm"})
        resp_photo = self.client.get(
            self.url, {"q": "UniqueQTerm", "item_type": "photo"}
        )
        expected = {
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 3,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 1,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 1,
            ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 1,
        }
        self.assertEqual(
            _counts_by_slug(resp_all.context["item_type_filter_links"]), expected
        )
        self.assertEqual(
            _counts_by_slug(resp_photo.context["item_type_filter_links"]), expected
        )
        self.assertEqual(resp_photo.context["total_count"], 1)

    def test_advanced_filter_counts_and_no_m2m_inflation(self):
        cat = ArchiveCategory.objects.create(name="Count Cat", slug="count-cat")
        tag_a = Tag.objects.create(name="Count Tag A")
        tag_b = Tag.objects.create(name="Count Tag B")
        manual = _public_manual(title="Adv count manual")
        photo = _public_photo(title="Adv count photo")
        video = _public_video(title="Adv count video")
        other = _public_manual(title="Adv count other")
        for item in (manual, photo, video):
            update_archive_item_discovery_metadata(
                item,
                category_names=["Count Cat"],
                event_names=[],
                tag_names=["Count Tag A", "Count Tag B"],
            )
        update_archive_item_discovery_metadata(
            other,
            category_names=[],
            event_names=[],
            tag_names=["Count Tag A"],
        )

        authorized = archive_browse_queryset_for_user(None)
        filtered = filter_archive_items_by_advanced_filters(
            authorized,
            normalize_archive_advanced_filters(
                {
                    "category": str(cat.id),
                    "tag": [str(tag_a.id), str(tag_b.id)],
                }
            ),
        )
        with CaptureQueriesContext(connection) as ctx:
            counts = aggregate_archive_public_list_type_counts(filtered)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL], 3)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS], 1)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO], 1)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO], 1)
        self.assertEqual(_type_aggregate_query_count(ctx), 1)

        resp = self.client.get(
            self.url,
            [
                ("category", str(cat.id)),
                ("tag", str(tag_a.id)),
                ("tag", str(tag_b.id)),
                ("item_type", "photo"),
            ],
        )
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(
            _counts_by_slug(resp.context["item_type_filter_links"])[
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL
            ],
            3,
        )

    def test_visibility_restrictions_respected_in_tab_counts(self):
        _public_manual(title="Vis public manual")
        _public_photo(title="Vis public photo")
        create_manual_text_archive_item(
            title="Vis private manual",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        private_photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Vis private photo",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        PhotoContent.objects.create(
            archive_item=private_photo,
            original_file_key=f"photos/{private_photo.pk}/original.jpg",
            original_filename="original.jpg",
            original_mime_type="image/jpeg",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
        )

        anon = self.client.get(self.url)
        self.assertEqual(
            _counts_by_slug(anon.context["item_type_filter_links"]),
            {
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 2,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 1,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 1,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 0,
            },
        )

        family = User.objects.create_user(username="type-count-family", password="x")
        family.groups.add(self.family_group)
        self.client.force_login(family)
        family_resp = self.client.get(self.url)
        self.assertEqual(
            _counts_by_slug(family_resp.context["item_type_filter_links"]),
            {
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL: 4,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_DOCUMENTS_AND_TEXTS: 2,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_PHOTO: 2,
                ARCHIVE_PUBLIC_LIST_TYPE_FILTER_VIDEO: 0,
            },
        )

    def test_ordinary_archive_request_uses_one_type_aggregate_not_per_tab_counts(self):
        for i in range(3):
            _public_manual(title=f"Cost manual {i}")
        _public_photo(title="Cost photo")
        _public_video(title="Cost video")

        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_type_aggregate_query_count(ctx), 1)
        # Guard against accidental per-tab COUNT(*) loops (no item_type predicate
        # repeats for each public tab slug in separate simple counts).
        simple_type_counts = 0
        for query in ctx:
            sql = query["sql"].lower().replace('"', "")
            if "count(*)" not in sql and "count(" not in sql:
                continue
            if "documents_archiveitem" not in sql:
                continue
            if "filter" in sql or "case when" in sql:
                continue
            if "item_type" in sql:
                simple_type_counts += 1
        self.assertEqual(simple_type_counts, 0)

    def test_search_filtered_aggregate_still_one_query(self):
        item = _public_manual(title="AggSearchTerm title")
        _rebuild(item.pk)
        authorized = archive_browse_queryset_for_user(None)
        searched = filter_archive_items_by_search_query(authorized, "AggSearchTerm")
        with CaptureQueriesContext(connection) as ctx:
            counts = aggregate_archive_public_list_type_counts(searched)
        self.assertEqual(counts[ARCHIVE_PUBLIC_LIST_TYPE_FILTER_ALL], 1)
        self.assertEqual(_type_aggregate_query_count(ctx), 1)
