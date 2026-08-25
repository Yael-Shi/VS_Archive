"""Public /archive/ advanced Person filter: ArchiveItemPerson only, not PhotoPerson."""

from __future__ import annotations

from datetime import date
from urllib.parse import parse_qs

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemPerson,
    Document,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_advanced_search import (
    archive_advanced_filter_choice_context,
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import (
    archive_public_list_active_filter_summary_context,
    build_archive_public_list_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
    update_archive_item_discovery_metadata,
)
from documents.test_archive_item import create_viewable_ocr_document
from documents.test_historical_person_tag_reuse import _create_tag

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _ids(queryset) -> list[int]:
    return list(queryset.values_list("pk", flat=True))


def _public_manual(title: str, **kwargs) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=kwargs.pop("body", "body"),
        visibility=ArchiveItem.Visibility.PUBLIC,
        **kwargs,
    )


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


def _add_photo(item: ArchiveItem, *, position: int = 1) -> PhotoContent:
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=f"photos/{item.pk}-{position}/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        upload_error="",
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


def _person_related_query_count(captured_queries) -> int:
    count = 0
    for query in captured_queries:
        sql = query["sql"].lower().replace('"', "")
        if not sql.lstrip().startswith("select"):
            continue
        if "documents_person" not in sql:
            continue
        if "documents_photoperson" in sql:
            continue
        if "documents_archiveitemperson" in sql or "archive_items" in sql:
            count += 1
    return count


