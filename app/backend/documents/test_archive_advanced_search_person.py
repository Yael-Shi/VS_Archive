"""Public /archive/ advanced Person filter: AIP or renderable PhotoPerson."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemAuthor,
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
    person_public_page_url,
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


def _author_id(item: ArchiveItem) -> int:
    return ArchiveItemAuthor.objects.get(archive_item=item).author_id


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


def _add_photo(
    item: ArchiveItem,
    *,
    position: int = 1,
    upload_status: str = PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
) -> PhotoContent:
    key = (
        f"photos/{item.pk}-{position}/original.jpg"
        if original_file_key is None
        else original_file_key
    )
    return PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=key,
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
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
        if (
            "documents_archiveitemperson" in sql
            or "archive_items" in sql
            or "documents_photoperson" in sql
        ):
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

    def test_photoperson_only_item_matches_person_filter(self):
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
        self.assertEqual(ids, [photo_only.pk])

    def test_dual_aip_and_photoperson_item_appears_once(self):
        person = Person.objects.create(name="Both Relations")
        item = _create_photo_item(title="AIP and PhotoPerson")
        photo = _add_photo(item)
        PhotoPerson.objects.create(photo_content=photo, person=person)
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [item.pk])
        self.assertEqual(ids.count(item.pk), 1)

    def test_multiple_matching_photos_on_same_item_appear_once(self):
        person = Person.objects.create(name="Multi Photo")
        item = _create_photo_item(title="Two matching photos")
        first = _add_photo(item, position=1)
        second = _add_photo(item, position=2)
        PhotoPerson.objects.create(photo_content=first, person=person)
        PhotoPerson.objects.create(photo_content=second, person=person)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [item.pk])
        self.assertEqual(ids.count(item.pk), 1)

    def test_non_renderable_photoperson_does_not_match(self):
        person = Person.objects.create(name="Pending Appearance")
        pending_only = _create_photo_item(title="Pending PhotoPerson only")
        pending_photo = _add_photo(
            pending_only,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        PhotoPerson.objects.create(photo_content=pending_photo, person=person)

        visible = _create_photo_item(title="Visible with pending extra")
        _add_photo(visible, position=1)
        extra = _add_photo(
            visible,
            position=2,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="   ",
        )
        PhotoPerson.objects.create(photo_content=extra, person=person)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [])

    def test_people_present_free_text_does_not_match_person_filter(self):
        person = Person.objects.create(name="Free Text Twin")
        item = _create_photo_item(title="people_present only")
        photo = _add_photo(item)
        photo.people_present = person.name
        photo.save(update_fields=["people_present", "updated_at"])

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters({"person": str(person.id)}),
            )
        )
        self.assertEqual(ids, [])

    def test_repeatable_person_ids_or_across_aip_and_photoperson(self):
        ada = Person.objects.create(name="Ada Mixed")
        charles = Person.objects.create(name="Charles Mixed")
        aip_only = _public_manual("Ada AIP")
        pp_only = _create_photo_item(title="Charles PhotoPerson")
        ArchiveItemPerson.objects.create(archive_item=aip_only, person=ada)
        PhotoPerson.objects.create(photo_content=_add_photo(pp_only), person=charles)

        ids = _ids(
            filter_archive_items_by_advanced_filters(
                ArchiveItem.objects.all(),
                normalize_archive_advanced_filters(
                    [("person", str(ada.id)), ("person", str(charles.id))]
                ),
            )
        )
        self.assertEqual(set(ids), {aip_only.pk, pp_only.pk})
        self.assertEqual(len(ids), 2)

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

    def test_photoperson_only_private_and_restricted_visibility_still_enforced(self):
        person = Person.objects.create(name="Hidden PhotoPerson Filter")
        private = _create_photo_item(
            title="PERSON-PP-PRIVATE-TITLE",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        restricted = _create_photo_item(
            title="PERSON-PP-RESTRICTED-TITLE",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        public = _create_photo_item(title="PERSON-PP-PUBLIC-TITLE")
        PhotoPerson.objects.create(photo_content=_add_photo(private), person=person)
        PhotoPerson.objects.create(photo_content=_add_photo(restricted), person=person)
        PhotoPerson.objects.create(photo_content=_add_photo(public), person=person)

        anon = self.client.get(self.url, {"person": str(person.id)})
        self.assertEqual(anon.status_code, 200)
        anon_html = anon.content.decode("utf-8")
        self.assertIn("PERSON-PP-PUBLIC-TITLE", anon_html)
        self.assertNotIn("PERSON-PP-PRIVATE-TITLE", anon_html)
        self.assertNotIn("PERSON-PP-RESTRICTED-TITLE", anon_html)
        self.assertEqual(anon.context["total_count"], 1)

        self.client.force_login(self.family)
        family = self.client.get(self.url, {"person": str(person.id)})
        family_titles = {item.title for item in family.context["items"]}
        self.assertEqual(
            family_titles, {"PERSON-PP-PUBLIC-TITLE", "PERSON-PP-PRIVATE-TITLE"}
        )
        self.assertNotIn("PERSON-PP-RESTRICTED-TITLE", family_titles)

        self.client.force_login(self.restricted_user)
        restricted_resp = self.client.get(self.url, {"person": str(person.id)})
        restricted_titles = {item.title for item in restricted_resp.context["items"]}
        self.assertEqual(
            restricted_titles,
            {"PERSON-PP-PUBLIC-TITLE", "PERSON-PP-RESTRICTED-TITLE"},
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
        self.assertEqual(person_chip["person_id"], person.id)
        self.assertEqual(person_chip["detail_href"], person_public_page_url(person.id))
        self.assertEqual(person_chip["remove_aria_label"], "הסרת Chip Canonical")
        html = resp.content.decode("utf-8")
        self.assertIn("Chip Canonical", html)
        self.assertNotIn("Chip Alias", person_chip["value"])
        self.assertNotIn("Chip Alias", person_chip["remove_aria_label"])

    def test_multiple_selected_people_render_separate_chips(self):
        ada = Person.objects.create(name="Ada Chip")
        charles = Person.objects.create(name="Charles Chip")
        item = _public_manual("Two person chips")
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        resp = self.client.get(
            self.url,
            [("person", str(ada.id)), ("person", str(charles.id))],
        )
        person_chips = [
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person"
        ]
        self.assertEqual(
            [chip["person_id"] for chip in person_chips], [ada.id, charles.id]
        )
        self.assertEqual(
            [chip["value"] for chip in person_chips], ["Ada Chip", "Charles Chip"]
        )
        html = resp.content.decode("utf-8")
        self.assertEqual(html.count('class="filter-chip filter-chip--person"'), 2)
        self.assertNotIn("Ada Chip, Charles Chip", html)
        self.assertNotIn('<span class="filter-chip__key">אנשים:</span>', html)

    def test_each_person_chip_name_links_to_its_person_page(self):
        ada = Person.objects.create(name="Ada Link")
        charles = Person.objects.create(name="Charles Link")
        item = _public_manual("Name link item")
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        resp = self.client.get(
            self.url,
            [("person", str(ada.id)), ("person", str(charles.id))],
        )
        person_chips = [
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person"
        ]
        self.assertEqual(
            [chip["detail_href"] for chip in person_chips],
            [person_public_page_url(ada.id), person_public_page_url(charles.id)],
        )
        html = resp.content.decode("utf-8")
        self.assertIn(
            f'<a class="filter-chip__person-name" href="{person_public_page_url(ada.id)}">Ada Link</a>',
            html,
        )
        self.assertIn(
            f'<a class="filter-chip__person-name" href="{person_public_page_url(charles.id)}">Charles Link</a>',
            html,
        )

    def test_each_person_chip_remove_drops_only_that_person(self):
        ada = Person.objects.create(name="Ada Chip")
        charles = Person.objects.create(name="Charles Chip")
        item = _public_manual("Remove one person")
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        resp = self.client.get(
            self.url,
            [("person", str(ada.id)), ("person", str(charles.id))],
        )
        person_chips = [
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person"
        ]
        ada_chip = next(chip for chip in person_chips if chip["person_id"] == ada.id)
        charles_chip = next(
            chip for chip in person_chips if chip["person_id"] == charles.id
        )
        ada_parsed = parse_qs(str(ada_chip["remove_href_suffix"]).lstrip("?"))
        charles_parsed = parse_qs(str(charles_chip["remove_href_suffix"]).lstrip("?"))
        self.assertEqual(ada_parsed["person"], [str(charles.id)])
        self.assertEqual(charles_parsed["person"], [str(ada.id)])

        removed_ada = self.client.get(f"{self.url}{ada_chip['remove_href_suffix']}")
        self.assertEqual(
            removed_ada.context["advanced_filter_person_ids"], (charles.id,)
        )
        remaining = [
            chip
            for chip in removed_ada.context["active_filter_chips"]
            if chip["kind"] == "person"
        ]
        self.assertEqual([chip["person_id"] for chip in remaining], [charles.id])

    def test_person_chip_remove_preserves_filters_drops_page_keeps_advanced(self):
        ada = Person.objects.create(name="Ada Keep")
        charles = Person.objects.create(name="Charles Keep")
        cat = ArchiveCategory.objects.create(name="Keep Cat", slug="keep-cat")
        event = ArchiveEvent.objects.create(name="Keep Event", slug="keep-event")
        tag = _create_tag(name="Keep Tag")
        item = _public_manual("Chip keep state", author_name="Keep Author")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Keep Cat"],
            event_names=["Keep Event"],
            tag_names=["Keep Tag"],
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)
        author_id = _author_id(item)

        filters = normalize_archive_advanced_filters(
            [
                ("author", str(author_id)),
                ("category", str(cat.id)),
                ("event", str(event.id)),
                ("tag", str(tag.id)),
                ("person", str(ada.id)),
                ("person", str(charles.id)),
                ("year", "1950"),
                ("year_to", "1960"),
            ]
        )
        summary = archive_public_list_active_filter_summary_context(
            q="KeepQuery",
            item_type_filter="photo",
            per_page=24,
            advanced_filters=filters,
            category_choices=[cat],
            event_choices=[event],
            tag_choices=[tag],
            person_choices=[ada, charles],
        )
        ada_chip = next(
            chip
            for chip in summary["active_filter_chips"]
            if chip["kind"] == "person" and chip["person_id"] == ada.id
        )
        parsed = parse_qs(str(ada_chip["remove_href_suffix"]).lstrip("?"))
        self.assertEqual(parsed["person"], [str(charles.id)])
        self.assertEqual(parsed["q"], ["KeepQuery"])
        self.assertEqual(parsed["author"], [str(author_id)])
        self.assertEqual(parsed["category"], [str(cat.id)])
        self.assertEqual(parsed["event"], [str(event.id)])
        self.assertEqual(parsed["tag"], [str(tag.id)])
        self.assertEqual(parsed["year"], ["1950"])
        self.assertEqual(parsed["year_to"], ["1960"])
        self.assertEqual(parsed["item_type"], ["photo"])
        self.assertEqual(parsed["per_page"], ["24"])
        self.assertEqual(parsed["advanced"], ["1"])
        self.assertNotIn("page", parsed)

        resp = self.client.get(
            self.url,
            [
                ("q", "KeepQuery"),
                ("author", str(author_id)),
                ("category", str(cat.id)),
                ("event", str(event.id)),
                ("tag", str(tag.id)),
                ("person", str(ada.id)),
                ("person", str(charles.id)),
                ("year", "1950"),
                ("year_to", "1960"),
                ("item_type", "photo"),
                ("per_page", "24"),
                ("page", "2"),
            ],
        )
        live_chip = next(
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person" and chip["person_id"] == ada.id
        )
        live_parsed = parse_qs(str(live_chip["remove_href_suffix"]).lstrip("?"))
        self.assertEqual(live_parsed["person"], [str(charles.id)])
        self.assertEqual(live_parsed["q"], ["KeepQuery"])
        self.assertEqual(live_parsed["author"], [str(author_id)])
        self.assertEqual(live_parsed["category"], [str(cat.id)])
        self.assertEqual(live_parsed["event"], [str(event.id)])
        self.assertEqual(live_parsed["tag"], [str(tag.id)])
        self.assertEqual(live_parsed["year"], ["1950"])
        self.assertEqual(live_parsed["year_to"], ["1960"])
        self.assertEqual(live_parsed["item_type"], ["photo"])
        self.assertEqual(live_parsed["per_page"], ["24"])
        self.assertEqual(live_parsed["advanced"], ["1"])
        self.assertNotIn("page", live_parsed)

    def test_removing_final_person_keeps_advanced_search_open(self):
        person = Person.objects.create(name="Last Chip")
        item = _public_manual("Last person chip")
        ArchiveItemPerson.objects.create(archive_item=item, person=person)

        resp = self.client.get(self.url, {"person": str(person.id), "page": "2"})
        person_chip = next(
            chip
            for chip in resp.context["active_filter_chips"]
            if chip["kind"] == "person"
        )
        parsed = parse_qs(str(person_chip["remove_href_suffix"]).lstrip("?"))
        self.assertNotIn("person", parsed)
        self.assertEqual(parsed["advanced"], ["1"])
        self.assertNotIn("page", parsed)

        cleared = self.client.get(f"{self.url}{person_chip['remove_href_suffix']}")
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.context["advanced_filter_person_ids"], ())
        self.assertTrue(cleared.context["advanced_panel_open"])
        self.assertIn('id="archive-filter-person"', cleared.content.decode("utf-8"))

    def test_person_chip_remove_aria_label_includes_name(self):
        ada = Person.objects.create(name="Ada Accessible")
        charles = Person.objects.create(name="Charles Accessible")
        item = _public_manual("Accessible chips")
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)

        resp = self.client.get(
            self.url,
            [("person", str(ada.id)), ("person", str(charles.id))],
        )
        html = resp.content.decode("utf-8")
        for person in (ada, charles):
            chip = next(
                item
                for item in resp.context["active_filter_chips"]
                if item["kind"] == "person" and item["person_id"] == person.id
            )
            self.assertEqual(chip["remove_aria_label"], f"הסרת {person.name}")
            self.assertIn(f'aria-label="הסרת {person.name}"', html)
            self.assertIn('class="filter-chip__remove"', html)
            self.assertIn("×", html)

    def test_non_person_chips_remain_grouped_action_links(self):
        ada = Person.objects.create(name="Ada Other")
        charles = Person.objects.create(name="Charles Other")
        cat_a = ArchiveCategory.objects.create(name="Chip Cat A", slug="chip-cat-a")
        cat_b = ArchiveCategory.objects.create(name="Chip Cat B", slug="chip-cat-b")
        event = ArchiveEvent.objects.create(name="Chip Event", slug="chip-event")
        tag_a = _create_tag(name="Chip Tag A")
        tag_b = _create_tag(name="Chip Tag B")
        item = _public_manual("Unrelated chips item", author_name="Chip Author")
        update_archive_item_discovery_metadata(
            item,
            category_names=["Chip Cat A", "Chip Cat B"],
            event_names=["Chip Event"],
            tag_names=["Chip Tag A", "Chip Tag B"],
        )
        ArchiveItemPerson.objects.create(archive_item=item, person=ada)
        ArchiveItemPerson.objects.create(archive_item=item, person=charles)
        author_id = _author_id(item)

        resp = self.client.get(
            self.url,
            [
                ("q", "ChipQuery"),
                ("author", str(author_id)),
                ("category", str(cat_a.id)),
                ("category", str(cat_b.id)),
                ("event", str(event.id)),
                ("tag", str(tag_a.id)),
                ("tag", str(tag_b.id)),
                ("person", str(ada.id)),
                ("person", str(charles.id)),
            ],
        )
        chips = resp.context["active_filter_chips"]
        category_chip = next(chip for chip in chips if chip["kind"] == "category")
        event_chip = next(chip for chip in chips if chip["kind"] == "event")
        tag_chip = next(chip for chip in chips if chip["kind"] == "tag")
        author_chip = next(chip for chip in chips if chip["kind"] == "author")
        q_chip = next(chip for chip in chips if chip["kind"] == "q")
        person_chips = [chip for chip in chips if chip["kind"] == "person"]

        self.assertEqual(len([chip for chip in chips if chip["kind"] == "category"]), 1)
        self.assertEqual(category_chip["value"], "Chip Cat A, Chip Cat B")
        self.assertNotIn("detail_href", category_chip)
        self.assertNotIn("remove_aria_label", category_chip)
        self.assertNotIn("person_id", category_chip)
        self.assertEqual(event_chip["value"], "Chip Event")
        self.assertEqual(tag_chip["value"], "Chip Tag A, Chip Tag B")
        self.assertEqual(author_chip["value"], "Chip Author")
        self.assertEqual(q_chip["value"], "ChipQuery")
        self.assertEqual(len(person_chips), 2)

        category_remove = parse_qs(str(category_chip["remove_href_suffix"]).lstrip("?"))
        self.assertNotIn("category", category_remove)
        self.assertEqual(category_remove["person"], [str(ada.id), str(charles.id)])
        self.assertNotIn("advanced", category_remove)

        html = resp.content.decode("utf-8")
        self.assertIn('title="הסרת קטגוריות"', html)
        self.assertIn('title="הסרת תגיות"', html)
        self.assertIn('title="הסרת אירוע"', html)
        self.assertIn('title="הסרת מחבר/ת"', html)
        self.assertIn('title="הסרת חיפוש"', html)
        self.assertEqual(html.count('class="filter-chip__remove"'), 2)
        self.assertEqual(html.count('class="filter-chip filter-chip--person"'), 2)
        self.assertNotIn("filter-chip--person", str(category_chip))

    def test_person_chip_html_query_count_does_not_scale_with_selected_people(self):
        few_item = _public_manual("Chip query few")
        many_item = _public_manual("Chip query many")
        few_people = []
        many_people = []
        for index in range(2):
            person = Person.objects.create(name=f"ChipFew {index:02d}")
            ArchiveItemPerson.objects.create(archive_item=few_item, person=person)
            few_people.append(person)
        for index in range(8):
            person = Person.objects.create(name=f"ChipMany {index:02d}")
            ArchiveItemPerson.objects.create(archive_item=many_item, person=person)
            many_people.append(person)

        few_params = [("person", str(person.id)) for person in few_people]
        many_params = [("person", str(person.id)) for person in many_people]
        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(self.url, few_params)
        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(self.url, many_params)
        self.assertEqual(few_resp.status_code, 200)
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(len(few_resp.context["active_filter_chips"]), 2)
        self.assertEqual(len(many_resp.context["active_filter_chips"]), 8)
        self.assertEqual(
            _person_related_query_count(few_ctx),
            _person_related_query_count(many_ctx),
        )

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
        self.assertTrue(
            any(
                "documents_photoperson" in query["sql"].lower().replace('"', "")
                for query in few_ctx.captured_queries
            )
        )
        self.assertGreaterEqual(len(few_choices["advanced_filter_person_choices"]), 22)
        self.assertGreaterEqual(len(many_choices["advanced_filter_person_choices"]), 22)

        with CaptureQueriesContext(connection) as request_ctx:
            resp = self.client.get(self.url, {"advanced": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_person_related_query_count(request_ctx), 2)
        self.assertTrue(
            any(
                "documents_person" in query["sql"].lower().replace('"', "")
                and "documents_photoperson" in query["sql"].lower().replace('"', "")
                for query in request_ctx.captured_queries
            )
        )

    def test_photoperson_only_person_is_a_choice(self):
        linked_person = Person.objects.create(name="Linked Choice")
        appearance_only = Person.objects.create(name="Appearance Only")
        PersonAlias.objects.create(person=appearance_only, name="Appearance Alias")
        linked_item = _public_manual("Has ArchiveItemPerson")
        ArchiveItemPerson.objects.create(archive_item=linked_item, person=linked_person)
        photo_item = _create_photo_item(title="Appearance photo")
        photo = _add_photo(photo_item)
        PhotoPerson.objects.create(photo_content=photo, person=appearance_only)

        resp = self.client.get(self.url, {"advanced": "1"})
        names = [p.name for p in resp.context["advanced_filter_person_choices"]]
        self.assertEqual(names, ["Appearance Only", "Linked Choice"])
        html = resp.content.decode("utf-8")
        self.assertIn("Appearance Only", html)
        self.assertNotIn("Appearance Alias", html)

    def test_picker_excludes_inaccessible_and_non_renderable_photoperson_only(self):
        public_person = Person.objects.create(name="Public Appearance")
        private_person = Person.objects.create(name="Private Appearance")
        pending_person = Person.objects.create(name="Pending Appearance")
        PhotoPerson.objects.create(
            photo_content=_add_photo(_create_photo_item(title="Public PP")),
            person=public_person,
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(
                _create_photo_item(
                    title="Private PP",
                    visibility=ArchiveItem.Visibility.PRIVATE,
                )
            ),
            person=private_person,
        )
        PhotoPerson.objects.create(
            photo_content=_add_photo(
                _create_photo_item(title="Pending PP"),
                upload_status=PhotoContent.UploadStatus.PENDING,
                original_file_key="",
            ),
            person=pending_person,
        )

        resp = self.client.get(self.url, {"advanced": "1"})
        names = [p.name for p in resp.context["advanced_filter_person_choices"]]
        self.assertEqual(names, ["Public Appearance"])
        self.assertNotIn("Private Appearance", names)
        self.assertNotIn("Pending Appearance", names)

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


class ArchivePersonFilterChipCssTests(SimpleTestCase):
    def test_person_chip_css_uses_logical_properties_and_focus(self):
        css = (
            Path(__file__).resolve().parents[1]
            / "public"
            / "static"
            / "public"
            / "app.css"
        )
        text = css.read_text(encoding="utf-8")
        start = text.index(".filter-chip--person {")
        end = text.index(".archive-search-results-count {")
        person_chip_css = text[start:end]
        self.assertIn("padding-inline:", person_chip_css)
        self.assertIn("padding-block:", person_chip_css)
        self.assertIn("min-inline-size: 2.75rem", person_chip_css)
        self.assertIn("min-block-size: 2.75rem", person_chip_css)
        self.assertIn("margin-inline-start:", person_chip_css)
        self.assertIn(".filter-chip__person-name:focus-visible", person_chip_css)
        self.assertIn(".filter-chip__remove:focus-visible", person_chip_css)
        self.assertIn("var(--focus-ring)", person_chip_css)
        self.assertNotIn("margin-left", person_chip_css)
        self.assertNotIn("margin-right", person_chip_css)
        self.assertNotIn("padding-left", person_chip_css)
        self.assertNotIn("padding-right", person_chip_css)
