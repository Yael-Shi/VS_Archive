"""Public Stage A presentation: ArchiveItemPerson on cards/detail; hide mapped Tags."""

from __future__ import annotations

from html import escape
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.historical_person_tag_map import historical_person_name_tag_ids
from documents.models import (
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemPerson,
    Document,
    Person,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_presentation import (
    build_archive_browse_card,
    person_public_page_url,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_video_archive_item,
)
from documents.tag_pk_sequence_support import (
    ensure_tag_pk_sequence_past_historical_ids as _reset_tag_pk_sequence,
)
from documents.test_archive_item import create_viewable_ocr_document
from documents.test_historical_person_tag_reuse import _create_tag

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
PRESIGNED_URL = "https://s3.example/presigned/photo"
MAPPED_TAG_ID = next(iter(sorted(historical_person_name_tag_ids())))


def _mapped_historical_tag(*, name: str = "mapped-historical-person-tag") -> Tag:
    tag = Tag.objects.create(pk=MAPPED_TAG_ID, name=name)
    _reset_tag_pk_sequence()
    return tag


def _public_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _public_ocr(title: str):
    return create_viewable_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        visibility=Document.Visibility.PUBLIC,
    )


def _public_video(title: str) -> ArchiveItem:
    return create_video_archive_item(
        title=title,
        source_url=YOUTUBE_URL,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _public_photo(title: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )
    _add_uploaded_photo(item)
    return item


def _add_uploaded_photo(item: ArchiveItem, *, position: int = 1) -> PhotoContent:
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        upload_error="",
    )
    photo.original_file_key = f"photos/{photo.id}/original.jpg"
    photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


def _link_person(item: ArchiveItem, person: Person) -> None:
    ArchiveItemPerson.objects.create(archive_item=item, person=person)


def _person_href(person: Person) -> str:
    return person_public_page_url(person.id)


def _person_href_html(person: Person) -> str:
    return escape(_person_href(person))


def _people_select_query_count(captured_queries) -> int:
    count = 0
    for query in captured_queries:
        sql = query["sql"].lower().replace('"', "")
        if not sql.lstrip().startswith("select"):
            continue
        if "documents_photoperson" in sql:
            continue
        if "documents_archiveitemperson" in sql or "documents_person" in sql:
            count += 1
    return count