class ArchiveAdvancedPersonFilterQuerysetTests(TestCase):
    def test_single_person_id_filters_archive_item_person_links(self):
        ada = Person.objects.create(name="Ada Lovelace")
        charles = Person.objects.create(name="Charles Babbage")
        linked = _public_manual("Linked to Ada")
        other = _public_manual("Linked to Charles")
        ArchiveItemPerson.objects.create(archive_item=linked, person=ada)
        ArchiveItemPerson.objects.create(archive_item=other, person=charles)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(ada.id)}),
            )
        )
        self.assertEqual(ids, [linked.pk])

    def test_repeatable_person_ids_use_or_semantics(self):
        ada = Person.objects.create(name="Ada")
        charles = Person.objects.create(name="Charles")
        only_ada = _public_manual("Only Ada")
        only_charles = _public_manual("Only Charles")
        both = _public_manual("Both people")
        _public_manual("Neither person")
        ArchiveItemPerson.objects.create(archive_item=only_ada, person=ada)
        ArchiveItemPerson.objects.create(archive_item=only_charles, person=charles)
        ArchiveItemPerson.objects.create(archive_item=both, person=ada)
        ArchiveItemPerson.objects.create(archive_item=both, person=charles)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters(
                    [("person", str(ada.id)), ("person", str(charles.id))]
                ),
            )
        )
        self.assertEqual(set(ids), {only_ada.pk, only_charles.pk, both.pk})
        self.assertEqual(ids.count(both.pk), 1)

    def test_person_group_ands_with_category_event_tag_and_year(self):
        person = Person.objects.create(name="And Person")
        cat = ArchiveCategory.objects.create(name="And Cat", slug="and-person-cat")
        event = ArchiveEvent.objects.create(name="And Event", slug="and-person-event")
        tag = Tag.objects.create(name="And Tag")
        match = create_manual_text_archive_item(
            title="And match",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            date_start=date(1953, 1, 1),
            date_end=date(1953, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        update_archive_item_discovery_metadata(
            match,
            category_names=["And Cat"],
            event_names=["And Event"],
            tag_names=["And Tag"],
        )
        ArchiveItemPerson.objects.create(archive_item=match, person=person)

        person_only = _public_manual("Person only")
        ArchiveItemPerson.objects.create(archive_item=person_only, person=person)
        cat_only = _public_manual("Cat only")
        update_archive_item_discovery_metadata(
            cat_only,
            category_names=["And Cat"],
            event_names=[],
            tag_names=[],
        )

        filters = normalize_archive_advanced_filters(
            [
                ("person", str(person.id)),
                ("category", str(cat.id)),
                ("event", str(event.id)),
                ("tag", str(tag.id)),
                ("year", "1953"),
            ]
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            ),
            [match.pk],
        )

    def test_photoperson_only_item_does_not_match_person_filter(self):
        person = Person.objects.create(name="Appears Only")
        photo_only = _create_photo_item(title="PhotoPerson only")
        photo = _add_photo(photo_only)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        self.assertFalse(photo_only.people.filter(pk=person.pk).exists())

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [])

    def test_archive_item_person_photo_item_does_match(self):
        person = Person.objects.create(name="Item Linked Photo")
        photo_item = _create_photo_item(title="ArchiveItemPerson photo")
        _add_photo(photo_item)
        ArchiveItemPerson.objects.create(archive_item=photo_item, person=person)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [photo_item.pk])

    def test_video_ocr_manual_and_photo_item_types_all_match(self):
        person = Person.objects.create(name="All Types Person")
        manual = _public_manual("Manual linked")
        ocr = create_viewable_ocr_document(
            title="OCR linked",
            doc_type=Document.DocType.PDF,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
        )
        video = create_video_archive_item(
            title="Video linked",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = _create_photo_item(title="Photo linked")
        _add_photo(photo)
        for item in (manual, ocr.archive_item, video, photo):
            ArchiveItemPerson.objects.create(archive_item=item, person=person)

        ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"person": str(person.id)}),
                )
            )
        )
        self.assertEqual(ids, {manual.pk, ocr.archive_item_id, video.pk, photo.pk})

    def test_malformed_person_ids_are_skipped_like_other_relation_filters(self):
        person = Person.objects.create(name="Valid Person")
        linked = _public_manual("Linked")
        ArchiveItemPerson.objects.create(archive_item=linked, person=person)
        filters = normalize_archive_advanced_filters(
            [
                ("person", "abc"),
                ("person", "0"),
                ("person", "-2"),
                ("person", person.name),
                ("person", "Ada Alias"),
                ("person", str(person.id)),
                ("person", str(person.id)),
            ]
        )
        self.assertEqual(filters.person_ids, (person.id,))
        ids = _ids(
            filter_archive_items_by_advanced_filters(ArchiveItem.objects.all(), filters)
        )
        self.assertEqual(ids, [linked.pk])

    def test_unknown_person_id_matches_nothing_like_unknown_category(self):
        cat = ArchiveCategory.objects.create(name="Known Cat", slug="known-cat")
        person = Person.objects.create(name="Known Person")
        linked = _public_manual("Known linked")
        update_archive_item_discovery_metadata(
            linked,
            category_names=["Known Cat"],
            event_names=[],
            tag_names=[],
        )
        ArchiveItemPerson.objects.create(archive_item=linked, person=person)

        unknown_person = normalize_archive_advanced_filters({"person": "999999"})
        unknown_category = normalize_archive_advanced_filters({"category": "999999"})
        self.assertEqual(unknown_person.person_ids, (999999,))
        self.assertEqual(unknown_category.category_ids, (999999,))
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), unknown_person
                )
            ),
            [],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), unknown_category
                )
            ),
            [],
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"category": str(cat.id)}),
                )
            ),
            [linked.pk],
        )

        mixed = normalize_archive_advanced_filters(
            [("person", "999999"), ("person", str(person.id))]
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), mixed
                )
            ),
            [linked.pk],
        )


