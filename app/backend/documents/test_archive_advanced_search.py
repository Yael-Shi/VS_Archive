"""Public /archive/ advanced filter backend contract (PR1)."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    Tag,
)
from documents.services.archive_advanced_search import (
    ArchiveAdvancedFilters,
    archive_advanced_filter_choice_context,
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    archive_public_list_filter_context,
    archive_public_list_pagination_context,
    build_archive_public_list_query,
    filter_archive_items_by_public_list_type,
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    update_archive_item_discovery_metadata,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.test_historical_person_tag_reuse import _create_tag

User = get_user_model()


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _rebuild(archive_item_id: int) -> None:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    rebuild_archive_item_search_index(item)


def _public_item(
    *,
    title: str,
    body: str = "body",
    author_name: str = "",
    date_start=None,
    date_end=None,
    date_precision: str | None = None,
    category_names: list[str] | None = None,
    event_names: list[str] | None = None,
    tag_names: list[str] | None = None,
) -> ArchiveItem:
    item = create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )
    if category_names is not None or event_names is not None or tag_names is not None:
        update_archive_item_discovery_metadata(
            item,
            category_names=category_names or [],
            event_names=event_names or [],
            tag_names=tag_names or [],
        )
    return item


class ArchiveAdvancedFilterNormalizationTests(SimpleTestCase):
    def test_author_is_trimmed_single_value(self):
        filters = normalize_archive_advanced_filters({"author": "  Alice  "})
        self.assertEqual(filters.author, "Alice")

    def test_repeatable_author_params_keep_first_nonempty_only(self):
        filters = normalize_archive_advanced_filters(
            [
                ("author", "  "),
                ("author", "Alice"),
                ("author", "Bob"),
            ]
        )
        self.assertEqual(filters.author, "Alice")
        self.assertEqual(
            normalize_archive_advanced_filters(
                [("author", "Alice"), ("author", "Bob")]
            ).query_param_pairs(),
            [("author", "Alice")],
        )

    def test_repeatable_ids_preserve_order_and_dedupe(self):
        filters = normalize_archive_advanced_filters(
            [
                ("category", "3"),
                ("category", "1"),
                ("category", "3"),
                ("category", "abc"),
                ("category", "0"),
                ("category", "-2"),
                ("event", "9"),
                ("event", "9"),
                ("tag", "5"),
                ("person", "10"),
                ("person", "13"),
                ("person", "10"),
                ("person", "abc"),
                ("person", "0"),
            ]
        )
        self.assertEqual(filters.category_ids, (3, 1))
        self.assertEqual(filters.event_ids, (9,))
        self.assertEqual(filters.tag_ids, (5,))
        self.assertEqual(filters.person_ids, (10, 13))

    def test_year_to_without_year_is_ignored(self):
        filters = normalize_archive_advanced_filters({"year_to": "1960"})
        self.assertIsNone(filters.year)
        self.assertIsNone(filters.year_to)
        self.assertFalse(filters.is_active())

    def test_malformed_year_is_ignored(self):
        for raw in ("abc", "19.5", "+1950", "0", "10000", "-1"):
            with self.subTest(raw=raw):
                filters = normalize_archive_advanced_filters(
                    {"year": raw, "year_to": "1960"}
                )
                self.assertIsNone(filters.year)
                self.assertIsNone(filters.year_to)

    def test_malformed_year_to_falls_back_to_single_year(self):
        filters = normalize_archive_advanced_filters(
            {"year": "1953", "year_to": "nope"}
        )
        self.assertEqual(filters.year, 1953)
        self.assertEqual(filters.year_to, 1953)

    def test_reverse_year_range_falls_back_to_first_year_single_window(self):
        filters = normalize_archive_advanced_filters(
            {"year": "1960", "year_to": "1950"}
        )
        self.assertEqual(filters.year, 1960)
        self.assertEqual(filters.year_to, 1960)

    def test_query_param_pairs_round_trip_repeatables(self):
        filters = ArchiveAdvancedFilters(
            author="Alice",
            category_ids=(2, 7),
            event_ids=(4,),
            tag_ids=(8, 9),
            person_ids=(10, 13),
            year=1950,
            year_to=1955,
        )
        query = build_archive_public_list_query(
            q="hello",
            item_type_filter="photo",
            page=2,
            per_page=24,
            advanced_filters=filters,
        )
        parsed = parse_qs(query, keep_blank_values=True)
        self.assertEqual(parsed["q"], ["hello"])
        self.assertEqual(parsed["item_type"], ["photo"])
        self.assertEqual(parsed["author"], ["Alice"])
        self.assertEqual(parsed["category"], ["2", "7"])
        self.assertEqual(parsed["event"], ["4"])
        self.assertEqual(parsed["tag"], ["8", "9"])
        self.assertEqual(parsed["person"], ["10", "13"])
        self.assertEqual(parsed["year"], ["1950"])
        self.assertEqual(parsed["year_to"], ["1955"])
        self.assertEqual(parsed["page"], ["2"])
        self.assertEqual(parsed["per_page"], ["24"])


class ArchiveAdvancedFilterQuerysetTests(TestCase):
    def test_author_filter_exact_match(self):
        match = _public_item(title="Author match", author_name="Exact Author")
        _public_item(title="Author other", author_name="Other Author")
        _public_item(title="Author blank", author_name="")
        filters = normalize_archive_advanced_filters({"author": "Exact Author"})
        ids = _ids(
            filter_archive_items_by_advanced_filters(ArchiveItem.objects.all(), filters)
        )
        self.assertEqual(ids, [match.pk])

    def test_category_single_and_multiple_or(self):
        cat_a = ArchiveCategory.objects.create(name="Cat A", slug="cat-a")
        cat_b = ArchiveCategory.objects.create(name="Cat B", slug="cat-b")
        only_a = _public_item(title="Only A", category_names=["Cat A"])
        only_b = _public_item(title="Only B", category_names=["Cat B"])
        both = _public_item(title="Both cats", category_names=["Cat A", "Cat B"])
        _public_item(title="Neither cat", category_names=["Other Cat"])

        single = normalize_archive_advanced_filters({"category": str(cat_a.id)})
        self.assertEqual(
            set(
                _ids(
                    filter_archive_items_by_advanced_filters(
                        ArchiveItem.objects.all(), single
                    )
                )
            ),
            {only_a.pk, both.pk},
        )

        multi = normalize_archive_advanced_filters(
            [("category", str(cat_a.id)), ("category", str(cat_b.id))]
        )
        self.assertEqual(
            set(
                _ids(
                    filter_archive_items_by_advanced_filters(
                        ArchiveItem.objects.all(), multi
                    )
                )
            ),
            {only_a.pk, only_b.pk, both.pk},
        )

    def test_event_and_tag_multiple_or(self):
        event_a = ArchiveEvent.objects.create(name="Event A", slug="event-a")
        event_b = ArchiveEvent.objects.create(name="Event B", slug="event-b")
        tag_a = Tag.objects.create(name="Tag A")
        tag_b = Tag.objects.create(name="Tag B")
        only_event_a = _public_item(title="Event only A", event_names=["Event A"])
        only_event_b = _public_item(title="Event only B", event_names=["Event B"])
        only_tag_a = _public_item(title="Tag only A", tag_names=["Tag A"])
        only_tag_b = _public_item(title="Tag only B", tag_names=["Tag B"])

        event_ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters(
                    [("event", str(event_a.id)), ("event", str(event_b.id))]
                ),
            )
        )
        self.assertEqual(set(event_ids), {only_event_a.pk, only_event_b.pk})

        tag_ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters(
                    [("tag", str(tag_a.id)), ("tag", str(tag_b.id))]
                ),
            )
        )
        self.assertEqual(set(tag_ids), {only_tag_a.pk, only_tag_b.pk})

    def test_groups_combine_with_and(self):
        cat = ArchiveCategory.objects.create(name="And Cat", slug="and-cat")
        tag = Tag.objects.create(name="And Tag")
        match = _public_item(
            title="And match",
            category_names=["And Cat"],
            tag_names=["And Tag"],
        )
        _public_item(title="Cat only", category_names=["And Cat"])
        _public_item(title="Tag only", tag_names=["And Tag"])
        filters = normalize_archive_advanced_filters(
            [("category", str(cat.id)), ("tag", str(tag.id))]
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            ),
            [match.pk],
        )

    def test_item_matching_multiple_selected_m2m_appears_once(self):
        cat_a = ArchiveCategory.objects.create(name="Dup Cat A", slug="dup-cat-a")
        cat_b = ArchiveCategory.objects.create(name="Dup Cat B", slug="dup-cat-b")
        both = _public_item(
            title="Dup both",
            category_names=["Dup Cat A", "Dup Cat B"],
        )
        filters = normalize_archive_advanced_filters(
            [("category", str(cat_a.id)), ("category", str(cat_b.id))]
        )
        ids = _ids(
            filter_archive_items_by_advanced_filters(ArchiveItem.objects.all(), filters)
        )
        self.assertEqual(ids.count(both.pk), 1)
        self.assertEqual(ids, [both.pk])

    def test_q_plus_advanced_filter_preserves_ranking(self):
        shared = "advranksharedtoken"
        cat = ArchiveCategory.objects.create(name="Rank Cat", slug="rank-cat")
        body_hit = _public_item(
            title="Body rank",
            body=f"intro {shared} outro",
            category_names=["Rank Cat"],
        )
        meta_hit = _public_item(
            title="Meta rank",
            body="no shared",
            author_name=shared,
            category_names=["Rank Cat"],
        )
        title_hit = _public_item(
            title=shared,
            body="no shared body",
            category_names=["Rank Cat"],
        )
        excluded = _public_item(
            title=shared,
            body="other category",
            category_names=["Other Rank Cat"],
        )
        for item in (body_hit, meta_hit, title_hit, excluded):
            _rebuild(item.pk)

        qs = filter_archive_items_by_advanced_filters(
            ArchiveItem.objects.all(),
            normalize_archive_advanced_filters({"category": str(cat.id)}),
        )
        ranked = filter_archive_items_by_search_query(qs, shared)
        self.assertEqual(_ids(ranked), [title_hit.pk, meta_hit.pk, body_hit.pk])
        self.assertNotIn(excluded.pk, _ids(ranked))

    def test_item_type_and_advanced_filters_compose(self):
        cat = ArchiveCategory.objects.create(name="Type Cat", slug="type-cat")
        manual = _public_item(title="Type manual", category_names=["Type Cat"])
        photo = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="Type photo",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo.categories.add(cat)
        qs = filter_archive_items_by_public_list_type(
            ArchiveItem.objects.all(), "documents_and_texts"
        )
        filtered = filter_archive_items_by_advanced_filters(
            qs,
            normalize_archive_advanced_filters({"category": str(cat.id)}),
        )
        self.assertEqual(_ids(filtered), [manual.pk])

    def test_single_year_exact_month_year_and_range_overlap(self):
        exact = _public_item(
            title="Exact day 1953",
            date_start=date(1953, 6, 15),
            date_end=date(1953, 6, 15),
            date_precision=ArchiveItem.DatePrecision.EXACT_DAY,
        )
        month = _public_item(
            title="Month 1953",
            date_start=date(1953, 3, 1),
            date_end=date(1953, 3, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        year = _public_item(
            title="Year 1953",
            date_start=date(1953, 1, 1),
            date_end=date(1953, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        overlapping = _public_item(
            title="Range crossing 1953",
            date_start=date(1950, 1, 1),
            date_end=date(1955, 12, 31),
            date_precision=ArchiveItem.DatePrecision.RANGE_YEAR,
        )
        outside = _public_item(
            title="Outside 1954",
            date_start=date(1954, 1, 1),
            date_end=date(1954, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        filters = normalize_archive_advanced_filters({"year": "1953"})
        ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            )
        )
        self.assertEqual(ids, {exact.pk, month.pk, year.pk, overlapping.pk})
        self.assertNotIn(outside.pk, ids)

    def test_year_range_overlap_at_both_boundaries(self):
        start_edge = _public_item(
            title="Start edge 1950",
            date_start=date(1950, 1, 1),
            date_end=date(1950, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        end_edge = _public_item(
            title="End edge 1955",
            date_start=date(1955, 1, 1),
            date_end=date(1955, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        inside = _public_item(
            title="Inside 1952",
            date_start=date(1952, 5, 1),
            date_end=date(1952, 5, 31),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        before = _public_item(
            title="Before 1949",
            date_start=date(1949, 1, 1),
            date_end=date(1949, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        after = _public_item(
            title="After 1956",
            date_start=date(1956, 1, 1),
            date_end=date(1956, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        filters = normalize_archive_advanced_filters(
            {"year": "1950", "year_to": "1955"}
        )
        ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            )
        )
        self.assertEqual(ids, {start_edge.pk, end_edge.pk, inside.pk})
        self.assertNotIn(before.pk, ids)
        self.assertNotIn(after.pk, ids)

    def test_unknown_and_missing_dates_excluded_when_date_filter_active(self):
        unknown_with_bounds = create_manual_text_archive_item(
            title="Unknown with hidden bounds",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=date(1953, 1, 1),
            date_end=date(1953, 12, 31),
            date_precision=ArchiveItem.DatePrecision.UNKNOWN,
        )
        undated = _public_item(title="Truly undated")
        known = _public_item(
            title="Known 1953",
            date_start=date(1953, 1, 1),
            date_end=date(1953, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        filters = normalize_archive_advanced_filters({"year": "1953"})
        ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            )
        )
        self.assertEqual(ids, {known.pk})
        self.assertNotIn(unknown_with_bounds.pk, ids)
        self.assertNotIn(undated.pk, ids)

    def test_no_date_filter_keeps_undated_items_eligible(self):
        undated = _public_item(title="Undated eligible")
        filters = normalize_archive_advanced_filters({})
        ids = _ids(
            filter_archive_items_by_advanced_filters(ArchiveItem.objects.all(), filters)
        )
        self.assertIn(undated.pk, ids)


class ArchiveAdvancedFilterVisibilityAndViewTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family = User.objects.create_user(username="adv-family", password="x")
        self.family.groups.add(self.family_group)

    def test_choice_context_does_not_leak_private_metadata(self):
        public_cat = ArchiveCategory.objects.create(
            name="Public Choice Cat", slug="public-choice-cat"
        )
        private_cat = ArchiveCategory.objects.create(
            name="Private Choice Cat", slug="private-choice-cat"
        )
        public_tag = _create_tag(name="Public Choice Tag")
        private_tag = _create_tag(name="Private Choice Tag")
        public_person = Person.objects.create(name="Public Choice Person")
        private_person = Person.objects.create(name="Private Choice Person")
        PersonAlias.objects.create(person=public_person, name="Public Alias")
        public_event = ArchiveEvent.objects.create(
            name="Public Choice Event", slug="public-choice-event"
        )
        private_event = ArchiveEvent.objects.create(
            name="Private Choice Event", slug="private-choice-event"
        )

        public_item = _public_item(
            title="Public choice item",
            author_name="Public Author",
            category_names=["Public Choice Cat"],
            event_names=["Public Choice Event"],
            tag_names=["Public Choice Tag"],
        )
        ArchiveItemPerson.objects.create(archive_item=public_item, person=public_person)
        private_item = create_manual_text_archive_item(
            title="Private choice item",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="Private Author",
        )
        update_archive_item_discovery_metadata(
            private_item,
            category_names=["Private Choice Cat"],
            event_names=["Private Choice Event"],
            tag_names=["Private Choice Tag"],
        )
        ArchiveItemPerson.objects.create(
            archive_item=private_item, person=private_person
        )

        anon_qs = archive_browse_queryset_for_user(None)
        anon_choices = archive_advanced_filter_choice_context(anon_qs)
        self.assertEqual(
            anon_choices["advanced_filter_author_choices"], ("Public Author",)
        )
        self.assertEqual(
            [c.pk for c in anon_choices["advanced_filter_category_choices"]],
            [public_cat.pk],
        )
        self.assertEqual(
            [e.pk for e in anon_choices["advanced_filter_event_choices"]],
            [public_event.pk],
        )
        self.assertEqual(
            [t.pk for t in anon_choices["advanced_filter_tag_choices"]],
            [public_tag.pk],
        )
        self.assertEqual(
            [p.pk for p in anon_choices["advanced_filter_person_choices"]],
            [public_person.pk],
        )
        self.assertEqual(
            [p.name for p in anon_choices["advanced_filter_person_choices"]],
            ["Public Choice Person"],
        )
        self.assertNotIn(private_item.pk, _ids(anon_qs))
        self.assertIn(public_item.pk, _ids(anon_qs))

        family_qs = archive_browse_queryset_for_user(self.family)
        family_choices = archive_advanced_filter_choice_context(family_qs)
        self.assertEqual(
            set(family_choices["advanced_filter_author_choices"]),
            {"Public Author", "Private Author"},
        )
        self.assertEqual(
            {c.pk for c in family_choices["advanced_filter_category_choices"]},
            {public_cat.pk, private_cat.pk},
        )
        self.assertEqual(
            {e.pk for e in family_choices["advanced_filter_event_choices"]},
            {public_event.pk, private_event.pk},
        )
        self.assertEqual(
            {t.pk for t in family_choices["advanced_filter_tag_choices"]},
            {public_tag.pk, private_tag.pk},
        )
        self.assertEqual(
            {p.pk for p in family_choices["advanced_filter_person_choices"]},
            {public_person.pk, private_person.pk},
        )

    def test_unauthorized_items_cannot_surface_via_advanced_filters(self):
        cat = ArchiveCategory.objects.create(
            name="Secret Filter Cat", slug="secret-filter-cat"
        )
        private_item = create_manual_text_archive_item(
            title="SECRET-ADV-PRIVATE-TITLE",
            body="secret body",
            visibility=ArchiveItem.Visibility.PRIVATE,
            author_name="Secret Author",
            date_start=date(1953, 1, 1),
            date_end=date(1953, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        update_archive_item_discovery_metadata(
            private_item,
            category_names=["Secret Filter Cat"],
            event_names=[],
            tag_names=[],
        )
        _public_item(
            title="Public other",
            author_name="Other",
            category_names=["Other Cat"],
        )

        resp = self.client.get(
            reverse("archive-list"),
            {
                "author": "Secret Author",
                "category": str(cat.id),
                "year": "1953",
            },
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertNotIn("SECRET-ADV-PRIVATE-TITLE", html)
        self.assertEqual(resp.context["total_count"], 0)
        self.assertEqual(list(resp.context["items"]), [])
        # Submitted filter values may echo in query-preservation inputs, but
        # authorized choice context must not expose private-only metadata.
        self.assertNotIn(
            "Secret Author",
            resp.context["advanced_filter_author_choices"],
        )
        self.assertNotIn(
            cat.pk,
            [c.pk for c in resp.context["advanced_filter_category_choices"]],
        )
        self.assertNotIn(
            "Secret Filter Cat",
            [c.name for c in resp.context["advanced_filter_category_choices"]],
        )

    def test_pagination_and_type_links_preserve_advanced_filters(self):
        cat = ArchiveCategory.objects.create(name="Page Cat", slug="page-cat")
        event = ArchiveEvent.objects.create(name="Page Event", slug="page-event")
        tag = Tag.objects.create(name="Page Tag")
        filters = ArchiveAdvancedFilters(
            author="Page Author",
            category_ids=(cat.id,),
            event_ids=(event.id,),
            tag_ids=(tag.id,),
            person_ids=(10,),
            year=1950,
            year_to=1955,
        )
        for index in range(50):
            _public_item(
                title=f"ADVPAGE-{index:02d}",
                author_name="Page Author",
                category_names=["Page Cat"],
                event_names=["Page Event"],
                tag_names=["Page Tag"],
                date_start=date(1952, 1, 1),
                date_end=date(1952, 12, 31),
                date_precision=ArchiveItem.DatePrecision.YEAR,
            )

        pagination = archive_public_list_pagination_context(
            total_count=50,
            page=2,
            per_page=24,
            q="ADVPAGE",
            item_type_filter="documents_and_texts",
            advanced_filters=filters,
        )
        next_href = str(pagination["next_href_suffix"])
        prev_href = str(pagination["prev_href_suffix"])
        for href in (next_href, prev_href):
            parsed = parse_qs(href.lstrip("?"))
            self.assertEqual(parsed["author"], ["Page Author"])
            self.assertEqual(parsed["category"], [str(cat.id)])
            self.assertEqual(parsed["event"], [str(event.id)])
            self.assertEqual(parsed["tag"], [str(tag.id)])
            self.assertEqual(parsed["person"], ["10"])
            self.assertEqual(parsed["year"], ["1950"])
            self.assertEqual(parsed["year_to"], ["1955"])
            self.assertEqual(parsed["q"], ["ADVPAGE"])
            self.assertEqual(parsed["item_type"], ["documents_and_texts"])

        type_context = archive_public_list_filter_context(
            q="ADVPAGE",
            item_type_filter="documents_and_texts",
            per_page=24,
            advanced_filters=filters,
        )
        photo_link = next(
            link
            for link in type_context["item_type_filter_links"]
            if link["label"] == "תמונות"
        )
        photo_parsed = parse_qs(str(photo_link["href_suffix"]).lstrip("?"))
        self.assertEqual(photo_parsed["author"], ["Page Author"])
        self.assertEqual(photo_parsed["category"], [str(cat.id)])
        self.assertEqual(photo_parsed["event"], [str(event.id)])
        self.assertEqual(photo_parsed["tag"], [str(tag.id)])
        self.assertEqual(photo_parsed["person"], ["10"])
        self.assertNotIn("page", photo_parsed)

        resp = self.client.get(
            reverse("archive-list"),
            [
                ("q", "ADVPAGE"),
                ("item_type", "documents_and_texts"),
                ("author", "Page Author"),
                ("category", str(cat.id)),
                ("event", str(event.id)),
                ("tag", str(tag.id)),
                ("person", "10"),
                ("year", "1950"),
                ("year_to", "1955"),
                ("per_page", "24"),
                ("page", "2"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn('name="author" value="Page Author"', html)
        self.assertIn(f'name="category" value="{cat.id}"', html)
        self.assertIn(f'name="event" value="{event.id}"', html)
        self.assertIn(f'name="tag" value="{tag.id}"', html)
        self.assertIn('name="person" value="10"', html)
        self.assertIn('name="year" value="1950"', html)
        self.assertIn('name="year_to" value="1955"', html)
        self.assertNotIn('name="page"', html)
        self.assertIn("author=Page+Author", html)
        self.assertIn(f"category={cat.id}", html)
        self.assertIn("item_type=photo", html)

        # Ensure type-filter hrefs in rendered HTML round-trip repeatables.
        self.assertIn(f"category={cat.id}", html)
        self.assertIn(f"event={event.id}", html)
        self.assertIn(f"tag={tag.id}", html)
        self.assertIn("person=10", html)

    def test_view_applies_filters_before_pagination(self):
        cat = ArchiveCategory.objects.create(name="View Cat", slug="view-cat")
        match = _public_item(
            title="ADVVIEW-MATCH",
            author_name="View Author",
            category_names=["View Cat"],
            date_start=date(1953, 6, 1),
            date_end=date(1953, 6, 30),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        _public_item(
            title="ADVVIEW-OTHER",
            author_name="Other Author",
            category_names=["Other View Cat"],
            date_start=date(1960, 1, 1),
            date_end=date(1960, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        resp = self.client.get(
            reverse("archive-list"),
            {
                "author": "View Author",
                "category": str(cat.id),
                "year": "1953",
            },
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertIn("ADVVIEW-MATCH", html)
        self.assertNotIn("ADVVIEW-OTHER", html)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual([item.pk for item in resp.context["items"]], [match.pk])