class ArchivePersonPublicCardTests(TestCase):
    def test_all_item_types_render_person_links_on_shared_cards(self):
        person = Person.objects.create(name="Card Linked Person")
        ocr = _public_ocr("OCR person card")
        items = [
            _public_manual("Manual person card"),
            ocr.archive_item,
            _public_video("Video person card"),
            _public_photo("Photo person card"),
        ]
        for item in items:
            _link_person(item, person)

        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים קשורים")
        self.assertNotContains(resp, "אנשים קשורים לפריט")
        self.assertContains(resp, _person_href_html(person), count=4)
        self.assertContains(resp, "Card Linked Person")
        self.assertIn(f"/archive/people/{person.id}/", resp.content.decode())
        for item in items:
            self.assertContains(resp, item.title)

    def test_historical_tag_is_hidden_from_related_links_ordinary_tag_and_event_remain(
        self,
    ):
        item = _public_manual("Hidden historical tag card")
        event = ArchiveEvent.objects.create(name="Keep Event", slug="keep-event")
        ordinary = _create_tag(name="ordinary-keep-tag")
        historical = _mapped_historical_tag(name="hidden-historical-tag")
        item.events.add(event)
        item.tags.add(ordinary, historical)
        person = Person.objects.create(name="Linked Ada")
        _link_person(item, person)

        card = build_archive_browse_card(item)
        self.assertEqual(
            [link.name for link in card.related_links],
            ["Keep Event", "ordinary-keep-tag"],
        )
        self.assertNotIn(
            "hidden-historical-tag", [link.name for link in card.related_links]
        )
        self.assertEqual([link.name for link in card.person_links], ["Linked Ada"])
        self.assertEqual(card.person_links[0].href, _person_href(person))

        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, "קשור ל־")
        self.assertContains(resp, "Keep Event")
        self.assertContains(resp, "ordinary-keep-tag")
        self.assertNotContains(resp, "hidden-historical-tag")
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, _person_href_html(person))

    def test_people_only_item_still_shows_discovery_block(self):
        item = _public_manual("People only card")
        person = Person.objects.create(name="Only Person")
        _link_person(item, person)

        resp = self.client.get(reverse("archive-list"))
        html = resp.content.decode()
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, "Only Person")
        self.assertContains(resp, _person_href_html(person))
        self.assertNotIn("קשור ל־", html)
        self.assertNotIn("קטגוריה:", html)

    def test_duplicate_person_names_remain_distinct_id_links(self):
        item = _public_manual("Duplicate names card")
        first = Person.objects.create(name="Same Name")
        second = Person.objects.create(name="Same Name")
        _link_person(item, second)
        _link_person(item, first)

        card = build_archive_browse_card(item)
        self.assertEqual(
            [link.name for link in card.person_links], ["Same Name", "Same Name"]
        )
        hrefs = [link.href for link in card.person_links]
        self.assertEqual(hrefs, [_person_href(first), _person_href(second)])
        self.assertNotEqual(first.id, second.id)

        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, _person_href_html(first))
        self.assertContains(resp, _person_href_html(second))

    def test_photoperson_only_does_not_appear_on_cards(self):
        item = _public_photo("PhotoPerson only card")
        appearance = Person.objects.create(name="Appearance Only")
        PhotoPerson.objects.create(
            photo_content=item.photo_contents.get(),
            person=appearance,
        )

        card = build_archive_browse_card(item)
        self.assertEqual(card.person_links, ())

        resp = self.client.get(reverse("archive-list"))
        self.assertNotContains(resp, "Appearance Only")
        self.assertNotContains(resp, "אנשים קשורים")