class ArchiveAdvancedPersonFilterAccessTests(TestCase):
    def setUp(self):
        self.url = reverse("archive-list")
        self.family = User.objects.create_user(
            username="person-adv-family", password="x"
        )
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="person-adv-restricted", password="x")
        )

    def test_private_and_restricted_visibility_still_enforced(self):
        person = Person.objects.create(name="Hidden Filter Person")
        private = create_manual_text_archive_item(
            title="PERSON-PRIVATE-TITLE",
            body="secret",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted = create_manual_text_archive_item(
            title="PERSON-RESTRICTED-TITLE",
            body="secret",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        public = _public_manual("PERSON-PUBLIC-TITLE")
        ArchiveItemPerson.objects.create(archive_item=private, person=person)
        ArchiveItemPerson.objects.create(archive_item=restricted, person=person)
        ArchiveItemPerson.objects.create(archive_item=public, person=person)

        anon = self.client.get(self.url, {"person": str(person.id)})
        self.assertEqual(anon.status_code, 200)
        anon_html = anon.content.decode("utf-8")
        self.assertIn("PERSON-PUBLIC-TITLE", anon_html)
        self.assertNotIn("PERSON-PRIVATE-TITLE", anon_html)
        self.assertNotIn("PERSON-RESTRICTED-TITLE", anon_html)
        self.assertEqual(anon.context["total_count"], 1)

        self.client.force_login(self.family)
        family = self.client.get(self.url, {"person": str(person.id)})
        family_titles = {item.title for item in family.context["items"]}
        self.assertEqual(family_titles, {"PERSON-PUBLIC-TITLE", "PERSON-PRIVATE-TITLE"})
        self.assertNotIn("PERSON-RESTRICTED-TITLE", family_titles)

        self.client.force_login(self.restricted_user)
        restricted_resp = self.client.get(self.url, {"person": str(person.id)})
        restricted_titles = {item.title for item in restricted_resp.context["items"]}
        self.assertEqual(
            restricted_titles, {"PERSON-PUBLIC-TITLE", "PERSON-RESTRICTED-TITLE"}
        )


class ArchiveAdvancedPersonFilterUiTests(TestCase):
    def setUp(self):
        self.url = reverse("archive-list")

    def test_canonical_label_and_id_value_in_picker(self):
        person = Person.objects.create(name="יעקב כהן")
        PersonAlias.objects.create(person=person, name="Yankele")
        item = _public_manual("Picker item")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        resp = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(resp.status_code, 200)
        choices = list(resp.context["advanced_filter_person_choices"])
        self.assertEqual([p.pk for p in choices], [person.pk])
        self.assertEqual([p.name for p in choices], ["יעקב כהן"])
        html = resp.content.decode("utf-8")
        self.assertIn('id="archive-filter-person"', html)
        self.assertIn('name="person"', html)
        self.assertIn("multiple", html)
        self.assertIn(f'value="{person.id}"', html)
        self.assertIn("יעקב כהן", html)
        self.assertNotIn("Yankele", html)
        self.assertNotIn(f">{person.id}<", html)

    def test_aliases_are_not_independent_options(self):
        person = Person.objects.create(name="Canonical Only")
        PersonAlias.objects.create(person=person, name="Alias One")
        PersonAlias.objects.create(person=person, name="Alias Two")
        item = _public_manual("Alias options item")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        resp = self.client.get(self.url, {"advanced": "1"})
        choices = list(resp.context["advanced_filter_person_choices"])
        self.assertEqual([p.pk for p in choices], [person.pk])
        self.assertEqual([p.name for p in choices], ["Canonical Only"])
        html = resp.content.decode("utf-8")
        self.assertNotIn("Alias One", html)
        self.assertNotIn("Alias Two", html)

    def test_selected_person_state_persists_after_submit(self):
        ada = Person.objects.create(name="Ada Persist")
        charles = Person.objects.create(name="Charles Persist")
        item = _public_manual("Persist item")
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        resp = self.client.get(
            self.url,
            [
                ("advanced", "1"),
                ("person", str(ada.id)),
                ("person", str(charles.id)),
            ],
        )
        self.assertEqual(
            resp.context["advanced_filter_person_ids"], (ada.id, charles.id)
        )
        html = resp.content.decode("utf-8")
        self.assertRegex(html, rf'value="{ada.id}"\s+selected')
        self.assertRegex(html, rf'value="{charles.id}"\s+selected')

    def test_active_chips_show_canonical_name(self):
        person = Person.objects.create(name="Chip Canonical")
        PersonAlias.objects.create(person=person, name="Chip Alias")
        item = _public_manual("Chip person item")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        resp = self.client.get(self.url, {"person": str(person.id)})
        chips = resp.context["active_filter_chips"]
        person_chip = next(chip for chip in chips if chip["kind"] == "person")
        self.assertEqual(person_chip["value"], "Chip Canonical")
        self.assertIn("Chip Canonical", resp.content.decode("utf-8"))
        self.assertNotIn("Chip Alias", person_chip["value"])

    def test_removing_one_person_chip_preserves_other_state(self):
        ada = Person.objects.create(name="Ada Chip")
        charles = Person.objects.create(name="Charles Chip")
        cat = ArchiveCategory.objects.create(name="Keep Cat", slug="keep-cat")
        item = _public_manual("Chip keep state", author_name="Keep Author")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Keep Cat"],
            event_names=[],
            tag_names=[],
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        filters = normalize_archive_advanced_filters(
            [
                ("author", "Keep Author"),
                ("category", str(cat.id)),
                ("person", str(ada.id)),
                ("person", str(charles.id)),
            ]
        )
        summary = archive_public_list_active_filter_summary_context(
            q="KeepQuery",
            item_type_filter="photo",
            advanced_filters=filters,
            category_choices=[cat],
            person_choices=[ada, charles],
        )
        person_chip = next(
            chip for chip in summary["active_filter_chips"] if chip["kind"] == "person"
        )
        parsed = parse_qs(str(person_chip["remove_href_suffix"]).lstrip("?"))
        self.assertNotIn("person", parsed)
        self.assertEqual(parsed["q"], ["KeepQuery"])
        self.assertEqual(parsed["author"], ["Keep Author"])
        self.assertEqual(parsed["category"], [str(cat.id)])
        self.assertEqual(parsed["item_type"], ["photo"])

        resp = self.client.get(self.url, {"q": "KeepQuery", "person": str(ada.id)})
        live_chip = next(
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person"
        )
        live_parsed = parse_qs(str(live_chip["remove_href_suffix"]).lstrip("?"))
        self.assertEqual(live_parsed["q"], ["KeepQuery"])
        self.assertNotIn("person", live_parsed)

    def test_pagination_and_type_links_preserve_person_params(self):
        person = Person.objects.create(name="Page Person")
        for index in range(50):
            item = _public_manual(f"PERSONPAGE-{index:02d}")
            ArchiveItemPerson.objects.create(archive_item=item, person=person)

        query = build_archive_public_list_query(
            q="PERSONPAGE",
            item_type_filter="documents_and_texts",
            page=2,
            per_page=24,
            advanced_filters=normalize_archive_advanced_filters(
                {"person": str(person.id)}
            ),
        )
        parsed = parse_qs(query)
        self.assertEqual(parsed["person"], [str(person.id)])
        self.assertEqual(parsed["q"], ["PERSONPAGE"])
        self.assertEqual(parsed["page"], ["2"])

        resp = self.client.get(
            self.url,
            [
                ("q", "PERSONPAGE"),
                ("person", str(person.id)),
                ("per_page", "24"),
                ("page", "2"),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["page"], 2)
        self.assertEqual(resp.context["advanced_filter_person_ids"], (person.id,))
        html = resp.content.decode("utf-8")
        self.assertIn(f"person={person.id}", html)
        self.assertIn(f'name="person" value="{person.id}"', html)
        prev_parsed = parse_qs(str(resp.context["prev_href_suffix"]).lstrip("?"))
        self.assertEqual(prev_parsed["person"], [str(person.id)])
        self.assertEqual(prev_parsed["q"], ["PERSONPAGE"])

    def test_ordinary_and_q_only_skip_person_choice_loading(self):
        person = Person.objects.create(name="Skip Load Person")
        item = _public_manual("Skip load item")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        plain = self.client.get(self.url)
        self.assertFalse(plain.context["load_advanced_choices"])
        self.assertEqual(plain.context["advanced_filter_person_choices"], ())
        self.assertNotIn('id="archive-filter-person"', plain.content.decode("utf-8"))

        q_only = self.client.get(self.url, {"q": "Skip load"})
        self.assertFalse(q_only.context["load_advanced_choices"])
        self.assertEqual(q_only.context["advanced_filter_person_choices"], ())

        advanced = self.client.get(self.url, {"advanced": "1"})
        self.assertTrue(advanced.context["load_advanced_choices"])
        self.assertEqual(
            [p.pk for p in advanced.context["advanced_filter_person_choices"]],
            [person.pk],
        )

    def test_choice_query_count_does_not_scale_with_person_count(self):
        few_item = _public_manual("Few people")
        many_item = _public_manual("Many people")
        for index in range(2):
            person = Person.objects.create(name=f"Few {index:02d}")
            ArchiveItemPerson.objects.create(archive_item=few_item, person=person)
        for index in range(20):
            person = Person.objects.create(name=f"Many {index:02d}")
            ArchiveItemPerson.objects.create(archive_item=many_item, person=person)

        authorized = archive_browse_queryset_for_user(None)
        with CaptureQueriesContext(connection) as few_ctx:
            few_choices = archive_advanced_filter_choice_context(authorized)
        with CaptureQueriesContext(connection) as many_ctx:
            many_choices = archive_advanced_filter_choice_context(authorized)

        self.assertEqual(_person_related_query_count(few_ctx), 1)
        self.assertEqual(_person_related_query_count(many_ctx), 1)
        self.assertEqual(
            _person_related_query_count(few_ctx), _person_related_query_count(many_ctx)
        )
        self.assertGreaterEqual(len(few_choices["advanced_filter_person_choices"]), 22)
        self.assertGreaterEqual(len(many_choices["advanced_filter_person_choices"]), 22)

        with CaptureQueriesContext(connection) as request_ctx:
            resp = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_person_related_query_count(request_ctx), 2)

    def test_photoperson_only_person_is_not_a_choice(self):
        linked_person = Person.objects.create(name="Linked Choice")
        appearance_only = Person.objects.create(name="Appearance Only")
        linked_item = _public_manual("Has ArchiveItemPerson")
        ArchiveItemPerson.objects.create(archive_item=linked_item, person=linked_person)
        photo_item = _create_photo_item(title="Appearance photo")
        photo = _add_photo(photo_item)
        PhotoPerson.objects.create(photo_content=photo, person=appearance_only)

        resp = self.client.get(self.url, {"advanced": "1"})
        names = [p.name for p in resp.context["advanced_filter_person_choices"]]
        self.assertEqual(names, ["Linked Choice"])
        self.assertNotIn("Appearance Only", names)

    def test_public_person_page_is_not_a_filter_deeplink(self):
        person = Person.objects.create(name="Public Page Person")
        item = _public_manual("Public page person item")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        for name in (
            "archive-people-browse",
            "archive-person-browse",
        ):
            with self.assertRaises(NoReverseMatch):
                reverse(name)

        page = self.client.get(
            reverse("archive-person-detail", kwargs={"person_id": person.id})
        )
        self.assertEqual(page.status_code, 200)

        resp = self.client.get(self.url, {"person": str(person.id)})
        html = resp.content.decode("utf-8")
        self.assertIn(f"/archive/people/{person.id}/", html)
        card = resp.context["browse_cards"][0]
        self.assertNotIn("person=", card.detail_url)

    def test_existing_tag_advanced_filter_behavior_unchanged(self):
        person = Person.objects.create(name="Same Name Person")
        tag = _create_tag(name="Same Name Person")
        tagged = _public_manual("Tagged only")
        update_archive_item_discovery_metadata(
            tagged,
            category_names=[],
            event_names=[],
            tag_names=["Same Name Person"],
        )
        linked = _public_manual("Person linked only")
        ArchiveItemPerson.objects.create(archive_item=linked, person=person)
        both = _public_manual("Both tag and person")
        update_archive_item_discovery_metadata(
            both,
            category_names=[],
            event_names=[],
            tag_names=["Same Name Person"],
        )
        ArchiveItemPerson.objects.create(archive_item=both, person=person)

        tag_ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"tag": str(tag.id)}),
                )
            )
        )
        person_ids = set(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(),
                    normalize_archive_advanced_filters({"person": str(person.id)}),
                )
            )
        )
        self.assertEqual(tag_ids, {tagged.pk, both.pk})
        self.assertEqual(person_ids, {linked.pk, both.pk})

        resp = self.client.get(
            self.url,
            [
                ("advanced", "1"),
                ("tag", str(tag.id)),
                ("person", str(person.id)),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["advanced_filter_tag_ids"], (tag.id,))
        self.assertEqual(resp.context["advanced_filter_person_ids"], (person.id,))
        titles = {item.title for item in resp.context["items"]}
        self.assertEqual(titles, {"Both tag and person"})
        html = resp.content.decode("utf-8")
        self.assertIn('id="archive-filter-tag"', html)
        self.assertIn(reverse("archive-tag-browse", kwargs={"tag_id": tag.id}), html)

    def test_person_ordering_is_name_then_id(self):
        later = Person.objects.create(name="Zed")
        earlier = Person.objects.create(name="Amy")
        same_a = Person.objects.create(name="Same")
        same_b = Person.objects.create(name="Same")
        item = _public_manual("Order item")
        for person in (later, earlier, same_a, same_b):
            ArchiveItemPerson.objects.create(archive_item=item, person=person)

        resp = self.client.get(self.url, {"advanced": "1"})
        names_and_ids = [
            (p.name, p.pk) for p in resp.context["advanced_filter_person_choices"]
        ]
        self.assertEqual(
            names_and_ids,
            sorted(names_and_ids, key=lambda pair: (pair[0], pair[1])),
        )
        self.assertEqual(
            [name for name, _ in names_and_ids], ["Amy", "Same", "Same", "Zed"]
        )
        same_ids = [pk for name, pk in names_and_ids if name == "Same"]
        self.assertEqual(same_ids, sorted(same_ids))
        self.assertEqual(same_ids, [same_a.pk, same_b.pk])
