"""Stage B: mapped Tag browse redirect and public Tag-choice hiding (Option 0)."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.core.management.color import no_style
from django.db import connection
from django.test import TestCase
from django.urls import reverse

from documents.historical_person_tag_map import (
    historical_person_name_tag_ids,
    person_id_for_historical_person_name_tag,
)
from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
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
    archive_browse_queryset_for_user,
)
from documents.services.archive_item_presentation import person_archive_filter_url
from documents.services.archive_items import create_manual_text_archive_item
from documents.test_historical_person_tag_reuse import _create_tag

MAPPED_TAG_ID = next(iter(sorted(historical_person_name_tag_ids())))
MAPPED_PERSON_ID = person_id_for_historical_person_name_tag(MAPPED_TAG_ID)
assert MAPPED_PERSON_ID is not None
MISSING_UNMAPPED_TAG_ID = 9_999_003


def _reset_pk_sequence(model):
    statements = connection.ops.sequence_reset_sql(no_style(), [model])
    with connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)


def _mapped_historical_tag(*, name: str = "mapped-historical-person-tag") -> Tag:
    tag = Tag.objects.create(pk=MAPPED_TAG_ID, name=name)
    _reset_pk_sequence(Tag)
    return tag


def _mapped_person(*, name: str = "Mapped Canonical Person") -> Person:
    person = Person.objects.create(pk=MAPPED_PERSON_ID, name=name)
    _reset_pk_sequence(Person)
    return person


def _public_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _private_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Private body",
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _titles(response) -> set[str]:
    return {item.title for item in response.context["items"]}


def _ids(queryset) -> set[int]:
    return set(queryset.values_list("pk", flat=True))


class ArchivePersonTagStageBRedirectTests(TestCase):
    def test_mapped_tag_browse_redirects_to_person_filter_url(self):
        person = _mapped_person()
        _mapped_historical_tag()
        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": MAPPED_TAG_ID})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], person_archive_filter_url(person.id))
        self.assertEqual(resp["Location"], person_archive_filter_url(MAPPED_PERSON_ID))
        self.assertIn(f"person={MAPPED_PERSON_ID}", resp["Location"])
        self.assertIn("advanced=1", resp["Location"])

    def test_mapped_tag_browse_redirects_when_tag_row_is_missing(self):
        person = _mapped_person()
        self.assertFalse(Tag.objects.filter(pk=MAPPED_TAG_ID).exists())
        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": MAPPED_TAG_ID})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], person_archive_filter_url(person.id))

    def test_redirect_uses_mapped_person_id_not_name(self):
        mapped = _mapped_person(name="Canonical Mapped")
        decoy = Person.objects.create(name="Misleading Duplicate")
        _mapped_historical_tag(name="Misleading Duplicate")
        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": MAPPED_TAG_ID})
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], person_archive_filter_url(mapped.id))
        self.assertNotEqual(resp["Location"], person_archive_filter_url(decoy.id))
        self.assertNotIn(f"person={decoy.id}", resp["Location"])

    def test_ordinary_tag_browse_is_unchanged(self):
        item = _public_manual("Ordinary tag browse visible")
        tag = _create_tag(name="ordinary-browse-tag")
        item.tags.add(tag)
        resp = self.client.get(reverse("archive-tag-browse", kwargs={"tag_id": tag.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תגית: ordinary-browse-tag")
        self.assertContains(resp, item.title)

    def test_missing_unmapped_tag_remains_404(self):
        self.assertFalse(Tag.objects.filter(pk=MISSING_UNMAPPED_TAG_ID).exists())
        self.assertIsNone(
            person_id_for_historical_person_name_tag(MISSING_UNMAPPED_TAG_ID)
        )
        resp = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": MISSING_UNMAPPED_TAG_ID})
        )
        self.assertEqual(resp.status_code, 404)


class ArchivePersonTagStageBChoiceTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family = User.objects.create_user(username="stage-b-family", password="x")
        self.family.groups.add(self.family_group)

    def test_mapped_tags_hidden_from_authorized_choices_ordinary_visibility_unchanged(
        self,
    ):
        mapped_tag = _mapped_historical_tag(name="mapped-choice-tag")
        public_tag = _create_tag(name="ordinary-public-choice-tag")
        private_tag = _create_tag(name="ordinary-private-choice-tag")
        public_item = _public_manual("Public choice item")
        public_item.tags.add(mapped_tag, public_tag)
        private_item = _private_manual("Private choice item")
        private_item.tags.add(private_tag)

        anon_choices = archive_advanced_filter_choice_context(
            archive_browse_queryset_for_user(None)
        )
        anon_tag_ids = [tag.pk for tag in anon_choices["advanced_filter_tag_choices"]]
        self.assertEqual(anon_tag_ids, [public_tag.pk])
        self.assertNotIn(mapped_tag.pk, anon_tag_ids)
        self.assertNotIn(private_tag.pk, anon_tag_ids)

        family_choices = archive_advanced_filter_choice_context(
            archive_browse_queryset_for_user(self.family)
        )
        family_tag_ids = {
            tag.pk for tag in family_choices["advanced_filter_tag_choices"]
        }
        self.assertEqual(family_tag_ids, {public_tag.pk, private_tag.pk})
        self.assertNotIn(mapped_tag.pk, family_tag_ids)

        anon_http = self.client.get(reverse("archive-list"), {"advanced": "1"})
        self.assertEqual(anon_http.status_code, 200)
        self.assertEqual(
            [tag.pk for tag in anon_http.context["advanced_filter_tag_choices"]],
            [public_tag.pk],
        )

        self.client.force_login(self.family)
        family_http = self.client.get(reverse("archive-list"), {"advanced": "1"})
        self.assertEqual(
            {tag.pk for tag in family_http.context["advanced_filter_tag_choices"]},
            {public_tag.pk, private_tag.pk},
        )


class ArchivePersonTagStageBLegacyQueryTests(TestCase):
    def test_mapped_only_direct_tag_query_keeps_tag_semantics(self):
        mapped_tag = _mapped_historical_tag()
        person = _mapped_person()
        tagged_only = _public_manual("Tagged only")
        tagged_only.tags.add(mapped_tag)
        person_only = _public_manual("Person linked only")
        ArchiveItemPerson.objects.create(archive_item=person_only, person=person)

        filters = normalize_archive_advanced_filters({"tag": str(MAPPED_TAG_ID)})
        self.assertEqual(filters.tag_ids, (MAPPED_TAG_ID,))
        self.assertEqual(filters.person_ids, ())
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            ),
            {tagged_only.pk},
        )

        resp = self.client.get(
            reverse("archive-list"),
            [("tag", str(MAPPED_TAG_ID))],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["advanced_filter_tag_ids"], (MAPPED_TAG_ID,))
        self.assertEqual(resp.context["advanced_filter_person_ids"], ())
        self.assertEqual(_titles(resp), {"Tagged only"})

    def test_mixed_mapped_and_ordinary_tag_query_remains_or_within_tags(self):
        mapped_tag = _mapped_historical_tag()
        ordinary = _create_tag(name="ordinary-mixed-tag")
        mapped_only = _public_manual("Mapped tag only")
        mapped_only.tags.add(mapped_tag)
        ordinary_only = _public_manual("Ordinary tag only")
        ordinary_only.tags.add(ordinary)
        _public_manual("Neither tag")

        filters = normalize_archive_advanced_filters(
            [("tag", str(MAPPED_TAG_ID)), ("tag", str(ordinary.id))]
        )
        self.assertEqual(filters.tag_ids, (MAPPED_TAG_ID, ordinary.id))
        self.assertEqual(filters.person_ids, ())
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            ),
            {mapped_only.pk, ordinary_only.pk},
        )

        resp = self.client.get(
            reverse("archive-list"),
            [("tag", str(MAPPED_TAG_ID)), ("tag", str(ordinary.id))],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.context["advanced_filter_tag_ids"], (MAPPED_TAG_ID, ordinary.id)
        )
        self.assertEqual(resp.context["advanced_filter_person_ids"], ())
        self.assertEqual(_titles(resp), {"Mapped tag only", "Ordinary tag only"})

    def test_mapped_tag_and_person_query_remains_and_between_groups(self):
        mapped_tag = _mapped_historical_tag()
        person = _mapped_person()
        other = Person.objects.create(name="Other Person")
        both = _public_manual("Tag and person")
        both.tags.add(mapped_tag)
        ArchiveItemPerson.objects.create(archive_item=both, person=person)
        tagged_only = _public_manual("Tag only")
        tagged_only.tags.add(mapped_tag)
        person_only = _public_manual("Person only")
        ArchiveItemPerson.objects.create(archive_item=person_only, person=person)

        filters = normalize_archive_advanced_filters(
            [("tag", str(MAPPED_TAG_ID)), ("person", str(other.id))]
        )
        self.assertEqual(filters.tag_ids, (MAPPED_TAG_ID,))
        self.assertEqual(filters.person_ids, (other.id,))
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), filters
                )
            ),
            set(),
        )

        match_filters = normalize_archive_advanced_filters(
            [("tag", str(MAPPED_TAG_ID)), ("person", str(person.id))]
        )
        self.assertEqual(
            _ids(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), match_filters
                )
            ),
            {both.pk},
        )

        resp = self.client.get(
            reverse("archive-list"),
            [("tag", str(MAPPED_TAG_ID)), ("person", str(person.id))],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["advanced_filter_tag_ids"], (MAPPED_TAG_ID,))
        self.assertEqual(resp.context["advanced_filter_person_ids"], (person.id,))
        self.assertEqual(_titles(resp), {"Tag and person"})


class ArchivePersonTagStageBAuthorizationAndPhotoPersonTests(TestCase):
    def test_redirected_person_filter_preserves_authorization(self):
        person = _mapped_person()
        public_item = _public_manual("Public mapped person")
        ArchiveItemPerson.objects.create(archive_item=public_item, person=person)
        private_item = _private_manual("Private mapped person")
        ArchiveItemPerson.objects.create(archive_item=private_item, person=person)

        browse = self.client.get(
            reverse("archive-tag-browse", kwargs={"tag_id": MAPPED_TAG_ID})
        )
        self.assertEqual(browse.status_code, 302)
        self.assertEqual(browse["Location"], person_archive_filter_url(person.id))

        resp = self.client.get(browse["Location"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), {"Public mapped person"})
        self.assertNotIn("Private mapped person", _titles(resp))

    def test_photoperson_only_does_not_match_redirected_person_filter(self):
        person = _mapped_person()
        item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="PhotoPerson only",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        photo = PhotoContent.objects.create(
            archive_item=item,
            position=1,
            original_file_key="",
            original_filename="photo.jpg",
            original_mime_type="image/jpeg",
            original_size_bytes=1024,
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            upload_error="",
        )
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
        PhotoPerson.objects.create(photo_content=photo, person=person)

        resp = self.client.get(person_archive_filter_url(person.id))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("PhotoPerson only", _titles(resp))
        self.assertEqual(list(resp.context["items"]), [])