class ArchivePersonPublicDetailTests(TestCase):
    def test_manual_text_detail_shows_people_and_hides_historical_tag(self):
        item = _public_manual("Manual person detail")
        event = ArchiveEvent.objects.create(name="Detail Event", slug="detail-event")
        ordinary = _create_tag(name="detail-ordinary-tag")
        historical = _mapped_historical_tag(name="detail-historical-tag")
        item.events.add(event)
        item.tags.add(ordinary, historical)
        person = Person.objects.create(name="Detail Linked")
        _link_person(item, person)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, "Detail Linked")
        self.assertContains(resp, _person_href_html(person))
        self.assertContains(resp, "תגיות:")
        self.assertContains(resp, "detail-ordinary-tag")
        self.assertContains(resp, "Detail Event")
        self.assertNotContains(resp, "detail-historical-tag")
        self.assertIn(f"/archive/people/{person.id}/", resp.content.decode())

    def test_ocr_document_detail_shows_people_and_hides_historical_tag(self):
        doc = _public_ocr("OCR person detail")
        item = doc.archive_item
        ordinary = _create_tag(name="ocr-ordinary-tag")
        historical = _mapped_historical_tag(name="ocr-historical-tag")
        item.tags.add(ordinary, historical)
        person = Person.objects.create(name="OCR Linked")
        _link_person(item, person)

        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, "OCR Linked")
        self.assertContains(resp, _person_href_html(person))
        self.assertContains(resp, "ocr-ordinary-tag")
        self.assertNotContains(resp, "ocr-historical-tag")

    def test_people_only_detail_renders_discovery_without_tags_heading(self):
        item = _public_manual("People only detail")
        person = Person.objects.create(name="Solo Person")
        _link_person(item, person)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "archive-discovery-meta")
        self.assertContains(resp, "אנשים קשורים")
        self.assertContains(resp, _person_href_html(person))
        self.assertNotContains(resp, "תגיות:")
        self.assertNotContains(resp, "קטגוריות:")
        self.assertNotContains(resp, "אירועים:")

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_photo_identified_people_stay_separate_from_item_people(
        self, _mock_presign
    ):
        item = _public_photo("Photo people split")
        photo = item.photo_contents.get()
        related = Person.objects.create(name="Item Related Person")
        identified = Person.objects.create(name="Photo Identified Person")
        _link_person(item, related)
        PhotoPerson.objects.create(photo_content=photo, person=identified)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertContains(resp, "אנשים קשורים לפריט")
        self.assertContains(resp, "Item Related Person")
        self.assertContains(resp, _person_href_html(related))
        self.assertContains(resp, "אנשים בתמונה")
        self.assertContains(resp, "Photo Identified Person")
        self.assertContains(resp, _person_href_html(identified))
        header = html[html.index("document-detail-header") : html.index("</header>")]
        self.assertIn("אנשים קשורים לפריט", header)
        self.assertIn("Item Related Person", header)
        self.assertIn(_person_href_html(related), header)
        self.assertNotIn("אנשים בתמונה", header)
        self.assertNotIn("Photo Identified Person", header)
        identified_idx = html.index("אנשים בתמונה")
        related_idx = html.index("אנשים קשורים לפריט")
        identified_block = html[identified_idx : html.index("</div>", identified_idx)]
        related_block = html[related_idx : html.index("</div>", related_idx)]
        self.assertIn("Photo Identified Person", identified_block)
        self.assertIn(_person_href_html(identified), identified_block)
        self.assertNotIn("Item Related Person", identified_block)
        self.assertNotIn(_person_href_html(related), identified_block)
        self.assertIn("Item Related Person", related_block)
        self.assertIn(_person_href_html(related), related_block)
        self.assertNotIn("Photo Identified Person", related_block)
        self.assertNotIn(_person_href_html(identified), related_block)
        self.assertNotEqual(
            html.rfind("archive-detail-meta-block--photo", 0, identified_idx),
            -1,
        )

    def test_non_photo_detail_keeps_generic_related_people_heading(self):
        person = Person.objects.create(name="Generic Heading Person")
        manual = _public_manual("Manual people heading")
        video = _public_video("Video people heading")
        ocr = _public_ocr("OCR people heading")
        _link_person(manual, person)
        _link_person(video, person)
        _link_person(ocr.archive_item, person)

        manual_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": manual.id})
        )
        video_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": video.id})
        )
        ocr_resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": ocr.id})
        )
        for resp in (manual_resp, video_resp, ocr_resp):
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, "אנשים קשורים")
            self.assertNotContains(resp, "אנשים קשורים לפריט")
            self.assertNotContains(resp, "אנשים בתמונה")
            self.assertContains(resp, _person_href_html(person))

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_overlapping_photoperson_and_aip_shows_one_list(self, _mock_presign):
        item = _public_photo("Overlap only")
        photo = item.photo_contents.get()
        person = Person.objects.create(name="Both Relations Person")
        PhotoPerson.objects.create(photo_content=photo, person=person)
        _link_person(item, person)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        html = resp.content.decode()
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אנשים בתמונה")
        self.assertContains(resp, "Both Relations Person")
        self.assertContains(resp, _person_href_html(person))
        self.assertNotContains(resp, "אנשים קשורים לפריט")
        self.assertEqual(html.count("Both Relations Person"), 1)

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_overlapping_photoperson_leaves_extra_aip_in_secondary(self, _mock_presign):
        item = _public_photo("Overlap plus extra")
        photo = item.photo_contents.get()
        in_photo = Person.objects.create(name="In Photo Person")
        extra = Person.objects.create(name="Extra Item Person")
        PhotoPerson.objects.create(photo_content=photo, person=in_photo)
        _link_person(item, in_photo)
        _link_person(item, extra)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        html = resp.content.decode()
        self.assertContains(resp, "אנשים בתמונה")
        self.assertContains(resp, "אנשים קשורים לפריט")
        identified_idx = html.index("אנשים בתמונה")
        related_idx = html.index("אנשים קשורים לפריט")
        identified_block = html[identified_idx : html.index("</div>", identified_idx)]
        related_block = html[related_idx : html.index("</div>", related_idx)]
        self.assertIn("In Photo Person", identified_block)
        self.assertIn(_person_href_html(in_photo), identified_block)
        self.assertNotIn("Extra Item Person", identified_block)
        self.assertIn("Extra Item Person", related_block)
        self.assertIn(_person_href_html(extra), related_block)
        self.assertNotIn("In Photo Person", related_block)
        self.assertNotIn(_person_href_html(in_photo), related_block)

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_other_photo_person_stays_in_item_list_on_selected_photo(
        self, _mock_presign
    ):
        item = _public_photo("Multi photo people")
        first = item.photo_contents.get()
        second = _add_uploaded_photo(item, position=2)
        on_second = Person.objects.create(name="Only On Second")
        PhotoPerson.objects.create(photo_content=second, person=on_second)
        _link_person(item, on_second)

        album = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        self.assertEqual(album.status_code, 200)
        self.assertNotContains(album, "אנשים בתמונה")
        self.assertContains(album, "אנשים קשורים לפריט")
        self.assertContains(album, "Only On Second")
        self.assertContains(album, _person_href_html(on_second))

        first_view = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.id}),
            {"photo": str(first.id)},
        )
        html = first_view.content.decode()
        self.assertNotContains(first_view, "אנשים בתמונה")
        self.assertContains(first_view, "אנשים קשורים לפריט")
        self.assertContains(first_view, "Only On Second")
        self.assertIn("אנשים קשורים לפריט", html)
        related_block = html[
            html.index("אנשים קשורים לפריט") : html.index(
                "</div>", html.index("אנשים קשורים לפריט")
            )
        ]
        self.assertIn("Only On Second", related_block)
        self.assertIn(_person_href_html(on_second), related_block)

    @patch("documents.views.create_presigned_get", return_value=PRESIGNED_URL)
    def test_same_canonical_name_distinct_ids_remain_visible(self, _mock_presign):
        item = _public_photo("Same name distinct ids")
        photo = item.photo_contents.get()
        in_photo = Person.objects.create(name="Same Name")
        item_only = Person.objects.create(name="Same Name")
        PhotoPerson.objects.create(photo_content=photo, person=in_photo)
        _link_person(item, in_photo)
        _link_person(item, item_only)

        resp = self.client.get(reverse("archive-detail", kwargs={"item_id": item.id}))
        html = resp.content.decode()
        self.assertContains(resp, "אנשים בתמונה")
        self.assertContains(resp, "אנשים קשורים לפריט")
        identified_idx = html.index("אנשים בתמונה")
        related_idx = html.index("אנשים קשורים לפריט")
        identified_block = html[identified_idx : html.index("</div>", identified_idx)]
        related_block = html[related_idx : html.index("</div>", related_idx)]
        self.assertIn(_person_href_html(in_photo), identified_block)
        self.assertNotIn(_person_href_html(item_only), identified_block)
        self.assertIn(_person_href_html(item_only), related_block)
        self.assertNotIn(_person_href_html(in_photo), related_block)


class ArchivePersonPublicQueryCountTests(TestCase):
    def test_list_people_queries_do_not_grow_per_linked_item(self):
        few_person = Person.objects.create(name="Few Linked")
        for index in range(2):
            item = _public_manual(f"Few people item {index}")
            _link_person(item, few_person)

        with CaptureQueriesContext(connection) as few_ctx:
            few_resp = self.client.get(reverse("archive-list"))
        self.assertEqual(few_resp.status_code, 200)

        many_person = Person.objects.create(name="Many Linked")
        for index in range(4):
            item = _public_manual(f"Many people item {index}")
            _link_person(item, many_person)

        with CaptureQueriesContext(connection) as many_ctx:
            many_resp = self.client.get(reverse("archive-list"))
        self.assertEqual(many_resp.status_code, 200)
        self.assertEqual(
            _people_select_query_count(few_ctx),
            _people_select_query_count(many_ctx),
        )
        self.assertGreaterEqual(_people_select_query_count(few_ctx), 1)
        self.assertContains(many_resp, "Few Linked")
        self.assertContains(many_resp, "Many Linked")
